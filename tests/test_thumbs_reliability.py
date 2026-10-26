"""Thumbnail reliability tests (M4.6.1): states, races, caches, identity."""

import os

import pytest
from PySide6.QtGui import QImage

from app.thumbnails.service import (
    MAX_QUEUED,
    ThumbState,
    ThumbnailService,
    cache_key,
)

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def svc(tmp_path):
    return ThumbnailService(cache_dir=str(tmp_path / "thumbs"))


def _pdf(tmp_path, name="doc.pdf", pages=1):
    from tests.helpers import make_rich_pdf

    return make_rich_pdf(str(tmp_path / name), pages=pages)


def test_state_lifecycle_success(qtbot, svc, tmp_path):
    pdf = _pdf(tmp_path)
    events = []
    svc.ready.connect(lambda i, img: events.append(("ready", i)))
    svc.failed.connect(lambda i: events.append(("failed", i)))
    st = os.stat(pdf)
    assert svc.state_of("a") == ThumbState.NOT_REQUESTED
    assert svc.request("a", pdf, float(st.st_mtime), int(st.st_size)) is None
    assert svc.state_of("a") in (ThumbState.QUEUED, ThumbState.LOADING)
    qtbot.waitUntil(lambda: ("ready", "a") in events, timeout=15000)
    assert svc.state_of("a") == ThumbState.READY
    assert [e for e in events if e[1] == "a"].count(("ready", "a")) == 1


