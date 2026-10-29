// Reader/writer for the GoodNotes 6 container (port of wellnotes/goodnotes.py;
// format described in docs/goodnotes-format.md).
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:archive/archive.dart';

import '../model.dart';
import 'protobuf.dart';
import 'stroke_codec.dart';

const schemaVersion = 24;

class GnElement {
  final String kind; // stroke | image
  final double width;
  final List<double> color;
  final List<List<(double, double)>> paths;
  final String att;
  final double x, y, w, h;
  GnElement.stroke(this.width, this.color, this.paths)
      : kind = 'stroke', att = '', x = 0, y = 0, w = 0, h = 0;
  GnElement.image(this.att, this.x, this.y, this.w, this.h)
      : kind = 'image', width = 0, color = const [], paths = const [];
}

class GnPage {
  final String id;
  double width = 1091.3467, height = 1543.465;
  String? templateAtt;
  int templatePage = 1;
  List<double>? paper;
  final List<GnElement> elements = [];
  GnPage(this.id);
}

class GnDocument {
  String title = '';
  final List<GnPage> pages = [];
  final Map<String, Uint8List> attachments = {};
  Uint8List? thumbnail;
}

Map<String, Uint8List> _files(Archive a) => {
      for (final f in a.files)
        if (f.isFile) f.name: f.content,
    };

GnElement? _readStroke(PbMessage hdr, PbMessage el) {
  if (hdr.containsKey(3) || el.containsKey(14) || pbBytes(el, 2) == null) return null;
  try {
    final (width, paths) = decodeStrokePath(pbBytes(el, 2)!);
    if (paths.isEmpty) return null;
    final color = pbBytes(el, 4) == null ? [0.0, 0.0, 0.0, 1.0] : pbColor(pbBytes(el, 4)!);
    var (tx, ty) = (0.0, 0.0);
    final off = pbBytes(el, 6);
    if (off != null && off.isNotEmpty) (tx, ty) = pbPoint(off);
    return GnElement.stroke(width, color, [
      for (final p in paths) [for (final (x, y) in p) (x + tx, y + ty)]
    ]);
  } on Object {
    return null;
  }
}

GnElement? _readImage(PbMessage hdr, PbMessage el) {
  if (hdr.containsKey(3) || el.containsKey(14) || pbBytes(el, 2) == null) return null;
  final fr = pbParse(pbBytes(el, 2)!);
  final (x, y) = pbBytes(fr, 1) == null ? (0.0, 0.0) : pbPoint(pbBytes(fr, 1)!);
  final (w, h) = pbBytes(fr, 2) == null ? (100.0, 100.0) : pbPoint(pbBytes(fr, 2)!);
  var att = pbString(el, 4);
  if (att.isEmpty) att = pbString(hdr, 4);
  if (att.isEmpty) return null;
  return GnElement.image(att, x, y, w, h);
}

List<GnElement> _readNotes(Uint8List data) {
  final elements = <String, GnElement?>{};
  final order = <String>[];
  final recs = pbRecords(data).toList();
  for (var k = 0; k + 1 < recs.length; k += 2) {
    PbMessage hdr, body;
    try {
      hdr = pbParse(recs[k]);
      body = pbParse(recs[k + 1]);
    } on Object {
      continue;
    }
    final id = pbString(hdr, 1);
    if (!elements.containsKey(id)) order.add(id);
    GnElement? el;
    if (pbBytes(body, 7) != null) {
      el = _readStroke(hdr, pbParse(pbBytes(body, 7)!));
    } else if (pbBytes(body, 1) != null) {
      el = _readImage(hdr, pbParse(pbBytes(body, 1)!));
    }
    elements[id] = el;
  }
  return [for (final id in order) if (elements[id] != null) elements[id]!];
}

