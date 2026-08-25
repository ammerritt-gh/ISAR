"""Acceptance tests for the scene-EIG MI scoring engine (SCENE-5 slice 5.1).

Fixtures are hand-rolled, mirroring ``tests/scene/test_phenomena.py``'s idiom: real DTOs
(``SceneObservation``, ``CandidateRow``, ``Footprint``, ...) assembled by hand into
``isar.scene-revision/1``-shaped payload dicts, and real ``QuestionSpec``/``QuestionAssessment``
DTOs assembled into an ``isar.agenda-revision/1``-shaped payload with a real ``resolve_policy()``
block — never a running scene builder or agenda derivation, which this module does not need.
"""
import math

import pytest

from isar.scene.eig import (
    Design, SceneScoringError, build_beliefs, design_key, relevance_fingerprint,
    scoring_grid_step_meV, score)
from isar.scene.model import (CandidateRow, ComponentDescriptor, Footprint, FootprintBand,
                              ModeInterval, SampleKnowledge, SceneObservation, fingerprint)
from isar.scene.phenomena import FAMILIES
from isar.scene.questions import (PredicateDeclaration, QuestionAssessment, QuestionSpec,
                                  QuestionSubject)
from isar.schema.question_policy import resolve_policy

PARTITION = "p1"


# ----------------------------------------------------------------------------
# Fixture builders (mirrors test_phenomena.py's idiom)
# ----------------------------------------------------------------------------
def _mode_row(label: str, omega: float, sigma: float = 0.3, identity: str | None = None,
             slot: int = 0) -> CandidateRow:
    interval = ModeInterval(
        low=omega - sigma, center=omega, high=omega + sigma, nominal_level=0.68,
        interval_kind="hesse_delta", inference_paradigm="frequentist_asymptotic",
        source_quantity="freq_interval", authority={"authority_basis": "producer_sound"},
        conditioning_fingerprint=None)
    return CandidateRow(
        label=label, k=1, n_modes=1, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=identity, variant=None,
        omegas=(omega,), mode_intervals=(interval,),
        components=(ComponentDescriptor(kind="mode", slot=slot),))


def _two_mode_row(label: str, omega_a: float, omega_b: float, sigma: float = 0.3) -> CandidateRow:
    def _iv(omega):
        return ModeInterval(low=omega - sigma, center=omega, high=omega + sigma,
                            nominal_level=0.68, interval_kind="hesse_delta",
                            inference_paradigm="frequentist_asymptotic",
                            source_quantity="freq_interval",
                            authority={"authority_basis": "producer_sound"},
                            conditioning_fingerprint=None)
    return CandidateRow(
        label=label, k=2, n_modes=2, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=None, variant=None,
        omegas=(omega_a, omega_b), mode_intervals=(_iv(omega_a), _iv(omega_b)),
        components=(ComponentDescriptor(kind="mode", slot=0),
                   ComponentDescriptor(kind="mode", slot=1)))


def _elastic_row(label: str) -> CandidateRow:
    return CandidateRow(
        label=label, k=0, n_modes=0, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=None, variant=None,
        omegas=(), mode_intervals=(), components=(ComponentDescriptor(kind="elastic"),))


def _empty_row(label: str) -> CandidateRow:
    """A candidate with NO components at all — genuinely "nothing here", not even an elastic
    line, so ``build_beliefs`` mints it no belief and its composed rate is background alone. Used
    as the "absent" alternative wherever a real elastic peak (from ``_elastic_row``) would itself
    be a confound the test is not trying to exercise."""
    return CandidateRow(
        label=label, k=0, n_modes=0, log_evidence=0.0, evidence_method="test",
        posterior_probability=1.0, usable=True, identity=None, variant=None,
        omegas=(), mode_intervals=(), components=())


def _observation(scan_ref: str, rows: tuple[CandidateRow, ...], gauss_fwhm: float = 0.4
                 ) -> SceneObservation:
    return SceneObservation(
        scan_ref=scan_ref, q=(0.0, 0.0, 0.0), temperature_K=None,
        energy_window=(-30.0, 30.0), energy_bands=((-30.0, 30.0),),
        gauss_fwhm=gauss_fwhm, lorentz_fwhm=None, effective_exposure=1.0, background=0.0,
        best_label=rows[0].label, candidates=rows, evidence_comparison_status="complete",
        selection_fallback=None, arena_fingerprint=None, evidence_sink=None, sample="fcc-test",
        conditions=(), count_trust="trusted", lattice_epoch=None, fingerprint="test")


