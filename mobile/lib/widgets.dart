// Small shared widgets: colour dots, size control with presets + slider, helpers.
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:flutter/material.dart' hide Element, Page;
import 'package:image/image.dart' as img;

import 'model.dart';
import 'render.dart';
import 'tools.dart';

class ColorDot extends StatelessWidget {
  final List<double> color;
  final bool selected;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;
  final double size;
  const ColorDot(
      {super.key, required this.color, required this.selected, required this.onTap, this.onLongPress, this.size = 24});

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return InkWell(
      onTap: onTap,
      onLongPress: onLongPress,
      customBorder: const CircleBorder(),
      child: Container(
        width: size + 10,
        height: size + 10,
        alignment: Alignment.center,
        decoration: selected
            ? BoxDecoration(shape: BoxShape.circle, border: Border.all(color: scheme.primary, width: 2))
            : null,
        child: Container(
          width: size,
          height: size,
          decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: rgba(color, 1),
              border: Border.all(color: Colors.black26, width: 0.8)),
        ),
      ),
    );
  }
}

/// Three presets plus a slider; moving the slider edits the active preset (persisted).
class SizeControl extends StatefulWidget {
  final ToolSettings tools;
  final String tool;
  const SizeControl({super.key, required this.tools, required this.tool});

  @override
  State<SizeControl> createState() => _SizeControlState();
}

class _SizeControlState extends State<SizeControl> {
  int active = 1;

  List<double> get presets => widget.tools.presets[widget.tool]!;
  (double, double) get range => (sizeDefaults[widget.tool]!.$2, sizeDefaults[widget.tool]!.$3);

  @override
  void initState() {
    super.initState();
    final cur = widget.tools.size(widget.tool);
    active = List.generate(3, (k) => k).reduce((a, b) => (presets[a] - cur).abs() <= (presets[b] - cur).abs() ? a : b);
    if ((presets[active] - cur).abs() > 1e-6) widget.tools.setSize(widget.tool, presets[active]);
  }

  double _radius(double v) {
    final (lo, hi) = range;
    return 2.5 + 8 * (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo));
  }

  String _label(double v) => v < 10 ? v.toStringAsFixed(1).replaceAll(RegExp(r'\.0$'), '') : '${v.round()}';

  void _openSlider() {
    final (lo, hi) = range;
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => StatefulBuilder(builder: (ctx, setSheet) {
        final v = presets[active];
        return Padding(
          padding: const EdgeInsets.fromLTRB(24, 0, 24, 32),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text(widget.tool == 'text' ? 'Textgröße' : 'Stärke', style: Theme.of(ctx).textTheme.titleMedium),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(
                child: Slider(
                  value: math.log(v),
                  min: math.log(lo),
                  max: math.log(hi),
                  onChanged: (x) {
                    final nv = (math.exp(x) * 10).roundToDouble() / 10;
                    setSheet(() {});
                    setState(() => presets[active] = nv);
                    widget.tools.setSize(widget.tool, nv);
                  },
                  onChangeEnd: (_) => widget.tools.save(),
                ),
              ),
              SizedBox(width: 48, child: Text(_label(v), textAlign: TextAlign.end)),
            ]),
            Text('Der Wert wird für die gewählte Vorgabe gespeichert.', style: Theme.of(ctx).textTheme.bodySmall),
          ]),
        );
      }),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Row(mainAxisSize: MainAxisSize.min, children: [
      for (var k = 0; k < 3; k++)
        InkWell(
          onTap: () {
            setState(() => active = k);
            widget.tools.setSize(widget.tool, presets[k]);
          },
          customBorder: const CircleBorder(),
          child: Container(
            width: 34,
            height: 34,
            alignment: Alignment.center,
            decoration: k == active ? BoxDecoration(shape: BoxShape.circle, color: scheme.secondaryContainer) : null,
            child: CircleAvatar(radius: math.min(10, _radius(presets[k])), backgroundColor: scheme.onSurface),
          ),
        ),
      TextButton(onPressed: _openSlider, child: Text(_label(presets[active]))),
    ]);
  }
}

/// A larger palette for custom colours.
Future<List<double>?> pickColor(BuildContext context, List<double> current) {
  const hues = [0.0, 25, 45, 60, 90, 140, 175, 200, 220, 260, 290, 330];
  final swatches = <List<double>>[
    for (final l in [0.0, 0.25, 0.45, 0.65, 0.85]) [l, l, l, 1.0],
    for (final light in [0.35, 0.5, 0.7])
      for (final h in hues) _hsl(h.toDouble(), 0.75, light),
  ];
  return showDialog<List<double>>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('Farbe wählen'),
      content: SizedBox(
        width: 360,
        child: Wrap(spacing: 4, runSpacing: 4, children: [
          for (final c in swatches)
            ColorDot(color: c, selected: false, size: 22, onTap: () => Navigator.pop(ctx, c)),
        ]),
      ),
      actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen'))],
    ),
  );
}

List<double> _hsl(double h, double s, double l) {
  final c = (1 - (2 * l - 1).abs()) * s;
  final x = c * (1 - ((h / 60) % 2 - 1).abs());
  final m = l - c / 2;
  final (r, g, b) = switch (h ~/ 60) {
    0 => (c, x, 0.0),
    1 => (x, c, 0.0),
    2 => (0.0, c, x),
    3 => (0.0, x, c),
    4 => (x, 0.0, c),
    _ => (c, 0.0, x),
  };
  return [r + m, g + m, b + m, 1.0];
}

/// JPEG thumbnail of the first page (like GoodNotes' thumbnail.jpg).
Future<Uint8List?> pageThumbnail(Page page, Resources res, {int width = 480}) async {
  try {
    final image = await renderPageImage(page, res, width / page.width);
    final data = await image.toByteData();
    final w = image.width, h = image.height;
    image.dispose();
    if (data == null) return null;
    final im = img.Image.fromBytes(width: w, height: h, bytes: data.buffer, numChannels: 4, order: img.ChannelOrder.rgba);
    return Uint8List.fromList(img.encodeJpg(im, quality: 85));
  } on Object {
    return null;
  }
}
