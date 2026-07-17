"""The acquisition function: target selection, escalation ladder, and the energy-window rule."""
import pytest

from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition import acquire as acq
from isar.drive.objective import ObjectiveSpec


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