def _footprint(scan_ref: str, axis_coordinate: float, partition_id: str = PARTITION,
              bands: tuple = ()) -> Footprint:
    return Footprint(scan_ref=scan_ref, q=(axis_coordinate, 0.0, 0.0), bands=bands,
                     temperature_K=None, background=0.0, gauss_fwhm=0.4,
                     axis_coordinate=axis_coordinate, partition_id=partition_id)


def _alt(assignment, links=(), posterior: float = 1.0) -> dict:
    return {"assignment": [list(p) for p in assignment], "links": [list(p) for p in links],
           "base": 0.0, "factor_terms": {}, "prior_terms": {}, "reasons": {}, "total": 0.0,
           "posterior": posterior}


def _cluster(cluster_id: str, scan_refs, alternatives) -> dict:
    return {"cluster_id": cluster_id, "scan_refs": list(scan_refs), "alternatives": alternatives}


def _scene(observations, footprints, clusters, sample: SampleKnowledge | None = None) -> dict:
    sample = sample or SampleKnowledge()
    return {
        "observations": [o.to_dict() for o in observations],
        "footprints": [fp.to_dict() for fp in footprints],
        "clusters": clusters,
        "sample_knowledge": {**sample.to_dict(), "degradations": ()},
    }


def _spec(family: str, claim_kind: str, subject: QuestionSubject, priority: int = 0
         ) -> QuestionSpec:
    return QuestionSpec(family=family, claim_kind=claim_kind, subject=subject,
                        predicate=PredicateDeclaration(kind=claim_kind, version=1),
                        priority=priority)


def _agenda(spec_statuses, policy) -> dict:
    specs = [s for s, _ in spec_statuses]
    assessments = [QuestionAssessment(question_id=s.question_id, status=st).to_dict()
                  for s, st in spec_statuses]
    return {"schema": "isar.agenda-revision/1", "resolved_policy": policy,
           "resolved_policy_fingerprint": fingerprint(policy),
           "specs": [s.to_dict() for s in specs], "assessments": assessments}


def _policy(**card_guidelines) -> dict:
    return resolve_policy(card_guidelines=card_guidelines)


ACOUSTIC_HYP = {"acoustic": {"preset": "acoustic", "zc_energy": {"mode": 0.0, "sigma": 0.3},
                             "confidence": 0.8}}


# ----------------------------------------------------------------------------
# A reusable "present vs absent" two-alternative single cluster
# ----------------------------------------------------------------------------
def _confirm_presence_scene(omega: float, axis: float = 2.0, region=(1.5, 2.5),
                            posteriors=(0.5, 0.5), sample: SampleKnowledge | None = None) -> dict:
    obs = _observation("s1", (_mode_row("m", omega), _empty_row("e")))
    fp = _footprint("s1", axis)
    cluster = _cluster("c1", ["s1"], [
        _alt([("s1", "m")], posterior=posteriors[0]),
        _alt([("s1", "e")], posterior=posteriors[1]),
    ])
    return _scene([obs], [fp], [cluster], sample=sample), region


def _confirm_presence_spec(region, priority: int = 0) -> QuestionSpec:
    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=region)
    return _spec("ConfirmPresence", "structural_presence", subject, priority=priority)


# ----------------------------------------------------------------------------
# 1. Temperature A/B: MI collapses on the anti-Stokes side at low T, no temperature branch here
# ----------------------------------------------------------------------------
def test_temperature_ab_collapses_the_anti_stokes_side_at_low_temperature():
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=64)
    agenda = _agenda([(spec, "open")], policy)

    # Anti-Stokes-only window: the Stokes peak at +8 meV never enters it, so any discrimination
    # between "present" and "absent" comes ONLY from the Bose-weighted anti-Stokes mirror lobe —
    # exactly what a temperature branch in scoring code would otherwise be needed to price.
    low = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61, ncount=2_000_000.0,
                temperature_K=5.0)
    high = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61, ncount=2_000_000.0,
                 temperature_K=300.0)

    table = score(scene, agenda, None, [low, high], base_seed=1)
    mi = {row.design_key: row.terms[0].mi_nats for row in table.rows}
    assert mi[design_key(high)] > mi[design_key(low)]


