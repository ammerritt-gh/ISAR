"""Bayesian branch-assignment posterior (steering Phase A) — the cross-q identity that replaces the
hard gates. Physics enters as priors: the zone anchor rejects the optic near Γ, real anchors outvote
the prior, a bimodal scan abstains."""
import numpy as np
import pytest

from isar.session import branch_assignment as BA

FWHM = 1.366   # the deterministic-tier Cooper-Nathans resolution


def _pt(scan_id, q, omega0, *, branch_id="acoustic", weight=1.0, sample="S", T=200.0):
    return {"scan_id": scan_id, "sample": sample, "T": T, "t_bin": T, "q": [q, 0.0, 0.0],
            "omega0": omega0, "branch_id": branch_id, "weight": weight}


# ---------------------------------------------------------------- reduced-q fold
def test_reduced_q_folds_into_zone():
    assert BA.reduced_q([2.05, 0, 0], 2.0) == pytest.approx(0.05, abs=1e-9)
    assert BA.reduced_q([3.0, 0, 0], 2.0) == pytest.approx(1.0, abs=1e-9)
    assert BA.reduced_q([2.0, 0, 0], 2.0) == pytest.approx(0.0, abs=1e-9)
    # a different period changes the fold (a declared per-sample knob, not a hardcode)
    assert BA.reduced_q([2.05, 0, 0], 1.0) == pytest.approx(0.05, abs=1e-9)
    assert BA.reduced_q([2.5, 0, 0], 1.0) == pytest.approx(0.5, abs=1e-9)


# ---------------------------------------------------------------- zone anchor cold start
def test_cold_start_acoustic_rejects_optic_near_gamma():
    """With no real anchors, the acoustic zone anchor alone must reject a 6 meV feature at H=2.05 and
    accept the near-zero blob — the P-Z1 core, from the very first scan."""
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.05, 0, 0], 2.0)
    feats = [BA.Feature(0.4, 0.5, 260.0, np.inf, "mode"),
             BA.Feature(6.5, 0.5, 20.0, np.inf, "mode")]
    post = BA.assignment_posterior(feats, beliefs, u)
    assert post[0][BA.ACOUSTIC] > 0.9          # near-zero blob → acoustic
    assert post[1][BA.ACOUSTIC] < 0.1          # 6.5 meV → not acoustic
    assert post[1][BA.NEW] > 0.8               # → new/spurious (seeds a would-be optic, not acoustic)


def test_deliverable_guard_blocks_far_extrapolation():
    """An acoustic ω₀ is deliverable within the anchored range (+margin), so it bootstraps outward,
    but NOT far past the last anchor — where the linear belief would overshoot into the optic."""
    b = BA.build_beliefs([_pt("S:0", 0.40, 3.6), _pt("S:1", 0.55, 4.8)],
                         sample="S", t_bin=200.0, period=2.0)[BA.ACOUSTIC]
    assert b.deliverable(0.60)          # just past the last anchor (0.55) — bootstraps outward
    assert not b.deliverable(0.95)      # zone boundary, far beyond anchors — withhold (would be optic)
    # cold start: only the near-Γ region is deliverable
    cold = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)[BA.ACOUSTIC]
    assert cold.deliverable(0.15) and not cold.deliverable(0.6)


def test_zone_plus_one_anchor_pins_dispersion_line():
    """One real acoustic anchor + the zone anchor already give the right low-q ω₀ line."""
    b = BA.build_beliefs([_pt("S:0", 0.30, 2.8)], sample="S", t_bin=200.0, period=2.0)[BA.ACOUSTIC]
    mu, sigma = b.predict(0.05)
    assert mu == pytest.approx(2.8 * 0.05 / 0.30, rel=0.05)   # line through (0,0),(0.3,2.8)
    assert sigma <= 0.5


# ---------------------------------------------------------------- data outvotes the prior
def test_real_anchor_outvotes_zone_prior():
    """A dispersing acoustic branch (anchors rising with q) must make a mid-energy feature read as
    acoustic where the bare zone prior would have kept ω₀ near 0."""
    pts = [_pt("S:0", 0.20, 1.9), _pt("S:1", 0.30, 2.8), _pt("S:2", 0.40, 3.6)]
    beliefs = BA.build_beliefs(pts, sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.45, 0, 0], 2.0)        # 0.45 → truth acoustic ≈ 4.1
    post = BA.assignment_posterior([BA.Feature(4.1, 0.4, 100.0, np.inf, "mode")], beliefs, u)
    assert post[0][BA.ACOUSTIC] > 0.8


