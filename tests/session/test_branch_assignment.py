"""Bayesian branch-assignment posterior (steering Phase A) — the cross-q identity that replaces the
hard gates. Physics enters as priors: the zone anchor rejects the optic near Γ, real anchors outvote
the prior, a bimodal scan abstains."""
import numpy as np
import pytest

from isar.session import branch_assignment as BA
from isar.schema.hypotheses import preset

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


def test_parameter_priors_create_beliefs_without_creating_occupancy():
    hypotheses = [preset("acoustic").to_dict(), preset("optic").to_dict()]
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0,
                               hypotheses=hypotheses)
    assert beliefs["acoustic"].n_anchors == 0
    assert beliefs["optic"].n_anchors == 0
    optic_mu, _ = beliefs["optic"].predict(0.0)
    assert 6.0 < optic_mu < 7.0  # non-negative truncation shifts the 6 meV mode upward
    assert beliefs["optic"].predict_data_only(0.0) == (0.0, 1000.0)


def test_real_data_outvotes_wrong_optic_prior_and_data_only_never_sees_it():
    hypotheses = [preset("optic").to_dict()]
    pts = [_pt(f"O:{i}", q, energy, branch_id="optic")
           for i, (q, energy) in enumerate(((0.0, 3.0), (0.1, 3.1), (0.2, 3.2), (0.3, 3.3)))]
    belief = BA.build_beliefs(pts, sample="S", t_bin=200.0, period=2.0,
                              hypotheses=hypotheses)["optic"]
    combined, _ = belief.predict(0.0)
    data_only, _ = belief.predict_data_only(0.0)
    assert data_only == pytest.approx(3.0, abs=0.1)
    assert combined < 3.5


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


# ---------------------------------------------------------------- anti-Stokes fold
class _Peak:
    def __init__(self, e, w, i, s):
        self.energy, self.width, self.integrated_intensity, self.significance, self.p_value = e, w, i, s, 1e-4


class _Desc:
    def __init__(self, *peaks):
        self.peaks = peaks


def test_anti_stokes_descriptor_folds_into_mode():
    """A descriptor peak at -E within tol of a fit mode at +E is the same phonon's anti-Stokes wing —
    it must not enter as a rival feature (the v11 dead campaign: the mirror split the acoustic weight
    and the dominance triage abstained on every scan)."""
    modes = [{"omega0": 4.55, "omega0_err": 0.1, "gamma": 0.3, "amplitude": 5.0}]
    feats = BA.features_from(modes, _Desc(_Peak(-4.72, 1.4, 80.0, 15.0)), gauss_fwhm=FWHM)
    assert [round(f.energy, 2) for f in feats] == [4.55]
    assert feats[0].source == "mode"


def test_anti_stokes_descriptor_merges_with_stokes_descriptor():
    """Two descriptor wings of one phonon pool their evidence: summed intensity, max significance."""
    feats = BA.features_from([], _Desc(_Peak(2.2, 1.4, 100.0, 8.0), _Peak(-2.14, 1.4, 200.0, 12.0)),
                             gauss_fwhm=FWHM)
    assert len(feats) == 1
    assert feats[0].energy == pytest.approx(2.2)
    assert feats[0].intensity == pytest.approx(300.0)
    assert feats[0].significance == pytest.approx(12.0)


def test_lone_anti_stokes_descriptor_folds_to_positive():
    """An anti-Stokes-only detection (Stokes wing below threshold) is still the phonon at |E|."""
    feats = BA.features_from([], _Desc(_Peak(-3.1, 1.4, 40.0, 9.0)), gauss_fwhm=FWHM)
    assert len(feats) == 1 and feats[0].energy == pytest.approx(3.1)


def test_negative_mode_omega0_folds_to_positive():
    feats = BA.features_from([{"omega0": -3.5, "omega0_err": 0.1, "gamma": 0.3, "amplitude": 1.0}],
                             None, gauss_fwhm=FWHM)
    assert len(feats) == 1 and feats[0].energy == pytest.approx(3.5)


