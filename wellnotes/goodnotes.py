"""Reader/writer for the GoodNotes 6 ``.goodnotes`` container.

A ``.goodnotes`` file is a ZIP archive.  The parts we understand (reverse
engineered from real files, schema version 24):

``schema.pb``                 ``{1: 24}``
``index.events.pb``           length-delimited protobuf records describing the
                              document, pages, layers, attachments and notes
``index.notes.pb``            ``{1: notes id, 2: "notes/<id>"}`` records
``index.attachments.pb``      ``{1: att id, 2: "attachments/<id>"}`` records
``index.search.pb``           handwriting search index (ignored)
``notes/<id>``                pairs of (header, element) records per element
``attachments/<id>``          raw PDFs (page templates / imported PDFs) and images
``thumbnail.jpg``             cover thumbnail

Inside a notes file an element record has exactly one top-level field:
``7`` = ink stroke, ``1`` = image, ``9`` = selection/link group (ignored).
A stroke's geometry (field 2) is an Apple LZ4 frame (``bv41`` … ``bv4$``)
wrapping a ``tpl`` image with the format ``vuA(v)A(S(uu))A(S(uuuu))vA(f)``:
version, width (float bits), segment types (0 = move, 1 = quadratic curve),
move points, quad points (control x/y, end x/y), flag, extra floats.
"""

import random
import struct
import time
import uuid
import zipfile

SCHEMA_VERSION = 24
# GoodNotes page units per PDF point (A4 = 1091.35 x 1543.46).
GN_SCALE = 11 / 6


# --------------------------------------------------------------------------
# protobuf wire format


def _varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return r, i


def pb_parse(b):
    """Parse a message into ``{field: [values]}`` (bytes for length-delimited)."""
    out = {}
    i = 0
    n = len(b)
    while i < n:
        key, i = _varint(b, i)
        f, t = key >> 3, key & 7
        if t == 0:
            v, i = _varint(b, i)
        elif t == 1:
            v = b[i:i + 8]
            i += 8
        elif t == 5:
            v = b[i:i + 4]
            i += 4
        elif t == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        else:
            raise ValueError("unsupported wire type %d" % t)
        out.setdefault(f, []).append(v)
    return out


def pb_records(b):
    i = 0
    while i < len(b):
        ln, i = _varint(b, i)
        yield b[i:i + ln]
        i += ln


def _enc_varint(v):
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


class Msg:
    """Tiny protobuf message builder preserving field order."""

    def __init__(self):
        self.buf = bytearray()

    def varint(self, f, v):
        self.buf += _enc_varint(f << 3) + _enc_varint(v)
        return self

    def bytes(self, f, v):
        if isinstance(v, Msg):
            v = bytes(v.buf)
        elif isinstance(v, str):
            v = v.encode()
        self.buf += _enc_varint((f << 3) | 2) + _enc_varint(len(v)) + v
        return self

    def f32(self, f, v):
        self.buf += _enc_varint((f << 3) | 5) + struct.pack("<f", v)
        return self

    def f64(self, f, v):
        self.buf += _enc_varint((f << 3) | 1) + struct.pack("<d", v)
        return self

    def __bytes__(self):
        return bytes(self.buf)


def pb_delimited(msgs):
    out = bytearray()
    for m in msgs:
        m = bytes(m)
        out += _enc_varint(len(m)) + m
    return bytes(out)


def _s(d, f, default=""):
    v = d.get(f)
    if not v:
        return default
    try:
        return v[-1].decode()
    except UnicodeDecodeError:
        return default


def _f32(b):
    return struct.unpack("<f", b)[0]


def _point(b):
    d = pb_parse(b)
    x = _f32(d[1][0]) if 1 in d else 0.0
    y = _f32(d[2][0]) if 2 in d else 0.0
    return x, y


def _color(b):
    d = pb_parse(b)
    return tuple(_f32(d[i][0]) if i in d else (1.0 if i == 4 else 0.0) for i in (1, 2, 3, 4))


# --------------------------------------------------------------------------
# Apple LZ4 frames


def _lz4_block(src):
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    return bytes(dst)


