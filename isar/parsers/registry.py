"""Versioned parser catalogue keyed on `(facility, instrument, format_version)` (§6, §8 of the
schema-landscape research — Mantid's valid-from-date dispatch transferred).

`detect(path)` reads only the header (embedded date + format markers), never the data body, and
resolves the tuple. A drifted format is a *new registry entry keyed to its date range*, never an
edit to a working parser.
"""
from __future__ import annotations

import re
from pathlib import Path

# (facility, instrument, format_version) -> plugin
REGISTRY: dict[tuple[str, str, str], object] = {}


def register(plugin, facility: str, instrument: str, format_version: str) -> None:
    REGISTRY[(facility, instrument, format_version)] = plugin


def get(facility: str, instrument: str, format_version: str):
    return REGISTRY[(facility, instrument, format_version)]


def plugin_for(path: str | Path):
    return get(*detect(path))


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _header_lines(path: str | Path, n: int = 60) -> list[str]:
    out: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= n:
                break
            out.append(line.rstrip("\n"))
    return out


def _first_L_line(path: str | Path, max_lines: int = 400) -> str:
    """The first `#L` (column-label) line, scanned with a cap. The S30 raw vs supplementary split is
    a #L-content decision (raw has bare `AnaN`; supplementary has `AnaN_sum_o`), and the #L sits
    deeper than the global header (~line 91), so detection needs a bounded look past `_header_lines`.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            if line.startswith("#L "):
                return line.rstrip("\n")
    return ""


def _spec_year(text: str) -> str:
    m = re.search(r"^#D .*\b(20\d\d)\b", text, re.MULTILINE)
    if m:
        return m.group(1)
    m = re.search(r"\b(20\d\d)\b", text)
    return m.group(1) if m else "unknown"


def _numor_year(text: str) -> str:
    # DATE_: 09-Sep-25 11:32:10  → two-digit year → 20xx
    m = re.search(r"^DATE_:\s*\d{2}-\w{3}-(\d{2})", text, re.MULTILINE)
    return f"20{m.group(1)}" if m else "unknown"


def detect(path: str | Path) -> tuple[str, str, str]:
    """Resolve (facility, instrument, format_version) from a file's header."""
    head = _header_lines(path)
    text = "\n".join(head)

    # SPEC family — the IXS facilities (S30, ID28, BL43) all open with "#F ...".
    if head and head[0].startswith("#F"):
        low = text.lower()
        if "herix" in low or "sector30" in low:
            # S30 ships two flavours: the beamline-cleaned supplementary (AnaN_sum_o columns) and
            # the raw .spec with individual miniscans (bare AnaN). Split them by #L content so each
            # gets its own keyed parser variant (§6) rather than overloading one.
            if "_sum_o" in _first_L_line(path):
                return ("APS", "S30", _spec_year(text))
            return ("APS", "S30", f"{_spec_year(text)}-raw")
        elif "id28" in low:
            facility, instrument = "ESRF", "ID28"
        elif "specixs" in low or "bl43" in low:
            facility, instrument = "SP8", "BL43LXU"
        else:
            facility, instrument = "UNKNOWN", "SPEC"
        return (facility, instrument, _spec_year(text))

    # ILL numor family — keyword header with "INSTR: <name>".
    instr = next((l.split(":", 1)[1].strip() for l in head if l.startswith("INSTR:")), None)
    if instr:
        return ("ILL", instr, _numor_year(text))

    raise ValueError(f"unrecognized file format: {Path(path).name}")
