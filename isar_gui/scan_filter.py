"""ScanFilter — the orthogonal narrowing predicate for the Scans dock (Qt-free).

Grouping (which dimension tab is shown) and *filtering* (which units are visible at all) are separate
concerns. The dimension tabs answer "how is the data arranged"; a :class:`ScanFilter` answers "which
units do I care about right now", and it applies ACROSS every tab — so "group by temperature, show
only analyzer 9" is expressible where before it was not.

This module is deliberately Qt-free (imports only stdlib and the facade's :func:`~isar.workspace_api.scan_list.hkl_bin`),
so the predicate + query parser are unit-testable without a QApplication and nothing Qt leaks toward the read-only facade. The GUI widgets stay dumb: they build a :class:`ScanFilter` and
hand it to the view-model, which applies it.

Two entry points:
  * :class:`ScanFilter` — an immutable-ish value object with :meth:`ScanFilter.matches` (the AND-combined
    predicate over typed fields plus an ambiguous free-text OR clause) and JSON round-trip helpers for
    persistence in ``gui.state.json``.
  * :func:`parse_query` — turns a terse query string (``ana:9 T>100 status:held``, or a bare ``372``)
    into a :class:`ScanFilter`. It NEVER raises: a malformed token yields a best-effort filter plus a
    human error string the query box can surface, so the box degrades rather than crashing.

Query grammar (whitespace-separated tokens, all AND-combined):
  * ``ana:9`` / ``analyzer:9``       -> analyzer/detector id. A purely numeric value matches by
                                        the TRAILING number of the real id (``ana:9`` matches
                                        ``ana9``, ``det9`` and bare ``9`` — but not ``ana19``);
                                        a non-numeric value matches exactly (``ana:ana9``).
  * ``T>100`` ``T<50`` ``T>=4.2`` ``T<=300`` ``T=300``  -> temperature bound(s). ``>``/``<`` are
                                        strict (exclusive); repeated same-side bounds are
                                        last-token-wins.
  * ``status:held``                  -> ledger status prefix (``held`` covers ``held_for_*``; the
                                        display alias ``parsed`` maps back to ``ingested``)
  * ``type:measurement``             -> scan_type
  * ``verdict:accept``               -> fit verdict (read via ``unit.fit.verdict``)
  * ``q:1.5,0,0``                    -> q/HKL, binned with the same ``q_step`` as the q auto-groups
  * a bare word/number (e.g. ``100``) -> AMBIGUOUS free text: matched against scan number, T, q,
                                        q_target, file name, scan_type and analyzer id. Typing ``100``
                                        shows everything mentioning 100; narrow it with a typed prefix.
  * an unknown ``key:value`` token   -> reported as an error (best-effort filter still returned).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace

from isar.workspace_api.scan_list import hkl_bin

log = logging.getLogger(__name__)

# ``T`` comparison token, e.g. ``T>100`` / ``t>=4.2`` / ``T=300``. Case-insensitive on the leading T.
_T_TOKEN = re.compile(r"^[tT](>=|<=|>|<|=)\s*([-+]?\d*\.?\d+)$")

_T_EPS = 1e-6  # float slack so ``T=300`` matches a stored 300.0 without exact-equality brittleness


def _num(x):
    """``float(x)`` or ``None`` — never raises (mirrors the tolerant guards in :mod:`.viewmodels.scans`)."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt_t(t) -> str:
    return "" if t is None else f"{t:g}"


def _fmt_q(hkl) -> str:
    if not hkl:
        return ""
    try:
        return "[" + ", ".join(f"{float(v):.3g}" for v in hkl) + "]"
    except (TypeError, ValueError):
        return ""


