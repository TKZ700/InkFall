"""Windows Ink press-and-hold suppression for the canvas (M5-B.2.0.2).

Windows-only, dependency-free (ctypes + PySide6, no new packages),
reversible (filter removed on viewer close). Windows Ink itself stays
enabled; only the press-and-hold gesture + filling circle are disabled,
and only over bare canvas — toolbar, popups, scrollbars, and overlays
keep default behavior. Pen tap/double-tap ripples are a separate
cosmetic channel (SetWindowFeedbackSetting, per top-level HWND); see
``disable_pen_tap_feedback``.

Verified against Microsoft Learn
(WM_TABLET_QUERYSYSTEMGESTURESTATUS, Tpcshrd.h values) and the Qt 6
QAbstractNativeEventFilter contract ("windows_generic_MSG" carries a
MSG* for toplevel windows; return True + LRESULT to claim handling).

The OS sends WM_TABLET_QUERYSYSTEMGESTURESTATUS (via SendMessageTimeout)
to the hit-tested window on pen contact. lParam carries the screen
coordinates (GET_X/Y_LPARAM), so region scoping needs no cursor polling.
"""

from __future__ import annotations

import ctypes

#: Sent when the system asks a window which gestures it wants.
#: WM_TABLET_DEFBASE (0x02C0) + 12. Verified: Microsoft Learn,
#: "WM_TABLET_QUERYSYSTEMGESTURESTATUS message (Tpcshrd.h)".
WM_TABLET_QUERYSYSTEMGESTURESTATUS = 0x02CC

#: Disables the press-and-hold (right-click) gesture + filling circle.
#: Verified: same page, Tpcshrd.h table. Nothing else is disabled.
TABLET_DISABLE_PRESSANDHOLD = 0x00000001

#: Visual-feedback types (FEEDBACK_TYPE, winuser.h — NOT the gesture
#: status flags above). Verified: Microsoft Learn "FEEDBACK_TYPE
#: (winuser.h)": PEN_TAP = 3, PEN_DOUBLETAP = 4. The tap/double-tap
#: ripple is cosmetic feedback, not a gesture, so the gesture-query
#: filter above can never suppress it — that needs
#: SetWindowFeedbackSetting below.
FEEDBACK_PEN_TAP = 3
FEEDBACK_PEN_DOUBLETAP = 4

_WINDOWS_GENERIC_MSG = b"windows_generic_MSG"


def _ptr_size() -> int:
    return ctypes.sizeof(ctypes.c_void_p)


def _address_of(message) -> int | None:
    """Validate a native message pointer; None when unusable."""
    try:
        address = int(message)
    except Exception:
        return None
    if address <= 0:
        return None
    return address


def msg_message_id(message) -> int | None:
    """Read the MSG.message UINT (offset = pointer size)."""
    address = _address_of(message)
    if address is None:
        return None
    try:
        return int(ctypes.c_uint.from_address(
            address + _ptr_size()).value)
    except Exception:
        return None


def msg_hwnd(message) -> int | None:
    """Read the MSG.hwnd field (offset 0)."""
    address = _address_of(message)
    if address is None:
        return None
    try:
        return int(ctypes.c_void_p.from_address(address).value or 0)
    except Exception:
        return None


def msg_lparam(message) -> int | None:
    """Read the MSG.lParam field (offset = 3 * pointer size)."""
    address = _address_of(message)
    if address is None:
        return None
    try:
        raw = ctypes.c_void_p.from_address(
            address + 3 * _ptr_size()).value or 0
        return int(raw)
    except Exception:
        return None


def lparam_to_xy(lparam: int) -> tuple[int, int]:
    """Split an LPARAM into signed (x, y) shorts (GET_X/Y_LPARAM)."""
    try:
        value = int(lparam)
    except Exception:
        return (0, 0)
    word = _short(value & 0xFFFF)
    return (word, _short((value >> 16) & 0xFFFF))


def _short(word: int) -> int:
    word &= 0xFFFF
    return word - 0x10000 if word & 0x8000 else word


