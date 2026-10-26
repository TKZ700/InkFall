"""Regenerate ``assets/icons/inkfall.ico`` from the authoritative artwork.

``assets/icons/new_icon.png`` is the single source of truth for Inkfall's
brand mark: the running application loads it for the window, taskbar,
title bar, library header and tool shelf. Windows also needs a
multi-resolution ICO for the executable, the installer and the Desktop/Start
Menu shortcuts, so this script derives one from that PNG **without altering
the source image**.

Usage::

    uv run python scripts/make_icon.py           # rewrite the ICO
    uv run python scripts/make_icon.py --check   # fail if it is stale

The 256 px frame is PNG-compressed (the usual Windows convention); smaller
frames are uncompressed 32-bit BGRA DIBs with an AND mask, which Windows,
the resource compiler and NSIS all understand. Transparency is preserved.

Requires PySide6, which Inkfall already depends on at runtime, so no extra
package is needed and this is not part of the optional ``build`` group.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):  # ``python scripts/make_icon.py``
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402

SOURCE_ROOT = Path(__file__).resolve().parents[1]
MASTER_PNG = SOURCE_ROOT / "assets" / "icons" / "new_icon.png"
ICO_PATH = SOURCE_ROOT / "assets" / "icons" / "inkfall.ico"

#: Frames written into the ICO, ascending. Covers the Windows shell sizes
#: (16 px list view, 20/24/32 px DPI-scaled, 48 px large icons, 64/96 px
#: extra-large, 128/256 px thumbnails and the installer icon).
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

#: Frames above this size are stored PNG-compressed instead of as a DIB.
PNG_MIN_SIZE = 256

#: 96 dpi, written into the PNG frame so output bytes are reproducible
#: regardless of the host display scaling.
DOTS_PER_METRE = 3780

__all__ = ["DOTS_PER_METRE", "ICO_PATH", "MASTER_PNG", "PNG_MIN_SIZE", "SIZES",
           "build_ico_bytes", "main", "render_size"]


def render_size(master: QImage, size: int) -> QImage:
    """Return ``master`` as a square, transparent ``size`` px canvas.

    The artwork keeps its aspect ratio and its original framing: only the
    resolution changes, so the ICO looks like the same mark the runtime
    draws from the PNG.
    """
    scaled = master.scaled(
        QSize(size, size),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    # Pin the resolution: Qt's PNG encoder otherwise writes the active
    # screen's DPI into pHYs, so the committed bytes would change with the
    # developer's display settings (and break ``--check`` in CI).
    canvas.setDotsPerMeterX(DOTS_PER_METRE)
    canvas.setDotsPerMeterY(DOTS_PER_METRE)
    painter = QPainter(canvas)
    painter.drawImage(
        (size - scaled.width()) // 2,
        (size - scaled.height()) // 2,
        scaled,
    )
    painter.end()
    return canvas


def _dib_bytes(image: QImage) -> bytes:
    """Encode one frame as a 32-bit BGRA bottom-up DIB plus AND mask."""
    width, height = image.width(), image.height()
    image = image.convertToFormat(QImage.Format.Format_ARGB32)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,              # biSize
        width,           # biWidth
        height * 2,      # biHeight (XOR bitmap + AND mask)
        1,               # biPlanes
        32,              # biBitCount
        0,               # biCompression (BI_RGB)
        width * height * 4,
        0, 0, 0, 0,
    )

    pixels = bytearray()
    for y in range(height - 1, -1, -1):
        for x in range(width):
            color = image.pixelColor(x, y)
            pixels += bytes((color.blue(), color.green(),
                             color.red(), color.alpha()))

    # 1bpp AND mask: bit set means "leave the destination untouched".
    stride = ((width + 31) // 32) * 4
    mask = bytearray()
    for y in range(height - 1, -1, -1):
        row = bytearray(stride)
        for x in range(width):
            if image.pixelColor(x, y).alpha() == 0:
                row[x // 8] |= 0x80 >> (x % 8)
        mask += row

    return header + bytes(pixels) + bytes(mask)


def _png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise SystemExit("error: Qt could not encode a PNG frame")
    return bytes(buffer.data())


def build_ico_bytes(master: QImage, sizes: tuple[int, ...] = SIZES) -> bytes:
    """Assemble a complete ICO file from ``master`` for ``sizes``."""
    if master.isNull():
        raise SystemExit(f"error: cannot read {MASTER_PNG}")

    frames: list[tuple[int, bytes]] = []
    for size in sorted(set(sizes)):
        if size > min(master.width(), master.height()):
            raise SystemExit(
                f"error: refusing to upscale: {size}px frame from "
                f"{master.width()}x{master.height()} artwork")
        image = render_size(master, size)
        payload = _png_bytes(image) if size >= PNG_MIN_SIZE else _dib_bytes(image)
        frames.append((size, payload))

    offset = 6 + 16 * len(frames)
    directory = bytearray()
    blobs = bytearray()
    for size, payload in frames:
        directory += struct.pack(
            "<BBBBHHII",
            size % 256,  # 256 is encoded as 0
            size % 256,
            0,           # palette colours
            0,           # reserved
            1,           # planes
            32,          # bits per pixel
            len(payload),
            offset,
        )
        blobs += payload
        offset += len(payload)

    return struct.pack("<HHH", 0, 1, len(frames)) + bytes(directory) + bytes(blobs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the Windows ICO from new_icon.png.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the committed ICO is stale",
    )
    parser.add_argument("--source", type=Path, default=MASTER_PNG)
    parser.add_argument("--target", type=Path, default=ICO_PATH)
    options = parser.parse_args(argv)

    # Image I/O only: an offscreen QGuiApplication keeps this working
    # headless (CI) without opening a window.
    _app = QGuiApplication.instance()
    if _app is None:
        _app = QGuiApplication(["make_icon", "-platform", "offscreen"])

    master = QImage(str(options.source))
    if master.isNull():
        print(f"error: cannot read {options.source}", file=sys.stderr)
        return 1

    payload = build_ico_bytes(master)

    if options.check:
        current = options.target.read_bytes() if options.target.is_file() else b""
        if current != payload:
            print(
                f"error: {options.target} is stale; run "
                "`uv run python scripts/make_icon.py` and commit the result",
                file=sys.stderr,
            )
            return 1
        print(f"{options.target.name} is up to date "
              f"({len(payload)} bytes, {len(SIZES)} frames)")
        return 0

    options.target.write_bytes(payload)
    print(f"wrote {options.target} ({len(payload)} bytes)")
    print("frames: " + ", ".join(f"{size}px" for size in SIZES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
