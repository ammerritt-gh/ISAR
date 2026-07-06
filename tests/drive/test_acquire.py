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


def test_escalates_to_confirm_after_a_resolved_first_crossing():
    # Single-point path: one resolved production first crossing -> the next scan is a confirm.
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "quicklook", 0.9), _obs((2.1, 0.0, 0.0), "production", 0.30)]
    p = acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {})
    assert p.kind == "confirm" and p.ncount == acq.PRODUCTION_NCOUNT


def test_returns_none_when_all_converged():
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.1, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12})
    obs = [_obs((2.1, 0.0, 0.0), "production", 0.30), _obs((2.1, 0.0, 0.0), "confirm", 0.35)]
    assert acq.propose_scan(obj, _model(obs), obs, {"fwhm_dE": 0.8}, {}) is None


def test_energy_window_rule():
    e_lo, e_hi, e_step, npts = acq._energy_window(5.0, 0.8)
    assert e_lo == pytest.approx(-(5.0 + acq.LOW_MARGIN_MEV))          # spans the anti-Stokes side
    assert e_hi == pytest.approx(5.0 + acq.HIGH_RES_FACTOR * 0.8)       # tight high side vs leakage
    assert e_step == pytest.approx(acq.STEP_RES_FRACTION * 0.8)         # ~FWHM/4
    assert npts <= acq.MAX_POINTS


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
