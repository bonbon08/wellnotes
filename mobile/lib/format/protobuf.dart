// Minimal protobuf wire-format codec (see docs/goodnotes-format.md).
import 'dart:convert';
import 'dart:typed_data';

/// A parsed message: field number -> values in order.
/// Varints are [int]; length-delimited, fixed32 and fixed64 values are [Uint8List].
typedef PbMessage = Map<int, List<Object>>;

(int, int) _varint(Uint8List b, int i) {
  var result = 0;
  var shift = 0;
  while (true) {
    final c = b[i++];
    result |= (c & 0x7f) << shift;
    shift += 7;
    if (c & 0x80 == 0) return (result, i);
  }
}

PbMessage pbParse(Uint8List b) {
  final out = <int, List<Object>>{};
  var i = 0;
  while (i < b.length) {
    final (key, j) = _varint(b, i);
    i = j;
    final field = key >> 3;
    final type = key & 7;
    Object v;
    switch (type) {
      case 0:
        final (val, k) = _varint(b, i);
        v = val;
        i = k;
      case 1:
        v = Uint8List.sublistView(b, i, i + 8);
        i += 8;
      case 5:
        v = Uint8List.sublistView(b, i, i + 4);
        i += 4;
      case 2:
        final (len, k) = _varint(b, i);
        v = Uint8List.sublistView(b, k, k + len);
        i = k + len;
      default:
        throw FormatException('unsupported wire type $type');
    }
    out.putIfAbsent(field, () => []).add(v);
  }
  return out;
}

Iterable<Uint8List> pbRecords(Uint8List b) sync* {
  var i = 0;
  while (i < b.length) {
    final (len, j) = _varint(b, i);
    yield Uint8List.sublistView(b, j, j + len);
    i = j + len;
  }
}

String pbString(PbMessage m, int f, [String def = '']) {
  final v = m[f];
  if (v == null || v.isEmpty || v.last is! Uint8List) return def;
  try {
    return utf8.decode(v.last as Uint8List);
  } on FormatException {
    return def;
  }
}

Uint8List? pbBytes(PbMessage m, int f) {
  final v = m[f];
  if (v == null || v.isEmpty || v.first is! Uint8List) return null;
  return v.first as Uint8List;
}

int? pbInt(PbMessage m, int f) {
  final v = m[f];
  if (v == null || v.isEmpty || v.last is! int) return null;
  return v.last as int;
}

double f32(Uint8List b) => ByteData.sublistView(b).getFloat32(0, Endian.little);

(double, double) pbPoint(Uint8List b) {
  final m = pbParse(b);
  final x = pbBytes(m, 1);
  final y = pbBytes(m, 2);
  return (x == null ? 0.0 : f32(x), y == null ? 0.0 : f32(y));
}

List<double> pbColor(Uint8List b) {
  final m = pbParse(b);
  return [for (var i = 1; i <= 4; i++) pbBytes(m, i) == null ? (i == 4 ? 1.0 : 0.0) : f32(pbBytes(m, i)!)];
}

Uint8List _encVarint(int v) {
  final out = <int>[];
  // Treat values as unsigned 64-bit (device ids use the full range).
  var x = BigInt.from(v).toUnsigned(64);
  final mask = BigInt.from(0x7f);
  while (true) {
    final c = (x & mask).toInt();
    x = x >> 7;
    if (x == BigInt.zero) {
      out.add(c);
      return Uint8List.fromList(out);
    }
    out.add(c | 0x80);
  }
}

/// Message builder that preserves field order.
class Msg {
  final BytesBuilder _b = BytesBuilder(copy: false);

  Msg varint(int f, int v) {
    _b.add(_encVarint(f << 3));
    _b.add(_encVarint(v));
    return this;
  }

  Msg bytes(int f, Object v) {
    final Uint8List data = switch (v) {
      Msg m => m.toBytes(),
      String s => Uint8List.fromList(utf8.encode(s)),
      Uint8List u => u,
      List<int> l => Uint8List.fromList(l),
      _ => throw ArgumentError('unsupported value'),
    };
    _b.add(_encVarint((f << 3) | 2));
    _b.add(_encVarint(data.length));
    _b.add(data);
    return this;
  }

  Msg f32v(int f, double v) {
    _b.add(_encVarint((f << 3) | 5));
    final d = ByteData(4)..setFloat32(0, v, Endian.little);
    _b.add(d.buffer.asUint8List());
    return this;
  }

  Msg f64v(int f, double v) {
    _b.add(_encVarint((f << 3) | 1));
    final d = ByteData(8)..setFloat64(0, v, Endian.little);
    _b.add(d.buffer.asUint8List());
    return this;
  }

  Uint8List toBytes() => Uint8List.fromList(_b.toBytes());
}

Uint8List pbDelimited(Iterable<Object> msgs) {
  final b = BytesBuilder(copy: false);
  for (final m in msgs) {
    final data = m is Msg ? m.toBytes() : m as Uint8List;
    b.add(_encVarint(data.length));
    b.add(data);
  }
  return b.toBytes();
}
