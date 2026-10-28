"""Large-PDF save benchmark: per-stage timing of the UI-equivalent save.

Run with::

    uv run python -m benchmarks.save_benchmark [--pages 18] [--source PATH]
        [--json OUT.json]

Generates a seeded, image-heavy synthetic PDF (random-noise images, which
deflate/PNG barely compress) and then runs exactly the stage sequence the
MainWindow save path runs, headless and without Qt::

    begin_save -> prepare_for_save (materialize) -> pipeline rebind
    -> write_export in the real spawned save child -> complete_save

The document and pipeline stay open across two saves, mirroring real
usage: save #1 is the first save (materialize + decoder rebind + child
export), save #2 reuses the session so the renderer is already on the
work copy (no rebind). Progress stage arrivals (parent
``snapshot``/``replace``, child ``annotations``/``write``/``verify``) and
the child's returned ``timings`` payload give the per-stage breakdown.
Output reopens and reports page/manifest integrity.
``--source`` swaps the synthetic fixture for a representative existing
PDF; keep fixture seed and page count fixed when comparing baseline vs.
changed trees — these are local numbers, never CI claims.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diagnostics import get_rss_mb
from document.backend import enumerate_page_infos
from document.manager import DocumentManager
from rendering.pipeline import DocumentPipeline

IMG_W, IMG_H = 1200, 1600  # x3 bytes ≈ 5.76 MB raw per page image


def build_large_pdf(path: str, pages: int, seed: int = 0) -> str:
    """Deterministic image-heavy PDF; random bytes stay incompressible."""
    import numpy as np
    import pymupdf

    rng = np.random.default_rng(seed)
    doc = pymupdf.open()
    try:
        for i in range(pages):
            samples = rng.integers(
                0, 256, IMG_W * IMG_H * 3, dtype=np.uint8).tobytes()
            pix = pymupdf.Pixmap(pymupdf.csRGB, IMG_W, IMG_H, samples, False)
            page = doc.new_page(width=595.0, height=842.0)
            page.insert_image(pymupdf.Rect(36, 36, 559, 806), pixmap=pix)
            page.insert_text((72.0, 24.0), f"Inkfall save benchmark p{i + 1}")
        doc.set_metadata({"title": "Inkfall Save Benchmark", "author": "Inkfall"})
        doc.save(path)
    finally:
        doc.close()
    return path


def _one_save(manager: DocumentManager, doc, pipeline: DocumentPipeline,
              target: str) -> dict:
    """One begin->complete save of an open document, fully timed."""
    rss_before = get_rss_mb()
    rss_seen = [rss_before]
    stages: list[tuple[float, str]] = []
    child_payload: dict = {}

    def progress(name: str) -> None:
        stages.append((time.perf_counter(), name))

    def pump(poll):
        while True:
            done, payload = poll()
            if payload.get("stage"):
                progress(f"child:{payload['stage']}")
            if done:
                child_payload.update(payload)
                return payload
            rss_seen.append(get_rss_mb())
            time.sleep(0.01)

    t0 = time.perf_counter()
    path_key, key = manager.begin_save(doc, target)
    t_begin = time.perf_counter() - t0

    t0 = time.perf_counter()
    prepared = doc.prepare_for_save(target)
    t_prepare = time.perf_counter() - t0

    t_rebind = 0.0
    if pipeline.source_path != doc.render_path:
        t0 = time.perf_counter()
        pipeline.rebind_source(doc.render_path)
        t_rebind = time.perf_counter() - t0

    t0 = time.perf_counter()
    doc.write_export(target, progress=progress, pump=pump)
    t_export = time.perf_counter() - t0

    t0 = time.perf_counter()
    manager.complete_save(doc, target, path_key, key)
    t_complete = time.perf_counter() - t0

    rss_seen.append(get_rss_mb())
    result: dict = {
        "prepared_materialized": bool(prepared),
        "rebound": t_rebind > 0.0,
        "begin_save_s": round(t_begin, 3),
        "prepare_materialize_s": round(t_prepare, 3),
        "rebind_s": round(t_rebind, 3),
        "export_total_s": round(t_export, 3),
        "complete_save_s": round(t_complete, 3),
        "total_s": round(
            t_begin + t_prepare + t_rebind + t_export + t_complete, 3),
        "parent_rss_peak_delta_mb": round(max(rss_seen) - rss_before, 1),
        "output_bytes": os.path.getsize(target),
        "stage_arrivals": stages,
        "child_payload": {
            k: v for k, v in child_payload.items() if k != "temp"},
    }

    integrity: dict = {"reopens": False}
    try:
        from annotations.persistence import EMBEDDED_NAME
        import pymupdf

        check = pymupdf.open(target)
        integrity["reopens"] = True
        integrity["pages"] = len(check)
        integrity["expected_pages"] = doc.page_count
        integrity["manifest_present"] = (
            EMBEDDED_NAME in list(check.embfile_names() or ()))
        check.close()
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        integrity["error"] = str(exc)
    result["integrity"] = integrity
    return result


def run_document_cycle(source: str) -> dict:
    """Open once, save twice — first-save and already-materialized path."""
    manager = DocumentManager()
    doc = manager.open(source)
    pages = enumerate_page_infos(doc.render_path, doc.id)
    pipeline = DocumentPipeline(
        doc.id, doc.render_path, pages, process_decode_workers=2)
    pipeline.prewarm()
    try:
        return {
            "source_bytes": os.path.getsize(source),
            "first_save": _one_save(manager, doc, pipeline, source),
            "second_save": _one_save(manager, doc, pipeline, source),
        }
    finally:
        pipeline.close()
        manager.close(doc.id)


def _print_save(label: str, result: dict) -> None:
    print(f"  total: {result['total_s']}s "
          f"(begin {result['begin_save_s']}s, "
          f"prepare/materialize {result['prepare_materialize_s']}s "
          f"[materialized={result['prepared_materialized']}], "
          f"rebind {result['rebind_s']}s "
          f"[rebound={result['rebound']}], "
          f"export {result['export_total_s']}s, complete "
          f"{result['complete_save_s']}s)")
    arrivals = result["stage_arrivals"]
    if arrivals:
        base = arrivals[0][0]
        prev = base
        print("  stages (arrival offset / since previous):")
        for at, name in arrivals:
            print(f"    {name:<18} +{at - base:7.3f}s "
                  f"(+{at - prev:6.3f}s)")
            prev = at
    timings = result["child_payload"].get("timings")
    if timings:
        print(f"  child timings: {timings}")
    print(f"  output: {result['output_bytes'] / 1e6:.1f} MB, "
          f"parent peak RSS delta {result['parent_rss_peak_delta_mb']} MB")
    print(f"  integrity: {result['integrity']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=18)
    parser.add_argument("--source", default=None,
                        help="representative existing PDF instead of the "
                             "synthetic fixture")
    parser.add_argument("--json", default=None, help="write results JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="inkfall-save-bench-") as tmp:
        if args.source:
            source = args.source
            fixture = "representative"
        else:
            t0 = time.perf_counter()
            source = build_large_pdf(
                str(Path(tmp) / "large.pdf"), pages=args.pages)
            fixture = (f"synthetic {args.pages} pages, seed 0, "
                       f"built in {time.perf_counter() - t0:.1f}s")
        print(f"fixture: {fixture}")
        print(f"input: {os.path.getsize(source) / 1e6:.1f} MB — {source}")

        cycle = run_document_cycle(source)
        print("\n== save #1 (first save: materialize + rebind + child) ==")
        _print_save("first", cycle["first_save"])
        print("\n== save #2 (same session: renderer already on work copy) ==")
        _print_save("second", cycle["second_save"])

        if args.json:
            Path(args.json).write_text(
                json.dumps({"fixture": fixture, "cycle": cycle}, indent=2))
            print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
