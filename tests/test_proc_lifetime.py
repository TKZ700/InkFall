"""Helper-process lifetime: Job Object kill-on-close + child guards.

The crash-cleanup regression: a parent that dies abruptly used to leave
its spawn workers blocked in ``task_queue.get()`` forever. These tests
prove the fix with REAL process death — a subprocess launcher assigns
the job, spawns a blocking multiprocessing child, and then dies (hard
``os._exit`` or clean exit); the test polls the child's PID until it is
gone. A passing fixture teardown proves nothing here, by design; every
liveness check observes the OS, not Python objects.

Static guards pin the wiring: every child entry point must install the
guard, and the app entry must assign the job + call freeze_support.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.instance_registry import pid_alive

REPO_ROOT = Path(__file__).resolve().parents[1]

#: How long a terminated child may linger after parent death (generous:
#: job kill-on-close is kernel-side and near-instant).
_CHILD_REAP_TIMEOUT_S = 20.0

_LAUNCHER = r"""
import multiprocessing
import os
import sys
import time


def _child(ready):
    from core.proc_lifetime import install_child_guard

    install_child_guard()
    ready.put(os.getpid())
    while True:
        time.sleep(0.2)  # block forever; only the parent's death ends this


def main(mode):
    from core.proc_lifetime import assign_parent_job

    assign_parent_job()
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    proc = ctx.Process(target=_child, args=(ready,), name="inkfall-job-probe")
    proc.start()
    print(ready.get(timeout=60), flush=True)
    if mode == "crash":
        os._exit(1)  # hard death: no atexit, no teardown, no joins
    # "clean": return normally; the job handle closes with the process.


if __name__ == "__main__":
    main(sys.argv[1])
"""


def _run_launcher(tmp_path: Path, mode: str) -> int:
    """Run the launcher; return the reported child PID (parent then dies)."""
    launcher = tmp_path / f"launcher_{mode}.py"
    launcher.write_text(_LAUNCHER, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("INKFALL_DISABLE_JOB_GUARD", None)
    result = subprocess.run(
        [sys.executable, str(launcher), mode],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
        env=env)
    assert result.returncode == 0 or mode == "crash", (
        f"launcher failed:\n{result.stdout}\n{result.stderr}")
    pids = [line.strip() for line in result.stdout.splitlines()
            if line.strip().isdigit()]
    assert pids, f"launcher never reported the child PID:\n{result.stderr}"
    return int(pids[-1])


def _wait_until_dead(pid: int) -> bool:
    deadline = time.monotonic() + _CHILD_REAP_TIMEOUT_S
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_job_object_kills_children_when_parent_crashes(tmp_path: Path) -> None:
    """A hard-killed job-protected parent takes its helpers with it."""
    pid = _run_launcher(tmp_path, "crash")
    assert _wait_until_dead(pid), (
        f"child {pid} survived its parent's hard crash — orphaned helper "
        "regression (Job Object kill-on-close not in effect)")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_job_object_kills_children_when_parent_exits(tmp_path: Path) -> None:
    """Even a clean parent exit leaves no owned helper behind."""
    pid = _run_launcher(tmp_path, "clean")
    assert _wait_until_dead(pid), (
        f"child {pid} outlived a cleanly exited parent")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_job_assignment_is_idempotent_and_tolerates_failure(monkeypatch) -> None:
    """Assignment never breaks startup and never runs twice."""
    import core.proc_lifetime as pl

    monkeypatch.setattr(pl, "_job_assigned", False)
    monkeypatch.setenv("INKFALL_DISABLE_JOB_GUARD", "1")
    assert pl.assign_parent_job() is False, (
        "the opt-out environment variable must disable assignment")
    monkeypatch.delenv("INKFALL_DISABLE_JOB_GUARD")
    assert pl.assign_parent_job() is True
    assert pl.assign_parent_job() is True

    # A failure inside the assignment must be swallowed (backstop only):
    # a fresh module state with a broken ctypes path stays non-fatal.
    monkeypatch.setattr(pl, "_job_assigned", False)
    monkeypatch.setattr(pl, "_assign_job_windows",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pl.assign_parent_job() is False


@pytest.mark.skipif(sys.platform != "linux", reason="PDEATHSIG is Linux-only")
def test_pdeathsig_guard_kills_child_when_parent_crashes(tmp_path: Path) -> None:
    """On Linux the child-side guard covers parents without a Job Object."""
    pid = _run_launcher(tmp_path, "crash")
    assert _wait_until_dead(pid), (
        f"child {pid} survived its parent's crash without PDEATHSIG")


# ---------------------------------------------------------------------------
# static wiring guards (all platforms)
# ---------------------------------------------------------------------------


def _calls_in_function(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            names: set[str] = set()
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Name):
                    names.add(call.func.id)
                elif isinstance(call.func, ast.Attribute):
                    names.add(call.func.attr)
            return names
    raise AssertionError(f"function {function_name} not found in {path}")


def test_child_entry_points_install_the_lifetime_guard() -> None:
    """Every owned child entry must install the child-side guard first."""
    for module, entry in (
            (REPO_ROOT / "rendering" / "process_decoder.py", "_worker_main"),
            (REPO_ROOT / "document" / "backend.py", "_export_child_main")):
        calls = _calls_in_function(module, entry)
        assert "install_child_guard" in calls, (
            f"{module.name}:{entry} must call install_child_guard() so a "
            "crashed parent cannot orphan it on non-Windows platforms")


def test_app_entry_assigns_job_and_freeze_support() -> None:
    """main() must assign the job and bootstrap frozen multiprocessing."""
    calls = _calls_in_function(REPO_ROOT / "main.py", "main")
    assert "assign_parent_job" in calls, (
        "main() must assign the kill-on-close Job Object so owned helper "
        "processes die with the app")
    assert "freeze_support" in calls, (
        "main() must call multiprocessing.freeze_support() for the "
        "standalone build's spawn children")
