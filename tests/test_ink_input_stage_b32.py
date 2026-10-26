"""B.3.2 positional input-stage experiment (handler-level, Qt-free).

Compares baseline (identity) vs Candidate A (near-duplicate collapse)
vs Candidate B (causal speed-adaptive EMA) on a deterministic seeded
corpus driven through PenHandler.down/move/up.

Hard gates asserted here: identity default unchanged, stage-to-builder
parity (zero mismatches), collapse endpoint exactness, tap/micro-stroke
validity, lifecycle (repeat/identical/different release, cancel,
tool-switch, desync fallback), pressure invariance, finite geometry.
Soft screening bars (jitter -30%, corner <=0.25pt, cost p95) are
measured and printed; the verdict lives in the milestone report, and a
failing candidate must NOT turn this module red (experiment outcome is
evidence, not a regression).
"""

import math
import random
import time

from annotations.ink_geometry import (
    LiveCenterline,
    LiveOutline,
    build_outline,
    pressure_width,
    smooth_centerline,
)
from annotations.interaction import (
    CollapseStage,
    InputStage,
    InteractionController,
    PenHandler,
    SpeedAdaptiveStage,
)
from core.types import PageId
from tools.models import ToolInstance, ToolType

BASE_PT = 0.5 * 72.0 / 25.4  # default width_mm mapped like _feed_live


# ---------------------------------------------------------------- corpus

def _line(n=60, x0=0.0, y0=0.0, x1=120.0, y1=0.0, jitter=0.0, seed=7):
    rng = random.Random(seed)
    pts = []
    for i in range(n):
        f = i / (n - 1)
        pts.append((x0 + (x1 - x0) * f + (rng.random() * 2 - 1) * jitter,
                    y0 + (y1 - y0) * f + (rng.random() * 2 - 1) * jitter))
    return pts


def _circle(n=60, r=50.0, jitter=0.0, seed=11):
    rng = random.Random(seed)
    pts = []
    for i in range(n):
        a = (i / (n - 1)) * math.pi / 2
        pts.append((r * math.cos(a) + (rng.random() * 2 - 1) * jitter,
                    r * math.sin(a) + (rng.random() * 2 - 1) * jitter))
    return pts


def _corner():
    leg1 = [(i * 2.0, 0.0) for i in range(31)]
    leg2 = [(60.0, i * 2.0) for i in range(1, 31)]
    return leg1 + leg2


def _acute():
    leg1 = [(i * 2.0, 0.0) for i in range(31)]
    leg2 = [(60.0 + i * 2.0 * math.cos(math.pi / 4),
             i * 2.0 * math.sin(math.pi / 4)) for i in range(1, 31)]
    return leg1 + leg2


def _reversal():
    fwd = [(i * 2.0, 0.0) for i in range(31)]
    back = [(60.0 - i * 2.0, 0.0) for i in range(1, 31)]
    return fwd + back


def _zigzag(n=40, step=3.0, amp=4.0):
    return [(i * step, amp if i % 2 else -amp) for i in range(n)]


def _with_times(pts, step_ms=8, start=1000):
    return [(x, y, start + i * step_ms) for i, (x, y) in enumerate(pts)]


def _corpus():
    sparse = [(0.0, 0.0), (25.0, 0.5), (50.0, -0.5), (75.0, 0.5),
              (100.0, 0.0)]
    dense = [(float(i), 0.5 * math.sin(i * 0.5)) for i in range(101)]
    dups = [(0.0, 0.0)] * 5 + [(i * 2.0, 0.0) for i in range(1, 20)]
    neardup = [(0.0, 0.0), (0.05, 0.0), (0.2, 0.02), (0.5, 0.0),
               (3.0, 0.0), (3.1, 0.05), (6.0, 0.0)]
    irreg = _with_times(_line(40), step_ms=8)
    irreg = [(x, y, t) for (x, y, t) in irreg]
    irreg[10] = (irreg[10][0], irreg[10][1], irreg[9][2])  # repeat
    irreg[11] = (irreg[11][0], irreg[11][1], irreg[9][2] - 5)  # backward
    irreg[20] = (irreg[20][0], irreg[20][1], irreg[20][2] + 200)  # gap
    # Slow dense walk with jitter: the collapse design target (hover
    # clusters and dense low-speed wobble the sparse corpus never hits).
    slow = [(i * 0.25, 0.0) for i in range(121)]
    rng = random.Random(21)
    dense_jitter = [(x + (rng.random() * 2 - 1) * 0.6,
                     (rng.random() * 2 - 1) * 0.6) for (x, _y) in slow]
    return {
        "h_clean": _with_times(_line(y1=0.0)),
        "v_clean": _with_times(_line(x0=0.0, y0=0.0, x1=0.0, y1=120.0)),
        "d_clean": _with_times(_line(x1=120.0, y1=80.0)),
        "jitter03": _with_times(_line(jitter=0.3)),
        "jitter06": _with_times(_line(jitter=0.6)),
        "curve_jitter": _with_times(_circle(jitter=0.6)),
        "corner90": _with_times(_corner()),
        "corner45": _with_times(_acute()),
        "reversal": _with_times(_reversal()),
        "zigzag": _with_times(_zigzag()),
        "tap": _with_times([(50.0, 50.0)]),
        "twopt": _with_times([(10.0, 10.0), (20.0, 12.0)]),
        "sparse": _with_times(sparse),
        "dense": _with_times(dense),
        "dups": _with_times(dups),
        "neardup": _with_times(neardup),
        "irreg": irreg,
        "dense_jitter": _with_times(dense_jitter),
    }


