"""Color-picker diagnostics regression tests (release issue 2).

The packaged-build color-picker crash dies silently (windowed build, no
console, no crash reporter), and it could not be reproduced offscreen —
real QML click delivery with the production software backend opens,
closes and reopens the picker without a fault. These tests pin the two
mitigations that turn the next occurrence into actionable evidence:

1. The packaged self test must instantiate the real picker card
   (QQuickWidget + bridges), not merely compile the QML, so a
   frozen-build hosting defect fails the CI smoke step with details.
2. Fatal crashes must leave a diagnostics file with the faulting
   thread's stack (faulthandler), removed again on clean exit.

Root cause identified from the user's packaged crash dump
(``inkfall-fatal-12176.log``: "Windows fatal exception: stack overflow",
``mouseMoveEvent`` <-> ``_forward_to_view`` alternating ~100 times): a
button-less mouse move the QML scene does not accept is propagated by
Qt back up to the picker card, which forwards it down to the QQuickWidget
again — unbounded recursion until the native stack dies. Presses never
loop (the QML MouseArea accepts them), which is why click-only tests and
the offscreen platform never saw it. Pinned here by delivering real
unaccepted moves and asserting the forwarding depth stays 1.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.usefixtures("qapp")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_ready_card(qtbot):
    """Build the real picker card and wait for its QML to be Ready."""
    from PySide6.QtQuickWidgets import QQuickWidget

    from app.theme.tokens import dark_tokens
    from app.toolshelf.tool_popup import _QmlColorCard

    card = _QmlColorCard(dark_tokens())
    qtbot.addWidget(card)
    card.resize(260, 320)
    card.show()
    qtbot.waitUntil(
        lambda: (card._view is not None
                 and card._view.status() == QQuickWidget.Status.Ready),
        timeout=5000)
    return card


def test_unaccepted_move_does_not_recurse_through_the_card(qtbot) -> None:
    """One unaccepted button-less move = exactly one forward to the view.

    Delivery goes through ``QApplication.sendEvent`` so Qt's real
    ignore-propagation machinery runs. The move carries a held button
    (drag semantics — the only way a move reaches the card's handler,
    which has no mouse tracking) and targets the card's outer padding
    where no QML MouseArea grabs it: the card forwards it down to the
    view, the view ignores it, Qt propagates it back up to the card,
    and the card forwards it down again — the exact packaged loop.
    """
    from app.toolshelf.tool_popup import _QmlColorCard

    card = _make_ready_card(qtbot)

    depth = {"current": 0, "max": 0}
    original = _QmlColorCard._forward_to_view

    def _counting(self, event) -> None:
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        try:
            original(self, event)
        finally:
            depth["current"] -= 1

    _QmlColorCard._forward_to_view = _counting
    try:
        move = QMouseEvent(
            QEvent.Type.MouseMove, QPointF(3, 3), QPointF(500, 500),
            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(card, move)
    finally:
        _QmlColorCard._forward_to_view = original

    # Exactly two entries: the real forward, then the propagated copy
    # re-entering once and being accepted by the guard. Before the fix
    # this ran to CPython's recursion limit (240 measured) and to a native
    # stack overflow in the packaged build.
    assert depth["max"] <= 2, (
        f"one unaccepted move was forwarded {depth['max']} times deep — "
        "the ignore-propagation loop is back")

    # The card must not swallow the state either: the original event is
    # accepted by _forward_to_view, and the card stays usable.
    assert card._view is not None and card._view.isVisible()


_WINDOWED_MOVE_CHILD = """
import sys

sys.path.insert(0, r'{root}')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from app.application import create_application

app = create_application([])
from PySide6.QtQuickWidgets import QQuickWidget

from app.theme.tokens import dark_tokens
from app.toolshelf.tool_popup import _QmlColorCard

card = _QmlColorCard(dark_tokens())
card.resize(260, 320)
card.show()
if not QTest.qWaitForWindowExposed(card):
    print("platform-unavailable", flush=True)
    raise SystemExit(0)
deadline = 100
while (card._view is None
       or card._view.status() != QQuickWidget.Status.Ready):
    app.processEvents()
    deadline -= 1
    if deadline <= 0:
        print("qml-not-ready", flush=True)
        raise SystemExit(3)
