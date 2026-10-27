"""The checked writers for user values and conditions on ticked BL43 scans and analyzers.

Every check runs before anything is written: one invalid target or value refuses the whole save and the
entries file stays byte-identical. A save that passes is one line in ``intake/entries.jsonl`` (all its
targets in it), fsynced before ``ok=True``. A save never starts work and never touches a ledger, state or
report file; file values are never changed here. Called in the GUI process (Qt-free); ``workspace_api``
never imports this module.

Entry kinds (the store adds ``id``, ``seq`` and ``at``)::

    {"kind": "value",      "fact": "temperature"|"slit", "value": float|str, "targets": [scan keys], "reason": str|None}
    {"kind": "file_value", "fact": "temperature"|"slit",                     "targets": [scan keys], "reason": str|None}
    {"kind": "condition",  "condition": "excluded", "declared": bool,        "targets": [scan keys], "reason": str|None}
    {"kind": "condition",  "condition": "dead"|"counts_accepted", "declared": bool, "targets": [unit keys], "reason": str|None}
"""
from __future__ import annotations

import functools
import logging
import math
from collections.abc import Sequence
from numbers import Real
from pathlib import Path

from isar.intake import scans, store
from isar.intake.inputs import CONDITIONS, ENTERABLE, FILE_FACT  # noqa: F401  (the entry vocabulary lives on the read side)
from isar.intake.settings import _BUSY, SaveResult
from isar.parsers import metadata, sp8_bl43

log = logging.getLogger(__name__)

UNIT_CONDITIONS = ("dead", "counts_accepted")


@functools.lru_cache(maxsize=1)
def physical_analyzers() -> tuple[str, ...]:
    """The shipped BL43 card's ``[[detectors]]`` ids, in card order (the 28 physical analyzers)."""
    return tuple(d.detector_id for d in sp8_bl43.card_template().policy.detectors)


class _Bad(Exception):
    """A check failed; the message is the reason the save returns."""


def _given(targets) -> tuple:
    """The targets as given (C-save-result): a bare str is one target, a non-iterable none."""
    if isinstance(targets, str):
        return (targets,)
    try:
        return tuple(targets)
    except TypeError:
        return ()


def _targets(targets, what: str) -> list[str]:
    if isinstance(targets, (str, bytes)):
        raise _Bad(f"targets must be a list of {what}, not a string")
    if not isinstance(targets, Sequence):
        raise _Bad(f"targets must be a list of {what} (got {type(targets).__name__})")
    for t in targets:       # before deduplication: an unhashable target must not raise
        if not isinstance(t, str):
            raise _Bad(f"target {t!r} is not a key (targets must be {what})")
    out = list(dict.fromkeys(targets))
    if not out:
        raise _Bad("no targets given")
    return out


def _known_scans(root) -> dict:
    try:
        return scans.read_scans(root)
    except (OSError, ValueError) as exc:
        raise _Bad(f"the import record {scans.INTAKE_SCANS} cannot be read ({exc})") from exc


def _check_scans(root, targets: list[str]) -> None:
    known = _known_scans(root)
    for t in targets:
        if t not in known:
            raise _Bad(f"unknown scan {t}")


def _check_units(root, targets: list[str]) -> None:
    known = _known_scans(root)
    for t in targets:
        if t in known:
            raise _Bad(f"{t} names a scan, not an analyzer (a target is <scan key>:aNN)")
        scan, sep, det = t.rpartition(":")   # exact membership of both parts, never a parsed identity
        if not sep or scan not in known:
            raise _Bad(f"unknown scan {scan or t} (in {t})")
        if det not in physical_analyzers():
            raise _Bad(f"{det} is not a BL43 analyzer ({t})")


def _temperature(value) -> float:
    t = None
    if isinstance(value, Real) and not isinstance(value, bool):
        t = float(value)
    elif isinstance(value, str):
        try:
            t = float(value.strip())
        except ValueError:
            t = None
    if t is None or not math.isfinite(t) or t <= 0:
        raise _Bad(f"temperature must be a finite number greater than 0 K (got {value!r})")
    return t


