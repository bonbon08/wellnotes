"""Two-way synchronisation of the library with Nextcloud (WebDAV), Google Drive or a folder.

Only ``.goodnotes`` files (and the folders containing them) are synced.  The
state of the last successful sync is kept in ``<library>/.wellnotes-sync.json``
so that edits and deletions on either side can be told apart:

* changed locally only   -> upload
* changed remotely only  -> download
* changed on both sides  -> local version wins, remote version is kept as a
  "(Konflikt …)" copy on both sides
* deleted on one side and unchanged on the other -> delete (local files go to the trash)
"""

import base64
import datetime
import hashlib
import http.server
import json
import os
import secrets as pysecrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from gi.repository import Gio, GLib

from . import __version__, settings

EXT = ".goodnotes"
STATE_FILE = ".wellnotes-sync.json"
USER_AGENT = "WellNotes/" + __version__


class SyncError(Exception):
    pass


def _request(method, url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise SyncError("Verbindung fehlgeschlagen: %s" % getattr(e, "reason", e))


# --------------------------------------------------------------------------
# backends: list() -> {rel: version}; download(rel, path); upload(rel, path) -> version; delete(rel)


class FolderBackend:
    name = "Ordner"

    def __init__(self, root):
        if not root:
            raise SyncError("Kein Sync-Ordner gewählt")
        self.root = root
        os.makedirs(root, exist_ok=True)

    @staticmethod
    def _version(path):
        st = os.stat(path)
        return "%d-%d" % (st.st_mtime_ns, st.st_size)

    def list(self):
        return {rel: self._version(os.path.join(self.root, rel)) for rel in scan_files(self.root)}

    def download(self, rel, dest):
        shutil.copyfile(os.path.join(self.root, rel), dest)

    def upload(self, rel, src):
        target = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".wellnotes-part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
        return self._version(target)

    def delete(self, rel):
        path = os.path.join(self.root, rel)
        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error:
            os.unlink(path)


class WebDavBackend:
    name = "Nextcloud"
    NS = {"d": "DAV:"}

    def __init__(self, server, user, password, folder):
        if not (server and user and password):
            raise SyncError("Nextcloud: Server, Benutzer und App-Passwort angeben")
        server = server.strip().rstrip("/")
        if "://" not in server:
            server = "https://" + server
        folder = folder.strip("/") or "WellNotes"
        if "/remote.php/" in server or "/dav" in urllib.parse.urlparse(server).path:
            base = server
        else:
            base = "%s/remote.php/dav/files/%s" % (server, urllib.parse.quote(user))
        self.base = base + "/" + "/".join(urllib.parse.quote(p) for p in folder.split("/"))
        token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self.headers = {"Authorization": "Basic " + token}
        self._base_path = urllib.parse.unquote(urllib.parse.urlparse(self.base).path).rstrip("/")
        self._dirs = set()

    def _url(self, rel):
        return self.base + "/" + "/".join(urllib.parse.quote(p) for p in rel.split("/"))

    def _check(self, status, what):
        if status in (401, 403):
            raise SyncError("Nextcloud: Anmeldung abgelehnt (Benutzer/App-Passwort prüfen)")
        if status >= 400:
            raise SyncError("Nextcloud: %s fehlgeschlagen (HTTP %d)" % (what, status))

    def _propfind(self, url, depth):
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getetag/>'
                b'<d:resourcetype/><d:getlastmodified/></d:prop></d:propfind>')
        status, _, data = _request("PROPFIND", url, body, dict(self.headers, Depth=str(depth),
                                                               **{"Content-Type": "application/xml"}))
        return status, data

    def ensure_root(self):
        status, _ = self._propfind(self.base, 0)
        if status == 404:
            self._mkdirs("")
        else:
            self._check(status, "Verbinden")

    def _mkdirs(self, rel_dir):
        """MKCOL every missing folder of the sync folder and ``rel_dir`` below it."""
        u = urllib.parse.urlparse(self.base)
        origin = "%s://%s" % (u.scheme, u.netloc)
        full = self._base_path + ("/" + rel_dir.strip("/") if rel_dir.strip("/") else "")
        marker = "/remote.php/dav/files/"
        if marker in full:
            head, tail = full.split(marker, 1)
            user, _, rest = tail.partition("/")
            path, segments = head + marker + user, rest.split("/")
        else:
            path, segments = "", full.split("/")
        for seg in segments:
            if not seg:
                continue
            path += "/" + seg
            if path in self._dirs:
                continue
            status, _, _ = _request("MKCOL", origin + urllib.parse.quote(path), None, self.headers)
            inside = len(path) > len(self._base_path) or path == self._base_path
            # 405 = exists; parents above the sync folder may not be creatable by us, which is fine.
            if status not in (201, 405, 301, 302) and inside:
                self._check(status, "Ordner anlegen")
            self._dirs.add(path)

    def list(self):
        self.ensure_root()
        out = {}
        todo = [self.base]
        seen = set()
        while todo:
            url = todo.pop()
            if url in seen:
                continue
            seen.add(url)
            status, data = self._propfind(url, 1)
            self._check(status, "Auflisten")
            root = ET.fromstring(data)
            for resp in root.findall("d:response", self.NS):
                href = resp.findtext("d:href", "", self.NS)
                path = urllib.parse.unquote(urllib.parse.urlparse(href).path).rstrip("/")
                rel = path[len(self._base_path):].lstrip("/") if path.startswith(self._base_path) else None
                if not rel:
                    continue
                if any(seg.startswith(".") for seg in rel.split("/")):
                    continue
                prop = resp.find("d:propstat/d:prop", self.NS)
                is_dir = prop is not None and prop.find("d:resourcetype/d:collection", self.NS) is not None
                if is_dir:
                    self._dirs.add(path)
                    child = self._url(rel)
                    if child.rstrip("/") != url.rstrip("/"):
                        todo.append(child)
                elif rel.lower().endswith(EXT):
                    out[rel] = (prop.findtext("d:getetag", "", self.NS) if prop is not None else "") or \
                        (prop.findtext("d:getlastmodified", "", self.NS) if prop is not None else "")
        return out

    def download(self, rel, dest):
        status, _, data = _request("GET", self._url(rel), None, self.headers, timeout=300)
        self._check(status, "Herunterladen")
        with open(dest, "wb") as f:
            f.write(data)

    def upload(self, rel, src):
        if "/" in rel:
            self._mkdirs(rel.rsplit("/", 1)[0])
        with open(src, "rb") as f:
            data = f.read()
        status, headers, _ = _request("PUT", self._url(rel), data,
                                      dict(self.headers, **{"Content-Type": "application/zip"}), timeout=300)
        self._check(status, "Hochladen")
        h = {k.lower(): v for k, v in headers.items()}
        etag = h.get("oc-etag") or h.get("etag")
        if not etag:
            status, body = self._propfind(self._url(rel), 0)
            if status < 400:
                etag = ET.fromstring(body).findtext(".//d:getetag", "", self.NS)
        return etag or str(time.time())

    def delete(self, rel):
        status, _, _ = _request("DELETE", self._url(rel), None, self.headers)
        if status not in (404,):
            self._check(status, "Löschen")