def gesture_query_decision(message_id, hwnd, x: int, y: int,
                           top_hwnd: int, in_canvas: bool
                           ) -> tuple[bool, int]:
    """Pure suppress-or-ignore decision for a gesture query.

    Returns (True, TABLET_DISABLE_PRESSANDHOLD) only when the message
    is the gesture query, targets our top-level window, and the pen is
    over bare canvas. Everything else stays unclaimed: (False, 0).
    ``in_canvas`` is resolved by the caller (viewer geometry hit-test).
    """
    try:
        if message_id is None or int(message_id) \
                != WM_TABLET_QUERYSYSTEMGESTURESTATUS:
            return (False, 0)
        if hwnd is None or int(hwnd) != int(top_hwnd) or not int(top_hwnd):
            return (False, 0)
        if not in_canvas:
            return (False, 0)
        return (True, TABLET_DISABLE_PRESSANDHOLD)
    except Exception:
        return (False, 0)


class InkGestureFilter:
    """Qt-independent decision core (no PySide6 import needed).

    Kept free of Qt so the predicate + decoders unit-test anywhere.
    The viewer wraps this in a QAbstractNativeEventFilter (see
    InkGestureNativeFilter below).
    """

    def __init__(self, decide) -> None:
        """``decide(msg_hwnd, screen_x, screen_y) -> bool``."""
        self._decide = decide

    def handle(self, event_type, message) -> tuple[bool, int]:
        """Decide a native message; never raises, never retains it."""
        try:
            try:
                if bytes(event_type) != _WINDOWS_GENERIC_MSG:
                    return (False, 0)
            except Exception:
                return (False, 0)
            message_id = msg_message_id(message)
            if message_id != WM_TABLET_QUERYSYSTEMGESTURESTATUS:
                return (False, 0)
            hwnd = msg_hwnd(message)
            lparam = msg_lparam(message)
            if hwnd is None or lparam is None:
                return (False, 0)
            x, y = lparam_to_xy(lparam)
            try:
                # decide() compares the live top-level HWND itself and
                # the canvas hit-test; True only when both hold.
                in_canvas = bool(self._decide(hwnd, x, y))
            except Exception:
                in_canvas = False
            if in_canvas:
                return (True, TABLET_DISABLE_PRESSANDHOLD)
            return (False, 0)
        except Exception:
            return (False, 0)


def make_native_filter(decide):
    """Build the QAbstractNativeEventFilter (Qt import isolated here)."""
    from PySide6.QtCore import QAbstractNativeEventFilter

    core = InkGestureFilter(decide)

    class _NativeFilter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, eventType, message):
            try:
                return core.handle(eventType, message)
            except Exception:
                return (False, 0)

    return _NativeFilter()


# -- Pen tap visual-feedback suppression (B.2.1 post-Slice-3 fix C) ------

def set_pen_feedback(hwnd, feedback_type: int, enabled: bool) -> bool:
    """Enable/disable one FEEDBACK_TYPE visual on a top-level HWND.

    Windows-only ``user32.SetWindowFeedbackSetting`` via ctypes (no new
    dependencies). Cosmetic only: gestures, tap/double-tap delivery,
    and input routing are unaffected. Returns True only when the OS
    call reports success; False (never raises) on non-Windows
    platforms, invalid handles, or any failure.
    """
    try:
        import ctypes

        hwnd_value = int(hwnd)
        if hwnd_value <= 0:
            return False
        try:
            feedback = int(feedback_type)
        except (TypeError, ValueError):
            return False
        user32 = ctypes.windll.user32  # noqa: AttributeError on non-Windows
        config = ctypes.c_uint32(1 if enabled else 0)
        result = user32.SetWindowFeedbackSetting(
            ctypes.c_void_p(hwnd_value),
            ctypes.c_uint(feedback),
            ctypes.c_uint32(0),
            ctypes.c_uint(4),
            ctypes.byref(config))
        return bool(result)
    except Exception:
        return False


def disable_pen_tap_feedback(hwnd) -> bool:
    """Suppress pen tap + double-tap ripples on a top-level HWND.

    Disables exactly FEEDBACK_PEN_TAP and FEEDBACK_PEN_DOUBLETAP;
    barrel, press-and-hold, right-tap, and all touch feedback keep
    default behavior. True only when both calls succeed; never raises.
    """
    try:
        ok_tap = set_pen_feedback(hwnd, FEEDBACK_PEN_TAP, False)
        ok_doubletap = set_pen_feedback(hwnd, FEEDBACK_PEN_DOUBLETAP, False)
        return bool(ok_tap and ok_doubletap)
    except Exception:
        return False