# ----------------------------------------------------------------------------
# 2. Nuisance guard: identity ambiguity with no open ClassifyIdentity moves nothing; opening it
#    scores.
# ----------------------------------------------------------------------------
def test_nuisance_guard_identity_ambiguity_leaks_nothing_when_unopened():
    obs = _observation("s1", (_mode_row("a", 8.0, identity="alpha"),
                              _mode_row("b", 8.0, identity="beta", slot=0)))
    fp = _footprint("s1", 2.0)
    cluster = _cluster("c1", ["s1"], [_alt([("s1", "a")], posterior=0.5),
                                      _alt([("s1", "b")], posterior=0.5)])
    scene = _scene([obs], [fp], [cluster])
    region = (1.5, 2.5)
    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=region)
    cp_spec = _spec("ConfirmPresence", "structural_presence", subject)
    ci_spec = _spec("ClassifyIdentity", "identity_resolved", subject)
    design = Design(axis_coordinate=2.0, window=(-15.0, 15.0), n_points=61, ncount=2_000_000.0)
    policy = _policy(mi_n_draws=64)

    without_ci = score(scene, _agenda([(cp_spec, "open")], policy), None, [design], base_seed=3)
    with_ci = score(scene, _agenda([(cp_spec, "open"), (ci_spec, "open")], policy), None,
                    [design], base_seed=3)

    cp_without = without_ci.rows[0].terms[0]
    cp_with = next(t for t in with_ci.rows[0].terms if t.family == "ConfirmPresence")
    assert cp_without.family == "ConfirmPresence"
    assert cp_without.mi_nats == cp_with.mi_nats

    assert [t.family for t in without_ci.rows[0].terms] == ["ConfirmPresence"]
    ci_term = next(t for t in with_ci.rows[0].terms if t.family == "ClassifyIdentity")
    assert ci_term.mi_nats > 0.0


# ----------------------------------------------------------------------------
# 3. Priority-weight trade
# ----------------------------------------------------------------------------
def test_priority_weight_trade_and_retuned_weights_flip_the_ranking():
    obs_big = _observation("s_big", (_mode_row("m", 15.0), _empty_row("e")))
    obs_small = _observation("s_small", (_mode_row("m", 3.0), _empty_row("e")))
    fp_big = _footprint("s_big", 10.0)
    fp_small = _footprint("s_small", 2.0)
    cluster_big = _cluster("cbig", ["s_big"], [_alt([("s_big", "m")], posterior=0.5),
                                               _alt([("s_big", "e")], posterior=0.5)])
    cluster_small = _cluster("csmall", ["s_small"], [_alt([("s_small", "m")], posterior=0.99),
                                                      _alt([("s_small", "e")], posterior=0.01)])
    # A tiny declared dispersion slope keeps each peak SHARP even when a design queries it from
    # far away in q (isar.scene.phenomena.predicted_sigma's slope-cone term scales with distance
    # from the belief's own anchor) — without it, a design's own extrapolated width leaks
    # information about the OTHER cluster regardless of its window, which is real physics but
    # would confound the windowed trade-off this test is built to isolate.
    sample = SampleKnowledge(dispersion_slope_max=0.01)
    scene = _scene([obs_big, obs_small], [fp_big, fp_small], [cluster_big, cluster_small],
                  sample=sample)

    subject_big = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=(9.5, 10.5))
    subject_small = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=(1.5, 2.5))
    spec_big = _spec("ConfirmPresence", "structural_presence", subject_big, priority=5)
    spec_small = _spec("ConfirmPresence", "structural_presence", subject_small, priority=0)

    # axis_coordinate matches each cluster's own anchor (10.0 / 2.0): the predictive sigma's
    # slope-cone term grows with DISTANCE FROM THE ANCHOR (isar.scene.phenomena.predicted_sigma),
    # so a design placed far from an anchor reads as honestly wide regardless of its window — the
    # trade this test wants comes from the WINDOW missing the other cluster's peak, not from an
    # accidentally huge extrapolated width.
    design_big = Design(axis_coordinate=10.0, window=(10.0, 20.0), n_points=81, ncount=2_000_000.0)
    design_small = Design(axis_coordinate=2.0, window=(0.0, 6.0), n_points=81, ncount=2_000_000.0)

    default_policy = _policy(mi_n_draws=64)
    agenda = _agenda([(spec_big, "open"), (spec_small, "open")], default_policy)
    default_table = score(scene, agenda, None, [design_big, design_small], base_seed=5)
    default_scores = {row.design_key: row.total for row in default_table.rows}
    assert default_scores[design_key(design_big)] > default_scores[design_key(design_small)]

    retuned_policy = _policy(mi_n_draws=64,
                             priority_weights={"0": 50.0, "1": 1.0, "2": 0.8, "3": 0.6, "4": 0.4,
                                               "5": 0.01})
    retuned_agenda = _agenda([(spec_big, "open"), (spec_small, "open")], retuned_policy)
    retuned_table = score(scene, retuned_agenda, None, [design_big, design_small], base_seed=5)
    retuned_scores = {row.design_key: row.total for row in retuned_table.rows}
    assert retuned_scores[design_key(design_small)] > retuned_scores[design_key(design_big)]


