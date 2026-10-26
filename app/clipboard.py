"""System-clipboard transport for Inkfall annotation payloads (M6.8.2).

The *format* lives in the Qt-free :mod:`annotations.clipboard`; this
module is the only place that touches Qt's clipboard. It is deliberately
tiny and total: every entry point returns ``False``/``None`` instead of
raising, so a headless session, a platform without a clipboard service,
or a clipboard owned by another application can never break Copy, Cut,
Paste, the context menu, or the keyboard path.

Only the dedicated Inkfall MIME type is read or written. Paste never
mutates the clipboard, and Copy/Cut publish exactly one validated
payload (no bitmap, screenshot, HTML, or other unrelated content is
synthesized).
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QMimeData
from PySide6.QtGui import QGuiApplication

from annotations import clipboard as payload_format

__all__ = ["has_payload", "read_payload", "write_payload"]


def _clipboard():
    """The application clipboard, or ``None`` when unavailable."""
    try:
        return QGuiApplication.clipboard()
    except Exception:
        return None


def read_payload():
    """The validated Inkfall payload currently on the clipboard, or None.

    ``None`` covers every non-Inkfall case uniformly: no clipboard, an
    empty clipboard, unrelated MIME content, and malformed/unsupported/
    version-incompatible payloads. Never raises and never mutates.
    """
    try:
        board = _clipboard()
        if board is None:
            return None
        mime = board.mimeData()
        if mime is None or not mime.hasFormat(payload_format.MIME_TYPE):
            return None
        raw = bytes(mime.data(payload_format.MIME_TYPE))
        if len(raw) > payload_format.MAX_BYTES:
            return None
        return payload_format.ClipboardPayload.from_bytes(raw)
    except Exception:
        return None


def has_payload() -> bool:
    """True when Paste would have a valid, pasteable payload."""
    return read_payload() is not None


def write_payload(value) -> bool:
    """Publish one validated payload to the system clipboard.

    Accepts only a :class:`annotations.clipboard.ClipboardPayload` that
    has already been validated by the Qt-free layer (Copy/Cut build it
    with :func:`annotations.clipboard.payload_from_annotations`, which
    round-trips the wire bytes). Returns ``False`` when the clipboard
    refused the data, so the caller can abort a Cut before deleting.
    """
    if not isinstance(value, payload_format.ClipboardPayload):
        return False
    try:
        mime = QMimeData()
        mime.setData(payload_format.MIME_TYPE,
                     QByteArray(value.to_bytes()))
        board = _clipboard()
        if board is None:
            return False
        board.setMimeData(mime)
        return True
    except Exception:
        return False
