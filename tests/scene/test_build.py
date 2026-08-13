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
                              _log_discarded_bound, build_scene)
from isar.scene import build as build_mod
from isar.scene.factors import SceneFactor
from isar.scene.model import (CandidateRow, ComponentDescriptor, SceneObservation, canonical_bytes,
                              digest_bytes, observation_fingerprint)


# ----------------------------------------------------------------------------
# Hand-built observations
# ----------------------------------------------------------------------------
def cand(label, lnz, *, n_modes=1, omegas=None, usable=True, identity=None, extra=(),
        constraint_origin="none"):
    """One candidate for ``mk_obs``. ``extra`` declares the NON-MODE component kinds explicitly.

    Mode descriptors follow the caller's ``n_modes``, which is already an explicit declaration of
    how many modes the candidate has; ``elastic``/``quasielastic`` must be spelled, because the
    label is an identifier and nothing derives structure from it any more.

    ``constraint_origin`` defaults to ``"none"`` (recorded, unconstrained) — a normal delivered
    row under the current contract, never the tri-state ``None`` production reserves for a
    pre-H9 workspace (SCENE-4 PR 4.0 #21): a test that wants the refusal lane passes
    ``constraint_origin=None`` explicitly.
    """
    return (label, lnz, n_modes, (10.0,) * n_modes if omegas is None else tuple(omegas),
            usable, identity, tuple(extra), constraint_origin)


def components(n_modes, extra=()):
    return tuple([*(ComponentDescriptor("mode", i) for i in range(n_modes)),
                  *(ComponentDescriptor(kind) for kind in extra)])


def mk_obs(scan_ref, candidates, *, window=(-10.0, 10.0), bands=None, q=(1.0, 0.0, 0.0),
           status="complete", fallback=None, sink=None, temperature=300.0, exposure=1.0,
           count_trust="trusted", sample=None, conditions=None, lattice_epoch=None,
           topology_role=None):
    rows = tuple(
        CandidateRow(label=label, k=None, n_modes=n_modes, log_evidence=lnz,
                     evidence_method="laplace", posterior_probability=None, usable=usable,
                     identity=identity, variant=None, omegas=omegas,
                     components=components(len(omegas), extra), constraint_origin=origin)
        for label, lnz, n_modes, omegas, usable, identity, extra, origin in candidates)
    if conditions is None:
        # Deliberately NOT derived from ``temperature`` (the per-observation PHYSICS field,
        # e.g. the Bose-detectability tests that vary it per arena on purpose): ``conditions``
        # is the #28 PARTITION identity, a separate declaration in production too
        # (``isar.schema.measurement.MeasurementArena.conditions`` vs. ``fit.T``), and a fixture
        # that wants a different PARTITION passes ``conditions=`` explicitly.
        conditions = (("temperature_K", 300.0),)
    obs = SceneObservation(scan_ref=scan_ref, q=q, temperature_K=temperature, energy_window=window,
                           energy_bands=(None if window is None
                                         else (tuple(window),) if bands is None
                                         else tuple(tuple(b) for b in bands)),
                           gauss_fwhm=1.0, lorentz_fwhm=None, effective_exposure=exposure,
                           background=0.5, best_label=rows[0].label if rows else None,
                           candidates=rows, evidence_comparison_status=status,
                           selection_fallback=fallback, arena_fingerprint=None,
                           evidence_sink=sink, sample=sample, conditions=conditions,
                           count_trust=count_trust, lattice_epoch=lattice_epoch,
                           topology_role=topology_role, fingerprint="x")
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
# Declares its true (zero) range, so it costs the builder no pruning: a neutral factor that left
# score_bound=None would silently disable margin pruning for the whole scene.
ZERO = SceneFactor(name="zero", kind="structural_likelihood", version="1",
                   score_bound=(0.0, 0.0), score=lambda alt, view: (0.0, ()))
UNBOUNDED = SceneFactor(name="unbounded", kind="structural_likelihood", version="1",
                        score=lambda alt, view: (0.0, ()))


