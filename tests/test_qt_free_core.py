"""Design ruling G1's import boundary: ISAR's core (geometry, plotspec, workspace_api) must
never pull in Qt. This is the permanent M0 gate for Phase 0 — once the GUI package (with
PySide6/pyqtgraph) lands, nothing in these packages may start depending on it.

Modeled on tests/drive/test_boundary.py: an AST-walk half (static, catches lazily-imported
violations inside function bodies too) and a clean-subprocess half (runtime check, since the
pytest process itself may be polluted by other tests).
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ISAR_DIR = REPO_ROOT / "isar"

CORE_DIRS = {
    "geometry": ISAR_DIR / "geometry",
    "plotspec": ISAR_DIR / "plotspec",
    "workspace_api": ISAR_DIR / "workspace_api",
}
CORE_FILES = {"sandbox_api": ISAR_DIR / "sandbox_api.py"}

# plotspec/backends/mpl.py is explicitly allowed to import matplotlib; it is not part of the
# Qt-free surface being enforced here.
EXCLUDED = {ISAR_DIR / "plotspec" / "backends" / "mpl.py"}

FORBIDDEN_QT = {"PySide6", "PyQt5", "PyQt6", "pyqtgraph"}
# geometry is meant to be a leaf module with no analysis/heavy deps either.
FORBIDDEN_GEOMETRY_EXTRA = {"scipy", "matplotlib", "tavi", "mcp"}


def _modules():
    mods = []
    for base in CORE_DIRS.values():
        mods.extend(sorted(base.rglob("*.py")))
    mods.extend(CORE_FILES.values())
    return [m for m in mods if m not in EXCLUDED]


def _forbidden_roots_for(py: Path):
    forbidden = set(FORBIDDEN_QT)
    if CORE_DIRS["geometry"] in py.parents:
        forbidden |= FORBIDDEN_GEOMETRY_EXTRA
    return forbidden


def _root_of(name: str) -> str:
    return name.split(".", 1)[0]


def test_core_packages_exist_and_have_modules():
    for name, base in CORE_DIRS.items():
        mods = sorted(base.rglob("*.py"))
        assert mods, f"no modules found under isar/{name}/ (renamed or removed?)"
    for name, path in CORE_FILES.items():
        assert path.is_file(), f"Qt-free core module isar/{name}.py is missing"


@pytest.mark.parametrize("py", _modules(), ids=lambda p: str(p.relative_to(ISAR_DIR)))
def test_no_forbidden_imports_ast(py):
    forbidden = _forbidden_roots_for(py)
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _root_of(alias.name) in forbidden:
                    offenders.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — stays within the same package
                continue
            mod = node.module or ""
            if _root_of(mod) in forbidden:
                offenders.append((node.lineno, mod))
    assert not offenders, (
        f"{py.relative_to(ISAR_DIR)} imports forbidden modules (Qt-free core boundary): "
        f"{offenders}")


def test_importing_core_does_not_pull_in_qt_or_scipy():
    """Runtime check in a clean interpreter (subprocess): importing isar's Qt-free core packages
    must not pull in PySide6/pyqtgraph, and importing isar.workspace_api specifically must not
    pull in scipy (the facade promises a scipy-free open; scipy may load only lazily, on
    evaluate_model's audit fallback). matplotlib is NOT asserted on here — it's a hard dependency
    of isar (plotspec has an mpl backend) and is expected to be present.
    """
    qt_checks = "\n".join(
        f"assert not [m for m in sys.modules if m == {root!r} or m.startswith({root!r} + '.')], "
        f"'{root} leaked into isar core import'"
        for root in sorted(FORBIDDEN_QT)
    )
    script = (
        "import sys\n"
        "import isar\n"
        "import isar.workspace_api\n"
        "import isar.geometry\n"
        "import isar.plotspec.spec\n"
        f"{qt_checks}\n"
        "assert 'scipy' not in sys.modules, "
        "'scipy leaked into isar.workspace_api open (should be lazy)'\n"
        "print('QT_FREE_CORE_OK')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, (
        f"clean-interpreter Qt-free-core import failed:\nstdout={out.stdout}\nstderr={out.stderr}")
    assert "QT_FREE_CORE_OK" in out.stdout, (
        f"sentinel missing from subprocess output:\nstdout={out.stdout}\nstderr={out.stderr}")


def test_sandbox_compute_module_is_qt_free():
    """The G7 API runs on a worker thread, so importing it must pull in no Qt (G7/§6)."""
    qt_checks = "\n".join(
        f"assert not [m for m in sys.modules if m == {root!r} or m.startswith({root!r} + '.')], "
        f"'{root} leaked into isar.sandbox_api import'"
        for root in sorted(FORBIDDEN_QT)
    )
    script = "import sys\nimport isar.sandbox_api\n" + qt_checks + "\nprint('SANDBOX_QT_FREE_OK')\n"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0 and "SANDBOX_QT_FREE_OK" in out.stdout, (
        f"isar.sandbox_api is not Qt-free:\nstdout={out.stdout}\nstderr={out.stderr}")
