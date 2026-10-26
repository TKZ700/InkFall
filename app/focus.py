"""Shared keyboard-focus ring (Batch B, UI-16).

One recipe for every custom-painted chrome control that must show
keyboard focus: a 1.4px solid ``input_focus`` ring, inset 1px inside
the control's painted silhouette, rounded to the shape the caller
passes (an ellipse when ``radius`` is None). This replaces the drifting
per-file recipes (tab strip 1.4px, menu rows 1.2px, window controls and
HUD none) without introducing per-component colors or geometry.

The ring paints INSIDE the widget's own bounds and never changes
geometry: sizes, paddings, and layouts are unaffected whether or not a
control is focused.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen

__all__ = ["FOCUS_RING_WIDTH", "FOCUS_RING_INSET", "draw_focus_ring"]

#: Ring width shared by every focus treatment — matches the tab strip's
#: established 1.4px recipe. ``input_focus`` tokens are solid hex colors,
#: so ``QColor()`` parses them safely (M4.6.8 only affects rgba() strings).
FOCUS_RING_WIDTH = 1.4

#: Inset of the ring inside the passed silhouette.
FOCUS_RING_INSET = 1.0

#: Fallback when tokens lack ``input_focus`` (the dark accent value).
_FALLBACK = "#8B93E0"


def draw_focus_ring(painter: QPainter, rect, tokens,
                    radius: float | None = 6.0,
                    inset: float = FOCUS_RING_INSET) -> None:
    """Paint the shared focus ring inside ``rect`` on ``painter``.

    ``radius=None`` draws an ellipse (circular controls); otherwise a
    rounded rectangle with the given corner radius. Callers gate on
    ``hasFocus()`` and pass the silhouette they already paint, so the
    ring follows each control's shape — including capsule segments,
    whose outer half the widget's own bounds clip. Never raises.
    """
    try:
        if rect is None or tokens is None:
            return
        color = QColor(getattr(tokens, "input_focus", _FALLBACK))
        if not color.isValid():
            return
        ring = QRectF(rect).adjusted(inset, inset, -inset, -inset)
        if ring.isEmpty():
            return
        pen = QPen(color)
        pen.setWidthF(FOCUS_RING_WIDTH)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(pen)
        if radius is None:
            painter.drawEllipse(ring)
        else:
            painter.drawRoundedRect(ring, float(radius), float(radius))
    except Exception:
        pass