ARMS = {
    "baseline": InputStage,
    "A0.15": lambda: CollapseStage(0.15),
    "A0.30": lambda: CollapseStage(0.30),
    "A0.50": lambda: CollapseStage(0.50),
    "B_g20_g80_v05": lambda: SpeedAdaptiveStage(0.2, 0.8, 0.05),
    "B_g20_g80_v15": lambda: SpeedAdaptiveStage(0.2, 0.8, 0.15),
    "B_g20_g100_v05": lambda: SpeedAdaptiveStage(0.2, 1.0, 0.05),
    "B_g20_g100_v15": lambda: SpeedAdaptiveStage(0.2, 1.0, 0.15),
    "B_g35_g80_v05": lambda: SpeedAdaptiveStage(0.35, 0.8, 0.05),
    "B_g35_g80_v15": lambda: SpeedAdaptiveStage(0.35, 0.8, 0.15),
    "B_g35_g100_v05": lambda: SpeedAdaptiveStage(0.35, 1.0, 0.05),
    "B_g35_g100_v15": lambda: SpeedAdaptiveStage(0.35, 1.0, 0.15),
}

PARITY_CASES = ("jitter06", "curve_jitter", "corner90", "corner45",
                "reversal", "zigzag", "dense", "dups", "neardup", "irreg",
                "sparse", "twopt", "dense_jitter")


# ---------------------------------------------------------------- driving

def _pen_tool(pressure=False):
    return ToolInstance(kind=ToolType.PEN, name="P", color="#1b1e26",
                        width_mm=0.5, opacity=1.0, pressure=pressure)


def drive(factory, samples, pressure=False, pressures=None):
    """Drive press/moves through a PenHandler; release at last coord.

    The closing release repeats the last coordinate (exact dupe, so no
    append) to close the stroke without altering geometry. Returns
    (pen, stroke_done, log) where log holds per-push
    (x, y, t_clamped, final, pressure, result-or-None).
    """
    pen = PenHandler(ToolType.PEN, input_stage=factory())
    pen.configure(_pen_tool(pressure))
    page = PageId()
    n = len(samples)
    ps = list(pressures) if pressures is not None else [0.6] * n
    log = []
    orig_push = pen._stage.push

    def rec(x, y, t, *, final=False):
        r = orig_push(x, y, t, final=final)
        log.append((x, y, t, final, ps[len(log)],
                    None if r is None else (r.x, r.y)))
        return r

    pen._stage.push = rec
    x0, y0, t0 = samples[0]
    pen.down(page, x0, y0, ps[0], t0)
    for (x, y, t), p in zip(samples[1:], ps[1:]):
        pen.move(page, x, y, p, t)
    pre_outline = pen.live_outline()
    pre_center = pen.live_centerline()
    pre_fed = pen._fed
    pre_live_len = len(pen._live)
    pre_center_len = len(pen._center)
    lx, ly, lt = samples[-1]
    done = pen.up(page, lx, ly, ps[-1], lt)
    return {
        "pen": pen,
        "stroke": done["stroke_done"],
        "log": log,
        "raw": [(x, y) for (x, y, _t) in samples],
        "clamped_t": [e[2] for e in log],
        "pre_outline": pre_outline,
        "pre_center": pre_center,
        "pre_fed": pre_fed,
        "pre_live_len": pre_live_len,
        "pre_center_len": pre_center_len,
    }


