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

    ``lattice_epoch``/``lattice`` record WHICH lattice interpretation was current when the scene
    was built (``card.state.lattice_epochs``), so a revision cannot be replayed against a
    different one without the fingerprint saying so. Nothing consumes them yet.
    ponytail: they become load-bearing when multi-D geometry lands — declared HKL reflections
    projected onto the scan path through the epoch's lattice and orientation is the upgrade
    path, and it is the reason the scalar ``reflections`` here are path-coordinate rlu.
    """

    zone_period: float | None = None
    reflections: tuple[float, ...] = ()
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
                "lattice_epoch": self.lattice_epoch,
                "lattice": None if self.lattice is None else list(self.lattice)}


# ----------------------------------------------------------------------------
# Canonical form
# ----------------------------------------------------------------------------
# Byte-identical to isar/session/master_delivery.py:_persist_content_addressed, so a scene
# fingerprint and a content-addressed artifact digest agree on what "the same payload"
# means. Deliberately NOT imported from there: this module stays session-free.
def canonical_bytes(payload) -> bytes:
    """The one canonical JSON encoding: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
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
        d["omegas"] = list(self.omegas)
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

    ``component_id`` is ``"<scan_ref>#<candidate_label>#<mode_index>"`` for a mode, and
    ``"<scan_ref>#<candidate_label>#elastic"`` / ``"#qe"`` for the non-mode components of a
    phonon-less or elastic-bearing structure (:func:`is_mode` tells them apart). ``omega`` is
    the derived ω₀ of this mode on the same scale as ``CandidateRow.omegas`` — ``0.0`` for the
    non-mode components, which sit at the elastic line by construction; ``None`` is an
    unplaceable mode, which stays a component (it exists) while contributing no position a
    factor could couple on.
    """

    scan_ref: str
    component_id: str
    candidate_label: str
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
    """

    scan_ref: str
    q: tuple[float, float, float] | None
    bands: tuple[FootprintBand, ...]             # may be empty: an unplaceable window covers nothing
    temperature_K: float | None
    background: float | None
    gauss_fwhm: float | None
    lorentz_fwhm: float | None = None
    effective_exposure: float | None = None

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
        )


def footprint_of(obs: SceneObservation) -> Footprint:
    """Fold an observation's signed energy window onto intrinsic ``|ω|``. Pure, deterministic.

    The signed window ``(lo, hi)`` covers the Stokes side over ``[max(0, lo), hi]`` (when
    ``hi > 0``) and the anti-Stokes side over ``[max(0, -hi), -lo]`` (when ``lo < 0``). A
    window spanning zero therefore covers part of ``|ω|`` from both sides, which is a
    stronger statement than either alone, so the overlap is reported as one ``both`` band
    and the remainders keep their own side. ``energy_window is None`` → no bands: a scan
    with no usable calibration zero has no placeable footprint at all.
    """
    window = obs.energy_window
    if window is None:
        return _footprint(obs, ())
    lo, hi = float(window[0]), float(window[1])
    intervals = {}
    if hi > 0.0:
        intervals["stokes"] = (max(0.0, lo), hi)
    if lo < 0.0:
        intervals["anti_stokes"] = (max(0.0, -hi), -lo)
    edges = sorted({e for span in intervals.values() for e in span})
    bands = []
    for left, right in zip(edges, edges[1:]):
        if right <= left:                        # zero-length slivers carry no coverage
            continue
        mid = 0.5 * (left + right)
        sides = sorted(s for s, (a, b) in intervals.items() if a <= mid <= b)
        if not sides:
            continue
        bands.append(FootprintBand(left, right, "both" if len(sides) == 2 else sides[0]))
    return _footprint(obs, tuple(bands))


def _footprint(obs: SceneObservation, bands: tuple[FootprintBand, ...]) -> Footprint:
    return Footprint(scan_ref=obs.scan_ref, q=obs.q, bands=bands,
                     temperature_K=obs.temperature_K, background=obs.background,
                     gauss_fwhm=obs.gauss_fwhm, lorentz_fwhm=obs.lorentz_fwhm,
                     effective_exposure=obs.effective_exposure)


