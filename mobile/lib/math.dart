// LaTeX formulas for the Markdown import: flutter_math_fork (KaTeX syntax, including
// matrices, cases and aligned) rendered offscreen into PNGs with their baseline.
import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter_math_fork/flutter_math.dart';

/// Formulas are rendered at this font size (logical px) and scaled when placed.
const double mathFontSize = 20;
const double _pixelRatio = 3;

class MathImage {
  final Uint8List png;
  final double width,
      height,
      depth; // logical px at [mathFontSize]; depth = below the baseline
  MathImage(this.png, this.width, this.height, this.depth);
}

final Map<String, MathImage?> _cache = {};

/// Renders [tex]; returns null if the formula cannot be parsed.
Future<MathImage?> renderMath(String tex, {bool display = false}) async {
  final key = '${display ? 'D' : 'T'}$tex';
  if (_cache.containsKey(key)) return _cache[key];
  MathImage? result;
  try {
    result = await _render(tex, display);
  } on Object catch (e) {
    debugPrint('WellNotes: Formel nicht darstellbar ($tex): $e');
    result = null;
  }
  return _cache[key] = result;
}

Future<MathImage?> _render(String tex, bool display) async {
  var failed = false;
  double? baseline;
  final boundary = RenderRepaintBoundary();
  final view =
      ui.PlatformDispatcher.instance.implicitView ??
      ui.PlatformDispatcher.instance.views.first;
  final renderView = RenderView(
    view: view,
    child: RenderPositionedBox(alignment: Alignment.topLeft, child: boundary),
    configuration: const ViewConfiguration(
      logicalConstraints: BoxConstraints(maxWidth: 4000, maxHeight: 4000),
      devicePixelRatio: 1,
    ),
  );
  final pipeline = PipelineOwner()..rootNode = renderView;
  renderView.prepareInitialFrame();
  final buildOwner = BuildOwner(focusManager: FocusManager());
  final widget = Directionality(
    textDirection: TextDirection.ltr,
    child: MediaQuery(
      data: const MediaQueryData(),
      child: UnconstrainedBox(
        alignment: Alignment.topLeft,
        child: _BaselineProbe(
          onLayout: (b) => baseline = b,
          child: Math.tex(
            tex,
            mathStyle: display ? MathStyle.display : MathStyle.text,
            textStyle: const TextStyle(
              fontSize: mathFontSize,
              color: Color(0xff1a1a1f),
            ),
            onErrorFallback: (e) {
              failed = true;
              return const SizedBox.shrink();
            },
          ),
        ),
      ),
    ),
  );
  final root = RenderObjectToWidgetAdapter<RenderBox>(
    container: boundary,
    child: widget,
  ).attachToRenderTree(buildOwner);
  buildOwner
    ..buildScope(root)
    ..finalizeTree();
  pipeline
    ..flushLayout()
    ..flushCompositingBits()
    ..flushPaint();
  if (failed) return null;
  // GlobalKeys only resolve in the main build owner, so take the formula's box from the
  // render tree: boundary -> UnconstrainedBox -> probe.
  final box = switch (boundary.child) {
    RenderObjectWithChildMixin<RenderBox>(:final child) => child,
    _ => null,
  };
  if (box == null || !box.hasSize || box.size.isEmpty) {
    debugPrint('WellNotes: Formel ohne Größe ($tex)');
    return null;
  }
  final size = box.size;
  final base = baseline ?? size.height;
  final image = await boundary.toImage(pixelRatio: _pixelRatio);
  final w = (size.width * _pixelRatio).ceil(),
      h = (size.height * _pixelRatio).ceil();
  // Crop to the formula (the boundary may be larger than its child).
  final rec = ui.PictureRecorder();
  ui.Canvas(rec).drawImage(image, Offset.zero, ui.Paint());
  final cropped = await rec.endRecording().toImage(
    w.clamp(1, image.width),
    h.clamp(1, image.height),
  );
  final bytes = await cropped.toByteData(format: ui.ImageByteFormat.png);
  image.dispose();
  cropped.dispose();
  buildOwner.finalizeTree();
  if (bytes == null) return null;
  return MathImage(
    bytes.buffer.asUint8List(),
    size.width,
    size.height,
    size.height - base,
  );
}

/// Reports its child's baseline; baselines may only be queried by the parent during layout.
class _BaselineProbe extends SingleChildRenderObjectWidget {
  final ValueChanged<double?> onLayout;
  const _BaselineProbe({required this.onLayout, required super.child});

  @override
  RenderObject createRenderObject(BuildContext context) =>
      _RenderBaselineProbe(onLayout);
}

class _RenderBaselineProbe extends RenderProxyBox {
  final ValueChanged<double?> onLayout;
  _RenderBaselineProbe(this.onLayout);

  @override
  void performLayout() {
    super.performLayout();
    onLayout(child?.getDistanceToBaseline(TextBaseline.alphabetic));
  }
}
