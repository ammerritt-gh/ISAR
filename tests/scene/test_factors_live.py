"""The live physics factors: presence, and the machinery it and the linkage need (PR 3b).

Every test here is a physical claim rather than a snapshot: a phonon does not appear at one q
and vanish at the next, absence is evidence ONLY where the neighbour actually looked, and which
peak belongs to which track is never decided by ω rank. Observations are hand-built — no fitter,
no workspace — so a failure here is a failure of the factor, not of a fit.
"""
import math
from dataclasses import replace
from types import MappingProxyType

import pytest

from isar.scene import build as build_mod
from isar.scene.build import ClusterView, SceneAlternative, build_scene
from isar.scene.factors import (FACTORS, PRESENCE_MATCH_NATS, PRESENCE_MISS_NATS,
                                factor_manifest, neighbour_reports, registry_fingerprint)
from isar.scene.model import (SampleKnowledge, Visibility, footprint_of,
                              observation_fingerprint, visibility_of)

from .test_build import cand, cluster_of, mk_obs


def q(h):
    return (h, 0.0, 0.0)


# A declared dispersion support, which is the ONLY thing that makes a track's continuation
# reachable over a finite Δq (review finding 5). 2 meV/rlu over the 0.1 rlu edges these arenas sit
# on widens the reach by 0.2 meV — enough to say where a track could have gone, far too little to
# change which peaks chain to which. Tests that need an absence to be CHARGED declare it; tests
# about coverage, blocking or the undeclared-reach rule deliberately do not.
SLOPE = SampleKnowledge(dispersion_slope_max=2.0)


def terms(cluster, name):
    """``{assignment tuple: factor term}`` for one factor, over a cluster's alternatives."""
    return {(tuple(tuple(p) for p in a["assignment"]), tuple(tuple(l) for l in a["links"])):
            a["factor_terms"][name] for a in cluster["alternatives"]}


# ----------------------------------------------------------------------------
# Part 1 — components for phonon-less physics
# ----------------------------------------------------------------------------
def test_elastic_only_arenas_couple_through_their_elastic_components():
    """The deferral this closes: an ``elastic_only`` arena used to emit no component at all, so a
    run of phonon-less arenas could not be one scene structure — which is a physical claim
    ("nothing inelastic over this stretch of q"), not an absence of data."""
    obs = [mk_obs("e:1:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))], q=q(1.0)),
           mk_obs("e:2:d", [cand("elastic+bg", -11.0, n_modes=0, omegas=(), extra=("elastic",))], q=q(1.1))]
    rev = build_scene(obs)
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d", "e:2:d"]]


def test_a_bg_only_candidate_emits_nothing_and_couples_to_nothing():
    """Truly nothing there: no elastic term, no mode, so no component to couple on."""
    obs = [mk_obs("e:1:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -11.0, n_modes=0, omegas=())], q=q(1.1))]
    rev = build_scene(obs)
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"], ["e:2:d"]]


@pytest.mark.parametrize("label,n,extra,tags,kinds", [
    ("bg", 0, (), [], []),
    ("elastic+bg", 0, ("elastic",), ["elastic"], ["elastic"]),
    ("stokes+bg", 1, (), ["0"], ["mode"]),
    ("elastic+stokes+qe+bg", 1, ("elastic", "quasielastic"), ["0", "elastic", "qe"],
     ["mode", "elastic", "quasielastic"]),
    ("stokes2+bg@acoustic", 2, (), ["0", "1"], ["mode", "mode"]),
    # The label says nothing this test reads: an identity (``@acoustic``) or variant (``_neardeg``)
    # suffix, or a label with no structure grammar at all, changes no component.
    ("whatever_the_label_says", 2, ("elastic",), ["0", "1", "elastic"],
     ["mode", "mode", "elastic"]),
])
def test_the_persisted_descriptors_place_the_components(label, n, extra, tags, kinds):
    """Review finding 9: component KIND comes from the persisted descriptors, never from the
    label. The id keeps its tag as an identifier, but nothing decides physics by reading it."""
    obs = mk_obs("e:1:d", [cand(label, -10.0, n_modes=n, extra=extra,
                                omegas=tuple(4.0 + i for i in range(n)))])
    comps = build_mod._components_of(obs, footprint_of(obs), frozenset({label}))
    assert [c.component_id.rsplit("#", 1)[-1] for c in comps] == tags
    assert [c.kind for c in comps] == kinds
    assert all(c.omega == 0.0 for c in comps if c.kind != "mode")


# ----------------------------------------------------------------------------
# Part 2 — visibility semantics
# ----------------------------------------------------------------------------
def _footprint(window=(-10.0, 10.0)):
    """A footprint with ``mk_obs``'s gauss_fwhm of 1.0, folded onto intrinsic |ω|."""
    return footprint_of(mk_obs("e:1:d", [cand("A", -1.0)], window=window))


def test_visibility_censored_outside_the_footprint():
    """The gate the whole design rests on: a band nobody looked at is never evidence."""
    assert visibility_of(8.0, _footprint(window=(-3.0, 3.0)), False) == \
        Visibility.CENSORED_OUTSIDE_FOOTPRINT
    # ... and not even a match could turn a gap into an observation.
    assert visibility_of(8.0, _footprint(window=(-3.0, 3.0)), True) == \
        Visibility.CENSORED_OUTSIDE_FOOTPRINT


def test_visibility_observed_when_covered_and_matched():
    assert visibility_of(5.0, _footprint(), True) == Visibility.OBSERVED


def test_visibility_searched_no_signal_is_the_only_penalizable_absence():
    assert visibility_of(5.0, _footprint(), False) == Visibility.SEARCHED_NO_SIGNAL


def test_visibility_blocked_under_the_elastic_line():
    """Endpoint-under-Bragg: a band inside the elastic width, with an elastic component chosen
    there, is hidden — not absent. Penalizing it would invent a missing phonon under every Bragg
    peak."""
    fp = _footprint()
    assert visibility_of(0.5, fp, False, blocking_present=True) == Visibility.BLOCKED_UNRESOLVED
    # No elastic component there: the same band was genuinely searched.
    assert visibility_of(0.5, fp, False) == Visibility.SEARCHED_NO_SIGNAL
    # Outside the elastic width, the elastic line hides nothing.
    assert visibility_of(4.0, fp, False, blocking_present=True) == Visibility.SEARCHED_NO_SIGNAL
    # A detection under the elastic line stays a detection.
    assert visibility_of(0.5, fp, True, blocking_present=True) == Visibility.OBSERVED


def test_structural_endpoint_is_never_assigned_without_declared_zone_knowledge():
    """The state is not a free-floating excuse for an absence: with nothing declared about the
    sample there is no physics that could terminate a track, so it can never appear."""
    assert Visibility.STRUCTURAL_ENDPOINT == "structural_endpoint"
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(5.1,))], q=q(1.1))]
    seen = {r["visibility"] for c in build_scene(obs).payload["clusters"] for r in c["visibility"]}
    assert Visibility.STRUCTURAL_ENDPOINT not in seen


# ----------------------------------------------------------------------------
# Part 4 — presence
# ----------------------------------------------------------------------------
def test_neighbour_support_pulls_an_ambiguous_arena_towards_the_two_mode_reading():
    """Adrian's rule doing its work: the middle arena's evidence cannot separate one mode from
    two, and both neighbours see two. The scene, not the scan, resolves it."""
    two = cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.0, 8.0))
    one = cand("stokes+bg", -10.0, omegas=(4.0,))
    obs = [mk_obs("e:1:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(3.9, 7.9))], q=q(1.0)),
           mk_obs("e:2:d", [two, one], q=q(1.1)),
           mk_obs("e:3:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.1, 8.1))], q=q(1.2))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    marginal = cl["marginals"]["label"]["e:2:d"]
    assert marginal["stokes2+bg"] > marginal["stokes+bg"]
    # ... and the mode-count marginal says the same thing, which is what a reader looks at.
    assert cl["marginals"]["n_modes"]["e:2:d"]["2"] > cl["marginals"]["n_modes"]["e:2:d"]["1"]


def test_a_neighbour_that_never_looked_at_the_band_costs_nothing():
    """The footprint gate. The narrow-window neighbour is not evidence of absence, so the two
    readings of the wide arena stay exactly as its own evidence left them."""
    wide = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(2.0,)),
                            cand("stokes2+bg", -10.0, n_modes=2, omegas=(2.0, 8.0))], q=q(1.0))
    narrow = mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(2.0,))], q=q(1.1),
                    window=(-9.0, 9.0))
    # The same neighbour with a window that never reaches the disputed 8 meV band.
    # Both fields: the bands ARE the coverage now, and the window is only their envelope.
    blind = replace(narrow, energy_window=(-3.0, 3.0), energy_bands=((-3.0, 3.0),))
    blind = replace(blind, fingerprint=observation_fingerprint(replace(blind, fingerprint="")))

    censored = cluster_of(build_scene([wide, blind], sample=SLOPE), "e:1:d")
    assert visibility_of(8.0, footprint_of(blind), False) == Visibility.CENSORED_OUTSIDE_FOOTPRINT
    # The 8 meV mode was never looked at, so the two readings of e:1:d score identically: the
    # gap moved belief in neither direction.
    scored = terms(censored, "presence")
    assert len(set(scored.values())) == 1

    # Same arenas, the neighbour now looking at 2 meV as well: the absence becomes evidence, and
    # the extra mode costs exactly one miss.
    searched = terms(cluster_of(build_scene([wide, narrow], sample=SLOPE), "e:1:d"), "presence")
    by_label = {a[0][1]: t for (a, _links), t in searched.items()}
    assert by_label["stokes+bg"] == PRESENCE_MATCH_NATS * 2
    assert by_label["stokes2+bg"] == PRESENCE_MATCH_NATS * 2 + PRESENCE_MISS_NATS


