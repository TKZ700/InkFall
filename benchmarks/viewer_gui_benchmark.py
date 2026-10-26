"""M3-C GUI benchmark: real viewer, offscreen Qt. Numbers only.

Measures application startup, PDF open, pipeline/layout construction,
first displayed tile, full first viewport, scroll-handler cost (GUI
thread), painted tiles, cache hit/miss, stale/cancelled/dropped counts,
cache bytes and RSS. No FPS/smoothness claims.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow
from core.diagnostics import get_rss_mb
from tests.helpers import make_rich_pdf

TIMEOUT_S = 60.0


def _wait_until(predicate, timeout_s: float = TIMEOUT_S, what: str = "condition") -> None:
    app = QApplication.instance()
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        assert app is not None
        app.processEvents()
        time.sleep(0.005)


def main() -> int:
    rss_start = get_rss_mb()
    rss_peak = rss_start
    with tempfile.TemporaryDirectory(prefix="inkfall-gui-bench-") as tmp:
        pdf = make_rich_pdf(str(Path(tmp) / "gui.pdf"), pages=6)

        t0 = time.perf_counter()
        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        window.resize(800, 600)
        window.show()
        app.processEvents()
        print(f"startup (QApp + window): {(time.perf_counter() - t0) * 1000:.0f}ms")

        viewer = window.viewer
        t0 = time.perf_counter()
        assert viewer.open_document(pdf) is True
        open_ms = (time.perf_counter() - t0) * 1000.0
        layout = viewer._pipeline.layout
        print(f"open (manager + pipeline + layout): {open_ms:.0f}ms, "
              f"pages={layout.page_count}, height={layout.total_height_px}px")

        t0 = time.perf_counter()
        _wait_until(lambda: viewer.total_painted_tiles > 0, what="first tile")
        print(f"first displayed tile: {(time.perf_counter() - t0) * 1000:.0f}ms")

        t0 = time.perf_counter()
        _wait_until(lambda: viewer.painted_placeholders == 0 and viewer.total_painted_tiles > 0,
                    what="full viewport")
        print(f"first viewport fully populated: {(time.perf_counter() - t0) * 1000:.0f}ms, "
              f"painted={viewer.total_painted_tiles}")
        rss_peak = max(rss_peak, get_rss_mb())

        # Scroll-handler cost on the GUI thread (update call only, no waiting).
        viewer._scrollbar.setValue(viewer._scrollbar.maximum() // 2)
        app.processEvents()
        t0 = time.perf_counter()
        for _ in range(20):
            viewer._request_update()
        handler_ms = (time.perf_counter() - t0) * 1000.0 / 20.0
        print(f"scroll update handler cost: {handler_ms:.3f}ms per call (GUI thread)")

        # Poll cost: one synchronous visible_ready + display-diff pass.
        t0 = time.perf_counter()
        for _ in range(20):
            viewer._poll()
        poll_ms = (time.perf_counter() - t0) * 1000.0 / 20.0
        print(f"GUI poll cost: {poll_ms:.3f}ms per pass")
        rss_peak = max(rss_peak, get_rss_mb())

        stats = viewer._pipeline.stats()
        cache = stats["cache"]
        print(f"painted tiles total={viewer.total_painted_tiles}, "
              f"placeholders total={viewer.total_painted_placeholders}")
        print(f"cache hits={cache['hits']} misses={cache['misses']} "
              f"evictions={cache['evictions']} resident={cache['resident_bytes'] // 1024}KiB")
        print(f"stale={stats['stale_discarded']} cancelled={stats['engine']['cancelled']} "
              f"dropped_prefetch={stats['engine']['dropped_prefetch']} "
              f"queue_full={stats['queue_full']}")

        viewer.close_document()
        window.close()
        app.processEvents()
        rss_end = get_rss_mb()
        print(f"RSS before={rss_start:.1f}MiB peak={max(rss_peak, rss_end):.1f}MiB "
              f"after={rss_end:.1f}MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