GnDocument readGoodnotes(Uint8List zipBytes) {
  final files = _files(ZipDecoder().decodeBytes(zipBytes));
  final doc = GnDocument();
  files.forEach((name, data) {
    if (name.startsWith('attachments/') && name.length > 12) doc.attachments[name.substring(12)] = data;
  });
  doc.thumbnail = files['thumbnail.jpg'];
  final pages = <String, GnPage>{};
  final order = <String>[];
  final layers = <String, String>{};
  final notesIds = <String>[];
  final events = files['index.events.pb'];
  if (events != null) {
    for (final rec in pbRecords(events)) {
      PbMessage r;
      try {
        r = pbParse(rec);
      } on Object {
        continue;
      }
      if (pbBytes(r, 30) case final b?) {
        final d = pbParse(b);
        if (pbBytes(d, 2) case final t?) {
          final title = pbString(pbParse(t), 1);
          if (title.isNotEmpty) doc.title = title;
        }
      } else if (pbBytes(r, 2) case final b?) {
        final d = pbParse(b);
        var pid = pbString(d, 2);
        if (pid.isEmpty) pid = pbString(r, 1);
        final pg = pages.putIfAbsent(pid, () {
          order.add(pid);
          return GnPage(pid);
        });
        if (d.containsKey(4)) pg.templateAtt = pbString(d, 4);
        if (pbInt(d, 5) case final n?) pg.templatePage = math.max(1, n);
        if (d.containsKey(8)) {
          final (w, h) = pbPoint(d[8]!.last as Uint8List);
          pg
            ..width = w
            ..height = h;
        }
      } else if (pbBytes(r, 54) case final b?) {
        final d = pbParse(b);
        var lid = pbString(d, 2);
        if (lid.isEmpty) lid = pbString(r, 1);
        if (d.containsKey(3)) layers[lid] = pbString(pbParse(d[3]!.last as Uint8List), 1);
        try {
          final c = pbParse(pbBytes(pbParse(pbBytes(pbParse(pbBytes(d, 17)!), 1)!), 2)!);
          if (pbBytes(c, 2) case final paper?) pages[layers[lid]]?.paper = pbColor(paper);
        } on Object {
          // no paper colour stored
        }
      } else if (pbBytes(r, 102) case final b?) {
        var nid = pbString(pbParse(b), 1);
        if (nid.isEmpty) nid = pbString(r, 1);
        notesIds.add(nid);
      }
    }
  }
  final notesFiles = <String, String>{};
  if (files['index.notes.pb'] case final idx?) {
    for (final rec in pbRecords(idx)) {
      final d = pbParse(rec);
      final p = pbString(d, 2);
      if (files.containsKey(p)) notesFiles[pbString(d, 1)] = p;
    }
  }
  for (final name in files.keys) {
    if (name.startsWith('notes/') && name.length > 6) notesFiles.putIfAbsent(name.substring(6), () => name);
  }
  for (final nid in notesFiles.keys) {
    if (!notesIds.contains(nid)) notesIds.add(nid);
  }
  // notes id == layer id with the last hex digit incremented (observed); match on the prefix.
  String? pageFor(String nid) {
    if (layers.containsKey(nid)) return layers[nid];
    for (final lid in layers.keys) {
      if (lid.length == nid.length && lid.substring(0, lid.length - 1) == nid.substring(0, nid.length - 1)) {
        return layers[lid];
      }
    }
    return null;
  }

  final unassigned = List.of(order);
  final assignment = <String, String>{};
  for (final nid in notesIds) {
    final pid = pageFor(nid);
    if (pid != null && pages.containsKey(pid)) {
      assignment[nid] = pid;
      unassigned.remove(pid);
    }
  }
  for (final nid in notesIds) {
    if (!assignment.containsKey(nid) && unassigned.isNotEmpty) assignment[nid] = unassigned.removeAt(0);
  }
  assignment.forEach((nid, pid) {
    final p = notesFiles[nid];
    if (p != null) pages[pid]!.elements.addAll(_readNotes(files[p]!));
  });
  doc.pages.addAll([for (final pid in order) pages[pid]!]);
  if (doc.pages.isEmpty) {
    notesFiles.forEach((nid, p) => doc.pages.add(GnPage(nid)..elements.addAll(_readNotes(files[p]!))));
  }
  return doc;
}

// --------------------------------------------------------------------------
// writing

final _rand = math.Random.secure();
int _r32() => _rand.nextInt(1 << 32);
int _r62() => (_rand.nextInt(1 << 31) << 31) | _rand.nextInt(1 << 31);

String _sibling(String uid) =>
    uid.substring(0, uid.length - 1) + ((int.parse(uid[uid.length - 1], radix: 16) + 1) % 16).toRadixString(16).toUpperCase();

Msg _pt(double x, double y) => Msg().f32v(1, x).f32v(2, y);

Msg _color(List<double> rgba) {
  final m = Msg();
  for (var i = 0; i < 4; i++) {
    final c = i < rgba.length ? rgba[i] : 1.0;
    if ((i < 3 && c != 0) || i == 3) m.f32v(i + 1, c);
  }
  return m;
}