def test_a_band_inside_a_neighbours_mask_hole_costs_nothing_either():
    """Review finding 4, at the builder. The neighbour's axis spans 8 meV but an excluded region
    took 6–10 out of it, so nothing ever counted there. Priced off the outer window the disputed
    mode would look searched-and-missing; priced off the kept runs it is censored, exactly like a
    neighbour that never reached the band at all."""
    wide = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(2.0,)),
                            cand("stokes2+bg", -10.0, n_modes=2, omegas=(2.0, 8.0))], q=q(1.0))
    holed = mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(2.0,))], q=q(1.1),
                   window=(-10.0, 10.0), bands=((-10.0, -6.0), (-4.0, 4.0), (6.0, 10.0)))
    # The hole is at |ω| ∈ (4, 6); the 8 meV band sits in a KEPT run on both sides of zero.
    assert visibility_of(5.0, footprint_of(holed), False) == Visibility.CENSORED_OUTSIDE_FOOTPRINT
    assert visibility_of(8.0, footprint_of(holed), False) == Visibility.SEARCHED_NO_SIGNAL

    # A mode inside the hole: both readings of e:1:d score identically — no absence charge.
    in_hole = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(2.0,)),
                               cand("stokes2+bg", -10.0, n_modes=2, omegas=(2.0, 5.0))], q=q(1.0))
    assert len(set(terms(cluster_of(build_scene([in_hole, holed], sample=SLOPE), "e:1:d"),
                         "presence").values())) == 1
    # ... and the same mode moved out of the hole is charged its miss, so the exemption is the
    # hole and not the scene.
    scored = terms(cluster_of(build_scene([wide, holed], sample=SLOPE), "e:1:d"), "presence")
    by_label = {a[0][1]: t for (a, _links), t in scored.items()}
    assert by_label["stokes2+bg"] == by_label["stokes+bg"] + PRESENCE_MISS_NATS


def test_a_one_q_spurion_is_priced_against_the_empty_arena_that_searched_for_it():
    """Carried item 15, and the physics it had inverted. The neighbour's surviving candidates are
    background-only — it looked at 8 meV and found NOTHING AT ALL, which is the strongest absence
    evidence there is. Component-pair coupling could not see it (an empty arena emits no
    component), so the spurion used to sit in its own cluster completely unpriced, while the same
    spurion beside a neighbour that happened to hold something else WAS priced.

    Coverage coupling closes it: the arenas are one cluster, the miss is charged, and the
    spurion is still enumerated with a real posterior — the scene weights hypotheses, it does not
    delete them."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(8.0,)),
                            cand("bg", -10.5, n_modes=0, omegas=())], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.1))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    assert cl["scan_refs"] == ["e:1:d", "e:2:d"]
    by_label = {a[0][1]: t for (a, _links), t in terms(cl, "presence").items()}
    assert by_label["stokes+bg"] == PRESENCE_MISS_NATS
    assert by_label["bg"] == 0.0
    posterior = {a["assignment"][0][1]: a["posterior"] for a in cl["alternatives"]}
    assert posterior["stokes+bg"] > 0.0 and posterior["bg"] > 0.0


def test_one_neighbouring_peak_cannot_support_two_claimed_modes():
    """Review finding 7. The neighbour holds ONE peak at 4.2 meV, and the two-mode reading of the
    first arena claims both 4.0 and 4.5. Under the old any-partner-within-tolerance match that one
    observed peak supported BOTH claims, so the two-mode reading collected two matches from one
    observation and the mode-count marginal was pushed to 2 by an accounting artefact.

    Support now comes from the alternative's linkage, which the builder's degree guard keeps
    one-to-one: 4.0 takes the peak, 4.5 is unsupported and pays its miss. Asserted on the marginal
    a reader actually looks at, not only on the report states."""
    obs = [mk_obs("e:1:d", [cand("one", -10.0, omegas=(4.0,)),
                            cand("two", -10.0, n_modes=2, omegas=(4.0, 4.5))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(4.2,))], q=q(1.1))]
    cl = cluster_of(build_scene(obs, sample=SLOPE), "e:1:d")
    by_label = {a[0][1]: t for (a, _l), t in terms(cl, "presence").items()}
    assert by_label["one"] == 2 * PRESENCE_MATCH_NATS
    assert by_label["two"] == 2 * PRESENCE_MATCH_NATS + PRESENCE_MISS_NATS
    n_modes = cl["marginals"]["n_modes"]["e:1:d"]
    assert n_modes["1"] > n_modes["2"]


def test_one_absent_continuation_is_charged_exactly_once_across_every_factor():
    """Review finding 8, as a ledger grep. The neighbour looked at 8 meV and found nothing at all;
    that is ONE null. It used to be charged twice — a presence miss and a connectivity break — so
    the same silence moved the posterior by 3 nats where the model says 1."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(8.0,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.1))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    alt, = cl["alternatives"]
    assert [why for reasons in alt["reasons"].values() for why in reasons] == \
        ["miss e:1:d#stokes+bg#0 not seen at e:2:d"]
    assert [t for t in alt["factor_terms"].values() if t != 0.0] == [PRESENCE_MISS_NATS]


def test_coverage_coupling_needs_coverage():
    """No coverage is no claim, so it must draw no edge either: a cluster that grew on a band the
    neighbour never looked at would put an arena into a joint normalization on the strength of a
    gap. The neighbour's window stops at 3 meV; the mode sits at 8."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(8.0,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.1),
                  window=(-3.0, 3.0))]
    assert [c["scan_refs"] for c in build_scene(obs).payload["clusters"]] == \
        [["e:1:d"], ["e:2:d"]]


def test_coverage_coupling_does_not_resurrect_an_incomplete_arena():
    """H7 is upstream of the graph and stays there. An arena whose candidate comparison never
    finished is experiment-map-only; giving components a way to couple to a whole ARENA must not
    become a way for an unscoreable one to re-enter the scene through its footprint."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(8.0,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.1),
                  status="incomplete", fallback="evidence_budget_exhausted")]
    rev = build_scene(obs)
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"]]
    assert rev.payload["non_scoring"] == [{"scan_ref": "e:2:d", "reason": "evidence_incomplete"}]


# ----------------------------------------------------------------------------
# Part 4 — connectivity
# ----------------------------------------------------------------------------
def _chain(middle_labels):
    return [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0)),
            mk_obs("e:2:d", middle_labels, q=q(1.1)),
            mk_obs("e:3:d", [cand("stokes+bg", -10.0, omegas=(5.2,))], q=q(1.2))]


def test_a_linear_chain_links_into_one_track_with_no_penalty():
    cl = cluster_of(build_scene(_chain([cand("stokes+bg", -10.0, omegas=(5.1,))])), "e:1:d")
    alt, = cl["alternatives"]
    # One track: each adjacent pair linked once (1-2 and 2-3; 1 and 3 are not adjacent).
    assert len(alt["links"]) == 2
    assert alt["factor_terms"]["presence"] == 4 * PRESENCE_MATCH_NATS


