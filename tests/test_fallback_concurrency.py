"""M3 Gate 5 regression: bound concurrent in-process fallback renders.

Failure class: cold/high-zoom navigation marks decoder groups stale or
withdrawn, so tiles fall back to per-tile engine renders. Up to
``resolved_workers()`` of those raster sections run concurrently, hold
the GIL inside MuPDF, and starve the GUI thread for seconds (M3
Finding F-1). The fix serializes the raster section behind
``EngineConfig.max_concurrent_renders`` (1 when process decoding is
active; legacy mode untouched).

Deterministic by construction: the ``max_active_renders`` high-water
mark is asserted directly — never timing sleeps for correctness.
"""

from __future__ import annotations

from core.types import DocumentId
from rendering.cache import cache_identity
from rendering.engine import RendererEngine
from rendering.pipeline import DocumentPipeline, _fallback_gate_config
from rendering.scheduler import EngineConfig
from rendering.types import TileRect
from tests.helpers import make_dense_pdf
from tests.test_pipeline import _drain, _pipe
from tests.test_render_engine import _req


def _tile_reqs(pdf: str, n: int):
    """N distinct tile requests (distinct keys => no dedup merging)."""
    out = []
    for i in range(n):
        out.append(
            _req(
                pdf,
                page_index=0,
                tile=TileRect((i * 173) % 339, (i * 211) % 586, 256, 256),
            )
        )
    return out


# -- config derivation -----------------------------------------------------

def test_config_default_preserves_historical_concurrency():
    cfg = EngineConfig()
    assert cfg.max_concurrent_renders == 0
    assert cfg.resolved_max_concurrent_renders() == cfg.resolved_workers()


def test_explicit_bound_resolves_verbatim():
    assert EngineConfig(max_concurrent_renders=1).resolved_max_concurrent_renders() == 1
    assert EngineConfig(max_concurrent_renders=3).resolved_max_concurrent_renders() == 3


def test_pipeline_derives_bound_only_when_proc_active(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "d.pdf"))
    doc = DocumentId("gate")
    sizes = [(595.0, 842.0, 0)]
    caller = EngineConfig(worker_count=2)
    proc_pipe = _pipe(pdf, sizes, engine_config=caller, process_decode_workers=2)
    try:
        assert proc_pipe._engine.config.max_concurrent_renders == 1
        assert caller.max_concurrent_renders == 0  # caller config never mutated
    finally:
        proc_pipe.close()
    legacy = _pipe(pdf, sizes, engine_config=EngineConfig(worker_count=2),
                   process_decode_workers=0)
    try:
        assert legacy._engine.config.max_concurrent_renders == 0
    finally:
        legacy.close()
    explicit = _pipe(
        pdf, sizes,
        engine_config=EngineConfig(worker_count=2, max_concurrent_renders=3),
        process_decode_workers=2,
    )
    try:
        assert explicit._engine.config.max_concurrent_renders == 3
    finally:
        explicit.close()


def test_fallback_gate_config_helper():
    assert _fallback_gate_config(None, 2).max_concurrent_renders == 1
    assert _fallback_gate_config(None, 1).max_concurrent_renders == 1
    assert _fallback_gate_config(None, 0).max_concurrent_renders == 0
    assert _fallback_gate_config(
        EngineConfig(max_concurrent_renders=2), 2).max_concurrent_renders == 2


# -- engine-level serialization --------------------------------------------

def test_bound_one_engine_serializes_renders_byte_identical(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "e.pdf"))
    reqs = _tile_reqs(pdf, 8)
    gated = RendererEngine(EngineConfig(worker_count=4, max_concurrent_renders=1))
    try:
        futs = [gated.submit(r) for r in reqs]
        gated_results = [f.result(timeout=120) for f in futs]
    finally:
        gated.shutdown()
    plain = RendererEngine(EngineConfig(worker_count=4))
    try:
        futs = [plain.submit(r) for r in reqs]
        plain_results = [f.result(timeout=120) for f in futs]
    finally:
        plain.shutdown()
    assert all(r.success for r in gated_results)
    for g, p in zip(gated_results, plain_results):
        assert (g.width_px, g.height_px, g.stride) == (p.width_px, p.height_px, p.stride)
        assert bytes(g.data) == bytes(p.data)
    assert gated.metrics()["max_active_renders"] <= 1
    assert gated.metrics()["max_active_renders"] >= 1
    assert gated.metrics()["active_renders"] == 0


