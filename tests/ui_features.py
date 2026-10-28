# Fullscreen, text on ruled lines, size presets, no accidental closing.  Run via tests/harness.py.
from gi.repository import Adw
from gi.repository import Gdk
from wellnotes import fileformat, render

LIB = os.environ["WELLNOTES_LIBRARY"]
W = lambda: None  # noqa: E731


def s_new():
    Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
    doc = fileformat.new_notebook("Linien", "a4", "lined", "yellow", "#e07a5f")
    p = os.path.join(LIB, "Linien.goodnotes")
    doc.path = p
    fileformat.save(doc, p)
    app.window.open_document(p)


def s_gestures():
    ed = app.window.editors[0]
    check(not ed.get_can_pop(), "notebook cannot be popped by swipe/escape gestures")


def s_text():
    ed = app.window.editors[0]
    c = ed.canvas
    c.set_zoom(1.0, 0, 0)
    c.vadj.set_value(0)
    ed._select_tool("text")
    page = c.doc.pages[0]
    step, y0, margin = render.paper_lines(page)
    for k, txt in enumerate(["Das ist getippter Text auf den Linien des Blatts.",
                             "Zweiter Absatz – mit einem langen Satz, der automatisch umbricht und trotzdem "
                             "auf der nächsten Linie weiterläuft."]):
        _, sy = c.to_screen(0, 0, y0 + step * (2 + k * 2) - step * 0.3)
        sx, _ = c.to_screen(0, 200, 0)
        drag(c, [(sx, sy), (sx, sy)])
        c._text_editor["view"].get_buffer().set_text(txt)
        c.commit_text()
    t = [e for e in page.elements if e.kind == "text"]
    check(len(t) == 2 and all(abs(e.line_h - step) < 1e-6 for e in t), "text boxes use the line spacing")
    check(all(abs(((e.y - y0) / step) - round((e.y - y0) / step)) < 1e-6 for e in t), "text rows snap to lines")


def s_sizes():
    ed = app.window.editors[0]
    ed._select_tool("pen")
    sc = ed.options.get_visible_child().get_last_child()
    check(type(sc).__name__ == "SizeControl", "pen has a size control")
    sc.dots[2].set_active(True)
    sc.spin.set_value(9.5)
    check(abs(ed.tools.pen_width - 9.5) < 1e-6 and abs(sc.presets[2] - 9.5) < 1e-6, "slider edits active preset")
    sc.dots[0].set_active(True)
    check(abs(ed.tools.pen_width - sc.presets[0]) < 1e-6, "preset switch")


def s_fullscreen():
    ed = app.window.editors[0]
    ed.toggle_fullscreen()
    ed.on_fullscreen_changed(True)  # Broadway does not report fullscreen state changes
    check(not ed.header.get_visible() and ed.fs_start.get_visible(), "fullscreen hides header, shows controls")


def s_unfs():
    ed = app.window.editors[0]
    ed._select_tool("text")
    ed.toggle_fullscreen()
    ed.on_fullscreen_changed(False)
    check(ed.header.get_visible(), "fullscreen off")


class _Dev:
    def get_source(self):
        return Gdk.InputSource.TOUCHSCREEN


class TouchEvent:
    def get_device(self):
        return _Dev()

    def get_device_tool(self):
        return None

    def get_axis(self, axis):
        return (False, 0.0)

    def get_modifier_state(self):
        return 0

    def get_event_type(self):
        return Gdk.EventType.TOUCH_UPDATE


def s_touch():
    import time
    ed = app.window.editors[0]
    c = ed.canvas
    c.set_zoom(0.6, 0, 0)
    c.vadj.set_value(0)
    n = len(c.doc.pages[0].elements)
    g = Gesture(event=TouchEvent())
    c._on_drag_begin(g, 400, 600)
    for k in range(1, 8):
        time.sleep(0.01)
        c._on_drag_update(g, 0, -k * 25)
    c._on_drag_end(g, 0, -175)
    check(len(c.doc.pages[0].elements) == n, "finger pans instead of drawing")
    check(c.vadj.get_value() > 150, "finger pan scrolls")
    check(c._kinetic is not None, "kinetic scrolling starts after a flick")
    c._stop_kinetic()


def s_pinch():
    ed = app.window.editors[0]
    c = ed.canvas

    class Pinch:
        center = (500, 400)

        def get_bounding_box_center(self):
            return (True,) + self.center

    g = Pinch()
    c._on_zoom_begin(g, None)
    ox, oy = c._origin()
    anchor = c._pinch_anchor
    g.center = (560, 300)  # fingers move while spreading
    c._on_zoom_scale(g, 3.0)
    ox, oy = c._origin()
    sx, sy = anchor[0] * c.zoom + ox, anchor[1] * c.zoom + oy
    wide = c._content[0] * c.zoom >= c.get_width()
    print("pinch: anchor now at", round(sx), round(sy), "content wider than view:", wide)
    # Horizontally the page is centred while it is narrower than the view.
    check(abs(sy - 300) < 2 and (abs(sx - 560) < 2 or not wide), "content follows the fingers while pinching")
    c._on_zoom_end(g, None)


steps += [s_new, W, s_touch, s_pinch, s_gestures, s_text, W, W, lambda: shot("text-lines"), s_sizes, s_fullscreen, W, W,
          lambda: shot("fullscreen"), s_unfs, W]
