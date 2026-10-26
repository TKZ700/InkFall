"""M5-A2.2.6: palette must stay rendered through the animated dismissal.

Root cause: ``ToolPopup.close_popup(animate=True)`` blanked every QML
source synchronously at dismissal start, so the 140ms slide-out ran
with empty QQuickWidgets. The fix defers guarded teardown to
``_finish_close`` (bridges still alive), keeps synchronous teardown for
``animate=False``, and invalidates stale completion timers via a close
generation.
"""

import pytest
from PySide6.QtCore import QRect
from PySide6.QtTest import QTest

from app.theme.tokens import dark_tokens
from tools.models import ToolType

pytestmark = pytest.mark.usefixtures("qapp")


def _make_popup(qtbot, tokens=None):
    from app.viewer import PdfViewer
    from document.manager import DocumentManager
    from app.toolshelf.tool_popup import PropertiesForm, ToolPopup

    tokens = tokens or dark_tokens()
    viewer = PdfViewer(DocumentManager())
    viewer.resize(1100, 750)
    viewer.show()
    qtbot.addWidget(viewer)
    QTest.qWait(100)
    slot = viewer.tool_store.add_slot(ToolType.PEN, name="P226")
    viewer.tool_shelf.refresh()
    popup = ToolPopup(tokens, viewer)
    form = PropertiesForm(slot, tokens, popup)
    popup.set_content(form)
    qtbot.addWidget(popup)
    popup.show_near(QRect(300, 300, 52, 52), viewer,
                    prefer_horizontal=True, animate=False)
    QTest.qWait(600)  # palette QML grid loads
    return viewer, popup, form


def _grid_view(form):
    return form._color_section._grid_view


def _is_live(view) -> bool:
    try:
        return view is not None and view.rootObject() is not None
    except Exception:
        return False


def test_animated_close_keeps_palette_until_transition_end(qtbot):
    _viewer, popup, form = _make_popup(qtbot)
    view = _grid_view(form)
    assert _is_live(view)
    emitted = []
    popup.closed.connect(lambda: emitted.append(1))

    popup.close_popup()  # animated canvas-click dismissal
    QTest.qWait(50)
    # Mid-transition: popup still visible with live QML content.
    assert popup.isVisible()
    assert _is_live(view)

    QTest.qWait(500)
    # After the transition: torn down, hidden, exactly one emit.
    assert not _is_live(view)
    assert not popup.isVisible()
    assert emitted == [1]


def test_immediate_close_tears_down_synchronously(qtbot):
    _viewer, popup, form = _make_popup(qtbot)
    view = _grid_view(form)
    assert _is_live(view)
    emitted = []
    popup.closed.connect(lambda: emitted.append(1))

    popup.close_popup(animate=False)
    QTest.qWait(100)
    assert not _is_live(view)
    assert not popup.isVisible()
    assert emitted == [1]


def test_interrupted_animated_close_single_teardown_single_emit(qtbot):
    _viewer, popup, form = _make_popup(qtbot)
    view = _grid_view(form)
    assert _is_live(view)
    emitted = []
    popup.closed.connect(lambda: emitted.append(1))

    popup.close_popup()  # animated close pending (200ms timer)
    QTest.qWait(30)
    popup.close_popup(animate=False)  # viewer-style interrupt
    QTest.qWait(500)  # stale animated timer fires harmlessly here
    assert not _is_live(view)
    assert not popup.isVisible()
    assert emitted == [1], f"stale timer must not re-emit: {emitted}"

    # A newly opened popup is unaffected by the old timer.
    _viewer2, popup2, form2 = _make_popup(qtbot)
    assert _is_live(_grid_view(form2))
    popup2.close_popup(animate=False)
    assert not popup2.isVisible()


def test_repeated_animated_open_close_cycles(qtbot):
    for _ in range(3):
        _viewer, popup, form = _make_popup(qtbot)
        view = _grid_view(form)
        assert _is_live(view)
        emitted = []
        popup.closed.connect(lambda: emitted.append(1))
        popup.close_popup()
        QTest.qWait(80)
        assert popup.isVisible() and _is_live(view)
        QTest.qWait(500)
        assert not popup.isVisible() and emitted == [1]
        popup.setParent(None)
        popup.deleteLater()
        QTest.qWait(100)


def test_close_event_tears_down_qml(qtbot):
    _viewer, popup, form = _make_popup(qtbot)
    view = _grid_view(form)
    assert _is_live(view)
    popup.close()
    QTest.qWait(200)
    assert not _is_live(view)
