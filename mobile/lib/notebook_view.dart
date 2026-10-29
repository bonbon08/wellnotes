// The notebook canvas: continuous vertical page view with stylus, touch and mouse input
// (counterpart of wellnotes/canvas.py).
import 'dart:async';
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart' hide Element, Page;
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart';

import 'model.dart';
import 'paper.dart';
import 'render.dart';
import 'shapes.dart' as shapes;
import 'tools.dart';

const double kPad = 40; // page units around and between pages
const int kMaxCachePixels = 8000000;

class _Cache {
  final int version;
  final double scale;
  final Set<String> skip;
  final ui.Image image;
  _Cache(this.version, this.scale, this.skip, this.image);
}

/// Clipboard shared between notebooks: elements plus the attachments they need.
final List<(Element, Map<String, List<int>>)> elementClipboard = [];

class NotebookView extends StatefulWidget {
  final Document doc;
  final ToolSettings tools;
  final VoidCallback onChanged;
  final ValueChanged<int> onPageChanged;
  final VoidCallback onSelectionChanged;

  const NotebookView({
    super.key,
    required this.doc,
    required this.tools,
    required this.onChanged,
    required this.onPageChanged,
    required this.onSelectionChanged,
  });

  @override
  State<NotebookView> createState() => NotebookViewState();
}

enum _Mode { none, ink, shape, erase, lasso, move, scale, pan, pinch, text }

class NotebookViewState extends State<NotebookView> with SingleTickerProviderStateMixin {
  late final Resources res = Resources(widget.doc);
  final Map<String, _Cache> _cache = {};
  final Set<String> _pending = {};
  final Map<String, List<Element>> _committed = {}; // drawn on top until the cache catches up
  Timer? _rerender;

  // geometry
  double zoom = 0.5;
  bool fitWidth = true;
  double _scrollX = 0, _scrollY = 0;
  Size _view = Size.zero;
  List<(double, double)> _layout = [];
  Size _content = const Size(1, 1);
  int _currentPage = 0;

  // interaction
  _Mode _mode = _Mode.none;
  Page? _page;
  int _pageIndex = 0;
  List<(double, double, double)> _points = [];
  bool _pressured = false;
  Object? _token;
  bool _erased = false;
  (double, double)? _lastErase;
  List<(double, double)> _lasso = [];
  Offset _dragStart = Offset.zero;
  Offset? _hover;
  bool _stylusDown = false;
  bool _stylusSeen = false;
  String _activeTool = 'pen';
  int? _drawPointer;
  final Map<int, Offset> _touches = {};
  final Map<int, VelocityTracker> _velocity = {};
  (double, double)? _pinchAnchor;
  double _pinchStartZoom = 1, _pinchStartDist = 1;
  Offset _panStartScroll = Offset.zero, _panStartPos = Offset.zero;
  late final Ticker _ticker = createTicker(_kineticTick);
  Offset _kineticVelocity = Offset.zero;
  Duration _lastTick = Duration.zero;

  // selection
  Page? selectionPage;
  Set<String> selectionIds = {};
  Box? _selBox;
  Offset _selOffset = Offset.zero;
  double _selScale = 1;
  (double, double, double, double)? _selAnchor;

  // text editing
  TextEl? _editing;
  Page? _editingPage;
  TextEl? _editingOriginal;
  final TextEditingController _textCtl = TextEditingController();
  final FocusNode _textFocus = FocusNode();

  Document get doc => widget.doc;
  int get currentPage => math.min(_currentPage, doc.pages.length - 1);
  bool get hasSelection => selectionPage != null && selectionIds.isNotEmpty;

  @override
  void initState() {
    super.initState();
    relayout();
    _textFocus.addListener(() {
      if (!_textFocus.hasFocus) commitText();
    });
  }

  @override
  void dispose() {
    _ticker.dispose();
    _rerender?.cancel();
    res.dispose();
    _textCtl.dispose();
    _textFocus.dispose();
    super.dispose();
  }

  // ------------------------------------------------------------------ layout

  void relayout() {
    final maxW = doc.pages.fold<double>(1000, (m, p) => math.max(m, p.width));
    var y = kPad;
    _layout = [];
    for (final p in doc.pages) {
      _layout.add(((maxW - p.width) / 2 + kPad, y));
      y += p.height + kPad;
    }
    _content = Size(maxW + 2 * kPad, y);
    _clampScroll();
    if (mounted) setState(() {});
  }

  void _applyFit() {
    if (fitWidth && _view.width > 0) zoom = ((_view.width - 8) / _content.width).clamp(0.05, 3.0);
  }

  void _clampScroll() {
    final cw = _content.width * zoom, ch = _content.height * zoom;
    _scrollX = cw <= _view.width ? 0 : _scrollX.clamp(0, cw - _view.width);
    _scrollY = _scrollY.clamp(0, math.max(0, ch - _view.height));
    _updateCurrentPage();
  }

  Offset get _origin {
    final cw = _content.width * zoom;
    return Offset(cw < _view.width ? (_view.width - cw) / 2 : -_scrollX, -_scrollY);
  }

