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
    identity_hypothesis_classes, mass_interval)
from isar.scene.questions import PredicateDeclaration, QuestionSpec, QuestionSubject

P1 = "a" * 64


# ----------------------------------------------------------------------------
# Payload builders (persisted shapes)
# ----------------------------------------------------------------------------
def cand(label, omegas=(), identity=None, usable=True, mode_intervals=None):
    components = [{"kind": "mode", "slot": i} for i in range(len(omegas))]
    if mode_intervals is None:
        mode_intervals = [None] * len(omegas)
    return {"label": label, "omegas": list(omegas), "identity": identity,
            "usable": usable, "components": components, "n_modes": len(omegas),
            "mode_intervals": list(mode_intervals)}


def trusted_interval(low, high, *, nominal_level=0.68):
    """A writer-byte-shaped ``ModeInterval`` carrying a producer-SOUND verdict (P0-C:
    ``scan_delivery._producer_soundness``) — trusted regardless of which label delivery
    eventually picks."""
    return {"low": low, "center": (low + high) / 2.0, "high": high,
            "nominal_level": nominal_level,
            "interval_kind": "parametric_bootstrap_percentile",
            "inference_paradigm": "frequentist_sampling_distribution",
            "source_quantity": "freq_interval",
            "authority": {"authority_basis": "producer_sound"},
            "conditioning_fingerprint": "c" * 64}


def offer_interval(low, high, *, nominal_level=0.68):
    """A writer-byte-shaped ``ModeInterval`` carrying a producer-UNSOUND verdict (P0-C) — never
    trusted by the scene layer, regardless of which label delivery eventually picks."""
    return {"low": low, "center": (low + high) / 2.0, "high": high,
            "nominal_level": nominal_level,
            "interval_kind": "parametric_bootstrap_percentile",
            "inference_paradigm": "frequentist_sampling_distribution",
            "source_quantity": "freq_interval",
            "authority": {"authority_basis": "producer_unsound",
                         "reasons": ("convergence_unrecorded",)},
            "conditioning_fingerprint": "c" * 64}


def obs(scan_ref, cands, count_trust="trusted", sample="s1", epoch=1, conditions=(),
        best_label=None):
    return {"scan_ref": scan_ref, "candidates": list(cands), "count_trust": count_trust,
            "sample": sample, "lattice_epoch": epoch,
            "conditions": [list(pair) for pair in conditions], "best_label": best_label}


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


def test_representability_ignores_an_identity_row_no_retained_alternative_assigns():
    """SCENE-4.2a P1-G falsified counterexample: candidate A carries an explicit identity, but
    every retained alternative assigns B — A is pruned/never-retained. Representability must be
    derived from RETAINED alternatives, not raw candidate rows, or a dead identity row opens
    ClassifyIdentity forever."""
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,), identity="acoustic"), cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "B")], 1.0)])])
    subject = spec("identity_resolved", {"min_mass": 0.9}, family="ClassifyIdentity").subject
    assert identity_hypothesis_classes(subject, scene) == frozenset()
    a = evaluate_predicate(spec("identity_resolved", {"min_mass": 0.9},
                                family="ClassifyIdentity"), scene)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["identity_hypotheses_unavailable"]


def test_representability_ignores_an_out_of_band_identity():
    """A retained alternative assigns the identity-bearing row, but its mode sits outside the
    subject's omega band — not placeable IN the claimed support, so it cannot represent it."""
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(50.0,), identity="acoustic")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0)])])
    subject = spec("identity_resolved", {"min_mass": 0.9}, family="ClassifyIdentity").subject
    assert identity_hypothesis_classes(subject, scene) == frozenset()


