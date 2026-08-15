"""The opening book: deterministic survey proposals in the sparse-coverage regime (SCENE-4.2a A5,
PR 32 wave W5: scoped, clipped, seeded, unrounded).

Every case pins the book's proposals against the REAL predicate they serve
(``isar.scene.predicates``): applying a proposal's measurement must flip exactly the closure it
targets, never the other one. Payload builders mirror ``tests/scene/test_predicates.py``.
"""
from __future__ import annotations

import pytest

from isar.analyze.catalogue import DEFAULT_PROFILE
from isar.drive.acquisition.opening_book import SurveyProposal, _zc_anchors, propose_survey
from isar.schema.question_policy import resolve_policy
from isar.scene.predicates import evaluate_predicate
from isar.scene.questions import PredicateDeclaration, QuestionSpec, QuestionSubject

P1 = "a" * 64
P2 = "b" * 64
EPOCH = "epoch:1"


# ----------------------------------------------------------------------------
# Payload builders
# ----------------------------------------------------------------------------
def fp(scan_ref, coord, bands=((0.0, 8.0),), exposure=5.0, partition=P1):
    return {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition,
            "effective_exposure": exposure,
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands]}


def obs(scan_ref, lattice_epoch=EPOCH):
    return {"scan_ref": scan_ref, "lattice_epoch": lattice_epoch}


def scene(footprints=(), sample_knowledge=None, observations=None):
    if observations is None:
        # Default: every footprint gets a matching observation declaring EPOCH, so the epoch
        # rule (frame compatibility) is satisfied unless a test overrides it.
        observations = [obs(f["scan_ref"]) for f in footprints]
    return {"observations": list(observations), "footprints": list(footprints), "clusters": [],
            "non_scoring": [], "sample_knowledge": sample_knowledge or {},
            "condition_t_round": None}


def step(spacing=0.05, domain=(0.0, 8.0), survey_order="placement_informed", exposure_floor=1.0,
         catalogue_profile=DEFAULT_PROFILE):
    policy = resolve_policy(user_ask={"survey_order": survey_order,
                                      "min_effective_exposure": exposure_floor})
    return {"region": {"kind": "path", "maximum_initial_spacing_rlu": spacing},
            "energy": {"intrinsic_omega_domain_meV": list(domain)},
            "catalogue_profile": catalogue_profile,
            "resolved_policy": policy}


def subject(region, partition=P1):
    return QuestionSubject(kind="region", partition_id=partition, region_rlu=region,
                           omega_band_meV=None)


def coverage_spec(step_payload, region, partition=P1):
    return QuestionSpec(
        family="DiscoverRegion", claim_kind="coverage_complete",
        subject=subject(region, partition),
        predicate=PredicateDeclaration(
            kind="coverage_complete", version=1,
            parameters={"maximum_initial_spacing_rlu":
                           step_payload["region"]["maximum_initial_spacing_rlu"],
                       "omega_domain_meV": step_payload["energy"]["intrinsic_omega_domain_meV"]}))


def completeness_spec(partition=P1):
    return QuestionSpec(
        family="VerifyCompleteness", claim_kind="completeness_relative",
        subject=QuestionSubject(kind="campaign", partition_id=partition),
        predicate=PredicateDeclaration(kind="completeness_relative", version=1,
                                       parameters={"min_effective_exposure": 1.0}))


def call(sc, st, region, partition=P1):
    return propose_survey(sc, st, subject(region, partition))


def apply_proposals(sc, proposals, *, full_bands=(0.0, 8.0), exposure=5.0, partition=P1):
    """Measure every proposal: q proposals become new fully-covered footprints; omega proposals
    fill their named gap on the existing footprint; exposure proposals top the node up."""
    footprints = {f["scan_ref"]: f for f in sc["footprints"]}
    observations = list(sc.get("observations") or ())
    for i, p in enumerate(proposals):
        if p.kind == "missing_q_node":
            ref = f"opened:{i}"
            footprints[ref] = fp(ref, p.axis_coordinate, bands=((full_bands[0], full_bands[1]),),
                                 exposure=exposure, partition=partition)
            observations.append(obs(ref))
        elif p.kind == "missing_omega_band":
            ref = next(r for r, f in footprints.items() if f["axis_coordinate"] == p.axis_coordinate)
            footprints[ref]["bands"].append(
                {"lo": p.detail["omega_lo"], "hi": p.detail["omega_hi"], "sides": "stokes"})
        elif p.kind == "underexposed_cell":
            ref = next(r for r, f in footprints.items() if f["axis_coordinate"] == p.axis_coordinate)
            footprints[ref]["effective_exposure"] += p.detail["deficit"]
    return scene(footprints=list(footprints.values()), sample_knowledge=sc.get("sample_knowledge"),
                observations=observations)