# ----------------------------------------------------------------------------
# 4. Paranoia monotonicity
# ----------------------------------------------------------------------------
def test_paranoia_monotonicity_over_the_discover_region_bernoulli():
    scene = _scene([], [], [])
    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=(20.0, 30.0))
    spec = _spec("DiscoverRegion", "coverage_complete", subject)
    # Large ncount + many draws: the unknown-family signal is a small FIXED fraction of the flat
    # background at any exposure (both scale linearly with ncount), so resolving it at all needs
    # enough total counts and enough draws to beat Poisson noise, not a bigger relative gap.
    design = Design(axis_coordinate=25.0, window=(-5.0, 5.0), n_points=21, ncount=5.0e8)
    policy_kwargs = {"mi_n_draws": 512}

    zero = score(scene, _agenda([(spec, "open")],
                                _policy(paranoia_unknown_prior=0.0, **policy_kwargs)), None,
                [design], base_seed=7)
    low = score(scene, _agenda([(spec, "open")],
                               _policy(paranoia_unknown_prior=0.05, **policy_kwargs)), None,
               [design], base_seed=7)
    high = score(scene, _agenda([(spec, "open")],
                                _policy(paranoia_unknown_prior=0.2, **policy_kwargs)), None,
                [design], base_seed=7)

    assert zero.rows[0].terms[0].mi_nats == 0.0
    assert zero.rows[0].total == 0.0
    assert 0.0 < low.rows[0].terms[0].mi_nats < high.rows[0].terms[0].mi_nats
    assert low.rows[0].total < high.rows[0].total


# ----------------------------------------------------------------------------
# 5. Hypotheses-live
# ----------------------------------------------------------------------------
def test_declared_hypothesis_changes_the_score():
    row_a = _mode_row("m", 8.0, identity="acoustic")
    row_b = _mode_row("m", 9.0, identity="acoustic")
    obs_a = _observation("s1", (row_a,))
    obs_b = _observation("s2", (row_b,))
    obs_c = _observation("s3", (_empty_row("e"),))
    fp_a, fp_b, fp_c = _footprint("s1", 2.2), _footprint("s2", 2.6), _footprint("s3", 2.4)
    cluster = _cluster("c1", ["s1", "s2", "s3"], [
        _alt([("s1", "m"), ("s2", "m")], links=[("s1#m#0", "s2#m#0")], posterior=0.5),
        _alt([("s3", "e")], posterior=0.5),
    ])
    sample = SampleKnowledge(zone_period=1.0, reflections=(2.0, 4.0))
    scene = _scene([obs_a, obs_b, obs_c], [fp_a, fp_b, fp_c], [cluster], sample=sample)

    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=(1.5, 3.5))
    spec = _confirm_presence_spec_for(subject)
    design = Design(axis_coordinate=2.0, window=(-2.0, 2.0), n_points=41, ncount=2_000_000.0)
    policy = _policy(mi_n_draws=64)

    without = score(scene, _agenda([(spec, "open")], policy), None, [design], base_seed=9)
    with_hyp = score(scene, _agenda([(spec, "open")], policy), ACOUSTIC_HYP, [design], base_seed=9)

    assert with_hyp.rows[0].terms[0].mi_nats > without.rows[0].terms[0].mi_nats


