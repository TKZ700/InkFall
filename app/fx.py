"""Reusable micro-interaction helpers (M4.6.5). Sole animation owner.

Short, lightweight, non-blocking: fades, stacked-page transitions,
slide+fades, press feedback, bounded scroll smoothing. All durations
come from tokens or callers (90–250ms); easing is OutCubic throughout.
Animations never run PDF work and never gate document opening.

Motion is Inkfall's visual design language: helpers always animate
under normal conditions. Reduced motion is supported globally —
manual override (Settings -> Interface) wins over the OS preference,
which wins over normal animations. When reduced motion is enabled,
every helper resolves immediately to the same final state (with the
same cleanup and ``finished`` callbacks), so callers never branch.
Every helper stays interruptible, container-level, and leak-free.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, Qt
from PySide6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget, QWidget

#: QSettings key for the manual motion override (M5-A1).
#: Values: "follow" (default) | "reduced" | "full".
MOTION_SETTINGS_KEY = "inkfall/m5/motion"

_VALID_MOTION_MODES = ("follow", "reduced", "full")

_motion_mode: str = "follow"


def set_motion_mode(mode: str) -> str:
    """Set the manual motion override; returns the normalized mode."""
    global _motion_mode
    normalized = str(mode or "follow").strip().lower()
    if normalized not in _VALID_MOTION_MODES:
        normalized = "follow"
    _motion_mode = normalized
    try:
        from PySide6.QtCore import QSettings

        QSettings().setValue(MOTION_SETTINGS_KEY, normalized)
    except Exception:
        pass
    return normalized


def motion_mode() -> str:
    """Current manual motion override (never the resolved state)."""
    return _motion_mode


def restore_motion_mode() -> str:
    """Reload the manual override from QSettings (app startup path)."""
    try:
        from PySide6.QtCore import QSettings

        stored = str(QSettings().value(MOTION_SETTINGS_KEY, "follow") or "follow")
    except Exception:
        stored = "follow"
    return _set_motion_mode_silent(stored)


def _set_motion_mode_silent(mode: str) -> str:
    global _motion_mode
    normalized = str(mode or "follow").strip().lower()
    if normalized not in _VALID_MOTION_MODES:
        normalized = "follow"
    _motion_mode = normalized
    return normalized


def _os_prefers_reduced_motion() -> bool:
    """Best-effort OS reduced-motion probe (never raises)."""
    try:
        if str(os.environ.get("QT_REDUCED_MOTION", "")).strip() == "1":
            return True
        if str(os.environ.get("INKFALL_REDUCED_MOTION", "")).strip() == "1":
            return True
    except Exception:
        pass
    try:
        import sys as _sys

        if _sys.platform == "win32":
            try:
                import winreg as _reg

                with _reg.OpenKey(
                    _reg.HKEY_CURRENT_USER, r"Control Panel\Desktop"
                ) as key:
                    try:
                        value, _kind = _reg.QueryValueEx(key, "MenuAnimation")
                        if str(value).strip() == "0":
                            return True
                    except FileNotFoundError:
                        pass
            except Exception:
                pass
    except Exception:
        pass
    return False


def reduced_motion_enabled() -> bool:
    """Resolve manual override > OS preference > normal animations."""
    mode = _motion_mode
    if mode == "reduced":
        return True
    if mode == "full":
        return False
    return _os_prefers_reduced_motion()


def _effective_duration(duration_ms: int) -> int:
    """Force instant resolution when reduced motion is enabled."""
    if reduced_motion_enabled():
        return 0
    return int(duration_ms)

__all__ = [
    "MOTION_SETTINGS_KEY",
    "fade_in",
    "fade_out",
    "fade_stack_to",
    "motion_mode",
    "reduced_motion_enabled",
    "restore_motion_mode",
    "set_motion_mode",
    "slide_fade_in",
    "slide_fade_out",
    "slide_fade_stack_to",
    "startup_entrance",
    "smooth_scroll_to",
    "stop_smooth_scroll",
    "press_feedback",
    "hover_lift",
    "hover_sink",
    "install_hover_lift",
    "tween_width",
    "animate_menu_open",
    "window_fade",
    "window_tween_geometry",
]


def _easing() -> QEasingCurve:
    return QEasingCurve(QEasingCurve.Type.OutCubic)


def subtree_has_quick_widget(widget) -> bool:
    """True when ``widget`` or any descendant is a QQuickWidget.

    M5-A2.2.2: a QWidget opacity effect on an ancestor of a QQuickWidget
    forces the effect-source pixmap path (``QWidgetEffectSourcePrivate``)
    to capture the Quick framebuffer inside a QWidget paint cycle — the
    reported ``QPainter::begin ... one painter at a time`` / ``Painter
    not active`` storm. Callers must use motion-only or QML-side opacity
    instead (never ``QGraphicsOpacityEffect``) on such subtrees.
    """
    try:
        from PySide6.QtQuickWidgets import QQuickWidget as _QW

        if isinstance(widget, _QW):
            return True
        try:
            if widget is not None and widget.findChildren(_QW):
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _slide_only(widget: QWidget, dx: int, dy: int, duration_ms: int,
                parent: QObject | None = None, finished=None,
                hide_on_finish: bool = False):
    """Position-only entrance/exit (no opacity effect, QML-safe).

    Same travel clamp, easing and duration as the slide+fade helpers so
    timing is preserved where a QWidget fade is incompatible with
    QQuickWidget descendants.
    """
    from PySide6.QtCore import QParallelAnimationGroup

    dx = max(-12, min(12, int(dx)))
    dy = max(-12, min(12, int(dy)))
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass
    try:
        widget.setVisible(True)
    except Exception:
        pass
    if not dx and not dy:
        try:
            if finished is not None:
                finished()
        except Exception:
            pass
        if hide_on_finish:
            try:
                widget.setVisible(False)
            except Exception:
                pass
        return None
    owner = parent or widget
    group = QParallelAnimationGroup(owner)
    try:
        from PySide6.QtCore import QPoint

        if hide_on_finish:
            start = widget.pos()
            slide = QPropertyAnimation(widget, b"pos", group)
            slide.setDuration(max(1, int(duration_ms)))
            slide.setEasingCurve(_easing())
            slide.setStartValue(start)
            slide.setEndValue(start + QPoint(dx, dy))
            group.addAnimation(slide)
        else:
            start = widget.pos()
            widget.move(start.x() + dx, start.y() + dy)
            slide = QPropertyAnimation(widget, b"pos", group)
            slide.setDuration(max(1, int(duration_ms)))
            slide.setEasingCurve(_easing())
            slide.setStartValue(start + QPoint(dx, dy))
            slide.setEndValue(start)
            group.addAnimation(slide)
    except Exception:
        pass

    def _done() -> None:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        if hide_on_finish:
            try:
                widget.setVisible(False)
            except Exception:
                pass
        try:
            if finished is not None:
                finished()
        except Exception:
            pass

    try:
        group.finished.connect(_done)
    except Exception:
        pass
    try:
        group.start()
    except Exception:
        pass
    return group


def _scroll_easing() -> QEasingCurve:
    """Linear follow for scrollbar glides (M4.6.6).

    Measured: OutCubic's fast-head/slow-tail + integer scrollbar
    steps left the viewport static in >90% of frames between wheel
    ticks (24 distinct values over ~90 frames). Linear moves every
    frame; the velocity-preserving retarget in ``smooth_scroll_to``
    keeps consecutive ticks continuous instead of jumpy.
    """
    return QEasingCurve(QEasingCurve.Type.Linear)


def fade_in(widget: QWidget, duration_ms: int = 180, parent: QObject | None = None) -> None:
    """Fade a widget from transparent to opaque (fire-and-forget).

    M5-A2.2.2: refuses the QWidget opacity effect when the subtree
    contains a QQuickWidget (effect-source pixmap would repaint the
    Quick framebuffer reentrantly). Callers animating QML containers
    must use QML-side opacity or motion-only instead; here we resolve
    to a stable visible end-state with no effect.
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        widget.setVisible(True)
        return
    if subtree_has_quick_widget(widget):
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        try:
            widget.setVisible(True)
        except Exception:
            pass
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", parent or widget)
    anim.setDuration(max(1, duration_ms))
    anim.setEasingCurve(_easing())
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start()


