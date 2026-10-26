"""M5-B.2.0.2 Stage 2: Windows Ink press-and-hold suppression filter.

Fabricated native-message structures only — these tests prove filter
logic (decode, scope, lifecycle), never native Windows behavior. The
disappearance of the hold circle requires hardware verification.
"""

from __future__ import annotations

import ctypes
import sys

import pytest
from PySide6.QtCore import QPoint

from app import ink_gesture as ig
from app.viewer import PdfViewer

pytestmark = pytest.mark.usefixtures("qapp")

TOP_HWND = 0xABCDEF
OTHER_HWND = 0x123456


def _make_msg(message_id, hwnd, x, y):
    """Fabricate an MSG struct; returns (buffer, address)."""
    P = ctypes.sizeof(ctypes.c_void_p)
    buf = ctypes.create_string_buffer(3 * P + 8)
    addr = ctypes.addressof(buf)
    ctypes.c_void_p.from_address(addr).value = hwnd
    ctypes.c_uint.from_address(addr + P).value = message_id & 0xFFFFFFFF
    lparam = (((y & 0xFFFF) << 16) | (x & 0xFFFF)) & 0xFFFFFFFFFFFFFFFF
    ctypes.c_void_p.from_address(addr + 3 * P).value = lparam
    return buf, addr


# -- constants ---------------------------------------------------------------

def test_verified_windows_constants():
    assert ig.WM_TABLET_QUERYSYSTEMGESTURESTATUS == 0x02C0 + 12 == 0x02CC
    assert ig.TABLET_DISABLE_PRESSANDHOLD == 0x00000001


# -- decoders ------------------------------------------------------------------

def test_msg_field_decoders_round_trip():
    _buf, addr = _make_msg(0x02CC, TOP_HWND, 300, -150)
    assert ig.msg_message_id(addr) == 0x02CC
    assert ig.msg_hwnd(addr) == TOP_HWND
    assert ig.msg_lparam(addr) is not None
    assert ig.lparam_to_xy(ig.msg_lparam(addr)) == (300, -150)


def test_msg_decoders_reject_garbage():
    assert ig.msg_message_id(0) is None
    assert ig.msg_message_id(None) is None
    assert ig.msg_message_id("nope") is None
    assert ig.msg_hwnd(0) is None
    assert ig.msg_hwnd(None) is None
    assert ig.msg_lparam(-4) is None


def test_lparam_signed_split():
    assert ig.lparam_to_xy(0x00030004) == (4, 3)
    assert ig.lparam_to_xy(0xFFFF0001) == (1, -1)
    assert ig.lparam_to_xy(0) == (0, 0)
    assert ig.lparam_to_xy("bad") == (0, 0)


# -- pure decision ---------------------------------------------------------------

def test_decision_suppresses_only_on_matching_canvas():
    ok = ig.gesture_query_decision(0x02CC, TOP_HWND, 10, 20, TOP_HWND, True)
    assert ok == (True, ig.TABLET_DISABLE_PRESSANDHOLD)
    assert ig.gesture_query_decision(0x0200, TOP_HWND, 10, 20, TOP_HWND,
                                     True) == (False, 0)
    assert ig.gesture_query_decision(0x02CC, OTHER_HWND, 10, 20, TOP_HWND,
                                     True) == (False, 0)
    assert ig.gesture_query_decision(0x02CC, TOP_HWND, 10, 20, TOP_HWND,
                                     False) == (False, 0)
    assert ig.gesture_query_decision(0x02CC, TOP_HWND, 10, 20, 0,
                                     True) == (False, 0)
    assert ig.gesture_query_decision(None, TOP_HWND, 10, 20, TOP_HWND,
                                     True) == (False, 0)


# -- filter object -----------------------------------------------------------------

def _filter(decide):
    return ig.InkGestureFilter(decide)


def test_filter_suppresses_matching_canvas_query():
    _buf, addr = _make_msg(0x02CC, TOP_HWND, 100, 200)
    seen = []
    filt = _filter(lambda h, x, y: seen.append((h, x, y)) or True)
    result = filt.handle(b"windows_generic_MSG", addr)
    assert result == (True, ig.TABLET_DISABLE_PRESSANDHOLD)
    assert seen == [(TOP_HWND, 100, 200)]


