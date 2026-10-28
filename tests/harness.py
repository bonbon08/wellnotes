"""Headless UI test runner (GTK Broadway backend).

    gtk4-broadwayd :7 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 WELLNOTES_LIBRARY=/tmp/wn-test python3 tests/harness.py tests/ui_tools.py

A test script defines step functions and appends them to ``steps``; one step
runs every 700 ms.  ``shot(name)`` saves a PNG of the window to $WELLNOTES_SHOTS
(default /tmp/wn-shots).  Helpers ``Gesture`` and ``drag`` drive the canvas.
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

from wellnotes import settings  # noqa: E402
from wellnotes.app import Application  # noqa: E402

SHOTS = os.environ.get("WELLNOTES_SHOTS", "/tmp/wn-shots")
os.makedirs(SHOTS, exist_ok=True)
# Never touch the user's real settings.
settings.PATH = os.path.join(os.environ.get("WELLNOTES_LIBRARY", "/tmp/wn-test"), "..", "wn-test-settings.json")
settings._data = None
steps = []
failures = []


def shot(name):
    win = app.window
    w, h = win.get_width(), win.get_height()
    snap = Gtk.Snapshot()
    c = Gdk.RGBA()
    c.parse("#242424" if Adw.StyleManager.get_default().get_dark() else "#fafafa")
    snap.append_color(c, Graphene.Rect().init(0, 0, w, h))
    child = win.get_first_child()
    while child is not None:
        win.snapshot_child(child, snap)
        child = child.get_next_sibling()
    node = snap.to_node()
    if node is None:
        print("shot skipped (no allocation yet):", name)
        return
    win.get_native().get_renderer().render_texture(node, None).save_to_png(os.path.join(SHOTS, name + ".png"))
    print("shot", name, flush=True)


class Gesture:
    """Stand-in for Gtk.GestureDrag (mouse-like input unless ``event`` is set)."""

    def __init__(self, button=1, event=None):
        self.button, self.event = button, event

    def get_current_event(self):
        return self.event

    def get_current_button(self):
        return self.button

    def set_state(self, state):
        pass


def drag(canvas, pts, gesture=None):
    g = gesture or Gesture()
    x0, y0 = pts[0]
    canvas._on_drag_begin(g, x0, y0)
    for x, y in pts[1:]:
        canvas._on_drag_update(g, x - x0, y - y0)
    canvas._on_drag_end(g, pts[-1][0] - x0, pts[-1][1] - y0)


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        failures.append(msg)


app = Application()
exec(open(sys.argv[1]).read())


def runner():
    if steps:
        f = steps.pop(0)
        try:
            f()
        except Exception:
            traceback.print_exc()
            failures.append(getattr(f, "__name__", "step"))
        return True
    print("FAILURES:", failures if failures else "none", flush=True)
    app.quit()
    return False


def start(*a):
    GLib.timeout_add(700, runner)


app.connect("activate", lambda *a: GLib.idle_add(start))
app.run([])
sys.exit(1 if failures else 0)