# ----------------------------------------------------------------------------
# Cases
# ----------------------------------------------------------------------------
def test_sparse_anchors_close_every_gap():
    # Deliberately not exact multiples of the spacing (see module docstring: node coordinates
    # land on the raw even-division grid with no float-boundary safety margin).
    st = step(spacing=0.103)
    sc = scene(footprints=[fp("e:1:d", 0.91), fp("e:2:d", 1.34)])
    proposals = call(sc, st, (0.0, 2.03))
    assert all(p.kind == "missing_q_node" for p in proposals)
    assert len(proposals) >= 15

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.0, 2.03)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


def test_q_complete_omega_incomplete_only_band_proposals():
    st = step(spacing=0.25, domain=(0.0, 8.0))
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 3.0),)),
                           fp("e:2:d", 0.2, bands=((0.0, 3.0),))])
    proposals = call(sc, st, (0.0, 0.2))
    assert proposals and all(p.kind == "missing_omega_band" for p in proposals)

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.0, 0.2)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


def test_qomega_complete_but_underexposed_only_exposure_proposals():
    st = step(spacing=0.25, domain=(0.0, 8.0), exposure_floor=2.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, exposure=0.5), fp("e:2:d", 0.2, exposure=0.5)])
    proposals = call(sc, st, (0.0, 0.2))
    assert proposals and all(p.kind == "underexposed_cell" for p in proposals)
    assert {round(p.detail["deficit"], 6) for p in proposals} == {1.5}

    # Never reported as an unsurveyed coverage gap.
    before = evaluate_predicate(coverage_spec(st, (0.0, 0.2)), sc)
    assert before.status == "answered" and dict(before.answer) == {"complete": True}

    # And DOES flip VerifyCompleteness's exposure floor.
    open_before = evaluate_predicate(completeness_spec(), sc)
    assert open_before.status == "open"
    after = apply_proposals(sc, proposals, exposure=0.0)
    open_after = evaluate_predicate(completeness_spec(), after)
    assert open_after.status == "answered" and dict(open_after.answer) == {"complete": True}


def test_dual_deficit_node_gets_both_omega_and_exposure_proposals():
    """A node with BOTH an ω gap and low exposure is two independent problems, not one — the
    book must not silently drop the second and cost the campaign an extra survey round."""
    st = step(spacing=0.25, domain=(0.0, 8.0), exposure_floor=2.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 3.0),), exposure=0.5),
                           fp("e:2:d", 0.2, bands=((0.0, 8.0),), exposure=5.0)])
    proposals = call(sc, st, (0.0, 0.2))
    kinds_at_zero = sorted(p.kind for p in proposals if p.axis_coordinate == 0.0)
    assert kinds_at_zero == ["missing_omega_band", "underexposed_cell"]
    assert [p.kind for p in proposals if p.axis_coordinate == 0.2] == []

    after = apply_proposals(sc, proposals, exposure=0.0)
    coverage = evaluate_predicate(coverage_spec(st, (0.0, 0.2)), after)
    assert coverage.status == "answered" and dict(coverage.answer) == {"complete": True}
    completeness = evaluate_predicate(completeness_spec(), after)
    assert completeness.status == "answered" and dict(completeness.answer) == {"complete": True}


def test_both_answered_scene_yields_zero_proposals():
    st = step(spacing=0.25, domain=(0.0, 8.0), exposure_floor=1.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 8.0),), exposure=5.0),
                           fp("e:2:d", 0.2, bands=((0.0, 8.0),), exposure=5.0)])
    assert call(sc, st, (0.0, 0.2)) == ()


