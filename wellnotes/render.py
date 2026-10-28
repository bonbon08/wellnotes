"""Cairo rendering of pages, elements and exports."""

import io
import math
import warnings

import cairo
import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gdk, GdkPixbuf, GLib, Pango, PangoCairo, Poppler  # noqa: E402

LINE_SPACING = 29.333  # GoodNotes ruled line spacing in page units


class ResourceCache:
    """Decoded attachments (PDF documents and image surfaces) per document."""

    def __init__(self, doc):
        self.doc = doc
        self.pdfs = {}
        self.images = {}

    def pdf(self, att):
        if att not in self.pdfs:
            data = self.doc.attachments.get(att)
            pdf = None
            if data:
                try:
                    pdf = Poppler.Document.new_from_bytes(GLib.Bytes.new(data), None)
                except GLib.Error:
                    pdf = None
            self.pdfs[att] = pdf
        return self.pdfs[att]

    def image(self, att):
        if att not in self.images:
            surf = None
            data = self.doc.attachments.get(att)
            if data:
                try:
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(data)
                    loader.close()
                    pb = loader.get_pixbuf().apply_embedded_orientation()
                    surf = pixbuf_to_surface(pb)
                except GLib.Error:
                    surf = None
            self.images[att] = surf
        return self.images[att]


def pixbuf_to_surface(pb):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
    cr = cairo.Context(surf)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    surf.flush()
    return surf


def _is_dark(rgb):
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] < 0.5


def draw_background(cr, page, res, vector=True):
    bg = page.background
    w, h = page.width, page.height
    cr.save()
    cr.rectangle(0, 0, w, h)
    cr.clip()
    cr.set_source_rgb(*bg.color[:3])
    cr.paint()
    line = (0.75, 0.82, 0.9) if not _is_dark(bg.color) else (0.35, 0.37, 0.42)
    if bg.kind in ("lined", "narrow"):
        step = LINE_SPACING if bg.kind == "lined" else LINE_SPACING * 0.75
        cr.set_source_rgb(*line)
        cr.set_line_width(1.0)
        y = step * 3.5
        while y < h - step:
            cr.move_to(0, y)
            cr.line_to(w, y)
            y += step
        cr.stroke()
        cr.set_source_rgba(0.9, 0.45, 0.45, 0.7)
        cr.move_to(step * 3.2, 0)
        cr.line_to(step * 3.2, h)
        cr.stroke()
    elif bg.kind == "grid":
        step = LINE_SPACING * 0.85
        cr.set_source_rgb(*line)
        cr.set_line_width(0.8)
        x = (w % step) / 2
        while x < w:
            cr.move_to(x, 0)
            cr.line_to(x, h)
            x += step
        y = (h % step) / 2
        while y < h:
            cr.move_to(0, y)
            cr.line_to(w, y)
            y += step
        cr.stroke()
    elif bg.kind == "dotted":
        step = LINE_SPACING * 0.85
        cr.set_source_rgb(*[c * 0.9 for c in line])
        y = (h % step) / 2
        while y < h:
            x = (w % step) / 2
            while x < w:
                cr.arc(x, y, 1.4, 0, 2 * math.pi)
                cr.close_path()
                x += step
            y += step
        cr.fill()
    elif bg.kind == "pdf" and bg.att:
        pdf = res.pdf(bg.att)
        if pdf and 0 <= bg.index < pdf.get_n_pages():
            pp = pdf.get_page(bg.index)
            pw, ph = pp.get_size()
            cr.scale(w / pw, h / ph)
            if vector:
                pp.render_for_printing(cr)
            else:
                pp.render(cr)
    cr.restore()


