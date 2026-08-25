"""SCENE-5 5.4 fast-lane world: the FCC boundary, belief- and scoring-level (no fits).

Not-slow lane per the plan's lane resolution: everything here is hand-rolled payloads through
``isar.scene.eig`` (build_beliefs / score), never a fit. Two claims, mirroring the 5.0 phenomena
tests' idiom one level up (through real MI scoring, not just ``predicted_omega``):

1. A curve belief anchored on the FCC test sample's declared geometry (``zone_period=1.0``,
   ``reflections=(2.0, 4.0)`` — 3.0 deliberately absent, the extinct fold) never predicts a
   return-to-zero at the (3,0,0)-analog boundary: it keeps rising past the last real anchor,
   exactly as it does at every other undeclared fold.
2. A "descent probe" — a narrow near-zero-energy window — is genuinely informative about
   presence only where the belief expects a real descent (the declared 2.0 reflection); the same
   probe at the extinct 3.0 fold sees the predicted peak sitting far outside its window on BOTH
   alternatives, so its ConfirmPresence MI collapses to ~0. This is the EIG placement authority
   naturally preferring 2.0 for a descent question, asserted through ``score()`` itself rather
   than by re-deriving the physics.
"""
from __future__ import annotations

import math

from isar.scene.eig import Design, build_beliefs, design_key, score
from isar.scene.model import CandidateRow, ComponentDescriptor, Footprint, ModeInterval, \
    SampleKnowledge, SceneObservation, fingerprint
from isar.scene.questions import PredicateDeclaration, QuestionAssessment, QuestionSpec, \
    QuestionSubject
from isar.schema.question_policy import resolve_policy

PARTITION = "p1"

# The FCC test sample's declared geometry (plan §5.4): zone period 1 rlu, only H=2 and H=4
# declared as real reflections — H=3 is the extinct fold the acoustic branch's boundary maximum
# sits on. Matches ``tests/drive/_closed_loop.py``'s ``fcc_sample_knowledge`` declaration.
FCC_SAMPLE = SampleKnowledge(zone_period=1.0, reflections=(2.0, 4.0))

# Gapless (acoustic) declared hypothesis — the only kind that ever plants a zero-energy anchor
# (isar.scene.phenomena.build_beliefs), and only at a fold that coincides with a declaration.
ACOUSTIC_HYP = {"acoustic": {"preset": "acoustic", "zc_energy": {"mode": 0.0, "sigma": 0.3},
                             "confidence": 0.8}}


def _mode_row(label: str, omega: float, sigma: float = 0.3, identity: str | None = None
             ) -> CandidateRow:
    interval = ModeInterval(
        low=omega - sigma, center=omega, high=omega + sigma, nominal_level=0.68,
        interval_kind="hesse_delta", inference_paradigm="frequentist_asymptotic",
        source_quantity="freq_interval", authority={"authority_basis": "producer_sound"},
        conditioning_fingerprint=None)
    return CandidateRow(
        label=label, k=1, n_modes=1, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=identity, variant=None,
        omegas=(omega,), mode_intervals=(interval,),
        components=(ComponentDescriptor(kind="mode", slot=0),))


def _empty_row(label: str) -> CandidateRow:
    return CandidateRow(
        label=label, k=0, n_modes=0, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=None, variant=None,
        omegas=(), mode_intervals=(), components=())


def _observation(scan_ref: str, rows: tuple[CandidateRow, ...]) -> SceneObservation:
    return SceneObservation(
        scan_ref=scan_ref, q=(0.0, 0.0, 0.0), temperature_K=None,
        energy_window=(-30.0, 30.0), energy_bands=((-30.0, 30.0),),
        gauss_fwhm=0.4, lorentz_fwhm=None, effective_exposure=1.0, background=0.0,
        best_label=rows[0].label, candidates=rows, evidence_comparison_status="complete",
        selection_fallback=None, arena_fingerprint=None, evidence_sink=None, sample="fcc-test",
        conditions=(), count_trust="trusted", lattice_epoch=None, fingerprint="test")


def _footprint(scan_ref: str, axis_coordinate: float) -> Footprint:
    return Footprint(scan_ref=scan_ref, q=(axis_coordinate, 0.0, 0.0), bands=(),
                     temperature_K=None, background=0.0, gauss_fwhm=0.4,
                     axis_coordinate=axis_coordinate, partition_id=PARTITION)


def _alt(assignment, posterior: float) -> dict:
    return {"assignment": [list(p) for p in assignment], "links": [], "base": 0.0,
           "factor_terms": {}, "prior_terms": {}, "reasons": {}, "total": 0.0,
           "posterior": posterior}


