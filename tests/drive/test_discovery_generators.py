"""The seven discovery generator bindings (PR 4.2b C4 / B3): one test per matrix row, pinning
construction (hkl inverse-map, tile-midpoint targeting, the parameter_refine case (i)/(ii)/
no_support_capable_interval walk, the bridge-node rule, the mismatch arena rule), the typed
clean-zero/INAPPLICABLE vocabulary with its exhaustion-clean flags, the cold-start refusal, and
kind-to-generator attribution. Payload builders mirror ``tests/scene/test_predicates.py`` and
``tests/drive/test_opening_book.py`` (copied, not imported — those modules own their own shapes).

Where a row's construction depends on the assessment REASON an evaluator names
(``track_bridge_scan``, the tolerance claim of ``parameter_refine``), the assessment comes from
the real ``isar.scene.predicates.evaluate_predicate`` — never a hand-typed note — so the
generator is tested against genuine evaluator output. Where a row's construction is agenda-shape
driven (cold start, kind attribution, tile-midpoint via an emergent/anchored opener), the agenda
comes from the real ``isar.scene.agenda.derive_agenda`` (mirroring ``tests/scene/test_agenda.py``
conventions), so specs and assessments are genuine there too.
"""
from __future__ import annotations

import pytest

from isar.analyze.catalogue import DEFAULT_PROFILE
from isar.schema.generators import FAMILY_GENERATORS
from isar.schema.question_policy import resolve_policy
from isar.scene.agenda import derive_agenda
from isar.scene.model import fingerprint
from isar.scene.predicates import evaluate_predicate
from isar.scene.questions import (
    PredicateDeclaration, QuestionAssessment, QuestionSpec, QuestionSubject)

import isar.drive.steering.generators as gen
from isar.drive.steering.generators import (
    DiscoveryContext,
    GENERATOR_FUNCTIONS,
    GeneratorOutcome,
    _hkl_at,
    dependency_depth,
    exposure_fill,
    identity_discriminator,
    mismatch_probe,
    opening_book_survey,
    parameter_refine,
    presence_probe,
    track_bridge_scan,
)

P1 = "a" * 64
P2 = "b" * 64


# ----------------------------------------------------------------------------
# Payload builders (persisted shapes, mirroring tests/scene/test_predicates.py)
# ----------------------------------------------------------------------------
def cand(label, omegas=(), identity=None, usable=True, mode_intervals=None):
    components = [{"kind": "mode", "slot": i} for i in range(len(omegas))]
    if mode_intervals is None:
        mode_intervals = [None] * len(omegas)
    return {"label": label, "omegas": list(omegas), "identity": identity,
            "usable": usable, "components": components, "n_modes": len(omegas),
            "mode_intervals": list(mode_intervals)}


def trusted_interval(low, high, *, nominal_level=0.68):
    return {"low": low, "center": (low + high) / 2.0, "high": high,
            "nominal_level": nominal_level,
            "interval_kind": "parametric_bootstrap_percentile",
            "inference_paradigm": "frequentist_sampling_distribution",
            "source_quantity": "freq_interval",
            "authority": {"authority_basis": "producer_sound"},
            "conditioning_fingerprint": "c" * 64}


def obs(scan_ref, cands=(), count_trust="trusted", sample="s1", epoch=1, conditions=()):
    return {"scan_ref": scan_ref, "candidates": list(cands), "count_trust": count_trust,
            "sample": sample, "lattice_epoch": epoch,
            "conditions": [list(pair) for pair in conditions], "best_label": None}


def fp(scan_ref, coord, bands=((0.0, 10.0),), exposure=5.0, partition=P1):
    return {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition,
            "effective_exposure": exposure,
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands]}


def alt(assignment, posterior, links=()):
    return {"assignment": [list(pair) for pair in assignment], "posterior": posterior,
            "links": [list(pair) for pair in links]}


