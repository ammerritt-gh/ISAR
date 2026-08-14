"""Predicate evaluators: authority, bound arithmetic, honest refusal (SCENE-4 PR 4.1, slice 4).

Every case kills a named overclaim: agreement among point estimates read as precision, missing
identity semantics read as a physical unknown, a fitless arena read as mismatch evidence,
per-cluster threshold success read as global confidence, a huge truncation bound overflowing
instead of going honestly vacuous. Payloads are hand-built persisted shapes.
"""
import math

import pytest

from isar.scene.predicates import (
    cluster_authority, combine_existential, combine_universal, decide, evaluate_predicate,
    mass_interval)
from isar.scene.questions import PredicateDeclaration, QuestionSpec, QuestionSubject

P1 = "a" * 64


# ----------------------------------------------------------------------------
# Payload builders (persisted shapes)
# ----------------------------------------------------------------------------
def cand(label, omegas=(), identity=None, usable=True):
    components = [{"kind": "mode", "slot": i} for i in range(len(omegas))]
    return {"label": label, "omegas": list(omegas), "identity": identity,
            "usable": usable, "components": components, "n_modes": len(omegas)}


def obs(scan_ref, cands, count_trust="trusted", sample="s1", epoch=1, conditions=()):
    return {"scan_ref": scan_ref, "candidates": list(cands), "count_trust": count_trust,
            "sample": sample, "lattice_epoch": epoch,
            "conditions": [list(pair) for pair in conditions]}


def fp(scan_ref, coord, bands=((0.0, 10.0),), exposure=5.0, partition=P1):
    return {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition,
            "effective_exposure": exposure,
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands]}


def alt(assignment, posterior, links=()):
    return {"assignment": [list(pair) for pair in assignment], "posterior": posterior,
            "links": [list(pair) for pair in links]}


def cluster(scan_refs, alternatives, *, truncated=False, unbounded=False, bound=None,
            linkage_truncated=False, cluster_id="c0"):
    return {"cluster_id": cluster_id, "scan_refs": list(scan_refs),
            "alternatives": list(alternatives), "truncated": truncated,
            "truncation_unbounded": unbounded, "log_discarded_mass_bound": bound,
            "linkage_truncated": linkage_truncated}


def payload(observations=(), footprints=(), clusters=(), non_scoring=(),
            sample_knowledge=None, t_round=None):
    return {"observations": list(observations), "footprints": list(footprints),
            "clusters": list(clusters), "non_scoring": list(non_scoring),
            "sample_knowledge": sample_knowledge or {}, "condition_t_round": t_round}


def spec(kind, params, family="ConfirmPresence", requirement="exact_or_bounded",
         subject_kind="phenomenon", **subject):
    base = dict(kind=subject_kind, partition_id=P1, region_rlu=(1.8, 2.2),
                omega_band_meV=(0.0, 10.0))
    base.update(subject)
    return QuestionSpec(family=family, claim_kind=kind,
                        subject=QuestionSubject(**base),
                        predicate=PredicateDeclaration(kind=kind, version=1, parameters=params,
                                                       authority_requirement=requirement))


def one_arena(alternatives, **cluster_kw):
    """One trusted arena at 2.0 with candidates A (mode at 5 meV) and B (phonon-less)."""
    return payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], alternatives, **cluster_kw)])


# ----------------------------------------------------------------------------
# Bound arithmetic
# ----------------------------------------------------------------------------
def test_the_adversarial_interval_prices_omitted_mass_against_the_claim():
    # r = 0.5 relative to leader p* = 0.6: q = 0.3, m = 0.6 -> [0.6/1.3, 0.9/1.3].
    log_q = math.log(0.5) + math.log(0.6)
    lo, hi = mass_interval(0.6, log_q)
    assert lo == pytest.approx(0.6 / 1.3) and hi == pytest.approx(0.9 / 1.3)
    assert mass_interval(0.6, None) == (0.6, 0.6)


def test_a_huge_finite_bound_goes_vacuous_without_overflow():
    assert mass_interval(0.99, 800.0) == (0.0, 1.0)     # no OverflowError, no raise


