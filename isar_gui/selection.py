"""The window's ordered spectrum selection and shared display identities."""
from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from isar.workspace_api.scan_list import PooledRow, ScanListSnapshot, SpectrumRow

ORIGIN_LIST = "list"
ORIGIN_GROUP = "group"
ORIGIN_STEP = "step"
ORIGIN_REFRESH = "refresh"
ORIGIN_OTHER = "other"


@dataclass(frozen=True)
class SelectionChange:
    keys: tuple[str, ...]
    active: str | None
    origin: str
    keys_changed: bool
    active_changed: bool


class Selection(QObject):
    """One selection shared by the scan list, plot, inspector and group chips."""

    changed = Signal(object)
    active_changed = Signal(object)

    def __init__(self, parent: QObject | None = None,
                 order: Callable[[Iterable[str]], Sequence[str]] | None = None) -> None:
        super().__init__(parent)
        self._keys: tuple[str, ...] = ()
        self._active: str | None = None
        self._order = order
        self._emitting = False
        self._pending: deque[tuple[str, tuple]] = deque()

    @property
    def keys(self) -> tuple[str, ...]:
        return self._keys

    @property
    def active(self) -> str | None:
        return self._active

    def position(self) -> tuple[int, int] | None:
        if self._active is None:
            return None
        return self._keys.index(self._active) + 1, len(self._keys)

    def set_order(self, order: Callable[[Iterable[str]], Sequence[str]]) -> None:
        self._order = order

    def set(self, keys: Iterable[str], *, clicked: str | None = None,
            origin: str = ORIGIN_OTHER) -> None:
        self._request("set", tuple(keys), clicked, origin)

    def clear(self, origin: str = ORIGIN_OTHER) -> None:
        self.set((), origin=origin)

    def set_active(self, key: str) -> bool:
        if key not in self._keys:
            return False
        self._request("active", key)
        return True

    def step(self, delta: int) -> None:
        self._request("step", int(delta))

    def resync(self) -> None:
        self._request("resync")

    def _request(self, operation: str, *args) -> None:
        if self._emitting:
            self._pending.append((operation, args))
            return
        self._apply(operation, args)
        while self._pending:
            queued_operation, queued_args = self._pending.popleft()
            self._apply(queued_operation, queued_args)

    def _ordered(self, keys: Iterable[str]) -> tuple[str, ...]:
        unique = tuple(dict.fromkeys(keys))
        return tuple(self._order(unique)) if self._order is not None else unique

    def _apply(self, operation: str, args: tuple) -> None:
        old_keys, old_active = self._keys, self._active
        origin = ORIGIN_STEP
        if operation == "set":
            keys, clicked, origin = args
            self._keys = self._ordered(keys)
            self._active = clicked if clicked in self._keys else (self._keys[0] if self._keys else None)
        elif operation == "active":
            self._active = args[0]
        elif operation == "step":
            if self._keys and self._active is not None:
                current = self._keys.index(self._active)
                index = min(len(self._keys) - 1, max(0, current + args[0]))
                self._active = self._keys[index]
        elif operation == "resync":
            origin = ORIGIN_REFRESH
            self._keys = self._ordered(self._keys)
            if self._active not in self._keys:
                self._active = self._keys[0] if self._keys else None
        else:
            raise ValueError(f"unknown selection operation: {operation}")

        keys_changed = self._keys != old_keys
        active_changed = self._active != old_active
        if not keys_changed and not active_changed and origin != ORIGIN_GROUP:
            return

        change = SelectionChange(self._keys, self._active, origin, keys_changed, active_changed)
        self._emitting = True
        try:
            self.changed.emit(change)
            if active_changed:
                self.active_changed.emit(self._active)
        finally:
            self._emitting = False


@dataclass(frozen=True)
class Identity:
    scan: str
    numbered: bool
    analyzer: str
    q: str
    t: str
    set_label: str


def _fact_text(fact, missing: str, precision: str) -> str:
    value = fact.value
    if not isinstance(value, (int, float)) or not fact.unit:
        return missing
    return f"{value:{precision}} {fact.unit}"


def identity(snapshot: ScanListSnapshot, key: str) -> Identity | None:
    row = snapshot.spectrum(key)
    if row is None:
        return None
    if isinstance(row, PooledRow):
        scan, numbered, analyzer, q_fact, t_fact, label = row.label, False, "", row.q, row.temperature, ""
    else:
        scan_set = snapshot.scan_set(row.set_key)
        if scan_set is None:
            return None
        numbered = len(scan_set.scan_numbers) == 1
        scan = str(scan_set.scan_numbers[0]) if numbered else scan_set.label
        analyzer = row.analyzer or key.rsplit(":", 1)[-1]
        q_fact, t_fact, label = row.q_abs, scan_set.temperature, scan_set.label
    return Identity(scan, numbered, analyzer,
                    _fact_text(q_fact, "Q missing", ".2f"),
                    _fact_text(t_fact, "T missing", "g"), label)


def unique_labels(snapshot: ScanListSnapshot, keys: Sequence[str],
                  parts: Callable[[Identity], Sequence[str]]) -> dict[str, str]:
    identities = {key: identity(snapshot, key) for key in dict.fromkeys(keys)}
    labels = {key: " · ".join(part for part in parts(value) if part)
              for key, value in identities.items() if value is not None}
    counts: dict[str, int] = {}
    for label in labels.values():
        counts[label] = counts.get(label, 0) + 1
    for key, label in tuple(labels.items()):
        if counts[label] > 1:
            suffix = identities[key].set_label
            labels[key] = f"{label} · {suffix}" if suffix else label
    counts.clear()
    for label in labels.values():
        counts[label] = counts.get(label, 0) + 1
    for key, label in tuple(labels.items()):
        if counts[label] > 1:
            labels[key] = f"{label} · {hashlib.sha256(key.encode('utf-8')).hexdigest()[:6]}"
    return labels
