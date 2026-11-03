# HANDOFF — Build the variable-width ink engine

**You are receiving a complete, evidence-backed specification plus a working
oracle implementation. Your job: port this ink engine into our PDF editor, in
our stack, and make the acceptance tests pass.**

This file is self-contained. You do not need network access, the original
binary, or the app. Everything needed is in this folder.

---

## 0. What to build (the one-paragraph brief)

A variable-width pen that turns pointer samples `(x, y, pressure)` into a **filled
silhouette**, not a stroked line. Every span between two consecutive samples
becomes a closed quadrilateral bounded by the **two common external tangents of
the two end circles** (radius = half the stroke width at each sample). All spans
are emitted as sub-paths of one geometry and resolved with a **single NonZero
boolean union**, which merges overlaps and produces mitred joins for free. The
silhouette is then optionally **fitted to cubic Béziers** (preserving corners
sharper than 40°, chord tolerance 0.01), **flattened** by recursive `t = 0.5`
subdivision, **simplified** with Ramer–Douglas–Peucker at 0.2, and serialised as
`M / L / C / Z` SVG path data or written to the PDF appearance stream.

That is the whole engine. It is small. The quality lives in three details:
**`w` is a radius**, the **degenerate-span guard**, and **input resampling**.

---

## 1. Files in this folder

| file | role |
|---|---|
| `IMPLEMENT.md` | this spec — the contract |
| `ink_engine_reference.py` | dependency-free reference implementation; the **oracle** for behaviour |
| `acceptance_test.py` | 26 assertions with exact expected values; run it, make it pass |

```bash
python acceptance_test.py      # must print: ALL ACCEPTANCE CHECKS PASSED
```

Workflow: read §3–§10, port the math into our codebase, then port
`acceptance_test.py` to our test framework and make every assertion green. Add
the visual checklist in §12 before you call it done.

---

## 2. Data model (copy this shape exactly)

```
Point    { x: f64, y: f64, w: f64 }        // w = HALF width = radius
Segment  { type: Move|Line|Bezier, cp[4]: Point }
           Move   uses cp[0]
           Line   uses cp[0] -> cp[1]
           Bezier uses cp[0], cp[1], cp[2], cp[3]      // p0, c1, c2, p3
Path     { segments: list<Segment>, fill_mode: EvenOdd|NonZero,
           closed: bool, fill: bool }
Geometry { paths: list<Path>, svg_cache: string }
```

Rules that are not negotiable:

* **`w` is a radius.** `Thickness = 2w`. Getting this wrong makes all ink twice
  as fat — the single most common porting bug.
* A stroke is **one geometry** containing many quads; it is not a polyline you
  stroke with a renderer.
* Default fill rule **NonZero**. EvenOdd produces holes where spans overlap.

---

## 3. THE CORE — span expansion (irreplaceable math)

Given two samples `A = (ax, ay, w₀)` and `B = (bx, by, w₁)`:

```
dx = bx - ax
dy = by - ay
L2 = dx*dx + dy*dy
dw = w0 - w1
if dw*dw >= L2:            return nothing      // native guard, see §5
L  = sqrt(L2)
ux = dx / L ; uy = dy / L
k  = (w0 - w1) / L
m  = sqrt(max(0, 1 - k*k))

for t in (+1, -1):
    nx = k*ux - t*m*uy
    ny = t*m*ux + k*uy
    PA(t) = ( ax + nx*w0 , ay + ny*w0 )
    PB(t) = ( bx + nx*w1 , by + ny*w1 )
```

Then assemble **one closed path**:

```
MOVE   PA(+1)
LINE   PA(+1) -> PA(-1)          // straight cut across the start
LINE   PA(-1) -> PB(-1)          // right tangent edge
LINE   PB(-1) -> PB(+1)          // straight cut across the end
CLOSE                            // closes PB(+1) -> PA(+1) = left tangent edge
```

### Why it is exact

