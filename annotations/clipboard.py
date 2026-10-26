"""Versioned copy/paste payload for annotation selections (M6.8.2 clipboard).

Qt-free, stdlib-only (same layer rules as the rest of ``annotations/*``):
the *data* format lives here, the system-clipboard transport lives in
``app/clipboard.py``. Keeping the two apart is what makes the payload
testable without a QApplication and keeps the models/geometry layers
free of any clipboard or Qt dependency.

What travels
------------
The payload is structured, editable annotation data — never a bitmap,
screenshot, rendering cache, undo object, selection-frame object, page
handle, or active gesture. Every supported annotation model is encoded
by *type* with explicit fields:

* :class:`~annotations.models.InkStroke` — points, color, width, opacity,
  raw pressure/timestamp samples, pressure-rendering flags/profile;
* :class:`~annotations.models.Highlighter` — points, color, width,
  opacity, optional text-assist metadata, and optional per-sample
  pressures (M7.1.4; absent in legacy payloads);
* :class:`~annotations.models.Shape` — kind, canonical rect, colors,
  opacities, width, fill/line/ending style, ordered points, close-path
  flag, and ``rotation_deg``;
* :class:`~annotations.models.StickyNote` — position, text, color.

Identity and page association are *not* copied: a pasted annotation gets
a fresh :class:`~core.types.AnnotationId` and a destination-derived
:class:`~core.types.PageId` (see :func:`paste_plan`). The source page
index is kept per record together with the payload's anchor page (the
lowest source page) so a multi-page group can be re-laid onto the
corresponding relative destination pages.

Format and validation
---------------------
One JSON object, ``format`` tag + integer ``version`` (currently
:data:`FORMAT_VERSION`) + ``anchor_page`` + ``records``. Parsing is
explicit and total: no code is ever executed, no ``eval``/``pickle``/
import path is involved, and arbitrary extra fields are rejected rather
than silently ignored. Every field is range-checked (finite numbers,
bounded coordinates, bounded point counts, opacity in ``[0, 1]``, known
shape kinds/line styles/endings/pressure profiles, hex colors, bounded
text) before an annotation object is constructed. Any problem raises
:class:`ClipboardError`, so callers can reject the whole payload and
leave the document, history, and clipboard untouched.

All-or-nothing is structural: :func:`payload_from_annotations` builds the
bytes and then re-parses them through :meth:`ClipboardPayload.from_bytes`,
so the exact wire format is what gets validated; :func:`paste_plan`
validates every destination page and constructs *every* candidate before
returning, so a caller never inserts a prefix of a group.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from dataclasses import dataclass

from annotations.ink_geometry import PRESSURE_PROFILES
from annotations.models import (
    AnnotationBase,
    Highlighter,
    InkStroke,
    Shape,
    ShapeKind,
    StickyNote,
    TextAssist,
)
from annotations.transform import move_annotation
from core.types import AnnotationId, PageId, Point, Rect, Size
from tools.models import LINE_ENDINGS, LINE_STYLES

__all__ = [
    "FORMAT_TAG",
    "FORMAT_VERSION",
    "MIME_TYPE",
    "MAX_COORD",
    "MAX_BYTES",
    "MAX_POINTS",
    "MAX_RECORDS",
    "MAX_TEXT",
    "ClipboardError",
    "ClipboardPayload",
    "ClipboardRecord",
    "paste_plan",
    "payload_from_annotations",
]

#: Custom MIME type for an Inkfall annotation payload (system clipboard).
MIME_TYPE = "application/x-inkfall-annotations"

#: Wire-format tag and version. A payload with any other tag/version is
#: rejected outright (no best-effort downgrade).
FORMAT_TAG = "inkfall-annotations"
FORMAT_VERSION = 1

#: Hard bounds so a hostile or corrupted clipboard cannot exhaust memory
#: or produce meaningless geometry.
MAX_RECORDS = 2000
MAX_POINTS = 20000
MAX_TEXT = 4000
MAX_COORD = 1_000_000.0
MAX_WIDTH_MM = 1000.0
#: Raw wire-size ceiling, checked before JSON parsing so a hostile or
#: corrupted clipboard can never make the parser allocate unbounded memory.
MAX_BYTES = 8 * 1024 * 1024
_MAX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|"
                        r"[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")

_KIND_INK = "ink"
_KIND_HIGHLIGHTER = "highlighter"
_KIND_SHAPE = "shape"
_KIND_NOTE = "note"
_KINDS = (_KIND_INK, _KIND_HIGHLIGHTER, _KIND_SHAPE, _KIND_NOTE)

_COMMON_KEYS = ("page", "kind", "data")
_INK_KEYS = ("points", "color", "width_mm", "opacity", "pressures",
             "times_ms", "pressure_enabled", "pressure_profile")
_HIGHLIGHTER_KEYS = ("points", "color", "width_mm", "opacity",
                     "text_assist", "pressures")
# M7.1.4: payloads copied before pressure support carried no ``pressures``
# key; decoding still accepts that exact legacy shape so a clipboard
# payload from an older run keeps working.
_HIGHLIGHTER_KEYS_LEGACY = ("points", "color", "width_mm", "opacity",
                            "text_assist")
_SHAPE_KEYS = ("kind", "rect", "color", "width_mm", "opacity",
               "fill_enabled", "fill_color", "fill_opacity", "line_style",
               "start_ending", "end_ending", "points", "close_path",
               "rotation_deg")
_NOTE_KEYS = ("position", "text", "color")
_DATA_KEYS = {
    _KIND_INK: _INK_KEYS,
    _KIND_HIGHLIGHTER: _HIGHLIGHTER_KEYS,
    _KIND_SHAPE: _SHAPE_KEYS,
    _KIND_NOTE: _NOTE_KEYS,
}


class ClipboardError(ValueError):
    """A payload, record, or paste target is invalid (never partial)."""


# --------------------------------------------------------------- validation

def _fail(what: str, detail: str = "") -> "ClipboardError":
    tail = f" ({detail})" if detail else ""
    return ClipboardError(f"{what}{tail}")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _number(value, what: str) -> float:
    """Finite float, else :class:`ClipboardError`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(what, "expected a number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise _fail(what, "not a number")
    if not math.isfinite(number):
        raise _fail(what, "must be finite")
    return number


def _bounded(value, what: str, lo: float, hi: float) -> float:
    number = _number(value, what)
    if not lo <= number <= hi:
        raise _fail(what, f"must be within [{lo}, {hi}]")
    return number


def _point(value, what: str) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _fail(what, "expected a [x, y] pair")
    x = _bounded(value[0], f"{what}.x", -MAX_COORD, MAX_COORD)
    y = _bounded(value[1], f"{what}.y", -MAX_COORD, MAX_COORD)
    return Point(x, y)


def _points(value, what: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise _fail(what, "expected a list of points")
    if len(value) > MAX_POINTS:
        raise _fail(what, f"too many points ({len(value)} > {MAX_POINTS})")
    return tuple(_point(item, f"{what}[{index}]")
                 for index, item in enumerate(value))


def _color(value, what: str) -> str:
    if not isinstance(value, str) or not _MAX_COLOR.match(value):
        raise _fail(what, "expected a #hex color")
    return value


def _boolean(value, what: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(what, "expected a boolean")
    return value


def _integer(value, what: str, lo: int, hi: int) -> int:
    if not _is_int(value):
        raise _fail(what, "expected an integer")
    if not lo <= value <= hi:
        raise _fail(what, f"must be within [{lo}, {hi}]")
    return int(value)


def _text(value, what: str) -> str:
    if not isinstance(value, str):
        raise _fail(what, "expected text")
    if len(value) > MAX_TEXT:
        raise _fail(what, f"longer than {MAX_TEXT} characters")
    if "\x00" in value:
        raise _fail(what, "contains a NUL character")
    return value


def _exact_keys(data, keys, what: str) -> None:
    if not isinstance(data, dict):
        raise _fail(what, "expected an object")
    expected = set(keys)
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"unsupported {extra}")
        raise _fail(what, ", ".join(detail))


def _style(value, what: str, allowed: tuple) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _fail(what, f"expected one of {allowed}")
    return value


# ----------------------------------------------------------------- payload

@dataclass(frozen=True, slots=True)
class ClipboardRecord:
    """One copied annotation: source page index + encoded data."""

    page: int
    kind: str
    data: dict


@dataclass(frozen=True, slots=True)
class ClipboardPayload:
    """A validated, versioned annotation payload.

    ``records`` are in document order (source page ascending, store
    insertion order within a page) and ``anchor_page`` is the lowest
    source page index, so ``record.page - anchor_page`` is the relative
    page offset a paste re-lays onto its destination.
    """

    records: tuple = ()
    anchor_page: int = 0

    @property
    def page_span(self) -> int:
        """How many relative destination pages the group needs."""
        if not self.records:
            return 0
        return max(record.page for record in self.records) \
            - int(self.anchor_page) + 1

    @property
    def count(self) -> int:
        """Number of annotations in the payload."""
        return len(self.records)

    def to_bytes(self) -> bytes:
        """Serialize to the versioned wire format (UTF-8 JSON)."""
        document = {
            "format": FORMAT_TAG,
            "version": FORMAT_VERSION,
            "anchor_page": int(self.anchor_page),
            "records": [
                {"page": int(r.page), "kind": r.kind, "data": r.data}
                for r in self.records
            ],
        }
        return json.dumps(document, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False
                          ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data) -> "ClipboardPayload":
        """Parse + fully validate a payload; raises :class:`ClipboardError`.

        Never raises anything else: a truncated byte string, invalid
        UTF-8, non-object JSON, wrong version, unknown kind, missing or
        extra field, non-finite number, out-of-range value, and oversized
        array are all reported as a :class:`ClipboardError` so callers can
        treat "not our payload" uniformly.
        """
        try:
            if isinstance(data, memoryview):
                data = data.tobytes()
            if isinstance(data, bytearray):
                data = bytes(data)
            if not isinstance(data, (bytes, str)):
                raise _fail("payload", "expected bytes")
            if len(data) > MAX_BYTES:
                raise _fail("payload", f"larger than {MAX_BYTES} bytes")
            text = data.decode("utf-8") if isinstance(data, bytes) else data
            document = json.loads(text)
        except ClipboardError:
            raise
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            raise _fail("payload", f"not valid JSON ({exc})") from None
        if not isinstance(document, dict):
            raise _fail("payload", "expected a JSON object")
        _exact_keys(document, ("format", "version", "anchor_page",
                               "records"), "payload")
        if document["format"] != FORMAT_TAG:
            raise _fail("payload", "not an Inkfall annotation payload")
        version = document["version"]
        if not _is_int(version) or version != FORMAT_VERSION:
            raise _fail("payload", f"unsupported version {version!r}")
        anchor = _integer(document["anchor_page"], "payload.anchor_page",
                          0, 10 ** 6)
        raw_records = document["records"]
        if not isinstance(raw_records, list) or not raw_records:
            raise _fail("payload.records", "expected a non-empty list")
        if len(raw_records) > MAX_RECORDS:
            raise _fail("payload.records",
                        f"too many records ({len(raw_records)} > "
                        f"{MAX_RECORDS})")
        records = []
        previous_page = -1
        for index, raw in enumerate(raw_records):
            record = _parse_record(raw, f"record[{index}]")
            if record.page < previous_page:
                raise _fail(f"record[{index}].page",
                            "records must be in document order")
            previous_page = record.page
            records.append(record)
        if records[0].page != anchor:
            raise _fail("payload.anchor_page",
                        "must equal the lowest record page")
        return cls(records=tuple(records), anchor_page=anchor)


def _parse_record(raw, what: str) -> ClipboardRecord:
    _exact_keys(raw, _COMMON_KEYS, what)
    page = _integer(raw["page"], f"{what}.page", 0, 10 ** 6)
    kind = raw["kind"]
    if kind not in _KINDS:
        raise _fail(f"{what}.kind", f"unsupported type {kind!r}")
    data = raw["data"]
    _exact_keys(data, _DATA_KEYS[kind], f"{what}.data")
    # Decode once here: a record that cannot build a model object is
    # rejected before any caller can plan an insertion.
    _decode(kind, data, f"{what}.data", page_id=None)
    return ClipboardRecord(page=page, kind=kind,
                           data={key: data[key] for key in _DATA_KEYS[kind]})


# ------------------------------------------------------------- encoding

def _encode_points(points) -> list:
    encoded = []
    for index, point in enumerate(points):
        x = _bounded(getattr(point, "x", None), f"point[{index}].x",
                     -MAX_COORD, MAX_COORD)
        y = _bounded(getattr(point, "y", None), f"point[{index}].y",
                     -MAX_COORD, MAX_COORD)
        encoded.append([x, y])
    return encoded


def _encode(annotation) -> tuple:
    """``(kind, data)`` for one annotation; raises for unsupported types."""
    if isinstance(annotation, InkStroke):
        points = tuple(getattr(annotation, "points", ()))
        if not points:
            raise _fail("ink stroke", "has no points")
        pressures = tuple(getattr(annotation, "pressures", ()) or ())
        times = tuple(getattr(annotation, "times_ms", ()) or ())
        if pressures and len(pressures) != len(points):
            raise _fail("ink stroke", "pressures do not match points")
        if times and len(times) != len(points):
            raise _fail("ink stroke", "times_ms do not match points")
        profile = str(getattr(annotation, "pressure_profile", "normal"))
        if profile not in PRESSURE_PROFILES:
            profile = "normal"
        data = {
            "points": _encode_points(points),
            "color": _color(getattr(annotation, "color", None), "color"),
            "width_mm": _bounded(getattr(annotation, "width_mm", None),
                                 "width_mm", 0.0, MAX_WIDTH_MM),
            "opacity": _bounded(getattr(annotation, "opacity", None),
                                "opacity", 0.0, 1.0),
            "pressures": [_bounded(value, "pressure", 0.0, 1.0)
                          for value in pressures],
            "times_ms": [_integer(value, "times_ms", 0, 2 ** 53)
                         for value in times],
            "pressure_enabled": _boolean(
                getattr(annotation, "pressure_enabled", False),
                "pressure_enabled"),
            "pressure_profile": profile,
        }
        return _KIND_INK, data
    if isinstance(annotation, Highlighter):
        points = tuple(getattr(annotation, "points", ()))
        if not points:
            raise _fail("highlighter", "has no points")
        assist = getattr(annotation, "text_assist", None)
        if assist is None:
            encoded_assist = None
        else:
            encoded_assist = {
                "char_count": _integer(getattr(assist, "char_count", 0),
                                       "text_assist.char_count", 0, 10 ** 7),
                "source": _text(getattr(assist, "source", ""),
                                "text_assist.source"),
            }
        pressures = tuple(getattr(annotation, "pressures", ()) or ())
        if pressures and len(pressures) != len(points):
            raise _fail("highlighter", "pressures do not match points")
        data = {
            "points": _encode_points(points),
            "color": _color(getattr(annotation, "color", None), "color"),
            "width_mm": _bounded(getattr(annotation, "width_mm", None),
                                 "width_mm", 0.0, MAX_WIDTH_MM),
            "opacity": _bounded(getattr(annotation, "opacity", None),
                                "opacity", 0.0, 1.0),
            "text_assist": encoded_assist,
            "pressures": [_bounded(value, "pressure", 0.0, 1.0)
                          for value in pressures],
        }
        return _KIND_HIGHLIGHTER, data
    if isinstance(annotation, Shape):
        try:
            kind = ShapeKind(getattr(annotation, "kind", None)).value
        except (TypeError, ValueError):
            raise _fail("shape", "unknown shape kind")
        rect = getattr(annotation, "rect", None)
        if rect is None:
            raise _fail("shape", "has no rect")
        data = {
            "kind": kind,
            "rect": [
                _bounded(getattr(rect, "x0", None), "rect.x",
                         -MAX_COORD, MAX_COORD),
                _bounded(getattr(rect, "y0", None), "rect.y",
                         -MAX_COORD, MAX_COORD),
                _bounded(getattr(rect, "width", None), "rect.width",
                         0.0, MAX_COORD),
                _bounded(getattr(rect, "height", None), "rect.height",
                         0.0, MAX_COORD),
            ],
            "color": _color(getattr(annotation, "color", None), "color"),
            "width_mm": _bounded(getattr(annotation, "width_mm", None),
                                 "width_mm", 0.0, MAX_WIDTH_MM),
            "opacity": _bounded(getattr(annotation, "opacity", None),
                                "opacity", 0.0, 1.0),
            "fill_enabled": _boolean(
                getattr(annotation, "fill_enabled", False), "fill_enabled"),
            "fill_color": _color(getattr(annotation, "fill_color", None),
                                 "fill_color"),
            "fill_opacity": _bounded(
                getattr(annotation, "fill_opacity", 1.0),
                "fill_opacity", 0.0, 1.0),
            "line_style": _style(getattr(annotation, "line_style", None),
                                 "line_style", LINE_STYLES),
            "start_ending": _style(getattr(annotation, "start_ending", None),
                                   "start_ending", LINE_ENDINGS),
            "end_ending": _style(getattr(annotation, "end_ending", None),
                                 "end_ending", LINE_ENDINGS),
            "points": _encode_points(tuple(getattr(annotation, "points", ()))),
            "close_path": _boolean(getattr(annotation, "close_path", False),
                                   "close_path"),
            "rotation_deg": _bounded(
                getattr(annotation, "rotation_deg", 0.0), "rotation_deg",
                -1.0e6, 1.0e6),
        }
        return _KIND_SHAPE, data
    if isinstance(annotation, StickyNote):
        position = getattr(annotation, "position", None)
        if position is None:
            raise _fail("note", "has no position")
        data = {
            "position": [
                _bounded(getattr(position, "x", None), "position.x",
                         -MAX_COORD, MAX_COORD),
                _bounded(getattr(position, "y", None), "position.y",
                         -MAX_COORD, MAX_COORD),
            ],
            "text": _text(getattr(annotation, "text", ""), "text"),
            "color": _color(getattr(annotation, "color", None), "color"),
        }
        return _KIND_NOTE, data
    name = type(annotation).__name__
    raise ClipboardError(f"annotation type {name} cannot be copied")


def payload_from_annotations(items) -> ClipboardPayload:
    """Build a validated payload from ``(page_index, annotation)`` pairs.

    The caller passes its *current* selection resolved against the store,
    in document order. Records are stably sorted by source page (the
    within-page order is preserved), unsupported/invalid members fail the
    whole copy, and the produced bytes are re-parsed through
    :meth:`ClipboardPayload.from_bytes` so the wire format itself is what
    has been validated.
    """
    try:
        pairs = list(items)
    except TypeError:
        raise ClipboardError("selection must be a sequence")
    if not pairs:
        raise ClipboardError("nothing is selected")
    if len(pairs) > MAX_RECORDS:
        raise ClipboardError(f"selection too large ({len(pairs)} records)")
    entries = []
    seen_ids = set()
    seen_pages = []
    for position, item in enumerate(pairs):
        try:
            page_index, annotation = item
        except (TypeError, ValueError):
            raise ClipboardError(
                "selection entries must be (page_index, annotation) pairs")
        if not isinstance(annotation, AnnotationBase):
            raise ClipboardError("selection contains a non-annotation value")
        page = _integer(page_index, "page_index", 0, 10 ** 6)
        key = str(getattr(annotation.id, "value", annotation.id))
        if key in seen_ids:
            raise ClipboardError("selection contains a duplicate identity")
        seen_ids.add(key)
        kind, data = _encode(annotation)
        entries.append((page, position, kind, data))
        seen_pages.append(page)
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    document = {
        "format": FORMAT_TAG,
        "version": FORMAT_VERSION,
        "anchor_page": int(min(seen_pages)),
        "records": [{"page": page, "kind": kind, "data": data}
                    for page, _position, kind, data in entries],
    }
    try:
        wire = json.dumps(document, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False
                          ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClipboardError(f"payload is not serializable ({exc})") from None
    return ClipboardPayload.from_bytes(wire)


# --------------------------------------------------------------- decoding

def _decode(kind: str, data: dict, what: str, page_id):
    """Build one fresh annotation from validated record data.

    A fresh :class:`~core.types.AnnotationId` is always generated (source
    identities are never reused); ``page_id`` is the destination page's
    identity (``None`` only for the validation pass). ``created_at`` is
    *not* copied: a pasted copy is a new object and gets a fresh
    timestamp, exactly like a newly drawn annotation.
    """
    keys = _DATA_KEYS[kind]
    if (kind == _KIND_HIGHLIGHTER and isinstance(data, dict)
            and set(data) == set(_HIGHLIGHTER_KEYS_LEGACY)):
        keys = _HIGHLIGHTER_KEYS_LEGACY
    _exact_keys(data, keys, what)
    created = None
    if kind == _KIND_INK:
        points = _points(data["points"], f"{what}.points")
        if not points:
            raise _fail(f"{what}.points", "a stroke needs at least one point")
        pressures = data["pressures"]
        times = data["times_ms"]
        if not isinstance(pressures, list) or not isinstance(times, list):
            raise _fail(what, "pressures/times_ms must be lists")
        if pressures and len(pressures) != len(points):
            raise _fail(f"{what}.pressures", "must match points")
        if times and len(times) != len(points):
            raise _fail(f"{what}.times_ms", "must match points")
        kwargs = dict(
            points=points,
            color=_color(data["color"], f"{what}.color"),
            width_mm=_bounded(data["width_mm"], f"{what}.width_mm",
                              0.0, MAX_WIDTH_MM),
            opacity=_bounded(data["opacity"], f"{what}.opacity", 0.0, 1.0),
            pressures=tuple(_bounded(value, f"{what}.pressures",
                                     0.0, 1.0) for value in pressures),
            times_ms=tuple(_integer(value, f"{what}.times_ms", 0, 2 ** 53)
                           for value in times),
            pressure_enabled=_boolean(data["pressure_enabled"],
                                      f"{what}.pressure_enabled"),
            pressure_profile=_style(data["pressure_profile"],
                                    f"{what}.pressure_profile",
                                    PRESSURE_PROFILES),
        )
        factory = InkStroke
    elif kind == _KIND_HIGHLIGHTER:
        points = _points(data["points"], f"{what}.points")
        if not points:
            raise _fail(f"{what}.points",
                        "a highlighter needs at least one point")
        assist = data["text_assist"]
        if assist is None:
            encoded_assist = None
        else:
            _exact_keys(assist, ("char_count", "source"),
                        f"{what}.text_assist")
            encoded_assist = TextAssist(
                char_count=_integer(assist["char_count"],
                                    f"{what}.text_assist.char_count",
                                    0, 10 ** 7),
                source=_text(assist["source"],
                             f"{what}.text_assist.source"))
        kwargs = dict(
            points=points,
            color=_color(data["color"], f"{what}.color"),
            width_mm=_bounded(data["width_mm"], f"{what}.width_mm",
                              0.0, MAX_WIDTH_MM),
            opacity=_bounded(data["opacity"], f"{what}.opacity", 0.0, 1.0),
            text_assist=encoded_assist,
        )
        pressures = data.get("pressures", [])
        if not isinstance(pressures, list):
            raise _fail(f"{what}.pressures", "must be a list")
        if pressures and len(pressures) != len(points):
            raise _fail(f"{what}.pressures", "must match points")
        kwargs["pressures"] = tuple(
            _bounded(value, f"{what}.pressures", 0.0, 1.0)
            for value in pressures)
        factory = Highlighter
    elif kind == _KIND_SHAPE:
        try:
            shape_kind = ShapeKind(data["kind"])
        except (TypeError, ValueError):
            raise _fail(f"{what}.kind", "unknown shape kind") from None
        raw_rect = data["rect"]
        if not isinstance(raw_rect, (list, tuple)) or len(raw_rect) != 4:
            raise _fail(f"{what}.rect", "expected [x, y, width, height]")
        rect = Rect(
            Point(_bounded(raw_rect[0], f"{what}.rect.x",
                           -MAX_COORD, MAX_COORD),
                  _bounded(raw_rect[1], f"{what}.rect.y",
                           -MAX_COORD, MAX_COORD)),
            Size(_bounded(raw_rect[2], f"{what}.rect.width",
                          0.0, MAX_COORD),
                 _bounded(raw_rect[3], f"{what}.rect.height",
                          0.0, MAX_COORD)))
        kwargs = dict(
            kind=shape_kind,
            rect=rect,
            color=_color(data["color"], f"{what}.color"),
            width_mm=_bounded(data["width_mm"], f"{what}.width_mm",
                              0.0, MAX_WIDTH_MM),
            opacity=_bounded(data["opacity"], f"{what}.opacity", 0.0, 1.0),
            fill_enabled=_boolean(data["fill_enabled"],
                                  f"{what}.fill_enabled"),
            fill_color=_color(data["fill_color"], f"{what}.fill_color"),
            fill_opacity=_bounded(data["fill_opacity"],
                                  f"{what}.fill_opacity", 0.0, 1.0),
            line_style=_style(data["line_style"], f"{what}.line_style",
                              LINE_STYLES),
            start_ending=_style(data["start_ending"],
                                f"{what}.start_ending", LINE_ENDINGS),
            end_ending=_style(data["end_ending"], f"{what}.end_ending",
                              LINE_ENDINGS),
            points=_points(data["points"], f"{what}.points"),
            close_path=_boolean(data["close_path"], f"{what}.close_path"),
            rotation_deg=_bounded(data["rotation_deg"],
                                  f"{what}.rotation_deg", -1.0e6, 1.0e6),
        )
        if shape_kind in (ShapeKind.LINE, ShapeKind.ARROW) \
                and len(kwargs["points"]) != 2:
            raise _fail(f"{what}.points",
                        "a line/arrow needs exactly two points")
        factory = Shape
    else:
        position = _point(data["position"], f"{what}.position")
        kwargs = dict(
            position=position,
            text=_text(data["text"], f"{what}.text"),
            color=_color(data["color"], f"{what}.color"),
        )
        factory = StickyNote
    annotation = factory(**kwargs)
    if page_id is not None:
        annotation = _with_identity(annotation, page_id)
    return annotation


def _with_identity(annotation, page_id):
    """Attach a fresh identity and the destination page identity."""
    return dataclasses.replace(annotation, id=AnnotationId(),
                               page_id=page_id)


def _translate(annotation, dx: float, dy: float):
    """Translate one decoded annotation by page-pt ``(dx, dy)``.

    Reuses the shared move helper for every transformable type (it keeps
    identity, page, and every unrelated field) and handles the note model
    the shared helper deliberately does not transform.
    """
    if not dx and not dy:
        return annotation
    moved = move_annotation(annotation, dx, dy)
    if moved is not None:
        return moved
    position = getattr(annotation, "position", None)
    if isinstance(annotation, StickyNote) and position is not None:
        return dataclasses.replace(
            annotation,
            position=Point(float(position.x) + float(dx),
                           float(position.y) + float(dy)))
    return None


# ------------------------------------------------------------ paste plan

def paste_plan(payload: ClipboardPayload, *, anchor_page: int,
               page_count: int, page_id_for_index,
               offset=(0.0, 0.0)) -> tuple:
    """Build the complete insertion plan: ``((page_index, annotation), ...)``.

    ``anchor_page`` is the destination page the pointer/menu targets; the
    payload's source pages map to ``anchor_page + (record.page -
    payload.anchor_page)``. Every destination page is range-checked and
    every candidate is constructed (fresh identity, destination page id,
    translated geometry) *before* the tuple is returned, so a rejected
    payload/target raises :class:`ClipboardError` with nothing inserted.

    ``offset`` is a single page-pt translation applied to every member.
    Translation is offset-independent across pages (the per-page widget
    offset cancels), so the group keeps its internal layout, its
    within-page ordering, and its relative page spread exactly.
    """
    if not isinstance(payload, ClipboardPayload):
        raise ClipboardError("not a clipboard payload")
    if not payload.records:
        raise ClipboardError("the payload is empty")
    anchor = _integer(anchor_page, "anchor_page", 0, 10 ** 6)
    count = _integer(page_count, "page_count", 0, 10 ** 7)
    try:
        dx, dy = offset
        dx = _bounded(dx, "offset.dx", -MAX_COORD, MAX_COORD)
        dy = _bounded(dy, "offset.dy", -MAX_COORD, MAX_COORD)
    except (TypeError, ValueError):
        raise _fail("offset", "expected a finite (dx, dy) pair") from None
    if not callable(page_id_for_index):
        raise ClipboardError("page_id_for_index must be callable")
    base = int(payload.anchor_page)
    span = payload.page_span
    if span < 1:
        raise ClipboardError("the payload is empty")
    if anchor + span - 1 >= count:
        raise ClipboardError(
            "the copied group needs "
            f"{span} page(s) from page {anchor + 1}, but the document "
            f"has {count}")
    plan = []
    seen = set()
    for index, record in enumerate(payload.records):
        destination = anchor + (int(record.page) - base)
        if not 0 <= destination < count:
            raise ClipboardError(
                f"record {index} maps to page {destination} "
                f"(out of range)")
        try:
            page_id = page_id_for_index(destination)
        except Exception as exc:
            raise ClipboardError(
                f"cannot derive page identity for page {destination} "
                f"({exc})") from None
        if not isinstance(page_id, PageId):
            raise ClipboardError("page identity must be a PageId")
        annotation = _decode(record.kind, record.data,
                             f"record[{index}].data", page_id=page_id)
        moved = _translate(annotation, dx, dy)
        if moved is None:
            raise ClipboardError(
                f"record {index} cannot be placed (unsupported geometry)")
        key = str(moved.id.value)
        if key in seen:  # pragma: no cover - uuid4 collision guard
            raise ClipboardError("generated a duplicate identity")
        seen.add(key)
        plan.append((destination, moved))
    return tuple(plan)
