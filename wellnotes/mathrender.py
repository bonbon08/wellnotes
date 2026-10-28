"""LaTeX formulas for the Markdown typesetter.

Formulas are rendered with matplotlib's mathtext (vector SVG, drawn with
librsvg).  mathtext has no environments, so ``matrix``/``pmatrix``/``bmatrix``/
``vmatrix``/``Bmatrix``, ``cases`` and ``aligned``/``align``/``gather``/``split``
are laid out here from individually rendered cells.  If matplotlib is missing
or a formula cannot be parsed, the source is shown as italic text instead.
"""

import io
import math
import re

import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import GLib, Pango, PangoCairo  # noqa: E402

try:
    gi.require_version("Rsvg", "2.0")
    from gi.repository import Rsvg
except (ValueError, ImportError):
    Rsvg = None

COLOR = "#1a1a1f"
_mpl = None
_cache = {}


def _matplotlib():
    global _mpl
    if _mpl is None:
        try:
            import matplotlib
            matplotlib.use("svg", force=False)
            matplotlib.rcParams["mathtext.fontset"] = "cm"
            matplotlib.rcParams["svg.fonttype"] = "path"
            from matplotlib import mathtext
            from matplotlib.font_manager import FontProperties
            _mpl = (mathtext, FontProperties)
        except ImportError:
            _mpl = False
    return _mpl


def available():
    return bool(_matplotlib()) and Rsvg is not None


class Box:
    """A laid-out formula: width, ascent above and depth below the baseline (pt)."""

    w = h = d = 0.0

    @property
    def ascent(self):
        return self.h - self.d

    def draw(self, cr, x, top):
        raise NotImplementedError


class SvgBox(Box):
    def __init__(self, handle, w, h, d):
        self.handle, self.w, self.h, self.d = handle, w, h, d

    def draw(self, cr, x, top):
        rect = Rsvg.Rectangle()
        rect.x, rect.y, rect.width, rect.height = x, top, self.w, self.h
        self.handle.render_document(cr, rect)


class TextBox(Box):
    """Fallback: the TeX source in italics."""

    def __init__(self, text, size):
        import cairo
        self.text, self.size = text, size
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        self._layout(cairo.Context(surf))

    def _layout(self, cr):
        lay = PangoCairo.create_layout(cr)
        fd = Pango.FontDescription.from_string("Serif Italic")
        fd.set_size(int(self.size * Pango.SCALE))
        lay.set_font_description(fd)
        lay.set_text(self.text, -1)
        ext = lay.get_extents()[1]
        self.w = ext.width / Pango.SCALE
        self.h = ext.height / Pango.SCALE
        self.d = self.h - lay.get_baseline() / Pango.SCALE
        return lay

    def draw(self, cr, x, top):
        lay = self._layout(cr)
        cr.save()
        cr.set_source_rgb(0.1, 0.1, 0.12)
        cr.move_to(x, top)
        PangoCairo.show_layout(cr, lay)
        cr.restore()


class HBox(Box):
    def __init__(self, boxes, gap=0.0):
        self.boxes = boxes
        self.gap = gap
        asc = max((b.ascent for b in boxes), default=0)
        self.d = max((b.d for b in boxes), default=0)
        self.h = asc + self.d
        self.w = sum(b.w for b in boxes) + gap * max(0, len(boxes) - 1)

    def draw(self, cr, x, top):
        base = top + self.ascent
        for b in self.boxes:
            b.draw(cr, x, base - b.ascent)
            x += b.w + self.gap


