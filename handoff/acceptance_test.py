"""
ACCEPTANCE TEST for the variable-width ink engine.

This file is the CONTRACT. Port the engine into your stack, then port this
test and make every assertion pass. The expected numbers are exact values
produced by the recovered implementation (Drawboard PDF 6.91.6.0
drawboard-ink-lib.dll) and by exact geometry, not by eyeballing.

Run:  python acceptance_test.py
"""

import math
import sys

try:
    from ink_engine_reference import (
        Point, segment_quad, segment_path, stroke_polygons, arc_cap,
        smooth_polyline, flatten_cubic, simplify, path_to_svg_data,
        polygons_to_svg, MOVE, LINE, BEZIER,
    )
except ImportError:
    from ink_engine import (
        Point, segment_quad, segment_path, stroke_polygons, arc_cap,
        smooth_polyline, flatten_cubic, simplify, path_to_svg_data,
        polygons_to_svg, MOVE, LINE, BEZIER,
    )

FAILED = []


def check(name, got, want, tol=1e-9):
    ok = False
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAILED.append(f"{name}: got {got!r} want {want!r}")
    print(("  PASS  " if ok else "  FAIL  ") + f"{name}: {got!r}" + ("" if ok else f"  (want {want!r})"))


def poly_area(poly):
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i].x, poly[i].y
        x2, y2 = poly[(i + 1) % n].x, poly[(i + 1) % n].y
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def dist_point_line(p, a, b):
    ax, ay, bx, by = a.x, a.y, b.x, b.y
    L = math.hypot(bx - ax, by - ay)
    return abs((p.x - ax) * (by - ay) - (p.y - ay) * (bx - ax)) / L


print("== T1  constant width -> rectangle of exactly 2w thickness ==")
a, b = Point(0, 0, 5.0), Point(100, 0, 5.0)
p0, p1, p2, p3 = segment_quad(a, b)
check("p0.x", p0.x, 0.0);   check("p0.y", p0.y, 5.0)
check("p1.x", p1.x, 100.0); check("p1.y", p1.y, 5.0)
check("p2.x", p2.x, 0.0);   check("p2.y", p2.y, -5.0)
check("p3.x", p3.x, 100.0); check("p3.y", p3.y, -5.0)
check("area == 2*w*L", poly_area([p0, p2, p3, p1]), 1000.0)

print("\n== T2  every corner sits exactly w from the centreline ==")
for nm, p in (("p0", p0), ("p1", p1), ("p2", p2), ("p3", p3)):
    check(f"dist({nm})", dist_point_line(p, a, b), 5.0)

print("\n== T3  tapered span: tangency still exact ==")
a2, b2 = Point(0, 0, 8.0), Point(60, 20, 2.0)
q = segment_quad(a2, b2)
L = math.hypot(60, 20)
for nm, p, r in (("p0", q[0], 8.0), ("p1", q[1], 2.0)):
    check(f"|{nm}-end| == w", math.hypot(p.x - (a2 if nm == "p0" else b2).x,
                                        p.y - (a2 if nm == "p0" else b2).y), r)
for i in (0, 2):
    n = ((q[i].x - a2.x) / a2.w, (q[i].y - a2.y) / a2.w)
    check("u.n == (w0-w1)/L", (60 * n[0] + 20 * n[1]) / L, (8.0 - 2.0) / L, 1e-12)

print("\n== T4  native rejection rule: dw^2 >= L^2 drops the span ==")
check("dw=10,L=5 -> None", segment_quad(Point(0, 0, 10), Point(3, 4, 0)), None)
check("dw=3,L=5 -> quad", segment_quad(Point(0, 0, 4), Point(3, 4, 1)) is not None, True)

print("\n== T5  quad assembly order: A+n+ , A-n+ , B-n+ , B+n+ (closed) ==")
sp = segment_path(Point(0, 0, 2), Point(50, 0, 2))
check("seg type 0 is MOVE", sp.segments[0].type, MOVE)
check("segment count", len(sp.segments), 4)
check("path closed", sp.closed, True)
pts_seq = [sp.segments[0].pts[0]] + [s.pts[1] for s in sp.segments[1:]]
orders = [(round(p.x, 6), round(p.y, 6)) for p in pts_seq]
check("corner order", orders, [(0.0, 2.0), (0.0, -2.0), (50.0, -2.0), (50.0, 2.0)])

print("\n== T6  one quad per span ==")
samples = [Point(0, 0, 1), Point(10, 0, 2), Point(20, 5, 3), Point(30, 5, 4)]
check("span count", len(stroke_polygons(samples)), 3)
check("round caps add 2 discs", len(stroke_polygons(samples, round_caps=True)), 5)

print("\n== T7  simplify: collinear points collapse, default tol 0.2 ==")
line = [Point(i, 0, 2) for i in range(11)]
check("collinear -> endpoints", len(simplify(line)), 2)
wiggle = [Point(0, 0, 1), Point(5, 0.1, 1), Point(10, 0, 1)]
check("0.1 deviation kept (<=0.2 -> dropped)", len(simplify(wiggle)), 2)
wiggle2 = [Point(0, 0, 1), Point(5, 1.0, 1), Point(10, 0, 1)]
check("1.0 deviation kept", len(simplify(wiggle2)), 3)

print("\n== T8  smoothing: 40 deg corner split, gentle curve -> cubics ==")
gentle = [Point(x, 20 * math.sin(x / 40.0), 2) for x in range(0, 200, 8)]
segs = smooth_polyline(gentle)
nbez = sum(1 for s in segs if s.type == BEZIER)
check("gentle curve produces beziers", nbez > 5, True)
corner = [Point(0, 0, 2), Point(50, 0, 2), Point(50, 50, 2), Point(100, 50, 2)]
segs2 = smooth_polyline(corner)
nmove = sum(1 for s in segs2 if s.type == MOVE)
check("90 deg corner preserved as a split", nmove >= 2, True)

print("\n== T9  bezier flattening: endpoints preserved, no NaN ==")
p0_, p1_, p2_, p3_ = Point(0, 0, 0), Point(0, 100, 0), Point(100, 100, 0), Point(100, 0, 0)
flat = flatten_cubic(p0_, p1_, p2_, p3_, tol=0.01)
check("ends at the last control point", (flat[-1].x, flat[-1].y), (100.0, 0.0))
check("no NaN", all(not math.isnan(p.x) and not math.isnan(p.y) for p in flat), True)

print("\n== T10 SVG serialisation grammar ==")
d = path_to_svg_data([segment_path(Point(0, 0, 2), Point(10, 0, 2)).segments[0],
                      segment_path(Point(0, 0, 2), Point(10, 0, 2)).segments[1]])
check("starts with 'M '", d.startswith("M "), True)
check("contains 'L '", "L " in d, True)
check("ends with 'Z '", d.endswith("Z "), True)
check("coord separator ', '", ", " in d, True)

print("\n== T11 full stroke is a single NonZero path, no NaN ==")
pts = [Point(60, 200, 1), Point(140, 120, 7), Point(240, 190, 14), Point(340, 120, 6),
       Point(430, 200, 1.5), Point(520, 140, 10)]
polys = stroke_polygons(pts, round_caps=True)
svg = polygons_to_svg(polys)
check("svg has nonzero rule", 'fill-rule="nonzero"' in svg, True)
check("no nan in svg", "nan" not in svg.lower(), True)
check("polygon count", len(polys), len(pts) - 1 + 2)

print()
if FAILED:
    print(f"{len(FAILED)} FAILURE(S):")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("ALL ACCEPTANCE CHECKS PASSED")
