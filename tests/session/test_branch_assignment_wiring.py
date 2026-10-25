"""Phase A wiring: the branch-assignment posterior feeds the session's prior, seeding, and near-Γ
constrained recipe. Guards that an optic-tagged point cannot pollute the acoustic predictors."""
from types import SimpleNamespace

import pytest

from isar.schema.card import CardState
from isar.session import branch_model as BM
from isar.session import session_prior as SP
from isar.session import scan_delivery


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


def test_add_point_stores_complete_branch_evidence_additively():
    state = CardState()
    posterior = {"acoustic": 0.2, "optic": 0.7, "new": 0.1}
    assert SP.add_point(
        state, scan_id="O0", sample="S", T=200.0, q=[0.1, 0.0, 0.0], omega0=6.0,
        modality="fold", verdict="resolved", branch_id="optic", weight=0.7,
        branch_posterior=posterior, assignment_entropy=0.8)
    assert state.dispersion_prior[0]["branch_posterior"] == posterior
    assert state.dispersion_prior[0]["assignment_entropy"] == pytest.approx(0.8)


def test_acquisition_provenance_resolves_driven_and_standalone_scans():
    state = CardState(acquisition_provenance=[{
        "job_id": "j-0042", "acquisition_id": "logical:7",
        "acquisition_target_energy": 6.25, "submitted_ncount": 100_000_000,
        "scan_kind": "production"}])
    card = SimpleNamespace(state=state)
    assert CardState.from_dict(state.to_dict()).acquisition_provenance == state.acquisition_provenance
    driven = SimpleNamespace(raw_header={"tavi": {"job_id": "j-0042"}})
    acquisition_id, provenance = scan_delivery._acquisition_provenance_for(driven, card, "S:42")
    assert acquisition_id == "logical:7"
    assert provenance["acquisition_target_energy"] == pytest.approx(6.25)
    standalone = SimpleNamespace(raw_header={})
    assert scan_delivery._acquisition_provenance_for(standalone, card, "S:9") == ("scan:S:9", None)


def test_effective_exposure_uses_preserved_selected_center_after_triage_withholds_fit():
    ctx = SimpleNamespace(
        x=[0.0, 1.0, 2.0, 3.0], mask=[True] * 4, gauss_fwhm=0.1,
        lorentz_fwhm=0.0, acoustic_omega=None, prior_omega=None, window=(0.0, 3.0))
    info = scan_delivery._effective_exposure_for(
        None, ctx, {"omega0": None}, absolute_exposure=[10.0, 20.0, 80.0, 160.0],
        target_center=2.0)
    assert info["center"] == pytest.approx(2.0)
    assert info["value"] == pytest.approx(80.0)


def test_prior_for_can_select_optic_branch():
    st = CardState()
    _add(st, "A0", 0.20, 1.85, branch_id="acoustic")
    _add(st, "O0", 0.10, 6.1, branch_id="optic")
    _add(st, "O1", 0.30, 6.5, branch_id="optic")
    pr = SP.prior_for(st, sample="S", T=200.0, q=[2.2, 0.0, 0.0], period=2.0,
                      branch_id="optic")
    assert pr is not None and pr.omega0 == pytest.approx(6.3, abs=0.5)


def test_triage_preserves_every_fit_as_a_provisional_tracked_candidate():
    """Per-q feature selection is gone: the triage no longer picks which fitted mode is the target,
    nor assigns a branch. Every fit is preserved as unordered evidence under one candidate identity
    that is stable across q, and the authoritative label is assigned later, over the whole path."""
    state = CardState(campaign_target={"branch": "optic", "label": "transverse optic",
                                       "energy_search_meV": [-2.0, 12.0]})
    card = SimpleNamespace(state=state, policy=SimpleNamespace(dispersion={"zone_period": 2.0}))
    ctx = SimpleNamespace(gauss_fwhm=1.0)
    modes = [
        {"omega0": 1.2, "omega0_err": 0.1, "gamma": 0.2, "amplitude": 900.0},
        {"omega0": 6.0, "omega0_err": 0.1, "gamma": 0.3, "amplitude": 300.0},
    ]
    optic = scan_delivery._assign_and_triage(
        {"omega0": 6.0, "modes": modes}, None, ctx, card=card, sample="S", T=200.0,
        q=[2.1, 0.0, 0.0], t_round=5.0, near_gamma=False)
    acoustic = scan_delivery._assign_and_triage(
        {"omega0": 1.2, "modes": modes}, None, ctx, card=card, sample="S", T=200.0,
        q=[2.1, 0.0, 0.0], t_round=5.0, near_gamma=False)
    assert optic["deliver"] is True and acoustic["deliver"] is True
    assert optic["branch_id"] is None and acoustic["branch_id"] is None
    assert optic["candidate_state"] == "provisional"
    assert optic["selected_energy"] == 6.0 and acoustic["selected_energy"] == 1.2
    neighbor = scan_delivery._assign_and_triage(
        {"omega0": 1.2, "modes": modes}, None, ctx, card=card, sample="S", T=200.0,
        q=[2.2, 0.0, 0.0], t_round=5.0, near_gamma=False)
    assert neighbor["candidate_id"] == acoustic["candidate_id"] == optic["candidate_id"]


