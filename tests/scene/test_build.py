"""The scene builder's probability model, exercised with neutral factors (SCENE-3 PR 3a).

Every test here is an invariant of SCENE_DESIGN §2b rather than a snapshot of an
implementation: one arena's evidence enters once, clusters normalize independently, the
kept set under a cap is exactly the top of the base ordering, and the persisted ledger
reconstructs to the numbers it was built from. Observations are hand-built — no fitter, no
workspace, no filesystem — so a failure here is a failure of the math.
"""
import itertools
import math
from dataclasses import replace

import pytest

from isar.scene.build import (SCENE_SCHEMA, ClusterView, SceneAlternative,
                              _discarded_bound, build_scene)
from isar.scene import build as build_mod
from isar.scene.factors import SceneFactor
from isar.scene.model import (CandidateRow, SceneObservation, canonical_bytes,
                              observation_fingerprint)


# ----------------------------------------------------------------------------
# Hand-built observations
# ----------------------------------------------------------------------------
def cand(label, lnz, *, n_modes=1, omegas=None, usable=True, identity=None):
    return (label, lnz, n_modes, (10.0,) * n_modes if omegas is None else tuple(omegas),
            usable, identity)


def mk_obs(scan_ref, candidates, *, window=(-10.0, 10.0), q=(1.0, 0.0, 0.0)):
    rows = tuple(
        CandidateRow(label=label, k=None, n_modes=n_modes, log_evidence=lnz,
                     evidence_method="laplace", posterior_probability=None, usable=usable,
                     identity=identity, variant=None, omegas=omegas)
        for label, lnz, n_modes, omegas, usable, identity in candidates)
    obs = SceneObservation(scan_ref=scan_ref, q=q, temperature_K=300.0, energy_window=window,
                           gauss_fwhm=1.0, lorentz_fwhm=None, effective_exposure=1.0,
                           background=0.5, best_label=rows[0].label if rows else None,
                           candidates=rows, fingerprint="x")
    # Stamped from a label-sorted copy: the builder sorts candidates, so permuting the input
    # tuple in a test must be a real permutation of the same observation, not a different one.
    canon = replace(obs, candidates=tuple(sorted(rows, key=lambda c: c.label)))
    return replace(obs, fingerprint=observation_fingerprint(canon))


# ----------------------------------------------------------------------------
# Neutral factors — the physics ones land in the next task
# ----------------------------------------------------------------------------
def _q_adjacent(a, b):
    return a.q is not None and b.q is not None and abs(a.q[0] - b.q[0]) <= 0.11


COUPLER = SceneFactor(name="q_adjacency", kind="structural_likelihood", version="1",
                      consumes=("q",), couples=_q_adjacent)
ZERO = SceneFactor(name="zero", kind="structural_likelihood", version="1",
                   score=lambda alt, view: (0.0, ()))


def biased(label="B", term=1.5):
    def score(alt, view):
        assert isinstance(alt, SceneAlternative) and isinstance(view, ClusterView)
        hit = any(chosen == label for _, chosen in alt.assignment)
        return (term if hit else 0.0), (("biased_hit",) if hit else ())

    return SceneFactor(name="biased", kind="structural_likelihood", version="1", score=score)


def chain_and_distant():
    """Three q-adjacent arenas (one chain cluster) plus one far away (a singleton)."""
    return [
        mk_obs("e:1:d", [cand("A", -10.0), cand("B", -11.0)], q=(1.0, 0.0, 0.0)),
        mk_obs("e:2:d", [cand("A", -12.0), cand("B", -12.5)], q=(1.1, 0.0, 0.0)),
        mk_obs("e:3:d", [cand("A", -9.0), cand("B", -9.4)], q=(1.2, 0.0, 0.0)),
        mk_obs("e:9:d", [cand("A", -5.0), cand("B", -5.5)], q=(3.0, 0.0, 0.0)),
    ]


def cluster_of(rev, cluster_id):
    return next(c for c in rev.payload["clusters"] if c["cluster_id"] == cluster_id)


# ----------------------------------------------------------------------------
# Intake
# ----------------------------------------------------------------------------
def test_duplicate_scan_ref_raises():
    obs = mk_obs("e:1:d", [cand("A", -10.0)])
    with pytest.raises(ValueError, match="duplicate scan_ref"):
        build_scene([obs, obs])


def test_duplicate_candidate_labels_in_one_arena_raise():
    """Silent evidence corruption if unchecked: the scorer keys on (scan_ref, label), so one
    row's lnZ would be dropped, the survivor enumerated twice, its mass doubled — and the
    cluster would still sum to 1.0."""
    obs = mk_obs("e:1:d", [cand("A", -1.0), cand("A", -5.0), cand("B", -2.0)])
    with pytest.raises(ValueError, match="duplicate candidate labels"):
        build_scene([obs], factors=[ZERO])


