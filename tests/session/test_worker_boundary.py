"""The worker's analysis-boundary mirror of tests/drive/test_boundary.py (mighty-starfish §F.5).

``isar.session.worker`` must never reach the driver, the CLI, or the report/plotting layer: it is a
pure analysis serve loop (``run_batch`` with ``formats=()``). Statically follow its imports through
``isar.session`` and assert no reachable module imports ``isar.drive`` / ``isar.cli`` / ``matplotlib``
at any level (module or function body — a lazily-imported violation still breaks the boundary)."""
import ast
from pathlib import Path

SESSION_ROOT = Path(__file__).resolve().parents[2] / "isar" / "session"
WORKER = SESSION_ROOT / "worker.py"
FORBIDDEN_ROOTS = ("isar.drive", "isar.cli", "matplotlib")


def _is_forbidden(module: str) -> bool:
    return any(module == r or module.startswith(r + ".") for r in FORBIDDEN_ROOTS)


def _session_path(module: str) -> Path | None:
    if module == "isar.session":
        return SESSION_ROOT / "__init__.py"
    prefix = "isar.session."
    if not module.startswith(prefix):
        return None
    relative = module[len(prefix):].replace(".", "/")
    module_path = SESSION_ROOT / f"{relative}.py"
    if module_path.exists():
        return module_path
    package_path = SESSION_ROOT / relative / "__init__.py"
    return package_path if package_path.exists() else None


def _imported_modules(module: str, tree: ast.AST, *, is_package: bool = False) -> set[str]:
    found = set()
    package = module if is_package else module.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base = ".".join(base_parts[:len(base_parts) - node.level + 1])
                imported = ".".join(part for part in (base, node.module or "") if part)
                found.add(imported)
                if node.module is None:
                    found.update(f"{imported}.{alias.name}" for alias in node.names)
            else:
                found.add(node.module or "")
    return found


def test_worker_transitive_session_imports_no_drive_cli_or_matplotlib():
    pending = [("isar.session.worker", WORKER)]
    seen = set()
    offenders = []
    while pending:
        module, path = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for imported in _imported_modules(module, tree, is_package=path.name == "__init__.py"):
            if _is_forbidden(imported):
                offenders.append((path.relative_to(SESSION_ROOT), imported))
                continue
            imported_path = _session_path(imported)
            if imported_path is not None and imported not in seen:
                pending.append((imported, imported_path))
    assert not offenders, f"worker-reachable session modules import forbidden modules: {offenders}"


def test_relative_import_resolution_distinguishes_package_init_from_module():
    tree = ast.parse("from .child import value\n")
    assert "isar.session.package.child" in _imported_modules(
        "isar.session.package", tree, is_package=True)
    assert "isar.session.child" in _imported_modules(
        "isar.session.module", tree, is_package=False)
