"""Application bootstrap (M4: shell with Library + viewer)."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from app.main_window import MainWindow
from app.theme.manager import ThemeManager
from document.manager import DocumentManager

#: Qt Quick rendering backend forced for all InkFall launches (M5-A2.2.5).
#: The default D3D11 RHI device created on first QQuickWidget init
#: triggers an NVIDIA-overlay-hooked native-window destroy+recreate
#: (main-window HWND change on first property popup); the software
#: adaptation creates no GPU device and avoids the hook entirely.
_QUICK_BACKEND_ENV = "QT_QUICK_BACKEND"
_QUICK_BACKEND_FORCED = "software"


def _ensure_quick_backend() -> str:
    """Force the Qt Quick software backend before any scene graph exists.

    Qt reads ``QT_QUICK_BACKEND`` at first scene-graph initialization
    (equivalent to ``QQuickWindow.setSceneGraphBackend("software")``
    early in ``main()``); setting it here in-process is identical to a
    pre-launch export because our first ``QQuickWidget`` is created
    lazily at the first property popup, long after startup.

    Policy: production always runs ``software``. A pre-existing
    conflicting value is overwritten and reported via ``qWarning``
    (previous value + reason) — never silently. Unset/empty is set
    quietly; an existing ``software`` (any case) is kept as-is.
    """
    try:
        current = str(os.environ.get(_QUICK_BACKEND_ENV, "") or "")
    except Exception:
        current = ""
    if current.strip().lower() == _QUICK_BACKEND_FORCED:
        return current
    if current.strip():
        try:
            from PySide6.QtCore import qWarning

            qWarning(
                f"Inkfall requires QT_QUICK_BACKEND={_QUICK_BACKEND_FORCED} "
                f"(NVIDIA-overlay HWND stability); overriding explicit "
                f"value {current!r}.")
        except Exception:
            pass
    try:
        os.environ[_QUICK_BACKEND_ENV] = _QUICK_BACKEND_FORCED
    except Exception:
        pass
    return _QUICK_BACKEND_FORCED


def create_application(argv: list[str] | None = None) -> QApplication:
    """Create (or reuse) the QApplication instance."""
    # Backend pin first: must precede any Qt Quick initialization on
    # every launch path (normal, IDE, packaged, diagnostic, tests),
    # including reuse of an existing QApplication.
    _ensure_quick_backend()
    existing = QApplication.instance()
    if existing is not None:
        assert isinstance(existing, QApplication)
        return existing
    app = QApplication(argv if argv is not None else sys.argv)
    app.setOrganizationName("Inkfall")
    app.setApplicationName("Inkfall")
    # Bundled Inter + Vazirmatn before any widget exists: English UI
    # resolves to Inter and Persian/Arabic runs fall back to Vazirmatn,
    # even where neither font is installed (never raises).
    try:
        from app.fonts import apply_application_font, load_bundled_fonts

        load_bundled_fonts()
        apply_application_font()
    except Exception:
        pass
    return app


def create_document_manager() -> DocumentManager:
    """Programmatic smoke path: instantiate the document lifecycle owner."""
    return DocumentManager()


def run() -> int:
    """Start Inkfall (Library shell around the PDF viewer)."""
    app = create_application()
    manager = create_document_manager()
    theme = ThemeManager()
    theme.apply()
    window = MainWindow(manager, theme=theme)
    window.show()
    return app.exec()
