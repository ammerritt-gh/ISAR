"""The phenomenon belief layer (SCENE-5 slice 5.0) — what the scene believes exists at
unmeasured (q, omega), and the seeded predictive counts that belief implies for a candidate
design.

**Decide-side only (D2).** Nothing in this module enters ``ln W(S)``, local evidence, or any
``isar.scene.factors`` predicate — it is read exclusively by scene-EIG scoring (SCENE-5 slice
5.1 onward). It is not imported by ``isar.scene.build`` or ``isar.scene.factors``, and it
extends no persisted contract: it consumes ``isar.scene-revision/1`` payloads and
:class:`isar.scene.model.SampleKnowledge` as plain data, and writes nothing back.

**Amplitude-blind by construction (D1).** ``isar.scene.model.CandidateRow`` persists no
amplitude and no linewidth (Gamma) — the scene layer never saw them and cannot invent them from
a fit. So the count scale and default linewidth this module predicts with come ONLY from the
declared, fingerprinted priors in the :data:`PhenomenonFamily` registry below — never from a
fitted value. Changing a family's declared prior is the ONLY way to move a prediction's count
scale; nothing else in this module may.

**Model-family registry (D1).** Mirrors ``isar.scene.factors``'s ``SceneFactor`` idiom: a
family is a name/version/config declaration, digested into a ``registry_fingerprint`` so a
belief scored under one registry can never be silently replayed under a retuned one. ``curve``
covers both branch and level phenomena (a level is a curve under a tight zero-slope prior — the
registry does not special-case it in v1). ``continuum`` is declared and REFUSING (O10): asking
it to predict raises :class:`ContinuumRefusesPrediction` rather than silently returning zero.

**Zone geometry is structural, not a guess (D3).** Folds sit at multiples of
``SampleKnowledge.zone_period``, anchored at the crystal origin q=0 (the O44 rule) — never at a
measured or tracker-selected q. A curve's prediction folds into that geometry: approaching a
fold with no declared gapless anchor there, the predicted energy REFLECTS (fold symmetry) and
never descends toward zero; a declared gapless hypothesis (the ``acoustic`` preset: zone-centre
energy pinned to 0) is realized here as a synthetic zero-energy anchor, so the same
interpolation machinery that reflects an undeclared fold predicts a genuine descent at a
declared one — no separate code path, no second physics.

**A gapless anchor is planted ONLY at a fold that coincides with a DECLARED REFLECTION.** Fold
geometry and the reflection list are independent declarations, and only their coincidence marks
a zone centre: FCC along [h00] folds at every integer, but the acoustic branch reaches zero
only at the even (declared-reflection) folds — (300) is integer-indexed, on a fold, and
extinct, and planting a zero there would predict exactly the phantom boundary descent this
layer exists to forbid. A boundary fold only mirrors; a declared gapless hypothesis with NO
declared reflections plants nothing and records
``no_declared_reflections_for_gapless_anchor``. The mirror machinery then generates the far
zone for free: with a zero at the Γ fold below the span and reflection at the boundary fold
above it, a query past the boundary folds back into the anchored span and approaches zero
toward the NEXT zone centre with no anchor ever planted there. Undeclared zone geometry or
dispersion slope leaves the matching constraint inert and records a named reason on the
belief/prediction, falling toward a wider (never narrower) predictive rather than guessing.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from .model import (LINK_TOL_FLOOR, NON_MODE_ID_TAGS, SampleKnowledge, bose_weight, fingerprint)

# ----------------------------------------------------------------------------
# Model-family registry
# ----------------------------------------------------------------------------
FAMILY_KINDS = ("curve", "elastic", "quasielastic", "unknown", "continuum")


@dataclass(frozen=True, slots=True)
class PhenomenonFamily:
    """One declared model family: every physical assumption its predictive makes.

    ``config`` is every tunable constant the family closes over, as sorted ``(name, value)``
    pairs — the same discipline as ``isar.scene.factors.SceneFactor.config`` and for the same
    reason: a retuned constant is a different family, and without this a revision scored under
    one tuning could not be told apart from one scored under another.
    """

    name: str
    version: str
    config: tuple[tuple[str, float], ...] = ()

    def cfg(self, key: str, default=None):
        for k, v in self.config:
            if k == key:
                return v
        return default


def validate_family(fam: PhenomenonFamily) -> None:
    """Raise ``ValueError`` unless ``fam`` is a well-formed declaration."""
    if not isinstance(fam, PhenomenonFamily):
        raise ValueError(f"not a PhenomenonFamily: {fam!r}")
    if fam.name not in FAMILY_KINDS:
        raise ValueError(f"phenomenon family declares name {fam.name!r}; allowed: {FAMILY_KINDS}")
    if not fam.version:
        raise ValueError(f"phenomenon family {fam.name!r} declares no version")
    for pair in fam.config:
        if len(pair) != 2 or not isinstance(pair[0], str):
            raise ValueError(f"phenomenon family {fam.name!r} has a malformed config entry "
                             f"{pair!r}: expected (name, value) pairs")
    if [k for k, _ in fam.config] != sorted(k for k, _ in fam.config):
        raise ValueError(f"phenomenon family {fam.name!r} has unsorted config keys: "
                         f"{[k for k, _ in fam.config]}")


def family_manifest(families) -> list[dict]:
    """The full declaration of every family in ``families``, in name order — what a caller
    embeds and what :func:`registry_fingerprint` digests. Order-free: two callers assembling the
    same family set in different orders (dict insertion order, iterable order) stamp the same
    fingerprint, because the manifest is sorted before it is hashed.
    """
    values = families.values() if isinstance(families, dict) else families
    return sorted(({"name": f.name, "version": f.version, "config": [[k, v] for k, v in f.config]}
                   for f in values),
                  key=lambda m: m["name"])


def registry_fingerprint(families) -> str:
    """Fingerprint of ``family_manifest(families)``. Changes iff any declared value changes."""
    return fingerprint(family_manifest(families))


class ContinuumRefusesPrediction(NotImplementedError):
    """``continuum`` is declared but REFUSING (O10): it has no predictive model yet.

    A caller that reaches this needs a real continuum model, not a silent zero prediction — a
    silent zero would make the EIG layer treat "we have no model" identically to "we predict no
    counts there", which are very different epistemic states.
    """


# Declared v1 priors, owner-tunable — every one lives in a family's ``config`` so a retuning
# moves ``registry_fingerprint`` and a belief scored under it cannot be replayed against a
# different tuning. See the module docstring: these are the ONLY source of predicted count
# scale and default linewidth, because the scene layer never sees a fitted amplitude or Gamma.
CURVE = PhenomenonFamily(
    name="curve", version="1",
    config=(("default_gamma_mev", 0.3),
            ("diffuse_sigma_growth_per_rlu", 5.0),
            ("reference_counts", 100.0),
            ("reference_ncount", 1_000_000.0)))
ELASTIC = PhenomenonFamily(
    name="elastic", version="1",
    config=(("default_width_mev", 0.2),
            ("reference_counts", 500.0),
            ("reference_ncount", 1_000_000.0)))
QUASIELASTIC = PhenomenonFamily(
    name="quasielastic", version="1",
    config=(("default_width_mev", 1.5),
            ("reference_counts", 150.0),
            ("reference_ncount", 1_000_000.0)))
UNKNOWN = PhenomenonFamily(
    name="unknown", version="1",
    # The universal floor: maximally diffuse over the whole declared intrinsic domain, with a
    # deliberately small count scale — "we have no idea" must never outbid a declared phenomenon.
    config=(("domain_hi_mev", 50.0), ("domain_lo_mev", -50.0),
            ("reference_counts", 5.0), ("reference_ncount", 1_000_000.0)))
CONTINUUM = PhenomenonFamily(name="continuum", version="1", config=())

FAMILIES: dict[str, PhenomenonFamily] = {f.name: f for f in
                                         (CURVE, ELASTIC, QUASIELASTIC, UNKNOWN, CONTINUUM)}


# ----------------------------------------------------------------------------
# Belief construction
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Anchor:
    """One measured (axis coordinate, energy) point a belief is built from."""

    axis_coordinate: float
    omega: float
    omega_sigma: float


@dataclass(frozen=True, slots=True)
class PhenomenonBelief:
    """One phenomenon instance for one (cluster, alternative) — the thing a design is scored
    against. ``anchors`` is sorted by ``axis_coordinate`` at construction (never trusted from the
    caller's order): every predictive walk below assumes ascending order.

    ``sample`` travels WITH the belief rather than being threaded through every call, because
    the fold/reachability geometry is a property of what this belief was built to respect, not
    of the query — two beliefs from two different revisions could legitimately carry different
    declared sample knowledge if the card changed between them.
    """

    cluster_id: str
    alternative_index: int
    family: str
    anchors: tuple[Anchor, ...]
    sample: SampleKnowledge
    degradations: tuple[str, ...] = ()

    def __post_init__(self):
        if self.family not in FAMILY_KINDS:
            raise ValueError(f"phenomenon belief declares family {self.family!r}; "
                             f"allowed: {FAMILY_KINDS}")
        if self.family in ("curve", "elastic", "quasielastic") and not self.anchors:
            raise ValueError(f"a {self.family!r} belief needs at least one anchor")
        object.__setattr__(self, "anchors",
                           tuple(sorted(self.anchors, key=lambda a: a.axis_coordinate)))


def _component_id(scan_ref: str, label: str, kind: str, slot: int | None) -> str:
    """The same ``"<scan_ref>#<candidate_label>#<...>"`` grammar
    ``isar.scene.model.ComponentObservation`` documents — reproduced here (never imported: that
    type is builder-internal) so a link pair in the persisted ``alternatives[].links`` resolves
    to the same id this module derives from ``candidates[].components``."""
    if kind == "mode":
        return f"{scan_ref}#{label}#{slot}"
    return f"{scan_ref}#{label}#{NON_MODE_ID_TAGS[kind]}"


def _resolution_floor(obs: dict) -> float:
    """The arena's resolution floor: its Gaussian FWHM, or ``LINK_TOL_FLOOR`` — the same floor
    ``isar.scene.model.link_tol`` applies, reused rather than re-derived."""
    return max(float(obs.get("gauss_fwhm") or 0.0), LINK_TOL_FLOOR)


def _interval_sigma(interval: dict | None, floor: float) -> float:
    """Half the persisted ``ModeInterval`` width, or the resolution floor when none was
    persisted for this slot (the ``mode_intervals`` population rule — see
    ``isar.scene.model.CandidateRow``)."""
    if interval is None:
        return floor
    return max(0.5 * (float(interval["high"]) - float(interval["low"])), floor)


def _resolve_hypothesis(identities: set, hypotheses: Mapping) -> dict | None:
    """The declared ``[hypotheses.*]`` entry this group's claimed identity resolves to, or
    ``None`` — precedence rule 2 of the plan: claimed identity, then a matching declaration, else
    the agnostic floor. Two DIFFERENT claimed identities in one linked group is not a case this
    slice resolves confidently, so it falls to the floor rather than picking one arbitrarily."""
    named = {i for i in identities if i}
    if len(named) == 1:
        return hypotheses.get(next(iter(named)))
    return None


def _is_gapless(hyp: dict | None) -> bool:
    if hyp is None:
        return False
    if hyp.get("preset") == "acoustic":
        return True
    zc = hyp.get("zc_energy")
    if not zc:
        return False
    center = zc.get("mode", zc.get("mean"))
    return center is not None and abs(float(center)) < 1e-9


def _zc_sigma(hyp: dict | None, default: float = 0.5) -> float:
    zc = (hyp or {}).get("zc_energy") or {}
    return float(zc.get("sigma", default))


def build_beliefs(payload: Mapping, sample: SampleKnowledge,
                  hypotheses: Mapping[str, dict] | None = None) -> tuple[PhenomenonBelief, ...]:
    """Construct one :class:`PhenomenonBelief` per curve chain, and one per elastic/quasielastic
    component, for every alternative of every cluster in ``payload`` (schema
    ``isar.scene-revision/1``, read as plain dicts). Offline and pure — no card reading, no I/O.

    A curve chain is one connected component of an alternative's ``links`` graph over its MODE
    components (union-find), including singletons — matching D1's "a level is a curve under a
    tight zero-slope prior": a lone mode with no linked neighbour is still a curve family
    instance, just with one anchor. Component semantics come ONLY from the persisted
    ``ComponentDescriptor.kind`` on the chosen candidate row (rule 2) — never from label parsing.
    """
    hypotheses = hypotheses or {}
    obs_by_ref = {o["scan_ref"]: o for o in payload.get("observations", ())}
    fp_by_ref = {fp["scan_ref"]: fp for fp in payload.get("footprints", ())}

    beliefs: list[PhenomenonBelief] = []
    for cluster in payload.get("clusters", ()):
        cluster_id = cluster["cluster_id"]
        for alt_idx, alt in enumerate(cluster.get("alternatives", ())):
            beliefs.extend(_beliefs_for_alternative(cluster_id, alt_idx, alt, obs_by_ref,
                                                     fp_by_ref, sample, hypotheses))
    return tuple(beliefs)


def _gather_components(assignment: dict, obs_by_ref: dict, fp_by_ref: dict) -> list[dict]:
    comps = []
    for ref, label in assignment.items():
        obs = obs_by_ref.get(ref)
        if obs is None:
            # Refusal, not a skip (H1): the revision embeds every scoring arena's observation,
            # so an assignment naming an arena the payload does not carry is a corrupt or
            # foreign payload — silently dropping it would build a belief missing anchors and
            # score designs against the wrong track.
            raise ValueError(f"alternative assignment names arena {ref!r} but the payload "
                             "embeds no such observation — corrupt or foreign revision payload")
        fp = fp_by_ref.get(ref)
        axis_coordinate = None if fp is None else fp.get("axis_coordinate")
        row = next((c for c in obs.get("candidates", ()) if c["label"] == label), None)
        if row is None:
            raise ValueError(f"alternative assignment chooses candidate {label!r} at arena "
                             f"{ref!r} but that observation carries no such row — corrupt or "
                             "foreign revision payload")
        floor = _resolution_floor(obs)
        omegas = row.get("omegas") or ()
        intervals = row.get("mode_intervals") or ()
        for desc in row.get("components", ()):
            kind, slot = desc["kind"], desc.get("slot")
            if kind == "mode":
                if slot is None or slot >= len(omegas) or omegas[slot] is None:
                    continue
                omega = float(omegas[slot])
                sigma = _interval_sigma(intervals[slot] if slot < len(intervals) else None, floor)
            else:
                omega, sigma = 0.0, floor
            comps.append({"id": _component_id(ref, label, kind, slot), "scan_ref": ref,
                          "kind": kind, "axis_coordinate": axis_coordinate, "omega": omega,
                          "omega_sigma": sigma, "identity": row.get("identity")})
    return comps


def _union_find(ids, links) -> dict:
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    return {i: find(i) for i in ids}


def _beliefs_for_alternative(cluster_id: str, alt_idx: int, alt: dict, obs_by_ref: dict,
                             fp_by_ref: dict, sample: SampleKnowledge,
                             hypotheses: Mapping) -> list[PhenomenonBelief]:
    assignment = {ref: label for ref, label in alt.get("assignment", ())}
    links = [tuple(pair) for pair in alt.get("links", ())]
    comps = _gather_components(assignment, obs_by_ref, fp_by_ref)

    mode_comps = [c for c in comps if c["kind"] == "mode" and c["axis_coordinate"] is not None]
    roots = _union_find([c["id"] for c in mode_comps], links)
    groups: dict[str, list[dict]] = {}
    for c in mode_comps:
        groups.setdefault(roots[c["id"]], []).append(c)

    out: list[PhenomenonBelief] = []
    for group in groups.values():
        anchors = tuple(Anchor(c["axis_coordinate"], c["omega"], c["omega_sigma"])
                        for c in group)
        hyp = _resolve_hypothesis({c["identity"] for c in group}, hypotheses)
        degradations = []
        if sample.zone_period is None:
            degradations.append("no_declared_zone_period")
        if sample.dispersion_slope_max is None:
            degradations.append("no_declared_dispersion_slope_max")
        if _is_gapless(hyp) and sample.zone_period is not None:
            # A zero-energy anchor may be planted ONLY at a fold that coincides with a DECLARED
            # reflection (module docstring: fold != reflection — FCC (300) sits on a fold and is
            # extinct, and a zero planted there is the forbidden phantom boundary descent). Only
            # the NEAREST fold beyond each span edge is considered: past an intervening
            # non-reflection fold the mirror machinery already generates the far zone, and a
            # straight-line anchor across it would cut through the boundary maximum.
            if not sample.reflections:
                degradations.append("no_declared_reflections_for_gapless_anchor")
            else:
                sigma = _zc_sigma(hyp)
                sorted_anchors = sorted(anchors, key=lambda a: a.axis_coordinate)
                lo, hi = sorted_anchors[0].axis_coordinate, sorted_anchors[-1].axis_coordinate
                extra = []
                for edge, direction in ((lo, -1), (hi, 1)):
                    fold = _nearest_fold_beyond(edge, direction, sample.zone_period)
                    if any(abs(fold - r) <= FOLD_REFLECTION_TOL for r in sample.reflections):
                        extra.append(Anchor(fold, 0.0, sigma))
                anchors = anchors + tuple(extra)
        out.append(PhenomenonBelief(cluster_id, alt_idx, "curve", anchors, sample,
                                    tuple(degradations)))

    for c in comps:
        if c["kind"] == "mode" or c["axis_coordinate"] is None:
            continue
        anchors = (Anchor(c["axis_coordinate"], c["omega"], c["omega_sigma"]),)
        out.append(PhenomenonBelief(cluster_id, alt_idx, c["kind"], anchors, sample, ()))
    return out


# ----------------------------------------------------------------------------
# Predictive geometry — zone folding, interpolation, the slope cone
# ----------------------------------------------------------------------------
# How close a fold and a declared reflection must sit (rlu) to count as the SAME position — an
# order of magnitude under PATH_COLLINEAR_TOL's q-positioning scale, because both quantities are
# declarations (card rlu), not measurements, so only representation noise separates them.
FOLD_REFLECTION_TOL = 1e-6


def _nearest_fold_beyond(edge: float, direction: int, period: float, *,
                         strict: bool = True) -> float:
    """The first fold (multiple of ``period``, anchored at q=0 — the O44 rule) beyond ``edge``
    in ``direction`` (+1 / -1). ``strict=False`` admits a fold sitting exactly AT ``edge`` —
    the mirror walk needs that (a gapless anchor planted at a Γ fold makes the span edge a fold,
    and a query just past it must reflect there), while anchor PLANTING must not (a fold at the
    edge is already anchored)."""
    tol = 1e-9 if strict else -1e-9
    n = edge / period
    if direction > 0:
        fold = math.floor(n + 1e-9) * period
        while fold <= edge + tol:
            fold += period
    else:
        fold = math.ceil(n - 1e-9) * period
        while fold >= edge - tol:
            fold -= period
    return fold


def _fold_to_span(q: float, lo: float, hi: float, period: float, max_iter: int = 8) -> float:
    """Map ``q`` into ``[lo, hi]`` by reflecting across every fold it crosses (D3's fold
    symmetry), or leave it beyond the span unmoved if it has not yet crossed one — the plain
    extrapolation case. Bounded rather than exact for an arbitrarily distant query: a design this
    far outside the measured span is already a different question than this slice answers.
    """
    for _ in range(max_iter):
        if lo <= q <= hi:
            return q
        if q > hi:
            fold = _nearest_fold_beyond(hi, 1, period, strict=False)
            if q <= fold + 1e-9:
                return q
            q = 2 * fold - q
        else:
            fold = _nearest_fold_beyond(lo, -1, period, strict=False)
            if q >= fold - 1e-9:
                return q
            q = 2 * fold - q
    return q


def _interp_linear(anchors: tuple[Anchor, ...], q: float) -> float:
    if len(anchors) == 1:
        return anchors[0].omega
    if q <= anchors[0].axis_coordinate:
        return anchors[0].omega
    if q >= anchors[-1].axis_coordinate:
        return anchors[-1].omega
    for a, b in zip(anchors, anchors[1:]):
        if a.axis_coordinate <= q <= b.axis_coordinate:
            span = b.axis_coordinate - a.axis_coordinate
            t = 0.0 if span == 0.0 else (q - a.axis_coordinate) / span
            return a.omega + t * (b.omega - a.omega)
    return anchors[-1].omega  # unreachable for a sorted, bracketed q


def _interp_sigma(anchors: tuple[Anchor, ...], q: float) -> float:
    if len(anchors) == 1:
        return anchors[0].omega_sigma
    if q <= anchors[0].axis_coordinate:
        return anchors[0].omega_sigma
    if q >= anchors[-1].axis_coordinate:
        return anchors[-1].omega_sigma
    for a, b in zip(anchors, anchors[1:]):
        if a.axis_coordinate <= q <= b.axis_coordinate:
            span = b.axis_coordinate - a.axis_coordinate
            t = 0.0 if span == 0.0 else (q - a.axis_coordinate) / span
            return a.omega_sigma + t * (b.omega_sigma - a.omega_sigma)
    return anchors[-1].omega_sigma


def _extrapolate_linear(anchors: tuple[Anchor, ...], q: float) -> float:
    if len(anchors) < 2:
        return anchors[0].omega
    if q > anchors[-1].axis_coordinate:
        a, b = anchors[-2], anchors[-1]
    else:
        a, b = anchors[1], anchors[0]
    span = b.axis_coordinate - a.axis_coordinate
    slope = 0.0 if span == 0.0 else (b.omega - a.omega) / span
    edge = anchors[-1] if q > anchors[-1].axis_coordinate else anchors[0]
    return edge.omega + slope * (q - edge.axis_coordinate)


def predicted_omega(belief: PhenomenonBelief, axis_coordinate: float, *,
                    _degrade: list | None = None) -> float:
    """The curve family's predicted energy at ``axis_coordinate``, honoring D3's fold symmetry.

    Interpolates between anchors inside the anchored span. Beyond it: reflected into the span
    across every declared fold it crosses (never descending toward zero at a fold unless a
    declared gapless hypothesis planted a zero-energy anchor there at belief-construction time —
    see :func:`build_beliefs`), or a plain local-slope extrapolation with no fold available,
    which records ``no_declared_zone_period`` on the caller's degradation list.
    """
    if belief.family != "curve":
        raise ValueError(f"predicted_omega is only defined for the curve family, got "
                         f"{belief.family!r}")
    degrade = _degrade if _degrade is not None else []
    anchors = belief.anchors
    lo, hi = anchors[0].axis_coordinate, anchors[-1].axis_coordinate
    q = float(axis_coordinate)
    if belief.sample.zone_period is not None:
        q_eff = _fold_to_span(q, lo, hi, belief.sample.zone_period)
    else:
        q_eff = q
        if q < lo or q > hi:
            degrade.append("no_declared_zone_period")
    # Intrinsic energy is |omega|: an extrapolation that crosses zero (a descending track walked
    # past its zero anchor with no fold to mirror it) is floored, never a negative prediction.
    if lo <= q_eff <= hi:
        return max(0.0, _interp_linear(anchors, q_eff))
    return max(0.0, _extrapolate_linear(anchors, q_eff))


def predicted_sigma(belief: PhenomenonBelief, axis_coordinate: float,
                    families: Mapping[str, PhenomenonFamily] | None = None, *,
                    _degrade: list | None = None) -> float:
    """The curve family's predicted energy UNCERTAINTY at ``axis_coordinate``.

    Inside the anchored span: interpolated anchor uncertainty alone. Beyond it: that PLUS a
    slope-cone term growing with the distance past the span — ``dispersion_slope_max × distance``
    where the sample declares a slope, else a declared diffuse-growth constant (the family's
    ``diffuse_sigma_growth_per_rlu``) and a recorded ``no_declared_dispersion_slope_max``, so an
    undeclared slope makes the predictive WIDER, never narrower, than a declared one would at the
    same distance — this is what makes width strictly greater at an unmeasured node than at an
    anchored one, in every case (SCENE-5 5.0 acceptance: predictive width is pinned).
    """
    if belief.family != "curve":
        raise ValueError(f"predicted_sigma is only defined for the curve family, got "
                         f"{belief.family!r}")
    families = families or FAMILIES
    degrade = _degrade if _degrade is not None else []
    anchors = belief.anchors
    lo, hi = anchors[0].axis_coordinate, anchors[-1].axis_coordinate
    q = float(axis_coordinate)
    clamped = min(max(q, lo), hi)
    base = _interp_sigma(anchors, clamped)
    if lo <= q <= hi:
        return base
    beyond = (lo - q) if q < lo else (q - hi)
    slope = belief.sample.dispersion_slope_max
    if slope is not None:
        return base + float(slope) * beyond
    degrade.append("no_declared_dispersion_slope_max")
    growth = families["curve"].cfg("diffuse_sigma_growth_per_rlu", 5.0)
    return base + float(growth) * beyond


# ----------------------------------------------------------------------------
# Prediction: expected counts and seeded draws
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Design:
    """One candidate placement to predict against: an axis coordinate, a signed energy window
    ``(lo, hi)`` in meV, how many grid points to evaluate, the proposed neutron-count exposure,
    and the sample temperature (``None`` when undeclared — a Bose-blind design)."""

    axis_coordinate: float
    window: tuple[float, float]
    n_points: int
    ncount: float
    temperature_K: float | None = None

    def __post_init__(self):
        if self.n_points < 1:
            raise ValueError(f"Design.n_points must be >= 1, got {self.n_points!r}")
        if self.window[1] < self.window[0]:
            raise ValueError(f"Design.window must be (lo, hi) with lo <= hi, got {self.window!r}")
        if self.ncount < 0.0:
            raise ValueError(f"Design.ncount must be >= 0, got {self.ncount!r}")


@dataclass(frozen=True, slots=True)
class Prediction:
    """The seeded predictive: the evaluated grid, the expected Poisson rate at each point, the
    drawn counts, and why anything on the belief's/prediction's declared inputs was inert."""

    omega_grid: tuple[float, ...]
    expected_counts: tuple[float, ...]
    draws: tuple[int, ...]
    degradations: tuple[str, ...]


def _reference_amplitude(fam: PhenomenonFamily, ncount: float) -> float:
    """Expected peak counts at ``ncount`` exposure, scaled linearly off the family's declared
    reference — the ONLY route to a count scale (see the module docstring: the scene is
    amplitude-blind, so this is a prior, never a fitted value)."""
    ref_counts, ref_ncount = fam.cfg("reference_counts"), fam.cfg("reference_ncount")
    if ref_counts is None or ref_ncount is None or float(ref_ncount) <= 0.0:
        raise ValueError(f"family {fam.name!r} declares no usable reference_counts/"
                         "reference_ncount prior")
    return float(ref_counts) * (float(ncount) / float(ref_ncount))


def _gaussian(grid: np.ndarray, mu: float, sigma: float) -> np.ndarray:
    sigma = max(float(sigma), 1e-6)
    return np.exp(-0.5 * ((grid - mu) / sigma) ** 2)


def predict(belief: PhenomenonBelief, design: Design,
           families: Mapping[str, PhenomenonFamily] | None = None, *,
           seed: int) -> Prediction:
    """The seeded predictive Poisson draw for ``belief`` over ``design``'s energy grid.

    Deterministic in ``seed`` alone, for fixed ``belief``/``design``/``families``: the same
    triple with the same seed reproduces byte-identical draws, and a different seed reliably
    differs (this is what lets scene-EIG replay a decision).
    """
    families = families or FAMILIES
    if belief.family == "continuum":
        raise ContinuumRefusesPrediction(
            f"family 'continuum' is declared but refuses prediction (O10): belief for cluster "
            f"{belief.cluster_id!r} alternative {belief.alternative_index}")
    fam = families.get(belief.family)
    if fam is None:
        raise ValueError(f"no registry entry for family {belief.family!r}")
    validate_family(fam)

    lo, hi = design.window
    grid = np.linspace(lo, hi, design.n_points)
    degrade = list(belief.degradations)

    if belief.family == "curve":
        center = predicted_omega(belief, design.axis_coordinate, _degrade=degrade)
        sigma_pos = predicted_sigma(belief, design.axis_coordinate, families, _degrade=degrade)
        # The peak's spectral width and the position uncertainty are DIFFERENT quantities: the
        # declared linewidth prior (``default_gamma_mev`` — the scene never sees a fitted Γ) says
        # how wide the phenomenon is, ``sigma_pos`` says how well we know where it sits. The
        # predictive smears over both, in quadrature — leaving the declared prior out made it
        # dead config whose retuning moved the registry fingerprint and changed nothing.
        sigma = math.hypot(sigma_pos, float(fam.cfg("default_gamma_mev", 0.0)))
        amplitude = _reference_amplitude(fam, design.ncount)
        lam = amplitude * _gaussian(grid, center, sigma)
        if design.temperature_K is not None:
            weight = bose_weight(center, design.temperature_K)
            lam = lam + amplitude * weight * _gaussian(grid, -center, sigma)
        else:
            degrade.append("no_declared_temperature_for_bose")
    elif belief.family in ("elastic", "quasielastic"):
        # An elastic/quasielastic belief always carries its arena anchor (__post_init__ refuses
        # these families anchorless), whose sigma is the resolution floor; the declared family
        # width prior rides on top in quadrature, exactly as for the curve family.
        sigma = math.hypot(belief.anchors[0].omega_sigma,
                           float(fam.cfg("default_width_mev", 0.0)))
        amplitude = _reference_amplitude(fam, design.ncount)
        lam = amplitude * _gaussian(grid, 0.0, sigma)
    elif belief.family == "unknown":
        lo_dom, hi_dom = fam.cfg("domain_lo_mev"), fam.cfg("domain_hi_mev")
        amplitude = _reference_amplitude(fam, design.ncount)
        width = max(float(hi_dom) - float(lo_dom), 1e-9)
        lam = np.where((grid >= lo_dom) & (grid <= hi_dom), amplitude / width, 0.0)
    else:
        raise ValueError(f"no predictive model for family {belief.family!r}")

    rng = np.random.default_rng(seed)
    draws = rng.poisson(np.clip(lam, 0.0, None))
    return Prediction(tuple(float(x) for x in grid), tuple(float(x) for x in lam),
                      tuple(int(x) for x in draws), tuple(dict.fromkeys(degrade)))