def test_filter_leaves_everything_else_unclaimed():
    _buf, addr = _make_msg(0x02CC, TOP_HWND, 100, 200)
    filt = _filter(lambda h, x, y: True)
    assert filt.handle(b"windows_dispatcher_MSG", addr) == (False, 0)
    assert filt.handle(b"xcb_generic_event_t", addr) == (False, 0)
    assert filt.handle("nope", addr) == (False, 0)
    assert filt.handle(None, addr) == (False, 0)
    _buf2, other = _make_msg(0x0111, TOP_HWND, 100, 200)
    assert filt.handle(b"windows_generic_MSG", other) == (False, 0)
    assert filt.handle(b"windows_generic_MSG", 0) == (False, 0)
    assert filt.handle(b"windows_generic_MSG", None) == (False, 0)
    assert filt.handle(b"windows_generic_MSG", "garbage") == (False, 0)


def test_filter_decide_false_or_raising_stays_unclaimed():
    _buf, addr = _make_msg(0x02CC, TOP_HWND, 100, 200)
    assert _filter(lambda h, x, y: False).handle(
        b"windows_generic_MSG", addr) == (False, 0)

    def boom(h, x, y):
        raise RuntimeError("decide exploded")

    assert _filter(boom).handle(b"windows_generic_MSG", addr) == (False, 0)


def test_native_filter_wrapper_delegates_and_never_raises():
    from PySide6.QtCore import QAbstractNativeEventFilter

    _buf, addr = _make_msg(0x02CC, TOP_HWND, 7, 9)
    filt = ig.make_native_filter(lambda h, x, y: True)
    assert isinstance(filt, QAbstractNativeEventFilter)
    assert filt.nativeEventFilter(b"windows_generic_MSG", addr) == (
        True, ig.TABLET_DISABLE_PRESSANDHOLD)
    assert filt.nativeEventFilter(b"windows_generic_MSG", 0) == (False, 0)


# -- viewer lifecycle -------------------------------------------------------------

def _make_viewer(qtbot):
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    qtbot.wait(100)
    return viewer