def fade_out(widget: QWidget, duration_ms: int = 140,
             parent: QObject | None = None, hide: bool = True) -> None:
    """Fade a widget to transparent (fire-and-forget, modal closes).

    M5-A2.2.2: same QML guard as :func:`fade_in` — no opacity effect
    over a QQuickWidget subtree. Resolves to the same end-state
    (hidden when ``hide``) without an effect.
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        if hide:
            widget.setVisible(False)
        return
    if subtree_has_quick_widget(widget):
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        if hide:
            try:
                widget.setVisible(False)
            except Exception:
                pass
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", parent or widget)
    anim.setDuration(max(1, duration_ms))
    anim.setEasingCurve(_easing())
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)

    def _done() -> None:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        if hide:
            widget.setVisible(False)

    anim.finished.connect(_done)
    anim.start()


def fade_stack_to(stack: QStackedWidget, index: int, duration_ms: int = 180) -> None:
    """Cross-fade a QStackedWidget page."""
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        stack.setCurrentIndex(index)
        return
    target = stack.widget(index)
    if target is None or stack.currentIndex() == index:
        stack.setCurrentIndex(index)
        return
    effect = QGraphicsOpacityEffect(target)
    effect.setOpacity(0.0)
    target.setGraphicsEffect(effect)
    stack.setCurrentIndex(index)
    anim = QPropertyAnimation(effect, b"opacity", stack)
    anim.setDuration(max(1, duration_ms))
    anim.setEasingCurve(_easing())
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.finished.connect(lambda: target.setGraphicsEffect(None))
    anim.start()


def slide_fade_stack_to(stack: QStackedWidget, index: int, duration_ms: int = 200,
                         parent: QObject | None = None, dx_px: int = 0,
                         dy_px: int = 0):
    """Directional workspace transition (M4.6.6 Library <-> PDF).

    Instant page switch — rendering/tiles never wait — then a tiny
    slide+fade on the incoming page (OutCubic). Same motion language
    both directions; callers pick the sign of the shift.
    """
    try:
        current = stack.currentIndex()
    except Exception:
        current = -1
    stack.setCurrentIndex(index)
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0 or current == index:
        return None
    try:
        target = stack.widget(index)
    except Exception:
        return None
    if target is None:
        return None
    return slide_fade_in(target, dx_px, duration_ms, parent or stack, dy_px=dy_px)


def startup_entrance(widget: QWidget, duration_ms: int = 220,
                     parent: QObject | None = None):
    """Visible-but-restrained startup: opacity 0.94 -> 1 + 5px rise.

    One-shot, non-blocking; the window is usable immediately.
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        return None
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.94)
    widget.setGraphicsEffect(effect)
    owner = parent or widget
    from PySide6.QtCore import QParallelAnimationGroup, QPoint

    group = QParallelAnimationGroup(owner)
    fade = QPropertyAnimation(effect, b"opacity", group)
    fade.setDuration(max(1, int(duration_ms)))
    fade.setEasingCurve(_easing())
    fade.setStartValue(0.94)
    fade.setEndValue(1.0)
    group.addAnimation(fade)
    try:
        start = widget.pos()
        widget.move(start.x(), start.y() + 5)
        slide = QPropertyAnimation(widget, b"pos", group)
        slide.setDuration(max(1, int(duration_ms)))
        slide.setEasingCurve(_easing())
        slide.setStartValue(start + QPoint(0, 5))
        slide.setEndValue(start)
        group.addAnimation(slide)
    except Exception:
        pass
    group.finished.connect(lambda: widget.setGraphicsEffect(None))
    group.start()
    return group


