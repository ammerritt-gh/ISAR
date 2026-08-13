"""SCENE-4 PR 4.0 (#25 + #28): scene PARTITION by (sample, ConditionSet, path identity).

Topology never collapses from one off-path arena; conditions never blend. Every test here builds
its own small path with the LIVE ``FACTORS`` registry (default ``build_scene`` factors), because
the coupling this package guards (``_couples``/``_couples_arena``) is gated on real
``path_adjacent`` — a neutral test factor would not exercise it.
"""
from isar.scene.build import build_scene
from isar.scene.factors import PRESENCE_MISS_NATS

from .test_build import cand, cluster_of, mk_obs

SLOPE_SAMPLE = None  # these fixtures keep peaks well inside the resolution-only reach


def q(h):
    return (h, 0.0, 0.0)


def terms(cluster, name):
    return {tuple(tuple(p) for p in a["assignment"]): a["factor_terms"].get(name)
            for a in cluster["alternatives"]}


# ----------------------------------------------------------------------------
# Condition partition (#28): same q, different binned condition, no coupling
# ----------------------------------------------------------------------------
def test_a_control_pair_at_the_same_condition_does_couple():
    """Control for the next test: same q, same (default) condition — an ordinary SCENE-3 pair,
    which couples and charges the miss."""
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    empty = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0))
    cl = cluster_of(build_scene([seen, empty]), "e:1:d")
    assert cl["scan_refs"] == ["e:1:d", "e:2:d"]
    assert list(terms(cl, "presence").values()) == [PRESENCE_MISS_NATS]


def test_the_same_q_path_at_two_binned_temperatures_forms_separate_components():
    """A mode present in one condition and absent in another, at the identical q, is two
    independent scientific observations — not a q-neighbour miss. Two arenas 100 K apart (well
    outside the default 0.1 K bin) never enter the same partition, so they never even reach the
    coupling check that would charge the absence."""
    seen = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0),
                  conditions=(("temperature_K", 100.0),))
    empty = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0),
                   conditions=(("temperature_K", 200.0),))
    rev = build_scene([seen, empty])
    clusters = {c["cluster_id"]: c for c in rev.payload["clusters"]}
    assert sorted(clusters) == ["e:1:d", "e:2:d"]
    assert clusters["e:1:d"]["scan_refs"] == ["e:1:d"]
    assert clusters["e:2:d"]["scan_refs"] == ["e:2:d"]
    # The presence term is neutral (no neighbour to compare against), not the miss the control
    # pair above charges — the arenas were never even placed adjacent.
    assert clusters["e:1:d"]["alternatives"][0]["factor_terms"]["presence"] == 0.0


def test_a_full_path_measured_at_two_conditions_stays_two_intact_paths():
    """Acceptance form: the same three-node q path, walked once at 100 K and once at 200 K, is
    two separate three-node components — not one six-node path, and no mode disappearing across
    the 100↔200 boundary is charged as a miss."""
    # Every node carries a mode (not bg-only) so each condition's own three arenas genuinely
    # couple into one component via ``_couples`` — an all-bg path never forms a multi-arena
    # cluster at all (no component on either side to couple through), which would test nothing
    # about partitioning.
    warm = [mk_obs(f"w:{i}:d", [cand("stokes+bg", -10.0, omegas=(5.0 + 0.01 * i,))],
                   q=q(1.0 + 0.1 * i), conditions=(("temperature_K", 100.0),))
            for i in range(3)]
    cold = [mk_obs(f"c:{i}:d", [cand("stokes+bg", -10.0, omegas=(5.0 + 0.01 * i,))],
                   q=q(1.0 + 0.1 * i), conditions=(("temperature_K", 200.0),))
            for i in range(3)]
    rev = build_scene(warm + cold)
    clusters = {c["cluster_id"]: c["scan_refs"] for c in rev.payload["clusters"]}
    assert sorted(clusters.values()) == [["c:0:d", "c:1:d", "c:2:d"], ["w:0:d", "w:1:d", "w:2:d"]]


def test_condition_binning_unavailable_fails_closed_and_records_a_reason():
    """A declared temperature with no declared binning policy (``condition_t_round`` non-positive
    or absent) cannot resolve a partition key — fails closed to no-coupling, with a reason."""
    a = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    b = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0))
    rev = build_scene([a, b], condition_t_round=None)
    assert "condition_binning_unavailable" in rev.payload["sample_knowledge"]["degradations"]
    clusters = {c["cluster_id"]: c["scan_refs"] for c in rev.payload["clusters"]}
    assert clusters == {"e:1:d": ["e:1:d"], "e:2:d": ["e:2:d"]}


# ----------------------------------------------------------------------------
# Sample partition (#28)
# ----------------------------------------------------------------------------
def test_different_declared_samples_never_couple():
    a = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0), sample="LiHoF4")
    b = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0), sample="LiYF4")
    rev = build_scene([a, b])
    clusters = {c["cluster_id"]: c["scan_refs"] for c in rev.payload["clusters"]}
    assert clusters == {"e:1:d": ["e:1:d"], "e:2:d": ["e:2:d"]}


# ----------------------------------------------------------------------------
# Lattice-epoch partition (#25/#28, H13)
# ----------------------------------------------------------------------------
def test_two_observations_on_incompatible_lattice_epochs_do_not_couple():
    a = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0), lattice_epoch=1)
    b = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0), lattice_epoch=2)
    rev = build_scene([a, b])
    clusters = {c["cluster_id"]: c["scan_refs"] for c in rev.payload["clusters"]}
    assert clusters == {"e:1:d": ["e:1:d"], "e:2:d": ["e:2:d"]}


