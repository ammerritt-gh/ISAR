"""The load-bearing architectural boundary (LLM_HARNESS_DESIGN §1b, §3).

`isar.drive` is DECISION-ONLY: it reaches analysis only through the `isar run` subprocess and ISAR's
file contract, never by importing a fit internal. The one permitted analysis-side import is
`isar.reduce.llm_client` — the shared LLMM HTTP plumbing.

This test statically walks the AST of every module under `isar/drive/` and asserts none of them
imports from a forbidden ISAR analysis subpackage. Static (not `sys.modules`) so a lazily-imported
violation inside a function body is still caught.
"""
import ast
from pathlib import Path

import pytest

DRIVE_DIR = Path(__file__).resolve().parents[2] / "isar" / "drive"

FORBIDDEN_ROOTS = {"isar.analyze", "isar.session", "isar.calibrate", "isar.parsers", "isar.reduce"}
# The single carve-out: the shared LLMM HTTP client is plumbing, not analysis.
ALLOWED_EXACT = {"isar.reduce.llm_client"}


def _drive_modules():
    return sorted(DRIVE_DIR.rglob("*.py"))


def _is_forbidden(module: str) -> bool:
    if module in ALLOWED_EXACT:
        return False
    for root in FORBIDDEN_ROOTS:
        if module == root or module.startswith(root + "."):
            # An allowed submodule (e.g. isar.reduce.llm_client) is exempted above.
            return True
    return False


def test_drive_package_exists_and_has_modules():
    mods = _drive_modules()
    assert mods, "no modules found under isar/drive/"


@pytest.mark.parametrize("py", _drive_modules(), ids=lambda p: str(p.relative_to(DRIVE_DIR)))
def test_no_forbidden_analysis_imports(py):
    tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    offenders.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — never crosses into another isar subpackage
                continue
            mod = node.module or ""
            if _is_forbidden(mod):
                offenders.append((node.lineno, mod))
            else:
                # `from isar.reduce import llm_client` — check the imported name too.
                for alias in node.names:
                    full = f"{mod}.{alias.name}" if mod else alias.name
                    if _is_forbidden(full):
                        offenders.append((node.lineno, full))
    assert not offenders, (
        f"{py.relative_to(DRIVE_DIR)} imports forbidden analysis modules "
        f"(driver is decision-only): {offenders}")


def test_llm_client_carveout_is_permitted():
    # Sanity: the exact allowed import is not flagged, but its siblings are.
    assert not _is_forbidden("isar.reduce.llm_client")
    assert _is_forbidden("isar.reduce.adjudicate")
    assert _is_forbidden("isar.analyze.loop")


def test_importing_drive_does_not_pull_analysis():
    """Runtime check in a CLEAN interpreter (a subprocess — the pytest process is polluted by other
    tests that legitimately import isar.session/isar.parsers). Importing the driver's working plumbing
    must not, by itself, pull in a forbidden analysis module."""
    import json
    import subprocess
    import sys

    script = (
        "import json, sys\n"
        "import isar.drive\n"
        "import isar.drive.instrument.tavi_client\n"
        "import isar.drive.isar_io\n"
        "import isar.drive.acquisition.calibration\n"
        "import isar.drive.session_log\n"
        "import isar.drive.loop\n"
        f"roots = {sorted(FORBIDDEN_ROOTS)!r}\n"
        f"allowed = {sorted(ALLOWED_EXACT)!r}\n"
        "bad = [n for n in sys.modules if any(n == r or n.startswith(r + '.') for r in roots) "
        "and n not in allowed]\n"
        "print(json.dumps(bad))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    loaded_forbidden = json.loads(out.stdout.strip().splitlines()[-1])
    assert not loaded_forbidden, f"importing isar.drive pulled in forbidden modules: {loaded_forbidden}"