def test_uniform_vs_placement_informed_same_set_different_order():
    st_uniform = step(spacing=0.3, survey_order="uniform")
    st_placed = step(spacing=0.3, survey_order="placement_informed")
    sc = scene(footprints=[fp("e:1:d", 0.05)],
               sample_knowledge={"zone_period": 1.0, "lattice_epoch": EPOCH})

    uniform = call(sc, st_uniform, (0.0, 2.0))
    placed = call(sc, st_placed, (0.0, 2.0))
    assert {(p.kind, p.axis_coordinate) for p in uniform} == \
        {(p.kind, p.axis_coordinate) for p in placed}
    assert [p.axis_coordinate for p in uniform] != [p.axis_coordinate for p in placed]
    # zone-centre anchors at 0.0 and 1.0 and 2.0: placement-informed pulls the node nearest an
    # anchor to the front, uniform is pure ascending geometric order.
    assert [p.axis_coordinate for p in uniform] == sorted(p.axis_coordinate for p in uniform)

    # Boundary anchors (period divides the region exactly, landing anchors exactly ON both
    # edges) are included by exact "<=" filtering, no epsilon either way.
    assert _zc_anchors(sc, (0.0, 2.0)) == (0.0, 1.0, 2.0)
    # A period NOT dividing the region: the region edges themselves are not multiples of the
    # period and must not be pulled in by a rounding epsilon.
    assert _zc_anchors(sc, (0.3, 1.7)) == (1.0,)

    # Deterministic across repeated calls.
    assert call(sc, st_placed, (0.0, 2.0)) == placed
    # Reversed footprint iteration order gives the identical (already-sorted) output.
    sc_rev = scene(footprints=list(reversed(sc["footprints"])),
                   sample_knowledge=sc["sample_knowledge"], observations=sc["observations"])
    assert call(sc_rev, st_placed, (0.0, 2.0)) == placed


def test_zone_period_declared_without_matching_epoch_is_inert():
    """A declared zone_period with NO compatible partition epoch (missing observation, or a
    mismatched one) must not silently drive placement-informed ordering — it stays uniform
    (fails closed to zero anchor score)."""
    st = step(spacing=0.3, survey_order="placement_informed")

    # No observations at all: epoch-of-partition is "missing" for every partition.
    sc_no_obs = {"observations": [], "footprints": [fp("e:1:d", 0.05)], "clusters": [],
                "non_scoring": [], "sample_knowledge": {"zone_period": 1.0, "lattice_epoch": EPOCH},
                "condition_t_round": None}
    placed = call(sc_no_obs, st, (0.0, 2.0))
    assert [p.axis_coordinate for p in placed] == sorted(p.axis_coordinate for p in placed)

    # Mismatched epoch: the partition's observation declares a DIFFERENT epoch than the
    # declared sample_knowledge.
    sc_mismatch = scene(footprints=[fp("e:1:d", 0.05)],
                        sample_knowledge={"zone_period": 1.0, "lattice_epoch": EPOCH},
                        observations=[obs("e:1:d", lattice_epoch="epoch:OTHER")])
    placed2 = call(sc_mismatch, st, (0.0, 2.0))
    assert [p.axis_coordinate for p in placed2] == sorted(p.axis_coordinate for p in placed2)


def test_iterative_remainder_only():
    st = step(spacing=0.3)
    sc = scene(footprints=[])
    first = call(sc, st, (0.0, 1.0))
    assert len(first) > 1

    # Measure the first k as answered nodes; the next call proposes exactly the remainder.
    k = 1
    measured = [fp(f"opened:{i}", p.axis_coordinate) for i, p in enumerate(first[:k])]
    sc2 = scene(footprints=measured)
    remainder = call(sc2, st, (0.0, 1.0))
    expected = tuple(p for p in first if p.axis_coordinate not in
                     {m.axis_coordinate for m in first[:k]})
    assert {p.axis_coordinate for p in remainder} == {p.axis_coordinate for p in expected}


def test_grid_anchored_to_endpoints_not_a_global_grid():
    # Endpoints deliberately off any 0.1-aligned global grid.
    st = step(spacing=0.07)
    sc = scene(footprints=[])
    lo, hi = 0.13, 0.13 + 3 * 0.07
    proposals = call(sc, st, (lo, hi))
    coords = sorted(p.axis_coordinate for p in proposals)
    expected = [0.13 + i * 0.07 for i in (1, 2)]
    assert coords == pytest.approx(expected)

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (lo, hi)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


# ----------------------------------------------------------------------------
# P0-A: subject scoping
# ----------------------------------------------------------------------------
def test_footprint_in_a_different_partition_does_not_close_this_subjects_gap():
    st = step(spacing=0.3)
    sc = scene(footprints=[fp("other:1", 1.0, partition=P2)])
    proposals = call(sc, st, (0.0, 2.0), partition=P1)
    # The foreign-partition footprint must not act as a surveyed node for P1's gap check: the
    # region is treated as ONE unbroken gap (evenly spaced fill), never split around q=1.0.
    assert all(p.kind == "missing_q_node" for p in proposals)
    coords = sorted(p.axis_coordinate for p in proposals)
    assert coords == pytest.approx([2.0 * i / 7 for i in range(1, 7)])