def test_cold_start_delivers_with_visible_anti_stokes_wing():
    """The v11 regression, end to end: cold beliefs, a mid-q Stokes fit mode with its bright
    anti-Stokes mirror and a weak optic descriptor peak must still DELIVER the acoustic — the mirror
    is folded, so dominance is acoustic-vs-optic, not acoustic-vs-its-own-reflection."""
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.35, 0, 0], 2.0)
    modes = [{"omega0": 3.17, "omega0_err": 0.1, "gamma": 0.3, "amplitude": 5.0}]
    desc = _Desc(_Peak(-3.07, 1.4, 120.0, 22.0), _Peak(6.8, 1.4, 5.0, 6.0))
    feats = BA.features_from(modes, desc, gauss_fwhm=FWHM)
    qoi = BA.acoustic_qoi(feats, BA.assignment_posterior(feats, beliefs, u), gauss_fwhm=FWHM)
    assert qoi["omega0"] == pytest.approx(3.17)
    assert BA.deliver_acoustic(qoi)


def _qoi_at(feats, beliefs, u):
    post = BA.assignment_posterior(feats, beliefs, u)
    return post, BA.acoustic_qoi(feats, post, gauss_fwhm=FWHM)


# ---------------------------------------------------------------- rival-branch (optic) banking
def test_secondary_branch_points_banks_far_optic():
    """A delivered low-q scan's clearly-non-acoustic high mode is offered as an optic anchor."""
    beliefs = BA.build_beliefs([_pt("S:0", 0.20, 1.85), _pt("S:1", 0.25, 2.30), _pt("S:2", 0.30, 2.68)],
                               sample="S", t_bin=200.0, period=2.0)
    feats = [BA.Feature(3.15, 0.2, 5.0, np.inf, "mode"), BA.Feature(6.95, 0.2, 0.1, np.inf, "mode")]
    post, qoi = _qoi_at(feats, beliefs, 0.35)
    assert BA.deliver_acoustic(qoi)
    sec = BA.secondary_branch_points(feats, post, qoi, gauss_fwhm=FWHM)
    assert len(sec) == 1
    assert sec[0]["branch_id"] == "optic" and sec[0]["omega0"] == pytest.approx(6.95)
    assert sec[0]["weight"] > 0.7


def test_secondary_branch_points_guards():
    """Never banked: the QoI itself, an ambiguous feature near the acoustic line, a weak descriptor
    bump, or a feature below the acoustic (phonon-first: a low shoulder is not an optic)."""
    beliefs = BA.build_beliefs([_pt("S:0", 0.20, 1.85), _pt("S:1", 0.30, 2.68)],
                               sample="S", t_bin=200.0, period=2.0)
    feats = [BA.Feature(2.30, 0.2, 5.0, np.inf, "mode"),      # QoI (acoustic)
             BA.Feature(2.75, 0.3, 1.0, np.inf, "mode"),      # ambiguous, near the line
             BA.Feature(6.90, 0.4, 1.0, 5.0, "descriptor"),   # weak descriptor bump (sig < 8)
             BA.Feature(0.90, 0.3, 1.0, 20.0, "descriptor")]  # below the acoustic
    post, qoi = _qoi_at(feats, beliefs, 0.25)
    sec = BA.secondary_branch_points(feats, post, qoi, gauss_fwhm=FWHM)
    assert sec == []


def test_optic_belief_recovers_mid_band_delivery():
    """The v12 abstention deadlock: WITHOUT an optic belief the extrapolating acoustic claims the
    6-7 meV feature too (occupancy beats the flat escape) → dominance ~0.5 → abstain. WITH banked
    optic anchors the optic claims its kin and the acoustic delivers."""
    acoustic = [_pt("S:0", 0.20, 1.85), _pt("S:1", 0.25, 2.30), _pt("S:2", 0.30, 2.68),
                _pt("S:3", 0.35, 3.13), _pt("S:4", 0.40, 3.53)]
    optic = [_pt("S:0#optic0", 0.20, 6.9, branch_id="optic", weight=0.95),
             _pt("S:2#optic0", 0.30, 6.8, branch_id="optic", weight=0.95)]
    feats = [BA.Feature(4.23, 0.2, 5.0, np.inf, "mode"), BA.Feature(6.35, 0.3, 1.0, 20.0, "descriptor")]
    u = 0.5
    no_optic = BA.build_beliefs(acoustic, sample="S", t_bin=200.0, period=2.0)
    _, qoi0 = _qoi_at(feats, no_optic, u)
    assert not BA.deliver_acoustic(qoi0)          # the deadlock this feature exists to break
    with_optic = BA.build_beliefs(acoustic + optic, sample="S", t_bin=200.0, period=2.0)
    _, qoi1 = _qoi_at(feats, with_optic, u)
    assert qoi1["omega0"] == pytest.approx(4.23)
    assert BA.deliver_acoustic(qoi1)