def test_triage_is_off_without_a_zone_period():
    """The one remaining gate: no configured fold period ⇒ no branch-assignment posterior at all."""
    card = SimpleNamespace(state=CardState(), policy=SimpleNamespace(dispersion={}))
    assert scan_delivery._assign_and_triage(
        {"omega0": 6.0, "modes": []}, None, SimpleNamespace(gauss_fwhm=1.0), card=card,
        sample="S", T=200.0, q=[2.1, 0.0, 0.0], t_round=5.0, near_gamma=False) is None


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
    """The near-Γ constrained recipe hard-fixes BOTH Γ and the height (the branch 1/ω law at the
    predicted ω₀), reducing the fit to ~ω₀-only; ω₀ is then pinned by position only (T-1)."""
    pts = [_anchor(f"A:{i}", w, branch_id="acoustic") for i, w in enumerate((3.0, 4.0, 5.0, 6.0))]
    b = BM.build_branch(pts, sample="S", t_bin=200.0)
    pred = BM.predict(b, 4.5)
    cons = pred.hard_constraints(exposure=1e6, height_per_intrinsic=0.8)
    assert "phonon.Gamma" in cons.hard and "phonon.h" in cons.hard
    assert not cons.soft                                  # nothing soft — the constrained recipe is hard
    assert cons.origins == {
        "phonon.Gamma": "prior_predictive",
        "phonon.h": "prior_predictive",
    }
    assert cons.hard["phonon.h"] == pytest.approx(pred.amplitude_norm * 1e6 * 0.8, rel=1e-6)
    # the ordinary soft rescue keeps the height soft (never hard), for contrast
    soft = pred.soft_constraints(exposure=1e6, height_per_intrinsic=0.8)
    assert "phonon.h" in soft.soft and not soft.hard
    assert soft.origins == {
        "phonon.Gamma": "prior_predictive",
        "phonon.h": "prior_predictive",
    }


# ------------------------------------------- identity vs branch assignment (SCENE-1 PR-c cmt E)
#
# Two independent claims about what was measured: the winning candidate's catalogue identity and
# the branch-assignment posterior. BA keeps driving delivery until SCENE-6, so the disagreement is
# RECORDED and nothing else — the point is to measure how often the axes differ before either is
# ever allowed to override the other.
def _fit(identity, **overrides):
    fit = {"verdict": "resolved", "omega0": 5.0, "evidence_identity": identity,
           "identity_ba_disagreement": None, "reasons": []}
    fit.update(overrides)
    return fit


def test_identity_ba_disagreement_is_recorded_when_the_axes_differ():
    fit = _fit("a")
    assert scan_delivery._record_identity_ba_disagreement(fit, "optic") == {
        "evidence_identity": "a", "branch_id": "optic"}
    assert fit["identity_ba_disagreement"] == {"evidence_identity": "a", "branch_id": "optic"}


@pytest.mark.parametrize("identity,branch_id", [
    ("acoustic", "acoustic"),     # the axes agree
    (None, "optic"),              # identity-agnostic candidate — today's every scan
    ("a", None),                  # no branch assignment (posterior off / non-ω₀ result)
    (None, None),
])
def test_identity_ba_disagreement_is_none_when_either_side_is_silent_or_they_agree(
        identity, branch_id):
    fit = _fit(identity)
    assert scan_delivery._record_identity_ba_disagreement(fit, branch_id) is None
    assert fit["identity_ba_disagreement"] is None


def test_identity_ba_disagreement_records_only(monkeypatch):
    """The diagnostic is written where branch triage runs, and changes nothing else about the fit."""
    monkeypatch.setattr(scan_delivery, "_assign_and_triage",
                        lambda *a, **k: {"deliver": True, "branch_id": "optic", "weight": 0.8,
                                         "entropy": 0.1, "resolvable": True})
    fit = _fit("a", confidence=0.7, interval=(4.0, 6.0), interval_reliable=True)
    before = {k: v for k, v in fit.items() if k != "identity_ba_disagreement"}

    branch_id, *_ = scan_delivery._apply_branch_triage(
        fit, None, None, card=None, sample="S", T=200.0, q=[0.1, 0.0, 0.0],
        t_round=200.0, near_gamma=False, master=False)

    assert branch_id == "optic"
    assert fit["identity_ba_disagreement"] == {"evidence_identity": "a", "branch_id": "optic"}
    # verdict, confidence, ω₀ and its interval are untouched by the disagreement
    assert {k: fit[k] for k in before} == before
