"""The checked writers for the inter-spectra user values (C-writers). GUI-allowed; ``workspace_api`` never imports it.

Each writer appends one delta line to ``intake/inter_spectra.jsonl`` through the intake store, after checks that
refuse the whole save and leave the file byte-identical. ISAR writes none of these on its own (hard rule 1).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from isar.analyze.coupled.settings import InterSettings
from isar.intake.inter_spectra import FILE, fit_id_ok, fold, read_user, setting_problem
from isar.intake.settings import SaveResult
from isar.intake.values import _Bad, _check_units, _given, _reason, _save, _targets


def _without_fast(root, units: Sequence[str]) -> list[str]:
    """The units with no stored fast solo fit: the ledger entry is ``fitted`` and carries its fast-pass record
    (file_fit's test). The session ledger is read once for all of them."""
    from isar.session.state import SessionState          # lazy: the intake side never loads isar.session otherwise
    from isar.session.workspace import SESSION_NAME
    try:
        session = SessionState.load(Path(root) / SESSION_NAME)
    except (OSError, ValueError) as exc:
        raise _Bad(f"the session ledger cannot be read ({exc})") from exc
    out = []
    for unit in units:
        entry = session.get(unit)
        det = entry.detectors.get(unit.rsplit(":", 1)[1]) if entry is not None and entry.status == "fitted" else None
        if not (det or {}).get("fast_pass"):
            out.append(unit)
    return out


def _refuse_if_locked(key: str):
    def check(present: list[dict]) -> str | None:
        lock = fold(present).locks.get(key)
        return f"locked to {lock.fit}; unlock first" if lock is not None and lock.locked else None
    return check


def _refuse_unless_locked(key: str):
    def check(present: list[dict]) -> str | None:
        lock = fold(present).locks.get(key)
        return None if lock is not None and lock.locked else f"{key} is not locked"
    return check


def _bad_fit(fit_id) -> _Bad:
    return _Bad(f"fit id {fit_id!r} is not 'fast' or 'f' followed by 12 hex digits")


def set_anchors(root, keys: Sequence[str], *, anchored: bool, reason: str | None = None) -> SaveResult:
    def build():
        if not isinstance(anchored, bool):
            raise _Bad("anchored must be true or false")
        units = _targets(keys, "unit keys")
        why = _reason(reason)
        _check_units(root, units)
        # shortcut: the no-op test reads outside the store lock; one GUI process writes, so the window is one click
        if all((u in read_user(root).anchors) == anchored for u in units):
            return None, anchored                       # idempotent: already so, nothing is written
        if anchored:
            missing = _without_fast(root, units)
            if missing:
                raise _Bad(f"{missing[0]} has no stored fast fit")
        return {"kind": "anchor", "declared": anchored, "targets": units, "reason": why}, anchored
    return _save(root, _given(keys), build, name=FILE)


def pick_fit(root, key: str, fit_id: str) -> SaveResult:          # refused while locked
    def build():
        units = _targets([key], "unit keys")
        _check_units(root, units)
        if not fit_id_ok(fit_id):
            raise _bad_fit(fit_id)
        return {"kind": "pick", "fit": fit_id, "targets": units, "reason": None}, fit_id
    return _save(root, _given(key), build, name=FILE, check=_refuse_if_locked(key))


def set_lock(root, key: str, *, locked: bool, fit_id: str | None = None) -> SaveResult:
    def build():
        if not isinstance(locked, bool):
            raise _Bad("locked must be true or false")
        units = _targets([key], "unit keys")
        _check_units(root, units)
        if locked and fit_id is None:
            raise _Bad("a lock needs the fit it pins (fast, or f followed by 12 hex digits)")
        if not locked and fit_id is not None:
            raise _Bad("fit_id is only given to lock a spectrum")
        if locked and not fit_id_ok(fit_id):
            raise _bad_fit(fit_id)
        fit = fit_id if locked else None
        return {"kind": "lock", "locked": locked, "fit": fit, "targets": units, "reason": None}, fit
    return _save(root, _given(key), build, name=FILE,
                 check=None if locked else _refuse_unless_locked(key))


def save_settings(root, changes: Mapping[str, object | None]) -> SaveResult:    # None -> default
    def build():
        if not isinstance(changes, Mapping) or not changes:
            raise _Bad("no settings given")
        for name, value in changes.items():
            why = setting_problem(name, value)
            if why is not None:
                raise _Bad(why)
        canon = InterSettings.from_values({k: v for k, v in changes.items() if v is not None}).to_values()
        out = {k: None if v is None else canon[k] for k, v in changes.items()}
        return {"kind": "settings", "changes": out, "targets": ["experiment"], "reason": None}, out
    return _save(root, ("experiment",), build, name=FILE)