def _slit(value) -> str:
    bad = _Bad(f"slit must be two finite positive numbers in mm, horizontal then vertical (got {value!r})")
    if isinstance(value, str):
        if "-" in value:                     # normalise_slit drops signs: "-1x40" would save as "1x40"
            raise bad
        label = metadata.normalise_slit(value)
        if label is None:
            raise bad
        h, v = (float(x) for x in label.split("x"))
    elif isinstance(value, Sequence) and len(value) == 2 \
            and all(isinstance(x, Real) and not isinstance(x, bool) for x in value):
        h, v = float(value[0]), float(value[1])
    else:
        raise bad
    if not (math.isfinite(h) and math.isfinite(v) and h > 0 and v > 0):
        raise bad
    return metadata.slit_label(h, v)


def _reason(reason) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise _Bad(f"reason must be text (got {type(reason).__name__})")
    return reason.strip() or None


def _fact(fact) -> None:
    if fact not in ENTERABLE:
        raise _Bad(f"unknown fact {fact!r} (enterable: {', '.join(ENTERABLE)})")


def _save(root, given: tuple, build) -> SaveResult:
    """Run ``build`` (the checks, returning ``(entry, value)``), then write one line or nothing."""
    try:
        entry, value = build()
    except _Bad as bad:
        return SaveResult(False, str(bad), None, given, None)
    try:
        stored = store.append(Path(root), store.ENTRIES, entry)
    except store.StoreBusy:
        reason = _BUSY
    except OSError as exc:
        reason = f"could not write {store.ENTRIES}: {exc}"
    else:
        return SaveResult(True, None, value, tuple(entry["targets"]), stored["id"])
    log.warning("intake save (%s) not written: %s", entry["kind"], reason)
    return SaveResult(False, reason, None, given, None)


def save_value(root, fact: str, value: object, targets: Sequence[str], *,
               reason: str | None = None) -> SaveResult:
    """Save a user temperature (K) or slit (mm, ``"HxV"``) on the ticked scans."""
    def build():
        _fact(fact)
        keys = _targets(targets, "scan keys")
        _check_scans(root, keys)
        v = _temperature(value) if fact == "temperature" else _slit(value)
        return {"kind": "value", "fact": fact, "value": v, "targets": keys, "reason": _reason(reason)}, v
    return _save(root, _given(targets), build)


def use_file_value(root, fact: str, targets: Sequence[str], *, reason: str | None = None) -> SaveResult:
    """Withdraw the user value on the ticked scans: the file value is used again."""
    def build():
        _fact(fact)
        keys = _targets(targets, "scan keys")
        _check_scans(root, keys)
        return {"kind": "file_value", "fact": fact, "targets": keys, "reason": _reason(reason)}, None
    return _save(root, _given(targets), build)


def save_condition(root, condition: str, targets: Sequence[str], *, declared: bool = True,
                   reason: str | None = None) -> SaveResult:
    """Declare (or, with ``declared=False``, reverse) a condition: an excluded scan, or a dead analyzer or
    accepted counts on ticked scan x analyzer pairs (``"<scan key>:aNN"``)."""
    def build():
        if condition not in CONDITIONS:
            raise _Bad(f"unknown condition {condition!r} (one of: {', '.join(CONDITIONS)})")
        if not isinstance(declared, bool):
            raise _Bad(f"declared must be true or false (got {declared!r})")
        unit = condition in UNIT_CONDITIONS
        keys = _targets(targets, "unit keys" if unit else "scan keys")
        (_check_units if unit else _check_scans)(root, keys)
        return {"kind": "condition", "condition": condition, "declared": declared, "targets": keys,
                "reason": _reason(reason)}, None
    return _save(root, _given(targets), build)
