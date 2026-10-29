// Loading/saving notebooks as .goodnotes files (port of wellnotes/fileformat.py).
//
// Files are real GoodNotes containers plus `wellnotes.json` with the lossless model.
// `gn_signature` (SHA-1 of index.events.pb) tells whether GoodNotes changed the file since.
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:archive/archive.dart';
import 'package:crypto/crypto.dart';
import 'package:path/path.dart' as p;
import 'package:pdf/pdf.dart' as pdf;
import 'package:pdf/widgets.dart' as pw;
import 'package:pdfrx/pdfrx.dart';

import 'format/goodnotes.dart';
import 'model.dart';
import 'paper.dart';

const ext = '.goodnotes';

String titleFromPath(String path) {
  final t = p.basenameWithoutExtension(path);
  // Notion exports append a 32-digit hex id to every page name.
  return t.replaceFirst(RegExp(r'\s+[0-9a-f]{32}$'), '');
}

Future<Document> loadDocument(String path) async {
  final bytes = await File(path).readAsBytes();
  final files = {for (final f in ZipDecoder().decodeBytes(bytes).files) if (f.isFile) f.name: f};
  final metaFile = files['wellnotes.json'];
  if (metaFile != null) {
    final meta = jsonDecode(utf8.decode(metaFile.content)) as Map<String, Object?>;
    final events = files['index.events.pb']?.content ?? Uint8List(0);
    final sig = meta['gn_signature'] as String?;
    if (sig == null || sig == sha1.convert(events).toString()) {
      final keep = (meta['attachments'] as List?)?.cast<String>().toSet();
      final atts = <String, Uint8List>{
        for (final e in files.entries)
          if (e.key.startsWith('attachments/') && (keep == null || keep.contains(e.key.substring(12))))
            e.key.substring(12): e.value.content,
      };
      return Document.fromJson(meta, atts)..path = path;
    }
  }
  final doc = fromGoodnotes(readGoodnotes(bytes))..path = path;
  if (doc.title.isEmpty) doc.title = titleFromPath(path);
  return doc;
}

Document fromGoodnotes(GnDocument g) {
  final doc = Document(g.title)..attachments = Map.of(g.attachments);
  for (final gp in g.pages) {
    final bg = gp.templateAtt != null && doc.attachments.containsKey(gp.templateAtt)
        ? Background(kind: 'pdf', att: gp.templateAtt, index: gp.templatePage - 1)
        : Background(color: (gp.paper ?? [1, 1, 1]).take(3).toList());
    final page = Page(gp.width, gp.height, background: bg);
    for (final el in gp.elements) {
      if (el.kind == 'stroke') {
        for (final path in el.paths) {
          page.elements.add(Stroke([for (final (x, y) in path) (x, y, 0.5)], el.color, el.width, style: 'ball'));
        }
      } else {
        page.elements.add(ImageEl(el.att, el.x, el.y, el.w, el.h));
      }
    }
    doc.pages.add(page);
  }
  if (doc.pages.isEmpty) {
    final (w, h) = pageSizes['a4']!;
    doc.pages.add(Page(w, h));
  }
  return doc;
}

/// Reads only the thumbnail and cover colour of a notebook (for the library grid).
Future<(Uint8List?, String?)> readCover(String path) async {
  try {
    final archive = ZipDecoder().decodeBytes(await File(path).readAsBytes());
    Uint8List? thumb;
    String? cover;
    for (final f in archive.files) {
      if (f.name == 'thumbnail.jpg') thumb = f.content;
      if (f.name == 'wellnotes.json') {
        cover = (jsonDecode(utf8.decode(f.content)) as Map)['cover'] as String?;
      }
    }
    return (thumb, cover);
  } on Object {
    return (null, null);
  }
}

// -- templates: one PDF page per paper/text combination, for GoodNotes

final Map<String, (String, Uint8List)> _templateCache = {};

String _attId(String key) {
  final h = sha1.convert(utf8.encode(key)).toString().toUpperCase();
  return '${h.substring(0, 8)}-${h.substring(8, 12)}-${h.substring(12, 16)}-${h.substring(16, 20)}-${h.substring(20, 32)}';
}

Future<(String, Uint8List)> _template(Page page) async {
  final texts = page.elements.whereType<TextEl>().toList();
  final bg = page.background;
  final key = jsonEncode([page.width, page.height, bg.kind, bg.color, [for (final t in texts) t.toJson()]]);
  final cached = _templateCache[key];
  if (cached != null) return cached;
  final w = page.width / gnScale, h = page.height / gnScale;
  final docPdf = pw.Document();
  docPdf.addPage(pw.Page(
    pageFormat: pdf.PdfPageFormat(w, h),
    margin: pw.EdgeInsets.zero,
    build: (ctx) => pw.Stack(children: [
      pw.Positioned.fill(child: pw.CustomPaint(painter: (g, size) => paintPaperPdf(g, page, w, h))),
      for (final t in texts)
        pw.Positioned(
          left: t.x / gnScale,
          top: t.y / gnScale,
          child: pw.SizedBox(
            width: t.w / gnScale,
            child: pw.Text(t.text,
                style: pw.TextStyle(
                    fontSize: t.size / gnScale,
                    color: pdf.PdfColor(t.color[0], t.color[1], t.color[2]),
                    lineSpacing: t.lineH > 0 ? (t.lineH - t.size * 1.17) / gnScale : null)),
          ),
        ),
    ]),
  ));
  final result = (_attId(key), await docPdf.save());
  _templateCache[key] = result;
  return result;
}

