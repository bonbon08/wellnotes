"""Small CommonMark/GFM subset parser and a Pango typesetter that produces a PDF.

Supported: ATX/Setext headings, paragraphs, emphasis, strong, strikethrough,
inline code, links, autolinks, images, (nested) bullet/ordered/task lists,
block quotes, fenced and indented code blocks, horizontal rules and GFM tables.

Obsidian extensions: ``[[wikilinks|alias]]``, embeds ``![[image.png|300]]``,
``![[Other note]]`` (transcluded) and ``![[file.pdf]]`` (appended as pages),
callouts ``> [!note] Title``, ``==highlights==``, ``#tags``, ``%%comments%%``,
footnotes ``[^1]`` and LaTeX ``$math$`` / ``$$math$$`` (see mathrender.py).
"""

import base64
import html
import io
import os
import re
import urllib.parse
import urllib.request

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib, Pango, PangoCairo  # noqa: E402

# --------------------------------------------------------------------------
# block parsing

_HR = re.compile(r"^ {0,3}([-*_])( *\1){2,} *$")
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)")
_BULLET = re.compile(r"^( {0,3})([-+*])( +|$)(.*)")
_ORDERED = re.compile(r"^( {0,3})(\d{1,9})([.)])( +|$)(.*)")
_QUOTE = re.compile(r"^ {0,3}> ?(.*)")
_TABLE_SEP = re.compile(r"^ *\|? *:?-+:? *(\| *:?-+:? *)*\|? *$")
_TASK = re.compile(r"^\[([ xX/-])\] +(.*)", re.S)
_CALLOUT = re.compile(r"^\[!(\w+)\][+-]?\s*(.*)$")
_EMBED = re.compile(r"^!\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]$")
_WIKILINK = re.compile(r"(!?)\[\[([^\]|#]*)(#[^\]|]*)?(?:\|([^\]]*))?\]\]")


def find_vault_root(path):
    """Return the enclosing Obsidian vault directory (contains .obsidian), if any."""
    d = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    home = os.path.expanduser("~")
    while True:
        if os.path.isdir(os.path.join(d, ".obsidian")):
            return d
        parent = os.path.dirname(d)
        if parent == d or d == home:
            return None
        d = parent


class Resolver:
    """Resolves image/embed targets relative to the note and, Obsidian-style, anywhere in the vault."""

    def __init__(self, base_dir=None, vault_root=None):
        self.base_dir = base_dir
        self.vault_root = vault_root
        self._index = None

    def _build(self):
        self._index = {}
        root = self.vault_root or self.base_dir
        if not root:
            return
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in filenames:
                low = f.lower()
                self._index.setdefault(low, os.path.join(dirpath, f))
                if low.endswith(".md"):
                    self._index.setdefault(low[:-3], os.path.join(dirpath, f))
                count += 1
            if count > 50000:
                break

    def resolve(self, target):
        from urllib.parse import unquote
        target = unquote(target.split("|")[0].split("#")[0].strip())
        if not target or "://" in target:
            return None
        for base in (self.base_dir, self.vault_root):
            if base:
                p = os.path.join(base, target)
                for cand in (p, p + ".md"):
                    if os.path.isfile(cand):
                        return cand
        if self._index is None:
            self._build()
        name = os.path.basename(target).lower()
        return self._index.get(name) or self._index.get(name + ".md")


def protect_table_pipes(line):
    """``|`` inside ``$math$`` or `code` in a table row is content, not a column separator."""
    def math(m):
        return m.group(0).replace("|", r"\vert ")

    def code(m):
        return m.group(0).replace("|", "\\|")

    line = re.sub(r"`[^`]*`", code, line)
    return re.sub(r"(?<![\\$])\$[^$]+\$", math, line)


def _split_row(line):
    line = protect_table_pipes(line.strip())
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells = re.split(r"(?<!\\)\|", line)
    return [c.strip().replace("\\|", "|") for c in cells]


