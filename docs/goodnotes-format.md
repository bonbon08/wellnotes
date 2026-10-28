# Das `.goodnotes`-Format (GoodNotes 6, Schema 24)

Diese Beschreibung ist durch Reverse Engineering einer echten GoodNotes-6-Datei entstanden. GoodNotes Limited hat das Format nicht dokumentiert. Feldnummern ohne gesicherte Bedeutung sind als *unbekannt* oder *vermutet* markiert. Die Implementierung steht in [`wellnotes/goodnotes.py`](../wellnotes/goodnotes.py).

## Container

Eine `.goodnotes`-Datei ist ein ZIP-Archiv:

| Pfad | Inhalt |
|---|---|
| `schema.pb` | Protobuf `{1: 24}`, die Schema-Version |
| `document.info.pb` | leer |
| `index.events.pb` | Ereignis-Log des Dokuments: Dokument, Seiten, Ebenen, Anhänge, Notizen |
| `index.notes.pb` | Verzeichnis der Notizdateien: `{1: notes-id, 2: "notes/<id>"}` |
| `index.attachments.pb` | Verzeichnis der Anhänge: `{1: att-id, 2: "attachments/<id>"}` |
| `index.search.pb` | Verzeichnis der Suchindizes |
| `notes/<id>` | Elemente (Striche, Bilder …) einer Seite |
| `attachments/<id>` | Rohdateien: PDF (Seitenvorlagen, importierte PDFs) oder Bilder (JPEG/PNG) |
| `search/<id>` | Handschrift-Erkennungsindex (wird ignoriert) |
| `thumbnail.jpg` | Vorschaubild der ersten Seite |

Alle `*.pb`-Dateien und Notizdateien sind **Folgen von längenpräfixierten Protobuf-Nachrichten** (Varint-Länge, dann Nachricht).

### Gemeinsame Bausteine

- **IDs** sind UUID-Strings in Großbuchstaben.
- **Lamport-Stempel** `{1: zähler, 2: zufall32}` stehen an vielen Feldern. Vermutlich dienen sie der CRDT-Zusammenführung („letzter Schreiber gewinnt“).
- **Gerät** ist eine 63-Bit-Zahl, die in allen Records eines Geräts gleich ist.
- **Zeit**: Millisekunden seit der Unix-Epoche (Varint) und ein `double` mit einem Hybrid-Zeitstempel.
- **Punkt** `{1: float x, 2: float y}`, **Rechteck** `{1: Punkt Ursprung, 2: Punkt Größe}`
- **Farbe** `{1: r, 2: g, 3: b, 4: a}` als Float 0…1; fehlende Felder sind 0, Alpha ist standardmäßig 1.

## `index.events.pb`

Jeder Record hat die Form `{1: objekt-id, <typ>: nachricht}`. Der Typ bestimmt die Bedeutung:

| Typ-Feld | Objekt | Wichtige Felder |
|---|---|---|
| 30 | Dokument | `2: {1: Titel, 2: Stempel}`, `9: "auto"` |
| 6 | Anhang | `1/2: att-id`, `5: Größe in Bytes`, `6: dokument-id` |
| 2 | Seite | `2: seiten-id`, `4: att-id der PDF-Vorlage`, `5: vermutlich Seitennummer im PDF (1-basiert)`, `7: Linienabstand`, `8: Punkt(Breite, Höhe)`, `9: Vorlagenname`, `18: Inhaltsrahmen` |
| 54 | Ebene | `2: ebenen-id`, `3: {1: seiten-id}`, `4: {1: Positionsschlüssel}`, `17: {1: {2: {1: Linienfarbe, 2: Papierfarbe}}}` |
| 102 | Notizdatei | `1: notes-id`, `16: dokument-id` |
| 105, 104 | Verknüpfung Notizen ↔ Dokument | `4` bzw. `2: notes-id`, gemeinsamer 64-Bit-Hash |
| 10 | Ansichtszustand | wird ignoriert |

**Seitengröße**: Seiten werden in GoodNotes-Einheiten angegeben, das sind PDF-Punkte × 11/6. A4 ergibt damit 1091,35 × 1543,46. Die PDF-Vorlage wird auf die volle Seitengröße skaliert.

**Zuordnung Notizen → Seite**: Die Ebene (Typ 54) verweist auf die Seite. In der Beispieldatei unterscheidet sich die notes-id von der ebenen-id nur in der letzten Hex-Ziffer (+1). WellNotes nutzt diese Regel und ordnet sonst in Reihenfolge zu. *Für mehrseitige Dokumente ist das noch nicht verifiziert.*

## Notizdateien `notes/<id>`

Die Records kommen paarweise, **Kopf** und **Element**:

**Kopf**: `{1: element-id, 2: Stempel, 3: 1 = gelöscht, 4: att-id (bei Bildern), 8: gerät, 9: zähler, 14: 5381, 16: 24}`

