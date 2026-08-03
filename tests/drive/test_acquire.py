"""The acquisition function: target selection, escalation ladder, and the energy-window rule."""
import math
from dataclasses import replace

import pytest

from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition import acquire as acq
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.schema.hypotheses import content_fingerprint


def _obj(tol=0.5):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": 1e12})


def _obs(hkl, kind, hw, resolved=True, disp="resolved", omega0=3.0):
    return {"hkl": hkl, "kind": kind, "calibrated_halfwidth": hw, "resolved": resolved,
            "disposition": disp, "omega0": omega0 if resolved else None}


def _model(obs):
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    return acq_model.fit(model_points(summarize_grid(_obj(), obs)))


def test_cold_start_quicklooks_an_unvisited_q():
    obj = _obj()
    p = acq.propose_scan(obj, acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})
    assert p is not None and p.kind == "quicklook"
    assert p.ncount == acq.QUICKLOOK_NCOUNT
    assert p.scan_design_inventory is not None
    design = p.scan_design_inventory.selected_candidate
    assert (design.e_lo, design.e_hi, design.e_step, design.n_points, design.ncount) == (
        p.e_lo, p.e_hi, p.e_step, p.n_points, p.ncount)


def test_unvisited_q_preferred_over_escalation():
    obj = _obj()
    # Three q have a quick-look; (2.4,0,0) is untouched -> it must be the target, as a quick-look.
    obs = [_obs((h, 0.0, 0.0), "quicklook", 0.9) for h in (2.1, 2.2, 2.3)]
    p = acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {})
    assert p.kind == "quicklook"
    assert (p.h, p.k, p.l) == (2.4, 0.0, 0.0)


def test_individual_quicklook_intervals_do_not_rank_production_targets():
    obj = _obj()
    obs = [_obs((2.1, 0.0, 0.0), "quicklook", 0.6),
           _obs((2.2, 0.0, 0.0), "quicklook", 0.7),
           _obs((2.3, 0.0, 0.0), "quicklook", 1.5),
           _obs((2.4, 0.0, 0.0), "quicklook", 0.65)]
    p = acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {})
    assert (p.h, p.k, p.l) == (2.1, 0.0, 0.0)
    assert p.kind == "production" and p.ncount == acq.PRODUCTION_NCOUNT


def test_dynamic_count_request_uses_remainder_ladder_growth_and_scan_cap():
    g = acq.GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                  last_submitted_ncount=100_000_000,
                  master={"halfwidth": 1.0, "effective_exposure": 100_000_000})
    policy = {"dynamic_ncount": True, "production_batch_ncount": 100_000_000,
              "tolerance_margin": 0.8, "ncount_growth_cap": 4.0,
              "max_ncount_per_scan": 4_000_000_000}
    requested, diagnostics = acq._count_request(
        g, kind="production", n_points=10, tolerance=0.5, policy=policy,
        budget_state={"kind": "counting_seconds", "amount": 1.0, "spent": 1.0})
    assert diagnostics["exposure_required"] == pytest.approx(625_000_000)
    assert diagnostics["exposure_remaining"] == pytest.approx(525_000_000)
    assert requested == 400_000_000  # 800M ladder request, clamped to 4x the last submission

    legal, limited = acq._count_request(
        g, kind="production", n_points=10, tolerance=0.5, policy=policy,
        budget_state={"kind": "counting_seconds", "amount": 1.0, "spent": 1.0},
        runtime_ncount_limit=100_000_000)
    assert legal == 100_000_000
    assert limited["requested_before_runtime_limit"] == 400_000_000
    assert limited["runtime_ncount_limit"] == 100_000_000

    g.last_submitted_ncount = 2_000_000_000
    g.master = {"halfwidth": 10.0, "effective_exposure": 4_000_000_000}
    capped, _ = acq._count_request(
        g, kind="production", n_points=10, tolerance=0.5, policy=policy,
        budget_state={"kind": "counting_seconds", "amount": 0.0, "spent": 0.0})
    assert capped == 4_000_000_000


def test_dynamic_count_request_clamps_only_neutron_budget_and_requires_floor():
    g = acq.GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5)
    policy = {"dynamic_ncount": True, "production_batch_ncount": 100_000_000,
              "ncount_growth_cap": 4.0, "max_ncount_per_scan": 4_000_000_000}
    with pytest.raises(acq.BudgetInsufficientForMinimumBatch,
                       match="budget_insufficient_for_minimum_batch"):
        acq._count_request(
            g, kind="production", n_points=10, tolerance=0.5, policy=policy,
            budget_state={"kind": "neutrons", "amount": 999_999_990, "spent": 0.0})

    requested, diagnostics = acq._count_request(
        g, kind="production", n_points=10, tolerance=0.5, policy=policy,
        budget_state={"kind": "neutrons", "amount": 1_500_000_000, "spent": 0.0})
    assert requested == 100_000_000
    assert diagnostics["neutron_budget_clamp"] == 150_000_000

    with pytest.raises(acq.InstrumentLimitBelowMinimumBatch,
                       match="instrument_limit_below_minimum_batch"):
        acq._count_request(
            g, kind="production", n_points=10, tolerance=0.5, policy=policy,
            budget_state={"kind": "counting_seconds", "amount": 1.0, "spent": 0.0},
            runtime_ncount_limit=99_999_999)


