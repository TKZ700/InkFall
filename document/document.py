"""Managed document (M1).

A :class:`Document` owns exactly one :class:`PdfBackend` handle and
exposes document-level operations in terms of Inkfall models only.
Application code never touches the backend (or PyMuPDF) directly.
"""

from __future__ import annotations

import dataclasses
import os
import tempfile

from core.types import DocumentId, PageId
from annotations.models import (
    AnnotationBase,
    Highlighter,
    InkStroke,
    Shape,
    ShapeKind,
    StickyNote,
)
from annotations.page_rotation import rotate_page_annotations
from annotations.persistence import (
    ManifestError,
    decode_manifest,
    encode_manifest,
)
from annotations.store import AnnotationStore
from document.annotation_pdf import native_specs
from document.backend import PdfBackend
from document.errors import (
    InvalidPdfError,
    ReadOnlyDocumentError,
)
from document.models import DocumentInfo, DocumentMetadata, PageInfo, page_id_for
from document.paper import (
    PaperSpec,
    paper_metadata_value,
    paper_spec_from_metadata,
)

__all__ = ["Document", "RemovedPage"]

#: mm -> PDF points, the app-wide width convention (see backend).
_MM_TO_PT = 72.0 / 25.4


def _stroke_width_pt(width_mm) -> float:
    try:
        return max(0.2, float(width_mm) * _MM_TO_PT)
    except (TypeError, ValueError):
        return 0.5 * _MM_TO_PT


def _dash_pattern(line_style) -> str | None:
    """Export dash spec for a shape line style (PDF dash string)."""
    style = str(line_style or "solid")
    if style == "dashed":
        return "[5 4] 0"
    if style == "dotted":
        return "[1 3] 0"
    return None


def _hex(value) -> str:
    text = str(value or "").strip()
    return text if text.startswith("#") else "#" + text.lstrip("#")


def _contrast_ink(color_hex: str) -> str:
    """Readable text color for a filled note/box (luminance split)."""
    text = str(color_hex or "").lstrip("#")
    if len(text) != 6:
        return "#1b1e26"
    try:
        r = int(text[0:2], 16) / 255.0
        g = int(text[2:4], 16) / 255.0
        b = int(text[4:6], 16) / 255.0
    except Exception:
        return "#1b1e26"
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1b1e26" if luminance > 0.55 else "#f2f4f8"


def _polyline_items(points, color, alpha, width, dashes=None,
                    fill=None, fill_alpha=0.0, close=False) -> list:
    """One stroke as export items (single-point strokes become dots)."""
    pts = [(float(p.x), float(p.y)) for p in points or ()]
    if not pts:
        return []
    if len(pts) == 1:
        x, y = pts[0]
        radius = max(0.4, float(width) * 0.5)
        return [("ell", x, y, radius, radius, _hex(color),
                 max(0.0, min(1.0, alpha)), 0.2,
                 _hex(color), max(0.0, min(1.0, alpha)))]
    return [("poly", tuple(pts), _hex(color),
             max(0.0, min(1.0, alpha)), float(width),
             _hex(fill) if fill is not None else None,
             max(0.0, min(1.0, fill_alpha)), bool(close), dashes)]


def _rotate_point(x: float, y: float, cx: float, cy: float,
                  degrees: float) -> tuple[float, float]:
    """Rotate about a center (positive = clockwise in y-down page space)."""
    import math

    rad = math.radians(float(degrees))
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = x - cx, y - cy
    return (cx + dx * cos_a - dy * sin_a,
            cy + dx * sin_a + dy * cos_a)


def _arrow_head(tip, tail, width, color, alpha) -> list:
    """Two-segment arrow head at ``tip`` pointing away from ``tail``."""
    import math

    tx, ty = float(tip[0]), float(tip[1])
    sx, sy = float(tail[0]), float(tail[1])
    dx, dy = tx - sx, ty - sy
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return []
    ux, uy = dx / length, dy / length
    head = max(6.0, float(width) * 4.0)
    wing = head * 0.5
    # Perpendicular of the unit direction.
    px, py = -uy, ux
    left = (tx - ux * head + px * wing, ty - uy * head + py * wing)
    right = (tx - ux * head - px * wing, ty - uy * head - py * wing)
    return [
        ("seg", left[0], left[1], tx, ty, _hex(color), alpha, width, None),
        ("seg", right[0], right[1], tx, ty, _hex(color), alpha, width, None),
    ]


