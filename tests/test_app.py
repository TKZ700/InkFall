"""Application tests: imports + QApplication/MainWindow init."""

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


def test_modules_import_successfully():
    import annotations.models  # noqa: F401
    import app.application  # noqa: F401
    import app.main_window  # noqa: F401
    import core.diagnostics  # noqa: F401
    import core.errors  # noqa: F401
    import core.types  # noqa: F401
    import document.models  # noqa: F401
    import rendering.types  # noqa: F401
    import tools.models  # noqa: F401


def test_main_window_initializes(qapp):
    from app.main_window import MainWindow
    from app.viewer import PdfViewer

    window = MainWindow()
    assert window.windowTitle() == "Inkfall"
    # M4 shell: central widget is the nav shell; the viewer stays hosted
    # and remains the sole document-session owner.
    assert isinstance(window.viewer, PdfViewer)
    assert window.centralWidget() is not None
    assert window.viewer.window() is window
    assert window.library_page is not None
    assert window.library_service is not None