def biased(label="B", term=1.5, bound=True):
    def score(alt, view):
        assert isinstance(alt, SceneAlternative) and isinstance(view, ClusterView)
        hit = any(chosen == label for _, chosen in alt.assignment)
        return (term if hit else 0.0), (("biased_hit",) if hit else ())

    return SceneFactor(name="biased", kind="structural_likelihood", version="1",
                       score_bound=(min(0.0, term), max(0.0, term)) if bound else None,
                       score=score)


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
    rev = build_scene(obs, factors=[ZERO])
    cl = cluster_of(rev, "e:1:d")
    excluded = {e["label"]: e for e in cl["pruning"]["e:1:d"]}
    assert set(excluded) == {"dead", "inf", "none"}
    assert all(e["reason"] == "no_evidence" and e["mass_bound"] is None
               for e in excluded.values())
    # The verbatim row persists ±inf as null — the same "no lnZ to sum" the builder already
    # read it as, and the only spelling that survives strict JSON.
    rows = {c["label"]: c for c in rev.payload["observations"][0]["candidates"]}
    assert rows["inf"]["log_evidence"] is None and rows["none"]["log_evidence"] is None


def test_arena_without_usable_evidence_is_experiment_map_only():
    obs = [mk_obs("e:1:d", [cand("A", -10.0)]),
           mk_obs("e:8:d", [cand("A", None), cand("B", -3.0, usable=False)], q=(5.0, 0.0, 0.0)),
           mk_obs("e:9:d", [], q=(6.0, 0.0, 0.0))]
    rev = build_scene(obs, factors=[COUPLER, ZERO])
    assert rev.payload["non_scoring"] == [{"scan_ref": "e:8:d", "reason": "no_evidence"},
                                          {"scan_ref": "e:9:d", "reason": "no_interpretation"}]
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"]]
    # Still on the experiment map: we looked there, and the footprint says so.
    assert {f["scan_ref"] for f in rev.payload["footprints"]} == {"e:1:d", "e:8:d", "e:9:d"}


def test_a_trusted_fitless_arena_fills_the_experiment_map():
    """SCENE-4 PR 4.0 (#26): a TRUSTED measurement with no usable interpretation (candidates
    empty — a fit crashed, or converged to nothing) is a different claim from a broken arena. It
    still fills the experiment map: non-scoring, but footprinted, exactly like any other
    non-scoring-but-measured arena above."""
    obs = [mk_obs("e:1:d", [cand("A", -10.0)]),
           mk_obs("e:9:d", [], q=(6.0, 0.0, 0.0), count_trust="trusted")]
    rev = build_scene(obs, factors=[ZERO])
    assert {"scan_ref": "e:9:d", "reason": "no_interpretation"} in rev.payload["non_scoring"]
    assert {f["scan_ref"] for f in rev.payload["footprints"]} == {"e:1:d", "e:9:d"}


def test_a_count_untrusted_arena_earns_no_footprint_but_is_recorded():
    """A gate outcome (excluded / held_for_calibration / undersampled) was ATTEMPTED but never
    actually measured, unlike every other non-scoring arena above — so it takes NO part in the
    footprint set or the path topology, while still appearing in ``non_scoring`` (nothing here is
    ever silently dropped)."""
    obs = [mk_obs("e:1:d", [cand("A", -10.0)]),
           mk_obs("e:5:d", [], q=(3.0, 0.0, 0.0), count_trust="excluded")]
    rev = build_scene(obs, factors=[ZERO])
    assert {"scan_ref": "e:5:d", "reason": "count_untrusted"} in rev.payload["non_scoring"]
    assert {f["scan_ref"] for f in rev.payload["footprints"]} == {"e:1:d"}
    # Nothing is deleted: the arena still travels with the revision, just with no coverage.
    assert {o["scan_ref"] for o in rev.payload["observations"]} == {"e:1:d", "e:5:d"}


def test_an_incomplete_comparison_never_becomes_a_confident_singleton():
    """H7's failure mode, exactly: the evidence budget expired with candidate B unresolved, so A
    is the only lnZ present. Scoring it would publish P(A)=1 for a comparison that never finished
    — the arena is experiment-map-only until the comparison is."""
    obs = [mk_obs("e:1:d", [cand("A", -10.0), cand("B", None)],
                  status="incomplete", fallback="evidence_budget_exhausted")]
    rev = build_scene(obs, factors=[ZERO])
    assert rev.payload["non_scoring"] == [{"scan_ref": "e:1:d", "reason": "evidence_incomplete"}]
    assert rev.payload["clusters"] == []
    # Nothing is deleted: the rows it did have travel with the revision.
    assert [c["label"] for c in rev.payload["observations"][0]["candidates"]] == ["A", "B"]