def test_no_install_off_windows(qtbot, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    viewer = _make_viewer(qtbot)
    assert viewer._ink_gesture_filter is None
    assert viewer._install_ink_gesture_filter() is False
    assert viewer._ink_gesture_filter is None
    viewer.close()


def test_install_once_and_remove_on_close(qtbot, monkeypatch):
    from PySide6.QtGui import QShowEvent

    monkeypatch.setattr(sys, "platform", "linux")
    viewer = _make_viewer(qtbot)
    assert viewer._ink_gesture_filter is None
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(viewer, "_ink_gesture_top_hwnd", lambda: TOP_HWND)
    assert viewer._install_ink_gesture_filter() is True
    first = viewer._ink_gesture_filter
    assert first is not None
    viewer.showEvent(QShowEvent())  # repeat shows never duplicate
    assert viewer._ink_gesture_filter is first
    viewer.close()
    assert viewer._ink_gesture_filter is None


def test_install_failure_leaves_viewer_usable(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    viewer = _make_viewer(qtbot)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(viewer, "_ink_gesture_top_hwnd", lambda: TOP_HWND)

    def boom(decide):
        raise RuntimeError("install exploded")

    monkeypatch.setattr(ig, "make_native_filter", boom)
    assert viewer._install_ink_gesture_filter() is False
    assert viewer._ink_gesture_filter is None
    from tests.helpers import make_probe_pdf

    pdf = make_probe_pdf(str(tmp_path / "ok.pdf"))
    assert viewer.open_document(pdf) is True
    viewer.close()


def test_removal_failure_never_crashes_close(qtbot, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    viewer = _make_viewer(qtbot)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(viewer, "_ink_gesture_top_hwnd", lambda: TOP_HWND)
    assert viewer._install_ink_gesture_filter() is True
    viewer._ink_gesture_filter = object()  # removal path must tolerate junk
    viewer.close()  # must not raise
    assert viewer._ink_gesture_filter is None


def test_viewer_decide_checks_hwnd_then_canvas(qtbot, monkeypatch):
    viewer = _make_viewer(qtbot)
    monkeypatch.setattr(viewer, "_ink_gesture_top_hwnd", lambda: TOP_HWND)
    monkeypatch.setattr(viewer, "_ink_canvas_hit", lambda sx, sy: True)
    assert viewer._ink_gesture_decide(TOP_HWND, 10, 20) is True
    assert viewer._ink_gesture_decide(OTHER_HWND, 10, 20) is False
    monkeypatch.setattr(viewer, "_ink_canvas_hit", lambda sx, sy: False)
    assert viewer._ink_gesture_decide(TOP_HWND, 10, 20) is False
    viewer.close()


def test_canvas_hit_excludes_chrome_and_outside(qtbot):
    from app.toolshelf.slot_button import SlotButton

    viewer = _make_viewer(qtbot)
    glob_center = viewer.mapToGlobal(viewer.rect().center())
    # Center may sit under the shelf on narrow layouts; assert the
    # rule, not the layout: bare point iff no child owns it.
    expected = viewer.childAt(viewer.rect().center()) is None
    assert viewer._ink_canvas_hit(glob_center.x(),
                                  glob_center.y()) is expected
    buttons = viewer.tool_shelf.findChildren(SlotButton)
    assert buttons
    btn_glob = buttons[0].mapToGlobal(buttons[0].rect().center())
    assert viewer._ink_canvas_hit(btn_glob.x(), btn_glob.y()) is False
    assert viewer._ink_canvas_hit(-10000, -10000) is False
    viewer.close()


# -- pen tap feedback suppression (post-Slice-3 fix C) ------------------------

def test_feedback_type_constants_match_winuser():
    # Microsoft Learn "FEEDBACK_TYPE (winuser.h)": PEN_TAP = 3,
    # PEN_DOUBLETAP = 4. Distinct namespace from the gesture flags.
    assert ig.FEEDBACK_PEN_TAP == 3
    assert ig.FEEDBACK_PEN_DOUBLETAP == 4


def test_set_feedback_rejects_bad_handles_without_raising():
    assert ig.set_pen_feedback(0, ig.FEEDBACK_PEN_TAP, False) is False
    assert ig.set_pen_feedback(-1, ig.FEEDBACK_PEN_TAP, False) is False
    assert ig.set_pen_feedback(None, ig.FEEDBACK_PEN_TAP, False) is False
    assert ig.set_pen_feedback("nope", ig.FEEDBACK_PEN_TAP, False) is False
    assert ig.set_pen_feedback(0, "bad-type", False) is False


def test_set_feedback_rejects_bogus_hwnd_without_raising():
    # No such window: the OS call reports failure; never raises.
    assert ig.set_pen_feedback(0xDEADBEEF, ig.FEEDBACK_PEN_TAP,
                               False) is False


def test_disable_composes_tap_and_doubletap(monkeypatch):
    calls = []

    def fake_set(hwnd, feedback_type, enabled):
        calls.append((hwnd, feedback_type, enabled))
        return True

    monkeypatch.setattr(ig, "set_pen_feedback", fake_set)
    assert ig.disable_pen_tap_feedback(TOP_HWND) is True
    assert calls == [(TOP_HWND, ig.FEEDBACK_PEN_TAP, False),
                     (TOP_HWND, ig.FEEDBACK_PEN_DOUBLETAP, False)]


def test_disable_requires_both_calls(monkeypatch):
    monkeypatch.setattr(ig, "set_pen_feedback",
                        lambda h, f, e: f != ig.FEEDBACK_PEN_DOUBLETAP)
    assert ig.disable_pen_tap_feedback(TOP_HWND) is False


def test_disable_propagates_os_failure(monkeypatch):
    monkeypatch.setattr(ig, "set_pen_feedback",
                        lambda h, f, e: (_ for _ in ()).throw(
                            RuntimeError("should be absorbed")))
    # set_pen_feedback itself absorbs; disable sees False.
    assert ig.disable_pen_tap_feedback(TOP_HWND) is False


def test_feedback_noop_off_windows(qtbot, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    viewer = _make_viewer(qtbot)
    assert viewer._disable_pen_tap_feedback() is False
    viewer.close()


def test_show_calls_install_then_feedback(qtbot, monkeypatch):
    from PySide6.QtGui import QShowEvent

    monkeypatch.setattr(sys, "platform", "linux")
    viewer = _make_viewer(qtbot)
    calls = []
    monkeypatch.setattr(viewer, "_install_ink_gesture_filter",
                        lambda: calls.append("install") or False)
    monkeypatch.setattr(viewer, "_disable_pen_tap_feedback",
                        lambda: calls.append("disable") or False)
    viewer.showEvent(QShowEvent())
    assert calls == ["install", "disable"]
    viewer.close()
