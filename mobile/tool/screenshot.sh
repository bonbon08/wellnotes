#!/bin/bash
# Screenshot of the running emulator/device, scaled to 1024 px wide.
# usage: tool/screenshot.sh out.png [adb-serial]
OUT=${1:-shot.png}
SERIAL=${2:-emulator-5554}
RAW=$(mktemp --suffix=.png)
adb -s "$SERIAL" exec-out screencap -p > "$RAW"
python3 - "$RAW" "$OUT" <<'PY'
import sys, cairo
s = cairo.ImageSurface.create_from_png(sys.argv[1])
k = 1024 / s.get_width()
o = cairo.ImageSurface(cairo.FORMAT_RGB24, 1024, int(s.get_height() * k))
c = cairo.Context(o); c.scale(k, k); c.set_source_surface(s, 0, 0); c.paint()
o.write_to_png(sys.argv[2])
PY
rm -f "$RAW"
