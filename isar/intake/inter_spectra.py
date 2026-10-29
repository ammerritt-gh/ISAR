"""The user's inter-spectra values, read from ``intake/inter_spectra.jsonl`` (C-settings-store). Qt-free.

Anchors, picks, locks and the inter-spectra settings are user values: ISAR never writes this file (AGENTS.md
hard rule 1). The checked writers are ``isar.intake.inter_values``; ``fold`` reads their delta lines, latest wins.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from isar.analyze.coupled.settings import SETTING_DEFS, InterSettings
from isar.intake import store

log = logging.getLogger(__name__)

FILE = "intake/inter_spectra.jsonl"
KINDS = ("anchor", "pick", "lock", "settings")
FAST = "fast"                                     # the reserved id of the fast solo fit
_FIT_ID = re.compile(r"f[0-9a-f]{12}")            # a refit's id: "f" + 12 hex (records.new_fit_id)
_NAMES = frozenset(d.name for d in SETTING_DEFS)


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


def fit_id_ok(fit) -> bool:
    return fit == FAST or (isinstance(fit, str) and _FIT_ID.fullmatch(fit) is not None)


def setting_problem(name, value) -> str | None:
    if name not in _NAMES:
        return f"unknown inter-spectra setting {name!r}"
    if value is None:
        return None
    try:
        InterSettings.from_values({name: value})
    except ValueError as exc:
        return str(exc)
    return None


def _problem(e: dict) -> str | None:
    """Why a store entry is not an inter-spectra entry ``fold`` can use; None when it is."""
    kind = e.get("kind")
    if kind not in KINDS:
        return f"unknown kind {kind!r}"
    targets = e.get("targets")
    if not (isinstance(targets, list) and targets and all(isinstance(t, str) for t in targets)):
        return "targets must be a non-empty list of keys"
    if e.get("reason") is not None and not isinstance(e["reason"], str):
        return "reason is not text"
    if kind == "anchor":
        return None if isinstance(e.get("declared"), bool) else "anchor entry has no bool 'declared'"
    if kind == "pick":
        return None if fit_id_ok(e.get("fit")) else f"pick names no fit id ({e.get('fit')!r})"
    if kind == "lock":
        if not isinstance(e.get("locked"), bool):
            return "lock entry has no bool 'locked'"
        fit = e.get("fit")
        if e["locked"] != (fit is not None) or (fit is not None and not fit_id_ok(fit)):
            return "lock entry must name a fit when locked, and none when unlocked"
        return None
    changes = e.get("changes")
    if not isinstance(changes, dict) or not changes:
        return "settings entry has no changes"
    for name, value in changes.items():
        why = setting_problem(name, value)
        if why is not None:
            return why
    return None


def fold(entries: Sequence[dict], lines: Sequence[int] = ()) -> InterUser:
    """Fold the store's valid entries, latest wins. A malformed entry is skipped (logged) and listed in ``damaged``;
    ``lines`` gives each entry's 1-based line (1..n when omitted). Also the writers' refusal check, so a refusal and
    the reader see the same state."""
    anchors: dict[str, tuple[int, bool]] = {}      # unit -> (seq, declared) of its latest declaring entry
    picks: dict[str, Pick] = {}
    locks: dict[str, LockState] = {}
    settings: dict[str, object] = {}
    damaged = []
    for i, e in enumerate(entries):
        line = lines[i] if lines else i + 1
        why = _problem(e)
        if why is not None:
            log.warning("%s line %d skipped: %s", FILE, line, why)
            damaged.append(store.Damaged(FILE, line, why))
            continue
        kind, seq = e["kind"], e["seq"]
        if kind == "anchor":
            for unit in e["targets"]:
                anchors[unit] = (seq, e["declared"])
        elif kind == "pick":
            for unit in e["targets"]:
                picks[unit] = Pick(e["fit"], seq)
        elif kind == "lock":
            for unit in e["targets"]:
                locks[unit] = LockState(e["locked"], e["fit"], seq)
        else:
            for name, value in e["changes"].items():
                if value is None:
                    settings.pop(name, None)
                else:
                    settings[name] = value
    declared = tuple(unit for unit, (_, is_declared) in sorted(anchors.items(), key=lambda kv: kv[1][0])
                     if is_declared)
    return InterUser(declared, picks, locks, settings, max((e["seq"] for e in entries), default=0),
                     tuple(damaged))


def read_user(root) -> InterUser:
    read = store.read_all(root, FILE)
    user = fold(read.entries, read.lines)
    return replace(user, damaged=tuple(sorted((*read.damaged, *user.damaged), key=lambda d: d.line)))


def read_inter_settings(root) -> InterSettings:
    return InterSettings.from_values(read_user(root).settings)     # every folded value was checked by fold