def _shape_items(shape: Shape) -> list:
    """Translate one Shape into export items (page-point coords)."""
    width = _stroke_width_pt(shape.width_mm)
    alpha = max(0.0, min(1.0, float(shape.opacity)))
    dashes = _dash_pattern(shape.line_style)
    fill = shape.fill_color if shape.fill_enabled else None
    fill_alpha = float(shape.fill_opacity) if shape.fill_enabled else 0.0
    color = shape.color
    items = []

    if shape.kind in (ShapeKind.LINE, ShapeKind.ARROW):
        points = list(shape.points or ())
        if len(points) < 2:
            return []
        start = (float(points[0].x), float(points[0].y))
        end = (float(points[-1].x), float(points[-1].y))
        items.append(("seg", start[0], start[1], end[0], end[1],
                      _hex(color), alpha, width, dashes))
        # Line endings (M6.7 vocabulary): dot/bar markers at both ends,
        # arrow heads where the shape kind or ending asks for one.
        def _ending(ending, at_tip, at_other):
            if ending == "arrow":
                return _arrow_head(at_tip, at_other, width, color, alpha)
            if ending == "dot":
                radius = max(0.6, width * 1.4)
                return [("ell", at_tip[0], at_tip[1], radius, radius,
                         _hex(color), alpha, 0.2, _hex(color), alpha)]
            if ending == "bar":
                import math

                dx = at_tip[0] - at_other[0]
                dy = at_tip[1] - at_other[1]
                length = math.hypot(dx, dy) or 1.0
                px, py = -dy / length, dx / length
                half = max(3.0, width * 2.0)
                return [("seg", at_tip[0] - px * half, at_tip[1] - py * half,
                         at_tip[0] + px * half, at_tip[1] + py * half,
                         _hex(color), alpha, width, None)]
            return []

        items.extend(_ending(shape.start_ending, start, end))
        if shape.kind == ShapeKind.ARROW:
            items.extend(_arrow_head(end, start, width, color, alpha))
        else:
            items.extend(_ending(shape.end_ending, end, start))
        return items

    if shape.kind == ShapeKind.RECTANGLE:
        rect = shape.rect
        x0 = float(rect.origin.x)
        y0 = float(rect.origin.y)
        w = float(rect.size.width)
        h = float(rect.size.height)
        cx, cy = x0 + w * 0.5, y0 + h * 0.5
        corners = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        if abs(float(shape.rotation_deg)) > 1e-6:
            corners = [_rotate_point(x, y, cx, cy, shape.rotation_deg)
                       for x, y in corners]
        return [("poly", tuple(corners), _hex(color), alpha, width,
                 _hex(fill) if fill is not None else None, fill_alpha,
                 True, dashes)]

    if shape.kind == ShapeKind.ELLIPSE:
        rect = shape.rect
        x0 = float(rect.origin.x)
        y0 = float(rect.origin.y)
        w = float(rect.size.width)
        h = float(rect.size.height)
        cx, cy = x0 + w * 0.5, y0 + h * 0.5
        rx, ry = w * 0.5, h * 0.5
        if abs(float(shape.rotation_deg)) <= 1e-6:
            return [("ell", cx, cy, rx, ry, _hex(color), alpha, width,
                     _hex(fill) if fill is not None else None, fill_alpha)]
        import math

        points = []
        for step in range(48):
            angle = math.tau * step / 48.0
            px = cx + rx * math.cos(angle)
            py = cy + ry * math.sin(angle)
            points.append(_rotate_point(px, py, cx, cy, shape.rotation_deg))
        return [("poly", tuple(points), _hex(color), alpha, width,
                 _hex(fill) if fill is not None else None, fill_alpha,
                 True, dashes)]

    if shape.kind == ShapeKind.POLYGON:
        points = [(float(p.x), float(p.y)) for p in shape.points or ()]
        if len(points) < 2:
            return []
        return [("poly", tuple(points), _hex(color), alpha, width,
                 _hex(fill) if (fill is not None and shape.close_path)
                 else None,
                 fill_alpha if shape.close_path else 0.0,
                 bool(shape.close_path), dashes)]

    return []