def test_out_of_region_footprint_yields_no_out_of_region_nodes():
    st = step(spacing=0.3)
    sc = scene(footprints=[fp("e:1:d", 100.0, partition=P1)])
    proposals = call(sc, st, (0.0, 1.0), partition=P1)
    assert proposals  # zero matches -> the P1-E seed, still in-region
    assert all(0.0 <= p.axis_coordinate <= 1.0 for p in proposals)


def test_context_probe_in_another_partition_never_satisfies_this_path():
    st = step(spacing=2.0)
    # A footprint that WOULD close the gap if it were workspace-wide, but lives in another
    # partition — must not satisfy P1's DiscoverRegion subject.
    sc = scene(footprints=[fp("probe", 0.9, partition=P2)])
    proposals = call(sc, st, (0.0, 1.0), partition=P1)
    # With no in-partition footprints and region width <= spacing, this is the P1-E seed path
    # (exactly one proposal, at the plain region midpoint — no trace of the foreign-partition
    # coordinate).
    assert len(proposals) == 1
    assert proposals[0].axis_coordinate == pytest.approx(0.5)


def test_omega_and_exposure_walks_fire_only_on_matched_footprints():
    st = step(spacing=2.0, domain=(0.0, 8.0), exposure_floor=2.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 8.0),), exposure=5.0, partition=P1),
                           fp("other:1", 0.5, bands=((0.0, 3.0),), exposure=0.1, partition=P2)])
    proposals = call(sc, st, (0.0, 1.0), partition=P1)
    # The P2 footprint's band gap and low exposure must not surface under P1's subject.
    assert not any(p.axis_coordinate == 0.5 for p in proposals)


# ----------------------------------------------------------------------------
# P1-D: bands clipped to the domain
# ----------------------------------------------------------------------------
def test_band_outside_domain_proposes_the_in_domain_gap_only():
    st = step(spacing=1.0, domain=(0.0, 10.0))
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((12.0, 15.0),))])
    proposals = call(sc, st, (0.0, 0.0001))
    band_proposals = [p for p in proposals if p.kind == "missing_omega_band"]
    assert len(band_proposals) == 1
    gap = band_proposals[0].detail
    assert 0.0 <= gap["omega_lo"] <= 10.0
    assert 0.0 <= gap["omega_hi"] <= 10.0


def test_wholly_external_band_is_a_no_op_for_the_omega_walk():
    st = step(spacing=1.0, domain=(0.0, 10.0))
    sc_with_band = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 10.0), (12.0, 15.0)))])
    sc_without = scene(footprints=[fp("e:2:d", 0.0, bands=((0.0, 10.0),))])
    p_with = call(sc_with_band, st, (0.0, 0.0001))
    p_without = call(sc_without, st, (0.0, 0.0001))
    assert [(p.kind, p.detail) for p in p_with] == [(p.kind, p.detail) for p in p_without]


@pytest.mark.parametrize("domain,band", [
    ((0.0, 10.0), (12.0, 15.0)),
    ((0.0, 10.0), (-5.0, -1.0)),
    ((2.0, 10.0), (0.0, 3.0)),
    ((0.0, 10.0), (9.0, 20.0)),
])
def test_gap_endpoints_always_within_the_domain(domain, band):
    st = step(spacing=1.0, domain=domain)
    coord = (domain[0] + domain[1]) / 2
    sc = scene(footprints=[fp("e:1:d", coord, bands=(band,))])
    proposals = call(sc, st, (coord, coord + 1e-6))
    for p in proposals:
        if p.kind == "missing_omega_band":
            assert domain[0] <= p.detail["omega_lo"] <= domain[1]
            assert domain[0] <= p.detail["omega_hi"] <= domain[1]


