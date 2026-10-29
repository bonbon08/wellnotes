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
  mathrender.py  LaTeX-Formeln (Matplotlib-mathtext + eigenes Layout für Matrizen, cases, aligned)
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
- **Rendering** (optimiert für schwache Tablets):
  - Jede Seite wird in einem Hintergrund-Thread als Bitmap in Zoom-Auflösung gerendert (höchstens etwa 10 Megapixel).
  - Die Bitmap wird in Streifen zu 256 Zeilen als `Gdk.Texture` an die GPU gegeben. Die GPU skaliert beim Zoomen.
  - Neue Striche und Radierer-Änderungen werden direkt in die Bitmap gemalt; dabei werden nur die betroffenen Streifen neu hochgeladen.
  - Der Strich während des Schreibens wird segmentweise in eine eigene Ebene gezeichnet und als eng begrenzter Cairo-Knoten eingeblendet.
  - Auswahlen werden beim Verschieben einmal als Textur gerendert.
- **Eingabe**: Ein `Gtk.GestureDrag` für alle Geräte. Die Geräteklasse (Stift, Touch, Maus) und der Werkzeugtyp (Radierer-Spitze) entscheiden über die Aktion.
  - Zwischengespeicherte Bewegungspunkte (`get_history`) werden nur verwendet, wenn sie plausibel zwischen den Positionen liegen. Manche Geräte liefern dort falsche Koordinaten.
- **Markdown** wird zu einem PDF gesetzt und wie ein importiertes PDF als Seitenhintergrund verwendet. Dadurch ist es beschreibbar und landet sauber im `.goodnotes`-Export.
- **Speichern** läuft in einem Thread auf einer Momentaufnahme des Dokuments (eigene `ResourceCache`-Instanz, keine geteilten Poppler-Objekte).

## Android-App (`mobile/`)

Die Android-App ist eine eigene Flutter-Anwendung mit demselben Dateiformat. Sie gleicht über dieselbe Sync-Logik mit der Linux-App ab.

```
mobile/lib/
  main.dart             App-Start, Bibliotheksort, App-Zustand
  library_screen.dart   Bibliothek, Import, Kontextmenüs
  editor_screen.dart    Notizbuch: Werkzeugleiste, Seiten, Autosave (Isolate), Export
  notebook_view.dart    Zeichenfläche: Stift/Touch/Maus, Gesten, Seiten-Caches, Werkzeuge
  render.dart           Zeichnen von Papier, Strichen, Text, Bildern, PDF-Seiten (pdfrx)
  model.dart            Datenmodell (JSON identisch zu wellnotes/model.py) + Undo
  storage.dart          Laden/Speichern (.goodnotes + wellnotes.json), PDF-Vorlagen
  format/               Protobuf, Apple-LZ4, tpl, GoodNotes-Container (Port von goodnotes.py)
  markdown_import.dart  Markdown/Obsidian -> PDF (Paket pdf, Noto-Schriften)
  math.dart             LaTeX über flutter_math_fork, offscreen gerendert
  sync.dart             WebDAV-Sync (Port von sync.py)
```

**Bauen** (Java 27 ist für Gradle zu neu, daher JDK 21):

```bash
cd mobile
JAVA_HOME=/usr/lib/jvm/java-21-openjdk flutter build apk --release --split-per-abi
```

**Tests:**
- Format und Modell auf dem Rechner: `flutter test`. Die Tests nutzen `../Feinschliffe.goodnotes`, falls vorhanden.
- Integrationstests auf Gerät oder Emulator: `flutter test integration_test/app_test.dart -d <gerät>`. Sie prüfen LaTeX, Markdown/Obsidian-Import, Zeichnen, Text auf Linien und Autosave.
  - Mit `--dart-define=WN_HOLD=60` bleibt die App eine Minute installiert. In der Zeit lassen sich die gerenderten PNGs aus `Android/data/io.github.wellnotes.wellnotes/files/WellNotes-test` holen.
- `tool/screenshot.sh out.png` speichert einen verkleinerten Screenshot des laufenden Emulators.

## Testen

Die Tests laufen ohne Bildschirm mit dem Broadway-Backend von GTK:

```bash
gtk4-broadwayd :7 &
mkdir -p /tmp/wn-test
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 WELLNOTES_LIBRARY=/tmp/wn-test python3 tests/harness.py tests/ui_tools.py
GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 WELLNOTES_LIBRARY=/tmp/wn-test python3 tests/harness.py tests/ui_features.py
```

- `ui_tools.py` prüft alle Werkzeuge, Undo/Redo, Seitenoperationen und das Speichern und misst die Zeit pro Stiftbewegung.
- `ui_features.py` prüft Touch-Scrollen mit Schwung, Pinch, Gesten, Text auf Linien, Größenregler und Vollbild.
- Screenshots landen in `/tmp/wn-shots`.

Broadway rendert in Software und spielt keine Animationen ab. Für Leistungsmessungen aussagekräftig ist deshalb vor allem der CPU-Anteil (`WN_PERF_RENDER=0`).

Die Harness zeichnet Bildschirmfotos per `Gtk.Widget.snapshot_child` und `renderer.render_texture(...)`. Eingaben simuliert sie, indem sie die Gesten-Handler von `NotebookCanvas` mit Stellvertreter-Objekten aufruft (Maus, Stift mit Druck, Touch).

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
