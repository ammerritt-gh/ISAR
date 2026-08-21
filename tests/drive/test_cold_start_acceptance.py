"""The SCENE-4.4 cold-start acceptance script (plan's "Cold-start ruling" items 3-4, D6).

The owner's walkthrough for the DEFAULT phonon catalogue profile, pinned as one acceptance
script rather than re-derived from scratch: a fresh workspace's first proposal hunts the acoustic
branch just off the nearest declared zone centre; persistent absence there pivots the hunt to
higher energy at that same zone-centre-adjacent node (the zone-centre placement rule's OTHER
class, optic, has no band restriction and the highest remaining order weight — SCENE-4
`isar/schema/placement.py` `_DEFAULT_PLACEMENT_RULES`); once a node is measured, the walk expands
OUTWARD from it rather than jumping. A fourth case pins the required degradation: blank knowledge
must never raise, only fall back to the uniform survey.

This is deliberately built at the SAME layer ``tests/drive/test_opening_book.py`` and
``tests/drive/test_cold_start.py`` already pin (``propose_survey`` / ``plan_next_action`` /
``DeclaredPartition``), not a new mechanism: SCENE-4 has no branch-labeled (acoustic-vs-optic)
placement state machine (`isar/schema/placement.py`'s ``omega_band_hint_meV`` is documented
provenance-only, never read for steering — review finding 2026-08-18). The walkthrough's beats
are realized by the SAME two rules those files already pin: zone-centre anchor-distance ordering
(favours the DEFAULT profile's acoustic row, whose ``order_weight=1.0`` is the highest
``zone_centre``-anchored weight in the profile) and the coverage-complete band walk (fills the
domain low-to-high at any one node once it has partial coverage). Beat (a) is genuinely new
coverage (the realized window through ``proposal_from_intent``, not only the abstract
``SurveyProposal``); beats (b)/(c) are new SEQUENCES over ``propose_survey``, cited against their
un-sequenced siblings inline; the degradation case is NOT duplicated here — see the module
docstring's final section for the citation.
"""
from __future__ import annotations

import pytest

from isar.analyze.catalogue import DEFAULT_PROFILE
from isar.drive.acquisition.opening_book import propose_survey
from isar.drive.steering.realization import proposal_from_intent
from isar.schema.action_identity import MeasurementIntent
from isar.schema.question_policy import resolve_policy
from isar.scene.questions import QuestionSubject

P1 = "a" * 64
EPOCH = "epoch:1"

#: One zone-centre anchor at the region's own start (period 2.0, region 2.0..3.0 contains only
#: the multiple 2.0 -- 4.0 is outside). A single attractor is deliberate: two anchors in one
#: region (as in test_opening_book's declared period=1.0 fixtures) makes the outward-expansion
#: claim ambiguous, since the far anchor pulls proposals back toward IT once the near one is
#: exhausted. One anchor is what actually isolates "outward from the initialized point."
REGION = (2.0, 3.0)
ZONE_PERIOD = 2.0
DOMAIN = (0.0, 10.0)
KNOWLEDGE = {"zone_period": ZONE_PERIOD, "lattice_epoch": EPOCH}


def fp(scan_ref, coord, bands, partition=P1, exposure=5.0):
    return {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition,
            "effective_exposure": exposure,
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands]}


def obs(scan_ref, lattice_epoch=EPOCH):
    return {"scan_ref": scan_ref, "lattice_epoch": lattice_epoch}


def scene(footprints=(), sample_knowledge=None, observations=None):
    if observations is None:
        observations = [obs(f["scan_ref"]) for f in footprints]
    return {"observations": list(observations), "footprints": list(footprints), "clusters": [],
            "non_scoring": [], "sample_knowledge": sample_knowledge or {},
            "condition_t_round": None}


def step(spacing=0.25, domain=DOMAIN, catalogue_profile=DEFAULT_PROFILE, survey_order="placement_informed"):
    policy = resolve_policy(user_ask={"survey_order": survey_order, "min_effective_exposure": 1.0})
    return {"region": {"kind": "path", "maximum_initial_spacing_rlu": spacing},
            "energy": {"intrinsic_omega_domain_meV": list(domain)},
            "catalogue_profile": catalogue_profile, "resolved_policy": policy}


