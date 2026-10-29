// Apple LZ4 frames and the tpl stroke format used by GoodNotes (see docs/goodnotes-format.md).
import 'dart:typed_data';

Uint8List _lz4Block(Uint8List src) {
  final out = <int>[];
  var i = 0;
  final n = src.length;
  while (i < n) {
    final tok = src[i++];
    var lit = tok >> 4;
    if (lit == 15) {
      while (true) {
        final b = src[i++];
        lit += b;
        if (b != 255) break;
      }
    }
    out.addAll(src.sublist(i, i + lit));
    i += lit;
    if (i >= n) break;
    final off = src[i] | (src[i + 1] << 8);
    i += 2;
    var ml = tok & 15;
    if (ml == 15) {
      while (true) {
        final b = src[i++];
        ml += b;
        if (b != 255) break;
      }
    }
    ml += 4;
    final start = out.length - off;
    for (var k = 0; k < ml; k++) {
      out.add(out[start + k]);
    }
  }
  return Uint8List.fromList(out);
}

bool _magic(Uint8List b, int i, String m) =>
    i + 4 <= b.length && b[i] == m.codeUnitAt(0) && b[i + 1] == m.codeUnitAt(1) &&
    b[i + 2] == m.codeUnitAt(2) && b[i + 3] == m.codeUnitAt(3);

Uint8List appleLz4Decode(Uint8List b) {
  final out = BytesBuilder(copy: false);
  final bd = ByteData.sublistView(b);
  var i = 0;
  while (i + 4 <= b.length) {
    if (_magic(b, i, r'bv4$')) break;
    if (_magic(b, i, 'bv41')) {
      final csize = bd.getUint32(i + 8, Endian.little);
      out.add(_lz4Block(Uint8List.sublistView(b, i + 12, i + 12 + csize)));
      i += 12 + csize;
    } else if (_magic(b, i, 'bv4-')) {
      final usize = bd.getUint32(i + 4, Endian.little);
      out.add(Uint8List.sublistView(b, i + 8, i + 8 + usize));
      i += 8 + usize;
    } else {
      throw const FormatException('bad LZ4 frame');
    }
  }
  return out.toBytes();
}

/// One literal-only LZ4 block (valid and simple; GoodNotes decodes it like any other).
Uint8List appleLz4Encode(Uint8List data) {
  final n = data.length;
  final blk = BytesBuilder(copy: false);
  if (n >= 15) {
    blk.addByte(0xF0);
    var rest = n - 15;
    while (rest >= 255) {
      blk.addByte(255);
      rest -= 255;
    }
    blk.addByte(rest);
  } else {
    blk.addByte(n << 4);
  }
  blk.add(data);
  final body = blk.toBytes();
  final hdr = ByteData(12);
  'bv41'.codeUnits.asMap().forEach((k, c) => hdr.setUint8(k, c));
  hdr.setUint32(4, n, Endian.little);
  hdr.setUint32(8, body.length, Endian.little);
  return (BytesBuilder(copy: false)
        ..add(hdr.buffer.asUint8List())
        ..add(body)
        ..add(r'bv4$'.codeUnits))
      .toBytes();
}

const strokeFormat = 'vuA(v)A(S(uu))A(S(uuuu))vA(f)';

double _bits(int u) => (ByteData(4)..setUint32(0, u, Endian.little)).getFloat32(0, Endian.little);