  Rect pageRect(int i) {
    final o = _origin;
    final (px, py) = _layout[i];
    final p = doc.pages[i];
    return Rect.fromLTWH(o.dx + px * zoom, o.dy + py * zoom, p.width * zoom, p.height * zoom);
  }

  (int, double, double) toPage(Offset s, [int? index]) {
    final o = _origin;
    final dx = (s.dx - o.dx) / zoom, dy = (s.dy - o.dy) / zoom;
    var i = index ?? 0;
    if (index == null) {
      for (var k = 0; k < _layout.length; k++) {
        if (dy >= _layout[k].$2 - kPad / 2) i = k;
      }
    }
    final (px, py) = _layout[i];
    return (i, dx - px, dy - py);
  }

  Offset toScreen(int i, double x, double y) {
    final o = _origin;
    final (px, py) = _layout[i];
    return Offset(o.dx + (px + x) * zoom, o.dy + (py + y) * zoom);
  }

  int pageIndex(Page p) => doc.pages.indexOf(p);

  List<int> visibleRange() => [
        for (var i = 0; i < doc.pages.length; i++)
          if (pageRect(i).bottom >= 0 && pageRect(i).top <= _view.height) i
      ];

  void _updateCurrentPage() {
    var best = _currentPage, bestVis = -1.0;
    for (var i = 0; i < doc.pages.length; i++) {
      final r = pageRect(i);
      final vis = math.min(r.bottom, _view.height) - math.max(r.top, 0);
      if (vis > bestVis) {
        best = i;
        bestVis = vis;
      }
    }
    if (best != _currentPage) {
      _currentPage = best;
      WidgetsBinding.instance.addPostFrameCallback((_) => widget.onPageChanged(best));
    }
  }

  void scrollToPage(int i) {
    if (i < 0 || i >= _layout.length) return;
    setState(() {
      _scrollY = (_layout[i].$2 - kPad / 2) * zoom;
      _clampScroll();
    });
  }

  void setZoom(double z, [Offset? focal]) {
    z = z.clamp(0.08, 6.0);
    final f = focal ?? _view.center(Offset.zero);
    final o = _origin;
    final dx = (f.dx - o.dx) / zoom, dy = (f.dy - o.dy) / zoom;
    fitWidth = false;
    zoom = z;
    if (_content.width * z >= _view.width) _scrollX = dx * z - f.dx;
    _scrollY = dy * z - f.dy;
    _clampScroll();
    _scheduleRerender();
    setState(() {});
  }

  void zoomFit() {
    final page = currentPage;
    fitWidth = true;
    _applyFit();
    _clampScroll();
    scrollToPage(page);
    _scheduleRerender();
  }

  // ------------------------------------------------------------------ caches

  double _desiredScale(Page p) {
    final dpr = MediaQuery.of(context).devicePixelRatio;
    return math.min(zoom * dpr, math.sqrt(kMaxCachePixels / (p.width * p.height)));
  }

  void invalidate([Page? page]) {
    if (page == null) {
      _committed.clear();
    } else {
      _committed.remove(page.id);
    }
    setState(() {});
  }

  void _scheduleRerender() {
    _rerender?.cancel();
    _rerender = Timer(const Duration(milliseconds: 200), () {
      if (mounted) setState(() {});
    });
  }

  _Cache? _cacheFor(Page page, Set<String> skip) {
    final c = _cache[page.id];
    final want = _desiredScale(page);
    final stale = c == null ||
        c.version != page.version ||
        !_sameSet(c.skip, skip) ||
        (_mode != _Mode.pinch && !(_rerender?.isActive ?? false) && (c.scale / want - 1).abs() > 0.05);
    if (stale) _request(page, want, skip);
    return c;
  }

  static bool _sameSet(Set<String> a, Set<String> b) => a.length == b.length && a.containsAll(b);

  void _request(Page page, double scale, Set<String> skip) {
    final key = '${page.id}:${page.version}:${scale.toStringAsFixed(3)}:${skip.join(",")}';
    if (_pending.contains(key)) return;
    _pending.add(key);
    final version = page.version;
    // Render a frozen copy so later edits don't race with the raster work.
    final frozen = Page(page.width, page.height, background: page.background, elements: List.of(page.elements), id: page.id);
    renderPageImage(frozen, res, scale, skip: Set.of(skip)).then((img) {
      _pending.remove(key);
      if (!mounted) return;
      if (version != page.version) {
        img.dispose();
        setState(() {});
        return;
      }
      _cache[page.id]?.image.dispose();
      _cache[page.id] = _Cache(version, scale, Set.of(skip), img);
      _committed.remove(page.id);
      setState(() {});
    });
  }

  void _evict(List<int> visible) {
    final keep = <String>{};
    for (final i in visible) {
      for (var j = i - 2; j <= i + 2; j++) {
        if (j >= 0 && j < doc.pages.length) keep.add(doc.pages[j].id);
      }
    }
    _cache.removeWhere((id, c) {
      if (keep.contains(id)) return false;
      c.image.dispose();
      return true;
    });
  }

  // ------------------------------------------------------------------ input

