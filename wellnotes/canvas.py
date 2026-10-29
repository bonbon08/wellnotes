"""The notebook canvas: continuous vertical page view with pen input."""

import math
import queue
import threading

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import render, shapes  # noqa: E402
from .model import Stroke, Text, bbox_union, clone_elements, dist_point_segment, point_in_polygon  # noqa: E402

PAD = 40.0  # page units around/between pages
MAX_CACHE_PIXELS = 10_000_000
STRIP = 256  # rows per GPU texture strip of a page cache
HANDLE = 9  # screen px
INK_TILE = 128  # device px per GPU texture tile of the live ink layer


class Tools:
    """Shared tool settings (the editor toolbar writes these)."""

    def __init__(self):
        self.tool = "pen"
        self.pen_color = (0.08, 0.08, 0.1, 1.0)
        self.pen_width = 2.4
        self.pen_style = "fountain"
        self.hl_color = (1.0, 0.86, 0.2, 1.0)
        self.hl_width = 22.0
        self.shape_color = (0.08, 0.08, 0.1, 1.0)
        self.shape_width = 2.4
        self.eraser_size = 14.0
        self.eraser_precise = False
        self.text_size = 28.0
        self.text_color = (0.08, 0.08, 0.1, 1.0)
        self.text_on_lines = True
        self.finger_draw = False


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, w, h)


def _rgba(r, g, b, a=1.0):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


