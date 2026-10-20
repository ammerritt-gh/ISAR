"""No routine test names the real-data folder.

`real_data/` (formerly `test_data/`) is gitignored and exists only in the main checkout, so a test
that opens a path under it fails with `FileNotFoundError` in every worktree and fresh checkout
(about forty did, until 2026-10-20). Checks that need real files live in `proofs/`; tests use
`artificial_data/` or data generated at test time (AGENTS.md hard rule 10).

The check is on string constants, not comments or identifiers: `real_data` or `test_data` must be
a whole path component (the constant is that name, or the name sits between `/`, `\\` or the
string's ends). Docstrings are not checked.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("tests", "isar/synth/tests")
FOLDER = re.compile(r"(?:^|[/\\])(?:real_data|test_data)(?:$|[/\\])")

# test_collect_evidence builds a throwaway fake folder in tmp_path to test the collector tool; this
# file spells the names out to find them; test_artificial_copy checks that the copy tool refuses
# output folders of those names, made under tmp_path.
ALLOWED = {
    "tests/docs/test_collect_evidence.py",
    "tests/docs/test_no_real_data_in_tests.py",
    "tests/test_artificial_copy.py",
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _hits(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    skip = _docstring_nodes(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and FOLDER.search(node.value)
    )


def test_no_test_names_the_real_data_folder():
    found = []
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            if rel not in ALLOWED:
                found.extend(f"{rel}:{line}" for line in _hits(path))
    assert not found, (
        "a test names real_data/ or test_data/, which exist only in the main checkout, so it "
        "fails in every worktree; move it to proofs/ or use artificial_data/ or generated data:\n  "
        + "\n  ".join(found)
    )