def _confirm_presence_spec_for(subject: QuestionSubject, priority: int = 0) -> QuestionSpec:
    return _spec("ConfirmPresence", "structural_presence", subject, priority=priority)


# ----------------------------------------------------------------------------
# 6. Multi-belief separation
# ----------------------------------------------------------------------------
def test_two_mode_alternative_separates_from_one_mode_under_sum_composition():
    obs = _observation("s1", (_mode_row("one", 8.0), _two_mode_row("two", 8.0, 14.0)))
    # A real measured band covering both mode positions: match_subject only matches a footprint
    # whose OWN persisted bands overlap a subject's declared omega_band_meV (the geometric
    # coverage claim predicates.py's own evaluators rely on) — an empty ``bands=()`` would
    # exclude the footprint outright regardless of what the candidates assert.
    fp = _footprint("s1", 2.0, bands=(FootprintBand(0.0, 20.0, "stokes"),))
    cluster = _cluster("c1", ["s1"], [_alt([("s1", "one")], posterior=0.5),
                                      _alt([("s1", "two")], posterior=0.5)])
    scene = _scene([obs], [fp], [cluster])
    # A band around the SECOND mode only: the one-mode alternative has nothing there, the
    # two-mode alternative does — this can only separate if build_beliefs/predict compose BOTH
    # of the two-mode alternative's beliefs (sum composition), never just the first.
    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=(1.5, 2.5),
                              omega_band_meV=(12.0, 16.0))
    spec = _spec("ConfirmPresence", "structural_presence", subject)
    design = Design(axis_coordinate=2.0, window=(10.0, 18.0), n_points=81, ncount=2_000_000.0)
    policy = _policy(mi_n_draws=64)

    table = score(scene, _agenda([(spec, "open")], policy), None, [design], base_seed=11)
    assert table.rows[0].terms[0].mi_nats > 0.0


# ----------------------------------------------------------------------------
# 7. No continuum from build_beliefs
# ----------------------------------------------------------------------------
def test_build_beliefs_never_emits_continuum():
    scene, _ = _confirm_presence_scene(omega=8.0)
    beliefs = build_beliefs(scene, SampleKnowledge())
    assert beliefs
    assert all(b.family != "continuum" for b in beliefs)


# ----------------------------------------------------------------------------
# 8. Cross-cluster additivity
# ----------------------------------------------------------------------------
def test_cross_cluster_additivity():
    obs_a = _observation("sa", (_mode_row("m", 8.0), _empty_row("e")))
    obs_b = _observation("sb", (_mode_row("m", 8.0), _empty_row("e")))
    fp_a, fp_b = _footprint("sa", 2.0), _footprint("sb", 2.2)
    cluster_a = _cluster("ca", ["sa"], [_alt([("sa", "m")], posterior=0.5),
                                        _alt([("sa", "e")], posterior=0.5)])
    cluster_b = _cluster("cb", ["sb"], [_alt([("sb", "m")], posterior=0.5),
                                        _alt([("sb", "e")], posterior=0.5)])
    subject = QuestionSubject(kind="region", partition_id=PARTITION, region_rlu=(1.5, 2.5))
    spec = _spec("ConfirmPresence", "structural_presence", subject)
    design = Design(axis_coordinate=2.0, window=(-15.0, 15.0), n_points=61, ncount=2_000_000.0)
    policy = _policy(mi_n_draws=64)
    agenda = _agenda([(spec, "open")], policy)

    both = _scene([obs_a, obs_b], [fp_a, fp_b], [cluster_a, cluster_b])
    only_a = _scene([obs_a], [fp_a], [cluster_a])
    only_b = _scene([obs_b], [fp_b], [cluster_b])

    mi_both = score(both, agenda, None, [design], base_seed=13).rows[0].terms[0]
    mi_a = score(only_a, agenda, None, [design], base_seed=13).rows[0].terms[0]
    mi_b = score(only_b, agenda, None, [design], base_seed=13).rows[0].terms[0]

    assert mi_both.contributing_clusters == 2
    assert math.isclose(mi_both.mi_nats, mi_a.mi_nats + mi_b.mi_nats, rel_tol=1e-9, abs_tol=1e-12)


