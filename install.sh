#!/usr/bin/env bash
# Installs WellNotes for the current user (launcher, desktop entry, icon, .goodnotes MIME type).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN="$HOME/.local/bin"
mkdir -p "$BIN" "$PREFIX/applications" "$PREFIX/icons/hicolor/scalable/apps" "$PREFIX/mime/packages"
ln -sf "$ROOT/bin/wellnotes" "$BIN/wellnotes"
install -m644 "$ROOT/data/io.github.wellnotes.WellNotes.desktop" "$PREFIX/applications/"
install -m644 "$ROOT/data/io.github.wellnotes.WellNotes.svg" "$PREFIX/icons/hicolor/scalable/apps/"
install -m644 "$ROOT/data/wellnotes-mime.xml" "$PREFIX/mime/packages/"
update-mime-database "$PREFIX/mime" >/dev/null 2>&1 || true
update-desktop-database "$PREFIX/applications" >/dev/null 2>&1 || true
gtk-update-icon-cache -q -t "$PREFIX/icons/hicolor" >/dev/null 2>&1 || true
echo "WellNotes installiert. Start über das App-Menü oder mit: wellnotes"
