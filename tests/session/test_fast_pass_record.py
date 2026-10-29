"""The persisted fast-pass record (plan C-fastpass-record, worked examples 1-7): the fields the
benchmark, the deep pass and the reports read, written by ``scan_delivery._fast_pass_record`` from
the engine's result. Hand-built engine results (no fit), so every example is exact. The ``csv_*``
tests at the end check each example's results.csv and modes.csv halves through the real writer.
"""
import dataclasses
import json
import math
from types import SimpleNamespace

import pytest

from isar.analyze import select
from isar.analyze.fast_pass import (FastPassResult, FastPassSettings, ModelResult, PeakResult,
                                    SpectrumFlag)
from isar.analyze.phonon import GAMMA_FLOOR
from isar.analyze.prior import PriorSettings
from isar.drive.isar_io import readers
from isar.session import outcome as outcome_mod
from isar.session import scan_delivery
from isar.workspace_api import scene
from isar.session.outcome import FitOutcome, fast_pass_of
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace

# The connection's lists, verbatim: a field the writer drops fails here.
RECORD_KEYS = {
    "schema", "implementation_version", "state", "preferred_count", "count_probabilities", "probabilities_conditional",
    "close_call_alternatives", "numerically_marginal", "search_limit_reason",
    "deep_pass_candidate", "selected_model_id", "selected_label", "arena_fingerprint",
    "ln_like_saturated", "models", "peaks", "elastic_height", "background", "grouped_intensity",
    "flags", "flag_statistics", "prior_provenance", "settings", "n_fits", "seed_count", "qe"}
MODEL_KEYS = {
    "model_id", "label", "n_peaks", "elastic", "background", "delta_modes", "ln_model_prior",
    "evidence_status", "ln_z", "ln_like", "ln_prior", "cost", "k_free", "free", "probability",
    "twin_of", "n_basins", "reason", "posterior_iterations", "params", "map_params",
    "qe", "on_demand", "removed", "superseded", "parent"}
PEAK_KEYS = {
    "index", "omega0", "Gamma", "delta", "height", "area_stokes", "area_both", "sigma_omega0",
    "sigma_Gamma", "sigma_height", "sigma_area", "area_status", "corr_omega0_Gamma", "regime",
    "status", "confident", "deletion_impact", "manual_fit"}


def _prior(n, variants=4):
    return -math.log(variants) - n * math.log(2)


def _model(label, n, *, status="ok", p=0.0, ln_z=-100.0, delta=(), twin_of=None, reason="",
           index=0):
    priced = status in ("ok", "ok_boundary")
    params = {f"phonon{i or ''}.omega0": 4.0 + 3 * i for i in range(n)}
    return ModelResult(
        model_id=f"{label}#{index}", label=label, n_peaks=n, elastic=label.startswith("elastic"),
        background="+bg" in label, delta_modes=tuple(delta), ln_model_prior=_prior(n),
        evidence_status=status, ln_z=ln_z if priced else None, ln_like=-90.0,
        ln_prior=-5.0 if priced else None, cost=50.0, twin_of=twin_of, n_basins=int(priced),
        reason=reason, params=params, map_params=dict(params) if priced else None,
        free=tuple(params), cov=None, probability=p, k_free=len(params), pinned=(),
        boundary_method="", posterior_iterations=12, hypothesis=None, fit=None)


def _peak(i, omega0, *, confident=True, delta=False):
    return PeakResult(
        index=i, omega0=omega0, Gamma=GAMMA_FLOOR if delta else 0.4, delta=delta, height=100.0,
        area_stokes=50.0, area_both=60.0, sigma_omega0=0.05,
        sigma_Gamma=None if delta else 0.05, sigma_height=5.0, sigma_area=3.0,
        sigma_area_both=3.5, area_status="usable", corr_omega0_Gamma=None if delta else 0.1,
        regime="underdamped",
        status={"omega0": "constrained", "Gamma": "boundary_limited" if delta else "constrained",
                "height": "constrained", "area": "constrained"},
        confident=confident, deletion_impact=500.0, manual_fit=False)


