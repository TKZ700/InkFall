"""M5-A2.2.2: no QWidget opacity effect over QML + zero QPainter warnings.

Regression tests for the Custom-editor QPainter storm
(``QPainter::begin: A paint device can only be painted by one painter
at a time`` + ``QWidgetEffectSourcePrivate::pixmap: Painter not
active``). Root cause: ``QGraphicsOpacityEffect`` on an ancestor of a
``QQuickWidget`` (ToolPopup slide/fade, picker-wrap fade) forces the
effect-source pixmap path to capture the Quick framebuffer inside a
QWidget paint cycle. The fix keeps slide/tween geometry but moves
opacity to QML-side animation (or motion-only fallback).

Native window-disappearance (Defect 1) cannot be proven offscreen; these
tests guard the painting half and the effect-guard invariant.
"""

import pytest
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens, light_tokens
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors",
                    "inkfall/m5/toolbar/presets"):
            QSettings().remove(key)
    except Exception:
        pass


def _make_form(qtbot, tokens):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    QTest.qWait(150)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P222")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, tokens, viewer)
    qtbot.addWidget(form)
    form.show()
    QTest.qWait(150)
    return viewer, form


def _assert_no_effect_chain(widget, label: str) -> None:
    """No QGraphicsEffect on widget or its ancestors (bounded walk)."""
    node = widget
    depth = 0
    while node is not None and depth < 10:
        try:
            assert node.graphicsEffect() is None, (
                f"{label}: {type(node).__name__} "
                f"({node.objectName()}) carries a QWidget effect")
        except AssertionError:
            raise
        except Exception:
            pass
        try:
            node = node.parentWidget()
        except Exception:
            node = None
        depth += 1


def test_fx_helpers_skip_effect_over_quick_subtree(qtbot):
    """fx fade/slide helpers must not install effects over QML."""
    from PySide6.QtWidgets import QWidget
    from PySide6.QtQuickWidgets import QQuickWidget

    from app import fx as _fx

    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    QTest.qWait(50)
    quick = QQuickWidget(host)
    quick.setObjectName("m5a222-probe")
    QTest.qWait(150)
    assert _fx.subtree_has_quick_widget(host)
    assert not _fx.subtree_has_quick_widget(QWidget())

    _fx.fade_in(host, 120, host)
    QTest.qWait(50)
    assert host.graphicsEffect() is None
    _fx.fade_out(host, 120, host, hide=False)
    QTest.qWait(50)
    assert host.graphicsEffect() is None
    _fx.slide_fade_in(host, 0, 120, host, dy_px=-6)
    QTest.qWait(300)
    assert host.graphicsEffect() is None
    _fx.slide_fade_out(host, 0, 120, host, dy_px=4, hide=False)
    QTest.qWait(300)
    assert host.graphicsEffect() is None

    # Non-QML widgets keep their animated behavior (effect installed).
    plain = QWidget()
    qtbot.addWidget(plain)
    plain.show()
    QTest.qWait(50)
    _fx.fade_in(plain, 200, plain)
    QTest.qWait(30)
    assert plain.graphicsEffect() is not None
    QTest.qWait(300)
    assert plain.graphicsEffect() is None  # cleared on finish


def test_popup_and_picker_carry_no_widget_effect(qtbot, cleared_settings):
    """Opened popup + picker + Custom drawers: effect-free chains."""
    for tokens in (dark_tokens(), light_tokens()):
        _viewer, form = _make_form(qtbot, tokens)
        try:
            from app import fx as _fx

            # Popup entrance over QML content must be slide-only.
            _fx.slide_fade_in(form, 0, 120, form, dy_px=-6)
            QTest.qWait(300)
            assert form.graphicsEffect() is None

            strip = form._color_section
            strip._on_palette_add_tile()
            QTest.qWait(900)
            assert strip._ring is not None
            assert strip._picker_wrap.graphicsEffect() is None
            _assert_no_effect_chain(strip._ring._view, "picker QML view")

            for section in (form._size_section, form._opacity_section):
                section._custom_btn.click()
                QTest.qWait(800)
                assert section._editor is not None
                assert section._editor.graphicsEffect() is None
                _assert_no_effect_chain(section._editor, "numeric QML view")
                section._custom_btn.click()
                QTest.qWait(400)

            strip._on_add_custom_collapse()
            QTest.qWait(600)
            assert strip._picker_wrap.graphicsEffect() is None
        finally:
            try:
                form.teardown_qml()
            except Exception:
                pass


def test_custom_and_picker_cycles_emit_no_qpainter_warnings(
        qtbot, cleared_settings):
    """Open/commit/cancel/reopen cycles must log zero QPainter warnings."""
    captured: list = []
    try:
        from PySide6.QtCore import qInstallMessageHandler

        def _handler(mode, context, message: str) -> None:
            try:
                text = str(message)
            except Exception:
                return
            if ("QPainter" in text or "EffectSource" in text
                    or "Painter not active" in text
                    or "paint device" in text):
                captured.append(text)

        qInstallMessageHandler(_handler)
    except Exception:
        pass
    try:
        for tokens in (dark_tokens(), light_tokens()):
            _viewer, form = _make_form(qtbot, tokens)
            try:
                strip = form._color_section
                strip._on_palette_add_tile()
                QTest.qWait(900)
                strip._on_add_custom_collapse()
                QTest.qWait(500)
                for section in (form._size_section, form._opacity_section):
                    section._custom_btn.click()
                    QTest.qWait(800)
                    # Keyboard paths: focus + Escape (cancel) round-trip.
                    try:
                        bridge = section._bridge
                        if bridge is not None:
                            bridge.requestFocus()
                            QTest.qWait(150)
                    except Exception:
                        pass
                    section._custom_btn.click()
                    QTest.qWait(400)
                    section._custom_btn.click()
                    QTest.qWait(700)
                    section._custom_btn.click()
                    QTest.qWait(400)
            finally:
                try:
                    form.teardown_qml()
                except Exception:
                    pass
    finally:
        try:
            from PySide6.QtCore import qInstallMessageHandler as _restore

            _restore(None)
        except Exception:
            pass
    assert not captured, (
        f"QPainter/effect warnings during Custom/picker cycles: "
        f"{captured[:3]}")