The edge line must touch both end circles. For a unit normal `n` of the edge,
`(B + w₁n − A − w₀n) · n = 0`, so `L(u·n) = w₀ − w₁`, i.e. `u·n = k`. Since `n`
is a unit vector, `n = k·u ± sqrt(1−k²)·u⊥` with `u⊥ = (−uy, ux)`. Both edge
points therefore sit at perpendicular distance exactly `w₀`/`w₁` from the
centreline — no approximation anywhere.

### Degenerate cases

| case | behaviour |
|---|---|
| `w₀ == w₁` | `k = 0`, the quad degenerates to an exact rectangle of thickness `2w` |
| `(w₀−w₁)² >= L²` | reject the span (native behaviour) or subdivide it (§5) |
| `L == 0` | skip; never divide by zero |
| `\|w₀\| == \|w₁\| == 0` | skip (zero-width span contributes nothing) |

---

## 4. Assembly and union

1. Build quads for every consecutive sample pair.
2. Put them all in one `Geometry` as separate sub-paths.
3. **Union them with NonZero fill** using a polygon clipper (Clipper /
   `clipper2` / `polygon-clipping` / `geo` / our existing clipper).
   Do it at **fixed point scale 1e5** (int64, `round(x*100000)`) — the original
   engine does exactly this, and it makes incremental unions deterministic and
   idempotent.
4. Convert back to double once, after the union.

If you cannot get a clipper running, an SVG/renderer-level fallback is to emit
all quads as sub-paths of one path with `fill-rule="nonzero"` — visually
identical when filling, but you then cannot get a single outline contour, holes,
area, or hit-testing. **Do not ship the fallback for the real editor.**

Joins: interior corners come out **mitred automatically** because the two
adjacent quads overlap near the shared sample and the union takes the outer
boundary. There is no separate join step. Very sharp turns produce a spike;
clamp by re-sampling (§6) rather than by adding join code.

---

## 5. The degenerate-span fix (do this, it beats the original)

The original drops any span where `(Δw)² ≥ L²`. In practice that means fast
strokes with pressure spikes develop **gaps** — the ink breaks. Fix it by
splitting the span instead of dropping it:

```
if (dw*dw >= 0.81*L2):                  // 0.9^2, safe margin
    split the span at t = 0.5 with
        wm = (w0 + w1) / 2
        (xm, ym) = midpoint
    emit recursively (depth cap ~6)
else:
    emit one quad
```

This is the single largest visual difference between a naive port and a
faithful one. The reference implementation keeps native behaviour for
comparison; ship the subdividing variant.

---

## 6. Input conditioning — where "beautiful" comes from

The geometry kernel is exact, but it is **per-span**. Feed it 120 Hz raw samples
and you get visible facets on curves. Condition the input first:

1. **Resample the centreline** to a maximum spacing of ~1.5–2 px (screen space)
   using Catmull-Rom through the raw points. Interpolate `w` linearly along the
   same parameter. Never interpolate pressure then re-map it — map first, then
   interpolate width.
2. **Drop duplicate/noise samples**: if `|P_i − P_{i−1}| < 0.6 px`, merge them
   (keep the max `w` so pressure spikes survive).
3. **One-Euro or EMA smoothing on pressure**, not on position: pressure is the
   noisy channel. `w` should be monotone-ish, never sawtoothing.
4. **Clamp `w`** to `[minWidth, maxWidth]` before geometry; never let `w` reach 0
   mid-stroke (that pinches the ribbon to a point and looks broken).
5. **Pressure → width**:

```
w = 0.5 * Thickness * ( (1 - alpha) + alpha * pow(pressure, gamma) )
      alpha = "pressure sensitivity" slider   in [0, 1]   (0 = constant pen)
      gamma = response curve                  ~1.0; 1.4-1.8 feels more natural
      pressure in [0, 1]   (normalise: raw/min(1, rawMax))
```

Expose `alpha` and `gamma` as pen settings; they are what users actually tune.

---