def _result(models, selected, peaks, *, probabilities, state="resolved", alternatives=(),
            reason="", flags=(), settings=None, conditional=False, unpriced=()):
    return FastPassResult(
        state=state, preferred_count=None if selected is None else selected.n_peaks,
        count_probabilities=probabilities, probabilities_conditional=conditional,
        close_call_alternatives=tuple(alternatives), numerically_marginal=False,
        search_limit_reason=reason, models=tuple(models), selected=selected, peaks=tuple(peaks),
        elastic_height=90.0, background=4.0, grouped_intensity=((4.5, 5.5, 50.0, 3.0),),
        flags=tuple(flags), flag_statistics={"count_ln_z": {3: -100.0}, "max_count": 900.0,
                                             "unpriced_contenders": list(unpriced)},
        prior_provenance={"recipe": "v5", "empirical_bayes": True},
        arena_fingerprint="arena-1", ln_like_saturated=-300.0,
        settings=settings or FastPassSettings(), timing={"seed_count": 3, "total": 1.5},
        n_fits=40, deep_pass_candidate=state != "resolved")


def _three_peaks():
    m3 = _model("elastic+stokes3+bg", 3, p=0.91)
    m4 = _model("elastic+stokes4+bg", 4, p=0.09, ln_z=-102.3)
    return _result([m3, m4], m3, [_peak(0, 5.0), _peak(1, 9.0), _peak(2, 14.0)],
                   probabilities={3: 0.91, 4: 0.09})


def test_ex1_resolved_clean_three_peaks():
    rec = scan_delivery._fast_pass_record(_three_peaks())
    assert set(rec) == RECORD_KEYS
    assert all(set(m) == MODEL_KEYS for m in rec["models"])
    assert all(PEAK_KEYS <= set(p) for p in rec["peaks"])
    assert rec["schema"] == "isar.fast-pass/1" and rec["state"] == "resolved"
    assert rec["count_probabilities"] == {"3": 0.91, "4": 0.09}
    assert list(rec["count_probabilities"]) == ["3", "4"]
    assert rec["close_call_alternatives"] == [] and rec["deep_pass_candidate"] is False
    assert rec["selected_model_id"] == "elastic+stokes3+bg#0"
    assert [p["confident"] for p in rec["peaks"]] == [True, True, True]
    assert rec["grouped_intensity"] == [{"e_lo": 4.5, "e_hi": 5.5, "intensity": 50.0, "sigma": 3.0}]
    assert rec["seed_count"] == 3 and rec["n_fits"] == 40
    assert "total" not in json.dumps(rec)          # no wall time in the record
    # strict JSON: the count_ln_z keys become strings, nothing is NaN
    assert json.loads(json.dumps(rec, allow_nan=False))["flag_statistics"]["count_ln_z"] == \
        {"3": -100.0}


def _close_call():
    m3 = _model("elastic+stokes3+bg", 3, p=0.30)
    m4 = _model("elastic+stokes4+bg", 4, p=0.62)
    m5 = _model("elastic+stokes5+bg", 5, p=0.08)
    flag = SpectrumFlag("ambiguous_decomposition", 5.0, 7.0, 1.2,
                        "possible hidden component near 6.0 meV; a neighbour may be absorbing it")
    return _result([m3, m4, m5], m4, [_peak(0, 3.0), _peak(1, 6.0), _peak(2, 9.0, confident=False),
                                      _peak(3, 14.0)],
                   probabilities={3: 0.30, 4: 0.62, 5: 0.08}, state="close_call", alternatives=(3, 5),
                   flags=[flag])


def test_ex2_close_call_four_components_three_confident():
    rec = scan_delivery._fast_pass_record(_close_call())
    assert rec["state"] == "close_call" and rec["close_call_alternatives"] == [3, 5]
    assert rec["deep_pass_candidate"] is True and rec["preferred_count"] == 4
    assert [p["confident"] for p in rec["peaks"]] == [True, True, False, True]
    assert rec["flags"] == [{"kind": "ambiguous_decomposition", "e_lo": 5.0, "e_hi": 7.0,
                             "statistic": 1.2,
                             "detail": _close_call().flags[0].detail}]


def test_ex3_unresolved_keeps_the_model_without_evidence():
    m2 = _model("elastic+stokes2+bg", 2, p=0.7)
    m3 = _model("elastic+stokes3+bg", 3, p=0.3)
    m4 = _model("stokes4+bg", 4, status="no_evidence", reason="posterior Hessian not positive definite")
    rec = scan_delivery._fast_pass_record(_result(
        [m2, m3, m4], m2, [_peak(0, 5.0), _peak(1, 9.0)], probabilities={2: 0.7, 3: 0.3},
        state="unresolved", conditional=True))
    kept = rec["models"][2]
    assert kept["model_id"] == "stokes4+bg#0" and kept["ln_z"] is None
    assert kept["evidence_status"] == "no_evidence"
    assert kept["reason"] == "posterior Hessian not positive definite"
    assert rec["state"] == "unresolved" and rec["probabilities_conditional"] is True


