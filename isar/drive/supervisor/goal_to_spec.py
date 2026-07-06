"""Goal → objective spec (STUB — LLM_HARNESS_DESIGN §2.1).

Contract: translate a natural-language goal ("map the acoustic phonon dispersion from A to B,
resolve ω₀ to ±0.1 meV, overnight") into a machine :class:`~isar.drive.objective.ObjectiveSpec`
— the reciprocal-space path, the per-point tolerance on ω₀, the time/neutron budget, and the tier.
This is a judgment task (parse free text, apply physics/world knowledge), so it runs through the LLM
supervisor via the shared :class:`isar.reduce.llm_client.Dispatcher`, with the ObjectiveSpec schema
enforced as structured output. Deterministic construction (no free text) is already available via
:meth:`ObjectiveSpec.along_h`.
"""
from __future__ import annotations

from isar.drive.objective import ObjectiveSpec


def goal_to_spec(goal: str, *, dispatcher=None, defaults: dict | None = None) -> ObjectiveSpec:
    """Parse ``goal`` into an :class:`ObjectiveSpec` (NOT YET IMPLEMENTED)."""
    raise NotImplementedError(
        "goal→ObjectiveSpec is the LLM supervisor's job (next round); see LLM_HARNESS_DESIGN §2.1. "
        "Build a spec deterministically with ObjectiveSpec.along_h in the meantime."
    )
