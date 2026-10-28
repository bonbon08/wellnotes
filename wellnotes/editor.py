"""Notebook editor page: toolbar, page sidebar and canvas."""

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import fileformat, render  # noqa: E402
from .canvas import NotebookCanvas  # noqa: E402
from .dialogs import PaperDialog  # noqa: E402
from .model import Document, Page  # noqa: E402

PEN_COLORS = [(0.08, 0.08, 0.1, 1), (0.13, 0.36, 0.86, 1), (0.85, 0.18, 0.18, 1), (0.1, 0.6, 0.3, 1),
              (0.55, 0.25, 0.75, 1)]
HL_COLORS = [(1.0, 0.86, 0.2, 1), (0.55, 0.9, 0.4, 1), (1.0, 0.55, 0.75, 1), (0.45, 0.8, 1.0, 1),
             (1.0, 0.65, 0.3, 1)]
PEN_WIDTHS = [1.4, 2.4, 4.0]
HL_WIDTHS = [12.0, 22.0, 34.0]
ERASER_SIZES = [6.0, 14.0, 30.0]
TEXT_SIZES = [20.0, 28.0, 40.0]

_clipboard = []  # internal element clipboard shared between editors
CLIP_MARK = "wellnotes:elements"


def _rgba(c):
    r = Gdk.RGBA()
    r.red, r.green, r.blue, r.alpha = c[0], c[1], c[2], c[3] if len(c) > 3 else 1
    return r


class Swatch(Gtk.ToggleButton):
    def __init__(self, color, diameter=18):
        super().__init__()
        self.color = color
        self.add_css_class("wn-swatch")
        self.add_css_class("flat")
        self.da = Gtk.DrawingArea(content_width=diameter + 4, content_height=diameter + 4)
        self.da.set_draw_func(self._draw)
        self.set_child(self.da)
        self.diameter = diameter

    def set_color(self, color):
        self.color = color
        self.da.queue_draw()

    def _draw(self, da, cr, w, h):
        import math
        r = self.diameter / 2
        cr.arc(w / 2, h / 2, r, 0, 2 * math.pi)
        cr.set_source_rgba(*self.color)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.set_line_width(1)
        cr.stroke()


class Dot(Gtk.ToggleButton):
    def __init__(self, size):
        super().__init__()
        self.add_css_class("flat")
        self.size = size
        da = Gtk.DrawingArea(content_width=22, content_height=22)
        da.set_draw_func(self._draw)
        self.set_child(da)

    def _draw(self, da, cr, w, h):
        import math
        fg = self.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
        cr.arc(w / 2, h / 2, self.size, 0, 2 * math.pi)
        cr.fill()


