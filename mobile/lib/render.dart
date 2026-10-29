// Painting of pages and elements in page units (port of wellnotes/render.py).
import 'dart:math' as math;
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/painting.dart';
import 'package:pdfrx/pdfrx.dart';

import 'model.dart';
import 'paper.dart';

ui.Color rgba(List<double> c, [double? alpha]) =>
    ui.Color.from(alpha: alpha ?? (c.length > 3 ? c[3] : 1.0), red: c[0], green: c[1], blue: c[2]);

/// Decoded attachments (images, PDF page renderings) of one document.
class Resources {
  final Document doc;
  final Map<String, ui.Image?> _images = {};
  final Map<String, Future<ui.Image?>> _loading = {};
  final Map<String, PdfDocument?> _pdfs = {};
  final Map<String, (double, ui.Image)> _pdfPages = {}; // "att#index" -> (pixel width, image)

  Resources(this.doc);

  ui.Image? image(String att) => _images[att];

  Future<ui.Image?> loadImage(String att) {
    if (_images.containsKey(att)) return Future.value(_images[att]);
    return _loading[att] ??= () async {
      final data = doc.attachments[att];
      ui.Image? img;
      if (data != null) {
        try {
          final codec = await ui.instantiateImageCodec(data);
          img = (await codec.getNextFrame()).image;
        } on Object {
          img = null;
        }
      }
      _images[att] = img;
      return img;
    }();
  }

  Future<PdfDocument?> _pdf(String att) async {
    if (_pdfs.containsKey(att)) return _pdfs[att];
    final data = doc.attachments[att];
    PdfDocument? d;
    if (data != null) {
      try {
        d = await PdfDocument.openData(Uint8List.fromList(data));
      } on Object {
        d = null;
      }
    }
    return _pdfs[att] = d;
  }

  ui.Image? pdfPage(String att, int index) => _pdfPages['$att#$index']?.$2;

  /// Makes sure the PDF background is rendered at least [pixelWidth] wide.
  Future<void> loadPdfPage(String att, int index, double pixelWidth) async {
    final key = '$att#$index';
    final have = _pdfPages[key];
    if (have != null && have.$1 >= pixelWidth * 0.95) return;
    final d = await _pdf(att);
    if (d == null || index < 0 || index >= d.pages.length) return;
    final pg = d.pages[index];
    final w = pixelWidth.clamp(64, 4096).toDouble();
    final h = w * pg.height / pg.width;
    final img = await pg.render(fullWidth: w, fullHeight: h, backgroundColor: 0xffffffff);
    if (img == null) return;
    final ui.Image uiImg = await img.createImage();
    img.dispose();
    _pdfPages[key] = (w, uiImg);
  }

  /// Loads everything a page needs before it can be painted synchronously.
  Future<void> prepare(Page page, double scale) async {
    final bg = page.background;
    if (bg.kind == 'pdf' && bg.att != null) await loadPdfPage(bg.att!, bg.index, page.width * scale);
    for (final e in page.elements) {
      if (e is ImageEl) await loadImage(e.att);
    }
  }

  void dispose() {
    for (final d in _pdfs.values) {
      d?.dispose();
    }
  }
}

void paintBackground(ui.Canvas c, Page page, Resources res) {
  final bg = page.background;
  if (bg.kind == 'pdf' && bg.att != null) {
    c.drawRect(ui.Rect.fromLTWH(0, 0, page.width, page.height), ui.Paint()..color = const ui.Color(0xffffffff));
    final img = res.pdfPage(bg.att!, bg.index);
    if (img != null) {
      c.drawImageRect(img, ui.Rect.fromLTWH(0, 0, img.width.toDouble(), img.height.toDouble()),
          ui.Rect.fromLTWH(0, 0, page.width, page.height), ui.Paint()..filterQuality = ui.FilterQuality.medium);
    }
  } else {
    paintPaper(c, page);
  }
}

double pressureWidth(double width, double p) => width * (0.35 + 0.9 * p);