@pytest.mark.parametrize("bad", ["", "None"])
def test_blank_fingerprint_raises(bad):
    obs = replace(mk_obs("e:1:d", [cand("A", -10.0)]), fingerprint=bad)
    with pytest.raises(ValueError, match="no fingerprint"):
        build_scene([obs])


# ----------------------------------------------------------------------------
# Clusters
# ----------------------------------------------------------------------------
def test_no_factors_gives_all_singleton_clusters():
    """The degenerate case is the correct one: nothing couples, so nothing shares a norm."""
    rev = build_scene(chain_and_distant(), factors=[])
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [
        ["e:1:d"], ["e:2:d"], ["e:3:d"], ["e:9:d"]]
    assert rev.payload["schema"] == SCENE_SCHEMA


def test_q_adjacency_builds_one_chain_cluster_and_one_singleton():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [
        ["e:1:d", "e:2:d", "e:3:d"], ["e:9:d"]]


def test_disconnected_cluster_is_byte_identical_without_the_distant_arena():
    """Independence, checked on bytes: an unrelated arena may not move one bit of a cluster."""
    obs = chain_and_distant()
    with_distant = build_scene(obs, factors=[COUPLER, ZERO])
    without = build_scene(obs[:3], factors=[COUPLER, ZERO])
    assert canonical_bytes(cluster_of(with_distant, "e:1:d")) == \
        canonical_bytes(cluster_of(without, "e:1:d"))


# ----------------------------------------------------------------------------
# Determinism
# ----------------------------------------------------------------------------
def test_byte_determinism_and_input_order_invariance():
    obs = chain_and_distant()
    permuted = [replace(o, candidates=tuple(reversed(o.candidates)))
                for o in reversed(obs)]
    a = build_scene(obs, factors=[COUPLER, ZERO])
    b = build_scene(obs, factors=[COUPLER, ZERO])
    c = build_scene(permuted, factors=[ZERO, COUPLER])
    assert canonical_bytes(a.to_dict()) == canonical_bytes(b.to_dict())
    assert a.digest == b.digest == c.digest


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------
def test_posteriors_normalize_within_each_cluster():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    for cl in rev.payload["clusters"]:
        assert math.fsum(a["posterior"] for a in cl["alternatives"]) == pytest.approx(1.0, abs=1e-12)
        for ref, marg in cl["marginals"]["label"].items():
            assert math.fsum(marg.values()) == pytest.approx(1.0, abs=1e-12), ref


def test_cluster_product_equals_the_brute_force_joint():
    """Factorization is not an approximation: the joint over the union is the product.

    Compared with a tight tolerance rather than ``==`` because the joint normalization sums
    exponentials in a different order and shift than the per-cluster ones do; the model is
    exact, only the float summation path differs.
    """
    obs = chain_and_distant()
    rev = build_scene(obs, factors=[COUPLER, ZERO])
    lnz = {(o.scan_ref, c.label): c.log_evidence for o in obs for c in o.candidates}

    joint = {}
    for combo in itertools.product(*[[(o.scan_ref, c.label) for c in o.candidates] for o in obs]):
        joint[combo] = math.fsum(lnz[p] for p in combo)
    norm = math.log(math.fsum(math.exp(v - max(joint.values())) for v in joint.values())) \
        + max(joint.values())

    per_alt = {tuple(tuple(p) for p in a["assignment"]): a["posterior"]
               for cl in rev.payload["clusters"] for a in cl["alternatives"]}
    for combo, total in joint.items():
        product = math.prod(per_alt[tuple(p for p in combo if p[0] in cl["scan_refs"])]
                            for cl in rev.payload["clusters"])
        assert product == pytest.approx(math.exp(total - norm), rel=1e-12)


def test_multimode_candidate_contributes_its_evidence_exactly_once():
    obs = [mk_obs("e:1:d", [cand("one", -10.0), cand("two", -8.0, n_modes=2, omegas=(4.0, 9.0))]),
           mk_obs("e:2:d", [cand("one", -7.0)], q=(1.05, 0.0, 0.0))]
    rev = build_scene(obs, factors=[COUPLER, ZERO])
    cl = cluster_of(rev, "e:1:d")
    bases = {tuple(a["assignment"][0]): a["base"] for a in cl["alternatives"]}
    # Two modes, one lnZ: -8.0 (not -16.0), plus the single-candidate partner arena's -7.0.
    assert bases[("e:1:d", "two")] == -8.0 + -7.0
    assert bases[("e:1:d", "one")] == -10.0 + -7.0
    assert cl["marginals"]["n_modes"]["e:1:d"].keys() == {"1", "2"}