@pytest.mark.parametrize("sink,reason", [
    ("auxiliary_track_node", "aux_sink_deferred"),
    ("track_edge_evidence", "aux_sink_deferred"),
    ("branch_belief_only", "aux_sink_deferred"),
    ("unknown_sink", "unknown_sink"),          # the reader's own quarantine marker
    ("some_future_routing", "unknown_sink"),   # anything this repo cannot interpret
])
def test_non_canonical_sinks_defer_or_quarantine_but_keep_their_rows(sink, reason):
    obs = [mk_obs("e:1:d", [cand("A", -10.0), cand("B", -11.0)], sink=sink)]
    rev = build_scene(obs, factors=[ZERO])
    assert rev.payload["non_scoring"] == [{"scan_ref": "e:1:d", "reason": reason}]
    assert rev.payload["clusters"] == []
    # Deferred routing, not discarded evidence: the candidate rows are embedded verbatim.
    assert [c["label"] for c in rev.payload["observations"][0]["candidates"]] == ["A", "B"]
    assert rev.payload["footprints"][0]["scan_ref"] == "e:1:d"


@pytest.mark.parametrize("sink", [None, "canonical_master"])
def test_canonical_and_absent_sinks_both_score(sink):
    rev = build_scene([mk_obs("e:1:d", [cand("A", -10.0)], sink=sink)], factors=[ZERO])
    assert [c["scan_refs"] for c in rev.payload["clusters"]] == [["e:1:d"]]


def test_conditioning_provenance_rides_into_the_revision():
    """H9 provenance is builder-level bookkeeping for the exactly-once rule, so it must survive
    into the embedded rows — while staying off ComponentObservation, which factors see."""
    constraints = ({"constraint_kind": "hard", "parameter": "phonon.Gamma",
                    "origin": "transported_prior", "value": 0.5},)
    # A real ``CONSTRAINT_ORIGINS`` member (SCENE-4 PR 4.0 #21 gives the field a closed
    # vocabulary): ``prior_predictive`` is data-derived transported conditioning, so this
    # arena is non-scoring under #21 — irrelevant here, since ``payload["observations"]`` carries
    # every validated row regardless of scoring status, which is the one thing this test checks.
    obs = mk_obs("e:1:d", [cand("A", -10.0)])
    obs = replace(obs, candidates=(replace(obs.candidates[0],
                                           constraint_origin="prior_predictive",
                                           effective_constraints=constraints),))
    obs = replace(obs, fingerprint=observation_fingerprint(replace(obs, fingerprint="")))
    row, = build_scene([obs], factors=[ZERO]).payload["observations"][0]["candidates"]
    assert row["constraint_origin"] == "prior_predictive"
    assert row["effective_constraints"] == [dict(constraints[0])]


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
    assert cl["log_discarded_mass_bound"] == pytest.approx(math.log(115) + lowest - leader,
                                                           rel=1e-12)


def test_discarded_bound_survives_counts_far_outside_the_float_range():
    """A cluster of a few hundred binary arenas overflows float long before the bound does — and
    in log space the bound itself never does."""
    assert _log_discarded_bound(115, -30.0, -6.0) == pytest.approx(math.log(115) - 24.0, rel=1e-12)
    huge = 2 ** 1100                                     # unconvertible to float at all
    assert _log_discarded_bound(huge, -800.0, 0.0) == pytest.approx(1100 * math.log(2) - 800)
    # The case the linear form reported as ``inf``: still a finite, usable number here.
    assert _log_discarded_bound(huge, -1.0, 0.0) == pytest.approx(1100 * math.log(2) - 1.0)


def test_a_bound_beyond_the_linear_float_range_is_still_a_finite_log_bound():
    """Review finding E: the linear bound overflowed to ``inf``, which is not JSON, so the payload
    recorded "no bound" for a truncation that HAD one — and a vacuous ``inf`` read as a bounded
    approximation before that. In log space the number is always there, and the only unbounded
    lane left is the real one."""
    # A factor spanning 1000 nats: exp(-5 + 1000) overflowed, -5 + 1000 does not.
    obs = [mk_obs("e:1:d", [cand("A", 0.0), cand("B", -5.0), cand("C", -9.0)])]
    rev = build_scene(obs, factors=[biased(label="B", term=1000.0)], alt_cap=2)
    cl = cluster_of(rev, "e:1:d")
    assert cl["truncated"] is True and cl["truncation_unbounded"] is False
    assert cl["log_discarded_mass_bound"] == pytest.approx(995.0, rel=1e-12)
    assert "discarded_mass_unbounded" not in cl        # the flag the log-space bound retires
    canonical_bytes(rev.payload)                       # strict JSON, no Infinity anywhere