# ----------------------------------------------------------------------------
# Cross-arena geometry: the vocabulary both the builder and the factors need
# ----------------------------------------------------------------------------
# Owner-tunable, and declared in every factor's ``config`` that closes over them, so a retuning
# moves the registry fingerprint. They live here rather than in ``factors``/``build`` because
# both sides need the same numbers and a second copy would be a second physics.
#
# Q_ADJACENCY: path-coordinate (h) distance within which two arenas are NEIGHBOURS — the
# distance over which "a phonon cannot appear at one q and vanish at the next" is a claim about
# the same physics. Sample knowledge (a real path parameterization) replaces the bare |Δh| in
# the next task.
Q_ADJACENCY = 0.15
# LINK_TOL_FLOOR: the smallest ω jump two arenas' modes may be linked across when the resolution
# width is unknown or absurdly small. The tolerance itself is the coarser of the two arenas'
# Gaussian widths — a track moves between neighbouring q by less than the resolution, or it was
# never resolved as one track.
LINK_TOL_FLOOR = 0.1

# The non-mode component suffixes (see ``ComponentObservation.component_id``).
NON_MODE_COMPONENTS = ("elastic", "qe")


def is_mode(component: ComponentObservation) -> bool:
    """True for a phonon mode, False for the ``#elastic`` / ``#qe`` non-mode components."""
    return component.component_id.rsplit("#", 1)[-1] not in NON_MODE_COMPONENTS


def path_coordinate(q) -> float | None:
    """The scalar path coordinate of an arena: h of ``(h, k, l)``. ``None`` when unplaceable."""
    return None if q is None else float(q[0])


def q_adjacent(a, b, tol: float = Q_ADJACENCY) -> bool:
    """Are two arenas neighbours on the q path? ``q is None`` couples to nothing."""
    ha, hb = path_coordinate(a), path_coordinate(b)
    return ha is not None and hb is not None and abs(ha - hb) <= tol


def link_tol(fp_a: Footprint, fp_b: Footprint) -> float:
    """The ω tolerance within which two arenas' modes may be the same track."""
    return max(fp_a.gauss_fwhm or 0.0, fp_b.gauss_fwhm or 0.0, LINK_TOL_FLOOR)


def covers(footprint: Footprint, band_omega: float) -> bool:
    """Did this observation look at intrinsic ``|ω| = band_omega``? Geometric coverage only."""
    w = abs(float(band_omega))
    return any(b.lo <= w <= b.hi for b in footprint.bands)


def visibility_of(band_omega: float, footprint: Footprint, has_match: bool, *,
                  elastic_present: bool = False) -> Visibility:
    """Why a hypothesized band at ``|ω| = band_omega`` is, or is not, seen in ``footprint``.

    The one gate every "expected but missing" penalty must pass (SCENE_DESIGN §2b): absence is
    evidence ONLY inside coverage. A band the neighbouring scan never looked at is
    ``censored_outside_footprint`` and can never be penalized — the gap is not a measurement.

    ``elastic_present`` says an ``#elastic``/``#qe`` component is chosen at that arena, so a band
    within one Gaussian FWHM of zero is hidden under the elastic line rather than absent from the
    data: ``blocked_unresolved``, also never penalized. (This is the endpoint-under-Bragg
    ingredient. ``structural_endpoint`` is a STRONGER statement that this function cannot make:
    it needs declared sample knowledge, so it is assigned afterwards by the
    ``zone_centre_approach`` factor's ``refine_visibility`` hook, which may only replace the two
    absence states below and never ``observed`` or ``censored_outside_footprint``.)

    A matched band is ``observed`` even when it sits under the elastic line: something WAS seen
    there, and calling it blocked would throw away a real detection.
    """
    if not covers(footprint, band_omega):
        return Visibility.CENSORED_OUTSIDE_FOOTPRINT
    if has_match:
        return Visibility.OBSERVED
    if elastic_present and abs(float(band_omega)) <= (footprint.gauss_fwhm or 0.0):
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


def _opt_triple(v) -> tuple[float, float, float] | None:
    return None if v is None else (float(v[0]), float(v[1]), float(v[2]))
