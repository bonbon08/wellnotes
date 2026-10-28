# Entwicklung

## Aus dem Quellcode starten

```bash
./bin/wellnotes                              # nutzt den Quellbaum direkt
WELLNOTES_LIBRARY=/tmp/testlib ./bin/wellnotes   # mit einer Test-Bibliothek
python3 -m wellnotes                         # gleichwertig, im Projektordner
```

Python-Pakete aus PyPI sind nicht nötig. Alle Abhängigkeiten kommen aus der Distribution: PyGObject, pycairo, GTK 4, libadwaita, Poppler-GLib, Pango; optional libsecret und GNOME Online Accounts.

## Architektur

```
wellnotes/
  app.py         Adw.Application, Hauptfenster, Datei-Öffnen, Sync-Anbindung
  library.py     Bibliothek: Ordner, Cover, Suche, Import (inkl. Hintergrund-Import), Kontextmenüs
  editor.py      Notizbuch-Seite: Werkzeugleiste, Seitenleiste, Auswahlmenü, Autosave (Thread), Export
  canvas.py      Zeichenfläche: eigenes Scrollen/Zoomen, Seiten-Caches, Stift/Touch-Eingabe,
                 Radierer, Lasso, Textfelder
  render.py      Cairo-Rendering von Papier, Strichen (druckabhängige Kontur), Bildern, PDF-Seiten;
                 PDF-Export und Vorschaubilder
  model.py       Dokument/Seite/Elemente (Stroke, Image, Text) und Undo/Redo über Element-Snapshots
  fileformat.py  Laden/Speichern (.goodnotes + wellnotes.json), Importe
  goodnotes.py   GoodNotes-Container: Protobuf-Codec, Apple-LZ4, tpl (siehe goodnotes-format.md)
  markdown.py    Markdown/Obsidian-Parser und Pango-Typesetter nach PDF
  importers.py   Vault-/Ordner-/Notion-ZIP-Import
  shapes.py      Formerkennung (Douglas-Peucker, Ellipsen-Fit)
  sync.py        Sync-Engine (Zustandsdatei, Konflikte) und Backends: WebDAV, Google Drive, Ordner
  settings.py    settings.json und Schlüsselbund (libsecret)
  prefs.py       Einstellungsdialog
  dialogs.py     Neues Notizbuch, Papiervorlage
```

**Wichtige Entscheidungen**

- **Seiten-Einheiten** entsprechen den GoodNotes-Einheiten (PDF-Punkte × 11/6). So brauchen Import und Export keine Umrechnung.
- **Elemente** werden nach dem Anlegen nicht mehr verändert. Verschieben, Umfärben und Radieren erzeugen neue Objekte. Undo speichert deshalb nur Listen-Snapshots der betroffenen Seiten.
- **Rendering**: Jede Seite wird als Bitmap in Zoom-Auflösung zwischengespeichert, begrenzt auf etwa 14 Megapixel.
  - Neue Striche werden direkt auf den Cache gemalt.
  - Beim Radieren wird nur die betroffene Region neu gezeichnet.
- **Eingabe**: Ein `Gtk.GestureDrag` für alle Geräte. Die Geräteklasse (Stift, Touch, Maus) und der Werkzeugtyp (Radierer-Spitze) entscheiden über die Aktion.
  - Zwischengespeicherte Bewegungspunkte (`get_history`) werden nur verwendet, wenn sie plausibel zwischen den Positionen liegen. Manche Geräte liefern dort falsche Koordinaten.
- **Markdown** wird zu einem PDF gesetzt und wie ein importiertes PDF als Seitenhintergrund verwendet. Dadurch ist es beschreibbar und landet sauber im `.goodnotes`-Export.
- **Speichern** läuft in einem Thread auf einer Momentaufnahme des Dokuments (eigene `ResourceCache`-Instanz, keine geteilten Poppler-Objekte).

## Testen

Die App lässt sich ohne Bildschirm mit dem Broadway-Backend von GTK testen:

```bash
gtk4-broadwayd :7 &
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 WELLNOTES_LIBRARY=/tmp/testlib python3 dein_skript.py
```

Bildschirmfotos entstehen, indem man die Kinder des Fensters per `Gtk.Widget.snapshot_child` in einen `Gtk.Snapshot` zeichnet und mit `renderer.render_texture(...)` als PNG speichert. Werkzeuge lassen sich testen, indem man die Gesten-Handler von `NotebookCanvas` (`_on_drag_begin/_update/_end`) mit einem Stellvertreter-Gesture-Objekt aufruft.

Die Sync-Engine (`sync.run_sync`) ist ohne GUI testbar, mit `FolderBackend` oder einem lokalen WebDAV-Server.

## Pakete bauen

### Python-Wheel

```bash
python -m build --wheel --no-isolation
```

### Arch Linux

```bash
cd packaging/arch
makepkg -si          # baut die in pkgver angegebene Version aus dem GitHub-Release
```

## Release

1. Version in `wellnotes/__init__.py`, `packaging/arch/PKGBUILD` (`pkgver`) und `data/*.metainfo.xml` erhöhen und `CHANGELOG.md` ergänzen.
2. `git tag vX.Y.Z && git push --tags` und ein GitHub-Release anlegen (`gh release create vX.Y.Z`).
3. Prüfsumme aktualisieren: im Ordner `packaging/arch` `updpkgsums` ausführen (oder `makepkg -g`), dann `makepkg --printsrcinfo > .SRCINFO`.