def cluster(scan_refs, alternatives, *, truncated=False, unbounded=False, bound=None,
            cluster_id="c0"):
    return {"cluster_id": cluster_id, "scan_refs": list(scan_refs),
            "alternatives": list(alternatives), "truncated": truncated,
            "truncation_unbounded": unbounded, "log_discarded_mass_bound": bound,
            "linkage_truncated": False}


def payload(observations=(), footprints=(), clusters=(), sample_knowledge=None):
    return {"observations": list(observations), "footprints": list(footprints),
            "clusters": list(clusters), "non_scoring": [],
            "sample_knowledge": sample_knowledge or {}, "condition_t_round": None}


def one_arena_with_interval(mode_interval, omega=5.0, posterior=1.0):
    return payload(
        observations=[obs("e:1:d", [cand("A", omegas=(omega,), mode_intervals=[mode_interval]),
                                    cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], posterior),
                                      alt([("e:1:d", "B")], 1.0 - posterior)])])


def _two_mode_scene(c_interval):
    return payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.95, 5.05)]),
            cand("C", omegas=(5.02,), mode_intervals=[c_interval])])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.6),
                                      alt([("e:1:d", "C")], 0.4)])])


def step(q_start=(0.0, 0.0, 0.0), q_end=(1.0, 0.0, 0.0), spacing=0.5, domain=(0.0, 10.0),
        survey_order="uniform", exposure_floor=1.0, catalogue_profile=DEFAULT_PROFILE):
    policy = resolve_policy(user_ask={"survey_order": survey_order,
                                      "min_effective_exposure": exposure_floor})
    return {"schema": "isar.campaign-step.discover_and_map/2",
            "region": {"kind": "path", "q_start": list(q_start), "q_end": list(q_end),
                       "maximum_initial_spacing_rlu": spacing},
            "energy": {"intrinsic_omega_domain_meV": list(domain)},
            "catalogue_profile": catalogue_profile,
            "deliverables": [{"kind": "phenomenon_map"}],
            "include_unknown": True,
            "resolved_policy": policy}


def make_spec(family, claim_kind, subject, params, *, priority=0, deps=(),
             authority_requirement="exact_or_bounded"):
    return QuestionSpec(
        family=family, claim_kind=claim_kind, subject=subject,
        predicate=PredicateDeclaration(kind=claim_kind, version=1, parameters=params,
                                       authority_requirement=authority_requirement),
        priority=priority, dependencies=deps)


def manual_context(scene, step_payload, specs, assessments, basis_digest="c" * 64):
    return DiscoveryContext(
        scene_payload=scene,
        agenda_payload={"specs": [s.to_dict() for s in specs],
                        "assessments": [a.to_dict() for a in assessments]},
        step_payload=step_payload, basis_agenda_digest=basis_digest,
        scene_digest=fingerprint(scene))


def derive(scene, step_payload, **kw):
    return derive_agenda(scene, step_payload, step_id="discover-1",
                         program_fingerprint="prog-1", **kw)


def context_for_revision(scene, step_payload, revision):
    return DiscoveryContext(
        scene_payload=scene, agenda_payload=revision.payload, step_payload=step_payload,
        basis_agenda_digest=revision.payload["basis_digest"], scene_digest=fingerprint(scene))


def spec_of(revision, family, claim_kind=None, **subject_filters):
    for s in revision.payload["specs"]:
        if s["family"] != family:
            continue
        if claim_kind is not None and s["claim_kind"] != claim_kind:
            continue
        if all(s["subject"].get(k) == v for k, v in subject_filters.items()):
            return s
    raise AssertionError(f"no {family}/{claim_kind} spec matching {subject_filters}")