# ----------------------------------------------------------------------------
# P1-E: zero-footprint seed
# ----------------------------------------------------------------------------
def test_region_width_at_or_under_spacing_with_zero_footprints_seeds_one_node():
    st = step(spacing=0.3)
    sc = scene(footprints=[])
    # Equality case: region width EXACTLY equals spacing.
    proposals = call(sc, st, (0.0, 0.3))
    assert len(proposals) == 1
    assert proposals[0].kind == "missing_q_node"
    assert proposals[0].axis_coordinate == pytest.approx(0.15)

    # Strictly-under case.
    proposals_under = call(sc, st, (0.0, 0.1))
    assert len(proposals_under) == 1
    assert proposals_under[0].axis_coordinate == pytest.approx(0.05)


def test_seed_prefers_the_nearest_declared_compatible_anchor():
    # region (0.4, 1.4), width 1.0 <= spacing 1.5 (so the ordinary walk proposes nothing and
    # the seed path fires): midpoint 0.9, only in-region zone-centre anchor is 1.0. An
    # OUT-OF-region footprint in the same partition (epoch-matched) establishes that partition's
    # lattice epoch is compatible, without itself matching this subject's region.
    st = step(spacing=1.5)
    sc = scene(footprints=[fp("elsewhere", 5.0, partition=P1)],
               sample_knowledge={"zone_period": 1.0, "lattice_epoch": EPOCH})
    proposals = call(sc, st, (0.4, 1.4))
    assert len(proposals) == 1
    assert proposals[0].axis_coordinate == 1.0


def test_seed_is_deterministic_under_payload_reordering():
    st = step(spacing=1.5)
    sc = scene(footprints=[fp("elsewhere", 5.0, partition=P1)],
               sample_knowledge={"zone_period": 1.0, "lattice_epoch": EPOCH})
    a = call(sc, st, (0.4, 1.4))
    sc2 = {**sc, "sample_knowledge": dict(reversed(list(sc["sample_knowledge"].items())))}
    b = call(sc2, st, (0.4, 1.4))
    assert a == b


def test_seed_unblocks_no_surveyed_nodes_in_region():
    st = step(spacing=0.3)
    sc = scene(footprints=[])
    proposals = call(sc, st, (0.0, 0.1))
    before = evaluate_predicate(coverage_spec(st, (0.0, 0.1)), sc)
    assert before.status == "open"
    assert dict(before.notes).get("reason") == "no_surveyed_nodes_in_region"

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.0, 0.1)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


# ----------------------------------------------------------------------------
# O: no reintroduced overflow from rounding
# ----------------------------------------------------------------------------
def test_gap_of_k_spacing_plus_epsilon_leaves_every_subgap_within_spacing():
    spacing = 0.1
    for k, eps in [(3, 1e-9), (5, 1e-7), (2, 1e-6)]:
        st = step(spacing=spacing)
        sc = scene(footprints=[])
        gap = k * spacing + eps
        proposals = call(sc, st, (0.0, gap))
        edges = [0.0, *sorted(p.axis_coordinate for p in proposals), gap]
        for a, b in zip(edges, edges[1:]):
            assert b - a <= spacing + 1e-9

# ----------------------------------------------------------------------------
# PR 4.2b C0: program-scoped subjects are refused at the front door
# ----------------------------------------------------------------------------
def test_program_scoped_subject_refused_loudly():
    """A partition_id=None subject can never match physical evidence (the agenda fans
    program-scoped questions out to per-partition children before any generator runs), so one
    reaching the book is an upstream contract violation — refused by name, never a blind seed
    and never a workspace-wide gap fill."""
    st = step()
    sc = scene(footprints=[fp("e:1:d", 0.5)])
    program_scoped = QuestionSubject(kind="region", partition_id=None, region_rlu=(0.0, 1.0),
                                     omega_band_meV=None)
    with pytest.raises(ValueError, match="program-scoped.*fan-out|fan-out.*program-scoped"):
        propose_survey(sc, st, program_scoped)
    # The dict form (driver convenience) is refused identically.
    with pytest.raises(ValueError, match="program-scoped"):
        propose_survey(sc, st, program_scoped.to_dict())


def test_partitioned_subject_behaviour_unchanged_by_the_guard():
    """Regression: the guard is a front-door refusal only — a partition-scoped subject's
    proposal set is byte-identical to the pre-guard behaviour (the epoch rule still decides
    anchor scoring, exactly as before)."""
    st = step(spacing=0.3)
    sc = scene(footprints=[fp("e:1:d", 0.05)],
               sample_knowledge={"zone_period": 1.0, "lattice_epoch": EPOCH})
    proposals = call(sc, st, (0.0, 2.0))
    assert proposals and all(p.kind == "missing_q_node" for p in proposals)
    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.0, 2.0)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}
