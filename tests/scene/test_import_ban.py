"""The scene layer's import boundary, enforced structurally (SCENE-3 PR 3a).

The scene must never be able to re-score local data: it reads frozen DTOs built by the
workspace adapter, never the fitter, the session writer, or the driver. That ban is only real
if it is checked, so this mirrors ``tests/test_qt_free_core.py``: an AST-walk half (catches a
lazily-imported violation inside a function body too) and a clean-subprocess half.
"""
import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENE_DIR = REPO_ROOT / "isar" / "scene"
ADAPTER = REPO_ROOT / "isar" / "workspace_api" / "scene.py"

# Modules the scene layer may not import, each naming what it would drag in: the session writer
# and the driver (writes + campaign control), the GUI (Qt), and the fitting/evidence machinery
# (the double-counting risk — a scene factor able to re-run a likelihood).
FORBIDDEN_PREFIXES = (
    "isar.session", "isar.drive", "isar_gui",
    "isar.analyze.loop", "isar.analyze.bayes", "isar.analyze.evidence", "isar.analyze.fit",
    "PySide6", "PyQt5", "PyQt6", "pyqtgraph",
)
# The adapter lives on the workspace side, so it legitimately reads workspace views — but it
# must never touch the writer or the scene store's WRITE surface (the store lands later in
# SCENE-3; this assertion is written so it holds the day it does).
ADAPTER_FORBIDDEN_PREFIXES = (
    "isar.session", "isar.scene.store", "PySide6", "PyQt5", "PyQt6", "pyqtgraph",
)


def _imported_modules(py: Path):
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import — stays inside the package
                continue
            yield node.lineno, (node.module or "")


def _offenders(py: Path, forbidden):
    return [(lineno, mod) for lineno, mod in _imported_modules(py)
            if any(mod == f or mod.startswith(f + ".") for f in forbidden)]


@pytest.mark.parametrize("py", sorted(SCENE_DIR.rglob("*.py")),
                         ids=lambda p: str(p.relative_to(SCENE_DIR)))
def test_scene_package_imports_nothing_forbidden(py):
    assert SCENE_DIR.is_dir() and py.is_file()
    assert not _offenders(py, FORBIDDEN_PREFIXES), (
        f"isar/scene/{py.relative_to(SCENE_DIR)} imports a banned module: "
        f"{_offenders(py, FORBIDDEN_PREFIXES)}")


def test_adapter_imports_nothing_forbidden():
    assert not _offenders(ADAPTER, ADAPTER_FORBIDDEN_PREFIXES), (
        f"isar/workspace_api/scene.py imports a banned module: "
        f"{_offenders(ADAPTER, ADAPTER_FORBIDDEN_PREFIXES)}")


def test_importing_scene_model_pulls_in_nothing_heavy():
    """Clean interpreter: the DTO vocabulary is stdlib-only, numpy included."""
    banned = ("isar.session", "isar.analyze", "PySide6", "PyQt5", "PyQt6", "numpy")
    checks = "\n".join(
        f"assert not [m for m in sys.modules if m == {root!r} or m.startswith({root!r} + '.')], "
        f"{root!r} + ' leaked into isar.scene.model'"
        for root in banned)
    script = "import sys\nimport isar.scene.model\n" + checks + "\nprint('SCENE_CLEAN_OK')\n"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=str(REPO_ROOT))
    assert out.returncode == 0 and "SCENE_CLEAN_OK" in out.stdout, (
        f"isar.scene.model is not import-clean:\nstdout={out.stdout}\nstderr={out.stderr}")
