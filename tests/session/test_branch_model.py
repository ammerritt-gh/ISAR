"""Phase 2b — the cross-q BranchModel (ω₀-keyed Γ and 1/ω intensity), and its soft constraints."""
import pytest

from isar.schema.card import CardState
from isar.session import branch_model as BM
from isar.session import session_prior as SP


def _pt(scan_id, w, g, a, sample="S", T=100.0, fwhm=1.5):
    """A dispersion-prior point dict as add_point would store (ω₀, Γ, portable intensity)."""
    return {"scan_id": scan_id, "sample": sample, "T": T, "t_bin": T, "q": [3, 0.2, 0],
            "omega0": w, "modality": "fold", "verdict": "resolved", "gamma": g,
            "intensity": {"value": a, "exposure": 1e6, "counting_mode": "monitor",
                          "gauss_fwhm": fwhm, "lorentz_fwhm": 0.0, "instrument": "S30"}}


def _anchors():
    # a = 600/w (acoustic 1/ω); Γ = 0.3 + 0.05·w. All well-separated (w ≥ 3 > 2.25 = 1.5·fwhm).
    return [_pt(f"S:{i}", w, 0.3 + 0.05 * w, 600.0 / w) for i, w in enumerate((3.0, 4.0, 5.0, 6.0))]


def test_retired_basis_anchors_are_excluded_and_said_out_loud(caplog):
    """A pre-H4 point holds the MEASURED amplitude A·(n+1) under the retired basis. The stored
    number cannot be told from an intrinsic one and the occupation is not recoverable, so fitting
    log A(ω₀) across the mixture would emit intensity constraints wrong by (n+1).

    Excluded, never reinterpreted — and logged, because a resumed campaign would otherwise lose
    anchors in silence."""
    fresh = _anchors()
    stale = [_pt(f"old:{i}", w, 0.4, 900.0 / w) for i, w in enumerate((3.5, 4.5, 5.5))]
    for point in stale:
        point["intensity"]["basis"] = "amplitude_per_exposure"      # retired by H4

    with caplog.at_level("WARNING"):
        mixed = BM.build_branch(fresh + stale, sample="S", t_bin=100.0)
    clean = BM.build_branch(fresh, sample="S", t_bin=100.0)

    assert mixed is not None
    assert mixed["n_anchors"] == clean["n_anchors"] == 4        # the stale three never entered
    assert mixed["logA"] == clean["logA"]                       # …and moved no coefficient
    assert "retired intensity basis" in caplog.text


def test_retired_basis_can_starve_a_branch_rather_than_bias_it():
    """Losing anchors is the acceptable outcome; a wrong 1/ω law is not. Below MIN_ANCHORS the
    existing "too sparse" path returns None instead of fitting what is left plus the stale."""
    stale = _anchors()
    for point in stale:
        point["intensity"]["basis"] = "amplitude_per_exposure"

    assert BM.build_branch(stale, sample="S", t_bin=100.0) is None


# --------------------------------------------------------------------------- build + predict
def test_build_recovers_one_over_omega_intensity_and_gamma_trend():
    b = BM.build_branch(_anchors(), sample="S", t_bin=100.0)
    assert b is not None and b["n_anchors"] == 4
    # 1/ω law → log A slope ≈ −1
    assert b["logA"][0] == pytest.approx(-1.0, abs=1e-6)
    p = BM.predict(b, 4.5)
    assert p.amplitude_norm == pytest.approx(600.0 / 4.5, rel=1e-3)
    assert p.gamma == pytest.approx(0.3 + 0.05 * 4.5, abs=1e-6)
    assert p.in_anchor_range is True


def test_too_few_anchors_returns_none():
    assert BM.build_branch(_anchors()[:2], sample="S", t_bin=100.0) is None


def test_extrapolation_inflates_sigma_and_flags_out_of_range():
    b = BM.build_branch(_anchors(), sample="S", t_bin=100.0)
    inside = BM.predict(b, 4.5)
    outside = BM.predict(b, 1.0)               # below the anchor ω₀ range
    assert outside.in_anchor_range is False
    assert outside.gamma_sigma > inside.gamma_sigma
    assert outside.amplitude_rel_sigma >= inside.amplitude_rel_sigma


