// Document model.  The JSON schema matches wellnotes/model.py so notebooks move freely
// between the Linux and the Android app.
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:uuid/uuid.dart';

const _uuid = Uuid();
String newId() => _uuid.v4().toUpperCase();

/// GoodNotes page units per PDF point (A4 = 1091.35 x 1543.46).
const gnScale = 11 / 6;

const pageSizes = {
  'a4': (595.28 * gnScale, 841.89 * gnScale),
  'a4-landscape': (841.89 * gnScale, 595.28 * gnScale),
  'letter': (612 * gnScale, 792 * gnScale),
  'a5': (419.53 * gnScale, 595.28 * gnScale),
};

const paperColors = {
  'white': [1.0, 1.0, 1.0],
  'yellow': [0.996, 0.976, 0.906],
  'gray': [0.95, 0.95, 0.95],
  'dark': [0.16, 0.16, 0.18],
};

const coverColors = [
  '#3d5a80', '#e07a5f', '#81b29a', '#f2cc8f', '#6d597a',
  '#264653', '#b5838d', '#2a9d8f', '#e76f51', '#457b9d',
];

typedef Box = (double, double, double, double);

Box? boxUnion(Iterable<Box> boxes) {
  Box? r;
  for (final b in boxes) {
    r = r == null
        ? b
        : (math.min(r.$1, b.$1), math.min(r.$2, b.$2), math.max(r.$3, b.$3), math.max(r.$4, b.$4));
  }
  return r;
}

List<double> _color(Object? v, [List<double> def = const [0, 0, 0, 1]]) {
  if (v is! List) return List.of(def);
  final c = v.map((e) => (e as num).toDouble()).toList();
  while (c.length < 4) {
    c.add(1.0);
  }
  return c;
}

typedef PointFn = (double, double) Function(double x, double y);

sealed class Element {
  String id;
  Element(String? id) : id = id ?? newId();
  String get kind;
  Box bbox();
  Element transformed(PointFn fn, double scale);
  Element recolored(List<double> color);
  Map<String, Object?> toJson();

  static Element? fromJson(Map<String, Object?> d) => switch (d['kind']) {
        'stroke' => Stroke.fromJson(d),
        'image' => ImageEl.fromJson(d),
        'text' => TextEl.fromJson(d),
        _ => null,
      };
}

class Stroke extends Element {
  final List<(double, double, double)> points; // x, y, pressure
  final List<double> color;
  final double width;
  final String tool; // pen | highlighter
  final String style; // fountain | ball
  Box? _bbox;

  Stroke(this.points, this.color, this.width, {this.tool = 'pen', this.style = 'fountain', String? id}) : super(id);

  @override
  String get kind => 'stroke';

  @override
  Box bbox() {
    if (_bbox != null) return _bbox!;
    var x0 = double.infinity, y0 = double.infinity, x1 = -double.infinity, y1 = -double.infinity;
    for (final p in points) {
      x0 = math.min(x0, p.$1);
      y0 = math.min(y0, p.$2);
      x1 = math.max(x1, p.$1);
      y1 = math.max(y1, p.$2);
    }
    return _bbox = (x0 - width, y0 - width, x1 + width, y1 + width);
  }

  @override
  Stroke transformed(PointFn fn, double scale) => Stroke(
      [for (final p in points) (fn(p.$1, p.$2).$1, fn(p.$1, p.$2).$2, p.$3)], color, width * scale,
      tool: tool, style: style);

  @override
  Stroke recolored(List<double> c) => Stroke(points, [c[0], c[1], c[2], color[3]], width, tool: tool, style: style);

  double _r(double v, int d) {
    final f = math.pow(10, d);
    return (v * f).roundToDouble() / f;
  }

  @override
  Map<String, Object?> toJson() => {
        'kind': 'stroke', 'id': id, 'color': color, 'width': width, 'tool': tool, 'style': style,
        'points': [for (final p in points) [_r(p.$1, 2), _r(p.$2, 2), _r(p.$3, 3)]],
      };

  factory Stroke.fromJson(Map<String, Object?> d) => Stroke(
        [
          for (final p in (d['points'] as List).cast<List>())
            ((p[0] as num).toDouble(), (p[1] as num).toDouble(), p.length > 2 ? (p[2] as num).toDouble() : 0.5)
        ],
        _color(d['color']),
        (d['width'] as num).toDouble(),
        tool: d['tool'] as String? ?? 'pen',
        style: d['style'] as String? ?? 'fountain',
        id: d['id'] as String?,
      );
}

class ImageEl extends Element {
  final String att;
  final double x, y, w, h;
  ImageEl(this.att, this.x, this.y, this.w, this.h, {String? id}) : super(id);