def test_identity_detection_kind_uses_a_fixed_auxiliary_floor_not_target_escalation():
    """A narrow yes/no rival probe (routed to the auxiliary tracker node) answers a detection
    question, not a precision one. It must NOT inherit the target-production dynamic escalation that
    would spend a geometrically growing exposure to answer a cheap discriminator — it gets a dedicated
    detection floor regardless of the point's production history."""
    g = acq.GridQ(index=0, hkl=(2.9, 0.0, 0.0), q=0.9, tolerance=0.5,
                  last_submitted_ncount=2_000_000_000, n_production=5,
                  master={"halfwidth": 10.0, "effective_exposure": 4_000_000_000})
    policy = {"dynamic_ncount": True, "production_batch_ncount": 100_000_000,
              "tolerance_margin": 0.8, "ncount_growth_cap": 4.0,
              "max_ncount_per_scan": 4_000_000_000, "identity_detection_ncount": 10_000_000}
    unlimited = {"kind": "counting_seconds", "amount": 0.0, "spent": 0.0}

    # A production scan at this saturated point climbs the escalation ladder well above the floor.
    prod, _ = acq._count_request(g, kind="production", n_points=10, tolerance=0.5,
                                 policy=policy, budget_state=unlimited)
    assert prod > 100_000_000

    # The identity probe at the same point stays at the dedicated auxiliary detection floor.
    probe, _diag = acq._count_request(g, kind="identity_rival_probe", n_points=10, tolerance=0.5,
                                      policy=policy, budget_state=unlimited)
    assert probe == 10_000_000


def test_identity_detection_floor_still_honours_the_neutron_budget_clamp():
    """The dedicated floor is a starting request, not an exemption from the budget/instrument clamps."""
    g = acq.GridQ(index=0, hkl=(2.9, 0.0, 0.0), q=0.9, tolerance=0.5)
    policy = {"dynamic_ncount": True, "identity_detection_ncount": 10_000_000}
    with pytest.raises(acq.BudgetInsufficientForMinimumBatch):
        acq._count_request(g, kind="identity_rival_probe", n_points=10, tolerance=0.5, policy=policy,
                           budget_state={"kind": "neutrons", "amount": 99_999_990, "spent": 0.0})


def test_static_policy_retains_fixed_production_count():
    g = acq.GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                  master={"halfwidth": 10.0, "effective_exposure": 1_000_000_000})
    requested, diagnostics = acq._count_request(
        g, kind="production", n_points=10, tolerance=0.5,
        policy={"dynamic_ncount": False, "production_batch_ncount": 25_000_000},
        budget_state={"kind": "counting_seconds", "amount": 0.0, "spent": 0.0})
    assert requested == 25_000_000
    assert diagnostics["dynamic_ncount"] is False