def test_representability_holds_for_an_in_band_retained_identity():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,), identity="acoustic"),
                                    cand("C", omegas=(5.2,))])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "C")], 0.05)])])
    subject = spec("identity_resolved", {"min_mass": 0.9}, family="ClassifyIdentity").subject
    assert identity_hypothesis_classes(subject, scene) == frozenset({"acoustic"})


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
def test_no_trusted_interval_anywhere_stays_open_not_closed():
    """No ``mode_intervals`` on this fixture's candidates at all (SCENE-4.2a A2): with no
    within-candidate uncertainty in the matched partition, closure is unavailable regardless of
    how confident the between-candidate posterior is."""
    scene = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(
        spec("parameter_within_tolerance", {"tolerance_meV": 0.5,
                                            "statistical_floor_meV": 0.1,
                                            "parameter_precision_min_mass": 0.9},
             family="EstimateParameter", subject_kind="parameter", parameter="omega0"), scene)
    assert a.status == "open"
    assert [b.name for b in a.blockers] == ["within_candidate_uncertainty_unavailable"]


def test_a_tolerance_below_the_floor_records_the_second_blocker():
    scene = one_arena([alt([("e:1:d", "A")], 1.0)])
    a = evaluate_predicate(
        spec("parameter_within_tolerance", {"tolerance_meV": 0.05,
                                            "statistical_floor_meV": 0.1,
                                            "parameter_precision_min_mass": 0.9},
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
# EstimateParameter closure (SCENE-4.2a A2): the A1 mode_intervals close the never-closes ruling
# ----------------------------------------------------------------------------
def estimate_spec(tolerance=0.1, floor=0.01, min_mass=0.9):
    return spec("parameter_within_tolerance",
               {"tolerance_meV": tolerance, "statistical_floor_meV": floor,
                "parameter_precision_min_mass": min_mass},
               family="EstimateParameter", subject_kind="parameter", parameter="omega0")


def one_arena_with_interval(mode_interval, omega=5.0, posterior=1.0):
    return payload(
        observations=[obs("e:1:d", [cand("A", omegas=(omega,), mode_intervals=[mode_interval]),
                                    cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], posterior),
                                      alt([("e:1:d", "B")], 1.0 - posterior)])])


def test_positive_closure_from_one_trusted_interval():
    scene = one_arena_with_interval(trusted_interval(4.9, 5.1))
    a = evaluate_predicate(estimate_spec(tolerance=0.15), scene)
    assert a.status == "answered"
    ans = dict(a.answer)
    # center_meV is the envelope's midpoint, not the endpoint the search picked (P1-J): a single
    # symmetric interval's window midpoint equals the interval midpoint regardless of tolerance.
    assert ans["center_meV"] == pytest.approx(5.0)
    lo, hi = ans["envelope_meV"]
    assert lo == pytest.approx(4.95) and hi == pytest.approx(5.05)
    assert ans["nominal_level"] == 0.68
    assert ans["contributing"] == (("e:1:d", "A", 0),)
    assert a.mass_interval == (1.0, 1.0)


def test_center_is_the_envelope_midpoint_regardless_of_tolerance():
    """P1-J: for a single symmetric interval the window midpoint equals the interval midpoint no
    matter how tolerance widens or narrows the envelope around it."""
    scene = one_arena_with_interval(trusted_interval(4.9, 5.1))
    wide = evaluate_predicate(estimate_spec(tolerance=0.5), scene)
    wans = dict(wide.answer)
    assert wans["center_meV"] == pytest.approx(5.0)
    lo, hi = wans["envelope_meV"]
    assert lo == pytest.approx(4.6) and hi == pytest.approx(5.4)

    tight = evaluate_predicate(estimate_spec(tolerance=0.3), scene)
    tans = dict(tight.answer)
    assert tans["center_meV"] == pytest.approx(5.0)
    lo, hi = tans["envelope_meV"]
    assert lo == pytest.approx(4.8) and hi == pytest.approx(5.2)


def _two_mode_scene(c_interval):
    return payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.95, 5.05)]),
            cand("C", omegas=(5.02,), mode_intervals=[c_interval])])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.6),
                                      alt([("e:1:d", "C")], 0.4)])])