# ----------------------------------------------------------------------------
# Shared machinery
# ----------------------------------------------------------------------------
def test_dispatch_table_covers_every_declared_generator():
    declared = {generator_id for ids in FAMILY_GENERATORS.values() for generator_id in ids}
    assert declared == {"opening_book_survey", "exposure_fill", "presence_probe",
                        "identity_discriminator", "track_bridge_scan", "parameter_refine",
                        "mismatch_probe"}
    assert set(GENERATOR_FUNCTIONS) == declared


def test_generator_outcome_requires_exactly_one_shape():
    with pytest.raises(ValueError):
        GeneratorOutcome()                                        # nothing set
    with pytest.raises(ValueError):
        GeneratorOutcome(proposals=(("i", "a"),), clean_zero_reason="x")  # two set
    with pytest.raises(ValueError):
        GeneratorOutcome(inapplicable_reason="x", exhaustion_clean=True)  # INAPPLICABLE + clean


def test_dependency_depth_is_the_longest_chain_length():
    agenda_payload = {"specs": [
        {"question_id": "a", "dependencies": []},
        {"question_id": "b", "dependencies": ["a"]},
        {"question_id": "c", "dependencies": ["b"]},
        {"question_id": "d", "dependencies": ["a", "c"]},
    ]}
    assert dependency_depth({"question_id": "a"}, agenda_payload) == 0
    assert dependency_depth({"question_id": "b"}, agenda_payload) == 1
    assert dependency_depth({"question_id": "c"}, agenda_payload) == 2
    assert dependency_depth({"question_id": "d"}, agenda_payload) == 3


def test_dependency_depth_treats_a_dependency_on_an_absent_question_as_depth_zero():
    agenda_payload = {"specs": [
        {"question_id": "a", "dependencies": []},
        {"question_id": "b", "dependencies": ["a", "ghost"]},
    ]}
    assert dependency_depth({"question_id": "b"}, agenda_payload) == 1


# ----------------------------------------------------------------------------
# hkl inverse-map round trip (shared rule)
# ----------------------------------------------------------------------------
def test_hkl_at_inverts_the_dominant_axis():
    st = step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0))
    assert _hkl_at(st, 2.4) == pytest.approx((2.4, 0.0, 0.0))
    st_y = step(q_start=(1.0, -1.0, 0.0), q_end=(1.0, 2.0, 0.0))
    assert _hkl_at(st_y, 0.5) == pytest.approx((1.0, 0.5, 0.0))


def test_hkl_round_trips_through_a_derived_survey_proposal():
    st = step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.3)
    scene = payload(footprints=[fp("e:1:d", 2.05, partition=P1)], observations=[obs("e:1:d")],
                    sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1})
    revision = derive(scene, st)
    dspec = spec_of(revision, "DiscoverRegion", "coverage_complete", partition_id=P1)
    ctx = context_for_revision(scene, st, revision)
    outcome = opening_book_survey(ctx, dspec)
    assert outcome.proposals
    for intent, association in outcome.proposals:
        assert intent.hkl[1:] == (0.0, 0.0)
        assert 2.0 <= intent.hkl[0] <= 3.0
        assert intent.action_class == association.generator == "opening_book_survey"
        assert intent.window_family == "discovery"
        assert intent.evidence_sink == "canonical_master"
        assert intent.evidence_basis_fingerprint == ctx.scene_digest
        assert association.basis_agenda_digest == ctx.basis_agenda_digest


# ----------------------------------------------------------------------------
# 3. presence_probe: tile-midpoint / anchor targeting
# ----------------------------------------------------------------------------
def test_presence_probe_targets_the_declared_anchor():
    scene = payload(footprints=[fp("e:1:d", 2.5, partition=P1)], observations=[obs("e:1:d")],
                    sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1})
    st = step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0))
    revision = derive(scene, st)
    anchored = spec_of(revision, "ConfirmPresence", "structural_presence", anchor=2.0)
    ctx = context_for_revision(scene, st, revision)
    outcome = presence_probe(ctx, anchored)
    intent, association = outcome.proposals[0]
    assert intent.hkl == pytest.approx((2.0, 0.0, 0.0))
    assert intent.energy_lo == pytest.approx(0.0) and intent.energy_hi == pytest.approx(10.0)


