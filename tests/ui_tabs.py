# Tabs and split view with several notebooks.  Run via tests/harness.py.
from wellnotes import fileformat

LIB = os.environ["WELLNOTES_LIBRARY"]
W = lambda: None  # noqa: E731
win = ws = None


def s_init():
    global win, ws
    win = app.window
    ws = win.workspace


def make(name, paper):
    doc = fileformat.new_notebook(name, "a4", paper, "white", "#e07a5f")
    p = os.path.join(LIB, name + ".goodnotes")
    doc.path = p
    fileformat.save(doc, p)
    return p


def s_open():
    for name, paper in [("Mathe", "grid"), ("Physik", "lined"), ("Chemie", "dotted")]:
        make(name, paper)
    win.open_document(os.path.join(LIB, "Mathe.goodnotes"))
    check(win.nav.get_visible_page() is ws, "first notebook opens the workspace")


def s_library_keeps_tabs():
    win.show_library()
    check(win.nav.get_visible_page() is win.library, "back shows the library")
    check(len(win.editors) == 1, "notebook stays open while the library is shown")
    check(win.library.open_button.get_visible() and "1" in win.library.open_content.get_label(),
          "library offers to return to open notebooks")
    win.open_document(os.path.join(LIB, "Physik.goodnotes"))
    check(win.nav.get_visible_page() is ws and ws.panes[0].view.get_n_pages() == 2, "second notebook opens as tab")
    check(ws.current().doc.title == "Physik", "new tab is selected")
    win.open_document(os.path.join(LIB, "Mathe.goodnotes"))
    check(len(win.editors) == 2 and ws.current().doc.title == "Mathe", "reopening selects the existing tab")


def s_split():
    global width0
    width0 = win.get_width()
    ed = ws.current()
    ed.actions.activate_action("split", None)
    check(ws.split and len(ws.panes) == 2, "split view on")
    left, right = ws.panes
    check([e.doc.title for e in right.editors()] == ["Mathe"] and
          [e.doc.title for e in left.editors()] == ["Physik"], "current notebook moved to the right pane")
    check(ws.active is right, "right pane active")
    check(not left.editors()[0].header.get_show_end_title_buttons() and
          right.editors()[0].header.get_show_end_title_buttons(), "window buttons only on the right")


def s_split_width():
    check(win.get_width() <= width0 + 1, "split view fits the window (%d -> %d px)" % (width0, win.get_width()))


def s_draw_both():
    left, right = ws.panes
    for pane, y in ((left, 300), (right, 340)):
        ed = pane.current()
        ed._select_tool("pen")
        c = ed.canvas
        w = c.get_width()
        check(w > 100, "%s canvas has room (%d px)" % (ed.doc.title, w))
        pts = [(60 + i * 3, y + 10 * (i % 2)) for i in range(40)]
        drag(c, pts)
        idx = c.to_page(*pts[0])[0]
        check(len(c.doc.pages[idx].elements) == 1, "drawing in %s" % ed.doc.title)


def s_open_into_right():
    left, right = ws.panes
    win.show_library(right)
    win.open_document(os.path.join(LIB, "Chemie.goodnotes"))
    check([e.doc.title for e in right.editors()] == ["Mathe", "Chemie"], "library opens into the chosen pane")
    check(right.bar.get_visible() and not right.bar.get_autohide(), "tab bars always shown in split view")


def s_close_right():
    left, right = ws.panes
    for ed in list(right.editors()):
        ed.actions.activate_action("close-tab", None)


def s_after_close():
    check(not ws.split, "closing the last notebook of a pane ends the split view")
    check([e.doc.title for e in win.editors] == ["Physik"], "other notebooks stay open")
    d = fileformat.load(os.path.join(LIB, "Mathe.goodnotes"))
    check(sum(len(p.elements) for p in d.pages) == 1, "closed notebook was saved")


def s_split_single():
    ws.current().actions.activate_action("split", None)
    left, right = ws.panes
    check(right.stack.get_visible_child_name() == "empty", "split with one notebook offers to open another")
    ws.toggle_split()
    check(not ws.split and len(win.editors) == 1, "split off")


def s_unsplit_both_sides():
    """Turning split view off with notebooks on both sides moves the right ones over (this hung)."""
    win.open_document(os.path.join(LIB, "Mathe.goodnotes"))
    ws.current().actions.activate_action("split", None)
    check([len(p.editors()) for p in ws.panes] == [1, 1], "one notebook per side")
    ws.toggle_split()
    check(not ws.split and len(ws.panes[0].editors()) == 2, "split off keeps both notebooks")
    check(ws.current().doc.title == "Mathe", "the active notebook stays selected")


def s_close_left():
    """Closing the left notebook of a split view moves the right one over (this hung)."""
    ws.current().actions.activate_action("split", None)
    left, right = ws.panes
    left.view.close_page(left.view.get_nth_page(0))


def s_after_close_left():
    check(not ws.split and [e.doc.title for e in win.editors] == ["Mathe"], "closing the left notebook ends split view")
    ed = ws.current()
    drag(ed.canvas, [(200 + i * 4, 500) for i in range(30)])
    check(sum(len(p.elements) for p in ed.doc.pages) == 2, "remaining notebook still takes input")
    win.open_document(os.path.join(LIB, "Physik.goodnotes"))


def s_delete_open():
    win.show_library()
    win.close_documents(os.path.join(LIB, "Mathe.goodnotes"))
    p = os.path.join(LIB, "Physik.goodnotes")
    win.close_documents(p)
    check(not win.editors, "deleting/moving an open notebook closes its tab first")
    d = fileformat.load(p)
    check(sum(len(pg.elements) for pg in d.pages) == 1, "its drawing was saved")


steps += [s_init, s_open, W, s_library_keeps_tabs, W, W, s_split, W, W, s_split_width, lambda: shot("split"), s_draw_both,
          s_open_into_right, W, lambda: shot("split-tabs"), W, W, W, lambda: shot("split-later"), s_close_right, W, s_after_close, s_split_single, W,
          s_unsplit_both_sides, W, s_close_left, W, W, s_after_close_left, W, s_delete_open, W]
