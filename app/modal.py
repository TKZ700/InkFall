"""Centered modal overlay primitive (M4.6.5). Shared by Settings + New.

One family, two semantics: a full-client-area dimmed backdrop
(``tokens.modal_dim``) hosts a single centered rounded panel. The
underlying Library is never resized or re-laid out; the grid does not
move. Backdrop click closes, Escape closes, focus is trapped while
open and returned to the launcher on close. Enter/exit animations run
through ``app.fx`` (fade + 6–10px vertical travel, OutCubic).
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, Qt, Signal, QVariantAnimation
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import fx
from app.icons import icon
from app.theme.tokens import InkfallTokens, token_rgba
from app.theme.typography import role_font

__all__ = ["CenteredModal", "ModalButton", "MODAL_OPEN_MS", "MODAL_CLOSE_MS"]

MODAL_OPEN_MS = 180
MODAL_CLOSE_MS = 140


class ModalButton(QPushButton):
    """Painted-hover shared control (M4.6.10 modal, M4.6.11 everywhere).

    One shared 140ms OutCubic hover progress drives wash + accent
    border (rounded, circular, or flat bar); press answers via the
    effect dip wired by owners (80-110ms). The idle border is
    transparent at the same 1px width as the keyboard focus ring, so
    focus never shifts geometry. TabFocus, no native rectangle;
    animator parented (dies with it). M4.6.11: ``bar=True`` paints a
    square title-bar wash; ``danger=True`` tints hover red (close).
    """

    def __init__(self, text: str = "", circular: bool = False,
                 radius: int = 6, parent=None, bar: bool = False,
                 danger: bool = False) -> None:
        super().__init__(text, parent)
        self._circular = bool(circular)
        self._bar = bool(bar)
        self._danger = bool(danger)
        self._radius = 0 if self._bar else (16 if circular else int(radius))
        self._tokens = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._hover_p = 0.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(140)
        self._hover_anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._hover_anim.setStartValue(0.0)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.valueChanged.connect(self._on_hover_tick)
        try:
            self.pressed.connect(lambda: self.update())
            self.released.connect(lambda: self.update())
        except Exception:
            pass

    def set_tokens(self, tokens) -> None:
        """Refresh theme tokens (wash colors follow theme switches)."""
        try:
            self._tokens = tokens
            self.update()
        except Exception:
            pass

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self._hover_glide_to(1.0)

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self._hover_glide_to(0.0)

    def _hover_glide_to(self, target: float) -> None:
        try:
            self._hover_anim.stop()
            self._hover_anim.setStartValue(float(self._hover_p))
            self._hover_anim.setEndValue(float(target))
            self._hover_anim.start()
        except Exception:
            pass

    def _on_hover_tick(self, value) -> None:
        try:
            self._hover_p = max(0.0, min(1.0, float(value)))
        except Exception:
            self._hover_p = 1.0
        try:
            self.update()
        except Exception:
            pass

    def paintEvent(self, event) -> None:  # noqa: N802
        """Progress wash first (transparent QSS bg), then native paint."""
        try:
            p = max(0.0, min(1.0, float(self._hover_p)))
        except Exception:
            p = 0.0
        down = False
        try:
            down = bool(self.isDown())
        except Exception:
            pass
        if (p > 0.0 or down or self.isChecked()) and self._tokens is not None:
            from PySide6.QtGui import QColor, QPainter, QPainterPath

            painter = QPainter(self)
            try:
                if painter.isActive():
                    t = self._tokens
                    area = self.rect().adjusted(0, 0, -1, -1)
                    path = QPainterPath()
                    if self._bar:
                        path.addRect(area)
                    elif self._circular:
                        path.addEllipse(area)
                    else:
                        path.addRoundedRect(area, self._radius, self._radius)
                    if down:
                        # accent_soft is an rgba() token: QColor() alone
                        # would misparse (opaque), so use token_rgba.
                        ar, ag, ab, aa = token_rgba(t.accent_soft)
                        soft = QColor(ar, ag, ab)
                        soft.setAlphaF(aa)
                        painter.fillPath(path, soft)
                    elif self.isChecked():
                        painter.fillPath(path, QColor(t.surface_overlay))
                    elif self._danger:
                        danger = QColor(t.danger)
                        danger.setAlpha(int(255 * p))
                        painter.fillPath(path, danger)
                    else:
                        wash = QColor(t.surface_overlay)
                        wash.setAlpha(int(255 * p))
                        painter.fillPath(path, wash)
                        if not self._bar:
                            edge = QColor(t.accent)
                            edge.setAlpha(int(200 * p))
                            painter.setPen(edge)
                            painter.drawPath(path)
            except Exception:
                pass
            finally:
                try:
                    if painter.isActive():
                        painter.end()
                except Exception:
                    pass
        # Keyboard-focus ring (UI-02/UI-16): the title bar's QSS forces
        # the CSS focus indicator transparent, so the ring is painted
        # here instead — gated on focus alone, outside the hover block,
        # in the control's own silhouette (flat bar / circle / rounded).
        if self._tokens is not None and self.hasFocus() and self.isEnabled():
            # Lazy QtGui import, mirroring the hover block above: the
            # method-local import there makes QPainter function-local,
            # so this block must bind it itself before use.
            from PySide6.QtGui import QPainter
            from app.focus import draw_focus_ring

            painter = QPainter(self)
            try:
                if painter.isActive():
                    if self._bar:
                        draw_focus_ring(painter, self.rect(), self._tokens,
                                        radius=6.0)
                    elif self._circular:
                        draw_focus_ring(painter, self.rect(), self._tokens,
                                        radius=None)
                    else:
                        draw_focus_ring(painter, self.rect(), self._tokens,
                                        radius=float(self._radius))
            finally:
                try:
                    if painter.isActive():
                        painter.end()
                except Exception:
                    pass
        super().paintEvent(event)


class CenteredModal(QWidget):
    """Dimmed-backdrop centered modal. Subclasses fill :attr:`body`."""

    closed = Signal()

    def __init__(self, tokens: InkfallTokens, panel_w: int, panel_h: int,
                 parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._panel_w = int(panel_w)
        self._panel_h = int(panel_h)
        self._launcher = None
        # M4.6.6: the overlay is genuinely translucent — alpha paint
        # blends with the Library beneath instead of compositing
        # against black. No opacity effect is EVER placed on the whole
        # overlay (that flattens alpha to black); only the panel child
        # animates, and the dim level is repaint-driven (see _dim).
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self._dim = 0.0
        self._dim_anim = None
        self._panel_anim = None
        # M4.6.7: compositor-proof dim — a one-shot capture of the
        # underlying client painted back dimmed. Sibling translucency
        # composites as black on some Windows paths; a captured plate
        # is correct everywhere by construction.
        self._backdrop_pm = None
        # M4.6.9 QPainter safety: grabs are synchronous subtree paints,
        # so they must never nest (no reentrant capture) and never run
        # from inside a paint storm — resize-driven recaptures defer to
        # the next loop turn and coalesce.
        self._capturing = False
        self._recapture_pending = False
        # M4.6.11 theme crossfade: old/new client plates (both exclude
        # this modal by construction) + blend 0->1. While blending,
        # paintEvent draws the mix; afterwards _backdrop_pm == new.
        self._theme_old_pm = None
        self._theme_new_pm = None
        self._theme_blend = 1.0
        self._theme_anim = None
        self._theme_epoch = 0
        self.setVisible(False)
        # Full-area overlay; never in page layout (manually managed).
        if parent is not None:
            try:
                self.setGeometry(parent.rect())
            except Exception:
                pass

        self._panel = QWidget(self)
        self._panel.setFixedSize(self._panel_w, self._panel_h)
        self._panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The rounded fill is painted by the overlay (paintEvent), so
        # the panel itself stays transparent — required for its own
        # fade effect to blend instead of flashing opaque.
        self._panel.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._panel.setAutoFillBackground(False)
        self._place_panel()

        chrome = QVBoxLayout(self._panel)
        chrome.setContentsMargins(24, 18, 24, 18)
        chrome.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel("")
        header.addWidget(self._title)
        header.addStretch(1)
        self._close_btn = ModalButton("", circular=True)
        self._close_btn.setFixedSize(32, 32)
        self._close_btn.setAccessibleName("Close dialog")
        self._close_btn.setAccessibleDescription("Close this dialog")
        self._close_btn.clicked.connect(self.close_modal)
        # M4.6.9: every modal control answers press (opacity dip;
        # borderless idle is preserved — see update_tokens).
        try:
            self._close_btn.pressed.connect(
                lambda: fx.press_feedback(self._close_btn, 90))
        except Exception:
            pass
        header.addWidget(self._close_btn)
        chrome.addLayout(header)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 8, 0, 0)
        self.body.setSpacing(8)
        chrome.addLayout(self.body, stretch=1)
        self.update_tokens(tokens)

    # -- content API -------------------------------------------------------

    def set_title(self, text: str) -> None:
        """Panel title (wordmark-adjacent, subordinate to content)."""
        self._title.setText(text)

    def set_panel_size(self, w: int, h: int) -> None:
        """Resize the panel (clamped to the overlay on open)."""
        self._panel_w, self._panel_h = int(w), int(h)
        self._panel.setFixedSize(self._panel_w, self._panel_h)
        self._place_panel()

    # -- open / close ----------------------------------------------------------

    def open_modal(self, launcher=None) -> None:
        """Show centered with backdrop fade + panel rise.

        Backdrop dim is repaint-driven (never a graphics effect on the
        overlay); the panel alone gets the slide+fade. Prior animations
        are stopped first so rapid toggles never fight.
        """
        self._launcher = launcher
        try:
            parent = self.parent()
            if parent is not None:
                self.setGeometry(parent.rect())
        except Exception:
            pass
        self._place_panel()
        self._stop_anims()
        self._capture_backdrop()
        try:
            self.setGraphicsEffect(None)
        except Exception:
            pass
        self._panel.setVisible(True)
        self.setVisible(True)
        self.raise_()
        try:
            parent = self.parent()
            if parent is not None:
                parent.installEventFilter(self)
        except Exception:
            pass
        self._animate_dim(1.0, MODAL_OPEN_MS)
        self._panel_anim = fx.slide_fade_in(self._panel, 0, MODAL_OPEN_MS, self, dy_px=8)
        try:
            from PySide6.QtWidgets import QApplication

            QApplication.instance().installEventFilter(self)
        except Exception:
            pass
        self._focus_first()

    def _capture_backdrop(self) -> None:
        """Snapshot the underlying client (M4.6.7 compositor-proof dim).

        Captured while this overlay is still hidden, so the plate never
        contains the modal itself. Recaptured on every open; released
        on close. The live Library behind is pixel-identical, so the
        plate reads as dark glass over the real content.

        M4.6.9: ``grab()`` paints the parent subtree synchronously, so
        captures are reentrancy-guarded and never issued from inside a
        paint — resize paths use ``_request_recapture()`` (deferred).
        """
        if self._capturing:
            return
        self._capturing = True
        self._backdrop_pm = None
        try:
            parent = self.parent()
            if parent is None or not parent.isVisible():
                return
            pm = parent.grab()
            if pm is not None and not pm.isNull():
                self._backdrop_pm = pm
        except Exception:
            self._backdrop_pm = None
        finally:
            self._capturing = False

    def _request_recapture(self) -> None:
        """Deferred plate refresh for resize storms (M4.6.9).

        Synchronous grabs inside resize/event-filter handling can nest
        into an in-flight paint (``QPainter::begin`` failure). Coalesce
        to one capture on the next loop turn, when no paint owns the
        device. Safe to call at any rate.
        """
        if self._recapture_pending or not self.isVisible():
            if not self.isVisible():
                self._recapture_pending = False
            return
        self._recapture_pending = True
        try:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self._recapture_safe)
        except Exception:
            self._recapture_pending = False

    def _recapture_safe(self) -> None:
        """Run one coalesced recapture (guarded, visibility-checked)."""
        self._recapture_pending = False
        try:
            if not self.isVisible():
                return
            parent = self.parent()
            if parent is not None:
                self.setGeometry(parent.rect())
            self._place_panel()
            if not self._plate_valid():
                self._capture_backdrop()
            self.update()
        except Exception:
            pass

    def capture_client_excluding_self(self):
        """Grab the underlying client with this modal hidden (M4.6.11).

        Hide -> grab -> restore runs synchronously in one deferred op so
        no intermediate frame reaches the screen with the modal missing.
        No layout/resize/focus change; visibility/state identical after.
        Returns the plate (or None).
        """
        if self._capturing:
            return None
        parent = self.parent()
        if parent is None:
            return None
        was_visible = self.isVisible()
        focused = None
        try:
            from PySide6.QtWidgets import QApplication

            focused = QApplication.focusWidget()
        except Exception:
            pass
        self._capturing = True
        try:
            if was_visible:
                self.setVisible(False)
            pm = parent.grab()
            if pm is not None and not pm.isNull():
                return pm
            return None
        except Exception:
            return None
        finally:
            try:
                if was_visible:
                    self.setVisible(True)
                    self.setGeometry(parent.rect())
                    self._place_panel()
                    if focused is not None:
                        try:
                            focused.setFocus()
                        except Exception:
                            pass
                    self.update()
            except Exception:
                pass
            self._capturing = False

    def crossfade_theme_plates(self, old_pm, new_pm, ms: int = 210) -> None:
        """Blend old->new client plates after a theme switch (M4.6.11).

        Both plates exclude this modal. The live panel stays a real
        widget above the blend; only the backdrop crossfades. Stale
        requests cancel via epoch; close cancels immediately.
        """
        self._theme_epoch = int(getattr(self, "_theme_epoch", 0)) + 1
        epoch = self._theme_epoch
        try:
            if self._theme_anim is not None:
                self._theme_anim.stop()
        except Exception:
            pass
        self._theme_anim = None
        if not self.isVisible():
            try:
                if new_pm is not None and not new_pm.isNull():
                    self._backdrop_pm = new_pm
            except Exception:
                pass
            self._theme_old_pm = None
            self._theme_new_pm = None
            self._theme_blend = 1.0
            return
        self._theme_old_pm = old_pm
        self._theme_new_pm = new_pm
        self._theme_blend = 0.0
        try:
            from PySide6.QtCore import QVariantAnimation

            anim = QVariantAnimation(self)
            anim.setDuration(max(1, int(ms)))
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            try:
                from PySide6.QtCore import QEasingCurve

                anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
            except Exception:
                pass

            def _tick(value, _epoch=epoch) -> None:
                try:
                    if _epoch != self._theme_epoch or not self.isVisible():
                        return
                    self._theme_blend = max(0.0, min(1.0, float(value)))
                    self.update()
                except Exception:
                    pass

            def _done(_epoch=epoch) -> None:
                try:
                    if _epoch != self._theme_epoch:
                        return
                    if (self._theme_new_pm is not None
                            and not self._theme_new_pm.isNull()):
                        self._backdrop_pm = self._theme_new_pm
                    self._theme_old_pm = None
                    self._theme_new_pm = None
                    self._theme_blend = 1.0
                    self._theme_anim = None
                    self.update()
                except Exception:
                    pass

            anim.valueChanged.connect(_tick)
            anim.finished.connect(_done)
            anim.start()
            self._theme_anim = anim
        except Exception:
            try:
                if new_pm is not None and not new_pm.isNull():
                    self._backdrop_pm = new_pm
            except Exception:
                pass
            self._theme_old_pm = None
            self._theme_new_pm = None
            self._theme_blend = 1.0

    def cancel_theme_crossfade(self) -> None:
        """Invalidate any in-flight theme blend (close/supersede)."""
        try:
            self._theme_epoch = int(getattr(self, "_theme_epoch", 0)) + 1
        except Exception:
            pass
        try:
            if self._theme_anim is not None:
                self._theme_anim.stop()
        except Exception:
            pass
        self._theme_anim = None
        self._theme_old_pm = None
        self._theme_new_pm = None
        self._theme_blend = 1.0

    def _plate_valid(self) -> bool:
        """True when the captured plate matches the current overlay size.

        Device pixels are normalized by the pixmap ratio before the
        comparison, so HiDPI plates validate correctly.
        """
        try:
            plate = self._backdrop_pm
            if plate is None or plate.isNull():
                return False
            dpr = float(plate.devicePixelRatio() or 1.0)
            w = int(round(plate.width() / dpr))
            h = int(round(plate.height() / dpr))
            return w == self.width() and h == self.height()
        except Exception:
            return False

    def _release_backdrop(self) -> None:
        self._backdrop_pm = None

    def _stop_anims(self) -> None:
        """Stop owned animations so re-triggers never stack or fight."""
        for name in ("_dim_anim", "_panel_anim"):
            try:
                anim = getattr(self, name, None)
                if anim is not None:
                    anim.stop()
            except Exception:
                pass
            try:
                setattr(self, name, None)
            except Exception:
                pass

    def _animate_dim(self, target: float, ms: int) -> None:
        """Repaint-driven backdrop dim (transparent -> modal_dim)."""
        from PySide6.QtCore import QVariantAnimation

        try:
            if self._dim_anim is not None:
                self._dim_anim.stop()
        except Exception:
            pass
        anim = QVariantAnimation(self)
        anim.setDuration(max(1, int(ms)))
        anim.setStartValue(float(self._dim))
        anim.setEndValue(float(target))
        anim.valueChanged.connect(self._on_dim_tick)
        if target <= 0.0:
            anim.finished.connect(self._on_dim_closed)
        anim.start()
        self._dim_anim = anim

    def _on_dim_tick(self, value) -> None:
        try:
            self._dim = max(0.0, min(1.0, float(value)))
            self.update()
        except Exception:
            pass

    def _on_dim_closed(self) -> None:
        self._dim_anim = None
        self._release_backdrop()
        try:
            self.setVisible(False)
        except Exception:
            pass

    def _remove_filters(self) -> None:
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            parent = self.parent()
            if parent is not None:
                parent.removeEventFilter(self)
        except Exception:
            pass

    def close_modal(self, animate: bool = True) -> None:
        """Dismiss with fade + tiny reverse shift, return focus.

        ``animate=False`` hides immediately (workspace switches take
        precedence over the close animation).
        """
        self._remove_filters()
        self._stop_anims()
        try:
            self.cancel_theme_crossfade()
        except Exception:
            pass
        if animate:
            self._panel_anim = fx.slide_fade_out(
                self._panel, 0, MODAL_CLOSE_MS, self, dy_px=4, hide=False)
            self._animate_dim(0.0, MODAL_CLOSE_MS)
        else:
            try:
                self.setGraphicsEffect(None)
                self._panel.setGraphicsEffect(None)
            except Exception:
                pass
            self._dim = 0.0
            self._release_backdrop()
            self.setVisible(False)
        self.closed.emit()
        try:
            if self._launcher is not None:
                self._launcher.setFocus()
        except Exception:
            pass
        self._launcher = None

    def _place_panel(self) -> None:
        """Center the panel, clamped inside the overlay."""
        try:
            area = self.rect()
            w = min(self._panel_w, max(200, area.width() - 80))
            h = min(self._panel_h, max(200, area.height() - 80))
            self._panel.setFixedSize(w, h)
            self._panel.move((area.width() - w) // 2, (area.height() - h) // 2)
        except Exception:
            pass

    # -- focus trap --------------------------------------------------------------

    def _focusables(self) -> list:
        """Tab-order widgets inside the panel (no layout spacers)."""
        found = []
        try:
            for child in self._panel.findChildren(QWidget):
                if (child.isVisibleTo(self._panel)
                        and child.focusPolicy() & Qt.FocusPolicy.TabFocus):
                    found.append(child)
        except Exception:
            pass
        return found

    def _focus_first(self) -> None:
        # M4.6.11: opening must not visually press the X — focus the
        # panel (or first non-close control), never the close button,
        # unless the user Tabs there explicitly.
        try:
            items = self._focusables()
            close = getattr(self, "_close_btn", None)
            for item in items:
                if item is not close:
                    item.setFocus()
                    return
            self._panel.setFocus()
        except Exception:
            pass

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        """Trap Tab/Shift+Tab inside the open panel (app-level filter).

        Also tracks the parent client size: a window resize/maximize
        while the modal is open re-fits the overlay and recaptures the
        plate so the dimmed Library never stretches stale pixels.
        """
        try:
            if self.isVisible():
                # M8: while a child dialog is modal (color picker, save
                # confirmation), its own focus order owns Tab — the
                # overlay must not steal keys from it.
                from PySide6.QtWidgets import QApplication

                active = QApplication.activeModalWidget()
                if active is not None and active is not self:
                    return False
            if (obj is self.parent()
                    and event.type() == QEvent.Type.Resize
                    and self.isVisible()):
                self._sync_to_parent()
                return False
            if (self.isVisible() and event.type() == QEvent.Type.KeyPress
                    and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)):
                from PySide6.QtWidgets import QApplication

                items = self._focusables()
                if not items:
                    return False
                focused = QApplication.focusWidget()
                forward = event.key() == Qt.Key.Key_Tab
                if focused not in items:
                    items[0 if forward else -1].setFocus()
                    return True
                idx = items.index(focused)
                nxt = idx + (1 if forward else -1)
                if 0 <= nxt < len(items):
                    return False  # natural order stays inside
                items[0 if forward else -1].setFocus()
                return True
        except Exception:
            pass
        return False

    # -- dismissal -----------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Backdrop click (outside the panel) closes."""
        try:
            if (event.button() == Qt.MouseButton.LeftButton
                    and not self._panel.geometry().contains(
                        event.position().toPoint())):
                self.close_modal()
                event.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Escape closes."""
        if event.key() == Qt.Key.Key_Escape:
            self.close_modal()
            event.accept()
            return
        super().keyPressEvent(event)

    def _sync_to_parent(self) -> None:
        """Re-fit the overlay to the live client and refresh the plate.

        M4.6.9: the recapture defers out of the resize event (never a
        synchronous grab inside event handling).
        """
        try:
            parent = self.parent()
            if parent is not None:
                self.setGeometry(parent.rect())
                self._place_panel()
                self._request_recapture()
                self.update()
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep the panel centered (Library geometry untouched)."""
        super().resizeEvent(event)
        if self.isVisible():
            self._place_panel()
            self._request_recapture()

    def hideEvent(self, event) -> None:  # noqa: N802
        """Direct hides (workspace switch) still release the filter."""
        super().hideEvent(event)
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            parent = self.parent()
            if parent is not None:
                parent.removeEventFilter(self)
        except Exception:
            pass

    # -- theming ---------------------------------------------------------------------

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Restyle chrome; content hooks via :meth:`_apply_tokens`."""
        self._tokens = tokens
        t = tokens
        self._title.setFont(role_font(t, "section"))
        self._title.setStyleSheet(f"color: {t.text_primary};")
        self._close_btn.setIcon(icon("x", t.text_secondary, t.icon_sm))
        self._close_btn.set_tokens(t)
        self._close_btn.setStyleSheet(
            # M4.6.10: transparent idle border (focus ring swaps color
            # only — geometry never shifts); hover/pressed are painted
            # by ModalButton progress, circular wash radius 16.
            # M4.6.11: content min-height 30px (+1px borders = fixed
            # 32px) so app-QSS padding never shrinks the circle, and
            # padding 0 keeps the wash centered.
            f"QPushButton {{ border: 1px solid transparent; min-height: 30px;"
            f" padding: 0px; border-radius: 16px; outline: 0;"
            f" background: transparent; }}"
            f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
        )
        try:
            self._apply_tokens(t)
        except Exception:
            pass
        try:
            self.update()
        except Exception:
            pass

    def _apply_tokens(self, tokens: InkfallTokens) -> None:
        """Subclass hook: restyle panel content (called by update_tokens)."""

    def paintEvent(self, event) -> None:  # noqa: N802
        """Captured-plate dim + rounded elevated panel (explicit paint).

        The captured Library plate is drawn 1:1 with the live content
        behind it, then veiled — recognizable at every dim level, never
        opaque black, independent of compositor translucency support.
        Both alphas track ``_dim``; without a valid plate (capture failed
        or size mismatch) an opaque theme-surface fallback carries the
        veil instead of a transparent black hole.
        """
        from PySide6.QtGui import QColor, QPainter, QPainterPath

        # M4.6.9: one clear owner — verify begin() succeeded before any
        # draw; end() exactly once via finally (never after a failed
        # begin, never skipped by an early throw).
        painter = QPainter(self)
        try:
            if not painter.isActive():
                return
            t = self._tokens
            dim = max(0.0, min(1.0, float(self._dim)))
            # M4.6.11 theme blend: old/new client plates (modal-excluded)
            # crossfade beneath the live panel.
            _old = getattr(self, "_theme_old_pm", None)
            _new = getattr(self, "_theme_new_pm", None)
            _blend = max(0.0, min(1.0, float(
                getattr(self, "_theme_blend", 1.0))))
            if (_old is not None and not _old.isNull()
                    and _new is not None and not _new.isNull()
                    and _blend < 1.0):
                painter.drawPixmap(self.rect(), _old)
                try:
                    painter.setOpacity(_blend)
                except Exception:
                    pass
                painter.drawPixmap(self.rect(), _new)
                try:
                    painter.setOpacity(1.0)
                except Exception:
                    pass
            elif self._plate_valid():
                painter.drawPixmap(self.rect(), self._backdrop_pm)
            else:
                fallback = QColor(t.background)
                fallback.setAlpha(255)
                painter.fillRect(self.rect(), fallback)
            veil_r, veil_g, veil_b, veil_a = token_rgba(t.modal_dim)
            veil = QColor(veil_r, veil_g, veil_b)
            veil.setAlphaF(max(0.0, min(1.0, veil_a * dim)))
            painter.fillRect(self.rect(), veil)
            path = QPainterPath()
            geo = self._panel.geometry()
            path.addRoundedRect(0.5 + geo.x(), 0.5 + geo.y(),
                                geo.width() - 1, geo.height() - 1,
                                t.modal_radius, t.modal_radius)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            fill = QColor(t.surface_raised)
            fill.setAlpha(int(fill.alpha() * dim))
            painter.fillPath(path, fill)
            if dim > 0.02:
                edge = QColor(t.border)
                edge.setAlpha(int(edge.alpha() * dim))
                painter.setPen(edge)
                painter.drawPath(path)
        except Exception:
            pass
        finally:
            try:
                if painter.isActive():
                    painter.end()
            except Exception:
                pass
        super().paintEvent(event)