**Element**: genau ein Feld der obersten Ebene bestimmt den Typ:

| Feld | Typ | Inhalt |
|---|---|---|
| 7 | Tintenstrich | `1: id`, `2: Geometrie (siehe unten)`, `4: Farbe`, `6: Verschiebung (Punkt), leer = keine`, `7: Stempel`, `14: 1 = gelöscht`, `21: 24` |
| 1 | Bild | `1: id`, `2: Rahmen`, `3: Rahmen mit Mittelpunkt als Ursprung`, `4: att-id`, `6: 1`, `14: 1 = gelöscht` |
| 9 | Auswahl/Gruppe (vermutet) | Liste von Element-IDs, Farbe mit 10 % Deckkraft; wird ignoriert |

Ein Element gilt als gelöscht, wenn der Kopf Feld 3 oder das Element Feld 14 enthält. Gelöschte Striche haben außerdem eine leere Geometrie. Kommt dieselbe Element-ID mehrfach vor, gilt der letzte Record.

### Strichgeometrie

Feld 2 eines Strichs ist ein **Apple-LZ4-Frame**:

```
"bv41" | u32 unkomprimierte Größe | u32 komprimierte Größe | LZ4-Block   (wiederholbar)
"bv4-" | u32 Größe | Rohdaten                                          (unkomprimierter Block)
"bv4$"                                                                 (Ende)
```

Der entpackte Inhalt ist ein Bild der Bibliothek [tpl](https://troydhanson.github.io/tpl/) mit dem Formatstring

```
v u A(v) A(S(uu)) A(S(uuuu)) v A(f)
```

| Teil | Bedeutung |
|---|---|
| `v` | Version (2) |
| `u` | Strichbreite als Bitmuster eines `float` |
| `A(v)` | Segmenttypen: 0 = moveTo, 1 = quadratische Kurve |
| `A(S(uu))` | Startpunkte (x, y als Float-Bits), einer pro moveTo |
| `A(S(uuuu))` | Kurven: Kontrollpunkt x/y, Endpunkt x/y (Float-Bits) |
| `v` | unbekannt (1) |
| `A(f)` | unbekannt, bisher leer |

Ein tpl-Bild beginnt mit `"tpl"`, einem Flag-Byte, `u32` Gesamtgröße und dem nullterminierten Formatstring. Danach folgen die Daten in Formatreihenfolge, Arrays jeweils mit `u32` Anzahl davor, alles Little-Endian.

Ein Strich ist also eine Mittellinie aus quadratischen Bézierkurven mit konstanter Breite. Die Koordinaten liegen in Seiteneinheiten, ohne die Verschiebung aus Feld 6.

## Was WellNotes schreibt

Beim Speichern erzeugt WellNotes die obige Struktur:
- ein Dokument
- pro Seite: Vorlage, Seite, Ebene, Notizdatei, Verknüpfungen
- die Anhänge

Dazu kommen:

- **`wellnotes.json`**: verlustfreies WellNotes-Modell (Druckverläufe, Textfelder, Werkzeugtyp, Papiervorlagen). `gn_signature` ist der SHA-1 von `index.events.pb`. Passt er nicht mehr, weil GoodNotes die Datei geändert hat, liest WellNotes die GoodNotes-Daten.
- **Seitenvorlagen**: Jede Seite bekommt ein einseitiges PDF mit ihrem Papier (liniert, kariert …), dem PDF-Hintergrund und den Textfeldern.
- **Striche**: Die Punkte werden per Mittelpunkt-Verfahren in quadratische Kurven umgewandelt und als nicht komprimierter LZ4-Block (nur Literale) geschrieben.

## Grenzen

- **Ungetestet**: ob GoodNotes von WellNotes geschriebene Dateien öffnet.
- **Variable Strichbreite**: GoodNotes speichert pro Strich eine Breite. Der Druckverlauf des Füllers wird beim Export zu einer Durchschnittsbreite; in `wellnotes.json` bleibt er erhalten.
- **Textmarker**: wird als halbtransparenter Strich exportiert. Das GoodNotes-Feld für den Werkzeugtyp ist unbekannt.
- **Textfelder**: Beim Export landen sie in der Seitenvorlage. In GoodNotes sind sie sichtbar, aber nicht editierbar.
- **Noch nicht importiert**: GoodNotes-Textfelder, Formen-Objekte, Links und Lesezeichen. Sie kamen in der Beispieldatei nicht vor.
- **Seitenreihenfolge und -zuordnung bei mehrseitigen Dokumenten**: abgeleitet, nicht verifiziert.

Beispieldateien (ohne private Inhalte) mit mehreren Seiten, Textfeldern oder Formen helfen sehr. Bitte als [Issue](https://github.com/bonbon08/wellnotes/issues) anhängen.
