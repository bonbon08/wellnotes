// Turns a hand-drawn stroke into a clean shape (port of wellnotes/shapes.py).
import 'dart:math' as math;

typedef Pt = (double, double);

double _hypot(double a, double b) => math.sqrt(a * a + b * b);

double _length(List<Pt> pts) {
  var l = 0.0;
  for (var i = 0; i + 1 < pts.length; i++) {
    l += _hypot(pts[i + 1].$1 - pts[i].$1, pts[i + 1].$2 - pts[i].$2);
  }
  return l;
}

List<Pt> _dp(List<Pt> pts, double eps) {
  if (pts.length < 3) return List.of(pts);
  final (ax, ay) = pts.first;
  final (bx, by) = pts.last;
  final dx = bx - ax, dy = by - ay;
  final ln = _hypot(dx, dy);
  var best = -1.0;
  var idx = 0;
  for (var i = 1; i < pts.length - 1; i++) {
    final (px, py) = pts[i];
    final d = ln == 0 ? _hypot(px - ax, py - ay) : (dy * px - dx * py + bx * ay - by * ax).abs() / ln;
    if (d > best) {
      best = d;
      idx = i;
    }
  }
  if (best > eps) {
    return [..._dp(pts.sublist(0, idx + 1), eps)..removeLast(), ..._dp(pts.sublist(idx), eps)];
  }
  return [pts.first, pts.last];
}

List<Pt> densify(List<Pt> corners, {double step = 3.0, bool closed = false}) {
  final pts = [...corners, if (closed) corners.first];
  final out = <Pt>[];
  for (var i = 0; i + 1 < pts.length; i++) {
    final (x0, y0) = pts[i];
    final (x1, y1) = pts[i + 1];
    final n = math.max(1, (_hypot(x1 - x0, y1 - y0) / step).floor());
    for (var k = 0; k < n; k++) {
      final t = k / n;
      out.add((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t));
    }
  }
  out.add(pts.last);
  return out;
}

double _angle(Pt a, Pt b, Pt c) {
  final v1 = (a.$1 - b.$1, a.$2 - b.$2), v2 = (c.$1 - b.$1, c.$2 - b.$2);
  var n = _hypot(v1.$1, v1.$2) * _hypot(v2.$1, v2.$2);
  if (n == 0) n = 1;
  return math.acos(((v1.$1 * v2.$1 + v1.$2 * v2.$2) / n).clamp(-1.0, 1.0)) * 180 / math.pi;
}