@dataclass(frozen=True)
class ScanFilter:
    """An AND-combined narrowing predicate over scan units.

    Every set-valued field is a wildcard when empty; a non-empty field requires membership. The
    numeric ``t_min``/``t_max`` are inclusive bounds. ``q_target`` is a raw HKL vector compared by
    binning at match time (the caller supplies ``q_step``). ``text`` is a free ambiguous clause:
    whitespace-split into tokens, each of which must appear (OR across fields) for the unit to pass.
    """

    analyzers: frozenset = field(default_factory=frozenset)
    statuses: frozenset = field(default_factory=frozenset)
    scan_types: frozenset = field(default_factory=frozenset)
    verdicts: frozenset = field(default_factory=frozenset)
    t_min: float | None = None
    t_max: float | None = None
    t_min_exclusive: bool = False   # True when parsed from strict ``>`` (default keeps old payloads inclusive)
    t_max_exclusive: bool = False   # True when parsed from strict ``<``
    q_target: tuple | None = None
    text: str = ""

    # ---- state ----
    def is_active(self) -> bool:
        return bool(self.analyzers or self.statuses or self.scan_types or self.verdicts
                    or self.t_min is not None or self.t_max is not None
                    or self.q_target is not None or self.text.strip())

    # ---- predicate ----
    def matches(self, unit, *, q_step: float) -> bool:
        """True when ``unit`` satisfies every active clause. Never raises."""
        if self.analyzers and not _analyzer_matches(getattr(unit, "det", None), self.analyzers):
            return False
        if self.statuses and not _status_matches(getattr(unit, "status", None), self.statuses):
            return False
        if self.scan_types and str(getattr(unit, "scan_type", None)) not in self.scan_types:
            return False
        if self.verdicts and _verdict_of(unit) not in self.verdicts:
            return False
        if self.t_min is not None or self.t_max is not None:
            t = _num(getattr(unit, "T", None))
            if t is None:
                return False
            if self.t_min is not None:
                if self.t_min_exclusive:
                    if t <= self.t_min + _T_EPS:    # strict >: the bound itself (± noise) fails
                        return False
                elif t < self.t_min - _T_EPS:
                    return False
            if self.t_max is not None:
                if self.t_max_exclusive:
                    if t >= self.t_max - _T_EPS:    # strict <: the bound itself (± noise) fails
                        return False
                elif t > self.t_max + _T_EPS:
                    return False
        if self.q_target is not None:
            want = hkl_bin(self.q_target, q_step)
            got = hkl_bin(getattr(unit, "q", None), q_step)
            if want is None or got is None or want != got:
                return False
        if self.text.strip():
            hay = _haystack(unit)
            for tok in self.text.split():
                if not _text_token_matches(tok, unit, hay, q_step):
                    return False
        return True

    # ---- persistence (JSON-safe) ----
    def to_dict(self) -> dict:
        """A JSON-serialisable dict (sets -> sorted lists); only active fields are emitted."""
        d: dict = {}
        if self.analyzers:
            d["analyzers"] = sorted(self.analyzers)
        if self.statuses:
            d["statuses"] = sorted(self.statuses)
        if self.scan_types:
            d["scan_types"] = sorted(self.scan_types)
        if self.verdicts:
            d["verdicts"] = sorted(self.verdicts)
        if self.t_min is not None:
            d["t_min"] = self.t_min
            if self.t_min_exclusive:
                d["t_min_exclusive"] = True
        if self.t_max is not None:
            d["t_max"] = self.t_max
            if self.t_max_exclusive:
                d["t_max_exclusive"] = True
        if self.q_target is not None:
            d["q_target"] = list(self.q_target)
        if self.text.strip():
            d["text"] = self.text
        return d

    @classmethod
    def from_dict(cls, d) -> "ScanFilter":
        """Rebuild from :meth:`to_dict` output; a non-dict or garbage value yields the empty filter."""
        if not isinstance(d, dict):
            return cls()

        def _strset(key):
            v = d.get(key)
            return frozenset(str(x) for x in v) if isinstance(v, (list, tuple, set)) else frozenset()

        q = d.get("q_target")
        q_target = None
        if isinstance(q, (list, tuple)):
            nums = [_num(x) for x in q]
            if nums and all(n is not None for n in nums):
                q_target = tuple(nums)
        return cls(
            analyzers=_strset("analyzers"), statuses=_strset("statuses"),
            scan_types=_strset("scan_types"), verdicts=_strset("verdicts"),
            t_min=_num(d.get("t_min")), t_max=_num(d.get("t_max")),
            # Absent flags load as False: old persisted payloads keep their inclusive meaning.
            t_min_exclusive=bool(d.get("t_min_exclusive", False)),
            t_max_exclusive=bool(d.get("t_max_exclusive", False)),
            q_target=q_target, text=str(d.get("text") or ""))


