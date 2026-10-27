"""Brand-icon and QML-plugin packaging regression tests.

``assets/icons/new_icon.png`` is the authoritative Inkfall artwork: the
running application draws the window/taskbar icon from it. Windows needs a
multi-resolution ICO for the executable, the NSIS installer and the Start
Menu shortcut, so ``scripts/make_icon.py`` derives
``assets/icons/inkfall.ico`` from that PNG.

These tests compare *decoded pixels*, not just filenames, so a build that
silently reverts to the retired "Falling Stroke" artwork fails here instead
of shipping a mis-branded installer. They also pin the Nuitka arguments
that select the ICO and the Qt QML plugin family. No Nuitka run, no build
and no artifact is produced.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from scripts import build_config
from scripts import make_icon
from scripts.build_config import (
    SOURCE_ROOT,
    WINDOWS_ICON,
    nuitka_arguments,
    project_version,
    required_bundle_entries,
)

pytestmark = pytest.mark.usefixtures("qapp")

BRAND = SOURCE_ROOT / "assets" / "icons"
MASTER_PNG = BRAND / "new_icon.png"
ICO = BRAND / "inkfall.ico"

#: Sizes Windows actually consumes (shell list view through 256 px
#: thumbnails/installer). The generator may emit more.
REQUIRED_SIZES = (16, 32, 48, 64, 128, 256)

#: Mean per-pixel RGBA delta allowed between an ICO frame and the master
#: rendered at the same size. Resampling to 8-bit BGRA introduces a tiny
#: rounding error; anything structural (wrong artwork, crop, reflow) is
#: orders of magnitude larger.
MAX_MEAN_DELTA = 1.0

#: Mean per-pixel RGBA delta allowed between a committed ICO frame and the
#: same frame regenerated on the current platform. Qt's
#: SmoothTransformation raster rounding is not bit-stable across machines:
#: resampling runs through CPU-dispatched code paths whose results differ by
#: ±1 LSB on a small fraction of pixels even between Windows hosts — the
#: committed 256 px frame measures 0.00057 against a fresh regeneration on
#: the windows-2022 CI runners while reproducing byte-exactly on the
#: authoring machine. Every platform therefore compares with this narrow
#: tolerance (no exact pixel/byte equality gate anywhere). Real staleness
#: measures orders of magnitude higher;
#: test_stale_artwork_fails_decisively pins that gap.
FRAME_MEAN_DELTA_MAX = 0.05


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ico_entries(path: Path = ICO) -> list[dict]:
    data = path.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert reserved == 0
    assert kind == 1, "not an icon file (type 2 is a cursor)"
    entries = []
    for index in range(count):
        offset = 6 + index * 16
        width, height, colors, rsvd, planes, bpp, size, start = struct.unpack(
            "<BBBBHHII", data[offset:offset + 16])
        entries.append({
            "width": width or 256,
            "height": height or 256,
            "colors": colors,
            "planes": planes,
            "bpp": bpp,
            "size": size,
            "offset": start,
            "blob": data[start:start + size],
        })
    return entries


def _frame(path: Path, size: int):
    """Decode the ICO frame whose width is ``size``."""
    from PySide6.QtGui import QImageReader

    reader = QImageReader(str(path))
    for index in range(reader.imageCount()):
        reader.jumpToImage(index)
        image = reader.read()
        if image.width() == size:
            return image
    raise AssertionError(f"{path} has no {size}px frame")


def _decode_ico(blob: bytes) -> dict[int, object]:
    """Decode every frame of an in-memory ICO, keyed by width."""
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImageReader

    buffer = QBuffer()
    buffer.setData(blob)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    frames: dict[int, object] = {}
    for index in range(reader.imageCount()):
        reader.jumpToImage(index)
        image = reader.read()
        assert not image.isNull(), "unreadable ICO frame"
        frames[image.width()] = image
    return frames


def _render_master(size: int):
    """The master artwork rendered the same way the generator does."""
    from PySide6.QtGui import QImage

    master = QImage(str(MASTER_PNG))
    assert not master.isNull(), f"cannot decode {MASTER_PNG}"
    return make_icon.render_size(master, size)


def _mean_delta(left, right) -> float:
    from PySide6.QtCore import Qt

    if left.size() != right.size():
        right = right.scaled(left.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    total = 0
    for y in range(left.height()):
        for x in range(left.width()):
            a = left.pixelColor(x, y)
            b = right.pixelColor(x, y)
            total += (abs(a.red() - b.red()) + abs(a.green() - b.green())
                      + abs(a.blue() - b.blue()) + abs(a.alpha() - b.alpha()))
    return total / (left.width() * left.height())


# ---------------------------------------------------------------------------
# authoritative artwork
# ---------------------------------------------------------------------------


def test_master_artwork_is_the_square_rgba_brand_source() -> None:
    from PySide6.QtGui import QImage

    image = QImage(str(MASTER_PNG))
    assert not image.isNull(), "new_icon.png must decode"
    assert image.width() == image.height() >= 256
    assert image.hasAlphaChannel()
    # A real brand mark has opaque content and transparent margins; a
    # fully transparent or fully opaque square would mean the asset was
    # replaced by a placeholder.
    alphas = {image.pixelColor(x, y).alpha()
              for x in range(0, image.width(), 17)
              for y in range(0, image.height(), 17)}
    assert 0 in alphas, "artwork should have transparent margins"
    assert max(alphas) >= 200, "artwork should have solid content"


def test_runtime_icon_is_built_from_the_master_artwork() -> None:
    """The window/taskbar icon must draw the current mark, not the ICO."""
    from app.icons import app_icon

    icon = app_icon()
    assert not icon.isNull()
    rendered = icon.pixmap(256, 256).toImage()
    assert rendered.width() == 256 and rendered.height() == 256
    assert _mean_delta(rendered, _render_master(256)) <= MAX_MEAN_DELTA


# ---------------------------------------------------------------------------
# generated ICO
# ---------------------------------------------------------------------------


def test_ico_is_a_valid_windows_icon_with_the_expected_sizes() -> None:
    entries = _ico_entries()
    sizes = [entry["width"] for entry in entries]
    assert sizes == sorted(sizes), "frames should be ascending"
    for size in REQUIRED_SIZES:
        assert size in sizes, f"missing {size}px frame"
    for entry in entries:
        assert entry["height"] == entry["width"]
        assert entry["bpp"] == 32, "32bpp keeps transparency at every size"
        assert entry["planes"] == 1
        assert entry["blob"], "empty frame payload"


def test_ico_frames_are_the_master_artwork_at_each_size() -> None:
    for size in REQUIRED_SIZES:
        frame = _frame(ICO, size)
        assert frame.hasAlphaChannel(), f"{size}px frame lost its alpha"
        delta = _mean_delta(frame, _render_master(size))
        assert delta <= MAX_MEAN_DELTA, (
            f"{size}px frame differs from new_icon.png (mean delta "
            f"{delta:.2f}); regenerate with "
            "`uv run python scripts/make_icon.py`")


def test_obsolete_legacy_png_ladder_is_not_required() -> None:
    """The retired per-size PNGs are not build inputs or runtime assets."""
    assert (BRAND / "png").exists() is False
    legacy = "assets/icons/png/inkfall-256.png"
    for platform in ("windows", "linux"):
        entries = required_bundle_entries(platform)
        assert legacy not in entries
        assert not [entry for entry in entries
                    if entry.startswith("assets/icons/png/")]


def test_committed_ico_was_produced_by_the_generator() -> None:
    """Decoded frames must equal a fresh generation from ``new_icon.png``.

    Qt's smooth-scale raster rounding is not bit-stable across machines:
    CPU-dispatched resampling paths differ by ±1 LSB on a few pixels even
    between Windows hosts (the windows-2022 CI runner measured a 0.00057
    mean delta on the 256 px frame that reproduces byte-exactly on the
    authoring machine). Every platform therefore compares decoded pixels
    with the narrow FRAME_MEAN_DELTA_MAX tolerance instead of exact pixel
    or byte equality. Fresh generator output must still be exactly
    reproducible within this run, and stale artwork measures orders of
    magnitude above the tolerance (test_stale_artwork_fails_decisively).
    """
    from PySide6.QtGui import QImage

    master = QImage(str(MASTER_PNG))
    regenerated = make_icon.build_ico_bytes(master)
    assert make_icon.build_ico_bytes(master) == regenerated, (
        "generator output is not reproducible; --check would flap")

    committed = _decode_ico(ICO.read_bytes())
    fresh = _decode_ico(regenerated)
    assert committed.keys() == fresh.keys()
    for size in committed:
        delta = _mean_delta(committed[size], fresh[size])
        assert delta <= FRAME_MEAN_DELTA_MAX, (
            f"{size}px frame is stale (mean delta {delta:.5f} exceeds "
            f"the cross-platform rounding budget {FRAME_MEAN_DELTA_MAX}); "
            "run `uv run python scripts/make_icon.py`")


def test_stale_artwork_fails_decisively() -> None:
    """The cross-platform tolerance must not mask stale or wrong artwork.

    A 180°-rotated master is the tamest possible artwork change (identical
    colour histogram, same alpha margins). Its regenerated frames must
    exceed FRAME_MEAN_DELTA_MAX by orders of magnitude on every size, so
    the platform-gated tolerance in test_committed_ico_was_produced_by_the_
    generator can only ever absorb ±1 LSB scaling rounding.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    source = QImage(str(MASTER_PNG))
    master = source.flipped(Qt.Orientation.Horizontal | Qt.Orientation.Vertical)
    stale = _decode_ico(make_icon.build_ico_bytes(master))
    fresh = _decode_ico(make_icon.build_ico_bytes(source))
    assert stale.keys() == fresh.keys()
    for size in stale:
        delta = _mean_delta(stale[size], fresh[size])
        assert delta > FRAME_MEAN_DELTA_MAX * 100, (
            f"rotated artwork at {size}px only measured {delta:.2f}; the "
            "tolerance would mask real staleness")