## 7. Caps

The kernel has **no arc generator** — span ends are straight cuts. The original
app adds caps outside the kernel. Do the same:

* **Round cap (default, looks best):** append a half-disc of radius `w` at the
  first and last sample, oriented away from the stroke, and include it in the
  union. Use ≥ 24 segments per π; error at `r = 10 px` is then ~0.05 px.
* **Square cap:** extend the first/last span by `w` along the tangent, radius
  unchanged.
* **Butt cap:** nothing — this is the raw kernel behaviour.

Crucially: the cap radius must equal the **end sample's `w`**, no shrink. Caps
that use `Thickness/2` while the ribbon uses a tapered `w` look pinched.

---

## 8. Post passes (all defaulted, all optional)

| pass | default | purpose |
|---|---|---|
| **Smooth** | tol `0.01`, corner `40°` | silhouette polyline → cubic Béziers for crisp, smooth output and small files |
| **Flatten** | `t = 0.5` recursive | Béziers → polyline, for hit-testing / area / PDF line output |
| **Simplify** | `0.2` | RDP to drop redundant vertices; keeps PDF streams small |

**Smooth** — corner preserve rule: a vertex is a hard corner when

```
dot( normalize(P_i − P_{i−1}), normalize(P_{i+1} − P_i) )  <  cos(40°) = 0.7660444431
```

Split the polyline at corners, then fit each run with an error-bounded cubic
fitter (Schneider / `FitCurves.c` / Paper.js `fitCurve`) using `tol = 0.01`.
Expose `tol` and the corner angle; `30–50°` is the useful range.

**Flatten** — recursive split at `t = 0.5`; stop when
`(d1 + d2)² <= tol·|chord|²`, where `d1`, `d2` are the control points'
perpendicular distances from the chord. Depth cap 16.

**Simplify** — de-duplicate consecutive points in **3-D**: drop a point only if
`x`, `y` **and** `w` all match, so a width change alone keeps a vertex. Then RDP
on `(x, y)` with `tol = 0.2`.

---

## 9. Serialisation

SVG path data — reproduce this grammar exactly (it is what the original emits):

```
M x, y
L x, y
C x1, y1 , x2, y2 , x, y
Z
```

* space after the command letter, then `x`, then `", "`, then `y`
* `"Z "` with a trailing space
* numbers at 6 significant digits (`%.6g`)

For PDF output: turn the unioned silhouette into the annotation appearance
stream (`/AP /N` Form XObject) as one path with `f` (nonzero fill), or write
Bézier segments if you ran the Smooth pass. Set `/InkList` with the raw
centreline points so other viewers can re-render natively.

---

## 10. Performance / live drawing

* Keep the **raw** sample list per stroke. Never persist the outline as the
  stroke's source of truth — re-derive on edit/resize.
* While the pointer is down: union the **new span only** into the previous
  result (Clipper supports subject∪clip incrementally), plus one live end cap.
  Do **not** re-union the whole stroke per frame into a fresh clipper.
* On pointer-up: run Smooth once, cache the result, and store the raw samples.
* Union in int64 at `1e5`. Keep the same scale for incremental merges so results
  are idempotent and free of hairline cracks.
* Budget: a 2000-sample stroke with per-span quads + one union is a few
  milliseconds. If it is not, you are re-unioning from scratch or allocating per
  span in a hot loop.

---

## 11. Acceptance criteria

**Automated** — `python acceptance_test.py` must print
`ALL ACCEPTANCE CHECKS PASSED`. It asserts, with exact values:

| test | contract |
|---|---|
| T1 | constant width `w=5`, `L=100` → corners `(0,±5)`, `(100,±5)`, area exactly `1000.0` |
| T2 | all four corners exactly `w` from the centreline |
| T3 | tapered span: `\|Pend − end\| == w_end`, and `u·n == (w₀−w₁)/L` to 1e-12 |
| T4 | `(Δw)² ≥ L²` → span rejected |
| T5 | corner order `A+n⁺, A+n⁻, B+n⁻, B+n⁺`, path closed, 4 segments |
| T6 | one quad per span; round caps add exactly 2 disc polygons |
| T7 | Simplify: collinear → 2 pts; 0.1 deviation dropped; 1.0 deviation kept |
| T8 | Smooth: gentle curve → Béziers; 90° corner preserved as a split |
| T9 | flatten preserves endpoints, no NaN |
| T10 | SVG grammar `M … L … Z `, `", "` separator |
| T11 | full stroke → NonZero path, no NaN, expected polygon count |

**Visual** — check every one of these on screen before declaring done:

1. A slow, hard-pressed stroke **tapers at both ends** and is thickest where
   pressure peaked; no pinch, no gap.
2. A fast flick shows **no gaps** (this is §5 working).
3. A tight loop crossing itself shows **no dark double-blend** and **no interior
   notch** (this is the union working).
4. A 90° corner is a **sharp corner**, not a rounded blob (§8 Smooth).
5. Round caps are **circular**, not pinched, not flat.
6. Zoom to 400% on a curve: the edge is **smooth**, not faceted (§6 resampling).
7. Stroke a circle: it closes without a seam or a spike at the join.
8. Erase a stroke partially: the remaining geometry is still valid (re-derive
   from raw samples, not by subtracting from the cached outline).

---

## 12. Porting notes per stack

* **C# / .NET** — closest to the original. Use `Clipper2` (`ClipperD`), `Point`
  as a `struct` with three doubles, and `List<Segment>` per path. Watch out for
  `float` sneaking in: the math needs `double` end to end.
* **C++** — `Clipper2` or `Clipper 6.x`; `Path64` at scale 1e5; keep the quad
  builder allocation-free (reuse a scratch polygon).
* **Rust** — `geo` + `geo::BooleanOps` (nonzero union) or `i_overlay`; `f64`
  everywhere; `Point { x: f64, y: f64, w: f64 }` derives `Copy`.
* **TypeScript / Canvas** — `polygon-clipping` or `martinez`; emit a single
  `Path2D` with all quads and `ctx.fill('nonzero')` for the fast path, clipper
  only when you need the true outline.
* **Any stack** — port `acceptance_test.py` first as a failing test suite, then
  implement until green. The numbers are exact; do not adjust the tolerances.

---

## 13. Provenance, and what is approximation

Recovered from `drawboard-ink-lib.dll` (Drawboard PDF 6.91.6.0 x64) by static
analysis: export table + vtable dispatch, `.pdata` function boundaries, and
full instruction-level decode of the span-expansion, quad-assembly, outline
(union), smooth, flatten, simplify and SVG-serialisation routines. The span
formula and the millimetre-exact invariants in the acceptance test are
**observed behaviour**, not invention.

Two things are **approximations** and you may tune them:

1. **The pressure → width curve** — the app's mapping lives in .NET NativeAOT
   code and was not recovered. §6.5 is a modelling choice that exposes the same
   two UI knobs the original exposes (sensitivity `alpha`, curve `gamma`).
2. **The corner threshold semantics** — `cos(40°)` and `tol 0.01` are observed
   constants; the exact branch structure of the fitter is inferred. The
   behaviour described in §8 is what produces the observed output.

Everything else in this document is the real thing.

---

### TL;DR

`w` is a radius. Each span = the tangent trapezoid
`n = k·u ± √(1−k²)·u⊥`, corners `A + w₀n`, `B + w₁n`. Union all spans with
NonZero at 1e5. Subdivide spans where `(Δw)² ≥ L²` instead of dropping them.
Resample input to ~2 px and smooth pressure, not position. Add round caps
outside the kernel with radius = end `w`. Smooth with 40° corner preservation,
flatten at `t = 0.5`, simplify at 0.2. Serialise `M/L/C … Z` at 6 significant
digits. Make `acceptance_test.py` green, then walk the visual checklist.