def test_a_track_that_vanishes_mid_cluster_pays_one_miss_per_searched_neighbour():
    """The middle arena looked at 5 meV and (on this reading) found nothing there, so both
    neighbours' tracks terminate into a searched, covered, empty band.

    Re-pinned for review finding 8: this used to be charged TWICE, once by presence as a missing
    peak and once by connectivity as a broken track. It is one null — the middle arena's silence
    at 5 meV, seen from each side — and presence owns it."""
    obs = _chain([cand("stokes+bg", -10.0, omegas=(5.1,)), cand("bg", -10.0, n_modes=0, omegas=())])
    cl = cluster_of(build_scene(obs), "e:1:d")
    by_middle = {a["assignment"][1][1]: a for a in cl["alternatives"]}
    assert by_middle["bg"]["factor_terms"] == {"presence": 2 * PRESENCE_MISS_NATS}
    assert len(by_middle["bg"]["reasons"]["presence"]) == 2
    assert by_middle["stokes+bg"]["posterior"] > by_middle["bg"]["posterior"]


def test_an_absence_under_the_elastic_line_is_not_evidence():
    """The middle arena's chosen candidate is elastic-only, and the track sits inside the elastic
    width: blocked, not missing — no penalty at all."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(0.4,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))], q=q(1.1))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    alt, = cl["alternatives"]
    assert alt["factor_terms"] == {"presence": 0.0}
    assert {r["visibility"] for r in cl["visibility"]} == {Visibility.BLOCKED_UNRESOLVED}


# ----------------------------------------------------------------------------
# Part 3 — the crossing: two linkages, one assignment, an honest 50/50
# ----------------------------------------------------------------------------
def _crossing_scene():
    """Two adjacent arenas, two modes each, ω order inverted between them."""
    return [mk_obs("e:1:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.0, 5.0))], q=q(1.0)),
            mk_obs("e:2:d", [cand("stokes2+bg", -12.0, n_modes=2, omegas=(4.2, 5.3))], q=q(1.1))]


def test_a_crossing_enumerates_both_linkages_at_equal_posterior():
    """The whole point of the factor: no ω-rank rule picks a track. Both readings are present,
    they share a base Σ lnZ exactly, both link every mode on both sides, so belief splits
    50/50 — and the ledger itemizes both rather than reporting one winner."""
    cl = cluster_of(build_scene(_crossing_scene()), "e:1:d")
    assert cl["n_assignments"] == 1 and cl["n_enumerated"] == 2
    assert cl["linkage_truncated"] is False
    a, b = cl["alternatives"]
    assert a["links"] != b["links"]
    assert a["base"] == b["base"]
    assert a["factor_terms"]["presence"] == b["factor_terms"]["presence"]
    assert a["posterior"] == pytest.approx(0.5, abs=1e-12)
    assert b["posterior"] == pytest.approx(0.5, abs=1e-12)


def test_the_heap_never_collapses_two_linkages_of_one_assignment():
    """Under the cap the enumeration ranks ASSIGNMENTS; linkage variants expand afterwards, so a
    truncated cluster still carries both readings of every assignment it kept."""
    cl = cluster_of(build_scene(_crossing_scene(), alt_cap=1), "e:1:d")
    assert cl["n_assignments"] == 1 and cl["n_enumerated"] == 2
    assert len({tuple(tuple(p) for p in a["links"]) for a in cl["alternatives"]}) == 2


def test_linkage_variants_are_capped_and_the_truncation_is_recorded(monkeypatch):
    monkeypatch.setattr(build_mod, "MAX_LINKAGE_VARIANTS", 1)
    cl = cluster_of(build_scene(_crossing_scene()), "e:1:d")
    assert cl["n_enumerated"] == 1 and cl["linkage_truncated"] is True
    # The default chaining is what survives a truncation — never a proposal.
    assert cl["alternatives"][0]["links"] == [["e:1:d#stokes2+bg#0", "e:2:d#stokes2+bg#0"],
                                              ["e:1:d#stokes2+bg#1", "e:2:d#stokes2+bg#1"]]


def test_a_partly_linked_junction_yields_two_valid_pairings_and_no_impossible_one():
    """The reviewer's repro. The default chaining links only ONE pair here (4.0→4.5 is inside the
    tolerance, 7.0→5.0 is not), so both pairings are alternatives for the SAME junction. Applying
    them as if they were independent junctions produced a linkage where one mode continued as two
    tracks — an impossible reading, published with real posterior mass."""
    obs = [mk_obs("e:1:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.0, 7.0))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.5, 5.0))], q=q(1.1))]
    cl = cluster_of(build_scene(obs, sample=SLOPE), "e:1:d")
    # Three readings, all of them possible: the partial default (the 7 meV mode's track ends
    # here), and the two complete pairings. The impossible fourth — one mode continuing as two
    # tracks — is now ungeneratable rather than merely unlikely.
    assert cl["n_enumerated"] == 3
    for a in cl["alternatives"]:
        ends = [l[0] for l in a["links"]] + [l[1] for l in a["links"]]
        assert len(ends) == len(set(ends))            # no component continues as two tracks
    complete = [a for a in cl["alternatives"] if len(a["links"]) == 2]
    partial = [a for a in cl["alternatives"] if len(a["links"]) == 1]
    assert len(complete) == 2 and len(partial) == 1
    # The two complete pairings support every mode and are therefore equal; the partial one pays
    # for the two it leaves unsupported rather than being ranked by ω.
    assert complete[0]["posterior"] == pytest.approx(complete[1]["posterior"], rel=1e-12)
    # Each complete pairing links all four modes, so every report is a match; the partial reading
    # leaves the 7.0 mode and the 5.0 mode unsupported and pays two misses against two matches.
    assert complete[0]["factor_terms"]["presence"] == 4 * PRESENCE_MATCH_NATS
    assert partial[0]["factor_terms"]["presence"] == \
        2 * PRESENCE_MATCH_NATS + 2 * PRESENCE_MISS_NATS
    assert partial[0]["posterior"] < complete[0]["posterior"]


def test_a_linkage_independent_miss_contributes_identically_to_every_variant():
    """Review finding 8's other half. Presence charges detection at the ASSIGNMENT level: a mode
    with no reachable partner at a covered neighbour is unlinked under EVERY linkage reading of
    that assignment, so its miss must appear once in each variant and cancel out of the
    within-assignment comparison. A miss that varied with the linkage would let the crossing's two
    readings be separated by a null neither of them explains."""
    obs = [*_crossing_scene(),
           mk_obs("e:3:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.2))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    assert cl["scan_refs"] == ["e:1:d", "e:2:d", "e:3:d"]
    assert cl["n_assignments"] == 1 and cl["n_enumerated"] == 2
    misses = [sorted(w for w in a["reasons"]["presence"] if w.endswith("at e:3:d"))
              for a in cl["alternatives"]]
    assert len(misses[0]) == 2 and misses[0] == misses[1]


def test_a_linkage_truncation_records_its_dropped_prior_and_leaves_the_cluster_unresolved(
        monkeypatch):
    """Review finding 10, both halves. The dropped readings here are NOT interchangeable with the
    kept one: the two complete pairings link all four modes (four matches) while the partial
    default leaves two unsupported, so a cap that keeps only the default drops readings worth
    3 nats more than what it published.

    So the cap may not renormalize the loss away. The prior divides by the ENUMERATED count, which
    leaves the kept variant carrying 1/3 of that assignment's prior and the missing 2/3 recorded;
    and because nothing bounds the dropped readings' factor terms, the cluster goes into the
    unresolved lane rather than looking like a narrowed one."""
    obs = [mk_obs("e:1:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.0, 7.0))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(4.5, 5.0))], q=q(1.1))]
    full = cluster_of(build_scene(obs, sample=SLOPE), "e:1:d")
    assert full["n_enumerated"] == 3
    assert len({a["factor_terms"]["presence"] for a in full["alternatives"]}) == 2

    monkeypatch.setattr(build_mod, "MAX_LINKAGE_VARIANTS", 1)
    cl = cluster_of(build_scene(obs, sample=SLOPE), "e:1:d")
    kept, = cl["alternatives"]
    assert kept["factor_terms"]["presence"] == \
        2 * PRESENCE_MATCH_NATS + 2 * PRESENCE_MISS_NATS      # the partial default survived
    assert cl["linkage_truncated"] is True
    assert cl["linkage_dropped_prior_mass"] == pytest.approx(2 / 3, rel=1e-12)
    assert kept["prior_terms"]["linkage_prior"] == -math.log(3)
    assert cl["truncated"] is True and cl["truncation_unbounded"] is True
    assert cl["log_discarded_mass_bound"] is None
    assert cl["marginals"]["truncated"] is True
    assert cl["marginals"]["truncation_unbounded"] is True


def test_an_impossible_linkage_is_refused_rather_than_scored():
    """The guard behind the fix: a proposer that emits a degree-2 link set fails loudly instead of
    publishing a reading that cannot exist."""
    both = (("e:1:d#stokes2+bg#0", "e:2:d#stokes2+bg#0"),
            ("e:1:d#stokes2+bg#0", "e:2:d#stokes2+bg#1"))
    broken = replace(FACTORS["connectivity"], propose=lambda alt, view: (both,))
    with pytest.raises(ValueError, match="more than one component of arena"):
        build_scene(_crossing_scene(), factors=[FACTORS["presence"], broken])


def _tolerance_probe(monkeypatch, tol_factor):
    """One ambiguous two-mode assignment against a rival with no crossing, at a chosen tolerance."""
    monkeypatch.setattr("isar.scene.factors.CROSSING_LINK_TOL_FACTOR", tol_factor)
    obs = [mk_obs("e:1:d", [cand("amb", -10.0, n_modes=2, omegas=(4.0, 5.0)),
                            cand("sep", -10.0, n_modes=2, omegas=(4.0, 5.0))], q=q(1.0)),
           mk_obs("e:2:d", [cand("amb", -10.0, n_modes=2, omegas=(4.2, 5.3))], q=q(1.1))]
    return cluster_of(build_scene(obs), "e:1:d")


@pytest.mark.parametrize("tol_factor", [1.0, 3.0])
def test_the_assignment_marginal_does_not_move_with_the_proposer_tolerance(monkeypatch,
                                                                           tol_factor):
    """Enumerating a second linkage is no probabilistic boost (the owner's N-mode precedent: a
    uniform prior over refinements dilutes, never boosts). Two assignments with identical evidence
    and identical factor terms must stay 50/50 whether the proposer offers one linkage each or
    two — otherwise an enumeration knob, not the data, would be moving belief by ln 2 nats."""
    cl = _tolerance_probe(monkeypatch, tol_factor)
    marginal = cl["marginals"]["label"]["e:1:d"]
    assert marginal["amb"] == pytest.approx(0.5, abs=1e-12)
    assert marginal["sep"] == pytest.approx(0.5, abs=1e-12)
    # ... and the ambiguity is still represented INSIDE each assignment that has it.
    variants = [a for a in cl["alternatives"] if a["assignment"][0][1] == "amb"]
    assert len(variants) == (2 if tol_factor == 3.0 else 1)
    assert all(a["prior_terms"]["linkage_prior"] == -math.log(len(variants)) for a in variants)


def test_the_proposer_tolerance_changes_what_is_enumerated_but_not_the_marginals(monkeypatch):
    """Guards the test above from passing because the tolerance never did anything."""
    tight, loose = _tolerance_probe(monkeypatch, 1.0), _tolerance_probe(monkeypatch, 3.0)
    assert tight["n_enumerated"] == 2 and loose["n_enumerated"] == 4
    # Equal to float noise, not bit-identical: the same marginal is summed over a different number
    # of terms, so the float summation path differs where the model does not.
    for ref, marginal in tight["marginals"]["label"].items():
        assert marginal == pytest.approx(loose["marginals"]["label"][ref], rel=1e-12)


def test_well_separated_branches_are_not_treated_as_ambiguous():
    """The proposer reaches only as far as the dispersion plausibly could: two branches 6 meV
    apart at both arenas have one reading, not two."""
    obs = [mk_obs("e:1:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(1.0, 9.0))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(1.2, 9.2))], q=q(1.1))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    assert cl["n_enumerated"] == 1


# ----------------------------------------------------------------------------
# Part 5 — declared sample knowledge: the zone-centre and localization factors
# ----------------------------------------------------------------------------
def _endpoint_scene(centre_h=1.0, mode_h=1.1, up_h=1.2, omega=1.5, up_omega=2.4,
                    neighbour="elastic+bg", kinds=("elastic",)):
    """A descending two-node TRACK whose lower end runs into an elastic-bearing arena.

    The endpoint is a reading of the linkage, never an amnesty granted from energy (review finding
    1), so the fixture has to carry a real track: the mode at ``up_h`` links to the mode at
    ``mode_h`` (0.9 meV apart, inside mk_obs's 1.0 meV resolution), and the track descends toward
    the arena at ``centre_h``. With mk_obs's gauss_fwhm of 1.0 the terminal mode at 1.5 meV is
    OUTSIDE the elastic width, so the baseline reading is a genuine searched-and-empty absence —
    the exemption has to be earned by the declaration, not handed over by ``blocked_unresolved``.
    """
    return [mk_obs("e:1:d", [cand(neighbour, -10.0, n_modes=0, omegas=(), extra=kinds)],
                   q=q(centre_h)),
            mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(omega,))], q=q(mode_h)),
            mk_obs("e:3:d", [cand("stokes+bg", -10.0, omegas=(up_omega,))], q=q(up_h))]


def _states(rev, cluster_id="e:1:d"):
    return {r["visibility"] for r in cluster_of(rev, cluster_id)["visibility"]}


def _endpoints(rev, cluster_id="e:1:d"):
    return [r for r in cluster_of(rev, cluster_id)["visibility"]
            if r["visibility"] == Visibility.STRUCTURAL_ENDPOINT]


def test_a_declared_zone_centre_terminates_the_track_instead_of_penalizing_it():
    """The whole point of the state. Without the declaration the scene says "you looked and saw
    nothing, so this mode is less likely"; with it the scene says "a gapless branch reaches ω→0
    here and is swallowed by the elastic response — nothing is missing".

    The no-penalty half is EMERGENT, not special-cased: presence keys on ``searched_no_signal``
    alone, so reinterpreting the state is all it takes."""
    obs = _endpoint_scene()
    blind = build_scene(obs)
    assert Visibility.SEARCHED_NO_SIGNAL in _states(blind)
    cl = cluster_of(blind, "e:1:d")
    assert list(terms(cl, "presence").values()) == [2 * PRESENCE_MATCH_NATS + PRESENCE_MISS_NATS]

    knowing = build_scene(obs, sample=SampleKnowledge(zone_period=1.0))
    assert Visibility.SEARCHED_NO_SIGNAL not in _states(knowing)
    assert [r["component_id"] for r in _endpoints(knowing)] == ["e:2:d#stokes+bg#0"]
    cl = cluster_of(knowing, "e:1:d")
    # The track's two observed continuations still score; only the terminated absence stops.
    assert list(terms(cl, "presence").values()) == [2 * PRESENCE_MATCH_NATS]


def test_an_isolated_low_energy_mode_gets_no_endpoint_and_stays_charged():
    """Control (ii) of review finding 1, and the failure the old predicate actually had: a single
    low-energy feature at one q, beside a declared zone centre, was handed the exemption on its
    energy alone. It is no branch — there is no track for a gapless dispersion to be the end of —
    so its absence at the centre is exactly as unexplained as it was."""
    obs = _endpoint_scene()[:2]                     # the centre arena and the lone mode
    rev = build_scene(obs, sample=SampleKnowledge(zone_period=1.0))
    assert _states(rev) == {Visibility.SEARCHED_NO_SIGNAL}
    assert list(terms(cluster_of(rev, "e:1:d"), "presence").values()) == [PRESENCE_MISS_NATS]


def test_a_flat_track_running_into_the_centre_gets_no_endpoint_either():
    """Control (iii): the low-lying optic case. The track is linked and sits next to the declared
    centre, but it is not DESCENDING — it has the same energy at both nodes, so nothing about it
    says it reaches ω → 0 at the fold. A gapless branch is a claim about the descent, and this is
    a branch that simply stopped being seen."""
    rev = build_scene(_endpoint_scene(up_omega=1.5), sample=SampleKnowledge(zone_period=1.0))
    assert Visibility.STRUCTURAL_ENDPOINT not in _states(rev)
    assert Visibility.SEARCHED_NO_SIGNAL in _states(rev)


def test_the_zone_centre_exemption_is_zone_local_and_not_a_blanket_amnesty():
    """An absence one fold away from a centre is still an absence. If proximity to *any* q could
    buy the exemption, the factor would quietly stop presence from ever pricing anything."""
    obs = _endpoint_scene(centre_h=1.4, mode_h=1.5, up_h=1.6)
    rev = build_scene(obs, sample=SampleKnowledge(zone_period=1.0))
    assert Visibility.STRUCTURAL_ENDPOINT not in _states(rev)


def test_the_endpoint_run_must_reach_the_centre_through_arenas_that_explain_the_silence():
    """The contiguity half of condition (c). A gapless branch reaching zero at a bare zone centre
    would have been SEEN there: without a chosen elastic component the arena explains nothing, the
    centre-ward run is broken, and the silence is still evidence."""
    obs = _endpoint_scene(neighbour="bg", kinds=())
    # bg-only neighbour, so the arenas are one cluster only via coverage coupling (item 15).
    rev = build_scene(obs, sample=SampleKnowledge(zone_period=1.0))
    assert cluster_of(rev, "e:1:d")["scan_refs"] == ["e:1:d", "e:2:d", "e:3:d"]
    assert Visibility.STRUCTURAL_ENDPOINT not in _states(rev)
    assert Visibility.SEARCHED_NO_SIGNAL in _states(rev)


def test_a_quasielastic_neighbour_blocks_a_low_band_but_earns_no_endpoint():
    """Review finding 6, all three halves of the QE/elastic split in one scene.

    A quasielastic feature is diffusive scattering, not a Bragg response. It DOES obscure a
    low-energy peak, so an absence under it is uninformative (``blocked_unresolved``, priced at
    zero) — but it is no elastic line for a gapless branch to be swallowed by, so the declared
    zone centre may not terminate the track there even when the track is a real descending one,
    and it is not a candidate reflection to be localized against the lattice at all.
    """
    obs = _endpoint_scene(neighbour="qe+bg", kinds=("quasielastic",), omega=0.4, up_omega=1.3)
    rev = build_scene(obs, sample=SampleKnowledge(zone_period=1.0, reflections=(1.0,)))

    # (b) the QE feature blocks the low-ω absence, and presence prices nothing.
    assert Visibility.BLOCKED_UNRESOLVED in _states(rev)
    cl = cluster_of(rev, "e:1:d")
    assert list(terms(cl, "presence").values()) == [2 * PRESENCE_MATCH_NATS]
    # (a) ... but the endpoint prerequisite is unmet, so the state is NOT terminated. The same
    # descending track into an ELASTIC arena under the same declaration is (the contrast is the
    # whole finding).
    assert Visibility.STRUCTURAL_ENDPOINT not in _states(rev)
    assert _endpoints(build_scene(_endpoint_scene(omega=0.4, up_omega=1.3),
                                  sample=SampleKnowledge(zone_period=1.0)))
    # (c) and it never enters the lattice classification — absent, not "unclassified", which keeps
    # its one meaning: an elastic component with nothing declared to compare it against.
    assert _classification(rev) == {}


def test_a_declared_slope_that_cannot_carry_the_track_to_zero_refuses_the_endpoint():
    """Condition (d), and the only honest form of the old energy gate. "3 meV is too high to be an
    acoustic endpoint" is not a statement the resolution can make — it is a statement about
    DISPERSION, so it needs a declared slope. At 2 meV/rlu a branch sheds 0.2 meV over the 0.1 rlu
    to the centre, which cannot account for 3 meV; with no slope declared the card's assertion that
    this fold carries a gapless branch is what licenses the reading, and the scene says so rather
    than substituting a resolution multiple for a dispersion bound."""
    obs = _endpoint_scene(omega=3.0, up_omega=3.9)
    bounded = build_scene(obs, sample=SampleKnowledge(zone_period=1.0, dispersion_slope_max=2.0))
    assert Visibility.STRUCTURAL_ENDPOINT not in _states(bounded)
    assert Visibility.SEARCHED_NO_SIGNAL in _states(bounded)
    undeclared = build_scene(obs, sample=SampleKnowledge(zone_period=1.0))
    assert _endpoints(undeclared)


def test_an_observed_mode_can_never_be_refined_away():
    """The constraint the builder enforces on every refiner, not on the zone factor's honour: a
    hook that could rewrite ``observed`` could argue a real detection out of existence, and one
    that could rewrite ``censored`` could turn a coverage gap into a measurement."""
    liar = replace(FACTORS["zone_centre_approach"],
                   refine_visibility=lambda report, alt, view: Visibility.OBSERVED)
    obs = _endpoint_scene()
    with pytest.raises(ValueError, match="manufacture a measurement"):
        build_scene(obs, factors=[FACTORS["presence"], liar],
                    sample=SampleKnowledge(zone_period=1.0))
    # A value that is not a visibility state at all fails the same way, naming the same factor —
    # not with the enum's own message, which names neither the factor nor the report.
    garbage = replace(liar, refine_visibility=lambda report, alt, view: "probably_fine")
    with pytest.raises(ValueError, match="zone_centre_approach.*probably_fine"):
        build_scene(obs, factors=[FACTORS["presence"], garbage],
                    sample=SampleKnowledge(zone_period=1.0))
    # ... and a refiner is not even consulted where the mode WAS seen: two matching modes leave
    # nothing for it to reinterpret.
    seen = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0)),
            mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(5.1,))], q=q(1.1))]
    rev = build_scene(seen, factors=[FACTORS["presence"], liar],
                      sample=SampleKnowledge(zone_period=1.0))
    assert _states(rev) == {Visibility.OBSERVED}


def _elastic_at(h, scan_ref="e:1:d"):
    return mk_obs(scan_ref, [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))], q=q(h))


def _classification(rev, cluster_id="e:1:d"):
    return cluster_of(rev, cluster_id)["classification"]["reciprocal_lattice_localization"]


def test_a_zone_centre_is_not_a_declared_reflection():
    """The FCC (300) pin, and the reason zone geometry and the reflection list are two
    declarations rather than one. (300) has integer indices, sits exactly on a fold of the
    reduced-zone period, and is EXTINCT — so an elastic feature at a periodic position is not
    thereby Bragg. Any shortcut that derived reflections from the period, from integer indices,
    or from a space group would classify this one wrong."""
    rev = build_scene([_elastic_at(3.0)],
                      sample=SampleKnowledge(zone_period=1.0, reflections=(2.0,)))
    assert _classification(rev) == {"e:1:d#elastic+bg#elastic": "off_lattice"}
    # The same position with the reflection actually declared is compatible with it.
    rev = build_scene([_elastic_at(3.0)],
                      sample=SampleKnowledge(zone_period=1.0, reflections=(2.0, 3.0)))
    assert _classification(rev) == {"e:1:d#elastic+bg#elastic": "bragg_compatible"}


def test_a_reflection_list_is_a_set_of_positions_however_it_was_typed():
    """The declaration is a SET: order and repetition are typing, not physics. Left verbatim they
    would digest to different revisions, so two cards saying the same thing would look
    unreplayable against each other — and the difference would be invisible to any reader."""
    assert SampleKnowledge(reflections=(3.0, 2.0, 2.0)).reflections == (2.0, 3.0)
    obs = [_elastic_at(2.0)]
    canonical = build_scene(obs, sample=SampleKnowledge(reflections=(2.0, 3.0)))
    for typed in ((3.0, 2.0), (2.0, 3.0, 2.0), (3.0, 3.0, 2.0)):
        assert build_scene(obs, sample=SampleKnowledge(reflections=typed)).digest == \
            canonical.digest


def test_an_unplaceable_arena_names_its_own_reason_rather_than_going_quiet():
    """The degradation ledger keys on the DECLARATION, which cannot see the arenas — so an
    unplaceable q under a perfectly good reflection list would record "unclassified" with no
    reason recorded anywhere. It gets a state of its own instead."""
    obs = [mk_obs("e:1:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))], q=None)]
    rev = build_scene(obs, sample=SampleKnowledge(reflections=(2.0,)))
    assert _classification(rev) == {"e:1:d#elastic+bg#elastic": "unplaceable_q"}
    assert rev.payload["sample_knowledge"]["degradations"] == ["no_declared_zone_period"]


def test_modes_are_not_lattice_classified():
    """A phonon at a reflection's q is a phonon. Classifying it as Bragg-compatible would put a
    lattice identity on an inelastic feature."""
    obs = [mk_obs("e:1:d", [cand("elastic+stokes+bg", -10.0, omegas=(5.0,), extra=("elastic",))], q=q(2.0))]
    rev = build_scene(obs, sample=SampleKnowledge(reflections=(2.0,)))
    assert _classification(rev) == {"e:1:d#elastic+stokes+bg#elastic": "bragg_compatible"}


def test_undeclared_knowledge_is_inert_and_says_so():
    """Absence of a declaration must never raise, never move a number, and never be silent. An
    unclassified component is a question that was never asked, not an off-lattice answer."""
    rev = build_scene([_elastic_at(2.0)])
    assert rev.payload["sample_knowledge"] == {
        "zone_period": None, "reflections": [], "dispersion_slope_max": None,
        "lattice_epoch": None, "lattice": None,
        "degradations": ["no_declared_reflections", "no_declared_zone_period"]}
    assert _classification(rev) == {"e:1:d#elastic+bg#elastic": "unclassified"}

    declared = build_scene([_elastic_at(2.0)],
                           sample=SampleKnowledge(zone_period=1.0, reflections=(2.0,),
                                                  lattice_epoch=3,
                                                  lattice=(4.05, 4.05, 4.05, 90.0, 90.0, 90.0)))
    assert declared.payload["sample_knowledge"]["degradations"] == []
    assert declared.payload["sample_knowledge"]["lattice_epoch"] == 3
    # Declaring knowledge is a different scene even where it changes no number here: a revision
    # scored blind to the lattice must never look replayable against one that was not.
    assert declared.digest != rev.digest


def test_a_stale_epoch_partition_gets_no_lattice_semantics():
    """PR #30 review response (disposition 12): an arena EXPLICITLY stamped a lattice epoch other
    than the CURRENTLY declared one must not receive the current frame's classification — a
    stale-epoch elastic feature is not necessarily near THIS epoch's declared reflection, and
    classifying it as though it were would silently misattribute a re-orientation. A different
    partition (``lattice_epoch`` is part of the partition key), so the two arenas never share a
    cluster and the contrast is entirely in what each one's OWN cluster is handed."""
    current = _elastic_at(2.0, scan_ref="e:1:d")
    stale = replace(_elastic_at(2.0, scan_ref="e:2:d"), lattice_epoch=2)
    current = replace(current, lattice_epoch=1)
    rev = build_scene([current, stale],
                      sample=SampleKnowledge(zone_period=1.0, reflections=(2.0,), lattice_epoch=1))
    assert _classification(rev, "e:1:d") == {"e:1:d#elastic+bg#elastic": "bragg_compatible"}
    assert _classification(rev, "e:2:d") == {"e:2:d#elastic+bg#elastic": "unclassified"}
    assert "knowledge_epoch_mismatch" in rev.payload["sample_knowledge"]["degradations"]


def test_a_stale_epoch_partition_gets_no_zone_centre_termination_either():
    """The same mismatch, on the OTHER knowledge-consuming factor: a track that would terminate at
    a declared zone centre under the current epoch stays a plain, charged absence when its arena
    is stamped a different one — ``structural_endpoint`` never fires, and the ordinary
    ``searched_no_signal`` miss the un-declared case already charges stands."""
    stale_obs = [replace(o, lattice_epoch=2) for o in _endpoint_scene()]
    current = build_scene(_endpoint_scene(), sample=SampleKnowledge(zone_period=1.0, lattice_epoch=1))
    assert Visibility.STRUCTURAL_ENDPOINT in _states(current)

    stale = build_scene(stale_obs, sample=SampleKnowledge(zone_period=1.0, lattice_epoch=1))
    assert Visibility.STRUCTURAL_ENDPOINT not in _states(stale)
    assert Visibility.SEARCHED_NO_SIGNAL in _states(stale)
    assert "knowledge_epoch_mismatch" in stale.payload["sample_knowledge"]["degradations"]


def test_the_classify_only_factor_costs_the_builder_no_pruning():
    """H12(c) in the other direction, RE-PINNED for #22 (was
    ``test_the_classify_only_factors_cost_the_builder_no_pruning``, which pinned the unsafe
    assumption below).

    A ``classify``-only factor (``reciprocal_lattice_localization``: no score, no
    ``refine_visibility``) still costs nothing — ``_score_span`` correctly ignores it, and adding
    it alone must leave margin pruning exactly as it was. A ``refine_visibility`` factor
    (``zone_centre_approach``) is different: it has no score of its own either, but its hook can
    turn a report ``presence`` would have charged into a free one, so its addition WIDENS the
    span by ``rescue_bound_per_report`` (== ``-PRESENCE_MISS_NATS``) per refinable report — the
    bug #22 named: the old span left that rescue capacity off the books entirely."""
    obs = [mk_obs("e:1:d", [cand("A", -10.0, omegas=(5.0,)),
                            cand("B", -400.0, omegas=(5.0,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("C", -10.0, omegas=(5.0,))], q=q(1.1))]
    bounded = replace(FACTORS["presence"], score_bound=(-5.0, 5.0))
    without = build_scene(obs, factors=[bounded])
    classify_only = build_scene(obs, factors=[bounded, FACTORS["reciprocal_lattice_localization"]])
    with_refiner = build_scene(obs, factors=[bounded, FACTORS["zone_centre_approach"],
                                             FACTORS["reciprocal_lattice_localization"]])
    assert without.payload["score_span_nats"] == classify_only.payload["score_span_nats"] == 10.0
    # 1 mode component per arena's usable candidates x 1 adjacent neighbour each = 2 refinable
    # reports, at 1.0 nat apiece (-PRESENCE_MISS_NATS).
    assert with_refiner.payload["score_span_nats"] == 12.0
    assert cluster_of(without, "e:1:d")["pruning"] == cluster_of(classify_only, "e:1:d")["pruning"]
    # The widened margin is real slack, not cosmetic: B sits 390 nats down, comfortably excluded
    # under either effective margin here, so all three scenes still prune it identically — a
    # fixture where the widened span actually SURVIVES a candidate the bare one would have
    # dropped lives in test_a_multi_report_endpoint_rescue_survives_the_widened_span below.
    assert [e["label"] for e in cluster_of(with_refiner, "e:1:d")["pruning"]["e:1:d"]] == ["B"]


def test_a_multi_report_endpoint_rescue_survives_the_widened_span():
    """#22 acceptance: "one rescued report per alternative is not the bound". Two mirrored
    descending tracks converge on the SAME declared zone centre, offering the refiner SIX
    refinable reports total (one mode component per non-centre arena, most with two path
    neighbours), counted globally over every usable candidate before any pruning
    (``_max_refinable_reports``) — not the one report a per-alternative accounting would see. The
    widened span is therefore ``6 x -PRESENCE_MISS_NATS == 6.0`` nats, and a candidate 3.5 nats
    down in an unrelated partition — safely inside six reports' worth of rescue, past what even
    two would cover — survives margin pruning on that strength alone.

    ``coupler`` keeps presence's ``couples``/``couples_arena`` (what makes the five endpoint
    arenas one cluster at all) while dropping its ``score``, so this scene's ONLY scoring
    contributor to the span is the refiner under test.
    """
    centre = mk_obs("e:1:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))],
                    q=q(1.0))
    right_near = mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(1.5,))], q=q(1.1))
    right_far = mk_obs("e:3:d", [cand("stokes+bg", -10.0, omegas=(2.4,))], q=q(1.2))
    left_near = mk_obs("e:0:d", [cand("stokes+bg", -10.0, omegas=(1.5,))], q=q(0.9))
    left_far = mk_obs("e:-1:d", [cand("stokes+bg", -10.0, omegas=(2.4,))], q=q(0.8))
    # A wholly separate PARTITION (#28), so the pruning target below can never couple, cluster,
    # or path-adjoin the endpoint fixture — its only connection to it is the GLOBAL span.
    target = mk_obs("p:1:d", [cand("lead", 0.0, omegas=(5.0,)),
                              cand("rescued", -3.5, omegas=(5.0,))],
                    q=q(9.0), conditions=(("temperature_K", 400.0),))
    coupler = replace(FACTORS["presence"], score=None)
    rev = build_scene([centre, right_near, right_far, left_near, left_far, target],
                      factors=[coupler, FACTORS["zone_centre_approach"]],
                      sample=SampleKnowledge(zone_period=1.0), margin_nats=0.0)
    assert rev.payload["score_span_nats"] == 6.0
    cl = cluster_of(rev, "p:1:d")
    assert cl["pruning"]["p:1:d"] == []
    assert {a["assignment"][0][1] for a in cl["alternatives"]} == {"lead", "rescued"}


