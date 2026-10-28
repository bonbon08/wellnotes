"""Turn a hand-drawn stroke into a clean shape (line, triangle, rectangle, polygon, ellipse)."""

import math


def _length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1))


def _dp(pts, eps):
    """Douglas-Peucker simplification."""
    if len(pts) < 3:
        return list(pts)
    ax, ay = pts[0]
    bx, by = pts[-1]
    dx, dy = bx - ax, by - ay
    ln = math.hypot(dx, dy)
    best, idx = -1.0, 0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if ln == 0:
            d = math.hypot(px - ax, py - ay)
        else:
            d = abs(dy * px - dx * py + bx * ay - by * ax) / ln
        if d > best:
            best, idx = d, i
    if best > eps:
        return _dp(pts[:idx + 1], eps)[:-1] + _dp(pts[idx:], eps)
    return [pts[0], pts[-1]]


def densify(corners, step=3.0, closed=False):
    pts = list(corners) + ([corners[0]] if closed else [])
    out = []
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for k in range(n):
            t = k / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    out.append(pts[-1])
    return out


def _angle(a, b, c):
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2) or 1
    return math.degrees(math.acos(max(-1, min(1, (v1[0] * v2[0] + v1[1] * v2[1]) / n))))


def recognize(points):
    """Return a list of (x, y) for the recognised shape, or None."""
    pts = [(p[0], p[1]) for p in points]
    if len(pts) < 3:
        return None
    L = _length(pts)
    if L < 10:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1
    gap = math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])

    if gap > 0.25 * L:
        # Open path: straight line?
        chord = gap
        ax, ay = pts[0]
        bx, by = pts[-1]
        dev = max(abs((by - ay) * px - (bx - ax) * py + bx * ay - by * ax) / chord for px, py in pts)
        if dev < max(6.0, 0.08 * chord):
            return densify([pts[0], pts[-1]])
        simp = _dp(pts, 0.08 * diag)
        if len(simp) <= 4:
            return densify(simp)
        return None

    # Closed path.
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    simp = _dp(pts, 0.07 * diag)
    if len(simp) > 2 and math.hypot(simp[-1][0] - simp[0][0], simp[-1][1] - simp[0][1]) < 0.2 * diag:
        simp = simp[:-1]
    # Merge corners that are too close.
    merged = []
    for p in simp:
        if not merged or math.hypot(p[0] - merged[-1][0], p[1] - merged[-1][1]) > 0.12 * diag:
            merged.append(p)
    if len(merged) > 3 and math.hypot(merged[-1][0] - merged[0][0], merged[-1][1] - merged[0][1]) < 0.12 * diag:
        merged.pop()

    radii = [math.hypot(x - cx, y - cy) for x, y in pts]
    mean_r = sum(radii) / len(radii)
    var = math.sqrt(sum((r - mean_r) ** 2 for r in radii) / len(radii)) / (mean_r or 1)

    sharp = 0
    for i in range(len(merged)):
        if _angle(merged[i - 1], merged[i], merged[(i + 1) % len(merged)]) < 135:
            sharp += 1

    if len(merged) == 3:
        return densify(merged, closed=True)
    if len(merged) == 4 and sharp >= 3:
        angles = [_angle(merged[i - 1], merged[i], merged[(i + 1) % 4]) for i in range(4)]
        if all(abs(a - 90) < 25 for a in angles):
            # Rectangle: if roughly axis aligned, snap to bounding box.
            e = [math.degrees(math.atan2(merged[(i + 1) % 4][1] - merged[i][1],
                                         merged[(i + 1) % 4][0] - merged[i][0])) % 90 for i in range(4)]
            if all(min(a, 90 - a) < 12 for a in e):
                x0, x1 = min(p[0] for p in merged), max(p[0] for p in merged)
                y0, y1 = min(p[1] for p in merged), max(p[1] for p in merged)
                return densify([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True)
        return densify(merged, closed=True)
    if var < 0.3 or sharp < 3:
        return _ellipse(pts, cx, cy)
    if 5 <= len(merged) <= 8:
        return densify(merged, closed=True)
    return _ellipse(pts, cx, cy)


def _ellipse(pts, cx, cy):
    sxx = sum((x - cx) ** 2 for x, _ in pts) / len(pts)
    syy = sum((y - cy) ** 2 for _, y in pts) / len(pts)
    sxy = sum((x - cx) * (y - cy) for x, y in pts) / len(pts)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    ct, st = math.cos(theta), math.sin(theta)
    us = [(x - cx) * ct + (y - cy) * st for x, y in pts]
    vs = [-(x - cx) * st + (y - cy) * ct for x, y in pts]
    a = (max(us) - min(us)) / 2
    b = (max(vs) - min(vs)) / 2
    ou = (max(us) + min(us)) / 2
    ov = (max(vs) + min(vs)) / 2
    ccx = cx + ou * ct - ov * st
    ccy = cy + ou * st + ov * ct
    if min(a, b) > 0.85 * max(a, b):  # snap to circle
        a = b = (a + b) / 2
    n = 96
    out = []
    for k in range(n + 1):
        t = 2 * math.pi * k / n
        u, v = a * math.cos(t), b * math.sin(t)
        out.append((ccx + u * ct - v * st, ccy + u * st + v * ct))
    return out