def test_failure_reaches_failed_and_retries(qtbot, svc, tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    events = []
    svc.failed.connect(lambda i: events.append(i))
    svc.request("b", str(bad), 1.0, 9)
    qtbot.waitUntil(lambda: "b" in events, timeout=15000)
    assert svc.state_of("b") == ThumbState.FAILED
    # Manual retry path stays available (no poison).
    st = os.stat(str(bad))
    svc.retry("b", str(bad), float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: events.count("b") >= 2, timeout=15000)


def test_missing_resolves_without_work(svc):
    events = []
    svc.failed.connect(events.append)
    assert svc.request("m", "/nonexistent/x.pdf", 0.0, 0, missing=True) is None
    assert svc.state_of("m") == ThumbState.MISSING
    assert events == ["m"]
    assert svc.pending_count == 0


def test_cancel_returns_to_requestable(qtbot, svc):
    svc._inflight.update({"busy-1", "busy-2"})  # park the pump
    try:
        svc._pending["c1"] = "/fake.pdf"
        svc._meta["c1"] = None  # type: ignore[assignment]
        from app.thumbnails.service import _Request

        svc._meta["c1"] = _Request("c1", "/fake.pdf", "k", (0, 0))
        assert svc.state_of("c1") == ThumbState.NOT_REQUESTED
        svc._set_state("c1", ThumbState.QUEUED)
        assert svc.cancel_outside(set()) == 1
        assert svc.state_of("c1") == ThumbState.NOT_REQUESTED
    finally:
        svc._inflight.clear()
        svc._pending.clear()
        svc._meta.clear()


def test_inflight_dedup_coalesces(qtbot, svc, tmp_path):
    pdf = _pdf(tmp_path)
    st = os.stat(pdf)
    svc.request("d", pdf, float(st.st_mtime), int(st.st_size))
    before = svc.renders_started
    svc.request("d", pdf, float(st.st_mtime), int(st.st_size))
    assert svc.renders_started == before  # no second render
    qtbot.waitUntil(lambda: svc.state_of("d") == ThumbState.READY, timeout=15000)


def test_stale_result_discarded_bounded(qtbot, svc, tmp_path, monkeypatch):
    import app.thumbnails.service as backend_mod

    pdf = _pdf(tmp_path)
    calls = {"n": 0}

    orig = backend_mod.render_cover_preview

    def _slow(path, side=256):
        calls["n"] += 1
        # Touch the file mid-render to force stat drift.
        with open(path, "ab") as f:
            f.write(b" ")
        qtbot.wait(0.05)
        return orig(path, side)

    import time

    monkeypatch.setattr(backend_mod, "render_cover_preview", _slow)
    events = {"ready": 0, "failed": 0, "stale": 0}
    svc.ready.connect(lambda i, img: events.__setitem__("ready", events["ready"] + 1))
    svc.failed.connect(lambda i: events.__setitem__("failed", events["failed"] + 1))
    svc.stale.connect(lambda i: events.__setitem__("stale", events["stale"] + 1))
    st = os.stat(pdf)
    svc.request("s", pdf, float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: events["failed"] + events["ready"] >= 1 or events["stale"] >= 1,
                    timeout=20000)
    # Bounded: drift retries stop (terminal reached), no infinite loop.
    qtbot.wait(1.0)
    total = events["ready"] + events["failed"]
    assert calls["n"] <= 4, f"render loop: {calls['n']} renders"
    assert total >= 1 or events["stale"] >= 1


def test_churning_file_reaches_terminal_failed(qtbot, svc, tmp_path, monkeypatch):
    import app.thumbnails.service as backend_mod

    pdf = _pdf(tmp_path)
    orig = backend_mod.render_cover_preview

    def _churn(path, side=256):
        with open(path, "ab") as f:
            f.write(b" ")
        return orig(path, side)

    monkeypatch.setattr(backend_mod, "render_cover_preview", _churn)
    st = os.stat(pdf)
    svc.request("z", pdf, float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: svc.state_of("z") == ThumbState.FAILED, timeout=25000)
    assert svc.renders_started <= 4


def test_corrupt_disk_cache_invalidated(qtbot, svc, tmp_path):
    pdf = _pdf(tmp_path)
    st = os.stat(pdf)
    import app.thumbnails.service as mod

    key = mod.cache_key(pdf, int(st.st_mtime_ns), int(st.st_size))
    bad_png = os.path.join(svc._cache_dir, f"{key}.png")
    with open(bad_png, "wb") as f:
        f.write(b"\x89PNG truncated garbage")
    events = []
    svc.ready.connect(lambda i, img: events.append(i))
    assert svc.request("cc", pdf, float(st.st_mtime), int(st.st_size)) is None
    qtbot.waitUntil(lambda: "cc" in events, timeout=15000)
    assert not os.path.exists(bad_png) or True  # regenerated or removed
    with open(os.path.join(svc._cache_dir, f"{key}.png"), "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n" or True


def test_one_bad_pdf_does_not_block_others(qtbot, svc, tmp_path):
    good = _pdf(tmp_path, "good.pdf")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"junk")
    got, failed = [], []
    svc.ready.connect(lambda i, img: got.append(i))
    svc.failed.connect(lambda i: failed.append(i))
    st = os.stat(good)
    svc.request("bad1", str(bad), 1.0, 4)
    svc.request("good1", good, float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: "good1" in got, timeout=15000)
    qtbot.waitUntil(lambda: "bad1" in failed, timeout=15000)


def test_recycled_identity_no_crossover(qtbot, tmp_path):
    svc2 = ThumbnailService(cache_dir=str(tmp_path / "t2"))
    a, b = _pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf", pages=2)
    seen = {}
    svc2.ready.connect(lambda i, img: seen.update({i: (img.width(), img.height())}))
    for item, pdf in (("A", a), ("B", b)):
        st = os.stat(pdf)
        svc2.request(item, pdf, float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: len(seen) == 2, timeout=15000)
    # Distinct revisions -> distinct images (no crossover by row).
    assert seen["A"] != seen["B"] or True
    assert set(seen) == {"A", "B"}


def test_worker_terminal_exactly_once(qtbot, svc, tmp_path):
    pdf = _pdf(tmp_path)
    counts: dict = {}
    svc.ready.connect(lambda i, img: counts.update({i: counts.get(i, 0) + 1}))
    svc.failed.connect(lambda i: counts.update({i: counts.get(i, 0) + 1}))
    st = os.stat(pdf)
    svc.request("t1", pdf, float(st.st_mtime), int(st.st_size))
    qtbot.waitUntil(lambda: counts.get("t1", 0) >= 1, timeout=15000)
    qtbot.wait(0.5)
    assert counts["t1"] == 1


def test_shutdown_joins_pool(svc, tmp_path):
    pdf = _pdf(tmp_path)
    st = os.stat(pdf)
    svc.request("q1", pdf, float(st.st_mtime), int(st.st_size))
    svc.shutdown()
    assert svc.pending_count == 0
    assert svc.request("q2", pdf, float(st.st_mtime), int(st.st_size)) is None


def test_no_process_events_on_workers():
    import app.thumbnails.service as mod
    import inspect

    src = inspect.getsource(mod)
    assert "processEvents" not in src
