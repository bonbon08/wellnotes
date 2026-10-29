"""Library page: folders and notebooks stored as .goodnotes files on disk."""

import datetime
import os
import shutil
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import fileformat  # noqa: E402
from .dialogs import NewNotebookDialog  # noqa: E402
from .model import PAPER_COLORS  # noqa: E402

IMPORT_EXTS = (".pdf", ".md", ".markdown", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def library_root():
    from . import settings
    root = os.environ.get("WELLNOTES_LIBRARY") or settings.get("library")
    if not root:
        docs = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS) or GLib.get_home_dir()
        root = os.path.join(docs, "WellNotes")
    os.makedirs(root, exist_ok=True)
    return root


def unique_path(folder, name, ext=""):
    name = name.replace("/", "-").strip() or "Unbenannt"
    path = os.path.join(folder, name + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, "%s %d%s" % (name, n, ext))
        n += 1
    return path


class CoverPicture(Gtk.Picture):
    """A picture with a fixed size regardless of its paintable's intrinsic size."""

    def __init__(self, width, height):
        super().__init__(content_fit=Gtk.ContentFit.COVER)
        self._size = (width, height)

    def do_measure(self, orientation, for_size):
        s = self._size[0] if orientation == Gtk.Orientation.HORIZONTAL else self._size[1]
        return s, s, -1, -1


def _fmt_date(ts):
    d = datetime.datetime.fromtimestamp(ts)
    today = datetime.date.today()
    if d.date() == today:
        return "Heute, %s" % d.strftime("%H:%M")
    if d.date() == today - datetime.timedelta(days=1):
        return "Gestern, %s" % d.strftime("%H:%M")
    return d.strftime("%d.%m.%Y")