def test_transformation_ablation_is_distinct_from_score_ablation():
    """#22 acceptance: the revision exposes how much the endpoint reading moved posterior odds
    INDEPENDENTLY of presence. The reading adds no score term of its own — it rewrites what
    presence reads — so ``score_ablation`` (which only ever zeroes an EXISTING term) has nothing
    named ``zone_centre_approach`` to remove; ``transformation_ablation`` answers the question
    score_ablation cannot: what would the posterior have been had the rewrite never fired."""
    obs = [mk_obs("e:1:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))],
                 q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(1.5,)),
                            cand("bg", -10.5, n_modes=0, omegas=())], q=q(1.1)),
           mk_obs("e:3:d", [cand("stokes+bg", -10.0, omegas=(2.4,))], q=q(1.2))]
    rev = build_scene(obs, factors=[FACTORS["presence"], FACTORS["zone_centre_approach"]],
                      sample=SampleKnowledge(zone_period=1.0))
    cl = cluster_of(rev, "e:1:d")
    assert "zone_centre_approach" in cl["transformation_ablation"]
    assert "zone_centre_approach" not in cl["score_ablation"]     # declares no score term at all
    at_e2 = {label: (a["posterior"], p) for a, p in
            zip(cl["alternatives"], cl["transformation_ablation"]["zone_centre_approach"])
            for ref, label in a["assignment"] if ref == "e:2:d"}
    with_reading, without_reading = at_e2["stokes+bg"]
    # With the reading: presence charges only the two observed continuations (2 x MATCH). Without
    # it: the same track's absence at the centre is still charged a MISS, exactly as blind.
    assert with_reading == pytest.approx(1.0 / (1.0 + math.exp(-2.5)), rel=1e-9)
    assert without_reading == pytest.approx(1.0 / (1.0 + math.exp(-1.5)), rel=1e-9)
    assert with_reading > without_reading
    assert rev.payload["transformation_ablation_alignment"] == "by_alternative_index"


