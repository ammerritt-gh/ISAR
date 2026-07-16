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

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

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


def _resolve_block_rows(scan_number: int, rows: list, row_lines: list, expected: int = 0):
    """Reconcile a scan block's data rows against its declared width without ever raising.

    ``expected`` is the header-declared table width (``#N``, falling back to the resolved ``#L``
    labels — see :func:`_expected_width`); 0 means no declaration survived, and the width of the
    first data row is the only anchor left. A real SPEC scan aborted mid-write leaves exactly ONE
    truncated final row (scan 173 of the FeSeS campaign: 38 of 73 columns at line 62468). The
    user-locked policy:

    - the FIRST row already deviates from a declared width -> the whole block is structurally
      inconsistent (a uniformly truncated block must never be silently accepted: columns would
      resolve by name past the data's edge and IndexError downstream, skipping the whole file);
      return ``None`` so the caller SKIPS the block (warned);
    - all rows at the expected width -> rectangular, keep every row (the normal path);
    - a single SHORT final row -> the aborted-scan artifact; DROP just that row (warned), keep the rest;
    - any other raggedness (a short/over-wide row that is not the final one, an over-wide final row) ->
      mid-block corruption; return ``None`` so the caller SKIPS the whole scan block (warned).

    Never invents data (no NaN padding) and never raises for shape — genuinely non-numeric text still
    raises earlier, at float() parse time, and is caught per-file by the ingest layer.
    """
    if not rows:
        return rows
    widths = [len(r) for r in rows]
    w0 = expected if expected > 0 else widths[0]
    if widths[0] != w0:
        log.warning("scan %s: skipped — first data row at line %d has %d columns, header declares %d",
                    scan_number, row_lines[0], widths[0], w0)
        return None
    if all(w == w0 for w in widths):
        return rows
    if all(w == w0 for w in widths[:-1]) and widths[-1] < w0:
        log.warning("scan %s: dropped truncated final row at line %d (%d of %d columns)",
                    scan_number, row_lines[-1], widths[-1], w0)
        return rows[:-1]
    bad = next(i for i, w in enumerate(widths) if w != w0)
    log.warning("scan %s: skipped — ragged data block at line %d (%d columns, expected %d)",
                scan_number, row_lines[bad], widths[bad], w0)
    return None


def _expected_width(block: SpecScanBlock):
    """The header-declared data width for ``block``, or ``None`` when #N and #L disagree.

    ``#N`` is the authoritative declared table width; the ``#L`` label line is only trustworthy
    after multi-word normalization, so it must RECONCILE with ``#N`` rather than override it — a
    disagreement means either a malformed header or an unlisted multi-word column name, and every
    by-name column lookup on such a block would silently misalign. When ``#N`` is absent, the
    resolved labels are the best remaining declaration; 0 means no declaration at all (first-row
    fallback in :func:`_resolve_block_rows`).
    """
    if block.n_columns > 0:
        if block.column_labels and len(block.column_labels) != block.n_columns:
            return None
        return block.n_columns
    return len(block.column_labels)


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
    row_lines: list[int] = []          # source line number for each appended row (for warnings)
    collecting = False
    start_line = 0

    def close(end_line: int) -> None:
        nonlocal cur, rows, row_lines, collecting
        if cur is not None:
            expected = _expected_width(cur)
            if expected is None:       # #N / #L disagreement: every by-name lookup would misalign
                log.warning("scan %s: skipped — #N declares %d columns but #L resolves %d labels",
                            cur.scan_number, cur.n_columns, len(cur.column_labels))
                kept = None
            else:
                kept = _resolve_block_rows(cur.scan_number, rows, row_lines, expected)
            if kept is not None:       # None => structural inconsistency; skip the whole block (warned)
                cur.data = (np.asarray(kept, dtype="float64") if kept
                            else np.empty((0, cur.n_columns)))
                cur.line_range = (start_line, end_line)
                blocks.append(cur)
        cur, rows, row_lines, collecting = None, [], [], False

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
                row_lines.append(lineno)

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
