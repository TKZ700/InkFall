"""Editorial library delegate (M4.6). Borderless document presentation.

No card frames, no fills, no blue selection blocks: the thumbnail is
the hero, typography carries hierarchy, whitespace separates. Paints
from model roles + tokens only — no PDF work, no I/O, O(visible).

Row types: section header (eyebrow + title + count), folder card
(large grid card / proper list row, navigational), hint line (quiet
single-line explanation), document card (grid), document row (list).

Folder cards carry a stable, restrained color: muted blue, teal,
violet, green, or amber, assigned by hashing the folder's stable id —
sorting or refreshing never reshuffles colors, and no metadata is
persisted (identity-derived decoration only).
"""

from __future__ import annotations

import zlib

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from app.icons import pixmap as icon_pixmap
from app.library.qt_model import Role
from app.theme.multilingual import font_for
from app.theme.tokens import InkfallTokens, token_rgba
from app.theme.typography import role_font

__all__ = [
    "LibraryDelegate",
    "retry_rect_for",
    "grid_columns_for_width",
    "card_width_for",
]

RETRY_BADGE = 24

# Restrained folder palette (muted blue, teal, violet, green, amber) —
# mid-tone hues that stay legible against both theme surfaces. Tints
# ride on alpha, so the same hexes work in light and dark.
_FOLDER_PALETTE = ("#5B7DB1", "#4E9A8F", "#8B7FC7", "#5F9E6E", "#C08A4E")

# M4.6.2 responsive grid defaults (mirror tokens; pure helpers allow
# headless testing without Qt).
# M4.6.5 denser grid (screenshot-locked): smaller cards, same 3:4 hero.
# M4.6.11 fixed geometry: canonical 180px card (tokens.thumb_w);
# resize changes column count/wrapping only, never card dimensions.
GRID_MIN_W = 170
GRID_MAX_W = 210
GRID_GAP_H = 16
GRID_GAP_V = 24
FIXED_CARD_W = 180.0


def grid_columns_for_width(usable_width: int, min_w: int = GRID_MIN_W,
                           gap: int = GRID_GAP_H) -> int:
    """Deterministic column count from available width.

    M4.6.11: fixed 180px cards — columns = max(2, floor((usable +
    gap) / (180 + gap))), capped at 12. Leftover pixels become right
    whitespace (left-aligned final row), never card stretch.
    ``min_w``/``gap`` kept for signature compat (gap honored).
    """
    usable = max(0, int(usable_width))
    step = 180 + int(gap)
    cols = max(2, (usable + int(gap)) // max(1, step))
    return max(2, min(12, int(cols)))


def card_width_for(usable_width: int, columns: int,
                   gap: int = GRID_GAP_H) -> float:
    """Even card width for a full row; final row reuses the same width.

    M4.6.11: always the canonical 180px (signature kept for compat).
    """
    _ = usable_width
    _ = columns
    _ = gap
    return float(FIXED_CARD_W)


def retry_rect_for(thumb_rect: QRect) -> QRect:
    """Layout-neutral retry badge geometry (bottom-right inside thumb)."""
    return QRect(thumb_rect.right() - RETRY_BADGE - 6, thumb_rect.bottom() - RETRY_BADGE - 6,
                 RETRY_BADGE, RETRY_BADGE)


def folder_color_for(folder_id: str) -> QColor:
    """Stable palette color for one folder (identity-derived, no storage)."""
    index = zlib.crc32(folder_id.encode("utf-8")) % len(_FOLDER_PALETTE)
    return QColor(_FOLDER_PALETTE[index])


def _elided(font: QFont, text: str, width: int) -> str:
    metrics = QFontMetrics(font)
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(8, width))


def _meta_line(item) -> str:
    parts: list[str] = []
    size = item.size_bytes
    if size >= 1024 * 1024:
        parts.append(f"{size / (1024 * 1024):.1f} MB")
    elif size >= 1024:
        parts.append(f"{size / 1024:.0f} KB")
    elif size > 0:
        parts.append(f"{size} B")
    if item.page_count:
        parts.append(f"{item.page_count} pages")
    return "  ·  ".join(parts) if parts else item.file_name


