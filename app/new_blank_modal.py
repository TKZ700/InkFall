"""New Blank customization dialog (M8).

One modal in the shared :mod:`app.modal` family: template chips, paper
size + orientation, paper/pattern colors, pattern spacing, and a live
page preview. The preview and the eventual PDF are rendered from the
same :func:`document.paper.pattern_primitives` output — the preview is a
small QPainter interpreter of the exact primitives the backend draws, so
what the user sees is what gets written (no second template pipeline).

Create emits a normalized :class:`document.paper.PaperSpec`; the window
owns document creation, the workspace switch, and every failure toast.
Nothing is saved to disk here: a blank opens as an editable, unsaved
workspace.
"""

from __future__ import annotations

import dataclasses

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.modal import CenteredModal, ModalButton
from app.theme.typography import role_font
from document.paper import (
    MAX_SPACING_MM,
    MIN_SPACING_MM,
    PAPER_SIZES,
    TEMPLATE_LABELS,
    TEMPLATE_ORDER,
    PaperSpec,
    page_size_pt,
    pattern_primitives,
)

__all__ = ["BlankPreferencesModal", "BLANK_W", "BLANK_H"]

BLANK_W = 660
BLANK_H = 584

#: Template chip grid columns.
_CHIP_COLUMNS = 2


def _hex_alpha(color_hex: str, alpha: float) -> QColor:
    """QColor from ``#rrggbb`` with a 0..1 alpha (never raises)."""
    color = QColor(str(color_hex or "#000000"))
    if not color.isValid():
        color = QColor("#000000")
    color.setAlphaF(max(0.0, min(1.0, float(alpha))))
    return color


