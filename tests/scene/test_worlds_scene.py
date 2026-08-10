"""The SCENE worlds put to the scene layer itself, on real evidence (SCENE-3 PR 3b).

Unlike ``test_worlds_fast.py``, which pins the generator, and unlike ``test_factors_live.py``,
which hand-builds arenas, this file runs the whole path: world → counts → fits → evidence
selection → candidate rows → scene. It is the only place a scene assertion is made against
numbers no test author chose.

Slow by construction (the fits are real, ~2–3 min for the crossing node set), so slow-marked; the
remaining worlds land with the zone-centre factor next.
"""
import pytest

from isar.scene.build import build_scene

from ._world_helpers import scene_observation

# Five nodes straddling the branch crossing of the ``crossing`` world (branch A rises 2→7 meV,
# branch B falls 6.5→3 meV, degenerate at H≈2.178). Spacing 0.05 keeps consecutive nodes inside
# Q_ADJACENCY (0.15), so the cluster spans the crossing instead of breaking at it.
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
    mass — and since neither reading breaks a track, they come out equal, which is the honest
    answer for a symmetric crossing.
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
        # ... and neither is preferred: equal connectivity, equal belief. No ω-rank rule ran.
        assert len({a["factor_terms"]["connectivity"] for a in alts}) == 1
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
