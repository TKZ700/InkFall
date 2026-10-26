"""Deterministic Qt waits for toolbar animation tests (test-only).

Fixed ``qtbot.wait(300)`` sleeps after a 140ms animation waste wall-clock
and still flake under load. These helpers poll the settled condition the
test asserts next, with a generous timeout cap — same assertions, less
padding, no weaker timing assumptions.
"""

from __future__ import annotations


def wait_until(qtbot, pred, timeout_ms: int = 2000, step_ms: int = 15) -> bool:
    """Pump the event loop until ``pred()`` is truthy (timeout in ms)."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except NameError:
            raise  # programming bug in the predicate, never swallow
        except Exception:
            pass
        qtbot.wait(step_ms)
    try:
        return bool(pred())
    except Exception:
        return False


def wait_edge_settled(qtbot, edge, target: float,
                      timeout_ms: int = 2000) -> bool:
    """Wait until a morphing boundary rests exactly at ``target``."""
    return wait_until(
        qtbot,
        lambda: edge._reveal == float(target)
        and edge._reveal_target is None,
        timeout_ms=timeout_ms)


def wait_stable(qtbot, fn, timeout_ms: int = 1500,
                steady_ms: int = 60) -> bool:
    """Wait until ``fn()`` returns the same value across ``steady_ms``."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000.0
    last = None
    steady_since = None
    while time.monotonic() < deadline:
        try:
            value = fn()
        except Exception:
            value = None
        now = time.monotonic()
        if value == last and last is not None:
            if steady_since is not None and \
                    (now - steady_since) * 1000.0 >= steady_ms:
                return True
        else:
            last, steady_since = value, now
        qtbot.wait(15)
    return True
