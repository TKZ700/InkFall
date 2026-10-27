"""Color-picker diagnostics regression tests (release issue 2).

The packaged-build color-picker crash dies silently (windowed build, no
console, no crash reporter), and it could not be reproduced offscreen —
real QML click delivery with the production software backend opens,
closes and reopens the picker without a fault. These tests pin the two
mitigations that turn the next occurrence into actionable evidence:

1. The packaged self test must instantiate the real picker card
   (QQuickWidget + bridges), not merely compile the QML, so a
   frozen-build hosting defect fails the CI smoke step with details.
2. Fatal crashes must leave a diagnostics file with the faulting
   thread's stack (faulthandler), removed again on clean exit.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("qapp")

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_selftest_picker_runtime_check_instantiates_the_card() -> None:
    """The self test builds the picker card and records Ready, no errors."""
    from app.selftest import _check_picker_runtime

    report: dict = {"checks": {}, "errors": []}
    _check_picker_runtime(report)

    result = report["checks"]["picker_runtime"]
    assert result["status"] == "Ready", result
    assert result["errors"] == []
    assert report["errors"] == []


def test_fatal_crash_leaves_a_diagnostics_file(tmp_path: Path) -> None:
    """A native abort writes a stack-trace file; clean exits remove it."""
    child = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "sys.path.insert(0, r'%s')\n"
        "import main\n"
        "main._enable_fatal_diagnostics()\n"
        "import faulthandler\n"
        "assert faulthandler.is_enabled()\n"
        "os.abort()\n" % REPO_ROOT
    )
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True,
        timeout=120, env=env, cwd=str(REPO_ROOT))

    dumps = glob.glob(os.path.join(
        tempfile.gettempdir(), "inkfall-fatal-*.log"))
    try:
        # The child aborted: the dump file must survive with a stack.
        assert result.returncode != 0
        assert dumps, "fatal crash produced no diagnostics file"
        content = open(dumps[-1], encoding="utf-8", errors="replace").read()
        assert "Fatal Python error" in content, content[:400]
        assert "Current thread" in content, content[:400]
        # The stack must name the faulting frame (the child's abort line).
        assert "line 8 in <module>" in content, content[:400]
    finally:
        for path in dumps:
            try:
                os.remove(path)
            except OSError:
                pass


def test_clean_exit_removes_the_diagnostics_file() -> None:
    """A clean process removes its faulthandler file (no litter)."""
    child = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
        "sys.path.insert(0, r'%s')\n"
        "import main\n"
        "main._enable_fatal_diagnostics()\n"
        "import faulthandler, glob, tempfile\n"
        "mine = glob.glob(os.path.join(tempfile.gettempdir(), "
        "'inkfall-fatal-*.log'))\n"
        "assert len(mine) == 1, mine\n"
        "sys.exit(0)\n" % REPO_ROOT
    )
    result = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True,
        timeout=120, cwd=str(REPO_ROOT))
    assert result.returncode == 0, result.stderr
