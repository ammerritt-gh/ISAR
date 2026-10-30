"""The pure checks of the q side of the inter-spectra proof (``proofs/inter_spectra_q_checks.py``) on synthetic
terms. No gate, no real data.
"""
from __future__ import annotations

from pathlib import Path

import isar
from isar.session.inter_spectra.layout import BL43_GRID
from isar.session.inter_spectra.records import Flag
from proofs import inter_spectra_q_checks as qc

REPO = Path(__file__).resolve().parents[1]


def k(det: str, scan: str = "s1") -> str:
    return f"{scan}:{det}"


def term(kind: str, *members: str, line: str | None = None) -> dict:
    return {"kind": kind, "members": list(members), "line": line}


def test_import_root_is_the_worktree():
    root = Path(isar.__file__).resolve().parents[1]
    print("\nimport root:", root)
    assert root == REPO
    assert Path(qc.__file__).resolve().parents[1] == REPO


def test_a_curvature_term_along_a_row_or_a_column_is_structurally_ok():
    assert qc.curvature_structure_ok([term("curvature", k("a04"), k("a05"), k("a06"), line="row 1")], BL43_GRID).ok
    assert qc.curvature_structure_ok([term("curvature", k("a05"), k("a16"), k("a27"), line="col 3")], BL43_GRID).ok


def test_a_curvature_term_centred_on_a_line_end_fails():
    # a03 is the first analyzer of row 1, so it has no neighbour on its left
    assert not qc.curvature_structure_ok([term("curvature", k("a04"), k("a03"), k("a05"), line="row 1")],
                                          BL43_GRID).ok


def test_a_curvature_term_spanning_two_scans_or_off_its_line_fails():
    assert not qc.curvature_structure_ok([term("curvature", k("a05"), k("a16", "s2"), k("a27"), line="col 3")],
                                         BL43_GRID).ok
    assert not qc.curvature_structure_ok([term("curvature", k("a04"), k("a05"), k("a16"), line="row 1")],
                                         BL43_GRID).ok


def test_two_members_of_one_term_in_one_parallel_group_fail():
    terms = [term("curvature", k("a04"), k("a05"), k("a06"), line="row 1")]
    assert qc.groups_conflict_free(terms, [[k("a04")], [k("a05")], [k("a06")]]).ok
    assert not qc.groups_conflict_free(terms, [[k("a04"), k("a06")], [k("a05")]]).ok


def test_a_diagonal_link_joining_q_and_T_fails():
    assert qc.links_join_one_axis([term("T-change", "s1:a04", "s2:a04")]).ok
    assert qc.links_join_one_axis([term("curvature", k("a04"), k("a05"), k("a06"), line="row 1")]).ok
    assert not qc.links_join_one_axis([term("T-change", "s1:a04", "s2:a05")]).ok
    assert not qc.links_join_one_axis([term("q-change", "s1:a04", "s2:a05")]).ok


def test_a_q_linked_spectrum_in_no_term_must_be_named_by_the_plan():
    named = {k("a04"): "its q-linked set has no curvature term"}
    assert qc.q_linked_spectra_named(named, [k("a04")], []).ok
    assert qc.q_linked_spectra_named({}, [k("a04")], [k("a04")]).ok
    assert not qc.q_linked_spectra_named({}, [k("a04")], []).ok


def flag_with(*reasons: tuple[str, str, list[str] | None]) -> Flag:
    """A flag of (code, severity, branch ids) reasons, as a refit line stores them."""
    return Flag(None, True, tuple({"code": c, "severity": s, "text": "", "ids": ids} for c, s, ids in reasons))


def test_a_flat_mode_flagged_above_info_on_any_spectrum_fails_the_quiet_check():
    flat = {"A": {"omega0": 10.0, "branch": "b1"}}
    quiet = {k("a04"): (302, None, flat), k("a05"): (302, flag_with(("x", "info", ["b1"])), flat)}
    assert qc.flats_quiet_on_every_spectrum(quiet, ("A",)).ok
    # the flat mode warned on a spectrum the expectation does not name: the proof must see it
    hit = {**quiet, k("a05"): (302, flag_with(("t_drift_cumulative", "warn", ["b1"])), flat)}
    verdict = qc.flats_quiet_on_every_spectrum(hit, ("A",))
    assert not verdict.ok and k("a05") in verdict.why
    # a reason on another branch is not the flat mode's
    other = {k("a05"): (302, flag_with(("t_drift_cumulative", "warn", ["b2"])), flat)}
    assert qc.flats_quiet_on_every_spectrum(other, ("A",)).ok
