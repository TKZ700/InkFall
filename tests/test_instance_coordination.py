"""Cross-instance same-file coordination (registry + hub + window wiring).

Coverage classes, explicitly labeled:

- *Simulated multi-instance* — two full hub/registry stacks inside ONE
  process over a real QLocalServer/QLocalSocket transport; the peer
  window is a scripted stub, so these prove the protocol, not the peer's
  window logic.
- *Window wiring* — a real offscreen MainWindow whose release-request
  handler and save-time coordination are driven directly.
- *Genuine multi-process* — a subprocess peer running its own
  QApplication and hub; the test talks to it over the real transport.

No test kills a process it did not create, and no real document outside
``tmp_path`` is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.instance_registry import (
    InstanceRecord,
    instances_dir,
    normalize_key,
    pid_alive,
    prune_stale,
    remove_instance_file,
    scan_instances,
    write_instance,
)

pytestmark = pytest.mark.usefixtures("qapp")

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# registry units (Qt-free)
# ---------------------------------------------------------------------------


def test_registry_write_scan_and_prune(tmp_path: Path) -> None:
    """Alive peers are listed; dead peers' files are pruned on sight."""
    directory = instances_dir(tmp_path / "reg")
    own = write_instance(directory, os.getpid(), "inkfall-1-a",
                         [[normalize_key("C:/books/a.pdf"), True]])
    dead_pid = _dead_pid()
    dead = write_instance(directory, dead_pid, "inkfall-2-b",
                          [[normalize_key("C:/books/a.pdf"), False]])

    records = scan_instances(directory)
    assert [r.server for r in records] == ["inkfall-1-a"], (
        "a dead peer must not be listed as a coordination target")
    assert records[0].docs == ((normalize_key("C:/books/a.pdf"), True),)
    assert not (Path(directory) / dead).exists(), (
        "a dead peer's stale registry file must be pruned")
    assert (Path(directory) / own).exists()

    # Corrupt files are ignored, never fatal.
    (Path(directory) / "instance-junk.json").write_text("{not json", "utf-8")
    assert len(scan_instances(directory)) == 1

    remove_instance_file(directory, own)
    assert not (Path(directory) / own).exists()
    assert prune_stale(directory) == 0


def test_pid_alive_never_signals(tmp_path: Path) -> None:
    """Liveness is read-only: self is alive, a dead PID is not."""
    assert pid_alive(os.getpid()) is True
    assert pid_alive(_dead_pid()) is False
    assert pid_alive(-1) is False
    assert pid_alive("not-a-pid") is False  # type: ignore[arg-type]


def _dead_pid() -> int:
    """A PID that has definitely exited (spawn and reap a short child)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.05)"])
    proc.wait(timeout=30)
    return proc.pid


# ---------------------------------------------------------------------------
# simulated multi-instance (real transport, scripted peer, one process)
# ---------------------------------------------------------------------------


def test_hub_round_trip_released(tmp_path: Path) -> None:
    """A peer that releases answers ``released`` over the real transport."""
    from app.instance_hub import InstanceHub

    directory = tmp_path / "hub-a"
    hub_a = InstanceHub(directory=directory)
    hub_b = InstanceHub(directory=directory)
    key = normalize_key(str(tmp_path / "book.pdf"))
    hub_b.publish([(key, False)])
    hub_b.releaseRequested.connect(
        lambda request_id, _path: hub_b.respond(request_id, "released"))

    assert scan_instances(hub_b.directory)[0].docs == ((key, False),)
    status = hub_a.request_release(
        hub_b.server_name, key, timeout_s=10)
    assert status == "released"
    hub_a.close()
    hub_b.close()
    assert not list(Path(directory).glob("instance-*.json")), (
        "close() must remove each window's registry file")


def test_hub_refusal_and_silence(tmp_path: Path) -> None:
    """Refusal propagates; a silent peer times out to ``None``."""
    from app.instance_hub import InstanceHub

    directory = tmp_path / "hub-b"
    hub_a = InstanceHub(directory=directory)
    refuser = InstanceHub(directory=directory)
    refuser.releaseRequested.connect(
        lambda request_id, _path: refuser.respond(request_id, "refused"))
    silent = InstanceHub(directory=directory)
    key = normalize_key(str(tmp_path / "book.pdf"))

    assert hub_a.request_release(
        refuser.server_name, key, timeout_s=10) == "refused"
    assert hub_a.request_release(
        silent.server_name, key, timeout_s=1.5) is None, (
        "a peer that never answers must time out to None, never hang "
        "the save")
    for hub in (hub_a, refuser, silent):
        hub.close()


def test_hub_ignores_malformed_lines(tmp_path: Path) -> None:
    """Garbage on the socket never crashes the hub or the responder."""
    from app.instance_hub import InstanceHub
    from PySide6.QtNetwork import QLocalSocket

    directory = tmp_path / "hub-c"
    hub = InstanceHub(directory=directory)
    requests: list[str] = []
    hub.releaseRequested.connect(lambda rid, path: requests.append(rid))

    junk = QLocalSocket()
    junk.connectToServer(hub.server_name)
    assert junk.waitForConnected(2000)
    junk.write(b"{not json}\n\n\x00\xff\n" + json.dumps({
        "type": "release-request", "request_id": "ok-1",
        "path": normalize_key(str(tmp_path / "x.pdf")),
    }).encode("utf-8") + b"\n")
    junk.flush()
    deadline = time.monotonic() + 5
    while not requests and time.monotonic() < deadline:
        from PySide6.QtCore import QCoreApplication, QEventLoop

        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)
    assert requests == ["ok-1"], (
        "malformed lines must be skipped; the valid request must land")
    junk.disconnectFromServer()
    hub.close()


# ---------------------------------------------------------------------------
# window wiring (real MainWindow, offscreen)
# ---------------------------------------------------------------------------


@pytest.fixture()
def shell(qtbot, monkeypatch, tmp_path):
    """A MainWindow shell (test_save_recovery pattern)."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.main_window import MainWindow
    from document.manager import DocumentManager
    from library.service import LibraryService
    from library.store import LibraryStore

    def _exec_never_blocks(self):
        button = self.escapeButton() or self.defaultButton()
        if button is not None:
            button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", _exec_never_blocks)

    app = QApplication.instance()
    if app is not None:
        app.setOrganizationName("Inkfall")
        app.setApplicationName("Inkfall")
    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    service = LibraryService(store)
    window = MainWindow(DocumentManager(), library_service=service)
    window.show()
    qtbot.addWidget(window)
    yield window
    try:
        window._leave_guard_passed = True
        window.close()
    except Exception:
        pass
    try:
        window._viewer.close_document()
    except Exception:
        pass
    try:
        for doc in list(window._manager.documents()):
            try:
                window._manager.close(doc.id)
            except Exception:
                pass
    except Exception:
        pass
    try:
        window._switch_pool.waitForDone(10000)
    except Exception:
        pass
    try:
        window._save_pool.waitForDone(10000)
    except Exception:
        pass
    try:
        window._instance_hub.close()
    except Exception:
        pass
    store.close()