def stabilize_full(factory, clamped, finals):
    """From-scratch stage run over clamped (x, y, t, pressure) rows."""
    stage = factory()
    out = []
    for i, (x, y, t, _p, fin) in enumerate(clamped):
        r = stage.push(x, y, t, final=(fin or i == len(clamped) - 1
                                       and finals))
        if r is not None:
            out.append((r.x, r.y))
    return out


def derived_of(run):
    return [r for (_x, _y, _t, _f, _p, r) in run["log"] if r is not None]


def derived_pressures(run):
    return [p for (_x, _y, _t, _f, p, r) in run["log"] if r is not None]


# ---------------------------------------------------------------- metrics

def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dev_stats(pts, segs):
    ds = [min(_seg_dist(x, y, *s) for s in segs) for (x, y) in pts]
    return max(ds), (sum(d * d for d in ds) / len(ds)) ** 0.5


def path_length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0],
                          pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


def corner_miss(pts, cx, cy):
    return min(math.hypot(x - cx, y - cy) for (x, y) in pts)


def corner_angle(pts, cx, cy, arm=5):
    i = min(range(len(pts)), key=lambda k: math.hypot(pts[k][0] - cx,
                                                     pts[k][1] - cy))
    a = pts[max(0, i - arm)]
    b = pts[min(len(pts) - 1, i + arm)]
    v1 = (a[0] - pts[i][0], a[1] - pts[i][1])
    v2 = (b[0] - pts[i][0], b[1] - pts[i][1])
    n1 = math.hypot(*v1)
    n2 = math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return 0.0
    c = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(c))


# ---------------------------------------------------------------- hard gates

def test_identity_default_preserves_current_behavior():
    run = drive(InputStage, _corpus()["jitter06"])
    assert [(p.x, p.y) for p in run["stroke"].points] == run["raw"]
    assert run["pre_fed"] == len(run["raw"])
    assert run["pre_center_len"] == len(run["raw"])
    oracle, _ = smooth_centerline(run["raw"], None)
    assert run["pre_center"] == oracle
    assert run["stroke"].pressures == (0.6,) * len(run["raw"])


def test_stage_to_builder_parity_zero_mismatches():
    corpus = _corpus()
    for name in PARITY_CASES:
        samples = corpus[name]
        for arm, factory in ARMS.items():
            # Pressure ON so both live builders are fed and comparable.
            run = drive(factory, samples, pressure=True)
            # L1: incremental derived == from-scratch per prefix.
            rows = [(x, y, t, p, f)
                    for (x, y, t, f, p, _r) in run["log"]]
            for k in range(1, len(rows) + 1):
                expect = stabilize_full(factory, rows[:k],
                                        finals=(k == len(rows)
                                                and rows[-1][4]))
                got = [r for (_x, _y, _t, _f, _p, r)
                       in run["log"][:k] if r is not None]
                assert got == expect, (name, arm, k)
            # L2: fresh builders over derived == live pre-up state.
            der = derived_of(run)
            if not der:
                continue
            lo, lc = LiveOutline(), LiveCenterline()
            for (x, y), p in zip(der, derived_pressures(run)):
                lo.append(x, y, pressure_width(p, BASE_PT, "normal"))
                lc.append(x, y)
            assert lo.outline() == run["pre_outline"], (name, arm)
            assert lc.smoothed() == run["pre_center"], (name, arm)
            assert lo.fallbacks == 0 and lc.fallbacks == 0, (name, arm)


def test_builder_oracles_hold_on_derived_sequences():
    corpus = _corpus()
    for name in ("jitter06", "corner90", "zigzag"):
        for arm in ("A0.30", "B_g35_g100_v15"):
            run = drive(ARMS[arm], corpus[name])
            der = derived_of(run)
            ws = [pressure_width(p, BASE_PT, "normal")
                  for p in derived_pressures(run)]
            sm, _sw = smooth_centerline(der, None)
            assert run["pre_center"] == sm, (name, arm)
            assert build_outline(list(sm), [BASE_PT] * len(sm)
                                 ).polygon is not None


