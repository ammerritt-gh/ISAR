"""The scene-EIG MI scoring engine (SCENE-5 slice 5.1) — how much each candidate design would
teach the open questions, computed from the hypothesis-map / intensity-map comparison and
nothing else (the SCENE-5 governing razor, ``docs/plans/scene5-eig.md`` §1).

**Offline and pure (D2's decide-side-only invariant, extended).** No store reads, no I/O, no
persisted-contract change. Everything this module needs arrives as plain dicts already read by
the caller: a scored ``isar.scene-revision/1`` payload, an ``isar.agenda-revision/1`` payload
(whose embedded ``resolved_policy`` block supplies ``priority_weights``, ``paranoia_unknown_prior``
and ``mi_n_draws`` with no store read of its own), the caller-supplied declared
``[hypotheses.*]`` mapping (neither payload carries it in v1), the phenomenon family registry, a
list of candidate :class:`~isar.scene.phenomena.Design` placements, and a base seed.

**Relevance registry.** Mirrors :mod:`isar.scene.phenomena`'s family-registry idiom: one declared
:class:`RelevanceExtractor` ``(id, version, config)`` per question family, digested into
``relevance_fingerprint`` the same way ``registry_fingerprint`` digests the phenomenon families —
so a score table can never be silently replayed under a retuned extractor set. An extractor maps
one cluster's persisted alternatives to the question's own variable classes (D5: nuisance
variables never leak between questions, because each family's extractor answers only its own
question). ``background_counts_per_point`` — the one constant every alternative's composed rate
vector adds, in both states of the DiscoverRegion Bernoulli — lives declared on the
``DiscoverRegion`` entry (the D6 coverage-value entry point) but is read by every family alike;
one declared number, one manifest entry, so a retune moves the one fingerprint that should move.

**The MI estimator.** Per (design, open question): every matched cluster whose extractor
partitions the alternatives into >=2 classes contributes an outer sum over its alternatives
(weighted by the payload's own ``posterior``), drawing ``mi_n_draws`` seeded Poisson vectors from
each alternative's COMPOSED rate vector (the sum of its :func:`isar.scene.phenomena.predict`
rates plus background — Poisson superposition, so a two-mode alternative separates from a
one-mode alternative by exactly the second mode's counts), then estimating
``MI = H(g) - E_Y[H(g|Y)]`` in nats from the resulting class posteriors. Additive across clusters.
DiscoverRegion is the one family with no persisted cluster: a synthetic two-state Bernoulli
(present = unknown-family predictive + background, absent = background only, prior =
``paranoia_unknown_prior``) at a design's node the scene has not already surveyed, scored through
the SAME estimator machinery — a prior of exactly 0 short-circuits to MI 0 with no Monte Carlo
noise, never a near-zero float. Seeds are derived deterministically
(:func:`isar.scene.model.fingerprint` over ``{design key, cluster id, question id, alternative
index, base seed}``), so a replay on the same inputs reproduces byte-identical draws.

**Not checked here.** Scene/agenda digest agreement is 5.2's concern (the caller wires a scene
revision and an agenda derived from it together); this module trusts the two payloads it is
handed. The one sanity refusal it does perform is on the ``resolved_policy`` block itself
(:func:`isar.schema.question_policy.validate_resolved_block` — a legal cross-layer import, not a
banned one: see ``tests/scene/test_import_ban.py``'s ``FORBIDDEN_PREFIXES``) and on a design list
carrying two designs with the same canonical key.

**Judgment calls made here, named rather than hidden:**

- ``ConfirmPresence``/``ClassifyIdentity``/``TestModelMismatch`` match a subject's clusters via
  :func:`isar.scene.questions.match_subject` (closed-interval ``region_rlu``/``omega_band_meV``
  membership) — the same matching predicates.py's own evaluators use — rather than re-deriving a
  half-open tile test for the subject's region; ``EstimateParameter`` and ``ConnectTrack`` use
  half-open grid-cell membership (:func:`isar.scene.predicates.tile_contains`) exactly where
  predicates.py itself does (endpoint tiles, the emergent ω tile).
- ``ConnectTrack``'s endpoint→component→reachability walk REIMPLEMENTS
  ``isar.scene.predicates._track_connected``'s private machinery minimally against the same
  payload fields, rather than importing it — that function is private, and MI needs the raw
  per-alternative reachability, not the aggregated existential mass ``_track_connected`` answers.
  The presence/identity/mismatch helpers below are the same posture: small private mirrors of
  ``predicates.py``'s own private per-alternative walks, never imports of them.
- ``ClassifyIdentity``'s class label is the alternative's one claimed identity when its matched
  in-band mode components agree, ``"none"`` when none carry an identity, and a sorted
  ``"ambiguous:..."`` composite on the rare disagreeing case — predicates.py's own evaluator
  never has to make this call because it asks a existential/universal mass question, not a
  per-alternative partition.
- ``EstimateParameter``'s class is the FIRST matched in-band mode's ω tile
  (:func:`isar.scene.questions.snap_tile` on ``emergent_omega_tile_meV``) when an alternative
  places more than one; the spec subject names one parameter, so a well-formed subject should
  never have an alternative asserting two different matched modes in one band.
- The background reference exposure (``background_counts_per_point`` is declared "at the family
  registry's reference_ncount exposure") is read off the ``curve`` family's own
  ``reference_ncount`` — every shipped family declares the same value today, and ``curve`` is the
  one family every question ultimately measures against.
- DiscoverRegion's "no cluster/footprint covers it" test is node-level: any persisted footprint
  in the subject's partition sitting at the design's exact ``axis_coordinate`` counts as covered.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from isar.schema.question_policy import validate_resolved_block

from .model import SampleKnowledge, fingerprint
from .phenomena import (FAMILIES as DEFAULT_PHENOMENON_FAMILIES, Design, PhenomenonBelief,
                        PhenomenonFamily, build_beliefs, predict,
                        registry_fingerprint as phenomenon_registry_fingerprint)
from .predicates import tile_contains
from .questions import QuestionSpec, QuestionSubject, match_subject, snap_tile

# ----------------------------------------------------------------------------
# Relevance registry
# ----------------------------------------------------------------------------
RELEVANCE_FAMILY_KINDS = ("ConfirmPresence", "ClassifyIdentity", "ConnectTrack",
                          "EstimateParameter", "TestModelMismatch", "VerifyCompleteness",
                          "DiscoverRegion")


@dataclass(frozen=True, slots=True)
class RelevanceExtractor:
    """One declared relevance mapping: which question family it serves, and every tunable
    constant it closes over — the same ``(name, version, config)`` discipline as
    ``isar.scene.phenomena.PhenomenonFamily``, and for the same reason: a retuned constant is a
    different extractor, and without this a score table computed under one tuning could not be
    told apart from one computed under another."""

    name: str
    version: str
    config: tuple[tuple[str, float], ...] = ()

    def cfg(self, key: str, default=None):
        for k, v in self.config:
            if k == key:
                return v
        return default


# SCENE-5 5.3 (Ruling A): the declared ``thermal_consistency`` corroboration component — a
# synthetic two-state MI term (module docstring below) added per contributing cluster, priced by
# the SAME ``paranoia_unknown_prior`` that prices DiscoverRegion's coverage doubt. Version "2":
# the component's presence is a declared physics-path change, so a score table computed under
# "1" (no corroboration term) can never be silently replayed as if scored under "2".
CONFIRM_PRESENCE = RelevanceExtractor(
    "ConfirmPresence", "2",
    config=(("thermal_consistency_component", "thermal_consistency/1"),))
CLASSIFY_IDENTITY = RelevanceExtractor("ClassifyIdentity", "1")
CONNECT_TRACK = RelevanceExtractor("ConnectTrack", "1")
ESTIMATE_PARAMETER = RelevanceExtractor("EstimateParameter", "1")
TEST_MODEL_MISMATCH = RelevanceExtractor("TestModelMismatch", "1")
VERIFY_COMPLETENESS = RelevanceExtractor("VerifyCompleteness", "1")
# The declared background prior (module docstring): read by every family's composed rate vector,
# not only DiscoverRegion's own Bernoulli.
DISCOVER_REGION = RelevanceExtractor("DiscoverRegion", "1",
                                     config=(("background_counts_per_point", 2.0),
                                             # SCENE-5 5.2: the declared, fingerprinted grid the
                                             # planner scores candidates on — never the executed
                                             # grid (ADR-018's identity is untouched; this is a
                                             # scoring-time approximation only). Living here
                                             # rather than a bare module constant means a retune
                                             # moves ``relevance_fingerprint`` like every other
                                             # declared number in this registry.
                                             ("scoring_grid_step_meV", 0.5)))

RELEVANCE_REGISTRY: dict[str, RelevanceExtractor] = {
    e.name: e for e in (CONFIRM_PRESENCE, CLASSIFY_IDENTITY, CONNECT_TRACK, ESTIMATE_PARAMETER,
                        TEST_MODEL_MISMATCH, VERIFY_COMPLETENESS, DISCOVER_REGION)}


def relevance_manifest() -> tuple[RelevanceExtractor, ...]:
    """The full declaration of every relevance extractor, in name order — order-free like
    ``phenomena.family_manifest``."""
    return tuple(sorted(RELEVANCE_REGISTRY.values(), key=lambda e: e.name))


def _relevance_manifest_dicts() -> list[dict]:
    return [{"name": e.name, "version": e.version, "config": [[k, v] for k, v in e.config]}
           for e in relevance_manifest()]


def relevance_fingerprint() -> str:
    """Fingerprint of the relevance manifest. Changes iff any declared extractor value changes."""
    return fingerprint(_relevance_manifest_dicts())


def _background_counts_per_point() -> float:
    return float(RELEVANCE_REGISTRY["DiscoverRegion"].cfg("background_counts_per_point"))


def scoring_grid_step_meV() -> float:
    """The declared scoring-grid quantum (SCENE-5 5.2) — the planner's caller-facing accessor,
    so it never re-reads the registry's internal config layout directly."""
    return float(RELEVANCE_REGISTRY["DiscoverRegion"].cfg("scoring_grid_step_meV"))