def _sticky_note_items(note: StickyNote) -> list:
    """Translate one StickyNote into a filled box + text export items."""
    try:
        x = float(note.position.x)
        y = float(note.position.y)
    except Exception:
        return []
    text = str(note.text or "")
    lines = max(1, text.count("\n") + 1)
    box_w = 190.0
    box_h = max(64.0, 16.0 * min(lines, 12) + 20.0)
    color = _hex(note.color)
    items = [("poly",
              ((x, y), (x + box_w, y), (x + box_w, y + box_h), (x, y + box_h)),
              color, 0.9, 0.8, color, 0.92, True, None)]
    if text:
        items.append(("text", x + 9.0, y + 7.0, box_w - 18.0, box_h - 14.0,
                      text, _contrast_ink(color), 9.5))
    return items


def _annotation_export_items(annotations) -> list:
    """Translate one page's annotations into the backend draw vocabulary.

    Everything the annotation store can hold is represented:
    ``InkStroke`` and ``Highlighter`` as vector polylines (highlighter
    keeps its translucent band opacity), every ``ShapeKind`` with its
    stroke/fill/dash/endings, and ``StickyNote`` as a filled box with
    text. Geometry is in the page's displayed frame (matching the
    viewer); the backend derotates it at save time.
    """
    items = []
    for annotation in annotations or ():
        try:
            if isinstance(annotation, InkStroke):
                items.extend(_polyline_items(
                    annotation.points, annotation.color,
                    annotation.opacity,
                    _stroke_width_pt(annotation.width_mm)))
            elif isinstance(annotation, Highlighter):
                items.extend(_polyline_items(
                    annotation.points, annotation.color,
                    annotation.opacity,
                    _stroke_width_pt(annotation.width_mm)))
            elif isinstance(annotation, Shape):
                items.extend(_shape_items(annotation))
            elif isinstance(annotation, StickyNote):
                items.extend(_sticky_note_items(annotation))
        except Exception:
            continue
    return items


def _identity_key(annotation_id) -> str:
    return str(getattr(annotation_id, "value", annotation_id))


def _page_key(page_id) -> str:
    return str(getattr(page_id, "value", page_id))


def _unrotated_size(width: float, height: float, rotation: int):
    """Media-box dims from a pre-rotated page rect + its rotation."""
    if int(rotation) % 180 == 90:
        return (float(height), float(width))
    return (float(width), float(height))


@dataclasses.dataclass(frozen=True, slots=True)
class RemovedPage:
    """Everything needed to put a deleted page back exactly (M6.8.2).

    ``blob`` is a standalone single-page PDF snapshot (content,
    intrinsic rotation, metadata); ``annotations`` are the Inkfall
    annotations that lived on the page, in insertion order.
    """

    index: int
    blob: bytes
    annotations: tuple = ()