# -- Google Drive --------------------------------------------------------------

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"


def goa_google_account():
    """A GNOME Online Accounts Google account with 'Files' enabled, if any."""
    try:
        import gi
        gi.require_version("Goa", "1.0")
        from gi.repository import Goa
        client = Goa.Client.new_sync(None)
    except (ValueError, ImportError, GLib.Error):
        return None
    for obj in client.get_accounts():
        acc = obj.get_account()
        if acc.props.provider_type == "google" and obj.get_oauth2_based() and obj.get_files() \
                and not acc.props.files_disabled:
            return obj
    return None


def google_login(client_id, client_secret, timeout=300):
    """Run the OAuth installed-app flow (loopback + PKCE) in the browser; returns a refresh token."""
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = pysecrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                result["code"] = q.get("code", [None])[0]
                result["error"] = q.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body style='font-family:sans-serif;padding:3em'><h2>WellNotes</h2>"
                             "<p>Anmeldung abgeschlossen. Du kannst dieses Fenster schließen.</p>"
                             "</body></html>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = "http://127.0.0.1:%d" % server.server_port
    url = GOOGLE_AUTH + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": DRIVE_SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})
    GLib.idle_add(lambda: Gio.AppInfo.launch_default_for_uri(url, None) and False)
    deadline = time.time() + timeout
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result.get("code"):
        raise SyncError("Google-Anmeldung abgebrochen%s" % (": " + result["error"] if result.get("error") else ""))
    status, _, data = _request("POST", GOOGLE_TOKEN, urllib.parse.urlencode({
        "code": result["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    tok = json.loads(data or b"{}")
    if status >= 400 or "refresh_token" not in tok:
        raise SyncError("Google-Anmeldung fehlgeschlagen: %s" % tok.get("error_description", tok.get("error", status)))
    return tok["refresh_token"]


class GoogleDriveBackend:
    name = "Google Drive"

    def __init__(self, folder="WellNotes", goa=None, client_id=None, client_secret=None, refresh_token=None):
        self.folder = folder.strip("/") or "WellNotes"
        self.goa = goa
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        if not goa and not (client_id and refresh_token):
            raise SyncError("Google Drive: bitte zuerst anmelden")
        self._token = None
        self._token_exp = 0
        self.ids = {}  # rel -> file id
        self.dir_ids = {}  # rel dir ("" = root) -> folder id

    def _access_token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        if self.goa:
            try:
                tok, expires = self.goa.get_oauth2_based().call_get_access_token_sync(None)
            except GLib.Error as e:
                raise SyncError("GNOME-Online-Konto: %s" % e.message)
            self._token, self._token_exp = tok, time.time() + (expires or 300)
        else:
            status, _, data = _request("POST", GOOGLE_TOKEN, urllib.parse.urlencode({
                "client_id": self.client_id, "client_secret": self.client_secret or "",
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})
            tok = json.loads(data or b"{}")
            if status >= 400:
                raise SyncError("Google: Token abgelaufen – bitte neu anmelden")
            self._token, self._token_exp = tok["access_token"], time.time() + tok.get("expires_in", 3600)
        return self._token

    def _api(self, method, url, body=None, headers=None, raw=False, timeout=60):
        h = {"Authorization": "Bearer " + self._access_token()}
        h.update(headers or {})
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        status, _, data = _request(method, url, body, h, timeout)
        if status in (401, 403):
            raise SyncError("Google Drive: Zugriff verweigert (%d)" % status)
        if status >= 400:
            raise SyncError("Google Drive: HTTP %d %s" % (status, data[:200].decode(errors="replace")))
        return data if raw else json.loads(data or b"{}")

    def _children(self, folder_id):
        out = []
        page = None
        while True:
            q = {"q": "'%s' in parents and trashed=false" % folder_id, "pageSize": 1000,
                 "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,modifiedTime)"}
            if page:
                q["pageToken"] = page
            r = self._api("GET", DRIVE + "/files?" + urllib.parse.urlencode(q))
            out += r.get("files", [])
            page = r.get("nextPageToken")
            if not page:
                return out

    def _find_or_create(self, name, parent):
        esc = name.replace("\\", "\\\\").replace("'", "\\'")
        q = "name='%s' and mimeType='%s' and '%s' in parents and trashed=false" % (esc, FOLDER_MIME, parent)
        r = self._api("GET", DRIVE + "/files?" + urllib.parse.urlencode({"q": q, "fields": "files(id)"}))
        if r.get("files"):
            return r["files"][0]["id"]
        return self._api("POST", DRIVE + "/files", {"name": name, "mimeType": FOLDER_MIME, "parents": [parent]})["id"]

    def _root(self):
        if "" not in self.dir_ids:
            parent = "root"
            for part in self.folder.split("/"):
                parent = self._find_or_create(part, parent)
            self.dir_ids[""] = parent
        return self.dir_ids[""]

    def _dir(self, rel_dir):
        if rel_dir in self.dir_ids:
            return self.dir_ids[rel_dir]
        parent_rel, _, name = rel_dir.rpartition("/")
        fid = self._find_or_create(name, self._dir(parent_rel))
        self.dir_ids[rel_dir] = fid
        return fid

    def list(self):
        self.ids.clear()
        out = {}
        todo = [("", self._root())]
        while todo:
            rel_dir, fid = todo.pop()
            for f in self._children(fid):
                if f["name"].startswith("."):
                    continue
                rel = (rel_dir + "/" if rel_dir else "") + f["name"]
                if f["mimeType"] == FOLDER_MIME:
                    self.dir_ids[rel] = f["id"]
                    todo.append((rel, f["id"]))
                elif f["name"].lower().endswith(EXT):
                    self.ids[rel] = f["id"]
                    out[rel] = f.get("md5Checksum") or f.get("modifiedTime", "")
        return out

    def download(self, rel, dest):
        data = self._api("GET", DRIVE + "/files/%s?alt=media" % self.ids[rel], raw=True, timeout=300)
        with open(dest, "wb") as f:
            f.write(data)

    def upload(self, rel, src):
        with open(src, "rb") as f:
            data = f.read()
        fields = "fields=id,md5Checksum,modifiedTime"
        if rel in self.ids:
            r = self._api("PATCH", DRIVE_UPLOAD + "/files/%s?uploadType=media&%s" % (self.ids[rel], fields), data,
                          {"Content-Type": "application/zip"}, timeout=300)
        else:
            parent_rel, _, name = rel.rpartition("/")
            meta = json.dumps({"name": name, "parents": [self._dir(parent_rel) if parent_rel else self._root()]})
            boundary = "wellnotes" + pysecrets.token_hex(8)
            body = ("--%s\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n%s\r\n--%s\r\n"
                    "Content-Type: application/zip\r\n\r\n" % (boundary, meta, boundary)).encode() + data + \
                ("\r\n--%s--" % boundary).encode()
            r = self._api("POST", DRIVE_UPLOAD + "/files?uploadType=multipart&" + fields, body,
                          {"Content-Type": "multipart/related; boundary=" + boundary}, timeout=300)
            self.ids[rel] = r["id"]
        return r.get("md5Checksum") or r.get("modifiedTime", "")

    def delete(self, rel):
        if rel in self.ids:
            self._api("PATCH", DRIVE + "/files/%s" % self.ids.pop(rel), {"trashed": True})


def make_backend():
    cfg = settings.get("sync")
    p = cfg.get("provider")
    if p == "nextcloud":
        user = cfg.get("nextcloud_user", "")
        return WebDavBackend(cfg.get("nextcloud_server", ""), user,
                             settings.lookup_secret("nextcloud", user + "@" + cfg.get("nextcloud_server", "")),
                             cfg.get("nextcloud_folder", "WellNotes"))
    if p == "gdrive":
        if cfg.get("gdrive_auth") == "goa":
            acc = goa_google_account()
            if not acc:
                raise SyncError("Kein Google-Konto in GNOME Online-Konten (mit „Dateien“) gefunden")
            return GoogleDriveBackend(cfg.get("gdrive_folder", "WellNotes"), goa=acc)
        cid = cfg.get("gdrive_client_id", "")
        return GoogleDriveBackend(cfg.get("gdrive_folder", "WellNotes"), client_id=cid,
                                  client_secret=settings.lookup_secret("gdrive-secret", cid),
                                  refresh_token=settings.lookup_secret("gdrive", cid))
    if p == "folder":
        return FolderBackend(cfg.get("folder_path", ""))
    return None


# --------------------------------------------------------------------------
# engine


def scan_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.lower().endswith(EXT) and not f.startswith("."):
                out.append(os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/"))
    return out


def _local_version(path):
    st = os.stat(path)
    return "%d-%d" % (st.st_mtime_ns, st.st_size)


def _conflict_name(rel):
    base, ext = os.path.splitext(rel)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M")
    return "%s (Konflikt %s)%s" % (base, stamp, ext)


class SyncResult:
    def __init__(self):
        self.uploaded = self.downloaded = self.deleted = 0
        self.conflicts = []
        self.skipped = []

    def summary(self):
        parts = []
        if self.uploaded:
            parts.append("%d hochgeladen" % self.uploaded)
        if self.downloaded:
            parts.append("%d heruntergeladen" % self.downloaded)
        if self.deleted:
            parts.append("%d gelöscht" % self.deleted)
        if self.conflicts:
            parts.append("%d Konflikt(e)" % len(self.conflicts))
        return ", ".join(parts) or "Alles aktuell"


def run_sync(root, backend, busy=frozenset(), key=""):
    """Synchronise ``root`` with ``backend``.  ``busy`` are absolute paths that must not be replaced."""
    state_path = os.path.join(root, STATE_FILE)
    try:
        with open(state_path) as f:
            state = json.load(f)
        if state.get("key") != key:
            state = {}
    except (OSError, ValueError):
        state = {}
    files = state.get("files", {})
    remote = backend.list()
    local = {rel: _local_version(os.path.join(root, rel)) for rel in scan_files(root)}
    res = SyncResult()
    tmpdir = tempfile.mkdtemp(prefix="wellnotes-sync-")

    def lpath(rel):
        return os.path.join(root, *rel.split("/"))

    def download(rel, target_rel=None):
        target_rel = target_rel or rel
        dest = lpath(target_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = os.path.join(tmpdir, "dl")
        backend.download(rel, tmp)
        shutil.move(tmp, dest)
        res.downloaded += 1
        return _local_version(dest)

    def upload(rel):
        v = backend.upload(rel, lpath(rel))
        res.uploaded += 1
        return v

    try:
        for rel in sorted(set(local) | set(remote) | set(files)):
            L, R, st = local.get(rel), remote.get(rel), files.get(rel)
            is_busy = lpath(rel) in busy
            if L and R:
                if st is None:
                    tmp = os.path.join(tmpdir, "cmp")
                    backend.download(rel, tmp)
                    with open(tmp, "rb") as a, open(lpath(rel), "rb") as b:
                        same = a.read() == b.read()
                    if same:
                        files[rel] = {"l": L, "r": R}
                        continue
                    crel = _conflict_name(rel)
                    shutil.move(tmp, lpath(crel))
                    files[crel] = {"l": _local_version(lpath(crel)), "r": backend.upload(crel, lpath(crel))}
                    files[rel] = {"l": L, "r": upload(rel)}
                    res.conflicts.append(rel)
                    continue
                lc, rc = L != st["l"], R != st["r"]
                if lc and not rc:
                    files[rel] = {"l": L, "r": upload(rel)}
                elif rc and not lc:
                    if is_busy:
                        res.skipped.append(rel)
                        continue
                    files[rel] = {"l": download(rel), "r": R}
                elif lc and rc:
                    crel = _conflict_name(rel)
                    lv = download(rel, crel)
                    files[crel] = {"l": lv, "r": backend.upload(crel, lpath(crel))}
                    files[rel] = {"l": L, "r": upload(rel)}
                    res.conflicts.append(rel)
            elif L and not R:
                if st and L == st["l"]:
                    if is_busy:
                        res.skipped.append(rel)
                        continue
                    try:
                        Gio.File.new_for_path(lpath(rel)).trash(None)
                    except GLib.Error:
                        os.unlink(lpath(rel))
                    files.pop(rel, None)
                    res.deleted += 1
                else:
                    files[rel] = {"l": L, "r": upload(rel)}
            elif R and not L:
                if st and R == st["r"]:
                    backend.delete(rel)
                    files.pop(rel, None)
                    res.deleted += 1
                else:
                    files[rel] = {"l": download(rel), "r": R}
            else:
                files.pop(rel, None)
        return res
    finally:
        state = {"key": key, "files": files, "last": time.time()}
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_path)
        shutil.rmtree(tmpdir, ignore_errors=True)


def backend_key():
    """Identifies the configured remote; switching it starts a fresh sync state."""
    cfg = settings.get("sync")
    p = cfg.get("provider")
    if p == "nextcloud":
        return "nc:%s:%s:%s" % (cfg.get("nextcloud_server"), cfg.get("nextcloud_user"), cfg.get("nextcloud_folder"))
    if p == "gdrive":
        return "gd:%s:%s" % (cfg.get("gdrive_auth"), cfg.get("gdrive_folder"))
    if p == "folder":
        return "dir:%s" % cfg.get("folder_path")
    return ""


class SyncManager:
    """Runs syncs in a background thread and reports to the UI via callbacks (main thread)."""

    def __init__(self, root_fn, busy_fn, on_state):
        self.root_fn = root_fn
        self.busy_fn = busy_fn
        self.on_state = on_state  # (running: bool, message: str|None, error: bool)
        self.running = False
        self.pending = False
        self.last = None
        self.last_error = None
        self._timer = 0
        self.reschedule()

    @property
    def enabled(self):
        return settings.get("sync", "provider") not in (None, "", "none")

    def reschedule(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        if self.enabled and settings.get("sync", "auto"):
            minutes = max(1, int(settings.get("sync", "interval") or 5))
            self._timer = GLib.timeout_add_seconds(minutes * 60, self._tick)

    def _tick(self):
        self.sync(quiet=True)
        return True

    def sync(self, quiet=False):
        if not self.enabled:
            return False
        if self.running:
            self.pending = True
            return True
        self.running = True
        self.on_state(True, None, False)
        root = self.root_fn()
        busy = frozenset(self.busy_fn())
        key = backend_key()

        def work():
            try:
                backend = make_backend()
                res = run_sync(root, backend, busy, key)
                GLib.idle_add(done, res, None)
            except SyncError as e:
                GLib.idle_add(done, None, str(e))
            except Exception as e:  # never let a sync crash the app
                GLib.idle_add(done, None, "Synchronisation fehlgeschlagen: %s" % e)

        def done(res, err):
            self.running = False
            self.last = time.time()
            if err:
                # Background syncs report a recurring error only once.
                show = not quiet or err != self.last_error
                self.last_error = err
                self.on_state(False, err if show else None, show)
            else:
                self.last_error = None
                msg = res.summary()
                if res.conflicts:
                    msg += " – Konfliktkopien angelegt"
                self.on_state(False, None if quiet and msg == "Alles aktuell" else msg, False)
            if self.pending:
                self.pending = False
                self.sync(quiet=True)
            return False

        threading.Thread(target=work, daemon=True).start()
        return True

    def test(self, callback):
        """Check the connection in the background; callback(ok, message) on the main thread."""
        def work():
            try:
                b = make_backend()
                if b is None:
                    raise SyncError("Keine Synchronisation gewählt")
                n = len(b.list())
                GLib.idle_add(callback, True, "Verbindung OK – %d Notizbuch/Notizbücher in %s" % (n, b.name))
            except SyncError as e:
                GLib.idle_add(callback, False, str(e))
            except Exception as e:
                GLib.idle_add(callback, False, str(e))
        threading.Thread(target=work, daemon=True).start()