def test_cluster_authority_lanes():
    exact = cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])
    assert cluster_authority(exact) == ("exact", None)
    bounded = cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.6), alt([("e:1:d", "B")], 0.4)],
                      truncated=True, bound=math.log(0.5))
    lane, log_q = cluster_authority(bounded)
    assert lane == "bounded" and log_q == pytest.approx(math.log(0.5) + math.log(0.6))
    assert cluster_authority(cluster([], [], truncated=True, unbounded=True))[0] == "unresolved"
    # Truncated with a lost bound fails closed, as does capped linkage enumeration.
    assert cluster_authority(cluster([], [], truncated=True, bound=None))[0] == "unresolved"
    assert cluster_authority(cluster([], [], linkage_truncated=True))[0] == "unresolved"


def test_twenty_confident_tiles_do_not_make_a_confident_region():
    interval = combine_universal([(0.95, 0.95)] * 20)
    assert interval[0] == pytest.approx(0.95 ** 20) and interval[0] < 0.9
    assert decide(interval, 0.9) != "yes"


def test_several_subthreshold_clusters_can_jointly_carry_an_existential_claim():
    interval = combine_existential([(0.5, 0.5), (0.5, 0.5)])
    assert interval[0] == pytest.approx(0.75)
    assert decide(interval, 0.7) == "yes"
    # One exact cluster at 0.5 is a decided NO at 0.7 — the mass really is below threshold.
    assert decide(combine_existential([(0.5, 0.5)]), 0.7) == "no"


# ----------------------------------------------------------------------------
# ConfirmPresence (structural, no identity needed)
# ----------------------------------------------------------------------------
def test_presence_answers_yes_from_structural_mode_mass():
    scene = one_arena([alt([("e:1:d", "A")], 0.95), alt([("e:1:d", "B")], 0.05)])
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.9}), scene)
    assert a.status == "answered" and dict(a.answer) == {"present": True}
    assert a.authority == "exact" and a.mass_interval == (0.95, 0.95)


def test_presence_answers_no_when_null_candidates_carry_the_mass():
    scene = one_arena([alt([("e:1:d", "A")], 0.02), alt([("e:1:d", "B")], 0.98)])
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.9}), scene)
    assert a.status == "answered" and dict(a.answer) == {"present": False}


def test_a_vacuous_bound_answers_nothing():
    scene = one_arena([alt([("e:1:d", "A")], 0.95), alt([("e:1:d", "B")], 0.05)],
                      truncated=True, bound=800.0)
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.9}), scene)
    assert a.status == "open" and a.mass_interval == (0.0, 1.0)
    assert dict(a.notes).get("bound_vacuous_for_threshold")


def test_a_bounded_cluster_answers_only_when_the_lower_end_clears():
    # q = r*p* = 0.02*0.95 = 0.019: lower 0.95/1.019 ~ 0.932 >= 0.9 -> still yes.
    scene = one_arena([alt([("e:1:d", "A")], 0.95), alt([("e:1:d", "B")], 0.05)],
                      truncated=True, bound=math.log(0.02))
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.9}), scene)
    assert a.status == "answered" and a.authority == "bounded"
    assert a.mass_interval[0] == pytest.approx(0.95 / (1 + 0.02 * 0.95))


def test_an_unresolved_cluster_cannot_answer():
    scene = one_arena([alt([("e:1:d", "A")], 1.0)], truncated=True, unbounded=True)
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.9}), scene)
    assert a.status == "open" and a.authority == "unresolved"
    assert [b.name for b in a.blockers] == ["posterior_truncation_unresolved"]


def test_an_exact_requirement_downgrades_bounded_clusters():
    scene = one_arena([alt([("e:1:d", "A")], 0.95), alt([("e:1:d", "B")], 0.05)],
                      truncated=True, bound=math.log(0.02))
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.9},
                                requirement="exact"), scene)
    assert a.status == "open" and a.authority == "unresolved"


def test_two_subthreshold_clusters_jointly_confirm_presence():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")]),
                      obs("e:2:d", [cand("A", omegas=(5.5,)), cand("B")])],
        footprints=[fp("e:1:d", 1.9), fp("e:2:d", 2.1)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.5), alt([("e:1:d", "B")], 0.5)],
                          cluster_id="c0"),
                  cluster(["e:2:d"], [alt([("e:2:d", "A")], 0.5), alt([("e:2:d", "B")], 0.5)],
                          cluster_id="c1")])
    a = evaluate_predicate(spec("structural_presence", {"min_mass": 0.7}), scene)
    assert a.status == "answered" and dict(a.answer) == {"present": True}
    assert a.mass_interval[0] == pytest.approx(0.75)


# ----------------------------------------------------------------------------
# ClassifyIdentity / TestModelMismatch (no manufactured answers)
# ----------------------------------------------------------------------------
def test_all_identity_none_leaves_classification_open_not_unknown():
    scene = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(spec("identity_resolved", {"min_mass": 0.9},
                                family="ClassifyIdentity"), scene)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["identity_hypotheses_unavailable"]