def test_dynamic_proposal_uses_pooled_exposure_and_authoritative_halfwidth():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    frozen["steering"] = {**frozen["steering"],
                          "quicklook_ncount": 50_000_000,
                          "production_batch_ncount": 100_000_000}
    frozen["hypothesis_fingerprint"] = content_fingerprint(
        frozen["hypotheses"], frozen["steering"])
    obj = ObjectiveSpec.segment(
        q_start=(2, 0, 0), q_end=(2, 0, 0), step_rlu=0.1, tolerance_meV=0.5,
        budget={"kind": "counting_seconds", "amount": 1.0},
        target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    obs = [_obs((2.0, 0.0, 0.0), "production", 1.0, omega0=6.0)]
    obs[0].update({"submitted_ncount": 100_000_000, "proposal_cost": 1.0})
    master = _master((2.0, 0.0, 0.0), 1.0)
    master["effective_exposure"] = 100_000_000
    proposal = acq.propose_scan(
        obj, _model(obs), obs, {"fwhm_dE": 0.8},
        {"kind": "counting_seconds", "amount": 1.0, "spent": 1.0},
        master_rows=[master])
    assert proposal.ncount == 400_000_000
    assert proposal.meta["count_diagnostics"]["exposure_required"] == pytest.approx(625_000_000)

    limited = acq.propose_scan(
        obj, _model(obs), obs, {"fwhm_dE": 0.8},
        {"kind": "counting_seconds", "amount": 1.0, "spent": 1.0},
        master_rows=[master], runtime_limits={"max_neutrons_per_point": 100_000_000})
    score = limited.meta["score"]
    final_cost = float(limited.n_points) * 100_000_000.0
    expected_eig = acq.counts_eig(score["sigma_prior"], obj.tolerance_meV,
                                  score["c_spent"], final_cost)
    assert limited.ncount == 100_000_000
    assert score["ranking_cost"] != final_cost
    assert score["cost"] == final_cost
    assert score["eig"] == pytest.approx(expected_eig)
    assert score["sigma_scan"] == pytest.approx(acq.predicted_sigma_after(
        score["sigma_prior"], score["c_spent"], final_cost))
    assert score["eig_per_cost"] == pytest.approx(expected_eig / final_cost)


def test_third_generic_no_signal_attempt_grows_above_production_floor():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    frozen["steering"] = {**frozen["steering"], "production_batch_ncount": 100_000_000}
    frozen["hypothesis_fingerprint"] = content_fingerprint(
        frozen["hypotheses"], frozen["steering"])
    obj = ObjectiveSpec.segment(
        q_start=(2, 0, 0), q_end=(2, 0, 0), step_rlu=0.1, tolerance_meV=0.5,
        budget={"kind": "counting_seconds", "amount": 10.0},
        target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    obs = [
        {**_obs((2.0, 0.0, 0.0), "quicklook", None, resolved=False, disp="declined"),
         "acquisition_id": "A", "submitted_ncount": 50_000_000, "proposal_cost": 1.0},
        {**_obs((2.0, 0.0, 0.0), "production", None, resolved=False, disp="declined"),
         "acquisition_id": "B", "submitted_ncount": 100_000_000, "proposal_cost": 1.0},
    ]
    proposal = acq.propose_scan(
        obj, acq_model.fit([]), obs, {"fwhm_dE": 0.8},
        {"kind": "counting_seconds", "amount": 10.0, "spent": 0.0})
    assert proposal.kind == "production"
    assert proposal.ncount == 200_000_000


def test_adequate_provisional_candidate_sends_next_scan_to_max_information_neighbor():
    source = acq.GridQ(
        index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5, n_quicklook=1,
        master={"candidate_id": "optic:lowest_energy:unspecified",
                "candidate_state": "provisional", "diagnostic_halfwidth": 0.4})
    low_info = acq.GridQ(index=1, hkl=(2.0, 0.1, 0.0), q=0.1, tolerance=0.5)
    high_info = acq.GridQ(index=2, hkl=(2.0, 0.2, 0.0), q=0.2, tolerance=0.5)
    picked, kind, reason, score = acq._select_target(
        [source, low_info, high_info], acq_model.fit([]),
        classify=lambda candidate: candidate.q)
    assert picked is high_info and kind == "quicklook"
    assert "neighboring-q" in reason
    assert score.purpose == "differentiate"


def test_statistics_limited_provisional_candidate_pools_same_q_first():
    source = acq.GridQ(
        index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5, n_quicklook=1,
        master={"candidate_id": "optic:lowest_energy:unspecified",
                "candidate_state": "provisional", "diagnostic_halfwidth": 0.8})
    neighbor = acq.GridQ(index=1, hkl=(2.0, 0.1, 0.0), q=0.1, tolerance=0.5)
    picked, kind, reason, _score = acq._select_target(
        [source, neighbor], acq_model.fit([]), classify=lambda candidate: candidate.q + 1.0)
    assert picked is source and kind == "production"
    assert "pool more exposure" in reason


def _master(hkl, hw, disp="resolved", reliable=True, n=2):
    return {"hkl": hkl, "omega0": 3.0, "halfwidth": hw, "reliable": reliable,
            "usable": disp in ("resolved", "flagged"), "disposition": disp, "n_members": n}


def test_master_predictive_ranking_advances_common_random_levels_until_distinguishable():
    def diagnostics(info, stderr, count, reduction):
        return {"usable": True, "expected_information_nats": info,
                "omega_information_nats": info * 0.7,
                "model_branch_information_nats": info * 0.3,
                "expected_omega_halfwidth_reduction_meV": reduction,
                "score_standard_error_nats": stderr, "simulation_count": count}

    first = acq.GridQ(index=0, hkl=(2.1, 0.0, 0.0), q=0.0, tolerance=0.2,
                      n_quicklook=1)
    second = acq.GridQ(index=1, hkl=(2.2, 0.0, 0.0), q=0.1, tolerance=0.2,
                       n_quicklook=1)
    for point, levels in (
            (first, [diagnostics(0.50, 0.30, 128, 0.08),
                     diagnostics(0.50, 0.01, 512, 0.08)]),
            (second, [diagnostics(0.45, 0.30, 128, 0.07),
                      diagnostics(0.30, 0.01, 512, 0.07)])):
        point.master = {**_master(point.hkl, 0.8), "diagnostic_halfwidth": 0.8,
                        "expected_next_information": levels[0],
                        "predictive": {"predicted_analysis_seconds": 1.0,
                                       "score_levels": levels}}
    picked, kind, _reason, score = acq._select_target(
        [first, second], acq_model.fit([]), ranking_cost_kind="analysis")
    assert picked is first and kind == "production"
    assert score.method == "master_predictive" and score.simulation_count == 512


def test_live_predictive_scoring_honors_zero_allowance_after_initial_level():
    models = [{
        "weight": 1.0, "omega0_draws": [2.0, 2.2],
        "expected_counts": [[20.0, 2.0, 1.0], [18.0, 3.0, 1.0]],
    }]
    points = [
        acq.GridQ(index=index, hkl=(2.1 + index / 10, 0.0, 0.0), q=index / 10,
                  tolerance=0.2, n_quicklook=1)
        for index in range(2)
    ]
    for point in points:
        initial = {"usable": True, "expected_information_nats": 0.1,
                   "score_standard_error_nats": 0.1, "simulation_count": 128,
                   "common_random_seed": 940301}
        point.master = {**_master(point.hkl, 0.8), "diagnostic_halfwidth": 0.8,
                        "expected_next_information": initial,
                        "predictive": {"predicted_analysis_seconds": 1.0,
                                       "diagnostics": initial, "models": models}}

    _picked, _kind, _reason, score = acq._select_target(
        points, acq_model.fit([]), ranking_cost_kind="analysis",
        scoring_allowance_seconds=0.0)

    assert score.method == "master_predictive"
    assert score.simulation_count == 128


def test_keeps_producing_when_both_master_and_single_over_tolerance():
    # Neither the master nor any single production fit is within tolerance yet → another production
    # scan to accumulate.
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "quicklook", 0.9), _obs((2.1, 0.0, 0.0), "production", 0.9)]
    masters = [_master((2.1, 0.0, 0.0), 0.9)]
    p = acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {}, master_rows=masters)
    assert p.kind == "production" and p.ncount == acq.PRODUCTION_NCOUNT