# ----------------------------------------------------------------------------
# 9. Byte-identical replay
# ----------------------------------------------------------------------------
def test_byte_identical_replay():
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=32)
    agenda = _agenda([(spec, "open")], policy)
    design = Design(axis_coordinate=2.0, window=(-15.0, 15.0), n_points=41, ncount=1_000_000.0)

    a = score(scene, agenda, None, [design], base_seed=17)
    b = score(scene, agenda, None, [design], base_seed=17)
    assert a == b


# ----------------------------------------------------------------------------
# 10. Determinism across dict ordering
# ----------------------------------------------------------------------------
def test_determinism_across_families_and_designs_ordering():
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=32)
    agenda = _agenda([(spec, "open")], policy)
    d1 = Design(axis_coordinate=2.0, window=(-15.0, 15.0), n_points=41, ncount=1_000_000.0)
    d2 = Design(axis_coordinate=2.0, window=(-15.0, 15.0), n_points=41, ncount=1_500_000.0)

    baseline = score(scene, agenda, None, [d1, d2], base_seed=19, families=FAMILIES)
    permuted_families = dict(reversed(list(FAMILIES.items())))
    permuted = score(scene, agenda, None, [d2, d1], base_seed=19, families=permuted_families)
    assert baseline == permuted


# ----------------------------------------------------------------------------
# Sanity refusals
# ----------------------------------------------------------------------------
def test_refuses_an_invalid_resolved_policy_block():
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    agenda = _agenda([(spec, "open")], _policy())
    agenda["resolved_policy"] = {"schema": "isar.question-policy/2"}
    design = Design(axis_coordinate=2.0, window=(-5.0, 5.0), n_points=11, ncount=1_000.0)
    with pytest.raises(ValueError, match="isar.question-policy/3"):
        score(scene, agenda, None, [design], base_seed=1)


# ----------------------------------------------------------------------------
# SceneScoringError typed boundary (SCENE-5 5.2, D7)
# ----------------------------------------------------------------------------
def test_scoring_grid_step_meV_is_declared_and_moves_the_relevance_fingerprint():
    """The declared default (0.5) and that it lives in the fingerprinted relevance config —
    changing it is intended to move ``relevance_fingerprint`` (the plan's own note)."""
    assert scoring_grid_step_meV() == pytest.approx(0.5)
    from isar.scene import eig as eig_module

    original = eig_module.RELEVANCE_REGISTRY["DiscoverRegion"]
    retuned = eig_module.RelevanceExtractor(
        "DiscoverRegion", "1",
        config=(("background_counts_per_point", 2.0), ("scoring_grid_step_meV", 1.0)))
    baseline_fp = relevance_fingerprint()
    eig_module.RELEVANCE_REGISTRY["DiscoverRegion"] = retuned
    try:
        assert scoring_grid_step_meV() == pytest.approx(1.0)
        assert relevance_fingerprint() != baseline_fp
    finally:
        eig_module.RELEVANCE_REGISTRY["DiscoverRegion"] = original


def test_belief_construction_failure_raises_scene_scoring_error_with_reason():
    """A corrupt scene (an assignment naming an arena the payload does not embed) makes
    ``build_beliefs`` raise; ``score`` wraps it as ``scene_belief_construction_failed``, never
    the raw exception."""
    scene, region = _confirm_presence_scene(omega=8.0)
    scene["clusters"][0]["alternatives"][0]["assignment"] = [("ghost-arena", "m")]
    spec = _confirm_presence_spec(region)
    agenda = _agenda([(spec, "open")], _policy())
    design = Design(axis_coordinate=2.0, window=(-5.0, 5.0), n_points=11, ncount=1_000.0)
    with pytest.raises(SceneScoringError) as excinfo:
        score(scene, agenda, None, [design], base_seed=1)
    assert excinfo.value.reason == "scene_belief_construction_failed"


def test_estimation_failure_raises_scene_scoring_error_with_reason():
    """A question spec naming a family the relevance registry has no extractor for makes
    ``_question_mi`` raise; ``score`` wraps it as ``scoring_exception``."""
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    agenda = _agenda([(spec, "open")], _policy())
    agenda["specs"][0]["family"] = "NoSuchFamily"
    design = Design(axis_coordinate=2.0, window=(-5.0, 5.0), n_points=11, ncount=1_000.0)
    with pytest.raises(SceneScoringError) as excinfo:
        score(scene, agenda, None, [design], base_seed=1)
    assert excinfo.value.reason == "scoring_exception"


