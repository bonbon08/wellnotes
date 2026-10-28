# Drives every canvas tool through the real gesture handlers.  Run via tests/harness.py.
import math
import time

from wellnotes import fileformat, goodnotes

LIB = os.environ["WELLNOTES_LIBRARY"]
W = lambda: None  # noqa: E731


def s_new():
    doc = fileformat.new_notebook("Test", "a4", "lined", "white", "#e07a5f")
    p = os.path.join(LIB, "Test.goodnotes")
    doc.path = p
    fileformat.save(doc, p)
    app.window.open_document(p)


def s_tools():
    ed = app.window.editors[0]
    c = ed.canvas
    d = c.doc
    els = lambda: d.pages[0].elements  # noqa: E731
    ed._select_tool("pen")
    drag(c, [(200 + i * 4, 200 + 20 * math.sin(i / 4)) for i in range(60)])
    check(len(els()) == 1 and len(els()[0].points) > 30, "pen stroke")
    check(c._ink_layer is None, "live ink layer released")
    ed._select_tool("shape")
    drag(c, [(500 + 70 * math.cos(t / 8), 300 + 50 * math.sin(t / 8)) for t in range(52)])
    drag(c, [(150, 400), (250, 402), (350, 399), (450, 401)])
    drag(c, [(600, 420), (700, 420), (700, 500), (600, 500), (600, 421)])
    check(len(els()) == 4 and all(e.style == "ball" for e in els()[1:]), "shapes")
    ed._select_tool("highlighter")
    drag(c, [(150 + i * 5, 230) for i in range(50)])
    check(els()[-1].tool == "highlighter", "highlighter")
    ed._select_tool("text")
    drag(c, [(200, 600), (200, 600)])
    te = c._text_editor
    check(bool(te), "text editor opens")
    te["view"].get_buffer().set_text("Hallo WellNotes!\nZweite Zeile")
    c.commit_text()
    check(els()[-1].kind == "text", "text box committed")
    ed._select_tool("lasso")
    drag(c, [(140, 170), (480, 170), (480, 260), (140, 260), (140, 171)])
    check(c.selection is not None and len(c.selection["ids"]) == 2, "lasso selects 2")
    x0, y0, x1, y1 = c._selection_screen_box()
    before = c.selected_elements()[0].bbox()
    drag(c, [((x0 + x1) / 2, (y0 + y1) / 2), ((x0 + x1) / 2 + 50, (y0 + y1) / 2 + 300)])
    after = c.selected_elements()[0].bbox()
    check(after[1] > before[1] + 100, "selection moved")
    x0, y0, x1, y1 = c._selection_screen_box()
    drag(c, [(x1 + 4, y1 + 4), (x1 + 60, y1 + 40)])
    check(c.selected_elements()[0].bbox()[2] > after[2], "selection scaled")
    n = len(els())
    c.duplicate_selection()
    check(len(els()) == n + 2, "duplicate")
    c.delete_selection()
    check(len(els()) == n, "delete")
    ed._select_tool("eraser")
    c.tools.eraser_precise = True
    n = len(els())
    drag(c, [(330, 150 + i * 10) for i in range(40)])
    check(len(els()) > n, "precise eraser splits strokes")
    c.tools.eraser_precise = False
    n = len(els())
    drag(c, [(150 + i * 20, 400) for i in range(30)])
    check(len(els()) < n, "stroke eraser removes")
    n = len(els())
    ed.undo()
    check(len(els()) > n, "undo")
    ed.redo()
    check(len(els()) == n, "redo")
    ed.add_page()
    ed.add_page()
    ed.duplicate_page(0)
    ed.move_page(0, 1)
    ed.delete_page(3)
    check(len(d.pages) == 3, "page operations")
    ed.undo()
    check(len(d.pages) == 4, "undo page delete")


def s_perf():
    ed = app.window.editors[0]
    c = ed.canvas
    ed._select_tool("pen")
    c.scroll_to_page(0)
    class PenEvent:  # a pressure-sensitive pen sample
        p = 0.5

        def get_axis(self, axis):
            return (True, self.p) if axis == Gdk.AxisUse.PRESSURE else (False, 0.0)

        def get_device(self):
            return None

        def get_device_tool(self):
            return None

        def get_modifier_state(self):
            return 0

        def get_event_type(self):
            return Gdk.EventType.BUTTON_PRESS

    ev = PenEvent()
    g = Gesture(event=ev)
    renderer = app.window.get_native().get_renderer()
    pts = [(150 + i * 1.5, 700 + 30 * math.sin(i / 6)) for i in range(400)]
    t = time.perf_counter()
    c._on_drag_begin(g, *pts[0])
    snap_t = 0.0
    for k, (x, y) in enumerate(pts[1:]):
        ev.p = 0.3 + 0.6 * abs(math.sin(k / 20))
        c._on_drag_update(g, x - pts[0][0], y - pts[0][1])
        t1 = time.perf_counter()
        s = Gtk.Snapshot()
        c.do_snapshot(s)
        node = s.to_node()
        if node is not None and os.environ.get("WN_PERF_RENDER", "1") == "1":
            renderer.render_texture(node, None)  # what the compositor does each frame
        snap_t += time.perf_counter() - t1
    c._on_drag_end(g, pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
    total = time.perf_counter() - t
    print("perf: %d updates, %.2f ms/update incl. frame (frame render %.2f ms)"
          % (len(pts), total / len(pts) * 1000, snap_t / len(pts) * 1000), flush=True)
    check(total / len(pts) < 0.02, "live ink under 20 ms per update")


def s_close():
    ed = app.window.editors[0]
    ed.canvas.scroll_to_page(0)
    app.window.nav.pop()


def s_check():
    p = os.path.join(LIB, "Test.goodnotes")
    d = fileformat.load(p)
    check(len(d.pages) == 4, "reload: 4 pages")
    g = goodnotes.read_goodnotes(p)
    check(len(g.pages) == 4, "GoodNotes view: 4 pages")


steps += [s_new, W, s_tools, W, lambda: shot("tools"), s_perf, W, lambda: shot("perf"), s_close, W, W, s_check]