class Editor(Adw.NavigationPage):
    def __init__(self, window, doc: Document):
        super().__init__(title=doc.title, tag="editor")
        self.window = window
        self.doc = doc
        self.tools = window.tools
        self._save_id = 0
        self._saving = False
        self._thumbs = {}
        self._thumb_queue = []
        self._thumb_idle = 0
        self._change_counter = 0
        self._closed = False

        self.canvas = NotebookCanvas(doc, self.tools)
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.connect("changed", self._on_changed)
        self.canvas.connect("page-changed", self._on_page_changed)
        self.canvas.connect("selection-changed", self._on_selection_changed)

        view = Adw.ToolbarView()
        view.add_top_bar(self._build_header())
        view.add_top_bar(self._build_toolbar())
        view.set_top_bar_style(Adw.ToolbarStyle.RAISED)

        overlay = Gtk.Overlay()
        overlay.set_child(self.canvas)
        vbar = Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.canvas.vadj)
        vbar.set_halign(Gtk.Align.END)
        vbar.add_css_class("overlay-indicator")
        overlay.add_overlay(vbar)
        hbar = Gtk.Scrollbar(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.canvas.hadj)
        hbar.set_valign(Gtk.Align.END)
        hbar.add_css_class("overlay-indicator")
        overlay.add_overlay(hbar)
        self.page_label = Gtk.Label(css_classes=["wn-page-pill"])
        self.page_label.set_halign(Gtk.Align.CENTER)
        self.page_label.set_valign(Gtk.Align.END)
        self.page_label.set_margin_bottom(14)
        self.page_label.set_can_target(False)
        overlay.add_overlay(self.page_label)
        self.toast_overlay = Adw.ToastOverlay(child=overlay)

        self.split = Adw.OverlaySplitView()
        self.split.set_sidebar(self._build_sidebar())
        self.split.set_content(self.toast_overlay)
        self.split.set_show_sidebar(False)
        self.split.set_min_sidebar_width(170)
        self.split.set_max_sidebar_width(220)
        self.split.bind_property("show-sidebar", self.sidebar_button, "active",
                                 GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        view.set_content(self.split)
        self.set_child(view)

        self._build_selection_popover()
        self._install_actions()
        self._select_tool(self.tools.tool)
        self._rebuild_sidebar()
        self._update_page_label()
        self.connect("hidden", lambda *a: self.close())

    # ------------------------------------------------------------------
    # UI construction

    def _build_header(self):
        hb = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=self.doc.title)
        hb.set_title_widget(self.title_widget)
        self.sidebar_button = Gtk.ToggleButton(icon_name="view-grid-symbolic", tooltip_text="Seitenübersicht")
        hb.pack_start(self.sidebar_button)

        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Seite danach einfügen", "ed.add-page-after")
        sec.append("Seite davor einfügen", "ed.add-page-before")
        sec.append("Seite duplizieren", "ed.duplicate-page")
        sec.append("Seite löschen", "ed.delete-page")
        sec.append("Papiervorlage ändern…", "ed.page-template")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("PDF einfügen…", "ed.insert-pdf")
        sec.append("Markdown einfügen…", "ed.insert-markdown")
        sec.append("Bild einfügen…", "ed.insert-image")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Mit dem Finger zeichnen", "ed.finger-draw")
        sec.append("Umbenennen…", "ed.rename")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Als PDF exportieren…", "ed.export-pdf")
        sec.append("Als .goodnotes exportieren…", "ed.export-goodnotes")
        menu.append_section(None, sec)
        more = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menü")
        hb.pack_end(more)

        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Neue Seite (Strg+Umschalt+N)",
                         action_name="ed.add-page-after")
        hb.pack_end(add)
        zoom = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="An Breite anpassen (Strg+0)",
                          action_name="ed.zoom-fit")
        hb.pack_end(zoom)
        box = Gtk.Box(css_classes=["linked"])
        box.append(Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Rückgängig (Strg+Z)",
                              action_name="ed.undo"))
        box.append(Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="Wiederholen (Strg+Umschalt+Z)",
                              action_name="ed.redo"))
        hb.pack_end(box)
        return hb

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=4, css_classes=["wn-toolbar"])
        bar.set_halign(Gtk.Align.CENTER)
        self.tool_buttons = {}
        group = None
        for name, icon, tip in [("pen", "wn-pen-symbolic", "Stift (P)"),
                                ("highlighter", "wn-highlighter-symbolic", "Textmarker (H)"),
                                ("eraser", "wn-eraser-symbolic", "Radierer (E)"),
                                ("shape", "wn-shapes-symbolic", "Formen (S)"),
                                ("lasso", "wn-lasso-symbolic", "Lasso (L)"),
                                ("text", "wn-text-symbolic", "Text (T)")]:
            b = Gtk.ToggleButton(icon_name=icon, tooltip_text=tip, css_classes=["flat", "wn-tool"])
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", self._on_tool_toggled, name)
            bar.append(b)
            self.tool_buttons[name] = b
        bar.append(Gtk.Button(icon_name="wn-image-symbolic", tooltip_text="Bild einfügen",
                              css_classes=["flat", "wn-tool"], action_name="ed.insert-image"))
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=6, margin_end=6))
        self.options = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hhomogeneous=False,
                                 interpolate_size=True)
        self.options.add_named(self._color_width_options("pen", PEN_COLORS, PEN_WIDTHS, styles=True), "pen")
        self.options.add_named(self._color_width_options("highlighter", HL_COLORS, HL_WIDTHS, dot_scale=0.35),
                               "highlighter")
        self.options.add_named(self._color_width_options("shape", PEN_COLORS, PEN_WIDTHS), "shape")
        self.options.add_named(self._color_width_options("text", PEN_COLORS, TEXT_SIZES, dot_scale=0.18), "text")
        self.options.add_named(self._eraser_options(), "eraser")
        self.options.add_named(Gtk.Label(label="Elemente umkreisen, um sie auszuwählen",
                                         css_classes=["dim-label"]), "lasso")
        bar.append(self.options)
        return bar

    def _color_width_options(self, tool, colors, widths, styles=False, dot_scale=None):
        box = Gtk.Box(spacing=2)
        attr_color = {"pen": "pen_color", "highlighter": "hl_color", "shape": "shape_color",
                      "text": "text_color"}[tool]
        attr_width = {"pen": "pen_width", "highlighter": "hl_width", "shape": "shape_width",
                      "text": "text_size"}[tool]
        if styles:
            sg = None
            for style, label, tip in [("fountain", "Füller", "Druckempfindlicher Füller"),
                                      ("ball", "Kuli", "Gleichmäßige Linienstärke")]:
                b = Gtk.ToggleButton(label=label, tooltip_text=tip, css_classes=["flat", "wn-small"])
                if sg:
                    b.set_group(sg)
                sg = sg or b
                b.set_active(self.tools.pen_style == style)
                b.connect("toggled", lambda b, s=style: b.get_active() and setattr(self.tools, "pen_style", s))
                box.append(b)
            box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))
        group = None
        current = getattr(self.tools, attr_color)
        swatches = []
        for c in colors:
            s = Swatch(c)
            if group:
                s.set_group(group)
            group = group or s
            s.set_active(tuple(c) == tuple(current))
            s.connect("toggled", self._on_swatch, attr_color)
            box.append(s)
            swatches.append(s)
        custom = Swatch(current if tuple(current) not in [tuple(c) for c in colors] else (0.5, 0.5, 0.5, 1))
        custom.set_group(group)
        custom.set_tooltip_text("Eigene Farbe (Rechtsklick zum Ändern)")
        custom.connect("toggled", self._on_swatch, attr_color)
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", lambda *a, s=custom: self._choose_color(s, attr_color))
        custom.add_controller(click)
        custom.connect("clicked", lambda s: s.get_active() and s.color == (0.5, 0.5, 0.5, 1)
                       and self._choose_color(s, attr_color))
        box.append(custom)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))
        wg = None
        cur_w = getattr(self.tools, attr_width)
        for k, w in enumerate(widths):
            d = Dot(2 + k * 2.2 if dot_scale is None else max(2, w * dot_scale * (0.7 if tool == "text" else 1)))
            d.set_tooltip_text(("Dünn", "Mittel", "Dick")[k] if tool != "text" else ("Klein", "Mittel", "Groß")[k])
            if wg:
                d.set_group(wg)
            wg = wg or d
            d.set_active(abs(w - cur_w) < 1e-6)
            d.connect("toggled", lambda d, w=w: d.get_active() and setattr(self.tools, attr_width, w))
            box.append(d)
        return box

    def _eraser_options(self):
        box = Gtk.Box(spacing=2)
        g = None
        for precise, label in [(False, "Ganzer Strich"), (True, "Präzise")]:
            b = Gtk.ToggleButton(label=label, css_classes=["flat", "wn-small"])
            if g:
                b.set_group(g)
            g = g or b
            b.set_active(self.tools.eraser_precise == precise)
            b.connect("toggled", lambda b, p=precise: b.get_active() and setattr(self.tools, "eraser_precise", p))
            box.append(b)
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))
        wg = None
        for k, s in enumerate(ERASER_SIZES):
            d = Dot(2.5 + k * 2.5)
            d.set_tooltip_text(("Klein", "Mittel", "Groß")[k])
            if wg:
                d.set_group(wg)
            wg = wg or d
            d.set_active(s == self.tools.eraser_size)
            d.connect("toggled", lambda d, s=s: d.get_active() and setattr(self.tools, "eraser_size", s))
            box.append(d)
        return box

    def _on_swatch(self, s, attr):
        if s.get_active():
            setattr(self.tools, attr, tuple(s.color))
            if attr == "text_color" and self.canvas._text_editor:
                self.canvas._text_editor["color"] = tuple(s.color)
                self.canvas._style_text_editor()

    def _choose_color(self, swatch, attr):
        dlg = Gtk.ColorDialog(with_alpha=False, title="Farbe wählen")

        def done(d, res):
            try:
                c = d.choose_rgba_finish(res)
            except GLib.Error:
                return
            swatch.set_color((c.red, c.green, c.blue, 1.0))
            swatch.set_active(True)
            setattr(self.tools, attr, tuple(swatch.color))
        dlg.choose_rgba(self.window, _rgba(swatch.color), None, done)
        return True

    def _build_sidebar(self):
        self.page_list = Gtk.ListBox(css_classes=["navigation-sidebar", "wn-pages"])
        self.page_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.page_list.connect("row-activated", self._on_page_row)
        sw = Gtk.ScrolledWindow(child=self.page_list, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_vexpand(True)
        return sw

    def _build_selection_popover(self):
        pop = Gtk.Popover(autohide=False, has_arrow=True, position=Gtk.PositionType.TOP)
        pop.set_parent(self.canvas)
        box = Gtk.Box(spacing=2)
        for icon, tip, action in [("edit-cut-symbolic", "Ausschneiden", "ed.cut"),
                                  ("edit-copy-symbolic", "Kopieren", "ed.copy"),
                                  ("edit-paste-symbolic", "Duplizieren", "ed.duplicate"),
                                  ("user-trash-symbolic", "Löschen", "ed.delete")]:
            box.append(Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action, css_classes=["flat"]))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))
        for c in PEN_COLORS:
            s = Gtk.Button(css_classes=["flat", "wn-swatch"], tooltip_text="Farbe ändern")
            da = Gtk.DrawingArea(content_width=20, content_height=20)

            def draw(da, cr, w, h, c=c):
                import math
                cr.arc(w / 2, h / 2, 8, 0, 2 * math.pi)
                cr.set_source_rgba(*c)
                cr.fill()
            da.set_draw_func(draw)
            s.set_child(da)
            s.connect("clicked", lambda b, c=c: self.canvas.recolor_selection(c))
            box.append(s)
        pop.set_child(box)
        self.sel_popover = pop

    # ------------------------------------------------------------------
    # actions

    def _install_actions(self):
        group = Gio.SimpleActionGroup()
        self.actions = group

        def add(name, cb, accels=None):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *x: cb())
            group.add_action(a)
            return a

        add("undo", self.undo)
        add("redo", self.redo)
        add("cut", self.cut)
        add("copy", self.copy)
        add("paste", self.paste)
        add("duplicate", self.canvas.duplicate_selection)
        add("delete", self.canvas.delete_selection)
        add("add-page-after", lambda: self.add_page(after=True))
        add("add-page-before", lambda: self.add_page(after=False))
        add("duplicate-page", self.duplicate_page)
        add("delete-page", self.delete_page)
        add("page-template", self.change_template)
        add("insert-pdf", lambda: self._pick_file("PDF einfügen", ["application/pdf"], self._insert_pdf))
        add("insert-markdown", lambda: self._pick_file("Markdown einfügen", ["text/markdown", "text/x-markdown",
                                                                              "text/plain"], self._insert_markdown))
        add("insert-image", lambda: self._pick_file("Bild einfügen", ["image/*"], self._insert_image))
        add("export-pdf", self.export_pdf)
        add("export-goodnotes", self.export_goodnotes)
        add("rename", self.rename)
        add("zoom-fit", self.canvas.zoom_fit)
        add("zoom-in", lambda: self.canvas.set_zoom(self.canvas.zoom * 1.25))
        add("zoom-out", lambda: self.canvas.set_zoom(self.canvas.zoom / 1.25))
        add("next-page", lambda: self.canvas.scroll_to_page(self.canvas.current_page + 1))
        add("prev-page", lambda: self.canvas.scroll_to_page(self.canvas.current_page - 1))
        add("save", lambda: self.save(sync=True))
        for t in ("pen", "highlighter", "eraser", "shape", "lasso", "text"):
            add("tool-" + t, lambda t=t: self._select_tool(t))
        finger = Gio.SimpleAction.new_stateful("finger-draw", None, GLib.Variant.new_boolean(self.tools.finger_draw))

        def toggle_finger(a, v):
            a.set_state(v)
            self.tools.finger_draw = v.get_boolean()
            self.canvas._stylus_seen = False
        finger.connect("change-state", toggle_finger)
        group.add_action(finger)
        self.insert_action_group("ed", group)

        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        sc.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
        for accel, action in [("<Control>z", "undo"), ("<Control><Shift>z", "redo"), ("<Control>y", "redo"),
                              ("<Control>x", "cut"), ("<Control>c", "copy"), ("<Control>v", "paste"),
                              ("<Control>d", "duplicate"), ("Delete", "delete"), ("BackSpace", "delete"),
                              ("<Control><Shift>n", "add-page-after"), ("<Control>0", "zoom-fit"),
                              ("<Control>plus", "zoom-in"), ("<Control>equal", "zoom-in"),
                              ("<Control>minus", "zoom-out"), ("Page_Down", "next-page"),
                              ("Page_Up", "prev-page"), ("<Control>s", "save"), ("<Control>e", "export-pdf"),
                              ("p", "tool-pen"), ("h", "tool-highlighter"), ("e", "tool-eraser"),
                              ("s", "tool-shape"), ("l", "tool-lasso"), ("t", "tool-text")]:
            sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(accel),
                                         action=Gtk.NamedAction.new("ed." + action)))
        self.canvas.add_controller(sc)
        self._update_undo_actions()

    def _update_undo_actions(self):
        self.actions.lookup_action("undo").set_enabled(bool(self.doc.undo_stack))
        self.actions.lookup_action("redo").set_enabled(bool(self.doc.redo_stack))
        has_sel = self.canvas.selection is not None
        for n in ("cut", "copy", "duplicate", "delete"):
            self.actions.lookup_action(n).set_enabled(has_sel)
        self.actions.lookup_action("delete-page").set_enabled(len(self.doc.pages) > 1)

    # ------------------------------------------------------------------
    # tools

    def _select_tool(self, name):
        self.tool_buttons[name].set_active(True)

    def _on_tool_toggled(self, b, name):
        if not b.get_active():
            return
        if self.canvas._text_editor:
            self.canvas.commit_text()
        if name != "lasso":
            self.canvas.clear_selection()
        self.tools.tool = name
        self.options.set_visible_child_name(name)
        self.canvas.queue_draw()

    # ------------------------------------------------------------------
    # change tracking, saving

    def _on_changed(self, *a):
        self._change_counter += 1
        self._update_undo_actions()
        self._schedule_save()
        self._queue_thumb(self.doc.pages[self.canvas.current_page] if self.doc.pages else None)
        if self.canvas.selection:
            GLib.idle_add(self._place_sel_popover)

    def _schedule_save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(1500, self._autosave)

    def _autosave(self):
        self._save_id = 0
        self.save()
        return False

    def _snapshot(self):
        snap = Document(self.doc.title)
        snap.id, snap.cover, snap.created, snap.modified = self.doc.id, self.doc.cover, self.doc.created, \
            self.doc.modified
        snap.pages = [Page(p.width, p.height, p.background.copy(), list(p.elements), p.id) for p in self.doc.pages]
        snap.attachments = dict(self.doc.attachments)
        if not hasattr(self.doc, "_gn_templates"):
            self.doc._gn_templates = {}
        snap._gn_templates = self.doc._gn_templates
        return snap

    def save(self, sync=False):
        if not self.doc.path:
            return
        if not self.doc.dirty and not sync:
            return
        if self._saving:
            self._schedule_save()
            return
        snap = self._snapshot()
        counter = self._change_counter
        path = self.doc.path

        def work():
            err = None
            try:
                fileformat.save(snap, path, render.ResourceCache(snap))
            except Exception as e:  # report any failure to the UI
                err = e
            GLib.idle_add(finish, err)

        def finish(err):
            self._saving = False
            if err:
                self.toast("Speichern fehlgeschlagen: %s" % err)
            elif counter == self._change_counter:
                self.doc.dirty = False
            return False

        self._saving = True
        if sync:
            try:
                fileformat.save(snap, path, render.ResourceCache(snap))
                if counter == self._change_counter:
                    self.doc.dirty = False
            except Exception as e:
                self.toast("Speichern fehlgeschlagen: %s" % e)
            self._saving = False
        else:
            threading.Thread(target=work, daemon=True).start()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_id = 0
        if self.canvas._text_editor:
            self.canvas.commit_text()
        if self.doc.dirty:
            self.save(sync=True)
        self.window.on_editor_closed(self)

    def toast(self, msg):
        self.toast_overlay.add_toast(Adw.Toast(title=msg, timeout=3))

    # ------------------------------------------------------------------
    # undo/redo & clipboard

    def _after_history(self, state):
        if state is None:
            return
        self.canvas.clear_selection()
        if state["structure"] is not None:
            self.canvas.relayout()
            self._rebuild_sidebar()
        for page, _ in state["pages"]:
            self.canvas.invalidate(page)
            self._queue_thumb(page)
        self.canvas.invalidate()
        self._on_changed()

    def undo(self):
        self._after_history(self.doc.undo())

    def redo(self):
        self._after_history(self.doc.redo())

    def copy(self):
        els = self.canvas.selected_elements()
        if not els:
            return
        _clipboard[:] = [(e, {e.att: self.doc.attachments[e.att]} if e.kind == "image" else {}) for e in els]
        self.get_clipboard().set(CLIP_MARK)

    def cut(self):
        self.copy()
        self.canvas.delete_selection()

    def paste(self):
        cb = self.get_clipboard()
        formats = cb.get_formats()
        if formats.contain_gtype(Gdk.Texture) and not formats.contain_mime_type("text/plain"):
            cb.read_texture_async(None, self._paste_texture)
            return

        def got_text(c, res):
            try:
                text = c.read_text_finish(res)
            except GLib.Error:
                text = None
            if text == CLIP_MARK and _clipboard:
                els = []
                for e, atts in _clipboard:
                    for k, v in atts.items():
                        self.doc.attachments.setdefault(k, v)
                    els.append(e)
                self.canvas.paste_elements(els)
            elif formats.contain_gtype(Gdk.Texture):
                cb.read_texture_async(None, self._paste_texture)
            elif text:
                from .model import Text
                page = self.doc.pages[self.canvas.current_page]
                _, cx, cy = self.canvas.to_page(self.canvas.get_width() / 2, self.canvas.get_height() / 2,
                                                index=self.canvas.current_page)
                t = Text(text, max(20, cx - 300), cy, min(600, page.width - 40), self.tools.text_size,
                         self.tools.text_color)
                self.canvas.add_elements(page, [t])
        cb.read_text_async(None, got_text)

    def _paste_texture(self, cb, res):
        try:
            tex = cb.read_texture_finish(res)
        except GLib.Error:
            return
        if tex is None:
            return
        att = self.doc.add_attachment(bytes(tex.save_to_png_bytes().get_data()))
        self.canvas.insert_image(att)

    # ------------------------------------------------------------------
    # selection popover

    def _on_selection_changed(self, canvas, has):
        self._update_undo_actions()
        if has:
            self._place_sel_popover()
        else:
            self.sel_popover.popdown()

    def _place_sel_popover(self):
        if not self.canvas.selection:
            self.sel_popover.popdown()
            return False
        x0, y0, x1, y1 = self.canvas._selection_screen_box()
        r = Gdk.Rectangle()
        r.x, r.y = int((x0 + x1) / 2), int(max(0, y0 - 8))
        r.width, r.height = 1, 1
        self.sel_popover.set_pointing_to(r)
        self.sel_popover.popup()
        return False

    # ------------------------------------------------------------------
    # pages

    def _on_page_changed(self, canvas, index):
        self._update_page_label()
        row = self.page_list.get_row_at_index(index)
        if row and self.page_list.get_selected_row() is not row:
            self.page_list.select_row(row)
        self._update_undo_actions()

    def _update_page_label(self):
        n = len(self.doc.pages)
        i = self.canvas.current_page + 1
        self.page_label.set_label("%d / %d" % (i, n))
        self.title_widget.set_subtitle("Seite %d von %d" % (i, n))

    def _structure_change(self, fn, focus=None):
        token = self.doc.begin(structure=True)
        fn()
        self.doc.commit(token)
        self.canvas.relayout()
        self._rebuild_sidebar()
        self._on_changed()
        if focus is not None:
            GLib.idle_add(lambda: self.canvas.scroll_to_page(focus) and False)
        self._update_page_label()

    def add_page(self, after=True, index=None):
        cur = self.canvas.current_page if index is None else index
        like = self.doc.pages[cur]
        page = like.blank_copy()
        if page.background.kind == "pdf":
            # Continuing after an imported PDF: use a plain page of the same size.
            from .model import Background
            page.background = Background("blank", (1, 1, 1))
        pos = cur + 1 if after else cur
        self._structure_change(lambda: self.doc.pages.insert(pos, page), focus=pos)

    def duplicate_page(self, index=None):
        cur = self.canvas.current_page if index is None else index
        page = self.doc.pages[cur].duplicate()
        self._structure_change(lambda: self.doc.pages.insert(cur + 1, page), focus=cur + 1)

    def delete_page(self, index=None):
        if len(self.doc.pages) <= 1:
            return
        cur = self.canvas.current_page if index is None else index
        self.canvas.clear_selection()
        self._structure_change(lambda: self.doc.pages.pop(cur), focus=max(0, cur - 1))
        self.toast("Seite gelöscht – Strg+Z zum Wiederherstellen")

    def move_page(self, index, delta):
        j = index + delta
        if not 0 <= j < len(self.doc.pages):
            return

        def mv():
            p = self.doc.pages.pop(index)
            self.doc.pages.insert(j, p)
        self._structure_change(mv, focus=j)

    def insert_pages(self, pages):
        pos = self.canvas.current_page + 1
        self._structure_change(lambda: self.doc.pages.__setitem__(slice(pos, pos), pages), focus=pos)

    def change_template(self):
        page = self.doc.pages[self.canvas.current_page]

        def apply(kind, color, all_pages):
            targets = self.doc.pages if all_pages else [page]
            for p in targets:
                if p.background.kind == "pdf":
                    continue
                p.background.kind = kind
                p.background.color = color
                p.touch()
                self._queue_thumb(p)
            self.doc.dirty = True
            self.canvas.invalidate()
            self._on_changed()
        PaperDialog(page.background.kind if page.background.kind != "pdf" else "blank",
                    page.background.color, apply).present(self.window)

    def _rebuild_sidebar(self):
        while (row := self.page_list.get_row_at_index(0)) is not None:
            self.page_list.remove(row)
        for i, page in enumerate(self.doc.pages):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6, margin_bottom=6)
            pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, css_classes=["wn-thumb"])
            pic.set_size_request(120, int(120 * page.height / page.width))
            pic.set_halign(Gtk.Align.CENTER)
            box.append(pic)
            box.append(Gtk.Label(label=str(i + 1), css_classes=["caption", "dim-label"]))
            row = Gtk.ListBoxRow(child=box)
            row.page = page
            row.pic = pic
            click = Gtk.GestureClick(button=3)
            click.connect("pressed", self._on_page_context, row)
            row.add_controller(click)
            self.page_list.append(row)
            cached = self._thumbs.get(page.id)
            if cached and cached[0] == page.version:
                pic.set_paintable(cached[1])
            else:
                self._queue_thumb(page)
        row = self.page_list.get_row_at_index(self.canvas.current_page)
        if row:
            self.page_list.select_row(row)

    def _queue_thumb(self, page):
        if page is None:
            return
        if page not in self._thumb_queue:
            self._thumb_queue.append(page)
        if not self._thumb_idle:
            self._thumb_idle = GLib.timeout_add(400, self._render_thumbs)

    def _render_thumbs(self):
        if not self._thumb_queue:
            self._thumb_idle = 0
            return False
        page = self._thumb_queue.pop(0)
        if self.canvas._mode is not None:
            self._thumb_queue.insert(0, page)
            return True
        cached = self._thumbs.get(page.id)
        if not cached or cached[0] != page.version:
            surf = render.page_surface(page, self.canvas.res, 240 / page.width)
            tex = render.surface_to_texture(surf)
            self._thumbs[page.id] = (page.version, tex)
        tex = self._thumbs[page.id][1]
        i = 0
        while (row := self.page_list.get_row_at_index(i)) is not None:
            if row.page is page:
                row.pic.set_paintable(tex)
            i += 1
        return True

    def _on_page_row(self, lb, row):
        self.canvas.scroll_to_page(row.get_index())
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _on_page_context(self, gesture, n, x, y, row):
        idx = row.get_index()
        menu = Gio.Menu()
        for label, act in [("Seite danach einfügen", "after"), ("Seite davor einfügen", "before"),
                           ("Duplizieren", "dup"), ("Nach oben", "up"), ("Nach unten", "down"),
                           ("Löschen", "del")]:
            menu.append(label, "page.%s" % act)
        grp = Gio.SimpleActionGroup()
        for act, fn in [("after", lambda: self.add_page(True, idx)), ("before", lambda: self.add_page(False, idx)),
                        ("dup", lambda: self.duplicate_page(idx)), ("up", lambda: self.move_page(idx, -1)),
                        ("down", lambda: self.move_page(idx, 1)), ("del", lambda: self.delete_page(idx))]:
            a = Gio.SimpleAction.new(act, None)
            a.connect("activate", lambda *z, fn=fn: fn())
            grp.add_action(a)
        row.insert_action_group("page", grp)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_parent(row)
        pop.set_has_arrow(False)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pop.set_pointing_to(r)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    # ------------------------------------------------------------------
    # files

    def _pick_file(self, title, mimes, cb):
        dlg = Gtk.FileDialog(title=title)
        f = Gtk.FileFilter()
        for m in mimes:
            f.add_mime_type(m)
        if "text/markdown" in mimes:
            for pat in ("*.md", "*.markdown"):
                f.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            try:
                cb(file.get_path())
            except Exception as e:
                self.toast("Fehler: %s" % e)
        dlg.open(self.window, None, done)

    def _insert_pdf(self, path):
        with open(path, "rb") as f:
            self.insert_pages(fileformat.pdf_pages(self.doc, f.read()))

    def _insert_markdown(self, path):
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        self.insert_pages(fileformat.markdown_pages(self.doc, text, os.path.dirname(path)))

    def _insert_image(self, path):
        with open(path, "rb") as f:
            att = self.doc.add_attachment(f.read())
        if not self.canvas.insert_image(att):
            self.toast("Bild konnte nicht geladen werden")
            return
        self._select_tool("lasso")
        page = self.canvas.selection["page"] if self.canvas.selection else None
        if page:
            self.canvas.select(page, self.canvas.selection["ids"])

    def export_pdf(self):
        dlg = Gtk.FileDialog(title="Als PDF exportieren", initial_name=self.doc.title + ".pdf")

        def done(d, res):
            try:
                file = d.save_finish(res)
            except GLib.Error:
                return
            try:
                render.export_pdf(self.doc, self.canvas.res, file.get_path())
                self.toast("PDF exportiert")
            except Exception as e:
                self.toast("Export fehlgeschlagen: %s" % e)
        dlg.save(self.window, None, done)

    def export_goodnotes(self):
        dlg = Gtk.FileDialog(title="Als .goodnotes exportieren", initial_name=self.doc.title + ".goodnotes")

        def done(d, res):
            try:
                file = d.save_finish(res)
            except GLib.Error:
                return
            try:
                fileformat.save(self._snapshot(), file.get_path(), render.ResourceCache(self.doc))
                self.toast("Exportiert")
            except Exception as e:
                self.toast("Export fehlgeschlagen: %s" % e)
        dlg.save(self.window, None, done)

    def rename(self):
        entry = Gtk.Entry(text=self.doc.title, activates_default=True)
        dlg = Adw.AlertDialog(heading="Notizbuch umbenennen", extra_child=entry)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", "Umbenennen")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(d, r):
            name = entry.get_text().strip()
            if r == "ok" and name and name != self.doc.title:
                new_path = self.window.library.rename_path(self.doc.path, name)
                if new_path:
                    self.doc.path = new_path
                self.doc.title = name
                self.doc.dirty = True
                self.title_widget.set_title(name)
                self.set_title(name)
                self._schedule_save()
        dlg.connect("response", resp)
        dlg.present(self.window)
