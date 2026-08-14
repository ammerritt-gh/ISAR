"""The opening book: deterministic survey proposals in the sparse-coverage regime (SCENE-4.2a A5).

Every case pins the book's proposals against the REAL predicate they serve
(``isar.scene.predicates``): applying a proposal's measurement must flip exactly the closure it
targets, never the other one. Payload builders mirror ``tests/scene/test_predicates.py``.
"""
from __future__ import annotations

import copy

import pytest

from isar.analyze.catalogue import DEFAULT_PROFILE
from isar.drive.acquisition.opening_book import SurveyProposal, _zc_anchors, propose_survey
from isar.schema.question_policy import resolve_policy
from isar.scene.predicates import evaluate_predicate
from isar.scene.questions import PredicateDeclaration, QuestionSpec, QuestionSubject

P1 = "a" * 64


# ----------------------------------------------------------------------------
# Payload builders
# ----------------------------------------------------------------------------
def fp(scan_ref, coord, bands=((0.0, 8.0),), exposure=5.0, partition=P1):
    return {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition,
            "effective_exposure": exposure,
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands]}


def scene(footprints=(), sample_knowledge=None):
    return {"observations": [], "footprints": list(footprints), "clusters": [],
            "non_scoring": [], "sample_knowledge": sample_knowledge or {},
            "condition_t_round": None}


def step(q_start=(1.9, 0.0, 0.0), q_end=(2.2, 0.0, 0.0), spacing=0.05,
         domain=(0.0, 8.0), survey_order="placement_informed", exposure_floor=1.0,
         catalogue_profile=DEFAULT_PROFILE):
    policy = resolve_policy(user_ask={"survey_order": survey_order,
                                      "min_effective_exposure": exposure_floor})
    return {"region": {"kind": "path", "q_start": list(q_start), "q_end": list(q_end),
                       "maximum_initial_spacing_rlu": spacing},
            "energy": {"intrinsic_omega_domain_meV": list(domain)},
            "catalogue_profile": catalogue_profile,
            "resolved_policy": policy}


def coverage_spec(step_payload, region):
    return QuestionSpec(
        family="DiscoverRegion", claim_kind="coverage_complete",
        subject=QuestionSubject(kind="region", partition_id=P1, region_rlu=region,
                                omega_band_meV=None),
        predicate=PredicateDeclaration(
            kind="coverage_complete", version=1,
            parameters={"maximum_initial_spacing_rlu": step_payload["region"]["maximum_initial_spacing_rlu"],
                       "omega_domain_meV": step_payload["energy"]["intrinsic_omega_domain_meV"]}))


def completeness_spec():
    return QuestionSpec(
        family="VerifyCompleteness", claim_kind="completeness_relative",
        subject=QuestionSubject(kind="campaign", partition_id=P1),
        predicate=PredicateDeclaration(kind="completeness_relative", version=1,
                                       parameters={"min_effective_exposure": 1.0}))


def apply_proposals(sc, proposals, *, full_bands=(0.0, 8.0), exposure=5.0):
    """Measure every proposal: q proposals become new fully-covered footprints; omega proposals
    fill their named gap on the existing footprint; exposure proposals top the node up."""
    footprints = {f["scan_ref"]: f for f in sc["footprints"]}
    for i, p in enumerate(proposals):
        if p.kind == "missing_q_node":
            ref = f"opened:{i}"
            footprints[ref] = fp(ref, p.axis_coordinate, bands=((full_bands[0], full_bands[1]),),
                                 exposure=exposure)
        elif p.kind == "missing_omega_band":
            ref = next(r for r, f in footprints.items() if f["axis_coordinate"] == p.axis_coordinate)
            footprints[ref]["bands"].append(
                {"lo": p.detail["omega_lo"], "hi": p.detail["omega_hi"], "sides": "stokes"})
        elif p.kind == "underexposed_cell":
            ref = next(r for r, f in footprints.items() if f["axis_coordinate"] == p.axis_coordinate)
            footprints[ref]["effective_exposure"] += p.detail["deficit"]
    return scene(footprints=list(footprints.values()), sample_knowledge=sc.get("sample_knowledge"))


# ----------------------------------------------------------------------------
# Cases
# ----------------------------------------------------------------------------
def test_sparse_anchors_close_every_gap():
    # Deliberately not exact multiples of the spacing (see module docstring: node coordinates
    # land on the raw even-division grid with no float-boundary safety margin).
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(2.03, 0.0, 0.0), spacing=0.103)
    sc = scene(footprints=[fp("e:1:d", 0.91), fp("e:2:d", 1.34)])
    proposals = propose_survey(sc, st)
    assert all(p.kind == "missing_q_node" for p in proposals)
    assert len(proposals) >= 15

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.0, 2.03)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


