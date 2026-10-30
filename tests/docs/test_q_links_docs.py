"""The architecture and design rows say q-links are live, with their dependency, and curvature stays Partial.

Prevents: a q-links row left at Partial after the q side is built (the status a reader trusts), and a dependency
(an accepted per-analyzer hkl) dropped from the row that says what a user needs. Prose beyond the row is not judged.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _status_cell(path: Path, start: str) -> str:
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(start)]
    assert len(rows) == 1, f"{path.name}: {len(rows)} rows start with {start!r}"
    return rows[0].split("|")[2]                     # the status cell


def test_architecture_and_design_say_q_links_are_implemented_with_their_dependency():
    arch = _status_cell(REPO / "docs" / "ARCHITECTURE.md", "| Inter-spectra q-links")
    design = _status_cell(REPO / "docs" / "design" / "inter_spectra.md", "| q-links from grid adjacency")
    assert "**Implemented**" in arch and "2026-10-30" in arch and "accepted" in arch and "hkl" in arch, arch
    assert design.strip().startswith("Implemented"), design


def test_curvature_stays_partial_until_the_real_run_settles_the_tolerance():
    arch = _status_cell(REPO / "docs" / "ARCHITECTURE.md", "| Inter-spectra curvature terms")
    assert "**Partial**" in arch and "collinearity tolerance" in arch, arch