/// Decodes a stroke geometry blob into (width, polylines) by sampling the quadratic curves.
(double, List<List<(double, double)>>) decodeStrokePath(Uint8List blob, {int samples = 6}) {
  final b = appleLz4Decode(blob);
  if (b.length < 8 || String.fromCharCodes(b.sublist(0, 3)) != 'tpl') {
    throw const FormatException('not a tpl image');
  }
  final end = b.indexOf(0, 8);
  final fmt = String.fromCharCodes(b.sublist(8, end));
  if (fmt != strokeFormat) throw FormatException('unknown stroke format $fmt');
  final d = ByteData.sublistView(b);
  var p = end + 1;
  int u16() {
    final v = d.getUint16(p, Endian.little);
    p += 2;
    return v;
  }

  int u32() {
    final v = d.getUint32(p, Endian.little);
    p += 4;
    return v;
  }

  u16(); // version
  final width = _bits(u32());
  final types = List.generate(u32(), (_) => u16());
  final moves = List.generate(u32(), (_) => (_bits(u32()), _bits(u32())));
  final quads = List.generate(u32(), (_) => (_bits(u32()), _bits(u32()), _bits(u32()), _bits(u32())));
  final paths = <List<(double, double)>>[];
  List<(double, double)>? cur;
  var mi = 0, qi = 0;
  for (final t in types) {
    if (t == 0) {
      if (mi >= moves.length) break;
      cur = [moves[mi++]];
      paths.add(cur);
    } else {
      if (qi >= quads.length || cur == null) break;
      final (cx, cy, x, y) = quads[qi++];
      final (x0, y0) = cur.last;
      for (var k = 1; k <= samples; k++) {
        final s = k / samples;
        final a = (1 - s) * (1 - s), bb = 2 * (1 - s) * s, c = s * s;
        cur.add((a * x0 + bb * cx + c * x, a * y0 + bb * cy + c * y));
      }
    }
  }
  return (width, paths.where((p) => p.isNotEmpty).toList());
}

/// Encodes a polyline as move + quadratic segments (midpoint scheme), wrapped in an LZ4 frame.
Uint8List encodeStrokePath(double width, List<(double, double)> points) {
  var pts = points;
  if (pts.length == 1) pts = [pts[0], (pts[0].$1 + 0.01, pts[0].$2)];
  final types = <int>[];
  final moves = <(double, double)>[];
  final quads = <(double, double, double, double)>[];
  if (pts.isNotEmpty) {
    types.add(0);
    moves.add(pts[0]);
    for (var i = 1; i < pts.length - 1; i++) {
      final mx = (pts[i].$1 + pts[i + 1].$1) / 2, my = (pts[i].$2 + pts[i + 1].$2) / 2;
      quads.add((pts[i].$1, pts[i].$2, mx, my));
      types.add(1);
    }
    quads.add((pts.last.$1, pts.last.$2, pts.last.$1, pts.last.$2));
    types.add(1);
  }
  final body = BytesBuilder(copy: false);
  void u16(int v) => body.add((ByteData(2)..setUint16(0, v, Endian.little)).buffer.asUint8List());
  void u32(int v) => body.add((ByteData(4)..setUint32(0, v, Endian.little)).buffer.asUint8List());
  void fl(double v) => body.add((ByteData(4)..setFloat32(0, v, Endian.little)).buffer.asUint8List());
  u16(2);
  fl(pts.isEmpty ? 0 : width);
  u32(types.length);
  types.forEach(u16);
  u32(moves.length);
  for (final (x, y) in moves) {
    fl(x);
    fl(y);
  }
  u32(quads.length);
  for (final (a, b, c, e) in quads) {
    fl(a);
    fl(b);
    fl(c);
    fl(e);
  }
  u16(1);
  u32(0);
  final fmt = [...strokeFormat.codeUnits, 0];
  final bodyBytes = body.toBytes();
  final size = 8 + fmt.length + bodyBytes.length;
  final head = ByteData(8)
    ..setUint8(0, 't'.codeUnitAt(0))
    ..setUint8(1, 'p'.codeUnitAt(0))
    ..setUint8(2, 'l'.codeUnitAt(0))
    ..setUint8(3, 0)
    ..setUint32(4, size, Endian.little);
  final tpl = (BytesBuilder(copy: false)
        ..add(head.buffer.asUint8List())
        ..add(fmt)
        ..add(bodyBytes))
      .toBytes();
  return appleLz4Encode(tpl);
}