def test_q_complete_omega_incomplete_only_band_proposals():
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(0.2, 0.0, 0.0), spacing=0.25, domain=(0.0, 8.0))
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 3.0),)),
                           fp("e:2:d", 0.2, bands=((0.0, 3.0),))])
    proposals = propose_survey(sc, st)
    assert proposals and all(p.kind == "missing_omega_band" for p in proposals)

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.0, 0.2)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}


def test_qomega_complete_but_underexposed_only_exposure_proposals():
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(0.2, 0.0, 0.0), spacing=0.25, domain=(0.0, 8.0),
             exposure_floor=2.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, exposure=0.5), fp("e:2:d", 0.2, exposure=0.5)])
    proposals = propose_survey(sc, st)
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
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(0.2, 0.0, 0.0), spacing=0.25, domain=(0.0, 8.0),
             exposure_floor=2.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 3.0),), exposure=0.5),
                           fp("e:2:d", 0.2, bands=((0.0, 8.0),), exposure=5.0)])
    proposals = propose_survey(sc, st)
    kinds_at_zero = sorted(p.kind for p in proposals if p.axis_coordinate == 0.0)
    assert kinds_at_zero == ["missing_omega_band", "underexposed_cell"]
    assert [p.kind for p in proposals if p.axis_coordinate == 0.2] == []

    after = apply_proposals(sc, proposals, exposure=0.0)
    coverage = evaluate_predicate(coverage_spec(st, (0.0, 0.2)), after)
    assert coverage.status == "answered" and dict(coverage.answer) == {"complete": True}
    completeness = evaluate_predicate(completeness_spec(), after)
    assert completeness.status == "answered" and dict(completeness.answer) == {"complete": True}


def test_both_answered_scene_yields_zero_proposals():
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(0.2, 0.0, 0.0), spacing=0.25, domain=(0.0, 8.0),
             exposure_floor=1.0)
    sc = scene(footprints=[fp("e:1:d", 0.0, bands=((0.0, 8.0),), exposure=5.0),
                           fp("e:2:d", 0.2, bands=((0.0, 8.0),), exposure=5.0)])
    assert propose_survey(sc, st) == ()


def test_uniform_vs_placement_informed_same_set_different_order():
    st_uniform = step(q_start=(0.0, 0.0, 0.0), q_end=(2.0, 0.0, 0.0), spacing=0.3,
                      survey_order="uniform")
    st_placed = step(q_start=(0.0, 0.0, 0.0), q_end=(2.0, 0.0, 0.0), spacing=0.3,
                     survey_order="placement_informed")
    sc = scene(footprints=[fp("e:1:d", 0.05)],
               sample_knowledge={"zone_period": 1.0})

    uniform = propose_survey(sc, st_uniform)
    placed = propose_survey(sc, st_placed)
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
    assert propose_survey(sc, st_placed) == placed
    # Reversed footprint iteration order gives the identical (already-sorted) output.
    sc_rev = scene(footprints=list(reversed(sc["footprints"])),
                   sample_knowledge=sc["sample_knowledge"])
    assert propose_survey(sc_rev, st_placed) == placed


def test_iterative_remainder_only():
    st = step(q_start=(0.0, 0.0, 0.0), q_end=(1.0, 0.0, 0.0), spacing=0.3)
    sc = scene(footprints=[])
    first = propose_survey(sc, st)
    assert len(first) > 1

    # Measure the first k as answered nodes; the next call proposes exactly the remainder.
    k = 1
    measured = [fp(f"opened:{i}", p.axis_coordinate) for i, p in enumerate(first[:k])]
    sc2 = scene(footprints=measured)
    remainder = propose_survey(sc2, st)
    expected = tuple(p for p in first if p.axis_coordinate not in
                     {m.axis_coordinate for m in first[:k]})
    assert {p.axis_coordinate for p in remainder} == {p.axis_coordinate for p in expected}


def test_grid_anchored_to_endpoints_not_a_global_grid():
    # Endpoints deliberately off any 0.1-aligned global grid.
    st = step(q_start=(0.13, 0.0, 0.0), q_end=(0.13 + 3 * 0.07, 0.0, 0.0), spacing=0.07)
    sc = scene(footprints=[])
    proposals = propose_survey(sc, st)
    coords = sorted(p.axis_coordinate for p in proposals)
    expected = [round(0.13 + i * 0.07, 9) for i in (1, 2)]
    assert coords == expected

    after = apply_proposals(sc, proposals)
    a = evaluate_predicate(coverage_spec(st, (0.13, 0.13 + 3 * 0.07)), after)
    assert a.status == "answered" and dict(a.answer) == {"complete": True}