class Library(Adw.NavigationPage):
    def __init__(self, window):
        super().__init__(title="Dokumente", tag="library")
        self.window = window
        self.root = library_root()
        self.folder = self.root
        self.sort = "date"
        self.query = ""

        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Dokumente")
        hb.set_title_widget(self.title)
        self.up_button = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Übergeordneter Ordner")
        self.up_button.connect("clicked", lambda b: self.open_folder(os.path.dirname(self.folder)))
        hb.pack_start(self.up_button)

        new_menu = Gio.Menu()
        new_menu.append("Notizbuch", "lib.new-notebook")
        new_menu.append("Ordner", "lib.new-folder")
        new_menu.append("Importieren…", "lib.import")
        new_menu.append("Obsidian-Vault / Markdown-Ordner…", "lib.import-folder")
        add = Gtk.MenuButton(icon_name="list-add-symbolic", menu_model=new_menu, tooltip_text="Neu",
                             css_classes=["suggested-action"])
        hb.pack_start(add)

        main_menu = Gio.Menu()
        sort = Gio.Menu()
        sort.append("Nach Datum", "lib.sort::date")
        sort.append("Nach Name", "lib.sort::name")
        main_menu.append_section("Sortieren", sort)
        sec = Gio.Menu()
        sec.append("Einstellungen", "app.preferences")
        sec.append("Bibliotheksordner öffnen", "lib.open-root")
        sec.append("Über WellNotes", "app.about")
        main_menu.append_section(None, sec)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=main_menu, tooltip_text="Menü"))
        self.sync_button = Gtk.Button(tooltip_text="Jetzt synchronisieren")
        self.sync_stack = Gtk.Stack()
        self.sync_stack.add_named(Gtk.Image(icon_name="emblem-synchronizing-symbolic"), "idle")
        self.sync_stack.add_named(Adw.Spinner(), "busy")
        self.sync_button.set_child(self.sync_stack)
        self.sync_button.connect("clicked", lambda b: self.window.sync.sync())
        hb.pack_end(self.sync_button)
        self.search_button = Gtk.ToggleButton(icon_name="system-search-symbolic", tooltip_text="Suchen (Strg+F)")
        hb.pack_end(self.search_button)
        self.open_content = Adw.ButtonContent(icon_name="view-paged-symbolic")
        self.open_button = Gtk.Button(child=self.open_content, tooltip_text="Zurück zu den geöffneten Heften",
                                      visible=False)
        self.open_button.connect("clicked", lambda b: self.window.show_workspace())
        hb.pack_end(self.open_button)
        tv.add_top_bar(hb)

        self.search_bar = Gtk.SearchBar()
        entry = Gtk.SearchEntry(placeholder_text="Dokumente durchsuchen")
        entry.connect("search-changed", self._on_search)
        self.search_bar.set_child(entry)
        self.search_bar.connect_entry(entry)
        self.search_button.bind_property("active", self.search_bar, "search-mode-enabled",
                                         GObject.BindingFlags.BIDIRECTIONAL)
        tv.add_top_bar(self.search_bar)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                valign=Gtk.Align.START, column_spacing=18, row_spacing=24,
                                min_children_per_line=2, max_children_per_line=12,
                                margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        self.flow.connect("child-activated", self._on_activate)
        self.empty = Adw.StatusPage(icon_name="wn-notebook-symbolic", title="Noch keine Dokumente",
                                    description="Erstelle ein Notizbuch oder importiere PDF-, Markdown- "
                                                "oder .goodnotes-Dateien.\nDateien kannst du auch hierher ziehen.")
        btn = Gtk.Button(label="Neues Notizbuch", css_classes=["pill", "suggested-action"],
                         halign=Gtk.Align.CENTER, action_name="lib.new-notebook")
        self.empty.set_child(btn)
        self.stack = Gtk.Stack()
        sw = Gtk.ScrolledWindow(child=self.flow, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.stack.add_named(sw, "grid")
        self.stack.add_named(self.empty, "empty")
        self.banner = Adw.Banner(button_label="Abbrechen")
        self.banner.connect("button-clicked", lambda b: setattr(self, "_cancel_import", True))
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self.banner)
        self.stack.set_vexpand(True)
        outer.append(self.stack)
        self.toast_overlay = Adw.ToastOverlay(child=outer)
        tv.set_content(self.toast_overlay)
        self.set_child(tv)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self._install_actions()
        self.refresh()

        self._monitor = None
        self._watch()

    # ------------------------------------------------------------------

    def _install_actions(self):
        g = Gio.SimpleActionGroup()
        for name, fn in [("new-notebook", self.new_notebook), ("new-folder", self.new_folder),
                         ("import", self.import_dialog), ("import-folder", self.import_folder_dialog),
                         ("open-root", lambda: Gtk.FileLauncher.new(Gio.File.new_for_path(self.folder))
                          .launch(self.window, None, None))]:
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda a, p, fn=fn: fn())
            g.add_action(a)
        sort = Gio.SimpleAction.new_stateful("sort", GLib.VariantType.new("s"), GLib.Variant.new_string("date"))

        def on_sort(a, v):
            a.set_state(v)
            self.sort = v.get_string()
            self.refresh()
        sort.connect("change-state", on_sort)
        g.add_action(sort)
        self.insert_action_group("lib", g)
        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.MANAGED)
        sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                     action=Gtk.CallbackAction.new(lambda *a: self.search_button.set_active(True)
                                                                   or True)))
        sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>n"),
                                     action=Gtk.NamedAction.new("lib.new-notebook")))
        sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>o"),
                                     action=Gtk.NamedAction.new("lib.import")))
        self.add_controller(sc)

    def _watch(self):
        if self._monitor:
            self._monitor.cancel()
        try:
            self._monitor = Gio.File.new_for_path(self.folder).monitor_directory(Gio.FileMonitorFlags.NONE, None)
            self._monitor.set_rate_limit(800)
            self._monitor.connect("changed", self._on_fs_changed)
        except GLib.Error:
            self._monitor = None

    def _on_fs_changed(self, *a):
        if not getattr(self, "_refresh_pending", False):
            self._refresh_pending = True

            def later():
                self._refresh_pending = False
                self.refresh()
                return False
            GLib.timeout_add(600, later)

    def open_folder(self, path):
        if not path.startswith(self.root):
            path = self.root
        self.folder = path
        self._watch()
        self.refresh()

    def _on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.refresh()

    # ------------------------------------------------------------------

    def items(self):
        out = []
        try:
            names = os.listdir(self.folder)
        except OSError:
            names = []
        for n in names:
            if n.startswith("."):
                continue
            p = os.path.join(self.folder, n)
            if os.path.isdir(p):
                out.append(("folder", p))
            elif n.lower().endswith(fileformat.EXT):
                out.append(("doc", p))
        if self.query:
            out = [i for i in out if self.query in os.path.basename(i[1]).lower()]

        def key(i):
            if self.sort == "name":
                return (i[0] != "folder", os.path.basename(i[1]).lower())
            return (i[0] != "folder", -os.path.getmtime(i[1]))
        out.sort(key=key)
        return out

    def refresh(self):
        n = len(self.window.editors)
        self.open_button.set_visible(n > 0)
        self.open_content.set_label("%d offen" % n)
        self.up_button.set_visible(self.folder != self.root)
        rel = os.path.relpath(self.folder, self.root)
        self.title.set_title("Dokumente" if rel == "." else os.path.basename(self.folder))
        self.title.set_subtitle("" if rel == "." else "Dokumente / " + rel.replace(os.sep, " / "))
        self.flow.remove_all()
        items = self.items()
        for kind, path in items:
            self.flow.append(self._card(kind, path))
        self.stack.set_visible_child_name("grid" if items or self.query else "empty")

    def _card(self, kind, path):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)
        name = os.path.basename(path)
        if kind == "doc":
            name = os.path.splitext(name)[0]
            cover = Gtk.Overlay(css_classes=["wn-cover"], halign=Gtk.Align.CENTER)
            frame = Gtk.Box(css_classes=["wn-cover-frame"], overflow=Gtk.Overflow.HIDDEN)
            pic = CoverPicture(150, 200)
            data = fileformat.read_thumbnail(path)
            if data:
                try:
                    pic.set_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
                except GLib.Error:
                    pass
            frame.append(pic)
            cover.set_child(frame)
            color = fileformat.read_cover_color(path)
            if color:
                spine = Gtk.Box(width_request=9, halign=Gtk.Align.START, css_classes=["wn-spine"])
                css = Gtk.CssProvider()
                css.load_from_string("box { background: %s; }" % color)
                spine.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                cover.add_overlay(spine)
            box.append(cover)
            sub = _fmt_date(os.path.getmtime(path))
        else:
            frame = Gtk.Box(css_classes=["wn-folder"], width_request=150, height_request=200)
            img = Gtk.Image(icon_name="folder-symbolic", pixel_size=72, hexpand=True, halign=Gtk.Align.CENTER)
            frame.append(img)
            box.append(frame)
            try:
                n = sum(1 for f in os.listdir(path) if f.endswith(fileformat.EXT) or
                        os.path.isdir(os.path.join(path, f)))
            except OSError:
                n = 0
            sub = "%d %s" % (n, "Element" if n == 1 else "Elemente")
        title = Gtk.Label(label=name, ellipsize=3, max_width_chars=18, css_classes=["heading"])
        box.append(title)
        box.append(Gtk.Label(label=sub, css_classes=["caption", "dim-label"]))
        child = Gtk.FlowBoxChild(child=box)
        child.kind = kind
        child.path = path
        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_context, child)
        child.add_controller(click)
        lp = Gtk.GestureLongPress()
        lp.connect("pressed", lambda g, x, y, c=child: self._on_context(g, 1, x, y, c))
        child.add_controller(lp)
        return child

    def _on_activate(self, flow, child):
        if child.kind == "folder":
            self.open_folder(child.path)
        else:
            self.window.open_document(child.path)

    def _on_context(self, gesture, n, x, y, child):
        menu = Gio.Menu()
        menu.append("Öffnen", "item.open")
        menu.append("Umbenennen…", "item.rename")
        if child.kind == "doc":
            menu.append("Duplizieren", "item.duplicate")
            menu.append("Als PDF exportieren…", "item.export-pdf")
        menu.append("Verschieben…", "item.move")
        menu.append("Löschen", "item.delete")
        g = Gio.SimpleActionGroup()
        for name, fn in [("open", lambda: self._on_activate(None, child)),
                         ("rename", lambda: self.rename_dialog(child.path)),
                         ("duplicate", lambda: self.duplicate(child.path)),
                         ("export-pdf", lambda: self.export_pdf(child.path)),
                         ("move", lambda: self.move_dialog(child.path)),
                         ("delete", lambda: self.delete(child.path))]:
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda a, p, fn=fn: fn())
            g.add_action(a)
        child.insert_action_group("item", g)
        pop = Gtk.PopoverMenu.new_from_model(menu)
        pop.set_parent(child)
        pop.set_has_arrow(False)
        r = Gdk.Rectangle()
        r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
        pop.set_pointing_to(r)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def toast(self, msg):
        self.toast_overlay.add_toast(Adw.Toast(title=msg, timeout=3))

    # ------------------------------------------------------------------
    # operations

    def new_notebook(self):
        def create(title, size, paper, color, cover):
            names = {v: k for k, v in PAPER_COLORS.items()}
            doc = fileformat.new_notebook(title, size, paper, names.get(tuple(color), "white"), cover)
            path = unique_path(self.folder, title, fileformat.EXT)
            doc.path = path
            fileformat.save(doc, path)
            self.refresh()
            self.window.open_document(path, doc)
        NewNotebookDialog(create).present(self.window)

    def new_folder(self):
        entry = Gtk.Entry(text="Neuer Ordner", activates_default=True)
        dlg = Adw.AlertDialog(heading="Neuer Ordner", extra_child=entry)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", "Erstellen")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(d, r):
            if r == "ok" and entry.get_text().strip():
                os.makedirs(unique_path(self.folder, entry.get_text().strip()))
                self.refresh()
        dlg.connect("response", resp)
        dlg.present(self.window)

    def import_dialog(self):
        dlg = Gtk.FileDialog(title="Importieren")
        f = Gtk.FileFilter(name="Unterstützte Dateien")
        for pat in ("*.goodnotes", "*.pdf", "*.md", "*.markdown", "*.png", "*.jpg", "*.jpeg", "*.webp", "*.zip"):
            f.add_pattern(pat)
        f.add_mime_type("application/pdf")
        f.add_mime_type("text/markdown")
        f.add_mime_type("image/*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self.import_paths(paths, open_single=True)
        dlg.open_multiple(self.window, None, done)

    def import_folder_dialog(self):
        dlg = Gtk.FileDialog(title="Obsidian-Vault oder Markdown-Ordner wählen")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.import_bulk(f.get_path())
        dlg.select_folder(self.window, None, done)

    def import_bulk(self, path):
        """Import a folder tree (Obsidian, Logseq, Joplin …) or a ZIP export (Notion) in the background."""
        from . import importers
        if getattr(self, "_importing", False):
            self.toast("Es läuft bereits ein Import")
            return
        self._importing = True
        self._cancel_import = False
        folder = self.folder
        name = os.path.basename(path.rstrip("/"))
        self.banner.set_title("Importiere „%s“ …" % name)
        self.banner.set_revealed(True)

        def progress(done, total, current):
            GLib.idle_add(lambda: self.banner.set_title(
                "Importiere „%s“: %d von %d %s" % (name, done, total, os.path.basename(current))) and False)

        def work():
            try:
                fn = importers.import_zip if path.lower().endswith(".zip") else importers.import_folder
                result = fn(path, folder, progress=progress, cancelled=lambda: self._cancel_import)
                GLib.idle_add(finish, result, None)
            except Exception as e:
                GLib.idle_add(finish, None, e)

        def finish(result, err):
            self._importing = False
            self.banner.set_revealed(False)
            if err:
                self.toast("Import fehlgeschlagen: %s" % err)
            else:
                dest, count, errors = result
                msg = "%d Notizbücher aus „%s“ importiert" % (count, name)
                if errors:
                    msg += " (%d Fehler)" % len(errors)
                    for e in errors[:20]:
                        print("Import:", e)
                self.toast(msg)
            self.refresh()
            return False
        threading.Thread(target=work, daemon=True).start()

    def set_root(self, path):
        os.makedirs(path, exist_ok=True)
        self.root = path
        self.open_folder(path)

    def on_sync_state(self, running, message, error):
        self.sync_stack.set_visible_child_name("busy" if running else "idle")
        self.sync_button.set_sensitive(not running)
        if not running:
            if message:
                self.toast(message)
            self.refresh()

    def update_sync_visibility(self):
        self.sync_button.set_visible(self.window.sync.enabled)

    def import_paths(self, paths, open_single=False):
        created = []
        for p in paths:
            if not p:
                continue
            if os.path.isdir(p) or p.lower().endswith(".zip"):
                self.import_bulk(p)
                continue
            try:
                created.append(self.import_path(p))
            except Exception as e:
                self.toast("Import von %s fehlgeschlagen: %s" % (os.path.basename(p), e))
        self.refresh()
        if open_single and len(created) == 1:
            self.window.open_document(created[0])
        elif created:
            self.toast("%d Datei(en) importiert" % len(created))
        return created

    def import_path(self, path):
        low = path.lower()
        title = fileformat.title_from_path(path)
        if low.endswith(fileformat.EXT):
            dest = unique_path(self.folder, title, fileformat.EXT)
            shutil.copy2(path, dest)
            return dest
        if not low.endswith(IMPORT_EXTS):
            raise ValueError("Dateityp wird nicht unterstützt")
        doc = fileformat.import_file(path)
        dest = unique_path(self.folder, title, fileformat.EXT)
        fileformat.save(doc, dest)
        return dest

    def _on_drop(self, target, value, x, y):
        paths = [f.get_path() for f in value.get_files()]
        self.import_paths(paths, open_single=True)
        return True

    def rename_path(self, path, name):
        if not path or not os.path.exists(path):
            return None
        ext = fileformat.EXT if os.path.isfile(path) else ""
        new = unique_path(os.path.dirname(path), name, ext)
        os.rename(path, new)
        return new

    def rename_dialog(self, path):
        is_doc = os.path.isfile(path)
        current = os.path.splitext(os.path.basename(path))[0] if is_doc else os.path.basename(path)
        entry = Gtk.Entry(text=current, activates_default=True)
        dlg = Adw.AlertDialog(heading="Umbenennen", extra_child=entry)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", "Umbenennen")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(d, r):
            name = entry.get_text().strip()
            if r == "ok" and name and name != current:
                self.window.close_documents(path)
                new = self.rename_path(path, name)
                if is_doc and new:
                    try:
                        doc = fileformat.load(new)
                        doc.title = name
                        fileformat.save(doc, new)
                    except Exception:
                        pass
                self.refresh()
        dlg.connect("response", resp)
        dlg.present(self.window)

    def duplicate(self, path):
        title = fileformat.title_from_path(path) + " Kopie"
        dest = unique_path(os.path.dirname(path), title, fileformat.EXT)
        shutil.copy2(path, dest)
        self.refresh()

    def delete(self, path):
        name = os.path.basename(path)
        dlg = Adw.AlertDialog(heading="„%s“ löschen?" % os.path.splitext(name)[0],
                              body="Das Element wird in den Papierkorb verschoben.")
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("delete", "Löschen")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(d, r):
            if r != "delete":
                return
            self.window.close_documents(path)
            try:
                Gio.File.new_for_path(path).trash(None)
            except GLib.Error:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            self.refresh()
        dlg.connect("response", resp)
        dlg.present(self.window)

    def move_dialog(self, path):
        folders = [self.root]
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                if not (full == path or full.startswith(path + os.sep)):
                    folders.append(full)
        labels = ["Dokumente" if f == self.root else "Dokumente / " +
                  os.path.relpath(f, self.root).replace(os.sep, " / ") for f in folders]
        drop = Gtk.DropDown.new_from_strings(labels)
        dlg = Adw.AlertDialog(heading="Verschieben nach", extra_child=drop)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", "Verschieben")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def resp(d, r):
            if r != "ok":
                return
            target = folders[drop.get_selected()]
            if os.path.dirname(path) == target:
                return
            name = os.path.basename(path)
            base, ext = os.path.splitext(name) if os.path.isfile(path) else (name, "")
            self.window.close_documents(path)
            shutil.move(path, unique_path(target, base, ext))
            self.refresh()
        dlg.connect("response", resp)
        dlg.present(self.window)

    def export_pdf(self, path):
        from . import render
        dlg = Gtk.FileDialog(title="Als PDF exportieren", initial_name=fileformat.title_from_path(path) + ".pdf")

        def done(d, res):
            try:
                out = d.save_finish(res).get_path()
            except GLib.Error:
                return
            try:
                doc = fileformat.load(path)
                render.export_pdf(doc, render.ResourceCache(doc), out)
                self.toast("PDF exportiert")
            except Exception as e:
                self.toast("Export fehlgeschlagen: %s" % e)
        dlg.save(self.window, None, done)