def parse_blocks(lines):
    blocks = []
    i = 0
    n = len(lines)
    para = []

    def flush():
        if para:
            blocks.append(("para", "\n".join(s.strip() for s in para)))
            para.clear()

    while i < n:
        line = lines[i].expandtabs(4)
        if not line.strip():
            flush()
            i += 1
            continue
        m = _FENCE.match(line)
        if m:
            flush()
            fence = m.group(2)
            ind = len(m.group(1))
            code = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence[0] * len(fence)):
                code.append(lines[i][ind:] if lines[i][:ind].strip() == "" else lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code), m.group(3)))
            continue
        if line.startswith("    ") and not para:
            code = []
            while i < n and (lines[i].startswith("    ") or not lines[i].strip()):
                code.append(lines[i][4:])
                i += 1
            while code and not code[-1].strip():
                code.pop()
            blocks.append(("code", "\n".join(code), ""))
            continue
        m = _ATX.match(line)
        if m:
            flush()
            blocks.append(("heading", len(m.group(1)), (m.group(2) or "").strip()))
            i += 1
            continue
        if para and re.match(r"^ {0,3}(=+|-+) *$", line):
            level = 1 if line.strip()[0] == "=" else 2
            text = "\n".join(s.strip() for s in para)
            para.clear()
            blocks.append(("heading", level, text))
            i += 1
            continue
        if _HR.match(line):
            flush()
            blocks.append(("hr",))
            i += 1
            continue
        m = _QUOTE.match(line)
        if m:
            flush()
            q = []
            while i < n and lines[i].strip():
                mm = _QUOTE.match(lines[i])
                q.append(mm.group(1) if mm else lines[i])
                i += 1
            cm = _CALLOUT.match(q[0].strip()) if q else None
            if cm:
                blocks.append(("callout", cm.group(1).lower(), cm.group(2).strip(), parse_blocks(q[1:])))
            else:
                blocks.append(("quote", parse_blocks(q)))
            continue
        if line.strip().startswith("$$"):
            flush()
            first = line.strip()[2:]
            math = []
            if first.rstrip().endswith("$$") and len(first.strip()) > 2:
                math.append(first.rstrip()[:-2])
                i += 1
            else:
                if first.strip():
                    math.append(first)
                i += 1
                while i < n and "$$" not in lines[i]:
                    math.append(lines[i])
                    i += 1
                if i < n:
                    rest = lines[i].split("$$")[0]
                    if rest.strip():
                        math.append(rest)
                    i += 1
            blocks.append(("math", "\n".join(math).strip()))
            continue
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]) and "-" in lines[i + 1]:
            flush()
            header = _split_row(line)
            aligns = []
            for c in _split_row(lines[i + 1]):
                c = c.strip()
                aligns.append("center" if c.startswith(":") and c.endswith(":") else
                              "right" if c.endswith(":") else "left")
            rows = []
            i += 2
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            blocks.append(("table", header, aligns, rows))
            continue
        mb = _BULLET.match(line)
        mo = _ORDERED.match(line)
        if mb or mo:
            flush()
            ordered = bool(mo)
            start = int(mo.group(2)) if mo else 1
            items = []
            while i < n:
                ln = lines[i].expandtabs(4)
                mb = _BULLET.match(ln)
                mo = _ORDERED.match(ln)
                m = mo if ordered else mb
                if not m or _HR.match(ln):
                    break
                lead = len(m.group(1))
                marker = m.group(2) + (m.group(3) if ordered else "")
                content_indent = lead + len(marker) + max(1, min(4, len(m.group(4 if ordered else 3)) or 1))
                first = m.group(5 if ordered else 4)
                item_lines = [first]
                i += 1
                while i < n:
                    nxt = lines[i].expandtabs(4)
                    if not nxt.strip():
                        if i + 1 < n and lines[i + 1].expandtabs(4).startswith(" " * content_indent):
                            item_lines.append("")
                            i += 1
                            continue
                        break
                    if nxt.startswith(" " * content_indent):
                        item_lines.append(nxt[content_indent:])
                    elif nxt.startswith("  ") and (_BULLET.match(nxt.lstrip()) or _ORDERED.match(nxt.lstrip())):
                        item_lines.append(nxt.lstrip() if len(nxt) - len(nxt.lstrip()) >= 2 else nxt)
                    elif _BULLET.match(nxt) or _ORDERED.match(nxt) or _ATX.match(nxt) or _FENCE.match(nxt) \
                            or _QUOTE.match(nxt) or _HR.match(nxt):
                        break
                    else:
                        item_lines.append(nxt.strip())  # lazy continuation
                    i += 1
                checked = None
                tm = _TASK.match(item_lines[0])
                if tm:
                    checked = tm.group(1) != " "
                    item_lines[0] = tm.group(2)
                items.append((checked, parse_blocks(item_lines)))
                while i < n and not lines[i].strip():
                    if i + 1 < n and (_BULLET.match(lines[i + 1]) if not ordered else _ORDERED.match(lines[i + 1])):
                        i += 1
                    else:
                        break
            blocks.append(("list", ordered, start, items))
            continue
        para.append(line)
        i += 1
    flush()
    # Images and embeds inside paragraphs (e.g. several images on consecutive
    # lines, or an image followed by text) become blocks of their own.
    out = []
    for b in blocks:
        if b[0] == "para" and "![" in b[1]:
            out.extend(_split_images(b[1]))
        else:
            out.append(b)
    return out


