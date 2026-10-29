// On-device tests (real engine, fonts and offscreen rendering):
//   flutter test integration_test/app_test.dart -d <device>
// Rendered pages are written to <external storage>/WellNotes-test/ for inspection (adb pull).
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart' hide Element, Page;
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';
import 'package:wellnotes/editor_screen.dart';
import 'package:wellnotes/importers.dart';
import 'package:wellnotes/main.dart';
import 'package:wellnotes/math.dart';
import 'package:wellnotes/model.dart';
import 'package:wellnotes/notebook_view.dart';
import 'package:wellnotes/render.dart';
import 'package:wellnotes/storage.dart';
import 'package:wellnotes/sync.dart';
import 'package:wellnotes/tools.dart';

Future<Directory> _outDir() async {
  final base = await getExternalStorageDirectory() ?? await getApplicationDocumentsDirectory();
  return Directory(p.join(base.path, 'WellNotes-test'))..createSync(recursive: true);
}

Future<void> _savePage(Page page, Resources res, String name) async {
  final img = await renderPageImage(page, res, 1.0);
  final png = await img.toByteData(format: ui.ImageByteFormat.png);
  await File(p.join((await _outDir()).path, name)).writeAsBytes(png!.buffer.asUint8List());
}

Future<File> _pngFile(String path, Color color) async {
  final rec = ui.PictureRecorder();
  final c = Canvas(rec);
  c.drawRect(const Rect.fromLTWH(0, 0, 300, 150), Paint()..color = color);
  c.drawCircle(const Offset(150, 75), 50, Paint()..color = Colors.white);
  final img = await rec.endRecording().toImage(300, 150);
  final png = await img.toByteData(format: ui.ImageByteFormat.png);
  return File(path)..writeAsBytesSync(png!.buffer.asUint8List());
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('LaTeX renders', (tester) async {
    await tester.runAsync(() async {
      final inline = await renderMath(r'\vec{AB}=\begin{pmatrix}3\\4\\1\end{pmatrix}');
      final block = await renderMath(r'\int_0^1 x^2\,dx=\frac13', display: true);
      final broken = await renderMath(r'\frac{');
      expect(inline, isNotNull);
      expect(inline!.height, greaterThan(inline.width / 10));
      expect(block, isNotNull);
      expect(broken, isNull);
    });
  });

  testWidgets('Obsidian markdown with images, callouts and math', (tester) async {
    await tester.runAsync(() async {
      final vault = await Directory.systemTemp.createTemp('vault');
      Directory(p.join(vault.path, '.obsidian')).createSync();
      Directory(p.join(vault.path, 'Anhänge')).createSync();
      await _pngFile(p.join(vault.path, 'Anhänge', 'bild.png'), const Color(0xff4d96e6));
      File(p.join(vault.path, 'Andere.md')).writeAsStringSync('# Andere\nText aus der **anderen** Notiz.');
      final note = File(p.join(vault.path, 'Haupt.md'))
        ..writeAsStringSync('''---
tags: [x]
---
# Vektoren
Gegeben ist \$\\vec{AB}=\\begin{pmatrix}3\\\\4\\\\1\\end{pmatrix}\$ und \$|\\vec{AB}|=\\sqrt{26}\$. Das ist ==markiert== mit #tag. %%weg%%
Siehe [[Andere|die andere Notiz]].

> [!warning] Achtung
> Ein **Callout**.

\$\$
f(x)=\\begin{cases} x^2 & x\\ge 0\\\\ -x & x<0\\end{cases}
\$\$

![[bild.png|200]]
![[bild.png|100]] Text nach dem Bild.

![[Andere]]

| Größe | Formel |
|---|---|
| Kreis | \$A=\\pi r^2\$ |

- [x] erledigt
- [ ] offen
''');
      final doc = Document('Haupt');
      doc.pages = await markdownPages(doc, note.readAsStringSync(), baseDir: vault.path);
      expect(doc.pages, isNotEmpty);
      expect(doc.pages.first.background.kind, 'pdf');
      final res = Resources(doc);
      for (var i = 0; i < doc.pages.length; i++) {
        await _savePage(doc.pages[i], res, 'markdown-${i + 1}.png');
      }
      debugPrint('WN markdown pages: ${doc.pages.length}');
      // round trip through the file format
      final path = p.join((await _outDir()).path, 'Haupt.goodnotes');
      await saveDocument(doc, path);
      final back = await loadDocument(path);
      expect(back.pages.length, doc.pages.length);
    });
  });

  testWidgets('drawing, text on lines and autosave in the editor', (tester) async {
    final out = await _outDir();
    final path = p.join(out.path, 'Editor.goodnotes');
    late Document doc;
    await tester.runAsync(() async {
      doc = newNotebook('Editor', paper: 'lined', color: List.of(paperColors['yellow']!));
      await saveDocument(doc, path);
    });
    final tools = ToolSettings();
    final state = AppState(out.path, tools, SyncManager(() => out.path, () => {}));
    await tester.pumpWidget(MaterialApp(home: EditorScreen(state: state, doc: doc)));
    await tester.pumpAndSettle();
    final canvas = find.byType(NotebookView);
    final box = tester.getRect(canvas);

    // A stylus stroke draws.
    final g = await tester.startGesture(box.center - const Offset(200, 0), kind: PointerDeviceKind.stylus);
    for (var i = 1; i <= 20; i++) {
      await g.moveBy(Offset(20, (i % 2 == 0 ? 8 : -8)));
      await tester.pump(const Duration(milliseconds: 8));
    }
    await g.up();
    await tester.pump();
    expect(doc.pages.first.elements.whereType<Stroke>().length, 1);

    // A finger swipe scrolls instead of drawing.
    await tester.dragFrom(box.center, const Offset(0, -150), kind: PointerDeviceKind.touch);
    await tester.pumpAndSettle();
    expect(doc.pages.first.elements.length, 1);

    // Text snaps to the ruled lines.
    tools.update(() => tools.tool = 'text');
    await tester.pump();
    await tester.tapAt(box.topLeft + Offset(box.width * 0.45, box.height * 0.3), kind: PointerDeviceKind.mouse);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'Getippter Text auf den Linien');
    await tester.tapAt(box.topLeft + const Offset(20, 20), kind: PointerDeviceKind.mouse);
    await tester.pumpAndSettle();
    final texts = doc.pages.first.elements.whereType<TextEl>().toList();
    expect(texts.length, 1);
    expect(texts.first.lineH, greaterThan(0));

    // Autosave writes the file (debounced 1.5 s).
    await tester.runAsync(() => Future<void>.delayed(const Duration(seconds: 4)));
    await tester.pump();
    late Document back;
    await tester.runAsync(() async {
      back = await loadDocument(path);
      await _savePage(back.pages.first, Resources(back), 'editor-1.png');
    });
    expect(back.pages.first.elements.length, 2);
  });

  // --dart-define=WN_HOLD=60 keeps the app installed for a while so the PNGs can be pulled.
  testWidgets('hold for inspection', (tester) async {
    const hold = int.fromEnvironment('WN_HOLD');
    if (hold > 0) await tester.runAsync(() => Future<void>.delayed(const Duration(seconds: hold)));
  });
}