class GridBox(Box):
    """Matrix-like environments, cases and aligned equations."""

    def __init__(self, rows, size, left="", right="", align=None, colgap=None):
        self.rows = rows
        self.size = size
        self.left, self.right = left, right
        ncol = max((len(r) for r in rows), default=0)
        self.colw = [max((r[c].w for r in rows if c < len(r)), default=0) for c in range(ncol)]
        self.align = align or ["c"] * ncol
        self.colgap = size * 0.9 if colgap is None else colgap
        self.rowgap = size * 0.35
        self.row_asc = [max((b.ascent for b in r), default=size * 0.7) for r in rows]
        self.row_dsc = [max((b.d for b in r), default=size * 0.2) for r in rows]
        inner_h = sum(self.row_asc) + sum(self.row_dsc) + self.rowgap * max(0, len(rows) - 1)
        inner_w = sum(self.colw) + self.colgap * max(0, ncol - 1)
        self.delim_w = size * 0.45
        self.pad = size * 0.15
        self.inner = (inner_w, inner_h)
        self.w = inner_w + (self.delim_w + self.pad if left else 0) + (self.delim_w + self.pad if right else 0)
        self.h = inner_h + size * 0.2
        axis = size * 0.25  # math axis above the baseline
        self.d = self.h / 2 - axis

    def draw(self, cr, x, top):
        s = self.size
        iw, ih = self.inner
        x0 = x + (self.delim_w + self.pad if self.left else 0)
        y = top + s * 0.1
        for ri, row in enumerate(self.rows):
            base = y + self.row_asc[ri]
            cx = x0
            for ci, b in enumerate(row):
                cw = self.colw[ci]
                a = self.align[ci] if ci < len(self.align) else "c"
                bx = cx + (cw - b.w if a == "r" else (cw - b.w) / 2 if a == "c" else 0)
                b.draw(cr, bx, base - b.ascent)
                cx += cw + self.colgap
            y = base + self.row_dsc[ri] + self.rowgap
        cr.save()
        cr.set_source_rgb(0.1, 0.1, 0.12)
        cr.set_line_width(max(0.6, s * 0.06))
        cr.set_line_cap(1)
        if self.left:
            _delim(cr, self.left, x, top, self.delim_w, self.h, left=True)
        if self.right:
            _delim(cr, self.right, x + self.w - self.delim_w, top, self.delim_w, self.h, left=False)
        cr.restore()


def _delim(cr, kind, x, top, w, h, left):
    kind = {")": "(", "]": "[", "}": "{"}.get(kind, kind)
    if kind == "(":
        xi, xo = (x + w, x + w * 0.15) if left else (x, x + w * 0.85)
        cr.move_to(xi, top)
        cr.curve_to(xo, top + h * 0.25, xo, top + h * 0.75, xi, top + h)
    elif kind == "[":
        xi, xo = (x + w * 0.85, x + w * 0.3) if left else (x + w * 0.15, x + w * 0.7)
        cr.move_to(xi, top)
        cr.line_to(xo, top)
        cr.line_to(xo, top + h)
        cr.line_to(xi, top + h)
    elif kind == "|":
        xm = x + w * 0.5
        cr.move_to(xm, top)
        cr.line_to(xm, top + h)
    elif kind == "||":
        for f in (0.35, 0.65):
            cr.move_to(x + w * f, top)
            cr.line_to(x + w * f, top + h)
    elif kind == "{":
        m = top + h / 2
        xi, xo, xt = (x + w * 0.9, x + w * 0.45, x + w * 0.05) if left else (x + w * 0.1, x + w * 0.55,
                                                                             x + w * 0.95)
        cr.move_to(xi, top)
        cr.curve_to(xo, top, xo, top, xo, top + h * 0.15)
        cr.line_to(xo, m - h * 0.1)
        cr.curve_to(xo, m, xo, m, xt, m)
        cr.curve_to(xo, m, xo, m, xo, m + h * 0.1)
        cr.line_to(xo, top + h * 0.85)
        cr.curve_to(xo, top + h, xo, top + h, xi, top + h)
    cr.stroke()