def _open_document(window, tmp_path: Path, name: str = "book.pdf") -> str:
    from tests.helpers import make_pdf

    pdf = make_pdf(str(tmp_path / name), pages=2)
    assert window._viewer.open_document(pdf) is True
    return pdf


def test_window_releases_clean_sessions_silently(
        shell, tmp_path: Path, monkeypatch) -> None:
    """A clean document (and its read-only duplicate) closes without UI."""
    pdf = _open_document(shell, tmp_path)
    assert shell._open_readonly_tab(pdf) is True
    assert len(shell._tab_sessions) == 2

    answered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        shell._instance_hub, "respond",
        lambda request_id, status: answered.append((request_id, status)))
    shell._on_instance_release_request("req-1", pdf)

    assert answered == [("req-1", "released")]
    assert shell._tab_sessions == [], (
        "a released window must have closed every session of the file")
    assert list(shell._manager.documents()) == []
    assert shell._manager._by_path == {}, (
        "the primary's path registration must be gone after release")


def test_window_refuses_release_for_dirty_document(
        shell, tmp_path: Path, monkeypatch) -> None:
    """Cancel on the unsaved-changes guard refuses and preserves edits."""
    from annotations.models import InkStroke
    from core.types import Point
    from document.models import page_id_for

    pdf = _open_document(shell, tmp_path)
    doc = shell._viewer.current_document
    doc.annotations.add(
        InkStroke(points=(Point(100.0, 100.0), Point(140.0, 140.0)),
                  page_id=page_id_for(doc.id, 0)),
        0)
    doc.mark_changed()

    answered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        shell._instance_hub, "respond",
        lambda request_id, status: answered.append((request_id, status)))
    shell._on_instance_release_request("req-2", pdf)

    assert answered == [("req-2", "refused")], (
        "the defused guard presses Cancel: the peer keeps its edits")
    assert len(shell._tab_sessions) == 1
    assert shell._viewer.current_document is doc
    assert doc.is_dirty
    assert len([a for _p, a in doc.annotations.iter_all()]) == 1


def test_coordinate_save_start_refusal_timeout_and_success(
        shell, tmp_path: Path, monkeypatch) -> None:
    """Peer refusal and wait-timeout raise SaveLockedError; release passes."""
    from document.backend import SaveLockedError

    pdf = _open_document(shell, tmp_path)
    key = normalize_key(pdf)
    peer = InstanceRecord(pid=os.getpid(), server="inkfall-9999-peer",
                          docs=((key, False),), file=Path("ignored.json"))

    calls: list[str] = []

    def _fake_scan(_directory):
        return [peer]

    def _fake_release(server_name, path, *, timeout_s=60.0,
                      request_id=None):
        calls.append(server_name)
        return _statuses.pop(0)

    monkeypatch.setattr("app.main_window.scan_instances", _fake_scan)
    monkeypatch.setattr(shell._instance_hub, "request_release",
                        _fake_release)

    _statuses = ["refused"]
    with pytest.raises(SaveLockedError):
        shell._coordinate_save_start(pdf)
    assert calls == ["inkfall-9999-peer"]

    _statuses = [None]
    with pytest.raises(SaveLockedError):
        shell._coordinate_save_start(pdf)

    _statuses = ["released"]
    shell._coordinate_save_start(pdf)

    # An unrelated peer file must not be asked at all.
    peer_unrelated = InstanceRecord(
        pid=os.getpid(), server="inkfall-9999-other",
        docs=((normalize_key(str(tmp_path / "other.pdf")), False),),
        file=Path("ignored.json"))
    monkeypatch.setattr("app.main_window.scan_instances",
                        lambda _directory: [peer_unrelated])
    monkeypatch.setattr(
        shell._instance_hub, "request_release",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("unrelated peer must not be asked")))
    shell._coordinate_save_start(pdf)