def _ascending_track_scene(anchor_h: float = 2.6, anchor_omega: float = 4.0) -> dict:
    """One arena at ``anchor_h`` on the FCC path, present-vs-absent alternatives: 'present'
    carries the mode (a single-anchor curve belief that, under the declared gapless hypothesis
    and FCC geometry, plants a zero anchor at H=2.0 and extrapolates rising past H=2.6 — never
    a zero anchor at the extinct H=3.0 fold), 'absent' carries nothing.
    """
    obs = _observation("s1", (_mode_row("m", anchor_omega, identity="acoustic"),
                              _empty_row("e")))
    fp = _footprint("s1", anchor_h)
    cluster = {"cluster_id": "c1", "scan_refs": ["s1"], "alternatives": [
        _alt([("s1", "m")], posterior=0.5), _alt([("s1", "e")], posterior=0.5)]}
    return {
        "observations": [obs.to_dict()], "footprints": [fp.to_dict()], "clusters": [cluster],
        "sample_knowledge": {**FCC_SAMPLE.to_dict(), "degradations": ()},
    }


def _spec(region) -> QuestionSpec:
    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=region)
    return QuestionSpec(family="ConfirmPresence", claim_kind="structural_presence",
                        subject=subject, predicate=PredicateDeclaration(
                            kind="structural_presence", version=1), priority=0)


def _agenda(spec: QuestionSpec) -> dict:
    policy = resolve_policy(card_guidelines={"mi_n_draws": 128})
    assessment = QuestionAssessment(question_id=spec.question_id, status="open").to_dict()
    return {"schema": "isar.agenda-revision/1", "resolved_policy": policy,
           "resolved_policy_fingerprint": fingerprint(policy),
           "specs": [spec.to_dict()], "assessments": [assessment]}


# ----------------------------------------------------------------------------
# 1. Belief level: the curve family never predicts a return-to-zero at the extinct fold
# ----------------------------------------------------------------------------
def test_scored_belief_never_predicts_return_to_zero_at_the_extinct_boundary():
    scene = _ascending_track_scene()
    beliefs = build_beliefs(scene, FCC_SAMPLE, ACOUSTIC_HYP)
    present = next(b for b in beliefs if b.family == "curve"
                  and any(a.omega == 4.0 for a in b.anchors))

    from isar.scene.phenomena import predicted_omega

    # Descends to zero AT the declared reflection (2.0)...
    assert math.isclose(predicted_omega(present, 2.0), 0.0, abs_tol=1e-6)
    # ...but the extinct (3,0,0)-analog boundary (3.0) is never a phantom zero: the branch keeps
    # rising past the last real anchor, all the way to the fold.
    at_boundary = predicted_omega(present, 3.0)
    assert at_boundary > predicted_omega(present, 2.6)
    assert at_boundary > 1.0


# ----------------------------------------------------------------------------
# 2. Scoring level: a descent probe is informative at 2.0, not at the extinct fold
# ----------------------------------------------------------------------------
def test_descent_probes_target_the_declared_2_0_gamma_not_the_extinct_fold():
    scene = _ascending_track_scene()
    spec = _spec(region=(1.5, 3.5))
    agenda = _agenda(spec)

    # Same narrow near-zero-energy window at both candidate nodes: only the belief's OWN
    # prediction (peak-in-window vs peak-elsewhere) can move the MI apart.
    at_reflection = Design(axis_coordinate=2.0, window=(-1.0, 1.0), n_points=21,
                           ncount=2_000_000.0)
    at_extinct_fold = Design(axis_coordinate=3.0, window=(-1.0, 1.0), n_points=21,
                             ncount=2_000_000.0)

    table = score(scene, agenda, ACOUSTIC_HYP, [at_reflection, at_extinct_fold], base_seed=101)
    mi = {row.design_key: row.terms[0].mi_nats for row in table.rows}

    # The declared reflection: the belief plants its mode exactly at ω=0 there, so the narrow
    # window sees a sharp, confidently-placed peak — near-maximal ConfirmPresence MI (ln 2, a
    # 50/50 present/absent split resolved outright).
    assert mi[design_key(at_reflection)] > 0.6
    # The extinct fold: the belief extrapolates the mode to ω≈6.7 with an honestly WIDE
    # position uncertainty (the undeclared-curvature slope cone — plan §5's named risk), so a
    # little probability leaks into the near-zero window, but far less confidently than the
    # declared reflection's sharp answer.
    assert mi[design_key(at_reflection)] > mi[design_key(at_extinct_fold)]