def test_collapse_endpoints_exact_and_counts():
    run = drive(lambda: CollapseStage(0.3), _corpus()["jitter06"])
    raw, der = run["raw"], derived_of(run)
    assert der[0] == raw[0]
    assert der[-1] == raw[-1]
    assert len(der) <= len(raw)
    assert len(der) > 2


def test_tap_and_twopt_valid_every_arm():
    corpus = _corpus()
    for arm, factory in ARMS.items():
        run = drive(factory, corpus["tap"])
        raw, der = run["raw"], derived_of(run)
        assert len(run["stroke"].points) == 1, arm
        assert der == raw, arm  # first-push transparency, all stages
        assert run["pre_center"] is not None, arm
    for arm, factory in ARMS.items():
        run = drive(factory, corpus["twopt"])
        raw, der = run["raw"], derived_of(run)
        assert len(run["stroke"].points) == 2, arm
        assert len(der) == 2, arm
        assert der[0] == raw[0], arm
        if arm == "baseline" or arm.startswith("A"):
            assert der[-1] == raw[-1], arm
        else:
            # Filter endpoint lag is measured, not gated: the screening
            # verdict lives in the milestone report (a breach must fail
            # the candidate there, not turn this module red).
            err = math.hypot(der[-1][0] - raw[-1][0],
                             der[-1][1] - raw[-1][1])
            print(f"TWOPT {arm}: endpoint_err={err:.3f}pt")
            assert math.isfinite(err), arm
        assert run["pre_center"] is not None, arm


def test_repeat_release_is_noop_and_no_double_endpoint():
    for arm, factory in ARMS.items():
        pen = PenHandler(ToolType.PEN, input_stage=factory())
        page = PageId()
        pen.down(page, 0.0, 0.0, 0.6, 1000)
        pen.move(page, 10.0, 0.0, 0.6, 1008)
        done = pen.up(page, 10.0, 0.0, 0.6, 1016)
        n = len(done["stroke_done"].points)
        assert pen.up(page, 99.0, 99.0, 0.6, 1024) == {"noop": True}
        assert pen._fed == 0 and len(pen._center) == 0, arm
        assert n == 2, arm


def test_identical_release_appends_nothing():
    for arm, factory in ARMS.items():
        run = drive(factory, _with_times([(0.0, 0.0), (10.0, 1.0)]))
        assert [(p.x, p.y) for p in run["stroke"].points] == [
            (0.0, 0.0), (10.0, 1.0)], arm


def test_different_release_appends_endpoint_once():
    for arm, factory in ARMS.items():
        pen = PenHandler(ToolType.PEN, input_stage=factory())
        page = PageId()
        pen.down(page, 0.0, 0.0, 0.6, 1000)
        pen.move(page, 10.0, 0.0, 0.6, 1008)
        fed_before = pen._fed
        done = pen.up(page, 20.0, 1.0, 0.6, 1016)
        pts = [(p.x, p.y) for p in done["stroke_done"].points]
        assert pts == [(0.0, 0.0), (10.0, 0.0), (20.0, 1.0)], arm
        assert pen._fed == 0, arm
        assert fed_before >= 2 or arm.startswith("A"), (arm, fed_before)


def test_cancel_and_new_stroke_clean():
    for arm, factory in ARMS.items():
        pen = PenHandler(ToolType.PEN, input_stage=factory())
        page = PageId()
        pen.down(page, 0.0, 0.0, 0.6, 1000)
        pen.move(page, 5.0, 5.0, 0.6, 1008)
        assert pen.cancel() == {"stroke_cancelled": True}
        assert pen._fed == 0 and len(pen._center) == 0, arm
        run2 = drive(factory, _with_times(_line(jitter=0.6)))
        fresh = drive(factory, _with_times(_line(jitter=0.6)))
        assert derived_of(run2) == derived_of(fresh), arm


def test_tool_switch_cancels_stage_state():
    tool_a = ToolInstance(kind=ToolType.PEN, name="A")
    tool_b = ToolInstance(kind=ToolType.PEN, name="B")
    ctrl = InteractionController(tool_a,
                                 input_stage_factory=lambda:
                                 CollapseStage(0.3))
    page = PageId()
    ctrl.handle_pen_press(page, 0.0, 0.0, 0.6, 1000)
    ctrl.handle_pen_move(page, 1.0, 0.0, 0.6, 1008)
    ctrl.set_tool(ToolInstance(kind=ToolType.PAN, name="Pan"))
    assert ctrl.cancel() == {"noop": True}
    ctrl.set_tool(tool_b)
    samples = _with_times(_line(jitter=0.6))
    expect = drive(lambda: CollapseStage(0.3), samples)
    x0, y0, t0 = samples[0]
    ctrl.handle_pen_press(page, x0, y0, 0.6, t0)
    for (x, y, t) in samples[1:]:
        ctrl.handle_pen_move(page, x, y, 0.6, t)
    # Raw capture identical; a fresh handler means no cross-stroke state.
    assert [(p.x, p.y) for p in ctrl._last_pen._points] == expect["raw"]