def test_returns_none_when_master_within_tolerance():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.30)]
    masters = [_master((2.1, 0.0, 0.0), 0.30)]     # pooled interval within tolerance -> converged
    assert acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {}, master_rows=masters) is None


def test_energy_window_rule():
    e_lo, e_hi, e_step, npts = acq._energy_window(5.0, 0.8)
    assert e_lo == pytest.approx(-(5.0 + acq.LOW_MARGIN_MEV))          # spans the anti-Stokes side
    assert e_hi == pytest.approx(5.0 + acq.HIGH_RES_FACTOR * 0.8)       # tight high side vs leakage
    assert e_step == pytest.approx(acq.STEP_RES_FRACTION * 0.8)         # ~FWHM/4
    assert npts <= acq.MAX_POINTS


def test_energy_window_surveys_when_prediction_uncertain():
    """A cold/uncertain ω(q) (large σ_pred) must widen the high side to the survey band so a dispersing
    branch lands in-window — the anti-deadlock rule. A confident σ keeps the tight window."""
    # cold model: e_pred≈0 but σ_pred huge → high side jumps to the survey ceiling, not 0+1.5·FWHM.
    _, e_hi_cold, _, _ = acq._energy_window(0.0, 0.8, acq.SURVEY_E_HI_MEV * 10)
    assert e_hi_cold == pytest.approx(acq.SURVEY_E_HI_MEV)
    # moderate uncertainty (σ>FWHM): high side = e_pred + k·σ, still under the ceiling.
    _, e_hi_mid, _, _ = acq._energy_window(1.0, 0.5, 1.5)
    assert e_hi_mid == pytest.approx(1.0 + acq.SURVEY_SIGMA_K * 1.5)
    # confident (σ≤FWHM): unchanged tight window.
    _, e_hi_tight, _, _ = acq._energy_window(4.0, 0.8, 0.2)
    assert e_hi_tight == pytest.approx(4.0 + acq.HIGH_RES_FACTOR * 0.8)
    # survey forced (q not yet resolved): scan the band even when the model looks confident (σ small) —
    # a concave dispersion sits above the local-linear e_pred, so a tight window would miss the peak.
    _, e_hi_force, _, _ = acq._energy_window(2.0, 0.8, 0.2, survey=True)
    assert e_hi_force == pytest.approx(acq.SURVEY_E_HI_MEV)


def test_energy_window_uses_compiled_optic_search_for_survey():
    e_lo, e_hi, _step, _npts = acq._energy_window(
        0.0, 0.8, acq.SURVEY_E_HI_MEV * 10, energy_search_meV=(-2.0, 12.0))
    assert e_lo <= -2.0
    assert e_hi == pytest.approx(12.0)


def test_energy_window_clamps_point_count():
    # A huge span at a fine step would exceed 100 points; the step is widened to cap it.
    _, _, _, npts = acq._energy_window(200.0, 0.01)
    assert npts <= acq.MAX_POINTS


def test_resolution_callable_and_refusal_fallback():
    obj = _obj()
    called = {}

    def resolve(h, k, l, dE):
        called["hkl"] = (h, k, l)
        return {"ok": True, "fwhm": {"dE": 1.2}}

    p = acq.propose_scan(obj, acq_model.fit([]), [], resolve, {})
    assert p.fwhm_dE == pytest.approx(1.2)
    assert called["hkl"][0] in (2.1, 2.2, 2.3, 2.4)
    # A refusal (ok == false) falls back to the default FWHM.
    p2 = acq.propose_scan(obj, acq_model.fit([]), [], lambda *a: {"ok": False}, {})
    assert p2.fwhm_dE == pytest.approx(acq.DEFAULT_FWHM_MEV)


def test_scan_command_grammar():
    p = acq.propose_scan(_obj(), acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})
    assert p.scan_command().startswith("deltaE ")
    assert len(p.scan_command().split()) == 4


def test_optic_hypothesis_starts_at_zone_center_in_positive_energy_band():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec.segment(q_start=(2, 0, 0), q_end=(2, 0.2, 0), step_rlu=0.1,
                                tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12},
                                target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    p = acq.propose_scan(obj, acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})
    assert (p.h, p.k, p.l) == (2.0, 0.0, 0.0)
    assert p.e_lo > 0.0 and p.e_lo <= 1.0
    assert p.e_hi == pytest.approx(12.0)
    assert p.meta["purpose"] == "hypothesis_check"


def test_rival_band_and_absent_signal_widening_remain_point_capped():
    e_lo, e_hi, _step, npts = acq._energy_window(
        6.0, 0.5, 0.5, energy_search_meV=(-2.0, 12.0), rival_band=(3.0, 1.0), widen=2.5)
    assert e_lo <= 1.0
    assert e_hi == 12.0
    assert npts <= acq.MAX_POINTS


