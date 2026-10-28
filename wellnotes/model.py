"""Document model: notebooks, pages and page elements."""

import copy
import math
import time
import uuid

from .goodnotes import GN_SCALE


def new_id():
    return str(uuid.uuid4()).upper()


# Page sizes in GoodNotes page units (PDF points * 11/6).
PAGE_SIZES = {
    "a4": (595.28 * GN_SCALE, 841.89 * GN_SCALE),
    "a4-landscape": (841.89 * GN_SCALE, 595.28 * GN_SCALE),
    "letter": (612 * GN_SCALE, 792 * GN_SCALE),
    "a5": (419.53 * GN_SCALE, 595.28 * GN_SCALE),
}

PAPER_COLORS = {
    "white": (1.0, 1.0, 1.0),
    "yellow": (0.996, 0.976, 0.906),
    "gray": (0.95, 0.95, 0.95),
    "dark": (0.16, 0.16, 0.18),
}

COVER_COLORS = [
    "#3d5a80", "#e07a5f", "#81b29a", "#f2cc8f", "#6d597a",
    "#264653", "#b5838d", "#2a9d8f", "#e76f51", "#457b9d",
]


class Stroke:
    kind = "stroke"

    def __init__(self, points, color=(0, 0, 0, 1), width=2.0, tool="pen", style="fountain", eid=None):
        self.id = eid or new_id()
        self.points = points  # [(x, y, pressure)]
        self.color = tuple(color)
        self.width = width
        self.tool = tool  # "pen" | "highlighter"
        self.style = style  # "fountain" | "ball"
        self._bbox = None

    def bbox(self):
        if self._bbox is None:
            xs = [p[0] for p in self.points]
            ys = [p[1] for p in self.points]
            r = self.width
            self._bbox = (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)
        return self._bbox

    def transformed(self, fn, scale=1.0):
        pts = [fn(p[0], p[1]) + (p[2],) for p in self.points]
        return Stroke(pts, self.color, self.width * scale, self.tool, self.style)

    def recolored(self, color):
        c = tuple(color[:3]) + (self.color[3],)
        return Stroke(self.points, c, self.width, self.tool, self.style)

    def to_json(self):
        return {
            "kind": "stroke", "id": self.id, "color": list(self.color), "width": self.width,
            "tool": self.tool, "style": self.style,
            "points": [[round(p[0], 2), round(p[1], 2), round(p[2], 3)] for p in self.points],
        }

    @classmethod
    def from_json(cls, d):
        return cls([tuple(p) for p in d["points"]], d["color"], d["width"], d.get("tool", "pen"),
                   d.get("style", "fountain"), d.get("id"))