  double _pressure(PointerEvent e) {
    if (e.kind != PointerDeviceKind.stylus && e.kind != PointerDeviceKind.invertedStylus) return 0.5;
    final range = e.pressureMax - e.pressureMin;
    if (range <= 0) return 0.5;
    return ((e.pressure - e.pressureMin) / range).clamp(0.0, 1.0);
  }

  bool _isStylus(PointerEvent e) => e.kind == PointerDeviceKind.stylus || e.kind == PointerDeviceKind.invertedStylus;

  void _onDown(PointerDownEvent e) {
    _ticker.stop();
    if (_editing != null && e.kind != PointerDeviceKind.touch) commitText();
    if (_isStylus(e)) {
      _stylusDown = true;
      _stylusSeen = true;
      if (_mode == _Mode.pan || _mode == _Mode.pinch) _endTouches();
      var tool = widget.tools.tool;
      if (e.kind == PointerDeviceKind.invertedStylus || e.buttons & kSecondaryStylusButton != 0) tool = 'eraser';
      _drawPointer = e.pointer;
      _begin(e.localPosition, tool, _pressure(e), pressured: true);
      return;
    }
    if (e.kind == PointerDeviceKind.touch) {
      if (_stylusDown) return; // palm rejection while the pen is down
      _touches[e.pointer] = e.localPosition;
      _velocity[e.pointer] = VelocityTracker.withKind(e.kind)..addPosition(e.timeStamp, e.localPosition);
      final drawWithFinger = widget.tools.fingerDraw && !_stylusSeen;
      if (_touches.length == 1 && drawWithFinger) {
        _drawPointer = e.pointer;
        _begin(e.localPosition, widget.tools.tool, 0.5);
        return;
      }
      if (_touches.length >= 2) {
        if (_drawPointer != null) _cancel();
        _startPinch();
      } else {
        _startPan(e.localPosition);
      }
      return;
    }
    // mouse / trackpad
    if (e.buttons & kPrimaryMouseButton != 0) {
      _drawPointer = e.pointer;
      _begin(e.localPosition, widget.tools.tool, 0.5);
    } else {
      _touches[e.pointer] = e.localPosition;
      _velocity[e.pointer] = VelocityTracker.withKind(e.kind)..addPosition(e.timeStamp, e.localPosition);
      _startPan(e.localPosition);
    }
  }

  void _onMove(PointerMoveEvent e) {
    if (e.pointer == _drawPointer) {
      _update(e.localPosition, _pressure(e));
      return;
    }
    if (_touches.containsKey(e.pointer)) {
      _touches[e.pointer] = e.localPosition;
      _velocity[e.pointer]?.addPosition(e.timeStamp, e.localPosition);
      if (_mode == _Mode.pinch) {
        _updatePinch();
      } else if (_mode == _Mode.pan) {
        setState(() {
          _scrollX = _panStartScroll.dx - (e.localPosition.dx - _panStartPos.dx);
          _scrollY = _panStartScroll.dy - (e.localPosition.dy - _panStartPos.dy);
          _clampScroll();
        });
      }
    }
  }

  void _onUp(PointerEvent e) {
    if (_isStylus(e)) _stylusDown = false;
    if (e.pointer == _drawPointer) {
      _drawPointer = null;
      _end(e.localPosition);
      return;
    }
    if (_touches.containsKey(e.pointer)) {
      final v = _velocity[e.pointer]?.getVelocity().pixelsPerSecond ?? Offset.zero;
      _touches.remove(e.pointer);
      _velocity.remove(e.pointer);
      if (_mode == _Mode.pinch) {
        if (_touches.length == 1) {
          _startPan(_touches.values.first);
        } else if (_touches.isEmpty) {
          _mode = _Mode.none;
          _scheduleRerender();
        }
      } else if (_mode == _Mode.pan && _touches.isEmpty) {
        _mode = _Mode.none;
        if (e is PointerUpEvent && v.distance > 150) _startKinetic(v);
      }
    }
  }

  void _onCancel(PointerCancelEvent e) {
    if (e.pointer == _drawPointer) {
      _drawPointer = null;
      _cancel();
    }
    _onUp(e);
  }

  void _endTouches() {
    _touches.clear();
    _velocity.clear();
    _mode = _Mode.none;
  }

  void _startPan(Offset pos) {
    _mode = _Mode.pan;
    _panStartScroll = Offset(_scrollX, _scrollY);
    _panStartPos = pos;
  }

  void _startPinch() {
    final pts = _touches.values.take(2).toList();
    final center = (pts[0] + pts[1]) / 2;
    final o = _origin;
    _mode = _Mode.pinch;
    _pinchStartZoom = zoom;
    _pinchStartDist = math.max(1, (pts[0] - pts[1]).distance);
    // The content point under the fingers follows them (zoom + two-finger pan).
    _pinchAnchor = ((center.dx - o.dx) / zoom, (center.dy - o.dy) / zoom);
  }

