"""The pure checks of the inter-spectra proof (``proofs/inter_spectra_checks.py``) on synthetic records: flag
attribution, the moving and flat verdicts, accounting, the timing split and the flag map. No gate, no real data.
"""
from __future__ import annotations

from pathlib import Path

import isar
from isar.session.inter_spectra.layout import grid_for
from isar.session.inter_spectra.records import Flag
from proofs import inter_spectra_checks as chk

REPO = Path(__file__).resolve().parents[1]
PAPER = {"A": 3.0, "B": 6.0, "C": 9.0, "D": 12.0}          # synthetic phonon energies, one temperature


def test_import_root_is_the_worktree():
    root = Path(isar.__file__).resolve().parents[1]
    print("import root:", root)
    assert root == REPO
    assert Path(chk.__file__).resolve().parents[1] == REPO


def reason(code, severity, ids):
    return {"code": code, "severity": severity, "text": "synthetic", "ids": list(ids), "values": {}}


def flag(*reasons):
    return Flag(score=None, flagged=bool(reasons), reasons=tuple(reasons))


def modes(*energies):
    return [{"branch": f"b{i}", "omega0": e} for i, e in enumerate(energies)]


# refit modes at two temperatures: A, B, C, D each refitted to a branch (b0..b3)
FITTED = modes(3.1, 5.9, 9.05, 12.0)
MISSING_B = modes(3.1, 9.05, 12.0)                          # no mode near 6 meV


def matches(fitted, tol=0.5):
    return {T: chk.match_modes(fitted, PAPER, tol) for T in (302.0, 97.0)}


def test_match_modes_takes_the_nearest_mode_within_tolerance_only():
    got = chk.match_modes(FITTED, PAPER, 0.5)
    assert {k: m["branch"] for k, m in got.items()} == {"A": "b0", "B": "b1", "C": "b2", "D": "b3"}
    assert chk.match_modes(MISSING_B, PAPER, 0.5)["B"] is None
    # tolerance None: half the smallest paper spacing (here 1.5 meV), so 5.9 meV is B's and 9.05 is C's
    assert chk.match_modes(FITTED, PAPER, None)["B"]["branch"] == "b1"
    assert chk.match_modes(MISSING_B, PAPER, None)["B"] is None


def test_b_flagged_is_ok_and_a_warn_flag_on_b_is_not_a_serious_one():
    events = {302.0: flag(reason("identity_ambiguous", "serious", ["b1"])), 97.0: None}
    assert chk.moving_is_flagged(events, matches(FITTED), None).ok
    warn_only = {302.0: flag(reason("branch_lost_mid_window", "warn", ["b1"])), 97.0: None}
    assert not chk.moving_is_flagged(warn_only, matches(FITTED), None).ok


def test_a_score_reason_names_the_spectrum_so_it_never_flags_b():
    events = {302.0: flag(reason("score_over_threshold", "serious", ["E:1:a06"])), 97.0: None}
    assert not chk.moving_is_flagged(events, matches(FITTED), None).ok


def test_b_unflagged_and_unmatched_is_not_ok():
    events = {302.0: None, 97.0: None}
    verdict = chk.moving_is_flagged(events, matches(MISSING_B), 0.5)
    assert not verdict.ok and "matched at no temperature" in verdict.why


def test_b_unflagged_but_refitted_within_tolerance_is_ok_only_when_tolerance_is_set():
    events = {302.0: None, 97.0: None}
    assert chk.moving_is_flagged(events, matches(FITTED, tol=0.5), 0.5).ok
    assert not chk.moving_is_flagged(events, matches(FITTED, tol=None), None).ok


def test_a_serious_flag_on_a_flat_mode_fails_and_an_info_flag_is_allowed():
    serious = {302.0: flag(reason("identity_ambiguous", "serious", ["b0"])), 97.0: None}
    assert not chk.flats_are_quiet(serious, matches(FITTED)).ok
    warn = {302.0: flag(reason("linewidth_change", "warn", ["b2"])), 97.0: None}
    assert not chk.flats_are_quiet(warn, matches(FITTED)).ok
    info = {302.0: flag(reason("neighbour_supported_split", "info", ["b0"])), 97.0: None}
    assert chk.flats_are_quiet(info, matches(FITTED)).ok
    # a reason on the moving mode is not a flat-mode failure
    on_b = {302.0: flag(reason("identity_ambiguous", "serious", ["b1"])), 97.0: None}
    assert chk.flats_are_quiet(on_b, matches(FITTED)).ok


def test_accounting_is_an_identity_that_an_off_by_one_breaks():
    balanced = {"done": 3, "failed": 0, "not_reached": 1, "fixed": 0, "total": 4}
    assert chk.accounting_ok(balanced).ok
    assert not chk.accounting_ok({**balanced, "not_reached": 0}).ok
    assert chk.accounting_ok({"done": 2, "failed": 1, "not_reached": 0, "fixed": 1, "total": 4}).ok


def _refit(each):
    return {"assign_s": each, "likelihood_s": each, "continuous_s": each, "uncertainty_s": each, "release_s": each}


def test_timing_splits_sum_to_the_wall_and_projects_against_the_target():
    view = {"wall_s": 100.0, "rings": 2, "sweeps": None, "refit_timings": [_refit(2.0)] * 4}
    rep = chk.timing_report(view, 50.0, cores=2, target_s=1800.0)
    assert rep["refits"] == 4 and rep["rings"] == 2
    assert rep["split_core_s"]["continuous_s"] == 8.0
    assert rep["split_core_s"]["remainder_core_s"] == 100.0 * 2 - 4 * 5 * 2.0
    assert rep["refit_total_s"] == {"median": 10.0, "p90": 10.0, "max": 10.0}
    assert rep["projection_s"] == 10.0 * 4 / 2 and rep["verdict"] == "under the target"
    # twice the target: over, named as a target and not a limit
    over = chk.timing_report(view, None, cores=2, target_s=10.0)
    assert over["verdict"] == "over the 10 s target (a target, not a limit)"
    assert over["projection_s"] == 20.0


def test_timing_never_raises_on_an_empty_batch():
    rep = chk.timing_report({"wall_s": 5.0, "refit_timings": []}, None, cores=1)
    assert rep["refits"] == 0 and rep["projection_s"] is None and rep["verdict"] is None


def test_flag_map_puts_a06_at_the_m_point_column():
    grid = grid_for("sp8_bl43")
    severe = flag(reason("identity_ambiguous", "serious", ["b1"]))
    text = chk.flag_map({(302.0, "a03"): None, (302.0, "a06"): severe, (302.0, "a04"): Flag(None, False, ())},
                        grid.position)
    rows = text.splitlines()
    assert rows[0] == "302 K"
    assert rows[1] == " ".join([" ", ".", " ", "!"])        # a03 is blank (no fit), a04 quiet, a06 serious
