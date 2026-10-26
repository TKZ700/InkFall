"""NumericBridge: validated intent channel for the QML numeric editor.

One instance per editor (Size or Opacity of one section). Differences
are properties (minimum/maximum/decimals/step/unit/label), never forks.

Python_tool side contract:
  on_commit(model_value: float)  -- validated + clamped value in MODEL units
  on_cancel()                    -- Escape: restore pre-edit value
  begin_edit()                   -- QML took focus: snapshot revert point
Display units (mm / %) are converted here so QML never knows the model.
"""

import re

from PySide6.QtCore import QObject, Property, Signal, Slot


_FLOAT_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)\s*([a-zA-Z%]*)\s*$")


class NumericBridge(QObject):
    valueChanged = Signal()
    displayTextChanged = Signal()
    focusRequested = Signal()
    # Range/label/unit metadata: set once at construction (immutable per
    # instance), notified so QML bindings are bindable, never warned.
    specChanged = Signal()

    def __init__(self, *, value: float, minimum: float, maximum: float,
                 decimals: int = 1, step: float = 0.1, unit: str = "",
                 label: str = "Exact", to_display=None, from_display=None,
                 on_commit=None, on_cancel=None, on_begin=None,
                 parent=None) -> None:
        super().__init__(parent)
        self._value = float(value)
        self._minimum = float(minimum)
        self._maximum = float(maximum)
        self._decimals = int(decimals)
        self._step = float(step)
        self._unit = str(unit)
        self._label = str(label)
        self._to_display = to_display or (lambda v: v)
        self._from_display = from_display or (lambda v: v)
        self._on_commit = on_commit
        self._on_cancel = on_cancel
        self._on_begin = on_begin

    # -- properties ----------------------------------------------------
    def _get_value(self) -> float:
        return self._value

    def _set_value(self, v: float) -> None:
        v = float(v)
        if v != self._value:
            self._value = v
            self.valueChanged.emit()
            self.displayTextChanged.emit()

    value = Property(float, _get_value, _set_value, notify=valueChanged)

    def _get_display(self) -> str:
        try:
            d = float(self._to_display(self._value))
        except Exception:
            d = self._value
        text = f"{d:.{self._decimals}f}"
        return f"{text} {self._unit}".strip() if self._unit else text

    displayText = Property(str, _get_display, notify=displayTextChanged)

    minimum = Property(float, lambda self: self._minimum,
                       notify=specChanged)
    maximum = Property(float, lambda self: self._maximum,
                       notify=specChanged)
    decimals = Property(int, lambda self: self._decimals,
                        notify=specChanged)
    step = Property(float, lambda self: self._step, notify=specChanged)
    unit = Property(str, lambda self: self._unit, notify=specChanged)
    label = Property(str, lambda self: self._label, notify=specChanged)

    # -- slots ----------------------------------------------------------
    @Slot(str, result=bool)
    def isValid(self, text: str) -> bool:  # noqa: N802
        try:
            return self._parse(text) is not None
        except Exception:
            return False

    @Slot(str)
    def commit(self, text: str) -> None:  # noqa: N802
        """Enter / focus-out: parse, clamp to tool range, forward."""
        try:
            parsed = self._parse(text)
            if parsed is None:
                # Invalid or incomplete: revert display, corrupt nothing.
                self.displayTextChanged.emit()
                return
            clamped = max(self._minimum, min(self._maximum, parsed))
            try:
                model_value = float(self._from_display(clamped))
            except Exception:
                model_value = clamped
            self._set_value(clamped)
            if self._on_commit is not None:
                self._on_commit(model_value)
            else:
                self.displayTextChanged.emit()
        except Exception:
            try:
                self.displayTextChanged.emit()
            except Exception:
                pass

    @Slot()
    def cancel(self) -> None:  # noqa: N802
        """Escape: restore the pre-edit committed value."""
        try:
            if self._on_cancel is not None:
                self._on_cancel()
            self.displayTextChanged.emit()
        except Exception:
            pass

    @Slot()
    def beginEdit(self) -> None:  # noqa: N802
        try:
            if self._on_begin is not None:
                self._on_begin()
        except Exception:
            pass

    def requestFocus(self) -> None:
        self.focusRequested.emit()

    def sync_external(self, display_value: float) -> None:
        """Model changed elsewhere (slider/tool switch): re-seed display."""
        self._set_value(float(display_value))

    # -- parsing ---------------------------------------------------------
    def _parse(self, text: str):
        if text is None:
            return None
        m = _FLOAT_RE.match(str(text).strip())
        if not m:
            return None
        try:
            return float(m.group(1))
        except Exception:
            return None
