"""M5-A2.2.5: Qt Quick software-backend pin (NVIDIA-overlay HWND fix).

Policy under test (see ``app.application._ensure_quick_backend``):
production always forces ``QT_QUICK_BACKEND=software``; a pre-existing
conflicting value is overridden (reported via qWarning, never
silently); unset/empty is set quietly; existing ``software`` (any
case) is kept.

The backend-active proof asserts against Qt diagnostics
(``QQuickWindow.sceneGraphBackend()`` after a real ``QQuickWidget``
exists) — never the environment variable alone.
"""

import os

import pytest

pytestmark = pytest.mark.usefixtures("qapp")

_ENV = "QT_QUICK_BACKEND"


def _pin():
    from app.application import _ensure_quick_backend

    return _ensure_quick_backend()


def test_unset_backend_is_set_quietly(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    assert _pin() == "software"
    assert os.environ.get(_ENV) == "software"


def test_empty_backend_is_set(monkeypatch):
    monkeypatch.setenv(_ENV, "")
    assert _pin() == "software"
    assert os.environ.get(_ENV) == "software"


def test_conflicting_backend_is_forced_to_software(monkeypatch):
    monkeypatch.setenv(_ENV, "opengl")
    assert _pin() == "software"
    assert os.environ.get(_ENV) == "software"


def test_existing_software_is_kept(monkeypatch):
    monkeypatch.setenv(_ENV, "Software")
    assert _pin() == "Software"
    assert os.environ.get(_ENV) == "Software"


def test_create_application_applies_pin(monkeypatch):
    """The production entry point forces the backend on every launch."""
    monkeypatch.delenv(_ENV, raising=False)
    from app.application import create_application

    create_application([])
    assert os.environ.get(_ENV) == "software"


def test_software_backend_actively_reported(monkeypatch, qapp):
    """Qt diagnostics confirm the software adaptation is really active."""
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtQuickWidgets import QQuickWidget
    from PySide6.QtTest import QTest

    pre = QQuickWindow.sceneGraphBackend()
    if pre and pre.lower() != "software":
        pytest.skip(
            f"scene graph already initialized in-process as {pre!r}; "
            f"backend selection is first-init-wins (not a product failure)")
    monkeypatch.setenv(_ENV, "software")
    view = QQuickWidget()
    view.resize(200, 200)
    view.show()
    QTest.qWait(300)
    try:
        assert QQuickWindow.sceneGraphBackend() == "software"
    finally:
        try:
            view.hide()
            view.setParent(None)
            view.deleteLater()
        except Exception:
            pass