def test_widening_one_contributing_interval_reopens_below_threshold():
    tight = evaluate_predicate(estimate_spec(tolerance=0.1),
                               _two_mode_scene(trusted_interval(4.97, 5.07)))
    assert tight.status == "answered"
    # The search picks c=4.97 (smallest full-mass center), but the published center is the
    # midpoint of the supported-center envelope [4.97, 5.05], not the search endpoint itself.
    assert dict(tight.answer)["center_meV"] == pytest.approx(5.01)

    # C's interval widened past what tolerance 0.1 can support (full width 1.0 > 2*tol) — it
    # drops out of the capable set entirely, and A's mass alone (0.6) can no longer clear 0.9.
    widened = evaluate_predicate(estimate_spec(tolerance=0.1),
                                 _two_mode_scene(trusted_interval(4.5, 5.5)))
    assert widened.status == "open"
    assert widened.mass_interval == (pytest.approx(0.6), pytest.approx(0.6))


def test_a_non_winner_survivor_with_a_sound_record_closes_where_delivery_picked_another_model():
    """P0-C inverted counterexample (owner ruling 2026-08-14): delivery selection is not interval
    authority. Candidate "A" carries the sound record and closes the estimate even though the
    observation's ``best_label`` names "B" — a DIFFERENT candidate that has no interval at all.
    Trust follows the writer's per-candidate producer-soundness verdict alone."""
    scene = payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.9, 5.1)]),
            cand("B", omegas=(5.0,), mode_intervals=[None])],
            best_label="B")],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.6),
                                      alt([("e:1:d", "B")], 0.4)])])
    a = evaluate_predicate(estimate_spec(tolerance=0.15, min_mass=0.5), scene)
    assert a.status == "answered"
    assert dict(a.answer)["contributing"] == (("e:1:d", "A", 0),)


def test_offer_only_survivor_fails_trust_gate_and_counts_adversarially():
    scene = payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.95, 5.05)]),
            cand("R", omegas=(5.0,), mode_intervals=[offer_interval(4.95, 5.05)])])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.7),
                                      alt([("e:1:d", "R")], 0.3)])])
    a = evaluate_predicate(estimate_spec(tolerance=0.1, min_mass=0.5), scene)
    assert a.status == "answered"
    assert a.mass_interval == (pytest.approx(0.7), pytest.approx(0.7))  # R's mass excluded


def test_two_narrow_separated_candidates_do_not_close():
    """The owner ruling's counterexample: A 5.0+/-0.1, B 7.0+/-0.1, 0.5/0.5 — narrow individually,
    but disagreeing, so no single center can retain more than half the mass."""
    scene = payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.9, 5.1)]),
            cand("B", omegas=(7.0,), mode_intervals=[trusted_interval(6.9, 7.1)])])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.5),
                                      alt([("e:1:d", "B")], 0.5)])])
    a = evaluate_predicate(estimate_spec(tolerance=0.1), scene)
    assert a.status == "open"
    assert a.mass_interval[0] == pytest.approx(0.5)


def test_mixed_nominal_level_refuses_closure_with_a_note():
    scene = _two_mode_scene(trusted_interval(4.97, 5.07, nominal_level=0.9))
    a = evaluate_predicate(estimate_spec(tolerance=0.1), scene)
    assert a.status == "open"
    kind, paradigm = "parametric_bootstrap_percentile", "frequentist_sampling_distribution"
    assert dict(a.notes)["mixed_interval_semantics"] == (
        (kind, paradigm, 0.68), (kind, paradigm, 0.9))


def test_mixed_interval_kind_refuses_closure_even_at_matching_level():
    """P1-H: the closure gate is the exact (interval_kind, inference_paradigm, nominal_level)
    triple, not just nominal_level -- two intervals agreeing on level but disagreeing on kind
    still refuse to combine."""
    scene = _two_mode_scene({**trusted_interval(4.97, 5.07), "interval_kind": "likelihood_profile"})
    a = evaluate_predicate(estimate_spec(tolerance=0.1), scene)
    assert a.status == "open"
    assert "mixed_interval_semantics" in dict(a.notes)


def test_identical_interval_semantics_close_and_persist_the_triple():
    scene = _two_mode_scene(trusted_interval(4.97, 5.07))
    a = evaluate_predicate(estimate_spec(tolerance=0.1), scene)
    assert a.status == "answered"
    ans = dict(a.answer)
    assert ans["interval_kind"] == "parametric_bootstrap_percentile"
    assert ans["inference_paradigm"] == "frequentist_sampling_distribution"
    assert ans["nominal_level"] == 0.68