def test_hypothesis_scan_respects_frozen_point_cap():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    frozen["steering"] = {**frozen["steering"], "max_scan_points": 24}
    frozen["hypothesis_fingerprint"] = content_fingerprint(
        frozen["hypotheses"], frozen["steering"])
    obj = ObjectiveSpec.segment(q_start=(2, 0, 0), q_end=(2, 0.1, 0), step_rlu=0.1,
                                tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12},
                                target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    proposal = acq.propose_scan(obj, acq_model.fit([]), [], {"fwhm_dE": 0.5}, {})
    assert proposal.n_points == 24
    assert proposal.meta["max_scan_points"] == 24
    assert acq.points_in_window(
        proposal.e_lo, proposal.e_hi, proposal.e_step,
        max_points=proposal.meta["max_scan_points"]) == 24


def test_higher_value_classification_probe_can_leave_certification_segment():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec.segment(q_start=(2, 0, 0), q_end=(2, 0.2, 0), step_rlu=0.1,
                                tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12},
                                target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    observations = [_obs(tuple(point), "quicklook", 0.8, omega0=6.0) for point in obj.path]
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    model = acq_model.fit(model_points(summarize_grid(obj, observations)))

    def classification_value(candidate):
        return 1.0 if candidate.hkl[1] <= 0.2 + 1e-9 else 2.0

    proposal = acq.propose_scan(
        obj, model, observations, {"fwhm_dE": 0.5}, {}, classify=classification_value)
    assert proposal.h == 2.0 and proposal.k > 0.2
    assert proposal.meta["driver_inserted"] is True
    assert proposal.meta["certification_eligible"] is False
    assert proposal.meta["purpose"] == proposal.meta["insertion_purpose"] == "differentiate"
    assert proposal.meta["score"]["classification_eig"] == pytest.approx(2.0)
    assert proposal.meta["score"]["combined_information"] > 0.0

    # Persisted evidence at that Q makes resume advance to a different candidate, never repeat it.
    observations.append(_obs((proposal.h, proposal.k, proposal.l), "quicklook", 0.8, omega0=6.0))
    resumed = acq.propose_scan(
        obj, model, observations, {"fwhm_dE": 0.5}, {}, classify=classification_value)
    assert (resumed.h, resumed.k, resumed.l) != (proposal.h, proposal.k, proposal.l)


def test_driver_insertion_stays_clear_of_extremely_fine_certification_grid():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec.segment(q_start=(2, 0, 0), q_end=(2, 0.00005, 0), step_rlu=0.00005,
                                tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12},
                                target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    observations = [_obs(tuple(point), "quicklook", 0.8, omega0=6.0) for point in obj.path]
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    model = acq_model.fit(model_points(summarize_grid(obj, observations)))
    proposal = acq.propose_scan(
        obj, model, observations, {"fwhm_dE": 0.5}, {}, classify=lambda _candidate: 2.0)
    assert proposal is not None
    assert proposal.meta["driver_inserted"] is False
    assert any(math.dist((proposal.h, proposal.k, proposal.l), tuple(point)) <= 1e-4
               for point in obj.path)


def test_driver_insertion_requires_finite_requested_grid_comparison():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    obj = ObjectiveSpec.segment(q_start=(2, 0, 0), q_end=(2, 0.2, 0), step_rlu=0.1,
                                tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12},
                                target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)
    observations = [_obs(tuple(point), "quicklook", 0.8, omega0=6.0) for point in obj.path]
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    model = acq_model.fit(model_points(summarize_grid(obj, observations)))

    def classification_value(candidate):
        on_grid = any(math.dist(candidate.hkl, tuple(point)) <= 1e-4 for point in obj.path)
        return float("nan") if on_grid else 2.0

    proposal = acq.propose_scan(
        obj, model, observations, {"fwhm_dE": 0.5}, {}, classify=classification_value)
    assert proposal is not None
    assert proposal.meta["driver_inserted"] is False


# -- enumerate/select seam (PR1.2): the inventory must faithfully mirror propose_scan -------------

def test_enumerate_select_mirrors_propose_scan_when_an_action_exists():
    obj = _obj()
    model = acq_model.fit([])
    inventory, candidates = acq.enumerate_actions(obj, model, [], resolution_info={"fwhm_dE": 0.8})
    chosen = acq.select_action(inventory, candidates)
    proposal = acq.propose_scan(obj, model, [], {"fwhm_dE": 0.8}, {})
    # An action is available, the enumeration completed, and it is not a terminal.
    assert inventory.is_complete is True
    assert inventory.has_eligible_action is True
    assert inventory.terminal_cause() is None
    # select_action picks the same q propose_scan builds its proposal at.
    assert chosen is not None and proposal is not None
    assert chosen.pick.hkl[0] == pytest.approx(proposal.h)


def test_enumerate_reports_action_inventory_exhausted_when_nothing_remains():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.30)]
    masters = [_master((2.1, 0.0, 0.0), 0.30)]     # pooled interval within tolerance -> converged
    inventory, candidates = acq.enumerate_actions(
        obj, _model(obs), obs, resolution_info={"fwhm_dE": 0.8}, master_rows=masters)
    assert acq.select_action(inventory, candidates) is None
    assert acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {}, master_rows=masters) is None
    assert inventory.has_eligible_action is False
    assert inventory.is_complete is True
    assert inventory.terminal_cause() == "action_inventory_exhausted"


