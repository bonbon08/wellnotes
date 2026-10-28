"""Dialogs: new notebook and paper template chooser."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from . import render  # noqa: E402
from .model import COVER_COLORS, PAGE_SIZES, PAPER_COLORS, Background, Document, Page  # noqa: E402

PAPERS = [("blank", "Blanko"), ("lined", "Liniert"), ("narrow", "Eng liniert"), ("grid", "Kariert"),
          ("dotted", "Gepunktet")]
PAPER_COLOR_NAMES = [("white", "Weiß"), ("yellow", "Gelblich"), ("gray", "Grau"), ("dark", "Dunkel")]
SIZES = [("a4", "A4 Hochformat"), ("a4-landscape", "A4 Querformat"), ("letter", "US Letter"), ("a5", "A5")]


class PaperChooser(Gtk.Box):
    """Grid of paper previews plus paper colour buttons."""

    def __init__(self, kind="lined", color=PAPER_COLORS["white"]):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.kind = kind
        self.color = tuple(color)
        self._res = render.ResourceCache(Document())
        self.previews = []
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, min_children_per_line=3,
                           column_spacing=8, row_spacing=8, homogeneous=True)
        group = None
        for k, label in PAPERS:
            da = Gtk.DrawingArea(content_width=72, content_height=100)
            da.set_draw_func(self._draw_preview, k)
            self.previews.append(da)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.append(da)
            box.append(Gtk.Label(label=label, css_classes=["caption"]))
            b = Gtk.ToggleButton(child=box, css_classes=["flat", "wn-paper-choice"])
            if group:
                b.set_group(group)
            group = group or b
            b.set_active(k == kind)
            b.connect("toggled", self._on_kind, k)
            flow.append(b)
        self.append(flow)
        colors = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        group = None
        for name, label in PAPER_COLOR_NAMES:
            b = Gtk.ToggleButton(label=label, css_classes=["wn-small"])
            if group:
                b.set_group(group)
            group = group or b
            b.set_active(tuple(PAPER_COLORS[name]) == self.color)
            b.connect("toggled", self._on_color, PAPER_COLORS[name])
            colors.append(b)
        self.append(colors)

    def _on_kind(self, b, k):
        if b.get_active():
            self.kind = k

    def _on_color(self, b, c):
        if b.get_active():
            self.color = tuple(c)
            for da in self.previews:
                da.queue_draw()

    def _draw_preview(self, da, cr, w, h, kind):
        pw, ph = PAGE_SIZES["a4"]
        page = Page(pw, ph, Background(kind, self.color))
        s = min(w / pw, h / ph)
        cr.translate((w - pw * s) / 2, (h - ph * s) / 2)
        cr.scale(s, s)
        render.draw_background(cr, page, self._res)
        cr.rectangle(0, 0, pw, ph)
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.set_line_width(1 / s)
        cr.stroke()


class NewNotebookDialog(Adw.Dialog):
    def __init__(self, on_create):
        super().__init__(title="Neues Notizbuch", content_width=520)
        self.on_create = on_create
        self.cover = COVER_COLORS[0]

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Abbrechen")
        cancel.connect("clicked", lambda b: self.close())
        hb.pack_start(cancel)
        create = Gtk.Button(label="Erstellen", css_classes=["suggested-action"])
        create.connect("clicked", self._create)
        hb.pack_end(create)
        tv.add_top_bar(hb)

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        self.title_row = Adw.EntryRow(title="Titel", text="Unbenanntes Notizbuch")
        self.title_row.connect("entry-activated", self._create)
        g.add(self.title_row)
        self.size_row = Adw.ComboRow(title="Format", model=Gtk.StringList.new([s[1] for s in SIZES]))
        g.add(self.size_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Umschlag")
        covers = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=10,
                             min_children_per_line=5, column_spacing=6, row_spacing=6)
        group = None
        for c in COVER_COLORS:
            b = Gtk.ToggleButton(css_classes=["wn-cover-choice"])
            da = Gtk.DrawingArea(content_width=30, content_height=40)
            da.set_draw_func(self._draw_cover, c)
            b.set_child(da)
            if group:
                b.set_group(group)
            group = group or b
            b.set_active(c == self.cover)
            b.connect("toggled", lambda b, c=c: b.get_active() and setattr(self, "cover", c))
            covers.append(b)
        g.add(covers)
        page.add(g)

        g = Adw.PreferencesGroup(title="Papier")
        self.paper = PaperChooser()
        g.add(self.paper)
        page.add(g)
        tv.set_content(page)
        self.set_child(tv)
        self.title_row.grab_focus()

    @staticmethod
    def _draw_cover(da, cr, w, h, color):
        from gi.repository import Gdk
        rgba = Gdk.RGBA()
        rgba.parse(color)
        cr.rectangle(2, 2, w - 4, h - 4)
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
        cr.fill()
        cr.rectangle(2, 2, 5, h - 4)
        cr.set_source_rgba(0, 0, 0, 0.2)
        cr.fill()

    def _create(self, *a):
        title = self.title_row.get_text().strip() or "Unbenanntes Notizbuch"
        size = SIZES[self.size_row.get_selected()][0]
        self.close()
        self.on_create(title, size, self.paper.kind, self.paper.color, self.cover)


class PaperDialog(Adw.Dialog):
    def __init__(self, kind, color, on_apply):
        super().__init__(title="Papiervorlage", content_width=480)
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        tv.add_top_bar(hb)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=18,
                      margin_start=18, margin_end=18)
        chooser = PaperChooser(kind, color)
        box.append(chooser)
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        one = Gtk.Button(label="Diese Seite")
        allp = Gtk.Button(label="Alle Seiten", css_classes=["suggested-action"])
        one.connect("clicked", lambda b: (on_apply(chooser.kind, chooser.color, False), self.close()))
        allp.connect("clicked", lambda b: (on_apply(chooser.kind, chooser.color, True), self.close()))
        buttons.append(one)
        buttons.append(allp)
        box.append(buttons)
        tv.set_content(box)
        self.set_child(tv)