def test_canonical_bytes_refuses_a_non_finite_payload():
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError):
            canonical_bytes({"x": bad})


def test_score_ablation_alignment_is_declared_in_the_payload():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    assert rev.payload["score_ablation_alignment"] == "by_alternative_index"
    for cl in rev.payload["clusters"]:
        assert len(cl["score_ablation"]["zero"]) == len(cl["alternatives"])


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
# Ledger and score-term ablation
# ----------------------------------------------------------------------------
def test_ledger_reconstructs_to_the_stored_numbers_exactly():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO, biased()])
    for cl in rev.payload["clusters"]:
        for a in cl["alternatives"]:
            terms = {**a["factor_terms"], **a["prior_terms"]}
            total = math.fsum([a["base"], *(terms[k] for k in sorted(terms))])
            assert total == a["total"]
            assert math.exp(total - cl["log_norm"]) == a["posterior"]


def test_score_ablation_reproduces_the_build_without_that_factor():
    obs = chain_and_distant()
    with_bias = build_scene(obs, factors=[COUPLER, ZERO, biased()])
    without = build_scene(obs, factors=[COUPLER, ZERO])
    for cl, ref_cl in zip(with_bias.payload["clusters"], without.payload["clusters"]):
        assert [a["assignment"] for a in cl["alternatives"]] == \
            [a["assignment"] for a in ref_cl["alternatives"]]
        assert cl["score_ablation"]["biased"] == [a["posterior"] for a in ref_cl["alternatives"]]


def test_score_ablation_of_a_neutral_factor_is_the_posterior_itself():
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    for cl in rev.payload["clusters"]:
        assert cl["score_ablation"]["zero"] == [a["posterior"] for a in cl["alternatives"]]


def test_the_biased_factor_actually_moves_the_posterior():
    """Guards the ablation tests above from passing because nothing ever changed."""
    obs = chain_and_distant()
    biased_rev = build_scene(obs, factors=[COUPLER, biased(term=3.0)])
    neutral = build_scene(obs, factors=[COUPLER, ZERO])
    cl, ref = cluster_of(biased_rev, "e:1:d"), cluster_of(neutral, "e:1:d")
    assert [a["posterior"] for a in cl["alternatives"]] != \
        [a["posterior"] for a in ref["alternatives"]]
    assert any(a["reasons"].get("biased") == ["biased_hit"] for a in cl["alternatives"])


# ----------------------------------------------------------------------------
# The pruning/cap contract under declared factor bounds
# ----------------------------------------------------------------------------
def test_a_bounded_factor_rescues_the_candidate_the_bare_margin_would_have_dropped():
    """The reviewer's example, verbatim. A at lnZ 0, B at lnZ -5, and a factor worth +10 nats
    to B. Deciding on base lnZ alone drops B and states a mass bound of e^-5 = 0.0067 - while
    B's true posterior is 0.993, so the recorded bound points the wrong way by two orders of
    magnitude. The declared bound widens the margin by the factor span, so B survives to be
    scored, and wins."""
    obs = [mk_obs("e:1:d", [cand("A", 0.0), cand("B", -5.0)])]
    cl = cluster_of(build_scene(obs, factors=[biased(label="B", term=10.0)], margin_nats=1.0),
                    "e:1:d")
    assert cl["pruning"]["e:1:d"] == []                     # 5 nats < margin 1 + span 10
    posterior = {a["assignment"][0][1]: a["posterior"] for a in cl["alternatives"]}
    assert posterior["B"] == pytest.approx(1.0 / (1.0 + math.exp(-5.0)), rel=1e-12)
    assert posterior["B"] > 0.99

    # Same margin, no factor to rescue anything: B goes, and the bare bound is then the truth.
    bare = cluster_of(build_scene(obs, factors=[], margin_nats=1.0), "e:1:d")
    dropped, = bare["pruning"]["e:1:d"]
    assert (dropped["label"], dropped["reason"]) == ("B", "margin_pruned")
    assert dropped["mass_bound"] == pytest.approx(math.exp(-5.0), rel=1e-12)
    assert dropped["effective_margin_nats"] == 1.0


