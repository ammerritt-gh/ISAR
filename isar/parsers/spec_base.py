"""Shared SPEC reader — the block parser the IXS facilities (S30, ID28, BL43LXU) share (§6).

Three of four IXS facilities write SPEC ASCII, so the byte mechanics live here once and the
per-facility differences (which column is signal, which `#V` line holds analyzer Q, the abscissa
kind) live entirely in the card. This reader is instrument-agnostic and card-free: it returns a
structured intermediate (`SpecHeader` + `SpecScanBlock`s) preserving *all* `#V`/`#H` groups and
*all* columns. Mapping that intermediate onto a `ScanRecord` is the card layer's job (`spec_s30`).

Mechanics ported (and generalized) from the reference parser `Data_Parsing_MnTe.py`: marker
dispatch on `#S/#V/#L`, columns resolved by name from `#L`, scans split on `#S`. Unlike the
reference, this keeps every analyzer and every column — selection is the card's decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Header markers that carry an integer group index, e.g. "#H12", "#O3", "#V8", "#P0", "#G1".
_NUMBERED = re.compile(r"^#([HOVPG])(\d+)\b")


@dataclass
class SpecHeader:
    F: str = ""
    E: int = 0
    D: str = ""
    C: str = ""
    H_groups: dict = field(default_factory=dict)   # int -> list[str] (metadata-group names)
    O_groups: dict = field(default_factory=dict)   # int -> list[str] (motor names)
    raw: dict = field(default_factory=dict)


@dataclass
class SpecScanBlock:
    scan_number: int
    command: str = ""
    D: str = ""
    T: tuple | None = None                         # (preset_value, unit)
    G: dict = field(default_factory=dict)          # int -> list[float] (sparse)
    Q: list = field(default_factory=list)          # [h, k, l]
    P_groups: dict = field(default_factory=dict)   # int -> list[float] (motor positions)
    C: str = ""
    V_groups: dict = field(default_factory=dict)   # int -> list[float] (parallel to H_groups)
    n_columns: int = 0
    column_labels: list = field(default_factory=list)
    data: np.ndarray | None = None
    line_range: tuple = (0, 0)


def _floats(tokens: list[str]) -> list[float]:
    return [float(t) for t in tokens]


def _join_multiword(label_line: str, multiword: list[str]) -> list[str]:
    """Tokenize a `#L` line, first collapsing known multi-word column names to single tokens.

    Raw S30 `#L` lines carry space-containing names (e.g. `Heat Stage T1`), so a naive whitespace
    split over-counts columns and misaligns by-name resolution against the data table. Each listed
    name is underscore-joined before the split (longest first, so a name that is a prefix of another
    cannot be partially consumed). Names absent from this line are simply not matched.
    """
    for name in sorted(multiword, key=len, reverse=True):
        label_line = label_line.replace(name, name.replace(" ", "_"))
    return label_line.split()


def read_spec(path: str | Path, *,
              multiword_columns: list[str] | None = None) -> tuple[SpecHeader, list[SpecScanBlock]]:
    """Parse a SPEC file into its global header and per-scan blocks (split on `#S`).

    `multiword_columns` (from the card) lists `#L` column names that contain spaces; when given,
    they are collapsed to single tokens so columns resolve by name. `None` keeps the historical
    whitespace-split behavior (the supplementary path, whose `#L` has no multi-word names).
    """
    header = SpecHeader()
    blocks: list[SpecScanBlock] = []

    cur: SpecScanBlock | None = None
    rows: list[list[float]] = []
    collecting = False
    start_line = 0

    def close(end_line: int) -> None:
        nonlocal cur, rows, collecting
        if cur is not None:
            cur.data = np.asarray(rows, dtype="float64") if rows else np.empty((0, cur.n_columns))
            cur.line_range = (start_line, end_line)
            blocks.append(cur)
        cur, rows, collecting = None, [], False

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lineno = 0
        for raw_line in f:
            lineno += 1
            line = raw_line.rstrip("\n")
            s = line.strip()
            if not s:
                continue

            if s.startswith("#S "):
                close(lineno - 1)
                parts = line.split(maxsplit=2)
                cur = SpecScanBlock(scan_number=int(parts[1]),
                                    command=parts[2] if len(parts) > 2 else "")
                start_line = lineno
                continue

            m = _NUMBERED.match(s)
            if m:
                kind, idx = m.group(1), int(m.group(2))
                rest = s[m.end():].split()
                if cur is None:
                    if kind == "H":
                        header.H_groups[idx] = rest
                    elif kind == "O":
                        header.O_groups[idx] = rest
                else:
                    if kind == "G":
                        cur.G[idx] = _floats(rest)
                    elif kind == "P":
                        cur.P_groups[idx] = _floats(rest)
                    elif kind == "V":
                        cur.V_groups[idx] = _floats(rest)
                continue

            tag = s.split(maxsplit=1)[0]
            body = s[len(tag):].strip()

            if tag == "#F":
                header.F = body
            elif tag == "#E":
                header.E = int(body) if body.lstrip("-").isdigit() else 0
            elif tag == "#D":
                (cur.__setattr__("D", body) if cur is not None else header.__setattr__("D", body))
            elif tag == "#C":
                (cur.__setattr__("C", body) if cur is not None else header.__setattr__("C", body))
            elif tag == "#T" and cur is not None:
                toks = body.split()
                val = float(toks[0]) if toks else 0.0
                unit = toks[1].strip("()") if len(toks) > 1 else ""
                cur.T = (val, unit)
            elif tag == "#Q" and cur is not None:
                cur.Q = _floats(body.split())
            elif tag == "#N" and cur is not None:
                cur.n_columns = int(body.split()[0])
            elif tag == "#L" and cur is not None:
                cur.column_labels = (_join_multiword(body, multiword_columns)
                                     if multiword_columns else body.split())
                collecting = True
            elif not s.startswith("#") and collecting and cur is not None:
                rows.append([float(t) for t in s.split()])

        close(lineno)

    return header, blocks


def column_index(block: SpecScanBlock, name: str) -> int:
    """Resolve a data column by its `#L` name (no hardcoded positions)."""
    return block.column_labels.index(name)


def v_group(block: SpecScanBlock, header: SpecHeader, name_prefix: str) -> list:
    """Return the `#V` values whose matching `#H` group is named `name_prefix` (e.g. 'Analyzer3').

    The `#V`/`#H` lines are parallel by index, so this resolves the analyzer Q by name rather than
    by a hardcoded line number.
    """
    for idx, names in header.H_groups.items():
        if names and names[0].startswith(name_prefix):
            return block.V_groups[idx]
    raise KeyError(f"no #H group named {name_prefix!r}")