def apple_lz4_decode(b):
    out = bytearray()
    i = 0
    while i + 4 <= len(b):
        magic = b[i:i + 4]
        if magic == b"bv4$":
            break
        if magic == b"bv41":
            _usize, csize = struct.unpack_from("<II", b, i + 4)
            out += _lz4_block(b[i + 12:i + 12 + csize])
            i += 12 + csize
        elif magic == b"bv4-":
            (usize,) = struct.unpack_from("<I", b, i + 4)
            out += b[i + 8:i + 8 + usize]
            i += 8 + usize
        else:
            raise ValueError("bad LZ4 frame magic %r" % magic)
    return bytes(out)


def apple_lz4_encode(data):
    """Encode as one literal-only LZ4 block (valid, uncompressed)."""
    n = len(data)
    blk = bytearray()
    if n >= 15:
        blk.append(0xF0)
        rest = n - 15
        while rest >= 255:
            blk.append(255)
            rest -= 255
        blk.append(rest)
    else:
        blk.append(n << 4)
    blk += data
    return b"bv41" + struct.pack("<II", n, len(blk)) + bytes(blk) + b"bv4$"


# --------------------------------------------------------------------------
# tpl serialisation (troydhanson/tpl) as used for stroke paths

STROKE_FMT = "vuA(v)A(S(uu))A(S(uuuu))vA(f)"


def _tpl_fmt(s, i=0):
    out = []
    while i < len(s):
        c = s[i]
        if c in "AS":
            sub, i = _tpl_fmt(s, i + 2)
            out.append((c, sub))
        elif c == ")":
            return out, i + 1
        else:
            out.append(c)
            i += 1
    return out, i


_TPL_SCALARS = {"v": "<H", "u": "<I", "i": "<i", "f": "<d", "c": "<B", "j": "<b", "I": "<q", "U": "<Q"}


def _tpl_read(items, b, pos):
    res = []
    for it in items:
        if isinstance(it, tuple):
            kind, sub = it
            if kind == "S":
                v, pos = _tpl_read(sub, b, pos)
                res.append(tuple(v))
            else:
                (n,) = struct.unpack_from("<I", b, pos)
                pos += 4
                arr = []
                for _ in range(n):
                    v, pos = _tpl_read(sub, b, pos)
                    arr.append(v[0] if len(v) == 1 else v)
                res.append(arr)
        elif it == "s":
            (n,) = struct.unpack_from("<I", b, pos)
            pos += 4
            res.append(b[pos:pos + n])
            pos += n
        else:
            fmt = _TPL_SCALARS[it]
            res.append(struct.unpack_from(fmt, b, pos)[0])
            pos += struct.calcsize(fmt)
    return res, pos


def tpl_load(b):
    if b[:3] != b"tpl":
        raise ValueError("not a tpl image")
    end = b.index(b"\0", 8)
    fmt = b[8:end].decode()
    items, _ = _tpl_fmt(fmt)
    vals, _ = _tpl_read(items, b, end + 1)
    return fmt, vals


def tpl_dump_stroke(width, types, moves, quads):
    body = bytearray()
    body += struct.pack("<H", 2)
    body += struct.pack("<f", width)
    body += struct.pack("<I", len(types))
    for t in types:
        body += struct.pack("<H", t)
    body += struct.pack("<I", len(moves))
    for x, y in moves:
        body += struct.pack("<ff", x, y)
    body += struct.pack("<I", len(quads))
    for q in quads:
        body += struct.pack("<ffff", *q)
    body += struct.pack("<H", 1)
    body += struct.pack("<I", 0)
    fmt = STROKE_FMT.encode() + b"\0"
    size = 8 + len(fmt) + len(body)
    return b"tpl\0" + struct.pack("<I", size) + fmt + bytes(body)


def _bits_to_float(u):
    return struct.unpack("<f", struct.pack("<I", u))[0]


