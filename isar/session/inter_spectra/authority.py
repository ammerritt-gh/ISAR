"""Which fit of a spectrum is in use, and what the readers see (C-authority). Pure; no I/O in ``resolve``.

S1U1 writes the types and stubs and the overlay key list; S1U5 fills the resolver and the readers. The
resolver is the one rule every reader uses, so the GUI, export and report cannot disagree.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from isar.intake.inter_spectra import InterUser, LockState, Pick
from isar.session.inter_spectra.records import InterLog, RefitLine

# The stored overlay's keys, redrawn in place of the solo fit's (C-authority, N3)
OVERLAY_KEYS = ("fast_pass", "modes", "model_manifest", "omega0", "best_label", "n_modes", "interval")


@dataclass(frozen=True)
class Stale:
    code: str; unit: str                  # "inputs_changed" (unit = the spectrum) | "neighbour_inputs_changed" (unit = the neighbour)


@dataclass(frozen=True)
class Resolution:
    unit: str
    fit: str | None            # the fit in use: "fast" | f-id | None (no solo fit and nothing chosen)
    source: str                # "fast_solo" | "batch" | "pick" | "lock" | "fallback"
    chosen: str | None         # what picks/acceptances/lock selected before the stale test ("fast" if nothing)
    locked: bool; lock_fit: str | None
    stale: Stale | None        # staleness of `chosen` (a refit); None for fast, or when it is not stale
    conditions: bool          # may this spectrum's fit condition a new fit? False only for a locked, stale fit
    notes: tuple[str, ...]     # dangling pick / lock naming a missing fit / inputs not judged


@dataclass(frozen=True)
class Effective:
    unit: str; outcome: dict; resolution: Resolution; label: str; line: RefitLine | None


def resolve(unit: str, *, refits: Sequence[RefitLine], pick: Pick | None, lock: LockState | None,
            has_fast: bool, inputs_now: Mapping[str, str | None]) -> Resolution:
    raise NotImplementedError("stub: filled by S1U5")


def resolve_all(log: InterLog, user: InterUser, inputs_now: Mapping[str, str | None],
                fast_units: Collection[str]) -> dict[str, Resolution]:
    raise NotImplementedError("stub: filled by S1U5")


def effective(root, outcomes: Mapping[str, dict], tabulated, rows=None, index=None) -> dict[str, Effective]:
    # one entry per unit with any refit; units absent from the result are their solo fit. Used by export and report.
    raise NotImplementedError("stub: filled by S1U5")


def overlay_outcome(oc: dict, overlay: dict) -> dict:           # {**oc} with OVERLAY_KEYS from overlay, solo verdict keys None
    raise NotImplementedError("stub: filled by S1U5")


def fit_label(line: RefitLine | None, batches) -> str:           # "Fast solo" | "Batch 2 · ring 1" | "Batch 3 · sweep 4"
    raise NotImplementedError("stub: filled by S1U5")
