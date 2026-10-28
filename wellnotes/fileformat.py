"""Loading/saving notebooks as ``.goodnotes`` files and importing other formats.

Files are written as real GoodNotes containers (protobuf index + notes +
attachments) so GoodNotes can read them.  Alongside we store
``wellnotes.json`` holding the lossless WellNotes model (pressure, text
boxes, highlighter, page templates).  ``gn_signature`` in that JSON is the
SHA-1 of ``index.events.pb``; if GoodNotes rewrote the file the signature
no longer matches and we re-import from the GoodNotes data instead.
"""

import hashlib
import json
import os
import re
import tempfile
import zipfile

from . import goodnotes as gn
from .model import Background, Document, Image, Page, Stroke, PAGE_SIZES, PAPER_COLORS
from .render import ResourceCache, page_template_pdf, thumbnail_jpeg

EXT = ".goodnotes"


def load(path):
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "wellnotes.json" in names:
            meta = json.loads(z.read("wellnotes.json"))
            sig = meta.get("gn_signature")
            events = z.read("index.events.pb") if "index.events.pb" in names else b""
            if not sig or sig == hashlib.sha1(events).hexdigest():
                atts = {n.split("/", 1)[1]: z.read(n) for n in names
                        if n.startswith("attachments/") and not n.endswith("/")}
                keep = set(meta.get("attachments", atts.keys()))
                doc = Document.from_json(meta, {k: v for k, v in atts.items() if k in keep})
                doc.path = path
                return doc
    doc = from_goodnotes(gn.read_goodnotes(path))
    doc.path = path
    if not doc.title:
        doc.title = title_from_path(path)
    return doc


def title_from_path(path):
    title = os.path.splitext(os.path.basename(path))[0]
    # Notion exports append a 32-digit hex id to every page name.
    return re.sub(r"\s+[0-9a-f]{32}$", "", title) or title


def from_goodnotes(g):
    doc = Document(g.title)
    doc.attachments = dict(g.attachments)
    for gp in g.pages:
        if gp.template_att and gp.template_att in doc.attachments:
            bg = Background("pdf", (1, 1, 1), gp.template_att, max(0, gp.template_page - 1))
        else:
            bg = Background("blank", (gp.paper or (1, 1, 1))[:3])
        page = Page(gp.width, gp.height, bg)
        for el in gp.elements:
            if el["kind"] == "stroke":
                for path in el["paths"]:
                    pts = [(x, y, 0.5) for x, y in path]
                    page.elements.append(Stroke(pts, el["color"], el["width"], "pen", "ball"))
            elif el["kind"] == "image":
                page.elements.append(Image(el["att"], el["x"], el["y"], el["w"], el["h"]))
        doc.pages.append(page)
    if not doc.pages:
        w, h = PAGE_SIZES["a4"]
        doc.pages.append(Page(w, h))
    return doc


def _template_key(page):
    texts = tuple((t.text, t.x, t.y, t.w, t.size, tuple(t.color), t.font) for t in page.elements if t.kind == "text")
    bg = page.background
    return (page.width, page.height, bg.kind, tuple(bg.color), bg.att, bg.index, texts)


def save(doc, path, res=None):
    res = res or ResourceCache(doc)
    if not hasattr(doc, "_gn_templates"):
        doc._gn_templates = {}
    used = set()
    for p in doc.pages:
        if p.background.att:
            used.add(p.background.att)
        for e in p.elements:
            if e.kind == "image":
                used.add(e.att)
    atts = {k: v for k, v in doc.attachments.items() if k in used}

    gn_atts = dict(atts)
    gn_pages = []
    for p in doc.pages:
        key = _template_key(p)
        if key not in doc._gn_templates:
            doc._gn_templates[key] = (_att_id(key), page_template_pdf(p, res))
        tid, data = doc._gn_templates[key]
        gn_atts[tid] = data
        els = []
        for e in p.elements:
            if e.kind == "stroke":
                color = tuple(e.color)
                width = e.width
                if e.tool == "highlighter":
                    color = color[:3] + (0.4,)
                elif e.style != "ball":
                    avg = sum(pt[2] for pt in e.points) / len(e.points)
                    width = e.width * (0.35 + 0.9 * avg)
                els.append({"kind": "stroke", "points": e.points, "width": width, "color": color})
            elif e.kind == "image":
                els.append({"kind": "image", "att": e.att, "x": e.x, "y": e.y, "w": e.w, "h": e.h})
        gn_pages.append({"width": p.width, "height": p.height, "template_att": tid, "template_page": 1,
                         "paper": p.background.color + (1,), "elements": els})

    thumb = thumbnail_jpeg(doc.pages[0], res) if doc.pages else None
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".wellnotes-", suffix=EXT, dir=d)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            events = gn.write_goodnotes(z, doc.title, gn_pages, gn_atts, thumb)
            written = set(z.namelist())
            for k, v in atts.items():
                if "attachments/" + k not in written:
                    z.writestr("attachments/" + k, v)
            meta = doc.to_json()
            meta["gn_signature"] = hashlib.sha1(events).hexdigest()
            meta["attachments"] = sorted(atts)
            z.writestr("wellnotes.json", json.dumps(meta, separators=(",", ":")))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    doc.path = path
    doc.dirty = False
    return thumb


