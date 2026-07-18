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

import math
from dataclasses import dataclass, field
from typing import Any

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
TARGET_SELECTORS = ("lowest_energy", "highest_energy", "nearest_energy")
OBJECTIVE_SCHEMA = "isar.objective/2"


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
    budget: dict[str, Any]
    tier: str = "deterministic"
    name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    target_branch: str = "acoustic"
    target_label: str = "acoustic"
    target_selector: str | None = None
    target_energy_meV: float | None = None
    energy_search_meV: tuple[float, float] | None = None
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    target_hypothesis: str | None = None
    hypothesis_source: str | None = None
    hypothesis_fingerprint: str | None = None
    steering: dict[str, Any] = field(default_factory=dict)

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
        if not isinstance(self.budget, dict) or "kind" not in self.budget or "amount" not in self.budget:
            raise ValueError("budget must be a dict with 'kind' and 'amount' keys")
        if self.energy_search_meV is None:
            self.energy_search_meV = (-2.0, 12.0) if self.target_branch == "optic" else (-2.0, 7.0)
        else:
            lo, hi = self.energy_search_meV
            self.energy_search_meV = (float(lo), float(hi))
            if not self.energy_search_meV[0] < self.energy_search_meV[1]:
                raise ValueError("energy_search_meV must be [low, high] with low < high")
        self.target_label = str(self.target_label or self.target_branch)
        self.hypotheses = [dict(h) for h in self.hypotheses]
        self.steering = dict(self.steering)
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
            policy = SteeringPolicy(**supplied_steering)
            # v2 objective snapshots created before dynamic counting contain the complete old policy
            # dictionary and its fingerprint.  Validate those values without expanding the dictionary;
            # newly compiled objectives explicitly carry dynamic_ncount and freeze the complete policy.
            self.steering = (policy.to_dict() if "dynamic_ncount" in supplied_steering
                             else supplied_steering)
            expected = content_fingerprint(self.hypotheses, self.steering)
            if self.hypothesis_fingerprint is not None and self.hypothesis_fingerprint != expected:
                raise ValueError("hypothesis_fingerprint does not match the frozen hypothesis snapshot")
            self.hypothesis_fingerprint = expected
        elif any((self.target_hypothesis, self.hypothesis_source, self.hypothesis_fingerprint,
                  self.steering)):
            raise ValueError("hypothesis metadata cannot exist without frozen hypotheses")

    # ---- constructors ------------------------------------------------------
    @classmethod
    def along_h(cls, *, h_start: float, h_end: float, step: float, k: float = 0.0, l: float = 0.0,
                tolerance_meV: float, budget: dict[str, Any], tier: str = "deterministic",
                name: str = "", meta: dict[str, Any] | None = None,
                target_branch: str = "acoustic", target_label: str = "acoustic",
                target_selector: str | None = None, target_energy_meV: float | None = None,
                energy_search_meV: tuple[float, float] | None = None,
                hypotheses: list[dict[str, Any]] | None = None,
                target_hypothesis: str | None = None, hypothesis_source: str | None = None,
                hypothesis_fingerprint: str | None = None,
                steering: dict[str, Any] | None = None) -> "ObjectiveSpec":
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
        return cls(path=pts, tolerance_meV=float(tolerance_meV), budget=dict(budget),
                   tier=tier, name=name, meta=dict(meta or {}), target_branch=target_branch,
                   target_label=target_label, target_selector=target_selector,
                   target_energy_meV=target_energy_meV, energy_search_meV=energy_search_meV,
                   hypotheses=list(hypotheses or []), target_hypothesis=target_hypothesis,
                   hypothesis_source=hypothesis_source,
                   hypothesis_fingerprint=hypothesis_fingerprint,
                   steering=dict(steering or {}))

    @classmethod
    def segment(cls, *, q_start, q_end, step_rlu: float, tolerance_meV: float,
                budget: dict[str, Any], tier: str = "deterministic", name: str = "",
                meta: dict[str, Any] | None = None, target_branch: str = "acoustic",
                target_label: str = "acoustic",
                target_selector: str | None = None, target_energy_meV: float | None = None,
                energy_search_meV: tuple[float, float] | None = None,
                hypotheses: list[dict[str, Any]] | None = None,
                target_hypothesis: str | None = None, hypothesis_source: str | None = None,
                hypothesis_fingerprint: str | None = None,
                steering: dict[str, Any] | None = None) -> "ObjectiveSpec":
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
        return cls(path=pts, tolerance_meV=float(tolerance_meV), budget=dict(budget), tier=tier,
                   name=name, meta=dict(meta or {}), target_branch=target_branch,
                   target_label=target_label, target_selector=target_selector,
                   target_energy_meV=target_energy_meV, energy_search_meV=energy_search_meV,
                   hypotheses=list(hypotheses or []), target_hypothesis=target_hypothesis,
                   hypothesis_source=hypothesis_source,
                   hypothesis_fingerprint=hypothesis_fingerprint,
                   steering=dict(steering or {}))

    # ---- (de)serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OBJECTIVE_SCHEMA,
            "path": [list(p) for p in self.path],
            "tolerance_meV": self.tolerance_meV,
            "budget": dict(self.budget),
            "tier": self.tier,
            "name": self.name,
            "meta": dict(self.meta),
            "target_branch": self.target_branch,
            "target_label": self.target_label,
            "target_selector": self.target_selector,
            "target_energy_meV": self.target_energy_meV,
            "energy_search_meV": list(self.energy_search_meV),
            "hypotheses": [dict(h) for h in self.hypotheses],
            "target_hypothesis": self.target_hypothesis,
            "hypothesis_source": self.hypothesis_source,
            "hypothesis_fingerprint": self.hypothesis_fingerprint,
            "steering": dict(self.steering),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ObjectiveSpec":
        if d.get("schema") != OBJECTIVE_SCHEMA:
            raise ValueError(f"objective schema must be {OBJECTIVE_SCHEMA!r}")
        return cls(
            path=[tuple(p) for p in d["path"]],
            tolerance_meV=float(d["tolerance_meV"]),
            budget=dict(d["budget"]),
            tier=d.get("tier", "deterministic"),
            name=d.get("name", ""),
            meta=dict(d.get("meta", {})),
            target_branch=d.get("target_branch", "acoustic"),
            target_label=d.get("target_label", d.get("target_branch", "acoustic")),
            target_selector=d.get("target_selector"),
            target_energy_meV=d.get("target_energy_meV"),
            energy_search_meV=(tuple(d["energy_search_meV"])
                               if d.get("energy_search_meV") is not None else None),
            hypotheses=list(d.get("hypotheses") or []),
            target_hypothesis=d.get("target_hypothesis"),
            hypothesis_source=d.get("hypothesis_source"),
            hypothesis_fingerprint=d.get("hypothesis_fingerprint"),
            steering=dict(d.get("steering") or {}),
        )


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