def test_propose_action_returns_proposal_and_inventory_mirroring_propose_scan():
    obj = _obj()
    model = acq_model.fit([])
    proposal, inventory = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    assert proposal is not None
    assert inventory.is_complete and inventory.has_eligible_action
    assert inventory.terminal_cause() is None
    # The back-compat wrapper drops the inventory but builds the byte-identical proposal.
    plain = acq.propose_scan(obj, model, [], {"fwhm_dE": 0.8}, {})
    assert (plain.h, plain.k, plain.l) == (proposal.h, proposal.k, proposal.l)
    assert plain.ncount == proposal.ncount and plain.kind == proposal.kind
    assert plain.e_lo == proposal.e_lo and plain.e_hi == proposal.e_hi


def test_propose_action_returns_none_proposal_with_exhausted_inventory():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.30)]
    masters = [_master((2.1, 0.0, 0.0), 0.30)]
    proposal, inventory = acq.propose_action(
        obj, _model(obs), obs, {"fwhm_dE": 0.8}, {}, master_rows=masters)
    assert proposal is None
    assert inventory.terminal_cause() == "action_inventory_exhausted"


# -- intent / proposal identity coherence (PR1.7d) -------------------------------------------------

def _hypothesis_objective():
    path = [(2.0, 0.0, 0.0), (2.1, 0.0, 0.0), (2.2, 0.0, 0.0)]
    return ObjectiveSpec(path=path, tolerance_meV=0.5,
                         budget={"kind": "neutrons", "amount": 1e12}, target_branch="acoustic",
                         **shipped_hypothesis_snapshot("acoustic", (-2.0, 7.0)))


def test_inventory_action_class_matches_the_emitted_proposal_for_a_driver_inserted_probe():
    """Review #8: the intent was built from the score's purpose *before* `_build_proposal` discovered
    the pick was driver-inserted and flipped that purpose to ``differentiate``, so the inventory
    recorded a quicklook while the emitted action was a differentiation probe."""
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    obj = _hypothesis_objective()
    obs = [_obs(p, "quicklook", 0.9) for p in obj.path]
    model = acq_model.fit(model_points(summarize_grid(obj, obs)))
    classify = lambda g: 9.0 if math.dist(tuple(g.hkl), (2.15, 0.0, 0.0)) <= 1e-6 else 0.0

    _inventory, candidates = acq.enumerate_actions(
        obj, model, obs, resolution_info={"fwhm_dE": 0.8}, classify=classify)
    proposal, _inv = acq.propose_action(obj, model, obs, {"fwhm_dE": 0.8}, {}, classify=classify)

    intent = candidates[0].evaluation.intent
    assert candidates[0].driver_inserted is True
    assert intent.action_class == "differentiation_probe"
    assert proposal.meta["driver_inserted"] is True
    assert proposal.meta["action_class"] == intent.action_class      # no post-hoc reclassification
    # The window family follows the same decision: a driver-inserted probe is a differentiation probe
    # by construction, so describing it as a "target" window while emitting `purpose=differentiate`
    # would leave the same incoherence one field over.
    assert intent.window_family == "differentiate"
    assert proposal.meta["purpose"] == "differentiate"
    assert proposal.meta["insertion_purpose"] == "differentiate"
    # The emitted scan joins back to the evaluation that justified it.
    assert proposal.meta["scientific_action_fingerprint"] == intent.scientific_action_fingerprint
    # ...and the intent is anchored to the frozen hypothesis snapshot it was formed under.
    assert intent.hypotheses_fingerprint == obj.hypothesis_fingerprint


def test_the_scientific_fingerprint_tracks_the_evidence_basis():
    """Review #7: two iterations formed against different authoritative evidence must not collapse to
    one scientific action identity."""
    from isar.schema.steering import ProposalScore
    obj = _obj()
    pick = acq.GridQ(index=0, hkl=(2.1, 0.0, 0.0), q=0.0, tolerance=0.5)
    score = ProposalScore(method="deficit")

    pick.evidence_master = {"content_hash": "rev-A"}
    first = acq._intent_from_pick(obj, pick, "production", score)
    pick.evidence_master = {"content_hash": "rev-B"}
    second = acq._intent_from_pick(obj, pick, "production", score)

    assert first.evidence_basis_fingerprint == "rev-A"
    assert second.evidence_basis_fingerprint == "rev-B"
    assert first.scientific_action_fingerprint != second.scientific_action_fingerprint


def test_the_evaluation_carries_the_real_score_vector_and_selection_reason():
    """The persisted artifact presents this as the ranked score record, so defaults were a lie."""
    obj = _obj()
    inventory, candidates = acq.enumerate_actions(
        obj, acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})
    evaluation, chosen = candidates[0].evaluation, candidates[0]
    assert evaluation.selection_reason == chosen.reason and evaluation.selection_reason
    assert evaluation.score_method == chosen.score.method
    persisted = inventory.to_full_dict()["evaluations"][0]
    assert persisted["selection_reason"] == chosen.reason
    assert persisted["score"]["score_method"] == chosen.score.method


# -- generator failure isolation (PR1.7b) ----------------------------------------------------------