def test_identity_answers_from_explicit_alternatives_and_tracks_unclassified_mass():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,), identity="acoustic"),
                                    cand("C", omegas=(5.2,))])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "C")], 0.05)])])
    a = evaluate_predicate(spec("identity_resolved", {"min_mass": 0.9},
                                family="ClassifyIdentity"), scene)
    assert a.status == "answered" and dict(a.answer) == {"identity": "acoustic"}
    assert dict(a.notes)["unclassified_mass"] == pytest.approx(0.05)


def test_a_fitless_arena_is_not_mismatch_evidence():
    scene = payload(observations=[obs("e:1:d", [])], footprints=[fp("e:1:d", 2.0)],
                    clusters=[])
    a = evaluate_predicate(spec("mismatch_assessed", {"min_mass": 0.9},
                                family="TestModelMismatch"), scene)
    assert a.status == "open" and dict(a.notes)["reason"] == "no_matched_evidence"


def test_mismatch_needs_an_explicit_unknown_competitor():
    without = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(spec("mismatch_assessed", {"min_mass": 0.9},
                                family="TestModelMismatch"), without)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["identity_hypotheses_unavailable"]

    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,), identity="acoustic"),
                                    cand("U", omegas=(5.1,), identity="unknown")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "U")], 0.95),
                                      alt([("e:1:d", "A")], 0.05)])])
    a = evaluate_predicate(spec("mismatch_assessed", {"min_mass": 0.9},
                                family="TestModelMismatch"), scene)
    assert a.status == "answered" and dict(a.answer) == {"mismatch_present": True}


# ----------------------------------------------------------------------------
# EstimateParameter (never closes) + ZC compatibility
# ----------------------------------------------------------------------------
def test_identical_candidate_means_do_not_close_estimate_parameter():
    scene = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(
        spec("parameter_within_tolerance", {"tolerance_meV": 0.5,
                                            "statistical_floor_meV": 0.1},
             family="EstimateParameter", subject_kind="parameter", parameter="omega0"), scene)
    assert a.status == "open"                        # zero spread still cannot close
    assert dict(a.notes)["mixture_spread_meV"] == 0.0
    assert [b.name for b in a.blockers] == ["within_candidate_uncertainty_unavailable"]


def test_a_tolerance_below_the_floor_records_the_second_blocker():
    scene = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(
        spec("parameter_within_tolerance", {"tolerance_meV": 0.05,
                                            "statistical_floor_meV": 0.1},
             family="EstimateParameter", subject_kind="parameter", parameter="omega0"), scene)
    assert {b.name for b in a.blockers} == {"within_candidate_uncertainty_unavailable",
                                            "tolerance_below_representable_precision"}


def zc_spec(**kw):
    return spec("zc_linear_compatibility", {"requested_zc_confidence": 0.9},
                family="EstimateParameter", subject_kind="parameter", parameter="zc_endpoint_omega",
                anchor=2.0, **kw)


def test_zc_compatibility_answers_without_a_numeric_confidence():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(0.5,)), cand("B")])],
        footprints=[fp("e:1:d", 2.1)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "B")], 0.05)])],
        sample_knowledge={"dispersion_slope_max": 10.0})
    a = evaluate_predicate(zc_spec(), scene)
    # omega 0.5 at distance 0.1 under slope 10: interval [0, 1.5] contains 0 -> compatible.
    assert a.status == "answered" and dict(a.answer) == {"zc_compatibility": "compatible"}
    assert dict(a.notes)["maximum_justifiable_confidence"] == 0.0
    lo, hi = dict(a.notes)["extrapolated_interval_meV"]
    assert lo == 0.0 and hi == pytest.approx(1.5)


def test_zc_incompatible_when_the_declared_slope_cannot_reach_zero():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")])],
        footprints=[fp("e:1:d", 2.1)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.98),
                                      alt([("e:1:d", "B")], 0.02)])],
        sample_knowledge={"dispersion_slope_max": 10.0})
    a = evaluate_predicate(zc_spec(), scene)
    # omega 5.0, s*d = 1.0: interval [4.0, 6.0] excludes 0 for the dominant mass.
    assert a.status == "answered" and dict(a.answer) == {"zc_compatibility": "incompatible"}


def test_zc_without_declared_dispersion_support_stays_open():
    scene = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(zc_spec(), scene)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["dispersion_support_undeclared"]