class _Cache:
    """A rendered page: a cairo surface plus lazily uploaded GPU textures in horizontal strips.

    Drawing onto the surface only re-uploads the strips that changed, so a new
    stroke costs a few hundred kilobytes instead of the whole page.
    """

    def __init__(self, version, scale, surface, skip):
        self.version = version
        self.scale = scale
        self.surface = surface
        self.skip = skip
        self.strips = [None] * ((surface.get_height() + STRIP - 1) // STRIP)

    def texture(self, i):
        """Strip ``i`` as (texture, first row).  Strips include one row of their neighbours, so
        filtering at the strip edges blends with real page pixels instead of transparency
        (which showed up as thin grey lines across the page)."""
        strip = self.strips[i]
        if strip is None:
            surf = self.surface
            surf.flush()
            stride = surf.get_stride()
            y0 = max(0, i * STRIP - 1)
            y1 = min(surf.get_height(), (i + 1) * STRIP + 1)
            data = surf.get_data()[y0 * stride:y1 * stride]
            tex = Gdk.MemoryTexture.new(surf.get_width(), y1 - y0, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                        GLib.Bytes.new(bytes(data)), stride)
            strip = self.strips[i] = (tex, y0)
        return strip

    def dirty(self, y0, y1):
        """Mark page-unit rows y0..y1 as changed."""
        a = max(0, (int(y0 * self.scale) - 1) // STRIP)
        b = min(len(self.strips) - 1, (int(math.ceil(y1 * self.scale)) + 1) // STRIP)
        for i in range(a, b + 1):
            self.strips[i] = None


class _Renderer:
    """Renders page caches in a background thread so scrolling never blocks on drawing."""

    def __init__(self, doc, on_done):
        self.on_done = on_done
        self.res = render.ResourceCache(doc)  # own Poppler documents: not shared across threads
        self.jobs = queue.Queue()
        self.pending = set()
        threading.Thread(target=self._run, daemon=True).start()

    def request(self, page, scale, skip):
        key = (page.id, page.version, round(scale, 4), skip)
        if key in self.pending:
            return
        self.pending.add(key)
        snapshot = render_snapshot(page)
        self.jobs.put((key, snapshot, scale, skip))

    def _run(self):
        while True:
            key, page, scale, skip = self.jobs.get()
            try:
                surf = render.page_surface(page, self.res, scale, skip=skip)
            except Exception as e:  # keep the worker alive; the page stays blank
                print("WellNotes: Rendern fehlgeschlagen:", e)
                surf = None
            GLib.idle_add(self._done, key, surf)

    def _done(self, key, surf):
        self.pending.discard(key)
        if surf is not None:
            self.on_done(key, surf)
        return False


def render_snapshot(page):
    """A frozen copy of a page for the render thread (elements are immutable)."""
    from .model import Page
    p = Page(page.width, page.height, page.background.copy(), list(page.elements), page.id)
    p.version = page.version
    return p


class NotebookCanvas(Gtk.Widget):
    __gtype_name__ = "WellNotesCanvas"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "page-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "selection-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "zoom-changed": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
    }

    def __init__(self, doc, tools):
        super().__init__()
        self.doc = doc
        self.res = render.ResourceCache(doc)
        self.tools = tools
        self.zoom = 0.5
        self.fit_width = True
        self._cache = {}
        self._renderer = _Renderer(doc, self._on_rendered)
        self._ink_layer = None
        self._ink_tiles = {}
        self._sel_tex = None
        self._layout = []
        self._content = (1, 1)
        self._current_page = 0
        self._rerender_id = 0

        self.hadj = Gtk.Adjustment()
        self.vadj = Gtk.Adjustment()
        self.hadj.connect("value-changed", self._on_scrolled)
        self.vadj.connect("value-changed", self._on_scrolled)

        # interaction state
        self._mode = None
        self._page = None
        self._points = []
        self._token = None
        self._erased = False
        self._last_erase = None
        self._lasso = []
        self.selection = None  # dict(page, ids, bbox)
        self._sel_offset = (0.0, 0.0)
        self._sel_scale = 1.0
        self._sel_anchor = None
        self._pan_start = None
        self._hover = None
        self._text_editor = None
        self._drag_start = (0, 0)
        self._stylus_seen = False

        self.set_focusable(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.set_cursor(Gdk.Cursor.new_from_name("crosshair"))

        drag = Gtk.GestureDrag()
        drag.set_button(0)
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        self._drag = drag

        # Pens go through GestureStylus, GTK's controller for tablet tools: it reports
        # every movement while the tip is down, together with the coalesced samples.
        stylus = Gtk.GestureStylus()
        stylus.connect("down", self._on_stylus_down)
        stylus.connect("motion", self._on_stylus_motion)
        stylus.connect("up", self._on_stylus_up)
        self.add_controller(stylus)
        self._stylus = stylus
        self._stylus_active = False

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES |
                                               Gtk.EventControllerScrollFlags.KINETIC)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        zoom = Gtk.GestureZoom()
        zoom.connect("begin", self._on_zoom_begin)
        zoom.connect("scale-changed", self._on_zoom_scale)
        zoom.connect("end", self._on_zoom_end)
        self.add_controller(zoom)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        self.relayout()

    # ------------------------------------------------------------------
    # layout and geometry

    def relayout(self):
        maxw = max((p.width for p in self.doc.pages), default=1000)
        y = PAD
        self._layout = []
        for p in self.doc.pages:
            self._layout.append(((maxw - p.width) / 2 + PAD, y))
            y += p.height + PAD
        self._content = (maxw + 2 * PAD, y)
        self._update_adjustments()
        self.queue_draw()

    def _update_adjustments(self):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        if self.fit_width:
            self.zoom = max(0.05, (w - 24) / self._content[0])
            self.zoom = min(self.zoom, 3.0)
        cw, ch = self._content[0] * self.zoom, self._content[1] * self.zoom
        for adj, total, size, step in ((self.hadj, cw, w, 40), (self.vadj, ch, h, 60)):
            conf = (min(adj.get_value(), max(0, total - size)), 0, max(total, size), step, size * 0.9, size)
            cur = (adj.get_value(), adj.get_lower(), adj.get_upper(), adj.get_step_increment(),
                   adj.get_page_increment(), adj.get_page_size())
            # Reconfiguring unconditionally makes the scrollbars re-queue an allocation -> layout loop.
            if any(abs(x - y) > 0.01 for x, y in zip(conf, cur)):
                adj.configure(*conf)

    def do_size_allocate(self, width, height, baseline):
        self._update_adjustments()
        te = self._text_editor
        if te and te.get("rect"):
            alloc = Gdk.Rectangle()
            alloc.x, alloc.y, alloc.width, alloc.height = te["rect"]
            te["view"].size_allocate(alloc, -1)

    def _origin(self):
        w = self.get_width()
        cw = self._content[0] * self.zoom
        ox = (w - cw) / 2 if cw < w else -self.hadj.get_value()
        return ox, -self.vadj.get_value()

    def page_rect(self, i):
        """Screen rect (x, y, w, h) of page i."""
        ox, oy = self._origin()
        px, py = self._layout[i]
        p = self.doc.pages[i]
        return ox + px * self.zoom, oy + py * self.zoom, p.width * self.zoom, p.height * self.zoom

    def to_page(self, sx, sy, index=None):
        """Screen -> (page index, x, y) in page units.  Uses the nearest page if not given."""
        ox, oy = self._origin()
        dx, dy = (sx - ox) / self.zoom, (sy - oy) / self.zoom
        if index is None:
            index = 0
            for i, (px, py) in enumerate(self._layout):
                if dy >= py - PAD / 2:
                    index = i
        if not self._layout:
            return None, 0, 0
        px, py = self._layout[index]
        return index, dx - px, dy - py

    def to_screen(self, index, x, y):
        ox, oy = self._origin()
        px, py = self._layout[index]
        return ox + (px + x) * self.zoom, oy + (py + y) * self.zoom

    def page_index(self, page):
        for i, p in enumerate(self.doc.pages):
            if p is page:
                return i
        return -1

    @property
    def current_page(self):
        return min(self._current_page, len(self.doc.pages) - 1)

    def visible_range(self):
        h = self.get_height()
        res = []
        for i in range(len(self.doc.pages)):
            x, y, w, ph = self.page_rect(i)
            if y + ph >= 0 and y <= h:
                res.append(i)
        return res

    def scroll_to_page(self, i, animate=False):
        if not 0 <= i < len(self._layout):
            return
        self.vadj.set_value(max(0, (self._layout[i][1] - PAD / 2) * self.zoom))

    def _on_scrolled(self, *a):
        self._update_current_page()
        self._place_text_editor()
        self.queue_draw()

    def _update_current_page(self):
        h = self.get_height()
        best, best_vis = self._current_page, -1
        for i in range(len(self.doc.pages)):
            x, y, w, ph = self.page_rect(i)
            vis = min(y + ph, h) - max(y, 0)
            if vis > best_vis:
                best, best_vis = i, vis
        if best != self._current_page:
            self._current_page = best
            self.emit("page-changed", best)

    # ------------------------------------------------------------------
    # zoom

    def set_zoom(self, zoom, sx=None, sy=None):
        zoom = max(0.08, min(6.0, zoom))
        if sx is None:
            sx, sy = self.get_width() / 2, self.get_height() / 2
        ox, oy = self._origin()
        dx, dy = (sx - ox) / self.zoom, (sy - oy) / self.zoom
        self.fit_width = False
        self.zoom = zoom
        self._update_adjustments()
        w = self.get_width()
        cw = self._content[0] * zoom
        if cw >= w:
            self.hadj.set_value(dx * zoom - sx)
        self.vadj.set_value(dy * zoom - sy)
        self._schedule_rerender()
        self._place_text_editor()
        self.queue_draw()
        self.emit("zoom-changed", zoom)

    def zoom_fit(self):
        page = self.current_page
        self.fit_width = True
        self._update_adjustments()
        self.scroll_to_page(page)
        self._schedule_rerender()
        self.emit("zoom-changed", self.zoom)

    def _on_zoom_begin(self, g, seq):
        self._stop_kinetic()
        self._zoom_start = self.zoom
        if self._mode in ("ink", "shape", "erase", "lasso"):
            self._cancel_interaction()
        self._mode = "pinch"
        ok, cx, cy = g.get_bounding_box_center()
        ox, oy = self._origin()
        # Content point under the fingers; it follows the fingers (pinch + two-finger pan).
        self._pinch_anchor = ((cx - ox) / self.zoom, (cy - oy) / self.zoom)

    def _on_zoom_end(self, g, seq):
        if self._mode == "pinch":
            self._mode = None
        self._schedule_rerender()

    def _on_zoom_scale(self, g, scale):
        ok, cx, cy = g.get_bounding_box_center()
        self.set_zoom(self._zoom_start * scale, cx, cy)
        ax, ay = self._pinch_anchor
        if self._content[0] * self.zoom >= self.get_width():
            self.hadj.set_value(ax * self.zoom - cx)
        self.vadj.set_value(ay * self.zoom - cy)

    # -- kinetic scrolling after a finger pan

    def _pan_sample(self, dx, dy):
        t = GLib.get_monotonic_time() / 1e6
        self._pan_samples.append((t, dx, dy))
        while len(self._pan_samples) > 2 and t - self._pan_samples[0][0] > 0.1:
            self._pan_samples.pop(0)

    def _start_kinetic(self):
        s = self._pan_samples
        if len(s) < 2 or s[-1][0] - s[0][0] <= 0:
            return
        dt = s[-1][0] - s[0][0]
        vx, vy = -(s[-1][1] - s[0][1]) / dt, -(s[-1][2] - s[0][2]) / dt
        if math.hypot(vx, vy) < 150:
            return
        self._kinetic = [vx, vy, GLib.get_monotonic_time() / 1e6]
        self._kinetic_id = self.add_tick_callback(self._kinetic_tick)

    def _kinetic_tick(self, widget, clock):
        if not self._kinetic:
            return False
        vx, vy, last = self._kinetic
        now = clock.get_frame_time() / 1e6
        dt = min(0.05, max(0.0, now - last))
        self.hadj.set_value(self.hadj.get_value() + vx * dt)
        self.vadj.set_value(self.vadj.get_value() + vy * dt)
        decay = math.exp(-4.0 * dt)
        vx, vy = vx * decay, vy * decay
        if math.hypot(vx, vy) < 20:
            self._kinetic = None
            self._kinetic_id = 0
            return False
        self._kinetic = [vx, vy, now]
        return True

    def _stop_kinetic(self):
        if getattr(self, "_kinetic_id", 0):
            self.remove_tick_callback(self._kinetic_id)
        self._kinetic = None
        self._kinetic_id = 0

    def _on_scroll(self, ctl, dx, dy):
        state = ctl.get_current_event_state()
        unit = ctl.get_unit()
        if state & Gdk.ModifierType.CONTROL_MASK:
            ev = ctl.get_current_event()
            pt = self._event_widget_point(ev) if ev else None
            factor = math.exp(-dy * (0.1 if unit == Gdk.ScrollUnit.WHEEL else 0.01))
            if pt:
                self.set_zoom(self.zoom * factor, *pt)
            else:
                self.set_zoom(self.zoom * factor)
            return True
        mult = 60 if unit == Gdk.ScrollUnit.WHEEL else 1
        if state & Gdk.ModifierType.SHIFT_MASK and dx == 0:
            dx, dy = dy, 0
        self.hadj.set_value(self.hadj.get_value() + dx * mult)
        self.vadj.set_value(self.vadj.get_value() + dy * mult)
        return True

    def _event_widget_point(self, ev):
        native = self.get_native()
        if native is None:
            return None
        ok, ex, ey = ev.get_position()
        if not ok:
            return None
        tx, ty = native.get_surface_transform()
        ok, p = native.compute_point(self, Graphene.Point().init(ex - tx, ey - ty))
        return (p.x, p.y) if ok else None

    # ------------------------------------------------------------------
    # page caches

    def invalidate(self, page=None):
        """Re-render a page (or all); the old picture stays visible until the new one is ready."""
        for c in ([self._cache.get(page.id)] if page else list(self._cache.values())):
            if c is not None:
                c.version = -1
        self.queue_draw()

    def _desired_scale(self, page):
        sf = self.get_scale_factor()
        s = self.zoom * sf
        cap = math.sqrt(MAX_CACHE_PIXELS / (page.width * page.height))
        return min(s, cap)

    def _cache_for(self, page, skip=frozenset()):
        """The best available cache for a page; schedules a (re)render when it is outdated."""
        c = self._cache.get(page.id)
        want = self._desired_scale(page)
        stale = c is None or c.version != page.version or c.skip != skip or \
            (self._mode != "pinch" and not self._rerender_id and abs(c.scale / want - 1) > 0.05)
        if stale:
            self._renderer.request(page, want, skip)
        return c

    def _on_rendered(self, key, surf):
        pid, version, scale, skip = key
        page = next((p for p in self.doc.pages if p.id == pid), None)
        if page is None or version != page.version:
            self.queue_draw()  # outdated result; the next draw requests a fresh one
            return
        self._cache[pid] = _Cache(version, scale, surf, skip)
        self.queue_draw()

    def _schedule_rerender(self):
        if self._rerender_id:
            GLib.source_remove(self._rerender_id)
        self._rerender_id = GLib.timeout_add(180, self._rerender_visible)

    def _rerender_visible(self):
        self._rerender_id = 0
        self.queue_draw()  # _cache_for notices the scale mismatch and requests new renders
        return False

    def _evict(self, visible):
        keep = set()
        for i in visible:
            for j in range(i - 2, i + 3):
                if 0 <= j < len(self.doc.pages):
                    keep.add(self.doc.pages[j].id)
        for pid in list(self._cache):
            if pid not in keep:
                del self._cache[pid]

    def _paint_onto_cache(self, page, elements, old_version):
        """Incrementally draw freshly added elements onto an up-to-date cache."""
        c = self._cache.get(page.id)
        if c is None or c.version != old_version or c.skip:
            return
        cr = cairo.Context(c.surface)
        cr.scale(c.scale, c.scale)
        cr.rectangle(0, 0, page.width, page.height)
        cr.clip()
        for el in elements:
            render.draw_element(cr, el, self.res)
        box = bbox_union(e.bbox() for e in elements)
        if box:
            c.dirty(box[1], box[3])
        c.version = page.version

    def _repaint_region(self, page, box, old_version):
        c = self._cache.get(page.id)
        if c is None or c.version != old_version or c.skip:
            return
        cr = cairo.Context(c.surface)
        cr.scale(c.scale, c.scale)
        x0, y0, x1, y1 = box
        cr.rectangle(math.floor(x0 * c.scale) / c.scale, math.floor(y0 * c.scale) / c.scale,
                     (math.ceil((x1 - x0) * c.scale) + 2) / c.scale, (math.ceil((y1 - y0) * c.scale) + 2) / c.scale)
        cr.clip()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        render.draw_background(cr, page, self.res, vector=False)
        render.draw_elements(cr, page, self.res, clip=box)
        c.dirty(y0, y1)
        c.version = page.version

    # ------------------------------------------------------------------
    # drawing (render nodes: page textures are scaled by the GPU)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        dark = Adw.StyleManager.get_default().get_dark()
        snapshot.append_color(_rgba(0.17, 0.17, 0.19) if dark else _rgba(0.89, 0.89, 0.91), _rect(0, 0, w, h))
        visible = self.visible_range()
        self._evict(visible)
        sel_page = self.selection["page"] if self.selection else None
        moving = self._mode in ("move", "scale") and self.selection
        shadow = _rgba(0, 0, 0, 0.18)
        for i in visible:
            page = self.doc.pages[i]
            x, y, pw, ph = self.page_rect(i)
            snapshot.append_color(shadow, _rect(x + 1, y + 2, pw, ph))
            skip = frozenset(self.selection["ids"]) if moving and page is sel_page else frozenset()
            if self._text_editor and self._text_editor["page"] is page and self._text_editor.get("el"):
                skip = skip | {self._text_editor["el"].id}
            c = self._cache_for(page, skip)
            snapshot.push_clip(_rect(x, y, pw, ph))
            if c is None:
                bg = page.background.color
                snapshot.append_color(_rgba(bg[0], bg[1], bg[2]), _rect(x, y, pw, ph))
            else:
                k = self.zoom / c.scale  # logical px per cache pixel
                cw = c.surface.get_width() * k
                filt = Gsk.ScalingFilter.TRILINEAR if k < 0.7 else Gsk.ScalingFilter.LINEAR
                for si in range(len(c.strips)):
                    sy = y + si * STRIP * k
                    sh = min(STRIP, c.surface.get_height() - si * STRIP) * k
                    if sy + sh < 0 or sy > h:
                        continue
                    tex, row0 = c.texture(si)
                    snapshot.append_scaled_texture(tex, filt, _rect(x, y + row0 * k, cw, tex.get_height() * k))
            if moving and page is sel_page and self._sel_tex:
                tex, bx0, by0, bx1, by1 = self._sel_tex
                fn, sc = self._transform_fn()
                ax, ay = fn(bx0, by0)
                sx, sy = self.to_screen(i, ax, ay)
                snapshot.append_scaled_texture(tex, Gsk.ScalingFilter.LINEAR,
                                               _rect(sx, sy, (bx1 - bx0) * sc * self.zoom,
                                                     (by1 - by0) * sc * self.zoom))
            snapshot.pop()
        self._snapshot_overlays(snapshot, w, h)
        if self._text_editor:
            self.snapshot_child(self._text_editor["view"], snapshot)

    def _snapshot_overlays(self, snapshot, w, h):
        if self._ink_layer is not None and self._ink_tiles:
            if self._ink_alpha_hl:
                snapshot.push_opacity(0.45)
            sf = self._ink_layer.get_device_scale()[0]
            for key in self._ink_tiles:
                tex = self._ink_tile(key)
                snapshot.append_texture(tex, _rect(key[0] * INK_TILE / sf, key[1] * INK_TILE / sf,
                                                   tex.get_width() / sf, tex.get_height() / sf))
            if self._ink_alpha_hl:
                snapshot.pop()
        # Each overlay gets its own tightly bounded cairo node, so e.g. moving the
        # eraser cursor only re-rasterises a few pixels.
        if self._mode == "lasso" and self._page is not None and len(self._lasso) > 1:
            i = self.page_index(self._page)
            pts = [self.to_screen(i, lx, ly) for lx, ly in self._lasso]
            bx0, by0 = min(p[0] for p in pts) - 3, min(p[1] for p in pts) - 3
            bx1, by1 = max(p[0] for p in pts) + 3, max(p[1] for p in pts) + 3
            cr = snapshot.append_cairo(_rect(bx0, by0, bx1 - bx0, by1 - by0))
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.set_source_rgb(0.2, 0.45, 0.9)
            for k, (sx, sy) in enumerate(pts):
                (cr.move_to if k == 0 else cr.line_to)(sx, sy)
            cr.close_path()
            cr.stroke_preserve()
            cr.set_source_rgba(0.2, 0.45, 0.9, 0.06)
            cr.fill()
        if self.selection and self.page_index(self.selection["page"]) >= 0:
            x0, y0, x1, y1 = self._selection_screen_box()
            m = HANDLE + 8
            cr = snapshot.append_cairo(_rect(x0 - m, y0 - m, x1 - x0 + 2 * m, y1 - y0 + 2 * m))
            self._draw_selection(cr)
        if self._hover and self.tools.tool == "eraser" and self._mode in (None, "erase"):
            sx, sy = self._hover
            r = self.tools.eraser_size * self.zoom
            cr = snapshot.append_cairo(_rect(sx - r - 2, sy - r - 2, 2 * r + 4, 2 * r + 4))
            cr.set_source_rgba(0.3, 0.3, 0.35, 0.8)
            cr.set_line_width(1)
            cr.arc(sx, sy, r, 0, 2 * math.pi)
            cr.stroke()

    # -- live ink: drawn incrementally into a screen-sized layer; only the tiles a new
    # segment touches are uploaded again, so long strokes stay cheap on slow tablets.

    def _ink_begin(self):
        sf = self.get_scale_factor()
        w, h = max(1, self.get_width()), max(1, self.get_height())
        layer = cairo.ImageSurface(cairo.FORMAT_ARGB32, w * sf, h * sf)
        layer.set_device_scale(sf, sf)
        self._ink_layer = layer
        self._ink_cr = cairo.Context(layer)
        self._ink_cr.set_line_cap(cairo.LINE_CAP_ROUND)
        self._ink_cr.set_line_join(cairo.LINE_JOIN_ROUND)
        self._ink_tiles = {}
        st = self._live_stroke()
        self._ink_alpha_hl = st.tool == "highlighter"
        self._ink_style = st
        self._ink_drawn = 0
        self._ink_extend()

    def _ink_extend(self):
        """Draw the not yet drawn segments of the live stroke into the ink layer."""
        if self._ink_layer is None:
            return
        st = self._ink_style
        pts = self._points
        cr = self._ink_cr
        i = self._page_index
        r, g, b = st.color[:3]
        cr.set_source_rgb(r, g, b)
        start = max(1, self._ink_drawn)
        if len(pts) == 1 and self._ink_drawn == 0:
            sx, sy = self.to_screen(i, pts[0][0], pts[0][1])
            w = self._ink_width(st, pts[0][2])
            cr.arc(sx, sy, w / 2, 0, 2 * math.pi)
            cr.fill()
            self._ink_dirty(sx, sy, sx, sy, w)
            self._ink_drawn = 1
            return
        for k in range(start, len(pts)):
            a, b_ = pts[k - 1], pts[k]
            ax, ay = self.to_screen(i, a[0], a[1])
            bx, by = self.to_screen(i, b_[0], b_[1])
            w = self._ink_width(st, (a[2] + b_[2]) / 2)
            cr.set_line_width(w)
            cr.move_to(ax, ay)
            cr.line_to(bx, by)
            cr.stroke()
            self._ink_dirty(ax, ay, bx, by, w)
        self._ink_drawn = len(pts)

    def _ink_width(self, st, p):
        # Same width model as render.draw_stroke, so the stroke doesn't jump when committed.
        base = st.width if st.tool == "highlighter" or st.style == "ball" else st.width * (0.35 + 0.9 * p)
        return max(0.8, base * self.zoom)

    def _ink_dirty(self, ax, ay, bx, by, w):
        """Mark the tiles under a new segment (widget coordinates) as changed."""
        layer = self._ink_layer
        sf = layer.get_device_scale()[0]
        m = w / 2 + 2
        cols = (layer.get_width() - 1) // INK_TILE
        rows = (layer.get_height() - 1) // INK_TILE
        tx0 = max(0, int((min(ax, bx) - m) * sf) // INK_TILE)
        tx1 = min(cols, int((max(ax, bx) + m) * sf) // INK_TILE)
        ty0 = max(0, int((min(ay, by) - m) * sf) // INK_TILE)
        ty1 = min(rows, int((max(ay, by) + m) * sf) // INK_TILE)
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                self._ink_tiles[(tx, ty)] = None

    def _ink_tile(self, key):
        tex = self._ink_tiles[key]
        if tex is None:
            layer = self._ink_layer
            layer.flush()
            stride = layer.get_stride()
            data = layer.get_data()
            x0, y0 = key[0] * INK_TILE, key[1] * INK_TILE
            w = min(INK_TILE, layer.get_width() - x0)
            h = min(INK_TILE, layer.get_height() - y0)
            rows = b"".join(data[(y0 + r) * stride + x0 * 4:(y0 + r) * stride + (x0 + w) * 4] for r in range(h))
            tex = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, GLib.Bytes.new(rows), w * 4)
            self._ink_tiles[key] = tex
        return tex

    def _ink_end(self):
        self._ink_layer = None
        self._ink_cr = None
        self._ink_tiles = {}

    def _live_stroke(self):
        t = self.tools
        if self._mode == "shape":
            return Stroke(self._points, t.shape_color, t.shape_width, "pen", "ball")
        if t.tool == "highlighter":
            return Stroke(self._points, t.hl_color, t.hl_width, "highlighter")
        return Stroke(self._points, t.pen_color, t.pen_width, "pen", t.pen_style)

    def _make_selection_texture(self):
        """Render the selection once; while it is dragged only this texture moves."""
        els = self.selected_elements()
        if not els:
            self._sel_tex = None
            return
        x0, y0, x1, y1 = bbox_union(e.bbox() for e in els)
        scale = self.zoom * self.get_scale_factor()
        cap = math.sqrt(8_000_000 / max(1.0, (x1 - x0) * (y1 - y0)))
        scale = min(scale, cap)
        w, h = max(1, int((x1 - x0) * scale) + 2), max(1, int((y1 - y0) * scale) + 2)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        cr.scale(scale, scale)
        cr.translate(-x0, -y0)
        for el in els:
            render.draw_element(cr, el, self.res)
        self._sel_tex = (render.surface_to_texture(surf), x0, y0, x0 + w / scale, y0 + h / scale)

    def _selection_screen_box(self):
        sel = self.selection
        i = self.page_index(sel["page"])
        x0, y0, x1, y1 = self._transformed_bbox()
        sx0, sy0 = self.to_screen(i, x0, y0)
        sx1, sy1 = self.to_screen(i, x1, y1)
        return sx0, sy0, sx1, sy1

    def _draw_selection(self, cr):
        if self.page_index(self.selection["page"]) < 0:
            self.selection = None
            return
        x0, y0, x1, y1 = self._selection_screen_box()
        cr.save()
        cr.set_source_rgba(0.2, 0.45, 0.9, 0.08)
        cr.rectangle(x0 - 4, y0 - 4, x1 - x0 + 8, y1 - y0 + 8)
        cr.fill_preserve()
        cr.set_source_rgb(0.2, 0.45, 0.9)
        cr.set_line_width(1.2)
        cr.set_dash([5, 3])
        cr.stroke()
        cr.set_dash([])
        for hx, hy in ((x0 - 4, y0 - 4), (x1 + 4, y0 - 4), (x0 - 4, y1 + 4), (x1 + 4, y1 + 4)):
            cr.arc(hx, hy, HANDLE / 2 + 1, 0, 2 * math.pi)
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(0.2, 0.45, 0.9)
            cr.stroke()
        cr.restore()

    # ------------------------------------------------------------------
    # input

    def _event_info(self, gesture):
        ev = gesture.get_current_event()
        pressure = None
        source = None
        tool_type = None
        if ev is not None:
            dev = ev.get_device()
            source = dev.get_source() if dev else None
            tool = ev.get_device_tool()
            if tool is not None:
                tool_type = tool.get_tool_type()
            ok, p = ev.get_axis(Gdk.AxisUse.PRESSURE)
            if ok:
                pressure = p
        return ev, source, tool_type, pressure

    def _history_points(self, gesture, ev, wx, wy):
        """Coalesced motion samples for smoother ink (widget coordinates).

        The coordinate space of history samples differs between backends and
        devices (some tablets report zeros or device coordinates), so samples
        are only used if they all lie between the previous and current point.
        """
        pts = []
        if ev is None or ev.get_event_type() != Gdk.EventType.MOTION_NOTIFY:
            return pts
        if isinstance(gesture, Gtk.GestureStylus):
            # The stylus backlog is already in widget coordinates.
            try:
                ok, hist = gesture.get_backlog()
            except (TypeError, AttributeError, ValueError):
                return pts
            if not ok or not hist:
                return pts
            offx = offy = 0.0
        else:
            try:
                hist = ev.get_history()
            except (TypeError, AttributeError):
                return pts
            if not hist:
                return pts
            ok, ex, ey = ev.get_position()
            if not ok:
                return pts
            offx, offy = wx - ex, wy - ey
        for tc in hist:
            try:
                axes = tc.axes
                x, y = axes[Gdk.AxisUse.X], axes[Gdk.AxisUse.Y]
                p = axes[Gdk.AxisUse.PRESSURE] if tc.flags & Gdk.AxisFlags.PRESSURE else None
            except (AttributeError, IndexError, TypeError):
                return []
            pts.append((x + offx, y + offy, p))
        lx, ly = self._last_wpt
        margin = 12
        x0, x1 = min(lx, wx) - margin, max(lx, wx) + margin
        y0, y1 = min(ly, wy) - margin, max(ly, wy) + margin
        if not all(x0 <= x <= x1 and y0 <= y <= y1 for x, y, _ in pts):
            return []
        return [(x, y, None if p is None else min(1.0, max(0.0, p))) for x, y, p in pts]

    def _on_stylus_down(self, g, x, y):
        self._stylus_active = True
        self._on_drag_begin(g, x, y)

    def _on_stylus_motion(self, g, x, y):
        if self._stylus_active:  # "motion" is also emitted while the pen hovers
            self._on_drag_update(g, x - self._drag_start[0], y - self._drag_start[1])

    def _on_stylus_up(self, g, x, y):
        if self._stylus_active:
            self._stylus_active = False
            self._on_drag_end(g, x - self._drag_start[0], y - self._drag_start[1])

    def _for_stylus(self, g, ev=None):
        """True for drag-gesture events that GestureStylus handles instead."""
        if g is not self._drag:
            return False
        if self._stylus_active:
            return True
        ev = ev or g.get_current_event()
        return ev is not None and ev.get_device_tool() is not None

    def _on_drag_begin(self, g, sx, sy):
        if self._for_stylus(g):
            g.set_state(Gtk.EventSequenceState.DENIED)
            return
        self._stop_kinetic()
        self.grab_focus()
        ev, source, tool_type, pressure = self._event_info(g)
        button = g.get_current_button()
        state = ev.get_modifier_state() if ev else 0
        self._drag_start = (sx, sy)
        self._last_wpt = (sx, sy)
        if self._text_editor:
            self.commit_text()
        is_touch = source == Gdk.InputSource.TOUCHSCREEN
        is_pen = source in (Gdk.InputSource.PEN,) or tool_type in (
            Gdk.DeviceToolType.PEN, Gdk.DeviceToolType.ERASER, Gdk.DeviceToolType.BRUSH,
            Gdk.DeviceToolType.PENCIL, Gdk.DeviceToolType.AIRBRUSH)
        if is_pen:
            self._stylus_seen = True
        if button == 2 or (is_touch and (not self.tools.finger_draw or self._stylus_seen)):
            self._mode = "pan"
            self._pan_start = (self.hadj.get_value(), self.vadj.get_value())
            self._pan_samples = []
            self._pan_kinetic = is_touch
            return
        if button == 3 and not is_pen:
            g.set_state(Gtk.EventSequenceState.DENIED)
            return
        tool = self.tools.tool
        if tool_type == Gdk.DeviceToolType.ERASER or (is_pen and state & (Gdk.ModifierType.BUTTON2_MASK |
                                                                          Gdk.ModifierType.BUTTON3_MASK)):
            tool = "eraser"
        g.set_state(Gtk.EventSequenceState.CLAIMED)
        index, px, py = self.to_page(sx, sy)
        if index is None:
            return
        page = self.doc.pages[index]

        if self.selection and tool != "lasso" and tool != "eraser":
            self.clear_selection()
        if tool == "lasso" and self.selection:
            hit = self._selection_hit(sx, sy)
            if hit:
                self._mode = hit
                self._sel_offset = (0.0, 0.0)
                self._sel_scale = 1.0
                self._make_selection_texture()
                if hit == "scale":
                    self._sel_anchor = self._scale_anchor
                return
            self.clear_selection()

        self._page = page
        self._page_index = index
        p = 0.5 if pressure is None else pressure
        if tool in ("pen", "highlighter"):
            self._mode = "ink"
            self._points = [(px, py, p)]
            self._pressured = pressure is not None
            self._ink_begin()
        elif tool == "shape":
            self._mode = "shape"
            self._points = [(px, py, 0.5)]
            self._pressured = False
            self._ink_begin()
        elif tool == "eraser":
            self._mode = "erase"
            self._token = self.doc.begin([page])
            self._erased = False
            self._last_erase = None
            self._erase_at(px, py)
        elif tool == "lasso":
            self._mode = "lasso"
            self._lasso = [(px, py)]
        elif tool == "text":
            self._mode = "text"
        self.queue_draw()

    def _on_drag_update(self, g, dx, dy):
        if g is self._drag and self._stylus_active:
            return
        sx, sy = self._drag_start[0] + dx, self._drag_start[1] + dy
        if self._mode == "pan":
            hx, vy = self._pan_start
            self.hadj.set_value(hx - dx)
            self.vadj.set_value(vy - dy)
            self._pan_sample(dx, dy)
            return
        if self._mode is None or self._page is None and self._mode not in ("move", "scale"):
            return
        ev, source, tool_type, pressure = self._event_info(g)
        if self._mode in ("move", "scale"):
            i = self.page_index(self.selection["page"])
            _, px0, py0 = self.to_page(*self._drag_start, index=i)
            _, px, py = self.to_page(sx, sy, index=i)
            if self._mode == "move":
                self._sel_offset = (px - px0, py - py0)
            else:
                ax, ay, cx0, cy0 = self._sel_anchor
                d0 = math.hypot(cx0 - ax, cy0 - ay) or 1
                d1 = math.hypot(px - ax, py - ay)
                self._sel_scale = max(0.05, d1 / d0)
            self.queue_draw()
            return
        if pressure is not None:
            pressure = min(1.0, max(0.0, pressure))
        samples = self._history_points(g, ev, sx, sy) + [(sx, sy, pressure)]
        self._last_wpt = (sx, sy)
        for wx, wy, pr in samples:
            _, px, py = self.to_page(wx, wy, index=self._page_index)
            if self._mode in ("ink", "shape"):
                lx, ly, _ = self._points[-1]
                if math.hypot(px - lx, py - ly) * self.zoom < 0.8:
                    continue
                if self._mode == "ink":
                    p = pr if pr is not None else (0.5 if not self._pressured else self._points[-1][2])
                    self._points.append((px, py, p))
                else:
                    self._points.append((px, py, 0.5))
            elif self._mode == "erase":
                self._erase_at(px, py)
            elif self._mode == "lasso":
                self._lasso.append((px, py))
        if self._mode in ("ink", "shape"):
            self._ink_extend()
        self.queue_draw()

    def _on_drag_end(self, g, dx, dy):
        if g is self._drag and self._stylus_active:
            return
        mode = self._mode
        self._mode = None
        if mode == "pan" and self._pan_kinetic:
            self._start_kinetic()
        if mode == "ink" or mode == "shape":
            self._finish_ink(mode)
            self._ink_end()
        elif mode == "erase":
            if self._erased:
                self.doc.commit(self._token)
                self.emit("changed")
            self._token = None
        elif mode == "lasso":
            self._finish_lasso()
        elif mode in ("move", "scale"):
            self._finish_transform()
            self._sel_tex = None
        elif mode == "text":
            sx, sy = self._drag_start[0] + dx, self._drag_start[1] + dy
            self._start_text(sx, sy)
        self._page = None
        self._points = []
        self._lasso = []
        self.queue_draw()

    def _cancel_interaction(self):
        if self._mode == "erase" and self._erased and self._token:
            self.doc.commit(self._token)
            self.emit("changed")
        self._mode = None
        self._stylus_active = False
        self._page = None
        self._points = []
        self._lasso = []
        self._ink_end()
        self._sel_tex = None
        self.queue_draw()

    def _on_motion(self, ctl, x, y):
        self._hover = (x, y)
        if self.tools.tool == "eraser":
            self.queue_draw()
        if self.tools.tool == "lasso" and self.selection:
            hit = self._selection_hit(x, y)
            name = {"move": "move", "scale": "nwse-resize"}.get(hit, "crosshair")
            self.set_cursor(Gdk.Cursor.new_from_name(name))
        elif self.tools.tool == "text":
            self.set_cursor(Gdk.Cursor.new_from_name("text"))
        else:
            self.set_cursor(Gdk.Cursor.new_from_name("crosshair" if self.tools.tool != "eraser" else "none"))

    def _on_leave(self, ctl):
        self._hover = None
        self.queue_draw()

    # ------------------------------------------------------------------
    # ink

    def _finish_ink(self, mode):
        page = self._page
        pts = self._points
        if not pts or page is None:
            return
        t = self.tools
        if mode == "shape":
            shape = shapes.recognize(pts)
            if shape:
                pts = [(x, y, 0.5) for x, y in shape]
            stroke = Stroke(pts, t.shape_color, t.shape_width, "pen", "ball")
        else:
            if len(pts) == 1:
                pts = pts + [(pts[0][0] + 0.3, pts[0][1] + 0.3, pts[0][2])]
            stroke = self._live_stroke()
            stroke.points = pts
        self.add_elements(page, [stroke])

    def add_elements(self, page, elements):
        token = self.doc.begin([page])
        old = page.version
        page.elements.extend(elements)
        self.doc.commit(token)
        if any(e.kind == "image" for e in elements):
            self.invalidate(page)
        else:
            self._paint_onto_cache(page, elements, old)
        self.queue_draw()
        self.emit("changed")

    # ------------------------------------------------------------------
    # eraser

    def _erase_at(self, x, y):
        page = self._page
        r = self.tools.eraser_size
        samples = [(x, y)]
        if self._last_erase:
            lx, ly = self._last_erase
            d = math.hypot(x - lx, y - ly)
            n = int(d / (r * 0.5))
            samples = [(lx + (x - lx) * k / (n + 1), ly + (y - ly) * k / (n + 1)) for k in range(1, n + 1)] + samples
        self._last_erase = (x, y)
        box = (min(s[0] for s in samples) - r, min(s[1] for s in samples) - r,
               max(s[0] for s in samples) + r, max(s[1] for s in samples) + r)
        changed = False
        new_elements = []
        dirty = []
        for el in page.elements:
            if el.kind != "stroke":
                new_elements.append(el)
                continue
            b = el.bbox()
            if b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3]:
                new_elements.append(el)
                continue
            rr = r + el.width / 2
            if self.tools.eraser_precise:
                pieces = _split_stroke(el, samples, rr)
                if pieces is None:
                    new_elements.append(el)
                    continue
                new_elements.extend(pieces)
            else:
                if not _stroke_hit(el, samples, rr):
                    new_elements.append(el)
                    continue
            changed = True
            dirty.append(b)
        if changed:
            old = page.version
            page.elements = new_elements
            page.touch()
            self._erased = True
            self._repaint_region(page, bbox_union(dirty), old)
            self.queue_draw()

    # ------------------------------------------------------------------
    # lasso & selection

    def _finish_lasso(self):
        page = self._page
        poly = self._lasso
        if len(poly) < 3 or page is None:
            return
        ids = set()
        for el in page.elements:
            if el.kind == "stroke":
                pts = el.points
                step = max(1, len(pts) // 24)
                sample = pts[::step]
                inside = sum(1 for p in sample if point_in_polygon(p[0], p[1], poly))
                if inside >= max(1, len(sample) * 0.5):
                    ids.add(el.id)
            else:
                b = el.bbox()
                if point_in_polygon((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, poly):
                    ids.add(el.id)
        if ids:
            self.select(page, ids)

    def select(self, page, ids):
        els = [e for e in page.elements if e.id in ids]
        if not els:
            self.clear_selection()
            return
        self.selection = {"page": page, "ids": set(ids), "bbox": bbox_union(e.bbox() for e in els)}
        self._sel_offset = (0.0, 0.0)
        self._sel_scale = 1.0
        self.queue_draw()
        self.emit("selection-changed", True)

    def clear_selection(self):
        if self.selection:
            self.selection = None
            self.queue_draw()
            self.emit("selection-changed", False)

    def selected_elements(self):
        if not self.selection:
            return []
        return [e for e in self.selection["page"].elements if e.id in self.selection["ids"]]

    def _selection_hit(self, sx, sy):
        x0, y0, x1, y1 = self._selection_screen_box()
        corners = {(x0 - 4, y0 - 4): (x1, y1), (x1 + 4, y0 - 4): (x0, y1),
                   (x0 - 4, y1 + 4): (x1, y0), (x1 + 4, y1 + 4): (x0, y0)}
        i = self.page_index(self.selection["page"])
        for (hx, hy), (ax, ay) in corners.items():
            if math.hypot(sx - hx, sy - hy) <= HANDLE + 4:
                _, pax, pay = self.to_page(ax, ay, index=i)
                _, pcx, pcy = self.to_page(hx, hy, index=i)
                self._scale_anchor = (pax, pay, pcx, pcy)
                return "scale"
        if x0 - 6 <= sx <= x1 + 6 and y0 - 6 <= sy <= y1 + 6:
            return "move"
        return None

    def _transform_fn(self):
        dx, dy = self._sel_offset
        s = self._sel_scale
        if s != 1.0 and self._sel_anchor:
            ax, ay = self._sel_anchor[:2]
            return (lambda x, y: (ax + (x - ax) * s, ay + (y - ay) * s)), s
        return (lambda x, y: (x + dx, y + dy)), 1.0

    def _transformed_bbox(self):
        x0, y0, x1, y1 = self.selection["bbox"]
        fn, _ = self._transform_fn()
        a = fn(x0, y0)
        b = fn(x1, y1)
        return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])

    def _transformed_selection(self):
        fn, s = self._transform_fn()
        return [e.transformed(fn, s) for e in self.selected_elements()]

    def _finish_transform(self):
        if not self.selection:
            return
        page = self.selection["page"]
        if self._sel_offset == (0.0, 0.0) and self._sel_scale == 1.0:
            self.emit("selection-changed", True)
            return
        new = {e.id: t for e, t in zip(self.selected_elements(), self._transformed_selection())}
        self._sel_offset = (0.0, 0.0)
        self._sel_scale = 1.0
        self.replace_elements(page, new)
        self.select(page, {e.id for e in new.values()})

    def replace_elements(self, page, mapping):
        """Replace elements by id (value None deletes)."""
        token = self.doc.begin([page])
        out = []
        for e in page.elements:
            if e.id in mapping:
                if mapping[e.id] is not None:
                    out.append(mapping[e.id])
            else:
                out.append(e)
        page.elements = out
        self.doc.commit(token)
        self.invalidate(page)
        self.queue_draw()
        self.emit("changed")

    def delete_selection(self):
        if not self.selection:
            return
        page = self.selection["page"]
        ids = self.selection["ids"]
        self.clear_selection()
        self.replace_elements(page, {i: None for i in ids})

    def recolor_selection(self, color):
        if not self.selection:
            return
        page = self.selection["page"]
        new = {e.id: e.recolored(color) for e in self.selected_elements()}
        self.replace_elements(page, new)
        self.select(page, {e.id for e in new.values()})

    def duplicate_selection(self):
        if not self.selection:
            return
        page = self.selection["page"]
        copies = [e.transformed(lambda x, y: (x + 30, y + 30)) for e in self.selected_elements()]
        self.add_elements(page, copies)
        self.select(page, {e.id for e in copies})

    def paste_elements(self, elements, page=None):
        page = page or self.doc.pages[self.current_page]
        els = clone_elements(elements)
        b = bbox_union(e.bbox() for e in els)
        # Place in the visible part of the page.
        i = self.page_index(page)
        _, cx, cy = self.to_page(self.get_width() / 2, self.get_height() / 2, index=i)
        dx = cx - (b[0] + b[2]) / 2
        dy = cy - (b[1] + b[3]) / 2
        els = [e.transformed(lambda x, y: (x + dx, y + dy)) for e in els]
        self.add_elements(page, els)
        self.select(page, {e.id for e in els})

    def insert_image(self, att, page=None):
        page = page or self.doc.pages[self.current_page]
        surf = self.res.image(att)
        if surf is None:
            return False
        from .fileformat import fit_image
        i = self.page_index(page)
        _, cx, cy = self.to_page(self.get_width() / 2, self.get_height() / 2, index=i)
        cx = min(max(cx, 0), page.width)
        cy = min(max(cy, 0), page.height)
        im = fit_image(att, surf.get_width(), surf.get_height(), page, cx, cy, max_frac=0.6)
        self.add_elements(page, [im])
        self.select(page, {im.id})
        return True

    # ------------------------------------------------------------------
    # text boxes

    def _start_text(self, sx, sy):
        index, px, py = self.to_page(sx, sy)
        if index is None:
            return
        page = self.doc.pages[index]
        if not (0 <= px <= page.width and 0 <= py <= page.height):
            return
        el = None
        for e in reversed(page.elements):
            if e.kind == "text":
                b = e.bbox()
                if b[0] <= px <= b[2] and b[1] <= py <= b[3]:
                    el = e
                    break
        t = self.tools
        lines = render.paper_lines(page) if t.text_on_lines else None
        line_h = 0.0
        if el is None:
            if lines:
                # Snap to the ruled line under the click: the text row is the band above that line.
                step, y0, margin = lines
                row = max(0, math.ceil((py - y0) / step))
                el_y = y0 + (row - 1) * step
                line_h = step
                size = step * 0.62
                if page.background.kind in ("lined", "narrow") and px < margin + step * 0.4:
                    px = margin + step * 0.4
            else:
                el_y = py - t.text_size * 0.7
                size = t.text_size
            w = min(page.width * 0.75, page.width - px - 30)
            if w < 120:
                px = max(10, page.width - 30 - 300)
                w = 300
            el_x, el_w = px, w
            color, text = t.text_color, ""
        else:
            el_x, el_y, el_w, size, color, text = el.x, el.y, el.w, el.size, el.color, el.text
            line_h = el.line_h
        self.open_text_editor(page, el, el_x, el_y, el_w, size, color, text, line_h)

    def open_text_editor(self, page, el, x, y, w, size, color, text, line_h=0.0):
        tv = Gtk.TextView()
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        tv.add_css_class("wn-text-editor")
        tv.get_buffer().set_text(text)
        tv.set_parent(self)
        self._text_editor = {"page": page, "el": el, "x": x, "y": y, "w": w, "size": size,
                             "color": color, "view": tv, "css": None, "line_h": line_h}
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_text_key)
        tv.add_controller(key)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *a: GLib.idle_add(self._maybe_commit_text, tv))
        tv.add_controller(focus)
        self._style_text_editor()
        self._place_text_editor()
        self.queue_draw()
        tv.grab_focus()

    def _style_text_editor(self):
        te = self._text_editor
        if not te:
            return
        css = Gtk.CssProvider()
        r, g, b, a = te["color"]
        css.load_from_string(".wn-text-editor, .wn-text-editor text { background: rgba(255,255,255,0.0);"
                             " font-family: Sans; font-size: %.2fpx; color: rgba(%d,%d,%d,%.2f); padding: 0; }"
                             % (te["size"] * self.zoom, r * 255, g * 255, b * 255, a))
        ctx = te["view"].get_style_context()
        if te["css"]:
            ctx.remove_provider(te["css"])
        ctx.add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        te["css"] = css
        te["zoom"] = self.zoom
        if te.get("line_h"):
            # Match the ruled lines while typing: fixed pitch, baseline just above each line.
            tv = te["view"]
            fd = Pango.FontDescription.from_string("Sans")
            fd.set_absolute_size(te["size"] * self.zoom * Pango.SCALE)
            m = tv.get_pango_context().get_metrics(fd, None)
            asc = m.get_ascent() / Pango.SCALE
            nat = asc + m.get_descent() / Pango.SCALE
            pitch = te["line_h"] * self.zoom
            above = max(0, round(pitch * (1 - render.TEXT_BASELINE) - asc))
            tv.set_pixels_above_lines(above)
            tv.set_pixels_below_lines(max(0, round(pitch - nat - above)))
            tv.set_pixels_inside_wrap(max(0, round(pitch - nat)))

    def _place_text_editor(self):
        te = self._text_editor
        if not te:
            return
        if te.get("zoom") != self.zoom:
            self._style_text_editor()
        i = self.page_index(te["page"])
        if i < 0:
            return
        sx, sy = self.to_screen(i, te["x"], te["y"])
        w = max(40, int(te["w"] * self.zoom))
        tv = te["view"]
        _, nat, _, _ = tv.measure(Gtk.Orientation.VERTICAL, w)
        h = max(nat, int((te.get("line_h") or te["size"] * 1.5) * self.zoom))
        te["rect"] = (int(sx), int(sy), w, h)
        self.queue_allocate()
        self.queue_draw()

    def _on_text_key(self, ctl, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self.commit_text()
            return True
        GLib.idle_add(self._place_text_editor)
        return False

    def _maybe_commit_text(self, tv):
        te = self._text_editor
        if te and te["view"] is tv and not tv.has_focus():
            self.commit_text()
        return False

    def commit_text(self):
        te = self._text_editor
        if not te:
            return
        self._text_editor = None
        buf = te["view"].get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).rstrip()
        te["view"].unparent()
        page, el = te["page"], te["el"]
        if el is None and text:
            self.add_elements(page, [Text(text, te["x"], te["y"], te["w"], te["size"], te["color"],
                                          line_h=te.get("line_h", 0.0))])
        elif el is not None:
            if not text:
                self.replace_elements(page, {el.id: None})
            elif text != el.text:
                self.replace_elements(page, {el.id: el.with_text(text)})
            else:
                self.invalidate(page)
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        return 100, 400, -1, -1

    def do_unroot(self):
        if self._text_editor:
            self.commit_text()
        Gtk.Widget.do_unroot(self)


def _stroke_hit(el, samples, r):
    pts = el.points
    for sx, sy in samples:
        if len(pts) == 1:
            if math.hypot(pts[0][0] - sx, pts[0][1] - sy) <= r:
                return True
        for k in range(len(pts) - 1):
            a, b = pts[k], pts[k + 1]
            if abs(a[0] - sx) > r + 50 and abs(b[0] - sx) > r + 50:
                continue
            if dist_point_segment(sx, sy, a[0], a[1], b[0], b[1]) <= r:
                return True
    return False


def _split_stroke(el, samples, r):
    """Precise eraser: return replacement pieces, or None if untouched."""
    keep = []
    touched = False
    for p in el.points:
        hit = any((p[0] - sx) ** 2 + (p[1] - sy) ** 2 <= r * r for sx, sy in samples)
        keep.append(not hit)
        touched |= hit
    if not touched:
        return None
    pieces = []
    run = []
    for p, k in zip(el.points, keep):
        if k:
            run.append(p)
        elif run:
            pieces.append(run)
            run = []
    if run:
        pieces.append(run)
    return [Stroke(r_, el.color, el.width, el.tool, el.style) for r_ in pieces if len(r_) >= 2]