# ---------------------------------------------------------------------------
# Match helpers
# ---------------------------------------------------------------------------
def _analyzer_matches(det, wanted: frozenset) -> bool:
    """True when the unit's detector id matches any wanted token.

    A purely numeric token matches by numeric normalization against the TRAILING number of the
    real id — real detector ids are strings like ``ana9`` (the S30 cards), so ``ana:9`` must reach
    them without the operator typing the prefix; comparing as ints keeps ``ana:9`` from matching
    ``ana19``. Non-numeric tokens (or ids with no trailing number) fall back to exact string match.
    """
    s = str(det) if det is not None else ""
    tail = re.search(r"(\d+)$", s)
    for w in wanted:
        if s == w:
            return True
        if w.isdigit() and tail is not None and int(tail.group(1)) == int(w):
            return True
    return False


def _verdict_of(unit) -> str:
    fit = getattr(unit, "fit", None)
    v = getattr(fit, "verdict", None) if fit is not None else None
    return str(v) if v is not None else ""


def _status_matches(status, wanted: frozenset) -> bool:
    """True when the unit's ledger status matches any wanted token by PREFIX.

    ``held`` therefore covers ``held_for_calibration`` / ``held_for_sample``; ``parsed`` (the display
    label for ``ingested``) is normalised back so the query matches what the chip shows."""
    s = str(status or "")
    for w in wanted:
        w = "ingested" if w == "parsed" else w
        if s == w or s.startswith(w):
            return True
    return False


def _haystack(unit) -> str:
    """A lowercase blob of a unit's human-facing fields for ambiguous substring matching."""
    parts = [
        "" if getattr(unit, "scan_number", None) is None else str(unit.scan_number),
        _fmt_t(getattr(unit, "T", None)),
        _fmt_q(getattr(unit, "q", None)),
        _fmt_q(getattr(unit, "q_target", None)),
        getattr(unit, "file_name", "") or "",
        getattr(unit, "scan_type", "") or "",
        "" if getattr(unit, "det", None) is None else str(unit.det),
        str(getattr(unit, "status", "") or ""),
    ]
    return " ".join(parts).lower()


def _text_token_matches(tok: str, unit, hay: str, q_step: float) -> bool:
    """A single free token matches if it is a substring of the haystack OR matches a number field.

    A numeric token additionally matches the temperature within one ``q_step``-independent slack of 0.5
    (so ``100`` catches ``T=100.2``) and any q component within a ``q_step`` bin — this is what makes a
    bare number feel like "anything mentioning 100"."""
    t = tok.lower()
    if t in hay:
        return True
    n = _num(tok)
    if n is not None:
        temp = _num(getattr(unit, "T", None))
        if temp is not None and abs(temp - n) <= 0.5:
            return True
        for vec in (getattr(unit, "q", None), getattr(unit, "q_target", None)):
            b_want, b_got = hkl_bin((n, n, n), q_step), hkl_bin(vec, q_step)
            if b_want is not None and b_got is not None and any(c == b_want[0] for c in b_got):
                return True
        num = getattr(unit, "scan_number", None)
        if num is not None and float(num) == n:
            return True
    return False