_INLINE_IMG = re.compile(r"!\[\[[^\]]+\]\]|!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


def _split_images(text):
    out = []
    pos = 0
    for m in _INLINE_IMG.finditer(text):
        before = text[pos:m.start()].strip()
        if before:
            out.append(("para", before))
        tok = m.group(0)
        if tok.startswith("![["):
            e = _EMBED.match(tok)
            if e:
                out.append(("embed", e.group(1).strip(), (e.group(2) or "")[1:], e.group(3)))
            else:
                out.append(("para", tok))
        else:
            out.append(("image", m.group(1), m.group(2).strip("<>")))
        pos = m.end()
    rest = text[pos:].strip()
    if rest:
        out.append(("para", rest))
    return out


_remote_cache = {}


def load_image_bytes(src, resolver):
    """Image data for a Markdown image source: file (resolved in the vault), data: URI or http(s) URL."""
    if src.startswith("data:"):
        header, _, payload = src.partition(",")
        try:
            return base64.b64decode(payload) if ";base64" in header else urllib.parse.unquote_to_bytes(payload)
        except ValueError:
            return None
    if src.startswith(("http://", "https://")):
        if src not in _remote_cache:
            try:
                req = urllib.request.Request(src, headers={"User-Agent": "WellNotes"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    _remote_cache[src] = r.read(30_000_000)
            except Exception:  # offline or broken link: show the placeholder
                _remote_cache[src] = None
        return _remote_cache[src]
    path = resolver.resolve(src) if resolver else src
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------
# inline -> Pango markup

LINK_COLOR = "#1a63c6"
CODE_BG = "#eef0f3"


MATH_BASE = 0xE000
MATH_MAX = 0xF8FF - MATH_BASE


def inline_markup(text, mono="Monospace", math_pool=None):
    """Markdown inline syntax -> Pango markup.

    With ``math_pool`` (a list), each ``$formula$`` is appended to the pool and
    replaced by a private-use character the typesetter turns into a formula.
    """
    codes = []

    def code_sub(m):
        codes.append(m.group(2).strip() if m.group(2).strip() else m.group(2))
        return "\x00%d\x00" % (len(codes) - 1)

    text = re.sub(r"(`+)(.+?)\1", code_sub, text, flags=re.S)
    text = re.sub(r"%%.*?%%", "", text, flags=re.S)
    maths = []

    def math_sub(m):
        maths.append(m.group(1))
        return "\x03%d\x03" % (len(maths) - 1)

    text = re.sub(r"(?<![\\$])\$(?![\s$])([^$\n]+?)(?<!\s)\$(?![\d$])", math_sub, text)
    escapes = []

    def esc_sub(m):
        escapes.append(m.group(1))
        return "\x01%d\x01" % (len(escapes) - 1)

    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|~<>])", esc_sub, text)
    links = []

    def wiki_sub(m):
        embed, target, heading, alias = m.groups()
        label = alias or (target + (" \u203a " + heading[1:] if heading else "")) or (heading or "")[1:]
        if embed:
            label = "[" + os.path.basename(label) + "]"
        links.append((label, target))
        return "\x02%d\x02" % (len(links) - 1)

    text = _WIKILINK.sub(wiki_sub, text)

    def link_sub(m):
        links.append((m.group(1), m.group(2)))
        return "\x02%d\x02" % (len(links) - 1)

    text = re.sub(r"!?\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)", link_sub, text)
    def autolink_sub(m):
        links.append((m.group(1), m.group(1)))
        return "\x02%d\x02" % (len(links) - 1)

    text = re.sub(r"<(https?://[^>\s]+)>", autolink_sub, text)
    s = html.escape(text, quote=False)
    s = re.sub(r"(?<![\w&#])#([^\W\d][\w/-]*)",
               lambda m: '<span foreground="#6b46c1" background="#efe9fb">\u2009#%s\u2009</span>' % m.group(1), s)
    s = re.sub(r"==(?=\S)(.+?)(?<=\S)==", r'<span background="#fff1a8">\1</span>', s, flags=re.S)
    s = re.sub(r"\[\^([^\]\s]+)\]", r"<sup>\1</sup>", s)
    s = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"<b>\2</b>", s, flags=re.S)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?!\*)", r"<i>\1</i>", s, flags=re.S)
    s = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"<i>\1</i>", s, flags=re.S)
    s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s, flags=re.S)
    s = re.sub(r"  \n|\\\n", " ", s)
    s = s.replace("\n", " ")

    def link_back(m):
        label, url = links[int(m.group(1))]
        inner = inline_markup(label, mono, math_pool) if label else html.escape(url)
        return '<span foreground="%s" underline="single">%s</span>' % (LINK_COLOR, inner)

    s = re.sub(r"\x02(\d+)\x02", link_back, s)
    s = re.sub(r"\x01(\d+)\x01", lambda m: html.escape(escapes[int(m.group(1))]), s)
    def math_back(m):
        tex = maths[int(m.group(1))]
        if math_pool is not None and len(math_pool) < MATH_MAX:
            math_pool.append(tex)
            return chr(MATH_BASE + len(math_pool) - 1)
        return '<span font_family="Serif" style="italic">%s</span>' % html.escape(tex)

    s = re.sub(r"\x03(\d+)\x03", math_back, s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: '<span font_family="%s" background="%s" size="92%%"> %s </span>'
               % (mono, CODE_BG, html.escape(codes[int(m.group(1))])), s)
    return s