@pytest.mark.parametrize("bad,match", [
    ({"zone_period": 0.0}, "zone_period"),
    ({"zone_period": -1.0}, "zone_period"),
    ({"zone_period": math.inf}, "zone_period"),
    ({"reflections": (math.nan,)}, "reflections"),
])
def test_a_malformed_declaration_fails_loudly(bad, match):
    """H1: no tolerant fallback. A card that says something unreadable about the sample is a card
    to fix — degrading it to silence would leave the ledger claiming nothing was ever declared."""
    with pytest.raises(ValueError, match=match):
        SampleKnowledge(**bad)


def test_neighbour_reports_are_computed_once_per_alternative():
    """Carried item 16. Three consumers walked the same arenas three times over identical inputs;
    the memo is scoped to the cluster view, so it dies with the build rather than handing one
    revision's reports to the next."""
    view = ClusterView(cluster_id="c", scan_refs=("e:1:d",),
                       components=MappingProxyType({"e:1:d": ()}),
                       footprints=MappingProxyType({"e:1:d": footprint_of(
                           mk_obs("e:1:d", [cand("A", -1.0)]))}))
    alt = SceneAlternative(cluster_id="c", assignment=(("e:1:d", "A"),), components=())
    first = neighbour_reports(alt, view)
    assert neighbour_reports(alt, view) is first
    assert list(view.reports_cache) == [alt]


