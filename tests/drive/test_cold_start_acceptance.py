"""The SCENE-4.4 cold-start acceptance script (plan's "Cold-start ruling" items 3-4, D6).

PR #35 review finding: the previous version of this file staged its beats independently, so beat
2's hand-built partial footprint (0-3 meV at the anchored node) could never have been the footprint
beat 1's own scan actually delivers -- beat 1 realizes the FULL intrinsic domain at that node
(the RULED window policy, Topic 5: broad windows over the intrinsic domain; the survey never
narrows per class), so a real delivery there always closes the band leg outright. This file is
now ONE causally-connected script: each planning round is fed exactly the coverage the PREVIOUS
round's realized proposal actually delivers (plan -> realize -> mark that realized coverage as
delivered -> plan again), through the SAME production grid rule (``proposal_from_intent``) that
turns an abstract ``SurveyProposal`` into a scan window (D6 item 4).

Under broad windows, a full-domain delivery leaves no band gap behind, so the sequence this file
pins is a pure Q-node walk: the first proposal sits at the hypothesis-favoured node just off the
declared zone centre (zone-centre anchor-distance ordering favours the DEFAULT profile's highest
``zone_centre``-anchored ``order_weight`` -- SCENE-4 ``isar/schema/placement.py``
``_DEFAULT_PLACEMENT_RULES``), and once a node is measured the walk expands OUTWARD from it to the
next-nearest still-uncovered node, never an unrelated jump. A separate case pins the ONE way
``missing_omega_band`` legitimately reopens a node that already received a real delivery: a later
program step declaring a WIDER energy domain than the step that made the earlier delivery (each
``CampaignProgram`` step carries its own declared ``intrinsic_omega_domain_meV`` --
``isar/drive/steering/generators.py``'s ``_domain``/``_clip_omega`` compare bands against the
CALLING step's own domain, so a step revision that widens it is the only way a previously
full-for-its-domain delivery becomes partial again). A final case pins the required degradation:
blank knowledge must never raise, only fall back to the uniform survey.

Any branch-labeled (e.g. acoustic-hunt-then-optic-pivot) CAUSAL placement sequence is out of
scope here -- SCENE-4 has no such state machine (``omega_band_hint_meV`` is documented
provenance-only, never read for steering -- review finding 2026-08-18); a real causal
acoustic-absence -> optic-pivot walk is scene-EIG placement work (SCENE-5), not this file.

This is deliberately built at the SAME layer ``tests/drive/test_opening_book.py`` and
``tests/drive/test_cold_start.py`` already pin (``propose_survey`` / ``plan_next_action`` /
``DeclaredPartition``), not a new mechanism, and re-uses the SAME two production rules those files
already pin: zone-centre anchor-distance ordering and the coverage-complete band walk. What is new
here is the SEQUENCING over ``propose_survey`` (feeding each round the previous round's own
realized delivery) and the realized-window evidence through ``proposal_from_intent``; the blank-
knowledge degradation case is NOT duplicated here -- see ``test_cold_start.py``'s
``test_blank_knowledge_still_proposes_a_uniform_survey`` for the end-to-end ``plan_next_action``
version this one only adds the unregistered-catalogue half of.
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


def _deliver(scan_ref: str, coord: float, proposal) -> dict:
    """The footprint a REAL delivery of ``proposal`` leaves behind -- the realized window, never
    a hand-built one, so the next planning round is fed exactly what production would have."""
    return fp(scan_ref, coord, bands=((proposal.e_lo, proposal.e_hi),))


# ----------------------------------------------------------------------------
# THE SEQUENCE -- plan, realize, deliver, plan again; broad windows leave no band gap behind, so
# the causally-connected walk is a pure outward Q-node expansion.
# ----------------------------------------------------------------------------
def test_the_cold_start_walk_is_one_causally_connected_sequence_of_broad_window_deliveries():
    """Round 1's proposal is fed to ``proposal_from_intent`` (the real realization rule); the
    REALIZED window -- not a hand-built one -- becomes round 2's delivered footprint, and so on.
    Because every realized window spans the full declared domain, no round ever reopens a band gap
    at an already-delivered node: the walk's only lever is outward Q-node expansion."""
    st = step()
    measured: list[dict] = []
    coords: list[float] = []

    for round_index in range(3):
        sc = scene(footprints=measured, sample_knowledge=KNOWLEDGE)
        proposals = propose_survey(sc, st, subject())
        first = proposals[0]

        assert first.kind == "missing_q_node", (
            "a full-domain delivery leaves no band gap, so every round's first proposal must "
            "still be a new Q node -- a missing_omega_band here would mean a previous round's "
            "realized window did NOT actually cover the declared domain")
        coords.append(first.axis_coordinate)

        proposal = realize(first.axis_coordinate, DOMAIN[0], DOMAIN[1])
        if round_index == 0:
            # Beat 1 evidence (D6 item 4): just off the zone centre, reaching ω -> 0 (the
            # elastic line) because the realized window is the full declared domain.
            assert first.axis_coordinate == pytest.approx(2.25)
            assert first.axis_coordinate not in (2.0, 3.0, 2.5)
            assert proposal.h == pytest.approx(2.25)
            assert proposal.e_lo == pytest.approx(0.0)
            assert proposal.e_hi == pytest.approx(10.0)

        measured.append(_deliver(f"e:{round_index}:d", first.axis_coordinate, proposal))

        # The delivery this round just made never reopens a band gap in a LATER round.
        later_proposals = propose_survey(
            scene(footprints=measured, sample_knowledge=KNOWLEDGE), st, subject())
        assert all(p.kind != "missing_omega_band" for p in later_proposals), (
            "a full-domain realized delivery must close the band leg outright")

    # Outward expansion: each round's node is the NEXT interior grid point, never a skip and
    # never a jump back toward the anchor.
    assert coords == pytest.approx([2.25, 2.5, 2.75])
    distances = [abs(c - ZONE_PERIOD) for c in coords]
    assert distances == sorted(distances)
    assert all(b - a == pytest.approx(0.25) for a, b in zip(coords, coords[1:])), \
        "outward means the NEXT node, not a skip"