# --------------------------------------------------------------------------
# typesetting


class _Setter:
    BODY = 10.5
    SANS = "Sans"
    MONO = "Monospace"

    def __init__(self, surface, width, height, resolver, pdf_embeds):
        self.surf = surface
        self.cr = cairo.Context(surface)
        self.W, self.H = width, height
        self.margin_x = 56
        self.top = 60
        self.bottom = height - 60
        self.y = self.top
        self.resolver = resolver
        self.pdf_embeds = pdf_embeds
        self.embed_stack = []
        from . import mathrender
        self.math = mathrender if mathrender.available() else None
        self.math_pool = [] if self.math else None

    def md(self, text):
        return inline_markup(text, self.MONO, self.math_pool)

    # -- helpers
    def new_page(self):
        self.cr.show_page()
        self._pages = self._page_no() + 1
        self.y = self.top

    def ensure(self, h):
        if self.y + h > self.bottom and self.y > self.top + 1:
            self.new_page()

    def layout(self, markup, width, size=None, family=None, bold=False, align="left", spacing=1.25):
        lay = PangoCairo.create_layout(self.cr)
        fd = Pango.FontDescription.from_string(family or self.SANS)
        fd.set_size(int((size or self.BODY) * Pango.SCALE))
        if bold:
            fd.set_weight(Pango.Weight.BOLD)
        lay.set_font_description(fd)
        lay.set_width(int(max(10, width) * Pango.SCALE))
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_line_spacing(spacing)
        lay.set_alignment({"left": Pango.Alignment.LEFT, "center": Pango.Alignment.CENTER,
                           "right": Pango.Alignment.RIGHT}[align])
        try:
            lay.set_markup(markup, -1)
        except GLib.Error:
            lay.set_text(re.sub(r"<[^>]+>", "", markup), -1)
        self._attach_maths(lay, size or self.BODY)
        return lay

    def _attach_maths(self, lay, size):
        """Reserve space for inline formulas (private-use placeholders) via shape attributes."""
        lay._maths = []
        if not self.math:
            return
        text = lay.get_text()
        if not any(MATH_BASE <= ord(c) <= MATH_BASE + MATH_MAX for c in text):
            return
        attrs = lay.get_attributes() or Pango.AttrList()
        byte = 0
        for c in text:
            n = len(c.encode("utf-8"))
            k = ord(c) - MATH_BASE
            if 0 <= k < len(self.math_pool):
                box = self.math.layout(self.math_pool[k], size)
                r = Pango.Rectangle()
                r.x, r.y = 0, int(-box.ascent * Pango.SCALE)
                r.width, r.height = int((box.w + 1.5) * Pango.SCALE), int(box.h * Pango.SCALE)
                a = Pango.attr_shape_new(r, r)
                a.start_index, a.end_index = byte, byte + n
                attrs.insert(a)
                lay._maths.append((byte, box))
            byte += n
        lay.set_attributes(attrs)
        if any(box.h > size * 1.6 for _, box in lay._maths):
            # A fixed line-height factor would clip tall formulas (matrices, fractions).
            lay.set_line_spacing(0)
            lay.set_spacing(int(size * 0.3 * Pango.SCALE))

    def _draw_line_maths(self, lay, line, x, baseline_y):
        for bi, box in getattr(lay, "_maths", ()):
            if line.start_index <= bi < line.start_index + line.length:
                pos = lay.index_to_pos(bi)
                box.draw(self.cr, x + pos.x / Pango.SCALE + 0.75, baseline_y - box.ascent)

    def show(self, lay, x, y):
        """PangoCairo.show_layout plus inline formulas."""
        PangoCairo.show_layout(self.cr, lay)
        if getattr(lay, "_maths", None):
            it = lay.get_iter()
            while True:
                self._draw_line_maths(lay, it.get_line_readonly(), x, y + it.get_baseline() / Pango.SCALE)
                if not it.next_line():
                    break

    def flow(self, lay, x, color=(0.1, 0.1, 0.12), line_bg=None, bar=None):
        """Draw a layout line by line, breaking pages between lines."""
        it = lay.get_iter()
        first_y = None
        while True:
            ink, logical = it.get_line_extents()
            top = logical.y / Pango.SCALE
            h = logical.height / Pango.SCALE
            if first_y is None:
                first_y = top
            baseline = it.get_baseline() / Pango.SCALE
            if self.y + (top - first_y) + h > self.bottom and self.y + (top - first_y) > self.top + 1:
                self.new_page()
                first_y = top
            ly = self.y + (top - first_y)
            if line_bg:
                bx, bw, bg = line_bg
                self.cr.set_source_rgb(*bg)
                self.cr.rectangle(bx, ly - 2, bw, h + 4)
                self.cr.fill()
            if bar:
                self.cr.set_source_rgb(0.78, 0.8, 0.84)
                self.cr.rectangle(bar, ly - 1, 3, h + 2)
                self.cr.fill()
            self.cr.set_source_rgb(*color)
            self.cr.move_to(x, ly + (baseline - top))
            PangoCairo.show_layout_line(self.cr, it.get_line_readonly())
            if lay._maths:
                self._draw_line_maths(lay, it.get_line_readonly(), x, ly + (baseline - top))
                self.cr.set_source_rgb(*color)
            last_bottom = ly + h
            if not it.next_line():
                break
        self.y = last_bottom

    # -- blocks
    def blocks(self, blocks, x, width, color=(0.1, 0.1, 0.12), bar=None, tight=False):
        for k, b in enumerate(blocks):
            kind = b[0]
            if kind == "para":
                self.flow(self.layout(self.md(b[1]), width), x, color, bar=bar)
                self.y += 4 if tight else 9
            elif kind == "heading":
                level, text = b[1], b[2]
                size = {1: 22, 2: 18, 3: 15, 4: 13, 5: 12, 6: 11}[level]
                self.y += {1: 14, 2: 12, 3: 8}.get(level, 6) if self.y > self.top + 1 else 0
                lay = self.layout(self.md(text), width, size, bold=True, spacing=1.1)
                self.ensure(lay.get_pixel_extents()[1].height + 30)
                self.flow(lay, x, (0.07, 0.07, 0.09), bar=bar)
                if level <= 2:
                    self.y += 4
                    self.cr.set_source_rgb(0.85, 0.86, 0.89)
                    self.cr.rectangle(x, self.y, width, 1)
                    self.cr.fill()
                self.y += 8
            elif kind == "hr":
                self.ensure(20)
                self.y += 8
                self.cr.set_source_rgb(0.8, 0.81, 0.84)
                self.cr.rectangle(x, self.y, width, 1.2)
                self.cr.fill()
                self.y += 12
            elif kind == "code":
                lay = self.layout(html.escape(b[1]) or " ", width - 20, self.BODY - 1.5, self.MONO, spacing=1.15)
                lay.set_wrap(Pango.WrapMode.CHAR)
                self.y += 6
                self.flow(lay, x + 10, (0.15, 0.17, 0.2), line_bg=(x, width, (0.95, 0.955, 0.965)), bar=bar)
                self.y += 14
            elif kind == "quote":
                self.y += 2
                self.blocks(b[1], x + 16, width - 16, (0.36, 0.38, 0.42), bar=x + 2, tight=True)
                self.y += 6
            elif kind == "list":
                self._list(b, x, width, color, bar)
            elif kind == "table":
                self._table(b, x, width)
            elif kind == "image":
                self._image(b[1], b[2], x, width)
            elif kind == "math":
                self._math_block(b[1], x, width)
            elif kind == "callout":
                self._callout(b, x, width)
            elif kind == "embed":
                self._embed(b, x, width, bar)

    CALLOUT_COLORS = {
        "note": (0.27, 0.47, 0.85), "info": (0.27, 0.47, 0.85), "todo": (0.27, 0.47, 0.85),
        "abstract": (0.1, 0.65, 0.75), "summary": (0.1, 0.65, 0.75), "tldr": (0.1, 0.65, 0.75),
        "tip": (0.1, 0.65, 0.6), "hint": (0.1, 0.65, 0.6), "important": (0.1, 0.65, 0.6),
        "success": (0.2, 0.65, 0.3), "check": (0.2, 0.65, 0.3), "done": (0.2, 0.65, 0.3),
        "question": (0.9, 0.6, 0.1), "help": (0.9, 0.6, 0.1), "faq": (0.9, 0.6, 0.1),
        "warning": (0.93, 0.5, 0.1), "caution": (0.93, 0.5, 0.1), "attention": (0.93, 0.5, 0.1),
        "failure": (0.85, 0.25, 0.25), "fail": (0.85, 0.25, 0.25), "missing": (0.85, 0.25, 0.25),
        "danger": (0.85, 0.2, 0.2), "error": (0.85, 0.2, 0.2), "bug": (0.85, 0.2, 0.2),
        "example": (0.5, 0.35, 0.85), "quote": (0.55, 0.55, 0.6), "cite": (0.55, 0.55, 0.6),
    }

    def _callout(self, b, x, width):
        _, kind, title, body = b
        color = self.CALLOUT_COLORS.get(kind, (0.27, 0.47, 0.85))
        title = title or kind.capitalize()
        self.y += 4
        lay = self.layout('<span foreground="#%02x%02x%02x">%s</span>' % (
            int(color[0] * 255), int(color[1] * 255), int(color[2] * 255), self.md(title)),
            width - 24, bold=True)
        self.ensure(lay.get_pixel_extents()[1].height + 20)
        start_y = self.y
        start_page = self._page_no()
        self.y += 6
        self.flow(lay, x + 14, color)
        self.y += 4
        self.blocks(body, x + 14, width - 24, tight=True)
        end_y = self.y + 2
        # Bar along the callout on its last page (a tint behind text would force raster fallbacks in PDF).
        top = start_y if self._page_no() == start_page else self.top
        self.cr.set_source_rgb(*color)
        self.cr.rectangle(x, top, 3, end_y - top)
        self.cr.fill()
        self.y = end_y + 10

    def _page_no(self):
        return getattr(self, "_pages", 0)

    def _embed(self, b, x, width, bar):
        _, target, heading, size = b
        path = self.resolver.resolve(target)
        low = (path or target).lower()
        if path and low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tif", ".tiff")):
            max_w = None
            if size:
                m = re.match(r"(\d+)", size)
                if m:
                    max_w = float(m.group(1)) * 0.75  # CSS px -> pt
            self._image(target, path, x, width, max_w)
        elif path and low.endswith(".md") and path not in self.embed_stack and len(self.embed_stack) < 3:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = _prepare(f.read())
            except OSError:
                text = ""
            blocks = parse_blocks(text.split("\n"))
            if heading:
                blocks = _section(blocks, heading)
            self.embed_stack.append(path)
            self.y += 2
            self.blocks(blocks, x + 14, width - 14, (0.2, 0.22, 0.26), bar=x + 2, tight=True)
            self.embed_stack.pop()
            self.y += 8
        elif path and low.endswith(".pdf"):
            self.pdf_embeds.append(path)
            self.flow(self.layout("<i>\u2192 PDF \u201e%s\u201c folgt als eigene Seiten</i>"
                                  % html.escape(os.path.basename(path)), width), x, (0.4, 0.4, 0.45))
            self.y += 8
        else:
            self.flow(self.layout("<i>[%s]</i>" % html.escape(target), width), x, (0.45, 0.45, 0.5))
            self.y += 8

    def _list(self, b, x, width, color, bar):
        _, ordered, start, items = b
        indent = 22
        for n, (checked, sub) in enumerate(items):
            marker_y = self.y
            if sub and sub[0][0] in ("para", "heading"):
                # Pre-check that the first line fits so the marker lands with it.
                self.ensure(self.BODY * 1.6)
                marker_y = self.y
            self.cr.set_source_rgb(*color)
            if checked is not None:
                s = self.BODY * 0.95
                self.cr.set_line_width(1.1)
                self.cr.rectangle(x + 3, marker_y + 3.5, s, s)
                if checked:
                    self.cr.set_source_rgb(0.18, 0.45, 0.85)
                    self.cr.fill_preserve()
                    self.cr.stroke()
                    self.cr.set_source_rgb(1, 1, 1)
                    self.cr.set_line_width(1.6)
                    self.cr.move_to(x + 5, marker_y + 3.5 + s * 0.55)
                    self.cr.line_to(x + 3 + s * 0.42, marker_y + 3.5 + s * 0.8)
                    self.cr.line_to(x + 1 + s, marker_y + 5.5)
                self.cr.stroke()
            elif ordered:
                lay = self.layout("%d." % (start + n), indent - 4, align="right")
                self.cr.move_to(x - 4, marker_y)
                PangoCairo.show_layout(self.cr, lay)
            else:
                lay = self.layout("•", indent)
                self.cr.move_to(x + 6, marker_y)
                PangoCairo.show_layout(self.cr, lay)
            self.blocks(sub, x + indent, width - indent, color, bar=bar, tight=True)
        self.y += 6

    def _table(self, b, x, width):
        _, header, aligns, rows = b
        ncol = max(len(header), *(len(r) for r in rows)) if rows else len(header)
        pad = 6
        allrows = [header] + rows
        natural = [0.0] * ncol
        minimum = [0.0] * ncol  # longest unbreakable word per column
        images = {}  # (row, col) -> cairo surface for cells that are just an image
        for ri, r in enumerate(allrows):
            for c in range(ncol):
                txt = r[c] if c < len(r) else ""
                surf = self._cell_image(txt)
                if surf is not None:
                    images[ri, c] = surf
                    natural[c] = max(natural[c], min(surf.get_width() * 0.75, 200) + 2 * pad)
                    minimum[c] = max(minimum[c], min(surf.get_width() * 0.75, 60) + 2 * pad)
                    continue
                markup = self.md(txt)
                lay = self.layout(markup, 10000, self.BODY - 0.5, bold=r is header)
                natural[c] = max(natural[c], lay.get_pixel_extents()[1].width + 2 * pad)
                lay.set_width(Pango.SCALE)
                lay.set_wrap(Pango.WrapMode.WORD)
                minimum[c] = max(minimum[c], lay.get_pixel_extents()[1].width + 2 * pad)
        if sum(natural) <= width:
            widths = natural
        elif sum(minimum) >= width:
            widths = [width * m / sum(minimum) for m in minimum]
        else:
            # Every column gets its longest word; the rest of the space follows the natural widths.
            spare = width - sum(minimum)
            extra = [n - m for n, m in zip(natural, minimum)]
            total_extra = sum(extra) or 1
            widths = [m + spare * e / total_extra for m, e in zip(minimum, extra)]
        tw = sum(widths)
        self.y += 4
        for ri, r in enumerate(allrows):
            lays = []
            heights = []
            for c in range(ncol):
                txt = r[c] if c < len(r) else ""
                al = aligns[c] if c < len(aligns) else "left"
                surf = images.get((ri, c))
                if surf is not None:
                    sc = min((widths[c] - 2 * pad) / surf.get_width(), 0.75)
                    lays.append((surf, sc))
                    heights.append(surf.get_height() * sc)
                    continue
                lay = self.layout(self.md(txt), widths[c] - 2 * pad, self.BODY - 0.5, bold=ri == 0, align=al)
                lays.append(lay)
                heights.append(lay.get_pixel_extents()[1].height)
            rh = max(heights) + 2 * pad
            self.ensure(rh)
            if ri == 0:
                self.cr.set_source_rgb(0.94, 0.95, 0.97)
                self.cr.rectangle(x, self.y, tw, rh)
                self.cr.fill()
            elif ri % 2 == 0:
                self.cr.set_source_rgb(0.975, 0.978, 0.985)
                self.cr.rectangle(x, self.y, tw, rh)
                self.cr.fill()
            cx = x
            for c, lay in enumerate(lays):
                if isinstance(lay, tuple):
                    surf, sc = lay
                    self.cr.save()
                    self.cr.translate(cx + pad, self.y + pad)
                    self.cr.scale(sc, sc)
                    self.cr.set_source_surface(surf, 0, 0)
                    self.cr.paint()
                    self.cr.restore()
                else:
                    self.cr.set_source_rgb(0.1, 0.1, 0.12)
                    self.cr.move_to(cx + pad, self.y + pad)
                    self.show(lay, cx + pad, self.y + pad)
                cx += widths[c]
            self.cr.set_source_rgb(0.82, 0.83, 0.86)
            self.cr.set_line_width(0.8)
            self.cr.rectangle(x, self.y, tw, rh)
            cx = x
            for c in range(ncol - 1):
                cx += widths[c]
                self.cr.move_to(cx, self.y)
                self.cr.line_to(cx, self.y + rh)
            self.cr.stroke()
            self.y += rh
        self.y += 12

    def _cell_image(self, txt):
        """Surface for a table cell that consists of a single image, else None."""
        m = _INLINE_IMG.fullmatch(txt.strip())
        if not m:
            return None
        tok = m.group(0)
        if tok.startswith("![["):
            e = _EMBED.match(tok)
            src = e.group(1).strip() if e else None
        else:
            src = m.group(2).strip("<>")
        data = load_image_bytes(src, self.resolver) if src else None
        if not data:
            return None
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            from .render import pixbuf_to_surface
            return pixbuf_to_surface(loader.get_pixbuf().apply_embedded_orientation())
        except (GLib.Error, AttributeError, TypeError):
            return None

    def _math_block(self, tex, x, width):
        if not self.math:
            lay = self.layout(html.escape(tex), width - 20, self.BODY - 1.5, self.MONO, spacing=1.15)
            self.flow(lay, x + 10, (0.15, 0.17, 0.2), line_bg=(x, width, (0.95, 0.955, 0.965)))
            self.y += 14
            return
        box = self.math.display(tex, self.BODY + 1.5)
        s = min(1.0, width / box.w) if box.w else 1.0
        self.y += 4
        self.ensure(box.h * s + 8)
        self.cr.save()
        self.cr.translate(x + (width - box.w * s) / 2, self.y)
        self.cr.scale(s, s)
        box.draw(self.cr, 0, 0)
        self.cr.restore()
        self.y += box.h * s + 12

    def _image(self, alt, src, x, width, max_w=None):
        data = load_image_bytes(src, self.resolver) if src else None
        try:
            if not data:
                raise ValueError("no data")
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pb = loader.get_pixbuf().apply_embedded_orientation()
        except (GLib.Error, ValueError, TypeError, AttributeError):
            self.flow(self.layout("<i>[Bild: %s]</i>" % html.escape(alt or ""), width), x, (0.45, 0.45, 0.5))
            self.y += 8
            return
        from .render import pixbuf_to_surface
        surf = pixbuf_to_surface(pb)
        iw, ih = surf.get_width(), surf.get_height()
        s = min(width / iw, (self.bottom - self.top) * 0.9 / ih, 1.0, (max_w or 1e9) / iw)
        w, h = iw * s, ih * s
        self.ensure(h + 8)
        self.cr.save()
        self.cr.translate(x + (width - w) / 2, self.y)
        self.cr.scale(s, s)
        self.cr.set_source_surface(surf, 0, 0)
        self.cr.paint()
        self.cr.restore()
        self.y += h + 12


