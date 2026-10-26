"""Multi-page viewing pipeline (M3-B). Orchestrator only, no Qt, no PyMuPDF.

Wires the validated M2/M3-A components without redesigning them::

    DocumentLayout (offsets + bisect)
        -> per-page plan_requests() (existing, unchanged)
        -> TileCache lookup (generation-normalized identity)
        -> admission control -> RendererEngine.submit (existing lanes/dedup)
        -> Future.add_done_callback -> epoch check -> cache insert

The pipeline OWNS its engine (created here, shut down here) so document
close deterministically releases worker handles. No global state; one
pipeline per open document. Bitmaps live only in worker temps (released
per M2) and in the bounded cache.
"""

from __future__ import annotations

import dataclasses
import threading
from concurrent.futures import Future

from core.types import DocumentId, Size
from document.models import PageInfo
from rendering.cache import TileCache, CachedTile, cache_identity
from rendering.engine import RendererEngine
from rendering.errors import QueueFullError, RenderCancelledError, RenderFailedError
from rendering.layout import DocumentLayout, unrotated_size
from rendering.page_decode import (
    DecodedPage,
    PageDecodeCache,
    decode_key,
    slice_tile,
)
from rendering.process_decoder import (
    MAX_PROCESS_WORKERS,
    ProcessDecoder,
    ProcessDecoderConfig,
)
from rendering.renderer import PIXEL_FORMAT
from rendering.scale import next_bucket, quantize_scale
from rendering.scheduler import EngineConfig
from rendering.tile_grid import MAX_PREFETCH_TILES, plan_requests, visible_tiles
from rendering.types import (
    DEFAULT_TILE_SIZE,
    RenderPriority,
    RenderRequest,
    RenderResult,
    TileKey,
    TileRect,
    is_stale,
)

__all__ = ["DocumentPipeline", "VISIBLE_HEADROOM", "superseded_prefetch"]

#: Prefetch is admitted only while scheduler pending stays below
#: max_pending - VISIBLE_HEADROOM, reserving capacity for visible work.
VISIBLE_HEADROOM = 8


def superseded_prefetch(
    in_flight: dict[tuple, tuple[Future, int, RenderRequest, RenderPriority]],
    wanted: set[tuple],
) -> list[RenderRequest]:
    """Pure helper: in-flight PREFETCH requests absent from the new plan.

    Deterministic (sorted by request key); the caller cancels them
    best-effort via the engine.
    """
    out = [
        entry[2]
        for identity, entry in in_flight.items()
        if entry[3] == RenderPriority.PREFETCH and identity not in wanted
    ]
    return sorted(out, key=lambda r: r.key())


def _group_key_of(req: RenderRequest) -> tuple:
    """Page-decode group identity: (page, scale, rotation, generation).

    Canonical construction shared by staging and withdrawal so both agree
    on which submitted decodes belong to the current plan.
    """
    return (
        int(req.page_index),
        float(req.scale),
        int(req.rotation_degrees),
        int(req.generation),
    )


def _fallback_gate_config(
    engine_config: EngineConfig | None, process_decode_workers: int
) -> EngineConfig:
    """Derive the engine config with the fallback single-render gate.

    When page decoding runs in isolated processes, in-process engine
    renders are exclusively fallback tiles (stale/withdrawn groups); up
    to ``resolved_workers()`` of them rasterizing concurrently holds the
    GIL and starves the GUI thread (M3 Finding F-1). Bounding them to
    one concurrent render restores heartbeat responsiveness while every
    submission/queueing/cancellation/completion semantic stays intact.

    Legacy mode (``process_decode_workers == 0``) renders everything
    in-process and keeps today's concurrency exactly: the caller's
    config (or the default) passes through unmodified. An explicitly
    set ``max_concurrent_renders`` is always honored. The caller's
    config object is never mutated.
    """
    base = engine_config if engine_config is not None else EngineConfig()
    if int(process_decode_workers) >= 1 and base.max_concurrent_renders == 0:
        return dataclasses.replace(base, max_concurrent_renders=1)
    return base


