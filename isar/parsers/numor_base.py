"""Shared reader for ILL **numor** ASCII (IN8/IN20/ThALES/… TAS data) — the spec_base analogue.

A numor is a keyword header (`INSTR:`, `EXPNO:`, `COMND:`, `POSQE:`, repeated `PARAM:`/`VARIA:`/
`ZEROS:` lines of `key=value` pairs) followed by a `DATA_:` marker, a column-label line, and a numeric
table. The column set varies by scan type (a `bs qh` Q-E scan logs QH/QK/QL/EN; a `sc a3` motor scan
logs A3), so this reader resolves columns BY NAME and never assumes positions — unlike the legacy IN8
parser, which took the monitor as "the column after EN".

Instrument-agnostic: it returns every keyword, every parsed `key=value` param, all column labels and
the full data array. Which columns are signal/monitor/temperature is the card's (and ill_in8's) job.
Real files carry one numor block; the synthetic emitter may concatenate several (one per scan), so
`read_numor` returns a list, split on the `RRRR…` banner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Header lines whose value is a list of `name=value` pairs we parse into `params` (floats).
_PAIR_KEYS = {"POSQE", "PARAM", "VARIA", "ZEROS", "STEPS"}
# Single-value keyword lines we keep verbatim in `keywords`.
_TEXT_KEYS = {"INSTR", "EXPNO", "USER_", "LOCAL", "FILE_", "DATE_", "TITLE", "TYPE_",
              "COMND", "CURVE"}


@dataclass
class NumorBlock:
    keywords: dict = field(default_factory=dict)        # single-value header lines (verbatim strings)
    params: dict = field(default_factory=dict)          # flat float map from key=value header pairs
    column_labels: list = field(default_factory=list)   # DATA_ column names (by name; may repeat)
    data: np.ndarray | None = None                      # [nP, ncol] float64
    line_range: tuple = (0, 0)


def _is_banner(line: str, ch: str) -> bool:
    s = line.strip()
    return len(s) >= 8 and set(s) == {ch}


def _parse_pairs(value: str, out: dict) -> None:
    """Parse `A= 1.0, B= -2, UN=meV` → {A:1.0, B:-2.0} (non-numeric pieces like UN=meV are skipped)."""
    for piece in value.split(","):
        if "=" not in piece:
            continue
        name, _, val = piece.partition("=")
        name = name.strip()
        try:
            out[name] = float(val.strip())
        except ValueError:
            pass


def _finalize(lines: list, start: int, end: int) -> NumorBlock | None:
    """Build one NumorBlock from header+data lines (the slice of one numor)."""
    blk = NumorBlock(line_range=(start, end))
    i = 0
    n = len(lines)
    # header: key: value lines until DATA_
    while i < n:
        line = lines[i]
        if line.startswith("DATA_:"):
            i += 1
            break
        key = line[:5].rstrip("_") if ":" in line[:7] else ""
        head, _, value = line.partition(":")
        head = head.strip()
        if head in _TEXT_KEYS:
            blk.keywords[head] = value.strip()
        elif head in _PAIR_KEYS:
            _parse_pairs(value, blk.params)
        i += 1
    # column-label line = next non-empty line after DATA_
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        return blk if blk.keywords else None
    blk.column_labels = lines[i].split()
    i += 1
    # data rows: numeric lines until a non-numeric line or EOF
    rows: list = []
    ncol = len(blk.column_labels)
    while i < n:
        toks = lines[i].split()
        if len(toks) >= ncol:
            try:
                rows.append([float(t) for t in toks[:ncol]])
            except ValueError:
                break
        elif toks:
            break
        i += 1
    blk.data = np.array(rows, dtype="float64") if rows else np.empty((0, ncol))
    return blk


def read_numor(path: str | Path) -> list[NumorBlock]:
    """Parse a numor file into one block per numor (split on the `RRRR…` banner)."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read().splitlines()

    # Split into per-numor segments on the RRRR banner that opens each numor.
    segments: list = []
    cur: list = []
    started = False
    cur_start = 0
    for idx, line in enumerate(raw):
        if _is_banner(line, "R"):
            if started and cur:
                segments.append((cur_start, cur))
            started = True
            cur_start = idx
            cur = []
            continue
        if started:
            cur.append(line)
    if started and cur:
        segments.append((cur_start, cur))
    if not segments:                       # no banner — treat the whole file as one segment
        segments = [(0, raw)]

    blocks = []
    for start, seg in segments:
        blk = _finalize(seg, start, start + len(seg))
        if blk is not None and blk.column_labels:
            blocks.append(blk)
    return blocks


def column_index(labels: list, name: str) -> int:
    """First column whose label is `name`, or -1 if absent (duplicate labels → the first wins)."""
    try:
        return labels.index(name)
    except ValueError:
        return -1


def param(block: NumorBlock, key: str, default: float | None = None) -> float | None:
    """A parsed header float (e.g. KFIX, DM, DA, FX, QH/QK/QL/EN from POSQE), or `default`."""
    return block.params.get(key, default)