# ----------------------------------------------------------------------------
# Part 6 — the path topology, the reach grammar and the detectability gates
# ----------------------------------------------------------------------------
def _spurion_at(h, omega=8.0, scan_ref="e:1:d"):
    return mk_obs(scan_ref, [cand("stokes+bg", -10.0, omegas=(omega,))], q=q(h))


def _empty_at(h, scan_ref, **kw):
    return mk_obs(scan_ref, [cand("bg", -10.0, n_modes=0, omegas=())], q=q(h), **kw)


def test_only_consecutive_arenas_judge_each_other():
    """Review finding 3, as the regression the owner named. Under a radius in q, a and c stayed
    "neighbours" after an intermediate scan b was inserted between them, so one absence was priced
    twice — once against b and once against c, over the same stretch of path. Adjacency is now the
    EDGE of the path, so inserting b replaces the (a, c) judgement with (a, b); no skip-neighbour
    term survives."""
    direct = cluster_of(build_scene([_spurion_at(1.0), _empty_at(1.2, "e:2:d")]), "e:1:d")
    assert {r["neighbour"] for r in direct["visibility"]} == {"e:2:d"}
    assert list(terms(direct, "presence").values()) == [PRESENCE_MISS_NATS]

    inserted = cluster_of(build_scene([_spurion_at(1.0), _empty_at(1.1, "e:2:d"),
                                       _empty_at(1.2, "e:3:d")]), "e:1:d")
    # ONE judgement, against the arena the path actually reaches next — not two. (e:3:d holds no
    # component and no mode reaches it, so it is a cluster of its own; what matters here is that
    # the mode at e:1:d no longer reports against it.)
    assert {r["neighbour"] for r in inserted["visibility"]} == {"e:2:d"}
    assert list(terms(inserted, "presence").values()) == [PRESENCE_MISS_NATS]


