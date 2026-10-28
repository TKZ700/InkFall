"""Shared pytest fixtures (M4.6.7, M4.6.12).

Isolates per-test QSettings state so persisted UI preferences
(section collapse, theme mode) never leak between tests — or from
manual smoke scripts run under the same OS user profile.

M4.6.12: forces headless Qt (offscreen platform) before any Qt
binding loads so automated tests never open visible desktop
windows. Real compositor behavior (DWM corners, native resize
feel, Snap/DPI) stays a manual real-desktop QA item.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(autouse=True)
def _isolate_inkfall_settings():
    try:
        from PySide6.QtCore import QSettings

        for key in ("recent", "categories", "pinned", "documents"):
            QSettings().remove(f"library/sections/{key}_collapsed")
    except Exception:
        pass
    yield
    try:
        from PySide6.QtCore import QSettings

        for key in ("recent", "categories", "pinned", "documents"):
            QSettings().remove(f"library/sections/{key}_collapsed")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_instance_registry(tmp_path, monkeypatch):
    """Cross-instance coordination files must never touch the real
    per-user registry (and must not leak between tests or parallel
    runs): every MainWindow creates an InstanceHub whose registry file
    would otherwise advertise a phantom peer to other InkFall windows
    on this machine."""
    monkeypatch.setenv("INKFALL_INSTANCES_DIR", str(tmp_path / "instances"))