  void _updatePinch() {
    if (_touches.length < 2 || _pinchAnchor == null) return;
    final pts = _touches.values.take(2).toList();
    final center = (pts[0] + pts[1]) / 2;
    final z = (_pinchStartZoom * (pts[0] - pts[1]).distance / _pinchStartDist).clamp(0.08, 6.0);
    setState(() {
      fitWidth = false;
      zoom = z;
      final (ax, ay) = _pinchAnchor!;
      if (_content.width * z >= _view.width) _scrollX = ax * z - center.dx;
      _scrollY = ay * z - center.dy;
      _clampScroll();
    });
  }

  void _startKinetic(Offset v) {
    _kineticVelocity = v;
    _lastTick = Duration.zero;
    _ticker
      ..stop()
      ..start();
  }

  void _kineticTick(Duration t) {
    final dt = _lastTick == Duration.zero ? 1 / 60 : math.min(0.05, (t - _lastTick).inMicroseconds / 1e6);
    _lastTick = t;
    setState(() {
      _scrollX -= _kineticVelocity.dx * dt;
      _scrollY -= _kineticVelocity.dy * dt;
      _clampScroll();
    });
    _kineticVelocity *= math.exp(-4.0 * dt);
    if (_kineticVelocity.distance < 20) _ticker.stop();
  }

  void _onSignal(PointerSignalEvent e) {
    if (e is PointerScrollEvent) {
      final ctrl = HardwareKeyboard.instance.isControlPressed;
      if (ctrl) {
        setZoom(zoom * math.exp(-e.scrollDelta.dy * 0.002), e.localPosition);
      } else {
        setState(() {
          final shift = HardwareKeyboard.instance.isShiftPressed;
          _scrollX += shift ? e.scrollDelta.dy : e.scrollDelta.dx;
          _scrollY += shift ? 0 : e.scrollDelta.dy;
          _clampScroll();
        });
      }
    }
  }

  void _onPanZoomStart(PointerPanZoomStartEvent e) {
    _ticker.stop();
    final o = _origin;
    _pinchStartZoom = zoom;
    _panStartScroll = Offset(_scrollX, _scrollY);
    _pinchAnchor = ((e.localPosition.dx - o.dx) / zoom, (e.localPosition.dy - o.dy) / zoom);
  }

  void _onPanZoomUpdate(PointerPanZoomUpdateEvent e) {
    setState(() {
      if ((e.scale - 1).abs() > 0.01) {
        fitWidth = false;
        zoom = (_pinchStartZoom * e.scale).clamp(0.08, 6.0);
        final (ax, ay) = _pinchAnchor!;
        final f = e.localPosition + e.pan;
        if (_content.width * zoom >= _view.width) _scrollX = ax * zoom - f.dx;
        _scrollY = ay * zoom - f.dy;
      } else {
        _scrollX = _panStartScroll.dx - e.pan.dx;
        _scrollY = _panStartScroll.dy - e.pan.dy;
      }
      _clampScroll();
    });
  }

  // ------------------------------------------------------------------ tool actions

  void _begin(Offset s, String tool, double p, {bool pressured = false}) {
    _dragStart = s;
    _activeTool = tool;
    final (index, px, py) = toPage(s);
    final page = doc.pages[index];
    if (hasSelection && tool != 'lasso' && tool != 'eraser') clearSelection();
    if (tool == 'lasso' && hasSelection) {
      final hit = _selectionHit(s);
      if (hit != null) {
        _mode = hit;
        _selOffset = Offset.zero;
        _selScale = 1;
        setState(() {});
        return;
      }
      clearSelection();
    }
    _page = page;
    _pageIndex = index;
    switch (tool) {
      case 'pen' || 'highlighter':
        _mode = _Mode.ink;
        _points = [(px, py, p)];
        _pressured = pressured;
      case 'shape':
        _mode = _Mode.shape;
        _points = [(px, py, 0.5)];
        _pressured = false;
      case 'eraser':
        _mode = _Mode.erase;
        _token = doc.begin(pages: [page]);
        _erased = false;
        _lastErase = null;
        _eraseAt(px, py);
      case 'lasso':
        _mode = _Mode.lasso;
        _lasso = [(px, py)];
      case 'text':
        _mode = _Mode.text;
    }
    setState(() {});
  }

  void _update(Offset s, double p) {
    switch (_mode) {
      case _Mode.move || _Mode.scale:
        final i = pageIndex(selectionPage!);
        final (_, x0, y0) = toPage(_dragStart, i);
        final (_, x, y) = toPage(s, i);
        if (_mode == _Mode.move) {
          _selOffset = Offset(x - x0, y - y0);
        } else {
          final (ax, ay, cx0, cy0) = _selAnchor!;
          final d0 = math.max(1e-6, math.sqrt((cx0 - ax) * (cx0 - ax) + (cy0 - ay) * (cy0 - ay)));
          _selScale = math.max(0.05, math.sqrt((x - ax) * (x - ax) + (y - ay) * (y - ay)) / d0);
        }
      case _Mode.ink || _Mode.shape:
        final (_, px, py) = toPage(s, _pageIndex);
        final last = _points.last;
        if (math.sqrt((px - last.$1) * (px - last.$1) + (py - last.$2) * (py - last.$2)) * zoom < 0.8) return;
        _points.add((px, py, _mode == _Mode.ink && _pressured ? p : 0.5));
      case _Mode.erase:
        final (_, px, py) = toPage(s, _pageIndex);
        _eraseAt(px, py);
      case _Mode.lasso:
        final (_, px, py) = toPage(s, _pageIndex);
        _lasso.add((px, py));
      default:
        return;
    }
    setState(() {});
  }