def test_identity_none_throughout_is_ordinary_operation():
    obs = chain_and_distant()
    assert all(c.identity is None for o in obs for c in o.candidates)
    rev = build_scene(obs, factors=[COUPLER, ZERO, biased()])
    assert rev.payload["clusters"] and rev.digest


# ----------------------------------------------------------------------------
# Pruning, truncation, non-scoring
# ----------------------------------------------------------------------------
def test_margin_pruning_records_the_bound_and_keeps_the_near_rival():
    obs = [mk_obs("e:1:d", [cand("lead", -10.0), cand("near", -60.0), cand("far", -160.0)])]
    cl = cluster_of(build_scene(obs, factors=[ZERO]), "e:1:d")
    excluded = {e["label"]: e for e in cl["pruning"]["e:1:d"]}
    assert set(excluded) == {"far"}
    assert excluded["far"]["reason"] == "margin_pruned"
    assert excluded["far"]["delta_nats"] == 150.0
    assert excluded["far"]["mass_bound"] == math.exp(-150.0)
    assert {a["assignment"][0][1] for a in cl["alternatives"]} == {"lead", "near"}


def test_unusable_and_nonfinite_candidates_are_excluded_without_a_bound():
    obs = [mk_obs("e:1:d", [cand("lead", -10.0), cand("dead", -11.0, usable=False),
                            cand("inf", -math.inf), cand("none", None)])]
    cl = cluster_of(build_scene(obs, factors=[ZERO]), "e:1:d")
    excluded = {e["label"]: e for e in cl["pruning"]["e:1:d"]}
    assert set(excluded) == {"dead", "inf", "none"}
    assert all(e["reason"] == "no_evidence" and e["mass_bound"] is None
               for e in excluded.values())


def test_arena_without_usable_evidence_is_experiment_map_only():
    obs = [mk_obs("e:1:d", [cand("A", -10.0)]),
           mk_obs("e:8:d", [cand("A", None), cand("B", -3.0, usable=False)], q=(5.0, 0.0, 0.0)),
           mk_obs("e:9:d", [], q=(6.0, 0.0, 0.0))]
    rev = build_scene(obs, factors=[COUPLER, ZERO])
    assert rev.payload["non_scoring"] == [{"scan_ref": "e:8:d", "reason": "no_evidence"},
                                          {"scan_ref": "e:9:d", "reason": "no_candidates"}]
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"]]
    # Still on the experiment map: we looked there, and the footprint says so.
    assert {f["scan_ref"] for f in rev.payload["footprints"]} == {"e:1:d", "e:8:d", "e:9:d"}


def test_cap_keeps_exactly_the_top_alternatives_and_states_the_dropped_bound():
    obs = [mk_obs(f"e:{i}:d", [cand(chr(ord('A') + j), -1.0 * (i + 1) * (j + 1))
                               for j in range(5)], q=(1.0 + 0.1 * i, 0.0, 0.0))
           for i in range(3)]
    rev = build_scene(obs, factors=[COUPLER, ZERO], alt_cap=10)
    cl = cluster_of(rev, "e:0:d")
    assert cl["truncated"] is True and cl["n_possible"] == 125 and cl["n_enumerated"] == 10
    assert cl["marginals"]["truncated"] is True

    lnz = {(o.scan_ref, c.label): c.log_evidence for o in obs for c in o.candidates}
    all_alts = [tuple((o.scan_ref, c.label) for o, c in zip(obs, combo))
                for combo in itertools.product(*[o.candidates for o in obs])]
    ranked = sorted(all_alts, key=lambda a: (-math.fsum(lnz[p] for p in a), a))
    assert sorted(tuple(tuple(p) for p in a["assignment"]) for a in cl["alternatives"]) == \
        sorted(ranked[:10])

    leader = math.fsum(lnz[p] for p in ranked[0])
    lowest = math.fsum(lnz[p] for p in ranked[9])
    assert cl["discarded_mass_bound"] == pytest.approx(115 * math.exp(lowest - leader), rel=1e-12)


def test_discarded_bound_survives_counts_far_outside_the_float_range():
    """A cluster of a few hundred binary arenas overflows float long before the bound does."""
    assert _discarded_bound(0, -5.0, -1.0) == 0.0
    assert _discarded_bound(115, -30.0, -6.0) == pytest.approx(115 * math.exp(-24.0), rel=1e-12)
    huge = 2 ** 1100                                     # unconvertible to float at all
    assert _discarded_bound(huge, -800.0, 0.0) == pytest.approx(math.exp(1100 * math.log(2) - 800))
    assert _discarded_bound(huge, -1.0, 0.0) == math.inf  # honestly unbounded, not a crash