class GnWritePage {
  final double width, height;
  final String? templateAtt;
  final int templatePage;
  final List<double> paper;
  final List<Object> elements; // GnWriteStroke | ImageEl
  GnWritePage(this.width, this.height, this.templateAtt, this.templatePage, this.paper, this.elements);
}

class GnWriteStroke {
  final List<(double, double)> points;
  final double width;
  final List<double> color;
  GnWriteStroke(this.points, this.width, this.color);
}

/// Adds GoodNotes index/notes/attachment entries to [archive]; returns index.events.pb.
Uint8List writeGoodnotes(Archive archive, String title, List<GnWritePage> pages, Map<String, Uint8List> attachments,
    {Uint8List? thumbnail}) {
  final device = _r62();
  var counter = DateTime.now().millisecondsSinceEpoch % 3000000;
  int tick() => ++counter;
  Msg stamp() => Msg().varint(1, 1).varint(2, _r32());
  final nowMs = DateTime.now().millisecondsSinceEpoch;
  final hybrid = nowMs + 6.2e10;
  final docId = newId();

  Msg meta(Msg m, int fHybrid, int fUid, int fDev, int fMs, int fSchema) => m
      .f64v(fHybrid, hybrid + tick())
      .bytes(fUid, newId())
      .varint(fDev, device)
      .varint(fMs, nowMs)
      .varint(fSchema, schemaVersion);

  final events = <Msg>[];
  final d = Msg()
      .bytes(1, docId)
      .bytes(2, Msg().bytes(1, title.isEmpty ? 'Notizbuch' : title).bytes(2, stamp()))
      .bytes(3, Msg().bytes(1, newId()).bytes(2, stamp()))
      .bytes(6, Msg().bytes(1, 'P').bytes(2, stamp()))
      .bytes(9, 'auto')
      .f64v(10, hybrid)
      .bytes(11, newId())
      .varint(13, device)
      .varint(14, nowMs)
      .bytes(17, Uint8List(0))
      .bytes(18, Uint8List(0))
      .bytes(19, Msg().bytes(2, stamp()))
      .varint(20, schemaVersion);
  events.add(Msg().bytes(1, docId).bytes(30, d));

  final attIndex = <Msg>[];
  final notesIndex = <Msg>[];
  final used = <String>{};
  void addAttachment(String id) {
    if (used.contains(id) || !attachments.containsKey(id)) return;
    used.add(id);
    final a = Msg()
        .bytes(1, id)
        .bytes(2, id)
        .varint(5, attachments[id]!.length)
        .bytes(6, docId)
        .f64v(10, hybrid + tick())
        .bytes(11, newId())
        .bytes(12, Uint8List(0))
        .varint(14, device)
        .varint(15, nowMs)
        .varint(16, schemaVersion);
    events.add(Msg().bytes(1, id).bytes(6, a));
    attIndex.add(Msg().bytes(1, id).bytes(2, 'attachments/$id'));
  }

  for (final pg in pages) {
    final pageId = newId(), layerId = newId();
    final notesId = _sibling(layerId);
    if (pg.templateAtt != null) addAttachment(pg.templateAtt!);
    final p = Msg().bytes(1, docId).bytes(2, pageId);
    if (pg.templateAtt != null) p.bytes(4, pg.templateAtt!);
    p
        .varint(5, pg.templatePage)
        .varint(6, 1)
        .f64v(7, 29.33333396911621)
        .bytes(8, _pt(pg.width, pg.height))
        .bytes(9, 'wellnotes')
        .f64v(10, hybrid + tick())
        .bytes(11, newId())
        .bytes(12, Msg().f64v(1, 29.33333396911621).bytes(2, stamp()))
        .bytes(13, Msg().varint(1, 1).bytes(2, stamp()))
        .varint(15, device)
        .varint(16, nowMs)
        .bytes(17, Msg().varint(1, 1).bytes(2, stamp()))
        .bytes(19, Msg().bytes(2, stamp()))
        .varint(21, schemaVersion);
    events.add(Msg().bytes(1, pageId).bytes(2, p));

    final layer = Msg()
        .bytes(1, docId)
        .bytes(2, layerId)
        .bytes(3, Msg().bytes(1, pageId).bytes(2, stamp()))
        .bytes(4, Msg().bytes(1, 'yJjpv').bytes(2, stamp()));
    meta(layer, 10, 11, 13, 14, 15);
    final colors = Msg().bytes(1, _color([0.8667, 0.8667, 0.8667, 1])).bytes(2, _color([...pg.paper.take(3), 1]));
    layer.bytes(17, Msg().bytes(1, Msg().bytes(2, colors)).bytes(2, stamp()));
    events.add(Msg().bytes(1, layerId).bytes(54, layer));

    final n = Msg().bytes(1, notesId);
    meta(n, 10, 11, 13, 14, 15);
    n.bytes(16, docId);
    events.add(Msg().bytes(1, notesId).bytes(102, n));
    final h = _r62();
    final e105 = Msg().varint(1, 1).bytes(2, docId).bytes(4, notesId).varint(5, h).bytes(6, 'auto');
    meta(e105, 10, 11, 13, 14, 15);
    events.add(Msg().bytes(1, notesId).bytes(105, e105));
    final e104 = Msg()
        .bytes(1, docId)
        .bytes(2, notesId)
        .f64v(10, hybrid + tick())
        .bytes(11, newId())
        .varint(12, h)
        .bytes(13, 'auto')
        .varint(14, 1)
        .bytes(15, newId())
        .varint(17, device)
        .varint(18, nowMs)
        .varint(19, schemaVersion);
    events.add(Msg().bytes(1, notesId).bytes(104, e104));

    final recs = <Msg>[];
    for (final el in pg.elements) {
      final eid = newId();
      final st = Msg().varint(1, 2).varint(2, _r32());
      final hdr = Msg().bytes(1, eid).bytes(2, st);
      if (el is ImageEl) {
        addAttachment(el.att);
        hdr.bytes(4, el.att);
      }
      hdr.varint(8, device).varint(9, tick()).varint(14, 5381).varint(16, schemaVersion);
      Msg body;
      if (el is GnWriteStroke) {
        final s = Msg()
            .bytes(1, eid)
            .bytes(2, encodeStrokePath(el.width, el.points))
            .bytes(4, _color(el.color))
            .bytes(6, Uint8List(0))
            .bytes(7, Msg().bytes(1, Msg().varint(1, 1).varint(2, _r32())))
            .bytes(9, Uint8List(0))
            .bytes(15, st)
            .bytes(20, Uint8List(0))
            .varint(21, schemaVersion);
        body = Msg().bytes(7, s);
      } else if (el is ImageEl) {
        final frame = Msg().bytes(1, _pt(el.x, el.y)).bytes(2, _pt(el.w, el.h));
        final center = Msg().bytes(1, _pt(el.x + el.w / 2, el.y + el.h / 2)).bytes(2, _pt(el.w, el.h));
        final im = Msg()
            .bytes(1, eid)
            .bytes(2, frame)
            .bytes(3, center)
            .bytes(4, el.att)
            .bytes(5, Msg().bytes(1, Msg().varint(1, 1).varint(2, _r32())))
            .varint(6, 1)
            .bytes(15, st)
            .varint(18, schemaVersion);
        body = Msg().bytes(1, im);
      } else {
        continue;
      }
      recs
        ..add(hdr)
        ..add(body);
    }
    archive.addFile(ArchiveFile.bytes('notes/$notesId', pbDelimited(recs)));
    notesIndex.add(Msg().bytes(1, notesId).bytes(2, 'notes/$notesId'));
  }

  final eventsBytes = pbDelimited(events);
  archive
    ..addFile(ArchiveFile.bytes('schema.pb', Msg().varint(1, schemaVersion).toBytes()))
    ..addFile(ArchiveFile.bytes('document.info.pb', Uint8List(0)))
    ..addFile(ArchiveFile.bytes('index.events.pb', eventsBytes))
    ..addFile(ArchiveFile.bytes('index.notes.pb', pbDelimited(notesIndex)))
    ..addFile(ArchiveFile.bytes('index.attachments.pb', pbDelimited(attIndex)))
    ..addFile(ArchiveFile.bytes('index.search.pb', Uint8List(0)));
  for (final id in used) {
    archive.addFile(ArchiveFile.bytes('attachments/$id', attachments[id]!));
  }
  if (thumbnail != null) archive.addFile(ArchiveFile.bytes('thumbnail.jpg', thumbnail));
  return eventsBytes;
}
