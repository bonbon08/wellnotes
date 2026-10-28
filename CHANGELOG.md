# Changelog

## 0.3.0 – 2026-10-29

- Deutlich flüssigeres Schreiben auf schwachen Geräten:
  - Seiten als GPU-Texturen in Streifen
  - der aktuelle Strich wird inkrementell gezeichnet
  - Seiten werden im Hintergrund-Thread gerendert
  - verschobene Auswahlen werden als Textur bewegt
- Gesten:
  - Wischen oder Esc schließt kein Notizbuch mehr versehentlich; zurück geht es nur über den Knopf oder Alt+←.
  - Pinch mit gleichzeitigem Verschieben
  - Schwung beim Scrollen mit dem Finger
- Vollbildmodus (F11) mit kompakter Werkzeugleiste
- Stärken und Textgrößen: drei frei belegbare Vorgaben je Werkzeug und ein Regler für beliebige Werte; die Vorgaben werden gespeichert
- Text auf den Linien: Textfelder rasten auf liniertem, kariertem oder gepunktetem Papier auf den Linien ein
- LaTeX in Markdown:
  - `$…$` und `$$…$$` werden als Formeln gesetzt
  - Matrizen, `cases` und `aligned` werden unterstützt
- Obsidian-Bilder:
  - Bilder auf aufeinanderfolgenden Zeilen, neben Text und in Tabellenzellen werden angezeigt
  - Web-Bilder und `data:`-Bilder werden geladen
- Tabellen: Spaltenbreiten richten sich nach dem längsten Wort, Wörter werden nicht mehr mitten im Wort getrennt
- Tests: `tests/` mit einem Headless-Testlauf für alle Werkzeuge und die neuen Funktionen

## 0.2.0 – 2026-10-28

- Synchronisation mit Nextcloud/WebDAV, Google Drive (GNOME Online-Konten oder eigene OAuth-Client-ID) und beliebigen Ordnern, in beide Richtungen und mit Konfliktkopien
- Import von Obsidian-Vaults, Markdown-Ordnern (Logseq, Joplin …) und Notion-ZIP-Exporten
- Obsidian-Syntax: Wikilinks, Einbettungen (Bilder, Notizen, Abschnitte, PDFs), Callouts, Markierungen, Tags, Kommentare, Fußnoten, Formeln
- Einstellungsdialog mit Bibliotheksort und Sync-Optionen; Passwörter liegen im Schlüsselbund
- Behoben: Bei manchen Stiften und Mäusen erzeugten fehlerhafte Zwischenpunkte Linien zur oberen linken Ecke
- Arch-Linux-PKGBUILD, AppStream-Metadaten, Dokumentation

## 0.1.0

- Erste Version: Bibliothek, Notizbücher, Stift mit Druck, Textmarker, Radierer, Formen, Lasso, Text, Bilder
- `.goodnotes` lesen und schreiben, PDF- und Markdown-Import, PDF-Export
