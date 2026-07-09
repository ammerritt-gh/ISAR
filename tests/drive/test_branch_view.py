"""Phase 5b-live Part B — the drive reads the persisted branch boundary-cleanly and grounds the Fisher
provider. The drift guard keeps the drive-local predict in lockstep with the session's."""
import math

import pytest

from isar.drive import loop
from isar.drive.acquisition.branch_view import BranchView, predict_branch, select_branch
from isar.drive.supervisor.stopping import GridQ
from isar.session import branch_model


def _anchor(sample, T, w, g, a, fwhm=0.5):
    return {"scan_id": f"{sample}-{w}", "sample": sample, "T": T, "t_bin": float(T),
            "omega0": float(w), "modality": "fold", "verdict": "resolved", "gamma": float(g),
            "fit_kind": "unconstrained",
            "intensity": {"value": float(a), "gauss_fwhm": fwhm, "lorentz_fwhm": 0.1,
                          "exposure": 1e7, "counting_mode": "monitor", "instrument": "x"}}


def _branch():
    pts = [_anchor("A", 300, 4.0, 0.30, 0.25), _anchor("A", 300, 5.0, 0.30, 0.20),
           _anchor("A", 300, 6.0, 0.30, 0.16)]
    return branch_model.build_branch(pts, sample="A", t_bin=300.0)


# --------------------------------------------------------------------------- drift guard
@pytest.mark.parametrize("w", [3.0, 4.0, 4.7, 6.0, 7.5])   # below / in / above the anchor range
def test_predict_branch_matches_session_predict(w):
    branch = _branch()
    drive = predict_branch(branch, w)
    sess = branch_model.predict(branch, w)
    assert isinstance(drive, BranchView)
    assert drive.gamma == pytest.approx(sess.gamma)
    assert drive.gamma_sigma == pytest.approx(sess.gamma_sigma)
    assert drive.amplitude_norm == pytest.approx(sess.amplitude_norm)
    assert drive.amplitude_rel_sigma == pytest.approx(sess.amplitude_rel_sigma)
    assert drive.in_anchor_range == sess.in_anchor_range


# --------------------------------------------------------------------------- select_branch
def test_select_branch_by_coverage_then_nearest_else_none():
    b_lo = {"branch_id": "acoustic", "w_min": 2.0, "w_max": 4.0}
    b_hi = {"branch_id": "acoustic", "w_min": 4.0, "w_max": 8.0}
    assert select_branch([b_lo, b_hi], 3.0) is b_lo          # covered by lo
    assert select_branch([b_lo, b_hi], 6.0) is b_hi          # covered by hi
    assert select_branch([b_lo, b_hi], 9.0) is b_hi          # none covers → nearest edge
    assert select_branch([], 5.0) is None
    assert select_branch([{"branch_id": "magnon", "w_min": 4.0, "w_max": 8.0}], 5.0) is None


# --------------------------------------------------------------------------- live Fisher provider
def _gridq(fit_kind):
    return GridQ(index=0, hkl=(2.1, 0.0, 0.0), q=0.0, tolerance=0.1, n_production=2,
                 best_prod_halfwidth=0.4, best_prod_omega0=5.0, counts_spent=2e10,
                 last_prod_cost=1e10, last_fit_kind=fit_kind)


def test_fisher_provider_grounds_unconstrained_in_range():
    card = {"branch_models": [_branch()]}
    provider = loop._make_fisher_provider(card, {"fwhm_dE": 0.5})
    assert provider is not None
    ss = provider(_gridq("unconstrained"), 5.0, 0.3)         # ω₀=5 in [4,6], last fit unconstrained
    assert ss is not None and math.isfinite(ss) and ss > 0


def test_fisher_provider_declines_when_ungrounded():
    card = {"branch_models": [_branch()]}
    provider = loop._make_fisher_provider(card, {"fwhm_dE": 0.5})
    assert provider(_gridq("soft_prior"), 5.0, 0.3) is None      # last fit not unconstrained
    assert provider(_gridq("unconstrained"), 50.0, 0.3) is None  # ω₀ far outside anchor range


def test_no_branch_yields_no_provider():
    assert loop._make_fisher_provider({"branch_models": []}, {"fwhm_dE": 0.5}) is None
    assert loop._make_fisher_provider({}, {"fwhm_dE": 0.5}) is None
