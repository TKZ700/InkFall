"""Per-document reading-position persistence (QSettings-backed).

The storage key is a hash of the document's stable registry identity, not
the temporary render working copy. A blank workspace keeps its synthetic
key until the first save; the caller migrates the entry after the document
adopts a real path.
"""

from __future__ import annotations

import hashlib
import json
import math

from PySide6.QtCore import QSettings

__all__ = ["ReadingPositionStore"]

_PREFIX = "reading-position/v1/"
_VERSION = 1


def _finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp_unit(value) -> float:
    return max(0.0, min(1.0, _finite(value)))


class ReadingPositionStore:
    """Load/save one bounded payload per stable document key."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings if settings is not None else QSettings()

    @staticmethod
    def _key(document_key: str) -> str:
        digest = hashlib.sha256(
            str(document_key).encode("utf-8", "surrogatepass")).hexdigest()
        return _PREFIX + digest

    def load(self, document_key: str) -> dict | None:
        if not document_key:
            return None
        try:
            raw = self._settings.value(self._key(document_key), "")
        except Exception:
            return None
        if not raw:
            return None
        try:
            payload = json.loads(str(raw))
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != _VERSION:
            return None
        page = payload.get("page")
        page_count = payload.get("page_count")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            return None
        if isinstance(page_count, bool) or not isinstance(page_count, int) \
                or page_count < 1:
            return None
        if page >= page_count:
            return None
        zoom = _finite(payload.get("zoom"), 1.0)
        if not 0.05 <= zoom <= 16.0:
            return None
        return {
            "page": page,
            "page_count": page_count,
            "fraction": _clamp_unit(payload.get("fraction")),
            "x_fraction": _clamp_unit(payload.get("x_fraction")),
            "zoom": zoom,
        }

    def save(self, document_key: str, payload: dict) -> None:
        if not document_key or not isinstance(payload, dict):
            return
        page = payload.get("page")
        page_count = payload.get("page_count")
        if isinstance(page, bool) or not isinstance(page, int) or page < 0:
            return
        if isinstance(page_count, bool) or not isinstance(page_count, int) \
                or page_count < 1 or page >= page_count:
            return
        zoom = _finite(payload.get("zoom"), 1.0)
        if not 0.05 <= zoom <= 16.0:
            zoom = 1.0
        data = {
            "version": _VERSION,
            "page": page,
            "page_count": page_count,
            "fraction": _clamp_unit(payload.get("fraction")),
            "x_fraction": _clamp_unit(payload.get("x_fraction")),
            "zoom": zoom,
        }
        try:
            self._settings.setValue(
                self._key(document_key),
                json.dumps(data, separators=(",", ":")))
            self._settings.sync()
        except Exception:
            pass

    def remove(self, document_key: str) -> None:
        if not document_key:
            return
        try:
            self._settings.remove(self._key(document_key))
            self._settings.sync()
        except Exception:
            pass
