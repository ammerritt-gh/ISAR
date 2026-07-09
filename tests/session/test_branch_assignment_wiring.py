"""Phase A wiring: the branch-assignment posterior feeds the session's prior, seeding, and near-Γ
constrained recipe. Guards that an optic-tagged point cannot pollute the acoustic predictors."""
import pytest

from isar.schema.card import CardState
from isar.session import branch_model as BM
from isar.session import session_prior as SP


def _add(state, scan_id, q, omega0, *, branch_id, weight=1.0, sample="S", T=200.0):
    return SP.add_point(state, scan_id=scan_id, sample=sample, T=T, q=[q, 0.0, 0.0], omega0=omega0,
                        modality="fold", verdict="resolved", gamma=0.3, gamma_err=0.05,
                        intensity={"value": 100.0, "exposure": 1e6, "counting_mode": "monitor",
                                   "gauss_fwhm": 1.366, "lorentz_fwhm": 0.0, "instrument": "TAVI"},
                        branch_id=branch_id, weight=weight)


# ---------------------------------------------------------------- prior_for is acoustic-clean
def test_prior_for_period_ignores_optic_pollution():
    """A stored optic anchor (6 meV) must NOT bias the acoustic prior at low q — the period path is
    the branch-filtered acoustic belief, not a global polyfit over every point."""
    st = CardState()
    _add(st, "A0", 0.20, 1.85, branch_id="acoustic")
    _add(st, "A1", 0.30, 2.75, branch_id="acoustic")
    _add(st, "O0", 0.10, 6.05, branch_id="optic")     # the wrong branch — must be filtered out
    pr = SP.prior_for(st, sample="S", T=200.0, q=[2.15, 0.0, 0.0], period=2.0)
    assert pr is not None and pr.omega0 == pytest.approx(1.4, abs=0.7)   # acoustic, not dragged to 6
    # the pre-steering polyfit path (no period) pools ALL points incl. the optic → a polluted,
    # materially different prediction (a garbage line through the acoustic+optic mix)
    old = SP.prior_for(st, sample="S", T=200.0, q=[2.15, 0.0, 0.0])
    assert old is not None and abs(old.omega0 - pr.omega0) > 2.0


def test_add_point_stores_branch_id_and_weight():
    st = CardState()
    _add(st, "A0", 0.20, 1.85, branch_id="acoustic", weight=0.9)
    p = st.dispersion_prior[0]
    assert p["branch_id"] == "acoustic" and p["weight"] == pytest.approx(0.9)


# ---------------------------------------------------------------- branch-filtered seeding
def _anchor(scan_id, w, *, branch_id, sample="S", T=200.0):
    return {"scan_id": scan_id, "sample": sample, "T": T, "t_bin": T, "q": [3, 0.2, 0],
            "omega0": w, "modality": "fold", "verdict": "resolved", "gamma": 0.3 + 0.05 * w,
            "branch_id": branch_id, "weight": 1.0,
            "intensity": {"value": 600.0 / w, "exposure": 1e6, "counting_mode": "monitor",
                          "gauss_fwhm": 1.5, "lorentz_fwhm": 0.0, "instrument": "TAVI"}}


def test_build_branch_filters_by_branch_id():
    """The acoustic Γ/A branch is built only from acoustic-assigned anchors; an optic anchor (even a
    clean, well-separated one) is excluded — it can no longer seed or bend the acoustic branch."""
    pts = [_anchor(f"A:{i}", w, branch_id="acoustic") for i, w in enumerate((3.0, 4.0, 5.0))]
    pts.append(_anchor("O:0", 6.5, branch_id="optic"))
    b = BM.build_branch(pts, sample="S", t_bin=200.0, branch_id="acoustic")
    assert b is not None and b["n_anchors"] == 3          # the optic anchor is filtered out
    assert b["w_max"] == pytest.approx(5.0)               # not 6.5
    # too few optic anchors to build the optic branch
    assert BM.build_branch(pts, sample="S", t_bin=200.0, branch_id="optic") is None


# ---------------------------------------------------------------- near-Γ constrained recipe
def test_hard_constraints_fix_gamma_and_amplitude():
    """The near-Γ constrained recipe hard-fixes BOTH Γ and A (to the branch 1/ω law), reducing the
    fit to ~ω₀-only so the blob brightness pins ω₀."""
    pts = [_anchor(f"A:{i}", w, branch_id="acoustic") for i, w in enumerate((3.0, 4.0, 5.0, 6.0))]
    b = BM.build_branch(pts, sample="S", t_bin=200.0)
    pred = BM.predict(b, 4.5)
    cons = pred.hard_constraints(exposure=1e6)
    assert "phonon.Gamma" in cons.hard and "phonon.A" in cons.hard
    assert not cons.soft                                  # nothing soft — the constrained recipe is hard
    assert cons.hard["phonon.A"] == pytest.approx(pred.amplitude_norm * 1e6, rel=1e-6)
    # the ordinary soft rescue keeps A soft (never hard), for contrast
    soft = pred.soft_constraints(exposure=1e6)
    assert "phonon.A" in soft.soft and not soft.hard
