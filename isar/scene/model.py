"""The two-map vocabulary of the scene layer (SCENE-3), and nothing else.

The scene layer keeps two maps apart: the *hypothesis map* — beliefs about what exists in
intrinsic (Q, |ω|) space — and the *experiment map* — what was actually looked at, as
footprints. This module is the shared DTO vocabulary of both, so it is deliberately
**stdlib-only**: no ``isar.session``, no ``isar.analyze``, no numpy, no Qt. Anything that
would drag a fitter or a workspace in here belongs in the builder that constructs these,
not in the vocabulary.

The scoring contract these types serve is SCENE_DESIGN §2b: local evidence enters
``ln W(S)`` once per observation arena, scene factors are structural only. The DTOs
enforce the "once" structurally — a ``SceneObservation`` carries no fitted-parameter
vectors, no covariances, no raw counts and no likelihood callables, so no scene factor
*can* re-score the local data even by mistake.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum


class Visibility(StrEnum):
    """Why a hypothesized excitation is, or is not, present in an observation.

    The absence states are distinct on purpose: only ``searched_no_signal`` is evidence
    against the hypothesis. ``censored_outside_footprint`` is a coverage gap and must
    contribute no update in either direction; ``blocked_unresolved`` means something else
    (elastic line, a stronger neighbour) made the region uninformative; and
    ``structural_endpoint`` is a hypothesis that legitimately has nothing to see there.
    """

    OBSERVED = "observed"
    SEARCHED_NO_SIGNAL = "searched_no_signal"
    CENSORED_OUTSIDE_FOOTPRINT = "censored_outside_footprint"
    BLOCKED_UNRESOLVED = "blocked_unresolved"
    STRUCTURAL_ENDPOINT = "structural_endpoint"


# ----------------------------------------------------------------------------
# Declared sample knowledge
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SampleKnowledge:
    """What the scientist DECLARED about the sample, as the scene reads it. Never derived.

    Every field is hand-authored intent lifted out of ``card.policy.toml`` by
    :func:`isar.workspace_api.scene.sample_knowledge` — this module stays card-free and
    stdlib-only. Nothing here is inferred from the data, and nothing here is optional in the
    sense of "guess it if absent": an undeclared field leaves the capability that consumes it
    inert, with a recorded degradation reason, and never a default that would let the scene
    invent geometry.

    **Zone geometry and the reflection list are INDEPENDENT declarations**, and conflating them
    is the failure this split exists to prevent. ``zone_period`` says where the reduced-zone
    folds are — the multiples of the period anchored at the crystal origin ``q = 0``, never at a
    measured or tracker-selected q (the O44 rule). ``reflections`` says where the scientist
    declares Bragg reflections actually are. A periodic position is NOT thereby a reflection:
    FCC (300) has integer indices, sits at a fold, and is extinct. So no code here derives
    reflections from the period, from integer indices, from a space group, or from a
    structure-factor rule — the only source is the declaration.

    ``dispersion_slope_max`` is the steepest |dω/dq| the scientist declares any branch of this
    sample can have, in meV per rlu. It is the ONLY thing that makes a track's continuation
    *reachable* over a finite Δq (review finding 5): energy resolution bounds how well two peaks
    are told apart, and bounds nothing at all about how far a dispersing branch moved between two
    q. Undeclared, reachability is UNKNOWN and the scene must fail toward not charging an absence,
    because "the track should have reappeared at ω ≈ ω_m" is exactly the smoothness assumption a
    scene with no declared dispersion support may not invent.

    ``lattice_epoch``/``lattice`` record WHICH lattice interpretation was current when the scene
    was built (``card.state.lattice_epochs``), so a revision cannot be replayed against a
    different one without the fingerprint saying so. Nothing consumes them yet.
    ponytail: they become load-bearing when multi-D geometry lands — declared HKL reflections
    projected onto the scan path through the epoch's lattice and orientation is the upgrade
    path, and it is the reason the scalar ``reflections`` here are axis rlu.
    """

    zone_period: float | None = None
    reflections: tuple[float, ...] = ()
    dispersion_slope_max: float | None = None
    lattice_epoch: int | None = None
    lattice: tuple[float, ...] | None = None

    def __post_init__(self):
        # Malformed declarations fail HERE, loudly (H1): a scene that silently dropped an
        # unreadable zone period would publish "no zone geometry declared" over a card that
        # declares one, and the degradation ledger would say the capability was inert when in
        # truth it was misread.
        if self.zone_period is not None and not (math.isfinite(self.zone_period)
                                                 and self.zone_period > 0.0):
            raise ValueError(f"sample zone_period must be a finite positive rlu period, got "
                             f"{self.zone_period!r}")
        if self.dispersion_slope_max is not None and not (
                math.isfinite(self.dispersion_slope_max) and self.dispersion_slope_max > 0.0):
            raise ValueError(f"sample dispersion_slope_max must be a finite positive meV per rlu, "
                             f"got {self.dispersion_slope_max!r}")
        if not all(math.isfinite(r) for r in self.reflections):
            raise ValueError(f"sample reflections must all be finite rlu positions, got "
                             f"{list(self.reflections)!r}")
        # Canonicalized HERE rather than at the one caller that happens to sort: the declaration
        # is a SET of positions, so ``(3.0, 2.0)`` and ``(2.0, 3.0)`` are the same physics, and
        # ``(2.0, 2.0)`` declares nothing ``(2.0,)`` does not. Left verbatim, each of those pairs
        # would digest to a different revision — a replay that is not replayable because a card
        # was retyped in another order.
        object.__setattr__(self, "reflections",
                           tuple(sorted({float(r) for r in self.reflections})))

    def to_dict(self) -> dict:
        return {"zone_period": self.zone_period,
                "reflections": list(self.reflections),
                "dispersion_slope_max": self.dispersion_slope_max,
                "lattice_epoch": self.lattice_epoch,
                "lattice": None if self.lattice is None else list(self.lattice)}


# ----------------------------------------------------------------------------
# Canonical form
# ----------------------------------------------------------------------------
# The revision payload's schema name lives HERE, with the serialization contract, so readers
# (the workspace facade's schema gate) can name it without importing the builder — the G16
# allow-list admits isar.scene.model, never isar.scene.build.
SCENE_SCHEMA = "isar.scene-revision/1"


# Byte-identical to isar/session/master_delivery.py:_persist_content_addressed, so a scene
# fingerprint and a content-addressed artifact digest agree on what "the same payload"
# means. Deliberately NOT imported from there: this module stays session-free.
def canonical_bytes(payload) -> bytes:
    """The one canonical JSON encoding: sorted keys, no whitespace, UTF-8, strict JSON.

    ``allow_nan=False`` because the encoding is what gets *stored and re-read*: Python's default
    emits the non-standard tokens ``Infinity``/``NaN``, which a strict reader (``json.loads``, and
    every non-Python consumer) rejects — a revision that digests cleanly and then cannot be
    replayed. A non-finite anywhere in a payload is therefore a loud ``ValueError`` here rather
    than a blob that is unreadable later; producers state such a quantity as ``None`` plus an
    explicit unbounded flag instead. Byte-identical to master_delivery's encoder on finite
    payloads, which is every payload that may exist.
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest_bytes(blob: bytes) -> str:
    """sha256 hexdigest of already-canonical bytes.

    Exists so a holder of the canonical encoding (``SceneRevision``) digests the exact bytes
    it stores instead of re-encoding a dict that may since have been handed out and mutated —
    one hash function, one definition of "the same payload".
    """
    return hashlib.sha256(blob).hexdigest()