ui.Path smoothPath(List<(double, double)> pts) {
  final path = ui.Path()..moveTo(pts[0].$1, pts[0].$2);
  if (pts.length == 1) return path..lineTo(pts[0].$1 + 0.01, pts[0].$2);
  if (pts.length == 2) return path..lineTo(pts[1].$1, pts[1].$2);
  for (var i = 1; i < pts.length - 1; i++) {
    final mx = (pts[i].$1 + pts[i + 1].$1) / 2, my = (pts[i].$2 + pts[i + 1].$2) / 2;
    path.quadraticBezierTo(pts[i].$1, pts[i].$2, mx, my);
  }
  return path..lineTo(pts.last.$1, pts.last.$2);
}

void paintStroke(ui.Canvas c, Stroke s) {
  final pts = s.points;
  if (pts.isEmpty) return;
  if (s.tool == 'highlighter') {
    // Multiply is applied when the layer is composited.  The explicit layer bounds matter:
    // Impeller derives the backdrop area of advanced blends from the path bounds, which are
    // empty for a perfectly straight horizontal or vertical stroke (it would vanish).
    final b = s.bbox();
    c.saveLayer(
        ui.Rect.fromLTRB(b.$1, b.$2, b.$3, b.$4),
        ui.Paint()
          ..blendMode = ui.BlendMode.multiply
          ..color = const ui.Color.fromARGB(115, 0, 0, 0)); // 45 % opacity
    c.drawPath(
        smoothPath([for (final p in pts) (p.$1, p.$2)]),
        ui.Paint()
          ..style = ui.PaintingStyle.stroke
          ..color = rgba(s.color, 1)
          ..strokeWidth = s.width
          ..strokeCap = ui.StrokeCap.square
          ..strokeJoin = ui.StrokeJoin.round);
    c.restore();
    return;
  }
  final paint = ui.Paint()
    ..color = rgba(s.color)
    ..isAntiAlias = true;
  var pmin = 1.0, pmax = 0.0;
  for (final p in pts) {
    pmin = math.min(pmin, p.$3);
    pmax = math.max(pmax, p.$3);
  }
  if (s.style == 'ball' || pmax - pmin < 0.02) {
    final w = s.style == 'ball' ? s.width : pressureWidth(s.width, pts[0].$3);
    c.drawPath(
        smoothPath([for (final p in pts) (p.$1, p.$2)]),
        paint
          ..style = ui.PaintingStyle.stroke
          ..strokeWidth = w
          ..strokeCap = ui.StrokeCap.round
          ..strokeJoin = ui.StrokeJoin.round);
  } else {
    c.drawPath(variableOutline(pts, s.width), paint..style = ui.PaintingStyle.fill);
  }
}

/// Closed outline of a pressure-sensitive stroke (left side, end cap, right side, start cap).
ui.Path variableOutline(List<(double, double, double)> raw, double width) {
  final pts = <(double, double, double)>[raw.first];
  for (final p in raw.skip(1)) {
    if ((p.$1 - pts.last.$1).abs() + (p.$2 - pts.last.$2).abs() > 0.3) pts.add(p);
  }
  if (pts.length < 2) {
    final r = pressureWidth(width, pts[0].$3) / 2;
    return ui.Path()..addOval(ui.Rect.fromCircle(center: ui.Offset(pts[0].$1, pts[0].$2), radius: r));
  }
  final n = pts.length;
  final sm = [
    for (var i = 0; i < n; i++) (pts[math.max(0, i - 1)].$3 + 2 * pts[i].$3 + pts[math.min(n - 1, i + 1)].$3) / 4
  ];
  final left = <(double, double)>[], right = <(double, double)>[];
  for (var i = 0; i < n; i++) {
    final a = pts[math.max(0, i - 1)], b = pts[math.min(n - 1, i + 1)];
    final dx = b.$1 - a.$1, dy = b.$2 - a.$2;
    var ln = math.sqrt(dx * dx + dy * dy);
    if (ln == 0) ln = 1;
    final nx = -dy / ln, ny = dx / ln;
    final r = pressureWidth(width, sm[i]) / 2;
    left.add((pts[i].$1 + nx * r, pts[i].$2 + ny * r));
    right.add((pts[i].$1 - nx * r, pts[i].$2 - ny * r));
  }
  final path = smoothPath(left);
  final rEnd = pressureWidth(width, sm.last) / 2;
  path.arcToPoint(ui.Offset(right.last.$1, right.last.$2), radius: ui.Radius.circular(rEnd), clockwise: true);
  final rr = right.reversed.toList();
  for (var i = 1; i < rr.length - 1; i++) {
    final mx = (rr[i].$1 + rr[i + 1].$1) / 2, my = (rr[i].$2 + rr[i + 1].$2) / 2;
    path.quadraticBezierTo(rr[i].$1, rr[i].$2, mx, my);
  }
  path.lineTo(rr.last.$1, rr.last.$2);
  final rStart = pressureWidth(width, sm.first) / 2;
  path.arcToPoint(ui.Offset(left.first.$1, left.first.$2), radius: ui.Radius.circular(rStart), clockwise: true);
  path.close();
  return path;
}