def decode_stroke_path(blob, samples=6):
    """Return (width, [[(x, y), ...] subpaths]) flattened from quadratic curves."""
    fmt, v = tpl_load(apple_lz4_decode(blob))
    if fmt != STROKE_FMT:
        raise ValueError("unknown stroke format " + fmt)
    width = _bits_to_float(v[1])
    types, moves, quads = v[2], v[3], v[4]
    paths = []
    cur = None
    mi = qi = 0
    for t in types:
        if t == 0:
            if mi >= len(moves):
                break
            x, y = moves[mi]
            mi += 1
            cur = [(_bits_to_float(x), _bits_to_float(y))]
            paths.append(cur)
        else:
            if qi >= len(quads) or cur is None:
                break
            cx, cy, x, y = (_bits_to_float(u) for u in quads[qi])
            qi += 1
            x0, y0 = cur[-1]
            for k in range(1, samples + 1):
                s = k / samples
                a = (1 - s) * (1 - s)
                bb = 2 * (1 - s) * s
                c = s * s
                cur.append((a * x0 + bb * cx + c * x, a * y0 + bb * cy + c * y))
    return width, [p for p in paths if p]


def encode_stroke_path(width, points):
    """Smooth a polyline into move + quadratic segments (midpoint scheme)."""
    pts = [(p[0], p[1]) for p in points]
    if not pts:
        return apple_lz4_encode(tpl_dump_stroke(0.0, [], [], []))
    if len(pts) == 1:
        pts = pts + [(pts[0][0] + 0.01, pts[0][1])]
    types = [0]
    moves = [pts[0]]
    quads = []
    for i in range(1, len(pts) - 1):
        mx = (pts[i][0] + pts[i + 1][0]) / 2
        my = (pts[i][1] + pts[i + 1][1]) / 2
        quads.append((pts[i][0], pts[i][1], mx, my))
        types.append(1)
    last = pts[-1]
    quads.append((last[0], last[1], last[0], last[1]))
    types.append(1)
    return apple_lz4_encode(tpl_dump_stroke(width, types, moves, quads))


# --------------------------------------------------------------------------
# reading


class GNPage:
    def __init__(self, pid):
        self.id = pid
        self.width = 1091.3467
        self.height = 1543.465
        self.template_att = None
        self.template_page = 1
        self.paper = None
        self.layer_ids = []
        self.elements = []  # dicts: {"kind": "stroke"/"image", ...}


class GNDocument:
    def __init__(self):
        self.title = ""
        self.pages = []
        self.attachments = {}  # id -> bytes
        self.thumbnail = None


def _is_live(header, body):
    return 3 not in header and 14 not in body


def _read_notes(data):
    """Return elements of a notes file in document order (last write wins)."""
    elements = {}
    order = []
    recs = list(pb_records(data))
    for k in range(0, len(recs) - 1, 2):
        try:
            hdr = pb_parse(recs[k])
            body = pb_parse(recs[k + 1])
        except (ValueError, IndexError):
            continue
        eid = _s(hdr, 1)
        if eid not in elements:
            order.append(eid)
        el = None
        if 7 in body:
            el = _read_stroke(hdr, pb_parse(body[7][0]))
        elif 1 in body:
            el = _read_image(hdr, pb_parse(body[1][0]))
        elements[eid] = el
    return [elements[e] for e in order if elements[e] is not None]


def _read_stroke(hdr, el):
    if 3 in hdr or 14 in el or 2 not in el:
        return None
    try:
        width, paths = decode_stroke_path(el[2][0])
    except (ValueError, struct.error, IndexError):
        return None
    if not paths:
        return None
    color = _color(el[4][0]) if 4 in el else (0, 0, 0, 1)
    tx = ty = 0.0
    if 6 in el and el[6][0]:
        tx, ty = _point(el[6][0])
    return {
        "kind": "stroke",
        "width": width,
        "color": color,
        "paths": [[(x + tx, y + ty) for x, y in p] for p in paths],
    }


def _read_image(hdr, el):
    if 3 in hdr or 14 in el or 2 not in el:
        return None
    fr = pb_parse(el[2][0])
    x, y = _point(fr[1][0]) if 1 in fr else (0, 0)
    w, h = _point(fr[2][0]) if 2 in fr else (100, 100)
    att = _s(el, 4) or _s(hdr, 4)
    if not att:
        return None
    return {"kind": "image", "att": att, "x": x, "y": y, "w": w, "h": h}


