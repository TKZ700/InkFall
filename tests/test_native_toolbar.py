"""M5-A1 native toolbar integration tests: Inkfall-owned controls only.

Proves the toolbar is genuinely native: slots are Inkfall-painted
``SlotButton`` cells, the menu is a Qt ``QMenu`` shown through the
Inkfall menu pattern, the editor is a ``CenteredModal`` with native
controls — and that no third-party widget toolkit remains anywhere
in the product (dependency removed).
"""

import ast
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMenu

from app import fx
from app.theme.tokens import dark_tokens, light_tokens
from app.toolshelf.slot_button import CELL, SlotButton, pen_diameter, preview_kind
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cleared_settings():
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors"):
            QSettings().remove(key)
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("inkfall/m5/toolbar/slots", "inkfall/m5/toolbar/dock",
                    "inkfall/m5/toolbar/active",
                    "inkfall/m5/toolbar/custom_colors"):
            QSettings().remove(key)
    except Exception:
        pass
    try:
        fx.set_motion_mode("follow")
    except Exception:
        pass


def _make_viewer(qtbot):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager

    viewer = PdfViewer(DocumentManager())
    viewer.resize(900, 700)
    viewer.show()
    qtbot.addWidget(viewer)
    return viewer


def test_no_third_party_widget_toolkit():
    assert "qfluentwidgets" not in str(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        import qfluentwidgets  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    assert installed is False
    assert not (ROOT / "app" / "fluent_adapter.py").exists()


def test_shelf_slots_are_native_inkfall_buttons(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    viewer.tool_shelf.refresh()
    assert len(viewer.tool_shelf._buttons) >= 3
    for button in viewer.tool_shelf._buttons.values():
        assert type(button) is SlotButton
        assert button.styleSheet() == ""


def test_pen_and_highlighter_use_distinct_tinted_glyphs():
    from tools.models import ToolInstance

    from app.toolshelf.slot_button import _PEN_ICONS, preview_kind

    assert _PEN_ICONS[ToolType.PEN] == "pen-nib"
    assert _PEN_ICONS[ToolType.HIGHLIGHTER] == "marker"
    pen = ToolInstance(kind=ToolType.PEN, name="P", width_mm=0.5)
    hl = ToolInstance(kind=ToolType.HIGHLIGHTER, name="H")
    assert preview_kind(pen) == "icon" and preview_kind(hl) == "icon"
    from app.icons import pixmap

    def _bytes(pm) -> bytes:
        from PySide6.QtGui import QImage

        return bytes(pm.toImage().convertToFormat(
            QImage.Format.Format_ARGB32).constBits())

    assert _bytes(pixmap("pen-nib", "#e5484d", 22)) != _bytes(
        pixmap("marker", "#e5484d", 22))


def test_slot_menu_is_native_qmenu(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1")
    menu = viewer.tool_shelf.build_slot_menu(pen)
    assert menu is not None
    assert type(menu) is QMenu
    labels = [a.text() for a in menu.actions()]
    assert any("Edit" in label for label in labels)
    assert "Duplicate" in labels and "Remove" in labels
    pan = next(s for s in viewer.tool_store.slots if s.kind == ToolType.PAN)
    pan_menu = viewer.tool_shelf.build_slot_menu(pan)
    assert "Remove" not in [a.text() for a in pan_menu.actions()]


def test_properties_form_uses_native_inkfall_controls(qtbot, cleared_settings):
    viewer = _make_viewer(qtbot)
    pen = viewer.tool_store.add_slot(ToolType.PEN, name="Pen #1", color="#ff0000",
                                     width_mm=0.9, opacity=0.9)
    from app.toolshelf.tool_popup import PropertiesForm, ToolPopup

    form = PropertiesForm(pen, dark_tokens(), viewer)
    qtbot.addWidget(form)
    # Live inspector: 10 preset swatches in the inline color section,
    # no Blend UI, no draft/Save/Cancel, no external color dialog.
    assert len(form._color_section._swatches) == 10
    assert getattr(form, "_blend_section", None) is None
    assert hasattr(form, "changed")
    import app.toolshelf.tool_popup as popup_mod

    assert "QColorDialog" not in dir(popup_mod)
    assert form._slot is not None and form._slot.name == "Pen #1"
    assert form._slot.color.lower() == "#ff0000"
    assert abs(form._slot.width_mm - 0.9) < 1e-9
    form._color_section._emit("#00ff00")
    assert form._slot.color.lower() == "#00ff00"
    # Pan/Selection expose no rows at all (capability-filtered, not hidden).
    pan = next(s for s in viewer.tool_store.slots if s.kind == ToolType.PAN)
    pan_form = PropertiesForm(pan, dark_tokens(), viewer)
    qtbot.addWidget(pan_form)
    assert pan_form._color_section is None
    assert pan_form._size_section is None
    assert pan_form._smooth is None
    # The popup hosts either content with identical chrome.
    popup = ToolPopup(dark_tokens(), viewer)
    qtbot.addWidget(popup)
    popup.set_content(form)
    assert popup._content is form


def test_no_qfluentwidgets_references_in_product():
    offenders: list[str] = []
    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "annotations").rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "qfluentwidgets" in source or "fluent_adapter" in source:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_no_global_stylesheet_takeover(qtbot, cleared_settings):
    app = QApplication.instance()
    before = app.styleSheet() or ""
    viewer = _make_viewer(qtbot)
    viewer.update_chrome_tokens(light_tokens())
    viewer.update_chrome_tokens(dark_tokens())
    after = app.styleSheet() or ""
    assert before == after


def test_native_toolkit_confined_to_app_layer():
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in ("core", "document", "rendering",
                                          "annotations", "tools", "library"):
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(a.name.split(".")[0] in ("PySide6", "PyQt6", "PyQt5")
                           for a in node.names):
                        offenders.append(rel.as_posix())
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in (
                            "PySide6", "PyQt6", "PyQt5"):
                        offenders.append(rel.as_posix())
    assert offenders == []


def test_cell_metrics_constant():
    # Thin silhouette: compact cells, glyphs keep full legibility.
    assert CELL == 40
