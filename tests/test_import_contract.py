"""A-11 import-contract lint (design ruling G16): the GUI's dependency surface, enforced statically.

``isar_gui`` is Qt land and may lean on ISAR's *thinking* only through the sanctioned read facade,
apart from the one G7 sandbox viewmodel → ``isar.sandbox_api`` seam — never by importing a fit
internal, the driver, the reducer, a parser, etc. Symmetrically,
``isar.workspace_api`` (the read facade) is allowed a fixed, enumerated set of ISAR modules and
nothing more: the canonical collation key, the schema/session state readers, the params-only curve
evaluator's dependencies, and the vendored geometry.

This walks the AST of every module under ``isar_gui/`` and ``isar/workspace_api/`` (static, so a
lazily-imported violation inside a function body is caught too, exactly as ``tests/drive``'s boundary
test does) and asserts each ``isar.*`` import targets an allowed root. This test and
docs/DECISIONS.md G16 are joint sources for the allow-lists; the two are kept in sync — see G16
for the ruling's rationale and phasing.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUI_DIR = REPO_ROOT / "isar_gui"
WORKSPACE_API_DIR = REPO_ROOT / "isar" / "workspace_api"

# G16: isar_gui may import ONLY the read facade, the vendored geometry, and the pure plotspec layer
# (any depth below those). Everything else from isar.* — drive, session, analyze, reduce, report,
# schema, cli, parsers … — is forbidden.
GUI_ALLOWED = {"isar.workspace_api", "isar.geometry", "isar.plotspec"}

# G7: only the sandbox viewmodel may cross from Qt land to the one frozen-config preview API.
SANDBOX_VIEWMODEL = GUI_DIR / "viewmodels" / "sandbox.py"
SANDBOX_VM_ALLOWED = GUI_ALLOWED | {"isar.sandbox_api"}

# G16: the read facade's own sanctioned imports (the one definition of "a dataset" plus the exact
# schema/session/report/analyze pieces it reads through, and the vendored geometry).
WORKSPACE_API_ALLOWED = {
    "isar.schema.card",
    "isar.session.state",
    "isar.session.workspace",
    "isar.session.unit_data",
    "isar.drive.session_log",
    "isar.drive.isar_io.readers",
    "isar.report.audit",
    "isar.analyze.components",
    # The shared Qt-free manifest→curve reconstruction the facade evaluates through; the same path
    # the predictive producer and evaluator use, so a workspace curve cannot disagree with a stored
    # prediction.
    "isar.analyze.model_manifest",
    "isar.geometry",
    "isar.reduce.collate",
    # config/consent validators the GUI reaches through the facade (config_gates.py): the LLM
    # endpoint safety check + cloud-fallback default, both Qt-free/scipy-free.
    "isar.reduce.llm_client",
    "isar.reduce.adjudicate",
}


def _constrained(module: str) -> bool:
    """True only for imports rooted at the ``isar`` package itself — NOT ``isar_gui`` (which shares a
    prefix but is a different top-level package) and not third-party modules."""
    return module == "isar" or module.startswith("isar.")


def _allowed(module: str, allowlist: set) -> bool:
    return any(module == a or module.startswith(a + ".") for a in allowlist)


def _offenders(py: Path, allowlist: set) -> list:
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _constrained(alias.name) and not _allowed(alias.name, allowlist):
                    offenders.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — stays within the same package
                continue
            mod = node.module or ""
            if not _constrained(mod):
                continue
            if _allowed(mod, allowlist):
                continue
            # e.g. `from isar.reduce import collate` — the resolved name may still be allowed.
            for alias in node.names:
                full = f"{mod}.{alias.name}"
                if not _allowed(full, allowlist):
                    offenders.append((node.lineno, full))
    return offenders


def _gui_modules():
    return [p for p in sorted(GUI_DIR.rglob("*.py")) if p != SANDBOX_VIEWMODEL]


def _workspace_api_modules():
    return sorted(WORKSPACE_API_DIR.rglob("*.py"))


# ---- nonempty-module-list guards (Task-6 lesson: a bad glob must fail, not vacuously pass) --------
def test_gui_package_exists_and_has_modules():
    assert _gui_modules(), "no modules found under isar_gui/ (renamed or removed?)"


def test_workspace_api_package_exists_and_has_modules():
    assert _workspace_api_modules(), "no modules found under isar/workspace_api/ (renamed or removed?)"


# ---- the contract ---------------------------------------------------------
@pytest.mark.parametrize("py", _gui_modules(), ids=lambda p: str(p.relative_to(GUI_DIR)))
def test_isar_gui_import_contract(py):
    offenders = _offenders(py, GUI_ALLOWED)
    assert not offenders, (
        f"{py.relative_to(GUI_DIR)} imports isar.* outside the G16 GUI allow-list "
        f"(workspace_api / geometry / plotspec only): {offenders}")


def test_sandbox_import_contract():
    """Only the sandbox viewmodel may import the single Qt-free G7 entry point."""
    assert SANDBOX_VIEWMODEL.exists(), "sandbox viewmodel missing (the G7 GUI entry point)"
    offenders = _offenders(SANDBOX_VIEWMODEL, SANDBOX_VM_ALLOWED)
    assert not offenders, (
        f"sandbox viewmodel imports outside the sanctioned G7 API: {offenders}")


@pytest.mark.parametrize("py", _workspace_api_modules(),
                         ids=lambda p: str(p.relative_to(WORKSPACE_API_DIR)))
def test_workspace_api_import_contract(py):
    offenders = _offenders(py, WORKSPACE_API_ALLOWED)
    assert not offenders, (
        f"{py.relative_to(WORKSPACE_API_DIR)} imports isar.* outside the G16 facade allow-list: "
        f"{offenders}")


def test_allowlist_logic_flags_a_known_violation():
    # Sanity: the allow-list logic actually rejects a forbidden root and accepts a sanctioned one,
    # so a future refactor can't silently turn the contract into a vacuous pass.
    assert not _allowed("isar.session.driver", GUI_ALLOWED)
    assert not _allowed("isar.analyze.loop", WORKSPACE_API_ALLOWED)
    assert _allowed("isar.plotspec.spec", GUI_ALLOWED)
    assert _allowed("isar.reduce.collate", WORKSPACE_API_ALLOWED)
    assert not _constrained("isar_gui.viewmodels.base")  # sibling package is not constrained