def test_presence_probe_targets_the_emergent_tile_midpoint():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")])],
        footprints=[fp("e:1:d", 2.0, partition=P1)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.1), alt([("e:1:d", "B")], 0.9)],
                          unbounded=True, truncated=True)])
    st = step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0))
    revision = derive(scene, st)
    emergent = [s for s in revision.payload["specs"] if s["family"] == "ConfirmPresence"
               and dict(s["policy_provenance"]).get("opened_by") == "emergent"]
    assert len(emergent) == 1
    dspec = emergent[0]
    assert dspec["subject"]["anchor"] is None
    ctx = context_for_revision(scene, st, revision)
    outcome = presence_probe(ctx, dspec)
    intent, association = outcome.proposals[0]
    q_lo, q_hi = dspec["subject"]["region_rlu"]
    assert intent.hkl[0] == pytest.approx((q_lo + q_hi) / 2.0)
    o_lo, o_hi = dspec["subject"]["omega_band_meV"]
    assert intent.energy_lo == pytest.approx(o_lo) and intent.energy_hi == pytest.approx(o_hi)


# ----------------------------------------------------------------------------
# 4. identity_discriminator
# ----------------------------------------------------------------------------
def test_identity_discriminator_is_inapplicable_with_no_representable_identity():
    subject = QuestionSubject(kind="phenomenon", partition_id=P1, region_rlu=(1.9, 2.1),
                              omega_band_meV=(0.0, 10.0))
    classify = make_spec("ClassifyIdentity", "identity_resolved", subject, {"min_mass": 0.9})
    scene = payload(observations=[obs("e:1:d", [cand("A", omegas=(5.0,))])],
                    footprints=[fp("e:1:d", 2.0, partition=P1)],
                    clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])
    ctx = manual_context(scene, step(), [classify], [])
    outcome = identity_discriminator(ctx, classify.to_dict())
    assert outcome.inapplicable_reason == "identity_hypotheses_unavailable"
    assert outcome.exhaustion_clean is False


def test_identity_discriminator_targets_the_highest_posterior_witness_ties_on_scan_ref():
    subject = QuestionSubject(kind="phenomenon", partition_id=P1, region_rlu=(1.9, 2.3),
                              omega_band_meV=(0.0, 10.0))
    classify = make_spec("ClassifyIdentity", "identity_resolved", subject, {"min_mass": 0.9})
    scene = payload(
        observations=[obs("e:2:d", [cand("A", omegas=(5.0,), identity="optical")]),
                     obs("e:1:d", [cand("A", omegas=(5.5,), identity="acoustic")])],
        footprints=[fp("e:2:d", 2.0, partition=P1), fp("e:1:d", 2.1, partition=P1)],
        clusters=[cluster(["e:2:d"], [alt([("e:2:d", "A")], 0.5)], cluster_id="c0"),
                 cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.5)], cluster_id="c1")])
    ctx = manual_context(scene, step(), [classify], [])
    outcome = identity_discriminator(ctx, classify.to_dict())
    intent, association = outcome.proposals[0]
    # Equal posterior on both clusters: the tie breaks to the smaller (scan_ref, label, slot),
    # "e:1:d" < "e:2:d" -- so the acoustic witness at 5.5 wins, not the alphabetically-later one.
    assert intent.window_center_meV == pytest.approx(5.5)
    assert intent.hkl[0] == pytest.approx((1.9 + 2.3) / 2.0)


# ----------------------------------------------------------------------------
# 5. track_bridge_scan
# ----------------------------------------------------------------------------
def _track_spec(endpoint_a, endpoint_b):
    subject = QuestionSubject(kind="track", partition_id=P1, endpoint_a_rlu=endpoint_a,
                              endpoint_b_rlu=endpoint_b)
    return make_spec("ConnectTrack", "track_connected", subject, {"min_mass": 0.9})