# ----------------------------------------------------------------------------
# SCENE-5 5.3 (Ruling A): the thermal_consistency corroboration term
# ----------------------------------------------------------------------------
def _single_alternative_confirm_presence_scene(omega: float = 8.0, axis: float = 2.0,
                                               region=(1.5, 2.5)) -> tuple[dict, tuple]:
    """ONE alternative, posterior 1.0 -- no class variance at all, so the ordinary partition MI
    (``_cluster_mi``) contributes exactly 0 and any MI on this question is the thermal_consistency
    term alone (isolates the new component from the pre-existing partition-MI machinery)."""
    obs = _observation("s1", (_mode_row("m", omega),))
    fp = _footprint("s1", axis)
    cluster = _cluster("c1", ["s1"], [_alt([("s1", "m")], posterior=1.0)])
    return _scene([obs], [fp], [cluster]), region


def test_thermal_consistency_term_positive_on_minus_e_zero_on_plus_e_twin_at_warm_t():
    """Warm T: the −E design's ConfirmPresence MI gains a strictly positive thermal_consistency
    term (its window covers where the derived anti-Stokes lobe sits); the +E twin's term stays
    ~0 (its window never overlaps the lobe, so WITH- and WITHOUT-lobe composed rates coincide) —
    no window/temperature branch in the term itself, only in the rates the shared predictive
    already produces."""
    scene, region = _single_alternative_confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=128, paranoia_unknown_prior=0.3)
    agenda = _agenda([(spec, "open")], policy)

    minus_e = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61, ncount=2_000_000.0,
                     temperature_K=300.0)
    plus_e = Design(axis_coordinate=2.0, window=(2.0, 15.0), n_points=61, ncount=2_000_000.0,
                    temperature_K=300.0)

    minus_mi = score(scene, agenda, None, [minus_e], base_seed=23).rows[0].terms[0].mi_nats
    plus_mi = score(scene, agenda, None, [plus_e], base_seed=23).rows[0].terms[0].mi_nats
    assert minus_mi > 1e-3
    assert plus_mi < 1e-6


def test_thermal_consistency_term_is_near_zero_at_cold_temperature():
    """Cold twin: the Bose factor collapses the anti-Stokes lobe to ~nothing, so the WITH/WITHOUT
    composed rates coincide even in the −E window and the isolated term reads ~0."""
    scene, region = _single_alternative_confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=128, paranoia_unknown_prior=0.3)
    agenda = _agenda([(spec, "open")], policy)

    cold_minus_e = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61,
                          ncount=2_000_000.0, temperature_K=5.0)
    cold_mi = score(scene, agenda, None, [cold_minus_e], base_seed=23).rows[0].terms[0].mi_nats
    assert cold_mi < 1e-6


def test_cold_minus_e_mirror_never_outranks_the_plus_e_canonical_in_a_score_table():
    """Ruling A's dominance proof (a two-alternative present/absent scene, the mechanism the
    isolated single-alternative tests above deliberately factor out): a cold −E mirror is priced
    a small thermal term on top of a near-zero partition-MI contribution (the class-variance term
    itself barely separates present/absent through the tiny cold-T anti-Stokes tail), while the
    +E canonical resolves presence directly at the Stokes peak with a large partition MI -- the
    +E canonical wins the score() table outright."""
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=128, paranoia_unknown_prior=0.3)
    agenda = _agenda([(spec, "open")], policy)

    cold_minus_e = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61,
                          ncount=2_000_000.0, temperature_K=5.0)
    plus_e_canonical = Design(axis_coordinate=2.0, window=(2.0, 15.0), n_points=61,
                              ncount=2_000_000.0, temperature_K=5.0)
    table = score(scene, agenda, None, [cold_minus_e, plus_e_canonical], base_seed=23)
    assert table.rows[0].design_key == design_key(plus_e_canonical), (
        "the cold mirror must never outrank the +E canonical")


