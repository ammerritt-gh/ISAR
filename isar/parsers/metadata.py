"""Shared reading helpers for any instrument's parser: `# key value` headers, card filename
patterns, count recovery from printed intensity and error, and facts merged by source.

Qt-free and instrument-agnostic. A catalogue entry's card supplies the filename patterns and
decides what a header key means; these helpers only read and merge (design R3, R8).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from isar.schema.scan import Flag

log = logging.getLogger(__name__)

# Fact precedence, first found wins (the design's ladder); `inferred` and `missing` are not sources.
PRECEDENCE = ("header", "sidecar", "filename", "card")

# Header keys whose values are numbers; every other key is kept as text.
_HEADER_KINDS = {
    "scan": "ints", "si_order": "int", "hkl": "floats",
    "T_K": "float", "Q_nm": "float", "dq": "float", "agaph_mm": "float", "agapv_mm": "float",
    "energy_keV": "float",
}
_NUMBER = re.compile(r"m?\d+(?:p\d+)?")


def read_header(lines: Iterable[str]) -> dict:
    """Leading `# key value` lines as {key: value}; reading stops at the first non-comment line.

    Known numeric keys are typed (see _HEADER_KINDS); a value that does not parse as its type is
    kept as text and logged, so the caller sees the raw value rather than a silent default.
    """
    out: dict = {}
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if not s.startswith("#"):
            break
        parts = s[1:].split(None, 1)
        if len(parts) == 2:
            out[parts[0]] = _typed(parts[0], parts[1].strip())
    return out


def _typed(key: str, text: str):
    kind = _HEADER_KINDS.get(key, "str")
    try:
        if kind == "str":
            return text
        if kind == "int":
            return int(text)
        if kind == "float":
            return float(text)
        if kind == "ints":
            return [int(p) for p in text.split()]
        return [float(p) for p in text.split()]
    except ValueError:
        log.warning("header %s=%r does not parse as %s; kept as text", key, text, kind)
        return text


def match_filename(name: str, patterns: Iterable[str]) -> dict:
    """Named groups of the first card pattern that matches all of `name`, decoded.

    `p` is the decimal point and a leading `m` is minus (`m0p0079` -> -0.0079); digits become int,
    other numbers float; a group containing `_` becomes the list of its decoded parts
    (`0p5_0p5_3` -> [0.5, 0.5, 3]). Returns {} when no pattern matches.
    """
    for pattern in patterns:
        m = re.fullmatch(pattern, name)
        if m:
            return {k: _decode_group(v) for k, v in m.groupdict().items() if v is not None}
    return {}


def _decode_group(text: str):
    if "_" in text:
        return [_decode(p) for p in text.split("_")]
    return _decode(text)


def _decode(token: str):
    if not _NUMBER.fullmatch(token):
        return token
    negative = token.startswith("m")
    body = token[1:] if negative else token
    value = float(body.replace("p", ".")) if "p" in body else int(body)
    return -value if negative else value


@dataclass
class CountRecovery:
    counts: np.ndarray      # N = (y/e)^2 per bin, rounded when integer; NaN where invalid
    not_integer: np.ndarray  # valid bins to report (integer: beyond the printed digits; else any N != round(N))
    invalid: np.ndarray     # e == 0 with y != 0, or non-finite; the caller masks these, never drops
    max_deviation: float    # largest |N - round(N)| over valid bins (0.0 when there are none)


def recover_counts(y_text: Sequence[str], e_text: Sequence[str], integer: bool) -> CountRecovery:
    """Raw counts from a processed file's printed intensity y and error e, per bin.

    Takes the printed strings, not floats: the digits printed in a bin set how far N may stray
    from an integer. With `integer`, N is rounded to the nearest integer and a bin is reported when
    N strays beyond max(0.25, 4*10^(1-s)*N), s the fewer significant digits printed in that bin's
    y and e. Without it, N stays unrounded and every bin with N != round(N) is reported.
    Zero counts give N = 0 with variance 0 (the caller's sigma^2 = N); no floor is added.
    """
    if len(y_text) != len(e_text):
        raise ValueError(f"recover_counts: {len(y_text)} intensities but {len(e_text)} errors")
    y = np.array([float(t) for t in y_text], dtype=float)
    e = np.array([float(t) for t in e_text], dtype=float)
    sig = np.array([min(_sig_digits(a), _sig_digits(b)) for a, b in zip(y_text, e_text)])
    invalid = ~(np.isfinite(y) & np.isfinite(e)) | ((e == 0) & (y != 0))
    # y == 0 gives N = 0 whatever e is, so the division is only ever taken with e != 0 where it matters.
    n = (np.where(y == 0, 0.0, y) / np.where(e == 0, 1.0, e)) ** 2
    n = np.where(invalid, 0.0, n)
    rounded = np.round(n)
    dev = np.abs(n - rounded)
    tol = np.maximum(0.25, 4 * 10.0 ** (1 - sig) * n) if integer else 0.0
    valid = ~invalid
    return CountRecovery(
        counts=np.where(invalid, np.nan, rounded if integer else n),
        not_integer=valid & (dev > tol),
        invalid=invalid,
        max_deviation=float(dev[valid].max()) if valid.any() else 0.0,
    )


def _sig_digits(text: str) -> int:
    """Significant digits printed in `text`; trailing zeros count, and a zero prints as one digit."""
    mantissa = re.split(r"[eE]", text.strip().lstrip("+-"))[0]
    return max(len(mantissa.replace(".", "").lstrip("0")), 1)


def merge_facts(sources: Mapping[str, Mapping[str, object]], expected: Iterable[str] = (),
                scope: str = "") -> tuple[dict, list[Flag]]:
    """Merge per-source fact dicts into one facts block with the design's precedence.

    `sources` maps a source label (header, sidecar, filename, card) to its {name: value}. The first
    source in PRECEDENCE that has a fact wins; a later source that disagrees is listed under
    `conflicts` and raises a `fact_conflict` flag. A name in `expected` that no source supplies is
    recorded with source `missing`. `scope` prefixes the flag reason (e.g. the analyzer's name).
    """
    unknown = set(sources) - set(PRECEDENCE)
    if unknown:
        raise ValueError(f"merge_facts: unknown fact source(s) {sorted(unknown)}")
    ordered = [label for label in PRECEDENCE if label in sources]
    names = dict.fromkeys([k for label in ordered for k in sources[label]] + list(expected))
    facts: dict = {}
    flags: list[Flag] = []
    for name in names:
        found = [(label, sources[label][name]) for label in ordered if name in sources[label]]
        if not found:
            facts[name] = {"value": None, "source": "missing"}
            continue
        (label, value), rest = found[0], found[1:]
        entry = {"value": value, "source": label}
        clashes = [{"source": other, "value": v} for other, v in rest if v != value]
        if clashes:
            entry["conflicts"] = clashes
            flags.append(Flag("fact_conflict", "warning",
                              f"{scope}{name}: {label} gives {value!r}; "
                              + ", ".join(f"{c['source']} gives {c['value']!r}" for c in clashes)))
        facts[name] = entry
    return facts, flags