def test_a_predicted_width_stops_at_the_lone_ceiling():
    # anchors fitted under recipe /6 may carry a 6 meV width; a solo fit at omega0 2 may claim only 2
    b = {"branch_id": "acoustic", "sample": "S", "t_bin": 100.0, "w_min": 1.0, "w_max": 5.0,
         "n_anchors": 3, "gamma": [0.0, 6.0, 0.1], "logA": [-1.0, 6.4, 0.1]}
    p = BM.predict(b, 2.0)
    assert p.gamma == pytest.approx(2.0)
    assert BM.predict(b, 10.0).gamma == pytest.approx(4.0)
    assert p.hard_constraints(1e6, 1.0).hard["phonon.Gamma"] == pytest.approx(2.0)


# --------------------------------------------------------------------------- clean-anchor gate
def test_clean_anchor_gate_rejects():
    good = _pt("S:0", 5.0, 0.5, 120.0)
    assert BM._is_clean_anchor(good, "S", 100.0) is True
    # wrong sample / wrong T bin
    assert BM._is_clean_anchor(good, "OTHER", 100.0) is False
    assert BM._is_clean_anchor(good, "S", 150.0) is False
    # missing Γ or intensity
    no_g = dict(good); no_g.pop("gamma")
    assert BM._is_clean_anchor(no_g, "S", 100.0) is False
    no_i = dict(good); no_i.pop("intensity")
    assert BM._is_clean_anchor(no_i, "S", 100.0) is False
    # not separated from the elastic line (ω₀ < 2Γ)
    merged = _pt("S:1", 0.4, 0.5, 100.0)
    assert BM._is_clean_anchor(merged, "S", 100.0) is False
    # contract 4: a constrained fit never seeds a branch (no self-reinforcement)
    constrained = dict(good); constrained["fit_kind"] = "soft_prior"
    assert BM._is_clean_anchor(constrained, "S", 100.0) is False


def test_add_point_stores_fit_kind_only_when_constrained():
    st = CardState()
    SP.add_point(st, scan_id="a", sample="S", T=100, q=[3, 0.1, 0], omega0=5.0, modality="fold",
                 verdict="resolved", fit_kind="unconstrained")
    assert "fit_kind" not in st.dispersion_prior[0]      # default omitted (lean point)
    SP.add_point(st, scan_id="b", sample="S", T=100, q=[3, 0.1, 0], omega0=5.0, modality="fold",
                 verdict="resolved", fit_kind="soft_prior")
    assert st.dispersion_prior[1]["fit_kind"] == "soft_prior"


# --------------------------------------------------------------------------- soft constraints
def test_soft_constraints_scale_amplitude_to_exposure():
    b = BM.build_branch(_anchors(), sample="S", t_bin=100.0)
    p = BM.predict(b, 4.5)
    pc = p.soft_constraints(exposure=2.0e6, height_per_intrinsic=0.8)
    assert "phonon.Gamma" in pc.soft and "phonon.h" in pc.soft
    assert pc.origins == {
        "phonon.Gamma": "prior_predictive",
        "phonon.h": "prior_predictive",
    }
    mu_g, sig_g = pc.soft["phonon.Gamma"]
    assert mu_g == pytest.approx(p.gamma) and sig_g == pytest.approx(p.gamma_sigma)
    mu_a, sig_a = pc.soft["phonon.h"]
    assert mu_a == pytest.approx(p.amplitude_norm * 2.0e6 * 0.8)   # normalized → height at this exposure
    assert sig_a >= BM.A_REL_SIGMA_FLOOR * mu_a - 1e-9            # loose relative σ, h stays soft
    # no exposure → no height constraint (Γ only)
    gamma_only = p.soft_constraints(exposure=0.0, height_per_intrinsic=0.8)
    assert "phonon.h" not in gamma_only.soft
    assert gamma_only.origins == {"phonon.Gamma": "prior_predictive"}


# --------------------------------------------------------------------------- end-to-end via state
def test_branch_for_from_cardstate():
    st = CardState()
    for i, w in enumerate((3.0, 4.0, 5.0, 6.0)):
        SP.add_point(st, scan_id=f"S:{i}", sample="S", T=100.0, q=[3, 0.2, 0], omega0=w,
                     modality="fold", verdict="resolved", gamma=0.3 + 0.05 * w,
                     intensity=_pt("x", w, 0.0, 600.0 / w)["intensity"])
    p = BM.branch_for(st, sample="S", T=100.0, omega0=4.5)
    assert p is not None and p.amplitude_norm == pytest.approx(600.0 / 4.5, rel=1e-3)
    assert BM.branch_for(st, sample="OTHER", T=100.0, omega0=4.5) is None