def test_track_bridge_scan_targets_the_unmeasured_endpoint_tile():
    track = _track_spec((1.9, 1.95), (2.9, 2.95))
    scene = payload(observations=[obs("e:1:d", [cand("A", omegas=(5.0,))])],
                    footprints=[fp("e:1:d", 1.92, partition=P1)],
                    clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])
    assessment = evaluate_predicate(track, scene)
    assert dict(assessment.notes).get("reason") == "endpoint_unmeasured"
    ctx = manual_context(scene, step(), [track], [assessment])
    outcome = track_bridge_scan(ctx, track.to_dict())
    intent, association = outcome.proposals[0]
    assert intent.hkl[0] == pytest.approx((2.9 + 2.95) / 2.0)


def test_track_bridge_scan_bridges_the_widest_gap_between_disjoint_clusters():
    track = _track_spec((1.9, 1.95), (2.5, 2.55))
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,))]),
                     obs("e:2:d", [cand("A", omegas=(5.5,))]),
                     obs("mid1", [cand("A", omegas=(5.2,))]),
                     obs("mid2", [cand("A", omegas=(5.3,))])],
        footprints=[fp("e:1:d", 1.92, partition=P1), fp("e:2:d", 2.52, partition=P1),
                   fp("mid1", 2.0, partition=P1), fp("mid2", 2.3, partition=P1)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)], cluster_id="c0"),
                 cluster(["e:2:d"], [alt([("e:2:d", "A")], 1.0)], cluster_id="c1"),
                 cluster(["mid1"], [alt([("mid1", "A")], 1.0)], cluster_id="c2"),
                 cluster(["mid2"], [alt([("mid2", "A")], 1.0)], cluster_id="c3")])
    assessment = evaluate_predicate(track, scene)
    assert dict(assessment.notes).get("reason") == "endpoints_in_disjoint_clusters"
    ctx = manual_context(scene, step(), [track], [assessment])
    outcome = track_bridge_scan(ctx, track.to_dict())
    intent, association = outcome.proposals[0]
    # edges [1.95, 2.0, 2.3, 2.5] -> gaps 0.05 / 0.3 / 0.2; widest is (2.0, 2.3) -> node 2.15.
    assert intent.hkl[0] == pytest.approx(2.15)


def test_track_bridge_scan_raises_on_the_unreachable_third_reason():
    track = _track_spec((1.9, 1.95), (2.9, 2.95))
    scene = payload()
    assessment = QuestionAssessment(question_id=track.question_id, status="open",
                                    reasons=(), notes=(("reason", "no_endpoint_tiles_declared"),))
    ctx = manual_context(scene, step(), [track], [assessment])
    with pytest.raises(ValueError, match="no_endpoint_tiles_declared|unreachable|generator defect"):
        track_bridge_scan(ctx, track.to_dict())


# ----------------------------------------------------------------------------
# 6. parameter_refine
# ----------------------------------------------------------------------------
def test_parameter_refine_case_i_targets_the_contributing_interval_at_the_best_center():
    scene = _two_mode_scene(trusted_interval(4.5, 5.5))     # C too wide for tolerance 0.1
    estimate = make_spec(
        "EstimateParameter", "parameter_within_tolerance",
        QuestionSubject(kind="parameter", partition_id=P1, region_rlu=(1.8, 2.2),
                        omega_band_meV=(0.0, 10.0), parameter="omega0"),
        {"tolerance_meV": 0.1, "statistical_floor_meV": 0.01, "parameter_precision_min_mass": 0.9})
    assessment = evaluate_predicate(estimate, scene)
    assert assessment.status == "open"
    assert "within_candidate_uncertainty_unavailable" not in {b.name for b in assessment.blockers}
    assert dict(assessment.notes).get("reason") != "no_support_capable_interval"
    ctx = manual_context(scene, step(), [estimate], [assessment])
    outcome = parameter_refine(ctx, estimate.to_dict())
    intent, association = outcome.proposals[0]
    assert intent.window_center_meV == pytest.approx(5.0)         # A's mode, the only capable one
    assert "case_i:e:1:d/A#0" == association.reason