class LibraryDelegate(QStyledItemDelegate):
    """Borderless editorial painter for library rows."""

    GRID_CARD = QSize(190, 337)
    LIST_ROW = QSize(400, 76)
    HEADER = QSize(400, 52)

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._title_font = role_font(tokens, "doc_title")
        self._meta_font = role_font(tokens, "meta")
        self._eyebrow_font = role_font(tokens, "eyebrow")
        self._section_font = role_font(tokens, "section")
        self._icons: dict[str, QPixmap] = {}
        self._badge_rects: dict[str, QRect] = {}
        self._folder_icons: dict[str, QPixmap] = {}
        self._build_icons(tokens)

    def clear_badges(self) -> None:
        """Forget painted retry geometry (called on model reset)."""
        self._badge_rects.clear()

    def retry_hit(self, item_id: str, pos, row: int | None = None) -> bool:
        """True when a viewport position lands on the item's retry badge."""
        # M4.6.11 row-specific: tuple key wins when row is known so two
        # visible occurrences of the same document stay independent.
        # Always fall back to the legacy id key (offscreen/cached rects).
        if row is not None:
            rect = self._badge_rects.get((int(row), item_id))
            if rect is not None and rect.contains(pos):
                return True
        rect = self._badge_rects.get(item_id)
        if rect is not None and rect.contains(pos):
            return True
        for (key, rect) in list(self._badge_rects.items()):
            try:
                if isinstance(key, tuple) and key[1] == item_id and rect.contains(pos):
                    return True
            except Exception:
                continue
        return False

    def _build_icons(self, tokens: InkfallTokens) -> None:
        self._icons = {
            "chevron": icon_pixmap("chevron-down", tokens.text_muted, 16),
            "chevron-active": icon_pixmap("chevron-down", tokens.accent, 16),
            "empty": icon_pixmap("empty", tokens.text_muted, 36),
            "alert": icon_pixmap("alert", tokens.text_muted, 36),
            "retry": icon_pixmap("retry", tokens.accent, 14),
        }
        # Palette-tinted folder glyphs are cached lazily per color+size
        # (see _folder_icon); the fixed-tint copies are not needed.
        self._folder_icons = {}

    def _folder_icon(self, color_hex: str, size: int) -> QPixmap:
        """Palette-tinted folder glyph, cached per (color, size)."""
        key = f"{color_hex}:{size}"
        mark = self._folder_icons.get(key)
        if mark is None:
            mark = icon_pixmap("folder", color_hex, size)
            self._folder_icons[key] = mark
        return mark

    def update_tokens(self, tokens: InkfallTokens) -> None:
        """Swap the active theme without rebuilding the view."""
        self._tokens = tokens
        self._title_font = role_font(tokens, "doc_title")
        self._meta_font = role_font(tokens, "meta")
        self._eyebrow_font = role_font(tokens, "eyebrow")
        self._section_font = role_font(tokens, "section")
        self._build_icons(tokens)

    def _mode(self, option) -> str:
        from PySide6.QtWidgets import QListView

        view = option.widget
        if isinstance(view, QListView) and view.viewMode() == QListView.ViewMode.ListMode:
            return "list"
        return "grid"

    def _flow_usable(self, option) -> int:
        """Usable content width for the IconMode flow (M4.6.8).

        The flow starts items one ``spacing()`` inset from the viewport
        edge, so a sizeHint of the full viewport width would overflow by
        exactly that inset on the right (clipping the header chevron,
        the count, and the last grid column). Subtract it with live
        values only — no hardcoded pixels, no per-resolution branches.
        """
        try:
            view = option.widget
            vp = view.viewport() if hasattr(view, "viewport") else None
            width = vp.width() if vp is not None else view.width()
            inset = view.spacing() if hasattr(view, "spacing") else 0
            return max(280, int(width) - int(inset))
        except Exception:
            return 600

    def _grid_metrics(self, option) -> tuple[int, float]:
        """(columns, card_width) for the current viewport width."""
        usable = self._flow_usable(option)
        cols = grid_columns_for_width(usable, GRID_MIN_W, GRID_GAP_H)
        return cols, float(FIXED_CARD_W)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        is_header = bool(index.data(Role.IS_HEADER))
        if is_header:
            # Full usable width (flow inset accounted) in Grid AND List.
            return QSize(self._flow_usable(option), self.HEADER.height())
        if bool(index.data(Role.IS_HINT)):
            return QSize(self._flow_usable(option), 28)
        mode = self._mode(option)
        if mode == "list":
            return QSize(self._flow_usable(option), self.LIST_ROW.height())
        # Grid: folder cards match book cards exactly (same responsive
        # width and height) so the Folders and Books rows share one
        # aligned grid. List: folder rows use the book row height.
        # QListView flows left-to-right; the partial final row keeps the
        # same width and is naturally left-aligned (never justified).
        _cols, card_w = self._grid_metrics(option)
        # M4.6.10: matches the painted 8px content inset in _paint_card.
        thumb_h = min(self._tokens.thumb_h, int((card_w - 16) * 4 / 3))
        return QSize(int(card_w), thumb_h + 84)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if bool(index.data(Role.IS_HEADER)):
            self._paint_header(painter, option, index)
        elif bool(index.data(Role.IS_HINT)):
            self._paint_hint(painter, option, index)
        elif bool(index.data(Role.IS_FOLDER)):
            if self._mode(option) == "list":
                self._paint_folder_list(painter, option, index)
            else:
                self._paint_folder_grid(painter, option, index)
        else:
            mode = self._mode(option)
            if mode == "list":
                self._paint_row(painter, option, index)
            else:
                self._paint_card(painter, option, index)
        painter.restore()

    def _paint_hint(self, painter: QPainter, option: QStyleOptionViewItem,
                    index: QModelIndex) -> None:
        """Quiet single-line explanation (empty Recent shelf, etc.)."""
        t = self._tokens
        text = index.data(Role.HINT) or ""
        rect = option.rect.adjusted(12, 0, -12, 0)
        painter.setPen(QColor(t.text_muted))
        painter.setFont(self._meta_font)
        painter.drawText(rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elided(self._meta_font, text, rect.width()))

    def _row_meta(self, index: QModelIndex) -> str | None:
        """Surface-specific meta text from the page (None = default)."""
        meta = index.data(Role.META)
        return str(meta) if meta else None

    # -- shared -----------------------------------------------------------

    def _selected(self, option: QStyleOptionViewItem) -> bool:
        return bool(option.state & QStyle.StateFlag.State_Selected)

    def _hover(self, option: QStyleOptionViewItem) -> bool:
        return bool(option.state & QStyle.StateFlag.State_MouseOver)

    def _pressed(self, option: QStyleOptionViewItem) -> bool:
        return bool(option.state & QStyle.StateFlag.State_Sunken)

    def _draw_thumb(self, painter: QPainter, target: QRect, image, missing: bool,
                     failed: bool, selected: bool, badge_id: str | None = None,
                     row: int | None = None) -> None:
        """Hero thumbnail: 3:4, quiet radius, ring only when selected."""
        t = self._tokens
        path = QPainterPath()
        path.addRoundedRect(target, 7, 7)
        painter.save()
        painter.setClipPath(path)
        thumb = image[0] if isinstance(image, tuple) else image
        if thumb is not None and not (hasattr(thumb, "isNull") and thumb.isNull()):
            pix = QPixmap.fromImage(thumb) if not isinstance(thumb, QPixmap) else thumb
            scaled = pix.scaled(
                target.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            if missing:
                painter.fillRect(target, QColor(0, 0, 0, 110))
        else:
            painter.fillRect(target, QColor(t.thumb_placeholder))
            mark = self._icons.get("alert" if failed else "empty")
            if mark is not None:
                painter.drawPixmap(
                    target.x() + (target.width() - mark.width()) // 2,
                    target.y() + (target.height() - mark.height()) // 2 - 8,
                    mark,
                )
            painter.setPen(QColor(t.text_muted))
            painter.setFont(self._meta_font)
            hint = "Couldn't load" if failed else ("Missing file" if missing else "Loading…")
            painter.drawText(target.adjusted(0, 24, 0, 0), Qt.AlignmentFlag.AlignHCenter, hint)
        painter.restore()
        if failed and badge_id is not None and not missing:
            badge = retry_rect_for(target)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            soft_r, soft_g, soft_b, soft_a = token_rgba(t.accent_soft)
            soft = QColor(soft_r, soft_g, soft_b)
            soft.setAlphaF(soft_a)
            painter.setBrush(soft)
            painter.drawEllipse(badge)
            glyph = self._icons.get("retry")
            if glyph is not None:
                painter.drawPixmap(
                    badge.x() + (badge.width() - glyph.width()) // 2,
                    badge.y() + (badge.height() - glyph.height()) // 2,
                    glyph,
                )
            painter.restore()
            try:
                _b = QRect(badge)
                self._badge_rects[badge_id] = QRect(_b)
                if row is not None:
                    self._badge_rects[(int(row), badge_id)] = QRect(_b)
            except Exception:
                pass
        elif badge_id is not None:
            try:
                self._badge_rects.pop(badge_id, None)
                if row is not None:
                    self._badge_rects.pop((int(row), badge_id), None)
                for key in [k for k in list(self._badge_rects.keys())
                            if isinstance(k, tuple) and k[1] == badge_id]:
                    self._badge_rects.pop(key, None)
            except Exception:
                pass
        if t.name == "light" and (thumb is None or (hasattr(thumb, "isNull") and thumb.isNull())):
            pass
        elif t.name == "light":
            painter.setPen(QColor(t.thumb_edge))
            painter.drawPath(path)
        if selected:
            ring = QPainterPath()
            ring.addRoundedRect(target.adjusted(-2, -2, 2, 2), 9, 9)
            painter.setPen(QColor(t.accent))
            painter.drawPath(ring)

    def _hover_progress_for(self, item, index=None) -> float:
        """Hover progress for one visible occurrence (M4.6.11).

        Row/view identity ``(row, item_id)`` — two visible occurrences
        of the same PDF animate independently. Falls back to the legacy
        ``_hover_id`` check when no row key is present (headless tests).
        """
        try:
            parent = self.parent()
            if parent is None:
                return 0.0
            progress = max(0.0, min(1.0, float(
                getattr(parent, "_hover_progress", 0.0))))
            if progress <= 0.0:
                return 0.0
            key = getattr(parent, "_hover_key", None)
            if key is not None:
                try:
                    krow, kid = key
                except Exception:
                    return 0.0
                if kid != item.id:
                    return 0.0
                if index is not None:
                    try:
                        if int(index.row()) != int(krow):
                            return 0.0
                    except Exception:
                        return 0.0
                return progress
            if getattr(parent, "_hover_id", None) != item.id:
                return 0.0
            return progress
        except Exception:
            return 0.0

    def _hover_lift(self, item, selected: bool, index=None) -> tuple[int, float]:
        """(lift_px, wash_alpha) for the animated hovered document only.

        Reads the page-owned hover state (single animator); selected
        rows never lift so hover and selection never fight. Zero for
        every non-hovered row — O(1), no timers, no widgets.
        M4.6.11: occurrence-scoped via ``_hover_progress_for``.
        """
        if selected:
            return (0, 0.0)
        try:
            progress = self._hover_progress_for(item, index)
        except Exception:
            return (0, 0.0)
        if progress <= 0.0:
            return (0, 0.0)
        # M4.6.12 moderate boost: wash targets 28/255 (~0.11) at full
        # progress — clearly visible yet unmistakably weaker than the
        # selected ring/wash. Lift stays <= 2px; shadow carries depth.
        return (max(0, min(2, int(round(2 * progress)))), 0.11 * progress)

    def _paint_lift_shadow(self, painter: QPainter, thumb_rect: QRect,
                           progress: float) -> None:
        """Soft elevation under a lifted thumbnail (no layout shift).

        M4.6.12: shadow builds with the wash (max ~84) so lift reads
        as one continuous physical response.
        """
        try:
            from PySide6.QtGui import QColor as _QColor

            shadow = QPainterPath()
            shadow.addRoundedRect(
                thumb_rect.adjusted(-3, 1, 3, 5), 9, 9)
            color = _QColor(0, 0, 0, int(84 * max(0.0, min(1.0, progress))))
            painter.fillPath(shadow, color)
        except Exception:
            pass

    def _paint_titles(self, painter: QPainter, title_rect: QRect, meta_rect: QRect,
                        item, selected: bool, hover: bool,
                        meta_text: str | None = None) -> None:
        t = self._tokens
        title_text = item.display_title
        title_font = font_for(title_text, "doc_title", t)
        meta = meta_text if meta_text else _meta_line(item)
        meta_font = font_for(meta, "meta", t)
        # M4.6.6: the title is never a link — no decoration change,
        # no tint, no brightness shift on hover/selection/press.
        # Selection is carried by the thumbnail ring alone; the
        # typeface never moves.
        _ = hover
        _ = selected
        painter.setPen(QColor(t.text_primary))
        painter.setFont(title_font)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elided(title_font, title_text, title_rect.width()))
        painter.setPen(QColor(t.text_secondary))
        painter.setFont(meta_font)
        painter.drawText(meta_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elided(meta_font, meta, meta_rect.width()))

    # -- section header -----------------------------------------------------

    def _visible_edge(self, option, rect) -> int:
        """Right paint bound clamped to the viewport (M4.6.8).

        The header cell may extend past the viewport edge by the flow
        inset; anchoring the chevron/count to the *visible* edge keeps
        them on-screen in Grid and List regardless of flow offsets.
        """
        edge = rect.right()
        try:
            view = option.widget
            vp = view.viewport() if hasattr(view, "viewport") else None
            if vp is not None:
                edge = min(edge, int(vp.width()))
        except Exception:
            pass
        return edge

    def _paint_header(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        t = self._tokens
        header = index.data(Role.HEADER)
        # M4.6.11 padded surface: 12px horizontal + 6px vertical inset
        # so text/chevron/count never touch the hover boundary. All four
        # sections share this geometry.
        rect = option.rect.adjusted(0, 0, 0, 0)
        if header is None:
            return
        content = rect.adjusted(12, 6, -12, -6)
        # M4.6.9 header hover/press: the whole row reads as one
        # interactive surface (wash + accent chevron + indicator).
        # Press compacts 1-2% statically (Sunken only, released on
        # repaint); hover fades via the page-owned header animator.
        hover_p = 0.0
        try:
            if (header.key
                    and getattr(self.parent(), "_hdr_anim_key", None) == header.key):
                hover_p = max(0.0, min(1.0, float(
                    getattr(self.parent(), "_hdr_progress", 0.0))))
        except Exception:
            hover_p = 0.0
        if self._pressed(option):
            dx = max(1, int(round(content.width() * 0.01)))
            _pr = content.adjusted(dx, 1, -dx, -1)
            painter.fillRect(_pr, QColor(0, 0, 0, 12))
        if hover_p > 0.0:
            wash = QPainterPath()
            wash.addRoundedRect(content, 8, 8)
            dim = QColor(t.card_hover)
            dim.setAlpha(int(28 * hover_p))
            painter.fillPath(wash, dim)
            edge = QColor(t.border)
            edge.setAlpha(int(90 * hover_p))
            painter.setPen(edge)
            painter.drawPath(wash)
            accent = QColor(t.accent)
            accent.setAlpha(int(60 + 110 * hover_p))
            painter.fillRect(QRect(content.x(), content.bottom() - 1, 28, 1), accent)
        painter.setPen(QColor(t.text_secondary if hover_p > 0.5 else t.text_muted))
        painter.setFont(self._eyebrow_font)
        painter.drawText(QRect(content.x(), content.y(), content.width(), 14),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         header.eyebrow.upper())
        # M4.6.12 reserved slots: chevron owns the outer 22px
        # (center edge-10, extents edge-18..edge-2), an 8px gap, then
        # the count right-aligned ending at edge-30. Titles elide
        # before either slot; identical in Grid/List, all sections.
        edge = self._visible_edge(option, content)
        count_label = f"{header.count}" if header.count else ""
        count_w = QFontMetrics(self._meta_font).horizontalAdvance(count_label) if count_label else 0
        count_left = edge - 30 - count_w if count_label else edge - 30
        title_avail = max(8, count_left - 8 - content.x())
        painter.setPen(QColor(t.text_primary))
        painter.setFont(self._section_font)
        painter.drawText(QRect(content.x(), content.y() + 14, title_avail, 26),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elided(self._section_font, header.title, title_avail))
        if count_label:
            painter.setPen(QColor(
                t.text_secondary if hover_p > 0.5 else t.text_muted))
            painter.setFont(self._meta_font)
            painter.drawText(QRect(edge - 30 - count_w,
                                   content.y() + 14, count_w, 26),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, count_label)
        # M4.6.7 collapse chevron: down = expanded, right = collapsed,
        # rotating with the single section animator. Accent-tinted while
        # the header holds keyboard focus (selected) so Tab position is
        # visible without any focus rectangle. M4.6.9: also accent on
        # hover so the affordance brightens unmistakably.
        if header.key:
            collapsed, progress = self._section_chevron_state(header.key)
            if progress is None:
                angle = -90.0 if collapsed else 0.0
            elif collapsed:
                angle = -90.0 * progress  # expanded -> collapsed
            else:
                angle = -90.0 * (1.0 - progress)  # collapsed -> expanded
            focused = bool(option.state & QStyle.StateFlag.State_Selected)
            lit = focused or hover_p > 0.5
            mark = self._icons.get("chevron-active" if lit else "chevron")
            if mark is not None and not mark.isNull():
                painter.save()
                painter.translate(self._visible_edge(option, content) - 10,
                                  content.y() + content.height() - 6)
                painter.rotate(angle)
                painter.drawPixmap(-8, -8, mark)
                painter.restore()

    def _section_chevron_state(self, key: str) -> tuple[bool, float | None]:
        """(collapsed, anim_progress) from the page (None when settled)."""
        try:
            parent = self.parent()
            if parent is not None and hasattr(parent, "_section_chevron"):
                return parent._section_chevron(key)
        except Exception:
            pass
        return False, None

    # -- document card (grid): thumbnail hero + type, no frame --------------

    def _paint_card(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item = index.data(Role.ITEM)
        if item is None:
            return
        t = self._tokens
        # Shared left origin: zero horizontal inset; QListView spacing
        # provides the gutters so every card aligns to the content grid.
        # M4.6.10 breathing room: the wash keeps the cell rect, but all
        # content (thumb/titles) lives 8px inside it — the hover
        # surface reads padded, and the lift shadow stays in-bounds.
        rect = option.rect.adjusted(0, 6, 0, -6)
        inner = rect.adjusted(8, 0, -8, 0)
        selected = self._selected(option)
        # M4.6.11 occurrence hover: Qt MouseOver AND row-key match, so a
        # duplicate id in another section never lights up.
        _occ_p = self._hover_progress_for(item, index)
        hover = bool(self._hover(option) and _occ_p > 0.0)
        try:
            if getattr(self.parent(), "_hover_key", None) is not None and not hover:
                hover = False
            elif getattr(self.parent(), "_hover_key", None) is None:
                hover = self._hover(option)
        except Exception:
            pass
        # M4.6.12 hover: visible wash + subtle hairline build together
        # (animated via the page-owned single animator). No heavy
        # highlight, no title change; selected rows keep the ring only.
        lift, wash_alpha = self._hover_lift(item, selected, index)
        if wash_alpha > 0.0:
            wash = QPainterPath()
            wash.addRoundedRect(rect, 10, 10)
            color = QColor(t.card_hover)
            color.setAlpha(int(255 * wash_alpha))
            painter.fillPath(wash, color)
            # M4.6.12 subtle hairline: ~70 alpha x occurrence progress,
            # far below selection strength, same rounded surface. Drawn
            # on a 1px-inset path so the stroke never leaves the card
            # bounds (paint-stays-in-item invariant holds).
            hair = QColor(t.border)
            hair.setAlpha(int(70 * max(0.0, min(1.0, _occ_p))))
            painter.setPen(hair)
            hairline = QPainterPath()
            hairline.addRoundedRect(rect.adjusted(1, 1, -1, -1), 9, 9)
            painter.drawPath(hairline)
        if self._pressed(option):
            painter.fillRect(rect, QColor(0, 0, 0, 10))
        # M4.6.11 press compress: 1-2% inset while the occurrence press
        # animator runs (never layout, never bounce).
        try:
            _pp = float(getattr(self.parent(), "_press_progress", 0.0) or 0.0)
            _pk = getattr(self.parent(), "_press_key", None)
            if _pp > 0.0 and _pk is not None and _pk[1] == item.id and int(_pk[0]) == int(index.row()):
                _dx = max(1, int(round(rect.width() * 0.01 * _pp)))
                _dy = max(1, int(round(rect.height() * 0.01 * _pp)))
                rect = rect.adjusted(_dx, _dy, -_dx, -_dy)
                inner = rect.adjusted(8, 0, -8, 0)
                # M4.6.12 press depth: moderate darkening scaled by the
                # press progress — perceptible on a normal click, never
                # selection-like. Navigation itself stays immediate.
                painter.fillRect(rect, QColor(0, 0, 0, int(20 * max(0.0, min(1.0, _pp)))))
        except Exception:
            pass
        thumb = index.data(Role.THUMB)
        failed = item.id in getattr(self.parent(), "_thumb_failed", set())
        tw = inner.width()
        th = min(t.thumb_h, int(tw * 4 / 3))
        thumb_rect = QRect(inner.x(), rect.y() - lift, tw, th)
        if lift:
            parent = self.parent()
            try:
                progress = float(getattr(parent, "_hover_progress", 0.0))
            except Exception:
                progress = 0.0
            self._paint_lift_shadow(painter, thumb_rect, progress)
        self._draw_thumb(painter, thumb_rect, thumb, item.missing, failed, selected, item.id,
                         row=index.row())
        title_rect = QRect(inner.x(), thumb_rect.bottom() + 8, tw, 22)
        meta_rect = QRect(inner.x(), title_rect.bottom() + 1, tw, 18)
        self._paint_titles(painter, title_rect, meta_rect, item, selected, hover,
                           meta_text=self._row_meta(index))

    # -- folder cards: palette-tinted, navigational ---------------------------

    def _folder_drop_target(self, folder_id: str) -> bool:
        """True while this folder is the active drag-drop target."""
        try:
            return getattr(self.parent(), "_drop_folder_id", None) == folder_id
        except Exception:
            return False

    def _folder_count(self, folder_id: str) -> int:
        parent = self.parent()
        if parent is not None and hasattr(parent, "folder_count"):
            try:
                return parent.folder_count(folder_id)
            except Exception:
                return 0
        return 0

    def _paint_folder_grid(self, painter: QPainter, option: QStyleOptionViewItem,
                           index: QModelIndex) -> None:
        """Large folder card, mirroring the book card's exact geometry."""
        t = self._tokens
        folder = index.data(Role.FOLDER)
        if folder is None:
            return
        rect = option.rect.adjusted(0, 6, 0, -6)
        # M4.6.8 press compact: the pressed card settles 1-3% smaller
        # (inset, never a transform — text stays crisp), then releases.
        try:
            compact = float(self.parent()._cat_compact(folder.id))
        except Exception:
            compact = 1.0
        if compact < 1.0:
            dx = max(1, int(round(rect.width() * (1.0 - compact) / 2)))
            dy = max(1, int(round(rect.height() * (1.0 - compact) / 2)))
            rect = rect.adjusted(dx, dy, -dx, -dy)
        inner = rect.adjusted(8, 0, -8, 0)
        selected = self._selected(option)
        hover_p = 0.0
        try:
            if getattr(self.parent(), "_cat_hover_key", None) == folder.id:
                hover_p = max(0.0, min(1.0, float(
                    getattr(self.parent(), "_cat_hover_p", 0.0))))
        except Exception:
            hover_p = 0.0
        if hover_p > 0.0:
            wash = QPainterPath()
            wash.addRoundedRect(rect, 10, 10)
            dim = QColor(t.card_hover)
            dim.setAlpha(int(30 * hover_p))
            painter.fillPath(wash, dim)
        if self._pressed(option):
            painter.fillRect(rect, QColor(0, 0, 0, 10))
        color = folder_color_for(folder.id)
        drop = self._folder_drop_target(folder.id)
        # Folder hero: a palette-tinted plate sized exactly like the
        # book thumbnail, holding the palette-colored glyph. The drop
        # target lifts the tint and border unmistakably.
        tw = inner.width()
        th = min(t.thumb_h, int(tw * 4 / 3))
        plate = QRect(inner.x(), rect.y(), tw, th)
        plate_path = QPainterPath()
        plate_path.addRoundedRect(plate, 8, 8)
        tint = QColor(color)
        tint.setAlpha(56 if drop else int(34 + 14 * hover_p))
        painter.fillPath(plate_path, tint)
        edge = QColor(color)
        edge.setAlpha(220 if drop else 70)
        pen = painter.pen()
        painter.setPen(edge)
        painter.drawPath(plate_path)
        painter.setPen(pen)
        mark = self._folder_icon(color.name(), 48)
        painter.drawPixmap(plate.x() + (plate.width() - mark.width()) // 2,
                           plate.y() + (plate.height() - mark.height()) // 2, mark)
        if selected:
            ring = QPainterPath()
            ring.addRoundedRect(plate.adjusted(-2, -2, 2, 2), 10, 10)
            painter.setPen(QColor(t.accent))
            painter.drawPath(ring)
        name_font = font_for(folder.name, "doc_title", t)
        painter.setPen(QColor(t.text_primary))
        painter.setFont(name_font)
        title_rect = QRect(inner.x(), plate.bottom() + 8, tw, 22)
        painter.save()
        painter.setClipRect(title_rect)
        painter.drawText(title_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elided(name_font, folder.name, max(8, tw - 4)))
        painter.restore()
        count = self._folder_count(folder.id)
        painter.setPen(QColor(t.text_secondary if hover_p > 0.5 else t.text_muted))
        painter.setFont(self._meta_font)
        count_rect = QRect(inner.x(), title_rect.bottom() + 1, tw, 18)
        painter.save()
        painter.setClipRect(count_rect)
        painter.drawText(count_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"{count} item{'s' if count != 1 else ''}")
        painter.restore()

    def _paint_folder_list(self, painter: QPainter, option: QStyleOptionViewItem,
                           index: QModelIndex) -> None:
        """Folder as a proper list row, aligned with the book rows."""
        t = self._tokens
        folder = index.data(Role.FOLDER)
        if folder is None:
            return
        rect = option.rect.adjusted(0, 2, 0, -2)
        selected = self._selected(option)
        hover_p = 0.0
        try:
            if getattr(self.parent(), "_cat_hover_key", None) == folder.id:
                hover_p = max(0.0, min(1.0, float(
                    getattr(self.parent(), "_cat_hover_p", 0.0))))
        except Exception:
            hover_p = 0.0
        if self._pressed(option):
            painter.fillRect(rect, QColor(0, 0, 0, 10))
        color = folder_color_for(folder.id)
        drop = self._folder_drop_target(folder.id)
        # Plate mirrors the book row's 44x58 thumbnail position.
        plate = QRect(rect.x() + 4, rect.y() + 6, 44, 58)
        plate_path = QPainterPath()
        plate_path.addRoundedRect(plate, 7, 7)
        tint = QColor(color)
        tint.setAlpha(56 if drop else int(34 + 18 * hover_p))
        painter.fillPath(plate_path, tint)
        edge = QColor(color)
        edge.setAlpha(220 if drop else 70)
        pen = painter.pen()
        painter.setPen(edge)
        painter.drawPath(plate_path)
        painter.setPen(pen)
        mark = self._folder_icon(color.name(), 24)
        painter.drawPixmap(plate.x() + (plate.width() - mark.width()) // 2,
                           plate.y() + (plate.height() - mark.height()) // 2, mark)
        if selected:
            ring = QPainterPath()
            ring.addRoundedRect(plate.adjusted(-2, -2, 2, 2), 9, 9)
            painter.setPen(QColor(t.accent))
            painter.drawPath(ring)
        text_x = plate.right() + 14
        text_w = rect.right() - text_x - 12
        name_font = font_for(folder.name, "doc_title", t)
        painter.setPen(QColor(t.text_primary))
        painter.setFont(name_font)
        title_rect = QRect(text_x, rect.y() + 12, text_w, 24)
        painter.save()
        painter.setClipRect(title_rect)
        painter.drawText(title_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         _elided(name_font, folder.name, max(8, text_w - 4)))
        painter.restore()
        count = self._folder_count(folder.id)
        painter.setPen(QColor(t.text_secondary if hover_p > 0.5 else t.text_muted))
        painter.setFont(self._meta_font)
        count_rect = QRect(text_x, title_rect.bottom() + 1, text_w, 20)
        painter.save()
        painter.setClipRect(count_rect)
        painter.drawText(count_rect,
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"{count} item{'s' if count != 1 else ''}")
        painter.restore()

    # -- document row (list): quiet horizontal ----------------------------------

    def _paint_row(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item = index.data(Role.ITEM)
        if item is None:
            return
        t = self._tokens
        rect = option.rect.adjusted(0, 2, 0, -2)
        selected = self._selected(option)
        _occ_p = self._hover_progress_for(item, index)
        hover = bool(self._hover(option) and _occ_p > 0.0)
        try:
            if getattr(self.parent(), "_hover_key", None) is None:
                hover = self._hover(option)
        except Exception:
            pass
        # M4.6.9: list rows share the card press response (tight
        # geometry — darken only, never layout).
        if self._pressed(option):
            painter.fillRect(rect, QColor(0, 0, 0, 10))
        # M4.6.5: rows lift the thumbnail only (tight geometry, no wash).
        lift, _alpha = self._hover_lift(item, selected, index)
        thumb = index.data(Role.THUMB)
        failed = item.id in getattr(self.parent(), "_thumb_failed", set())
        thumb_rect = QRect(rect.x() + 4, rect.y() + 6 - lift, 44, 58)
        if lift:
            parent = self.parent()
            try:
                progress = float(getattr(parent, "_hover_progress", 0.0))
            except Exception:
                progress = 0.0
            self._paint_lift_shadow(painter, thumb_rect, progress)
        self._draw_thumb(painter, thumb_rect, thumb, item.missing, failed, selected, item.id,
                         row=index.row())
        text_x = thumb_rect.right() + 14
        text_w = rect.right() - text_x - 12
        title_rect = QRect(text_x, rect.y() + 12, text_w, 24)
        meta_rect = QRect(text_x, title_rect.bottom() + 1, text_w, 20)
        self._paint_titles(painter, title_rect, meta_rect, item, selected, hover,
                           meta_text=self._row_meta(index))