def slide_fade_in(widget: QWidget, dx_px: int = 0, duration_ms: int = 160,
                  parent: QObject | None = None, dy_px: int = 0) -> None:
    """Container-level slide+fade (filter changes, modal entrances).

    Tiny travel (≤12px per axis) + opacity, OutCubic. Kills any prior
    effect on the widget so rapid re-triggers never stack animations.
    Never gates I/O or rendering.

    M5-A2.2.2: over a QQuickWidget subtree the opacity half is skipped
    (slide-only, same travel/duration/easing) to avoid the
    effect-source/QPainter conflict; QML content fades via its own
    QML-side opacity where offered.
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        widget.setVisible(True)
        return
    if subtree_has_quick_widget(widget):
        return _slide_only(widget, dx_px, dy_px, duration_ms, parent)
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass
    dx = max(-12, min(12, int(dx_px)))
    dy = max(-12, min(12, int(dy_px)))
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    owner = parent or widget
    from PySide6.QtCore import QParallelAnimationGroup
    group = QParallelAnimationGroup(owner)
    fade = QPropertyAnimation(effect, b"opacity", group)
    fade.setDuration(max(1, duration_ms))
    fade.setEasingCurve(_easing())
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    group.addAnimation(fade)
    if dx or dy:
        try:
            from PySide6.QtCore import QPoint

            start = widget.pos()
            widget.move(start.x() + dx, start.y() + dy)
            slide = QPropertyAnimation(widget, b"pos", group)
            slide.setDuration(max(1, duration_ms))
            slide.setEasingCurve(_easing())
            slide.setStartValue(start + QPoint(dx, dy))
            slide.setEndValue(start)
            group.addAnimation(slide)
        except Exception:
            pass
    group.finished.connect(lambda: widget.setGraphicsEffect(None))
    group.start()
    return group


def slide_fade_out(widget: QWidget, dx_px: int = 0, duration_ms: int = 140,
                   parent: QObject | None = None, dy_px: int = 0,
                   hide: bool = True):
    """Container-level fade + tiny reverse shift (modal closes).

    Mirror of :func:`slide_fade_in`; hides the widget on completion.

    M5-A2.2.2: over a QQuickWidget subtree the opacity half is skipped
    (slide-only then hide, same timing) to avoid the effect-source
    conflict.
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        if hide:
            widget.setVisible(False)
        return
    if subtree_has_quick_widget(widget):
        return _slide_only(widget, dx_px, dy_px, duration_ms, parent,
                           hide_on_finish=hide)
    try:
        widget.setGraphicsEffect(None)
    except Exception:
        pass
    dx = max(-12, min(12, int(dx_px)))
    dy = max(-12, min(12, int(dy_px)))
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)
    owner = parent or widget
    from PySide6.QtCore import QParallelAnimationGroup, QPoint

    group = QParallelAnimationGroup(owner)
    fade = QPropertyAnimation(effect, b"opacity", group)
    fade.setDuration(max(1, duration_ms))
    fade.setEasingCurve(_easing())
    fade.setStartValue(1.0)
    fade.setEndValue(0.0)
    group.addAnimation(fade)
    if dx or dy:
        try:
            start = widget.pos()
            slide = QPropertyAnimation(widget, b"pos", group)
            slide.setDuration(max(1, duration_ms))
            slide.setEasingCurve(_easing())
            slide.setStartValue(start)
            slide.setEndValue(start + QPoint(dx, dy))
            group.addAnimation(slide)
        except Exception:
            pass

    def _done() -> None:
        try:
            widget.setGraphicsEffect(None)
        except Exception:
            pass
        if hide:
            widget.setVisible(False)

    group.finished.connect(_done)
    group.start()
    return group