def subject(region=REGION, partition=P1):
    return QuestionSubject(kind="region", partition_id=partition, region_rlu=region,
                           omega_band_meV=None)


def realize(coord: float, e_lo: float, e_hi: float, fwhm=0.5):
    """One proposal through the SAME grid rule production realizes requests under (D6 item 4:
    the realized window, not the abstract intent)."""
    intent = MeasurementIntent(action_class="discovery_survey", hkl=(coord, 0.0, 0.0),
                               energy_lo=e_lo, energy_hi=e_hi, window_family="discovery")
    return proposal_from_intent(intent, fwhm_meV=fwhm, provenance={}, hypotheses_fingerprint=None,
                                reason="cold-start acceptance", ncount=int(1e8))


# ----------------------------------------------------------------------------
# BEAT 1 -- the first proposal hunts the acoustic branch just off the zone centre, at low ω
# ----------------------------------------------------------------------------
def test_beat1_first_proposal_is_just_off_the_zone_centre_at_low_energy():
    st = step()
    sc = scene(sample_knowledge=KNOWLEDGE)
    proposals = propose_survey(sc, st, subject())
    first = proposals[0]

    assert first.kind == "missing_q_node"
    # Just off the declared zone centre (2.0) -- never ON it, never at the region's OTHER
    # (non-anchored) boundary (3.0), never the plain geometric midpoint (2.5) a uniform survey
    # would open first (test_opening_book.test_uniform_vs_placement_informed_same_set_different_order
    # already pins that placement-informed and uniform propose the identical SET in a different
    # order; this pins which member of that set comes first under the DEFAULT catalogue profile).
    assert first.axis_coordinate == pytest.approx(2.25)
    assert first.axis_coordinate not in (2.0, 3.0, 2.5)

    # Realized through the production grid rule: the window is the declared domain, so it
    # reaches ω near zero -- the acoustic branch's home (ω → 0 at the zone centre, the highest
    # zone_centre order_weight in the DEFAULT profile, `isar/schema/placement.py`
    # `_DEFAULT_PLACEMENT_RULES["acoustic"]`). This is D6 item-4 EVIDENCE, not asserted away:
    # see the module docstring / realization.py's re-pointed comment for what it means.
    proposal = realize(first.axis_coordinate, DOMAIN[0], DOMAIN[1])
    assert proposal.h == pytest.approx(2.25)
    assert proposal.e_lo == pytest.approx(0.0)         # touches the elastic line -- see item 4
    assert proposal.e_hi == pytest.approx(10.0)


# ----------------------------------------------------------------------------
# BEAT 2 -- persistent absence at low ω pivots the hunt upward, at the SAME anchored node
# ----------------------------------------------------------------------------
def test_beat2_absence_in_the_low_band_pivots_the_hunt_upward_at_the_anchored_node():
    """The DEFAULT profile's other zone_centre-anchored class (optic) declares no band
    restriction (``omega_band_hint_meV=None``) and the next-highest order_weight after acoustic
    -- so once the acoustic-home low band is measured and yields nothing (no candidate is
    recorded for it: the scene below has zero observations naming a phenomenon there, the same
    "phonon-less" absence `isar.scene.predicates._structural_presence` reads as a ConfirmPresence
    miss), the coverage-complete band walk is what carries the hunt to the remaining (higher-E)
    part of the domain -- and it does so AT THAT SAME q, ahead of moving to a new zone-centre-
    adjacent q node, because the anchor-distance term in propose_survey's ranking does not
    distinguish an omega-gap proposal from a q-node proposal at the identical coordinate."""
    st = step()
    # The node beat 1 opened, now measured -- but only its low third of the domain (the
    # "acoustic hunt", covered and empty: no candidate/cluster recorded for it below).
    sc = scene(footprints=[fp("e:1:d", 2.25, bands=((0.0, 3.0),))],
              sample_knowledge=KNOWLEDGE)
    proposals = propose_survey(sc, st, subject())

    first = proposals[0]
    assert first.kind == "missing_omega_band"
    assert first.axis_coordinate == pytest.approx(2.25)          # the SAME anchored node
    assert first.detail["omega_lo"] == pytest.approx(3.0)        # finite E upward, not from zero
    assert first.detail["omega_hi"] == pytest.approx(10.0)

    # And it outranks BOTH still-unopened q nodes in the region (2.5, 2.75): the pivot happens
    # before the walk spreads to new q, not after.
    q_nodes = [p for p in proposals if p.kind == "missing_q_node"]
    assert len(q_nodes) == 2
    assert all(proposals.index(p) > proposals.index(first) for p in q_nodes)

    proposal = realize(first.axis_coordinate, first.detail["omega_lo"], first.detail["omega_hi"])
    assert proposal.e_lo == pytest.approx(3.0)
    assert proposal.e_lo > 0.0, "the pivoted hunt starts above the elastic line, not from zero"