def test_an_undeclared_epoch_couples_only_with_another_undeclared_epoch():
    a = mk_obs("e:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0), lattice_epoch=None)
    b = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=q(1.0), lattice_epoch=None)
    cl = cluster_of(build_scene([a, b]), "e:1:d")
    assert cl["scan_refs"] == ["e:1:d", "e:2:d"]


# ----------------------------------------------------------------------------
# Path identity / off-path arenas (#25)
# ----------------------------------------------------------------------------
def test_one_off_path_context_arena_leaves_the_path_intact_and_joins_nothing():
    """The #25 bug, fixed: today one off-path q fails the WHOLE set closed. Tagged
    ``topology_role="context_probe"``, the outlier is withheld from the path computation
    entirely — the three on-path arenas keep their ordinals and couple exactly as if it were
    never added."""
    path = [mk_obs(f"e:{i}:d", [cand("stokes+bg", -10.0, omegas=(5.0 + 0.1 * i,))],
                   q=q(1.0 + 0.1 * i)) for i in range(3)]
    off = mk_obs("probe:d", [cand("stokes+bg", -10.0, omegas=(5.0,))],
                q=(1.1, 0.4, 0.0), topology_role="context_probe")
    rev = build_scene(path + [off])
    on_path = cluster_of(rev, "e:0:d")
    assert on_path["scan_refs"] == ["e:0:d", "e:1:d", "e:2:d"]
    assert "path_topology_unavailable" not in rev.payload["sample_knowledge"]["degradations"]
    probe_cluster = cluster_of(rev, "probe:d")
    assert probe_cluster["scan_refs"] == ["probe:d"]
    footprints = {f["scan_ref"]: f for f in rev.payload["footprints"]}
    assert footprints["probe:d"]["path_index"] is None
    assert footprints["e:0:d"]["path_index"] is not None


def test_without_the_context_probe_tag_the_same_outlier_still_collapses_the_path():
    """Contrast case: the #25 fix is provenance-gated. The IDENTICAL outlier, undeclared
    (``topology_role=None``, today's only production value), still breaks whole-set collinearity
    — reproducing SCENE-3 behaviour exactly for a provenance-less workspace."""
    path = [mk_obs(f"e:{i}:d", [cand("stokes+bg", -10.0, omegas=(5.0 + 0.1 * i,))],
                   q=q(1.0 + 0.1 * i)) for i in range(3)]
    off = mk_obs("probe:d", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=(1.1, 0.4, 0.0))
    rev = build_scene(path + [off])
    assert "path_topology_unavailable" in rev.payload["sample_knowledge"]["degradations"]
    clusters = {c["cluster_id"]: c["scan_refs"] for c in rev.payload["clusters"]}
    assert clusters == {r["scan_ref"]: [r["scan_ref"]] for r in
                        [{"scan_ref": s} for s in ("e:0:d", "e:1:d", "e:2:d", "probe:d")]}


def test_two_off_path_arenas_that_happen_to_be_collinear_do_not_form_a_path():
    """Both tagged context_probe, and collinear WITH EACH OTHER: neither may ever enter a
    ``path_topology`` computation, so they can never accidentally chain into a path of their
    own — each is its own singleton, whatever their geometry."""
    path = [mk_obs(f"e:{i}:d", [cand("stokes+bg", -10.0, omegas=(5.0 + 0.1 * i,))],
                   q=q(1.0 + 0.1 * i)) for i in range(3)]
    probes = [mk_obs("probe:1:d", [cand("stokes+bg", -10.0, omegas=(5.0,))],
                     q=(5.0, 1.0, 0.0), topology_role="context_probe"),
              mk_obs("probe:2:d", [cand("stokes+bg", -10.0, omegas=(5.0,))],
                     q=(6.0, 1.0, 0.0), topology_role="context_probe")]
    rev = build_scene(path + probes)
    on_path = cluster_of(rev, "e:0:d")
    assert on_path["scan_refs"] == ["e:0:d", "e:1:d", "e:2:d"]
    footprints = {f["scan_ref"]: f for f in rev.payload["footprints"]}
    assert footprints["probe:1:d"]["path_index"] is None
    assert footprints["probe:2:d"]["path_index"] is None
    clusters = {c["cluster_id"] for c in rev.payload["clusters"]}
    assert "probe:1:d" in clusters and "probe:2:d" in clusters
    assert cluster_of(rev, "probe:1:d")["scan_refs"] == ["probe:1:d"]
    assert cluster_of(rev, "probe:2:d")["scan_refs"] == ["probe:2:d"]


def test_two_detectors_at_the_same_q_and_condition_remain_valid_repeated_observations():
    """Same partition, same q: two detectors at one position couple exactly as SCENE-3 always
    let them — partitioning changes nothing within one partition."""
    a = mk_obs("e:1:det1", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    b = mk_obs("e:1:det2", [cand("stokes+bg", -10.0, omegas=(5.0,))], q=q(1.0))
    cl = cluster_of(build_scene([a, b]), "e:1:det1")
    assert cl["scan_refs"] == ["e:1:det1", "e:1:det2"]


def test_a_provenance_less_scattered_set_reproduces_scene_3_behaviour():
    """No arena declares a topology role (today's only production state): three genuinely
    scattered q behave exactly as before this package — each its own cluster, reason recorded."""
    a = mk_obs("e:1:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=(1.0, 0.0, 0.0))
    b = mk_obs("e:2:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=(1.1, 0.4, 0.0))
    c = mk_obs("e:3:d", [cand("bg", -10.0, n_modes=0, omegas=())], q=(1.2, -0.3, 0.2))
    rev = build_scene([a, b, c])
    assert [cl["scan_refs"] for cl in rev.payload["clusters"]] == [
        ["e:1:d"], ["e:2:d"], ["e:3:d"]]
    assert "path_topology_unavailable" in rev.payload["sample_knowledge"]["degradations"]
