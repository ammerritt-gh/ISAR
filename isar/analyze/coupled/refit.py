"""The coupled refit's entry points (C-refit). S1U1 fixes the signatures; S1U2 fills the bodies."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Literal

from isar.analyze.coupled.settings import InterSettings
from isar.analyze.coupled.types import (Assignment, Dormant, Held, Priced, PricingJob, RefitResult, Snapshot)

if TYPE_CHECKING:
    from isar.analyze.coupled.engine import Factor
    from isar.session.inter_spectra.graph import Link


def held_from_fit(record: dict, *, unit: str, fit: str, inputs: str | None, context, T: float | None,
                  branch_ids: tuple[str, ...] | None = None, dormant: tuple[Dormant, ...] = ()) -> Held:
    raise NotImplementedError("stub: filled by S1U2")


def assign_jobs(snapshot: Snapshot, *, cached: Mapping[str, Priced] = {}) -> tuple[Assignment | None, tuple[PricingJob, ...]]:
    raise NotImplementedError("stub: filled by S1U2")


def price(snapshot: Snapshot, job: PricingJob) -> Priced:
    raise NotImplementedError("stub: filled by S1U2")


def assign_decide(snapshot: Snapshot, priced: Sequence[Priced]) -> Assignment:
    raise NotImplementedError("stub: filled by S1U2")


def assign(snapshot: Snapshot, *, cached: Mapping[str, Priced] = {}) -> tuple[Assignment, tuple[Priced, ...]]:
    raise NotImplementedError("stub: filled by S1U2")


def refit(snapshot: Snapshot, *, stage: Literal["continuous", "both"], assignment: Assignment | None = None,
          cached: Mapping[str, Priced] = {}, workers: int = 1) -> RefitResult:
    raise NotImplementedError("stub: filled by S1U2")


def total_objective(factors: Iterable[Factor], energies: Mapping[tuple[str, str], float],
                    data: Mapping[str, float], unary: Mapping[str, float]) -> tuple[float, dict[str, float]]:
    raise NotImplementedError("stub: filled by S1U2")


def seam_report(a: RefitResult, b: RefitResult, link: Link, settings: InterSettings) -> dict:
    raise NotImplementedError("stub: filled by S1U2")
