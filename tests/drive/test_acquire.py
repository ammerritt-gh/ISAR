"""The acquisition function: target selection, escalation ladder, and the energy-window rule."""
import math

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


def test_unvisited_q_preferred_over_escalation():
    obj = _obj()
    # Three q have a quick-look; (2.4,0,0) is untouched -> it must be the target, as a quick-look.
    obs = [_obs((h, 0.0, 0.0), "quicklook", 0.9) for h in (2.1, 2.2, 2.3)]
    p = acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {})
    assert p.kind == "quicklook"
    assert (p.h, p.k, p.l) == (2.4, 0.0, 0.0)


def test_picks_worst_deficit_q_for_production():
    obj = _obj()
    # All quick-looked; (2.3,0,0) has the widest calibrated half-width -> largest deficit.
    obs = [_obs((2.1, 0.0, 0.0), "quicklook", 0.6),
           _obs((2.2, 0.0, 0.0), "quicklook", 0.7),
           _obs((2.3, 0.0, 0.0), "quicklook", 1.5),
           _obs((2.4, 0.0, 0.0), "quicklook", 0.65)]
    p = acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {})
    assert (p.h, p.k, p.l) == (2.3, 0.0, 0.0)
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


def _master(hkl, hw, disp="resolved", reliable=True, n=2):
    return {"hkl": hkl, "omega0": 3.0, "halfwidth": hw, "reliable": reliable,
            "usable": disp in ("resolved", "flagged"), "disposition": disp, "n_members": n}


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