class _SwatchButton(QWidget):
    """Color chip: paints the current color, opens a picker on click.

    A plain ``QPushButton`` cannot paint a reliable color fill under the
    shared modal hover wash, so this tiny widget owns its paint and
    keyboard activation directly (TabFocus + Enter/Space, no native
    focus rectangle).
    """

    clicked = Signal()

    def __init__(self, label: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self._color = str(color)
        self._tokens = None
        self._hover = False
        self.setFixedSize(38, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(f"{label} color")
        self.setToolTip(f"{label}: {self._color}")

    def set_color(self, color: str) -> None:
        self._color = str(color)
        self.setToolTip(f"{self.accessibleName()}: {self._color}")
        self.update()

    def set_tokens(self, tokens) -> None:
        self._tokens = tokens
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            if not painter.isActive():
                return
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            t = self._tokens
            edge = QColor("#00000000")
            if t is not None:
                edge = QColor(t.accent if (self._hover or self.hasFocus())
                              else t.input_border)
            painter.setPen(QPen(edge, 2.0 if self.hasFocus() else 1.0))
            painter.setBrush(QColor(self._color))
            painter.drawRoundedRect(
                QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0), 6.0, 6.0)
        except Exception:
            pass
        finally:
            try:
                if painter.isActive():
                    painter.end()
            except Exception:
                pass

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _PaperPreview(QWidget):
    """Live page preview painted from the shared pattern primitives."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spec = PaperSpec()
        self._tokens = None
        self.setMinimumSize(250, 300)
        self.setAccessibleName("Paper preview")

    def set_spec(self, spec: PaperSpec) -> None:
        self._spec = spec.normalized()
        self.update()

    def set_tokens(self, tokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            if not painter.isActive():
                return
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            t = self._tokens
            backdrop = QColor(t.surface if t is not None else "#121419")
            painter.fillRect(self.rect(), backdrop)
            spec = self._spec
            page_w, page_h = page_size_pt(spec)
            area = self.rect().adjusted(16, 16, -16, -16)
            if area.width() <= 8 or area.height() <= 8 or page_w <= 0 or page_h <= 0:
                return
            scale = min(area.width() / page_w, area.height() / page_h)
            width = page_w * scale
            height = page_h * scale
            left = area.x() + (area.width() - width) / 2.0
            top = area.y() + (area.height() - height) / 2.0
            page_rect = QRectF(left, top, width, height)
            # Soft drop shadow so the sheet reads above the backdrop.
            painter.fillRect(page_rect.adjusted(2.0, 3.0, 2.0, 3.0),
                             QColor(0, 0, 0, 70))
            painter.fillRect(page_rect, QColor(spec.paper_color))
            painter.save()
            try:
                painter.setClipRect(page_rect)
                painter.translate(left, top)
                painter.scale(scale, scale)
                self._paint_items(painter, spec, scale)
            finally:
                painter.restore()
            edge = QColor(t.border if t is not None else "#262C38")
            painter.setPen(QPen(edge, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(page_rect.adjusted(-0.5, -0.5, 0.5, 0.5))
        except Exception:
            pass
        finally:
            try:
                if painter.isActive():
                    painter.end()
            except Exception:
                pass

    def _paint_items(self, painter: QPainter, spec: PaperSpec,
                     scale: float) -> None:
        """Interpret the shared template vocabulary (page-point coords).

        The page transform is already applied, so stroke widths are
        converted to device pixels (cosmetic pens, floor 0.8px) to keep
        the preview legible at thumbnail scale — the saved PDF keeps the
        true point widths.
        """
        pattern = QColor(spec.pattern_color)
        try:
            factor = max(1e-6, float(scale))
        except Exception:
            factor = 1.0

        def _stroke(alpha: float, width: float) -> QPen:
            pen = QPen(_hex_alpha(pattern.name(), alpha),
                       max(0.8, float(width) * factor))
            pen.setCosmetic(True)
            return pen

        for item in pattern_primitives(spec):
            try:
                kind = item[0]
                if kind == "line":
                    _, x0, y0, x1, y1, alpha, width = item
                    painter.setPen(_stroke(alpha, width))
                    painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
                elif kind == "dot":
                    _, cx, cy, radius = item
                    r = max(float(radius), 0.9 / factor)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(pattern)
                    painter.drawEllipse(QRectF(cx - r, cy - r,
                                               r * 2.0, r * 2.0))
                elif kind == "rect":
                    _, x0, y0, w, h, alpha, width = item
                    painter.setPen(_stroke(alpha, width))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(QRectF(x0, y0, w, h))
                elif kind == "band":
                    _, x0, y0, w, h, alpha = item
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(_hex_alpha(pattern.name(), alpha))
                    painter.drawRect(QRectF(x0, y0, w, h))
            except Exception:
                continue


class BlankPreferencesModal(CenteredModal):
    """Paper customization dialog; emits a ``PaperSpec`` on Create."""

    createRequested = Signal(object)

    def __init__(self, tokens, parent=None) -> None:
        self._built = False
        super().__init__(tokens, BLANK_W, BLANK_H, parent)
        self.set_title("New blank")
        self._spec = PaperSpec()
        self._template_group = QButtonGroup(self)
        self._template_group.setExclusive(True)
        self._template_buttons: dict[str, ModalButton] = {}
        self._size_group = QButtonGroup(self)
        self._size_group.setExclusive(True)
        self._size_buttons: dict[str, ModalButton] = {}
        self._orientation_group = QButtonGroup(self)
        self._orientation_group.setExclusive(True)
        self._orientation_buttons: dict[str, ModalButton] = {}

        content = QHBoxLayout()
        content.setContentsMargins(0, 8, 0, 0)
        content.setSpacing(24)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        self._build_template_section(left)
        self._build_paper_section(left)
        self._build_color_section(left)
        self._build_spacing_section(left)
        left.addStretch(1)
        content.addLayout(left, stretch=1)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        self._preview = _PaperPreview(self._panel)
        right.addWidget(self._preview, stretch=1)
        self._caption = QLabel("")
        self._caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self._caption)
        content.addLayout(right, stretch=1)
        self.body.addLayout(content, stretch=1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 14, 0, 0)
        footer.setSpacing(10)
        footer.addStretch(1)
        self._cancel_btn = ModalButton("Cancel", radius=8)
        self._cancel_btn.setFixedHeight(34)
        self._cancel_btn.setMinimumWidth(92)
        self._cancel_btn.clicked.connect(lambda: self.close_modal())
        self._create_btn = ModalButton("Create", radius=8)
        self._create_btn.setFixedHeight(34)
        self._create_btn.setMinimumWidth(104)
        self._create_btn.setDefault(True)
        self._create_btn.clicked.connect(self._on_create)
        for btn in (self._cancel_btn, self._create_btn):
            try:
                from app import fx as _fx

                btn.pressed.connect(lambda b=btn: _fx.press_feedback(b, 90))
            except Exception:
                pass
        footer.addWidget(self._cancel_btn)
        footer.addWidget(self._create_btn)
        self.body.addLayout(footer)

        self._built = True
        self._sync_controls()
        self._refresh_preview()
        self.update_tokens(tokens)

    # -- construction ------------------------------------------------------

    @staticmethod
    def _section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(f"inkfall-blank-{text.lower().replace(' ', '-')}")
        return label

    def _build_template_section(self, layout: QVBoxLayout) -> None:
        self._template_title = self._section_label("Template")
        layout.addWidget(self._template_title)
        grid_host = QWidget(self._panel)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 2, 0, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for index, key in enumerate(TEMPLATE_ORDER):
            btn = ModalButton(TEMPLATE_LABELS[key], radius=6)
            btn.setCheckable(True)
            btn.setFixedHeight(32)
            btn.setAccessibleName(f"{TEMPLATE_LABELS[key]} template")
            btn.clicked.connect(lambda _checked=False, k=key: self._set_template(k))
            self._wire_press(btn)
            self._template_group.addButton(btn)
            self._template_buttons[key] = btn
            grid.addWidget(btn, index // _CHIP_COLUMNS,
                           index % _CHIP_COLUMNS)
        layout.addWidget(grid_host)

    def _build_paper_section(self, layout: QVBoxLayout) -> None:
        self._paper_title = self._section_label("Paper")
        layout.addWidget(self._paper_title)
        size_host = QWidget(self._panel)
        size_row = QHBoxLayout(size_host)
        size_row.setContentsMargins(0, 0, 0, 0)
        size_row.setSpacing(8)
        for name in PAPER_SIZES:
            btn = ModalButton(name, radius=6)
            btn.setCheckable(True)
            btn.setFixedHeight(32)
            btn.setAccessibleName(f"{name} paper size")
            btn.clicked.connect(lambda _checked=False, n=name: self._set_size(n))
            self._wire_press(btn)
            self._size_group.addButton(btn)
            self._size_buttons[name] = btn
            size_row.addWidget(btn, stretch=1)
        layout.addWidget(size_host)

        orientation_host = QWidget(self._panel)
        orientation_row = QHBoxLayout(orientation_host)
        orientation_row.setContentsMargins(0, 0, 0, 0)
        orientation_row.setSpacing(8)
        for value, label in (("portrait", "Portrait"),
                             ("landscape", "Landscape")):
            btn = ModalButton(label, radius=6)
            btn.setCheckable(True)
            btn.setFixedHeight(32)
            btn.setAccessibleName(f"{label} orientation")
            btn.clicked.connect(
                lambda _checked=False, v=value: self._set_orientation(v))
            self._wire_press(btn)
            self._orientation_group.addButton(btn)
            self._orientation_buttons[value] = btn
            orientation_row.addWidget(btn, stretch=1)
        layout.addWidget(orientation_host)

    def _build_color_section(self, layout: QVBoxLayout) -> None:
        self._color_title = self._section_label("Colors")
        layout.addWidget(self._color_title)
        host = QWidget(self._panel)
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self._paper_swatch = _SwatchButton("Paper", self._spec.paper_color,
                                           self._panel)
        self._paper_swatch.clicked.connect(self._pick_paper_color)
        self._pattern_swatch = _SwatchButton("Pattern", self._spec.pattern_color,
                                             self._panel)
        self._pattern_swatch.clicked.connect(self._pick_pattern_color)
        self._paper_label = QLabel("Paper")
        self._pattern_label = QLabel("Pattern")
        row.addWidget(self._paper_label)
        row.addWidget(self._paper_swatch)
        row.addSpacing(14)
        row.addWidget(self._pattern_label)
        row.addWidget(self._pattern_swatch)
        row.addStretch(1)
        layout.addWidget(host)

    def _build_spacing_section(self, layout: QVBoxLayout) -> None:
        self._spacing_title = self._section_label("Spacing")
        layout.addWidget(self._spacing_title)
        host = QWidget(self._panel)
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self._minus_btn = ModalButton("−", circular=True)
        self._minus_btn.setFixedSize(28, 28)
        self._minus_btn.setAccessibleName("Decrease spacing")
        self._minus_btn.clicked.connect(lambda: self._step_spacing(-1.0))
        self._plus_btn = ModalButton("+", circular=True)
        self._plus_btn.setFixedSize(28, 28)
        self._plus_btn.setAccessibleName("Increase spacing")
        self._plus_btn.clicked.connect(lambda: self._step_spacing(1.0))
        self._spacing_value = QLabel("")
        self._spacing_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spacing_value.setMinimumWidth(64)
        for btn in (self._minus_btn, self._plus_btn):
            self._wire_press(btn)
        row.addWidget(self._minus_btn)
        row.addWidget(self._spacing_value)
        row.addWidget(self._plus_btn)
        row.addStretch(1)
        layout.addWidget(host)

    @staticmethod
    def _wire_press(btn) -> None:
        try:
            from app import fx as _fx

            btn.pressed.connect(lambda b=btn: _fx.press_feedback(b, 90))
        except Exception:
            pass

    # -- state ---------------------------------------------------------------

    def _current_spec(self) -> PaperSpec:
        return self._spec.normalized()

    def _apply(self, **changes) -> None:
        """Adopt changed spec fields and refresh every dependent view."""
        self._spec = dataclasses.replace(self._spec, **changes).normalized()
        self._sync_controls()
        self._refresh_preview()

    def _set_template(self, key: str) -> None:
        self._apply(template=key)

    def _set_size(self, name: str) -> None:
        self._apply(size_name=name)

    def _set_orientation(self, value: str) -> None:
        self._apply(orientation=value)

    def _step_spacing(self, delta: float) -> None:
        value = self._spec.spacing_mm + delta
        value = max(MIN_SPACING_MM, min(MAX_SPACING_MM, value))
        self._apply(spacing_mm=value)

    def _pick_paper_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._spec.paper_color), self, "Paper color")
        if color.isValid():
            self._apply(paper_color=color.name())
            self._paper_swatch.set_color(self._spec.paper_color)

    def _pick_pattern_color(self) -> None:
        color = QColorDialog.getColor(
            QColor(self._spec.pattern_color), self, "Pattern color")
        if color.isValid():
            self._apply(pattern_color=color.name())
            self._pattern_swatch.set_color(self._spec.pattern_color)

    def _sync_controls(self) -> None:
        spec = self._spec
        for key, btn in self._template_buttons.items():
            btn.setChecked(key == spec.template)
        for name, btn in self._size_buttons.items():
            btn.setChecked(name == spec.size_name)
        for value, btn in self._orientation_buttons.items():
            btn.setChecked(value == spec.orientation)
        try:
            self._paper_swatch.set_color(spec.paper_color)
            self._pattern_swatch.set_color(spec.pattern_color)
        except Exception:
            pass
        try:
            self._spacing_value.setText(f"{spec.spacing_mm:.0f} mm")
            self._minus_btn.setEnabled(spec.spacing_mm > MIN_SPACING_MM)
            self._plus_btn.setEnabled(spec.spacing_mm < MAX_SPACING_MM)
        except Exception:
            pass
        self._sync_chip_styles()

    def _refresh_preview(self) -> None:
        try:
            self._preview.set_spec(self._spec)
            width_pt, height_pt = page_size_pt(self._spec)
            orientation = ("Landscape" if height_pt < width_pt
                           else "Portrait")
            self._caption.setText(
                f"{self._spec.size_name} · {orientation} · "
                f"{self._spec.spacing_mm:.0f} mm")
        except Exception:
            pass

    def _on_create(self) -> None:
        try:
            self.createRequested.emit(self._current_spec())
        except Exception:
            pass

    # -- theming -------------------------------------------------------------

    def update_tokens(self, tokens) -> None:
        super().update_tokens(tokens)
        try:
            if self._built:
                self._sync_chip_styles()
        except Exception:
            pass

    def _sync_chip_styles(self) -> None:
        """Style chips (base + checked border); called on token changes."""
        if not getattr(self, "_built", False):
            return
        try:
            t = self._tokens
            for group in (self._template_buttons, self._size_buttons,
                          self._orientation_buttons):
                for btn in group.values():
                    btn.set_tokens(t)
                    btn.setFont(role_font(t, "body"))
                    btn.setStyleSheet(
                        f"QPushButton {{ color: {t.text_secondary};"
                        f" background-color: transparent;"
                        f" border: 1px solid {t.input_border};"
                        f" border-radius: 6px; padding: 4px 8px; outline: 0; }}"
                        f"QPushButton:checked {{ color: {t.text_primary};"
                        f" border: 1px solid {t.accent}; }}"
                        f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
                    )
            for btn in (self._minus_btn, self._plus_btn):
                btn.set_tokens(t)
                btn.setFont(role_font(t, "body"))
            self._cancel_btn.set_tokens(t)
            self._cancel_btn.setFont(role_font(t, "body"))
            self._cancel_btn.setStyleSheet(
                f"QPushButton {{ color: {t.text_secondary};"
                f" background-color: transparent;"
                f" border: 1px solid {t.input_border}; border-radius: 8px;"
                f" padding: 6px 16px; outline: 0; }}"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )
            self._create_btn.set_tokens(t)
            self._create_btn.setFont(role_font(t, "body"))
            self._create_btn.setStyleSheet(
                f"QPushButton {{ color: {t.background};"
                f" background-color: {t.accent};"
                f" border: 1px solid {t.accent}; border-radius: 8px;"
                f" padding: 6px 16px; outline: 0; }}"
                f"QPushButton:focus {{ border: 1px solid {t.input_focus}; }}"
            )
            for title in (self._template_title, self._paper_title,
                          self._color_title, self._spacing_title):
                title.setFont(role_font(t, "section"))
                title.setStyleSheet(
                    f"color: {t.text_primary}; margin-top: 4px;")
            for label in (self._paper_label, self._pattern_label):
                label.setFont(role_font(t, "meta"))
                label.setStyleSheet(f"color: {t.text_muted};")
            self._spacing_value.setFont(role_font(t, "meta"))
            self._spacing_value.setStyleSheet(f"color: {t.text_secondary};")
            self._caption.setFont(role_font(t, "meta"))
            self._caption.setStyleSheet(f"color: {t.text_muted};")
            self._paper_swatch.set_tokens(t)
            self._pattern_swatch.set_tokens(t)
            self._preview.set_tokens(t)
        except Exception:
            pass