  @override
  String get kind => 'image';

  @override
  Box bbox() => (x, y, x + w, y + h);

  @override
  ImageEl transformed(PointFn fn, double scale) {
    final (nx, ny) = fn(x, y);
    return ImageEl(att, nx, ny, w * scale, h * scale);
  }

  @override
  ImageEl recolored(List<double> color) => this;

  @override
  Map<String, Object?> toJson() => {'kind': 'image', 'id': id, 'att': att, 'x': x, 'y': y, 'w': w, 'h': h};

  factory ImageEl.fromJson(Map<String, Object?> d) => ImageEl(d['att'] as String, (d['x'] as num).toDouble(),
      (d['y'] as num).toDouble(), (d['w'] as num).toDouble(), (d['h'] as num).toDouble(),
      id: d['id'] as String?);
}

/// A text box.  With [lineH] > 0 the lines sit on the ruled lines of the paper.
class TextEl extends Element {
  final String text;
  final double x, y, w, size, lineH;
  final List<double> color;
  final String font;
  double h;

  TextEl(this.text, this.x, this.y, this.w,
      {this.size = 28, List<double>? color, this.font = 'Sans', this.lineH = 0, String? id, double? h})
      : color = color ?? const [0, 0, 0, 1],
        h = h ?? (lineH > 0 ? lineH : size * 1.5),
        super(id);

  @override
  String get kind => 'text';

  @override
  Box bbox() => (x, y, x + w, y + math.max(h, lineH > 0 ? lineH : size * 1.5));

  TextEl copyWith({String? text, double? x, double? y, double? w, double? size, List<double>? color, double? lineH}) =>
      TextEl(text ?? this.text, x ?? this.x, y ?? this.y, w ?? this.w,
          size: size ?? this.size, color: color ?? this.color, font: font, lineH: lineH ?? this.lineH, h: h);

  @override
  TextEl transformed(PointFn fn, double scale) {
    final (nx, ny) = fn(x, y);
    return copyWith(x: nx, y: ny, w: w * scale, size: size * scale, lineH: lineH * scale)..h = h * scale;
  }

  @override
  TextEl recolored(List<double> c) => copyWith(color: [c[0], c[1], c[2], 1]);

  @override
  Map<String, Object?> toJson() => {
        'kind': 'text', 'id': id, 'text': text, 'x': x, 'y': y, 'w': w, 'size': size, 'color': color,
        'font': font, if (lineH > 0) 'line_h': lineH,
      };

  factory TextEl.fromJson(Map<String, Object?> d) => TextEl(
        d['text'] as String? ?? '', (d['x'] as num).toDouble(), (d['y'] as num).toDouble(),
        (d['w'] as num).toDouble(),
        size: (d['size'] as num?)?.toDouble() ?? 28, color: _color(d['color']),
        font: d['font'] as String? ?? 'Sans', lineH: (d['line_h'] as num?)?.toDouble() ?? 0, id: d['id'] as String?,
      );
}

/// kind: blank | lined | narrow | grid | dotted | pdf
class Background {
  String kind;
  List<double> color;
  String? att;
  int index;
  Background({this.kind = 'blank', List<double>? color, this.att, this.index = 0})
      : color = color ?? [1.0, 1.0, 1.0];

  Background copy() => Background(kind: kind, color: List.of(color), att: att, index: index);

  Map<String, Object?> toJson() => {'kind': kind, 'color': color.take(3).toList(), 'att': att, 'index': index};

  factory Background.fromJson(Map<String, Object?>? d) => d == null
      ? Background()
      : Background(
          kind: d['kind'] as String? ?? 'blank',
          color: _color(d['color'], const [1, 1, 1, 1]).take(3).toList(),
          att: d['att'] as String?,
          index: (d['index'] as num?)?.toInt() ?? 0,
        );
}

class Page {
  final String id;
  double width, height;
  Background background;
  List<Element> elements;
  int version = 0;

  Page(this.width, this.height, {Background? background, List<Element>? elements, String? id})
      : id = id ?? newId(),
        background = background ?? Background(),
        elements = elements ?? [];

  void touch() => version++;

  Page blankCopy() => Page(width, height, background: background.copy());
  Page duplicate() => Page(width, height, background: background.copy(), elements: List.of(elements));

  Map<String, Object?> toJson() => {
        'id': id, 'width': width, 'height': height, 'background': background.toJson(),
        'elements': [for (final e in elements) e.toJson()],
      };

  factory Page.fromJson(Map<String, Object?> d) => Page(
        (d['width'] as num).toDouble(),
        (d['height'] as num).toDouble(),
        background: Background.fromJson(d['background'] as Map<String, Object?>?),
        elements: [
          for (final e in (d['elements'] as List? ?? const []))
            ?Element.fromJson((e as Map).cast<String, Object?>())
        ],
        id: d['id'] as String?,
      );
}