def _prepare(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip YAML front matter (Obsidian properties).
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:].lstrip("\n")
    # Obsidian comments may span lines.
    return re.sub(r"%%.*?%%", "", text, flags=re.S)


def _section(blocks, heading):
    """Blocks under the given heading (for ``![[Note#Heading]]``)."""
    out = None
    level = 0
    want = heading.strip().lower()
    for b in blocks:
        if b[0] == "heading":
            if out is not None and b[1] <= level:
                break
            if out is None and b[2].strip().lower() == want:
                out = [b]
                level = b[1]
                continue
        if out is not None:
            out.append(b)
    return out or blocks


def typeset_pdf(text, width=595.28, height=841.89, base_dir=None, resolver=None, pdf_embeds=None):
    """Typeset Markdown to PDF bytes with the given page size in points.

    Embedded PDFs (``![[file.pdf]]``) are appended to ``pdf_embeds`` so the
    caller can add their pages after the note.
    """
    text = _prepare(text)
    blocks = parse_blocks(text.split("\n"))
    buf = io.BytesIO()
    surf = cairo.PDFSurface(buf, width, height)
    resolver = resolver or Resolver(base_dir, find_vault_root(base_dir) if base_dir else None)
    s = _Setter(surf, width, height, resolver, pdf_embeds if pdf_embeds is not None else [])
    s.blocks(blocks, s.margin_x, width - 2 * s.margin_x)
    s.cr.show_page()
    surf.finish()
    return buf.getvalue()
