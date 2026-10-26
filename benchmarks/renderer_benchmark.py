"""M2 renderer benchmark (baseline, no optimization).

Run::

    uv run python -m benchmarks.renderer_benchmark         # 100-page corpus
    uv run python -m benchmarks.renderer_benchmark --stress  # + 600-page memory/stress

Corpus: generated rich PDFs (text + vector geometry + gradient bands —
real raster workload, never blank pages). The 600-page workload runs ONLY
with --stress, never as part of the normal suite.

Records first-tile latency, multi-tile throughput, zoom/rotation scaling,
1/2/4-worker comparison, RGB-vs-RGBA conversion comparison, and RSS
before/peak/after. The public contract stays RGBA8 regardless of the
comparison outcome (M2 decision).
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diagnostics import get_rss_mb
from core.types import DocumentId, PageId, Size
from rendering.coordinates import rotated_render_size
from rendering.engine import RendererEngine
from rendering.renderer import render_tile_raw
from rendering.scheduler import EngineConfig
from rendering.types import CancellationToken, RenderRequest, TileRect
from rendering.workers import WorkerHandles
from tests.helpers import make_rich_pdf

PW, PH = 595.0, 842.0


def _req(pdf: str, page: int, tile: TileRect, scale: float = 1.0,
         rotation: int = 0, generation: int = 0) -> RenderRequest:
    return RenderRequest(
        document_id=DocumentId("bench"),
        page_id=PageId(f"bench:p{page}"),
        page_index=page,
        scale=scale,
        rotation_degrees=rotation,
        tile=tile,
        generation=generation,
        source_path=pdf,
    )


def _summarize(name: str, totals: list[float]) -> dict:
    totals = sorted(totals)
    n = len(totals)
    p50 = statistics.median(totals)
    p95 = totals[max(0, int(n * 0.95) - 1)]
    print(f"  {name}: n={n} first={totals[0]:.2f}ms median={p50:.2f}ms "
          f"p95={p95:.2f}ms total={sum(totals):.0f}ms "
          f"throughput={1000.0 * n / max(sum(totals), 1e-9):.1f} tiles/s")
    return {"n": n, "first_ms": totals[0], "median_ms": p50, "p95_ms": p95,
            "total_ms": sum(totals)}


def _render_many(pdf: str, pages: int, requests: list[RenderRequest],
                 workers: int) -> tuple[list, dict, float]:
    engine = RendererEngine(EngineConfig(worker_count=workers))
    try:
        t0 = time.perf_counter()
        futs = [engine.submit(r) for r in requests]
        results = [f.result(timeout=120) for f in futs]
        wall = (time.perf_counter() - t0) * 1000.0
        snap = engine.metrics()
        return results, snap, wall
    finally:
        engine.shutdown()


def main() -> int:
    stress = "--stress" in sys.argv
    rss_start = get_rss_mb()
    rss_peak = rss_start
    with tempfile.TemporaryDirectory(prefix="inkfall-render-bench-") as tmp:
        pdf = make_rich_pdf(str(Path(tmp) / "bench100.pdf"), pages=100)
        print(f"corpus: 100 pages {PW:.0f}x{PH:.0f}pt (rich vector/text/gradient)")

        # A. First tile (cold engine, 1 worker).
        engine = RendererEngine(EngineConfig(worker_count=1))
        try:
            t0 = time.perf_counter()
            first = engine.submit(_req(pdf, 0, TileRect(0, 0, 512, 512))).result(timeout=120)
            first_ms = (time.perf_counter() - t0) * 1000.0
        finally:
            engine.shutdown()
        print(f"A. first tile request->result: {first_ms:.2f}ms "
              f"(queue={first.queue_wait_ms:.2f} render={first.render_ms:.2f} "
              f"convert={first.convert_ms:.2f})")
        rss_peak = max(rss_peak, get_rss_mb())

        # B. Multiple tiles (1/4/16/64) with 4 workers.
        for count in (1, 4, 16, 64):
            reqs = [_req(pdf, i % 100, TileRect((i * 137) % 200, (i * 251) % 400, 256, 256),
                         generation=i) for i in range(count)]
            results, _snap, _wall = _render_many(pdf, 100, reqs, workers=4)
            totals = [r.total_ms for r in results]
            _summarize(f"B. {count} tiles / 4 workers", totals)
            rss_peak = max(rss_peak, get_rss_mb())

        # C. Zoom 1/2/4/8x (single 512 tile, page 0).
        for scale in (1.0, 2.0, 4.0, 8.0):
            res, _, _ = _render_many(pdf, 100, [_req(pdf, 0, TileRect(0, 0, 512, 512),
                                                             scale=scale)], workers=2)
            r = res[0]
            print(f"C. zoom {scale:.0f}x: tile={r.width_px}x{r.height_px}px "
                  f"total={r.total_ms:.2f}ms render={r.render_ms:.2f}ms "
                  f"convert={r.convert_ms:.2f}ms bytes={len(r.data or b'')}")
            rss_peak = max(rss_peak, get_rss_mb())

        # D. Rotation 0/90/180/270.
        for rot in (0, 90, 180, 270):
            fw, fh = rotated_render_size(Size(PW, PH), 1.0, rot)
            tw, th = min(512, fw), min(512, fh)
            res, _, _ = _render_many(pdf, 100, [_req(pdf, 0, TileRect(0, 0, tw, th),
                                                             rotation=rot)], workers=2)
            r = res[0]
            print(f"D. rotation {rot}deg: tile={r.width_px}x{r.height_px}px "
                  f"total={r.total_ms:.2f}ms render={r.render_ms:.2f}ms")
            rss_peak = max(rss_peak, get_rss_mb())

        # E. Worker scaling on identical 16-tile workload.
        for workers in (1, 2, 4):
            reqs = [_req(pdf, i % 100, TileRect((i * 137) % 200, (i * 251) % 400, 256, 256),
                         generation=1000 + i) for i in range(16)]
            results, snap, wall = _render_many(pdf, 100, reqs, workers=workers)
            totals = sorted(r.total_ms for r in results)
            qw = snap["total_ms"]
            print(f"E. {workers} worker(s): wall={wall:.0f}ms "
                  f"median={statistics.median(totals):.2f}ms p95={totals[max(0, int(16 * 0.95) - 1)]:.2f}ms "
                  f"avg_queue={snap['queue_wait_ms']['avg']:.2f}ms "
                  f"throughput={16000.0 / max(wall, 1e-9):.1f} tiles/s")
            rss_peak = max(rss_peak, get_rss_mb())

        # F. RGB vs RGBA conversion comparison (raw raster core, same tiles).
        handles = WorkerHandles()
        try:
            import pymupdf

            doc = handles.acquire(pdf)
            page = doc[0]
            pw, ph = float(page.rect.width), float(page.rect.height)
            tiles = [TileRect((i * 137) % 200, (i * 251) % 400, 256, 256) for i in range(8)]
            for alpha, label in ((False, "RGB"), (True, "RGBA8")):
                rms, cms, sizes = [], [], []
                for i, t in enumerate(tiles):
                    rq = _req(pdf, 0, t, generation=2000 + i)
                    data, _w, _h, rm, cm = render_tile_raw(
                        doc, rq, pw, ph, CancellationToken(), alpha=alpha)
                    assert data is not None
                    rms.append(rm)
                    cms.append(cm)
                    sizes.append(len(data))
                print(f"F. {label}: render_avg={sum(rms) / len(rms):.2f}ms "
                      f"convert_avg={sum(cms) / len(cms):.2f}ms "
                      f"bytes_per_tile={sizes[0]} (contract={'RGBA8' if alpha else 'RGB-benchmark-only'})")
        finally:
            handles.close_all()
        rss_peak = max(rss_peak, get_rss_mb())

        if stress:
            big = make_rich_pdf(str(Path(tmp) / "bench600.pdf"), pages=600)
            rss_pre = get_rss_mb()
            reqs = [_req(big, i % 600, TileRect((i * 137) % 200, (i * 251) % 400, 256, 256),
                         generation=3000 + i) for i in range(64)]
            _results, _snap, wall = _render_many(big, 600, reqs, workers=4)
            rss_post = get_rss_mb()
            rss_peak = max(rss_peak, rss_post)
            print(f"STRESS 600 pages x64 tiles / 4 workers: wall={wall:.0f}ms "
                  f"rss_pre={rss_pre:.1f}MiB rss_post={rss_post:.1f}MiB "
                  f"delta={rss_post - rss_pre:+.1f}MiB")

        rss_end = get_rss_mb()
        rss_peak = max(rss_peak, rss_end)
        print(f"RSS before={rss_start:.1f}MiB peak={rss_peak:.1f}MiB after={rss_end:.1f}MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
