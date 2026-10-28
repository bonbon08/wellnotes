"""Persistent settings (~/.config/wellnotes/settings.json) and secrets (system keyring)."""

import json
import os

import gi
from gi.repository import GLib

try:
    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ValueError, ImportError):
    Secret = None

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "wellnotes")
PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "library": "",
    "sync": {
        "provider": "none",  # none | nextcloud | gdrive | folder
        "auto": True,
        "interval": 5,  # minutes
        "nextcloud_server": "",
        "nextcloud_user": "",
        "nextcloud_folder": "WellNotes",
        "gdrive_auth": "goa",  # goa | oauth
        "gdrive_client_id": "",
        "gdrive_folder": "WellNotes",
        "folder_path": "",
    },
}

_data = None


def load():
    global _data
    if _data is None:
        _data = json.loads(json.dumps(DEFAULTS))
        try:
            with open(PATH) as f:
                stored = json.load(f)
            for k, v in stored.items():
                if isinstance(v, dict) and isinstance(_data.get(k), dict):
                    _data[k].update(v)
                else:
                    _data[k] = v
        except (OSError, ValueError):
            pass
    return _data


def save():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(load(), f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


def get(section, key=None):
    d = load()
    return d[section] if key is None else d[section].get(key)


def put(section, key, value):
    load()[section][key] = value
    save()


# --------------------------------------------------------------------------
# secrets

_SCHEMA = None


def _schema():
    global _SCHEMA
    if _SCHEMA is None and Secret is not None:
        _SCHEMA = Secret.Schema.new("io.github.wellnotes.Secret", Secret.SchemaFlags.NONE,
                                    {"service": Secret.SchemaAttributeType.STRING,
                                     "account": Secret.SchemaAttributeType.STRING})
    return _SCHEMA


def _fallback_path():
    return os.path.join(CONFIG_DIR, "secrets.json")


def _fallback():
    try:
        with open(_fallback_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def store_secret(service, account, value):
    if Secret is not None:
        try:
            Secret.password_store_sync(_schema(), {"service": service, "account": account},
                                       Secret.COLLECTION_DEFAULT, "WellNotes: %s (%s)" % (service, account),
                                       value, None)
            return
        except GLib.Error:
            pass
    # No keyring available: keep it in a user-only readable file.
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = _fallback()
    data["%s/%s" % (service, account)] = value
    with open(_fallback_path(), "w") as f:
        json.dump(data, f)
    os.chmod(_fallback_path(), 0o600)


def lookup_secret(service, account):
    if Secret is not None:
        try:
            v = Secret.password_lookup_sync(_schema(), {"service": service, "account": account}, None)
            if v is not None:
                return v
        except GLib.Error:
            pass
    return _fallback().get("%s/%s" % (service, account))


def clear_secret(service, account):
    if Secret is not None:
        try:
            Secret.password_clear_sync(_schema(), {"service": service, "account": account}, None)
        except GLib.Error:
            pass
    data = _fallback()
    if data.pop("%s/%s" % (service, account), None) is not None:
        with open(_fallback_path(), "w") as f:
            json.dump(data, f)