def test_desync_fallback_still_triggers():
    pen = PenHandler(ToolType.PEN)
    page = PageId()
    pen.down(page, 0.0, 0.0, 0.6, 1000)
    pen.move(page, 5.0, 0.0, 0.6, 1008)
    pen.move(page, 10.0, 0.0, 0.6, 1016)
    assert pen._fed == 3
    assert pen.live_centerline() is not None
    pen._center.append(999.0, 999.0)  # genuine desync, bypassing feeds
    assert len(pen._center) == 4 and pen._fed == 3
    assert pen.live_centerline() is None


def test_collapse_does_not_mask_desync():
    pen = PenHandler(ToolType.PEN, input_stage=CollapseStage(0.3))
    page = PageId()
    pen.down(page, 0.0, 0.0, 0.6, 1000)
    pen.move(page, 0.1, 0.0, 0.6, 1008)  # dropped by collapse
    assert pen._fed == 1 and len(pen._center) == 1
    assert pen.live_centerline() is not None  # drops are NOT desync
    pen._center.append(7.0, 7.0)
    assert pen.live_centerline() is None


def test_pressure_values_and_profiles_unchanged():
    ramp = [i / 59 for i in range(60)]
    samples = _with_times(_line(60))
    base = drive(InputStage, samples, pressure=True, pressures=ramp)
    for arm, factory in ARMS.items():
        run = drive(factory, samples, pressure=True, pressures=ramp)
        assert run["stroke"].pressures == base["stroke"].pressures, arm
        assert run["stroke"].pressure_profile == "normal", arm
        assert run["stroke"].pressure_enabled is True, arm
    off = drive(lambda: CollapseStage(0.3), samples, pressure=False)
    assert off["stroke"].pressure_enabled is False
    assert off["stroke"].pressures == (0.6,) * 60


def test_geometry_stays_finite_no_unexpected_fallbacks():
    corpus = _corpus()
    for name, samples in corpus.items():
        for arm, factory in ARMS.items():
            run = drive(factory, samples)
            for (x, y) in derived_of(run):
                assert math.isfinite(x) and math.isfinite(y), (name, arm)
            assert run["pre_center"] is not None, (name, arm)
            if len(derived_of(run)) >= 3:
                sm, _ = smooth_centerline(derived_of(run), None)
                o = build_outline(list(sm),
                                  [BASE_PT] * len(sm))
                assert all(math.isfinite(p.x) and math.isfinite(p.y)
                           for p in o.polygon), (name, arm)


def test_stage_cost_sanity_bound():
    dense = _with_times([(float(i), 0.5 * math.sin(i * 0.5))
                         for i in range(2000)], step_ms=8)
    for arm, factory in ARMS.items():
        stage = factory()
        clamped, prev = [], None
        for (_x, _y, t) in dense:
            v = int(t)
            prev = v if prev is None else (v if v > prev else prev + 1)
            clamped.append(prev)
        costs = []
        stage.reset()
        for (x, y, _t), tc in zip(dense, clamped):
            t0 = time.perf_counter()
            stage.push(x, y, tc)
            costs.append((time.perf_counter() - t0) * 1000.0)
        costs.sort()
        p95 = costs[min(len(costs) - 1, int(0.95 * len(costs)))]
        print(f"COST {arm}: med={costs[len(costs)//2]:.4f} "
              f"p95={p95:.4f} max={costs[-1]:.4f} ms/sample")
        assert p95 < 5.0, (arm, p95)


def test_constructor_validation():
    import pytest
    with pytest.raises(ValueError):
        SpeedAdaptiveStage(0.9, 0.1, 0.1)
    with pytest.raises(ValueError):
        SpeedAdaptiveStage(0.3, 1.2, 0.1)
    with pytest.raises(ValueError):
        SpeedAdaptiveStage(0.3, 0.8, 0.0)
    assert CollapseStage(float("nan"))._d == 0.3