# ----------------------------------------------------------------------------
# ConnectTrack
# ----------------------------------------------------------------------------
def track_spec():
    return spec("track_connected", {"min_mass": 0.9}, family="ConnectTrack", subject_kind="track",
                region_rlu=None, omega_band_meV=None,
                endpoint_a_rlu=(1.9, 1.95), endpoint_b_rlu=(2.1, 2.15))


def test_track_connects_through_linked_components_not_component_ids():
    link = (("e:1:d#A#0", "e:2:d#A#0"),)
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,))]),
                      obs("e:2:d", [cand("A", omegas=(5.5,))])],
        footprints=[fp("e:1:d", 1.92), fp("e:2:d", 2.12)],
        clusters=[cluster(["e:1:d", "e:2:d"],
                          [alt([("e:1:d", "A"), ("e:2:d", "A")], 0.95, links=link),
                           alt([("e:1:d", "A"), ("e:2:d", "A")], 0.05)])])
    a = evaluate_predicate(track_spec(), scene)
    assert a.status == "answered" and dict(a.answer) == {"connected": True}


def test_endpoints_in_disjoint_clusters_stay_open_not_disconnected():
    """Absent adjacency may be an unsurveyed middle — never charged as absence."""
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,))]),
                      obs("e:2:d", [cand("A", omegas=(5.5,))])],
        footprints=[fp("e:1:d", 1.92), fp("e:2:d", 2.12)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)], cluster_id="c0"),
                  cluster(["e:2:d"], [alt([("e:2:d", "A")], 1.0)], cluster_id="c1")])
    a = evaluate_predicate(track_spec(), scene)
    assert a.status == "open"
    assert dict(a.notes)["reason"] == "endpoints_in_disjoint_clusters"


# ----------------------------------------------------------------------------
# DiscoverRegion coverage + VerifyCompleteness
# ----------------------------------------------------------------------------
def coverage_spec():
    return spec("coverage_complete", {"maximum_initial_spacing_rlu": 0.15,
                                      "omega_domain_meV": (0.0, 8.0)},
                family="DiscoverRegion", subject_kind="region", region_rlu=(1.9, 2.2),
                omega_band_meV=None, catalogue_class=None)


def test_coverage_answers_when_no_gap_exceeds_the_frozen_spacing():
    scene = payload(footprints=[fp("e:1:d", 1.95), fp("e:2:d", 2.05), fp("e:3:d", 2.15)])
    a = evaluate_predicate(coverage_spec(), scene)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


def test_coverage_stays_open_on_a_gap_and_blocks_on_quarantine():
    scene = payload(footprints=[fp("e:1:d", 1.95), fp("e:3:d", 2.15)],
                    non_scoring=[{"scan_ref": "e:2:d", "reason": "count_untrusted"}])
    a = evaluate_predicate(coverage_spec(), scene)
    assert a.status == "open"
    assert dict(a.notes)["widest_unsurveyed_gap_rlu"] == pytest.approx(0.2)
    assert [b.name for b in a.blockers] == ["quarantined_scan"]


def completeness_spec(dependencies=(), partition=P1):
    return QuestionSpec(
        family="VerifyCompleteness", claim_kind="completeness_relative",
        subject=QuestionSubject(kind="campaign", partition_id=partition,
                                frame=(("catalogue_version", "v1"),)),
        predicate=PredicateDeclaration(kind="completeness_relative", version=1,
                                       parameters={"min_effective_exposure": 1.0}),
        dependencies=dependencies)


def clean_scene():
    return payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])


def test_completeness_answers_only_when_everything_relevant_is_closed_and_clean():
    from isar.scene.questions import QuestionAssessment
    dep = spec("structural_presence", {"min_mass": 0.9})
    answered = QuestionAssessment(question_id=dep.question_id, status="answered",
                                  answer=(("present", True),))
    c = completeness_spec(dependencies=(dep.question_id,))
    a = evaluate_predicate(c, clean_scene(),
                           prior_assessments={dep.question_id: answered})
    assert a.status == "answered" and dict(a.answer) == {"complete": True}
    # The same dependency open -> completeness open, the ruling-1 consequence at this level.
    open_dep = QuestionAssessment(question_id=dep.question_id, status="open")
    a = evaluate_predicate(c, clean_scene(),
                           prior_assessments={dep.question_id: open_dep})
    assert a.status == "open"
    assert dep.question_id in dict(a.notes)["open_dependencies"]