_DELIMS = {"matrix": ("", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "vmatrix": ("|", "|"),
           "Vmatrix": ("||", "||"), "Bmatrix": ("{", "}"), "smallmatrix": ("", ""), "cases": ("{", ""),
           "rcases": ("", "{")}
_ALIGNED = ("aligned", "align", "align*", "split", "gather", "gather*", "gathered", "equation", "equation*",
            "alignedat", "eqnarray", "eqnarray*")
_ENV = re.compile(r"\\begin\{([a-zA-Z*]+)\}")


def _clean(tex):
    tex = tex.strip()
    for a, b in ((r"\dfrac", r"\frac"), (r"\tfrac", r"\frac"), (r"\displaystyle", ""), (r"\textstyle", ""),
                 (r"\limits", ""), (r"\nolimits", ""), (r"\lvert", "|"), (r"\rvert", "|"), (r"\lVert", r"\Vert"),
                 (r"\rVert", r"\Vert"), (r"\mid", "|"), (r"\dots", r"\ldots"), (r"\coloneqq", ":="),
                 (r"\implies", r"\Rightarrow"), (r"\iff", r"\Leftrightarrow"), (r"\tag", r"\quad")):
        tex = tex.replace(a, b)
    for short, full in (("ge", "geq"), ("le", "leq"), ("ne", "neq")):
        tex = re.sub(r"\\%s(?![a-zA-Z])" % short, lambda m, f=full: "\\" + f, tex)
    tex = re.sub(r"\\operatorname\*?\{([^}]*)\}", r"\\mathrm{\1}", tex)
    tex = re.sub(r"\\(?:label|nonumber|notag)(\{[^}]*\})?", "", tex)
    return tex


def _plain(tex, size):
    tex = tex.strip()
    if not tex:
        return None
    key = (tex, size)
    if key in _cache:
        return _cache[key]
    mpl = _matplotlib()
    box = None
    if mpl and Rsvg is not None:
        mathtext, FontProperties = mpl
        buf = io.BytesIO()
        try:
            depth = mathtext.math_to_image("$%s$" % tex, buf, prop=FontProperties(size=size), dpi=72,
                                           format="svg", color=COLOR)
            handle = Rsvg.Handle.new_from_data(buf.getvalue())
            ok, w, h = handle.get_intrinsic_size_in_pixels()
            if ok:
                box = SvgBox(handle, w, h, float(depth))
        except Exception:  # mathtext raises assorted parser errors; fall back to text
            box = None
    if box is None:
        box = TextBox(tex, size * 0.95)
    _cache[key] = box
    return box


def _split_top(s, sep):
    """Split on ``sep`` outside braces and nested environments."""
    parts, depth, env, cur, i = [], 0, 0, "", 0
    while i < len(s):
        if s.startswith("\\begin{", i):
            env += 1
        elif s.startswith("\\end{", i):
            env -= 1
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if depth == 0 and env == 0 and s.startswith(sep, i) and not (sep == "&" and s[i - 1:i] == "\\"):
            parts.append(cur)
            cur = ""
            i += len(sep)
            continue
        cur += c
        i += 1
    parts.append(cur)
    return parts


def _find_end(tex, start, name):
    """Index of the matching \\end{name} for a \\begin{name} ending at ``start``."""
    depth = 1
    pat = re.compile(r"\\(begin|end)\{%s\}" % re.escape(name))
    for m in pat.finditer(tex, start):
        depth += 1 if m.group(1) == "begin" else -1
        if depth == 0:
            return m.start(), m.end()
    return None


def layout(tex, size=11.0):
    """Lay out a formula (without $ delimiters) and return a Box."""
    tex = _clean(tex)
    boxes = []
    pos = 0
    for m in _ENV.finditer(tex):
        if m.start() < pos:
            continue
        name = m.group(1)
        end = _find_end(tex, m.end(), name)
        if not end:
            break
        before = tex[pos:m.start()]
        b = _plain(before, size)
        if b:
            boxes.append(b)
        body = tex[m.end():end[0]]
        if name == "alignedat":
            body = re.sub(r"^\{\d+\}", "", body)
        boxes.append(_env(name, body, size))
        pos = end[1]
    rest = _plain(tex[pos:], size)
    if rest:
        boxes.append(rest)
    if not boxes:
        return TextBox(tex, size)
    return boxes[0] if len(boxes) == 1 else HBox(boxes, gap=size * 0.15)


def _env(name, body, size):
    rows = [r for r in _split_top(body, r"\\") if r.strip()]
    if name in _DELIMS:
        left, right = _DELIMS[name]
        cell_size = size * (0.75 if name == "smallmatrix" else 1.0)
        grid = [[layout(c, cell_size) for c in _split_top(r, "&")] for r in rows]
        if name in ("cases", "rcases"):
            return GridBox(grid, size, left, right, align=["l", "l"], colgap=size * 1.2)
        return GridBox(grid, size, left, right)
    if name in _ALIGNED:
        grid = []
        for r in rows:
            cells = _split_top(r, "&")
            if len(cells) == 1:
                grid.append([layout(cells[0], size)])
            else:
                # Pairs of right/left aligned columns, glued at the & (like amsmath).
                grid.append([layout(c, size) for c in cells])
        ncol = max(len(r) for r in grid) if grid else 1
        align = ["c"] if ncol == 1 else ["r" if i % 2 == 0 else "l" for i in range(ncol)]
        return GridBox(grid, size, align=align, colgap=size * 0.1 if ncol > 1 else 0)
    return TextBox(body, size)


def display(tex, size=12.0):
    """Display math (``$$…$$``): lines separated by \\\\ outside environments are stacked."""
    tex = tex.strip()
    if tex.startswith("\\[") and tex.endswith("\\]"):
        tex = tex[2:-2]
    lines = [l for l in _split_top(tex, r"\\") if l.strip()]
    if len(lines) > 1 and not _ENV.search(tex):
        return _env("gather", tex, size)
    return layout(tex, size)
