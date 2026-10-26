"""Architecture boundary tests (M1/M2).

PyMuPDF: only ``document/backend.py``, ``rendering/renderer.py`` and
``rendering/workers.py`` (plus test helpers/benchmarks) may import it.
Qt: ``rendering/*`` (engine must stay GUI-independent) and all lower
layers (``core/*``, ``document/*``, ``annotations/*``, ``tools/*``) must
never import Qt. Only ``app/*`` and ``main.py`` may.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOWED = {
    Path("document/backend.py"),
    Path("rendering/renderer.py"),
    Path("rendering/workers.py"),
    Path("benchmarks/document_open.py"),
    Path("benchmarks/renderer_benchmark.py"),
}

# Test-only helpers are allowed to use PyMuPDF to generate fixtures.
ALLOWED_PREFIXES = (Path("tests/helpers.py"),)


def _imports(source: str, names: set[str]) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in names:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in names:
                return True
    return False


def _imports_pymupdf(source: str) -> bool:
    return _imports(source, {"fitz", "pymupdf"})


def test_only_backend_imports_pymupdf():
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        if rel in ALLOWED or rel.as_posix().startswith("tests/helpers"):
            continue
        if rel.parts and rel.parts[0] == "tests" and rel.name != "helpers.py":
            # Test files must not import PyMuPDF directly either (use helpers/backend).
            pass
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _imports_pymupdf(source):
            offenders.append(rel.as_posix())
    assert offenders == [], f"PyMuPDF imported outside approved modules: {offenders}"


def test_no_qt_in_rendering_or_lower_layers():
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT)
        if not rel.parts or rel.parts[0] not in ("rendering", "core", "document",
                                                 "annotations", "tools"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _imports(source, {"PySide6", "PySide2", "PyQt5", "PyQt6"}):
            offenders.append(rel.as_posix())
    assert offenders == [], f"Qt imported outside app/: {offenders}"


def test_no_raw_fitz_types_in_models():
    for rel in ("document/models.py", "document/document.py", "document/manager.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert "fitz." not in source
        assert "pymupdf." not in source


def test_viewer_may_use_qt_but_never_fitz():
    viewer = (ROOT / "app" / "viewer.py").read_text(encoding="utf-8")
    assert _imports(viewer, {"PySide6", "PySide2", "PyQt5", "PyQt6"})
    assert not _imports_pymupdf(viewer)


def test_viewport_controller_is_qt_and_fitz_free():
    controller = (ROOT / "app" / "viewport_controller.py").read_text(encoding="utf-8")
    assert not _imports(controller, {"PySide6", "PySide2", "PyQt5", "PyQt6"})
    assert not _imports_pymupdf(controller)