  void _end(Offset s) {
    final mode = _mode;
    _mode = _Mode.none;
    switch (mode) {
      case _Mode.ink || _Mode.shape:
        _finishInk(mode);
      case _Mode.erase:
        if (_erased) {
          doc.commit(_token!);
          widget.onChanged();
        }
        _token = null;
      case _Mode.lasso:
        _finishLasso();
      case _Mode.move || _Mode.scale:
        _finishTransform();
      case _Mode.text:
        _startText(s);
      default:
        break;
    }
    _page = null;
    _points = [];
    _lasso = [];
    setState(() {});
  }

  void _cancel() {
    if (_mode == _Mode.erase && _erased && _token != null) {
      doc.commit(_token!);
      widget.onChanged();
    }
    _mode = _Mode.none;
    _page = null;
    _points = [];
    _lasso = [];
    setState(() {});
  }

  Stroke _liveStroke() {
    final t = widget.tools;
    if (_mode == _Mode.shape) return Stroke(_points, t.shapeColor, t.shapeWidth, style: 'ball');
    if (_activeTool == 'highlighter') return Stroke(_points, t.hlColor, t.hlWidth, tool: 'highlighter');
    return Stroke(_points, t.penColor, t.penWidth, style: t.penStyle);
  }

  void _finishInk(_Mode mode) {
    final page = _page;
    var pts = _points;
    if (page == null || pts.isEmpty) return;
    final t = widget.tools;
    Stroke stroke;
    if (mode == _Mode.shape) {
      final shape = shapes.recognize([for (final p in pts) (p.$1, p.$2)]);
      if (shape != null) pts = [for (final (x, y) in shape) (x, y, 0.5)];
      stroke = Stroke(pts, t.shapeColor, t.shapeWidth, style: 'ball');
    } else {
      if (pts.length == 1) pts = [...pts, (pts[0].$1 + 0.3, pts[0].$2 + 0.3, pts[0].$3)];
      final live = _liveStroke();
      stroke = Stroke(pts, live.color, live.width, tool: live.tool, style: live.style);
    }
    addElements(page, [stroke]);
  }

  void addElements(Page page, List<Element> els) {
    final token = doc.begin(pages: [page]);
    page.elements.addAll(els);
    doc.commit(token);
    // Keep showing the new elements until the page image has been re-rendered.
    _committed.putIfAbsent(page.id, () => []).addAll(els);
    for (final e in els) {
      if (e is ImageEl) res.loadImage(e.att).then((_) => mounted ? setState(() {}) : null);
    }
    setState(() {});
    widget.onChanged();
  }

  void _eraseAt(double x, double y) {
    final page = _page!;
    final r = widget.tools.eraserSize;
    var samples = <(double, double)>[(x, y)];
    if (_lastErase != null) {
      final (lx, ly) = _lastErase!;
      final d = math.sqrt((x - lx) * (x - lx) + (y - ly) * (y - ly));
      final n = (d / (r * 0.5)).floor();
      samples = [for (var k = 1; k <= n; k++) (lx + (x - lx) * k / (n + 1), ly + (y - ly) * k / (n + 1)), (x, y)];
    }
    _lastErase = (x, y);
    final out = <Element>[];
    var changed = false;
    for (final el in page.elements) {
      if (el is! Stroke) {
        out.add(el);
        continue;
      }
      final b = el.bbox();
      final hitBox = samples.any((s) => s.$1 >= b.$1 - r && s.$1 <= b.$3 + r && s.$2 >= b.$2 - r && s.$2 <= b.$4 + r);
      if (!hitBox) {
        out.add(el);
        continue;
      }
      final rr = r + el.width / 2;
      if (widget.tools.eraserPrecise) {
        final pieces = _splitStroke(el, samples, rr);
        if (pieces == null) {
          out.add(el);
          continue;
        }
        out.addAll(pieces);
      } else if (!_strokeHit(el, samples, rr)) {
        out.add(el);
        continue;
      }
      changed = true;
    }
    if (changed) {
      page.elements = out;
      page.touch();
      _committed.remove(page.id);
      _erased = true;
    }
  }

  static bool _strokeHit(Stroke el, List<(double, double)> samples, double r) {
    final pts = el.points;
    for (final (sx, sy) in samples) {
      if (pts.length == 1 && math.sqrt(math.pow(pts[0].$1 - sx, 2) + math.pow(pts[0].$2 - sy, 2)) <= r) return true;
      for (var k = 0; k + 1 < pts.length; k++) {
        if (distPointSegment(sx, sy, pts[k].$1, pts[k].$2, pts[k + 1].$1, pts[k + 1].$2) <= r) return true;
      }
    }
    return false;
  }