def test_a_constant_h_path_along_k_orders_and_couples_like_any_other():
    """The failure that made h-as-path-coordinate a P0: a campaign walking (2,2,0) → (2,0,0) moves
    along k at constant h, so ranking on h collapses every arena onto one point — no ordering, and
    under a |Δh| radius every arena "adjacent" to every other. The path is derived from the q set,
    so this one orders and couples exactly like an h scan."""
    obs = [mk_obs(f"e:{i}:d", [cand("stokes+bg", -10.0, omegas=(5.0 + 0.1 * i,))],
                  q=(2.0, 2.0 - 0.1 * i, 0.0)) for i in range(3)]
    cl = cluster_of(build_scene(obs), "e:0:d")
    assert cl["scan_refs"] == ["e:0:d", "e:1:d", "e:2:d"]
    # Consecutive along k, so the ends do not judge each other.
    assert {(r["scan_ref"], r["neighbour"]) for r in cl["visibility"]} == {
        ("e:0:d", "e:1:d"), ("e:1:d", "e:0:d"), ("e:1:d", "e:2:d"), ("e:2:d", "e:1:d")}


def test_declared_geometry_is_read_along_the_path_axis_not_along_h():
    """The same failure, on the declared side. On a path along k every arena shares h = 2.0, so a
    reflection declared at 2.0 would make EVERY arena Bragg-compatible if the scene compared h.
    The coordinate is the rlu along the axis the path runs on, so the declaration lands where the
    scientist meant it."""
    obs = [mk_obs(f"e:{i}:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))],
                  q=(2.0, 0.1 * i, 0.0)) for i in range(3)]
    on_h = build_scene(obs, sample=SampleKnowledge(reflections=(2.0,)))
    assert set(_classification(on_h, "e:0:d").values()) == {"off_lattice"}
    on_k = build_scene(obs, sample=SampleKnowledge(reflections=(0.0,)))
    assert _classification(on_k, "e:0:d") == {"e:0:d#elastic+bg#elastic": "bragg_compatible",
                                              "e:1:d#elastic+bg#elastic": "off_lattice",
                                              "e:2:d#elastic+bg#elastic": "off_lattice"}


