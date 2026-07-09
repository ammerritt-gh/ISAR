"""The worker's analysis-boundary mirror of tests/drive/test_boundary.py (mighty-starfish §F.5).

``isar.session.worker`` must never reach the driver, the CLI, or the report/plotting layer: it is a
pure analysis serve loop (``run_batch`` with ``formats=()``). Statically AST-walk the module and assert
it imports none of ``isar.drive`` / ``isar.cli`` / ``matplotlib`` at any level (module or function
body — a lazily-imported violation would still break the boundary and re-pull matplotlib per job)."""
import ast
from pathlib import Path

WORKER = Path(__file__).resolve().parents[2] / "isar" / "session" / "worker.py"
FORBIDDEN_ROOTS = ("isar.drive", "isar.cli", "matplotlib")


def _is_forbidden(module: str) -> bool:
    return any(module == r or module.startswith(r + ".") for r in FORBIDDEN_ROOTS)


def test_worker_imports_no_drive_cli_or_matplotlib():
    tree = ast.parse(WORKER.read_text(encoding="utf-8"), filename=str(WORKER))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [(node.lineno, a.name) for a in node.names if _is_forbidden(a.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import stays within isar.session
                continue
            if _is_forbidden(node.module or ""):
                offenders.append((node.lineno, node.module))
    assert not offenders, f"isar/session/worker.py imports forbidden modules: {offenders}"