  static List<Stroke>? _splitStroke(Stroke el, List<(double, double)> samples, double r) {
    var touched = false;
    final pieces = <List<(double, double, double)>>[];
    var run = <(double, double, double)>[];
    for (final p in el.points) {
      final hit = samples.any((s) => (p.$1 - s.$1) * (p.$1 - s.$1) + (p.$2 - s.$2) * (p.$2 - s.$2) <= r * r);
      if (hit) {
        touched = true;
        if (run.isNotEmpty) pieces.add(run);
        run = [];
      } else {
        run.add(p);
      }
    }
    if (run.isNotEmpty) pieces.add(run);
    if (!touched) return null;
    return [
      for (final r_ in pieces)
        if (r_.length >= 2) Stroke(r_, el.color, el.width, tool: el.tool, style: el.style)
    ];
  }

  // ------------------------------------------------------------------ selection

  void _finishLasso() {
    final page = _page;
    if (page == null || _lasso.length < 3) return;
    final ids = <String>{};
    for (final el in page.elements) {
      if (el is Stroke) {
        final step = math.max(1, el.points.length ~/ 24);
        var inside = 0, n = 0;
        for (var k = 0; k < el.points.length; k += step) {
          n++;
          if (pointInPolygon(el.points[k].$1, el.points[k].$2, _lasso)) inside++;
        }
        if (inside >= math.max(1, n * 0.5)) ids.add(el.id);
      } else {
        final b = el.bbox();
        if (pointInPolygon((b.$1 + b.$3) / 2, (b.$2 + b.$4) / 2, _lasso)) ids.add(el.id);
      }
    }
    if (ids.isNotEmpty) select(page, ids);
  }

  void select(Page page, Set<String> ids) {
    final els = page.elements.where((e) => ids.contains(e.id)).toList();
    if (els.isEmpty) {
      clearSelection();
      return;
    }
    selectionPage = page;
    selectionIds = ids;
    _selBox = boxUnion(els.map((e) => e.bbox()));
    _selOffset = Offset.zero;
    _selScale = 1;
    setState(() {});
    widget.onSelectionChanged();
  }

  void clearSelection() {
    if (selectionPage == null) return;
    selectionPage = null;
    selectionIds = {};
    _selBox = null;
    setState(() {});
    widget.onSelectionChanged();
  }

  List<Element> selectedElements() =>
      selectionPage == null ? [] : selectionPage!.elements.where((e) => selectionIds.contains(e.id)).toList();

  (double, double) Function(double, double) _transformFn() {
    if (_selScale != 1 && _selAnchor != null) {
      final (ax, ay, _, _) = _selAnchor!;
      final s = _selScale;
      return (x, y) => (ax + (x - ax) * s, ay + (y - ay) * s);
    }
    final o = _selOffset;
    return (x, y) => (x + o.dx, y + o.dy);
  }

  Box _transformedBox() {
    final (x0, y0, x1, y1) = _selBox!;
    final f = _transformFn();
    final a = f(x0, y0), b = f(x1, y1);
    return (math.min(a.$1, b.$1), math.min(a.$2, b.$2), math.max(a.$1, b.$1), math.max(a.$2, b.$2));
  }

  Rect? selectionScreenRect() {
    if (!hasSelection || pageIndex(selectionPage!) < 0) return null;
    final i = pageIndex(selectionPage!);
    final (x0, y0, x1, y1) = _transformedBox();
    return Rect.fromPoints(toScreen(i, x0, y0), toScreen(i, x1, y1));
  }

  _Mode? _selectionHit(Offset s) {
    final r = selectionScreenRect();
    if (r == null) return null;
    final rr = r.inflate(6);
    final i = pageIndex(selectionPage!);
    final corners = {rr.topLeft: r.bottomRight, rr.topRight: r.bottomLeft, rr.bottomLeft: r.topRight, rr.bottomRight: r.topLeft};
    for (final e in corners.entries) {
      if ((s - e.key).distance <= 22) {
        final (_, ax, ay) = toPage(e.value, i);
        final (_, cx, cy) = toPage(e.key, i);
        _selAnchor = (ax, ay, cx, cy);
        return _Mode.scale;
      }
    }
    return rr.inflate(8).contains(s) ? _Mode.move : null;
  }

  void _finishTransform() {
    if (!hasSelection) return;
    if (_selOffset == Offset.zero && _selScale == 1) return;
    final page = selectionPage!;
    final f = _transformFn();
    final s = _selScale;
    final mapping = {for (final e in selectedElements()) e.id: e.transformed(f, s)};
    _selOffset = Offset.zero;
    _selScale = 1;
    replaceElements(page, mapping);
    select(page, {for (final e in mapping.values) e.id});
  }

  void replaceElements(Page page, Map<String, Element?> mapping) {
    final token = doc.begin(pages: [page]);
    page.elements = [
      for (final e in page.elements)
        if (!mapping.containsKey(e.id)) e else if (mapping[e.id] != null) mapping[e.id]!
    ];
    doc.commit(token);
    _committed.remove(page.id);
    setState(() {});
    widget.onChanged();
  }

  void deleteSelection() {
    if (!hasSelection) return;
    final page = selectionPage!;
    final ids = Set.of(selectionIds);
    clearSelection();
    replaceElements(page, {for (final id in ids) id: null});
  }

