// Format round trips.  Uses ../Feinschliffe.goodnotes when present (a private sample, not in git).
import 'dart:io';
import 'dart:typed_data';

import 'package:archive/archive.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wellnotes/format/goodnotes.dart';
import 'package:wellnotes/format/stroke_codec.dart';
import 'package:wellnotes/model.dart';

void main() {
  test('stroke path round trip', () {
    final pts = [for (var i = 0; i < 50; i++) (i * 3.0, 10.0 + i % 7)];
    final (w, paths) = decodeStrokePath(encodeStrokePath(2.5, pts));
    expect(w, closeTo(2.5, 1e-6));
    expect(paths.single.first, pts.first);
    expect(paths.single.last.$1, closeTo(pts.last.$1, 1e-3));
  });

  test('write and read GoodNotes container', () {
    final archive = Archive();
    writeGoodnotes(archive, 'Test', [
      GnWritePage(1091.35, 1543.46, null, 1, [1, 1, 1], [
        GnWriteStroke([(10, 10), (50, 60), (90, 20)], 2.0, [0.1, 0.2, 0.9, 1]),
      ]),
      GnWritePage(1091.35, 1543.46, null, 1, [1, 1, 0.9], []),
    ], {});
    final doc = readGoodnotes(Uint8List.fromList(ZipEncoder().encode(archive)));
    expect(doc.title, 'Test');
    expect(doc.pages.length, 2);
    expect(doc.pages[0].elements.single.kind, 'stroke');
    expect(doc.pages[0].elements.single.color[2], closeTo(0.9, 1e-6));
    expect(doc.pages[1].elements, isEmpty);
  });

  test('real GoodNotes sample', () {
    final f = File('../Feinschliffe.goodnotes');
    if (!f.existsSync()) {
      markTestSkipped('sample not available');
      return;
    }
    final doc = readGoodnotes(f.readAsBytesSync());
    expect(doc.title, 'Feinschliffe');
    expect(doc.pages.length, 1);
    expect(doc.pages[0].elements.where((e) => e.kind == 'stroke').length, 546);
    expect(doc.pages[0].elements.where((e) => e.kind == 'image').length, 4);
    expect(doc.pages[0].templateAtt, isNotNull);
  });

  test('model JSON round trip', () {
    final d = Document('X');
    d.pages.add(Page(100, 200, elements: [
      Stroke([(1, 2, 0.5), (3, 4, 0.7)], [0, 0, 0, 1], 2),
      TextEl('Hallo', 5, 6, 50, lineH: 29.3),
    ]));
    final back = Document.fromJson(d.toJson(), {});
    expect(back.pages.single.elements.length, 2);
    expect((back.pages.single.elements[1] as TextEl).lineH, closeTo(29.3, 1e-9));
  });
}