def fingerprint(payload) -> str:
    """sha256 hexdigest of the canonical encoding of ``payload``."""
    return digest_bytes(canonical_bytes(payload))


# ----------------------------------------------------------------------------
# Hypothesis-map side: what one scan's candidates claim
# ----------------------------------------------------------------------------
# The component kinds a candidate may declare. ``mode`` is a phonon; ``elastic`` is the sharp
# resolution-limited response at ω=0; ``quasielastic`` is the broad one. They are kept apart because
# only ``elastic`` is Bragg-adjacent — the endpoint and lattice-localization arguments are about the
# elastic line specifically — while BOTH obscure a low-energy band (``blocking_present``).
COMPONENT_KINDS = ("mode", "elastic", "quasielastic")


@dataclass(frozen=True, slots=True)
class ComponentDescriptor:
    """What one component of a fitted candidate IS, as the writer enumerated it.

    Persisted per candidate row by :func:`isar.session.scan_delivery._candidate_rows`, derived from
    the ENUMERATED STRUCTURE the candidate was fitted as. The scene consumes these and derives
    component semantics from nothing else: a display label is a stable identifier, not a second
    schema for a reader to reverse-engineer, and a label grammar that drifts (a new suffix, a
    renamed term) would silently reclassify physics rather than fail.

    ``slot`` is the index into the row's ``omegas`` for a ``mode`` and ``None`` for every other
    kind, which sit at the elastic line by construction.
    """

    kind: str
    slot: int | None = None

    def __post_init__(self):
        if self.kind not in COMPONENT_KINDS:
            raise ValueError(f"component descriptor declares kind {self.kind!r}; "
                             f"allowed: {COMPONENT_KINDS}")
        if (self.slot is None) == (self.kind == "mode"):
            raise ValueError(f"component descriptor {self.kind!r} carries slot {self.slot!r}: a "
                             "mode is placed by its index into omegas, and nothing else is placed "
                             "at all")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "slot": self.slot}

    @classmethod
    def from_dict(cls, d: dict) -> "ComponentDescriptor":
        slot = d.get("slot")
        return cls(kind=str(d["kind"]), slot=None if slot is None else int(slot))


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """One locally-fitted structure×identity alternative, as the scene reads it.

    ``log_evidence`` is the lnZ(j, h) of the scoring contract — the *only* number a scene
    alternative may sum. ``posterior_probability`` is the local normalized posterior and is
    carried for reporting: multiplying normalized per-scan posteriors across scans
    double-counts the local priors (SCENE_DESIGN §2b) and is banned.
    """

    label: str
    k: int | None
    n_modes: int
    log_evidence: float | None
    evidence_method: str | None
    posterior_probability: float | None
    usable: bool
    # None is a first-class state, not a missing value: no production catalogue entry
    # declares identity priors, and the scene may establish identity on its own evidence.
    identity: str | None
    variant: str | None
    # Derived ω₀ (meV), the scale FitOutcome.omega0, the branch models and the dispersion
    # prior all publish — never the raw fitted Ω_d. A ``None`` slot is a mode whose position
    # is unplaceable; ``()`` is a phonon-less or result-less candidate, so the length agrees
    # with ``n_modes`` except in the no-result case.
    omegas: tuple[float | None, ...]
    # What this candidate is MADE OF, in the writer's canonical order (modes by slot, then the
    # non-mode kinds by name). Required, with no default: a row that declares no components is a
    # row written before the descriptors existed, and defaulting it to ``()`` would present a
    # multimode candidate as structureless instead of refusing the stale workspace.
    components: tuple[ComponentDescriptor, ...]
    # Conditioning provenance (H9), carried VERBATIM as the row persists it: the aggregate
    # ``constraint_origin`` and the ``effective_constraints`` list the fit actually ran under
    # (``None`` = no semantics record, ``()`` = recorded and unconstrained). This is
    # builder-level bookkeeping for the exactly-once rule — which arena a transported
    # constraint already priced — and is deliberately NOT projected onto
    # ``ComponentObservation``: a scene factor that could see conditioning could re-price the
    # local evidence it is forbidden to touch.
    constraint_origin: str | None = None
    effective_constraints: tuple | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # A non-finite lnZ (±inf, and the NaN an evidence lane actually tends to produce)
        # persists as ``null``. Not a coercion of a meaningful number: the builder
        # already declares a non-finite lnZ unsummable and routes it exactly like ``None``
        # (``build._usable_lnz``, excluded as ``no_evidence``), so the two spellings are already
        # indistinguishable to every consumer. Collapsing them at the serialization boundary is
        # what lets the row travel as strict JSON; leaving ``Infinity`` in would make the whole
        # revision unreadable to protect a distinction nothing may act on.
        if d["log_evidence"] is not None and not math.isfinite(d["log_evidence"]):
            d["log_evidence"] = None
        d["omegas"] = list(self.omegas)
        d["components"] = [c.to_dict() for c in self.components]
        d["effective_constraints"] = (None if self.effective_constraints is None
                                      else [dict(c) for c in self.effective_constraints])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CandidateRow":
        return cls(
            label=str(d["label"]),
            k=None if d.get("k") is None else int(d["k"]),
            n_modes=int(d["n_modes"]),
            log_evidence=_opt_float(d.get("log_evidence")),
            evidence_method=_opt_str(d.get("evidence_method")),
            posterior_probability=_opt_float(d.get("posterior_probability")),
            usable=bool(d["usable"]),
            identity=_opt_str(d.get("identity")),
            variant=_opt_str(d.get("variant")),
            omegas=tuple(_opt_float(w) for w in d.get("omegas") or ()),
            # ``d["components"]``, not ``d.get(...)``: a payload without descriptors predates the
            # delivery contract and must fail rather than read as a componentless candidate.
            components=tuple(ComponentDescriptor.from_dict(c) for c in d["components"]),
            constraint_origin=_opt_str(d.get("constraint_origin")),
            effective_constraints=(
                None if d.get("effective_constraints") is None
                else tuple(dict(c) for c in d["effective_constraints"])),
        )