class DocumentPipeline:
    """Per-document viewing pipeline: layout + cache + engine wiring."""

    def __init__(
        self,
        document_id: DocumentId,
        source_path: str,
        pages: list[PageInfo],
        engine_config: EngineConfig | None = None,
        cache_budget_bytes: int | None = None,
        tile_size: int = DEFAULT_TILE_SIZE,
        gap_px: int = 16,
        ui_zoom: float = 1.0,
        user_rotation: int = 0,
        process_decode_workers: int = 2,
    ) -> None:
        """Build a pipeline from backend page descriptors.

        ``pages`` are :class:`PageInfo` as returned by the document backend
        (rect dimensions pre-rotated by PyMuPDF, plus intrinsic rotation).
        Media dimensions are recovered internally via :func:`unrotated_size`
        so layout/raster math stays in unrotated space.

        ``process_decode_workers`` selects the page-decode execution path:
        ``0`` renders full-page decodes in-process via the engine (Path F
        baseline); ``1``/``2`` route them to a bounded persistent pool of
        isolated decoder processes (prototype, default 2). Per-group
        fallback to individual tile renders is identical either way.
        """
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if not pages:
            raise ValueError("pages must be non-empty")
        if not 0 <= int(process_decode_workers) <= MAX_PROCESS_WORKERS:
            raise ValueError(
                f"process_decode_workers must be 0..{MAX_PROCESS_WORKERS}"
            )
        self._document_id = document_id
        self._source_path = source_path
        self._tile_size = int(tile_size)
        self._gap_px = gap_px
        self._page_sizes_pt = [
            Size(*unrotated_size(info.width, info.height, info.rotation))
            for info in pages
        ]
        self._intrinsics = [int(info.rotation) for info in pages]
        self._engine = RendererEngine(_fallback_gate_config(
            engine_config, int(process_decode_workers)))
        self._cache = TileCache(cache_budget_bytes) if cache_budget_bytes else TileCache()
        self._lock = threading.Lock()
        self._current_bucket = quantize_scale(ui_zoom)
        self._user_rotation = int(user_rotation)
        self._epoch = 0
        self._layout = self._build_layout()
        self._in_flight: dict[tuple, tuple[Future, int, RenderRequest, RenderPriority]] = {}
        self._decodes = PageDecodeCache()
        self._decode_cover: dict[tuple, dict] = {}
        # M4.6.5: withdraw memo — withdraw(wanted) is idempotent for an
        # unchanged wanted set (all queuers submit from the current plan
        # before withdraw runs, so nothing new can appear between two
        # identical updates). Skipping the redundant call avoids taking
        # the decoder lock on the GUI thread ~200x per scroll gesture;
        # lock acquisition there measured up to ~18ms under load (the
        # upward-scroll stall). Real viewport changes still withdraw.
        self._last_withdraw_wanted: frozenset = frozenset()
        self._decode_failed: set[tuple] = set()
        # M4.7 scroll direction: derived from actual viewport movement
        # (sign of scroll delta with a sub-tile deadband), never from
        # wheel events. -1 = up, +1 = down, 0 = unknown/stationary.
        # Updated on every update(); direction flips drive scheduler
        # reprioritization of the current visible set.
        self._last_scroll_y: float | None = None
        self._scroll_direction: int = 0
        self.direction_flips = 0
        # M4.7 current-relevance snapshots: the wanted sets of the most
        # recent update(). The completion admission gate checks arrivals
        # against these (overwritten per update, never unioned), so an
        # A -> B -> A reversal re-admits A when it is wanted again.
        self._last_wanted_idents: frozenset = frozenset()
        self._last_wanted_groups: frozenset = frozenset()
        self._last_wanted_ids: frozenset = frozenset()
        # M4.7 submission flip-stamps: identity -> direction_flips at
        # submit time. The admission gate discards only work submitted
        # BEFORE a direction flip that is also no longer wanted, so
        # steady traversal (no flip) admits slightly-lagging results
        # exactly as before. Popped on completion; reconciled on size.
        self._submit_flip: dict[tuple, int] = {}
        self._proc = (
            ProcessDecoder(ProcessDecoderConfig(workers=int(process_decode_workers)))
            if int(process_decode_workers) >= 1
            else None
        )
        self._closed = False
        self.hits = 0
        self.submitted_visible = 0
        self.submitted_prefetch = 0
        self.attached_in_flight = 0
        self.stale_discarded = 0
        self.queue_full = 0
        self.prefetch_dropped = 0
        self.prefetch_deferred = 0
        self.prefetch_cancelled = 0
        self.submit_errors = 0
        self.decode_submits = 0
        self.sliced_tiles = 0
        self.decode_fallbacks = 0
        # M4.7: completions discarded by the current-relevance admission
        # gate (valid generation, no longer wanted). Additive only.
        self.superseded_discarded = 0
        # H1: withdrawn/superseded decodes that were retired WITHOUT
        # poisoning their group (retryable on revisit). Genuine failures
        # still use decode_fallbacks + _decode_failed. Additive only.
        self.decode_superseded = 0
        # M4.7: covered tiles skipped at fan-out (no longer wanted when
        # their decode landed). Additive only.
        self.fanout_skipped = 0

    # -- configuration ---------------------------------------------------

    @property
    def raster_scale(self) -> float:
        """Active quantized raster bucket."""
        with self._lock:
            return self._current_bucket

    @property
    def epoch(self) -> int:
        """Current freshness epoch (bumped on bucket/rotation change)."""
        with self._lock:
            return self._epoch

    def set_zoom(self, ui_zoom: float) -> float:
        """Apply continuous UI zoom; rebuild layout only on bucket change."""
        with self._lock:
            self._ensure_open_locked()
            bucket = next_bucket(ui_zoom, self._current_bucket)
            if bucket != self._current_bucket:
                self._current_bucket = bucket
                self._layout = self._build_layout()
                self._epoch += 1
                self._decodes.clear()
            return self._current_bucket

    def set_user_rotation(self, user_rotation: int) -> None:
        """Apply user rotation; rebuild layout on change."""
        with self._lock:
            self._ensure_open_locked()
            rot = int(user_rotation)
            if rot != self._user_rotation:
                self._user_rotation = rot
                self._layout = self._build_layout()
                self._epoch += 1
                self._decodes.clear()

    def _build_layout(self) -> DocumentLayout:
        return DocumentLayout(
            self._document_id,
            self._page_sizes_pt,
            self._intrinsics,
            self._current_bucket,
            self._user_rotation,
            self._gap_px,
        )

    # -- planning / submission -------------------------------------------

    def update(
        self, scroll_y_doc: float, viewport_width_px: int, viewport_height_px: int,
        scroll_x_doc: float = 0.0,
    ) -> dict:
        """Plan current viewport, serve cache hits, submit misses.

        Visible misses submit before any prefetch; prefetch is admitted
        only with queue headroom. Returns a summary dict (hits, submitted
        counts, drops). Never raises for scheduler pressure (counts it).

        M5-B.1.2: ``scroll_x_doc`` is the document-space horizontal
        viewport origin in render px (default 0.0 preserves legacy
        behavior for all existing positional callers).

        Locking: engine calls (submit/cancel) run OUTSIDE the pipeline
        lock because completed/cancelled futures invoke our callback
        inline — holding the lock across them would self-deadlock.
        """
        with self._lock:
            self._ensure_open_locked()
            epoch = self._epoch
            # M4.7 direction from actual viewport movement (sticky:
            # only genuine movement changes it; identical polls
            # keep it). Any nonzero delta counts — trackpad ticks can
            # be a few doc px, and a slow reversal must still register.
            scroll_y = float(scroll_y_doc)
            flipped = False
            if self._last_scroll_y is not None:
                delta = scroll_y - self._last_scroll_y
                if delta != 0.0:
                    new_dir = 1 if delta > 0 else -1
                    if new_dir != self._scroll_direction:
                        if self._scroll_direction != 0:
                            self.direction_flips += 1
                            flipped = True
                        self._scroll_direction = new_dir
            self._last_scroll_y = scroll_y
            visible_reqs, prefetch_reqs = self._plan_locked(
                scroll_y_doc, viewport_width_px, viewport_height_px, epoch,
                self._scroll_direction, scroll_x_doc=scroll_x_doc,
            )
            wanted = {cache_identity(TileKey.from_request(r)) for r in (*visible_reqs, *prefetch_reqs)}
            hits = 0
            visible_unresolved = 0
            to_submit: list[tuple[RenderRequest, RenderPriority]] = []
            to_decode: list[tuple[RenderRequest, RenderPriority]] = []
            pending: dict[tuple, dict] = {}
            for req in visible_reqs:
                outcome = self._decode_stage_locked(req, RenderPriority.VISIBLE, pending)
                if outcome == "hit":
                    hits += 1
                elif outcome == "submit":
                    visible_unresolved += 1
                    to_submit.append((req, RenderPriority.VISIBLE))
                elif outcome in ("attached", "pending"):
                    visible_unresolved += 1
            if visible_unresolved == 0:
                for req in prefetch_reqs:
                    outcome = self._decode_stage_locked(req, RenderPriority.PREFETCH, pending)
                    if outcome == "submit":
                        to_submit.append((req, RenderPriority.PREFETCH))
            elif prefetch_reqs:
                self.prefetch_deferred += len(prefetch_reqs)
            for group_key, group in pending.items():
                decode_req = self._decode_request_locked(group)
                did = cache_identity(TileKey.from_request(decode_req))
                wanted.add(did)
                self._decode_cover[did] = {
                    "group": group_key,
                    "request": decode_req,
                    "reqs": list(group["reqs"]),
                }
                if self._proc is not None:
                    to_decode.append((decode_req, group["priority"]))
                else:
                    to_submit.append((decode_req, group["priority"]))
            # Groups wanted by the CURRENT plan (visible + planned prefetch,
            # even when prefetch staging is deferred this round). Submitted
            # decodes for other groups are superseded viewports: withdraw
            # them from the decoder queue while still unassigned. Running
            # decodes are never touched; withdrawn groups fall back to
            # per-tile renders if their viewport returns.
            wanted_groups = {_group_key_of(r) for r in (*visible_reqs, *prefetch_reqs)}
            wanted_ids = {
                cover["request"].request_id
                for cover in self._decode_cover.values()
                if cover["group"] in wanted_groups
            }
            for req, _prio in to_decode:
                wanted_ids.add(req.request_id)
            # M4.7 current-relevance snapshots for the completion
            # admission gate (overwritten every update).
            self._last_wanted_idents = frozenset(wanted)
            self._last_wanted_groups = frozenset(wanted_groups)
            self._last_wanted_ids = frozenset(wanted_ids)
            # Bound flip-stamp orphans left by dropped submissions.
            if len(self._submit_flip) > 4 * self._engine.config.max_pending:
                live = set(self._in_flight) | set(self._decode_cover)
                for stale_key in [k for k in self._submit_flip if k not in live]:
                    self._submit_flip.pop(stale_key, None)
            # Scheduler keys for the current visible set (promotion on
            # direction flip happens outside the lock, below).
            visible_keys = [r.key() for r in visible_reqs] if flipped else []
            to_cancel = superseded_prefetch(self._in_flight, wanted)
        cancelled = 0
        for stale_req in to_cancel:
            try:
                if self._engine.cancel(stale_req):
                    cancelled += 1
            except Exception:
                pass
        if cancelled:
            with self._lock:
                self.prefetch_cancelled += cancelled
        if visible_keys:
            # M4.7 reversal: currently-visible queued work outranks
            # stale-direction work (promote-only; stale keeps its rank).
            try:
                self._engine.reprioritize(visible_keys)
            except Exception:
                pass
        for req, priority in to_submit:
            if priority == RenderPriority.PREFETCH and (
                self._engine.pending_count()
                >= self._engine.config.max_pending - VISIBLE_HEADROOM
            ):
                with self._lock:
                    self.prefetch_dropped += 1
                continue
            self._submit_outside(req, priority, epoch)
        for req, priority in to_decode:
            self._submit_decode_outside(req, priority, epoch)
        if self._proc is not None:
            wanted_frozen = frozenset(wanted_ids)
            if wanted_frozen != self._last_withdraw_wanted:
                self._last_withdraw_wanted = wanted_frozen
                self._proc.withdraw(wanted_ids)
        return {
            "visible": len(visible_reqs),
            "prefetch": len(prefetch_reqs),
            "hits": hits,
            "epoch": epoch,
        }

    def _decode_stage_locked(
        self, req: RenderRequest, priority: RenderPriority, pending: dict
    ) -> str:
        """Stage one tile request via the page-decode path when eligible.

        Returns "hit" (tile served, from cache or sliced), "submit"
        (individual tile render), "attached" (covered by in-flight work) or
        "pending" (accumulated into a page group for one shared decode).
        Falls back to :meth:`_stage_locked` whenever page decoding cannot
        apply, so behavior is identical to the pre-prototype path there.
        """
        tile_key = TileKey.from_request(req)
        if self._cache.get(tile_key) is not None:
            self.hits += 1
            return "hit"
        if not self._decode_eligible_locked():
            return self._stage_locked(req, priority)
        group = (
            int(req.page_index),
            float(req.scale),
            int(req.rotation_degrees),
            int(req.generation),
        )
        if group in self._decode_failed:
            return self._stage_locked(req, priority)
        try:
            fw, fh = self._page_render_size_locked(req.page_index)
        except Exception:
            return self._stage_locked(req, priority)
        if fw <= 0 or fh <= 0:
            return self._stage_locked(req, priority)
        key = decode_key(
            req.document_id.value,
            req.page_id.value,
            float(req.scale),
            int(req.rotation_degrees),
        )
        decoded = self._decodes.get(key, int(req.generation))
        if decoded is not None:
            try:
                data = slice_tile(
                    decoded, req.tile.x, req.tile.y, req.tile.width, req.tile.height
                )
            except Exception:
                return self._stage_locked(req, priority)
            self._cache.put(
                CachedTile(
                    key=tile_key,
                    data=data,
                    width_px=req.tile.width,
                    height_px=req.tile.height,
                    stride=req.tile.width * 4,
                )
            )
            self.hits += 1
            self.sliced_tiles += 1
            return "hit"
        did = cache_identity(
            TileKey(
                document_id=req.document_id,
                page_id=req.page_id,
                generation=req.generation,
                scale=req.scale,
                rotation=int(req.rotation_degrees),
                tile_x=0,
                tile_y=0,
                tile_w=fw,
                tile_h=fh,
            )
        )
        existing = self._in_flight.get(did)
        if existing is not None:
            if not existing[0].done():
                self.attached_in_flight += 1
                return "attached"
            self._in_flight.pop(did, None)
        slot = pending.get(group)
        if slot is None:
            pending[group] = {"fw": fw, "fh": fh, "reqs": [req], "priority": priority}
        else:
            slot["reqs"].append(req)
            if priority == RenderPriority.VISIBLE:
                slot["priority"] = RenderPriority.VISIBLE
        return "pending"

    def _decode_eligible_locked(self) -> bool:
        """Full-page decode buckets (M3 Gate 5, stage 2: 4.0x added).

        8.0x stays in-process (payload/child-RSS risk without validation
        evidence); see _on_decode_complete_locked for oversized handling.
        """
        return float(self._current_bucket) in (0.5, 1.0, 2.0, 4.0)

    def _page_render_size_locked(self, page_index: int) -> tuple[int, int]:
        """Full-page render dimensions at the current bucket (raises IndexError)."""
        entry = self._layout.entry(int(page_index))
        return int(entry.render_width_px), int(entry.render_height_px)

    def _decode_request_locked(self, group: dict) -> RenderRequest:
        """Build the single full-page decode request for a pending group."""
        reqs = group["reqs"]
        exemplar = reqs[0]
        fw, fh = int(group["fw"]), int(group["fh"])
        return RenderRequest(
            document_id=exemplar.document_id,
            page_id=exemplar.page_id,
            page_index=exemplar.page_index,
            target_size=Size(float(fw), float(fh)),
            scale=exemplar.scale,
            rotation_degrees=int(exemplar.rotation_degrees),
            tile=TileRect(0, 0, fw, fh),
            generation=exemplar.generation,
            priority=group["priority"],
            source_path=self._source_path,
        )

    def _stage_locked(
        self, req: RenderRequest, priority: RenderPriority
    ) -> str:
        """Cache/in-flight check under lock. Never touches the engine."""
        tile_key = TileKey.from_request(req)
        if self._cache.get(tile_key) is not None:
            self.hits += 1
            return "hit"
        existing = self._in_flight.get(cache_identity(tile_key))
        if existing is not None:
            if not existing[0].done():
                self.attached_in_flight += 1
                return "attached"
            self._in_flight.pop(cache_identity(tile_key), None)
        return "submit"

    def _submit_outside(
        self, req: RenderRequest, priority: RenderPriority, epoch: int
    ) -> str:
        """Submit one request; callback-safe (no lock held across it)."""
        identity = cache_identity(TileKey.from_request(req))
        try:
            future = self._engine.submit(req)
        except QueueFullError:
            with self._lock:
                self.queue_full += 1
                if priority == RenderPriority.PREFETCH:
                    self.prefetch_dropped += 1
            return "dropped"
        except RenderCancelledError:
            # Engine cancellation fires only for superseded prefetch
            # (superseded_prefetch loop) or close: cleanup tracking but
            # never poison the group — a revisit must retry the decode.
            with self._lock:
                self.submit_errors += 1
                self._decode_cover.pop(identity, None)
                self.decode_superseded += 1
            return "dropped"
        except RenderFailedError:
            with self._lock:
                self.submit_errors += 1
                cover = self._decode_cover.pop(identity, None)
                if cover is not None:
                    self._decode_failed.add(cover["group"])
                    self.decode_fallbacks += 1
            return "dropped"
        with self._lock:
            if future.done():
                # Completed between submit and registration. No callback
                # was ever registered (registration below happens only on
                # the not-done path), so this result is dropped here;
                # the next update() re-submits the tile if it is still
                # wanted. Nothing to track.
                return "done"
            self._in_flight[identity] = (future, epoch, req, priority)
            self._submit_flip[identity] = self.direction_flips
            if identity in self._decode_cover:
                self.decode_submits += 1
            if priority == RenderPriority.VISIBLE:
                self.submitted_visible += 1
            else:
                self.submitted_prefetch += 1
        future.add_done_callback(
            lambda fut, ident=identity, ep=epoch: self._on_complete(fut, ident, ep)
        )
        return "submitted"

    def _submit_decode_outside(
        self, req: RenderRequest, priority: RenderPriority, epoch: int
    ) -> str:
        """Submit one page decode to the isolated process pool.

        Mirrors :meth:`_submit_outside` bookkeeping exactly (same
        ``_in_flight`` identity, same ``_decode_cover``, same counters), so
        delivery flows through the single :meth:`_on_complete` path. The raw
        child payload is adapted into a :class:`RenderResult` first; a
        decoder future that is already complete delivers inline via the
        callback, closing the warm-result race deterministically.
        """
        identity = cache_identity(TileKey.from_request(req))
        if priority == RenderPriority.PREFETCH and self._proc is not None and (
            self._engine.pending_count() + self._proc.pending_count()
            >= self._engine.config.max_pending - VISIBLE_HEADROOM
        ):
            with self._lock:
                self._decode_cover.pop(identity, None)  # re-pend next update
                self.prefetch_dropped += 1
            return "dropped"
        assert self._proc is not None
        try:
            raw: Future = self._proc.submit(
                req.request_id,
                req.source_path or self._source_path,
                int(req.page_index),
                float(req.scale),
                int(req.rotation_degrees),
                int(req.tile.width),
                int(req.tile.height),
                priority_visible=(priority == RenderPriority.VISIBLE),
            )
        except (RenderFailedError, RenderCancelledError):
            with self._lock:
                self.submit_errors += 1
                cover = self._decode_cover.pop(identity, None)
                if cover is not None:
                    self._decode_failed.add(cover["group"])
                    self.decode_fallbacks += 1
            return "dropped"
        mapped: Future = Future()

        def _adapt(fut: Future) -> None:
            exc = fut.exception()
            if exc is not None:
                if not mapped.done():
                    mapped.set_exception(exc)
                return
            try:
                data, w, h, stride, render_ms, convert_ms = fut.result()
                result = RenderResult(
                    request=req,
                    size=Size(float(w), float(h)),
                    scale=req.scale,
                    success=True,
                    data=data,
                    width_px=int(w),
                    height_px=int(h),
                    pixel_format=PIXEL_FORMAT,
                    stride=int(stride),
                    generation=req.generation,
                    render_ms=float(render_ms),
                    convert_ms=float(convert_ms),
                    total_ms=float(render_ms) + float(convert_ms),
                )
            except Exception as e:
                if not mapped.done():
                    mapped.set_exception(
                        RenderFailedError(f"decode result invalid: {e}")
                    )
                return
            if not mapped.done():
                mapped.set_result(result)

        raw.add_done_callback(_adapt)
        with self._lock:
            self._in_flight[identity] = (mapped, epoch, req, priority)
            self._submit_flip[identity] = self.direction_flips
            if identity in self._decode_cover:
                self.decode_submits += 1
            if priority == RenderPriority.VISIBLE:
                self.submitted_visible += 1
            else:
                self.submitted_prefetch += 1
        mapped.add_done_callback(
            lambda fut, ident=identity, ep=epoch: self._on_complete(fut, ident, ep)
        )
        return "submitted"

    def _plan_locked(
        self, scroll_y_doc: float, vw: int, vh: int, epoch: int,
        direction: int = 0, scroll_x_doc: float = 0.0,
    ) -> tuple[list[RenderRequest], list[RenderRequest]]:
        y0 = float(scroll_y_doc)
        y1 = y0 + vh
        band = float(self._tile_size)
        visible_pages = set(self._layout.pages_in(y0, y1))
        expanded_pages = set(self._layout.pages_in(y0 - band, y1 + band))
        visible_reqs: list[RenderRequest] = []
        prefetch_reqs: list[RenderRequest] = []
        # H2 leading-first travel order: iterate pages leading-edge
        # first (ascending when scrolling up, descending when scrolling
        # down) so the newly-entered viewport content submits — and
        # therefore renders and arrives — before trailing content.
        # Coverage is unchanged (same symmetric band/ring); only
        # submission priority order changes. Counts, bounds, and the
        # retention policy are untouched.
        ordered = sorted(expanded_pages, reverse=(direction > 0))
        for index in ordered:
            entry = self._layout.entry(index)
            vs = self._layout.page_viewport(index, y0, vw, vh, epoch,
                                            scroll_x_doc=scroll_x_doc)
            page_size = Size(entry.width_pt, entry.height_pt)
            if index in visible_pages:
                vis, pre = plan_requests(vs, page_size, self._tile_size, 1, self._source_path)
                visible_reqs.extend(self._as_user_rotation(r) for r in vis)
                prefetch_reqs.extend(self._as_user_rotation(r) for r in pre)
            else:
                # Neighbor page in the expanded band: edge tiles as PREFETCH.
                keys = visible_tiles(vs, entry.render_width_px, entry.render_height_px, self._tile_size)
                for key in keys:
                    rect = key.to_tile_rect()
                    prefetch_reqs.append(
                        RenderRequest(
                            document_id=key.document_id,
                            page_id=key.page_id,
                            page_index=index,
                            target_size=Size(float(rect.width), float(rect.height)),
                            scale=key.scale,
                            rotation_degrees=self._user_rotation,
                            tile=rect,
                            generation=epoch,
                            priority=RenderPriority.PREFETCH,
                            source_path=self._source_path,
                        )
                    )
        # Global prefetch bound: plan output stays within visible + 8.
        allowed = MAX_PREFETCH_TILES
        return visible_reqs, prefetch_reqs[:allowed]

    def _as_user_rotation(self, req: RenderRequest) -> RenderRequest:
        """Rewrite a planned request's rotation from effective to user.

        Planning runs in effective-rotation space (correct tile-grid
        dimensions); the engine composes intrinsic rotation itself at render
        time, so submitted requests carry user rotation only. Keys stay
        cache-correct: per page, user rotation maps 1:1 to pixels.
        """
        if int(req.rotation_degrees) == self._user_rotation:
            return req
        return dataclasses.replace(req, rotation_degrees=self._user_rotation)

    def _on_complete(self, future: Future, identity: tuple, epoch: int) -> None:
        with self._lock:
            entry = self._in_flight.get(identity)
            if entry is not None and entry[0] is future:
                self._in_flight.pop(identity, None)
            submitted_flip = self._submit_flip.pop(identity, -1)
            cover = self._decode_cover.pop(identity, None)
            if cover is not None:
                self._on_decode_complete_locked(future, cover, epoch, submitted_flip)
                return
            if self._closed or future.cancelled():
                return
            exc = future.exception()
            if exc is not None:
                return  # engine already counted the failure
            if epoch != self._epoch:
                # Stale epoch: never inserted, never delivered.
                self.stale_discarded += 1
                return
            # M4.7 current-relevance admission gate: discard only work
            # submitted BEFORE a direction flip that is also no longer
            # wanted (stale-direction leftovers). Steady traversal (no
            # flip since submission) admits lagging results exactly as
            # before, so continuous motion keeps accumulating tiles.
            # Checked against the CURRENT wanted snapshot (overwritten
            # every update), so A -> B -> A re-admits A.
            if (identity not in self._last_wanted_idents
                    and submitted_flip < self.direction_flips):
                self.superseded_discarded += 1
                return
            try:
                result = future.result()
            except Exception:
                return
            if result.data is None or is_stale(result, self._epoch):
                if result.data is not None:
                    self.stale_discarded += 1
                return
            key = TileKey.from_request(result.request)
            self._cache.put(
                CachedTile(
                    key=key,
                    data=result.data,
                    width_px=result.width_px,
                    height_px=result.height_px,
                    stride=result.stride,
                )
            )

    def _on_decode_complete_locked(self, future: Future, cover: dict, epoch: int,
                                     submitted_flip: int = -1) -> None:
        """Store a completed page decode; failures fall back to tile renders.

        Runs under the pipeline lock (like :meth:`_on_complete`). Never
        inserts tiles: tiles are sliced lazily from the resident decode on
        the next :meth:`update`.
        """
        group = cover["group"]
        if self._closed or future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            # H1 semantic: a WITHDRAWN/superseded decode (cancellation)
            # is NOT a decode failure — the page stays retryable so a
            # later revisit re-decodes via the GIL-free subprocess path
            # instead of being forced into per-tile engine fallback.
            # Genuine failures (render errors, invalid payloads) still
            # poison the group exactly as before.
            if isinstance(exc, RenderCancelledError):
                self.decode_superseded += 1
                return
            self._decode_failed.add(group)
            self.decode_fallbacks += 1
            return
        if epoch != self._epoch:
            self.stale_discarded += 1
            return
        # M4.7 current-relevance admission gate (see _on_complete): an
        # obsolete running decode still finishes and frees its worker,
        # but a pre-flip result that is no longer wanted is dropped
        # instead of polluting the decode cache and fanning stale tiles
        # into the tile cache. Never marks _decode_failed — a reverted
        # viewport re-decodes cleanly, and steady-traversal lag admits.
        if (group not in self._last_wanted_groups
                and submitted_flip < self.direction_flips):
            self.superseded_discarded += 1
            return
        try:
            result = future.result()
        except Exception:
            self._decode_failed.add(group)
            self.decode_fallbacks += 1
            return
        if result.data is None or is_stale(result, self._epoch):
            if result.data is not None:
                self.stale_discarded += 1
            else:
                self._decode_failed.add(group)
                self.decode_fallbacks += 1
            return
        width_px, height_px, stride = (
            int(result.width_px),
            int(result.height_px),
            int(result.stride),
        )
        if (
            width_px <= 0
            or height_px <= 0
            or stride != width_px * 4
            or len(result.data) != width_px * height_px * 4
        ):
            self._decode_failed.add(group)
            self.decode_fallbacks += 1
            return
        dreq = cover["request"]
        page = DecodedPage(
            key=decode_key(
                dreq.document_id.value,
                dreq.page_id.value,
                float(dreq.scale),
                int(dreq.rotation_degrees),
            ),
            data=result.data,
            width_px=width_px,
            height_px=height_px,
            stride=stride,
            generation=epoch,
        )
        if not self._decodes.put(page):
            # Oversized payload (e.g. a 4.0x raster larger than the whole
            # PageDecodeCache budget): fan out the covered tiles from the
            # TRANSIENT page instead of failing. The page is never
            # retained (unreachable on return); tiles land in TileCache
            # under normal identities, so later updates hit them there.
            # Groups with genuinely broken geometry were already marked
            # failed above and keep the per-tile fallback path.
            self._fan_out_locked(page, cover.get("reqs", ()))
            return
        self._fan_out_locked(page, cover.get("reqs", ()))

    def _fan_out_locked(self, page: DecodedPage, reqs) -> None:
        """Slice covered tiles into the tile cache at decode completion.

        Mirrors the lazy slicing in :meth:`_decode_stage_locked` so tiles
        materialize as soon as their decode lands, without waiting for the
        next :meth:`update`. Per-tile failures fall back to individual
        renders on the next update instead of failing the whole decode.

        M4.7: only tiles still in the current wanted set are sliced.
        During fast traversal the plan moves on before the decode lands;
        fanning out stale tiles would memcpy megabytes per completion and
        evict useful forward entries for tiles no viewport will revisit.
        Skipped tiles re-slice lazily from the resident decode (or fall
        back) if their viewport returns.
        """
        wanted = self._last_wanted_idents
        for req in reqs:
            tile_key = TileKey.from_request(req)
            if cache_identity(tile_key) not in wanted:
                self.fanout_skipped += 1
                continue
            try:
                data = slice_tile(
                    page, req.tile.x, req.tile.y, req.tile.width, req.tile.height
                )
            except Exception:
                continue
            self._cache.put(
                CachedTile(
                    key=tile_key,
                    data=data,
                    width_px=req.tile.width,
                    height_px=req.tile.height,
                    stride=req.tile.width * 4,
                )
            )
            self.sliced_tiles += 1

    # -- polling ----------------------------------------------------------

    def visible_ready(
        self, scroll_y_doc: float, viewport_width_px: int, viewport_height_px: int,
        scroll_x_doc: float = 0.0,
    ) -> tuple[CachedTile, ...]:
        """Qt-free polling: resident tiles for the current visible set."""
        with self._lock:
            self._ensure_open_locked()
            y0 = float(scroll_y_doc)
            out: list[CachedTile] = []
            for index in self._layout.pages_in(y0, y0 + viewport_height_px):
                entry = self._layout.entry(index)
                vs = self._layout.page_viewport(
                    index, y0, viewport_width_px, viewport_height_px, self._epoch,
                    scroll_x_doc=scroll_x_doc,
                )
                page_size = Size(entry.width_pt, entry.height_pt)
                vis, _pre = plan_requests(vs, page_size, self._tile_size, 0, self._source_path)
                for req in vis:
                    tile = self._cache.get(
                        TileKey.from_request(self._as_user_rotation(req))
                    )
                    if tile is not None:
                        out.append(tile)
            return tuple(out)

    # -- stats / lifecycle -------------------------------------------------

    def prewarm(self) -> None:
        """Start the process-decoder pool eagerly (never raises).

        Construction spawns nothing by design; the first decode would
        otherwise pay the worker-process spawn on the submitting (GUI)
        thread. Background preparation calls this so the spawn happens
        off the GUI thread. A closed pipeline is a silent no-op. The
        engine's thread pool stays lazy on first submit (thread creation
        is not a freeze source); nothing is submitted here.
        """
        try:
            with self._lock:
                self._ensure_open_locked()
                proc = self._proc
        except Exception:
            return
        if proc is not None:
            proc.prewarm()

    def stats(self) -> dict:
        """Programmatic snapshot: pipeline + cache + engine metrics."""
        with self._lock:
            return {
                "epoch": self._epoch,
                "raster_scale": self._current_bucket,
                "in_flight": len(self._in_flight),
                "hits": self.hits,
                "submitted_visible": self.submitted_visible,
                "submitted_prefetch": self.submitted_prefetch,
                "attached_in_flight": self.attached_in_flight,
                "stale_discarded": self.stale_discarded,
                "queue_full": self.queue_full,
                "prefetch_dropped": self.prefetch_dropped,
                "prefetch_deferred": self.prefetch_deferred,
                "prefetch_cancelled": self.prefetch_cancelled,
                "superseded_discarded": self.superseded_discarded,
                "decode_superseded": self.decode_superseded,
                "fanout_skipped": self.fanout_skipped,
                "scroll_direction": self._scroll_direction,
                "direction_flips": self.direction_flips,
                "submit_errors": self.submit_errors,
                "decode_submits": self.decode_submits,
                "sliced_tiles": self.sliced_tiles,
                "decode_fallbacks": self.decode_fallbacks,
                "process_decode_workers": (
                    self._proc.config.workers if self._proc is not None else 0
                ),
                "process_decoder": (
                    self._proc.stats() if self._proc is not None else None
                ),
                "page_decode": self._decodes.stats(),
                "cache": self._cache.stats(),
                "engine": self._engine.metrics(),
            }

    def close(self) -> None:
        """Cancel in-flight work, shut down the engine, drop the cache.

        The engine join happens WITHOUT holding the pipeline lock: worker
        completion callbacks need that lock, so joining while holding it
        would deadlock. The process decoder shuts down under the same
        discipline (safe with zero workers ever spawned).
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._in_flight.values())
            self._in_flight.clear()
        for _future, _epoch, req, _prio in pending:
            try:
                self._engine.cancel(req)
            except Exception:
                pass
        try:
            self._engine.shutdown()
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.shutdown()
            except Exception:
                pass
        self._cache.clear()
        self._decodes.clear()
        self._decode_cover.clear()
        self._decode_failed.clear()

    @property
    def source_path(self) -> str:
        """Current render-file source (never a save destination)."""
        return self._source_path

    def rebind_source(self, source_path: str) -> None:
        """Point the pipeline at a new render file without resetting layout.

        Same-path saving detaches the renderer from the source file before
        replacing it (Windows locks). The document materializes a sanitized
        working copy and this method releases the old engine/process
        decoder handles, swaps the source path, and retires stale caches.
        Layout, zoom, rotation, and scroll remain untouched.
        """
        if not source_path or not isinstance(source_path, str):
            raise ValueError(f"invalid render source: {source_path!r}")
        with self._lock:
            self._ensure_open_locked()
            old_engine = self._engine
            old_proc = self._proc
            engine_config = old_engine.config
            proc_config = old_proc.config if old_proc is not None else None
            pending = list(self._in_flight.values())
            self._in_flight.clear()

        new_engine = RendererEngine(engine_config)
        new_proc = None
        try:
            if proc_config is not None:
                new_proc = ProcessDecoder(proc_config)
        except Exception:
            try:
                new_engine.shutdown()
            except Exception:
                pass
            raise

        for _future, _epoch, req, _prio in pending:
            try:
                old_engine.cancel(req)
            except Exception:
                pass
        try:
            old_engine.shutdown()
        except Exception:
            pass
        if old_proc is not None:
            try:
                old_proc.shutdown()
            except Exception:
                pass

        with self._lock:
            self._source_path = source_path
            self._engine = new_engine
            self._proc = new_proc
            self._epoch += 1
            self._cache.clear()
            self._decodes.clear()
            self._decode_cover.clear()
            self._decode_failed.clear()
            self._submit_flip.clear()
            self._last_withdraw_wanted = frozenset()
            self._last_wanted_idents = frozenset()
            self._last_wanted_groups = frozenset()
            self._last_wanted_ids = frozenset()
            self._last_scroll_y = None
            self._scroll_direction = 0

    @property
    def layout(self) -> DocumentLayout:
        """Read-only document layout (immutable; rebuilt on bucket/rotation change)."""
        with self._lock:
            return self._layout

    def visible_plan(
        self, scroll_y_doc: float, viewport_width_px: int, viewport_height_px: int,
        scroll_x_doc: float = 0.0,
    ) -> tuple[TileKey, ...]:
        """Pure read-only plan of visible tile keys (no submission, no I/O).

        For viewers/polling code that needs placeholder geometry without
        submitting work. Keys carry the current epoch/generation.
        """
        with self._lock:
            self._ensure_open_locked()
            y0 = float(scroll_y_doc)
            out: list[TileKey] = []
            for index in self._layout.pages_in(y0, y0 + viewport_height_px):
                entry = self._layout.entry(index)
                vs = self._layout.page_viewport(
                    index, y0, viewport_width_px, viewport_height_px, self._epoch,
                    scroll_x_doc=scroll_x_doc,
                )
                page_size = Size(entry.width_pt, entry.height_pt)
                vis, _pre = plan_requests(vs, page_size, self._tile_size, 0, self._source_path)
                for req in vis:
                    out.append(TileKey.from_request(self._as_user_rotation(req)))
            return tuple(out)

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RenderCancelledError("pipeline is closed")

    def __enter__(self) -> DocumentPipeline:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
