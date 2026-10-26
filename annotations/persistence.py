"""Versioned on-disk annotation manifest for Inkfall PDFs.

The manifest is the authoritative record of Inkfall-created annotations.
Native PDF annotations are written alongside it for external viewers, but
they cannot carry the full Inkfall model (pressure history, exact styling,
stable identity, ordering, timestamps). This module owns the manifest
format, its bounds, and its validation.

The clipboard codec remains the single field validator: this module imports
its private encode/decode helpers and only adds the persistence envelope
(identity, page, order, timestamp). Clipboard behavior and its wire format
are unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import math

from annotations import clipboard as _clip
from annotations.models import AnnotationBase
from core.types import AnnotationId

__all__ = [
    "EMBEDDED_NAME",
    "MANIFEST_FORMAT",
    "MANIFEST_VERSION",
    "MAX_MANIFEST_BYTES",
    "MAX_MANIFEST_RECORDS",
    "ManifestError",
    "ManifestUnsupportedVersion",
    "decode_manifest",
    "encode_manifest",
]

#: Constant embedded-file name. Updating this one entry never disturbs
#: unrelated PDF attachments.
EMBEDDED_NAME = "inkfall.annotations.v1.json"

MANIFEST_FORMAT = "inkfall-document-annotations"
MANIFEST_VERSION = 1

#: Bounds checked before and after JSON parsing.
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_RECORDS = 20000
_MAX_ID_LEN = 256
_MAX_CREATED = 1.0e15


class ManifestError(ValueError):
    """The manifest is absent, malformed, or unsafe to modify."""


class ManifestUnsupportedVersion(ManifestError):
    """The manifest was written by a newer Inkfall schema."""

    def __init__(self, version) -> None:
        self.version = version
        super().__init__(
            f"unsupported Inkfall annotation manifest version {version!r}")


def _identity_text(annotation_id) -> str:
    try:
        value = annotation_id.value
    except AttributeError:
        value = annotation_id
    text = str(value)
    if not text or len(text) > _MAX_ID_LEN or "\x00" in text:
        raise ManifestError("invalid annotation identity")
    return text


def _created_at(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestError("invalid annotation created_at")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= _MAX_CREATED:
        raise ManifestError("invalid annotation created_at")
    return number


def _exact_keys(data, keys, what: str) -> None:
    if not isinstance(data, dict):
        raise ManifestError(f"{what} must be an object")
    expected = set(keys)
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"unsupported {extra}")
        raise ManifestError(f"{what}: {', '.join(parts)}")


def encode_manifest(store, page_count: int) -> bytes:
    """Serialize a complete annotation store into the manifest bytes.

    Raises :class:`ManifestError` instead of writing a partial or lossy
    manifest. The caller must treat that as a failed save.
    """
    if isinstance(page_count, bool) or not isinstance(page_count, int) \
            or page_count < 0:
        raise ManifestError("invalid manifest page count")
    try:
        store_pages = int(store.page_count)
    except Exception:
        store_pages = page_count
    if store_pages != page_count:
        raise ManifestError("annotation store page count does not match PDF")

    records = []
    for page in range(page_count):
        try:
            annotations = list(store.page(page))
        except Exception as exc:
            raise ManifestError(
                f"cannot read annotations on page {page}: {exc}") from exc
        for order, annotation in enumerate(annotations):
            if not isinstance(annotation, AnnotationBase):
                raise ManifestError(
                    f"page {page} order {order}: non-annotation value")
            try:
                kind, data = _clip._encode(annotation)
            except Exception as exc:
                raise ManifestError(
                    f"page {page} order {order}: {exc}") from exc
            records.append({
                "page": int(page),
                "order": int(order),
                "id": _identity_text(annotation.id),
                "created_at": _created_at(
                    getattr(annotation, "created_at", 0.0)),
                "kind": kind,
                "data": data,
            })

    if len(records) > MAX_MANIFEST_RECORDS:
        raise ManifestError(
            f"too many annotations for the manifest "
            f"({len(records)} > {MAX_MANIFEST_RECORDS})")

    document = {
        "format": MANIFEST_FORMAT,
        "version": MANIFEST_VERSION,
        "page_count": int(page_count),
        "records": records,
    }
    try:
        payload = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"manifest is not serializable: {exc}") from exc
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    return payload


def decode_manifest(data, page_count: int, page_id_for_index):
    """Parse and fully validate a manifest.

    Returns ``((page_index, annotation), ...)`` sorted by page and order.
    Every record is validated before any annotation is returned, so a
    caller never restores a partial manifest. Unsupported versions and
    unsafe manifests raise :class:`ManifestError`.
    """
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, bytearray):
        data = bytes(data)
    if not isinstance(data, (bytes, str)):
        raise ManifestError("manifest payload must be bytes or text")
    if isinstance(page_count, bool) or not isinstance(page_count, int) \
            or page_count < 0:
        raise ManifestError("invalid manifest page count")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestError("manifest is too large")
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        document = json.loads(text)
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestError("manifest must be a JSON object")
    _exact_keys(document, ("format", "version", "page_count", "records"),
                "manifest")
    if document["format"] != MANIFEST_FORMAT:
        raise ManifestError("not an Inkfall annotation manifest")
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestError("manifest version must be an integer")
    if version != MANIFEST_VERSION:
        raise ManifestUnsupportedVersion(version)
    stored_pages = document["page_count"]
    if isinstance(stored_pages, bool) or not isinstance(stored_pages, int):
        raise ManifestError("manifest page_count must be an integer")
    if stored_pages != int(page_count):
        raise ManifestError(
            "manifest page count does not match the PDF; refusing to edit "
            "annotation data that cannot be safely reconciled")
    raw_records = document["records"]
    if not isinstance(raw_records, list):
        raise ManifestError("manifest records must be a list")
    if len(raw_records) > MAX_MANIFEST_RECORDS:
        raise ManifestError("manifest contains too many records")
    if not callable(page_id_for_index):
        raise ManifestError("page identity factory is not callable")

    seen_ids = set()
    seen_orders = set()
    decoded = []
    for index, raw in enumerate(raw_records):
        what = f"record[{index}]"
        _exact_keys(raw, ("page", "order", "id", "created_at", "kind",
                          "data"), what)
        page = raw["page"]
        order = raw["order"]
        if isinstance(page, bool) or not isinstance(page, int):
            raise ManifestError(f"{what}.page must be an integer")
        if not 0 <= page < int(page_count):
            raise ManifestError(f"{what}.page is outside the PDF")
        if isinstance(order, bool) or not isinstance(order, int) or order < 0:
            raise ManifestError(f"{what}.order must be a non-negative integer")
        if (page, order) in seen_orders:
            raise ManifestError(f"{what} duplicates a page order")
        seen_orders.add((page, order))
        identity = raw["id"]
        if not isinstance(identity, str) or not identity \
                or len(identity) > _MAX_ID_LEN or "\x00" in identity:
            raise ManifestError(f"{what}.id is invalid")
        if identity in seen_ids:
            raise ManifestError(f"{what}.id duplicates another annotation")
        seen_ids.add(identity)
        created = _created_at(raw["created_at"])
        kind = raw["kind"]
        if kind not in _clip._KINDS:
            raise ManifestError(f"{what}.kind is unsupported")
        data_obj = raw["data"]
        try:
            annotation = _clip._decode(
                kind, data_obj, f"{what}.data",
                page_id=page_id_for_index(page))
        except Exception as exc:
            raise ManifestError(f"{what}: {exc}") from exc
        annotation = dataclasses.replace(
            annotation,
            id=AnnotationId(identity),
            page_id=page_id_for_index(page),
            created_at=created,
        )
        decoded.append((page, order, index, annotation))

    decoded.sort(key=lambda item: (item[0], item[1], item[2]))
    return tuple((page, annotation) for page, _order, _index, annotation
                 in decoded)