def test_a_raising_generator_becomes_a_failed_report_not_an_escaped_exception(monkeypatch):
    """Before this, a generator exception escaped `enumerate_actions` before an ActionInventory existed,
    so the production path could never produce the FAILED/controller_fault state the model promises —
    it just killed the campaign. The fault must now be recorded as incomplete accounting."""
    from isar.drive.acquisition.inventory import LEGACY_SELECTOR_GENERATOR

    def boom(*_args, **_kwargs):
        raise RuntimeError("classification provider exploded")

    monkeypatch.setattr(acq, "_select_target", boom)
    inventory, candidates = acq.enumerate_actions(
        _obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})
    assert candidates == []
    # Isolation is the claim: the faulting generator is recorded as failed and its peers keep
    # reporting normally. Asserting it is the *only* report would instead pin the registry's arity,
    # which PR2.7a deliberately changed.
    failed = inventory.failed_reports()
    assert [r.generator for r in failed] == [LEGACY_SELECTOR_GENERATOR]
    assert len(failed) == 1 and "RuntimeError" in failed[0].error_code
    assert "classification provider exploded" in failed[0].error_code
    # An unknown action space is never an exhaustion proof.
    assert inventory.is_complete is False
    assert inventory.terminal_cause() == "controller_fault"


def test_the_live_inventory_validates_against_the_declared_registry():
    """The happy path must be registry-valid by construction, so validation is a real gate."""
    inventory, _candidates = acq.enumerate_actions(
        _obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8})
    assert inventory.validate() == []
    assert inventory.is_complete is True


# -- indexing fault (PR1.5): a driver-inserted candidate must not carry a synthetic grid index -----

def _ambiguous_adequate_anchor():
    from isar.drive.supervisor.stopping import GridQ
    anchor = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5, n_production=1,
                   track_state="ambiguous")
    anchor.local_identity_clear = False
    anchor.evidence_master = {
        "branch_candidates": [{"energy_meV": 5.98, "sigma_meV": 0.05}],
        "candidate_state": "provisional", "diagnostic_halfwidth": 0.05,
        "target_branch": "acoustic", "branch_posterior": {"acoustic": 0.7}}
    neighbor = GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5, n_production=1,
                     track_state="confirmed")
    neighbor.local_identity_clear = True
    return [anchor, neighbor]


def test_tracked_broadening_crashes_on_a_raw_synthetic_index_but_not_a_normalized_one():
    """The fault, isolated. ``_driver_inserted_candidate`` builds a candidate with the synthetic
    ``index = len(grid) + offset``; if that value enters the working grid, ``_broadening_classification
    _diagnostics`` uses it as a list position and indexes out of range. The identical candidate carrying
    its real position broadens safely — which is the invariant ``enumerate_actions`` now guarantees."""
    from isar.drive.supervisor.stopping import GridQ
    classify = lambda g: 1.0

    base = _ambiguous_adequate_anchor()
    raw = [*base, GridQ(index=len(base) + 3, hkl=(2.2, 0.0, 0.0), q=0.2, tolerance=1.5)]
    with pytest.raises(IndexError):
        acq._tracked_frontier_target(raw, anchor_index=0, classify=classify)

    normalized = _ambiguous_adequate_anchor()
    normalized.append(GridQ(index=len(normalized), hkl=(2.2, 0.0, 0.0), q=0.2, tolerance=1.5))
    result = acq._tracked_frontier_target(normalized, anchor_index=0, classify=classify)
    assert result is not None and result[1] == "quicklook"     # broadens instead of crashing


def test_enumerate_actions_normalizes_the_driver_inserted_candidate_index():
    """The fix in situ. A driver-inserted probe wins with a synthetic index (offset >= 1), but once
    ``enumerate_actions`` appends it to the working grid its index is its real list position, so the
    synthetic value never reaches a position-based reader."""
    from isar.drive.supervisor.stopping import model_points, summarize_grid
    path = [(2.0, 0.0, 0.0), (2.1, 0.0, 0.0), (2.2, 0.0, 0.0)]
    obj = ObjectiveSpec(path=path, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
                        target_branch="acoustic", **shipped_hypothesis_snapshot("acoustic", (-2.0, 7.0)))
    obs = [_obs(p, "quicklook", 0.9) for p in path]            # every grid q visited
    model = acq_model.fit(model_points(summarize_grid(obj, obs)))
    # Favour the second midpoint (2.15,0,0) — candidate offset 1, so its synthetic index overshoots.
    classify = lambda g: 9.0 if math.dist(tuple(g.hkl), (2.15, 0.0, 0.0)) <= 1e-6 else 0.0

    grid = summarize_grid(obj, obs)
    inserted = acq._driver_inserted_candidate(obj, grid, obs, classify)
    assert inserted is not None
    assert inserted.index == len(grid) + 1                     # raw synthetic index (offset 1)

    _inventory, candidates = acq.enumerate_actions(
        obj, model, obs, resolution_info={"fwhm_dE": 0.8}, classify=classify)
    pick = candidates[0].pick
    assert tuple(pick.hkl) == pytest.approx((2.15, 0.0, 0.0))  # the inserted probe was selected
    assert pick.index == len(grid)                             # normalized to its real grid position


# --- scan-window EIG mode: shadow plumbing must not move a single neutron ------------------------

