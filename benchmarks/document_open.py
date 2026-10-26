"""M1 baseline benchmark: document open lifecycle (no rendering).

Run with::

    uv run python -m benchmarks.document_open

Measures cold open, page-count access, first/random page metadata access,
close, and RSS before/after open/after close. Baseline for later
milestones — do not optimize from these numbers alone.
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diagnostics import get_rss_mb
from document.manager import DocumentManager
from tests.helpers import make_pdf


def _ms(seconds: float) -> float:
    return seconds * 1000.0


def main() -> int:
    random.seed(42)
    with tempfile.TemporaryDirectory(prefix="inkfall-bench-") as tmp:
        pdf = make_pdf(str(Path(tmp) / "bench.pdf"), pages=100)
        mgr = DocumentManager()
        rss_before = get_rss_mb()

        t0 = time.perf_counter()
        doc = mgr.open(pdf)
        cold_open = time.perf_counter() - t0
        rss_after_open = get_rss_mb()

        t0 = time.perf_counter()
        count = doc.page_count
        page_count_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        first = doc.page_info(0)
        first_page_t = time.perf_counter() - t0

        indices = [random.randrange(count) for _ in range(20)]
        t0 = time.perf_counter()
        for i in indices:
            doc.page_info(i)
        random_pages_t = time.perf_counter() - t0

        _ = doc.metadata
        _ = doc.info()

        t0 = time.perf_counter()
        mgr.close(doc.id)
        close_t = time.perf_counter() - t0
        rss_after_close = get_rss_mb()

        print("inkfall document_open benchmark (M1 baseline)")
        print(f"  pages:                 {count}")
        print(f"  first page size:       {first.width:.1f} x {first.height:.1f} pt")
        print(f"  cold open:             {_ms(cold_open):.2f} ms")
        print(f"  page_count access:     {_ms(page_count_t):.3f} ms")
        print(f"  first page_info:       {_ms(first_page_t):.3f} ms")
        print(f"  20x random page_info:  {_ms(random_pages_t):.2f} ms")
        print(f"  close:                 {_ms(close_t):.2f} ms")
        print(f"  RSS before open:       {rss_before:.1f} MiB")
        print(f"  RSS after open:        {rss_after_open:.1f} MiB")
        print(f"  RSS after close:       {rss_after_close:.1f} MiB")
        print(f"  RSS delta open-close:  {rss_after_close - rss_before:+.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
