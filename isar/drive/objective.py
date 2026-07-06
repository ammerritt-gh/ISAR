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

from dataclasses import dataclass, field
from typing import Any

# The two fidelity tiers a driver campaign can run against (LLM_HARNESS_DESIGN §6). "deterministic"
# is TAVI's fast analytic engine (develop the loop); "mcstas" is the full Monte-Carlo (validate).
TIERS = ("deterministic", "mcstas")


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

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier {self.tier!r} not in {TIERS}")
        # Normalize path entries to plain float triples so round-trips are stable.
        norm: list[tuple[float, float, float]] = []
        for p in self.path:
            h, k, l = p
            norm.append((float(h), float(k), float(l)))
        self.path = norm
        if not isinstance(self.budget, dict) or "kind" not in self.budget or "amount" not in self.budget:
            raise ValueError("budget must be a dict with 'kind' and 'amount' keys")

    # ---- constructors ------------------------------------------------------
    @classmethod
    def along_h(cls, *, h_start: float, h_end: float, step: float, k: float = 0.0, l: float = 0.0,
                tolerance_meV: float, budget: dict[str, Any], tier: str = "deterministic",
                name: str = "", meta: dict[str, Any] | None = None) -> "ObjectiveSpec":
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
                   tier=tier, name=name, meta=dict(meta or {}))

    # ---- (de)serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "path": [list(p) for p in self.path],
            "tolerance_meV": self.tolerance_meV,
            "budget": dict(self.budget),
            "tier": self.tier,
            "name": self.name,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ObjectiveSpec":
        return cls(
            path=[tuple(p) for p in d["path"]],
            tolerance_meV=float(d["tolerance_meV"]),
            budget=dict(d["budget"]),
            tier=d.get("tier", "deterministic"),
            name=d.get("name", ""),
            meta=dict(d.get("meta", {})),
        )
