"""Toast feedback tests (M4.6.1): caps, structure, expiry, motion."""

import pytest

from app.toast import TOAST_MAX_WIDTH_PX, ToastHost

pytestmark = pytest.mark.usefixtures("qapp")


@pytest.fixture()
def toast(qtbot):
    from app.theme.tokens import dark_tokens

    host = ToastHost(dark_tokens())
    qtbot.addWidget(host)
    host.show()
    return host


def test_success_compact(toast, qtbot):
    toast.show_message('Imported "Calculus.pdf"')
    assert toast.pending_count() >= 1
    qtbot.waitUntil(lambda: toast.pending_count() == 0, timeout=8000)


def test_long_error_capped(toast, qtbot):
    long_path = "C:\\" + "\\".join(["very-long-directory-name"] * 12) + "\\doc.pdf"
    toast.show_message("Import failed", f"Could not open {long_path}: access denied (OS 5)")
    # Geometry cap holds regardless of message length.
    assert toast._pill.maximumWidth() == TOAST_MAX_WIDTH_PX
    assert TOAST_MAX_WIDTH_PX <= 420
    qtbot.waitUntil(lambda: toast.pending_count() == 0, timeout=9000)


def test_structured_title_detail(toast, qtbot):
    toast.show_message("Import failed", "Could not open the selected PDF.", kind="error")
    qtbot.waitUntil(lambda: toast.pending_count() == 0, timeout=9000)
    assert toast._label.toolTip() == ""
    assert "Import failed" in toast._label.accessibleDescription()


def test_empty_ignored(toast):
    assert toast.pending_count() == 0
    toast.show_message("   ")
    assert toast.pending_count() == 0


def test_dismiss_clears_effect(toast, qtbot):
    toast.show_message("Hello")
    qtbot.waitUntil(lambda: toast.pending_count() == 0, timeout=8000)
    assert toast._pill.graphicsEffect() is None


def test_theme_tokens_applied(toast):
    from app.theme.tokens import light_tokens

    toast.update_tokens(light_tokens())
    assert toast._tokens.name == "light"