def test_a_scattered_q_set_fails_closed_with_a_recorded_reason():
    """The fail-closed arm. Three q that are not a path have no ordering, so there is no honest
    adjacency to derive: every arena is its own cluster, nothing cross-arena is priced, and the
    build says why rather than inventing an order and pricing absences against it."""
    obs = [_spurion_at(1.0), mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())],
                                    q=(1.1, 0.4, 0.0)),
           mk_obs("e:3:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=(1.2, -0.3, 0.2))]
    rev = build_scene(obs)
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"], ["e:2:d"], ["e:3:d"]]
    assert "path_topology_unavailable" in rev.payload["sample_knowledge"]["degradations"]


def test_an_off_axis_path_keeps_its_topology_and_loses_its_declared_geometry():
    """A collinear path that is not a reciprocal axis is still a path — it orders and couples. What
    it cannot do is carry the card's SCALAR zone period and reflection positions, which are rlu
    along an axis: (2,2,0) → (2,0,0) is honest here only because it runs along k, and a diagonal
    is not. The capability goes inert with a reason instead of picking a component to pretend
    with."""
    obs = [mk_obs(f"e:{i}:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=(), extra=("elastic",))],
                  q=(1.0 + 0.1 * i, 1.0 + 0.1 * i, 0.0)) for i in range(3)]
    rev = build_scene(obs, sample=SampleKnowledge(zone_period=1.0, reflections=(1.0,)))
    assert cluster_of(rev, "e:0:d")["scan_refs"] == ["e:0:d", "e:1:d", "e:2:d"]
    assert rev.payload["sample_knowledge"]["degradations"] == [
        "lattice_geometry_inapplicable_on_path", "zone_geometry_inapplicable_on_path"]
    assert set(_classification(rev, "e:0:d").values()) == {"path_axis_unavailable"}


@pytest.mark.parametrize("offset,collinear", [(0.005, True), (0.02, False)])
def test_collinearity_is_judged_at_the_q_positioning_scale(offset, collinear):
    """The tolerance is a positioning tolerance, not a grid one: sub-step jitter about a line is
    the same path, and a deliberate step onto another row of the 0.05 rlu HKL grid is not."""
    obs = [_spurion_at(1.0), mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())],
                                    q=(1.1, offset, 0.0)),
           mk_obs("e:3:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=(1.2, 0.0, 0.0))]
    degradations = build_scene(obs).payload["sample_knowledge"]["degradations"]
    assert ("path_topology_unavailable" not in degradations) is collinear


def test_a_declared_slope_links_a_track_the_resolution_alone_would_break():
    """Review finding 5. Two arenas 0.1 rlu apart hold peaks 2 meV apart, which is twice the
    resolution — so nearest-ω chaining on the resolution width calls them different tracks. But
    energy resolution says how well two peaks are TOLD APART and nothing about how far a branch
    MOVED: at a declared 25 meV/rlu a branch sheds 2.5 meV over that edge, and the continuation is
    reachable. Only a declared dispersion support may say so."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(4.0,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(6.0,))], q=q(1.1))]
    blind, = cluster_of(build_scene(obs), "e:1:d")["alternatives"]
    assert blind["links"] == []
    steep, = cluster_of(build_scene(obs, sample=SampleKnowledge(dispersion_slope_max=25.0)),
                        "e:1:d")["alternatives"]
    assert steep["links"] == [["e:1:d#stokes+bg#0", "e:2:d#stokes+bg#0"]]
    assert steep["factor_terms"]["presence"] == 2 * PRESENCE_MATCH_NATS


def test_without_a_declared_slope_a_neighbour_holding_other_peaks_charges_nothing():
    """The other half of finding 5, and the one that used to charge. The neighbour looked and holds
    a peak — at 2 meV, not at 8. Charging the 8 meV mode as missing asserts that the track should
    have reappeared near where it was, which is the smoothness assumption an undeclared dispersion
    gives the scene no right to make. An absent partner at ANY ω is a different fact, and a
    neighbour with no modes at all is still charged."""
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(8.0,))], q=q(1.0))
    holds_other = mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(2.0,))], q=q(1.1))
    cl = cluster_of(build_scene([seen, holds_other]), "e:1:d")
    report, = [r for r in cl["visibility"] if r["component_id"].startswith("e:1:d")]
    assert report["visibility"] == Visibility.BLOCKED_UNRESOLVED
    assert report["reason"] == "reachability_unknown"
    assert list(terms(cl, "presence").values()) == [0.0]
    # Declare the support and the same silence is evidence again.
    declared = cluster_of(build_scene([seen, holds_other], sample=SLOPE), "e:1:d")
    # Both silences are evidence now: neither arena holds the other's peak.
    assert list(terms(declared, "presence").values()) == [2 * PRESENCE_MISS_NATS]
    # ... and an entirely empty neighbour never needed the declaration: nothing was there at any ω.
    empty = cluster_of(build_scene([seen, _empty_at(1.1, "e:2:d")]), "e:1:d")
    assert list(terms(empty, "presence").values()) == [PRESENCE_MISS_NATS]


def _anti_stokes_only(scan_ref, h, temperature):
    """A neighbour whose kept axis lies entirely on the ENERGY-GAIN side."""
    return _empty_at(h, scan_ref, window=(-10.0, -0.5), temperature=temperature)


def test_a_bose_dead_anti_stokes_band_cannot_exclude_anything():
    """Review finding 2, warm-and-cold. The neighbour looked at |ω| = 5 meV — but only from the
    anti-Stokes side, whose intensity is the Stokes side's times exp(−ω/k_BT). At 300 K that is
    0.82 and the absence is real evidence; at 10 K it is 0.003, so the band could not have shown
    the mode however long anyone counted, and calling it searched-and-empty would price a
    measurement nobody could make."""
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    warm = cluster_of(build_scene([seen, _anti_stokes_only("e:2:d", 1.1, 300.0)]), "e:1:d")
    assert list(terms(warm, "presence").values()) == [PRESENCE_MISS_NATS]

    cold = cluster_of(build_scene([seen, _anti_stokes_only("e:2:d", 1.1, 10.0)]), "e:1:d")
    report, = [r for r in cold["visibility"] if r["component_id"].startswith("e:1:d")]
    assert report["visibility"] == Visibility.BLOCKED_UNRESOLVED
    assert report["reason"] == "bose_dead"
    assert list(terms(cold, "presence").values()) == [0.0]
    # An unknown temperature establishes nothing either, and says which of the two it is.
    unknown = cluster_of(build_scene([seen, _anti_stokes_only("e:2:d", 1.1, None)]), "e:1:d")
    assert [r["reason"] for r in unknown["visibility"]
            if r["component_id"].startswith("e:1:d")] == ["bose_unknown"]


@pytest.mark.parametrize("exposure,reason", [
    (1.0, None), (0.5, None), (0.05, "insufficient_detection_power"),
    (None, "detection_power_unknown")])
def test_absence_is_evidence_only_from_a_comparably_deep_look(exposure, reason):
    """The counting-power half of finding 2. A glance that saw nothing is not the measurement a
    long count that saw nothing is, and geometric inclusion cannot tell them apart. Comparable is
    order-of-magnitude (``DETECT_EXPOSURE_RATIO``), not equal — half the exposure still excludes,
    a twentieth does not, and an unrecorded exposure establishes nothing at all."""
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    cl = cluster_of(build_scene([seen, _empty_at(1.1, "e:2:d", exposure=exposure)]), "e:1:d")
    report, = [r for r in cl["visibility"] if r["component_id"].startswith("e:1:d")]
    assert report["reason"] == reason
    assert list(terms(cl, "presence").values()) == \
        [PRESENCE_MISS_NATS if reason is None else 0.0]


# ----------------------------------------------------------------------------
# Determinism and the registry declaration
# ----------------------------------------------------------------------------
def test_determinism_with_the_live_registry():
    obs = _chain([cand("stokes+bg", -10.0, omegas=(5.1,)), cand("bg", -11.0, n_modes=0, omegas=())])
    permuted = [replace(o, candidates=tuple(reversed(o.candidates))) for o in reversed(obs)]
    assert build_scene(obs).digest == build_scene(permuted).digest
    assert build_scene(obs).canonical == build_scene(obs).canonical


def test_the_live_registry_is_the_four_physics_factors():
    assert sorted(FACTORS) == ["connectivity", "presence", "reciprocal_lattice_localization",
                               "zone_centre_approach"]
    manifest = {m["name"]: m for m in factor_manifest(FACTORS)}
    assert manifest["presence"]["score_bound"] is None
    assert dict(manifest["presence"]["config"])["presence_miss_nats"] == PRESENCE_MISS_NATS
    # Review finding 8: connectivity shapes the hypothesis space (edges, crossing proposals) and
    # prices nothing. Its one term charged the absence of a neighbouring component, which is the
    # null presence already charges as a miss, and no topology-only term is left to replace it —
    # so the manifest is where a reader sees that it moves no belief.
    assert manifest["connectivity"]["roles"] == ["couples", "propose"]
    assert "connect_break_nats" not in dict(manifest["connectivity"]["config"])
    assert manifest["presence"]["roles"] == ["couples", "score", "couples_arena"]
    # The two knowledge factors move no belief, so neither declares a score — and the manifest
    # is where a reader can see that rather than having to trust it.
    assert manifest["zone_centre_approach"]["roles"] == ["refine_visibility", "degradations"]
    assert manifest["reciprocal_lattice_localization"]["roles"] == ["classify", "degradations"]
    assert manifest["zone_centre_approach"]["consumes"] == [
        "q", "omega", "footprint", "links", "sample.zone_period", "sample.dispersion_slope_max"]
    assert manifest["reciprocal_lattice_localization"]["consumes"] == ["q", "sample.reflections"]


def test_retuning_a_nats_constant_moves_the_registry_fingerprint():
    """A retuned weight is a different factor: two revisions scored under different tunings must
    never carry the same fingerprint and look replayable against each other."""
    retuned = replace(FACTORS["presence"],
                      config=tuple((k, -2.0 if k == "presence_miss_nats" else v)
                                   for k, v in FACTORS["presence"].config))
    assert registry_fingerprint([retuned, FACTORS["connectivity"]]) != \
        registry_fingerprint(FACTORS)


def test_margin_pruning_is_disabled_while_the_unbounded_factors_are_active():
    """The declared consequence of ``score_bound=None`` (H12(c)): nothing may be discarded on base
    lnZ, because an unbounded term could rescue it. A candidate 150 nats down survives."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,)),
                            cand("stokes2+bg", -160.0, n_modes=2, omegas=(5.0, 8.0))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes+bg", -10.0, omegas=(5.1,))], q=q(1.1))]
    rev = build_scene(obs)
    assert rev.payload["score_span_nats"] is None
    cl = cluster_of(rev, "e:1:d")
    assert cl["pruning"]["e:1:d"] == []
    assert {a["assignment"][0][1] for a in cl["alternatives"]} == {"stokes+bg", "stokes2+bg"}
    # It survives, and it still loses on its own evidence: 150 nats is not rescued by ±few nats.
    assert math.isclose(sum(a["posterior"] for a in cl["alternatives"]
                            if a["assignment"][0][1] == "stokes+bg"), 1.0, abs_tol=1e-9)
