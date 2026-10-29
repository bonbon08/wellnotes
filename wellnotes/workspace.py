"""Open notebooks as tabs, optionally in two panes side by side (split view)."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


class Pane(Gtk.Box):
    """One column of the workspace: a tab bar and the notebooks opened in it."""

    def __init__(self, ws):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True)
        self.add_css_class("wn-pane")
        self.view = Adw.TabView(vexpand=True)
        self.bar = Adw.TabBar(view=self.view, autohide=True)
        new = Gtk.Button(icon_name="tab-new-symbolic", tooltip_text="Heft öffnen (Strg+T)", css_classes=["flat"])
        new.connect("clicked", lambda b: ws.window.show_library(self))
        self.bar.set_end_action_widget(new)

        empty = Adw.StatusPage(icon_name="view-dual-symbolic", title="Zweites Heft",
                               description="Ziehe einen Tab hierher oder öffne ein Heft aus der Bibliothek.")
        open_btn = Gtk.Button(label="Heft öffnen…", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        open_btn.connect("clicked", lambda b: ws.window.show_library(self))
        empty.set_child(open_btn)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.view, "tabs")
        self.stack.add_named(empty, "empty")
        self.append(self.bar)
        self.append(self.stack)

        self.view.connect("close-page", ws._on_close_page)
        self.view.connect("notify::selected-page", lambda *a: ws._on_selected(self))
        self.view.connect("page-attached", lambda *a: ws._on_pages_changed())
        self.view.connect("page-detached", lambda *a: ws._on_pages_changed())
        # Any press inside the pane (pen, finger, mouse) makes it the active one; never claims.
        click = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *a: ws.set_active(self))
        self.add_controller(click)

    def editors(self):
        return [self.view.get_nth_page(i).get_child() for i in range(self.view.get_n_pages())]

    def current(self):
        page = self.view.get_selected_page()
        return page.get_child() if page else None


class Workspace(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Hefte", tag="workspace")
        # Like the editor before: no swipe/escape pops, only the explicit back button.
        self.set_can_pop(False)
        self.window = window
        self.target = None  # pane that receives the next notebook opened from the library
        self._fullscreen = False
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True,
                               shrink_start_child=False, shrink_end_child=False)
        self.panes = [Pane(self)]
        self.active = self.panes[0]
        self.paned.set_start_child(self.panes[0])
        self.set_child(self.paned)

    # ------------------------------------------------------------------
    # queries

    @property
    def split(self):
        return len(self.panes) > 1

    def editors(self):
        return [ed for pane in self.panes for ed in pane.editors()]

    def current(self):
        return self.active.current()

    def _locate(self, ed):
        for pane in self.panes:
            for i in range(pane.view.get_n_pages()):
                page = pane.view.get_nth_page(i)
                if page.get_child() is ed:
                    return pane, page
        return None, None

    # ------------------------------------------------------------------
    # tabs

    def add(self, ed):
        pane = self.target if self.target in self.panes else self.active
        self.target = None
        page = pane.view.append(ed)
        page.set_title(ed.doc.title)
        page.set_tooltip(GLib.markup_escape_text(ed.doc.path or ed.doc.title))
        pane.view.set_selected_page(page)
        self.set_active(pane)

    def select(self, ed):
        pane, page = self._locate(ed)
        if page is not None:
            pane.view.set_selected_page(page)
            self.set_active(pane)

    def update_title(self, ed):
        _, page = self._locate(ed)
        if page is not None:
            page.set_title(ed.doc.title)
            page.set_tooltip(GLib.markup_escape_text(ed.doc.path or ed.doc.title))

    def close_editor(self, ed):
        pane, page = self._locate(ed)
        if page is not None:
            pane.view.close_page(page)

    def _on_close_page(self, view, page):
        page.get_child().close()  # saves
        view.close_page_finish(page, True)
        GLib.idle_add(self._after_close)
        return True

    def _after_close(self):
        if not self.editors():
            self.window.show_library()
        elif self.split and any(not p.editors() for p in self.panes):
            self.set_split(False)
        return False

    def set_active(self, pane):
        if pane not in self.panes:
            return
        self.active = pane
        for p in self.panes:
            if self.split and p is pane:
                p.add_css_class("wn-pane-active")
            else:
                p.remove_css_class("wn-pane-active")
        ed = pane.current()
        if ed is not None and self.get_root() is not None:
            ed.canvas.grab_focus()

    def _on_selected(self, pane):
        if pane is self.active:
            self.set_active(pane)

    def _on_pages_changed(self):
        GLib.idle_add(self._refresh_panes)

    def _refresh_panes(self):
        for k, pane in enumerate(self.panes):
            pane.stack.set_visible_child_name("tabs" if pane.view.get_n_pages() else "empty")
            pane.bar.set_autohide(not self.split)
            if self.split:
                pane.add_css_class("wn-split")
            else:
                pane.remove_css_class("wn-split")
            pane.bar.set_visible(not self._fullscreen)
            # Window buttons only once: on the outer edges of the outermost header bars.
            for ed in pane.editors():
                ed.header.set_show_start_title_buttons(k == 0)
                ed.header.set_show_end_title_buttons(k == len(self.panes) - 1)
        return False

    # ------------------------------------------------------------------
    # split view

    def toggle_split(self, ed=None):
        self.set_split(not self.split, ed)

    def set_split(self, on, ed=None):
        if on == self.split:
            return
        left = self.panes[0]
        if on:
            right = Pane(self)
            self.panes.append(right)
            self.paned.set_end_child(right)
            width = self.paned.get_width()
            if width > 0:
                self.paned.set_position(width // 2)
            else:
                GLib.idle_add(lambda: self.paned.set_position(self.paned.get_width() // 2) and False)
            # Move a notebook over if the left side keeps one; otherwise the right side
            # offers to open one.
            ed = ed or left.current()
            if left.view.get_n_pages() > 1 and ed is not None:
                _, page = self._locate(ed)
                left.view.transfer_page(page, right.view, 0)
                right.view.set_selected_page(right.view.get_nth_page(0))
            self.set_active(right)
        else:
            right = self.panes.pop()
            keep = self.active.current()
            while right.view.get_n_pages():
                left.view.transfer_page(right.view.get_nth_page(0), left.view, left.view.get_n_pages())
            self.paned.set_end_child(None)
            self.set_active(left)
            if keep is not None:
                self.select(keep)
        self._refresh_panes()

    # ------------------------------------------------------------------

    def set_fullscreen(self, fullscreen):
        self._fullscreen = fullscreen
        self._refresh_panes()