# ---------------------------------------------------------------- QoI + triage
def test_qoi_delivers_resolvable_dominant_feature():
    beliefs = BA.build_beliefs([_pt("S:0", 0.20, 1.9), _pt("S:1", 0.30, 2.8)],
                               sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.35, 0, 0], 2.0)
    feats = [BA.Feature(3.2, 0.3, 100.0, np.inf, "mode"), BA.Feature(6.9, 0.4, 18.0, np.inf, "mode")]
    post = BA.assignment_posterior(feats, beliefs, u)
    qoi = BA.acoustic_qoi(feats, post, gauss_fwhm=FWHM)
    assert qoi["omega0"] == pytest.approx(3.2, abs=1e-6)   # MAP feature, not a blend
    assert qoi["resolvable"] and qoi["dominance"] > 0.7
    assert BA.deliver_acoustic(qoi)


def test_qoi_abstains_when_unresolvable_near_gamma():
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.05, 0, 0], 2.0)
    feats = [BA.Feature(0.4, 0.5, 260.0, np.inf, "mode")]
    qoi = BA.acoustic_qoi(feats, BA.assignment_posterior(feats, beliefs, u), gauss_fwhm=FWHM)
    assert qoi["p_acoustic"] > 0.9 and not qoi["resolvable"]   # confident acoustic, but unresolvable
    assert not BA.deliver_acoustic(qoi)                        # → constrained fit / branch_continuation


def test_qoi_abstains_on_bimodal_scan():
    """No branch established: two features both plausibly acoustic → low dominance → do not deliver a
    blend (the failure mode the dominance gate exists to catch)."""
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.5, 0, 0], 2.0)         # mid-zone, broad cold-start belief
    feats = [BA.Feature(4.3, 0.4, 80.0, np.inf, "mode"), BA.Feature(7.0, 0.4, 70.0, np.inf, "mode")]
    qoi = BA.acoustic_qoi(feats, BA.assignment_posterior(feats, beliefs, u), gauss_fwhm=FWHM)
    assert not BA.deliver_acoustic(qoi)


# ---------------------------------------------------------------- optic belief separates branches
def test_optic_belief_claims_the_flat_high_feature():
    """Once an optic branch exists (flat ≈6), a 6 meV feature at a new q reads optic, not acoustic —
    the discrimination that stops the 6 meV from ever seeding acoustic."""
    pts = [_pt("A:0", 0.20, 1.9), _pt("A:1", 0.30, 2.8),
           _pt("O:0", 0.05, 6.05, branch_id="optic"), _pt("O:1", 0.20, 6.2, branch_id="optic")]
    beliefs = BA.build_beliefs(pts, sample="S", t_bin=200.0, period=2.0)
    assert "optic" in beliefs
    u = BA.reduced_q([2.10, 0, 0], 2.0)
    post = BA.assignment_posterior([BA.Feature(6.1, 0.4, 20.0, np.inf, "mode")], beliefs, u)
    assert post[0]["optic"] > post[0][BA.ACOUSTIC]


# ---------------------------------------------------------------- feature extraction / merge
def test_features_from_merges_mode_and_descriptor():
    class _P:
        def __init__(self, e, w, i, s):
            self.energy, self.width, self.integrated_intensity, self.significance, self.p_value = e, w, i, s, 1e-4

    class _D:
        peaks = (_P(3.0, 1.4, 100.0, 8.0), _P(6.5, 1.4, 20.0, 4.0))

    modes = [{"omega0": 3.02, "omega0_err": 0.1, "gamma": 0.3, "amplitude": 100.0}]
    feats = BA.features_from(modes, _D(), gauss_fwhm=FWHM)
    # the 3.0 descriptor peak is merged into the 3.02 fit mode; the 6.5 optic survives as its own
    assert [round(f.energy, 2) for f in feats] == [3.02, 6.5]
    assert feats[0].source == "mode" and feats[1].source == "descriptor"
