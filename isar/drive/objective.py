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

# The two fidelity tiers a driver campaign can run against (LLM_HARNESS_DESIGN §6). "deterministic"
# is TAVI's fast analytic engine (develop the loop); "mcstas" is the full Monte-Carlo (validate).
TIERS = ("deterministic", "mcstas")
TARGET_BRANCHES = ("acoustic", "optic")


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
    energy_search_meV: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise ValueError(f"tier {self.tier!r} not in {TIERS}")
        if self.target_branch not in TARGET_BRANCHES:
            raise ValueError(f"target_branch {self.target_branch!r} not in {TARGET_BRANCHES}")
        # Normalize path entries to plain float triples so round-trips are stable.
        norm: list[tuple[float, float, float]] = []
        for p in self.path:
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

    # ---- constructors ------------------------------------------------------
    @classmethod
    def along_h(cls, *, h_start: float, h_end: float, step: float, k: float = 0.0, l: float = 0.0,
                tolerance_meV: float, budget: dict[str, Any], tier: str = "deterministic",
                name: str = "", meta: dict[str, Any] | None = None,
                target_branch: str = "acoustic", target_label: str = "acoustic",
                energy_search_meV: tuple[float, float] | None = None) -> "ObjectiveSpec":
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
                   target_label=target_label, energy_search_meV=energy_search_meV)

    @classmethod
    def segment(cls, *, q_start, q_end, step_rlu: float, tolerance_meV: float,
                budget: dict[str, Any], tier: str = "deterministic", name: str = "",
                meta: dict[str, Any] | None = None, target_branch: str = "acoustic",
                target_label: str = "acoustic",
                energy_search_meV: tuple[float, float] | None = None) -> "ObjectiveSpec":
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
                   target_label=target_label, energy_search_meV=energy_search_meV)

    # ---- (de)serialization -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "path": [list(p) for p in self.path],
            "tolerance_meV": self.tolerance_meV,
            "budget": dict(self.budget),
            "tier": self.tier,
            "name": self.name,
            "meta": dict(self.meta),
            "target_branch": self.target_branch,
            "target_label": self.target_label,
            "energy_search_meV": list(self.energy_search_meV),
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
            target_branch=d.get("target_branch", "acoustic"),
            target_label=d.get("target_label", d.get("target_branch", "acoustic")),
            energy_search_meV=(tuple(d["energy_search_meV"])
                               if d.get("energy_search_meV") is not None else None),
        )