class Image:
    kind = "image"

    def __init__(self, att, x, y, w, h, eid=None):
        self.id = eid or new_id()
        self.att = att
        self.x, self.y, self.w, self.h = x, y, w, h

    def bbox(self):
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    def transformed(self, fn, scale=1.0):
        x, y = fn(self.x, self.y)
        return Image(self.att, x, y, self.w * scale, self.h * scale)

    def recolored(self, color):
        return self

    def to_json(self):
        return {"kind": "image", "id": self.id, "att": self.att, "x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_json(cls, d):
        return cls(d["att"], d["x"], d["y"], d["w"], d["h"], d.get("id"))


class Text:
    """A text box.  With ``line_h`` > 0 the lines sit on the ruled lines of the paper."""

    kind = "text"

    def __init__(self, text, x, y, w, size=28.0, color=(0, 0, 0, 1), font="Sans", eid=None, line_h=0.0):
        self.id = eid or new_id()
        self.text = text
        self.x, self.y, self.w = x, y, w
        self.size = size
        self.color = tuple(color)
        self.font = font
        self.line_h = line_h
        self.h = line_h or size * 1.5  # updated by the renderer after layout

    def bbox(self):
        return (self.x, self.y, self.x + self.w, self.y + max(self.h, self.line_h or self.size * 1.5))

    def _copy(self, **kw):
        d = dict(text=self.text, x=self.x, y=self.y, w=self.w, size=self.size, color=self.color, font=self.font,
                 line_h=self.line_h)
        d.update(kw)
        t = Text(d["text"], d["x"], d["y"], d["w"], d["size"], d["color"], d["font"], line_h=d["line_h"])
        t.h = self.h * (d["size"] / self.size if self.size else 1)
        return t

    def transformed(self, fn, scale=1.0):
        x, y = fn(self.x, self.y)
        return self._copy(x=x, y=y, w=self.w * scale, size=self.size * scale, line_h=self.line_h * scale)

    def recolored(self, color):
        return self._copy(color=tuple(color[:3]) + (1,))

    def with_text(self, text):
        return self._copy(text=text)

    def to_json(self):
        d = {"kind": "text", "id": self.id, "text": self.text, "x": self.x, "y": self.y, "w": self.w,
             "size": self.size, "color": list(self.color), "font": self.font}
        if self.line_h:
            d["line_h"] = self.line_h
        return d

    @classmethod
    def from_json(cls, d):
        return cls(d["text"], d["x"], d["y"], d["w"], d.get("size", 28.0), d.get("color", (0, 0, 0, 1)),
                   d.get("font", "Sans"), d.get("id"), d.get("line_h", 0.0))


ELEMENT_TYPES = {"stroke": Stroke, "image": Image, "text": Text}


class Background:
    """kind: blank | lined | narrow | grid | dotted | pdf"""

    def __init__(self, kind="blank", color=PAPER_COLORS["white"], att=None, index=0):
        self.kind = kind
        self.color = tuple(color)
        self.att = att
        self.index = index

    def copy(self):
        return Background(self.kind, self.color, self.att, self.index)

    def to_json(self):
        return {"kind": self.kind, "color": list(self.color), "att": self.att, "index": self.index}

    @classmethod
    def from_json(cls, d):
        return cls(d.get("kind", "blank"), d.get("color", (1, 1, 1)), d.get("att"), d.get("index", 0))


class Page:
    def __init__(self, width, height, background=None, elements=None, pid=None):
        self.id = pid or new_id()
        self.width = width
        self.height = height
        self.background = background or Background()
        self.elements = elements or []
        self.version = 0  # bumped on every change; used for render caches

    def touch(self):
        self.version += 1

    def blank_copy(self):
        return Page(self.width, self.height, self.background.copy())

    def duplicate(self):
        return Page(self.width, self.height, self.background.copy(), list(self.elements))

    def to_json(self):
        return {"id": self.id, "width": self.width, "height": self.height,
                "background": self.background.to_json(),
                "elements": [e.to_json() for e in self.elements]}

    @classmethod
    def from_json(cls, d):
        els = [ELEMENT_TYPES[e["kind"]].from_json(e) for e in d.get("elements", []) if e.get("kind") in ELEMENT_TYPES]
        return cls(d["width"], d["height"], Background.from_json(d.get("background", {})), els, d.get("id"))


class Document:
    def __init__(self, title="Notizbuch"):
        self.id = new_id()
        self.title = title
        self.pages = []
        self.attachments = {}  # id -> bytes
        self.cover = COVER_COLORS[0]
        self.created = time.time()
        self.modified = time.time()
        self.path = None
        self.dirty = False
        self.undo_stack = []
        self.redo_stack = []

    # -- attachments
    def add_attachment(self, data):
        for k, v in self.attachments.items():
            if v is data or (len(v) == len(data) and v == data):
                return k
        att = new_id()
        self.attachments[att] = data
        return att

    # -- undo / redo
    def _snapshot(self, pages):
        return [(p, list(p.elements)) for p in pages]

    def begin(self, pages=None, structure=False):
        """Start an undoable change; returns a token passed to commit()."""
        return {
            "pages": self._snapshot(pages or []),
            "structure": list(self.pages) if structure else None,
        }

    def commit(self, token):
        after = {
            "pages": self._snapshot([p for p, _ in token["pages"]]),
            "structure": list(self.pages) if token["structure"] is not None else None,
        }
        self.undo_stack.append((token, after))
        if len(self.undo_stack) > 200:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self._changed(token)

    def _apply(self, state):
        if state["structure"] is not None:
            self.pages[:] = state["structure"]
        for page, els in state["pages"]:
            page.elements = list(els)

    def _changed(self, state):
        for page, _ in state["pages"]:
            page.touch()
        self.dirty = True
        self.modified = time.time()

    def undo(self):
        if not self.undo_stack:
            return None
        before, after = self.undo_stack.pop()
        self._apply(before)
        self.redo_stack.append((before, after))
        self._changed(before)
        return before

    def redo(self):
        if not self.redo_stack:
            return None
        before, after = self.redo_stack.pop()
        self._apply(after)
        self.undo_stack.append((before, after))
        self._changed(after)
        return after

    # -- serialisation
    def to_json(self):
        return {
            "format": "wellnotes", "version": 1, "id": self.id, "title": self.title, "cover": self.cover,
            "created": self.created, "modified": self.modified,
            "pages": [p.to_json() for p in self.pages],
        }

    @classmethod
    def from_json(cls, d, attachments):
        doc = cls(d.get("title", "Notizbuch"))
        doc.id = d.get("id", doc.id)
        doc.cover = d.get("cover", doc.cover)
        doc.created = d.get("created", doc.created)
        doc.modified = d.get("modified", doc.modified)
        doc.pages = [Page.from_json(p) for p in d.get("pages", [])]
        doc.attachments = attachments
        return doc


def point_in_polygon(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def dist_point_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def bbox_union(boxes):
    boxes = list(boxes)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def clone_elements(elements):
    out = []
    for e in elements:
        c = copy.copy(e)
        c.id = new_id()
        out.append(c)
    return out