def test_parameter_refine_case_ii_targets_the_highest_posterior_witness_with_no_trusted_interval():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])
    estimate = make_spec(
        "EstimateParameter", "parameter_within_tolerance",
        QuestionSubject(kind="parameter", partition_id=P1, region_rlu=(1.8, 2.2),
                        omega_band_meV=(0.0, 10.0), parameter="omega0"),
        {"tolerance_meV": 0.5, "statistical_floor_meV": 0.1, "parameter_precision_min_mass": 0.9})
    assessment = evaluate_predicate(estimate, scene)
    assert [b.name for b in assessment.blockers] == ["within_candidate_uncertainty_unavailable"]
    ctx = manual_context(scene, step(), [estimate], [assessment])
    outcome = parameter_refine(ctx, estimate.to_dict())
    intent, association = outcome.proposals[0]
    assert intent.window_center_meV == pytest.approx(5.0)
    assert association.reason.startswith("case_ii:")


def test_parameter_refine_no_support_capable_interval_routes_to_case_ii_deterministically():
    """C4 accept criterion: a scene whose only trusted producer-sound interval is wider than
    2*tolerance still yields a deterministic non-empty proposal, at the case-(ii) arena."""
    scene = one_arena_with_interval(trusted_interval(4.5, 5.5))    # width 1.0 > 2*0.1
    estimate = make_spec(
        "EstimateParameter", "parameter_within_tolerance",
        QuestionSubject(kind="parameter", partition_id=P1, region_rlu=(1.8, 2.2),
                        omega_band_meV=(0.0, 10.0), parameter="omega0"),
        {"tolerance_meV": 0.1, "statistical_floor_meV": 0.01, "parameter_precision_min_mass": 0.9})
    assessment = evaluate_predicate(estimate, scene)
    assert dict(assessment.notes).get("reason") == "no_support_capable_interval"
    ctx = manual_context(scene, step(), [estimate], [assessment])
    outcome = parameter_refine(ctx, estimate.to_dict())
    intent, association = outcome.proposals[0]
    assert intent.window_center_meV == pytest.approx(5.0)
    assert association.reason == "case_ii:e:1:d/A#0"
    # Deterministic under a second call.
    outcome2 = parameter_refine(ctx, estimate.to_dict())
    assert outcome.proposals == outcome2.proposals


def _zc_fixture(anchor=2.0, nearest_tile=(2.9, 3.1)):
    zc_subject = QuestionSubject(kind="parameter", partition_id=P1, region_rlu=(1.9, 2.1),
                                 omega_band_meV=(0.0, 10.0), parameter="zc_endpoint_omega",
                                 anchor=anchor)
    zc = make_spec("EstimateParameter", "zc_linear_compatibility", zc_subject,
                   {"requested_zc_confidence": 0.9})
    confirmed_subject = QuestionSubject(kind="phenomenon", partition_id=P1,
                                        region_rlu=nearest_tile, omega_band_meV=(0.0, 10.0))
    confirmed = make_spec("ConfirmPresence", "structural_presence", confirmed_subject,
                          {"min_mass": 0.9})
    confirmed_assessment = QuestionAssessment(question_id=confirmed.question_id, status="answered",
                                              authority="exact", answer=(("present", True),))
    return zc, confirmed, confirmed_assessment


