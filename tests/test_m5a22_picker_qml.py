"""M5-A2.2.1: square HSV picker (gradient square + hue slider).

Python owns HSV state/math; QML owns gradients/markers/preview/hex.
No image provider, no per-pixel JS, no tree rebuilds on drag.
"""
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens, light_tokens
from tools.models import ToolType
from tests.test_m5a22_numeric_qml import cleared_settings, _make_viewer  # noqa

pytestmark = pytest.mark.usefixtures("qapp")

W = H = 200.0


def _open_picker(qtbot, strip):
    strip._on_palette_add_tile()
    qtbot.wait(1200)
    assert strip._ring is not None
    assert strip._qml_error is None
    return strip._ring


def _hsv(hexed):
    c = QColor(hexed)
    h, s, v, _a = c.getHsv()
    return h, s / 255.0, v / 255.0


def _close_enough(a: str, b: str, tol: int = 5) -> bool:
    """Hex equality within 8-bit HSV round-trip quantization."""
    ca, cb = QColor(a), QColor(b)
    return (abs(ca.red() - cb.red()) <= tol
            and abs(ca.green() - cb.green()) <= tol
            and abs(ca.blue() - cb.blue()) <= tol)


def test_card_ready_no_qml_errors(qtbot, cleared_settings):
    from PySide6.QtQuickWidgets import QQuickWidget

    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    card = _open_picker(qtbot, form._color_section)
    assert card._view is not None
    assert card.status() == QQuickWidget.Ready
    assert card.errors() == []


def test_square_coordinates_corners_and_clamping():
    from app.qml_bridge.color_provider import SquarePickerBridge

    bridge = SquarePickerBridge()
    got = []
    bridge.picked.connect(got.append)
    cases = [
        # (x, y) -> expected (s, v)
        ((0, 0), (0.0, 1.0)),       # top-left: white
        ((200, 0), (1.0, 1.0)),     # top-right: pure hue
        ((0, 200), (0.0, 0.0)),     # bottom-left: black
        ((200, 200), (1.0, 0.0)),   # bottom-right: black
        ((100, 100), (0.5, 0.5)),   # center
        ((50, 150), (0.25, 0.25)),
    ]
    for (x, y), (s, v) in cases:
        # Distinct start: every press below must change state+emit.
        bridge.setHSV(100, 0.9, 0.1)
        got.clear()
        bridge.pressSV(x, y, W, H)
        assert bridge.sat == pytest.approx(s, abs=1e-9)
        assert bridge.val == pytest.approx(v, abs=1e-9)
        assert bridge.hue == 100  # square never changes hue
        assert got and QColor(got[-1]).isValid()
    # Out-of-area clicks clamp instead of corrupting.
    bridge.setHSV(0, 0.5, 0.5)
    got.clear()
    bridge.pressSV(-30, 260, W, H)
    assert (bridge.sat, bridge.val) == (0.0, 0.0)
    assert got and QColor(got[-1]).isValid()
    bridge.pressSV(400, -40, W, H)
    assert (bridge.sat, bridge.val) == (1.0, 1.0)


def test_hue_slider_preserves_sv():
    from app.qml_bridge.color_provider import SquarePickerBridge

    bridge = SquarePickerBridge()
    hues = []
    bridge.huePicked.connect(hues.append)
    bridge.setHSV(10, 0.6, 0.7)
    bridge.pressHue(100, W)  # mid slider
    assert bridge.hue == pytest.approx(180, abs=2)
    assert bridge.sat == pytest.approx(0.6)
    assert bridge.val == pytest.approx(0.7)
    assert hues and hues[-1] == bridge.hue
    # Slider ends cover the full range; out-of-range clamps.
    bridge.pressHue(0, W)
    assert bridge.hue == 0
    bridge.pressHue(200, W)
    assert bridge.hue == 359
    bridge.pressHue(-50, W)
    assert bridge.hue == 0
    assert bridge.sat == pytest.approx(0.6)
    assert bridge.val == pytest.approx(0.7)


def test_hex_syncs_picker_and_preview():
    from app.qml_bridge.color_provider import SquarePickerBridge

    bridge = SquarePickerBridge()
    got = []
    bridge.picked.connect(got.append)
    bridge.commitHex("#2b6cff")
    h, s, v = _hsv("#2b6cff")
    assert bridge.hue == h
    assert bridge.sat == pytest.approx(s, abs=1 / 255 + 1e-9)
    assert bridge.val == pytest.approx(v, abs=1 / 255 + 1e-9)
    assert _close_enough(bridge.previewColor, "#2b6cff")
    assert _close_enough(bridge.hexText, "#2b6cff")
    assert got and _close_enough(got[-1], "#2b6cff")
    # Markers follow hex input.
    assert bridge.svMarkX == pytest.approx(s, abs=0.02)
    assert bridge.svMarkY == pytest.approx(1 - v, abs=0.02)
    # Invalid hex: model untouched, field reverts.
    bridge.commitHex("not-a-color")
    assert _close_enough(bridge.previewColor, "#2b6cff")
    bridge.commitHex("#12345")  # incomplete
    assert _close_enough(bridge.previewColor, "#2b6cff")