  void recolorSelection(List<double> c) {
    if (!hasSelection) return;
    final page = selectionPage!;
    final mapping = {for (final e in selectedElements()) e.id: e.recolored(c)};
    replaceElements(page, mapping);
    select(page, {for (final e in mapping.values) e.id});
  }

  void duplicateSelection() {
    if (!hasSelection) return;
    final page = selectionPage!;
    final copies = [for (final e in selectedElements()) e.transformed((x, y) => (x + 30, y + 30), 1)];
    addElements(page, copies);
    select(page, {for (final e in copies) e.id});
  }

  void copySelection() {
    elementClipboard
      ..clear()
      ..addAll([
        for (final e in selectedElements())
          (e, e is ImageEl ? {e.att: doc.attachments[e.att]!.toList()} : <String, List<int>>{})
      ]);
  }

  void paste() {
    if (elementClipboard.isEmpty) return;
    final page = doc.pages[currentPage];
    final els = <Element>[];
    for (final (e, atts) in elementClipboard) {
      atts.forEach((k, v) => doc.attachments.putIfAbsent(k, () => Uint8List.fromList(v)));
      els.add(e.transformed((x, y) => (x, y), 1));
    }
    final b = boxUnion(els.map((e) => e.bbox()))!;
    final (_, cx, cy) = toPage(_view.center(Offset.zero), currentPage);
    final dx = cx - (b.$1 + b.$3) / 2, dy = cy - (b.$2 + b.$4) / 2;
    final moved = [for (final e in els) e.transformed((x, y) => (x + dx, y + dy), 1)];
    addElements(page, moved);
    select(page, {for (final e in moved) e.id});
  }

  Future<void> insertImage(String att, int iw, int ih, ImageEl Function(Page, double, double) fit) async {
    final page = doc.pages[currentPage];
    var (_, cx, cy) = toPage(_view.center(Offset.zero), currentPage);
    cx = cx.clamp(0, page.width);
    cy = cy.clamp(0, page.height);
    final im = fit(page, cx, cy);
    await res.loadImage(att);
    addElements(page, [im]);
    select(page, {im.id});
  }

  // ------------------------------------------------------------------ text

  void _startText(Offset s) {
    final (index, px, py) = toPage(s);
    final page = doc.pages[index];
    if (px < 0 || py < 0 || px > page.width || py > page.height) return;
    TextEl? el;
    for (final e in page.elements.reversed) {
      if (e is TextEl) {
        final b = e.bbox();
        if (px >= b.$1 && px <= b.$3 && py >= b.$2 && py <= b.$4) {
          el = e;
          break;
        }
      }
    }
    final t = widget.tools;
    if (el == null) {
      final lines = t.textOnLines ? paperLines(page) : null;
      var x = px, y = py - t.textSize * 0.7, size = t.textSize, lineH = 0.0;
      if (lines != null) {
        final (step, y0, margin) = lines;
        final row = math.max(0, ((py - y0) / step).ceil());
        y = y0 + (row - 1) * step;
        lineH = step;
        size = step * 0.62;
        if ((page.background.kind == 'lined' || page.background.kind == 'narrow') && x < margin + step * 0.4) {
          x = margin + step * 0.4;
        }
      }
      var w = math.min(page.width * 0.75, page.width - x - 30);
      if (w < 120) {
        x = math.max(10, page.width - 330);
        w = 300;
      }
      el = TextEl('', x, y, w, size: size, color: t.textColor, lineH: lineH);
      _editingOriginal = null;
    } else {
      _editingOriginal = el;
    }
    _editing = el;
    _editingPage = page;
    _textCtl.text = el.text;
    setState(() {});
    WidgetsBinding.instance.addPostFrameCallback((_) => _textFocus.requestFocus());
  }

  void commitText() {
    final el = _editing, page = _editingPage;
    if (el == null || page == null) return;
    _editing = null;
    _editingPage = null;
    final text = _textCtl.text.trimRight();
    final orig = _editingOriginal;
    _editingOriginal = null;
    if (orig == null) {
      if (text.isNotEmpty) addElements(page, [el.copyWith(text: text)]);
    } else if (text.isEmpty) {
      replaceElements(page, {orig.id: null});
    } else if (text != orig.text) {
      replaceElements(page, {orig.id: orig.copyWith(text: text)});
    }
    setState(() {});
  }