def test_ex4_search_limited_by_the_ceiling():
    m6 = _model("elastic+stokes6+bg", 6, p=1.0)
    rec = scan_delivery._fast_pass_record(_result(
        [m6], m6, [_peak(i, 2.0 + 2.5 * i) for i in range(6)], probabilities={6: 1.0},
        state="search_limited", reason="ceiling",
        settings=FastPassSettings(mode_ceiling=6, prior=PriorSettings(), confident_anchor=((0.0, 50.0),))))
    assert rec["state"] == "search_limited" and rec["search_limit_reason"] == "ceiling"
    assert rec["preferred_count"] == 6 and rec["deep_pass_candidate"] is True
    assert rec["settings"]["mode_ceiling"] == 6
    assert rec["settings"]["prior"]["amplitude_margin"] == 1.2     # the prior nested, as a dict
    assert rec["settings"]["confident_anchor"] == [[0.0, 50.0]]


def test_ex5_count_zero_resolved():
    m0 = _model("elastic+bg", 0, p=1.0)
    rec = scan_delivery._fast_pass_record(_result([m0], m0, [], probabilities={0: 1.0}))
    assert rec["peaks"] == [] and rec["preferred_count"] == 0
    assert rec["selected_label"] == "elastic+bg"


def test_ex6_degenerate_twins_keep_their_parents_and_name_the_terminal_model():
    parent_bg = _model("elastic+stokes3+bg", 3, status="degenerate_twin",
                       twin_of="elastic+stokes3#0", reason="background at zero")
    terminal_3 = _model("elastic+stokes3", 3, p=0.6)
    parent_w = _model("elastic+stokes4+bg", 4, status="degenerate_twin",
                      twin_of="elastic+stokes4+bg_delta4#0", reason="phonon4.Gamma at its floor")
    delta = _model("elastic+stokes4+bg_delta4", 4, p=0.4, delta=(3,))
    res = _result([terminal_3, parent_bg, parent_w, delta], terminal_3,
                  [_peak(0, 4.0), _peak(1, 7.0), _peak(2, 10.0)], probabilities={3: 0.6, 4: 0.4})
    rec = scan_delivery._fast_pass_record(res)
    by_id = {m["model_id"]: m for m in rec["models"]}
    assert by_id["elastic+stokes3+bg#0"]["probability"] == 0.0
    assert by_id["elastic+stokes3+bg#0"]["twin_of"] == "elastic+stokes3#0"
    assert by_id["elastic+stokes4+bg#0"]["twin_of"] == "elastic+stokes4+bg_delta4#0"
    twin = by_id["elastic+stokes4+bg_delta4#0"]
    assert twin["delta_modes"] == [3] and twin["ln_z"] == -100.0
    assert math.isclose(twin["ln_model_prior"], -math.log(4) - 4 * math.log(2))

    # The candidate rows: a twin's score takes no weight and names its terminal model.
    fits = [SimpleNamespace(label=m.label, optimizer_converged=True, cost=50.0, k=m.k_free,
                            ndof=70, n_modes=m.n_peaks, fit_semantics=None, params=m.params)
            for m in res.models]
    rows = {m.label: m for m in res.models}
    sel = scan_delivery._selection_from_fast_pass(res, fits, rows)
    scores = {s.label: s for s in sel.scores}
    for parent, terminal in (("elastic+stokes3+bg", "elastic+stokes3#0"),
                             ("elastic+stokes4+bg", "elastic+stokes4+bg_delta4#0")):
        assert scores[parent].posterior_probability == 0.0 and scores[parent].candidate_prior == 0.0
        assert scores[parent].log_evidence is None
        assert scores[parent].evidence_validity["twin_of"] == terminal
    assert sel.best == "elastic+stokes3" and sel.selection_fallback is None
    assert scores["elastic+stokes3"].posterior_probability == 0.6
    # the model prior restricted to the counts with evidence (two here)
    assert math.isclose(scores["elastic+stokes4+bg_delta4"].candidate_prior,
                        math.exp(_prior(4)) / 2)
    assert {r.label for r in sel.evidence.records} == {"elastic+stokes3",
                                                       "elastic+stokes4+bg_delta4"}
    assert sel.survivors == ["elastic+stokes3"] and isinstance(sel, select.Selection)