def smooth_scroll_to(bar, to_value: int, duration_ms: int = 140,
                      parent: QObject | None = None,
                      max_travel: int | None = None,
                      on_finished=None) -> None:
    """Bounded scrollbar glide (wheel smoothing), always enabled.

    Single QPropertyAnimation on the scrollbar value, re-targetable: a
    new call stops the previous glide first, so rapid wheel input never
    stacks animations. Retargets are velocity-preserving — the new
    input extends the in-flight target instead of restarting from the
    current value (no restart stutter). Never blocks the GUI thread;
    virtualization unaffected (viewport-level).     ``max_travel``
    optionally clamps the glide distance from the current value (no
    excessive momentum). ``on_finished`` (optional callable) runs once
    the settled value is in place — including the instant paths — so
    callers can resync dependent state (e.g. overflow arrows) from the
    actual final position.
    """
    def _notify_finished() -> None:
        if on_finished is None:
            return
        try:
            on_finished()
        except Exception:
            pass

    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0 or bar is None:
        try:
            bar.setValue(int(to_value))
        except Exception:
            pass
        _notify_finished()
        return
    try:
        active = False
        try:
            previous = bar.property("_inkfall_glide")
            active = previous is not None
            if active:
                previous.stop()
        except Exception:
            pass
        lo, hi = bar.minimum(), bar.maximum()
        if active:
            # Velocity-preserving retarget: extend the in-flight target
            # by this tick's delta instead of restarting from scratch.
            try:
                prev_target = bar.property("_inkfall_target")
                follow = (int(prev_target) if isinstance(prev_target, (int, float))
                          else bar.value())
            except Exception:
                follow = bar.value()
            target = max(lo, min(hi, follow + (int(to_value) - bar.value())))
        else:
            target = max(lo, min(hi, int(to_value)))
        if max_travel is not None:
            span = max(0, int(max_travel))
            target = max(bar.value() - span, min(bar.value() + span, target))
        if target == bar.value():
            try:
                bar.setProperty("_inkfall_glide", None)
                bar.setProperty("_inkfall_target", None)
            except Exception:
                pass
            _notify_finished()
            return
        anim = QPropertyAnimation(bar, b"value", parent or bar)
        anim.setDuration(max(1, int(duration_ms)))
        anim.setEasingCurve(_scroll_easing())
        anim.setStartValue(bar.value())
        anim.setEndValue(target)

        def _release() -> None:
            try:
                if bar.property("_inkfall_glide") is anim:
                    bar.setProperty("_inkfall_glide", None)
                    bar.setProperty("_inkfall_target", None)
            except Exception:
                pass
            _notify_finished()

        anim.finished.connect(_release)
        anim.start()
        try:
            bar.setProperty("_inkfall_glide", anim)
            bar.setProperty("_inkfall_target", int(target))
        except Exception:
            pass
    except Exception:
        try:
            bar.setValue(int(to_value))
        except Exception:
            pass
        _notify_finished()


