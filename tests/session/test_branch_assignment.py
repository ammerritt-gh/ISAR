"""Branch beliefs (steering Phase A) — the cross-q dispersion belief that replaces the hard gates.
Physics enters as priors: the zone anchor pins ω₀→0 at Γ, real anchors outvote it, and an optic
belief extrapolates flat rather than plunging into the acoustic band."""
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
def test_zone_plus_one_anchor_pins_dispersion_line():
    """One real acoustic anchor + the zone anchor already give the right low-q ω₀ line."""
    b = BA.build_beliefs([_pt("S:0", 0.30, 2.8)], sample="S", t_bin=200.0, period=2.0)[BA.ACOUSTIC]
    mu, sigma = b.predict(0.05)
    assert mu == pytest.approx(2.8 * 0.05 / 0.30, rel=0.05)   # line through (0,0),(0.3,2.8)
    assert sigma <= 0.5


def test_branch_conditional_point_cannot_seed_beliefs():
    """Contract 4 at the ω₀ belief: a constrained (branch_conditional) delivery is retained in the
    prior but must not seed the assignment belief — otherwise a conditioned fit feeds the posterior
    that justifies conditioning the next one (self-reinforcement)."""
    conditioned = _pt("S:0", 0.30, 2.8)
    conditioned["fit_kind"] = "branch_conditional"
    beliefs = BA.build_beliefs([conditioned], sample="S", t_bin=200.0, period=2.0)
    assert beliefs[BA.ACOUSTIC].n_anchors == 0
    # the belief is exactly the cold-start belief — the point contributed nothing
    cold = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    assert beliefs[BA.ACOUSTIC].predict(0.15) == cold[BA.ACOUSTIC].predict(0.15)


# ---------------------------------------------------------------- data outvotes the prior
def test_real_anchor_outvotes_zone_prior():
    """A dispersing acoustic branch (anchors rising with q) must make a mid-energy feature read as
    acoustic where the bare zone prior would have kept ω₀ near 0."""
    pts = [_pt("S:0", 0.20, 1.9), _pt("S:1", 0.30, 2.8), _pt("S:2", 0.40, 3.6)]
    beliefs = BA.build_beliefs(pts, sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.45, 0, 0], 2.0)        # 0.45 → truth acoustic ≈ 4.1
    mu, sigma = beliefs[BA.ACOUSTIC].predict(u)
    assert abs(mu - 4.1) < sigma               # the anchored line, not the bare zone prior at ω₀≈0
    assert mu > 3.0


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


# ---------------------------------------------------------------- optic belief separates branches
def test_optic_belief_claims_the_flat_high_feature():
    """Once an optic branch exists (flat ≈6), a 6 meV feature at a new q reads optic, not acoustic —
    the discrimination that stops the 6 meV from ever seeding acoustic."""
    pts = [_pt("A:0", 0.20, 1.9), _pt("A:1", 0.30, 2.8),
           _pt("O:0", 0.05, 6.05, branch_id="optic"), _pt("O:1", 0.20, 6.2, branch_id="optic")]
    beliefs = BA.build_beliefs(pts, sample="S", t_bin=200.0, period=2.0)
    assert "optic" in beliefs
    u = BA.reduced_q([2.10, 0, 0], 2.0)
    optic_mu, optic_sigma = beliefs["optic"].predict(u)
    acoustic_mu, acoustic_sigma = beliefs[BA.ACOUSTIC].predict(u)
    # a 6.1 meV feature at this q sits on the optic line and far off the acoustic one
    assert abs(optic_mu - 6.1) < optic_sigma
    assert abs(acoustic_mu - 6.1) > acoustic_sigma


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


def test_stokes_mode_and_its_anti_stokes_mirror_are_one_feature():
    """The v11 regression: a mid-q Stokes fit mode with its bright anti-Stokes mirror and a weak
    optic descriptor peak must yield TWO features, not three — the mirror folds into its own mode
    instead of standing as a rival to it."""
    modes = [{"omega0": 3.17, "omega0_err": 0.1, "gamma": 0.3, "amplitude": 5.0}]
    desc = _Desc(_Peak(-3.07, 1.4, 120.0, 22.0), _Peak(6.8, 1.4, 5.0, 6.0))
    feats = BA.features_from(modes, desc, gauss_fwhm=FWHM)
    assert [round(f.energy, 2) for f in feats] == [3.17, 6.8]


# ---------------------------------------------------------------- optic belief extrapolates flat
def test_two_anchor_optic_belief_extrapolates_flat():
    """The v14 regression: two close, noisy optic anchors (6.66 @ u=0.25, 6.31 @ u=0.30) must not
    assert a −7 meV/rlu line that plunges into the acoustic band at mid-q."""
    optic = [_pt("S:0#optic0", 0.25, 6.66, branch_id="optic", weight=0.97),
             _pt("S:1#optic0", 0.30, 6.31, branch_id="optic", weight=0.77)]
    acoustic = [_pt("S:0", 0.20, 1.85), _pt("S:1", 0.25, 2.30), _pt("S:2", 0.30, 2.68),
                _pt("S:3", 0.35, 3.13), _pt("S:4", 0.40, 3.53)]
    beliefs = BA.build_beliefs(acoustic + optic, sample="S", t_bin=200.0, period=2.0)
    mu, sigma = beliefs["optic"].predict(0.5)
    assert mu > 6.0                        # stays on the optic level, not 4.9
    # and the acoustic line at that q is nowhere near it
    assert beliefs[BA.ACOUSTIC].predict(0.5)[0] < 5.0


def test_fold_pairs_a_mirrored_descriptor_detection():
    """Control: the ±6.8 descriptor pair is one folded feature, and near Γ the cold acoustic belief
    is nowhere near it — folding fixes the mirror split without reopening the P-Z1 latch."""
    beliefs = BA.build_beliefs([], sample="S", t_bin=200.0, period=2.0)
    u = BA.reduced_q([2.05, 0, 0], 2.0)
    feats = BA.features_from([], _Desc(_Peak(-6.8, 1.4, 20.0, 8.0), _Peak(6.8, 1.4, 22.0, 8.5)),
                             gauss_fwhm=FWHM)
    assert len(feats) == 1                      # the ±6.8 pair is one folded feature
    mu, sigma = beliefs[BA.ACOUSTIC].predict(u)
    assert feats[0].energy - mu > sigma         # and it still cannot read acoustic near Γ


@pytest.mark.parametrize("mu,sigma", [(-20.0, 1.0), (-100.0, 2.0)])
def test_lower_truncated_prior_is_concentrated_above_zero_in_far_tail(mu, sigma):
    mean, std = BA.lower_truncated_normal_moments(mu, sigma)
    alpha = -mu / sigma
    assert 0.0 < mean < 2.0 * sigma / alpha
    assert 0.0 < std < 2.0 * sigma / alpha