def test_lofo_alignment_is_declared_in_the_payload():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    assert rev.payload["lofo_alignment"] == "by_alternative_index"
    for cl in rev.payload["clusters"]:
        assert len(cl["lofo"]["zero"]) == len(cl["alternatives"])


def test_tied_base_scores_at_the_cap_boundary_break_by_label_tuple():
    """Ties straddling the cap must resolve one way, always: 3×3 candidates all at -1.0."""
    obs = [mk_obs(f"e:{i}:d", [cand(lbl, -1.0) for lbl in "ABC"], q=(1.0 + 0.1 * i, 0.0, 0.0))
           for i in range(2)]
    rev = build_scene(obs, factors=[COUPLER, ZERO], alt_cap=4)
    cl = cluster_of(rev, "e:0:d")
    kept = [tuple(tuple(p) for p in a["assignment"]) for a in cl["alternatives"]]
    assert cl["truncated"] is True and cl["n_possible"] == 9
    assert kept == [(("e:0:d", "A"), ("e:1:d", "A")), (("e:0:d", "A"), ("e:1:d", "B")),
                    (("e:0:d", "A"), ("e:1:d", "C")), (("e:0:d", "B"), ("e:1:d", "A"))]
    assert build_scene(obs, factors=[COUPLER, ZERO], alt_cap=4).digest == rev.digest


def test_alt_cap_constant_resolves_at_call_time(monkeypatch):
    """The sentinel trap: a default-argument capture would ignore this monkeypatch."""
    obs = [mk_obs("e:1:d", [cand("A", -1.0), cand("B", -2.0), cand("C", -3.0)])]
    monkeypatch.setattr(build_mod, "SCENE_ALT_CAP", 2)
    rev = build_scene(obs, factors=[ZERO])
    assert rev.payload["alt_cap"] == 2
    cl = cluster_of(rev, "e:1:d")
    assert cl["truncated"] is True and cl["n_enumerated"] == 2


def test_margin_constant_resolves_at_call_time(monkeypatch):
    obs = [mk_obs("e:1:d", [cand("lead", -10.0), cand("near", -60.0)])]
    monkeypatch.setattr(build_mod, "SCENE_PRUNE_MARGIN", 10.0)
    cl = cluster_of(build_scene(obs, factors=[ZERO]), "e:1:d")
    assert [e["label"] for e in cl["pruning"]["e:1:d"]] == ["near"]


# ----------------------------------------------------------------------------
# Ledger and LOFO
# ----------------------------------------------------------------------------
def test_ledger_reconstructs_to_the_stored_numbers_exactly():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO, biased()])
    for cl in rev.payload["clusters"]:
        for a in cl["alternatives"]:
            terms = {**a["factor_terms"], **a["prior_terms"]}
            total = math.fsum([a["base"], *(terms[k] for k in sorted(terms))])
            assert total == a["total"]
            assert math.exp(total - cl["log_norm"]) == a["posterior"]


def test_lofo_reproduces_the_build_without_that_factor():
    obs = chain_and_distant()
    with_bias = build_scene(obs, factors=[COUPLER, ZERO, biased()])
    without = build_scene(obs, factors=[COUPLER, ZERO])
    for cl, ref_cl in zip(with_bias.payload["clusters"], without.payload["clusters"]):
        assert [a["assignment"] for a in cl["alternatives"]] == \
            [a["assignment"] for a in ref_cl["alternatives"]]
        assert cl["lofo"]["biased"] == [a["posterior"] for a in ref_cl["alternatives"]]


def test_lofo_of_a_neutral_factor_is_the_posterior_itself():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    for cl in rev.payload["clusters"]:
        assert cl["lofo"]["zero"] == [a["posterior"] for a in cl["alternatives"]]


def test_the_biased_factor_actually_moves_the_posterior():
    """Guards the LOFO tests above from passing because nothing ever changed."""
    obs = chain_and_distant()
    biased_rev = build_scene(obs, factors=[COUPLER, biased(term=3.0)])
    neutral = build_scene(obs, factors=[COUPLER, ZERO])
    cl, ref = cluster_of(biased_rev, "e:1:d"), cluster_of(neutral, "e:1:d")
    assert [a["posterior"] for a in cl["alternatives"]] != \
        [a["posterior"] for a in ref["alternatives"]]
    assert any(a["reasons"].get("biased") == ["biased_hit"] for a in cl["alternatives"])
