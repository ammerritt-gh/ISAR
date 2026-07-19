"""The drive-local persisted-branch view stays in lockstep with the session's."""

import pytest

from isar.drive.acquisition.branch_view import BranchView, predict_branch, select_branch
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
