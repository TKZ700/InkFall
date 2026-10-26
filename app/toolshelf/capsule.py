"""Selected-tool capsule + options button (M6.9.4).

Drawboard-inspired affordance attached to the *selected* toolbar slot:
ONE continuous rounded capsule holding two stacked controls of the SAME
diameter — the selected tool's own circular button (its own paint, its own
icon) on top, and a solid circular three-dot options button directly
below it, sharing one vertical centreline.

The outer contour is a single capsule outline (a rounded rect whose end
radius is half the capsule's width), *not* the union of the two circles:
a union of circles produces a peanut. The contour's top end is concentric
with the tool's own circular selection halo, so the tool keeps a uniform
``CAPSULE_PAD`` of capsule around it, and the contour's bottom end wraps
the options circle.

PAINTING ORDER IS THE ARCHITECTURE. The capsule is a *backdrop*: an
overlay child of the shelf kept BELOW the slot tiles in the z-order
(``ToolShelf.raise_capsule_stack`` raises only the pointer target). Every
slot button therefore paints its own surface and glyph straight *over*
the capsule:

* the selected tool's halo and icon stay fully visible and unmodified —
  nothing to punch out, and no boolean path to get wrong. (The earlier
  implementation subtracted the tool circle from the body and filled that
  difference with the winding rule, which re-filled the hole and painted
  an opaque shape over the icon; Qt paths produced by subtraction must be
  filled odd-even. This design removes the operation altogether.)
* a neighbouring tool's glyph is never covered either: the capsule may
  reach into its cell, but the neighbour's tile paints on top of it.

Geometry comes from the slot button's own dimensions
(``app.toolshelf.slot_button``): a 40px cell holding a 30px circular
selected halo, so a custom slot's pitch is 43px (cell + 3px gap).

TWO EQUAL CIRCLES, DELIBERATELY SEPARATED. The specification requires
the options button to have the *same* diameter as the tool button, and
this module does exactly that: ``CAPSULE_DOTS_D == CAPSULE_TOOL_D == 30``.
Two tangent 30px circles need 60px of run below the tool's centre; the
plain 43px pitch cannot offer that, which is why the two used to overlap
and crowd each other. The *real layout* now reserves that run (the
selected cell grows along the axis, ``ToolShelf._reserved_extent``), so
the two discs are separated by ``CAPSULE_GAP`` instead: their centres are
``capsule_advance()`` (34px) apart and the shared contour is a 40 x 72
capsule — the 30px tool circle plus ``CAPSULE_PAD`` at its top end, the
30px options circle wrapped by the bottom end, and ``CAPSULE_BOTTOM_PAD``
of extra body *below* that circle so the lower control keeps breathing
room inside the capsule (the width already gives it ``CAPSULE_PAD`` at
both sides). The extra height is the capsule's own geometry — the
toolbar's pitch, cell and gaps are not touched: a capsule-free toolbar is
exactly the plain 43px rhythm.

The capsule's span, in rows from the selected cell's top: its top cap at
``-SEPARATION_ACTIVE`` (concentric with the tool's lifted circle), the
options circle following it, and its lower edge at ``capsule length -
SEPARATION_ACTIVE`` (66px). That lower edge is what the *real layout* has
to clear, and it does: while the capsule is shown the selected cell grows
to exactly ``capsule length - SEPARATION_ACTIVE`` along the toolbar axis,
so the next slot's cell — and with it that tool's own halo and glyph —
genuinely starts one normal inter-slot gap below the capsule's contour.
The capsule's lower end is also what the custom viewport has to keep
clear of the Custom|Add scroll arrow, and its upper end what that
viewport has to keep clear of the Core|Custom separator: both
boundaries are the shelf's job (``ToolShelf._keep_capsule_in_viewport``),
which scrolls the reserved run into view when the capsule would
otherwise cross past either end of the viewport (the upper end sits
``-SEPARATION_ACTIVE`` from the cell, so a selected *first* custom can
only ever close to that one-paint-offset minimum at the very start of
the strip — nothing above it can be scrolled into).

The options circle's centre stays inside this slot's own run (cell plus
the reserved space), so the *clickable* options target never reaches a
neighbouring cell (whose hit target has moved down with its cell).

``CapsuleOptionsButton`` is the pointer target in front of the face: its
geometry is exactly the part of the disc that belongs to this slot (the
disc's bounding box clipped on the toolbar axis to this slot's own cell
plus the following gap). The capsule itself is mouse-transparent, so the
slot buttons underneath keep their own hit targets, focus rings and
keyboard activation.

Both are positioned from ONE authoritative shelf-local calculation, and
the capsule is only ever shown at its final target (the reveal animation
is opacity-only, never positional) — selecting a tool can therefore never
flash or slide through a wrong location. No per-tool colour is used here:
the selected slot already carries its identity.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QWidget

from app.theme.tokens import InkfallTokens
from app.toolshelf.surfaces import (
    hover_wash,
    ink_color,
    selected_surface,
    token_color,
)

__all__ = [
    "ToolCapsule",
    "CapsuleOptionsButton",
    "CAPSULE_TOOL_D",
    "CAPSULE_DOTS_D",
    "CAPSULE_GAP",
    "CAPSULE_MERGE",
    "CAPSULE_PAD",
    "CAPSULE_BOTTOM_PAD",
    "CAPSULE_DOT_R",
    "CAPSULE_DOT_GAP",
    "CAPSULE_HIGHLIGHT_MS",
    "capsule_advance",
    "capsule_size",
]

#: Selected tool circle diameter. Matches the slot button's own circular
#: selected-state halo (30px inside a 40px cell) so the capsule's top end
#: is concentric with it.
CAPSULE_TOOL_D = 30
#: Options button diameter — the SAME as the tool button's, as specified:
#: a real opaque clickable second button, never a badge and never a tail.
#: The pitch cost of that equality is documented in the module docstring.
CAPSULE_DOTS_D = 30
#: Along-axis daylight between the tool circle and the options circle
#: (their centres are ``capsule_advance()`` apart). Two tangent 30px
#: circles need 60px of run under the tool; the old 43px pitch had to
#: overlap them by 6px to fit, which left the two controls visibly
#: crowding each other. Now that the *real layout* reserves the capsule's
#: run (``ToolShelf._reserved_extent``, so every following tool genuinely
#: moves down), the run exists and the two discs can be deliberately
#: separated instead: a modest 4px of daylight, no change to either disc's
#: diameter and no change to the 43px pitch the toolbar keeps when the
#: capsule is hidden.
CAPSULE_GAP = 4
#: Historical name, kept because other modules (and the focused suite)
#: import it: ``capsule_advance()`` reads it as the overlap between the
#: two circles, so a *negative* merge is exactly ``CAPSULE_GAP``.
CAPSULE_MERGE = -CAPSULE_GAP
#: Capsule padding around the tool circle — the same 5px the slot's own
#: 40px cell leaves around its 30px halo, so the capsule coincides with the
#: selected cell's footprint and is concentric with the tool circle.
CAPSULE_PAD = 5
#: Extra capsule body below the options circle. The width already keeps
#: ``CAPSULE_PAD`` at both sides of that circle; this is the matching
#: breathing room at its lower end, and it is where the "slightly taller
#: capsule" comes from — the toolbar's pitch is untouched.
CAPSULE_BOTTOM_PAD = 3

#: Radius of one of the three painted dots, in logical pixels. Sized to the
#: 30px options disc (the reference's own cluster is ~6/10 of its disc), so
#: the three read as three on a disc that large.
CAPSULE_DOT_R = 1.8
#: Centre-to-centre distance of the three dots. Keeps a genuinely dark
#: pixel between neighbouring dots while the cluster stays inside the disc.
CAPSULE_DOT_GAP = 5.5

#: Duration of the active-control highlight's travel between the tool's
#: circle and the options circle. Deliberately the same value the shelf
#: uses for the capsule's own room (``ToolShelf.SEPARATION_MS``), so the
#: indicator and the capsule's reserved run move as ONE gesture; the
#: easing is the same OutCubic curve.
CAPSULE_HIGHLIGHT_MS = 170


def capsule_advance() -> float:
    """Centre-to-centre distance from the tool circle to the dots.

    ``CAPSULE_GAP`` of daylight sits between the two equal discs:
    ``30 + CAPSULE_GAP``.
    """
    return (CAPSULE_TOOL_D + CAPSULE_DOTS_D) / 2.0 - CAPSULE_MERGE


def capsule_size(horizontal: bool) -> tuple[int, int]:
    """``(width, height)`` of the shared capsule, in device pixels.

    ``across`` is the tool circle plus ``CAPSULE_PAD`` on both sides: the
    capsule's rounded ends have ``across / 2`` as their radius, so the top
    end is concentric with the tool circle and the bottom end wraps the
    options circle. ``along`` runs from that top end, past the tool
    circle, to the far edge of the options circle, plus
    ``CAPSULE_BOTTOM_PAD`` of extra body so the lower control is not
    tangent to the contour.
    """
    across = 2 * CAPSULE_PAD + CAPSULE_TOOL_D
    along = (CAPSULE_PAD + CAPSULE_TOOL_D / 2.0 + capsule_advance()
             + CAPSULE_DOTS_D / 2.0 + CAPSULE_BOTTOM_PAD)
    return (int(round(along)), across) if horizontal else (
        across, int(round(along)))


def _capsule_outline(rect: QRectF) -> QPainterPath:
    """The capsule's own contour: ONE continuous rounded rectangle.

    The end radius is half the short side, so both ends are true round
    caps. Deliberately not the union of the two circles: merged circles
    produce a peanut. Inset by half a pixel so the 1px border stroked on
    this contour stays inside the widget.
    """
    inset = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    radius = min(inset.width(), inset.height()) / 2.0
    outline = QPainterPath()
    outline.addRoundedRect(inset, radius, radius)
    return outline


class ToolCapsule(QWidget):
    """ONE continuous capsule around the tool button and the options button.

    Purely visual and mouse-transparent (the slot buttons underneath keep
    their own hit targets, focus rings and keyboard activation). It paints
    a single near-black backdrop:

    * the capsule's body — one rounded outline, concentric with the tool's
      own circular selection halo at its top end, so that halo and the
      glyph are simply painted over it by the slot button;
    * the options button's own opaque disc and its three dots;
    * the ONE active-control highlight, a restrained wash disc that sits
      behind the tool's circle and travels to the options circle while the
      properties popup is open — never two highlights at once.

    The slot's z-order does the work the old hole used to do: the capsule
    is kept below the tiles, so the selected tool's halo/glyph and any
    neighbouring glyph are painted on top of it and can never be covered.
    No path subtraction is involved, so no fill rule can fill anything
    back in.
    """

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._tool_center = QPointF(0.0, 0.0)
        self._dots_center = QPointF(0.0, 0.0)
        self._options: CapsuleOptionsButton | None = None
        # The ONE active-control highlight: 0.0 = behind the tool circle,
        # 1.0 = behind the options circle. Never two highlights at once.
        self._highlight = 0.0
        self._highlight_target = 0.0
        self._highlight_on = False
        self._highlight_anim = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAutoFillBackground(False)

    # -- geometry ---------------------------------------------------------

    def set_circles(self, tool_center: QPointF,
                    dots_center: QPointF) -> None:
        """Both inner circle centres, in capsule-local coordinates."""
        try:
            self._tool_center = QPointF(tool_center)
            self._dots_center = QPointF(dots_center)
        except Exception:
            self._tool_center = QPointF(0.0, 0.0)
            self._dots_center = QPointF(0.0, 0.0)
        self.update()

    @property
    def tool_center(self) -> QPointF:
        return QPointF(self._tool_center)

    @property
    def dots_center(self) -> QPointF:
        return QPointF(self._dots_center)

    def bind_options_button(self, button: "CapsuleOptionsButton") -> None:
        """The pointer target painted face/state is read from."""
        self._options = button
        try:
            button.bind_visual(self)
        except Exception:
            pass
        self.update()

    # -- active-control highlight -----------------------------------------

    def set_options_active(self, active: bool, animate: bool = True) -> None:
        """Move the ONE highlight between the tool and the options circle.

        ``active`` is True while the properties popup is open: the
        highlight travels to the three-dot control and reads one wash step
        stronger, exactly as the Drawboard reference does. It travels back
        to the tool circle (and back to the resting wash) when the popup
        closes — through the toggle, through an outside click, or through a
        tool switch. There is only ever this single disc: it moves between
        the two positions instead of a second highlight appearing.
        """
        active = bool(active)
        target = 1.0 if active else 0.0
        self._highlight_on = active
        try:
            if (self._highlight_anim is not None
                    and abs(float(self._highlight_target) - target) < 1e-6):
                return  # already gliding exactly there
            if self._highlight_anim is None and abs(
                    float(self._highlight) - target) < 1e-6:
                return  # already settled there
        except Exception:
            pass
        reduce = False
        try:
            from app import fx as _fx

            reduce = bool(_fx.reduced_motion_enabled())
        except Exception:
            reduce = False
        if reduce or not animate:
            self._highlight = target
            self._highlight_target = target
            self._stop_highlight_anim()
            self.update()
            return
        try:
            from PySide6.QtCore import QEasingCurve, QVariantAnimation

            start = float(self._highlight)
            # Parented to the capsule's owner (the shelf), never to the
            # capsule itself: the shelf sweeps a capsule's own animations
            # when it re-establishes or hides the overlay, and that sweep
            # must not strand the highlight halfway between the controls.
            anim = QVariantAnimation(self.parentWidget() or self)
            anim.setDuration(CAPSULE_HIGHLIGHT_MS)
            anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.valueChanged.connect(self._on_highlight_tick)
            anim.finished.connect(
                lambda _t=target, _a=anim: self._on_highlight_done(_t, _a)
            )
            self._stop_highlight_anim()
            self._highlight_target = target
            self._highlight_anim = anim
            anim.start()
        except Exception:
            self._highlight = target
            self._highlight_target = target
            self._stop_highlight_anim()
            self.update()

    def _stop_highlight_anim(self) -> None:
        anim = getattr(self, "_highlight_anim", None)
        self._highlight_anim = None
        try:
            if anim is not None:
                anim.stop()
                anim.deleteLater()
        except (RuntimeError, AttributeError):
            pass

    def _on_highlight_tick(self, value) -> None:
        try:
            self._highlight = max(0.0, min(1.0, float(value)))
        except Exception:
            self._highlight = 1.0 if self._highlight_on else 0.0
        self.update()

    def _on_highlight_done(self, target: float, anim) -> None:
        """Snap a naturally-finished glide onto its exact terminal state."""
        try:
            if getattr(self, "_highlight_anim", None) is not anim:
                return  # superseded mid-flight; a newer glide owns state
            self._highlight = max(0.0, min(1.0, float(target)))
            self._highlight_target = float(target)
        except Exception:
            pass
        self._stop_highlight_anim()
        self.update()

    # -- theme ------------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    # -- paint ------------------------------------------------------------

    def _options_state(self) -> tuple[bool, bool, bool, bool, bool]:
        """``(enabled, hovered, down, focused, open)`` of the pointer target.

        ``focused`` means KEYBOARD focus only: a mouse click focuses the
        button as well, and this control is explicitly ring-free in every
        mouse-driven state (hover, pressed, open).
        """
        button = self._options
        if button is None:
            return (True, False, False, False, False)
        try:
            try:
                focused = bool(button.keyboard_focused)
            except AttributeError:
                focused = bool(button.hasFocus())
            return (button.isEnabled(), button.underMouse(),
                    button.isDown(), focused,
                    bool(button.is_open))
        except Exception:
            return (True, False, False, False, False)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            dots_r = CAPSULE_DOTS_D / 2.0

            outline = _capsule_outline(QRectF(self.rect()))

            # The Drawboard reference's selected state is one solid near-black
            # pill: the body and the options face are the same surface, and
            # the three dots are what mark the options control.
            body_fill = selected_surface(self._tokens)
            edge = token_color(self._tokens, "border", (38, 44, 56, 255))
            edge.setAlpha(255)

            # 1. The capsule's body: one continuous backdrop. Everything the
            #    slot tiles paint (the selected tool's halo and glyph, any
            #    neighbour's glyph) lands on top of this, because the capsule
            #    is kept below the tiles — so nothing has to be punched out.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(body_fill)
            painter.drawPath(outline)

            # 2. The options button: its own opaque disc (the same near-black
            #    surface as the body, so the capsule stays one silhouette).
            self._paint_face(painter, dots_r)

            # 3. The ONE active-control highlight, over both control faces
            #    and under the glyphs/dots: it moves between the tool's
            #    circle and the options circle and is what tells which of
            #    the two controls is live. The slot tiles paint over the
            #    capsule, so the tool's own icon is never obscured by it.
            self._paint_highlight(painter)

            # 4. The three dots last, on top of the highlight.
            self._paint_dots(painter, dots_r)

            # 5. The capsule's own hairline last, so the contour stays
            #    defined across the body and around the disc's rim.
            pen = QPen(edge)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(outline)
        except Exception:
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass

    def _paint_face(self, painter: QPainter, radius: float) -> None:
        """The options button's face: an opaque near-black disc.

        The same surface as the capsule's body (the reference's selected
        state is one solid black pill), so by default the two merge into
        the single continuous capsule. Hover and pressed read from
        :class:`CapsuleOptionsButton` and answer with the shared *fill*
        language: one restrained ink wash over that disc — never the
        accent ring this used to draw. The circular silhouette never
        changes, in any state.

        The open state is deliberately NOT painted here: while the popup
        is open the single active-control highlight
        (:meth:`_paint_highlight`) is the disc's fill, so the two can
        never stack into a stronger wash than the language defines.
        """
        enabled, hovered, down, _focused, open_ = self._options_state()
        centre = self._dots_center

        if not enabled:
            painter.setOpacity(0.4)
        try:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(selected_surface(self._tokens))
            painter.drawEllipse(centre, radius, radius)

            if (hovered or down) and not open_:
                painter.setBrush(
                    hover_wash(self._tokens, pressed=down))
                painter.drawEllipse(centre, radius, radius)
        finally:
            if not enabled:
                painter.setOpacity(1.0)

    def _paint_highlight(self, painter: QPainter) -> None:
        """The single active-control highlight, at its interpolated centre.

        ONE 30px disc — the same diameter as both control areas, as
        specified — carrying a restrained ink wash of the capsule's own
        body. It sits behind the tool's circle while the tool itself is
        active and travels to the options circle while the properties
        popup is open, which is what makes the live control obvious on the
        solid black capsule without a ring, a border or a second shape.
        The slot tiles paint above the capsule, so the selected tool's
        icon and the three dots are never obscured by it.
        """
        try:
            along = max(0.0, min(1.0, float(self._highlight)))
        except Exception:
            along = 0.0
        tool = self._tool_center
        dots = self._dots_center
        centre = QPointF(
            tool.x() + (dots.x() - tool.x()) * along,
            tool.y() + (dots.y() - tool.y()) * along,
        )
        radius = CAPSULE_TOOL_D / 2.0
        enabled, _hovered, down, _focused, open_ = self._options_state()
        try:
            if not enabled:
                painter.setOpacity(0.4)
            painter.setPen(Qt.PenStyle.NoPen)
            # Strength follows the state the travel is heading to: an
            # opening gesture stays at the active wash all the way, a
            # closing one settles back to the resting wash on arrival.
            pressed = bool(open_ or down or self._highlight_on)
            painter.setBrush(hover_wash(self._tokens, pressed=pressed))
            painter.drawEllipse(centre, radius, radius)
        except Exception:
            pass
        finally:
            if not enabled:
                painter.setOpacity(1.0)

    def _paint_dots(self, painter: QPainter, radius: float) -> None:
        """The three light dots and the focus ring, over the face."""
        enabled, hovered, down, focused, open_ = self._options_state()
        centre = self._dots_center

        if not enabled:
            painter.setOpacity(0.4)
        try:
            # The theme's lightest ink, so the dots stay clearly visible on
            # that black face. Centred on the disc's centre, and on the same
            # crisp grid for all three (one integer gap keeps the cluster
            # symmetric instead of snapping only the middle dot).
            dots = ink_color(self._tokens)
            dots.setAlpha(
                255 if (hovered or down or focused or open_) else 226)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dots)
            for offset in (-CAPSULE_DOT_GAP, 0.0, CAPSULE_DOT_GAP):
                painter.drawEllipse(
                    QPointF(centre.x() + offset, centre.y()),
                    CAPSULE_DOT_R, CAPSULE_DOT_R)

            if focused:
                focus = token_color(self._tokens, "input_focus",
                                    (139, 147, 224, 255))
                focus_pen = QPen(focus)
                focus_pen.setWidthF(1.0)
                focus_pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(focus_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(centre, radius - 1.75, radius - 1.75)
        finally:
            if not enabled:
                painter.setOpacity(1.0)


class CapsuleOptionsButton(QAbstractButton):
    """Pointer target for the painted three-dot button inside the capsule.

    Deliberately paints nothing: the options *face* is part of the shared
    capsule visual, which is allowed to be a ``CAPSULE_DOTS_D`` circle even
    though the clickable band the slot's own pitch offers is shorter than
    that. This widget's geometry is exactly that band — the face clipped on
    the toolbar axis to the selected slot's own cell plus the following
    inter-slot gap — so a click over the painted overflow reaches the
    neighbouring tool instead of this button, and never the other way
    round.

    It still owns the interaction contract: cursor, focus policy,
    accessible name, hover / pressed / open state and the toggle signal.
    """

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._visual: ToolCapsule | None = None
        self._open = False
        # Keyboard-only focus ring (a mouse click must leave no ring).
        self._keyboard_focus = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        # Inkfall ships no application tooltips: the accessible name and
        # description carry the affordance's meaning.
        self.setAccessibleName("Tool properties")
        self.setAccessibleDescription(
            "Open or close the properties for the selected toolbar tool")

    # -- theme ------------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._refresh()

    def bind_visual(self, capsule: ToolCapsule | None) -> None:
        """The shared capsule that paints this button's face."""
        self._visual = capsule
        self._refresh()

    @property
    def is_open(self) -> bool:
        """True while the properties popup this button owns is open."""
        return bool(getattr(self, "_open", False))

    def set_open(self, open_: bool) -> None:
        """Mark the popup's open state (one stronger fill, never a ring).

        The open state is part of the same restrained fill language: the
        capsule's single active-control highlight travels to this control
        and takes the stronger wash while the popup is open, and travels
        back to the selected tool's own circle when it closes.
        """
        changed = bool(open_) != bool(self._open)
        self._open = bool(open_)
        self._refresh()
        if changed:
            try:
                if self._visual is not None:
                    self._visual.set_options_active(self._open)
            except Exception:
                pass

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(CAPSULE_DOTS_D, CAPSULE_DOTS_D)

    def _refresh(self) -> None:
        """Repaint this target and the face the capsule paints for it."""
        try:
            self.update()
        except Exception:
            pass
        try:
            if self._visual is not None:
                self._visual.update()
        except Exception:
            pass

    # -- interaction state -------------------------------------------------

    def enterEvent(self, event) -> None:  # noqa: N802
        self._refresh()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._refresh()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        self._refresh()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        self._refresh()

    def focusInEvent(self, event) -> None:  # noqa: N802
        try:
            reason = event.reason()
            self._keyboard_focus = reason in (
                Qt.FocusReason.TabFocusReason,
                Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason)
        except Exception:
            self._keyboard_focus = False
        super().focusInEvent(event)
        self._refresh()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self._keyboard_focus = False
        super().focusOutEvent(event)
        self._refresh()

    @property
    def keyboard_focused(self) -> bool:
        """True only for keyboard-driven focus (never a mouse click)."""
        try:
            return bool(self._keyboard_focus) and self.hasFocus()
        except Exception:
            return False

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        try:
            if event.type() == event.Type.EnabledChange:
                self._refresh()
        except Exception:
            pass

    # -- paint ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        """Nothing to paint: the face belongs to the shared capsule.

        The capsule is a sibling (transparent for mouse events, so it
        never intercepts a click) and paints the full circular face in
        this region; over-painting here would only re-cover it with the
        widget's own smaller rectangle.
        """
