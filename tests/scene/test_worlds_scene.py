"""The SCENE worlds put to the scene layer itself, on real evidence (SCENE-3 PR 3b).

Unlike ``test_worlds_fast.py``, which pins the generator, and unlike ``test_factors_live.py``,
which hand-builds arenas, this file runs the whole path: world → counts → fits → evidence
selection → candidate rows → scene. It is the only place a scene assertion is made against
numbers no test author chose.

Slow by construction (the fits are real), so slow-marked.
"""
import pytest

from isar.analyze import verdict as _verdict
from isar.analyze.prior import MODE_ENERGY_FLOOR_FRACTION, resolution_fwhm
from isar.scene.build import build_scene
from isar.scene.model import SampleKnowledge, Visibility

from ._world_helpers import run_selection, scan_ctx, scene_observation

# Five nodes straddling the branch crossing of the ``crossing`` world (branch A rises 2→7 meV,
# branch B falls 6.5→3 meV, degenerate at H≈2.178). The nodes are collinear along H, so
# consecutive-ordinal adjacency chains them and the cluster spans the crossing instead of
# breaking at it.
CROSSING_NODES = (2.05, 2.10, 2.15, 2.20, 2.25)


@pytest.fixture(scope="module")
def crossing_arenas():
    return [scene_observation("crossing", h) for h in CROSSING_NODES]


@pytest.fixture(scope="module")
def crossing_scene(crossing_arenas):
    return build_scene(crossing_arenas)


