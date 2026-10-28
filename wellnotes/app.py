"""Application and main window."""

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, fileformat, settings  # noqa: E402
from .canvas import Tools  # noqa: E402
from .editor import Editor  # noqa: E402
from .library import Library  # noqa: E402
from .sync import SyncManager  # noqa: E402

APP_ID = "io.github.wellnotes.WellNotes"
HERE = os.path.dirname(os.path.abspath(__file__))


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="WellNotes")
        self.set_default_size(1280, 860)
        self.tools = Tools()
        self.editors = []
        self.nav = Adw.NavigationView()
        self.library = Library(self)
        self.nav.add(self.library)
        self.set_content(self.nav)
        self.prefs = None
        self.sync = SyncManager(lambda: self.library.root,
                                lambda: [ed.doc.path for ed in self.editors if ed.doc.path],
                                self._on_sync_state)
        self.library.update_sync_visibility()
        self.connect("close-request", self._on_close)
        if self.sync.enabled and settings.get("sync", "auto"):
            GLib.timeout_add_seconds(2, lambda: self.sync.sync(quiet=True) and False)

    def _on_sync_state(self, running, message, error):
        self.library.on_sync_state(running, message, error)
        if self.prefs:
            self.prefs.on_sync_state(running, message, error)

    def show_preferences(self):
        from .prefs import PreferencesDialog
        self.prefs = PreferencesDialog(self)

        def closed(*a):
            self.prefs = None
            self.library.update_sync_visibility()
        self.prefs.connect("closed", closed)
        self.prefs.present(self)

    def open_document(self, path, doc=None):
        for ed in self.editors:
            if ed.doc.path == path:
                self.nav.push(ed)
                return ed
        try:
            doc = doc or fileformat.load(path)
        except Exception as e:
            self.library.toast("Öffnen fehlgeschlagen: %s" % e)
            return None
        doc.path = path
        if not doc.title:
            doc.title = fileformat.title_from_path(path)
        ed = Editor(self, doc)
        self.editors.append(ed)
        self.nav.push(ed)
        return ed

    def on_editor_closed(self, ed):
        if ed in self.editors:
            self.editors.remove(ed)
        self.library.refresh()
        if settings.get("sync", "auto"):
            self.sync.sync(quiet=True)

    def _on_close(self, *a):
        for ed in list(self.editors):
            ed.close()
        return False


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        GLib.set_application_name("WellNotes")
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(HERE, "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.Window.set_default_icon_name(APP_ID)
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._about)
        self.add_action(about)
        prefs = Gio.SimpleAction.new("preferences", None)
        prefs.connect("activate", lambda *a: self.window and self.window.show_preferences())
        self.add_action(prefs)
        self.set_accels_for_action("app.preferences", ["<Control>comma"])
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *a: self.window and self.window.close())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Control>q"])

    def _ensure_window(self):
        if self.window is None:
            self.window = Window(self)
        return self.window

    def do_activate(self):
        self._ensure_window().present()

    def do_open(self, files, n, hint):
        win = self._ensure_window()
        win.present()
        for f in files:
            path = f.get_path()
            if not path:
                continue
            if path.lower().endswith(fileformat.EXT):
                win.open_document(path)
            else:
                created = win.library.import_paths([path])
                if created:
                    win.open_document(created[0])

    def _about(self, *a):
        dlg = Adw.AboutDialog(application_name="WellNotes", application_icon="io.github.wellnotes.WellNotes",
                              version=__version__, developer_name="WellNotes",
                              comments="Handschriftliche Notizen für Linux – kompatibel mit .goodnotes-Dateien.",
                              license_type=Gtk.License.GPL_3_0)
        dlg.present(self.window)


def main():
    return Application().run(sys.argv)