def read_goodnotes(path_or_file):
    doc = GNDocument()
    with zipfile.ZipFile(path_or_file) as z:
        names = set(z.namelist())
        for n in names:
            if n.startswith("attachments/") and not n.endswith("/"):
                doc.attachments[n.split("/", 1)[1]] = z.read(n)
        if "thumbnail.jpg" in names:
            doc.thumbnail = z.read("thumbnail.jpg")

        pages = {}
        page_order = []
        layers = {}  # layer id -> page id
        notes_ids = []
        if "index.events.pb" in names:
            for rec in pb_records(z.read("index.events.pb")):
                try:
                    r = pb_parse(rec)
                except ValueError:
                    continue
                if 30 in r:
                    d = pb_parse(r[30][0])
                    if 2 in d:
                        t = _s(pb_parse(d[2][0]), 1)
                        if t:
                            doc.title = t
                elif 2 in r:
                    d = pb_parse(r[2][0])
                    pid = _s(d, 2) or _s(r, 1)
                    if pid not in pages:
                        pages[pid] = GNPage(pid)
                        page_order.append(pid)
                    pg = pages[pid]
                    if 4 in d:
                        pg.template_att = _s(d, 4)
                    if 5 in d:
                        pg.template_page = max(1, d[5][-1])
                    if 8 in d:
                        pg.width, pg.height = _point(d[8][-1])
                elif 54 in r:
                    d = pb_parse(r[54][0])
                    lid = _s(d, 2) or _s(r, 1)
                    if 3 in d:
                        layers[lid] = _s(pb_parse(d[3][-1]), 1)
                    if 17 in d:
                        try:
                            c = pb_parse(pb_parse(pb_parse(d[17][0])[1][0])[2][0])
                            if 2 in c:
                                pages.setdefault(layers.get(lid), GNPage(None)).paper = _color(c[2][0])
                        except (KeyError, IndexError, ValueError):
                            pass
                elif 102 in r:
                    notes_ids.append(_s(pb_parse(r[102][0]), 1) or _s(r, 1))

        notes_files = {}
        if "index.notes.pb" in names:
            for rec in pb_records(z.read("index.notes.pb")):
                d = pb_parse(rec)
                nid, p = _s(d, 1), _s(d, 2)
                if p in names:
                    notes_files[nid] = p
        for n in names:
            if n.startswith("notes/") and not n.endswith("/"):
                notes_files.setdefault(n.split("/", 1)[1], n)
        for nid in notes_files:
            if nid not in notes_ids:
                notes_ids.append(nid)

        # Map notes -> page via layers.  Observed: notes id == layer id with
        # the last hex digit incremented, so match on the shared prefix.
        def page_for(nid):
            if nid in layers:
                return layers[nid]
            cands = [lid for lid in layers if lid[:-1] == nid[:-1]]
            return layers[cands[0]] if cands else None

        unassigned = [pid for pid in page_order]
        assignment = {}
        for nid in notes_ids:
            pid = page_for(nid)
            if pid in pages:
                assignment[nid] = pid
                if pid in unassigned:
                    unassigned.remove(pid)
        for nid in notes_ids:
            if nid not in assignment and unassigned:
                assignment[nid] = unassigned.pop(0)

        for nid, pid in assignment.items():
            p = notes_files.get(nid)
            if p:
                pages[pid].elements.extend(_read_notes(z.read(p)))

        doc.pages = [pages[pid] for pid in page_order]
        if not doc.pages and notes_files:
            for nid, p in notes_files.items():
                pg = GNPage(nid)
                pg.elements = _read_notes(z.read(p))
                doc.pages.append(pg)
    return doc


def is_goodnotes_native(path_or_file):
    try:
        with zipfile.ZipFile(path_or_file) as z:
            return "index.events.pb" in z.namelist()
    except zipfile.BadZipFile:
        return False


# --------------------------------------------------------------------------
# writing


class _Clock:
    def __init__(self, device):
        self.device = device
        self.counter = int(time.time() * 1000) % 3_000_000

    def stamp(self):
        self.counter += 1
        return Msg().varint(1, 1).varint(2, random.getrandbits(32))

    def tick(self):
        self.counter += 1
        return self.counter


def _uid():
    return str(uuid.uuid4()).upper()


def _sibling_id(uid):
    """Derive the notes id from a layer id the way GoodNotes does (+1 on last nibble)."""
    last = int(uid[-1], 16)
    return uid[:-1] + "%X" % ((last + 1) % 16)