@dataclass(frozen=True, slots=True)
class ComponentObservation:
    """The one surface a scene factor may read about a component. An allowlist, not a view.

    Deliberately absent, and the reason each absence is structural rather than stylistic:
    counts, θ vectors and covariances (a factor able to see them could re-fit the local
    data), ``log_evidence`` (a factor able to see it could re-price evidence already summed
    once in ``base``), and the local ``posterior_probability`` (multiplying normalized
    per-scan posteriors double-counts the local priors — SCENE_DESIGN §2b).

    ``kind`` is the persisted :class:`ComponentDescriptor` kind, carried through verbatim — it is
    the ONLY thing that says what a component is. ``component_id`` is
    ``"<scan_ref>#<candidate_label>#<mode_index>"`` for a mode and
    ``"<scan_ref>#<candidate_label>#elastic"`` / ``"#qe"`` for the non-mode components: a stable
    identifier, never a source of semantics. ``omega`` is the derived ω₀ of this mode on the same
    scale as ``CandidateRow.omegas`` — ``0.0`` for the non-mode components, which sit at the elastic
    line by construction; ``None`` is an unplaceable mode, which stays a component (it exists) while
    contributing no position a factor could couple on.
    """

    scan_ref: str
    component_id: str
    candidate_label: str
    kind: str
    q: tuple[float, float, float] | None
    omega: float | None
    footprint: "Footprint"
    visibility: Visibility