def _smooth_path(cr, pts):
    cr.move_to(pts[0][0], pts[0][1])
    if len(pts) == 1:
        cr.line_to(pts[0][0] + 0.01, pts[0][1])
        return
    if len(pts) == 2:
        cr.line_to(pts[1][0], pts[1][1])
        return
    for i in range(1, len(pts) - 1):
        x0, y0 = cr.get_current_point()
        cx, cy = pts[i][0], pts[i][1]
        mx, my = (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2
        cr.curve_to(x0 + 2 / 3 * (cx - x0), y0 + 2 / 3 * (cy - y0),
                    mx + 2 / 3 * (cx - mx), my + 2 / 3 * (cy - my), mx, my)
    cr.line_to(pts[-1][0], pts[-1][1])


def _pressure_width(width, p):
    return width * (0.35 + 0.9 * p)


def draw_stroke(cr, s):
    pts = s.points
    if not pts:
        return
    cr.save()
    if s.tool == "highlighter":
        cr.set_operator(cairo.OPERATOR_MULTIPLY)
        cr.set_source_rgba(s.color[0], s.color[1], s.color[2], 0.45)
        cr.set_line_cap(cairo.LINE_CAP_SQUARE)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(s.width)
        _smooth_path(cr, pts)
        cr.stroke()
        cr.restore()
        return
    cr.set_source_rgba(*s.color)
    pressures = [p[2] for p in pts]
    if s.style == "ball" or max(pressures) - min(pressures) < 0.02:
        w = s.width if s.style == "ball" else _pressure_width(s.width, pressures[0])
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_width(w)
        _smooth_path(cr, pts)
        cr.stroke()
    else:
        _fill_variable(cr, pts, s.width)
    cr.restore()


def _fill_variable(cr, pts, width):
    # Dedupe points that are too close; they produce unstable normals.
    clean = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - clean[-1][0]) + abs(p[1] - clean[-1][1]) > 0.3:
            clean.append(p)
    pts = clean
    if len(pts) < 2:
        r = _pressure_width(width, pts[0][2]) / 2
        cr.arc(pts[0][0], pts[0][1], r, 0, 2 * math.pi)
        cr.fill()
        return
    # Smooth pressure a little to avoid blotches.
    pr = [p[2] for p in pts]
    sm = [(pr[max(0, i - 1)] + 2 * pr[i] + pr[min(len(pr) - 1, i + 1)]) / 4 for i in range(len(pr))]
    left, right = [], []
    n = len(pts)
    for i in range(n):
        a = pts[max(0, i - 1)]
        b = pts[min(n - 1, i + 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / ln, dx / ln
        r = _pressure_width(width, sm[i]) / 2
        left.append((pts[i][0] + nx * r, pts[i][1] + ny * r))
        right.append((pts[i][0] - nx * r, pts[i][1] - ny * r))
    _smooth_path(cr, left)
    r_end = _pressure_width(width, sm[-1]) / 2
    a_end = math.atan2(left[-1][1] - pts[-1][1], left[-1][0] - pts[-1][0])
    cr.arc_negative(pts[-1][0], pts[-1][1], r_end, a_end, a_end - math.pi)
    rr = right[::-1]
    for i in range(1, len(rr) - 1):
        x0, y0 = cr.get_current_point()
        cx, cy = rr[i]
        mx, my = (rr[i][0] + rr[i + 1][0]) / 2, (rr[i][1] + rr[i + 1][1]) / 2
        cr.curve_to(x0 + 2 / 3 * (cx - x0), y0 + 2 / 3 * (cy - y0),
                    mx + 2 / 3 * (cx - mx), my + 2 / 3 * (cy - my), mx, my)
    cr.line_to(*rr[-1])
    r_start = _pressure_width(width, sm[0]) / 2
    a_start = math.atan2(right[0][1] - pts[0][1], right[0][0] - pts[0][0])
    cr.arc_negative(pts[0][0], pts[0][1], r_start, a_start, a_start - math.pi)
    cr.close_path()
    cr.set_fill_rule(cairo.FILL_RULE_WINDING)
    cr.fill()


def text_layout(cr, t):
    layout = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(t.font)
    fd.set_absolute_size(t.size * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_width(int(t.w * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_text(t.text or "", -1)
    return layout


def draw_text(cr, t):
    cr.save()
    layout = text_layout(cr, t)
    t.h = layout.get_pixel_extents()[1].height + t.size * 0.3
    cr.move_to(t.x, t.y)
    cr.set_source_rgba(*t.color)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def draw_image(cr, im, res):
    surf = res.image(im.att)
    cr.save()
    if surf is None:
        cr.rectangle(im.x, im.y, im.w, im.h)
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.3)
        cr.fill()
    else:
        cr.translate(im.x, im.y)
        cr.scale(im.w / surf.get_width(), im.h / surf.get_height())
        cr.set_source_surface(surf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
    cr.restore()


def draw_element(cr, el, res):
    if el.kind == "stroke":
        draw_stroke(cr, el)
    elif el.kind == "image":
        draw_image(cr, el, res)
    elif el.kind == "text":
        draw_text(cr, el)


def draw_elements(cr, page, res, clip=None, skip=()):
    # Images first so ink always lies on top of pasted pictures (as in GoodNotes).
    for kind_pass in ("image", None):
        for el in page.elements:
            if (el.kind == "image") != (kind_pass == "image") or el.id in skip:
                continue
            if clip is not None:
                b = el.bbox()
                if b[2] < clip[0] or b[0] > clip[2] or b[3] < clip[1] or b[1] > clip[3]:
                    continue
            draw_element(cr, el, res)


def render_page(cr, page, res, vector=False, skip=()):
    draw_background(cr, page, res, vector=vector)
    draw_elements(cr, page, res, skip=skip)


def page_surface(page, res, scale, skip=()):
    w = max(1, int(page.width * scale))
    h = max(1, int(page.height * scale))
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    cr.scale(scale, scale)
    render_page(cr, page, res, skip=skip)
    surf.flush()
    return surf


def surface_to_texture(surf):
    surf.flush()
    data = bytes(surf.get_data())
    return Gdk.MemoryTexture.new(surf.get_width(), surf.get_height(), Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(data), surf.get_stride())


def thumbnail_jpeg(page, res, width=480):
    surf = page_surface(page, res, width / page.width)
    buf = io.BytesIO()
    surf.write_to_png(buf)
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(buf.getvalue())
    loader.close()
    ok, data = loader.get_pixbuf().save_to_bufferv("jpeg", ["quality"], ["85"])
    return bytes(data)


def export_pdf(doc, res, path, pages=None):
    pages = pages or doc.pages
    if not pages:
        return
    from .goodnotes import GN_SCALE
    surf = cairo.PDFSurface(path, pages[0].width / GN_SCALE, pages[0].height / GN_SCALE)
    cr = cairo.Context(surf)
    for page in pages:
        surf.set_size(page.width / GN_SCALE, page.height / GN_SCALE)
        cr.save()
        cr.scale(1 / GN_SCALE, 1 / GN_SCALE)
        render_page(cr, page, res, vector=True)
        cr.restore()
        cr.show_page()
    surf.finish()


def page_template_pdf(page, res, include_text=True):
    """One-page PDF of the page background (+ text boxes) for GoodNotes export."""
    from .goodnotes import GN_SCALE
    buf = io.BytesIO()
    surf = cairo.PDFSurface(buf, page.width / GN_SCALE, page.height / GN_SCALE)
    cr = cairo.Context(surf)
    cr.scale(1 / GN_SCALE, 1 / GN_SCALE)
    draw_background(cr, page, res, vector=True)
    if include_text:
        for el in page.elements:
            if el.kind == "text":
                draw_text(cr, el)
    cr.show_page()
    surf.finish()
    return buf.getvalue()