def test_coordinate_save_start_skips_while_releasing(
        shell, monkeypatch) -> None:
    """The peer's own release-save must not re-coordinate (deadlock)."""
    monkeypatch.setattr(
        "app.main_window.scan_instances",
        lambda _directory: (_ for _ in ()).throw(
            AssertionError("release saves must not scan for peers")))
    shell._instance_release_saving = True
    shell._coordinate_save_start("C:/whatever/book.pdf")


def test_window_closes_hub_on_exit(shell) -> None:
    """closeEvent removes the window's registry file."""
    directory = Path(shell._instance_hub.directory)
    own = [p for p in directory.glob("instance-*.json")]
    assert own, "the window must advertise itself while alive"
    shell._instance_hub.close()
    assert not list(directory.glob("instance-*.json")), (
        "closeEvent must retire the instance registry file")


# ---------------------------------------------------------------------------
# genuine multi-process (subprocess peer, real transport)
# ---------------------------------------------------------------------------

_PEER_SCRIPT = r"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.environ["INKFALL_REPO_ROOT"])

from app.instance_hub import InstanceHub
from core.instance_registry import normalize_key

policy, pdf, directory = sys.argv[1], sys.argv[2], sys.argv[3]

app = QApplication([])
hub = InstanceHub(directory=directory)
hub.publish([(normalize_key(pdf), policy == "dirty")])


def _on_request(request_id, _path):
    if policy == "silence":
        return  # never answer: the saver must time out
    hub.respond(request_id, "released" if policy == "release" else "refused")


hub.releaseRequested.connect(_on_request)
QTimer.singleShot(30000, app.quit)  # never leak the peer on a hung test
app.exec()
hub.close()
"""


def _start_peer(tmp_path: Path, pdf: str, policy: str) -> tuple:
    directory = str(tmp_path / f"peer-{policy}")
    script = tmp_path / f"peer_{policy}.py"
    script.write_text(_PEER_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["INKFALL_REPO_ROOT"] = str(REPO_ROOT)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    proc = subprocess.Popen(
        [sys.executable, str(script), policy, pdf, directory],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        cwd=str(REPO_ROOT))
    key = normalize_key(pdf)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        records = scan_instances(directory)
        if any(any(p == key for p, _d in r.docs) for r in records):
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        proc.wait(timeout=15)
        raise AssertionError(
            f"peer ({policy}) never advertised the document; "
            f"stderr: {proc.stderr.read() if proc.stderr else b''}")
    server = [r.server for r in scan_instances(directory)
              if any(p == key for p, _d in r.docs)][0]
    return proc, directory, server


def test_real_second_process_releases_the_file(tmp_path: Path) -> None:
    """Genuine multi-process: a live peer window releases on request."""
    from app.instance_hub import InstanceHub
    from tests.helpers import make_pdf

    pdf = make_pdf(str(tmp_path / "shared.pdf"), pages=1)
    proc, directory, server = _start_peer(tmp_path, pdf, "release")
    hub = InstanceHub(directory=directory)
    try:
        assert hub.request_release(
            server, normalize_key(pdf), timeout_s=15) == "released"
    finally:
        hub.close()
        proc.terminate()
        proc.wait(timeout=15)


def test_real_second_process_refusal_and_silence(tmp_path: Path) -> None:
    """Genuine multi-process: refusal propagates, silence times out."""
    from app.instance_hub import InstanceHub
    from tests.helpers import make_pdf

    pdf = make_pdf(str(tmp_path / "shared2.pdf"), pages=1)
    hub = InstanceHub(directory=str(tmp_path / "parent"))
    try:
        proc, directory, server = _start_peer(tmp_path, pdf, "refuse")
        try:
            assert hub.request_release(
                server, normalize_key(pdf), timeout_s=15) == "refused"
        finally:
            proc.terminate()
            proc.wait(timeout=15)

        proc, directory, server = _start_peer(tmp_path, pdf, "silence")
        try:
            assert hub.request_release(
                server, normalize_key(pdf), timeout_s=3) is None, (
                "an unresponsive peer must hit the bounded wait")
        finally:
            proc.terminate()
            proc.wait(timeout=15)
    finally:
        hub.close()
