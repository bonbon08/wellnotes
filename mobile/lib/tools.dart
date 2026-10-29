// Tool settings shared by toolbar and canvas; size presets are persisted.
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

const penColors = [
  [0.08, 0.08, 0.1, 1.0], [0.13, 0.36, 0.86, 1.0], [0.85, 0.18, 0.18, 1.0], [0.1, 0.6, 0.3, 1.0],
  [0.55, 0.25, 0.75, 1.0],
];
const hlColors = [
  [1.0, 0.86, 0.2, 1.0], [0.55, 0.9, 0.4, 1.0], [1.0, 0.55, 0.75, 1.0], [0.45, 0.8, 1.0, 1.0],
  [1.0, 0.65, 0.3, 1.0],
];

/// Default presets and slider range per tool (page units; A4 is 1091 wide).
const sizeDefaults = <String, (List<double>, double, double)>{
  'pen': ([1.4, 2.4, 4.0], 0.3, 30),
  'shape': ([1.4, 2.4, 4.0], 0.3, 30),
  'highlighter': ([12, 22, 34], 4, 90),
  'text': ([20, 28, 40], 8, 140),
  'eraser': ([6, 14, 30], 2, 100),
};

class ToolSettings extends ChangeNotifier {
  String tool = 'pen';
  List<double> penColor = List.of(penColors[0]);
  double penWidth = 2.4;
  String penStyle = 'fountain';
  List<double> hlColor = List.of(hlColors[0]);
  double hlWidth = 22;
  List<double> shapeColor = List.of(penColors[0]);
  double shapeWidth = 2.4;
  double eraserSize = 14;
  bool eraserPrecise = false;
  double textSize = 28;
  List<double> textColor = List.of(penColors[0]);
  bool textOnLines = true;
  bool fingerDraw = false;
  Map<String, List<double>> presets = {for (final e in sizeDefaults.entries) e.key: List.of(e.value.$1)};

  static Future<ToolSettings> load() async {
    final t = ToolSettings();
    try {
      final prefs = await SharedPreferences.getInstance();
      final raw = prefs.getString('presets');
      if (raw != null) {
        (jsonDecode(raw) as Map).forEach((k, v) {
          if (t.presets.containsKey(k) && v is List && v.length == 3) {
            t.presets[k as String] = [for (final x in v) (x as num).toDouble()];
          }
        });
      }
      t.fingerDraw = prefs.getBool('fingerDraw') ?? false;
      t.textOnLines = prefs.getBool('textOnLines') ?? true;
    } on Object {
      // first start or no storage: defaults
    }
    return t;
  }

  Future<void> save() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('presets', jsonEncode(presets));
    await prefs.setBool('fingerDraw', fingerDraw);
    await prefs.setBool('textOnLines', textOnLines);
  }

  double size(String t) => switch (t) {
        'pen' => penWidth,
        'shape' => shapeWidth,
        'highlighter' => hlWidth,
        'text' => textSize,
        _ => eraserSize,
      };

  void setSize(String t, double v) {
    switch (t) {
      case 'pen':
        penWidth = v;
      case 'shape':
        shapeWidth = v;
      case 'highlighter':
        hlWidth = v;
      case 'text':
        textSize = v;
      default:
        eraserSize = v;
    }
    notifyListeners();
  }

  List<double> color(String t) => switch (t) {
        'highlighter' => hlColor,
        'shape' => shapeColor,
        'text' => textColor,
        _ => penColor,
      };

  void setColor(String t, List<double> c) {
    switch (t) {
      case 'highlighter':
        hlColor = c;
      case 'shape':
        shapeColor = c;
      case 'text':
        textColor = c;
      default:
        penColor = c;
    }
    notifyListeners();
  }

  void update(VoidCallback f) {
    f();
    notifyListeners();
  }
}
