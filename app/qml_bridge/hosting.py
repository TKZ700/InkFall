"""QQuickWidget hosting helper for inline popup drawers."""

from PySide6.QtCore import QUrl, Qt
from PySide6.QtQuickWidgets import QQuickWidget

_STYLE_SET = False


def _ensure_control_style() -> None:
    """Select the Basic Controls style once (before first engine use).

    Our TextFields carry fully custom backgrounds; without an explicit
    style Qt logs 'The current style does not support customization'
    warnings. Basic is QML-only (no QWidget impact).
    """
    global _STYLE_SET
    if _STYLE_SET:
        return
    _STYLE_SET = True
    try:
        from PySide6.QtQuickControls2 import QQuickStyle

        QQuickStyle.setStyle("Basic")
    except Exception:
        pass


def qml_url(relative: str) -> QUrl:
    """URL for a QML file below the active ``qml/`` resource root.

    Resolves beside the executable in a frozen build and at the
    repository root during development (see :mod:`app.resources`).
    """
    from app.resources import resource_path

    return QUrl.fromLocalFile(str(resource_path("qml") / relative))


def make_quick_editor(source: str, bridge, theme_bridge,
                      object_name: str, parent=None) -> QQuickWidget:
    """Build a QQuickWidget bound to ``bridge`` + ``theme`` context props.

    SizeRootObjectToView so the QML root follows the popup width; height
    is synced from the root implicitHeight (see sync_height below).

    Raises RuntimeError with the engine errors when the QML source
    fails to load (never a silent half-built view).
    """
    if bridge is None or theme_bridge is None:
        raise RuntimeError(
            f"QML editor {source}: mandatory bridges missing "
            f"(bridge={bridge}, theme={theme_bridge})")
    _ensure_control_style()
    view = QQuickWidget(parent)
    view.setObjectName(object_name)
    view.setResizeMode(
        QQuickWidget.ResizeMode.SizeRootObjectToView)
    view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    view.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
    try:
        view.setClearColor(Qt.GlobalColor.transparent)
    except Exception:
        pass
    engine = view.engine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.rootContext().setContextProperty("theme", theme_bridge)
    view.setSource(qml_url(source))
    validate_quick_view(view, source)
    return view


def validate_quick_view(view: QQuickWidget, source: str) -> None:
    """Fail loudly on QML load errors (never silent null bindings).

    Note: a local file may still be Loading (async); only Error fails
    here. Callers re-check on statusChanged for post-load failures.
    """
    try:
        if view.status() == QQuickWidget.Status.Error:
            try:
                details = "; ".join(e.toString() for e in view.errors())
            except Exception:
                details = "<unreadable engine errors>"
            raise RuntimeError(
                f"QML failed to load {source}: {details}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"QML validation failed for {source}: {exc}") from exc


def check_view_error(view: QQuickWidget, source: str) -> str | None:
    """Late (async) error check for statusChanged handlers.

    Returns the error text when the view reached Error, else None, so
    handlers can report instead of silently ignoring a dead component.
    """
    try:
        if view.status() == QQuickWidget.Status.Error:
            try:
                return "; ".join(e.toString() for e in view.errors())
            except Exception:
                return "<unreadable engine errors>"
    except Exception as exc:
        return f"validation failed: {exc}"
    return None


def shutdown_quick_view(view) -> None:
    """Destroy a view's QML component synchronously (teardown path).

    Blank the source while bridges are still alive so engine teardown
    can never evaluate bindings against deleted context objects (the
    null-TypeError class on close/delete). Safe to call twice.
    """
    try:
        if view is None:
            return
        from PySide6.QtCore import QUrl as _QUrl

        try:
            view.setSource(_QUrl())
        except Exception:
            pass
    except Exception:
        pass


def sync_height(view: QQuickWidget, fallback: int = 56,
                _attempt: int = 0) -> int:
    """Fix the widget height to the QML root implicitHeight.

    Retries a few event-loop turns: at Ready the root may not have
    completed layout yet (implicitHeight 0/None on the first pass).
    """
    from PySide6.QtCore import QTimer

    height = 0
    try:
        root = view.rootObject()
        if root is not None:
            try:
                height = int(root.property("implicitHeight") or 0)
            except Exception:
                height = 0
    except Exception:
        height = 0
    if height <= 0 and _attempt < 6:
        try:
            QTimer.singleShot(
                20, lambda: sync_height(view, fallback, _attempt + 1))
        except Exception:
            pass
        height = fallback if _attempt > 0 else 0
        if height <= 0:
            return 0
    if height <= 0:
        height = fallback
    try:
        view.setFixedHeight(max(40, height))
    except Exception:
        pass
    return height
