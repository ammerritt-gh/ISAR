"""The machine objective — what the LLM supervisor produces and the rest of the driver consumes.

An :class:`ObjectiveSpec` is the settled, machine-readable form of a natural-language goal
("map the acoustic phonon dispersion from A to B, resolve ω₀ to ±0.1 meV, overnight",
LLM_HARNESS_DESIGN §2.1, §8): a reciprocal-space **path**, a per-point **tolerance** on ω₀, a
**budget**, and the fidelity **tier**. It carries no physics logic — it is a plain, serializable
dataclass so the acquisition engine (§2.2) and the stopping criterion (§2.1) share one source of
truth and the session log can record it verbatim.

This module is WORKING plumbing. Translating a natural-language goal *into* an ObjectiveSpec is the
LLM supervisor's job — see :mod:`isar.drive.supervisor.goal_to_spec` (a stub this round).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from isar.schema.eig_policy import ScanWindowEIGConfig
from isar.schema.hypotheses import (
    SteeringPolicy,
    content_fingerprint,
    preset,
    resolve_hypothesis,
)

# The two fidelity tiers a driver campaign can run against (LLM_HARNESS_DESIGN §6). "deterministic"
# is TAVI's fast analytic engine (develop the loop); "mcstas" is the full Monte-Carlo (validate).
TIERS = ("deterministic", "mcstas")
TARGET_BRANCHES = ("acoustic", "optic")
TARGET_SELECTORS = ("lowest_energy", "highest_energy", "nearest_energy", "tracked_branch")
OBJECTIVE_SCHEMA = "isar.objective/2"
CURRENT_OBJECTIVE_SCOPE_REVISION = "isar.current_objective_comparison_scope/1"
_DEFAULT_ANALYSIS_LIMIT = object()

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- canonical budget defaults
# THE single source of truth for the campaign budget a goal/spec gets when it declares none. Every
# other surface (the supervisor compiler, the CLI, the GUI campaign form, example goal resources)
# imports these rather than restating a number — a second copy is how an example goal ends up
# carrying a budget nobody chose. They are re-exported from
# :mod:`isar.drive.supervisor.goal_to_spec`, which is where callers outside this package find them.
#
# Tier-aware because the two tiers cost wildly different wall time per counting second:
# ``deterministic`` is TAVI's fast analytic engine, where an hour of virtual counting time already
# covers a full development campaign; ``mcstas`` is the full Monte-Carlo, sized for an overnight run.
DEFAULT_BUDGET_HOURS_BY_TIER: dict[str, float] = {"deterministic": 1.0, "mcstas": 8.0}
#: The fallback for a tier not listed above — the conservative overnight figure.
DEFAULT_BUDGET_HOURS = 8.0
#: No neutron budget by default. ``None`` means UNLIMITED, and every spend check treats it as
#: unconstrained via an explicit ``is None`` guard (never as an infinite amount to compare against).
DEFAULT_BUDGET_NEUTRONS: float | None = None


def default_budget_hours(tier: str) -> float:
    """The canonical default counting-time budget, in hours, for ``tier``."""
    return float(DEFAULT_BUDGET_HOURS_BY_TIER.get(str(tier), DEFAULT_BUDGET_HOURS))


@dataclass
class ObjectiveSpec:
    """A measurement goal reduced to machine terms.

    Attributes:
        path: the reciprocal-space path as an ordered list of ``(h, k, l)`` points (r.l.u.). The
            acquisition engine selects Q positions *along* this path; it is not a fixed measurement
            grid. Build a straight H-segment with :meth:`along_h`.
        tolerance_meV: the per-point target on the **calibrated** σ(ω₀). The objective is *met* when
            calibrated σ(ω₀) ≤ this everywhere along ``path``. NOTE the calibrated interval half-width
            (from ISAR's ``results.csv`` intervals, inflated by the σ-calibration) is what the
            stopping test consumes — NEVER ISAR's prior sigma, which floors at 0.3 meV by design.
        budget: the campaign budget. ``kind`` is ``"counting_seconds"`` (the only term available until
            TAVI grows the virtual instrument clock) or ``"neutrons"``; ``amount`` is the limit.
        tier: ``"deterministic"`` | ``"mcstas"`` (see :data:`TIERS`).
        name: a short human label for the objective (used in the session log / report). Optional.
        meta: free-form provenance the supervisor may attach (the original goal string, warm-start
            assumptions, sound-velocity estimate, …). Never consumed for a decision here.
    """

    path: list[tuple[float, float, float]]
    tolerance_meV: float
    budget: dict[str, Any] | None
    tier: str = "deterministic"
    name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    target_branch: str = "acoustic"
    target_label: str = "acoustic"
    target_selector: str | None = None
    target_energy_meV: float | None = None
    energy_search_meV: tuple[float, float] | None = None
    # Where the *identity* question may be asked, as opposed to where the target is expected.
    # ``energy_search_meV`` bounds target delivery and precision work; a rival mode that settles which
    # branch a peak belongs to can legitimately sit outside it (AP6: target 5.754 meV, rival 7.115 meV,
    # ceiling 7.0). ``None`` means undeclared, and an undeclared identity domain is NEVER silently
    # widened - see ``acquire.identity_domain``.
    identity_search_meV: tuple[float, float] | None = None
    # Who chose each band: "operator" (explicitly requested) or "compiler" (a shipped default).
    # Recorded rather than inferred, because a band that merely *equals* the shipped default proves
    # nothing about who chose it, and widening a scientist's declared range is not ours to do.
    target_search_source: str | None = None
    identity_search_source: str | None = None
    # The sample temperature this campaign is measuring at, in kelvin. Belief evidence is stored per
    # temperature bin, so declaring it is what lets the controller pick the right slice instead of
    # guessing when more than one is complete. ``None`` is fine while only one slice exists.
    target_temperature_K: float | None = None
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    target_hypothesis: str | None = None
    hypothesis_source: str | None = None
    hypothesis_fingerprint: str | None = None
    steering: dict[str, Any] = field(default_factory=dict)
    scan_window_eig: ScanWindowEIGConfig | Mapping[str, Any] = field(
        default_factory=ScanWindowEIGConfig)
    analysis_limit_seconds: float | None | object = _DEFAULT_ANALYSIS_LIMIT

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier {self.tier!r} not in {TIERS}")
        if self.target_branch not in TARGET_BRANCHES:
            raise ValueError(f"target_branch {self.target_branch!r} not in {TARGET_BRANCHES}")
        if self.target_energy_meV is not None:
            self.target_energy_meV = float(self.target_energy_meV)
            if not math.isfinite(self.target_energy_meV):
                raise ValueError("target_energy_meV must be finite")
        if self.target_selector is None:
            self.target_selector = ("nearest_energy" if self.target_energy_meV is not None
                                    else "lowest_energy")
        if self.target_selector not in TARGET_SELECTORS:
            raise ValueError(f"target_selector {self.target_selector!r} not in {TARGET_SELECTORS}")
        if self.target_selector == "nearest_energy" and self.target_energy_meV is None:
            raise ValueError("nearest_energy target_selector requires target_energy_meV")
        # A tracked selector follows one connected physical branch along the objective path.  Both
        # shipped branch identities are supported; local lowest/highest/nearest selectors remain
        # available when independent per-q feature selection is what the operator requested.
        # Normalize path entries to plain float triples so round-trips are stable.
        norm: list[tuple[float, float, float]] = []
        for p in self.path:
            if not isinstance(p, (list, tuple)) or len(p) != 3:
                raise ValueError("every objective path point must contain exactly three coordinates")
            h, k, l = p
            # Round to 9 dp so grid arithmetic (e.g. 2.1 + 0.1·2) does not leave 2.3000000000000003.
            norm.append((round(float(h), 9), round(float(k), 9), round(float(l), 9)))
        self.path = norm
        if not self.path:
            raise ValueError("path must contain at least one Q point")
        if self.budget is not None:
            if (not isinstance(self.budget, dict) or "kind" not in self.budget
                    or "amount" not in self.budget):
                raise ValueError("budget must be None or a dict with 'kind' and 'amount' keys")
            self.budget = dict(self.budget)
        elif self.tier != "deterministic":
            self.budget = {"kind": "counting_seconds",
                           "amount": default_budget_hours(self.tier) * 3600.0}
        # NOTE a deterministic-tier spec constructed here with ``budget=None`` keeps it: at this
        # layer ``None`` is an explicit "unlimited", and the loop honours it. The canonical tier
        # default is applied one layer up, where an *undeclared* budget is distinguishable from a
        # deliberately unlimited one (:func:`isar.drive.supervisor.goal_to_spec._budget`), which is
        # the path every goal and spec file takes.
        if self.analysis_limit_seconds is _DEFAULT_ANALYSIS_LIMIT:
            self.analysis_limit_seconds = 3600.0 if self.tier == "deterministic" else None
        elif self.analysis_limit_seconds is not None:
            self.analysis_limit_seconds = float(self.analysis_limit_seconds)
            if not math.isfinite(self.analysis_limit_seconds) or self.analysis_limit_seconds <= 0.0:
                raise ValueError("analysis_limit_seconds must be positive or None")
        # An analysis guardrail longer than the whole counting-time budget is not a guardrail: the
        # campaign would stop on beam time with the analysis clock still nominally open, and the
        # operator's "one hour" would silently mean "one hour of counting plus an hour of fitting".
        # Clamp it to the budget on the deterministic tier, where both clocks are the same wall time.
        if (self.tier == "deterministic" and self.analysis_limit_seconds is not None
                and isinstance(self.budget, dict)
                and self.budget.get("kind") == "counting_seconds"
                and self.budget.get("amount") is not None):
            total = float(self.budget["amount"])
            if math.isfinite(total) and 0.0 < total < float(self.analysis_limit_seconds):
                log.info("clamping analysis_limit_seconds %.1fs to the %.1fs counting-time budget",
                         float(self.analysis_limit_seconds), total)
                self.analysis_limit_seconds = total
        if self.target_temperature_K is not None:
            self.target_temperature_K = float(self.target_temperature_K)
            if not math.isfinite(self.target_temperature_K) or self.target_temperature_K <= 0.0:
                raise ValueError("target_temperature_K must be a positive, finite temperature in K")
        if self.energy_search_meV is None:
            self.energy_search_meV = (-2.0, 12.0) if self.target_branch == "optic" else (-2.0, 7.0)
        else:
            lo, hi = self.energy_search_meV
            self.energy_search_meV = (float(lo), float(hi))
            if not self.energy_search_meV[0] < self.energy_search_meV[1]:
                raise ValueError("energy_search_meV must be [low, high] with low < high")
        if self.identity_search_meV is not None:
            # Stays exactly what was declared — never normalized to a fallback. Filling it in here
            # would make ``dataclasses.replace()`` read the fallback back as a declaration, turning
            # "never declared" into "declared exactly this" and silently retiring the policy-forbidden
            # report that tells an operator identity work was blocked. Callers wanting the effective
            # band use :attr:`effective_identity_search_meV`.
            lo, hi = self.identity_search_meV
            self.identity_search_meV = (float(lo), float(hi))
            if not self.identity_search_meV[0] < self.identity_search_meV[1]:
                raise ValueError("identity_search_meV must be [low, high] with low < high")
        for name in ("target_search_source", "identity_search_source"):
            value = getattr(self, name)
            if value is not None and value not in ("operator", "compiler"):
                raise ValueError(f"{name} must be 'operator', 'compiler', or None")
        self.target_label = str(self.target_label or self.target_branch)
        self.hypotheses = [dict(h) for h in self.hypotheses]
        self.steering = dict(self.steering)
        if isinstance(self.scan_window_eig, Mapping):
            self.scan_window_eig = ScanWindowEIGConfig.from_dict(self.scan_window_eig)
        elif isinstance(self.scan_window_eig, ScanWindowEIGConfig):
            self.scan_window_eig = ScanWindowEIGConfig.from_dict(self.scan_window_eig.to_dict())
        else:
            raise TypeError("scan_window_eig must be a frozen ScanWindowEIGConfig")
        # A full frozen objective is a campaign-start contract, not merely a policy container.
        # Validate every currently supported scientific policy before beamtime starts.  Unknown
        # versioned policy kinds remain authenticated and are refused diagnostically at runtime.
        policy_set = self.scan_window_eig.policy_set
        if policy_set is not None:
            from isar.eig.policy import compile_qoi_distinction_policy
            for policy in policy_set.policies:
                if policy.policy_kind == "isar.scalar_absolute_partitions/o36.5":
                    for variant in policy.variants:
                        compile_qoi_distinction_policy(policy, variant)
        if self.steering.get("scan_window_eig_mode", "off") == "authoritative":
            from isar.eig.o41_authority import validate_o41_authoritative_profile
            validate_o41_authoritative_profile(self.scan_window_eig)
        if self.hypotheses:
            names: set[str] = set()
            for hypothesis in self.hypotheses:
                name = hypothesis.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("every frozen hypothesis must have a non-empty string name")
                if name in names:
                    raise ValueError(f"duplicate frozen hypothesis name {name!r}")
                names.add(name)
            if not self.target_hypothesis or self.target_hypothesis not in names:
                raise ValueError("target_hypothesis must name one of the frozen hypotheses")
            supplied_steering = dict(self.steering)
            SteeringPolicy(**supplied_steering)
            # A frozen objective's hash covers the exact steering dictionary written at compile time.
            # Validate that historical dictionary with current typed defaults, but do not expand it:
            # adding a later runtime default must not invalidate or silently rewrite an old snapshot.
            expected = content_fingerprint(self.hypotheses, supplied_steering)
            if self.hypothesis_fingerprint is not None and self.hypothesis_fingerprint != expected:
                raise ValueError("hypothesis_fingerprint does not match the frozen hypothesis snapshot")
            self.steering = supplied_steering
            self.hypothesis_fingerprint = expected
        elif any((self.target_hypothesis, self.hypothesis_source, self.hypothesis_fingerprint,
                  self.steering)):
            raise ValueError("hypothesis metadata cannot exist without frozen hypotheses")

    # ---- search domains ----------------------------------------------------
    @property
    def target_search_meV(self) -> tuple[float, float]:
        """Where the target mode is expected — the band that bounds delivery and precision work.

        The stored field keeps its original name ``energy_search_meV`` so the frozen contract stays
        additive (DECISIONS O12); this is the vocabulary that distinguishes it from the identity
        domain now that the two are no longer the same thing.
        """
        return self.energy_search_meV

    @property
    def effective_identity_search_meV(self) -> tuple[float, float]:
        """The identity band actually in force: the declared one, else the target band.

        The stored ``identity_search_meV`` field is left as ``None`` when nothing was declared, so
        "declared, and happens to equal the target band" stays distinguishable from "never declared"
        through every copy, ``replace()`` and round-trip. That distinction is what the whole migration
        rule turns on, so it must live in the field rather than in a derived flag.
        """
        return self.identity_search_meV or self.energy_search_meV

    # ---- constructors ------------------------------------------------------
    @classmethod
    def along_h(cls, *, h_start: float, h_end: float, step: float, k: float = 0.0, l: float = 0.0,
                tolerance_meV: float, budget: dict[str, Any] | None = None,
                tier: str = "deterministic",
                name: str = "", meta: dict[str, Any] | None = None,
                target_branch: str = "acoustic", target_label: str = "acoustic",
                target_selector: str | None = None, target_energy_meV: float | None = None,
                energy_search_meV: tuple[float, float] | None = None,
                identity_search_meV: tuple[float, float] | None = None,
                target_search_source: str | None = None,
                identity_search_source: str | None = None,
                target_temperature_K: float | None = None,
                hypotheses: list[dict[str, Any]] | None = None,
                target_hypothesis: str | None = None, hypothesis_source: str | None = None,
                hypothesis_fingerprint: str | None = None,
                steering: dict[str, Any] | None = None,
                scan_window_eig: ScanWindowEIGConfig | Mapping[str, Any] | None = None,
                analysis_limit_seconds: float | None | object = _DEFAULT_ANALYSIS_LIMIT) -> "ObjectiveSpec":
        """A path that walks H from ``h_start`` to ``h_end`` in increments of ``step`` at fixed K, L.

        The endpoint is included when it lands on the grid (within a tolerance of ``step``). ``step``
        must be positive; the direction is taken from the sign of ``h_end - h_start``.
        """
        if step <= 0:
            raise ValueError("step must be positive")
        span = h_end - h_start
        n = int(round(abs(span) / step))
        sign = 1.0 if span >= 0 else -1.0
        pts = [(h_start + sign * step * i, k, l) for i in range(n + 1)]
        return cls(path=pts, tolerance_meV=float(tolerance_meV),
                   budget=(dict(budget) if budget is not None else None),
                   tier=tier, name=name, meta=dict(meta or {}), target_branch=target_branch,
                   target_label=target_label, target_selector=target_selector,
                   target_energy_meV=target_energy_meV, energy_search_meV=energy_search_meV,
                   identity_search_meV=identity_search_meV,
                   target_search_source=target_search_source,
                   identity_search_source=identity_search_source,
                   target_temperature_K=target_temperature_K,
                   hypotheses=list(hypotheses or []), target_hypothesis=target_hypothesis,
                   hypothesis_source=hypothesis_source,
                   hypothesis_fingerprint=hypothesis_fingerprint,
                   steering=dict(steering or {}),
                   scan_window_eig=(ScanWindowEIGConfig() if scan_window_eig is None
                                    else scan_window_eig),
                   analysis_limit_seconds=analysis_limit_seconds)

    @classmethod
    def segment(cls, *, q_start, q_end, step_rlu: float, tolerance_meV: float,
                budget: dict[str, Any] | None = None, tier: str = "deterministic", name: str = "",
                meta: dict[str, Any] | None = None, target_branch: str = "acoustic",
                target_label: str = "acoustic",
                target_selector: str | None = None, target_energy_meV: float | None = None,
                energy_search_meV: tuple[float, float] | None = None,
                identity_search_meV: tuple[float, float] | None = None,
                target_search_source: str | None = None,
                identity_search_source: str | None = None,
                target_temperature_K: float | None = None,
                hypotheses: list[dict[str, Any]] | None = None,
                target_hypothesis: str | None = None, hypothesis_source: str | None = None,
                hypothesis_fingerprint: str | None = None,
                steering: dict[str, Any] | None = None,
                scan_window_eig: ScanWindowEIGConfig | Mapping[str, Any] | None = None,
                analysis_limit_seconds: float | None | object = _DEFAULT_ANALYSIS_LIMIT) -> "ObjectiveSpec":
        """A straight reciprocal-space segment whose intervals never exceed ``step_rlu``.

        Unlike :meth:`along_h`, this uses ``ceil`` and evenly divides the complete vector so the
        requested endpoint is always included.  A degenerate segment intentionally remains a valid
        one-point explicit objective; the natural-language compiler applies its stricter guard.
        """
        if step_rlu <= 0:
            raise ValueError("step_rlu must be positive")
        start = tuple(float(v) for v in q_start)
        end = tuple(float(v) for v in q_end)
        if len(start) != 3 or len(end) != 3:
            raise ValueError("q_start and q_end must each contain three coordinates")
        delta = tuple(b - a for a, b in zip(start, end))
        distance = math.sqrt(sum(d * d for d in delta))
        n = max(1, int(math.ceil(distance / float(step_rlu)))) if distance else 0
        pts = ([start] if n == 0 else
               [tuple(a + d * (i / n) for a, d in zip(start, delta)) for i in range(n + 1)])
        return cls(path=pts, tolerance_meV=float(tolerance_meV),
                   budget=(dict(budget) if budget is not None else None), tier=tier,
                   name=name, meta=dict(meta or {}), target_branch=target_branch,
                   target_label=target_label, target_selector=target_selector,
                   target_energy_meV=target_energy_meV, energy_search_meV=energy_search_meV,
                   identity_search_meV=identity_search_meV,
                   target_search_source=target_search_source,
                   identity_search_source=identity_search_source,
                   target_temperature_K=target_temperature_K,
                   hypotheses=list(hypotheses or []), target_hypothesis=target_hypothesis,
                   hypothesis_source=hypothesis_source,
                   hypothesis_fingerprint=hypothesis_fingerprint,
                   steering=dict(steering or {}),
                   scan_window_eig=(ScanWindowEIGConfig() if scan_window_eig is None
                                    else scan_window_eig),
                   analysis_limit_seconds=analysis_limit_seconds)

    # ---- (de)serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OBJECTIVE_SCHEMA,
            "path": [list(p) for p in self.path],
            "tolerance_meV": self.tolerance_meV,
            "budget": (dict(self.budget) if self.budget is not None else None),
            "analysis_limit_seconds": self.analysis_limit_seconds,
            "tier": self.tier,
            "name": self.name,
            "meta": dict(self.meta),
            "target_branch": self.target_branch,
            "target_label": self.target_label,
            "target_selector": self.target_selector,
            "target_energy_meV": self.target_energy_meV,
            "energy_search_meV": list(self.energy_search_meV),
            "identity_search_meV": (list(self.identity_search_meV)
                                    if self.identity_search_meV is not None else None),
            "target_search_source": self.target_search_source,
            "identity_search_source": self.identity_search_source,
            "target_temperature_K": self.target_temperature_K,
            "hypotheses": [dict(h) for h in self.hypotheses],
            "target_hypothesis": self.target_hypothesis,
            "hypothesis_source": self.hypothesis_source,
            "hypothesis_fingerprint": self.hypothesis_fingerprint,
            "steering": dict(self.steering),
            "scan_window_eig": self.scan_window_eig.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ObjectiveSpec":
        if d.get("schema") != OBJECTIVE_SCHEMA:
            raise ValueError(f"objective schema must be {OBJECTIVE_SCHEMA!r}")
        unknown = set(d) - ({"schema"} | set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"full frozen objective fields are invalid: {sorted(unknown)}")
        return cls(
            path=[tuple(p) for p in d["path"]],
            tolerance_meV=float(d["tolerance_meV"]),
            budget=(dict(d["budget"]) if d.get("budget") is not None else None),
            tier=d.get("tier", "deterministic"),
            name=d.get("name", ""),
            meta=dict(d.get("meta", {})),
            target_branch=d.get("target_branch", "acoustic"),
            target_label=d.get("target_label", d.get("target_branch", "acoustic")),
            target_selector=d.get("target_selector"),
            target_energy_meV=d.get("target_energy_meV"),
            energy_search_meV=(tuple(d["energy_search_meV"])
                               if d.get("energy_search_meV") is not None else None),
            identity_search_meV=(tuple(d["identity_search_meV"])
                                 if d.get("identity_search_meV") is not None else None),
            target_search_source=d.get("target_search_source"),
            identity_search_source=d.get("identity_search_source"),
            target_temperature_K=d.get("target_temperature_K"),
            hypotheses=list(d.get("hypotheses") or []),
            target_hypothesis=d.get("target_hypothesis"),
            hypothesis_source=d.get("hypothesis_source"),
            hypothesis_fingerprint=d.get("hypothesis_fingerprint"),
            steering=dict(d.get("steering") or {}),
            # ``scan_window_eig`` was added to objective/2 without minting a new objective schema.
            # Old frozen objective/2 records therefore read as the frozen no-policy/default-scoring
            # value.  New writers still materialize the block through ``to_dict``; an explicit null
            # remains invalid and is deliberately not treated as omission.
            scan_window_eig=(
                ScanWindowEIGConfig() if "scan_window_eig" not in d else d["scan_window_eig"]),
            analysis_limit_seconds=(d["analysis_limit_seconds"]
                                    if "analysis_limit_seconds" in d else _DEFAULT_ANALYSIS_LIMIT),
        )


def current_objective_comparison_scope(objective: ObjectiveSpec) -> Mapping[str, str]:
    """Adapt the scientific objective to the stable campaign-policy applicability identity.

    Steering and ``scan_window_eig`` are deliberately excluded.  A policy can therefore bind to
    this exact identity and then be embedded in the objective without a recursive fingerprint.
    """
    from isar.schema.statistics import semantics_fingerprint

    if not isinstance(objective, ObjectiveSpec):
        raise TypeError("objective must be an ObjectiveSpec")
    raw = objective.to_dict()
    if raw.get("schema") != OBJECTIVE_SCHEMA:
        raise ValueError("objective schema is unsupported")
    payload = {
        "schema": raw["schema"],
        "path": raw["path"],
        "tolerance_meV": raw["tolerance_meV"],
        "target_branch": raw["target_branch"],
        "target_label": raw["target_label"],
        "target_selector": raw["target_selector"],
        "target_energy_meV": raw["target_energy_meV"],
        "energy_search_meV": raw["energy_search_meV"],
        "identity_search_meV": raw["identity_search_meV"],
        "target_temperature_K": raw["target_temperature_K"],
        "hypotheses": raw["hypotheses"],
        "target_hypothesis": raw["target_hypothesis"],
        "hypothesis_fingerprint": raw["hypothesis_fingerprint"],
    }
    return {
        "producer_revision": CURRENT_OBJECTIVE_SCOPE_REVISION,
        "fingerprint": semantics_fingerprint(CURRENT_OBJECTIVE_SCOPE_REVISION, payload),
    }


def shipped_hypothesis_snapshot(target_branch: str, energy_search_meV) -> dict[str, Any]:
    """Freeze the shipped target plus its acoustic/optic rival for a newly compiled goal."""
    energy = tuple(energy_search_meV or ((-2.0, 12.0) if target_branch == "optic" else (-2.0, 7.0)))
    names = [target_branch] + [name for name in ("acoustic", "optic") if name != target_branch]
    hypotheses = [preset(name, energy).to_dict() for name in names]
    steering = SteeringPolicy(dynamic_ncount=True).to_dict()
    return {"hypotheses": hypotheses, "target_hypothesis": target_branch,
            "hypothesis_source": "shipped_preset",
            "hypothesis_fingerprint": content_fingerprint(hypotheses, steering),
            "steering": steering}


def resolve_hypothesis_snapshot(target_branch: str, energy_search_meV, *,
                                card_hypotheses: dict[str, Any] | None = None,
                                goal_override: dict[str, Any] | None = None,
                                steering_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Freeze goal > card > shipped declarations, including all declared rival branches.

    The returned source names the declaration that won for the target.  Rivals from the card remain
    available to classification even when a goal override replaces only the target declaration.
    """
    energy = tuple(energy_search_meV or (
        (-2.0, 12.0) if target_branch == "optic" else (-2.0, 7.0)))
    declarations = {str(name): dict(value or {})
                    for name, value in dict(card_hypotheses or {}).items()}
    source = "card_policy" if target_branch in declarations else "shipped_preset"
    if goal_override:
        declarations[target_branch] = {**declarations.get(target_branch, {}), **dict(goal_override)}
        source = "goal_override"

    ordered_names = [target_branch]
    ordered_names.extend(name for name in declarations if name != target_branch)
    # Always carry the standard acoustic/optic rival unless the scientist supplied an equivalent
    # custom set.  This is a prior with zero real occupancy, not fabricated classification evidence.
    ordered_names.extend(name for name in ("acoustic", "optic") if name not in ordered_names)
    hypotheses = [resolve_hypothesis(
        name, declarations.get(name), energy_search_meV=energy).to_dict()
        for name in ordered_names]
    steering_values = {"dynamic_ncount": True, **dict(steering_override or {})}
    steering = SteeringPolicy(**steering_values).to_dict()
    return {"hypotheses": hypotheses, "target_hypothesis": target_branch,
            "hypothesis_source": source,
            "hypothesis_fingerprint": content_fingerprint(hypotheses, steering),
            "steering": steering}