# ----------------------------------------------------------------------------
# BEAT 3 -- once a point is initialized, the walk extrapolates OUTWARD from it
# ----------------------------------------------------------------------------
def test_beat3_the_walk_expands_outward_from_the_initialized_node_not_to_an_unrelated_one():
    """Each round measures exactly the node the PREVIOUS round proposed first, then re-derives.
    With one zone-centre anchor in the region (see the module docstring), anchor-distance
    ordering can only ever prefer the node closest to what has already been initialized -- so
    the sequence is monotonically outbound, never a jump past an unmeasured neighbour to some
    farther, unrelated point."""
    st = step()
    measured: list[dict] = []
    coords: list[float] = []
    for _ in range(3):
        sc = scene(footprints=measured, sample_knowledge=KNOWLEDGE)
        proposals = propose_survey(sc, st, subject())
        q_proposals = [p for p in proposals if p.kind == "missing_q_node"]
        assert q_proposals, "the region must still have an unopened q node"
        coord = q_proposals[0].axis_coordinate
        coords.append(coord)
        measured.append(fp(f"e:{len(measured)}:d", coord, bands=((0.0, 10.0),)))

    assert coords == pytest.approx([2.25, 2.5, 2.75])
    # Strictly increasing distance from the one anchor: each new node is adjacent to the
    # previous, never a jump over it to a farther point first.
    distances = [abs(c - ZONE_PERIOD) for c in coords]
    assert distances == sorted(distances)
    assert all(b - a == pytest.approx(0.25) for a, b in zip(coords, coords[1:])), \
        "outward means the NEXT node, not a skip"


# ----------------------------------------------------------------------------
# DEGRADATION -- blank knowledge (or a flat/unregistered catalogue) never raises
# ----------------------------------------------------------------------------
def test_degradation_blank_knowledge_and_flat_catalogue_still_propose_never_raise():
    """The 4.3 acceptance already pins this end to end through ``plan_next_action`` --
    ``tests/drive/test_cold_start.py::test_blank_knowledge_still_proposes_a_uniform_survey``
    (no declared zone_period/epoch: the derivation notes ``predetermined_openers_inert`` and the
    survey degrades to the plain geometric fill) -- and ``survey_order: uniform`` staying
    user-selectable is pinned by
    ``tests/drive/test_opening_book.py::test_uniform_vs_placement_informed_same_set_different_order``.
    This case adds only what neither already covers: an UNREGISTERED catalogue profile (the
    "flat catalogue" half of the acceptance item) must degrade the same way, at this layer,
    rather than raise -- ``zone_centre_order_weight`` is documented never to raise on one
    (`isar/schema/placement.py`), but nothing before this pinned that promise against a live
    ``propose_survey`` call."""
    st = step(catalogue_profile="isar.catalogue.unregistered/1")
    sc = scene(sample_knowledge=KNOWLEDGE)          # zone geometry IS declared here
    proposals = propose_survey(sc, st, subject())   # must not raise
    assert proposals and all(p.kind == "missing_q_node" for p in proposals)
    # Falls back to the plain geometric order -- no anchor score survives an unregistered
    # profile, so the ordering is indistinguishable from ``survey_order: uniform``.
    assert [p.axis_coordinate for p in proposals] == sorted(p.axis_coordinate for p in proposals)