def test_largest_frame_is_png_compressed_for_size() -> None:
    """256px frames are conventionally PNG-compressed inside an ICO."""
    entries = {entry["width"]: entry for entry in _ico_entries()}
    assert entries[256]["blob"][:8] == b"\x89PNG\r\n\x1a\n"
    for size in (16, 32, 48):
        assert entries[size]["blob"][:8] != b"\x89PNG\r\n\x1a\n"


def test_generator_covers_the_windows_size_ladder_and_refuses_upscaling() -> None:
    from PySide6.QtGui import QImage

    for size in REQUIRED_SIZES:
        assert size in make_icon.SIZES

    small = QImage(64, 64, QImage.Format.Format_ARGB32)
    small.fill(0)
    with pytest.raises(SystemExit):
        make_icon.build_ico_bytes(small, sizes=(16, 128))


# ---------------------------------------------------------------------------
# build configuration
# ---------------------------------------------------------------------------


def test_windows_build_embeds_the_generated_ico(tmp_path: Path) -> None:
    args = nuitka_arguments(
        "windows", project_version(), source_root=SOURCE_ROOT,
        build_root=tmp_path / "build", dist_dir=tmp_path / "dist")
    icon_args = [arg for arg in args if arg.startswith("--windows-icon-from-ico=")]
    assert icon_args == [f"--windows-icon-from-ico={SOURCE_ROOT / WINDOWS_ICON}"]
    # The argument must point at the regenerated file, not a copy.
    assert Path(icon_args[0].split("=", 1)[1]) == ICO
    assert WINDOWS_ICON == "assets/icons/inkfall.ico"


