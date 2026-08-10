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
# Canonical form
# ----------------------------------------------------------------------------
# Byte-identical to isar/session/master_delivery.py:_persist_content_addressed, so a scene
# fingerprint and a content-addressed artifact digest agree on what "the same payload"
# means. Deliberately NOT imported from there: this module stays session-free.
def canonical_bytes(payload) -> bytes:
    """The one canonical JSON encoding: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def fingerprint(payload) -> str:
    """sha256 hexdigest of the canonical encoding of ``payload``."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


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
    reserves ``"<scan_ref>#<candidate_label>#elastic"`` / ``"#qe"`` for the non-mode
    components a later slice will emit. ``omega`` is the derived ω₀ of this mode on the same
    scale as ``CandidateRow.omegas``; ``None`` is an unplaceable mode, which stays a
    component (it exists) while contributing no position a factor could couple on.
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
    """

    scan_ref: str
    q: tuple[float, float, float] | None
    bands: tuple[FootprintBand, ...]             # may be empty: an unplaceable window covers nothing
    temperature_K: float | None
    background: float | None
    gauss_fwhm: float | None

    def to_dict(self) -> dict:
        return {
            "scan_ref": self.scan_ref,
            "q": None if self.q is None else list(self.q),
            "bands": [b.to_dict() for b in self.bands],
            "temperature_K": self.temperature_K,
            "background": self.background,
            "gauss_fwhm": self.gauss_fwhm,
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
                     gauss_fwhm=obs.gauss_fwhm)


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