def test_completeness_stays_open_on_uninterpreted_or_untrusted_or_underexposed_evidence():
    fitless = clean_scene()
    fitless["observations"].append(obs("e:2:d", []))
    fitless["footprints"].append(fp("e:2:d", 2.1))
    a = evaluate_predicate(completeness_spec(), fitless)
    assert a.status == "open" and "e:2:d" in dict(a.notes)["uninterpreted_measurements"]

    untrusted = clean_scene()
    untrusted["non_scoring"] = [{"scan_ref": "e:9:d", "reason": "count_untrusted"}]
    assert evaluate_predicate(completeness_spec(), untrusted).status == "open"

    dim = clean_scene()
    dim["footprints"][0]["effective_exposure"] = 0.1
    a = evaluate_predicate(completeness_spec(), dim)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["detection_authority_unavailable"]


def test_a_boundary_scan_belongs_to_exactly_one_tile():
    """PR #31 finding J: closed-interval tile evaluation put a boundary scan in both
    adjacent endpoint tiles, so a component there could satisfy track_connected without
    traversing anything. Tiles are half-open cells; a boundary scan is in ONE of them."""
    from isar.scene.predicates import tile_contains
    assert tile_contains((2.0, 2.05), 2.0)
    assert not tile_contains((2.0, 2.05), 2.05)

    scene = payload(observations=[obs("e:1:d", [cand("A", omegas=(5.0,))])],
                    footprints=[fp("e:1:d", 2.05)],
                    clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])
    boundary = spec("track_connected", {"min_mass": 0.9}, family="ConnectTrack",
                    subject_kind="track", region_rlu=None, omega_band_meV=None,
                    endpoint_a_rlu=(2.0, 2.05), endpoint_b_rlu=(2.05, 2.10))
    a = evaluate_predicate(boundary, scene)
    assert a.status == "open" and dict(a.notes)["reason"] == "endpoint_unmeasured"


def test_completeness_blocks_only_its_own_condition_partition():
    """PR #31 finding K: a quarantined arena at one temperature must not block another
    condition's completeness — attribution uses the builder's own partition identity."""
    from isar.scene.model import partition_id, partition_key
    t_round = 1.0
    cond300 = (("temperature_K", 300.0),)
    cond100 = (("temperature_K", 100.0),)
    pid300 = partition_id(partition_key("s1", cond300, 1, "any", t_round))

    def scene(quarantined_conditions):
        return payload(
            observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")],
                              conditions=cond300),
                          obs("e:9:d", [], count_trust="excluded",
                              conditions=quarantined_conditions)],
            footprints=[fp("e:1:d", 2.0, partition=pid300)],
            clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])],
            non_scoring=[{"scan_ref": "e:9:d", "reason": "count_untrusted"}],
            t_round=t_round)

    other_condition = evaluate_predicate(completeness_spec(partition=pid300),
                                         scene(cond100))
    assert other_condition.status == "answered"
    same_condition = evaluate_predicate(completeness_spec(partition=pid300),
                                        scene(cond300))
    assert same_condition.status == "open"
    assert "e:9:d" in dict(same_condition.notes)["non_scoring_arenas"]


def test_completeness_ignores_leader_only_visibility_annotations():
    """Visibility rows annotate the leading alternative only; a universal claim may not read
    them as posterior consensus — the evaluator answers from stored alternatives alone."""
    scene = clean_scene()
    with_visibility = clean_scene()
    with_visibility["clusters"][0]["visibility"] = [
        {"scan_ref": "e:1:d", "state": "searched_no_signal"}]
    assert (evaluate_predicate(completeness_spec(), scene).to_dict()
            == evaluate_predicate(completeness_spec(), with_visibility).to_dict())


def test_completeness_blocks_on_an_unresolved_cluster():
    scene = clean_scene()
    scene["clusters"][0]["truncation_unbounded"] = True
    scene["clusters"][0]["truncated"] = True
    a = evaluate_predicate(completeness_spec(), scene)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["posterior_truncation_unresolved"]


def test_an_unknown_predicate_kind_refuses_loudly():
    with pytest.raises(ValueError, match="no evaluator for predicate kind"):
        evaluate_predicate(spec("structural_presence", {"min_mass": 0.9})
                           .__class__(family="ConfirmPresence", claim_kind="x",
                                      subject=QuestionSubject(kind="phenomenon",
                                                              partition_id=P1),
                                      predicate=PredicateDeclaration(kind="not_a_kind",
                                                                     version=1)),
                           payload())