# -- pipeline fallback storm ----------------------------------------------

def _seed_failed_group(pipe: DocumentPipeline, page: int = 0) -> None:
    plan = pipe.visible_plan(0.0, 600, 900)
    assert plan, "expected planned tiles for the seed viewport"
    group = (int(page), float(pipe.raster_scale), 0, int(plan[0].generation))
    with pipe._lock:
        pipe._decode_failed.add(group)


def test_pipeline_fallback_storm_bounded_and_complete(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "f.pdf"))
    sizes = [(595.0, 842.0, 0), (595.0, 842.0, 0)]
    pipe = _pipe(pdf, sizes, process_decode_workers=2)
    try:
        _seed_failed_group(pipe)
        pipe.update(0.0, 600, 900)
        stats = _drain(pipe)
        eng = stats["engine"]
        assert eng["max_active_renders"] <= 1
        assert stats["in_flight"] == 0
        assert eng["pending"] == 0
        ready = pipe.visible_ready(0.0, 600, 900)
        planned = pipe.visible_plan(0.0, 600, 900)
        assert len(ready) == len(planned) > 0
        assert {cache_identity(t.key) for t in ready} == {
            cache_identity(k) for k in planned
        }
    finally:
        pipe.close()


def test_pipeline_fallback_bytes_match_legacy_engine(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "g.pdf"))
    sizes = [(595.0, 842.0, 0), (595.0, 842.0, 0)]
    gated = _pipe(pdf, sizes, process_decode_workers=2)
    legacy = _pipe(pdf, sizes, process_decode_workers=0)
    try:
        _seed_failed_group(gated)
        gated.update(0.0, 600, 900)
        _drain(gated)
        legacy.update(0.0, 600, 900)
        _drain(legacy)
        g_ready = {cache_identity(t.key): bytes(t.data)
                   for t in gated.visible_ready(0.0, 600, 900)}
        l_ready = {cache_identity(t.key): bytes(t.data)
                   for t in legacy.visible_ready(0.0, 600, 900)}
        assert g_ready.keys() == l_ready.keys() and g_ready
        for key in g_ready:
            assert g_ready[key] == l_ready[key]
    finally:
        gated.close()
        legacy.close()


def test_epoch_bump_during_fallback_storm_resolves(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "h.pdf"))
    sizes = [(595.0, 842.0, 0), (595.0, 842.0, 0)]
    pipe = _pipe(pdf, sizes, process_decode_workers=2)
    try:
        _seed_failed_group(pipe)
        pipe.update(0.0, 600, 900)
        pipe.set_zoom(1.5)
        pipe.update(0.0, 600, 900)
        stats = _drain(pipe)
        assert stats["engine"]["max_active_renders"] <= 1
        assert stats["in_flight"] == 0
        assert stats["engine"]["pending"] == 0
        ready = pipe.visible_ready(0.0, 600, 900)
        planned = pipe.visible_plan(0.0, 600, 900)
        assert len(ready) == len(planned) > 0
    finally:
        pipe.close()


def test_close_during_bounded_fallback_renders(tmp_path):
    pdf = make_dense_pdf(str(tmp_path / "i.pdf"))
    sizes = [(595.0, 842.0, 0), (595.0, 842.0, 0)]
    pipe = _pipe(pdf, sizes, process_decode_workers=2)
    try:
        _seed_failed_group(pipe)
        pipe.update(0.0, 600, 900)
        pipe.close()  # must not hang on the render gate
        pipe.close()  # idempotent
        assert pipe.stats()["engine"]["max_active_renders"] <= 1
    finally:
        pipe.close()