@dataclass(frozen=True, slots=True)
class SceneObservation:
    """One observation arena: what was measured, and what the local fits made of it.

    Deliberately carries NO fitted-parameter vectors, covariances, raw counts, or
    likelihood callables — the factor-level double-counting ban is structural.
    """

    scan_ref: str                                # "exp:scan:det"
    q: tuple[float, float, float] | None
    temperature_K: float | None
    # Signed calibrated meV on the same ω scale as ``omegas``/``omega0`` — the masked extent
    # of the axis actually fitted. ``None`` when the calibration zero is unusable: a scan is
    # held until calibration exists, never placed by assuming a zero of 0.0.
    energy_window: tuple[float, float] | None
    # The measured energy CELLS of the kept bins, merged into runs, on the same scale — the
    # mask-hole-honest form of the window above. An ``exclude_region`` or a monitor-short stretch
    # opens a hole exactly at the dropped bins, so a band the scan never fitted is not claimed as
    # searched. Being cells rather than points, the runs reach half a bin outside the window's kept
    # POINT extent at each end; the window is unchanged for the consumers that read it. ``None``
    # when the window is unplaceable.
    energy_bands: tuple[tuple[float, float], ...] | None
    gauss_fwhm: float | None
    lorentz_fwhm: float | None
    effective_exposure: float | None
    background: float | None
    best_label: str | None
    candidates: tuple[CandidateRow, ...]
    # Is the local candidate comparison this arena's evidence came from COMPLETE (H7)?
    #   "complete"    — selection ran the Bayesian lane and named a winner on evidence.
    #   "incomplete"  — the comparison was cut short with a contender unresolved (the evidence
    #                   escalation budget expired); the surviving lnZ set is not the whole
    #                   arena, so a posterior built from it would be confidently wrong.
    #   "unavailable" — no readable selection semantics, or a lane that publishes no evidence.
    # Only "complete" may score. ``selection_fallback`` is the verbatim persisted reason (the
    # ``isar.fit_selection_semantics/2`` field of the same name) and ``arena_fingerprint`` the
    # persisted ``evidence_arena_fingerprint`` — the single-likelihood guarantee the compared
    # lnZ values share, kept so a later slice can refuse to pool across arenas.
    evidence_comparison_status: str              # "complete" | "incomplete" | "unavailable"
    selection_fallback: str | None
    arena_fingerprint: str | None
    # Where this scan's evidence was routed (``isar.schema.steering.EVIDENCE_SINKS``), verbatim;
    # ``None`` when nothing persisted a sink, which reads as canonical. Non-canonical sinks are
    # scientific evidence channels of their own (identity, connectivity, transition), so the
    # candidates are PRESERVED here and the routing decision is the builder's.
    evidence_sink: str | None
    fingerprint: str                             # sha256 of this observation's canonical payload

    def to_dict(self) -> dict:
        return {
            "scan_ref": self.scan_ref,
            "q": None if self.q is None else list(self.q),
            "temperature_K": self.temperature_K,
            "energy_window": None if self.energy_window is None else list(self.energy_window),
            "energy_bands": (None if self.energy_bands is None
                             else [list(b) for b in self.energy_bands]),
            "gauss_fwhm": self.gauss_fwhm,
            "lorentz_fwhm": self.lorentz_fwhm,
            "effective_exposure": self.effective_exposure,
            "background": self.background,
            "best_label": self.best_label,
            "candidates": [c.to_dict() for c in self.candidates],
            "evidence_comparison_status": self.evidence_comparison_status,
            "selection_fallback": self.selection_fallback,
            "arena_fingerprint": self.arena_fingerprint,
            "evidence_sink": self.evidence_sink,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SceneObservation":
        return cls(
            scan_ref=str(d["scan_ref"]),
            q=_opt_triple(d.get("q")),
            temperature_K=_opt_float(d.get("temperature_K")),
            energy_window=_opt_pair(d.get("energy_window")),
            # Required like ``evidence_comparison_status``: a payload that cannot say which runs of
            # the axis were fitted cannot be read as coverage, and defaulting it to the outer
            # window would claim the mask holes as searched.
            energy_bands=_opt_bands(d["energy_bands"]),
            gauss_fwhm=_opt_float(d.get("gauss_fwhm")),
            lorentz_fwhm=_opt_float(d.get("lorentz_fwhm")),
            effective_exposure=_opt_float(d.get("effective_exposure")),
            background=_opt_float(d.get("background")),
            best_label=_opt_str(d.get("best_label")),
            candidates=tuple(CandidateRow.from_dict(c) for c in d.get("candidates") or ()),
            # Required, like the fingerprint: a payload that cannot say whether its comparison
            # was complete is not readable as an arena, and defaulting it either way would be a
            # guess about evidence. Old payloads fail loudly here — rebuild the scene.
            evidence_comparison_status=str(d["evidence_comparison_status"]),
            selection_fallback=_opt_str(d.get("selection_fallback")),
            arena_fingerprint=_opt_str(d.get("arena_fingerprint")),
            evidence_sink=_opt_str(d.get("evidence_sink")),
            fingerprint=str(d["fingerprint"]),
        )


def observation_fingerprint(obs: SceneObservation) -> str:
    """The fingerprint of everything in ``obs`` except the fingerprint field itself.

    The builder's pattern is construct-then-stamp:
    ``obs = replace(obs, fingerprint=observation_fingerprint(obs))`` — so whatever the
    field held while building cannot feed back into the digest.

    Candidate ORDER is content here, not presentation: the same rows in a different tuple
    order digest differently. A caller that needs a stable digest across rebuilds must fix a
    deterministic candidate order before constructing the observation.
    """
    payload = obs.to_dict()
    payload.pop("fingerprint", None)
    return fingerprint(payload)


# ----------------------------------------------------------------------------
# Experiment-map side: where we looked, in intrinsic |ω|
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FootprintBand:
    """A covered interval of intrinsic ``|ω| >= 0``, and which signed side(s) covered it."""

    lo: float
    hi: float
    sides: str                                   # "stokes" | "anti_stokes" | "both"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FootprintBand":
        return cls(lo=float(d["lo"]), hi=float(d["hi"]), sides=str(d["sides"]))


@dataclass(frozen=True, slots=True)
class Footprint:
    """Where one observation looked, expressed in intrinsic (unsigned) energy.

    ``bands`` is *geometric* coverage only — the axis extent, folded onto ``|ω|``. Whether a
    hypothesis was actually detectable there depends on its own amplitude and on the Bose
    factor for the side in question, so the detectability inputs travel alongside the bands
    and the cut is made at projection time, per hypothesis, never baked in here.

    The detectability inputs are the FULL set the observation carries: resolution has two
    components (``gauss_fwhm`` and ``lorentz_fwhm`` — a Voigt whose Lorentzian wings decide
    whether a weak neighbour was resolvable at all), and ``effective_exposure`` is how deep
    the look was. A footprint carrying only the Gaussian width would let a projection call a
    region "searched" on the wrong resolution, and one carrying no exposure could not tell a
    long look that saw nothing from a glance that saw nothing.

    ``path_index`` and ``axis_coordinate`` are this arena's place in the scene's derived path
    topology (:func:`path_topology`), stamped by the builder because they are facts about the
    arena SET rather than about one observation. ``path_index`` is the ordinal of its node along
    the ordered path — the only thing adjacency is ever read from — and ``axis_coordinate`` is its
    position in rlu along the reciprocal axis the path runs on, which is the only coordinate the
    card's scalar zone period and reflection list can be compared against. Both are ``None`` when
    the topology could not be derived, and every consumer then couples and classifies nothing.
    """

    scan_ref: str
    q: tuple[float, float, float] | None
    bands: tuple[FootprintBand, ...]             # may be empty: an unplaceable window covers nothing
    temperature_K: float | None
    background: float | None
    gauss_fwhm: float | None
    lorentz_fwhm: float | None = None
    effective_exposure: float | None = None
    path_index: int | None = None
    axis_coordinate: float | None = None

    def to_dict(self) -> dict:
        return {
            "scan_ref": self.scan_ref,
            "q": None if self.q is None else list(self.q),
            "bands": [b.to_dict() for b in self.bands],
            "temperature_K": self.temperature_K,
            "background": self.background,
            "gauss_fwhm": self.gauss_fwhm,
            "lorentz_fwhm": self.lorentz_fwhm,
            "effective_exposure": self.effective_exposure,
            "path_index": self.path_index,
            "axis_coordinate": self.axis_coordinate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Footprint":
        return cls(
            scan_ref=str(d["scan_ref"]),
            q=_opt_triple(d.get("q")),
            bands=tuple(FootprintBand.from_dict(b) for b in d.get("bands") or ()),
            temperature_K=_opt_float(d.get("temperature_K")),
            background=_opt_float(d.get("background")),
            gauss_fwhm=_opt_float(d.get("gauss_fwhm")),
            lorentz_fwhm=_opt_float(d.get("lorentz_fwhm")),
            effective_exposure=_opt_float(d.get("effective_exposure")),
            path_index=None if d.get("path_index") is None else int(d["path_index"]),
            axis_coordinate=_opt_float(d.get("axis_coordinate")),
        )


def footprint_of(obs: SceneObservation, topology: "PathTopology | None" = None) -> Footprint:
    """Fold an observation's KEPT energy runs onto intrinsic ``|ω|``. Pure, deterministic.

    Each signed run ``(lo, hi)`` covers the Stokes side over ``[max(0, lo), hi]`` (when ``hi > 0``)
    and the anti-Stokes side over ``[max(0, -hi), -lo]`` (when ``lo < 0``). A run spanning zero
    therefore covers part of ``|ω|`` from both sides, which is a stronger statement than either
    alone, so the overlap is reported as one ``both`` band and the remainders keep their own side.

    Built from ``energy_bands`` rather than the outer window, because a footprint is a claim about
    what was MEASURED: a QC-masked or excluded stretch in the middle of the axis was not searched,
    and a footprint spanning it would let ``covers`` answer "we looked there" for a region no
    counting ever reached — absence priced against a hole. ``energy_bands is None`` → no bands: a
    scan with no usable calibration zero has no placeable footprint at all.
    """
    if obs.energy_bands is None:
        return _footprint(obs, (), topology)
    intervals = []
    for band in obs.energy_bands:
        lo, hi = float(band[0]), float(band[1])
        if hi > 0.0:
            intervals.append((max(0.0, lo), hi, "stokes"))
        if lo < 0.0:
            intervals.append((max(0.0, -hi), -lo, "anti_stokes"))
    edges = sorted({e for a, b, _ in intervals for e in (a, b)})
    bands = []
    for left, right in zip(edges, edges[1:]):
        if right <= left:                        # zero-length slivers carry no coverage
            continue
        mid = 0.5 * (left + right)
        sides = sorted({s for a, b, s in intervals if a <= mid <= b})
        if not sides:                            # a hole between two kept runs: no coverage at all
            continue
        bands.append(FootprintBand(left, right, "both" if len(sides) == 2 else sides[0]))
    return _footprint(obs, tuple(bands), topology)


def _footprint(obs: SceneObservation, bands: tuple[FootprintBand, ...],
               topology: "PathTopology | None") -> Footprint:
    return Footprint(scan_ref=obs.scan_ref, q=obs.q, bands=bands,
                     temperature_K=obs.temperature_K, background=obs.background,
                     gauss_fwhm=obs.gauss_fwhm, lorentz_fwhm=obs.lorentz_fwhm,
                     effective_exposure=obs.effective_exposure,
                     path_index=None if topology is None else topology.index.get(obs.scan_ref),
                     axis_coordinate=(None if topology is None
                                      else topology.axis_coordinate.get(obs.scan_ref)))


# ----------------------------------------------------------------------------
# Cross-arena geometry: the vocabulary both the builder and the factors need
# ----------------------------------------------------------------------------
# Owner-tunable, and declared in every factor's ``config`` that closes over them, so a retuning
# moves the registry fingerprint. They live here rather than in ``factors``/``build`` because
# both sides need the same numbers and a second copy would be a second physics.
#
# PATH_COLLINEAR_TOL: how far off the straight line through the two most-separated arenas a q may
# sit and still be read as a point OF that line, in rlu. It is a Q-POSITIONING tolerance, not a
# grid tolerance: a spectrometer places a requested q to well inside the 0.05 rlu step ISAR rounds
# HKL to (``CardPolicy.hkl_round_step``), so 0.01 rlu is an order of magnitude below the smallest
# deliberate step between two grid nodes. That gap is the whole point — two nodes the scientist
# deliberately put on different rows of the grid can never read as positioning noise about one
# line, and a real path's sub-step jitter never reads as a scattered q set.
PATH_COLLINEAR_TOL = 0.01
# PATH_AXIS_TOL: how far the unit path direction may tilt off a reciprocal axis and still be that
# axis. The declared ``zone_period`` and ``reflections`` are scalars in rlu ALONG an axis, so they
# mean nothing on a path that runs across two of them; 0.01 of a unit direction (~0.6°) is again
# the positioning scale rather than a physics knob.
PATH_AXIS_TOL = 0.01
# LINK_TOL_FLOOR: the smallest ω jump two arenas' modes may be linked across when the resolution
# width is unknown or absurdly small. The tolerance itself is the coarser of the two arenas'
# Gaussian widths — a track moves between neighbouring q by less than the resolution, or it was
# never resolved as one track.
LINK_TOL_FLOOR = 0.1

# The ``component_id`` suffix each non-mode kind is identified by. An IDENTIFIER, read by nothing
# that decides physics: what a component is comes from ``ComponentObservation.kind``.
NON_MODE_ID_TAGS = {"elastic": "elastic", "quasielastic": "qe"}

# The kinds that obscure a band at the elastic line. Both the sharp elastic response and a broad
# quasielastic one hide a low-energy peak, so both make an absence there uninformative — but only
# ``elastic`` is a Bragg-adjacent claim, which is why this set is NOT the endpoint prerequisite.
BLOCKING_KINDS = ("elastic", "quasielastic")


def is_mode(component: ComponentObservation) -> bool:
    """True for a phonon mode, False for the elastic / quasielastic components."""
    return component.kind == "mode"


def blocks_elastic_line(component: ComponentObservation) -> bool:
    """True for a component that hides a band at ``|ω| ≈ 0`` (elastic or quasielastic)."""
    return component.kind in BLOCKING_KINDS


@dataclass(frozen=True, slots=True)
class PathTopology:
    """The ORDERED path the arenas were measured along, derived from the q set alone.

    ``h`` is not a path coordinate and a radius in q is not path adjacency (review finding 3): a
    campaign that walks (2,2,0) → (2,0,0) moves along k at constant h, so ranking or thresholding
    on h collapses the whole path onto one point, and a radius predicate makes every arena inside
    it a "neighbour" of every other — so inserting an intermediate scan between a and c leaves the
    a–c term standing beside the two new ones, and one absence is priced twice over.

    This carries the path instead. ``index`` is each arena's ordinal along the ordered projection
    and adjacency is CONSECUTIVE ordinals only, so the edges are the segments of the path and
    nothing else; ``axis_coordinate`` is the arena's rlu position along the reciprocal axis the
    path runs on, present only when the path IS an axis, because the card's scalar zone period and
    reflection list are declared along an axis and mean nothing across two of them.

    ``collinear`` false is the fail-closed arm: a genuinely scattered q set has no path, so it
    yields no index at all, every arena is its own cluster, and the build records
    ``path_topology_unavailable``. A scene may still classify and localize per arena; what it may
    not do is invent an ordering that decides which absences are evidence.
    """

    collinear: bool
    axis: int | None                             # 0/1/2 when the path runs along a reciprocal axis
    index: dict                                  # scan_ref -> ordinal along the path
    axis_coordinate: dict                        # scan_ref -> rlu along ``axis``


def path_topology(q_by_ref) -> PathTopology:
    """Derive the ordered path from the arenas' q. Pure, deterministic, stdlib.

    Take the distinct q vectors; the direction is fixed by the two most-separated of them (a
    deterministic choice — ties break on the sorted vectors themselves), every point is projected
    onto it, and the set is a PATH only if every perpendicular residual is within
    ``PATH_COLLINEAR_TOL``. Ordinals are the rank of the projection among the distinct projected
    values, so two arenas at the same q (two detectors, one position) share an ordinal and are
    neighbours a fortiori, while everything else is a neighbour of its immediate predecessor and
    successor only.

    With a single distinct q there is no path and therefore no direction. The only axis such a
    scene can establish is the one the arena itself sits on, so a q with exactly one nonzero
    component declares that axis and anything else declares none — a one-arena scene keeps its
    lattice classification without inventing a direction it never traversed.

    ponytail: O(n²) over distinct q to pick the extremes. Campaign scale is tens of arenas; a
    convex-hull or PCA pass is the upgrade path, and neither is stdlib.
    """
    placeable = {r: (float(q[0]), float(q[1]), float(q[2]))
                 for r, q in sorted(q_by_ref.items()) if q is not None}
    points = sorted(set(placeable.values()))
    if not points:
        return PathTopology(collinear=True, axis=None, index={}, axis_coordinate={})
    if len(points) == 1:
        axis = _sole_nonzero(points[0])
        return PathTopology(collinear=True, axis=axis,
                            index={r: 0 for r in placeable},
                            axis_coordinate=_axis_coordinates(placeable, axis))
    a, b = max(itertools.combinations(points, 2), key=lambda pair: _dist2(*pair))
    span = math.sqrt(_dist2(a, b))
    direction = tuple((bi - ai) / span for ai, bi in zip(a, b))
    projected = {}
    for ref, q in placeable.items():
        offset = tuple(qi - ai for qi, ai in zip(q, a))
        t = math.fsum(oi * di for oi, di in zip(offset, direction))
        residual = math.sqrt(math.fsum((oi - t * di) ** 2 for oi, di in zip(offset, direction)))
        if residual > PATH_COLLINEAR_TOL:
            return PathTopology(collinear=False, axis=None, index={}, axis_coordinate={})
        projected[ref] = t
    order = sorted(set(projected.values()))
    axis = _sole_nonzero(direction, tol=PATH_AXIS_TOL)
    return PathTopology(collinear=True, axis=axis,
                        index={r: order.index(t) for r, t in projected.items()},
                        axis_coordinate=_axis_coordinates(placeable, axis))


def _dist2(a, b) -> float:
    return math.fsum((ai - bi) ** 2 for ai, bi in zip(a, b))


def _sole_nonzero(vector, tol: float = 0.0) -> int | None:
    """The index of ``vector``'s only nonzero component, or ``None`` if it has none or several."""
    nonzero = [i for i, v in enumerate(vector) if abs(v) > tol]
    return nonzero[0] if len(nonzero) == 1 else None


def _axis_coordinates(placeable, axis: int | None) -> dict:
    return {} if axis is None else {r: q[axis] for r, q in placeable.items()}


def path_adjacent(fp_a: Footprint, fp_b: Footprint) -> bool:
    """Are two arenas CONSECUTIVE nodes of the derived path (or the same node)?

    The one adjacency predicate in the layer. An arena with no ordinal — an unplaceable q, or a
    scene whose q set is not a path at all — is adjacent to nothing, which is the fail-closed arm:
    no cross-arena coupling, no absence priced across an ordering nobody established.
    """
    ia, ib = fp_a.path_index, fp_b.path_index
    return ia is not None and ib is not None and abs(ia - ib) <= 1


def path_delta_q(fp_a: Footprint, fp_b: Footprint) -> float | None:
    """The edge length |Δq| in rlu between two arenas, or ``None`` when either is unplaceable.

    Euclidean in rlu, which on a collinear path is exactly the difference of the two projections —
    the segment length a declared dispersion slope is multiplied by.
    """
    if fp_a.q is None or fp_b.q is None:
        return None
    return math.sqrt(_dist2(fp_a.q, fp_b.q))


def link_tol(fp_a: Footprint, fp_b: Footprint) -> float:
    """The RESOLUTION-scale ω tolerance within which two arenas' peaks are one unresolved track."""
    return max(fp_a.gauss_fwhm or 0.0, fp_b.gauss_fwhm or 0.0, LINK_TOL_FLOOR)


def link_reach(fp_a: Footprint, fp_b: Footprint, slope_max: float | None,
               factor: float = 1.0) -> float:
    """How far in ω a track may move between two arenas: resolution floor + declared dispersion.

    ``factor × link_tol`` is the resolution-scale floor — two peaks closer than the resolution were
    never resolved as two tracks, whatever the dispersion does — and ``slope_max × Δq`` is the only
    term that says how far a branch could actually have MOVED (review finding 5). Energy resolution
    is a statement about telling two peaks apart and none whatever about dω/dq, so with no declared
    slope this returns the floor alone and the caller must treat non-continuation as UNKNOWN rather
    than as an absence (see :func:`exclusion_blocked_reason`).

    One grammar for both consumers: the crossing proposer passes its ``factor`` and gets the same
    formula, so proposal reach and linkage reach can never drift into two physics.
    """
    floor = factor * link_tol(fp_a, fp_b)
    dq = path_delta_q(fp_a, fp_b)
    if slope_max is None or dq is None:
        return floor
    return floor + float(slope_max) * dq


# ----------------------------------------------------------------------------
# Detectability: what has to hold before an absence may be evidence at all
# ----------------------------------------------------------------------------
# Boltzmann constant in meV/K, the value isar/analyze/lineshapes.py:KB uses. Restated rather than
# imported: isar/scene is stdlib-only by construction (tests/scene/test_import_ban.py), and the
# scene may not drag the fitter in to read a constant.
KB_MEV_PER_K = 0.086173324

# The detailed-balance weight n(ω,T)/(n(ω,T)+1) = exp(−ω/k_BT) below which an anti-Stokes band
# cannot exclude anything. Production drops the anti-Stokes side when its PREDICTED COUNTS —
# Stokes excursion × that weight — fall below AS_DETECT_Z=3 times the background noise
# (isar/analyze/phonon.py:AS_DETECT_Z / antistokes_mode, the same physics). The scene holds no
# amplitudes by construction, so it mirrors that z as a floor on the weight itself: a band whose
# anti-Stokes intensity is suppressed more than the detection z-score below the Stokes side cannot
# be read as "we looked and it was not there".
# ponytail: a weight floor, not a projection. The real upgrade is per-hypothesis anti-Stokes
# amplitude projection against this footprint's background — which needs an amplitude the scene
# deliberately cannot see, so it belongs to a later slice that carries a declared intensity model.
AS_DETECT_WEIGHT = 1.0 / 3.0

# How much shallower than the arena where a mode was SEEN a neighbour's look may be and still
# exclude it. Absence is evidence only if the neighbour counted comparably deep: a glance that saw
# nothing is not the same measurement as a long count that saw nothing, and pricing them alike is
# how a steering run that spent its neutrons elsewhere would come back as evidence against the
# mode it never looked hard for. 0.3 is an ORDER-OF-MAGNITUDE-comparable test rather than equality
# — a factor of ~3 in counting time is ~1.7 in Poisson σ, so a peak detected with any real margin
# at i is still detectable at j — and nothing finer is defensible without an amplitude.
# ponytail: the honest upgrade is the same per-hypothesis amplitude projection named above; this
# ratio is the exposure-only stand-in for it.
DETECT_EXPOSURE_RATIO = 0.3


def bose_weight(omega: float, temperature_K: float) -> float:
    """The detailed-balance weight ``exp(−ω/k_BT)`` — anti-Stokes intensity over Stokes."""
    if temperature_K <= 0.0:
        return 0.0
    return math.exp(-abs(float(omega)) / (KB_MEV_PER_K * float(temperature_K)))


def _covering_sides(footprint: Footprint, band_omega: float) -> set:
    w = abs(float(band_omega))
    return {b.sides for b in footprint.bands if b.lo <= w <= b.hi}


def exclusion_blocked_reason(band_omega: float, here: Footprint, there: Footprint, *,
                             partners_present: bool, slope_max: float | None) -> str | None:
    """Why ``there``'s silence at ``|ω| = band_omega`` cannot exclude the mode, or ``None``.

    Geometric coverage says a scan LOOKED at a band; it does not say the scan could have SEEN
    anything there (review finding 2), and only the second is evidence. Three gates, each a
    fail-closed answer computed from footprint facts alone:

    * **reachability** — with no declared dispersion slope the scene cannot say where a track
      should have reappeared, so a neighbour holding mode components at other energies is not a
      neighbour that found nothing: charging it would be the smoothness assumption of finding 5.
      Only a neighbour with NO mode components at all is an unambiguous non-detection.
    * **Bose** — a band covered only from the anti-Stokes side is only informative if the
      anti-Stokes intensity was there to see. Below ``AS_DETECT_WEIGHT`` it was not, and an
      unknown temperature cannot establish that it was.
    * **detection power** — the look must be comparable in depth to the one that saw the mode
      (``DETECT_EXPOSURE_RATIO``). An unrecorded exposure on either side establishes nothing.

    Failing any gate makes the region uninformative, which is exactly ``blocked_unresolved``: the
    caller demotes the absence and records the reason, so the ledger says WHY nothing was charged
    rather than showing an absence that silently priced at zero.
    """
    if partners_present and slope_max is None:
        return "reachability_unknown"
    if _covering_sides(there, band_omega) == {"anti_stokes"}:
        if there.temperature_K is None:
            return "bose_unknown"
        if bose_weight(band_omega, there.temperature_K) < AS_DETECT_WEIGHT:
            return "bose_dead"
    if here.effective_exposure is None or there.effective_exposure is None:
        return "detection_power_unknown"
    if there.effective_exposure < DETECT_EXPOSURE_RATIO * here.effective_exposure:
        return "insufficient_detection_power"
    return None


def covers(footprint: Footprint, band_omega: float) -> bool:
    """Did this observation look at intrinsic ``|ω| = band_omega``? Geometric coverage only."""
    w = abs(float(band_omega))
    return any(b.lo <= w <= b.hi for b in footprint.bands)


def visibility_of(band_omega: float, footprint: Footprint, has_match: bool, *,
                  blocking_present: bool = False) -> Visibility:
    """Why a hypothesized band at ``|ω| = band_omega`` is, or is not, seen in ``footprint``.

    The one gate every "expected but missing" penalty must pass (SCENE_DESIGN §2b): absence is
    evidence ONLY inside coverage. A band the neighbouring scan never looked at is
    ``censored_outside_footprint`` and can never be penalized — the gap is not a measurement.

    ``blocking_present`` says an elastic OR quasielastic component is chosen at that arena, so a
    band within one Gaussian FWHM of zero is hidden under that response rather than absent from the
    data: ``blocked_unresolved``, also never penalized. Detectability is the whole question here —
    a broad quasielastic feature obscures a low-energy peak exactly as a sharp elastic line does,
    while being no Bragg claim at all, so the two are only ever conflated in the one place they
    genuinely agree. (``structural_endpoint`` is a STRONGER statement that this function cannot
    make: it needs declared sample knowledge AND the elastic line specifically, so it is assigned
    afterwards by the ``zone_centre_approach`` factor's ``refine_visibility`` hook, which may only
    replace the two absence states below and never ``observed`` or ``censored_outside_footprint``.)

    A matched band is ``observed`` even when it sits under the elastic line: something WAS seen
    there, and calling it blocked would throw away a real detection.
    """
    if not covers(footprint, band_omega):
        return Visibility.CENSORED_OUTSIDE_FOOTPRINT
    if has_match:
        return Visibility.OBSERVED
    if blocking_present and abs(float(band_omega)) <= (footprint.gauss_fwhm or 0.0):
        return Visibility.BLOCKED_UNRESOLVED
    return Visibility.SEARCHED_NO_SIGNAL


# ----------------------------------------------------------------------------
# JSON-boundary coercion: JSON has lists where the DTOs have tuples, and None means
# "no number" everywhere in this layer — never NaN, never a substituted default.
# ----------------------------------------------------------------------------
def _opt_float(v) -> float | None:
    return None if v is None else float(v)


def _opt_str(v) -> str | None:
    return None if v is None else str(v)


def _opt_pair(v) -> tuple[float, float] | None:
    return None if v is None else (float(v[0]), float(v[1]))


def _opt_bands(v) -> tuple[tuple[float, float], ...] | None:
    return None if v is None else tuple((float(b[0]), float(b[1])) for b in v)


def _opt_triple(v) -> tuple[float, float, float] | None:
    return None if v is None else (float(v[0]), float(v[1]), float(v[2]))
