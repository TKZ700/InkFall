"""Floating page-preview navigator: a fixed-height page filmstrip.

The bottom-right control pair gains a Pages segment beside the View
segment (one shared capsule, two independent controls). Pages toggles
ONE floating panel with a fixed, deliberately designed size:

* A near-full-width horizontal filmstrip of real page previews floating
  above the capsule. It is a plain child of :class:`PdfViewer` (the
  shelf/undo-HUD/ViewControls overlay pattern): it never enters the
  viewer layout, so the document viewport, page geometry and scrollbar
  ranges are untouched while it is open.
* There is NO resize/expand gesture: the panel has one fixed height and
  every page is reached through horizontal scrolling (mouse, trackpad,
  touch). Page selection, current-page highlighting, Escape/close
  dismissal, and safe outside-click behavior are unchanged.

Thumbnails are REAL page renders with bounded resource usage. The
delivery architecture is deliberately the proven ThumbnailService
shape, with rendering where the project's own GIL evidence says it
belongs: a 2-worker
:class:`rendering.process_decoder.ProcessDecoder` (child PROCESSES,
each owning its PDF handle — the same GIL-free rasterization path the
render pipeline uses, because in-process MuPDF rasters hold the GIL and
starve the GUI thread) renders each page; the collector hands the RGBA
buffer to the engine through a PLAIN method call (thread-safe
bookkeeping under a lock), the QImage is built and detached there, and
an ARGUMENT-LESS ``updated`` signal is queued to the GUI thread for the
repaint — no arguments cross the thread boundary, so there is nothing
to marshal and nothing to drop silently.

Requests are prioritized (current page and neighbors first, then the
visible window, then a small lookahead), queued work that scrolls out
of range is dropped, and the cache is a bounded LRU whose keys carry
the render working copy's path plus ``mtime_ns``/size, so a save, page
operation, or any other source change can never display stale pixels.

Virtualization: the filmstrip keeps ONE painted widget for the whole
document. Every page occupies a uniform slot, so the visible page
range, hit tests, and the scroll extent are constant-time arithmetic
from the scroll offset (no scan from page zero, no per-page table, no
widget per page), and only the visible window plus a small lookahead
is painted and requested. Opening the panel performs no PDF I/O and no
per-page work beyond the geometry snapshot it already had.

Known, deliberate limitation: a thumbnail shows the PDF page content and
native PDF annotations of the render working copy. Inkfall's LIVE
annotation overlays (strokes/shapes held in the AnnotationStore and
painted by the viewer on top of display tiles) are NOT part of the
rendered pixels and therefore not in the previews. No second annotation
renderer exists for this feature.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QRunnable,
    QRect,
    QRectF,
    QTimer,
    QThreadPool,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QScrollBar,
    QWidget,
)

from app import fx as _fx
from app.icons import pixmap as _pixmap
from app.previews import flatten_on_white
from app.theme.tokens import InkfallTokens
from annotations.preview_render import (
    capture_page_annotations,
    render_annotation_overlay,
)
from rendering.process_decoder import ProcessDecoder, ProcessDecoderConfig
from app.toolshelf.surfaces import (
    hover_wash,
    ink_color,
    selected_surface,
    token_color,
)

__all__ = ["PageNavigator", "PagesPanel", "PagesTrigger"]

_LOG = logging.getLogger(__name__)

#: Thumbnail render size (max side). One size serves the filmstrip so
#: there is exactly one cache entry per page per source revision.
_THUMB_MAX_SIDE = 160
#: Bounded memory cache: entry count AND byte budget (explicit, not
#: unlimited). Byte cost is the detached RGBA buffer (w*h*4).
_CACHE_MAX_ENTRIES = 256
_CACHE_MAX_BYTES = 24 * 1024 * 1024
#: Queued (not yet in-flight) request cap; front = next to run.
_QUEUE_MAX = 96
_POOL_THREADS = 2
#: A failed page is retryable after this cooldown (transient I/O or a
#: momentarily locked file must not disable a page forever).
_FAIL_RETRY_S = 4.0
#: Bounded LRU size for appearance-transformed filmstrip thumbnails
#: (C-05). Thumbnails are <=160px on their long side, so 64 entries
#: (the visible window plus lookahead, with room to spare) cost a few
#: MB at most and never grow with the document.
_DISPLAY_CACHE_MAX = 64

_SHADOW = 12
_HEADER_H = 44
_EDGE = 8
_GAP = 14
#: Capsule metrics: the shared [View | Pages] container is two 44px
#: control slots wide (the View trigger owns the left slot; the Pages
#: segment sits at +44 inside the same silhouette).
_CAPSULE_BTN = 44
#: Fixed filmstrip panel metrics. The thumbnail box height targets a
#: ~120-150px preview width for portrait pages (aspect preserved); the
#: live value derives from the view's own height and shrinks on small
#: windows instead of clipping.
_COMPACT_THUMB_H = 176
_COMPACT_NUM_H = 20
_COMPACT_PAD_X = 16
_COMPACT_PAD_Y = 12
#: Uniform cell slot width. Every page occupies the SAME slot so all
#: filmstrip geometry is O(1) arithmetic (no prefix table, no O(pages)
#: rebuild on the GUI thread); each page's thumbnail is drawn
#: aspect-fit INSIDE its slot, so page shapes stay accurate and nothing
#: is ever stretched.
_CELL_W = 136
#: Inner gutter between a slot's edge and its thumbnail.
_CELL_INNER = 4
#: Pointer travel (px) below which a press/release counts as a click
#: rather than a scrub — the touch-drag vs thumbnail-tap boundary.
_CLICK_SLOP = 6
#: Vertical thinning of the scrollbar inside the view.
_BAR_W = 8
#: Lookahead pages requested beyond the visible window on each side.
_LOOKAHEAD = 6

_FACTS_REFRESH_MS = 2000


def _font(tokens: InkfallTokens, px: int, weight: int = 500) -> QFont:
    """Panel font: one bundled family, explicit pixel size."""
    font = QFont()
    try:
        family = str(tokens.font_family).split(",")[0].strip()
        if family:
            font.setFamily(family)
    except Exception:
        pass
    try:
        font.setPixelSize(max(8, int(px)))
        font.setWeight(QFont.Weight(int(weight)))
    except Exception:
        pass
    return font


def _color(tokens: InkfallTokens, name: str) -> QColor:
    return token_color(tokens, name, (180, 186, 199, 255))


def _rev_facts(path: str) -> tuple[int, int]:
    """Source-revision facts for the cache key: (mtime_ns, size).

    One ``os.stat`` per request batch (not per page, not per paint) on
    the GUI thread; a save or page operation that changes the render
    working copy therefore changes every key derived afterwards.
    """
    try:
        st = os.stat(path)
        return int(st.st_mtime_ns), int(st.st_size)
    except Exception:
        return 0, 0


def _display_image(image: QImage, mode: str) -> QImage:
    """Appearance transform for DISPLAY only (cache keeps originals).

    Reuses the viewer's own display-time transform — the exact pixels
    the canvas shows under the same mode. Imported lazily: the viewer
    imports this module, so the import must not be circular at load.
    """
    if mode in (None, "", "light"):
        return image
    try:
        from app.viewer import _appearance_tile

        return _appearance_tile(image, mode)
    except Exception:
        return image


class _OverlayJob(QRunnable):
    """Render one page's annotation snapshot into a transparent overlay.

    Runs on the engine's single overlay worker. Receives ONLY the plain
    snapshot tuple (captured on the GUI thread) and pixel dimensions —
    never the store, the document, widgets, or PDF handles. QImage +
    QPainter are thread-safe off the GUI thread; no QPixmap is created
    here.
    """

    def __init__(self, engine: "PageThumbEngine", okey, snapshot: tuple,
                 width: int, height: int, page_w_pt: float,
                 page_h_pt: float) -> None:
        super().__init__()
        self._engine = engine
        self._okey = okey
        self._snapshot = snapshot
        self._width = int(width)
        self._height = int(height)
        self._page_w = float(page_w_pt)
        self._page_h = float(page_h_pt)

    def run(self) -> None:  # noqa: N802
        try:
            image = render_annotation_overlay(
                self._snapshot, self._width, self._height,
                self._page_w, self._page_h)
        except Exception:
            image = None  # retried by the next request batch, never in paint
        try:
            self._engine._on_overlay_done(self._okey, image)
        except RuntimeError:
            pass  # engine (or app) torn down under us: nothing to do


class PageThumbEngine(QObject):
    """Bounded LRU thumbnail cache + GIL-free subprocess rasterizer.

    Rendering runs in the project's ISOLATED-PROCESS decoder
    (:class:`rendering.process_decoder.ProcessDecoder`, 2 workers): MuPDF
    rasterization holds the GIL, so in-process rasters starve the GUI
    thread (the project's own M3 Finding F-1 — the documented reason the
    subprocess decoder exists). Full-page preview rasters are exactly the
    heavyweight case that mechanism was built for, so the engine submits
    them to decoder child processes (worker-owned PDF handles, GIL-free)
    instead of rasterizing in-process.

    ALL engine bookkeeping (cache, queues, in-flight set) is guarded by
    one lock. Completion arrives on the decoder's collector thread via
    ``future.add_done_callback``; the QImage is constructed and detached
    there (QImage is thread-safe to create and share read-only; only
    QPixmap is GUI-bound), and the ONLY thing that crosses to the GUI
    thread is an ARGUMENT-LESS ``updated`` signal via Qt's queued
    connection — no signal-argument marshaling anywhere on the delivery
    path, and a completed result can never be lost between the worker
    and the repaint.

    ``shutdown()`` owns the deterministic decoder join (feeder/collector/
    workers) and is invoked on application/viewer teardown only — never
    on ordinary open/close/scroll. Drop-before-publish guards make a
    job outliving its document a no-op.
    """

    #: Emitted (from any thread) when the cache contents changed; the
    #: GUI repaints the active view. Deliberately argument-less.
    updated = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._closed = False
        self._proc = ProcessDecoder(ProcessDecoderConfig(
            workers=_POOL_THREADS))
        self._req_counter = 0
        self._req_ids: dict = {}
        self._cache: "OrderedDict[tuple, QImage]" = OrderedDict()
        self._cache_bytes = 0
        self._meta: dict = {}
        self._pending: set = set()
        self._inflight: set = set()
        self._order: list = []
        self._failed_at: dict = {}
        self._dropped_docs: set = set()
        # Annotation overlay cache (transparent previews composited over
        # the base PDF image at paint time), keyed (base_key, ann_rev).
        # Charged to the SAME byte budget as the base cache; overlays
        # evict first — they are cheap local QPainter re-derivations,
        # while bases cost a subprocess render. One dedicated worker
        # keeps overlay rendering serial and off the GUI thread.
        self._overlays: "OrderedDict[tuple, QImage]" = OrderedDict()
        self._overlay_pending: set = set()
        self._overlay_pool = QThreadPool(self)
        self._overlay_pool.setMaxThreadCount(1)
        # Spawn the decoder's worker processes OFF the GUI thread: they
        # would otherwise pay the ~300ms ``spawn`` on the first submit
        # (the thread that opens the panel). Idempotent; no work is
        # submitted.
        threading.Thread(target=self._proc.prewarm, daemon=True,
                         name="thumb-decoder-prewarm").start()

    # -- queries (GUI thread) --------------------------------------------

    def cached(self, key):
        with self._lock:
            return self._cache.get(key)

    def is_failed(self, key) -> bool:
        with self._lock:
            return key in self._failed_at

    # -- annotation overlays (GUI thread scheduling) ----------------------

    def overlay_cached(self, key, rev):
        """The annotation overlay for one base key + annotation revision.

        ``None`` while not yet rendered — the view draws the base PDF
        image alone and never blocks or schedules from paint.
        """
        okey = (key, int(rev))
        with self._lock:
            image = self._overlays.get(okey)
            if image is not None:
                self._overlays.move_to_end(okey)
            return image

    def schedule_overlay(self, key, rev, snapshot: tuple, width: int,
                         height: int, page_w_pt: float,
                         page_h_pt: float) -> None:
        """Render one page's annotation overlay off the GUI thread.

        Bounded by construction: called per visible page from the
        coalesced request path; identical (key, rev) pairs deduplicate
        through the pending set; the single overlay worker serializes
        rendering; empty snapshots are skipped (nothing to composite).
        """
        okey = (key, int(rev))
        if not snapshot:
            return
        with self._lock:
            if (self._closed or okey in self._overlays
                    or okey in self._overlay_pending):
                return
            self._overlay_pending.add(okey)
        self._overlay_pool.start(_OverlayJob(
            self, okey, snapshot, int(width), int(height),
            float(page_w_pt), float(page_h_pt)))

    def _on_overlay_done(self, okey, image) -> None:
        """Overlay finished (worker thread): store + repaint via the
        argument-less queued signal. Closed/dropped states discard."""
        with self._lock:
            self._overlay_pending.discard(okey)
            doc = None
            try:
                doc = okey[0][0]
            except Exception:
                doc = None
            if self._closed or (doc is not None
                                and doc in self._dropped_docs):
                return
            if image is not None and not image.isNull():
                if okey in self._overlays:
                    self._cache_bytes -= self._cost(self._overlays[okey])
                self._overlays[okey] = image
                self._overlays.move_to_end(okey)
                self._cache_bytes += self._cost(image)
                self._evict_locked()
        try:
            self.updated.emit()
        except RuntimeError:
            pass

    # -- scheduling (GUI thread) -----------------------------------------

    def schedule(self, specs) -> None:
        """Request/prioritize/drop in one pass.

        ``specs`` is [(key, path, page_index, scale, full_w, full_h)] in
        priority order (first = most urgent). Cached keys are ignored; failed keys retry after a
        cooldown (transient failures are never permanent); in-flight keys
        are re-prioritized; queued keys outside ``specs`` are DROPPED
        (scroll-out-of-range deprioritization) — in-flight work is never
        cancelled, it simply finishes and is stored (bounded).
        """
        new_keys = []
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            for key, path, page, scale, full_w, full_h in specs:
                if key in self._cache:
                    continue
                failed_at = self._failed_at.get(key)
                if failed_at is not None and (now - failed_at) < _FAIL_RETRY_S:
                    continue
                self._failed_at.pop(key, None)
                if key in self._pending or key in self._inflight:
                    new_keys.append(key)
                    continue
                self._pending.add(key)
                self._meta[key] = (str(path), int(page), float(scale),
                                   int(full_w), int(full_h))
                new_keys.append(key)
            wanted = set(new_keys)
            self._order = ([k for k in new_keys if k in self._pending]
                           + [k for k in self._order
                              if k in self._pending and k not in wanted])
            del self._order[_QUEUE_MAX:]
            keep = set(self._order)
            for key in [k for k in self._pending if k not in keep]:
                self._pending.discard(key)
                self._meta.pop(key, None)
            self._pump_locked()

    def _pump_locked(self) -> None:
        """Submit queued decodes while fewer than the worker limit are
        live. The gate counts OUR OWN in-flight set — accurate by
        construction — and every terminal outcome (success OR failure)
        re-pumps, so the queue always drains. The decoder's internal
        queue therefore never holds more than the two running decodes,
        and stale queued work is dropped at schedule() time, before any
        child process sees it.
        """
        if self._closed:
            return
        while self._order and len(self._inflight) < _POOL_THREADS:
            key = self._order.pop(0)
            if key not in self._pending:
                continue
            self._pending.discard(key)
            self._inflight.add(key)
            path, page, scale, full_w, full_h = self._meta.get(
                key, ("", 0, 1.0, _THUMB_MAX_SIDE, _THUMB_MAX_SIDE))
            self._req_counter += 1
            request_id = f"thumb-{self._req_counter}"
            self._req_ids[key] = request_id
            try:
                future = self._proc.submit(
                    request_id, path, int(page), float(scale), 0,
                    int(full_w), int(full_h), priority_visible=True)
            except Exception:
                # Decoder refused/shut down: release the slot and record
                # the failure (the retry cooldown applies).
                self._inflight.discard(key)
                self._req_ids.pop(key, None)
                self._failed_at[key] = time.monotonic()
                continue
            try:
                future.add_done_callback(
                    lambda fut, k=key: self._on_future_done(fut, k))
            except Exception:
                self._inflight.discard(key)
                self._req_ids.pop(key, None)
                self._failed_at[key] = time.monotonic()

    # -- completion (decoder collector thread, via add_done_callback) ----

    def _on_future_done(self, future, key) -> None:
        """One decode finished (worker side, plain call — no marshaling).

        Builds the detached QImage here on the collector thread (QImage
        is thread-safe to create; painting happens on the GUI thread)
        and hands everything to the shared completion path.
        """
        error = None
        result = None
        try:
            if future.cancelled():
                error = "decode cancelled"
            else:
                exc = future.exception()
                if exc is not None:
                    error = f"{exc.__class__.__name__}: {exc}"
                else:
                    result = future.result()
        except Exception as exc:  # noqa: BLE001 - one line, one failure
            error = f"{exc.__class__.__name__}: {exc}"
        try:
            self._on_job_done(key, result, error)
        except RuntimeError:
            pass  # engine (or app) torn down under us: nothing to do

    def _on_job_done(self, key, result, error: str | None) -> None:
        image = None
        if result is not None:
            rgba, width, height, stride = (result[0], result[1],
                                           result[2], result[3])
            try:
                # Detached copy: the cached image owns its buffer; no
                # worker memory is aliased. QImage (NOT QPixmap) is the
                # thread-safe type; painting happens on the GUI thread.
                image = QImage(rgba, width, height, stride,
                               QImage.Format.Format_RGBA8888).copy()
            except Exception:
                image = None
            if image is not None and image.isNull():
                image = None
            elif image is not None:
                # Shared preview paper: a page without its own painted
                # background must not vanish into the strip's placeholder.
                image = flatten_on_white(image)
        with self._lock:
            self._inflight.discard(key)
            self._meta.pop(key, None)
            doc = key[0] if isinstance(key, tuple) else None
            if doc in self._dropped_docs:
                return  # document closed under the job: never publish
            if self._closed:
                # Engine shut down under the job: release the slot and
                # publish nothing — a late callback may not re-pump,
                # resubmit, or touch the decoder again.
                return
            if image is None:
                self._failed_at[key] = time.monotonic()
                if error is None:
                    error = "render returned no image"
            else:
                self._failed_at.pop(key, None)
                if key in self._cache:
                    self._cache_bytes -= self._cost(self._cache[key])
                self._cache[key] = image
                self._cache.move_to_end(key)
                self._cache_bytes += self._cost(image)
                self._evict_locked()
        if image is None:
            # Concise diagnostic context for genuine rendering failures
            # (one line per failed page; successes never log).
            try:
                page = key[1] if isinstance(key, tuple) else "?"
                _LOG.warning("page preview failed (page %s): %s",
                             page, error or "unknown error")
            except Exception:
                pass
        try:
            self.updated.emit()
        except RuntimeError:
            pass  # engine deleted during teardown: the result is dropped
        # A worker slot freed: keep the queue draining (pool submission
        # is thread-safe; the shared state above is released).
        with self._lock:
            self._pump_locked()

    @staticmethod
    def _cost(image: QImage) -> int:
        try:
            return int(image.width()) * int(image.height()) * 4
        except Exception:
            return 0

    def _evict_locked(self) -> None:
        """Shared budget: bases AND overlays count against 24 MiB, each
        capped at 256 entries. Overlays evict first — they are cheap
        local QPainter re-derivations, while bases cost a subprocess
        render (the explicit accounting change for the annotation
        layer; total memory stays bounded at 24 MiB)."""
        while ((self._cache or self._overlays)
               and (self._cache_bytes > _CACHE_MAX_BYTES
                    or len(self._cache) > _CACHE_MAX_ENTRIES
                    or len(self._overlays) > _CACHE_MAX_ENTRIES)):
            if self._overlays:
                _key, image = self._overlays.popitem(last=False)
            elif self._cache:
                _key, image = self._cache.popitem(last=False)
            else:
                break
            self._cache_bytes -= self._cost(image)

    # -- lifecycle -------------------------------------------------------

    def drop_document(self, doc_value) -> None:
        """Free one document's entries/queues (tab closure, doc switch)."""
        doc_value = str(doc_value)
        with self._lock:
            self._dropped_docs.add(doc_value)
            for key in [k for k in self._cache if k[0] == doc_value]:
                self._cache_bytes -= self._cost(self._cache.pop(key))
            for key in [k for k in self._order if k[0] == doc_value]:
                self._order.remove(key)
                self._pending.discard(key)
                self._meta.pop(key, None)
            for key in [k for k in self._failed_at if k[0] == doc_value]:
                self._failed_at.pop(key, None)
            for okey in [k for k in self._overlays if k[0][0] == doc_value]:
                self._cache_bytes -= self._cost(self._overlays.pop(okey))
            for okey in [k for k in self._overlay_pending
                         if k[0][0] == doc_value]:
                self._overlay_pending.discard(okey)
        self.updated.emit()

    def clear_failures(self) -> None:
        with self._lock:
            self._failed_at.clear()

    def shutdown(self) -> None:
        """Own the decoder's deterministic join (teardown only).

        Called from viewer/application shutdown — never on ordinary
        open/close/scroll: the join covers feeder, collector and worker
        processes, exactly like the render pipeline's own close. The
        engine is marked closed FIRST, so late completion callbacks can
        neither publish, re-pump, nor resubmit; idempotent.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._order = []
            for key in list(self._pending):
                self._meta.pop(key, None)
            self._pending.clear()
            self._overlays.clear()
            self._overlay_pending.clear()
        # Drain queued-but-unstarted overlay jobs (non-blocking, C-12):
        # the pool is parented to this engine so Qt destroys it with the
        # engine object, but without clear() its queued jobs could still
        # run pointlessly during teardown. An already-running job may
        # finish; _on_overlay_done's _closed guard discards its result.
        try:
            self._overlay_pool.clear()
        except Exception:
            pass
        try:
            self._proc.shutdown()
        except Exception:
            pass


class _CapsuleBody(QWidget):
    """Shared background of the [View | Pages] control capsule.

    One rounded silhouette (radius = half the button height) painted
    once — the View trigger's own disc painting is suppressed in segment
    mode and both buttons contribute only their hover/press wash and
    glyph on top. Mouse-transparent: the two buttons keep separate hit
    targets; the body never eats a click.
    """

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self.setFixedSize(_CAPSULE_BTN * 2, _CAPSULE_BTN)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            radius = rect.height() / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(selected_surface(tokens))
            painter.drawRoundedRect(rect, radius, radius)
            rim = _color(tokens, "border")
            rim.setAlpha(255)
            painter.setPen(QPen(rim, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, radius, radius)
        finally:
            try:
                painter.end()
            except Exception:
                pass


class PagesTrigger(QAbstractButton):
    """The Pages segment of the shared View/Pages capsule.

    Same 44px slot as the View trigger; the shared `_CapsuleBody`
    paints the background, so this button contributes only its
    hover/press wash (painted as the full silhouette shifted one slot
    left, so the internal edge is straight), its glyph, and — while the
    navigator is open — a subtle accent wash marking the active
    control. Hit target, focus policy and accessibility are its own.
    """

    SIZE = 44

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._hover = False
        self._accent = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Page navigator")
        self.setAccessibleDescription(
            "Toggle the floating page filmstrip")
        self.setStyleSheet(
            "QAbstractButton { background: transparent; border: none;"
            " outline: 0; }")
        try:
            self.pressed.connect(self.update)
            self.released.connect(self.update)
        except Exception:
            pass

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self.update()

    def set_accent(self, on: bool) -> None:
        on = bool(on)
        if on != self._accent:
            self._accent = on
            self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            down = bool(self.isDown())
            if down or self._hover or self._accent:
                painter.setPen(Qt.PenStyle.NoPen)
                if self._accent:
                    painter.setBrush(_color(tokens, "accent_soft"))
                else:
                    painter.setBrush(hover_wash(tokens, down))
                # Right segment of the shared capsule: the wash is the
                # FULL silhouette in this button's local coordinates
                # (shifted left by one slot) — the widget's own bounds
                # clip the left half, so the outer corners round and the
                # internal edge is straight.
                painter.drawRoundedRect(
                    QRectF(float(-self.SIZE), 0.0,
                           float(self.SIZE * 2), float(self.SIZE)),
                    float(self.SIZE / 2.0), float(self.SIZE / 2.0))
            if self.hasFocus():
                # Ring on the same full-capsule silhouette; the widget's
                # own bounds clip the left half.
                from app.focus import draw_focus_ring

                draw_focus_ring(
                    painter,
                    QRectF(float(-self.SIZE), 0.0,
                           float(self.SIZE * 2), float(self.SIZE)),
                    tokens, radius=float(self.SIZE / 2.0))
            size = 18
            pix = _pixmap("grid", ink_color(tokens).name(), size)
            if not pix.isNull():
                painter.drawPixmap(
                    self.SIZE // 2 - size // 2,
                    self.SIZE // 2 - size // 2, pix)
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _CloseButton(QAbstractButton):
    """Header close affordance (muted ink, hover wash)."""

    def __init__(self, tokens: InkfallTokens, parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._hover = False
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Close page navigator")
        self.setStyleSheet(
            "QAbstractButton { background: transparent; border: none;"
            " outline: 0; }")

    def set_tokens(self, tokens: InkfallTokens) -> None:
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

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            if self.isDown() or self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(hover_wash(tokens, self.isDown()))
                painter.drawRoundedRect(rect, 6.0, 6.0)
            pix = _pixmap("x", _color(tokens, "text_secondary").name(), 14)
            if not pix.isNull():
                painter.drawPixmap(
                    int(rect.center().x()) - 7,
                    int(rect.center().y()) - 7, pix)
        finally:
            try:
                painter.end()
            except Exception:
                pass


class _FilmstripView(QWidget):
    """The virtualized, fixed-height page filmstrip.

    ONE painted widget for the whole document — no widget per page, and
    NO per-page geometry table: every page occupies a uniform slot, so
    slot position, visible range, hit tests, and the scroll extent are
    all O(1) arithmetic (opening, scrolling and resizing never touch
    per-page data on the GUI thread, at any page count). Each page's
    thumbnail is drawn aspect-fit inside its slot from the immutable
    pipeline layout's PAGE dimensions (pt) — read only for VISIBLE
    pages, so zoom-bucket changes never invalidate the strip and page
    shapes stay accurate (nothing is stretched).
    """

    pageActivated = Signal(int)
    # Right-click on a thumbnail: (page index, global pos). Never
    # emitted for empty space — the receiver can trust the target.
    pageContextMenuRequested = Signal(int, QPoint)

    def __init__(self, navigator: "PageNavigator", parent=None) -> None:
        super().__init__(parent)
        self._navigator = navigator
        self._tokens = navigator.tokens
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._state: dict | None = None
        self._scroll = QScrollBar(Qt.Orientation.Horizontal, self)
        self._scroll.setCursor(Qt.CursorShape.ArrowCursor)
        self._scroll.setStyleSheet(self._bar_qss())
        self._scroll.valueChanged.connect(self._on_scrolled)
        self._press_page = None
        self._press_pos = QPoint()
        self._press_offset = 0
        self._dragging = False
        # Bounded LRU of appearance-transformed thumbnails (C-05): the
        # dark/inverted HSL pass re-ran per visible cell per repaint;
        # entries are keyed by the engine's content-revision key + mode,
        # so regeneration, document switch, or mode change can never
        # return stale pixels. Bounded small: thumbnails are <=160px.
        self._display_cache: "OrderedDict[tuple, QImage]" = OrderedDict()
        # Scroll-driven viewport work (thumbnail scheduling) is
        # coalesced: scrollbar valueChanged fires per pixel during a
        # drag, and each request batch stats the source once — the
        # timer collapses bursts into one batch.
        self._request_timer = QTimer(self)
        self._request_timer.setSingleShot(True)
        self._request_timer.setInterval(80)
        self._request_timer.timeout.connect(self._request_window)

    # -- styling ---------------------------------------------------------

    def _bar_qss(self) -> str:
        t = self._tokens
        # NOTE: Qt stylesheets have no `min-length`; a horizontal
        # handle's minimum travel-length constraint is `min-width`.
        return (
            f"QScrollBar:horizontal {{ background: transparent; margin: 0;"
            f" height: {_BAR_W}px; }}"
            f"QScrollBar::handle {{ background:"
            f" {_color(t, 'text_muted').name()};"
            f" border-radius: {_BAR_W // 2}px; min-width: 24px; }}"
            f"QScrollBar::handle:hover {{ background:"
            f" {_color(t, 'text_secondary').name()}; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ height: 0;"
            f" width: 0; }}"
            f"QScrollBar::add-page, QScrollBar::sub-page {{ background:"
            f" transparent; }}")

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._scroll.setStyleSheet(self._bar_qss())
        self.update()

    # -- state -----------------------------------------------------------

    def set_state(self, state: dict | None) -> None:
        self._state = state
        self._rebuild_layout()
        self.update()

    def update_current(self, page: int) -> None:
        """Cheap current-page ring update (no layout rebuild)."""
        if self._state is not None:
            try:
                self._state["current"] = max(0, int(page))
            except Exception:
                pass
        self.update()

    def refresh(self) -> None:
        self.update()

    # -- geometry --------------------------------------------------------

    def _box_h(self) -> int:
        """Live thumbnail box height from the view's own height."""
        avail = max(60, self.height() - _COMPACT_NUM_H - _BAR_W - 4)
        return max(64, min(_COMPACT_THUMB_H, avail))

    def _display_aspect(self, page: int) -> tuple[float, float]:
        """Displayed page shape (intrinsic rotation composed).

        ``width_pt/height_pt`` are the UNROTATED media dimensions; a
        page carrying ``/Rotate 90`` displays with swapped proportions.
        The layout's ``effective_rotation`` is authoritative (intrinsic
        + user rotation), so swap for quarter turns.
        """
        state = self._state
        if state is not None:
            layout = state.get("layout")
            if layout is not None:
                try:
                    entry = layout.entry(int(page))
                    w = float(entry.width_pt)
                    h = float(entry.height_pt)
                    if w <= 0 or h <= 0:
                        return (0.72, 1.0)
                    eff = int(entry.effective_rotation) % 360
                    if eff % 180 == 90:
                        w, h = h, w
                    return (w, h)
                except Exception:
                    pass
        return (0.72, 1.0)

    def _thumb_rect(self, page: int, image: QImage | None = None):
        """Aspect-fit preview rect inside the page's uniform slot.

        ``image`` (when already rendered) is the AUTHORITY on shape: its
        own pixel dimensions are the actual rendered preview's —
        including intrinsic rotation — so the destination rect can never
        stretch it. Without an image, the rotation-composed display
        aspect from the layout shapes the empty placeholder.
        """
        cell = self._cell_rect(page)
        if cell is None:
            return None
        box_h = self._box_h()
        inner_w = _CELL_W - 2 * _CELL_INNER
        if image is not None and not image.isNull():
            w = float(image.width())
            h = float(image.height())
        else:
            w, h = self._display_aspect(page)
        if w <= 0 or h <= 0:
            w, h = float(inner_w), float(box_h)
        scale = min(float(inner_w) / w, float(box_h) / h)
        w = max(1.0, w * scale)
        h = max(1.0, h * scale)
        x = cell.x() + (cell.width() - w) / 2.0
        y = cell.y() + (box_h - h) / 2.0
        return QRect(int(round(x)), int(round(y)),
                     max(1, int(round(w))), max(1, int(round(h))))

    def _rebuild_layout(self) -> None:
        """Recompute the scroll extent — O(1).

        Uniform slots make every geometry answer constant-time
        arithmetic (slot x, visible range, hit tests, total extent), so
        opening, scrolling, and resizing never touch per-page data on
        the GUI thread. Page aspects are read per VISIBLE page only,
        straight from the immutable pipeline layout.
        """
        state = self._state
        count = int((state or {}).get("count") or 0)
        self._total = max(
            0, 2 * _COMPACT_PAD_X
            + max(0, count) * (_CELL_W + _GAP) - _GAP)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        extent = max(1, self.width())
        maximum = max(0, self._total - extent)
        self._scroll.setRange(0, maximum)
        self._scroll.setPageStep(max(1, extent * 9 // 10))
        self._scroll.setSingleStep(48)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._scroll.setGeometry(
            0, max(0, self.height() - _BAR_W - 2),
            max(0, self.width()), _BAR_W)
        self._rebuild_layout()

    def _on_scrolled(self) -> None:
        self.update()
        # Coalesce: one request batch per quiet 80ms, not one per pixel.
        self._request_timer.start()

    # -- cell geometry (all O(1)) ----------------------------------------

    def _slot_x(self, page: int) -> int:
        """Cell slot left edge — O(1) arithmetic, no per-page table."""
        return _COMPACT_PAD_X + page * (_CELL_W + _GAP)

    def _cell_rect(self, page: int) -> QRect | None:
        state = self._state
        if not state:
            return None
        count = int(state.get("count") or 0)
        if not 0 <= page < count:
            return None
        return QRect(self._slot_x(page), _COMPACT_PAD_Y // 2,
                     _CELL_W, self._box_h())

    def _first_visible(self, offset: int) -> int:
        """First page whose slot may intersect the viewport — O(1)."""
        state = self._state
        count = int((state or {}).get("count") or 0)
        if count <= 0:
            return 0
        index = (offset - _COMPACT_PAD_X) // (_CELL_W + _GAP)
        return max(0, min(int(index), count - 1))

    def _visible_pages(self) -> list:
        state = self._state
        if not state:
            return []
        count = int(state.get("count") or 0)
        offset = int(self._scroll.value())
        first = self._first_visible(offset - _CELL_W)
        span = max(1, self.width()) // (_CELL_W + _GAP) + 3
        return list(range(first, min(count, first + span)))

    # -- requests --------------------------------------------------------

    def _request_window(self) -> None:
        state = self._state
        if not state:
            return
        count = int(state.get("count") or 0)
        if count <= 0:
            return
        current = max(0, min(int(state.get("current") or 0), count - 1))
        ordered = [current]
        for delta in (1, -1, 2, -2):
            p = current + delta
            if 0 <= p < count and p not in ordered:
                ordered.append(p)
        visible = self._visible_pages()
        for page in visible:
            if page not in ordered:
                ordered.append(page)
        for page in (visible[:_LOOKAHEAD] + visible[-_LOOKAHEAD:]):
            for delta in (1, 2, 3):
                p = page + delta
                if 0 <= p < count and p not in ordered:
                    ordered.append(p)
        self._navigator.request_thumbs(ordered)
        # Annotation overlays ride the same coalesced batch: one
        # bounded capture per annotated visible page.
        self._navigator.request_overlays(ordered)

    def reveal(self, page: int) -> None:
        """Scroll the strip so ``page``'s slot sits comfortably in view."""
        cell = self._cell_rect(int(page))
        if cell is None:
            return
        extent = max(1, self.width())
        center = cell.x() + cell.width() // 2
        value = center - extent // 2
        self._scroll.setValue(max(self._scroll.minimum(),
                                  min(self._scroll.maximum(), value)))
        self._request_window()

    def _display_cached(self, key, image: QImage, mode: str) -> QImage:
        """LRU-cached appearance transform (C-05).

        ``key`` is the engine's content-revision key — the SAME key the
        base image was fetched under — so the pair (key, mode) uniquely
        identifies (source pixels, transform). A regenerated thumbnail
        (new mtime/size/rotation facts), a document switch, or an
        appearance change therefore lands on a different key and the old
        entry is never read again; the bounded LRU sheds it. Light mode
        and key-less lookups bypass the cache entirely (the original is
        returned as-is, no reference stored).
        """
        if mode in (None, "", "light") or key is None:
            return _display_image(image, mode)
        ck = (key, mode)
        cached = self._display_cache.get(ck)
        if cached is not None:
            self._display_cache.move_to_end(ck)
            return cached
        shown = _display_image(image, mode)
        self._display_cache[ck] = shown
        while len(self._display_cache) > _DISPLAY_CACHE_MAX:
            self._display_cache.popitem(last=False)
        return shown

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            state = self._state
            if not state:
                return
            tokens = self._tokens
            offset = int(self._scroll.value())
            box_h = self._box_h()
            current = int(state.get("current") or -1)
            engine = self._navigator.engine
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, True)
            for page in self._visible_pages():
                cell = self._cell_rect(page)
                if cell is None:
                    continue
                key = self._navigator.key_for(page)
                image = engine.cached(key) if key is not None else None
                # The preview's OWN pixel dimensions are the authority on
                # shape (they include intrinsic rotation); the
                # rotation-composed display aspect shapes the placeholder
                # until the image arrives. Either way the rect is
                # aspect-fit and centered in the slot — never stretched.
                thumb = self._thumb_rect(
                    page, image if (image is not None
                                    and not image.isNull()) else None)
                if thumb is None:
                    continue
                box = thumb.translated(-offset, 0)
                radius = 6.0
                # Page-shaped placeholder (also the render-failure
                # fallback: quiet, never an error surface).
                painter.setPen(QPen(_color(tokens, "thumb_edge"), 1.0))
                painter.setBrush(_color(tokens, "thumb_placeholder"))
                painter.drawRoundedRect(QRectF(box), radius, radius)
                if image is not None and not image.isNull():
                    mode = state.get("appearance") or "light"
                    shown = self._display_cached(key, image, mode)
                    painter.drawImage(box, shown)
                    # Inkfall's annotation layer: a transparent overlay
                    # rendered from the page's live annotation snapshot,
                    # composited above the PDF content at the SAME rect
                    # (identical pixel dimensions). Lookup is O(1); a
                    # missing overlay leaves the bare PDF preview
                    # visible until its render lands — paint never
                    # schedules or waits.
                    store = state.get("annotations")
                    if store is not None and key is not None:
                        try:
                            overlay = engine.overlay_cached(
                                key, int(store.revision(page)))
                        except Exception:
                            overlay = None
                        if overlay is not None and not overlay.isNull():
                            painter.drawImage(box, overlay)
                # Current page: the one interaction accent.
                if page == current:
                    painter.setPen(QPen(_color(tokens, "accent"), 2.0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(
                        QRectF(box).adjusted(-1.5, -1.5, 1.5, 1.5),
                        radius + 1.0, radius + 1.0)
                # 1-based page number under the cell.
                num_rect = QRect(cell.x() - offset, cell.y() + box_h + 4,
                                 cell.width(), _COMPACT_NUM_H)
                painter.setFont(_font(tokens, 11, 500))
                painter.setPen(QPen(
                    _color(tokens, "text_primary" if page == current
                           else "text_secondary"), 1.0))
                painter.drawText(num_rect,
                                 Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignVCenter,
                                 str(page + 1))
        finally:
            try:
                painter.end()
            except Exception:
                pass

    # -- input -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                pos = event.position().toPoint()
                self._press_page = self._page_at(pos)
                self._press_pos = QPoint(pos)
                self._press_offset = int(self._scroll.value())
                self._dragging = False
                event.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        try:
            if (event.buttons() & Qt.MouseButton.LeftButton
                    and not self._press_pos.isNull()):
                pos = event.position().toPoint()
                delta = self._press_pos.x() - pos.x()
                # A press that travels is a SCRUB (touch pan / mouse
                # drag-scroll), never a thumbnail click: the panel stays
                # open and the strip follows the pointer.
                if not self._dragging and abs(delta) > _CLICK_SLOP:
                    self._dragging = True
                if self._dragging:
                    self._scroll.setValue(self._press_offset + int(delta))
                    event.accept()
                    return
        except Exception:
            pass
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                # Only a genuine click (no drag travel, same cell)
                # activates a page — a touch-drag scroll must never
                # navigate or close the panel.
                if not self._dragging and self._press_page is not None:
                    page = self._page_at(event.position().toPoint())
                    if page is not None and page == self._press_page:
                        self.pageActivated.emit(int(page))
                self._press_page = None
                self._press_pos = QPoint()
                self._dragging = False
                event.accept()
                return
        except Exception:
            pass
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        try:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Ctrl+wheel is the canvas zoom gesture (UI-05): never
                # scroll the strip; ignore so Qt propagates the event up
                # to the viewer, which owns zooming.
                super().wheelEvent(event)
                return
            delta = event.angleDelta()
            steps = delta.y() if delta.y() != 0 else delta.x()
            if steps:
                bar = self._scroll
                bar.setValue(bar.value() - int(steps))
                event.accept()
                return
        except Exception:
            pass
        super().wheelEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """Right-click a thumbnail: request that page's context menu.

        The target is resolved with this strip's own O(1) hit test, so
        the menu always belongs to the page under the pointer — never
        the current selection. Empty space (gaps, past the cells, below
        the number band) opens nothing: ``_page_at`` returns None and
        the event is ignored, exactly like the canvas context menu.
        Right-click never activates the page (press/release are
        LeftButton-only) and never starts a drag-scrub.
        """
        try:
            page = self._page_at(event.position().toPoint())
        except Exception:
            page = None
        if page is None:
            event.ignore()
            return
        event.accept()
        self.pageContextMenuRequested.emit(int(page),
                                           event.globalPosition().toPoint())

    def _page_at(self, pos: QPoint):
        state = self._state
        if not state:
            return None
        count = int(state.get("count") or 0)
        if count <= 0:
            return None
        box_h = self._box_h()
        if pos.y() > _COMPACT_PAD_Y // 2 + box_h + 4 + _COMPACT_NUM_H:
            return None
        offset = int(self._scroll.value())
        index = (pos.x() + offset - _COMPACT_PAD_X) // (_CELL_W + _GAP)
        page = int(index)
        if not 0 <= page < count:
            return None
        # Landed in the gap between slots (or past the slot's right
        # edge): not a cell hit.
        if pos.x() + offset >= self._slot_x(page) + _CELL_W:
            return None
        return page


class PagesPanel(QWidget):
    """The fixed-size floating filmstrip panel.

    Self-painted floating surface — the ViewControlsPanel recipe
    (translucent widget, manual layered soft shadow, modal radius,
    ``surface_raised`` body, one hairline border). Fixed height by
    design: no drag handle, no resize gesture, no expanded mode — the
    header carries the Pages title, the page count, and the close
    button; the thumbnail row scrolls horizontally below it. A click
    anywhere on the panel is chrome input, never a document click.
    """

    dismissRequested = Signal()
    pageActivated = Signal(int)
    pageContextMenuRequested = Signal(int, QPoint)

    _SHADOW = _SHADOW

    def __init__(self, tokens: InkfallTokens, navigator: "PageNavigator",
                 parent=None) -> None:
        super().__init__(parent)
        self._tokens = tokens
        self._navigator = navigator
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._title = "Pages"
        self._count = 0
        self._close = _CloseButton(tokens, self)
        self._close.clicked.connect(self.dismissRequested.emit)
        self._strip = _FilmstripView(navigator, self)
        self._strip.pageActivated.connect(self.pageActivated.emit)
        self._strip.pageContextMenuRequested.connect(
            self.pageContextMenuRequested.emit)
        self._state: dict | None = None

    # -- state -----------------------------------------------------------

    def set_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._close.set_tokens(tokens)
        self._strip.set_tokens(tokens)
        self.update()

    def set_state(self, state: dict | None) -> None:
        self._state = state
        self._count = int((state or {}).get("count") or 0)
        self._strip.set_state(state)
        self.update()

    def current_page(self) -> int:
        return int((self._state or {}).get("current") or 0)

    def refresh(self) -> None:
        """Thumbnails landed: repaint the strip (it re-requests)."""
        self._strip.refresh()

    def update_current(self, page: int) -> None:
        """Cheap current-page ring update (no layout rebuild)."""
        if self._state is not None:
            try:
                self._state["current"] = max(0, int(page))
            except Exception:
                pass
        self._strip.update_current(int(page))

    def reveal(self, page: int) -> None:
        self._strip.reveal(int(page))

    def request_window(self) -> None:
        """Schedule the visible-window thumbnails (open-time entry)."""
        self._strip._request_window()

    # -- geometry --------------------------------------------------------

    def body_rect(self) -> QRect:
        return self.rect().adjusted(_SHADOW, _SHADOW,
                                    -_SHADOW, -_SHADOW)

    def _header_rect(self) -> QRect:
        body = self.body_rect()
        return QRect(body.x(), body.y(), body.width(), _HEADER_H)

    def _view_rect(self) -> QRect:
        """Thumbnail row: comfortable 16px side / vertical padding."""
        body = self.body_rect()
        return QRect(body.x() + _COMPACT_PAD_X,
                     body.y() + _HEADER_H + 2,
                     max(0, body.width() - 2 * _COMPACT_PAD_X),
                     max(0, body.height() - _HEADER_H - 2
                         - _COMPACT_PAD_Y))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        header = self._header_rect()
        self._close.move(header.right() - 32, header.y()
                          + max(0, (header.height() - 28) // 2))
        self._close.raise_()
        self._strip.setGeometry(self._view_rect())
        self._strip.raise_()

    # -- painting --------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            tokens = self._tokens
            body = QRectF(self.body_rect())
            radius = float(getattr(tokens, "modal_radius", 14))
            # Manual layered soft shadow (no QGraphicsDropShadowEffect,
            # which would force a square backing on the translucent
            # overlay) — the ViewControlsPanel/tool-popup recipe.
            painter.setPen(Qt.PenStyle.NoPen)
            for i, alpha in ((6, 40), (4, 48), (2, 56)):
                shadow = QColor(0, 0, 0, alpha)
                painter.setBrush(shadow)
                painter.drawRoundedRect(
                    body.adjusted(-i, -i + 2, i, i + 2),
                    radius + i, radius + i)
            painter.setBrush(_color(tokens, "surface_raised"))
            painter.setPen(QPen(_color(tokens, "border"), 1.0))
            painter.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5),
                                    radius, radius)
            # Compact header: title + count (no drag handle).
            header = self._header_rect()
            painter.setFont(_font(tokens, 13, 600))
            painter.setPen(QPen(_color(tokens, "text_primary"), 1.0))
            painter.drawText(
                QRect(header.x() + 16, header.y() + 6,
                      header.width() - 120, 20),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._title)
            painter.setFont(_font(tokens, 11, 500))
            painter.setPen(QPen(_color(tokens, "text_muted"), 1.0))
            count_text = (f"{self._count} pages" if self._count != 1
                          else "1 page")
            painter.drawText(
                QRect(header.x() + 16, header.y() + 23,
                      header.width() - 120, 14),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                count_text)
        finally:
            try:
                painter.end()
            except Exception:
                pass

    # -- input -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # A click anywhere on the panel is chrome input, never a
        # document click. Child widgets (strip, close button) receive
        # their own presses first.
        event.accept()

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Wheel over the panel body (header, count text, shadow margins)
        # is chrome input (UI-05): it must never scroll or zoom the PDF
        # underneath, so plain wheel is consumed here. Ctrl+wheel is the
        # canvas zoom gesture and is ignored so Qt propagates it to the
        # viewer. The strip's own wheel handler runs first for events
        # over the filmstrip; this only covers the rest of the surface.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        try:
            if event.key() == Qt.Key.Key_Escape:
                self.dismissRequested.emit()
                event.accept()
                return
        except Exception:
            pass
        super().keyPressEvent(event)


class PageNavigator(QObject):
    """Controller: Pages trigger + fixed filmstrip panel + thumb engine.

    Modeled on :class:`~app.view_controls.ViewControls`: the two surfaces
    are plain children of the host viewer (overlay pattern — they
    survive fullscreen and never alter document layout), the controller
    is a QObject, and an application-level event filter installed while
    the panel is open handles Escape and outside presses.

    Outside-press contract (geometric ownership boundary): a press whose
    global point lands on the visible panel or capsule belongs to the
    navigator and is never reinterpreted; a press on the PDF CANVAS
    consumes the press (and its release) so the dismissal click can
    never become an inking gesture; a press on ANY application chrome
    (title bar, tabs, window controls, shelf, HUD, scrollbars, View
    controls, menus) dismisses the panel WITHOUT consuming the event.
    """

    pageSelected = Signal(int)
    # Right-click on an All Pages thumbnail: (page index, global pos).
    # Only ever emitted for a real page under the pointer.
    pageContextMenuRequested = Signal(int, QPoint)

    def __init__(self, host: QWidget, tokens: InkfallTokens,
                 geometry_provider=None, origin_provider=None,
                 chrome_provider=None, capsule_host=None,
                 parent=None) -> None:
        super().__init__(parent or host)
        self._host = host
        self._tokens = tokens
        self._geometry = geometry_provider
        self._origin = origin_provider
        self._chrome = chrome_provider
        # The View trigger: the capsule's LEFT segment and the single
        # source of truth for the shared silhouette's position. The body
        # and the Pages segment are seated FROM ITS LIVE GEOMETRY on
        # every reposition, so painting and hit targets can never drift
        # apart.
        self._capsule_host = capsule_host
        self._available = False
        self._open = False
        self._swallow_release = False
        self._filter_installed = False
        self._snapshot: dict | None = None
        self._current = 0
        self._appearance = "light"
        self._facts: tuple | None = None
        self._facts_path: str | None = None
        self._engine = PageThumbEngine(self)
        self._trigger = PagesTrigger(tokens, host)
        self._trigger.clicked.connect(self.toggle)
        # Shared [View | Pages] capsule background: one silhouette behind
        # both control slots, parented to the host viewer and seated at
        # the View trigger's position by reposition() (kept at the bottom
        # of the child stack so both buttons paint above it).
        self._capsule_body = _CapsuleBody(tokens, host)
        self._capsule_body.lower()
        self._panel = PagesPanel(tokens, self, host)
        self._panel.dismissRequested.connect(
            lambda: self.close_panel(animate=True))
        self._panel.pageActivated.connect(self._on_page_activated)
        self._panel.pageContextMenuRequested.connect(
            self.pageContextMenuRequested.emit)
        self._engine.updated.connect(self._panel.refresh)
        # A QWidget child is VISIBLE BY DEFAULT: without this the panel
        # sits at its default top-left geometry the moment the viewer is
        # shown. The panel only ever appears through open_panel().
        self._panel.hide()
        self._facts_timer = QTimer(self)
        self._facts_timer.setInterval(_FACTS_REFRESH_MS)
        self._facts_timer.timeout.connect(self._expire_facts)

    # -- surfaces --------------------------------------------------------

    @property
    def trigger(self) -> PagesTrigger:
        return self._trigger

    @property
    def capsule_body(self) -> _CapsuleBody:
        return self._capsule_body

    @property
    def panel(self) -> PagesPanel:
        return self._panel

    @property
    def engine(self) -> PageThumbEngine:
        return self._engine

    @property
    def tokens(self) -> InkfallTokens:
        return self._tokens

    @property
    def is_open(self) -> bool:
        return self._open

    # -- availability / theme -------------------------------------------

    def set_available(self, available: bool) -> None:
        """Show the trigger only for a live document session."""
        self._available = bool(available)
        if not self._available:
            self.close_panel(animate=False)
        self._trigger.setVisible(self._available)
        if self._available:
            self.reposition()

    def update_tokens(self, tokens: InkfallTokens) -> None:
        self._tokens = tokens
        self._trigger.set_tokens(tokens)
        self._capsule_body.set_tokens(tokens)
        self._panel.set_tokens(tokens)

    # -- state -----------------------------------------------------------

    def sync_state(self, current: int | None = None) -> None:
        """Pull the page-geometry snapshot and push it into the panel."""
        provider = self._geometry
        snapshot = None
        if provider is not None:
            try:
                snapshot = provider()
            except Exception:
                snapshot = None
        if snapshot is None:
            self._snapshot = None
            self._panel.set_state(None)
            return
        if current is not None:
            self._current = max(0, int(current))
        self._snapshot = snapshot
        path = str(snapshot.get("path") or "")
        if path != self._facts_path:
            # New render working copy (document switch, page operation,
            # rebind): refresh revision facts so keys cannot collide
            # across source generations.
            self._facts_path = path
            self._facts = _rev_facts(path) if path else None
        self._push_state()

    def _push_state(self) -> None:
        snapshot = self._snapshot
        if not snapshot:
            self._last_push = None
            self._panel.set_state(None)
            return
        # A new snapshot OBJECT means a new pipeline generation (page
        # geometry changed) — a full state push rebuilds the strip's
        # prefix table. A same-generation sync only moved the current
        # page: a cheap ring update, so per-scroll-frame syncs stay
        # O(1) on huge documents.
        generation = (id(snapshot), self._appearance)
        generation_changed = generation != getattr(self, "_last_push", None)
        self._last_push = generation
        if generation_changed:
            state = dict(snapshot)
            state["current"] = self._current
            state["appearance"] = self._appearance
            self._panel.set_state(state)
        else:
            self._panel.update_current(self._current)

    def set_appearance(self, mode: str) -> None:
        mode = str(mode or "light")
        if mode == self._appearance:
            return
        self._appearance = mode
        self._last_push = None
        self._push_state()

    def _expire_facts(self) -> None:
        """Periodic refresh while open (same-path saves + annotations).

        Also runs one bounded visible-window request pass: store
        revisions bumped by stroke/erase/undo commits (while the panel
        is open and idle) re-schedule only the affected overlays here —
        a few revision reads plus overlay lookups when nothing changed,
        never a document scan.
        """
        if not self._open or not self._snapshot:
            return
        path = str(self._snapshot.get("path") or "")
        if path:
            self._facts = _rev_facts(path)
            self._push_state()
        try:
            self._panel.request_window()
        except Exception:
            pass

    def key_for(self, page: int):
        """Cache key for one page under the CURRENT source revision."""
        snapshot = self._snapshot
        if not snapshot:
            return None
        facts = self._facts
        if facts is None:
            # Lazy revision resolution: a paint must never fall back to a
            # placeholder just because a request batch has not run yet.
            path = str(snapshot.get("path") or "")
            if not path:
                return None
            facts = self._facts = _rev_facts(path)
            self._facts_path = path
        return (str(snapshot.get("doc")), int(page), _THUMB_MAX_SIDE,
                int(snapshot.get("rotation") or 0),
                str(snapshot.get("path") or ""),
                facts[0], facts[1])

    def _page_render_geometry(self, page: int):
        """Render geometry for one page from the pipeline layout.

        Returns ``(scale, full_w, full_h)``: the uniform scale that fits
        the page's DISPLAYED shape (intrinsic rotation composed —
        ``effective_rotation`` is layout-authoritative) into the
        thumbnail box, plus the full-page pixel dimensions at that
        scale. These are exactly the dimensions the decoder child
        renders, so the delivered image aspect matches the page.

        Returns ``None`` when the geometry cannot be resolved (no
        snapshot / bad index) — the caller skips that page instead of
        submitting a blind request.
        """
        snapshot = self._snapshot
        if not snapshot:
            return None
        layout = snapshot.get("layout")
        if layout is None:
            return None
        try:
            entry = layout.entry(int(page))
            w = float(entry.width_pt)
            h = float(entry.height_pt)
            if w <= 0 or h <= 0:
                return None
            eff = int(entry.effective_rotation) % 360
            if eff % 180 == 90:
                w, h = h, w
            scale = min(
                _THUMB_MAX_SIDE / w, _THUMB_MAX_SIDE / h, 2.0)
            if scale <= 0:
                return None
            # Displayed page size in points (rotation composed) — the
            # annotation overlay's page-pt → pixel factor source.
            return (scale, int(round(w * scale)), int(round(h * scale)),
                    w, h)
        except Exception:
            return None

    def request_thumbs(self, ordered_pages) -> None:
        """Priority-ordered request batch (one stat per batch, never per
        page)."""
        snapshot = self._snapshot
        if not snapshot:
            return
        path = str(snapshot.get("path") or "")
        if not path:
            return
        self._facts = _rev_facts(path)
        self._facts_path = path
        specs = []
        for page in ordered_pages:
            key = self.key_for(int(page))
            if key is None:
                continue
            geometry = self._page_render_geometry(int(page))
            if geometry is None:
                continue
            scale, full_w, full_h = geometry[0], geometry[1], geometry[2]
            specs.append((key, path, int(page), scale, full_w, full_h))
        if specs:
            self._engine.schedule(specs)

    def request_overlays(self, ordered_pages) -> None:
        """Schedule annotation-overlay renders for the visible window.

        Runs on the GUI thread: reads the store's per-page revision and
        captures ONE page's plain-data snapshot per overlay request
        (bounded — visible + lookahead pages only, and only for pages
        that actually carry annotations). The snapshot is immutable and
        handed to the engine's single overlay worker; nothing
        document-owned crosses a thread.
        """
        snapshot = self._snapshot
        if not snapshot:
            return
        store = snapshot.get("annotations")
        if store is None:
            return
        for page in ordered_pages:
            key = self.key_for(int(page))
            if key is None:
                continue
            geometry = self._page_render_geometry(int(page))
            if geometry is None:
                continue
            _scale, full_w, full_h, page_w_pt, page_h_pt = geometry
            try:
                rev = int(store.revision(int(page)))
            except Exception:
                continue
            already = self._engine.overlay_cached(key, rev)
            if already is not None:
                continue
            captured = capture_page_annotations(store, int(page))
            if not captured:
                continue  # nothing Inkfall-drawn on this page
            self._engine.schedule_overlay(
                key, rev, captured, full_w, full_h, page_w_pt, page_h_pt)

    def drop_document(self, doc_value) -> None:
        """One document closed: free its thumbnails and queues."""
        self._engine.drop_document(doc_value)
        if self._snapshot and str(self._snapshot.get("doc")) == str(doc_value):
            self._snapshot = None
            self._panel.set_state(None)

    def shutdown(self) -> None:
        """Tear the navigator down for application exit (idempotent).

        Called from the viewer's application-exit path — after the
        unsaved-work guard has passed. Closes the panel, marks the
        engine closed (no further requests accepted, no late callback
        may re-pump or restart the decoder) and joins the decoder.
        Repeated or concurrent calls are no-ops.
        """
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self._facts_timer.stop()
        self.close_panel(animate=False)
        self._install_filter(False)
        self._engine.shutdown()

    # -- geometry --------------------------------------------------------

    def reposition(self) -> None:
        """Re-seat the capsule and the open panel on live geometry.

        The View trigger keeps its existing row anchor (the viewer's row
        math); the Pages segment sits at +44 inside the SAME silhouette
        and the shared body spans both slots. The capsule position is
        read from the View trigger's LIVE geometry — one source of
        truth, so paint and hit targets always agree.
        """
        origin = None
        if self._capsule_host is not None:
            try:
                g = self._capsule_host.geometry()
                origin = QPoint(int(g.x()), int(g.y()))
            except Exception:
                origin = None
        if origin is None and self._origin is not None:
            try:
                origin = self._origin()
            except Exception:
                origin = None
        if origin is not None:
            self._capsule_body.move(origin)
            self._capsule_body.lower()
            self._trigger.move(origin.x() + _CAPSULE_BTN, origin.y())
        self._trigger.raise_()
        if self._open:
            self._panel.setGeometry(self._compact_rect())
            self._panel.raise_()

    def _compact_rect(self) -> QRect:
        """Fixed-size filmstrip: the full viewing area minus a
        deliberate, symmetric 10px margin on each side, centered on the
        VIEWER rectangle, floating immediately above the control
        capsule (never a screen or top-level coordinate). On short
        viewers the height yields so the silhouette never covers the
        capsule row or reaches the title/tab row (the viewer widget
        already lives below it).
        """
        host = self._host
        origin = None
        if self._origin is not None:
            try:
                origin = self._origin()
            except Exception:
                origin = None
        if origin is None:
            origin = QPoint(host.width() - _CAPSULE_BTN * 2 - _EDGE,
                            host.height() - _CAPSULE_BTN - _EDGE)
        margin = 10
        width = max(320, host.width() - 2 * margin)
        # Fixed panel height: shadow + header + row padding + scrollbar
        # + number band + the full thumbnail box target.
        chrome_h = (_SHADOW * 2 + _HEADER_H + 2 + _COMPACT_PAD_Y + _BAR_W
                    + _COMPACT_NUM_H + 4)
        avail_h = max(160, origin.y() - _EDGE - _GAP)
        height = min(chrome_h + _COMPACT_THUMB_H, max(200, avail_h))
        x = max(_EDGE, (host.width() - width) // 2)
        y = origin.y() - _GAP - height + _SHADOW
        y = max(_EDGE, min(host.height() - height - _EDGE, y))
        return QRect(x, y, width, height)

    # -- open / close ----------------------------------------------------

    def _stop_panel_animations(self) -> None:
        """Kill every in-flight presentation animation on the panel.

        Guards the close→reopen race (the close's fade-out hide firing
        after a reopen, hiding an ``_open`` panel): both transitions
        start from a clean, fully-opaque presentation.
        """
        try:
            from PySide6.QtCore import QAbstractAnimation

            for anim in self._panel.findChildren(QAbstractAnimation):
                try:
                    anim.stop()
                except Exception:
                    pass
                try:
                    anim.deleteLater()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._panel.setGraphicsEffect(None)
        except Exception:
            pass

    def open_panel(self) -> None:
        if not self._available or self._open:
            return
        self._stop_panel_animations()
        self.sync_state()
        self._open = True
        self._panel.setGeometry(self._compact_rect())
        self._panel.show()
        self._panel.raise_()
        self._trigger.set_accent(True)
        self._trigger.raise_()
        try:
            _fx.slide_fade_in(self._panel, 0, 160, self._host, dy_px=6)
        except Exception:
            pass
        self._install_filter(True)
        self._facts_timer.start()
        self.reveal_current()
        self._panel.request_window()
        self._panel.refresh()
        # Re-assert the intended geometry after the open animation: the
        # transition never owns the rectangle.
        QTimer.singleShot(200, self._reassert_geometry)

    def _reassert_geometry(self) -> None:
        if self._open:
            self._panel.setGeometry(self._compact_rect())
            self._panel._relayout()

    def close_panel(self, animate: bool = True) -> None:
        if not self._open:
            return
        self._open = False
        self._facts_timer.stop()
        self._install_filter(False)
        self._trigger.set_accent(False)
        # Full dismissal-state cleanup: no stale swallow flag may
        # survive into the next open (or reach the canvas).
        self._swallow_release = False
        if animate:
            self._stop_panel_animations()
            try:
                _fx.slide_fade_out(self._panel, 140, self._host, hide=True)
            except Exception:
                self._panel.hide()
        else:
            self._panel.hide()

    def toggle(self) -> None:
        if self._open:
            self.close_panel(animate=True)
        else:
            self.open_panel()

    def reveal_current(self) -> None:
        self._panel.reveal(self._panel.current_page())
        self._panel.refresh()

    def _on_page_activated(self, page: int) -> None:
        """Thumbnail click: navigate WITHOUT closing the panel.

        The strip stays open so pages can be clicked through
        continuously. Highlight + scroll follow through the viewer's
        goto_page → sync_state/reveal_current path, which keeps the
        selected thumbnail visible and schedules its base/overlay
        renders. Dismissal remains: Pages toggle, Escape, the close
        button, and outside clicks.
        """
        self.pageSelected.emit(int(page))

    # -- dismissal filter -------------------------------------------------

    def _install_filter(self, install: bool) -> None:
        try:
            app = QApplication.instance()
            if app is None:
                return
            if install and not self._filter_installed:
                app.installEventFilter(self)
                self._filter_installed = True
            elif not install and self._filter_installed:
                app.removeEventFilter(self)
                self._filter_installed = False
        except Exception:
            pass

    def _in_panel_tree(self, widget) -> bool:
        node = widget
        while node is not None:
            if (node is self._panel or node is self._trigger
                    or node is self._capsule_body):
                return True
            try:
                node = node.parentWidget()
            except Exception:
                return False
        return False

    def _on_navigator_surface(self, global_pos) -> bool:
        """Geometric ownership boundary (widget-walk independent).

        A press whose GLOBAL point lands on the visible panel or either
        capsule segment belongs to the navigator even if the watched
        widget's ancestor walk misresolves. This is the primary
        containment test; the tree walk is the fallback.
        """
        try:
            local = self._host.mapFromGlobal(global_pos)
        except Exception:
            return False
        try:
            if (self._panel.isVisible()
                    and self._panel.geometry().contains(local)):
                return True
        except Exception:
            pass
        for surface in (self._trigger, self._capsule_body):
            try:
                if (surface.isVisible()
                        and surface.geometry().contains(local)):
                    return True
            except Exception:
                continue
        return False

    def panel_covers_viewer_point(self, x: float, y: float) -> bool:
        """True when a viewer-local point is on the open panel surface.

        The panel is a viewer child, so viewer-local coordinates (the
        stylus dispatcher's space) compare directly against its
        geometry. The viewer's TabletPress path uses this: a tap inside
        the panel is chrome input and must never run the outside-press
        dismissal (which would both dismiss the navigator and fall
        through to the ink router).
        """
        try:
            return bool(self._panel.isVisible()
                        and self._panel.geometry().contains(
                            QPoint(int(x), int(y))))
        except Exception:
            return False

    def _is_canvas(self, widget) -> bool:
        """True when ``widget`` is the PDF canvas (or an inking surface).

        Chrome widgets (shelf, undo HUD, scrollbars, View controls, this
        navigator, the tool popup) are excluded, so their presses pass
        through unconsumed; everything that descends from the viewer
        without being chrome is the canvas.
        """
        chrome = []
        if self._chrome is not None:
            try:
                chrome = list(self._chrome() or ())
            except Exception:
                chrome = []
        chrome_ids = {id(w) for w in chrome if w is not None}
        node = widget
        while node is not None:
            if id(node) in chrome_ids or node is self._panel \
                    or node is self._trigger:
                return False
            if node is self._host:
                return True
            try:
                node = node.parentWidget()
            except Exception:
                return False
        return False

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        try:
            if not self._open:
                return False
            etype = event.type()
            if etype == QEvent.Type.KeyPress \
                    and event.key() == Qt.Key.Key_Escape:
                # Modal precedence (CenteredModal / shortcuts convention):
                # while a native modal dialog is up, its own key handling
                # owns Escape — the navigator must not consume it, or the
                # first Escape would close the panel instead of the dialog.
                if QApplication.activeModalWidget() is not None:
                    return False
                self.close_panel(animate=True)
                return True
            if etype in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                         QEvent.Type.TouchEnd, QEvent.Type.Wheel):
                # Raw touch streams and wheel events are NEVER outside
                # presses: synthesized mouse presses carry the dismissal
                # logic; interfering here would break touch scrolling.
                return False
            if etype == QEvent.Type.MouseButtonRelease \
                    and self._swallow_release:
                # The matching half of a consumed canvas dismissal press.
                self._swallow_release = False
                return True
            if etype == QEvent.Type.MouseButtonPress:
                try:
                    gpos = event.globalPosition().toPoint()
                except Exception:
                    gpos = None
                # OWNERSHIP BOUNDARY FIRST: geometry containment (with
                # the widget-tree walk as fallback). A press on the
                # navigator's visible surface is navigator input — it is
                # never reinterpreted as an outside click, and it is
                # never forwarded to the canvas.
                if self._in_panel_tree(obj) or (
                        gpos is not None
                        and self._on_navigator_surface(gpos)):
                    return False
                if self._is_canvas(obj):
                    # Consume: a dismissal click on the canvas must never
                    # start a stroke or annotation gesture.
                    self._swallow_release = True
                    self.close_panel(animate=True)
                    return True
                # Application chrome (title bar, tabs, window controls,
                # shelf, HUD, menus, other windows): dismiss without
                # consuming — the chrome gets its normal click.
                self.close_panel(animate=True)
                return False
        except Exception:
            pass
        return False