def test_bridge_notifies_only_on_change():
    from app.qml_bridge.color_provider import SquarePickerBridge

    bridge = SquarePickerBridge()
    counts = {"hsv": 0, "prev": 0, "hex": 0}
    bridge.hsvChanged.connect(lambda: counts.__setitem__(
        "hsv", counts["hsv"] + 1))
    bridge.previewChanged.connect(lambda: counts.__setitem__(
        "prev", counts["prev"] + 1))
    bridge.hexChanged.connect(lambda: counts.__setitem__(
        "hex", counts["hex"] + 1))
    bridge.setHSV(100, 0.5, 0.5)
    assert counts == {"hsv": 1, "prev": 1, "hex": 1}
    bridge.setHSV(100, 0.5, 0.5)  # identical: silent
    assert counts == {"hsv": 1, "prev": 1, "hex": 1}
    # QML-read props all bindable: every Property has a NOTIFY.
    mo = bridge.metaObject()
    for i in range(mo.propertyCount()):
        prop = mo.property(i)
        name = prop.name()
        if name in ("objectName",):
            continue
        assert prop.hasNotifySignal(), f"non-bindable: {name}"


def test_qml_engine_log_clean_through_lifecycle(
        qtbot, cleared_settings):
    """Open/interact/close the real popup: zero QQml warnings."""
    from PySide6.QtCore import qInstallMessageHandler, QtMsgType

    from app.toolshelf.tool_popup import PropertiesForm

    captured = []
    prev = qInstallMessageHandler(
        lambda mode, ctx, msg: captured.append(str(msg))
        if mode in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg)
        else None)
    try:
        viewer = _make_viewer(qtbot)
        pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1",
                                         color="#e5484d")
        viewer.tool_shelf.refresh()
        form = PropertiesForm(pen, dark_tokens(), viewer)
        qtbot.addWidget(form)
        form.show()
        qtbot.wait(200)
        strip = form._color_section
        card = _open_picker(qtbot, strip)
        bridge = card._bridge
        bridge.moveSV(150, 50, W, H)
        bridge.moveHue(100, W)
        bridge.commitHex("#2b6cff")
        qtbot.wait(200)
        form._size_section._custom_btn.click()
        qtbot.wait(800)
        strip._on_palette_add_tile()  # collapse
        qtbot.wait(300)
        form.close()
        qtbot.wait(200)
    finally:
        qInstallMessageHandler(prev)
    bad = [m for m in captured
           if ("QQmlExpression" in m or "TypeError" in m
               or "of null" in m or "non-bindable" in m)]
    assert bad == []


def test_live_drag_responsive_no_per_move_persist(
        qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    card = _open_picker(qtbot, form._color_section)
    bridge = card._bridge
    calls = []
    real_persist = viewer.tool_store.persist
    viewer.tool_store.persist = lambda *a, **k: calls.append(1)
    try:
        t0 = time.perf_counter()
        for i in range(0, 201, 5):
            bridge.moveSV(i, 200 - i, W, H)
            bridge.moveHue(i, W)
        qtbot.wait(150)
        total = (time.perf_counter() - t0) * 1000
    finally:
        viewer.tool_store.persist = real_persist
    per_move = total / 82
    assert per_move < 5.0, f"{per_move:.2f}ms/move"
    assert QColor(form._slot.color).isValid()
    assert _close_enough(form._slot.color, bridge.previewColor)
    assert len(calls) == 0  # debounced, never per-move


def test_reopen_resyncs_no_stale_color(qtbot, cleared_settings):
    from app.toolshelf.tool_popup import PropertiesForm

    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="P1",
                                     color="#2b6cff")
    viewer.tool_shelf.refresh()
    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    form.show()
    qtbot.wait(100)
    strip = form._color_section
    card = _open_picker(qtbot, strip)
    card._bridge.moveSV(200, 0, W, H)  # drag toward pure hue
    qtbot.wait(100)
    assert form._slot.color.lower() != "#2b6cff"
    strip._on_palette_add_tile()  # commit + collapse
    qtbot.wait(400)
    strip._on_palette_add_tile()  # reopen
    qtbot.wait(800)
    assert _close_enough(card._bridge.previewColor, form._slot.color)


def test_rendered_gradient_accuracy(qtbot, cleared_settings):
    """Sampled QML gradient pixels match expected HSV (tolerance for
    8-bit gradient quantization)."""
    from app.toolshelf.tool_popup import _QmlColorCard

    card = _QmlColorCard(dark_tokens())
    qtbot.addWidget(card)
    card.resize(260, 340)
    card.show()
    qtbot.wait(1500)
    assert card._view is not None
    card.set_hsv(0, 1.0, 1.0)  # pure red base
    qtbot.wait(300)
    fb = card._view.grabFramebuffer()
    vw = fb.width()
    # SV square geometry in view coords: box is 200 wide centered.
    x0 = (vw - 200) / 2
    y0 = 12.0
    scale = fb.width() / card._view.width()

    def px(fx, fy):
        return fb.pixelColor(int((x0 + fx) * scale),
                             int((y0 + fy) * scale))

    def close(c, r, g, b, tol=30):
        return (abs(c.red() - r) <= tol and abs(c.green() - g) <= tol
                and abs(c.blue() - b) <= tol)

    # Insets avoid the 8px rounded corners of the square; the hue base
    # is pure red (set_hsv(0, 1, 1) above). Expected values compose both
    # overlays: white mix (1-x/200) then black mix (y/200).
    assert close(px(180, 15), 236, 23, 23)    # top-right: red-dominant
    assert close(px(12, 10), 242, 228, 228)   # top-left: near-white
    assert close(px(100, 195), 6, 3, 3)       # bottom center: black
    mid = px(100, 100)                        # center: mid red ~(128,64,64)
    assert mid.red() > 110 and mid.red() - mid.green() > 40 \
        and mid.red() - mid.blue() > 40
    # Card corners stay transparent (rounded container, square field).
    for x, y in ((2, 2), (fb.width() - 3, 2),
                 (2, fb.height() - 3),
                 (fb.width() - 3, fb.height() - 3)):
        assert fb.pixelColor(x, y).alpha() < 32
