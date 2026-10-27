"""Frozen-build self test (``Inkfall --self-test``).

A packaged build can fail in ways a source checkout never does: the
``qml/`` tree may not have been copied, ``assets/fonts`` may be missing,
Qt's QML import paths may not resolve, or the brand icon may be absent.
This module checks exactly those things without opening a window and
without touching user data, so CI can prove that an installed
application resolves its own resources instead of the build tree.

Invocation::

    Inkfall --self-test [--self-report PATH]

Exit code 0 means every check passed. Exit code 1 means at least one
failed; the JSON report (stdout and/or ``--self-report``) carries the
details. ``--self-report`` exists because a windowed Windows build has
no attached console to print to.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

__all__ = ["run_self_test"]

#: QML components the tool shelf loads at runtime. These are the files
#: that a missing ``--include-data-dir=qml=qml`` breaks first.
_REQUIRED_QML = (
    "toolprops/ColorPicker.qml",
    "toolprops/NumericEditor.qml",
    "toolprops/PaletteStrip.qml",
)


def _option(argv: list[str], name: str) -> str | None:
    """Read ``--name VALUE`` or ``--name=VALUE`` from ``argv``."""
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
        prefix = name + "="
        if arg.startswith(prefix):
            return arg[len(prefix):]
    return None


#: Mean per-pixel RGBA delta tolerated when comparing a packaged icon
#: against the authoritative PNG. Resampling introduces a small rounding
#: error; wrong artwork scores far above this.
_ICON_MAX_DELTA = 2.0

#: Size the ICO and the runtime QIcon are compared at.
_ICON_SIZE = 256


def _mean_delta(left, right, step: int = 2) -> float:
    """Mean per-pixel RGBA difference between two equal-size images."""
    total = 0
    samples = 0
    for y in range(0, left.height(), step):
        for x in range(0, left.width(), step):
            a = left.pixelColor(x, y)
            b = right.pixelColor(x, y)
            total += (abs(a.red() - b.red()) + abs(a.green() - b.green())
                      + abs(a.blue() - b.blue()) + abs(a.alpha() - b.alpha()))
            samples += 1
    return total / max(samples, 1)


def _render_brand_master(master, size: int):
    """Render the master PNG the way :func:`app.icons.app_icon` does."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    scaled = master.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.drawImage(
        (size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def _check_brand_icon(report: dict, icon) -> None:
    """Prove the packaged ICO and runtime icon show the current artwork.

    Checking that a path exists proves nothing about branding: the build
    previously shipped a valid ICO containing the retired artwork. This
    compares decoded pixels against ``new_icon.png`` instead.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QImageReader

    from app.resources import resource_path

    brand_dir = resource_path("assets", "icons")
    master_path = brand_dir / "new_icon.png"
    ico_path = brand_dir / "inkfall.ico"
    result = {
        "master": str(master_path),
        "ico": str(ico_path),
        "ico_size": None,
        "ico_delta": None,
        "runtime_delta": None,
    }
    report["checks"]["brand_icon"] = result

    master = QImage(str(master_path))
    if master.isNull():
        report["errors"].append(
            f"authoritative brand artwork missing or unreadable: {master_path}")
        return
    expected = _render_brand_master(master, _ICON_SIZE)

    reader = QImageReader(str(ico_path))
    largest = None
    for index in range(reader.imageCount()):
        reader.jumpToImage(index)
        frame = reader.read()
        if frame.isNull():
            continue
        if largest is None or frame.width() > largest.width():
            largest = frame
    if largest is None:
        report["errors"].append(f"packaged ICO has no readable frames: {ico_path}")
    else:
        result["ico_size"] = largest.width()
        if largest.width() != _ICON_SIZE:
            largest = largest.scaled(
                _ICON_SIZE, _ICON_SIZE,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        result["ico_delta"] = round(_mean_delta(largest, expected), 3)
        if result["ico_delta"] > _ICON_MAX_DELTA:
            report["errors"].append(
                "packaged ICO does not match new_icon.png "
                f"(mean delta {result['ico_delta']}); regenerate it with "
                "scripts/make_icon.py")

    if not icon.isNull():
        rendered = icon.pixmap(_ICON_SIZE, _ICON_SIZE).toImage()
        if rendered.isNull() or rendered.width() != _ICON_SIZE:
            report["errors"].append(
                "runtime application icon did not render at "
                f"{_ICON_SIZE}px")
        else:
            result["runtime_delta"] = round(_mean_delta(rendered, expected), 3)
            if result["runtime_delta"] > _ICON_MAX_DELTA:
                report["errors"].append(
                    "runtime application icon does not match new_icon.png "
                    f"(mean delta {result['runtime_delta']})")


def _collect_checks(report: dict) -> None:
    """Run the packaged-layout checks, recording results in ``report``."""
    from app.application import create_application

    app = create_application([])
    report["checks"]["qt_platform"] = app.platformName()

    from app.resources import is_frozen, resource_root

    report["frozen"] = is_frozen()
    report["resource_root"] = str(resource_root())

    # --- bundled fonts -------------------------------------------------
    from app.fonts import (
        bundled_arabic_family_available,
        bundled_family_available,
        bundled_font_dir,
        bundled_license_path,
        load_bundled_fonts,
    )

    font_dir = bundled_font_dir()
    report["checks"]["font_dir"] = str(font_dir) if font_dir else None
    report["checks"]["font_errors"] = list(
        getattr(load_bundled_fonts, "load_errors", []))
    report["checks"]["font_family_available"] = bundled_family_available()
    report["checks"]["font_arabic_family_available"] = \
        bundled_arabic_family_available()
    license_path = bundled_license_path()
    report["checks"]["vazirmatn_license"] = (
        str(license_path) if license_path else None)
    if font_dir is None:
        report["errors"].append("bundled font directory not found")
    if report["checks"]["font_errors"]:
        report["errors"].append(
            "font registration errors: %s" % report["checks"]["font_errors"])
    if not report["checks"]["font_family_available"]:
        report["errors"].append("bundled Inter family did not register")
    if not report["checks"]["font_arabic_family_available"]:
        report["errors"].append("bundled Vazirmatn family did not register")
    if license_path is None:
        report["errors"].append("bundled Vazirmatn OFL notice is missing")

    # --- brand icon ----------------------------------------------------
    from app.icons import app_icon

    icon = app_icon()
    report["checks"]["app_icon_ok"] = not icon.isNull()
    if icon.isNull():
        report["errors"].append("application icon could not be loaded")
    _check_brand_icon(report, icon)

    # --- QML components ------------------------------------------------
    from PySide6.QtQml import QQmlComponent, QQmlEngine

    from app.qml_bridge.hosting import _ensure_control_style, qml_url

    _ensure_control_style()
    engine = QQmlEngine()
    qml_results: dict[str, str] = {}
    for relative in _REQUIRED_QML:
        try:
            component = QQmlComponent(engine, qml_url(relative))
            if component.isReady():
                qml_results[relative] = "ready"
                continue
            detail = component.errorString().strip() or "not ready"
            qml_results[relative] = detail
            report["errors"].append(f"QML {relative}: {detail}")
        except Exception as exc:  # pragma: no cover - frozen-only path
            qml_results[relative] = f"exception: {exc}"
            report["errors"].append(f"QML {relative}: {exc}")
    report["checks"]["qml"] = qml_results

    _check_picker_runtime(report)


def _check_picker_runtime(report: dict) -> None:
    """Instantiate the square color picker exactly like the tool shelf.

    Compiling the QML (above) misses packaged-only failures that happen
    when the picker is actually hosted: the ``QQuickWidget``/engine pair,
    the Python bridge context properties, and the live scene graph. The
    color picker is the only UI surface that constructs its own
    ``QQuickWidget`` lazily inside a QML-initiated slot, so a
    frozen-build defect surfaces precisely there ("opening the color
    picker crashes") and nowhere else. This check builds the same card
    without a window and records the outcome.
    """
    result: dict = {"status": None, "errors": []}
    report["checks"]["picker_runtime"] = result
    try:
        from PySide6.QtCore import QCoreApplication, QUrl, Qt
        from PySide6.QtQuickWidgets import QQuickWidget

        from app.qml_bridge.color_provider import SquarePickerBridge
        from app.qml_bridge.hosting import qml_url
        from app.qml_bridge.theme_bridge import ThemeBridge
        from app.theme.tokens import dark_tokens

        card_view = QQuickWidget()
        try:
            card_view.setResizeMode(
                QQuickWidget.ResizeMode.SizeRootObjectToView)
            try:
                card_view.setClearColor(Qt.GlobalColor.transparent)
            except Exception:
                pass
            bridge = SquarePickerBridge(card_view)
            theme = ThemeBridge(dark_tokens(), card_view)
            engine = card_view.engine()
            engine.rootContext().setContextProperty("bridge", bridge)
            engine.rootContext().setContextProperty("theme", theme)
            card_view.setSource(qml_url("toolprops/ColorPicker.qml"))
            deadline = 0
            while (card_view.status() == QQuickWidget.Status.Loading
                   and deadline < 40):
                QCoreApplication.processEvents()
                deadline += 1
            result["status"] = str(card_view.status().name)
            errors = [e.toString() for e in card_view.errors()]
            result["errors"] = errors
            if card_view.status() != QQuickWidget.Status.Ready:
                report["errors"].append(
                    "color picker QQuickWidget did not reach Ready: "
                    f"status={result['status']} errors={errors}")
        finally:
            try:
                card_view.setSource(QUrl())
            except Exception:
                pass
            try:
                card_view.close()
            except Exception:
                pass
    except Exception as exc:
        result["status"] = f"exception: {type(exc).__name__}"
        result["errors"] = [str(exc)]
        report["errors"].append(
            f"color picker runtime instantiation failed: {exc}")


def _safe_print(text: str, stream=None) -> None:
    """Print without failing when a windowed build has no console.

    A Windows build compiled with ``--windows-console-mode=disable`` may
    expose ``sys.stdout``/``sys.stderr`` as ``None``; a bare ``print``
    would then raise and turn a passing self-test into a crash.
    """
    target = sys.stdout if stream is None else stream
    if target is None:
        return
    try:
        print(text, file=target)
    except (OSError, ValueError, AttributeError):
        pass


def run_self_test(argv: list[str]) -> int:
    """Run the self test and return a process exit code."""
    report: dict = {
        "ok": False,
        "frozen": False,
        "resource_root": None,
        "checks": {},
        "errors": [],
    }
    try:
        _collect_checks(report)
    except Exception as exc:  # pragma: no cover - frozen-only path
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        report["traceback"] = traceback.format_exc()
    report["ok"] = not report["errors"]

    payload = json.dumps(report, indent=2, sort_keys=True)
    target = _option(argv, "--self-report")
    if target:
        try:
            Path(target).write_text(payload + "\n", encoding="utf-8")
        except OSError as exc:
            _safe_print(
                f"self-test: could not write report to {target}: {exc}",
                sys.stderr,
            )
    _safe_print(payload)
    return 0 if report["ok"] else 1
