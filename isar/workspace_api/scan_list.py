"""The Qt-free scan-list read: the value objects behind the GUI's scan list and scan inspector.

One function over the open experiment's read-only facade, :func:`scan_list`, returns a frozen
:class:`ScanListSnapshot`: scan sets, their analyzers (spectra), the files that were not read, and
pooled masters. This module is Qt-free (hard rule 6) and read-only (hard rule 7): it never opens a
file, re-fits, writes or reads ``results.csv``.

The dataclasses, constants and lookup helpers here are final. :func:`scan_list` is filled by a later
unit; until then it returns an empty snapshot and never raises, because the window calls it on every
bind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Re-exported so the GUI (isar_gui/theme.py) takes the results writer's words from the facade and
# never imports isar.session.* itself (the G16 import contract).
from isar.session.outcome import NOT_FITTED, NOT_RECORDED

PARSED_OK = "ok"          # Parsed: the file was read; nothing is missing from T and Q
NOT_READ = "not read"     # Parsed: a file that was not read
NOT_RUN = "not run"       # Fast/Deep: no result stored and nothing holds it
HELD = "held"             # Fast: the scan is held (the word results.csv writes for it)

__all__ = [
    "PARSED_OK", "NOT_READ", "NOT_RUN", "HELD", "NOT_FITTED", "NOT_RECORDED",
    "Fact", "Status", "SpectrumRow", "UnreadFile", "ScanSetRow", "PooledRow", "ScanListSnapshot",
    "natural_key", "scan_list",
]


@dataclass(frozen=True)
class Fact:
    value: float | tuple[float, ...] | str | None   # None = missing
    unit: str | None          # "K", "nm⁻¹", "rlu"; None for a unitless fact (the slit text)
    source: str               # the stored source word ("header", "trailer", "filename", "sidecar",
                              # "card", "inferred", "scan", "fit") or "missing"
    origin: str               # "file" | "card" | "inferred" | "fit" | "missing"; stage two adds "user"
    reason: str | None = None # why it is missing (hover text); None when value is present


@dataclass(frozen=True)
class Status:
    word: str                 # shown verbatim, never renamed
    stored: bool              # Parsed: True when the file was read. Fast/Deep: True when the word
                              # comes from that pass's own stored record ("through the pass")
    reason: str | None = None # tooltip text: the stored reason, the hold reason, what is missing


@dataclass(frozen=True)
class SpectrumRow:
    key: str                  # unit_key(experiment, scan_number, detector): THE identity
    set_key: str              # scan_prefix(key): its scan set's identity
    analyzer: str | None      # detector id, e.g. "a06"
    q: Fact
    parsed: Status
    fast: Status
    deep: Status
    file_name: str | None     # the analyzer's own file name; None when it cannot be told
    file_path: str | None
    n_points: int | None
    fit: Any = field(default=None, compare=False, repr=False)  # the FitView the display shows; None: raw counts


@dataclass(frozen=True)
class UnreadFile:
    path: str                 # as the manifest holds it (resolved absolute)
    name: str
    reason: str
    set_key: str | None = None   # set, when it is a member left out of a set that was read


@dataclass(frozen=True)
class ScanSetRow:
    key: str                  # scan_prefix of its spectra: "experiment:scan_number"
    label: str                # the display label, never the hashed placeholder number
    experiment_id: str | None
    scan_numbers: tuple[int, ...]   # as the files list them; () when they list none
    summed: bool              # the parser flagged it pre_combined_input
    n_merged: int | None      # the sidecar's merged-scan count, when the files give it
    temperature: Fact
    slit: Fact
    spectra: tuple[SpectrumRow, ...]      # natural order of analyzer
    unread: tuple[UnreadFile, ...] = ()   # member files of this set that were not read
    expected_analyzers: int | None = None # stage one: always None; stage two: from the backend
    files_received: int = 0   # the parse summary's denominator: distinct manifest files (ws.files entries) of the set


@dataclass(frozen=True)
class PooledRow:
    key: str                  # the master's unit key
    label: str                # "pooled master (N members)"
    temperature: Fact
    q: Fact
    n_members: int | None
    fast: Status
    deep: Status
    fit: Any = field(default=None, compare=False, repr=False)  # the master's FitView


@dataclass(frozen=True)
class ScanListSnapshot:
    sets: tuple[ScanSetRow, ...] = ()
    files_not_read: tuple[UnreadFile, ...] = ()   # every unread file, left-out members included
    pooled: tuple[PooledRow, ...] = ()

    @property
    def n_spectra(self) -> int:
        """Spectra in sets; pooled masters are not counted."""
        return sum(len(s.spectra) for s in self.sets)

    def spectrum(self, key: str) -> SpectrumRow | PooledRow | None:
        """The spectrum or pooled master with this unit key, or ``None``."""
        for s in self.sets:
            for sp in s.spectra:
                if sp.key == key:
                    return sp
        for p in self.pooled:
            if p.key == key:
                return p
        return None

    def scan_set(self, set_key: str) -> ScanSetRow | None:
        """The scan set with this key, or ``None``."""
        for s in self.sets:
            if s.key == set_key:
                return s
        return None


_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key where digit runs compare as numbers and text compares case-insensitively:
    ``"a3" < "a14"``, ``"58" < "102"``."""
    parts = _DIGITS.split("" if text is None else str(text))
    # re.split puts digit runs at odd positions; a digit run sorts before text at the same position.
    return tuple((0, int(p), "") if i % 2 else (1, 0, p.casefold())
                 for i, p in enumerate(parts) if p != "")


def scan_list(ws) -> ScanListSnapshot:
    """The scan list of the open experiment ``ws`` (a ``Workspace`` or anything with its read
    surface, such as the GUI's null workspace). Never raises.

    Stub: returns an empty snapshot until the read itself is written."""
    return ScanListSnapshot()