TextPainter textPainter(TextEl t) {
  final tp = TextPainter(
    text: TextSpan(
        text: t.text,
        style: TextStyle(fontSize: t.size, color: rgba(t.color), fontFamily: 'NotoSans', height: 1.17)),
    textDirection: TextDirection.ltr,
    strutStyle: t.lineH > 0
        ? StrutStyle(fontFamily: 'NotoSans', fontSize: t.size, height: t.lineH / t.size, forceStrutHeight: true)
        : null,
  )..layout(maxWidth: math.max(10, t.w));
  return tp;
}

void paintText(ui.Canvas c, TextEl t) {
  final tp = textPainter(t);
  var dy = 0.0;
  if (t.lineH > 0) {
    final lines = tp.computeLineMetrics();
    if (lines.isNotEmpty) dy = t.lineH * (1 - textBaseline) - lines.first.baseline;
    t.h = math.max(1, lines.length) * t.lineH;
  } else {
    t.h = tp.height + t.size * 0.3;
  }
  tp.paint(c, ui.Offset(t.x, t.y + dy));
}

void paintImage(ui.Canvas c, ImageEl im, Resources res) {
  final img = res.image(im.att);
  final dst = ui.Rect.fromLTWH(im.x, im.y, im.w, im.h);
  if (img == null) {
    c.drawRect(dst, ui.Paint()..color = const ui.Color(0x4d808080));
    return;
  }
  c.drawImageRect(img, ui.Rect.fromLTWH(0, 0, img.width.toDouble(), img.height.toDouble()), dst,
      ui.Paint()..filterQuality = ui.FilterQuality.medium);
}

void paintElement(ui.Canvas c, Element e, Resources res) {
  switch (e) {
    case Stroke s:
      paintStroke(c, s);
    case ImageEl im:
      paintImage(c, im, res);
    case TextEl t:
      paintText(c, t);
  }
}

void paintElements(ui.Canvas c, Page page, Resources res, {Set<String> skip = const {}}) {
  // Images first so ink always lies on top of pictures (as in GoodNotes).
  for (final e in page.elements) {
    if (e is ImageEl && !skip.contains(e.id)) paintImage(c, e, res);
  }
  for (final e in page.elements) {
    if (e is! ImageEl && !skip.contains(e.id)) paintElement(c, e, res);
  }
}

void paintPage(ui.Canvas c, Page page, Resources res, {Set<String> skip = const {}}) {
  c.save();
  c.clipRect(ui.Rect.fromLTWH(0, 0, page.width, page.height));
  paintBackground(c, page, res);
  paintElements(c, page, res, skip: skip);
  c.restore();
}

/// Renders a page into an image [scale] pixels per page unit.
Future<ui.Image> renderPageImage(Page page, Resources res, double scale, {Set<String> skip = const {}}) async {
  await res.prepare(page, scale);
  final rec = ui.PictureRecorder();
  final c = ui.Canvas(rec);
  c.scale(scale);
  paintPage(c, page, res, skip: skip);
  final pic = rec.endRecording();
  final img = await pic.toImage(math.max(1, (page.width * scale).round()), math.max(1, (page.height * scale).round()));
  pic.dispose();
  return img;
}