def _att_id(key):
    h = hashlib.sha1(repr(key).encode()).hexdigest().upper()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:32])


def read_thumbnail(path):
    try:
        with zipfile.ZipFile(path) as z:
            if "thumbnail.jpg" in z.namelist():
                return z.read("thumbnail.jpg")
    except (zipfile.BadZipFile, OSError):
        pass
    return None


def read_cover_color(path):
    try:
        with zipfile.ZipFile(path) as z:
            if "wellnotes.json" in z.namelist():
                return json.loads(z.read("wellnotes.json")).get("cover")
    except (zipfile.BadZipFile, OSError, ValueError):
        pass
    return None


# --------------------------------------------------------------------------
# new documents and imports


def new_notebook(title, size="a4", paper="lined", color="white", cover=None):
    doc = Document(title)
    if cover:
        doc.cover = cover
    w, h = PAGE_SIZES.get(size, PAGE_SIZES["a4"])
    doc.pages.append(Page(w, h, Background(paper, PAPER_COLORS.get(color, (1, 1, 1)))))
    return doc


def pdf_pages(doc, data):
    import gi
    gi.require_version("Poppler", "0.18")
    from gi.repository import GLib, Poppler
    pdf = Poppler.Document.new_from_bytes(GLib.Bytes.new(data), None)
    att = doc.add_attachment(data)
    pages = []
    for i in range(pdf.get_n_pages()):
        pw, ph = pdf.get_page(i).get_size()
        pages.append(Page(pw * gn.GN_SCALE, ph * gn.GN_SCALE, Background("pdf", (1, 1, 1), att, i)))
    return pages


def image_page(doc, data, like=None):
    from .render import ResourceCache
    att = doc.add_attachment(data)
    surf = ResourceCache(doc).image(att)
    if surf is None:
        raise ValueError("Bild konnte nicht gelesen werden")
    w, h = (like.width, like.height) if like else PAGE_SIZES["a4"]
    bg = like.background.copy() if like else Background("blank")
    page = Page(w, h, bg)
    page.elements.append(fit_image(att, surf.get_width(), surf.get_height(), page))
    return page


def fit_image(att, iw, ih, page, cx=None, cy=None, max_frac=0.8):
    s = min(page.width * max_frac / iw, page.height * max_frac / ih, 1.5)
    w, h = iw * s, ih * s
    cx = page.width / 2 if cx is None else cx
    cy = page.height / 2 if cy is None else cy
    return Image(att, cx - w / 2, cy - h / 2, w, h)


def markdown_pages(doc, text, base_dir=None, size="a4", resolver=None):
    """Typeset Markdown into pages; PDFs embedded with ``![[file.pdf]]`` follow as extra pages."""
    from .markdown import typeset_pdf
    w, h = PAGE_SIZES.get(size, PAGE_SIZES["a4"])
    embeds = []
    data = typeset_pdf(text, w / gn.GN_SCALE, h / gn.GN_SCALE, base_dir, resolver=resolver, pdf_embeds=embeds)
    pages = pdf_pages(doc, data)
    for path in dict.fromkeys(embeds):
        try:
            with open(path, "rb") as f:
                pages += pdf_pages(doc, f.read())
        except Exception:  # unreadable or broken PDF: keep the note itself
            continue
    return pages


def import_file(path):
    """Create a Document from an importable file (PDF, Markdown, image)."""
    ext = os.path.splitext(path)[1].lower()
    title = title_from_path(path)
    with open(path, "rb") as f:
        data = f.read()
    doc = Document(title)
    if ext == ".pdf":
        doc.pages = pdf_pages(doc, data)
    elif ext in (".md", ".markdown", ".mdown", ".mkd"):
        doc.pages = markdown_pages(doc, data.decode("utf-8", "replace"), os.path.dirname(path))
    else:
        doc.pages = [image_page(doc, data)]
    if not doc.pages:
        raise ValueError("Keine Seiten gefunden")
    return doc