# ---------------------------------------------------------------------------
# Query parser
# ---------------------------------------------------------------------------
def parse_query(text: str) -> tuple[ScanFilter, str | None]:
    """Parse a query string into a :class:`ScanFilter`. Never raises.

    Returns ``(filter, error)`` where ``error`` is ``None`` on a clean parse or a short human message
    naming the first offending token (the filter is still the best-effort parse of the good tokens).
    """
    analyzers: set = set()
    statuses: set = set()
    scan_types: set = set()
    verdicts: set = set()
    t_min: float | None = None
    t_max: float | None = None
    t_min_exclusive = False
    t_max_exclusive = False
    q_target: tuple | None = None
    free: list[str] = []
    error: str | None = None

    for tok in (text or "").split():
        m = _T_TOKEN.match(tok)
        if m:
            op, val = m.group(1), _num(m.group(2))
            if val is None:
                error = error or f"bad temperature value in '{tok}'"
                continue
            # Each comparison sets its bound AND its exclusivity flag (repeated same-side bounds
            # are last-token-wins, so a later inclusive clause clears an earlier strict one).
            if op == ">":
                t_min, t_min_exclusive = val, True
            elif op == ">=":
                t_min, t_min_exclusive = val, False
            elif op == "<":
                t_max, t_max_exclusive = val, True
            elif op == "<=":
                t_max, t_max_exclusive = val, False
            elif op == "=":
                t_min = t_max = val
                t_min_exclusive = t_max_exclusive = False
            continue

        if ":" in tok:
            key, _, value = tok.partition(":")
            key = key.lower()
            value = value.strip()
            if not value:
                error = error or f"empty value in '{tok}'"
                continue
            if key in ("ana", "analyzer"):
                analyzers.add(value)
            elif key == "status":
                statuses.add(value.lower())
            elif key in ("type", "scan_type"):
                scan_types.add(value)
            elif key == "verdict":
                verdicts.add(value)
            elif key == "q":
                nums = [_num(x) for x in value.split(",")]
                if nums and all(n is not None for n in nums):
                    q_target = tuple(nums)
                else:
                    error = error or f"bad q vector in '{tok}'"
            else:
                error = error or f"unknown filter '{key}:' in '{tok}'"
            continue

        # A bare word/number -> ambiguous free text.
        free.append(tok)

    filt = ScanFilter(
        analyzers=frozenset(analyzers), statuses=frozenset(statuses),
        scan_types=frozenset(scan_types), verdicts=frozenset(verdicts),
        t_min=t_min, t_max=t_max,
        t_min_exclusive=t_min_exclusive, t_max_exclusive=t_max_exclusive,
        q_target=q_target, text=" ".join(free))
    return filt, error


def describe(f: ScanFilter) -> str:
    """A short one-line summary of the active clauses, for a banner / status line."""
    if not f.is_active():
        return ""
    bits: list[str] = []
    if f.analyzers:
        bits.append("analyzer " + "/".join(sorted(f.analyzers)))
    if f.statuses:
        bits.append("status " + "/".join(sorted(f.statuses)))
    if f.scan_types:
        bits.append("type " + "/".join(sorted(f.scan_types)))
    if f.verdicts:
        bits.append("verdict " + "/".join(sorted(f.verdicts)))
    if f.t_min is not None and f.t_max is not None:
        if f.t_min == f.t_max and not (f.t_min_exclusive or f.t_max_exclusive):
            bits.append(f"T = {f.t_min:g} K")
        else:
            # Strictness must survive into two-sided ranges: 100 < T ≤ 200, not a bare dash.
            lo = "<" if f.t_min_exclusive else "≤"
            hi = "<" if f.t_max_exclusive else "≤"
            bits.append(f"{f.t_min:g} {lo} T {hi} {f.t_max:g} K")
    elif f.t_min is not None:
        bits.append(f"T {'>' if f.t_min_exclusive else '≥'} {f.t_min:g} K")
    elif f.t_max is not None:
        bits.append(f"T {'<' if f.t_max_exclusive else '≤'} {f.t_max:g} K")
    if f.q_target is not None:
        bits.append("q " + _fmt_q(f.q_target))
    if f.text.strip():
        bits.append(f'“{f.text.strip()}”')
    return ", ".join(bits)


def merged(base: ScanFilter, **changes) -> ScanFilter:
    """A copy of ``base`` with ``changes`` applied (thin wrapper over :func:`dataclasses.replace`)."""
    return replace(base, **changes)