def test_parameter_refine_zc_bridges_toward_the_nearest_confirmed_tile_never_at_the_anchor():
    zc, confirmed, confirmed_assessment = _zc_fixture(anchor=2.0, nearest_tile=(2.9, 3.1))
    ctx = manual_context(payload(), step(), [zc, confirmed], [confirmed_assessment])
    outcome = parameter_refine(ctx, zc.to_dict())
    intent, association = outcome.proposals[0]
    expected_node = (2.0 + 3.0) / 2.0
    assert intent.hkl[0] == pytest.approx(expected_node)
    assert intent.hkl[0] != pytest.approx(2.0)                    # never AT the anchor


def test_parameter_refine_zc_clean_zero_when_no_adjacent_tile_exists():
    zc = make_spec(
        "EstimateParameter", "zc_linear_compatibility",
        QuestionSubject(kind="parameter", partition_id=P1, region_rlu=(1.9, 2.1),
                        omega_band_meV=(0.0, 10.0), parameter="zc_endpoint_omega", anchor=2.0),
        {"requested_zc_confidence": 0.9})
    ctx = manual_context(payload(), step(), [zc], [])
    outcome = parameter_refine(ctx, zc.to_dict())
    assert outcome.clean_zero_reason == "zc_adjacent_coverage_complete"
    assert outcome.exhaustion_clean is True


def test_parameter_refine_zc_clean_zero_when_the_bridge_node_is_already_measured():
    zc, confirmed, confirmed_assessment = _zc_fixture(anchor=2.0, nearest_tile=(2.9, 3.1))
    already = payload(footprints=[fp("e:already:d", 2.5, partition=P1)])
    ctx = manual_context(already, step(), [zc, confirmed], [confirmed_assessment])
    outcome = parameter_refine(ctx, zc.to_dict())
    assert outcome.clean_zero_reason == "zc_adjacent_coverage_complete"
    assert outcome.exhaustion_clean is True


# ----------------------------------------------------------------------------
# 7. mismatch_probe
# ----------------------------------------------------------------------------
def _mismatch_spec():
    subject = QuestionSubject(kind="model", partition_id=P1, region_rlu=(1.8, 2.2),
                              omega_band_meV=(0.0, 10.0))
    return make_spec("TestModelMismatch", "mismatch_assessed", subject, {"min_mass": 0.9})


def test_mismatch_probe_is_inapplicable_with_no_explicit_competitor():
    mismatch = _mismatch_spec()
    scene = payload(observations=[obs("e:1:d", [cand("A", omegas=(5.0,), identity="acoustic")])],
                    footprints=[fp("e:1:d", 2.0, partition=P1)],
                    clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])
    ctx = manual_context(scene, step(), [mismatch], [])
    outcome = mismatch_probe(ctx, mismatch.to_dict())
    assert outcome.inapplicable_reason == "no_explicit_mismatch_competitor"
    assert outcome.exhaustion_clean is False


def test_mismatch_probe_targets_the_smallest_coordinate_arena_then_scan_ref():
    mismatch = _mismatch_spec()
    # "e:2:d" sits at the SMALLER coordinate despite sorting later alphabetically than "e:1:d" --
    # the rule is (coordinate, scan_ref), coordinate first.
    scene = payload(
        observations=[obs("e:2:d", [cand("A", omegas=(5.0,), identity="acoustic"),
                                    cand("U", omegas=(5.1,), identity="unknown")]),
                     obs("e:1:d", [cand("A", omegas=(5.2,), identity="acoustic"),
                                  cand("U", omegas=(5.3,), identity="unknown")])],
        footprints=[fp("e:2:d", 1.9, partition=P1), fp("e:1:d", 2.1, partition=P1)],
        clusters=[cluster(["e:2:d"], [alt([("e:2:d", "U")], 1.0)], cluster_id="c0"),
                 cluster(["e:1:d"], [alt([("e:1:d", "U")], 1.0)], cluster_id="c1")])
    ctx = manual_context(scene, step(), [mismatch], [])
    outcome = mismatch_probe(ctx, mismatch.to_dict())
    intent, association = outcome.proposals[0]
    assert intent.hkl[0] == pytest.approx(1.9)
    assert intent.window_center_meV == pytest.approx(5.1)
    assert "e:2:d" in association.reason