def test_ex7_an_outcome_without_a_fast_pass_reads_as_empty():
    oc = FitOutcome(detector_id="d", verdict="unconstrained", disposition="abstain",
                    confidence=0.0, omega0=None, omega0_err=None, interval=None,
                    modality="unpooled", gamma=None, gamma_provisional=False, best_label=None,
                    cost_per_dof=None, q=[0, 0, 0], T=10.0, sample="S", prior_used=None, calib={})
    assert oc.to_dict()["fast_pass"] is None and fast_pass_of(oc.to_dict()) == {}


# --------------------------------------------------------------------------- the CSV halves
def _modes(omegas, gamma_err=None):
    return [{"omega0": w, "omega0_err": 0.1, "gamma": 0.4, "gamma_err": gamma_err,
             "amplitude": 50.0, "amplitude_intrinsic": 25.0, "hesse_trusted": True}
            for w in omegas]


def _csv(tmp_path, res, modes, *, omega0=None, provenance=None):
    """results.csv and modes.csv as the writer produces them for one spectrum carrying ``res``."""
    best = None if res.selected is None else res.selected.label
    oc = FitOutcome(detector_id="ana7", verdict="unconstrained", disposition="abstain",
                    confidence=0.0, omega0=omega0, omega0_err=None, interval=None,
                    modality="unpooled", gamma=None, gamma_provisional=False, best_label=best,
                    cost_per_dof=None, q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None,
                    calib={}, n_modes=len(modes), modes=modes or None,
                    fast_pass=scan_delivery._fast_pass_record(res))
    st = SessionState()
    st.put(ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement", status="fitted",
                     detectors={"ana7": oc.to_dict()}, provenance=provenance or {}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    outcome_mod._write_results_csv(ws, st)
    row, = readers.read_results_csv(ws.root)
    return row, readers.read_modes_csv(ws.root)


def test_an_unresolved_comparison_reads_incomplete_in_the_scene():
    m2 = _model("elastic+stokes2+bg", 2, p=0.7)
    m3 = _model("elastic+stokes3+bg", 3, p=0.3)
    m4 = _model("stokes4+bg", 4, status="no_evidence", reason="posterior Hessian not positive definite")
    res = _result([m2, m3, m4], m2, [_peak(0, 5.0), _peak(1, 9.0)], probabilities={2: 0.7, 3: 0.3},
                  state="unresolved", conditional=True)
    fits = [SimpleNamespace(label=m.label, optimizer_converged=True, cost=50.0, k=m.k_free,
                            ndof=70, n_modes=m.n_peaks, fit_semantics=None, params=m.params)
            for m in res.models]
    sel = scan_delivery._selection_from_fast_pass(res, fits, {m.label: m for m in res.models})
    assert sel.selection_fallback == "evidence_budget_exhausted"
    record = {"selection_method": "aic_fallback", "selection_fallback": sel.selection_fallback}
    status, fallback, _ = scene._evidence_comparison(SimpleNamespace(fit_selection_semantics=record),
                                                     "scan 7")
    assert status == "incomplete" and fallback == "evidence_budget_exhausted"


def test_csv_ex1_resolved_clean_three_peaks(tmp_path):
    provenance = {"analyzer_facts": {"Q_nm": {"value": 3.1, "source": "trailer"}}}
    row, modes = _csv(tmp_path, _three_peaks(), _modes([5.0, 9.0, 14.0]), omega0=5.0,
                      provenance=provenance)
    assert row["count_state"] == "resolved" and row["count_probability"] == 0.91
    assert row["n_confident"] == 3 and row["n_tentative"] == 0 and row["flags"] is None
    assert row["deep_pass_candidate"] is False and "3" in row["count_state_reason"]
    assert len(modes) == 3 and all(m["confident"] is True for m in modes)
    assert all(m["Q_nm"] == pytest.approx(3.1) for m in modes)


def test_csv_ex2_close_call_names_alternatives_and_flags_the_tentative_mode(tmp_path):
    row, modes = _csv(tmp_path, _close_call(), _modes([3.0, 6.0, 9.0, 14.0]))
    assert row["count_state"] == "close_call" and row["count_probability"] == 0.62
    assert row["n_confident"] == 3 and row["n_tentative"] == 1
    assert row["flags"] == "ambiguous decomposition 5.00-7.00 meV"
    assert row["deep_pass_candidate"] is True
    assert "3 (0.30)" in row["count_state_reason"] and "5 (0.08)" in row["count_state_reason"]
    assert len(modes) == 4 and [m["confident"] for m in modes] == [True, True, False, True]


def test_csv_ex3_unresolved_is_conditional_and_names_the_unpriced_count(tmp_path):
    m2 = _model("elastic+stokes2+bg", 2, p=0.7)
    m3 = _model("elastic+stokes3+bg", 3, p=0.3)
    m4 = _model("stokes4+bg", 4, status="no_evidence", reason="posterior Hessian not positive definite")
    res = _result([m2, m3, m4], m2, [_peak(0, 5.0), _peak(1, 9.0)], probabilities={2: 0.7, 3: 0.3},
                  state="unresolved", conditional=True, unpriced=[4])
    row, modes = _csv(tmp_path, res, _modes([5.0, 9.0]))
    assert row["count_state"] == "unresolved" and row["probabilities_conditional"] is True
    assert "count 4 has no evidence (posterior Hessian not positive definite)" in row["count_state_reason"]
    assert len(modes) == 2


def test_csv_ex4_search_limited_by_the_ceiling(tmp_path):
    m6 = _model("elastic+stokes6+bg", 6, p=1.0)
    res = _result([m6], m6, [_peak(i, 2.0 + 2.5 * i) for i in range(6)], probabilities={6: 1.0},
                  state="search_limited", reason="ceiling")
    row, modes = _csv(tmp_path, res, _modes([2.0 + 2.5 * i for i in range(6)]))
    assert row["count_state"] == "search_limited" and row["search_limit_reason"] == "ceiling"
    assert row["deep_pass_candidate"] is True and "search limited (ceiling)" in row["count_state_reason"]
    assert len(modes) == 6


def test_csv_ex5_count_zero_has_no_modes(tmp_path):
    m0 = _model("elastic+bg", 0, p=1.0)
    row, modes = _csv(tmp_path, _result([m0], m0, [], probabilities={0: 1.0}), [])
    assert row["n_modes"] == 0 and row["omega0"] is None and row["n_confident"] == 0
    assert row["count_state"] == "resolved" and modes == []


def test_csv_ex6_a_selected_delta_twin_marks_its_resolution_limited_mode(tmp_path):
    delta = _model("elastic+stokes4+bg_delta4", 4, p=1.0, delta=(3,))
    peaks = [_peak(0, 4.0), _peak(1, 7.0), _peak(2, 10.0), _peak(3, 13.0, delta=True)]
    row, modes = _csv(tmp_path, _result([delta], delta, peaks, probabilities={4: 1.0}),
                      _modes([4.0, 7.0, 10.0, 13.0]))
    assert [m["delta"] for m in modes] == [False, False, False, True]
    assert modes[3]["gamma_status"] == "boundary_limited" and modes[3]["gamma_err"] is None


def test_csv_ex7_a_scan_with_no_fit_reads_not_fitted(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    st = SessionState()
    st.put(ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement", status="failed",
                     reasons=["fit raised"]))
    outcome_mod._write_results_csv(ws, st)
    row, = readers.read_results_csv(ws.root)
    assert row["count_state"] == "not fitted" and row["count_state_reason"] == "fit raised"
    assert readers.read_modes_csv(ws.root) == []


def test_csv_the_reason_names_only_unpriced_counts_once(tmp_path):
    """A count with a priced basin is not 'without evidence' for a no_evidence sibling; an unpriced
    contender is named once, with its best basin's reason."""
    m2 = _model("elastic+stokes2+bg", 2, p=1.0)
    m2x = _model("stokes2+bg", 2, status="no_evidence", reason="sibling basin", index=1)
    m3a = dataclasses.replace(_model("elastic+stokes3+bg", 3, status="no_evidence",
                                     reason="first basin", index=0), ln_like=-95.0)
    m3b = dataclasses.replace(_model("elastic+stokes3+bg", 3, status="no_evidence",
                                     reason="second basin", index=1), ln_like=-80.0)
    res = _result([m2, m2x, m3a, m3b], m2, [_peak(0, 5.0), _peak(1, 9.0)], probabilities={2: 1.0},
                  state="unresolved", unpriced=[3])
    row, _ = _csv(tmp_path, res, _modes([5.0, 9.0]))
    reason = row["count_state_reason"]
    assert reason.count("has no evidence") == 1 and "count 3 has no evidence (second basin)" in reason
    assert "count 2 has no evidence" not in reason