def test_zero_posterior_alternative_neither_blocks_nor_joins_closure():
    """P1-I: a discordant-level alternative carrying ZERO retained posterior must not appear in
    contributors/envelope and must not trigger mixed_interval_semantics -- metadata is derived
    from the same positive-posterior support the mass calculation used."""
    scene = payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.9, 5.1)]),
            cand("Z", omegas=(5.0,),
                mode_intervals=[trusted_interval(4.9, 5.1, nominal_level=0.9)])])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 1.0),
                                      alt([("e:1:d", "Z")], 0.0)])])
    a = evaluate_predicate(estimate_spec(tolerance=0.15), scene)
    assert a.status == "answered"
    ans = dict(a.answer)
    assert ans["contributing"] == (("e:1:d", "A", 0),)
    assert ans["nominal_level"] == 0.68
    assert "mixed_interval_semantics" not in dict(a.notes)


def test_mass_exactly_at_min_mass_closes():
    """P2-K: closure clears on >= min_mass, matching decide()'s own threshold semantics -- a
    parameter_precision_min_mass of 1.0 must be satisfiable at certainty."""
    scene = one_arena_with_interval(trusted_interval(4.9, 5.1), posterior=1.0)
    a = evaluate_predicate(estimate_spec(tolerance=0.15, min_mass=1.0), scene)
    assert a.status == "answered"
    assert a.mass_interval == (1.0, 1.0)


def test_tie_break_is_replay_identical_under_reversed_alternative_order():
    def scene(alternatives):
        return payload(
            observations=[obs("e:1:d", [
                cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.95, 5.05)]),
                cand("B", omegas=(5.0,), mode_intervals=[trusted_interval(4.95, 5.05)])])],
            footprints=[fp("e:1:d", 2.0)],
            clusters=[cluster(["e:1:d"], list(alternatives))])

    forward = [alt([("e:1:d", "A")], 0.5), alt([("e:1:d", "B")], 0.5)]
    a1 = evaluate_predicate(estimate_spec(tolerance=0.1, min_mass=0.5), scene(forward))
    a2 = evaluate_predicate(estimate_spec(tolerance=0.1, min_mass=0.5),
                            scene(list(reversed(forward))))
    assert a1.to_dict() == a2.to_dict()


def test_half_width_semantics_pinned_at_the_boundary():
    at_boundary = one_arena_with_interval(trusted_interval(4.9, 5.1))   # width 0.2 == 2*tol
    a = evaluate_predicate(estimate_spec(tolerance=0.1, min_mass=0.5), at_boundary)
    assert a.status == "answered"

    just_wider = one_arena_with_interval(trusted_interval(4.89, 5.11))  # width 0.22 > 2*tol
    b = evaluate_predicate(estimate_spec(tolerance=0.1, min_mass=0.5), just_wider)
    assert b.status == "open"
    assert dict(b.notes).get("reason") == "no_support_capable_interval"


def test_untrusted_unpopulated_and_ambiguous_mass_all_count_adversarially():
    ambiguous_row = {
        "label": "M", "omegas": [5.0, 5.02], "identity": None, "usable": True,
        "components": [{"kind": "mode", "slot": 0}, {"kind": "mode", "slot": 1}], "n_modes": 2,
        "mode_intervals": [trusted_interval(4.95, 5.05), trusted_interval(4.97, 5.07)]}
    scene = payload(
        observations=[obs("e:1:d", [
            cand("A", omegas=(5.0,), mode_intervals=[trusted_interval(4.95, 5.05)]),
            cand("U", omegas=(5.0,), mode_intervals=[None]),
            cand("R", omegas=(5.0,), mode_intervals=[offer_interval(4.95, 5.05)]),
            ambiguous_row])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.4),
                                      alt([("e:1:d", "U")], 0.2),
                                      alt([("e:1:d", "R")], 0.2),
                                      alt([("e:1:d", "M")], 0.2)])])
    a = evaluate_predicate(estimate_spec(tolerance=0.1, min_mass=0.3), scene)
    assert a.status == "answered"
    assert a.mass_interval == (pytest.approx(0.4), pytest.approx(0.4))


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
