"""The checked writers for the inter-spectra user values (C-writers). GUI-allowed; ``workspace_api`` never imports it.

Each writer appends one delta line to ``intake/inter_spectra.jsonl`` through the intake store, after checks that
refuse the whole save and leave the file byte-identical. ISAR writes none of these on its own (hard rule 1).
S1U5 fills the bodies.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from isar.intake.settings import SaveResult


def set_anchors(root, keys: Sequence[str], *, anchored: bool, reason: str | None = None) -> SaveResult:
    raise NotImplementedError("stub: filled by S1U5")


def pick_fit(root, key: str, fit_id: str) -> SaveResult:          # refused while locked
    raise NotImplementedError("stub: filled by S1U5")


def set_lock(root, key: str, *, locked: bool, fit_id: str | None = None) -> SaveResult:
    raise NotImplementedError("stub: filled by S1U5")


def save_settings(root, changes: Mapping[str, object | None]) -> SaveResult:    # None -> default
    raise NotImplementedError("stub: filled by S1U5")