def stop_smooth_scroll(bar) -> None:
    """Cancel any in-flight glide (used on teardown paths)."""
    try:
        previous = bar.property("_inkfall_glide") if bar is not None else None
        if previous is not None:
            previous.stop()
        if bar is not None:
            bar.setProperty("_inkfall_glide", None)
            bar.setProperty("_inkfall_target", None)
    except Exception:
        pass


def press_feedback(widget: QWidget, duration_ms: int = 90) -> None:
    """Subtle press acknowledgment (brief opacity dip, restores itself)."""
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        return
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(max(1, duration_ms // 2))
    anim.setStartValue(1.0)
    anim.setEndValue(0.55)
    anim.setEasingCurve(_easing())

    def _restore() -> None:
        back = QPropertyAnimation(effect, b"opacity", widget)
        back.setDuration(max(1, duration_ms // 2))
        back.setStartValue(0.55)
        back.setEndValue(1.0)
        back.finished.connect(lambda: widget.setGraphicsEffect(None))
        back.start()

    anim.finished.connect(_restore)
    anim.start()


def cursor_overrides_supported() -> bool:
    """Capability probe (kept for tests)."""
    return hasattr(Qt.CursorShape, "PointingHandCursor")


# -- tactile hover/press (M5-A1) -----------------------------------------
# Lift + shadow instead of bright-surface hover swaps: text/icon colors
# are always preserved. Painted cells (swatches, visual tiles, shelf
# slots) implement the same language inside their paint passes (1-2px
# content translation); these helpers cover real QWidget controls
# (popup buttons, cards) via a drop-shadow bump that never fights the
# layout manager. Reduced motion resolves to the same end state.


def hover_lift(widget: QWidget, rise_px: int = 2,
               duration_ms: int = 120) -> None:
    """Grow the control's shadow (lift read); colors untouched."""
    try:
        from PySide6.QtWidgets import QGraphicsDropShadowEffect

        from PySide6.QtGui import QColor as _QC
    except Exception:
        return
    try:
        duration_ms = _effective_duration(duration_ms)
        effect = widget.graphicsEffect()
        if not isinstance(effect, QGraphicsDropShadowEffect):
            try:
                old = effect
                if old is not None:
                    widget.setGraphicsEffect(None)
            except Exception:
                pass
            effect = QGraphicsDropShadowEffect(widget)
            effect.setColor(_QC(0, 0, 0, 110))
            effect.setOffset(0, 1)
            effect.setBlurRadius(8)
            widget.setGraphicsEffect(effect)
        try:
            widget.setProperty("_inkfall_hover", True)
        except Exception:
            pass
        if duration_ms <= 0:
            effect.setBlurRadius(20)
            effect.setOffset(0, 1 + max(1, int(rise_px)))
            return
        for prop, end in (("blurRadius", 20.0),
                          ("yOffset", float(1 + max(1, int(rise_px))))):
            try:
                anim = QPropertyAnimation(effect, prop.encode(), widget)
                anim.setDuration(max(1, int(duration_ms)))
                anim.setEasingCurve(_easing())
                anim.setEndValue(end)
                anim.start()
            except Exception:
                pass
        try:
            effect.setBlurRadius(20)
            effect.setOffset(0, 1 + max(1, int(rise_px)))
        except Exception:
            pass
    except Exception:
        pass


def hover_sink(widget: QWidget, duration_ms: int = 120) -> None:
    """Return the control's shadow to rest (leave path of hover_lift)."""
    try:
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
    except Exception:
        return
    try:
        duration_ms = _effective_duration(duration_ms)
        effect = widget.graphicsEffect()
        if not isinstance(effect, QGraphicsDropShadowEffect):
            return
        try:
            widget.setProperty("_inkfall_hover", False)
        except Exception:
            pass
        if duration_ms <= 0:
            try:
                widget.setGraphicsEffect(None)
            except Exception:
                pass
            return

        def _clear() -> None:
            try:
                if not widget.property("_inkfall_hover"):
                    widget.setGraphicsEffect(None)
            except Exception:
                pass

        try:
            anim = QPropertyAnimation(effect, b"blurRadius", widget)
            anim.setDuration(max(1, int(duration_ms)))
            anim.setEasingCurve(_easing())
            anim.setEndValue(8.0)
            try:
                anim.finished.connect(_clear)
            except Exception:
                pass
            anim.start()
        except Exception:
            _clear()
        try:
            effect.setOffset(0, 1)
        except Exception:
            pass
    except Exception:
        pass


class _HoverLiftFilter(QObject):
    """Enter/leave filter wiring hover_lift/hover_sink to a control."""

    def __init__(self, widget: QWidget, rise_px: int = 2) -> None:
        super().__init__(widget)
        self._rise = rise_px

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        try:
            etype = event.type()
            from PySide6.QtCore import QEvent as _QE

            if etype == _QE.Type.Enter:
                hover_lift(watched, self._rise)
            elif etype == _QE.Type.Leave:
                hover_sink(watched)
        except Exception:
            pass
        return False


def install_hover_lift(widget: QWidget, rise_px: int = 2) -> QWidget:
    """Attach lift-on-hover to a QWidget control; returns the widget."""
    try:
        if widget.property("_inkfall_hover_filter") is not None:
            return widget  # idempotent across token refreshes
        filt = _HoverLiftFilter(widget, rise_px)
        widget.installEventFilter(filt)
        try:
            widget.setProperty("_inkfall_hover_filter", filt)
        except Exception:
            pass
    except Exception:
        pass
    return widget


# -- top-level window motion (M4.6.8) --------------------------------------
# These tween the ACTUAL top-level window (opacity/geometry), never
# content widgets. Fire-and-forget with an optional ``finished``
# callback; callers own epoch guards. Windows owns minimize/maximize/
# fullscreen border crossings, so restore-type paths pair a short dip
# (masking the compositor-owned snap) with a visible settle — the
# maximum honest native-compatible feedback, documented per call site.


def window_fade(window: QWidget, to_opacity: float, duration_ms: int = 120,
                parent: QObject | None = None, finished=None):
    """Tween a top-level window's opacity (OutCubic, non-blocking).

    Returns the animator (callers may stop it to preempt).
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        try:
            window.setWindowOpacity(max(0.0, min(1.0, float(to_opacity))))
        except Exception:
            pass
        if finished is not None:
            try:
                finished()
            except Exception:
                pass
        return None
    try:
        anim = QPropertyAnimation(window, b"windowOpacity", parent or window)
        anim.setDuration(max(1, int(duration_ms)))
        anim.setEasingCurve(_easing())
        try:
            anim.setStartValue(float(window.windowOpacity()))
        except Exception:
            anim.setStartValue(1.0)
        anim.setEndValue(max(0.0, min(1.0, float(to_opacity))))
        if finished is not None:
            anim.finished.connect(finished)
        anim.start()
        return anim
    except Exception:
        try:
            window.setWindowOpacity(max(0.0, min(1.0, float(to_opacity))))
        except Exception:
            pass
        if finished is not None:
            try:
                finished()
            except Exception:
                pass
        return None


def window_tween_geometry(window: QWidget, to_rect, duration_ms: int = 200,
                          parent: QObject | None = None, finished=None) -> None:
    """Tween a top-level window's real geometry (OutCubic, non-blocking).

    Only meaningful while the window is in normal state (maximized /
    fullscreen windows ignore geometry changes — callers must leave
    those states first and document the seam).

    Returns the animator (callers may stop it to preempt).
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        try:
            from PySide6.QtCore import QRect as _QR

            window.setGeometry(_QR(to_rect))
        except Exception:
            try:
                window.setGeometry(to_rect)
            except Exception:
                pass
        if finished is not None:
            try:
                finished()
            except Exception:
                pass
        return None
    try:
        anim = QPropertyAnimation(window, b"geometry", parent or window)
        anim.setDuration(max(1, int(duration_ms)))
        anim.setEasingCurve(_easing())
        try:
            from PySide6.QtCore import QRect

            target = QRect(to_rect)
        except Exception:
            target = to_rect
        try:
            anim.setStartValue(window.geometry())
        except Exception:
            pass
        anim.setEndValue(target)
        if finished is not None:
            anim.finished.connect(finished)
        anim.start()
        return anim
    except Exception:
        if finished is not None:
            try:
                finished()
            except Exception:
                pass
        return None


def tween_width(widget: QWidget, to_width: int, duration_ms: int = 200,
                parent: QObject | None = None, finished=None) -> None:
    """Animate a widget's width (search expand/collapse). Ends exact.

    Parallel minimumWidth/maximumWidth pair, OutCubic, fire-and-forget
    with an optional ``finished`` callback (e.g. hide on collapse).
    Rapid re-triggers restart cleanly (prior pair is killed first).
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        widget.setFixedWidth(max(0, int(to_width)))
        try:
            if finished is not None:
                finished()
        except Exception:
            pass
        return
    try:
        previous = widget.property("_inkfall_tween")
        if previous is not None:
            try:
                previous.stop()
            except Exception:
                pass
    except Exception:
        pass
    from PySide6.QtCore import QParallelAnimationGroup

    group = QParallelAnimationGroup(parent or widget)
    for prop in (b"minimumWidth", b"maximumWidth"):
        anim = QPropertyAnimation(widget, prop, group)
        anim.setDuration(max(1, int(duration_ms)))
        anim.setEasingCurve(_easing())
        anim.setStartValue(widget.width())
        anim.setEndValue(max(0, int(to_width)))
        group.addAnimation(anim)

    def _done() -> None:
        try:
            widget.setFixedWidth(max(0, int(to_width)))
        except Exception:
            pass
        try:
            if widget.property("_inkfall_tween") is group:
                widget.setProperty("_inkfall_tween", None)
        except Exception:
            pass
        try:
            if finished is not None:
                finished()
        except Exception:
            pass

    group.finished.connect(_done)
    group.start()
    try:
        widget.setProperty("_inkfall_tween", group)
    except Exception:
        pass


def tween_height(widget: QWidget, to_height: int, duration_ms: int = 160,
                 parent: QObject | None = None, finished=None) -> None:
    """Animate a widget's height (collapsible section reveal). Ends exact.

    Parallel minimumHeight/maximumHeight pair, OutCubic, fire-and-forget
    with an optional ``finished`` callback. Rapid re-triggers restart
    cleanly (prior pair is killed first). Reduced motion snaps instantly
    and still invokes ``finished``. Height twin of :func:`tween_width`
    (separate ``_inkfall_tween_h`` slot so width/height tweens on one
    widget never cancel each other).
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        widget.setFixedHeight(max(0, int(to_height)))
        try:
            if finished is not None:
                finished()
        except Exception:
            pass
        return
    try:
        previous = widget.property("_inkfall_tween_h")
        if previous is not None:
            try:
                previous.stop()
            except Exception:
                pass
    except Exception:
        pass
    from PySide6.QtCore import QParallelAnimationGroup

    group = QParallelAnimationGroup(parent or widget)
    for prop in (b"minimumHeight", b"maximumHeight"):
        anim = QPropertyAnimation(widget, prop, group)
        anim.setDuration(max(1, int(duration_ms)))
        anim.setEasingCurve(_easing())
        anim.setStartValue(widget.height())
        anim.setEndValue(max(0, int(to_height)))
        group.addAnimation(anim)

    def _done() -> None:
        try:
            widget.setFixedHeight(max(0, int(to_height)))
        except Exception:
            pass
        try:
            if widget.property("_inkfall_tween_h") is group:
                widget.setProperty("_inkfall_tween_h", None)
        except Exception:
            pass
        try:
            if finished is not None:
                finished()
        except Exception:
            pass

    group.finished.connect(_done)
    group.start()
    try:
        widget.setProperty("_inkfall_tween_h", group)
    except Exception:
        pass


def animate_menu_open(menu, duration_ms: int = 140, rise_px: int = -6,
                      parent: QObject | None = None) -> None:
    """Subtle menu entrance: fade + tiny vertical settle (sort menu).

    Positive     ``rise_px`` enters bottom-to-top; negative enters
    top-to-bottom (sort: -6px drop onto the button anchor, M4.6.10).
    No giant popup; anchor position untouched.
    """
    duration_ms = _effective_duration(duration_ms)
    if duration_ms <= 0:
        return
    try:
        from PySide6.QtWidgets import QGraphicsOpacityEffect as _Effect

        effect = _Effect(menu)
        effect.setOpacity(0.0)
        menu.setGraphicsEffect(effect)
        owner = parent or menu
        from PySide6.QtCore import QParallelAnimationGroup

        group = QParallelAnimationGroup(owner)
        fade = QPropertyAnimation(effect, b"opacity", group)
        fade.setDuration(max(1, int(duration_ms)))
        fade.setEasingCurve(_easing())
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        group.addAnimation(fade)
        try:
            start = menu.pos()
            menu.move(start.x(), start.y() + int(rise_px))
            slide = QPropertyAnimation(menu, b"pos", group)
            slide.setDuration(max(1, int(duration_ms)))
            slide.setEasingCurve(_easing())
            slide.setStartValue(start + type(start)(0, int(rise_px)))
            slide.setEndValue(start)
            group.addAnimation(slide)
        except Exception:
            pass
        group.finished.connect(lambda: menu.setGraphicsEffect(None))
        group.start()
    except Exception:
        pass
