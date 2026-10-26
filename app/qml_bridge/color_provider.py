"""Square HSV picker bridge: Python owns color state and math, QML owns
presentation (gradient square, hue slider, markers, preview, hex field).

Qt property semantics: every QML-read value is a ``Property`` with a
``NOTIFY`` signal, emitted only when the value actually changes. No
change signals are emitted from ``__init__`` — QML binds the initial
values on load. There is no image provider: the square and slider are
declarative Qt Quick gradients (GPU-composited, no per-pixel JS, no
per-move texture uploads, no tree rebuilds during drags).
"""

from PySide6.QtCore import QObject, Property, Signal, Slot
from PySide6.QtGui import QColor


def _clamp01(v: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.0


def _clamp_hue(v) -> int:
    try:
        return max(0, min(359, int(round(float(v)))))
    except Exception:
        return 0


def _hex_of(hue: int, sat: float, val: float) -> str:
    try:
        return QColor.fromHsv(hue, int(sat * 255),
                              int(val * 255)).name()
    except Exception:
        return "#000000"


def _normalize_hex(text) -> str | None:
    try:
        t = str(text).strip()
        if not t.startswith("#"):
            t = f"#{t}"
        if len(t) == 7 and QColor(t).isValid():
            return t.lower()
    except Exception:
        pass
    return None


class SquarePickerBridge(QObject):
    """HSV state + picking math for the square picker card."""

    picked = Signal(str)       # live color, canonical "#rrggbb"
    huePicked = Signal(int)    # hue-gesture (SV preserved)
    hsvChanged = Signal()      # hue/sat/val/markers/base changed
    previewChanged = Signal()  # preview hex changed
    hexChanged = Signal()      # hex field text changed

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hue = 0
        self._sat = 1.0
        self._val = 1.0

    # -- bindable state (NOTIFY, emit on change only) --------------------
    def _get_hue(self) -> int:
        return self._hue

    def _set_hue(self, v: int) -> None:
        self.setHSV(_clamp_hue(v), self._sat, self._val)

    hue = Property(int, _get_hue, _set_hue, notify=hsvChanged)

    def _get_sat(self) -> float:
        return self._sat

    def _set_sat(self, v: float) -> None:
        self.setHSV(self._hue, _clamp01(v), self._val)

    sat = Property(float, _get_sat, _set_sat, notify=hsvChanged)

    def _get_val(self) -> float:
        return self._val

    def _set_val(self, v: float) -> None:
        self.setHSV(self._hue, self._sat, _clamp01(v))

    val = Property(float, _get_val, _set_val, notify=hsvChanged)

    def _get_hue_color(self) -> str:
        return _hex_of(self._hue, 1.0, 1.0)

    hueColor = Property(str, _get_hue_color, notify=hsvChanged)

    def _get_preview(self) -> str:
        return _hex_of(self._hue, self._sat, self._val)

    previewColor = Property(str, _get_preview, notify=previewChanged)

    def _get_hex(self) -> str:
        return _hex_of(self._hue, self._sat, self._val)

    hexText = Property(str, _get_hex, notify=hexChanged)

    svMarkX = Property(float, lambda self: self._sat, notify=hsvChanged)
    svMarkY = Property(float, lambda self: 1.0 - self._val,
                       notify=hsvChanged)
    hueMarkX = Property(float, lambda self: self._hue / 359.0,
                        notify=hsvChanged)

    # -- core assignment (single emit point) -------------------------------
    def _assign(self, hue: int, sat: float, val: float) -> bool:
        hue, sat, val = _clamp_hue(hue), _clamp01(sat), _clamp01(val)
        if (hue, sat, val) == (self._hue, self._sat, self._val):
            return False
        self._hue, self._sat, self._val = hue, sat, val
        self.hsvChanged.emit()
        self.previewChanged.emit()
        self.hexChanged.emit()
        return True

    # -- external sync (model/hex/strip path; markers follow, no pick) ----
    @Slot(int, float, float)
    def setHSV(self, hue: int, sat: float, val: float) -> None:  # noqa: N802
        try:
            self._assign(hue, sat, val)
        except Exception:
            pass

    # -- pointer input (QML MouseAreas; coordinates clamped) ----------------
    @Slot(float, float, float, float)
    def pressSV(self, x: float, y: float,  # noqa: N802
                w: float, h: float) -> None:
        self._sv_at(float(x), float(y), float(w), float(h))

    @Slot(float, float, float, float)
    def moveSV(self, x: float, y: float,  # noqa: N802
               w: float, h: float) -> None:
        self._sv_at(float(x), float(y), float(w), float(h))

    def _sv_at(self, x: float, y: float, w: float, h: float) -> None:
        try:
            if w <= 0 or h <= 0:
                return
            s = _clamp01(x / w)
            v = _clamp01(1.0 - y / h)
            if self._assign(self._hue, s, v):  # hue preserved
                self.picked.emit(_hex_of(self._hue, s, v))
        except Exception:
            pass

    @Slot(float, float)
    def pressHue(self, x: float, w: float) -> None:  # noqa: N802
        self._hue_at(float(x), float(w))

    @Slot(float, float)
    def moveHue(self, x: float, w: float) -> None:  # noqa: N802
        self._hue_at(float(x), float(w))

    def _hue_at(self, x: float, w: float) -> None:
        try:
            if w <= 0:
                return
            hue = _clamp_hue(_clamp01(x / w) * 359.0)
            sat, val = self._sat, self._val  # snapshot: SV preserved
            if self._assign(hue, sat, val):
                self.huePicked.emit(hue)
                self.picked.emit(_hex_of(hue, sat, val))
        except Exception:
            pass

    # -- hex input ------------------------------------------------------------
    @Slot(str)
    def commitHex(self, text: str) -> None:  # noqa: N802
        """Hex field commit: valid input syncs square/slider/preview and
        live-applies; invalid input reverts the field, corrupts nothing."""
        try:
            hexed = _normalize_hex(text)
            if hexed is None:
                self.hexChanged.emit()  # QML resets when unfocused
                return
            color = QColor(hexed)
            h, s, v, _a = color.getHsv()
            if h < 0:
                h = 0
            if self._assign(h, s / 255.0, v / 255.0):
                self.picked.emit(hexed)
            else:
                # Same color re-typed: still confirm it downstream.
                self.picked.emit(hexed)
        except Exception:
            pass