/// Saves [doc] to [path] atomically.  [thumbnail] is a JPEG of the first page.
Future<void> saveDocument(Document doc, String path, {Uint8List? thumbnail}) async {
  final used = <String>{};
  for (final pg in doc.pages) {
    if (pg.background.att != null) used.add(pg.background.att!);
    for (final e in pg.elements) {
      if (e is ImageEl) used.add(e.att);
    }
  }
  final atts = {for (final k in used) if (doc.attachments[k] != null) k: doc.attachments[k]!};
  final gnAtts = Map.of(atts);
  final gnPages = <GnWritePage>[];
  for (final pg in doc.pages) {
    String? tid;
    var tpage = 1;
    if (pg.background.kind == 'pdf' && pg.background.att != null) {
      // Reference the imported PDF page directly.
      tid = pg.background.att;
      tpage = pg.background.index + 1;
    } else {
      final (id, data) = await _template(pg);
      tid = id;
      gnAtts[id] = data;
    }
    final els = <Object>[];
    for (final e in pg.elements) {
      if (e is Stroke) {
        var color = e.color;
        var width = e.width;
        if (e.tool == 'highlighter') {
          color = [...e.color.take(3), 0.4];
        } else if (e.style != 'ball' && e.points.isNotEmpty) {
          final avg = e.points.fold<double>(0, (s, p) => s + p.$3) / e.points.length;
          width = e.width * (0.35 + 0.9 * avg);
        }
        els.add(GnWriteStroke([for (final pt in e.points) (pt.$1, pt.$2)], width, color));
      } else if (e is ImageEl) {
        els.add(e);
      }
    }
    gnPages.add(GnWritePage(pg.width, pg.height, tid, tpage, pg.background.color, els));
  }
  final archive = Archive();
  final events = writeGoodnotes(archive, doc.title, gnPages, gnAtts, thumbnail: thumbnail);
  final written = {for (final f in archive.files) f.name};
  atts.forEach((k, v) {
    if (!written.contains('attachments/$k')) archive.addFile(ArchiveFile.bytes('attachments/$k', v));
  });
  final meta = doc.toJson()
    ..['gn_signature'] = sha1.convert(events).toString()
    ..['attachments'] = (atts.keys.toList()..sort());
  archive.addFile(ArchiveFile.string('wellnotes.json', jsonEncode(meta)));
  final bytes = ZipEncoder().encodeBytes(archive);
  final tmp = File(p.join(p.dirname(path), '.wellnotes-${DateTime.now().microsecondsSinceEpoch}$ext'));
  await tmp.writeAsBytes(bytes, flush: true);
  await tmp.rename(path);
  doc.path = path;
}

// -- new documents and imports

Document newNotebook(String title, {String size = 'a4', String paper = 'lined', List<double>? color, String? cover}) {
  final doc = Document(title);
  if (cover != null) doc.cover = cover;
  final (w, h) = pageSizes[size] ?? pageSizes['a4']!;
  doc.pages.add(Page(w, h, background: Background(kind: paper, color: color ?? [1, 1, 1])));
  return doc;
}

Future<List<Page>> pdfPages(Document doc, Uint8List data) async {
  final pdfDoc = await PdfDocument.openData(data);
  try {
    final att = doc.addAttachment(data);
    return [
      for (var i = 0; i < pdfDoc.pages.length; i++)
        Page(pdfDoc.pages[i].width * gnScale, pdfDoc.pages[i].height * gnScale,
            background: Background(kind: 'pdf', att: att, index: i)),
    ];
  } finally {
    await pdfDoc.dispose();
  }
}

Future<(int, int)> imageSize(Uint8List data) async {
  final codec = await ui.instantiateImageCodec(data);
  final frame = await codec.getNextFrame();
  final r = (frame.image.width, frame.image.height);
  frame.image.dispose();
  return r;
}

ImageEl fitImage(String att, int iw, int ih, Page page, {double? cx, double? cy, double maxFrac = 0.8}) {
  final s = [page.width * maxFrac / iw, page.height * maxFrac / ih, 1.5].reduce((a, b) => a < b ? a : b);
  final w = iw * s, h = ih * s;
  final x = (cx ?? page.width / 2) - w / 2, y = (cy ?? page.height / 2) - h / 2;
  return ImageEl(att, x, y, w, h);
}

Future<Page> imagePage(Document doc, Uint8List data, {Page? like}) async {
  final att = doc.addAttachment(data);
  final (iw, ih) = await imageSize(data);
  final (w, h) = like != null ? (like.width, like.height) : pageSizes['a4']!;
  final page = Page(w, h, background: like?.background.copy());
  page.elements.add(fitImage(att, iw, ih, page));
  return page;
}