# ----------------------------------------------------------------------------
# A GENUINE missing_omega_band -- not hand-built: a later program step widens the declared
# domain, so an earlier delivery that was full-for-ITS-domain is partial for the new one.
# ----------------------------------------------------------------------------
def test_a_program_revision_that_widens_the_declared_domain_reopens_the_earlier_delivery_as_a_band_gap():
    """``generators.py``'s ``_domain``/``_clip_omega`` compare a node's bands against the CALLING
    step's own declared ``intrinsic_omega_domain_meV`` -- each ``CampaignProgram`` step carries its
    own domain, so a later step that declares a WIDER one is a real program revision, not a test
    fixture wearing a scenario's name. This is the only way this layer can honestly reopen a
    node's band leg after a real (broad-window) delivery already closed it for its own step."""
    narrow_step = step(domain=(0.0, 3.0))
    sc = scene(sample_knowledge=KNOWLEDGE)
    first = propose_survey(sc, narrow_step, subject())[0]
    assert first.kind == "missing_q_node"
    assert first.axis_coordinate == pytest.approx(2.25)

    delivery = realize(first.axis_coordinate, 0.0, 3.0)
    assert delivery.e_lo == pytest.approx(0.0) and delivery.e_hi == pytest.approx(3.0)
    measured = [_deliver("e:0:d", first.axis_coordinate, delivery)]

    # The program revision: the SAME node's coverage is now judged against a wider domain.
    wide_step = step(domain=(0.0, 10.0))
    proposals = propose_survey(scene(footprints=measured, sample_knowledge=KNOWLEDGE),
                               wide_step, subject())
    reopened = proposals[0]
    assert reopened.kind == "missing_omega_band"
    assert reopened.axis_coordinate == pytest.approx(2.25)         # the SAME node, not a new one
    assert reopened.detail["omega_lo"] == pytest.approx(3.0)       # finite E upward, not from zero
    assert reopened.detail["omega_hi"] == pytest.approx(10.0)

    # The reopened band leg outranks BOTH still-unopened q nodes in the region (2.5, 2.75): the
    # revision's own node is served before the walk spreads to new q.
    q_nodes = [p for p in proposals if p.kind == "missing_q_node"]
    assert len(q_nodes) == 2
    assert all(proposals.index(p) > proposals.index(reopened) for p in q_nodes)

    reproposal = realize(reopened.axis_coordinate, reopened.detail["omega_lo"],
                         reopened.detail["omega_hi"])
    assert reproposal.e_lo == pytest.approx(3.0)
    assert reproposal.e_lo > 0.0, "the reopened band starts above the elastic line, not from zero"


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