# ----------------------------------------------------------------------------
# Typed scoring-layer fault boundary (SCENE-5 5.2, D7)
# ----------------------------------------------------------------------------
class SceneScoringError(Exception):
    """The ONLY fault the planner's software-fault fallback may catch (D7). Raised around belief
    construction (``reason="scene_belief_construction_failed"``) and around estimation
    (``reason="scoring_exception"``) in :func:`score`, wrapping whatever unexpected exception
    those phases raised. Input-validation ``ValueError``s (an invalid ``resolved_policy`` block,
    duplicate design keys) are caller bugs and stay plain ``ValueError`` — they propagate, never
    recorded as a scoring fallback."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ----------------------------------------------------------------------------
# Design identity
# ----------------------------------------------------------------------------
def _design_dict(design: Design) -> dict:
    return {"axis_coordinate": design.axis_coordinate, "window": list(design.window),
            "n_points": design.n_points, "ncount": design.ncount,
            "temperature_K": design.temperature_K}


def design_key(design: Design) -> str:
    """The canonical identity of one candidate design — a fingerprint of its declared fields,
    used both as the tie-break in a sorted :class:`ScoreTable` and as one ingredient of every
    seed this module derives."""
    return fingerprint(_design_dict(design))


def _derive_seed(design: Design, cluster_id: str, question_id: str, alternative_index: int,
                 base_seed: int, *, component: str | None = None) -> int:
    """A seed that is a deterministic, content-derived function of exactly what it should be —
    never a running counter or iteration-order artifact, so a replay on the same inputs draws
    byte-identical Poisson vectors regardless of dict/cluster iteration order.

    ``component`` (SCENE-5 5.3) is omitted from the payload by default, so every pre-5.3 seed is
    byte-identical to before; the ``thermal_consistency`` corroboration term passes a distinct
    component name so its two-state draws never collide with a partition-MI alternative's seed
    at the same ``(design, cluster, question)`` key."""
    payload = {"design_key": design_key(design), "cluster_id": cluster_id,
              "question_id": question_id, "alternative_index": alternative_index,
              "base_seed": base_seed}
    if component is not None:
        payload["component"] = component
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=False) \
        & ((1 << 63) - 1)


# ----------------------------------------------------------------------------
# Per-alternative payload access (small private mirrors of predicates.py's own private walks —
# never imports of them; see the module docstring)
# ----------------------------------------------------------------------------
def _candidates_by_ref(scene_payload: Mapping) -> dict[str, dict[str, Mapping]]:
    out: dict[str, dict[str, Mapping]] = {}
    for obs in scene_payload.get("observations") or ():
        out[obs["scan_ref"]] = {c["label"]: c for c in obs.get("candidates") or ()}
    return out


def _in_band(omega, band) -> bool:
    if omega is None or not (isinstance(omega, (int, float)) and math.isfinite(omega)):
        return False
    if band is None:
        return True
    return band[0] <= abs(omega) <= band[1]


def _matched_mode_omegas(alternative: Mapping, refs: set, candidates: Mapping, band) -> list:
    out = []
    for scan_ref, label in alternative.get("assignment") or ():
        if scan_ref not in refs:
            continue
        row = candidates.get(scan_ref, {}).get(label)
        if row is None:
            continue
        omegas = row.get("omegas") or []
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            slot = component["slot"]
            omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(omega, band):
                out.append(float(omega))
    return out


def _matched_identities(alternative: Mapping, refs: set, candidates: Mapping, band) -> set:
    out = set()
    for scan_ref, label in alternative.get("assignment") or ():
        if scan_ref not in refs:
            continue
        row = candidates.get(scan_ref, {}).get(label)
        if row is None:
            continue
        omegas = row.get("omegas") or []
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            slot = component["slot"]
            omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(omega, band):
                out.add(row.get("identity"))
                break
    return out


# ----------------------------------------------------------------------------
# Per-family class extractors: (cluster_index, {alternative_index: class_label}) pairs
# ----------------------------------------------------------------------------
def _confirm_presence_classes(spec: QuestionSpec, scene_payload: Mapping):
    match = match_subject(spec.subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), spec.subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()
    out = []
    for idx in match.cluster_indices:
        classes = {}
        for a_idx, alt in enumerate(clusters[idx].get("alternatives") or ()):
            present = bool(_matched_mode_omegas(alt, refs, candidates, band))
            classes[a_idx] = "present" if present else "absent"
        out.append((idx, classes))
    return out


def _classify_identity_classes(spec: QuestionSpec, scene_payload: Mapping):
    match = match_subject(spec.subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), spec.subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()
    out = []
    for idx in match.cluster_indices:
        classes = {}
        for a_idx, alt in enumerate(clusters[idx].get("alternatives") or ()):
            identities = _matched_identities(alt, refs, candidates, band)
            identities.discard(None)
            if not identities:
                label = "none"
            elif len(identities) == 1:
                label = next(iter(identities))
            else:
                label = "ambiguous:" + ",".join(sorted(identities))
            classes[a_idx] = label
        out.append((idx, classes))
    return out


def _mismatch_classes(spec: QuestionSpec, scene_payload: Mapping):
    match = match_subject(spec.subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs = set(match.scan_refs)
    clusters = scene_payload.get("clusters") or ()
    out = []
    for idx in match.cluster_indices:
        classes = {}
        for a_idx, alt in enumerate(clusters[idx].get("alternatives") or ()):
            has_unknown = False
            for scan_ref, label in alt.get("assignment") or ():
                if scan_ref not in refs:
                    continue
                row = candidates.get(scan_ref, {}).get(label)
                if row is not None and row.get("identity") == "unknown":
                    has_unknown = True
                    break
            classes[a_idx] = has_unknown
        out.append((idx, classes))
    return out


def _estimate_parameter_classes(spec: QuestionSpec, scene_payload: Mapping, quantum: float):
    match = match_subject(spec.subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), spec.subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()
    out = []
    for idx in match.cluster_indices:
        classes = {}
        for a_idx, alt in enumerate(clusters[idx].get("alternatives") or ()):
            omegas = _matched_mode_omegas(alt, refs, candidates, band)
            classes[a_idx] = "none" if not omegas else snap_tile(omegas[0], quantum)
        out.append((idx, classes))
    return out


def _connect_track_classes(spec: QuestionSpec, scene_payload: Mapping):
    """Mirrors ``isar.scene.predicates._track_connected``'s component->coordinate mapping and
    reachability walk, minimally, against the same persisted fields (``component_id`` grammar,
    ``links`` adjacency) — reimplemented rather than imported because that function answers an
    aggregated existential mass, not the per-alternative reachability MI needs."""
    subject = spec.subject
    tiles = (subject.endpoint_a_rlu, subject.endpoint_b_rlu)
    if tiles[0] is None:
        return []
    footprints = [fp for fp in scene_payload.get("footprints") or ()
                  if fp.get("partition_id") == subject.partition_id
                  and fp.get("axis_coordinate") is not None]
    ends = [{fp["scan_ref"] for fp in footprints if tile_contains(tile, fp["axis_coordinate"])}
           for tile in tiles]
    if not ends[0] or not ends[1]:
        return []
    clusters = scene_payload.get("clusters") or ()
    joint = [i for i, cluster in enumerate(clusters)
             if ends[0].intersection(cluster.get("scan_refs") or ())
             and ends[1].intersection(cluster.get("scan_refs") or ())]

    def scan_of(component_id: str) -> str:
        return component_id.rsplit("#", 2)[0]

    out = []
    for idx in joint:
        classes = {}
        for a_idx, alt in enumerate(clusters[idx].get("alternatives") or ()):
            adjacency: dict[str, set] = {}
            for a, b in alt.get("links") or ():
                adjacency.setdefault(a, set()).add(b)
                adjacency.setdefault(b, set()).add(a)
            starts = [c for c in adjacency if scan_of(c) in ends[0]]
            seen = set(starts)
            frontier = list(starts)
            connected = False
            while frontier:
                node = frontier.pop()
                if scan_of(node) in ends[1]:
                    connected = True
                    break
                for neighbour in adjacency.get(node, ()):
                    if neighbour not in seen:
                        seen.add(neighbour)
                        frontier.append(neighbour)
            classes[a_idx] = connected
        out.append((idx, classes))
    return out


_FAMILY_EXTRACTORS = {
    "ConfirmPresence": _confirm_presence_classes,
    "ClassifyIdentity": _classify_identity_classes,
    "TestModelMismatch": _mismatch_classes,
    "ConnectTrack": _connect_track_classes,
}


# ----------------------------------------------------------------------------
# MI estimator
# ----------------------------------------------------------------------------
def _logsumexp(a: np.ndarray, axis: int) -> np.ndarray:
    m = np.max(a, axis=axis, keepdims=True)
    return np.squeeze(m + np.log(np.sum(np.exp(a - m), axis=axis, keepdims=True)), axis=axis)


def _mi_from_rates(rates: np.ndarray, posteriors: np.ndarray, classes: Sequence,
                   mi_n_draws: int, seeds: Sequence[int]) -> float:
    """``MI = H(g) - E_Y[H(g|Y)]`` in nats, for a class label ``g`` computed over ``len(rates)``
    alternatives with expected Poisson count vectors ``rates`` and posterior weights
    ``posteriors``. Outer sum over alternatives (their own declared prior); inner sum over
    ``mi_n_draws`` seeded Poisson draws per alternative, vectorized over the grid and over
    alternatives at once (the ``y!`` term is a constant that cancels in the normalized class
    posterior, so it is never formed). Tiny negative MC noise clamps to 0; a single-class
    partition, or a partition whose posterior mass is entirely on one class, is 0 exactly with no
    draws taken.
    """
    posteriors = np.asarray(posteriors, dtype=float)
    total = float(posteriors.sum())
    if total <= 0.0:
        return 0.0
    posteriors = posteriors / total
    classes = list(classes)
    class_names = sorted(set(classes), key=repr)
    prior_class = {c: float(sum(p for p, cl in zip(posteriors, classes) if cl == c))
                  for c in class_names}
    class_names = [c for c in class_names if prior_class[c] > 0.0]
    if len(class_names) < 2:
        return 0.0
    h_prior = -sum(prior_class[c] * math.log(prior_class[c]) for c in class_names)

    rates = np.asarray(rates, dtype=float)
    log_rates = np.log(np.clip(rates, 1e-300, None))
    sum_rates = rates.sum(axis=1)
    log_prior_a = np.log(np.clip(posteriors, 1e-300, None))

    e_h_given_y = 0.0
    for i, seed in enumerate(seeds):
        p_a = posteriors[i]
        if p_a <= 0.0:
            continue
        rng = np.random.default_rng(int(seed))
        draws = rng.poisson(rates[i], size=(mi_n_draws, rates.shape[1]))
        loglik = draws @ log_rates.T - sum_rates[None, :]           # (D, A)
        logpost = loglik + log_prior_a[None, :]
        denom = _logsumexp(logpost, axis=1)                         # (D,)
        h_y = np.zeros(mi_n_draws)
        for c in class_names:
            idxs = [j for j, cl in enumerate(classes) if cl == c]
            class_logpost = _logsumexp(logpost[:, idxs], axis=1)
            p_c = np.clip(np.exp(class_logpost - denom), 1e-300, 1.0)
            h_y -= p_c * np.log(p_c)
        e_h_given_y += p_a * float(h_y.mean())
    return max(0.0, h_prior - e_h_given_y)


def _composed_rate(beliefs: Sequence[PhenomenonBelief], design: Design,
                   families: Mapping[str, PhenomenonFamily], background: np.ndarray, *,
                   include_anti_stokes: bool = True) -> np.ndarray:
    vec = background.copy()
    for belief in beliefs:
        # draws discarded; only the rate is used. ``include_anti_stokes`` (SCENE-5 5.3) is the
        # thermal_consistency term's WITH/WITHOUT lobe switch — default True composes exactly
        # what every pre-5.3 caller already got.
        pred = predict(belief, design, families, seed=0, include_anti_stokes=include_anti_stokes)
        vec = vec + np.asarray(pred.expected_counts, dtype=float)
    return vec


def _leading_alternative_index(cluster: Mapping) -> int | None:
    """The highest-posterior alternative's index, or ``None`` for a cluster with none — ties
    break to the smaller index (stable, replay-identical over dict/list iteration order)."""
    alternatives = cluster.get("alternatives") or ()
    if not alternatives:
        return None
    best_idx, best_posterior = 0, -1.0
    for idx, alternative in enumerate(alternatives):
        posterior = float(alternative.get("posterior") or 0.0)
        if posterior > best_posterior:
            best_idx, best_posterior = idx, posterior
    return best_idx


def _thermal_consistency_mi(scene_payload: Mapping, cluster_index: int,
                            beliefs_by_ca: Mapping[tuple, Sequence[PhenomenonBelief]],
                            design: Design, families: Mapping[str, PhenomenonFamily],
                            background: np.ndarray, mi_n_draws: int, base_seed: int,
                            question_id: str, paranoia: float) -> float:
    """SCENE-5 5.3 (Ruling A) — the ConfirmPresence relevance extractor's declared
    ``thermal_consistency`` component: one synthetic two-state MI term per contributing cluster,
    {as_believed, not_as_believed}, rates = the cluster's LEADING alternative's composed rate
    WITH vs WITHOUT its anti-Stokes lobes (the predict-level lobe switch in
    :mod:`isar.scene.phenomena` — the SAME physics, never a second implementation). Prior =
    ``paranoia_unknown_prior``, the DiscoverRegion precedent: paranoia 0 short-circuits to
    exactly 0, no Monte Carlo noise. On a design whose window does not overlap where the
    anti-Stokes lobe sits (a +E window, or a curve belief with no declared temperature), the two
    states predict identical counts and the term is ~0 through the SAME MI estimator — no
    temperature/window branch here, the dominance is priced by the rates alone.
    """
    if paranoia <= 0.0:
        return 0.0
    cluster = scene_payload["clusters"][cluster_index]
    cluster_id = cluster["cluster_id"]
    alt_idx = _leading_alternative_index(cluster)
    if alt_idx is None:
        return 0.0
    beliefs = beliefs_by_ca.get((cluster_id, alt_idx), ())
    if not beliefs:
        return 0.0
    with_lobes = _composed_rate(beliefs, design, families, background)
    without_lobes = _composed_rate(beliefs, design, families, background,
                                   include_anti_stokes=False)
    rates = np.array([with_lobes, without_lobes])
    posteriors = np.array([paranoia, 1.0 - paranoia])
    classes = ["as_believed", "not_as_believed"]
    seeds = [_derive_seed(design, cluster_id, question_id, i, base_seed,
                          component="thermal_consistency") for i in (0, 1)]
    return _mi_from_rates(rates, posteriors, classes, mi_n_draws, seeds)


def _cluster_mi(scene_payload: Mapping, cluster_index: int, class_of: Mapping[int, object],
                beliefs_by_ca: Mapping[tuple, Sequence[PhenomenonBelief]], design: Design,
                families: Mapping[str, PhenomenonFamily], background: np.ndarray,
                mi_n_draws: int, base_seed: int, question_id: str) -> float:
    cluster = scene_payload["clusters"][cluster_index]
    cluster_id = cluster["cluster_id"]
    alternatives = cluster.get("alternatives") or ()
    posteriors = np.array([a.get("posterior") or 0.0 for a in alternatives], dtype=float)
    classes = [class_of.get(i, "none") for i in range(len(alternatives))]
    rates = np.array([_composed_rate(beliefs_by_ca.get((cluster_id, i), ()), design, families,
                                     background)
                      for i in range(len(alternatives))])
    seeds = [_derive_seed(design, cluster_id, question_id, i, base_seed)
            for i in range(len(alternatives))]
    return _mi_from_rates(rates, posteriors, classes, mi_n_draws, seeds)


def _background_rate(design: Design, families: Mapping[str, PhenomenonFamily]) -> np.ndarray:
    reference_ncount = float(families["curve"].cfg("reference_ncount"))
    if not (math.isfinite(reference_ncount) and reference_ncount > 0.0):
        raise ValueError("curve family declares no usable reference_ncount for background scaling")
    per_point = _background_counts_per_point() * (float(design.ncount) / reference_ncount)
    return np.full(design.n_points, per_point, dtype=float)


def _node_covered(scene_payload: Mapping, partition_id, axis_coordinate: float,
                  tol: float = 1e-9) -> bool:
    for fp in scene_payload.get("footprints") or ():
        if fp.get("partition_id") != partition_id:
            continue
        coord = fp.get("axis_coordinate")
        if coord is not None and abs(float(coord) - float(axis_coordinate)) <= tol:
            return True
    return False


def _discover_region_mi(spec: QuestionSpec, design: Design, families: Mapping[str, PhenomenonFamily],
                        background: np.ndarray, mi_n_draws: int, base_seed: int,
                        paranoia: float) -> float:
    unknown_belief = PhenomenonBelief(cluster_id="__discover_region__", alternative_index=0,
                                      family="unknown", anchors=(), sample=SampleKnowledge())
    pred = predict(unknown_belief, design, families, seed=0)
    present_rate = background + np.asarray(pred.expected_counts, dtype=float)
    rates = np.array([present_rate, background])
    posteriors = np.array([paranoia, 1.0 - paranoia])
    classes = ["present", "absent"]
    seeds = [_derive_seed(design, "__discover_region__", spec.question_id, i, base_seed)
            for i in (0, 1)]
    return _mi_from_rates(rates, posteriors, classes, mi_n_draws, seeds)


def _question_mi(spec: QuestionSpec, scene_payload: Mapping,
                 beliefs_by_ca: Mapping[tuple, Sequence[PhenomenonBelief]], design: Design,
                 families: Mapping[str, PhenomenonFamily], background: np.ndarray,
                 mi_n_draws: int, base_seed: int, quantum: float,
                 paranoia: float) -> tuple[float, int]:
    """``(mi_nats, contributing_cluster_count)`` for one open question against one design."""
    if spec.family == "VerifyCompleteness":
        # Declared v1 posture (docs/plans/scene5-eig.md 5.1): exposure fill is deterministic
        # progress, never scene-EIG's to price.
        return 0.0, 0
    if spec.family == "DiscoverRegion":
        if paranoia <= 0.0:
            return 0.0, 0                       # degenerate partition short-circuit — MI exactly 0
        if _node_covered(scene_payload, spec.subject.partition_id, design.axis_coordinate):
            return 0.0, 0
        return _discover_region_mi(spec, design, families, background, mi_n_draws, base_seed,
                                   paranoia), 1
    if spec.family == "EstimateParameter":
        cluster_classes = _estimate_parameter_classes(spec, scene_payload, quantum)
    else:
        extractor = _FAMILY_EXTRACTORS.get(spec.family)
        if extractor is None:
            raise ValueError(f"no relevance extractor for question family {spec.family!r}")
        cluster_classes = extractor(spec, scene_payload)

    total_mi, contributing = 0.0, 0
    for idx, class_of in cluster_classes:
        cluster_mi = 0.0
        has_variance = len(set(class_of.values())) >= 2
        if has_variance:
            cluster_mi += _cluster_mi(scene_payload, idx, class_of, beliefs_by_ca, design,
                                      families, background, mi_n_draws, base_seed,
                                      spec.question_id)
        if spec.family == "ConfirmPresence":
            # SCENE-5 5.3 (Ruling A): the declared thermal_consistency component ADDS to this
            # question's cluster MI sum, folded into mi_nats -- no new QuestionTerm field. It
            # runs independent of ``has_variance``: a cluster where every alternative already
            # agrees on presence can still carry corroboration doubt.
            cluster_mi += _thermal_consistency_mi(
                scene_payload, idx, beliefs_by_ca, design, families, background, mi_n_draws,
                base_seed, spec.question_id, paranoia)
        if cluster_mi <= 0.0:
            continue
        contributing += 1
        total_mi += cluster_mi
    return total_mi, contributing


# ----------------------------------------------------------------------------
# Output: the frozen score table
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QuestionTerm:
    """One open question's itemized contribution to one design's score."""

    question_id: str
    family: str
    priority: int
    weight: float
    mi_nats: float
    contributing_clusters: int


@dataclass(frozen=True, slots=True)
class ScoreRow:
    """One candidate design, its priority-weighted total score, and every question that fed it."""

    design: Design
    design_key: str
    total: float
    terms: tuple[QuestionTerm, ...]


@dataclass(frozen=True, slots=True)
class ScoreTable:
    """The full ranked result of one :func:`score` call — rows sorted by ``total`` descending,
    ties broken by :func:`design_key` ascending, so replay order is never a set/dict iteration
    artifact."""

    rows: tuple[ScoreRow, ...]
    relevance_manifest: tuple[RelevanceExtractor, ...]
    relevance_fingerprint: str
    family_registry_fingerprint: str
    resolved_policy_fingerprint: str
    base_seed: int


def _sample_knowledge_from_payload(scene_payload: Mapping) -> SampleKnowledge:
    sk = scene_payload.get("sample_knowledge") or {}
    lattice = sk.get("lattice")
    return SampleKnowledge(
        zone_period=sk.get("zone_period"),
        reflections=tuple(sk.get("reflections") or ()),
        dispersion_slope_max=sk.get("dispersion_slope_max"),
        lattice_epoch=sk.get("lattice_epoch"),
        lattice=None if lattice is None else tuple(lattice))
    # ``degradations`` (a builder-added key on this block, isar/scene/build.py) is deliberately
    # not read: it is a record of what the SCENE found inert, not a SampleKnowledge field.


def score(scene_payload: Mapping, agenda_payload: Mapping, hypotheses: Mapping[str, dict] | None,
         designs: Sequence[Design], base_seed: int,
         families: Mapping[str, PhenomenonFamily] | None = None) -> ScoreTable:
    """Score every design in ``designs`` against every OPEN question in ``agenda_payload``.

    Refuses (``ValueError``) on a ``resolved_policy`` block that fails
    :func:`isar.schema.question_policy.validate_resolved_block`, or on two designs sharing one
    :func:`design_key`. Does NOT check scene/agenda digest agreement — that is 5.2's concern, the
    caller's to enforce before handing payloads here (module docstring).

    Every OTHER failure in belief construction or estimation raises :class:`SceneScoringError`
    (D7) rather than the original exception — the planner's ONE typed fallback boundary, never
    these two input-validation refusals above.
    """
    policy = agenda_payload.get("resolved_policy")
    validate_resolved_block(policy, "agenda_payload.resolved_policy")
    thresholds = policy["thresholds"]
    mi_n_draws = int(thresholds["mi_n_draws"])
    paranoia = float(thresholds["paranoia_unknown_prior"])
    quantum = float(thresholds["emergent_omega_tile_meV"])
    priority_weights = policy["priority_weights"]

    families = dict(families) if families else dict(DEFAULT_PHENOMENON_FAMILIES)

    designs = list(designs)
    keys = [design_key(d) for d in designs]
    seen: set[str] = set()
    dupes = {k for k in keys if k in seen or seen.add(k)}
    if dupes:
        raise ValueError(f"duplicate design keys in designs list: {sorted(dupes)}")

    try:
        sample = _sample_knowledge_from_payload(scene_payload)
        beliefs = build_beliefs(scene_payload, sample, hypotheses)
    except Exception as error:
        raise SceneScoringError("scene_belief_construction_failed", str(error)) from error

    try:
        beliefs_by_ca: dict[tuple, list[PhenomenonBelief]] = {}
        for belief in beliefs:
            beliefs_by_ca.setdefault((belief.cluster_id, belief.alternative_index),
                                     []).append(belief)

        specs = {}
        for row in agenda_payload.get("specs") or ():
            spec = QuestionSpec.from_dict(row)
            specs[spec.question_id] = spec
        open_ids = {a["question_id"] for a in agenda_payload.get("assessments") or ()
                   if a.get("status") == "open"}
        open_specs = sorted((s for qid, s in specs.items() if qid in open_ids),
                            key=lambda s: s.question_id)

        rows = []
        for design, dkey in zip(designs, keys):
            background = _background_rate(design, families)
            terms = []
            total = 0.0
            for spec in open_specs:
                mi, contributing = _question_mi(spec, scene_payload, beliefs_by_ca, design,
                                                families, background, mi_n_draws, base_seed,
                                                quantum, paranoia)
                weight = float(priority_weights[str(spec.priority)])
                total += weight * mi
                terms.append(QuestionTerm(question_id=spec.question_id, family=spec.family,
                                          priority=spec.priority, weight=weight, mi_nats=mi,
                                          contributing_clusters=contributing))
            rows.append(ScoreRow(design=design, design_key=dkey, total=total, terms=tuple(terms)))
        rows.sort(key=lambda r: (-r.total, r.design_key))

        return ScoreTable(
            rows=tuple(rows),
            relevance_manifest=relevance_manifest(),
            relevance_fingerprint=relevance_fingerprint(),
            family_registry_fingerprint=phenomenon_registry_fingerprint(families),
            resolved_policy_fingerprint=fingerprint(policy),
            base_seed=base_seed)
    except Exception as error:
        raise SceneScoringError("scoring_exception", str(error)) from error