# ----------------------------------------------------------------------------
# Cold start: opening_book_survey / exposure_fill check partition_id BEFORE calling the book
# ----------------------------------------------------------------------------
def test_cold_start_program_scoped_subject_is_inapplicable_and_the_book_is_never_called(
        monkeypatch):
    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("propose_survey must never be called for a program-scoped subject")
    monkeypatch.setattr(gen, "propose_survey", _must_not_be_called)

    scene = payload()                                    # unmeasured workspace: no footprints
    st = step()
    revision = derive(scene, st)
    dspec = spec_of(revision, "DiscoverRegion", "coverage_complete")
    assert dspec["subject"]["partition_id"] is None
    verify_spec = spec_of(revision, "VerifyCompleteness", "completeness_relative")
    assert verify_spec["subject"]["partition_id"] is None
    ctx = context_for_revision(scene, st, revision)

    survey_outcome = opening_book_survey(ctx, dspec)
    assert survey_outcome.inapplicable_reason == "program_scoped_subject_unfanned"
    assert survey_outcome.exhaustion_clean is False

    fill_outcome = exposure_fill(ctx, verify_spec)
    assert fill_outcome.inapplicable_reason == "program_scoped_subject_unfanned"
    assert fill_outcome.exhaustion_clean is False


# ----------------------------------------------------------------------------
# Kind -> generator attribution: one node owing both a band gap and an exposure deficit
# ----------------------------------------------------------------------------
def test_opening_book_survey_and_exposure_fill_attribute_correctly_at_one_node():
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(0.2, 0.0, 0.0), spacing=0.25, domain=(0.0, 8.0),
             exposure_floor=2.0)
    scene = payload(
        footprints=[fp("e:1:d", 0.0, bands=((0.0, 3.0),), exposure=0.5, partition=P1),
                   fp("e:2:d", 0.2, bands=((0.0, 8.0),), exposure=5.0, partition=P1)],
        observations=[obs("e:1:d"), obs("e:2:d")])
    discover = make_spec(
        "DiscoverRegion", "coverage_complete",
        QuestionSubject(kind="region", partition_id=P1, region_rlu=(0.0, 0.2),
                        omega_band_meV=(0.0, 8.0)),
        {"maximum_initial_spacing_rlu": 0.25, "omega_domain_meV": [0.0, 8.0]})
    verify = make_spec(
        "VerifyCompleteness", "completeness_relative",
        QuestionSubject(kind="campaign", partition_id=P1, region_rlu=(0.0, 0.2),
                        omega_band_meV=(0.0, 8.0)),
        {"min_effective_exposure": 2.0})
    ctx = manual_context(scene, st, [discover, verify], [])

    survey_outcome = opening_book_survey(ctx, discover.to_dict())
    fill_outcome = exposure_fill(ctx, verify.to_dict())

    assert len(survey_outcome.proposals) == 1
    intent, association = survey_outcome.proposals[0]
    assert intent.hkl[0] == pytest.approx(0.0)
    assert association.generator == "opening_book_survey"
    assert association.reason == "opening_book:missing_omega_band"

    assert len(fill_outcome.proposals) == 1
    intent2, association2 = fill_outcome.proposals[0]
    assert intent2.hkl[0] == pytest.approx(0.0)
    assert association2.generator == "exposure_fill"
    assert "deficit" in association2.reason

    # opening_book_survey never emits an underexposed_cell action; exposure_fill never emits a
    # q/omega gap action -- attribution stays disjoint even at a single node owing both.
    assert all(a.generator == "opening_book_survey" for _, a in survey_outcome.proposals)
    assert all(a.generator == "exposure_fill" for _, a in fill_outcome.proposals)
