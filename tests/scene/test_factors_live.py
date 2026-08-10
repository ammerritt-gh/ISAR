"""The live physics factors: presence, connectivity, and the machinery they need (PR 3b).

Every test here is a physical claim rather than a snapshot: a phonon does not appear at one q
and vanish at the next, absence is evidence ONLY where the neighbour actually looked, and which
peak belongs to which track is never decided by ω rank. Observations are hand-built — no fitter,
no workspace — so a failure here is a failure of the factor, not of a fit.
"""
import math
from dataclasses import replace

import pytest

from isar.scene import build as build_mod
from isar.scene.build import build_scene
from isar.scene.factors import (CONNECT_BREAK_NATS, FACTORS, PRESENCE_MATCH_NATS,
                                PRESENCE_MISS_NATS, factor_manifest, registry_fingerprint)
from isar.scene.model import (Visibility, footprint_of, observation_fingerprint,
                              visibility_of)

from .test_build import cand, cluster_of, mk_obs


def q(h):
    return (h, 0.0, 0.0)


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
    obs = [mk_obs("e:1:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=())], q=q(1.0)),
           mk_obs("e:2:d", [cand("elastic+bg", -11.0, n_modes=0, omegas=())], q=q(1.1))]
    rev = build_scene(obs)
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d", "e:2:d"]]


def test_a_bg_only_candidate_emits_nothing_and_couples_to_nothing():
    """Truly nothing there: no elastic term, no mode, so no component to couple on."""
    obs = [mk_obs("e:1:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -11.0, n_modes=0, omegas=())], q=q(1.1))]
    rev = build_scene(obs)
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"], ["e:2:d"]]


@pytest.mark.parametrize("label,tags", [
    ("bg", []),
    ("elastic+bg", ["elastic"]),
    ("stokes+bg", ["0"]),
    ("elastic+stokes+qe+bg", ["0", "elastic", "qe"]),
    ("stokes2+bg@acoustic", ["0", "1"]),
    ("elastic+stokes2+bg@acoustic_neardeg", ["0", "1", "elastic"]),
])
def test_the_label_grammar_places_the_non_mode_components(label, tags):
    """Identity (``@acoustic``) and variant (``_neardeg``) suffixes land on the LAST term, and
    ``elastic``/``qe`` are never last — so they survive both."""
    n = sum(1 for t in tags if t.isdigit())
    obs = mk_obs("e:1:d", [cand(label, -10.0, n_modes=n, omegas=tuple(4.0 + i for i in range(n)))])
    comps = build_mod._components_of(obs, footprint_of(obs), frozenset({label}))
    assert [c.component_id.rsplit("#", 1)[-1] for c in comps] == tags
    assert all(c.omega == 0.0 for c in comps if not c.component_id.rsplit("#", 1)[-1].isdigit())


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
    assert visibility_of(0.5, fp, False, elastic_present=True) == Visibility.BLOCKED_UNRESOLVED
    # No elastic component there: the same band was genuinely searched.
    assert visibility_of(0.5, fp, False) == Visibility.SEARCHED_NO_SIGNAL
    # Outside the elastic width, the elastic line hides nothing.
    assert visibility_of(4.0, fp, False, elastic_present=True) == Visibility.SEARCHED_NO_SIGNAL
    # A detection under the elastic line stays a detection.
    assert visibility_of(0.5, fp, True, elastic_present=True) == Visibility.OBSERVED


def test_structural_endpoint_is_reserved_for_the_zone_centre_factor():
    """It exists in the vocabulary and nothing assigns it yet — the next task's job."""
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
    blind = replace(narrow, energy_window=(-3.0, 3.0))
    blind = replace(blind, fingerprint=observation_fingerprint(replace(blind, fingerprint="")))

    censored = cluster_of(build_scene([wide, blind]), "e:1:d")
    assert visibility_of(8.0, footprint_of(blind), False) == Visibility.CENSORED_OUTSIDE_FOOTPRINT
    # The 8 meV mode was never looked at, so the two readings of e:1:d score identically: the
    # gap moved belief in neither direction.
    scored = terms(censored, "presence")
    assert len(set(scored.values())) == 1

    # Same arenas, the neighbour now looking at 2 meV as well: the absence becomes evidence, and
    # the extra mode costs exactly one miss.
    searched = terms(cluster_of(build_scene([wide, narrow]), "e:1:d"), "presence")
    by_label = {a[0][1]: t for (a, _links), t in searched.items()}
    assert by_label["stokes+bg"] == PRESENCE_MATCH_NATS * 2
    assert by_label["stokes2+bg"] == PRESENCE_MATCH_NATS * 2 + PRESENCE_MISS_NATS


def test_a_one_q_spurion_earns_no_cross_support_but_survives():
    """No neighbour saw it, so it gets no reward and a miss — and it is still enumerated with a
    real posterior. The scene weights hypotheses; it does not delete them."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(8.0,)),
                            cand("bg", -10.5, n_modes=0, omegas=())], q=q(1.0)),
           mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.1))]
    # The bg-only neighbour emits no component, so nothing couples: the spurion arena is its own
    # cluster and its local evidence stands untouched.
    rev = build_scene(obs)
    cl = cluster_of(rev, "e:1:d")
    assert cl["scan_refs"] == ["e:1:d"]
    posterior = {a["assignment"][0][1]: a["posterior"] for a in cl["alternatives"]}
    assert posterior["stokes+bg"] > 0.0 and posterior["bg"] > 0.0
    assert all(t == 0.0 for t in terms(cl, "presence").values())


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
    assert alt["factor_terms"]["connectivity"] == 0.0
    # One track: each adjacent pair linked once (1-2 and 2-3; 1 and 3 are not adjacent).
    assert len(alt["links"]) == 2
    assert alt["factor_terms"]["presence"] == 4 * PRESENCE_MATCH_NATS


def test_a_track_that_vanishes_mid_cluster_pays_one_break_per_searched_neighbour():
    """The middle arena looked at 5 meV and (on this reading) found nothing there, so both
    neighbours' tracks terminate into a searched, covered, empty band."""
    obs = _chain([cand("stokes+bg", -10.0, omegas=(5.1,)), cand("bg", -10.0, n_modes=0, omegas=())])
    cl = cluster_of(build_scene(obs), "e:1:d")
    by_middle = {a["assignment"][1][1]: a for a in cl["alternatives"]}
    assert by_middle["bg"]["factor_terms"]["connectivity"] == 2 * CONNECT_BREAK_NATS
    assert by_middle["stokes+bg"]["factor_terms"]["connectivity"] == 0.0
    assert len(by_middle["bg"]["reasons"]["connectivity"]) == 2
    assert by_middle["stokes+bg"]["posterior"] > by_middle["bg"]["posterior"]


def test_an_absence_under_the_elastic_line_is_not_a_break():
    """The middle arena's chosen candidate is elastic-only, and the track sits inside the elastic
    width: blocked, not broken — no penalty from either factor."""
    obs = [mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(0.4,))], q=q(1.0)),
           mk_obs("e:2:d", [cand("elastic+bg", -10.0, n_modes=0, omegas=())], q=q(1.1))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    alt, = cl["alternatives"]
    assert alt["factor_terms"] == {"connectivity": 0.0, "presence": 0.0}
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
    they share a base Σ lnZ exactly, neither has a break, so belief splits 50/50 — and the ledger
    itemizes both rather than reporting one winner."""
    cl = cluster_of(build_scene(_crossing_scene()), "e:1:d")
    assert cl["n_assignments"] == 1 and cl["n_enumerated"] == 2
    assert cl["linkage_truncated"] is False
    a, b = cl["alternatives"]
    assert a["links"] != b["links"]
    assert a["base"] == b["base"]
    assert a["factor_terms"]["connectivity"] == b["factor_terms"]["connectivity"] == 0.0
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


def test_well_separated_branches_are_not_treated_as_ambiguous():
    """The proposer reaches only as far as the dispersion plausibly could: two branches 6 meV
    apart at both arenas have one reading, not two."""
    obs = [mk_obs("e:1:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(1.0, 9.0))], q=q(1.0)),
           mk_obs("e:2:d", [cand("stokes2+bg", -10.0, n_modes=2, omegas=(1.2, 9.2))], q=q(1.1))]
    cl = cluster_of(build_scene(obs), "e:1:d")
    assert cl["n_enumerated"] == 1


# ----------------------------------------------------------------------------
# Determinism and the registry declaration
# ----------------------------------------------------------------------------
def test_determinism_with_the_live_registry():
    obs = _chain([cand("stokes+bg", -10.0, omegas=(5.1,)), cand("bg", -11.0, n_modes=0, omegas=())])
    permuted = [replace(o, candidates=tuple(reversed(o.candidates))) for o in reversed(obs)]
    assert build_scene(obs).digest == build_scene(permuted).digest
    assert build_scene(obs).canonical == build_scene(obs).canonical


def test_the_live_registry_is_the_two_physics_factors():
    assert sorted(FACTORS) == ["connectivity", "presence"]
    manifest = {m["name"]: m for m in factor_manifest(FACTORS)}
    assert manifest["presence"]["score_bound"] is None
    assert manifest["connectivity"]["score_bound"] is None
    assert dict(manifest["presence"]["config"])["presence_miss_nats"] == PRESENCE_MISS_NATS
    assert dict(manifest["connectivity"]["config"])["connect_break_nats"] == CONNECT_BREAK_NATS
    assert manifest["connectivity"]["roles"] == ["couples", "score", "propose"]


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
