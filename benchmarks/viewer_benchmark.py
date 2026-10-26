"""M3-B viewer benchmark (pipeline-level baselines, no optimization).

Run::

    uv run --no-sync python -m benchmarks.viewer_benchmark         # ~20-page corpus
    uv run --no-sync python -m benchmarks.viewer_benchmark --stress  # + 600-page

Covers: cold open + layout build, first viewport, step scroll, fast
scroll, scroll-back reuse, zoom ladder + hysteresis wiggle, rotation,
mixed sizes, worker scaling on realistic tiles. No 60 FPS claims: these
are M3 baselines for future viewer work.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diagnostics import get_rss_mb
from core.types import DocumentId
from document.backend import PdfBackend
from document.models import PageInfo
from rendering.pipeline import DocumentPipeline
from rendering.scheduler import EngineConfig
from tests.helpers import (
    make_dense_pdf,
    make_intrinsic_pdf,
    make_mixed_pdf,
    make_rich_pdf,
)

VW, VH = 800, 600


def _pages_from_backend(pdf: str) -> tuple[DocumentId, list[PageInfo]]:
    backend = PdfBackend.open(pdf)
    try:
        doc_id = backend.document_id
        return doc_id, [backend.page_info(i) for i in range(backend.page_count)]
    finally:
        backend.close()


def _drain(pipe: DocumentPipeline, timeout_s: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while True:
        stats = pipe.stats()
        if stats["in_flight"] == 0 and stats["engine"]["pending"] == 0:
            return stats
        assert time.monotonic() < deadline, "pipeline did not quiesce"
        time.sleep(0.005)


def _scroll(pipe: DocumentPipeline, scroll: float) -> tuple[dict, dict]:
    t0 = time.perf_counter()
    summary = pipe.update(scroll, VW, VH)
    stats = _drain(pipe)
    return summary, stats


def main() -> int:
    stress = "--stress" in sys.argv
    rss_start = get_rss_mb()
    rss_peak = rss_start
    with tempfile.TemporaryDirectory(prefix="inkfall-viewer-bench-") as tmp:
        pdf = make_rich_pdf(str(Path(tmp) / "doc20.pdf"), pages=20)
        doc_id, pages = _pages_from_backend(pdf)

        t0 = time.perf_counter()
        pipe = DocumentPipeline(doc_id, pdf, pages,
                                engine_config=EngineConfig(worker_count=2))
        print(f"1. open + pipeline build (20pp): {(time.perf_counter() - t0) * 1000:.1f}ms")
        layout_total = pipe._layout.total_height_px
        print(f"   layout height: {layout_total}px over {len(pages)} pages")

        t0 = time.perf_counter()
        summary, stats = _scroll(pipe, 0.0)
        first_ms = (time.perf_counter() - t0) * 1000.0
        ready = pipe.visible_ready(0.0, VW, VH)
        print(f"2. first viewport: {first_ms:.1f}ms wall, "
              f"{len(ready)} tiles ready, cache {stats['cache']['resident_bytes'] // 1024}KiB")
        rss_peak = max(rss_peak, get_rss_mb())

        # Fast scroll while the cache is still cold (only viewport 0 seen).
        t0 = time.perf_counter()
        pipe.update(float(layout_total - VH), VW, VH)
        stats = _drain(pipe)
        print(f"3. fast scroll to end (cold): {(time.perf_counter() - t0) * 1000:.0f}ms, "
              f"cancelled={stats['engine']['cancelled']}, "
              f"dropped_prefetch={stats['engine']['dropped_prefetch']}")
        rss_peak = max(rss_peak, get_rss_mb())

        # Step scroll through the document one viewport at a time.
        step = float(VH)
        scrolls = []
        y = 0.0
        while y < layout_total:
            scrolls.append(y)
            y += step
        t0 = time.perf_counter()
        for scroll in scrolls:
            pipe.update(scroll, VW, VH)
        stats = _drain(pipe)
        wall = (time.perf_counter() - t0) * 1000.0
        cache = stats["cache"]
        print(f"4. step scroll x{len(scrolls)}: {wall:.0f}ms, "
              f"hit_rate={cache['hits'] / max(cache['hits'] + cache['misses'], 1):.2f}, "
              f"resident={cache['resident_bytes'] // 1024}KiB/{cache['resident_tiles']} tiles, "
              f"stale={stats['stale_discarded']}")
        rss_peak = max(rss_peak, get_rss_mb())

        # Scroll back: fully warm after step coverage.
        t0 = time.perf_counter()
        summary = pipe.update(0.0, VW, VH)
        stats = _drain(pipe)
        print(f"5. scroll back to start: {(time.perf_counter() - t0) * 1000:.0f}ms, "
              f"viewport_hits={summary['hits']}/{summary['visible']} "
              f"(reuse expected)")
        rss_peak = max(rss_peak, get_rss_mb())
        pipe.close()

        # Zoom ladder + hysteresis wiggle on a fresh pipeline.
        pipe = DocumentPipeline(doc_id, pdf, pages,
                                engine_config=EngineConfig(worker_count=2))
        for zoom in (1.0, 2.0, 4.0, 8.0):
            bucket = pipe.set_zoom(zoom)
            t0 = time.perf_counter()
            pipe.update(0.0, VW, VH)
            stats = _drain(pipe)
            ready = pipe.visible_ready(0.0, VW, VH)
            print(f"6. zoom {zoom:.0f}x -> bucket {bucket:.1f}: "
                  f"{(time.perf_counter() - t0) * 1000:.0f}ms, "
                  f"{len(ready)} tiles, resident={stats['cache']['resident_bytes'] // 1024}KiB")
        before = stats["submitted_visible"] + stats["submitted_prefetch"]
        assert pipe.set_zoom(1.13) == 1.0
        summary = pipe.update(0.0, VW, VH)
        _drain(pipe)
        after = pipe.stats()["submitted_visible"] + pipe.stats()["submitted_prefetch"]
        print(f"   wiggle 8.0 -> 1.13 (bucket 1.0): {after - before} new submits, "
              f"viewport_hits={summary['hits']}/{summary['visible']} "
              f"(1.0-bucket tiles retained across epochs: reuse by design)")
        rss_peak = max(rss_peak, get_rss_mb())
        pipe.close()

        # Rotation + mixed sizes.
        mixed = make_mixed_pdf(str(Path(tmp) / "mixed.pdf"), pages=8)
        mid, mpages = _pages_from_backend(mixed)
        pipe = DocumentPipeline(mid, mixed, mpages,
                                engine_config=EngineConfig(worker_count=2))
        pipe.set_user_rotation(90)
        t0 = time.perf_counter()
        pipe.update(0.0, VW, VH)
        stats = _drain(pipe)
        print(f"8. mixed sizes + user rot 90: {(time.perf_counter() - t0) * 1000:.0f}ms, "
              f"ready={len(pipe.visible_ready(0.0, VW, VH))}, "
              f"failed={stats['engine']['failed']}")
        rss_peak = max(rss_peak, get_rss_mb())
        pipe.close()

        intr = make_intrinsic_pdf(str(Path(tmp) / "intr.pdf"))
        iid, ipages = _pages_from_backend(intr)
        pipe = DocumentPipeline(iid, intr, ipages,
                                engine_config=EngineConfig(worker_count=2))
        pipe.update(0.0, VW, VH)
        stats = _drain(pipe)
        print(f"   intrinsic-rotation doc: failed={stats['engine']['failed']}, "
              f"ready={len(pipe.visible_ready(0.0, VW, VH))}")
        pipe.close()

        # Worker scaling on realistic tiles (dense 1190x1684 @2x viewport).
        dense = make_dense_pdf(str(Path(tmp) / "dense.pdf"), pages=2)
        did, dpages = _pages_from_backend(dense)
        for workers in (1, 2, 4):
            pipe = DocumentPipeline(
                did, dense, dpages,
                engine_config=EngineConfig(worker_count=workers),
                ui_zoom=2.0,
            )
            t0 = time.perf_counter()
            pipe.update(0.0, 1190, 1684 // 2)
            stats = _drain(pipe)
            wall = (time.perf_counter() - t0) * 1000.0
            print(f"W. {workers} worker(s), dense 2x viewport: wall={wall:.0f}ms "
                  f"avg_tile={stats['engine']['total_ms']['avg']:.2f}ms "
                  f"queue_avg={stats['engine']['queue_wait_ms']['avg']:.2f}ms")
            rss_peak = max(rss_peak, get_rss_mb())
            pipe.close()

        if stress:
            big = make_rich_pdf(str(Path(tmp) / "big600.pdf"), pages=600)
            t0 = time.perf_counter()
            bid, bpages = _pages_from_backend(big)
            print(f"S. cold 600pp open + page_info x600: {(time.perf_counter() - t0) * 1000:.0f}ms")
            pipe = DocumentPipeline(bid, big, bpages,
                                    engine_config=EngineConfig(worker_count=2))
            print(f"   layout build (600pp): total={pipe._layout.total_height_px}px")
            t0 = time.perf_counter()
            pipe.update(0.0, VW, VH)
            stats = _drain(pipe)
            print(f"   first viewport 600pp: {(time.perf_counter() - t0) * 1000:.0f}ms, "
                  f"ready={len(pipe.visible_ready(0.0, VW, VH))}")
            rss_mid = get_rss_mb()
            pipe.update(float(pipe._layout.total_height_px - VH), VW, VH)
            _drain(pipe)
            pipe.update(0.0, VW, VH)
            stats = _drain(pipe)
            print(f"   end-to-end + back: hits={stats['hits']}, "
                  f"resident={stats['cache']['resident_bytes'] // 1024}KiB, "
                  f"RSS mid={rss_mid:.1f}MiB")
            rss_peak = max(rss_peak, get_rss_mb())
            pipe.close()

        rss_end = get_rss_mb()
        print(f"RSS before={rss_start:.1f}MiB peak={max(rss_peak, rss_end):.1f}MiB "
              f"after={rss_end:.1f}MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