def test_thermal_consistency_term_is_exactly_zero_at_paranoia_zero():
    """Paranoia 0 is the DiscoverRegion precedent: a degenerate short-circuit to exactly 0, no
    Monte Carlo noise -- even in a warm −E window where the term is otherwise strongly positive."""
    scene, region = _single_alternative_confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    policy = _policy(mi_n_draws=128, paranoia_unknown_prior=0.0)
    agenda = _agenda([(spec, "open")], policy)
    minus_e = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61, ncount=2_000_000.0,
                     temperature_K=300.0)
    table = score(scene, agenda, None, [minus_e], base_seed=23)
    assert table.rows[0].terms[0].mi_nats == 0.0


def test_thermal_consistency_doubt_prior_sits_on_the_not_as_believed_state(monkeypatch):
    """PR #36 review (Codex P2-1). H23(a) declares ``paranoia_unknown_prior`` as the prior mass
    on the DOUBT state, so the association must be ``as_believed`` ← ``1 − p`` and
    ``not_as_believed`` ← ``p``. The prior entropy H(p) is symmetric, so every p=0.5 test above
    is structurally blind to an inverted association; what is NOT symmetric is the expected
    conditional entropy, which weights each state's DRAWS by its prior. Two pins, so the
    regression cannot come back silently: (1) a spy on ``_mi_from_rates`` asserts the
    association itself; (2) the term is genuinely asymmetric under p ↔ 1−p at partial
    discrimination, proving the association is observable and the spy not vacuous."""
    import numpy as np

    from isar.scene import eig as eig_mod

    scene, region = _single_alternative_confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    minus_e = Design(axis_coordinate=2.0, window=(-15.0, -2.0), n_points=61,
                     ncount=40_000.0, temperature_K=300.0)  # partial, not saturated

    captured: list = []
    real = eig_mod._mi_from_rates

    def spy(rates, posteriors, classes, mi_n_draws, seeds):
        if list(classes) == ["as_believed", "not_as_believed"]:
            captured.append(np.asarray(posteriors, dtype=float).copy())
        return real(rates, posteriors, classes, mi_n_draws, seeds)

    monkeypatch.setattr(eig_mod, "_mi_from_rates", spy)

    def term_at(p: float) -> float:
        agenda = _agenda([(spec, "open")], _policy(mi_n_draws=256, paranoia_unknown_prior=p))
        return score(scene, agenda, None, [minus_e], base_seed=23).rows[0].terms[0].mi_nats

    low = term_at(0.1)
    assert captured, "the thermal_consistency term never reached the estimator"
    assert np.allclose(captured[-1], [0.9, 0.1]), (
        f"doubt prior must sit on not_as_believed: expected [0.9, 0.1], got {captured[-1]}")

    high = term_at(0.9)
    assert np.allclose(captured[-1], [0.1, 0.9])
    assert low != high, ("the term must be asymmetric under p <-> 1-p at partial "
                         "discrimination — a symmetric value means the association is "
                         "unobservable and this pin is vacuous")


def test_confirm_presence_relevance_fingerprint_moves_with_the_declared_component():
    """The relevance_fingerprint changes iff the declared thermal_consistency component (or any
    of its config) changes -- the same discipline as every other declared registry entry."""
    from isar.scene import eig as eig_module

    original = eig_module.RELEVANCE_REGISTRY["ConfirmPresence"]
    baseline_fp = relevance_fingerprint()
    without_component = eig_module.RelevanceExtractor("ConfirmPresence", "1")
    eig_module.RELEVANCE_REGISTRY["ConfirmPresence"] = without_component
    try:
        assert relevance_fingerprint() != baseline_fp
    finally:
        eig_module.RELEVANCE_REGISTRY["ConfirmPresence"] = original
    assert relevance_fingerprint() == baseline_fp


def test_refuses_duplicate_design_keys():
    scene, region = _confirm_presence_scene(omega=8.0)
    spec = _confirm_presence_spec(region)
    agenda = _agenda([(spec, "open")], _policy())
    d1 = Design(axis_coordinate=2.0, window=(-5.0, 5.0), n_points=11, ncount=1_000.0)
    d2 = Design(axis_coordinate=2.0, window=(-5.0, 5.0), n_points=11, ncount=1_000.0)
    with pytest.raises(ValueError, match="duplicate design keys"):
        score(scene, agenda, None, [d1, d2], base_seed=1)
