"""Display-time preview compositing helpers.

PDF pages do not have to paint a background: a blank page renders with
fully transparent pixels.  Every preview surface (page-navigation
thumbnails, Home Library covers, any future thumbnail consumer) shows
such a page on white paper instead of letting it disappear into the
surrounding UI.

The backing is applied to the *display/cache copy* only.  PDF pixels,
annotations, saved documents and exported files are never touched, and
the raw preview contract in :mod:`document.backend` keeps returning RGBA
bytes.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QImage, QPainter

__all__ = ["PREVIEW_PAPER", "flatten_on_white"]

#: Preview paper.  Deliberately independent of the viewer's read-mode
#: colours: previews are small paper facsimiles, not the reading canvas.
PREVIEW_PAPER = QColor("#ffffff")


def flatten_on_white(image: QImage) -> QImage:
    """Return ``image`` composited over opaque white.

    A null image is returned unchanged.  Images without an alpha channel
    are returned unchanged (no copy, no repaint): they already have an
    opaque backing.  RGBA images are composited once into a detached
    RGBA8888 buffer, preserving the RGB of every opaque pixel exactly.
    """
    if image is None or image.isNull():
        return image
    try:
        if not image.hasAlphaChannel():
            return image
    except Exception:
        return image
    try:
        result = QImage(image.size(), QImage.Format.Format_RGBA8888)
        if result.isNull():
            return image
        result.fill(PREVIEW_PAPER)
        painter = QPainter(result)
        try:
            painter.drawImage(0, 0, image)
        finally:
            painter.end()
        try:
            result.setDevicePixelRatio(image.devicePixelRatio())
        except Exception:
            pass
        return result
    except Exception:
        # A preview must never fail to display because backing failed.
        return image