def test_an_unbounded_scoring_factor_disables_margin_pruning_entirely():
    """A factor that will not declare a bound could rescue anything, so nothing may be discarded
    on base lnZ - a candidate 150 nats down is retained and scored."""
    obs = [mk_obs("e:1:d", [cand("lead", -10.0), cand("hopeless", -160.0),
                            cand("dead", -11.0, usable=False)])]
    rev = build_scene(obs, factors=[UNBOUNDED])
    assert rev.payload["score_span_nats"] is None
    cl = cluster_of(rev, "e:1:d")
    assert {a["assignment"][0][1] for a in cl["alternatives"]} == {"lead", "hopeless"}
    # The one exclusion a factor CANNOT reverse still stands: no summable lnZ, no term to rescue.
    assert [(e["label"], e["reason"]) for e in cl["pruning"]["e:1:d"]] == [("dead", "no_evidence")]


def test_an_unbounded_factor_over_the_cap_states_no_bound_at_all():
    """World-11 honesty: an unbounded truncation is an UNRESOLVED cluster, never a silently
    narrowed one, and the marginals a reader lifts out of it carry that with them."""
    obs = [mk_obs("e:1:d", [cand("A", 0.0), cand("B", -1.0), cand("C", -2.0)])]
    cl = cluster_of(build_scene(obs, factors=[UNBOUNDED], alt_cap=2), "e:1:d")
    assert cl["truncated"] is True and cl["truncation_unbounded"] is True
    assert cl["log_discarded_mass_bound"] is None
    assert cl["marginals"]["truncation_unbounded"] is True


def test_a_bounded_truncation_widens_its_stated_bound_by_the_factor_span():
    obs = [mk_obs("e:1:d", [cand("A", 0.0), cand("B", -5.0), cand("C", -9.0)])]
    rev = build_scene(obs, factors=[biased(label="B", term=10.0)], alt_cap=2)
    assert rev.payload["score_span_nats"] == 10.0
    cl = cluster_of(rev, "e:1:d")
    assert cl["truncated"] is True and cl["truncation_unbounded"] is False
    # One dropped alternative (C), lowest kept base -5, leader base 0, span 10.
    assert cl["log_discarded_mass_bound"] == pytest.approx(5.0, rel=1e-12)
    # A bound above 1 is a true bound that constrains nothing - and says so, rather than
    # claiming e^-5 of dropped mass while a +10 nat factor sits on the dropped alternative.
    assert cl["log_discarded_mass_bound"] > 0.0


def test_an_empty_factor_set_keeps_the_bare_margin_and_the_bare_bound():
    """With no scoring factor the span is 0 and every number is the bare arithmetic.

    Re-pinned in SCENE-3 PR 3b: the LIVE registry is no longer empty (presence + connectivity,
    both unbounded), so ``build_scene(obs)`` and ``build_scene(obs, factors=[])`` are deliberately
    different scenes now — see ``test_the_live_registry_is_the_two_physics_factors``. What this
    test protects is the empty-factor-set arithmetic itself.
    """
    obs = chain_and_distant()
    assert build_scene(obs, factors=[]).payload["score_span_nats"] == 0.0

    pruned = [mk_obs("e:1:d", [cand("lead", -10.0), cand("far", -160.0)])]
    cl = cluster_of(build_scene(pruned, factors=[]), "e:1:d")
    dropped, = cl["pruning"]["e:1:d"]
    assert dropped["delta_nats"] == 150.0
    assert dropped["mass_bound"] == math.exp(-150.0)
    assert dropped["effective_margin_nats"] == build_mod.SCENE_PRUNE_MARGIN

    capped = cluster_of(build_scene(
        [mk_obs("e:1:d", [cand("A", 0.0), cand("B", -5.0), cand("C", -9.0)])],
        factors=[], alt_cap=2), "e:1:d")
    assert capped["log_discarded_mass_bound"] == pytest.approx(-5.0, rel=1e-12)
    assert capped["truncation_unbounded"] is False