@pytest.mark.slow
def test_the_crossing_world_is_one_cluster_spanning_the_crossing(crossing_scene):
    """A branch crossing is one scene structure, not two campaigns that meet in the middle."""
    clusters = crossing_scene.payload["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["scan_refs"] == [f"world:{h:.2f}:d" for h in CROSSING_NODES]


@pytest.mark.slow
def test_the_crossing_world_enumerates_both_linkages_and_chooses_neither_by_rank(crossing_scene):
    """The failure this world exists to catch: 'lowest = acoustic' relabels the same physical
    branch halfway along the path. The scene must instead hold BOTH linkages with real posterior
    mass — and since neither reading leaves a mode unsupported, they come out equal, which is
    the honest answer for a symmetric crossing.
    """
    cl = crossing_scene.payload["clusters"][0]
    by_assignment = {}
    for a in cl["alternatives"]:
        key = tuple(tuple(p) for p in a["assignment"])
        by_assignment.setdefault(key, []).append(a)
    ambiguous = [alts for alts in by_assignment.values() if len(alts) > 1]
    assert ambiguous, "no assignment carried an alternative linkage across the crossing"

    for alts in ambiguous:
        # Same candidates, so the local evidence cannot separate the readings at all...
        assert len({a["base"] for a in alts}) == 1
        # ... they are distinct readings, not duplicates ...
        assert len({tuple(tuple(p) for p in a["links"]) for a in alts}) == len(alts)
        # ... and neither is preferred: equal presence terms, equal belief. No ω-rank rule ran.
        assert len({a["factor_terms"]["presence"] for a in alts}) == 1
        assert len({a["posterior"] for a in alts}) == 1

    # The leading ambiguous assignment holds REAL mass — the 50/50 is a live answer, not a pair of
    # underflowed also-rans. (Hopeless assignments survive too: with unbounded factors nothing may
    # be pruned on base lnZ, so their posteriors legitimately underflow to 0.)
    best = max(ambiguous, key=lambda alts: alts[0]["base"])
    assert all(a["posterior"] > 0.0 for a in best)


@pytest.mark.slow
def test_the_scene_replays_byte_identically_from_the_same_seed(crossing_scene, crossing_arenas):
    """Same seed, same rows, same revision — the replay guarantee, on real evidence.

    Both halves matter: the fit path reproduces the arena (a fresh run of one node fingerprints
    identically), and the builder reproduces the revision from the arenas.
    """
    assert build_scene(crossing_arenas).digest == crossing_scene.digest
    fresh = scene_observation("crossing", CROSSING_NODES[1])
    assert fresh.fingerprint == crossing_arenas[1].fingerprint


# ----------------------------------------------------------------------------
# endpoint_under_bragg — the structural endpoint, on a world that really has one
# ----------------------------------------------------------------------------
# H = 2.00 is a zone centre of the declared period; 2.05/2.10/2.15/2.20 walk out of it. The
# world's shallow acoustic branch (omega0 = 2|sin(pi(H-2))|) is 0.00/0.31/0.62/0.92/1.18 meV
# there, against a ~0.96 meV resolution FWHM. The recipe keeps a mode at least
# MODE_ENERGY_FLOOR_FRACTION x FWHM (~0.48 meV) off the elastic line, and the rung-1 ladder offers a
# second mode at every node, so the local layer resolves the acoustic branch from H = 2.10 outwards
# (fitted 0.82 meV against the true 0.62) and delivers an elastic line plus the optic branch alone
# at 2.00 and 2.05 (true 0.00 and 0.31 meV, under the floor). That is the world's whole point, and
# the scene has to price the disappearance without inventing a phonon at zero.
ENDPOINT_NODES = (2.00, 2.05, 2.10, 2.15, 2.20)
ENDPOINT_SAMPLE = SampleKnowledge(zone_period=1.0, reflections=(2.0,))
# The same path with a period whose folds (1.8, 2.7) miss it entirely — the control for "the
# exemption is anchored to a declared fold, not to being low-energy near a Bragg peak".
OFF_FOLD_SAMPLE = SampleKnowledge(zone_period=0.9, reflections=(2.0,))


@pytest.fixture(scope="module")
def endpoint_arenas():
    return [scene_observation("endpoint_under_bragg", h) for h in ENDPOINT_NODES]


@pytest.fixture(scope="module")
def endpoint_scene(endpoint_arenas):
    return build_scene(endpoint_arenas, sample=ENDPOINT_SAMPLE)


def _leading(cluster):
    return max(cluster["alternatives"], key=lambda a: a["posterior"])


@pytest.mark.slow
def test_the_endpoint_world_terminates_the_acoustic_track_at_the_declared_zone_centre(
        endpoint_scene):
    """The declared physics, doing the work the data cannot. The acoustic branch really is there
    at every one of these H; below ~2.15 it is inside the elastic response and no amount of
    counting would separate it. Told where the zone centre is, the scene says the track ENDS
    there rather than that it went missing.

    What the world actually delivers, and what this therefore pins: the local layer resolves the
    acoustic branch from H = 2.10 (fitted 0.82 meV; the truth there is 0.62, above the ~0.48 meV
    recipe floor) outwards, so the LINKED track is the nodes 2.15 → 2.10, descending toward the
    centre. The endpoint is granted at the terminal node's centre-ward edge — against the
    CONSECUTIVE arena 2.05, not against 2.00 two grid steps away (review finding 3) — and it is
    granted because the contiguous run 2.05 → 2.00 is covered, elastic-bearing, and ends on the
    declared fold. (At 2.05 the truth is 0.31 meV, under the floor, so no mode is offered there.)"""
    cl, = endpoint_scene.payload["clusters"]
    assert cl["scan_refs"] == [f"world:{h:.2f}:d" for h in ENDPOINT_NODES]
    endpoints = [r for r in cl["visibility"]
                 if r["visibility"] == Visibility.STRUCTURAL_ENDPOINT]
    assert endpoints, "no track terminated at the zone centre"
    for r in endpoints:
        # truth omega0 = 2|sin(pi(H-2))| = 0.62 meV at 2.10 (above the floor), 0.31 at 2.05 (below)
        assert r["scan_ref"] == "world:2.10:d"          # the terminal node of the resolved track
        assert r["neighbour"] == "world:2.05:d"         # its centre-ward path edge
        assert r["elastic_present"] is True             # there IS an elastic response to hide in
        assert abs(r["omega"]) < 2.0                    # the acoustic branch, not the optic one
    # A reading of the LINKAGE, not of one mode's energy: the track it terminates is really there,
    # and it continues away from the centre into H = 2.15 (truth 0.92 meV, resolved). Asserted on
    # the linkage rather than on a candidate label, because which 2-mode reading of H = 2.15 wins
    # (with or without its own elastic term) is the local layer's business and not this claim's.
    endpoint, = endpoints
    partners = [b for a, b in _leading(cl)["links"] if a == endpoint["component_id"]]
    assert [p for p in partners if p.startswith("world:2.15:d#")], _leading(cl)["links"]


@pytest.mark.slow
def test_the_endpoint_says_what_blocked_unresolved_could_not(endpoint_arenas, endpoint_scene):
    """What the declaration actually buys on THIS world, stated honestly.

    The acoustic mode at H = 2.10 sits at 0.82 meV (truth 0.62), inside the ~0.96 meV elastic
    width, so without any sample knowledge the scene already declines to penalize its absence at
    the zone centre — ``blocked_unresolved``. Neither reading is priced, and that is the point: the two
    states differ in what they tell an operator, not in arithmetic. Blocked says *count longer,
    the answer may change*; structural endpoint says *the declared gapless branch ends here, so
    it never will*.

    The absence-penalty half of the claim (``searched_no_signal`` → no miss) is pinned
    on hand-built arenas in ``test_factors_live.py``, where the mode is far enough above the
    elastic line for a real penalty to be there to remove."""
    blind = build_scene(endpoint_arenas)
    base = {(r["component_id"], r["neighbour"]): r["visibility"]
            for r in blind.payload["clusters"][0]["visibility"]}
    cl, = endpoint_scene.payload["clusters"]
    best = _leading(cl)
    priced = [why for reasons in best["reasons"].values() for why in reasons]
    for r in cl["visibility"]:
        if r["visibility"] != Visibility.STRUCTURAL_ENDPOINT:
            continue
        assert base[(r["component_id"], r["neighbour"])] in (Visibility.BLOCKED_UNRESOLVED,
                                                             Visibility.SEARCHED_NO_SIGNAL)
        assert f"miss {r['component_id']} not seen at {r['neighbour']}" not in priced
    # The leading alternative is the same either way: reinterpreting an absence that was already
    # unpriced must move no belief at all.
    assert _leading(blind.payload["clusters"][0])["assignment"] == best["assignment"]


@pytest.mark.slow
def test_the_zone_centre_exemption_does_not_spread_to_the_rest_of_the_path(endpoint_arenas,
                                                                          endpoint_scene):
    """The control the exemption needs to be worth anything: it is anchored to a DECLARED fold and
    to nothing else. Re-scored with a period whose folds (1.8, 2.7) miss this stretch of path, the
    identical arenas, the identical descending track and the identical elastic responses produce
    NO endpoint — the same absence goes back to being unresolved, which is what "look again with
    better data" means. If proximity to a Bragg peak or low energy could buy the reading, this
    scene would carry one."""
    cl, = endpoint_scene.payload["clusters"]
    terminated = {(r["component_id"], r["neighbour"]) for r in cl["visibility"]
                  if r["visibility"] == Visibility.STRUCTURAL_ENDPOINT}
    assert terminated

    off_fold, = build_scene(endpoint_arenas, sample=OFF_FOLD_SAMPLE).payload["clusters"]
    assert not [r for r in off_fold["visibility"]
                if r["visibility"] == Visibility.STRUCTURAL_ENDPOINT]
    assert {(r["component_id"], r["neighbour"]) for r in off_fold["visibility"]
            if r["visibility"] == Visibility.BLOCKED_UNRESOLVED} >= terminated


@pytest.mark.slow
def test_the_endpoint_world_never_places_a_phonon_on_the_elastic_line(endpoint_scene):
    """The failure the exemption must not buy. "The branch is unresolvable here" is the honest
    answer; a fitted DHO at omega ~ 0 would be the scene agreeing with the declaration by
    inventing the mode the declaration says cannot be seen. No candidate at all — not merely no
    winner, not merely no surviving alternative — may carry one.

    The claim is made over EVERY candidate row of every arena, not over the alternative set: the
    rung-1 ladder offers 8-9 candidates per node, so the 5-node cluster has 5250 assignments
    against the 4096 cap and is legitimately ``truncated`` — the alternative set is then only the
    top of the base ordering, and a claim over it would say nothing about the rest."""
    payload = endpoint_scene.payload
    # The recipe's own floor, from the world's resolution: a Stokes mode may not sit nearer the
    # elastic line than this (truth: 0.31 meV at 2.05 and 0.00 at 2.00 are under it, 0.62 at 2.10 is not).
    floor = MODE_ENERGY_FLOOR_FRACTION * resolution_fwhm(scan_ctx("endpoint_under_bragg", 2.0))
    for o in payload["observations"]:
        for c in o["candidates"]:
            for w in c["omegas"]:
                assert w is None or abs(w) >= floor, \
                    f"zero-DHO phantom at {o['scan_ref']} in {c['label']}: {w}"
    # And the other direction: the elastic line is really there at every node up to 2.15 (truth: the
    # Gaussian Bragg envelope, FWHM 0.20, is 1.00 / 0.84 / 0.50 / 0.21 of its peak at 2.00..2.15), so the
    # leading reading must carry it rather than hand its intensity to a low-energy mode. 2.20 is
    # excluded: the envelope has fallen to 0.06 there and the data may legitimately drop it.
    cl, = payload["clusters"]
    for ref, label in _leading(cl)["assignment"]:
        if ref != "world:2.20:d":
            assert label.startswith("elastic"), f"elastic line dropped at {ref}: {label}"


# ----------------------------------------------------------------------------
# off_lattice_elastic — Bragg is a scene-level identity, and this one is not Bragg
# ----------------------------------------------------------------------------
@pytest.mark.slow
def test_an_off_lattice_elastic_feature_is_classified_not_bragg():
    """The world exists because "the lowest thing on this path" is not always a reflection. The
    local layer is lattice-blind BY DESIGN — it sees a resolution-limited feature at omega = 0 and
    calls the scan ``elastic_only``, which is the correct local answer and prices no lattice
    position. Only the scene, holding the declared reflection list, can say the feature sits
    nowhere the scientist declared a reflection — and it CLASSIFIES rather than penalizing,
    because elastic intensity is ubiquitous and off-lattice elastic is not improbable."""
    ctx, results, sel, _hyps = run_selection("off_lattice_elastic", 2.5)
    assert _verdict.classify(results, ctx, sel).verdict == "elastic_only"

    obs = scene_observation("off_lattice_elastic", 2.5)
    rev = build_scene([obs], sample=SampleKnowledge(zone_period=1.0, reflections=(2.0, 3.0)))
    cl, = rev.payload["clusters"]
    states = cl["classification"]["reciprocal_lattice_localization"]
    assert states and set(states.values()) == {"off_lattice"}
    assert all(cid.startswith("world:2.50:d#") for cid in states)


# ----------------------------------------------------------------------------
# unknown_extra — the machine may be honest at its ceiling, never confident past it
# ----------------------------------------------------------------------------
UNKNOWN_NODES = (2.20, 2.25, 2.30)


@pytest.fixture(scope="module")
def unknown_extra_scene():
    return build_scene([scene_observation("unknown_extra", h) for h in UNKNOWN_NODES])


@pytest.mark.slow
def test_the_unknown_extra_level_survives_scene_construction_as_a_live_competitor(
        unknown_extra_scene):
    """Three true modes (acoustic, a dispersionless 5 meV level, optic) against a 2-mode
    enumeration cap. The scene may not resolve the level — but it must not lose it: the
    multimode readings keep real posterior mass, nothing is pruned or capped away, and no
    visibility rule suppresses the extra feature into an absence.

    Slow by design (the genuinely ambiguous 3-vs-2 contest escalates to dynesty)."""
    cl, = unknown_extra_scene.payload["clusters"]
    assert cl["scan_refs"] == [f"world:{h:.2f}:d" for h in UNKNOWN_NODES]
    # Not narrowed by any builder knob: the answer is the full enumeration.
    assert cl["truncated"] is False and cl["linkage_truncated"] is False
    assert all(e["reason"] != "margin_pruned"
               for excluded in cl["pruning"].values() for e in excluded)
    # The multimode reading holds belief at every arena, not just at the one that fitted best.
    for ref in cl["scan_refs"]:
        assert cl["marginals"]["n_modes"][ref].get("2", 0.0) > 0.0
    # ... and the blended extra feature is SEEN across the path rather than argued away: no
    # searched-and-empty verdict anywhere in the 4-6 meV band the level and acoustic share.
    assert not [r for r in cl["visibility"]
                if r["visibility"] == Visibility.SEARCHED_NO_SIGNAL and 3.5 < abs(r["omega"]) < 6.0]