/// Recognised shape as a polyline, or null to keep the freehand stroke.
List<Pt>? recognize(List<Pt> pts) {
  if (pts.length < 3) return null;
  final L = _length(pts);
  if (L < 10) return null;
  final xs = pts.map((p) => p.$1), ys = pts.map((p) => p.$2);
  var diag = _hypot(xs.reduce(math.max) - xs.reduce(math.min), ys.reduce(math.max) - ys.reduce(math.min));
  if (diag == 0) diag = 1;
  final gap = _hypot(pts.last.$1 - pts.first.$1, pts.last.$2 - pts.first.$2);

  if (gap > 0.25 * L) {
    final (ax, ay) = pts.first;
    final (bx, by) = pts.last;
    var dev = 0.0;
    for (final (px, py) in pts) {
      dev = math.max(dev, ((by - ay) * px - (bx - ax) * py + bx * ay - by * ax).abs() / gap);
    }
    if (dev < math.max(6.0, 0.08 * gap)) return densify([pts.first, pts.last]);
    final simp = _dp(pts, 0.08 * diag);
    return simp.length <= 4 ? densify(simp) : null;
  }

  final cx = xs.reduce((a, b) => a + b) / pts.length, cy = ys.reduce((a, b) => a + b) / pts.length;
  var simp = _dp(pts, 0.07 * diag);
  if (simp.length > 2 && _hypot(simp.last.$1 - simp.first.$1, simp.last.$2 - simp.first.$2) < 0.2 * diag) {
    simp = simp.sublist(0, simp.length - 1);
  }
  final merged = <Pt>[];
  for (final p in simp) {
    if (merged.isEmpty || _hypot(p.$1 - merged.last.$1, p.$2 - merged.last.$2) > 0.12 * diag) merged.add(p);
  }
  if (merged.length > 3 && _hypot(merged.last.$1 - merged.first.$1, merged.last.$2 - merged.first.$2) < 0.12 * diag) {
    merged.removeLast();
  }
  final radii = [for (final (x, y) in pts) _hypot(x - cx, y - cy)];
  final meanR = radii.reduce((a, b) => a + b) / radii.length;
  final variance = math.sqrt(radii.map((r) => (r - meanR) * (r - meanR)).reduce((a, b) => a + b) / radii.length) /
      (meanR == 0 ? 1 : meanR);
  var sharp = 0;
  for (var i = 0; i < merged.length; i++) {
    if (_angle(merged[(i - 1 + merged.length) % merged.length], merged[i], merged[(i + 1) % merged.length]) < 135) {
      sharp++;
    }
  }
  if (merged.length == 3) return densify(merged, closed: true);
  if (merged.length == 4 && sharp >= 3) {
    final angles = [for (var i = 0; i < 4; i++) _angle(merged[(i + 3) % 4], merged[i], merged[(i + 1) % 4])];
    if (angles.every((a) => (a - 90).abs() < 25)) {
      final axis = [
        for (var i = 0; i < 4; i++)
          (math.atan2(merged[(i + 1) % 4].$2 - merged[i].$2, merged[(i + 1) % 4].$1 - merged[i].$1) * 180 / math.pi) %
              90
      ];
      if (axis.every((a) => math.min(a, 90 - a) < 12)) {
        final mx = merged.map((p) => p.$1), my = merged.map((p) => p.$2);
        final x0 = mx.reduce(math.min), x1 = mx.reduce(math.max), y0 = my.reduce(math.min), y1 = my.reduce(math.max);
        return densify([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed: true);
      }
    }
    return densify(merged, closed: true);
  }
  if (variance < 0.3 || sharp < 3) return _ellipse(pts, cx, cy);
  if (merged.length >= 5 && merged.length <= 8) return densify(merged, closed: true);
  return _ellipse(pts, cx, cy);
}

List<Pt> _ellipse(List<Pt> pts, double cx, double cy) {
  var sxx = 0.0, syy = 0.0, sxy = 0.0;
  for (final (x, y) in pts) {
    sxx += (x - cx) * (x - cx);
    syy += (y - cy) * (y - cy);
    sxy += (x - cx) * (y - cy);
  }
  final theta = 0.5 * math.atan2(2 * sxy, sxx - syy);
  final ct = math.cos(theta), st = math.sin(theta);
  final us = [for (final (x, y) in pts) (x - cx) * ct + (y - cy) * st];
  final vs = [for (final (x, y) in pts) -(x - cx) * st + (y - cy) * ct];
  var a = (us.reduce(math.max) - us.reduce(math.min)) / 2;
  var b = (vs.reduce(math.max) - vs.reduce(math.min)) / 2;
  final ou = (us.reduce(math.max) + us.reduce(math.min)) / 2, ov = (vs.reduce(math.max) + vs.reduce(math.min)) / 2;
  final ccx = cx + ou * ct - ov * st, ccy = cy + ou * st + ov * ct;
  if (math.min(a, b) > 0.85 * math.max(a, b)) a = b = (a + b) / 2;
  return [
    for (var k = 0; k <= 96; k++)
      (
        ccx + a * math.cos(2 * math.pi * k / 96) * ct - b * math.sin(2 * math.pi * k / 96) * st,
        ccy + a * math.cos(2 * math.pi * k / 96) * st + b * math.sin(2 * math.pi * k / 96) * ct,
      )
  ];
}
