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


SCENE_FILES = sorted(SCENE_DIR.rglob("*.py"))
# Checked at module level, not inside a test: a renamed/moved directory would otherwise collect
# zero parametrized cases and pass vacuously.
assert SCENE_DIR.is_dir() and SCENE_FILES, f"no modules found under {SCENE_DIR} (renamed?)"
assert ADAPTER.is_file(), f"missing {ADAPTER}"


def _absolute(module: str, level: int, py: Path) -> str:
    """The absolute module name a (possibly relative) import resolves to from inside ``py``.

    A relative import is NOT automatically inside the package: ``from ..session.outcome import X``
    in ``isar/workspace_api/scene.py`` reaches ``isar.session`` exactly like the absolute form, so
    the ban list has to be applied to the resolved name.
    """
    if not level:
        return module
    package = py.resolve().parent.relative_to(REPO_ROOT).parts     # __init__.py included: same pkg
    base = package[:len(package) - (level - 1)] if level > 1 else package
    return ".".join((*base, module)) if module else ".".join(base)


def _offenders_in(source: str, py: Path, forbidden):
    """``[(lineno, resolved module)]`` for every banned import in ``source``.

    Static only: a dynamic ``importlib.import_module(name)`` is out of scope. The fresh-interpreter
    probe below backstops that for ``isar.scene.model`` ONLY — it cannot cover the adapter, whose
    package ``__init__`` legitimately imports ``isar.session.state``, so there a dynamic import
    would go unseen and the static check is the whole enforcement.
    """
    out = []
    for node in ast.walk(ast.parse(source, filename=str(py))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # ``from .. import session`` names the module in the alias, not in node.module.
            names = ([_absolute(node.module, node.level, py)] if node.module else
                     [_absolute(alias.name, node.level, py) for alias in node.names])
        else:
            continue
        for name in names:
            if any(name == f or name.startswith(f + ".") for f in forbidden):
                out.append((node.lineno, name))
    return out


def _offenders(py: Path, forbidden):
    return _offenders_in(py.read_text(encoding="utf-8"), py, forbidden)


@pytest.mark.parametrize("py", SCENE_FILES, ids=lambda p: str(p.relative_to(SCENE_DIR)))
def test_scene_package_imports_nothing_forbidden(py):
    assert not _offenders(py, FORBIDDEN_PREFIXES), (
        f"isar/scene/{py.relative_to(SCENE_DIR)} imports a banned module: "
        f"{_offenders(py, FORBIDDEN_PREFIXES)}")


def test_adapter_imports_nothing_forbidden():
    assert not _offenders(ADAPTER, ADAPTER_FORBIDDEN_PREFIXES), (
        f"isar/workspace_api/scene.py imports a banned module: "
        f"{_offenders(ADAPTER, ADAPTER_FORBIDDEN_PREFIXES)}")


@pytest.mark.parametrize("source, expected", [
    ("from ..session.outcome import FitOutcome", "isar.session.outcome"),
    ("from ..scene.store import write", "isar.scene.store"),
    ("from .. import session", "isar.session"),               # module named in the alias
    ("from . import views", "isar.workspace_api.views"),      # in-package: allowed
    ("from isar.session import state", "isar.session"),
])
def test_the_checker_bites_on_relative_imports(source, expected):
    """The checker itself, on synthetic source placed at the adapter's path."""
    found = _offenders_in(source, ADAPTER, ADAPTER_FORBIDDEN_PREFIXES)
    banned = any(expected == f or expected.startswith(f + ".")
                 for f in ADAPTER_FORBIDDEN_PREFIXES)
    assert found == ([(1, expected)] if banned else []), f"{source!r} resolved wrong: {found}"


# The scene modules that claim to be stdlib-only. SCENE-4 PR 4.1 adds the question vocabulary
# (and, when it lands, the predicate evaluators) — the claim is only real while it is probed.
STDLIB_ONLY_MODULES = ("isar.scene.model", "isar.scene.questions")


@pytest.mark.parametrize("module", STDLIB_ONLY_MODULES)
def test_importing_a_stdlib_only_scene_module_pulls_in_nothing_heavy(module):
    """Clean interpreter: the DTO vocabulary is stdlib-only, numpy included."""
    banned = ("isar.session", "isar.analyze", "PySide6", "PyQt5", "PyQt6", "numpy")
    checks = "\n".join(
        f"assert not [m for m in sys.modules if m == {root!r} or m.startswith({root!r} + '.')], "
        f"{root!r} + ' leaked into {module}'"
        for root in banned)
    script = f"import sys\nimport {module}\n" + checks + "\nprint('SCENE_CLEAN_OK')\n"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=str(REPO_ROOT))
    assert out.returncode == 0 and "SCENE_CLEAN_OK" in out.stdout, (
        f"{module} is not import-clean:\nstdout={out.stdout}\nstderr={out.stderr}")
