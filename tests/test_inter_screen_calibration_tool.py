"""tools/inter_screen_calibration.py and tools/inter_gradient_compare.py (S1U5): the reports built from synthetic
records. Nothing here opens an experiment or reads measured data (hard rule 10); the tools themselves are run
deliberately on a scratch copy, never by the routine suite.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import isar

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "tools"))

import inter_gradient_compare as gc  # noqa: E402
import inter_screen_calibration as cal  # noqa: E402


def test_import_root_is_the_worktree():
    root = Path(isar.__file__).resolve().parents[1]
    print("import root:", root)
    assert root == REPO


# ---- the calibration report -----------------------------------------------------------------------------------
FACE2 = math.log(math.exp(-10.5) + math.exp(-14.0))   # count 2 on the screens: a screened -10.5 and an exact -14.0


def _record(basins, decisions, first, *, counts=None, cands=None, shadow_counts=None):
    search = {"version": 2, "counts": counts or {"2": {"ln_total": -10.0}, "3": {"ln_total": -12.0}},
              "screens": {"mode": "shadow", "screened": 4, "promoted": 0},
              "shadow": {"basins": basins, "decisions": decisions, "first_divergence": first,
                         "counts": shadow_counts or {"2": FACE2, "3": -12.2}}}
    cands = cands if cands is not None else [
        {"count": 2, "ln_z": -10.0, "ln_z_screen": -10.5}, {"count": 2, "ln_z": -14.0, "ln_z_screen": None},
        {"count": 3, "ln_z": -12.0, "ln_z_screen": -12.2}, {"count": 3, "ln_z": None, "ln_z_screen": None}]
    return {"search": search, "candidates": cands}


def test_basin_count_and_candidate_underratings_are_full_minus_screen():
    rep = cal.build_report({"u1": _record([[2, ["b1", "b2"], -10.0, -10.6], [3, ["b1", None, None], -12.0, -11.0],
                                           [3, [None, None, None], None, -20.0]], [], None)}, 5.0)
    x = rep["spectra"]["u1"]
    assert math.isclose(x["basins"][0]["underrating"], 0.6) and math.isclose(x["basins"][1]["underrating"], -1.0)
    assert x["basins"][2]["underrating"] is None                    # no matching full basin: unmatched, not an error
    assert rep["split_basin_underrating"]["n"] == 2 and math.isclose(rep["split_basin_underrating"]["max"], 0.6)
    assert math.isclose(x["counts"]["2"]["face_screen_total"], FACE2)   # the shadow view's total, the record's -10.0 full
    assert math.isclose(x["counts"]["2"]["underrating"], -10.0 - FACE2)
    assert math.isclose(rep["candidate_underrating"]["max"], 0.5)      # the exact candidate has no screen to compare
    assert rep["criterion_met"] and rep["divergences"] == 0 and rep["would_promote"] == 0


def test_count_totals_are_the_shadow_views_own_so_a_basin_filed_under_another_label_counts():
    # the candidates list holds only the exact -14.0 of count 2 (the screened basin sits under another label, so no
    # row carries its screen): the total must still read the shadow view's -12.0, not the rows'
    cands = [{"count": 2, "ln_z": -14.0, "ln_z_screen": None}]
    rep = cal.build_report({"u1": _record([], [], None, cands=cands, shadow_counts={"2": -12.0})}, 5.0)
    x = rep["spectra"]["u1"]
    assert x["counts"]["2"]["face_screen_total"] == -12.0 and math.isclose(x["counts"]["2"]["underrating"], 2.0)
    # a count the screens leave no mass for is infinitely underrated, never dropped; the criterion fails
    assert x["counts"]["3"]["face_screen_total"] is None and x["counts"]["3"]["underrating"] == math.inf
    assert rep["count_total_underrating"]["max"] == math.inf and not rep["criterion_met"]
    # a record without the shadow totals (version 2, written before them) is named unreadable, not a KeyError
    old = _record([], [], None)
    del old["search"]["shadow"]["counts"]
    assert cal.build_report({"u1": old}, 5.0)["unreadable"] == ["u1"]


def test_a_definite_divergence_or_a_big_underrating_fails_the_criterion_and_a_small_one_does_not():
    decisions = [{"kind": "walk", "at": "3", "full": True, "screen": None},
                 {"kind": "best_key", "at": "k", "full": True, "screen": False},
                 {"kind": "final", "at": "decision", "full": True, "screen": None}]
    rep = cal.build_report({"u1": _record([[2, ["b1"], -10.0, -10.1]], decisions, 1)}, 5.0)
    x = rep["spectra"]["u1"]
    assert x["first_divergence"]["index"] == 1 and x["first_divergence"]["kind"] == "best_key"
    assert x["would_promote"] == 2 and len(x["divergences"]) == 1 and x["final_would_promote"]
    assert x["kinds_that_differ"] == ["best_key"] and not x["final_choice_differs"]
    assert not rep["criterion_met"]
    big = cal.build_report({"u1": _record([[2, ["b1"], -10.0, -13.0]], [], None)}, 5.0)    # 3.0 > 2.5, no divergence
    assert big["split_basin_underrating"]["over_half"] == 1 and big["split_basin_underrating"]["over_all"] == 0
    assert not big["criterion_met"]


def test_a_spectrum_without_a_shadow_record_is_named_not_averaged_in():
    ok = _record([[2, ["b1"], -10.0, -10.1]], [], None)
    rep = cal.build_report({"u1": ok, "u2": {"search": {"version": 2, "shadow": None}, "candidates": []},
                            "u3": {"search": None, "candidates": []}}, 5.0)
    assert rep["unreadable"] == ["u2", "u3"] and list(rep["spectra"]) == ["u1"] and not rep["criterion_met"]
    text = cal.render(rep)
    assert "u2, u3" in text and "frozen priors" in text and "criterion met" in text


# ---- the gradient comparison's verdicts -----------------------------------------------------------------------------
SPEC = SimpleNamespace(fs=SimpleNamespace(basin_merge_fwhm=1.0, basin_gamma_frac=0.5, basin_height_frac=0.5), fwhm=1.0)


def compare(fd, new):
    return gc.compare_job(fd, new, SPEC)


def _basin(ln_z, *, F=10.0, status="ok", posdef=True, split=False, theta=(1.0, 2.0), sd=(0.5, 0.5), priced=True,
           e=1.0, index=0):
    d = {"label": "m", "delta": [], "index": index, "status": status, "reason": "", "ln_z": ln_z, "split": split,
         "pinned": [], "priced": priced}
    if priced:   # rec: the search's basin record, one mode at energy e
        d |= {"theta": list(theta), "F": F, "sd": list(sd), "posdef": posdef,
              "rec": (0.0, "m", ((0, e, 0.5, 10.0, False, None),), (None, None), None, None)}
    return d


def _job(basins, ln_z, *, status="ok", mask=()):
    p = SimpleNamespace(ln_z=ln_z, status=status, mode=(True, True, False, tuple(mask), ()))
    return p, basins, 1.0


def test_agreement_within_the_tolerance_is_agree_and_a_difference_with_no_better_optimum_is_a_miss():
    same = compare(_job([_basin(-5.0)], -5.0), _job([_basin(-5.01)], -5.01))
    assert same["verdict"] == "agree"
    worse = compare(_job([_basin(-5.0, F=10.0)], -5.0), _job([_basin(-5.2, F=10.2)], -5.2))
    assert worse["verdict"] == "miss" and "no better optimum" in worse["basins"][0]["why"]
    tie = compare(_job([_basin(-5.0, F=10.0)], -5.0), _job([_basin(-5.2, F=10.0)], -5.2))
    assert tie["verdict"] == "miss"                                 # a different answer at the same F is not a gain
    mask = compare(_job([_basin(-5.0)], -5.0, mask=(0,)), _job([_basin(-5.0)], -5.0, mask=()))
    assert mask["verdict"] == "miss" and mask["notes"] == ["width mask [0] -> []"]


def test_a_fd_optimiser_that_stopped_short_is_a_correction_only_at_a_positive_definite_better_mode():
    fd = _job([_basin(-7.0, F=12.0, posdef=False)], -7.0)
    better = compare(fd, _job([_basin(-5.0, F=10.0)], -5.0))
    assert better["verdict"] == "correction" and better["basins"][0]["verdict"] == "correction"
    assert compare(fd, _job([_basin(-5.0, F=13.0)], -5.0))["verdict"] == "miss"     # worse F: a miss
    # the other direction: the gradient path is the one that lost the basin
    lost = compare(_job([_basin(-5.0)], -5.0), _job([_basin(None, status="undetermined", priced=False)], None))
    assert lost["verdict"] == "miss"
    found = compare(_job([_basin(None, status="undetermined", priced=False)], None), _job([_basin(-5.0)], -5.0))
    assert found["verdict"] == "correction"
    flat = compare(_job([_basin(None, status="undetermined", priced=False)], None),
                   _job([_basin(-5.0, posdef=False)], -5.0))
    assert flat["verdict"] == "miss"                                # priced at a mode that is not positive definite
    # a split basin has no covariance (posdef False) but its integral converged: the packet's rule takes it as a correction
    split = compare(_job([_basin(None, status="undetermined", priced=False)], None),
                    _job([_basin(-5.0, split=True, posdef=False)], -5.0))
    assert split["verdict"] == "correction" and "correction" in [r["verdict"] for r in split["basins"]]


def test_basins_pair_by_the_search_identity_not_by_engine_index():
    # the two optimisers list the same two basins in opposite order: by index they would be compared crosswise
    fd = _job([_basin(-5.0, e=1.0, index=0), _basin(-7.0, e=5.0, index=1)], -4.0)
    new = _job([_basin(-7.0, e=5.0, index=0), _basin(-5.0, e=1.0, index=1)], -4.0)
    out = compare(fd, new)
    assert out["verdict"] == "agree" and len(out["basins"]) == 2
    assert all(r["d_ln_z"] == 0.0 for r in out["basins"])


def test_a_correction_basin_does_not_hide_a_changed_width_mask_or_status():
    fd = _job([_basin(-7.0, F=12.0, posdef=False)], -7.0, mask=())
    out = compare(fd, _job([_basin(-5.0, F=10.0)], -5.0, mask=(0,)))
    assert out["basins"][0]["verdict"] == "correction"
    assert out["verdict"] == "miss" and "width mask [] -> [0]" in out["notes"]
    # the same job with the mask unchanged: the correction stands, and its job ln Z change is explained by it
    assert compare(fd, _job([_basin(-5.0, F=10.0)], -5.0))["verdict"] == "correction"