def test_two_anchor_optic_belief_extrapolates_flat():
    """The v14 regression: two close, noisy optic anchors (6.66 @ u=0.25, 6.31 @ u=0.30) must not
    assert a −7 meV/rlu line that plunges into the acoustic band at mid-q and claims its features."""
    optic = [_pt("S:0#optic0", 0.25, 6.66, branch_id="optic", weight=0.97),
             _pt("S:1#optic0", 0.30, 6.31, branch_id="optic", weight=0.77)]
    acoustic = [_pt("S:0", 0.20, 1.85), _pt("S:1", 0.25, 2.30), _pt("S:2", 0.30, 2.68),
                _pt("S:3", 0.35, 3.13), _pt("S:4", 0.40, 3.53)]
    beliefs = BA.build_beliefs(acoustic + optic, sample="S", t_bin=200.0, period=2.0)
    mu, sigma = beliefs["optic"].predict(0.5)
    assert mu > 6.0                        # stays on the optic level, not 4.9
    # and the mid-band acoustic still delivers against it
    feats = [BA.Feature(4.23, 0.2, 5.0, np.inf, "mode"), BA.Feature(6.35, 0.3, 1.0, 20.0, "descriptor")]
    _, qoi = _qoi_at(feats, beliefs, 0.5)
    assert qoi["omega0"] == pytest.approx(4.23)
    assert BA.deliver_acoustic(qoi)


def test_fold_does_not_relax_the_zone_anchor():
    """Control: near Γ the folded 6.8 meV optic is still rejected — folding fixes the mirror split
    without reopening the P-Z1 latch."""
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.05, 0, 0], 2.0)
    feats = BA.features_from([], _Desc(_Peak(-6.8, 1.4, 20.0, 8.0), _Peak(6.8, 1.4, 22.0, 8.5)),
                             gauss_fwhm=FWHM)
    assert len(feats) == 1                      # the ±6.8 pair is one folded feature
    post = BA.assignment_posterior(feats, beliefs, u)
    assert post[0][BA.ACOUSTIC] < 0.1           # and it still cannot read acoustic near Γ
    qoi = BA.acoustic_qoi(feats, post, gauss_fwhm=FWHM)
    assert not BA.deliver_acoustic(qoi)


def test_target_selector_chooses_candidate_without_changing_its_posterior():
    feats = [BA.Feature(1.0, 0.1, 10.0, np.inf, "mode"),
             BA.Feature(6.0, 0.1, 8.0, np.inf, "mode"),
             BA.Feature(9.0, 0.1, 5.0, np.inf, "mode")]
    posterior = [
        {"acoustic": 0.94, "optic": 0.01, "new": 0.05},
        {"acoustic": 0.05, "optic": 0.9, "new": 0.05},
        {"acoustic": 0.1, "optic": 0.2, "new": 0.7},
    ]
    low = BA.selected_branch_qoi(feats, posterior, "optic", gauss_fwhm=0.5,
                                 selector="lowest_energy")
    near = BA.selected_branch_qoi(feats, posterior, "optic", gauss_fwhm=0.5,
                                  selector="nearest_energy", target_energy=6.4)
    high = BA.selected_branch_qoi(feats, posterior, "optic", gauss_fwhm=0.5,
                                  selector="highest_energy")
    assert low["omega0"] == 1.0 and low["posterior"] == posterior[0]
    assert near["omega0"] == 6.0 and near["posterior"] == posterior[1]
    assert high["omega0"] == 9.0 and high["posterior"] == posterior[2]
    assert not BA.deliver_branch(low)
    assert BA.deliver_branch(near)


def test_scientist_target_energy_conditions_branch_odds_without_removing_rivals():
    features = [BA.Feature(1.0, 0.1, 10.0, 10.0, "mode"),
                BA.Feature(6.0, 0.1, 10.0, 10.0, "mode")]
    posterior = [
        {"acoustic": 0.7, "optic": 0.2, "new": 0.1},
        {"acoustic": 0.2, "optic": 0.5, "new": 0.3},
    ]
    result = BA.condition_on_target_energy(
        features, posterior, "optic", target_energy=6.0, sigma=1.0)
    assert result[0]["optic"] < posterior[0]["optic"]
    assert result[1]["optic"] > posterior[1]["optic"]
    assert set(result[1]) == {"acoustic", "optic", "new"}
    assert sum(result[1].values()) == pytest.approx(1.0)