def _pt_msg(x, y):
    return Msg().f32(1, x).f32(2, y)


def _color_msg(rgba):
    m = Msg()
    for i, c in enumerate(rgba, start=1):
        if (i < 4 and c) or i == 4:
            m.f32(i, float(c))
    return m


def write_goodnotes(zf, title, pages, attachments, thumbnail=None, device=None):
    """Write GoodNotes index/notes files into an open ``zipfile.ZipFile``.

    ``pages`` is a list of dicts with keys ``width``, ``height``,
    ``template_att`` (PDF attachment id), ``template_page`` (1-based),
    ``paper`` (rgba) and ``elements`` (stroke dicts with ``points``,
    ``width``, ``color``; image dicts with ``att``, ``x``, ``y``, ``w``, ``h``).
    ``attachments`` maps attachment id -> bytes.
    """
    device = device or random.getrandbits(62)
    clock = _Clock(device)
    now_ms = int(time.time() * 1000)
    hybrid = time.time() * 1000 + 6.2e10
    doc_id = _uid()

    def meta(m, f_hybrid, f_uid, f_dev, f_ms, f_schema):
        m.f64(f_hybrid, hybrid + clock.tick()).bytes(f_uid, _uid())
        m.varint(f_dev, device).varint(f_ms, now_ms).varint(f_schema, SCHEMA_VERSION)
        return m

    events = []
    d = Msg().bytes(1, doc_id)
    d.bytes(2, Msg().bytes(1, title or "Notizbuch").bytes(2, clock.stamp()))
    d.bytes(3, Msg().bytes(1, _uid()).bytes(2, clock.stamp()))
    d.bytes(6, Msg().bytes(1, "P").bytes(2, clock.stamp()))
    d.bytes(9, "auto")
    d.f64(10, hybrid).bytes(11, _uid()).varint(13, device).varint(14, now_ms)
    d.bytes(17, b"").bytes(18, b"").bytes(19, Msg().bytes(2, clock.stamp())).varint(20, SCHEMA_VERSION)
    events.append(Msg().bytes(1, doc_id).bytes(30, d))

    att_index = []
    notes_index = []
    notes_files = {}
    used_atts = set()

    def add_attachment(att_id):
        if att_id in used_atts or att_id not in attachments:
            return
        used_atts.add(att_id)
        a = Msg().bytes(1, att_id).bytes(2, att_id).varint(5, len(attachments[att_id]))
        a.bytes(6, doc_id).f64(10, hybrid + clock.tick()).bytes(11, _uid()).bytes(12, b"")
        a.varint(14, device).varint(15, now_ms).varint(16, SCHEMA_VERSION)
        events.append(Msg().bytes(1, att_id).bytes(6, a))
        att_index.append(Msg().bytes(1, att_id).bytes(2, "attachments/" + att_id))

    for pg in pages:
        page_id = _uid()
        layer_id = _uid()
        notes_id = _sibling_id(layer_id)
        if pg.get("template_att"):
            add_attachment(pg["template_att"])
        p = Msg().bytes(1, doc_id).bytes(2, page_id)
        if pg.get("template_att"):
            p.bytes(4, pg["template_att"])
        p.varint(5, pg.get("template_page", 1)).varint(6, 1)
        p.f64(7, 29.33333396911621).bytes(8, _pt_msg(pg["width"], pg["height"]))
        p.bytes(9, "wellnotes")
        p.f64(10, hybrid + clock.tick()).bytes(11, _uid())
        p.bytes(12, Msg().f64(1, 29.33333396911621).bytes(2, clock.stamp()))
        p.bytes(13, Msg().varint(1, 1).bytes(2, clock.stamp()))
        p.varint(15, device).varint(16, now_ms)
        p.bytes(17, Msg().varint(1, 1).bytes(2, clock.stamp()))
        p.bytes(19, Msg().bytes(2, clock.stamp())).varint(21, SCHEMA_VERSION)
        events.append(Msg().bytes(1, page_id).bytes(2, p))

        paper = pg.get("paper") or (1, 1, 1, 1)
        layer = Msg().bytes(1, doc_id).bytes(2, layer_id)
        layer.bytes(3, Msg().bytes(1, page_id).bytes(2, clock.stamp()))
        layer.bytes(4, Msg().bytes(1, "yJjpv").bytes(2, clock.stamp()))
        meta(layer, 10, 11, 13, 14, 15)
        colors = Msg().bytes(1, _color_msg((0.8667, 0.8667, 0.8667, 1))).bytes(2, _color_msg(paper))
        layer.bytes(17, Msg().bytes(1, Msg().bytes(2, colors)).bytes(2, clock.stamp()))
        events.append(Msg().bytes(1, layer_id).bytes(54, layer))

        n = Msg().bytes(1, notes_id)
        meta(n, 10, 11, 13, 14, 15)
        n.bytes(16, doc_id)
        events.append(Msg().bytes(1, notes_id).bytes(102, n))
        h = random.getrandbits(63)
        e105 = Msg().varint(1, 1).bytes(2, doc_id).bytes(4, notes_id).varint(5, h).bytes(6, "auto")
        meta(e105, 10, 11, 13, 14, 15)
        events.append(Msg().bytes(1, notes_id).bytes(105, e105))
        e104 = Msg().bytes(1, doc_id).bytes(2, notes_id).f64(10, hybrid + clock.tick()).bytes(11, _uid())
        e104.varint(12, h).bytes(13, "auto").varint(14, 1).bytes(15, _uid())
        e104.varint(17, device).varint(18, now_ms).varint(19, SCHEMA_VERSION)
        events.append(Msg().bytes(1, notes_id).bytes(104, e104))

        recs = []
        for el in pg.get("elements", []):
            eid = _uid()
            stamp = Msg().varint(1, 2).varint(2, random.getrandbits(32))
            hdr = Msg().bytes(1, eid).bytes(2, stamp)
            if el["kind"] == "image":
                add_attachment(el["att"])
                hdr.bytes(4, el["att"])
            hdr.varint(8, device).varint(9, clock.tick()).varint(14, 5381).varint(16, SCHEMA_VERSION)
            if el["kind"] == "stroke":
                s = Msg().bytes(1, eid).bytes(2, encode_stroke_path(el["width"], el["points"]))
                s.bytes(4, _color_msg(el["color"])).bytes(6, b"")
                s.bytes(7, Msg().bytes(1, Msg().varint(1, 1).varint(2, random.getrandbits(32))))
                s.bytes(9, b"").bytes(15, stamp).bytes(20, b"").varint(21, SCHEMA_VERSION)
                body = Msg().bytes(7, s)
            elif el["kind"] == "image":
                x, y, w, hh = el["x"], el["y"], el["w"], el["h"]
                frame = Msg().bytes(1, _pt_msg(x, y)).bytes(2, _pt_msg(w, hh))
                center = Msg().bytes(1, _pt_msg(x + w / 2, y + hh / 2)).bytes(2, _pt_msg(w, hh))
                im = Msg().bytes(1, eid).bytes(2, frame).bytes(3, center).bytes(4, el["att"])
                im.bytes(5, Msg().bytes(1, Msg().varint(1, 1).varint(2, random.getrandbits(32))))
                im.varint(6, 1).bytes(15, stamp).varint(18, SCHEMA_VERSION)
                body = Msg().bytes(1, im)
            else:
                continue
            recs += [hdr, body]
        notes_files[notes_id] = pb_delimited(recs)
        notes_index.append(Msg().bytes(1, notes_id).bytes(2, "notes/" + notes_id))

    zf.writestr("schema.pb", bytes(Msg().varint(1, SCHEMA_VERSION)))
    zf.writestr("document.info.pb", b"")
    zf.writestr("index.events.pb", pb_delimited(events))
    zf.writestr("index.notes.pb", pb_delimited(notes_index))
    zf.writestr("index.attachments.pb", pb_delimited(att_index))
    zf.writestr("index.search.pb", b"")
    for nid, data in notes_files.items():
        zf.writestr("notes/" + nid, data)
    for att_id in used_atts:
        zf.writestr("attachments/" + att_id, attachments[att_id])
    if thumbnail:
        zf.writestr("thumbnail.jpg", thumbnail)
    return pb_delimited(events)