def _eig_objective(mode: str):
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    frozen["steering"] = {**frozen["steering"], "scan_window_eig_mode": mode}
    frozen["hypothesis_fingerprint"] = content_fingerprint(
        frozen["hypotheses"], frozen["steering"])
    return ObjectiveSpec.segment(
        q_start=(2, 0, 0), q_end=(2, 0.2, 0), step_rlu=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e12},
        target_branch="optic", energy_search_meV=(-2.0, 12.0), **frozen)


def _request(proposal):
    """The physical request: what the instrument is actually asked to do."""
    return (proposal.h, proposal.k, proposal.l, proposal.e_lo, proposal.e_hi,
            proposal.e_step, proposal.n_points, proposal.ncount, proposal.kind,
            proposal.scan_command())


def _proposal_with_divergent_evidence_revisions(mode: str):
    """Build through the real proposal seam with distinct transition and master identities."""
    objective = _eig_objective(mode)
    model = acq_model.fit([])
    plan = acq.plan_actions(
        objective, model, [], resolution_info={"fwhm_dE": 0.8}, budget_state={})
    chosen = acq.select_action(plan.inventory, plan.candidates)
    assert chosen is not None
    chosen.pick.evidence_master = {
        "master_revision": "master-revision-exact",
        "content_hash": "master-content-fallback",
    }
    transitioned_score = replace(
        chosen.score,
        evidence_transition={"authoritative_evidence_revision": "transition-revision"},
    )
    chosen = replace(
        chosen, generated=replace(chosen.generated, score=transitioned_score))
    build = plan._build_context
    return acq._build_proposal(
        build[0], chosen, build[1], build[2], build[3], build[4], build[5], build[6])


def test_shadow_mode_emits_the_identical_request_as_the_off_mode_legacy_path():
    # Only the *physical* request can be asserted identical: materializing the new steering field
    # remints every hypothesis/scientific-action fingerprint once, by design (the knob is part of
    # the frozen policy snapshot).  Neutrons, window, and ordering must not move at all.
    off = acq.propose_scan(_eig_objective("off"), acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})
    shadow = acq.propose_scan(
        _eig_objective("shadow"), acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})

    assert _request(off) == _request(shadow)
    assert (off.meta["authoritative_evidence_revision"]
            == shadow.meta["authoritative_evidence_revision"])
    from isar.drive import loop as drive_loop
    assert drive_loop._request_fingerprint_for(
        off, e_lo=off.e_lo, e_hi=off.e_hi, e_step=off.e_step, ncount=off.ncount,
    ) == drive_loop._request_fingerprint_for(
        shadow, e_lo=shadow.e_lo, e_hi=shadow.e_hi,
        e_step=shadow.e_step, ncount=shadow.ncount,
    )
    assert off.meta["count_diagnostics"] == shadow.meta["count_diagnostics"]
    assert (off.scan_design_inventory.selected_candidate_id
            == shadow.scan_design_inventory.selected_candidate_id)
    assert len(off.scan_design_inventory.candidates) == 1
    assert len(shadow.scan_design_inventory.candidates) > 1
    assert shadow.scan_design_inventory.selection_basis == (
        "shadow_legacy_control_unscored")
    assert off.scan_design_inventory.selection_basis == "legacy_control_off"
    assert shadow.scan_design_inventory.context.scan_window_eig_mode == "shadow"


def test_shadow_exact_source_uses_master_revision_without_reminting_proposal_identity():
    off = _proposal_with_divergent_evidence_revisions("off")
    shadow = _proposal_with_divergent_evidence_revisions("shadow")

    assert _request(off) == _request(shadow)
    assert off.meta["authoritative_evidence_revision"] == "transition-revision"
    assert shadow.meta["authoritative_evidence_revision"] == "transition-revision"
    source = shadow.shadow_scoring_source
    assert source.master_revision == "master-revision-exact"
    assert source.action_provenance["authoritative_evidence_revision"] == (
        "master-revision-exact")

    from isar.drive import loop as drive_loop
    assert drive_loop._request_fingerprint_for(
        off, e_lo=off.e_lo, e_hi=off.e_hi, e_step=off.e_step, ncount=off.ncount,
    ) == drive_loop._request_fingerprint_for(
        shadow, e_lo=shadow.e_lo, e_hi=shadow.e_hi,
        e_step=shadow.e_step, ncount=shadow.ncount,
    )


def test_shadow_candidates_share_the_selected_action_q_kind_and_reason():
    proposal = acq.propose_scan(
        _eig_objective("shadow"), acq_model.fit([]), [], {"fwhm_dE": 0.8}, {})
    selected = proposal.scan_design_inventory.selected_candidate

    for candidate in proposal.scan_design_inventory.candidates:
        assert candidate.hkl == selected.hkl and candidate.q == selected.q
        assert candidate.kind == selected.kind
        assert candidate.reason == selected.reason
    assert (selected.e_lo, selected.e_hi, selected.e_step, selected.n_points,
            selected.ncount) == (proposal.e_lo, proposal.e_hi, proposal.e_step,
                                 proposal.n_points, proposal.ncount)


def test_declaring_authoritative_scan_window_eig_refuses_at_campaign_setup():
    with pytest.raises(ValueError, match="scan_window_eig_authoritative_unavailable"):
        _eig_objective("authoritative")