def test_the_default_cap_enumerates_a_realistic_cluster_exactly():
    """Six arenas x three candidates = 729 sat above the old cap of 64, so every such scene
    published a truncated posterior by default. Exact enumeration is the intended posture."""
    assert build_mod.SCENE_ALT_CAP == 4096
    obs = [mk_obs(f"e:{i}:d", [cand(lbl, -1.0 * (i + 1)) for lbl in "ABC"],
                  q=(1.0 + 0.1 * i, 0.0, 0.0)) for i in range(6)]
    cl = cluster_of(build_scene(obs, factors=[COUPLER, ZERO]), "e:0:d")
    assert cl["n_possible"] == 729 and cl["n_enumerated"] == 729
    assert cl["truncated"] is False and cl["log_discarded_mass_bound"] is None


# ----------------------------------------------------------------------------
# The graph is built from scoreable survivors only
# ----------------------------------------------------------------------------
OMEGA_COUPLER = SceneFactor(
    name="omega_adjacency", kind="structural_likelihood", version="1", consumes=("omega",),
    couples=lambda a, b: (a.omega is not None and b.omega is not None
                          and abs(a.omega - b.omega) <= 0.5))


def _bridged_scene(with_bridge: bool):
    """Two omega-separated arena pairs, optionally bridged by ONE unusable candidate."""
    middle = [cand("A", -12.0, omegas=(10.1,))]
    if with_bridge:
        middle.append(cand("bridge", -3.0, usable=False, omegas=(30.0,)))
    return build_scene([
        mk_obs("e:1:d", [cand("A", -10.0, omegas=(10.0,)), cand("B", -11.0, omegas=(10.2,))]),
        mk_obs("e:2:d", middle),
        mk_obs("e:5:d", [cand("A", -9.0, omegas=(30.0,))]),
        mk_obs("e:6:d", [cand("A", -9.4, omegas=(30.1,))]),
    ], factors=[OMEGA_COUPLER, ZERO])


def test_an_excluded_candidate_cannot_bridge_two_clusters():
    """Reviewer #8. The bridging candidate has no usable evidence, so it is never enumerated -
    but building the graph before pruning let it merge two independent clusters into one joint
    normalization it takes no part in, and truncation is joint over a cluster."""
    bridged, plain = _bridged_scene(True), _bridged_scene(False)
    assert [c["scan_refs"] for c in plain.payload["clusters"]] == \
        [["e:1:d", "e:2:d"], ["e:5:d", "e:6:d"]]
    assert [c["scan_refs"] for c in bridged.payload["clusters"]] == \
        [["e:1:d", "e:2:d"], ["e:5:d", "e:6:d"]]
    # Byte-identical numbers, not merely the same partition. (The pruning ledger differs, by
    # design: the excluded candidate is recorded, it is only barred from scoring.)
    assert [canonical_bytes(c["marginals"]) for c in bridged.payload["clusters"]] == \
        [canonical_bytes(c["marginals"]) for c in plain.payload["clusters"]]
    assert [(e["label"], e["reason"]) for e in cluster_of(bridged, "e:1:d")["pruning"]["e:2:d"]] \
        == [("bridge", "no_evidence")]


# ----------------------------------------------------------------------------
# The linkage variable (PR 3b), as the builder owns it
# ----------------------------------------------------------------------------
def test_the_default_linkage_chains_nearest_omega_and_leaves_the_unreachable_unlinked():
    """Nearest-ω chaining, and the refusal that matters: the 9 meV mode has no partner within
    tolerance at the neighbour, so it stays UNLINKED rather than being forced onto a distant one.
    A forced link would invent a track the data does not support."""
    obs = [mk_obs("e:1:d", [cand("A", -10.0, n_modes=2, omegas=(4.0, 9.0))], q=(1.0, 0.0, 0.0)),
           mk_obs("e:2:d", [cand("A", -10.0, omegas=(4.3,))], q=(1.05, 0.0, 0.0))]
    cl = cluster_of(build_scene(obs, factors=[COUPLER, ZERO]), "e:1:d")
    alt, = cl["alternatives"]
    assert alt["links"] == [["e:1:d#A#0", "e:2:d#A#0"]]


def test_without_a_proposer_every_assignment_has_exactly_one_linkage():
    """A factor set with no ``propose`` leaves the alternative count exactly what 3a enumerated."""
    cl = cluster_of(build_scene(chain_and_distant(), factors=[COUPLER, ZERO]), "e:1:d")
    assert cl["n_enumerated"] == cl["n_assignments"] == 8
    assert cl["linkage_truncated"] is False