def test_qml_plugin_family_is_declared_for_every_platform(tmp_path: Path) -> None:
    assert build_config.QT_PLUGIN_FAMILIES == ("qml",)
    for platform in ("windows", "linux"):
        args = nuitka_arguments(
            platform, project_version(), source_root=SOURCE_ROOT,
            build_root=tmp_path / platform / "build",
            dist_dir=tmp_path / platform / "dist")
        assert "--include-qt-plugins=qml" in args
        # Scope guard: do not bundle unrelated plugin families.
        families = [arg.split("=", 1)[1] for arg in args
                    if arg.startswith("--include-qt-plugins=")]
        assert families == ["qml"]


def test_bundle_manifest_keeps_brand_and_qml_assets() -> None:
    for platform in ("windows", "linux"):
        entries = required_bundle_entries(platform)
        assert WINDOWS_ICON in entries
        assert "assets/icons/new_icon.png" in entries
        assert "assets/fonts/Inter-Regular.ttf" in entries
        assert "assets/fonts/Vazirmatn-Regular.ttf" in entries
        assert "assets/fonts/Vazirmatn-OFL.txt" in entries
        assert "qml/toolprops/ColorPicker.qml" in entries
        # The retired per-size PNG ladder is gone from the repository and
        # must never be a standalone-verification requirement again.
        assert "assets/icons/png/inkfall-256.png" not in entries
        for entry in entries:
            if entry.startswith(("qml/", "assets/")):
                assert (SOURCE_ROOT / entry).is_file(), entry
