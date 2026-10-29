"""The user's inter-spectra values, read from ``intake/inter_spectra.jsonl`` (C-settings-store). Qt-free.

Anchors, picks, locks and the inter-spectra settings are user values: ISAR never writes this file (AGENTS.md
hard rule 1). The checked writers are ``isar.intake.inter_values``; ``read_user`` folds their delta lines.
"""
from __future__ import annotations

from dataclasses import dataclass

from isar.analyze.coupled.settings import InterSettings

FILE = "intake/inter_spectra.jsonl"
KINDS = ("anchor", "pick", "lock", "settings")


@dataclass(frozen=True)
class Pick:
    fit: str; seq: int


@dataclass(frozen=True)
class LockState:
    locked: bool; fit: str | None; seq: int


@dataclass(frozen=True)
class InterUser:
    anchors: tuple[str, ...]                    # declared anchors, ordered by the seq of their latest declaring entry
    picks: dict[str, Pick]
    locks: dict[str, LockState]
    settings: dict[str, object]                 # folded deltas: only the overridden names
    max_seq: int
    damaged: tuple


def read_user(root) -> InterUser:           # tolerant: an unknown kind or malformed line is skipped, logged, listed in damaged
    raise NotImplementedError("stub: filled by S1U5")


def read_inter_settings(root) -> InterSettings:
    raise NotImplementedError("stub: filled by S1U5")