# ----------------------------------------------------------------------------
# Immutability
# ----------------------------------------------------------------------------
def test_a_factor_cannot_mutate_the_cluster_view():
    """Reviewer #5. A frozen dataclass freezes the fields, not the dicts behind them: a factor
    writing into the view would change what every later factor and alternative scores against."""
    def score(alt, view):
        with pytest.raises(TypeError):
            view.components[alt.cluster_id] = ()
        with pytest.raises(TypeError):
            view.footprints[alt.cluster_id] = None
        return 0.0, ()

    f = SceneFactor(name="mutator", kind="structural_likelihood", version="1",
                    score_bound=(0.0, 0.0), score=score)
    assert build_scene(chain_and_distant(), factors=[COUPLER, f]).digest


def test_the_payload_handed_out_cannot_desynchronize_from_the_digest():
    """Reviewer #6. The digest is taken over stored bytes, and every ``payload`` read is a fresh
    copy of them, so no holder of a payload dict can leave the revision self-inconsistent."""
    rev = build_scene(chain_and_distant(), factors=[COUPLER, ZERO])
    scribbled = rev.payload
    scribbled["schema"] = "tampered"
    scribbled["clusters"][0]["alternatives"][0]["posterior"] = 0.5
    assert rev.payload["schema"] == SCENE_SCHEMA
    assert rev.payload["clusters"][0]["alternatives"][0]["posterior"] != 0.5
    assert rev.digest == digest_bytes(canonical_bytes(rev.payload))
    assert rev.digest == build_scene(chain_and_distant(), factors=[COUPLER, ZERO]).digest


# ----------------------------------------------------------------------------
# The factor manifest
# ----------------------------------------------------------------------------
DECLARED = SceneFactor(name="declared", kind="structural_likelihood", version="1",
                       consumes=("q",), config=(("threshold", 0.25),), score_bound=(0.0, 1.0),
                       score=lambda alt, view: (0.0, ()))


def test_the_payload_carries_the_full_factor_declaration():
    """Including the builder's OWN declared term: the per-assignment linkage prior is part of
    what scored the scene, so it is in the manifest and in the fingerprint like any factor."""
    rev = build_scene(chain_and_distant(), factors=[COUPLER, DECLARED])
    assert rev.payload["factor_manifest"] == [
        {"name": "declared", "kind": "structural_likelihood", "version": "1",
         "consumes": ["q"], "roles": ["score"], "config": [["threshold", 0.25]],
         "score_bound": [0.0, 1.0]},
        # v2, unbounded: the prior divides by the ENUMERATED linkage count rather than the kept
        # one (review finding 10), and nothing bounds how many readings the proposers offer.
        {"name": "linkage_prior", "kind": "scene_prior", "version": "2",
         "consumes": ["links"], "roles": [],
         "config": [["max_linkage_variants", float(build_mod.MAX_LINKAGE_VARIANTS)]],
         "score_bound": None},
        {"name": "q_adjacency", "kind": "structural_likelihood", "version": "1",
         "consumes": ["q"], "roles": ["couples"], "config": [], "score_bound": None},
    ]


def test_the_builders_linkage_prior_name_is_reserved():
    reserved = replace(DECLARED, name="linkage_prior")
    with pytest.raises(ValueError, match="reserved"):
        build_scene(chain_and_distant(), factors=[reserved])


@pytest.mark.parametrize("changed", [
    {"consumes": ("q", "omega")},
    {"config": (("threshold", 0.5),)},
    {"score_bound": (0.0, 2.0)},
    {"version": "2"},
    # A factor that grows a non-scoring hook is a different declaration too: it classifies
    # components, reinterprets absences, or draws cluster edges nothing else drew.
    {"classify": lambda view: {}},
    {"refine_visibility": lambda report, view: None},
    {"couples_arena": lambda c, fp: False},
    {"degradations": lambda sample: ()},
])
def test_the_registry_fingerprint_moves_with_every_declaration(changed):
    """The doc/code mismatch this closes: the fingerprint was taken over (name, kind, version)
    while the docstring promised ``consumes`` participated. A factor reading new variables, run
    at a new tuning, or promising a new bound is a different factor, and a revision that could
    not say so would look replayable against one it is not."""
    obs = chain_and_distant()
    a = build_scene(obs, factors=[DECLARED])
    b = build_scene(obs, factors=[replace(DECLARED, **changed)])
    assert a.payload["registry_fingerprint"] != b.payload["registry_fingerprint"]
    assert a.digest != b.digest