  // ------------------------------------------------------------------ build

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, constraints) {
      final size = constraints.biggest;
      if (size != _view) {
        _view = size;
        _applyFit();
        _clampScroll();
      }
      final children = <Widget>[
        Positioned.fill(
          child: Listener(
            onPointerDown: _onDown,
            onPointerMove: _onMove,
            onPointerUp: _onUp,
            onPointerCancel: _onCancel,
            onPointerSignal: _onSignal,
            onPointerHover: (e) {
              if (widget.tools.tool == 'eraser') setState(() => _hover = e.localPosition);
            },
            onPointerPanZoomStart: _onPanZoomStart,
            onPointerPanZoomUpdate: _onPanZoomUpdate,
            child: MouseRegion(
              onExit: (_) => setState(() => _hover = null),
              cursor: widget.tools.tool == 'text' ? SystemMouseCursors.text : SystemMouseCursors.precise,
              child: CustomPaint(painter: _Painter(this), size: size),
            ),
          ),
        ),
      ];
      if (_editing != null && _editingPage != null && pageIndex(_editingPage!) >= 0) {
        children.add(_textEditor());
      }
      return ClipRect(child: Stack(children: children));
    });
  }

  Widget _textEditor() {
    final el = _editing!;
    final i = pageIndex(_editingPage!);
    final pos = toScreen(i, el.x, el.y);
    final fontSize = el.size * zoom;
    final lineH = el.lineH * zoom;
    return Positioned(
      left: pos.dx,
      top: pos.dy + (el.lineH > 0 ? lineH * (1 - textBaseline) - fontSize * 1.07 : 0),
      width: math.max(40, el.w * zoom),
      child: TextField(
        controller: _textCtl,
        focusNode: _textFocus,
        maxLines: null,
        autofocus: true,
        style: TextStyle(fontSize: fontSize, color: rgba(el.color), fontFamily: 'NotoSans', height: 1.17),
        strutStyle: el.lineH > 0
            ? StrutStyle(fontFamily: 'NotoSans', fontSize: fontSize, height: lineH / fontSize, forceStrutHeight: true)
            : null,
        decoration: const InputDecoration(isCollapsed: true, border: InputBorder.none),
        onTapOutside: (_) => commitText(),
      ),
    );
  }
}

class _Painter extends CustomPainter {
  final NotebookViewState s;
  _Painter(this.s);

  @override
  void paint(Canvas canvas, Size size) {
    final dark = Theme.of(s.context).brightness == Brightness.dark;
    canvas.drawRect(Offset.zero & size, Paint()..color = dark ? const Color(0xff2b2b30) : const Color(0xffe3e3e8));
    final visible = s.visibleRange();
    s._evict(visible);
    final moving = s._mode == _Mode.move || s._mode == _Mode.scale;
    for (final i in visible) {
      final page = s.doc.pages[i];
      final r = s.pageRect(i);
      canvas.drawRect(r.shift(const Offset(1, 2)), Paint()..color = const Color(0x2e000000));
      final skip = <String>{
        if (moving && page == s.selectionPage) ...s.selectionIds,
        if (s._editingOriginal != null && s._editingPage == page) s._editingOriginal!.id,
      };
      final c = s._cacheFor(page, skip);
      canvas.save();
      canvas.clipRect(r);
      if (c == null) {
        canvas.drawRect(r, Paint()..color = rgba(page.background.color, 1));
      } else {
        canvas.drawImageRect(c.image, Rect.fromLTWH(0, 0, c.image.width.toDouble(), c.image.height.toDouble()), r,
            Paint()..filterQuality = FilterQuality.medium);
      }
      canvas.translate(r.left, r.top);
      canvas.scale(s.zoom);
      for (final e in s._committed[page.id] ?? const <Element>[]) {
        paintElement(canvas, e, s.res);
      }
      if (s._page == page && (s._mode == _Mode.ink || s._mode == _Mode.shape) && s._points.isNotEmpty) {
        paintStroke(canvas, s._liveStroke());
      }
      if (moving && page == s.selectionPage) {
        final f = s._transformFn();
        for (final e in s.selectedElements()) {
          paintElement(canvas, e.transformed(f, s._selScale), s.res);
        }
      }
      canvas.restore();
      if (s._page == page && s._mode == _Mode.lasso && s._lasso.length > 1) {
        final path = Path();
        for (var k = 0; k < s._lasso.length; k++) {
          final o = s.toScreen(i, s._lasso[k].$1, s._lasso[k].$2);
          k == 0 ? path.moveTo(o.dx, o.dy) : path.lineTo(o.dx, o.dy);
        }
        path.close();
        canvas.drawPath(path, Paint()..color = const Color(0x0f3373e6));
        canvas.drawPath(
            path,
            Paint()
              ..style = PaintingStyle.stroke
              ..strokeWidth = 1.5
              ..color = const Color(0xff3373e6));
      }
    }
    final sel = s.selectionScreenRect();
    if (sel != null) {
      final r = sel.inflate(6);
      canvas.drawRect(r, Paint()..color = const Color(0x143373e6));
      canvas.drawRect(
          r,
          Paint()
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.2
            ..color = const Color(0xff3373e6));
      for (final c in [r.topLeft, r.topRight, r.bottomLeft, r.bottomRight]) {
        canvas.drawCircle(c, 7, Paint()..color = Colors.white);
        canvas.drawCircle(
            c,
            7,
            Paint()
              ..style = PaintingStyle.stroke
              ..strokeWidth = 1.5
              ..color = const Color(0xff3373e6));
      }
    }
    if (s._hover != null && s.widget.tools.tool == 'eraser') {
      canvas.drawCircle(
          s._hover!,
          s.widget.tools.eraserSize * s.zoom,
          Paint()
            ..style = PaintingStyle.stroke
            ..color = const Color(0xcc4d4d59));
    }
  }

  @override
  bool shouldRepaint(_Painter old) => true;
}
