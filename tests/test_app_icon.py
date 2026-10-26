"""Application icon tests: current PNG master, ICO and window wiring."""

from pathlib import Path

import pytest

BRAND = Path(__file__).resolve().parent.parent / "assets" / "icons"

pytestmark = pytest.mark.usefixtures("qapp")


def test_master_artwork_exists_and_loads():
    from PySide6.QtGui import QImage

    path = BRAND / "new_icon.png"
    assert path.is_file()
    image = QImage(str(path))
    assert not image.isNull()
    assert image.width() == image.height()
    assert image.hasAlphaChannel()


def test_ico_exists_and_loads():
    from PySide6.QtGui import QIcon

    ico = BRAND / "inkfall.ico"
    assert ico.is_file()
    assert ico.stat().st_size > 4096
    assert not QIcon(str(ico)).isNull()


def test_app_icon_helper_resolves():
    from app.icons import app_icon

    assert not app_icon().isNull()


def test_main_window_carries_brand_icon(qtbot, tmp_path):
    from app.main_window import MainWindow
    from document.manager import DocumentManager
    from library.service import LibraryService
    from library.store import LibraryStore

    store = LibraryStore(str(tmp_path / "lib.db"))
    store.open()
    try:
        window = MainWindow(DocumentManager(), library_service=LibraryService(store))
        qtbot.addWidget(window)
        assert not window.windowIcon().isNull()
    finally:
        store.close()