# Real moves across the whole card: padding, SV square, hue bar, gaps —
# the exact packaged-crash interaction (move over non-accepting areas).
for y in range(2, card.height() - 2, 13):
    for x in range(2, card.width() - 2, 13):
        QTest.mouseMove(card, QPoint(x, y))
        app.processEvents()
# Interaction parity: press + drag + release on the square.
QTest.mouseMove(card, QPoint(card.width() // 2, card.height() // 2))
QTest.mousePress(card, Qt.MouseButton.LeftButton)
QTest.mouseMove(card, QPoint(card.width() // 2 + 9, card.height() // 2))
QTest.mouseRelease(card, Qt.MouseButton.LeftButton)
app.processEvents()
print("picker-moves-ok", flush=True)
"""


def test_windowed_real_moves_over_the_picker_do_not_crash() -> None:
    """Real platform + real synthesized mouse moves over the picker card.

    Mirrors the packaged crash as closely as a dev-mode test can: a real
    window (no offscreen platform), a sweep of real moves across the
    card including the non-accepting padding, then a square drag. The
    child is a subprocess so a native stack overflow cannot take pytest
    down; it must report "picker-moves-ok" and exit 0. Skipped when the
    host has no interactive platform (headless CI).
    """
    child = _WINDOWED_MOVE_CHILD.replace("{root}", str(REPO_ROOT))
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    result = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True,
        timeout=180, env=env, cwd=str(REPO_ROOT))

    if "platform-unavailable" in result.stdout:
        pytest.skip("no interactive Qt platform on this host")
    assert result.returncode == 0, (
        f"child died rc={result.returncode}\n"
        f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}")
    assert "picker-moves-ok" in result.stdout, (
        f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}")
    assert "qml-not-ready" not in result.stdout


def test_selftest_picker_runtime_check_instantiates_the_card() -> None:
    """The self test builds the picker card and records Ready, no errors."""
    from app.selftest import _check_picker_runtime

    report: dict = {"checks": {}, "errors": []}
    _check_picker_runtime(report)

    result = report["checks"]["picker_runtime"]
    assert result["status"] == "Ready", result
    assert result["errors"] == []
    assert report["errors"] == []


def test_fatal_crash_leaves_a_diagnostics_file(tmp_path: Path) -> None:
    """A native abort writes a stack-trace file; clean exits remove it."""
    child = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "sys.path.insert(0, r'%s')\n"
        "import main\n"
        "main._enable_fatal_diagnostics()\n"
        "import faulthandler\n"
        "assert faulthandler.is_enabled()\n"
        "os.abort()\n" % REPO_ROOT
    )
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True,
        timeout=120, env=env, cwd=str(REPO_ROOT))

    dumps = glob.glob(os.path.join(
        tempfile.gettempdir(), "inkfall-fatal-*.log"))
    try:
        # The child aborted: the dump file must survive with a stack.
        assert result.returncode != 0
        assert dumps, "fatal crash produced no diagnostics file"
        content = open(dumps[-1], encoding="utf-8", errors="replace").read()
        assert "Fatal Python error" in content, content[:400]
        assert "Current thread" in content, content[:400]
        # The stack must name the faulting frame (the child's abort line).
        assert "line 8 in <module>" in content, content[:400]
    finally:
        for path in dumps:
            try:
                os.remove(path)
            except OSError:
                pass


def test_clean_exit_removes_the_diagnostics_file() -> None:
    """A clean process removes its faulthandler file (no litter)."""
    child = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "sys.path.insert(0, r'%s')\n"
        "import main\n"
        "main._enable_fatal_diagnostics()\n"
        "import faulthandler, glob, tempfile\n"
        "mine = glob.glob(os.path.join(tempfile.gettempdir(), "
        "'inkfall-fatal-*.log'))\n"
        "assert len(mine) == 1, mine\n"
        "sys.exit(0)\n" % REPO_ROOT
    )
    result = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True,
        timeout=120, cwd=str(REPO_ROOT))
    assert result.returncode == 0, result.stderr