class Document:
    """Single open PDF with deterministic lifecycle."""

    def __init__(self, backend: PdfBackend, normalized_path: str,
                 unsaved: bool = False,
                 paper_spec: PaperSpec | None = None,
                 read_only: bool = False) -> None:
        self._backend = backend
        self._normalized_path = normalized_path
        # Read-only duplicate session: this Document is a second, explicit
        # viewing session of a file whose primary session is already open.
        # It shares the source path's registry semantics (never registered
        # as THE path owner) and refuses every mutating operation at this
        # boundary — see :meth:`_ensure_writable`.
        self._read_only = bool(read_only)
        # M8 New Blank: an unsaved workspace is a real managed document
        # from the first keystroke. ``_unsaved`` marks that it has no
        # user-chosen destination yet; ``_saved_path`` is the filesystem
        # path it was last saved to (None until the first Save As);
        # ``_render_path`` (below) stays a private working copy and is
        # never the saved destination. Three distinct identities, one
        # owner each: registry key, save target, render source.
        self._unsaved = bool(unsaved)
        #: Actual filesystem path of the last successful save. An opened
        #: PDF's source path is tracked separately so Save can write back
        #: to it without a picker while the save target remains distinct
        #: from the render working copy.
        self._saved_path: str | None = None
        self._source_path: str | None = (
            None if self._unsaved else str(backend.path))
        self._dirty = False
        # A malformed/unsupported manifest must never be downgraded on
        # save. Native annotations stay visible; the session is readable
        # but Save is refused with this diagnostic.
        self._persistence_blocked = False
        self._persistence_error: str | None = None
        # Retained for compatibility with older callers; same-path saving
        # is now the normal behavior.
        self._needs_save_as = False
        # The template spec is recoverable from PDF keywords; a live
        # blank also gets it directly so insertion works even if the
        # metadata write at creation was best-effort.
        self._paper_spec = (paper_spec.normalized()
                            if paper_spec is not None else None)
        self._paper_spec_loaded = paper_spec is not None
        # M6.1: document-owned annotation store (per-page order +
        # stable identity). Independent of any viewer widget; cleared
        # on close so abandoned documents keep nothing alive.
        self._annotations = AnnotationStore(
            page_count=backend.page_count)
        # M6.8.2 Stage 5.1: the render pipeline reads from a *path*, so
        # an in-memory page edit is only visible to it through a working
        # copy. ``_render_path`` starts at the original and is repointed
        # at a temp file after the first page edit; the original file on
        # disk is never written.
        self._render_path = (str(backend.path) if not self._unsaved
                             else normalized_path)
        self._work_paths: list[str] = []

    @property
    def annotations(self) -> AnnotationStore:
        """This document's annotation store (viewer-independent).

        A read-only duplicate session hands out a FROZEN store: every
        mutating operation raises (AnnotationStoreReadOnlyError). The
        freeze is applied on first hand-out only, so this document's own
        internal replay and re-keying — which use the private attribute
        during open — are unaffected.
        """
        if self._read_only and not self._annotations.read_only:
            self._annotations.read_only = True
        return self._annotations

    @property
    def render_path(self) -> str:
        """Path the render pipeline should read (working copy or original)."""
        return self._render_path

    # -- M8 unsaved-workspace state -------------------------------------

    @property
    def is_unsaved(self) -> bool:
        """True while this document has no user-chosen destination file."""
        return bool(self._unsaved)

    @property
    def saved_path(self) -> str | None:
        """Filesystem path of the last successful save (None if never)."""
        return self._saved_path

    @property
    def save_target(self) -> str | None:
        """Destination Ctrl+S should use without a picker.

        Opened PDFs target their source path; saved documents target their
        last written file; a brand-new blank has no target until its first
        save.
        """
        return self._saved_path or self._source_path

    @property
    def reading_key(self) -> str:
        """Stable identity for reading-position storage.

        This is the registry key (normalized source path, or a synthetic
        blank key), never the temporary render working copy. A read-only
        duplicate session gets its own suffix so its navigation can never
        overwrite the primary session's reading position (or vice versa).
        """
        if self._read_only:
            return f"{self._normalized_path}#read-only"
        return self._normalized_path

    @property
    def read_only(self) -> bool:
        """True when this session can never write the source PDF."""
        return bool(self._read_only)

    @property
    def persistence_blocked(self) -> bool:
        """True when the Inkfall manifest cannot be safely rewritten."""
        return bool(self._persistence_blocked)

    @property
    def persistence_error(self) -> str | None:
        """Human-readable reason :attr:`persistence_blocked` is set."""
        return self._persistence_error

    @property
    def paper_spec(self) -> PaperSpec | None:
        """Recoverable Inkfall paper definition, or None for a plain PDF.

        Decoded lazily from PDF keywords so Save As/reopening preserves
        the template. Never inferred from rendered vector content.
        """
        if not self._paper_spec_loaded:
            try:
                self._paper_spec = paper_spec_from_metadata(
                    self._backend.metadata.keywords)
            except Exception:
                self._paper_spec = None
            self._paper_spec_loaded = True
        return self._paper_spec

    @property
    def can_save(self) -> bool:
        """True when Save can preserve this session without data loss.

        Opened PDFs save back to their source path; saved documents save to
        their last written file; new blanks ask for a path once. A document
        whose embedded manifest is unsupported or unsafe is deliberately
        not saveable so its original file is never overwritten with a
        downgraded manifest. A read-only duplicate session is never
        saveable, regardless of state.
        """
        if self._read_only or self._persistence_blocked or self.is_closed:
            return False
        return bool(self._unsaved or self.save_target is not None)

    def _ensure_writable(self) -> None:
        """Refuse every mutating operation on a read-only session.

        The boundary for the duplicate-view guarantee: page operations,
        save preparation, and export all pass through here, so no write
        path can reach the shared source file from a read-only session
        even if a UI gate is missed.
        """
        if self._read_only:
            raise ReadOnlyDocumentError(
                "This read-only session cannot modify the document.")

    @property
    def needs_save_as(self) -> bool:
        """Legacy compatibility flag; same-path Save is now supported."""
        return False

    @property
    def is_dirty(self) -> bool:
        """True when the session holds changes not yet exported to disk."""
        return bool(self._dirty)

    def mark_changed(self) -> bool:
        """Record a session content change; True when the flag flipped.

        Every live document tracks dirtiness, including opened PDFs that
        now save back to their source path. That lets explicit close and
        application exit warn before losing edits; Save availability is a
        separate question (see :attr:`can_save`).
        """
        if self._dirty:
            return False
        self._dirty = True
        return True

    def clear_dirty(self) -> None:
        """Forget the dirty flag (a successful save is the only caller)."""
        self._dirty = False

    def commit_saved(self, normalized_path: str,
                     save_path: str | None = None) -> None:
        """Adopt the saved file's identity after a successful export.

        Called by :class:`DocumentManager` once the registry has been
        re-keyed. The session itself is untouched: the backend handle,
        annotation store, history, and the private render working copy
        all stay live, so editing continues without a reopen.
        """
        self._normalized_path = normalized_path
        self._saved_path = str(save_path) if save_path else normalized_path
        self._unsaved = False
        self._dirty = False
        self._needs_save_as = False

    def _block_persistence(self, message: str) -> None:
        self._persistence_blocked = True
        self._persistence_error = str(message)

    def restore_persisted_annotations(self) -> bool:
        """Load the versioned manifest and strip matching native copies.

        The manifest is authoritative. A missing manifest is a normal
        ordinary PDF. An unsupported version, malformed record, page-count
        mismatch, or failed native cleanup marks the document save-blocked
        without overwriting anything; no partially restored store is kept.
        """
        if self._unsaved:
            return True
        try:
            payload = self._backend.read_annotation_manifest()
        except Exception as exc:
            self._block_persistence(
                f"Cannot read the Inkfall annotation manifest: {exc}")
            return False
        if payload is None:
            return True
        try:
            records = decode_manifest(
                payload, self.page_count,
                lambda index: page_id_for(self.id, index))
        except ManifestError as exc:
            self._block_persistence(
                f"Cannot safely update this document's annotations: {exc}")
            return False
        except Exception as exc:
            self._block_persistence(
                f"Cannot restore the Inkfall annotation manifest: {exc}")
            return False
        if not records:
            return True

        identities = {str(annotation.id.value)
                      for _page, annotation in records}
        removed = 0
        try:
            removed = self._backend.remove_native_annotations(identities)
        except Exception as exc:
            self._block_persistence(
                f"Cannot remove matching native PDF annotations safely: {exc}")
            return False

        try:
            for page_index, annotation in records:
                self._annotations.add(annotation, page_index)
        except Exception as exc:
            try:
                self._annotations.clear()
            except Exception:
                pass
            self._block_persistence(
                f"Cannot restore annotations into the edit store: {exc}")
            return False

        # A native annotation is rendered by the PDF rasterizer itself. Once
        # matching copies are stripped from the live handle, do not keep
        # rendering the original file (which still contains them) or the
        # viewer would paint the same annotation twice.
        if removed:
            try:
                self._materialize()
            except Exception as exc:
                try:
                    self._annotations.clear()
                except Exception:
                    pass
                self._block_persistence(
                    f"Cannot prepare a sanitized render copy: {exc}")
                return False
        return True

    def _is_source_target(self, target_path: str) -> bool:
        if self._source_path is None:
            return False
        try:
            return (os.path.normcase(os.path.abspath(
                        os.path.expanduser(str(target_path))))
                    == os.path.normcase(os.path.abspath(
                        os.path.expanduser(self._source_path))))
        except Exception:
            return False

    def _render_is_source(self) -> bool:
        if self._source_path is None:
            return False
        try:
            return (os.path.normcase(os.path.abspath(self._render_path))
                    == os.path.normcase(os.path.abspath(self._source_path)))
        except Exception:
            return False

    def _export_snapshot_candidate(self) -> str | None:
        """Current materialized working copy, if it can back the export.

        The newest work copy always mirrors the backend handle's page
        state: every handle-mutating operation (page edits, restore-time
        native stripping) materializes immediately, and annotations never
        touch the handle — so the export can reuse it instead of writing
        a second identical full serialization of the same content.
        ``None`` (renderer still on the source, or the work copy is
        unreadable) falls back to the backend's own snapshot write.
        """
        if self._render_path and not self._render_is_source():
            candidate = self._render_path
            try:
                if os.path.isfile(candidate):
                    return candidate
            except Exception:
                pass
        return None

    def prepare_for_save(self, target_path: str) -> bool:
        """Detach the renderer from the source before a same-path save.

        Windows keeps a file locked while a render worker has it open, and
        the rasterizer would also show pre-existing native duplicates. A
        sanitized working copy is materialized first. Returns True when the
        render path changed; the viewer must rebind its pipeline in that
        case.
        """
        self._ensure_writable()
        if self._persistence_blocked:
            raise InvalidPdfError(
                self._persistence_error
                or "This document's annotation manifest cannot be updated "
                   "safely.")
        # Refuse an encrypted source that cannot be rewritten with its
        # protection intact before any temp file or replacement work.
        self._backend.ensure_export_supported()
        if not self._is_source_target(target_path) or not self._render_is_source():
            return False
        self._materialize()
        return True

    def ensure_render_file(self) -> str:
        """Materialize the initial working copy for an unsaved blank.

        The render engine requires a readable file, so a brand-new
        in-memory blank gets its working copy at creation time — the
        same ``_materialize`` mechanism every page edit already uses.
        Never touches a real document's original path.
        """
        try:
            if self._render_path and os.path.isfile(self._render_path):
                return self._render_path
        except Exception:
            pass
        self._materialize()
        return self._render_path

    def export_page_items(self) -> list:
        """Per-page export items for the current annotation snapshot."""
        page_items = []
        count = int(self._backend.page_count)
        for index in range(count):
            try:
                annotations = list(self._annotations.page(index))
            except Exception:
                annotations = []
            page_items.append(_annotation_export_items(annotations))
        return page_items

    def write_export(self, target_path: str, *, progress=None,
                     pump=None) -> None:
        """Write a complete, transactional PDF snapshot to disk.

        The authoritative manifest is serialized before touching any file.
        Native annotations are inserted exactly once into a fresh snapshot,
        and the backend verifies the result before atomic replacement. The
        live annotation store, undo history, and viewport are not modified;
        a failure leaves the session usable.

        ``progress`` (optional) receives the backend's opaque stage names.
        ``pump`` (optional) selects the process-backed export used by the
        UI: it is an event-pumping wait primitive so Qt stays responsive
        while the child process does the GIL-holding MuPDF work. Without
        it the export runs synchronously in this process.
        """
        self._ensure_writable()
        if self._persistence_blocked:
            raise InvalidPdfError(
                self._persistence_error
                or "This document's annotation manifest cannot be updated "
                   "safely.")
        self._backend.ensure_export_supported()
        try:
            manifest_bytes = encode_manifest(
                self._annotations, self.page_count)
        except ManifestError as exc:
            raise InvalidPdfError(
                f"Cannot save annotations safely: {exc}") from exc
        native_pages = []
        for index in range(self.page_count):
            try:
                annotations = list(self._annotations.page(index))
            except Exception:
                annotations = []
            native_pages.append(native_specs(annotations))
        paper_keywords = None
        spec = self.paper_spec
        if spec is not None:
            try:
                paper_keywords = paper_metadata_value(spec)
            except Exception as exc:
                raise InvalidPdfError(
                    f"Cannot preserve PaperSpec metadata: {exc}") from exc
        if pump is not None:
            self._backend.export_pdf_in_process(
                target_path, native_pages, manifest_bytes, self.page_count,
                paper_keywords, progress=progress, pump=pump,
                snapshot_path=self._export_snapshot_candidate())
        else:
            self._backend.export_pdf(
                target_path, native_pages, manifest_bytes, self.page_count,
                paper_keywords, progress=progress)

    @property
    def id(self) -> DocumentId:
        """Stable document ID assigned by the backend at open time."""
        return self._backend.document_id

    @property
    def normalized_path(self) -> str:
        """Normalized absolute path used as the manager registry key."""
        return self._normalized_path

    @property
    def is_closed(self) -> bool:
        """True after :meth:`close`."""
        return self._backend.is_closed

    @property
    def page_count(self) -> int:
        """Number of pages (no page processing)."""
        return self._backend.page_count

    @property
    def metadata(self) -> DocumentMetadata:
        """Basic PDF metadata."""
        return self._backend.metadata

    @property
    def is_encrypted(self) -> bool:
        """True if the underlying file is encrypted."""
        return self._backend.is_encrypted

    def page_info(self, index: int) -> PageInfo:
        """Lazy per-page geometry + rotation."""
        return self._backend.page_info(index)

    # -- M6.8.2 Stage 5.1 page operations --------------------------------

    def rotate_page(self, index: int, degrees: int) -> int:
        """Rotate one page (clockwise quarter turns) and its annotations.

        The page's intrinsic ``/Rotate`` changes cumulatively and the
        annotations attached to the page are re-expressed in the new
        displayed frame so they stay glued to the PDF content. Returns
        the new intrinsic rotation.

        Ordering is deliberate: the only mutation performed before the
        fallible working-copy write is the backend ``/Rotate`` itself, and
        an inverse rotation restores it exactly (integer quarter turns),
        so a failed write cannot leave a partial mutation. Annotations
        (whose remap involves float arithmetic) are only touched once the
        write has succeeded.
        """
        self._ensure_writable()
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"index must be an int, got {index!r}")
        if isinstance(degrees, bool) or not isinstance(degrees, int):
            raise ValueError(f"degrees must be an int quarter turn: {degrees!r}")
        if degrees % 90 != 0:
            raise ValueError(f"degrees must be a multiple of 90: {degrees!r}")
        delta = int(degrees) % 360
        info = self._backend.page_info(index)  # validates index first
        if delta == 0:
            return int(info.rotation)
        media_w, media_h = _unrotated_size(
            info.size.width, info.size.height, info.rotation)
        old_rotation = int(info.rotation) % 360
        new_rotation = (old_rotation + delta) % 360
        new_value = self._backend.rotate_page(index, delta)
        try:
            self._materialize()
        except Exception:
            try:
                self._backend.rotate_page(index, (-delta) % 360)
            except Exception:
                pass
            raise
        self._rotate_page_annotations(
            index, media_w, media_h, old_rotation, new_rotation)
        return new_value

    def delete_page(self, index: int) -> RemovedPage:
        """Delete one page and its annotations; returns a restore token.

        The page is removed from the active document (content + metadata
        + annotations), later pages shift up one index, and every
        remaining annotation is re-keyed to its new page. The last
        remaining page may not be deleted. Validated before any
        mutation, so a rejected call changes nothing.

        As in :meth:`rotate_page`, only the backend page tree is changed
        before the fallible working-copy write, and the exact page blob
        re-inserted restores it, so a failed write leaves no partial
        mutation.
        """
        self._ensure_writable()
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"index must be an int, got {index!r}")
        if not 0 <= index < self._backend.page_count:
            raise ValueError(
                f"index {index!r} out of range [0, {self._backend.page_count})")
        if self._backend.page_count <= 1:
            raise ValueError("cannot delete the only page of a document")
        annotations = self._annotations.page(index)
        blob = self._backend.extract_page(index)
        self._backend.delete_page(index)
        try:
            self._materialize()
        except Exception:
            try:
                self._backend.insert_page(index, blob)
            except Exception:
                pass
            raise
        self._annotations.remove_page(index)
        self._reindex_annotation_pages()
        return RemovedPage(index=index, blob=blob, annotations=annotations)

    def insert_page(self, removed: RemovedPage) -> None:
        """Put a :class:`RemovedPage` back at its original index.

        Restores the page content, intrinsic rotation, metadata, and the
        annotations with their original identities and order; later pages
        shift back down. Validated before any mutation.
        """
        self._ensure_writable()
        if not isinstance(removed, RemovedPage):
            raise TypeError(
                f"removed must be a RemovedPage, got {type(removed).__name__}")
        index = removed.index
        if not 0 <= index <= self._backend.page_count:
            raise ValueError(
                f"index {index!r} out of range [0, {self._backend.page_count}]")
        self._backend.insert_page(index, removed.blob)
        try:
            self._materialize()
        except Exception:
            try:
                self._backend.delete_page(index)
            except Exception:
                pass
            raise
        self._annotations.insert_page(index)
        for annotation in removed.annotations:
            self._annotations.add(annotation, index)
        self._reindex_annotation_pages()

    def insert_blank_page(self, index: int, spec: PaperSpec | None,
                          width: float, height: float) -> None:
        """Insert one generated blank page at ``index`` (0..page_count).

        ``spec`` is the document's recovered template definition, or
        ``None`` for a plain-page fallback. ``width`` / ``height`` are
        the target page's displayed dimensions, so landscape and rotated
        targets stay visually consistent. Later pages shift down and
        every annotation is re-keyed to its new page.

        As with the other page operations, the fallible working-copy
        write happens after the backend page tree change and is rolled
        back exactly if it fails.
        """
        self._ensure_writable()
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError(f"index must be an int, got {index!r}")
        if not 0 <= index <= self._backend.page_count:
            raise ValueError(
                f"index {index!r} out of range [0, {self._backend.page_count}]")
        if spec is not None and not isinstance(spec, PaperSpec):
            raise TypeError(
                f"spec must be a PaperSpec or None, got {type(spec).__name__}")
        try:
            page_width = float(width)
            page_height = float(height)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"invalid blank page size: {width!r} x {height!r}") from e
        if page_width <= 0.0 or page_height <= 0.0:
            raise ValueError(
                f"blank page size must be positive: {page_width} x {page_height}")
        self._backend.insert_blank_page(
            index, spec, page_width, page_height)
        try:
            self._materialize()
        except Exception:
            try:
                self._backend.delete_page(index)
            except Exception:
                pass
            raise
        self._annotations.insert_page(index)
        self._reindex_annotation_pages()

    def _rotate_page_annotations(self, page_index: int, media_w: float,
                                 media_h: float, old_rotation: int,
                                 new_rotation: int) -> None:
        """Re-express one page's annotations in its new displayed frame."""
        store = self._annotations
        try:
            annotations = list(store.page(page_index))
        except Exception:
            return
        if not annotations:
            return
        candidates = rotate_page_annotations(
            annotations, media_w=media_w, media_h=media_h,
            old_rotation=old_rotation, new_rotation=new_rotation)
        for position, (original, candidate) in enumerate(
                zip(annotations, candidates)):
            if candidate is original:
                continue
            try:
                store.substitute([original.id], [candidate], page_index,
                                 position)
            except Exception:
                continue

    def _reindex_annotation_pages(self) -> None:
        """Re-key every annotation to its current page (identity-based).

        Page ids are index-derived (``page_id_for``), so a page-count
        change leaves annotations on shifted pages pointing at a stale
        page. Re-deriving from the store index is deterministic and
        idempotent (a no-op when nothing shifted).
        """
        store = self._annotations
        document_id = self.id
        for page_index in range(store.page_count):
            expected = page_id_for(document_id, page_index)
            expected_key = _page_key(expected)
            for position, annotation in enumerate(list(store.page(page_index))):
                if _page_key(getattr(annotation, "page_id", None)) == expected_key:
                    continue
                candidate = dataclasses.replace(annotation, page_id=expected)
                try:
                    store.substitute([annotation.id], [candidate], page_index,
                                     position)
                except Exception:
                    continue

    def _materialize(self) -> None:
        """Write the in-memory document to a fresh working copy.

        The render pipeline reads from a path; a fresh, unique temp path
        per edit guarantees no conflict with a still-open previous
        handle. Older working copies are pruned best-effort here (the
        caller releases the previous pipeline before calling a page
        operation) and again on close.
        """
        handle, path = tempfile.mkstemp(prefix="inkfall-work-",
                                        suffix=".pdf")
        os.close(handle)
        try:
            self._backend.save_copy(path)
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            raise
        previous = self._render_path
        self._work_paths.append(path)
        self._render_path = path
        self._prune_work_files(keep=path)

    def _prune_work_files(self, keep: str | None = None) -> None:
        try:
            protected = self._backend.pending_snapshot
        except Exception:
            protected = None
        remaining = []
        for path in self._work_paths:
            if path == keep or (protected is not None and path == protected):
                # ``keep`` backs the renderer; a pending snapshot backs
                # the live handle after a save. Neither may vanish here.
                remaining.append(path)
                continue
            try:
                os.remove(path)
            except FileNotFoundError:
                # Already retired elsewhere (e.g. the next same-path save
                # deletes its borrowed predecessor): drop the stale entry.
                continue
            except Exception:
                remaining.append(path)
        self._work_paths = remaining

    def info(self) -> DocumentInfo:
        """Snapshot descriptor for this document."""
        meta = self._backend.metadata
        if self._unsaved:
            title = "Untitled"
        elif self._saved_path:
            title = self._saved_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        else:
            title = (meta.title
                     or self._backend.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1])
        return DocumentInfo(
            id=self._backend.document_id,
            title=title,
            page_count=self._backend.page_count,
            path=self._normalized_path,
            metadata=meta,
            is_encrypted=meta.is_encrypted,
        )

    def close(self) -> None:
        """Release backend resources. Safe to call repeatedly."""
        try:
            self._annotations.clear()
        except Exception:
            pass
        try:
            self._prune_work_files(keep=None)
            self._render_path = self._normalized_path
        except Exception:
            pass
        self._backend.close()