class _UndoState {
  final List<(Page, List<Element>)> pages;
  final List<Page>? structure;
  _UndoState(this.pages, this.structure);
}

class Document {
  String id = newId();
  String title;
  List<Page> pages = [];
  Map<String, Uint8List> attachments = {};
  String cover = coverColors.first;
  double created = DateTime.now().millisecondsSinceEpoch / 1000;
  double modified = DateTime.now().millisecondsSinceEpoch / 1000;
  String? path;
  bool dirty = false;
  final List<(_UndoState, _UndoState)> _undo = [];
  final List<(_UndoState, _UndoState)> _redo = [];

  Document(this.title);

  bool get canUndo => _undo.isNotEmpty;
  bool get canRedo => _redo.isNotEmpty;

  String addAttachment(Uint8List data) {
    for (final e in attachments.entries) {
      if (e.value.length == data.length && _same(e.value, data)) return e.key;
    }
    final id = newId();
    attachments[id] = data;
    return id;
  }

  static bool _same(Uint8List a, Uint8List b) {
    for (var i = 0; i < a.length; i++) {
      if (a[i] != b[i]) return false;
    }
    return true;
  }

  _UndoState _snap(List<Page> pages, bool structure) =>
      _UndoState([for (final p in pages) (p, List.of(p.elements))], structure ? List.of(this.pages) : null);

  /// Starts an undoable change; pass the token to [commit] afterwards.
  Object begin({List<Page> pages = const [], bool structure = false}) => _snap(pages, structure);

  void commit(Object token) {
    final before = token as _UndoState;
    final after = _snap([for (final (p, _) in before.pages) p], before.structure != null);
    _undo.add((before, after));
    if (_undo.length > 200) _undo.removeAt(0);
    _redo.clear();
    _changed(before);
  }

  void _apply(_UndoState s) {
    if (s.structure != null) {
      pages
        ..clear()
        ..addAll(s.structure!);
    }
    for (final (p, els) in s.pages) {
      p.elements = List.of(els);
    }
  }

  void _changed(_UndoState s) {
    for (final (p, _) in s.pages) {
      p.touch();
    }
    dirty = true;
    modified = DateTime.now().millisecondsSinceEpoch / 1000;
  }

  /// Returns the affected pages, or null if there was nothing to undo.
  List<Page>? undo() {
    if (_undo.isEmpty) return null;
    final (before, after) = _undo.removeLast();
    _apply(before);
    _redo.add((before, after));
    _changed(before);
    return [for (final (p, _) in before.pages) p];
  }

  List<Page>? redo() {
    if (_redo.isEmpty) return null;
    final (before, after) = _redo.removeLast();
    _apply(after);
    _undo.add((before, after));
    _changed(after);
    return [for (final (p, _) in after.pages) p];
  }

  Map<String, Object?> toJson() => {
        'format': 'wellnotes', 'version': 1, 'id': id, 'title': title, 'cover': cover,
        'created': created, 'modified': modified, 'pages': [for (final p in pages) p.toJson()],
      };

  factory Document.fromJson(Map<String, Object?> d, Map<String, Uint8List> attachments) {
    final doc = Document(d['title'] as String? ?? 'Notizbuch')
      ..id = d['id'] as String? ?? newId()
      ..cover = d['cover'] as String? ?? coverColors.first
      ..created = (d['created'] as num?)?.toDouble() ?? 0
      ..modified = (d['modified'] as num?)?.toDouble() ?? 0
      ..attachments = attachments;
    doc.pages = [for (final p in (d['pages'] as List? ?? const [])) Page.fromJson((p as Map).cast<String, Object?>())];
    return doc;
  }
}

bool pointInPolygon(double x, double y, List<(double, double)> poly) {
  var inside = false;
  for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    final (xi, yi) = poly[i];
    final (xj, yj) = poly[j];
    if ((yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / ((yj - yi) == 0 ? 1e-9 : (yj - yi)) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

double distPointSegment(double px, double py, double ax, double ay, double bx, double by) {
  final dx = bx - ax, dy = by - ay;
  final ll = dx * dx + dy * dy;
  if (ll == 0) return math.sqrt((px - ax) * (px - ax) + (py - ay) * (py - ay));
  final t = (((px - ax) * dx + (py - ay) * dy) / ll).clamp(0.0, 1.0);
  final qx = ax + t * dx, qy = ay + t * dy;
  return math.sqrt((px - qx) * (px - qx) + (py - qy) * (py - qy));
}
