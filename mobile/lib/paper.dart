// Paper patterns (blank, lined, narrow, grid, dotted), identical to wellnotes/render.py.
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:pdf/pdf.dart' as pdf;

import 'model.dart';

const lineSpacing = 29.333; // GoodNotes ruled line spacing in page units
const textBaseline = 0.24; // baseline sits this fraction of a line above the ruled line

bool isDark(List<double> c) => 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2] < 0.5;

/// (line spacing, y of the first ruled line, left margin) for lined/grid/dotted paper.
(double, double, double)? paperLines(Page page) {
  final bg = page.background;
  if (bg.kind == 'lined' || bg.kind == 'narrow') {
    final step = bg.kind == 'lined' ? lineSpacing : lineSpacing * 0.75;
    return (step, step * 3.5, step * 3.2);
  }
  if (bg.kind == 'grid' || bg.kind == 'dotted') {
    const step = lineSpacing * 0.85;
    return (step, (page.height % step) / 2, (page.width % step) / 2);
  }
  return null;
}

List<double> _lineColor(List<double> paper) => isDark(paper) ? [0.35, 0.37, 0.42] : [0.75, 0.82, 0.9];

/// Paints the paper (not PDF backgrounds) in page units onto a Flutter canvas.
void paintPaper(ui.Canvas c, Page page) {
  final bg = page.background;
  final w = page.width, h = page.height;
  c.drawRect(ui.Rect.fromLTWH(0, 0, w, h),
      ui.Paint()..color = ui.Color.from(alpha: 1, red: bg.color[0], green: bg.color[1], blue: bg.color[2]));
  final lc = _lineColor(bg.color);
  final line = ui.Paint()
    ..color = ui.Color.from(alpha: 1, red: lc[0], green: lc[1], blue: lc[2])
    ..strokeWidth = 1.0;
  if (bg.kind == 'lined' || bg.kind == 'narrow') {
    final step = bg.kind == 'lined' ? lineSpacing : lineSpacing * 0.75;
    for (var y = step * 3.5; y < h - step; y += step) {
      c.drawLine(ui.Offset(0, y), ui.Offset(w, y), line);
    }
    c.drawLine(ui.Offset(step * 3.2, 0), ui.Offset(step * 3.2, h),
        ui.Paint()
          ..color = const ui.Color.fromARGB(178, 230, 115, 115)
          ..strokeWidth = 1.0);
  } else if (bg.kind == 'grid') {
    const step = lineSpacing * 0.85;
    line.strokeWidth = 0.8;
    for (var x = (w % step) / 2; x < w; x += step) {
      c.drawLine(ui.Offset(x, 0), ui.Offset(x, h), line);
    }
    for (var y = (h % step) / 2; y < h; y += step) {
      c.drawLine(ui.Offset(0, y), ui.Offset(w, y), line);
    }
  } else if (bg.kind == 'dotted') {
    const step = lineSpacing * 0.85;
    final dot = ui.Paint()..color = ui.Color.from(alpha: 1, red: lc[0] * 0.9, green: lc[1] * 0.9, blue: lc[2] * 0.9);
    for (var y = (h % step) / 2; y < h; y += step) {
      for (var x = (w % step) / 2; x < w; x += step) {
        c.drawCircle(ui.Offset(x, y), 1.4, dot);
      }
    }
  }
}

/// Same pattern for the PDF page templates (PDF origin is bottom-left, units are points).
void paintPaperPdf(pdf.PdfGraphics g, Page page, double wPt, double hPt) {
  final s = 1 / gnScale;
  final bg = page.background;
  final w = page.width, h = page.height;
  g.setFillColor(pdf.PdfColor(bg.color[0], bg.color[1], bg.color[2]));
  g.drawRect(0, 0, wPt, hPt);
  g.fillPath();
  final lc = _lineColor(bg.color);
  double py(double y) => hPt - y * s;
  if (bg.kind == 'lined' || bg.kind == 'narrow') {
    final step = bg.kind == 'lined' ? lineSpacing : lineSpacing * 0.75;
    g.setStrokeColor(pdf.PdfColor(lc[0], lc[1], lc[2]));
    g.setLineWidth(1.0 * s);
    for (var y = step * 3.5; y < h - step; y += step) {
      g.drawLine(0, py(y), wPt, py(y));
    }
    g.strokePath();
    g.setStrokeColor(const pdf.PdfColor(0.9, 0.45, 0.45, 0.7));
    g.drawLine(step * 3.2 * s, 0, step * 3.2 * s, hPt);
    g.strokePath();
  } else if (bg.kind == 'grid') {
    const step = lineSpacing * 0.85;
    g.setStrokeColor(pdf.PdfColor(lc[0], lc[1], lc[2]));
    g.setLineWidth(0.8 * s);
    for (var x = (w % step) / 2; x < w; x += step) {
      g.drawLine(x * s, 0, x * s, hPt);
    }
    for (var y = (h % step) / 2; y < h; y += step) {
      g.drawLine(0, py(y), wPt, py(y));
    }
    g.strokePath();
  } else if (bg.kind == 'dotted') {
    const step = lineSpacing * 0.85;
    g.setFillColor(pdf.PdfColor(lc[0] * 0.9, lc[1] * 0.9, lc[2] * 0.9));
    for (var y = (h % step) / 2; y < h; y += step) {
      for (var x = (w % step) / 2; x < w; x += step) {
        g.drawEllipse(x * s, py(y), 1.4 * s, 1.4 * s);
      }
    }
    g.fillPath();
  }
}

double clampD(double v, double lo, double hi) => math.max(lo, math.min(hi, v));
