"""Preferences: library location and synchronisation (Nextcloud, Google Drive, folder)."""

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import settings, sync  # noqa: E402

PROVIDERS = [("none", "Aus"), ("nextcloud", "Nextcloud / WebDAV"), ("gdrive", "Google Drive"),
             ("folder", "Ordner (z. B. Sync-Client)")]


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__(title="Einstellungen", content_width=640, content_height=600)
        self.window = window
        self.cfg = settings.get("sync")
        self.add(self._general_page())
        self.add(self._sync_page())
        self._update_visibility()
        self.connect("closed", lambda *a: self.window.sync.reschedule())

    # -- general

    def _general_page(self):
        page = Adw.PreferencesPage(title="Allgemein", icon_name="folder-documents-symbolic")
        g = Adw.PreferencesGroup(title="Bibliothek",
                                 description="Ordner, in dem deine Notizbücher als .goodnotes-Dateien liegen.")
        self.lib_row = Adw.ActionRow(title="Speicherort", subtitle=self.window.library.root)
        self.lib_row.add_css_class("property")
        b = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                       tooltip_text="Ordner wählen")
        b.connect("clicked", lambda *a: self._choose_folder("Bibliotheksordner wählen", self._set_library))
        self.lib_row.add_suffix(b)
        g.add(self.lib_row)
        page.add(g)
        return page

    def _set_library(self, path):
        settings.load()["library"] = path
        settings.save()
        self.lib_row.set_subtitle(path)
        self.window.library.set_root(path)

    def _choose_folder(self, title, cb):
        dlg = Gtk.FileDialog(title=title)

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                cb(f.get_path())
        dlg.select_folder(self.window, None, done)

    # -- sync

    def _sync_page(self):
        page = Adw.PreferencesPage(name="sync", title="Synchronisation", icon_name="emblem-synchronizing-symbolic")
        g = Adw.PreferencesGroup(description="Notizbücher werden in beide Richtungen abgeglichen. Bei Konflikten "
                                             "bleiben beide Versionen erhalten.")
        self.provider_row = Adw.ComboRow(title="Dienst", model=Gtk.StringList.new([p[1] for p in PROVIDERS]))
        ids = [p[0] for p in PROVIDERS]
        self.provider_row.set_selected(ids.index(self.cfg.get("provider", "none")) if self.cfg.get("provider") in ids
                                       else 0)
        self.provider_row.connect("notify::selected", self._on_provider)
        g.add(self.provider_row)
        self.auto_row = Adw.SwitchRow(title="Automatisch synchronisieren",
                                      subtitle="Beim Start, nach dem Schließen eines Notizbuchs und regelmäßig",
                                      active=bool(self.cfg.get("auto")))
        self.auto_row.connect("notify::active", lambda r, p: self._put("auto", r.get_active()))
        g.add(self.auto_row)
        self.interval_row = Adw.SpinRow.new_with_range(1, 120, 1)
        self.interval_row.set_title("Intervall (Minuten)")
        self.interval_row.set_value(int(self.cfg.get("interval") or 5))
        self.interval_row.connect("notify::value", lambda r, p: self._put("interval", int(r.get_value())))
        g.add(self.interval_row)
        page.add(g)

        # Nextcloud
        self.nc_group = Adw.PreferencesGroup(
            title="Nextcloud",
            description="Lege in Nextcloud unter Persönliche Einstellungen → Sicherheit ein App-Passwort an. "
                        "Auch andere WebDAV-Server funktionieren (vollständige WebDAV-URL als Server angeben).")
        self.nc_server = self._entry(self.nc_group, "Server", "nextcloud_server", "https://cloud.example.org")
        self.nc_user = self._entry(self.nc_group, "Benutzername", "nextcloud_user")
        self.nc_pass = Adw.PasswordEntryRow(title="App-Passwort", show_apply_button=True)
        if self._nc_secret():
            self.nc_pass.set_text(self._nc_secret())
        self.nc_pass.connect("apply", lambda r: self._store_nc_password())
        self.nc_group.add(self.nc_pass)
        self._entry(self.nc_group, "Ordner in der Nextcloud", "nextcloud_folder")
        page.add(self.nc_group)

        # Google Drive
        self.gd_group = Adw.PreferencesGroup(title="Google Drive")
        self.gd_auth = Adw.ComboRow(title="Anmeldung", model=Gtk.StringList.new(
            ["GNOME Online-Konten", "Eigene OAuth-Client-ID"]))
        self.gd_auth.set_selected(0 if self.cfg.get("gdrive_auth", "goa") == "goa" else 1)
        self.gd_auth.connect("notify::selected", self._on_gd_auth)
        self.gd_group.add(self.gd_auth)
        self.goa_row = Adw.ActionRow(title="Google-Konto")
        goa_btn = Gtk.Button(label="Online-Konten öffnen", valign=Gtk.Align.CENTER)
        goa_btn.connect("clicked", self._open_goa)
        self.goa_row.add_suffix(goa_btn)
        self.gd_group.add(self.goa_row)
        self.gd_client = self._entry(self.gd_group, "Client-ID", "gdrive_client_id")
        self.gd_secret = Adw.PasswordEntryRow(title="Client-Secret", show_apply_button=True)
        cid = self.cfg.get("gdrive_client_id", "")
        if cid and settings.lookup_secret("gdrive-secret", cid):
            self.gd_secret.set_text(settings.lookup_secret("gdrive-secret", cid))
        self.gd_secret.connect("apply", lambda r: settings.store_secret(
            "gdrive-secret", self.gd_client.get_text().strip(), r.get_text()))
        self.gd_group.add(self.gd_secret)
        self.gd_login = Adw.ActionRow(title="Google-Anmeldung")
        self.gd_login_btn = Gtk.Button(label="Im Browser anmelden", valign=Gtk.Align.CENTER,
                                       css_classes=["suggested-action"])
        self.gd_login_btn.connect("clicked", self._google_login)
        self.gd_login.add_suffix(self.gd_login_btn)
        self.gd_group.add(self.gd_login)
        self.gd_help = Adw.ActionRow(
            title="Client-ID erstellen",
            subtitle="Google Cloud Console → APIs und Dienste: Google Drive API aktivieren, OAuth-Client vom Typ "
                     "„Desktop-App“ anlegen und ID + Secret hier eintragen.")
        self.gd_help.set_subtitle_lines(4)
        link = Gtk.LinkButton(uri="https://console.cloud.google.com/apis/credentials", label="Öffnen",
                              valign=Gtk.Align.CENTER)
        self.gd_help.add_suffix(link)
        self.gd_group.add(self.gd_help)
        self._entry(self.gd_group, "Ordner in Google Drive", "gdrive_folder")
        page.add(self.gd_group)

        # Folder
        self.dir_group = Adw.PreferencesGroup(
            title="Ordner",
            description="Gleicht mit einem beliebigen Ordner ab, z. B. einem Unterordner deines Nextcloud- oder "
                        "Google-Drive-Desktop-Clients oder einem USB-Stick.")
        self.dir_row = Adw.ActionRow(title="Sync-Ordner", subtitle=self.cfg.get("folder_path") or "Nicht gewählt")
        self.dir_row.add_css_class("property")
        b = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        b.connect("clicked", lambda *a: self._choose_folder("Sync-Ordner wählen", self._set_sync_dir))
        self.dir_row.add_suffix(b)
        self.dir_group.add(self.dir_row)
        nc_local = os.path.expanduser("~/Nextcloud")
        if os.path.isdir(nc_local) and not self.cfg.get("folder_path"):
            suggest = Adw.ActionRow(title="Nextcloud-Client gefunden", subtitle=os.path.join(nc_local, "WellNotes"))
            use = Gtk.Button(label="Verwenden", valign=Gtk.Align.CENTER)
            use.connect("clicked", lambda *a: self._set_sync_dir(os.path.join(nc_local, "WellNotes")))
            suggest.add_suffix(use)
            self.dir_group.add(suggest)
        page.add(self.dir_group)

        # Actions
        g = Adw.PreferencesGroup()
        self.status_row = Adw.ActionRow(title="Status", subtitle=self._last_sync_text())
        self.status_row.set_subtitle_lines(3)
        box = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(visible=False)
        box.append(self.spinner)
        test = Gtk.Button(label="Verbindung testen")
        test.connect("clicked", self._test)
        box.append(test)
        now = Gtk.Button(label="Jetzt synchronisieren", css_classes=["suggested-action"])
        now.connect("clicked", self._sync_now)
        box.append(now)
        self.status_row.add_suffix(box)
        g.add(self.status_row)
        self.actions_group = g
        page.add(g)
        return page

    def _entry(self, group, title, key, placeholder=None):
        row = Adw.EntryRow(title=title, text=self.cfg.get(key) or "")
        row.connect("changed", lambda r: self._put(key, r.get_text().strip()))
        group.add(row)
        return row

    def _put(self, key, value):
        settings.put("sync", key, value)

    def _nc_account(self):
        return self.nc_user.get_text().strip() + "@" + self.nc_server.get_text().strip()

    def _nc_secret(self):
        return settings.lookup_secret("nextcloud", self.cfg.get("nextcloud_user", "") + "@" +
                                      self.cfg.get("nextcloud_server", ""))

    def _store_nc_password(self):
        if self.nc_pass.get_text():
            settings.store_secret("nextcloud", self._nc_account(), self.nc_pass.get_text())

    def _on_provider(self, *a):
        self._put("provider", PROVIDERS[self.provider_row.get_selected()][0])
        self._update_visibility()

    def _on_gd_auth(self, *a):
        self._put("gdrive_auth", "goa" if self.gd_auth.get_selected() == 0 else "oauth")
        self._update_visibility()

    def _update_visibility(self):
        p = PROVIDERS[self.provider_row.get_selected()][0]
        self.nc_group.set_visible(p == "nextcloud")
        self.gd_group.set_visible(p == "gdrive")
        self.dir_group.set_visible(p == "folder")
        self.actions_group.set_visible(p != "none")
        self.auto_row.set_sensitive(p != "none")
        self.interval_row.set_sensitive(p != "none")
        goa = self.gd_auth.get_selected() == 0
        self.goa_row.set_visible(goa)
        for w in (self.gd_client, self.gd_secret, self.gd_login, self.gd_help):
            w.set_visible(not goa)
        if p == "gdrive" and goa:
            acc = sync.goa_google_account()
            self.goa_row.set_subtitle(acc.get_account().props.presentation_identity if acc else
                                      "Kein Google-Konto eingerichtet (Einstellungen → Online-Konten, "
                                      "„Dateien“ aktivieren)")
        if p == "gdrive" and not goa:
            cid = self.gd_client.get_text().strip()
            self.gd_login.set_subtitle("Angemeldet" if cid and settings.lookup_secret("gdrive", cid)
                                       else "Nicht angemeldet")

    def _open_goa(self, *a):
        try:
            Gio.Subprocess.new(["gnome-control-center", "online-accounts"], Gio.SubprocessFlags.NONE)
        except GLib.Error:
            self._status("GNOME-Einstellungen nicht gefunden. Nutze stattdessen eine eigene Client-ID.", True)

    def _set_sync_dir(self, path):
        lib = os.path.realpath(self.window.library.root)
        real = os.path.realpath(path)
        if real == lib or real.startswith(lib + os.sep) or lib.startswith(real + os.sep):
            self._status("Der Sync-Ordner darf nicht in der Bibliothek liegen (und umgekehrt).", True)
            return
        self._put("folder_path", path)
        self.dir_row.set_subtitle(path)

    def _google_login(self, *a):
        cid = self.gd_client.get_text().strip()
        secret = self.gd_secret.get_text()
        if not cid:
            self._status("Bitte zuerst die Client-ID eintragen.", True)
            return
        settings.store_secret("gdrive-secret", cid, secret)
        self.gd_login_btn.set_sensitive(False)
        self._status("Browser geöffnet – bitte bei Google anmelden …")

        def work():
            try:
                token = sync.google_login(cid, secret)
                settings.store_secret("gdrive", cid, token)
                GLib.idle_add(finish, None)
            except sync.SyncError as e:
                GLib.idle_add(finish, str(e))

        def finish(err):
            self.gd_login_btn.set_sensitive(True)
            self._status(err or "Bei Google Drive angemeldet.", bool(err))
            self._update_visibility()
            return False
        threading.Thread(target=work, daemon=True).start()

    def _status(self, text, error=False):
        self.status_row.set_subtitle(text)
        if error:
            self.add_toast(Adw.Toast(title=text, timeout=4))

    def _busy(self, on):
        self.spinner.set_visible(on)

    def _test(self, *a):
        self._store_nc_password()
        self._busy(True)
        self._status("Prüfe Verbindung …")

        def done(ok, msg):
            self._busy(False)
            self._status(msg, not ok)
            return False
        self.window.sync.test(done)

    def _sync_now(self, *a):
        self._store_nc_password()
        self.window.sync.sync()
        self._status("Synchronisiere …")

    def _last_sync_text(self):
        import time
        last = self.window.sync.last
        return ("Zuletzt synchronisiert: " + time.strftime("%H:%M", time.localtime(last))) if last else \
            "Noch nicht synchronisiert"

    def on_sync_state(self, running, message, error):
        self._busy(running)
        if not running:
            self._status(message or self._last_sync_text(), error)