@pytest.mark.parametrize("bad,match", [
    ({"config": (("b", 1.0), ("a", 2.0))}, "unsorted config"),
    ({"config": (("a",),)}, "malformed config"),
    ({"score_bound": (1.0, 0.0)}, "score_bound"),
    ({"score_bound": (0.0, math.inf)}, "score_bound"),
    ({"score_bound": (0.0, 1.0), "score": None}, "no score"),
    ({"classify": "not a function"}, "non-callable classify"),
    ({"refine_visibility": 3}, "non-callable refine_visibility"),
    ({"couples_arena": "nope"}, "non-callable couples_arena"),
    ({"degradations": ()}, "non-callable degradations"),
])
def test_a_malformed_declaration_is_refused(bad, match):
    with pytest.raises(ValueError, match=match):
        build_scene(chain_and_distant(), factors=[replace(DECLARED, **bad)])


# ----------------------------------------------------------------------------
# Score-term ablation keeps the hypothesis space fixed
# ----------------------------------------------------------------------------
def test_score_ablation_removes_the_score_terms_but_not_the_partition_that_factor_built():
    """Reviewer #10, and the reason for the honest name: ablation answers "given the SAME
    hypotheses, how much did this factor's score terms move belief?". A factor's ``couples``
    contribution to what the hypotheses even are is deliberately not removed."""
    def score(alt, view):
        hit = any(chosen == "B" for _, chosen in alt.assignment)
        return (3.0 if hit else 0.0), ()

    both = SceneFactor(name="both_roles", kind="structural_likelihood", version="1",
                       consumes=("q",), score_bound=(0.0, 3.0), couples=_q_adjacent, score=score)
    obs = chain_and_distant()
    cl = cluster_of(build_scene(obs, factors=[both]), "e:1:d")
    # Its edges stand under ablation: the cluster is still the three-arena chain it built.
    assert cl["scan_refs"] == ["e:1:d", "e:2:d", "e:3:d"]
    ref = cluster_of(build_scene(obs, factors=[COUPLER, ZERO]), "e:1:d")
    assert [a["assignment"] for a in cl["alternatives"]] == \
        [a["assignment"] for a in ref["alternatives"]]
    assert cl["score_ablation"]["both_roles"] == \
        pytest.approx([a["posterior"] for a in ref["alternatives"]], rel=1e-12)
    # Guard: the score terms really were doing something before they were ablated away.
    assert [a["posterior"] for a in cl["alternatives"]] != \
        [a["posterior"] for a in ref["alternatives"]]
    # And ablation is NOT a rebuild without the factor - that scene has no chain at all.
    assert [c["scan_refs"] for c in build_scene(obs, factors=[]).payload["clusters"]] == \
        [["e:1:d"], ["e:2:d"], ["e:3:d"], ["e:9:d"]]


# ----------------------------------------------------------------------------
# Non-finite factor terms
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_factor_term_is_refused_naming_the_factor(bad):
    """Reviewer #9. A NaN term poisons the log-sum-exp into an all-NaN posterior that still
    "normalizes"; a -inf term deletes an alternative with no pruning record."""
    f = SceneFactor(name="broken", kind="structural_likelihood", version="1",
                    score_bound=(0.0, 0.0), score=lambda alt, view: (bad, ()))
    with pytest.raises(ValueError) as excinfo:
        build_scene(chain_and_distant(), factors=[COUPLER, f])
    message = str(excinfo.value)
    assert "broken" in message and "e:1:d" in message
    # -inf is not a veto in v1, and the message says what a vetoing factor would need instead.
    assert "veto" in message


def test_a_factor_that_exceeds_its_declared_bound_is_refused():
    """The bound is not documentation: pruning and truncation bounds are derived from it and
    candidates are discarded on its strength, so a factor that exceeds it has already made the
    recorded mass bounds false."""
    f = SceneFactor(name="liar", kind="structural_likelihood", version="1",
                    score_bound=(0.0, 0.0), score=lambda alt, view: (1.0, ()))
    with pytest.raises(ValueError, match="outside its declared score_bound"):
        build_scene(chain_and_distant(), factors=[f])
