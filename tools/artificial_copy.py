"""Make an artificial copy of a real facility file (SPEC .dat/.spec, ILL numor, BL43LXU text) for parser tests.

    python tools/artificial_copy.py <real file> --out <artificial_data subfolder> [--scans N | 5,12]

The copy keeps every byte of the facility's layout (header lines, comments, column names, spacing,
line endings) and replaces EVERY number in the data rows (counts, monitor, scanned axes alike) with
seeded random values of the same textual shape: width, sign, decimals and exponent form are kept, and
so is each column's coarse character (a non-negative integer column stays one; a monotone column stays
monotone in the same direction with the same equal-neighbour pattern; a constant column stays constant).
No measured value survives, so the copy is safe to commit; headers are kept as the facility wrote them.
The one exception is a monotone column whose distinct values fill every value their width allows (a
point counter 0..9, or 10..99): its draw can only be itself, it is fixed by its length and carries no
measured value, so those rows are kept and the column is listed in the truth file's `forced_columns`.

Order of work (nothing is written until step 3 passes):
  1. build the copy in memory (SPEC files are trimmed to the chosen scans; a numor is kept whole);
  2. assert the column characters on the replacements, then parse the real file and the in-memory copy
     with ISAR's own parser (the plugin `isar.parsers.registry` selects) and compare: identical file
     header; for every kept scan the same point count, header values and column names;
  3. write the copy and `<copy name>.truth.json` beside it: scans, points (the data rows the tool located),
     column names (the label line it kept), header values as text, and each data column's replacement
     tokens as written. The truth file is never taken from the parser's reading of the copy; the tool
     refuses if a column's token count differs from the points.
A failed check writes nothing and prints what differed. `--scans` only applies to SPEC files: `3` keeps
the first three scans, `5,12` (or `5,`) keeps scans numbered 5 and 12; the default is the first three.
The tool refuses an output folder inside `real_data` / `test_data` and never overwrites a file.

BL43LXU (SPring-8) files are whole-file tables: the per-analyzer text file of the CsV3Sb5 layout, the
Ba122 three-column export, and the SARF resolution curve (`sarf_*`, no extension) are read as one table
of numeric rows under `#` comment lines (comments are kept as written, except the measured numbers in
them: the trailing `#Q=` / `#T=` values and a SARF curve's `eshift=`, replaced at their width). Their
`.var` sidecar and the SARF `.gpi` summary are `name = value` files: every number is replaced except the
structural ones
(`.var`: the analyzer numbers `ano[i]` and the three settings the reader uses; `.gpi`: `n_lines`).
Two things differ from the SPEC/numor rules above:
  * a BL43LXU energy axis is a bin lattice the parser checks, so a table column that is an exact
    arithmetic progression (one fixed step, at least three rows, decimals) is replaced by the same
    progression shifted by a whole number of steps: the step and the lattice survive and every value
    changes (a file no lattice-checking reader consumes, like a SARF curve, also moves by a fraction of
    a step, so a grid offset a fit may have set does not survive). Its tokens may then change sign or
    digit count, and are right-aligned in the same field so no other byte moves;
  * the check against ISAR's parser is the plugin's own readers (`sp8_bl43.read_member`, `read_var`)
    for the analyzer, Ba122 and `.var` files, and a structural comparison (same lines, same token
    shapes) for the SARF curve and `.gpi`, whose reader arrives in a later slice.

A gnuplot macro `gpi.<set>` (the BL43LXU per-set file beside the data) is read as text: every number is
replaced at its width (an assignment's value, the digits of quoted text and of comments), except the values
of `agaph`, `agapv`, `sarf_name`, `resolution_name` and `problem_name1` (settings and names, not measurements).
Identifiers such as `hkl_a03` stay, so the analyzer numbers still match the data files. The check is ISAR's
own `read_gpi` on both files: the same fields read, the same layout.
"""
from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import logging
import math
import random
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

# Parse with the checkout this file lives in, never an editable install that points elsewhere.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import isar.parsers  # noqa: E402,F401  (registers the plugins by name)
from isar.parsers import numor_base, registry, spec_base  # noqa: E402

log = logging.getLogger("artificial_copy")

FORBIDDEN_DIRS = {"real_data", "test_data"}
SUPPORTED = {"spec_s30": "spec", "spec_s30_raw": "spec", "ill_in8": "numor", "sp8_bl43": "table"}
# `name = value` files: extension -> names whose value is structural and kept (the rest are replaced).
# `.var`: the analyzer numbers and the three settings sp8_bl43 reads. `.gpi`: the SARF line count.
KEYVAL = {".var": {"ano", "silicon_order", "meV_bin_size", "nscans"}, ".gpi": {"n_lines"}}
# Measured numbers on a table's comment lines, replaced at their width: the analyzer files' trailing
# `#Q=` / `#T=` values and a SARF curve's fitted `eshift=`. Group 2 is the number.
_COMMENT_NUMBER = (re.compile(rb"^(#[QT]=\s*)(\S+)(?=\s)"), re.compile(rb"(\beshift=\s*)(\S+?)(?=\))"))
DEFAULT_SCANS = 3
_GRID_SHIFTS = 50          # a progression moves by up to this many whole steps either way
_ATTEMPTS = 2000           # redraws before giving up on "every value changed" (a near-full width has few valid draws)


class Refuse(Exception):
    """The tool will not write a copy; the message says why."""


# ---------------------------------------------------------------------------
# Number tokens: shape, value, and the replacement lattice
# ---------------------------------------------------------------------------
_TOKEN = re.compile(rb"([+-]?)(\d*)(\.?)(\d*)(?:([eE])([+-]?)(\d+))?\Z")
_WORD = re.compile(rb"\S+")


class Shape(NamedTuple):
    sign: bytes
    ni: int            # digits before the decimal point
    dot: bytes
    nd: int            # decimals
    mark: bytes        # e / E / empty
    esign: bytes
    exp: bytes         # exponent digits as written


@functools.lru_cache(maxsize=200_000)
def parse_token(tok: bytes) -> Shape:
    m = _TOKEN.match(tok)
    if not m or not (m[2] or m[4]):
        raise Refuse(f"data row holds a token that is not a plain number: {tok!r}")
    return Shape(m[1], len(m[2]), m[3], len(m[4]), m[5] or b"", m[6] or b"", m[7] or b"")


@functools.lru_cache(maxsize=200_000)
def value_of(tok: bytes) -> Decimal:
    return Decimal(tok.decode("ascii"))


def _exp10(sh: Shape) -> int:
    return int((sh.esign + sh.exp).decode()) if sh.mark else 0


def _lattice(ni: int, nd: int, e0: int, sci: bool, int_like: bool) -> tuple[int, int, int]:
    """(lowest M, step, count) of the mantissa integers M a token of this width can take.

    The value is M * 10**(e0 - nd). A full-width (or normalised scientific) token keeps a non-zero
    leading digit so the width never changes; a one-digit token may lead with 0. Integer columns only
    take M that make the value integral.
    """
    nt = ni + nd
    lo = 10 ** (nt - 1) if (ni >= 2 or sci) else (0 if int_like else 1)
    hi = 10 ** nt - 1
    step = 10 ** max(0, nd - e0) if int_like else 1
    lo = -(-lo // step) * step
    hi = hi // step * step
    if hi < lo:
        raise Refuse(f"no integer value fits a token of width {ni}.{nd} (exponent {e0})")
    return lo, step, (hi - lo) // step + 1


def _fmt(sh: Shape, m_min: int, nd_min: int, e: int | None = None) -> bytes:
    """Render mantissa integer `m_min` (counted at `nd_min` decimals) in the token's own shape.

    `e` sets a new decimal exponent for a scientific token (same digit count and sign style);
    None keeps the exponent as written.
    """
    d = sh.nd - nd_min                    # a token with fewer decimals than the lattice drops trailing zeros
    digits = str(m_min * 10 ** d if d >= 0 else m_min // 10 ** -d).zfill(sh.ni + sh.nd)
    if len(digits) != sh.ni + sh.nd:
        raise Refuse("internal: replacement overflowed its token width")
    tail = sh.esign + sh.exp
    if e is not None:
        if (e < 0 and not sh.esign) or abs(e) >= 10 ** len(sh.exp):
            raise Refuse(f"internal: exponent {e} does not fit the token's exponent form")
        tail = (b"-" if e < 0 else b"+" if sh.esign else b"") + str(abs(e)).zfill(len(sh.exp)).encode()
    return sh.sign + digits[:sh.ni].encode() + sh.dot + digits[sh.ni:].encode() + sh.mark + tail


def _form(tok: bytes) -> tuple:
    """A token's textual shape: everything but its digits (and, for scientific, the exponent value)."""
    sh = parse_token(tok)
    return (sh.sign, sh.ni, sh.dot, sh.nd, sh.mark, bool(sh.esign), len(sh.exp))


def _is_int_like(toks: list[bytes]) -> bool:
    return all(parse_token(t).sign != b"-" and value_of(t) == value_of(t).to_integral_value()
               for t in toks)


def _kind(vals: list[Decimal]) -> str:
    nondec = all(a <= b for a, b in zip(vals, vals[1:]))
    noninc = all(a >= b for a, b in zip(vals, vals[1:]))
    if nondec and noninc:
        return "const"
    return "asc" if nondec else "desc" if noninc else "free"


def _draw_free(toks: list[bytes], rng: random.Random, int_like: bool) -> list[bytes]:
    out = []
    for t in toks:
        sh = parse_token(t)
        lo, step, n = _lattice(sh.ni, sh.nd, _exp10(sh), bool(sh.mark), int_like)
        for _ in range(50):
            new = _fmt(sh, lo + rng.randrange(n) * step, sh.nd)
            if new != t:
                break
        else:
            raise Refuse(f"cannot change the value {t!r}: its width has only one possible value")
        out.append(new)
    return out


def _draw_monotone(toks: list[bytes], rng: random.Random, int_like: bool,
                   kind: str) -> tuple[list[bytes], set[int]]:
    """Replace by distinct-value runs so ordering and the equal-neighbour pattern are preserved.

    Returns the new tokens and the rows kept as forced: a plain segment whose distinct values already
    fill every value its width allows (a point counter 0..9, or 10..99) can only be redrawn as itself,
    carries no measured value, and is kept unchanged.
    """
    shapes = [parse_token(t) for t in toks]
    vals = [value_of(t) for t in toks]
    runs: list[tuple[int, int]] = []                     # [start, end) of equal neighbours
    s = 0
    for i in range(1, len(toks) + 1):
        if i == len(toks) or vals[i] != vals[s]:
            runs.append((s, i))
            s = i
    # Runs sharing sign and integer width share one value range, so draw them together. Plain tokens
    # also keep their exponent form; scientific tokens draw the exponent too, inside the column's own
    # exponent span, so values crossing zero or a decade still order correctly.
    segs: list[list[tuple[int, int]]] = []
    key = None
    for r in runs:
        sh = shapes[r[0]]
        k = (sh.sign, sh.ni, sh.mark)
        if k != key:
            segs.append([])
            key = k
        segs[-1].append(r)
    new = [b""] * len(toks)
    forced: set[int] = set()
    for seg in segs:
        sh0 = shapes[seg[0][0]]
        members = [shapes[j] for a, b in seg for j in range(a, b)]
        sci = bool(sh0.mark)
        # Plain tokens: the finest decimals in the segment set the lattice, and a token written with
        # fewer (SPEC drops trailing zeros: "-5" beside "-4.9") only takes values that fit its own.
        nd_use = min(s.nd for s in members) if sci else max(s.nd for s in members)
        if sci:
            e_lo, e_hi = min(map(_exp10, members)), max(map(_exp10, members))
            if int_like or any(not s.esign for s in members):
                e_lo = max(e_lo, 0)
            cap = 10 ** min(len(s.exp) for s in members) - 1
            e_lo, e_hi = max(e_lo, -cap), min(e_hi, cap)
        else:
            e_lo = e_hi = 0
        lats = []                                          # (e, lowest M, step, count) per exponent
        for e in range(e_lo, e_hi + 1):
            try:
                lats.append((e, *_lattice(sh0.ni, nd_use, e if sci else _exp10(sh0), sci, int_like)))
            except Refuse:
                pass
        total = sum(c for *_, c in lats)
        if total < len(seg):
            raise Refuse(f"{len(seg)} distinct values do not fit a width-{sh0.ni} range "
                         f"with {total} possible values")
        if len(seg) == total and not sci:                 # saturated: every lattice value is in use
            lo, step, _ = lats[0][1:]
            if all(lo <= (m := int(abs(vals[a]).scaleb(nd_use))) < lo + total * step
                   and (m - lo) % step == 0 for a, _ in seg):
                for a, b in seg:
                    new[a:b] = toks[a:b]
                    forced.update(range(a, b))
                continue
        # Runs whose tokens have fewer decimals than the lattice must land on its coarser grid.
        gs = [10 ** max(0, nd_use - min(shapes[j].nd for j in range(a, b))) for a, b in seg]
        if (sh0.sign == b"-") != (kind == "desc"):        # magnitudes fall along the column: order gs low to high
            gs.reverse()
        for _ in range(_ATTEMPTS):                        # redraw this segment alone until all of it changed
            ks = set()                                    # range() cannot hold 10**19 lattice points
            while len(ks) < len(seg):
                ks.add(rng.randrange(total))
            ks = sorted(ks)
            prev = -1
            for i, g in enumerate(gs):
                k = max(ks[i], prev + 1)
                while g > 1 and (lats[0][1] + k * lats[0][2]) % g:
                    k += 1
                ks[i] = prev = k
            if prev >= total:                             # a snapped draw overflowed the top
                continue
            if sh0.sign == b"-":                          # larger value = smaller magnitude
                ks.reverse()
            if kind == "desc":
                ks.reverse()
            cand = {}
            for (a, b), k in zip(seg, ks):
                for e, lo, step, c in lats:
                    if k < c:
                        break
                    k -= c
                for j in range(a, b):
                    cand[j] = _fmt(shapes[j], lo + k * step, nd_use, e if sci else None)
            if all(t != toks[j] for j, t in cand.items()):
                break
        else:
            raise Refuse(f"could not change every value of a width-{sh0.ni} segment of {len(seg)} values")
        for j, t in cand.items():
            new[j] = t
    return new, forced


def _signs(vals: list[Decimal]) -> list[int]:
    return [(b > a) - (b < a) for a, b in zip(vals, vals[1:])]


def verify_column(old: list[bytes], new: list[bytes], label: str = "column",
                  forced: frozenset | set = frozenset()) -> None:
    """Refuse unless `new` keeps `old`'s textual shape and coarse character and changes every value
    (except the `forced` rows, which the draw kept because their width leaves no other choice)."""
    if len(old) != len(new):
        raise Refuse(f"{label}: replacement has {len(new)} values for {len(old)}")
    for i, (a, b) in enumerate(zip(old, new)):
        if a == b and i not in forced:
            raise Refuse(f"{label}: row {i} was not changed")
        if _form(a) != _form(b):
            raise Refuse(f"{label}: row {i} changed shape: {a!r} -> {b!r}")
    old_vals = [value_of(t) for t in old]
    if _kind(old_vals) != "free" and _signs(old_vals) != _signs([value_of(t) for t in new]):
        raise Refuse(f"{label}: ordering / equal-neighbour pattern (monotone, constant) not kept")
    if _is_int_like(old) and not _is_int_like(new):
        raise Refuse(f"{label}: non-negative integer column no longer integer")


def grid_step(toks: list[bytes]) -> Decimal | None:
    """The one fixed step of an exact arithmetic progression of plain decimals (>= 3 rows), else None."""
    if len(toks) < 3:
        return None
    shapes = [parse_token(t) for t in toks]
    if any(s.mark or s.sign == b"+" or s.nd == 0 or s.nd != shapes[0].nd for s in shapes):
        return None
    vals = [value_of(t) for t in toks]
    steps = {b - a for a, b in zip(vals, vals[1:])}
    return steps.pop() if len(steps) == 1 and 0 not in steps else None


def _draw_grid(toks: list[bytes], step: Decimal, rng: random.Random, rooms: list[int],
               free_phase: bool = False) -> list[bytes]:
    """The progression moved by a random non-zero whole number of steps (so a bin lattice keeps its
    phase and every value changes), each token right-aligned to fit the field it came from. With
    `free_phase` (a file no lattice-checking reader consumes) it also moves by a fraction of a step,
    so the grid's offset, which a fit may have set, does not survive."""
    nd = parse_token(toks[0]).nd
    vals = [value_of(t) for t in toks]
    unit = Decimal(1).scaleb(-nd)
    for _ in range(_ATTEMPTS):
        move = rng.randrange(1, _GRID_SHIFTS + 1) * rng.choice((-1, 1)) * step
        if free_phase and step > unit:
            move += rng.randrange(1, int(step / unit)) * unit
        new = [f"{abs(v + move) if v + move == 0 else v + move:.{nd}f}".encode("ascii") for v in vals]
        if all(len(t) <= room for t, room in zip(new, rooms)):
            return new
    raise Refuse(f"could not shift a {len(toks)}-row progression of step {step} inside its fields")


def replace_column(toks: list[bytes], rng: random.Random, label: str = "column",
                   rooms: list[int] | None = None, free_phase: bool = False) -> tuple[list[bytes], set[int]]:
    """(replacement tokens, rows kept as forced); every other row is changed.

    `rooms` (table files only) is the longest token each row's field can hold; with it, an exact
    arithmetic progression is shifted along itself instead of redrawn (see the module docstring)."""
    if not toks:
        return [], set()
    step = grid_step(toks) if rooms is not None else None
    if step is not None:
        new = _draw_grid(toks, step, rng, rooms, free_phase)
        if any(a == b for a, b in zip(toks, new)):
            raise Refuse(f"{label}: a shifted progression kept a value")
        return new, set()
    int_like = _is_int_like(toks)
    kind = _kind([value_of(t) for t in toks])
    for _ in range(_ATTEMPTS):
        new, forced = ((_draw_free(toks, rng, int_like), set()) if kind == "free"
                       else _draw_monotone(toks, rng, int_like, kind))
        if all(a != b for i, (a, b) in enumerate(zip(toks, new)) if i not in forced):
            break
    else:
        raise Refuse(f"{label}: could not change every value of a {kind} column")
    verify_column(toks, new, label, forced)
    return new, forced


# ---------------------------------------------------------------------------
# Locating data rows by the parsers' own structure markers
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Table:
    number: int | None          # SPEC scan number (None for a numor)
    start: int                  # first line index (the #S line / the RRRR banner)
    end: int = 0                # one past the last line
    label: int | None = None    # line index of the column-label line (`#L` / the line after DATA_:)
    rows: list = dataclasses.field(default_factory=list)   # line indices of data rows
    names: list | None = None   # table files: the column names, when the label line gives them


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def locate_spec(lines: list[bytes]) -> list[Table]:
    """spec_base.read_spec's rule: after a `#L` inside a `#S` block, every non-`#` line is a data row."""
    tables: list[Table] = []
    cur: Table | None = None
    collecting = False
    for i, raw in enumerate(lines):
        s = _text(raw).strip()
        if not s:
            continue
        if s.startswith("#S "):
            if cur:
                cur.end = i
            cur = Table(number=int(s.split(maxsplit=2)[1]), start=i)
            tables.append(cur)
            collecting = False
        elif s.split(maxsplit=1)[0] == "#L" and cur is not None:
            collecting = True
            cur.label = i
        elif not s.startswith("#") and collecting and cur is not None:
            cur.rows.append(i)
    if cur:
        cur.end = len(lines)
    return tables


def locate_numor(lines: list[bytes]) -> list[Table]:
    """numor_base.read_numor's rule: per RRRR-banner segment, rows follow DATA_: and the label line."""
    texts = [_text(x).rstrip("\r\n") for x in lines]
    has_banner = any(numor_base._is_banner(t, "R") for t in texts)
    tables: list[Table] = []
    cur: Table | None = None
    state = "header" if not has_banner else "pre"
    ncol = 0
    if not has_banner:
        cur = Table(number=None, start=0)
        tables.append(cur)
    for i, t in enumerate(texts):
        if numor_base._is_banner(t, "R"):
            if cur:
                cur.end = i
            cur = Table(number=None, start=i)
            tables.append(cur)
            state = "header"
        elif state == "header" and t.startswith("DATA_:"):
            state = "label"
        elif state == "label" and t.strip():
            ncol = len(t.split())
            cur.label = i
            state = "rows"
        elif state == "rows":
            toks = t.split()
            if len(toks) >= ncol:
                try:
                    [float(x) for x in toks[:ncol]]
                except ValueError:
                    state = "after"
                    continue
                cur.rows.append(i)
            elif toks:
                state = "after"
    for tb in tables:
        tb.end = tb.end or len(lines)
    return tables


def _is_number_row(s: str) -> bool:
    try:
        [float(x) for x in s.split()]
    except ValueError:
        return False
    return bool(s.strip())


def locate_table(lines: list[bytes]) -> Table:
    """A whole-file table: every non-blank line that is not a `#` comment and is all numbers is a data
    row (a comment after the rows, like BL43LXU's trailing `#T=` line, stays a comment). The label line
    is the last comment above the first row that names exactly one word per column."""
    tb = Table(number=None, start=0, end=len(lines))
    for i, raw in enumerate(lines):
        s = _text(raw).strip()
        if s and not s.startswith("#") and _is_number_row(s):
            tb.rows.append(i)
        elif s and not s.startswith("#"):
            raise Refuse(f"line {i + 1} is neither a comment nor a row of numbers: {s[:40]!r}")
    if not tb.rows:
        return tb
    ncol = len(_text(lines[tb.rows[0]]).split())
    for i in range(tb.rows[0] - 1, -1, -1):
        s = _text(lines[i]).strip()
        if s.startswith("#") and len(s[1:].split()) == ncol:
            tb.label, tb.names = i, s[1:].split()
            break
    return tb


_KEYVAL_LINE = re.compile(rb"^(\s*)([A-Za-z_][\w/]*)(?:\[(\d+)\])?(\s*=\s*)(\S+)(\s*)\Z")


def locate_keyval(lines: list[bytes], ext: str) -> tuple[Table, list]:
    """The `name = number` lines of a `.var` / `.gpi` whose value is replaced: (Table whose rows are
    those line indices, [(line index, stem, token)]). Indexed lines (`name[i] = v`) group by stem."""
    keep = KEYVAL[ext]
    tb = Table(number=None, start=0, end=len(lines))
    found = []
    for i, raw in enumerate(lines):
        m = _KEYVAL_LINE.match(raw.rstrip(b"\r\n"))
        if not m or m[2].decode() in keep:
            continue
        try:
            parse_token(m[5])
        except Refuse:
            continue                                   # a quoted or text value is not a number
        tb.rows.append(i)
        found.append((i, m[2].decode() + ("[]" if m[3] is not None else ""), m[5]))
    return tb, found


# ---------------------------------------------------------------------------
# Build the copy in memory
# ---------------------------------------------------------------------------
def choose_scans(tables: list[Table], scans: str | None) -> list[Table]:
    if scans and "," in scans:
        want = {int(x) for x in scans.split(",") if x.strip()}
        kept = [t for t in tables if t.number in want]
        missing = want - {t.number for t in kept}
        if missing:
            raise Refuse(f"scans not in the file: {sorted(missing)}")
        return kept
    n = int(scans) if scans else DEFAULT_SCANS
    if n < 1:
        raise Refuse("--scans must keep at least one scan")
    return tables[:n]


def _skeleton(raw: bytes) -> bytes:
    return _WORD.sub(b"#", raw)


def _comment_spans(lines: list[bytes], start: int, end: int) -> list[tuple[int, int, int]]:
    """(line, first byte, end byte) of the measured number on each comment line; at most one per line."""
    spans = []
    for i in range(start, end):
        body = lines[i].rstrip(b"\r\n")
        for pat in _COMMENT_NUMBER:
            m = pat.search(body)
            if m:
                spans.append((i, m.start(2), m.end(2)))
                break
    return spans


def build_copy(lines: list[bytes], kind: str, tables: list[Table], kept: list[Table],
               rng: random.Random, free_phase: bool = False) -> tuple[bytes, dict]:
    """(copy bytes, {table.start: (column tokens as written, forced column indexes)}).

    Only data-row numbers change; every other byte, line ending included, is copied as it was.
    `free_phase`: a table whose reader does not check a bin lattice also moves a progression by a
    fraction of its step (see `_draw_grid`).
    """
    new_lines = list(lines)
    written: dict[int, tuple[list[list[bytes]], list[int]]] = {}
    for tb in kept:
        rows = []
        for li in tb.rows:
            body = lines[li].rstrip(b"\r\n")
            rows.append((li, body, lines[li][len(body):], [m.span() for m in _WORD.finditer(body)]))
        width = max((len(r[3]) for r in rows), default=0)
        cols: list[list[bytes]] = []
        forced_cols: list[int] = []
        for j in range(width):
            sel = [sp for _, _, _, sp in rows if len(sp) > j]
            old = [body[sp[j][0]:sp[j][1]] for _, body, _, sp in rows if len(sp) > j]
            # A table token may grow into the blank space left of it (never into the previous word).
            rooms = ([sp[j][1] - (sp[j - 1][1] if j else 0) - (1 if j else 0) for sp in sel]
                     if kind == "table" else None)
            new, forced = replace_column(old, rng, f"scan {tb.number if tb.number is not None else tb.start}"
                                                   f" column {j}", rooms, free_phase)
            cols.append(new)
            if forced:
                forced_cols.append(j)
        pos = [0] * width
        for li, body, eol, spans in rows:
            ba = bytearray(body)
            for j, (a, b) in enumerate(spans):
                tok = cols[j][pos[j]]
                if len(tok) == b - a:
                    ba[a:b] = tok
                else:                                        # a shifted progression: same field, right-aligned
                    left = spans[j - 1][1] if j else 0
                    ba[left:b] = tok.rjust(b - left)
                pos[j] += 1
            same = (lambda x: b" ".join(_skeleton(x).split())) if kind == "table" else _skeleton
            if same(bytes(ba)) != same(body) or len(ba) != len(body):
                raise Refuse(f"internal: a non-number byte changed on line {li + 1}")
            new_lines[li] = bytes(ba) + eol
        if kind == "table":                               # after the data, so the data draws keep their order
            for i, a, b in _comment_spans(lines, tb.start, tb.end):
                body = lines[i].rstrip(b"\r\n")
                (tok,), _ = replace_column([body[a:b]], rng, f"line {i + 1} comment number")
                new_lines[i] = body[:a] + tok + body[b:] + lines[i][len(body):]
        written[tb.start] = (cols, forced_cols)
    if kind in ("numor", "table"):                        # a numor or a table is kept whole
        return b"".join(new_lines), written
    out = new_lines[:tables[0].start]                     # the file-level header
    for tb in kept:
        out += new_lines[tb.start:tb.end]
    return b"".join(out), written


def build_keyval(lines: list[bytes], found: list, rng: random.Random) -> tuple[bytes, list]:
    """(copy bytes, [(stem, new tokens, forced)]): each stem's values (`analeff[1..28]`, or the one
    scalar) are replaced as one column, in place at the same width; every other byte is copied."""
    by_stem: dict[str, list] = {}
    for i, stem, tok in found:
        by_stem.setdefault(stem, []).append((i, tok))
    new_lines = list(lines)
    written = []
    for stem, items in by_stem.items():
        new, forced = replace_column([t for _, t in items], rng, stem)
        for (i, old), tok in zip(items, new):
            body = lines[i].rstrip(b"\r\n")
            m = _KEYVAL_LINE.match(body)
            new_lines[i] = body[:m.start(5)] + tok + body[m.end(5):] + lines[i][len(body):]
        written.append((stem, new, bool(forced)))
    return b"".join(new_lines), written


# ---------------------------------------------------------------------------
# gnuplot macro `gpi.<set>` (BL43LXU): the names and settings kept, every other number replaced
# ---------------------------------------------------------------------------
MACRO_KEEP = frozenset({"agaph", "agapv", "sarf_name", "resolution_name", "problem_name1"})
_MACRO_ASSIGN = re.compile(r"\s*([A-Za-z_]\w*)\s*=(?!=)")
_MACRO_QUOTED = re.compile(r"('[^']*'|\"[^\"]*\")")          # split keeps the quotes as the odd parts
_MACRO_QTEXT = re.compile(r"\d+(?:\.\d+)?")
_MACRO_CODE = re.compile(r"([A-Za-z_]\w*)|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_MACRO_IDENT = re.compile(r"[A-Za-z_]\w*")
_MACRO_RUN = re.compile(r"\d+")


def _outside_quotes(text: str, marks: str):
    """Indexes of the `marks` characters outside quotes: the reader's cut rule (`#` comment, `;` statement)."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in marks:
            yield i


def _draw_number(tok: str, rng: random.Random) -> str:
    """A number token replaced at its width and sign style. `_draw_free` keeps a scientific exponent as
    written, so the exponent is redrawn here inside its own digits."""
    sh = parse_token(tok.encode("ascii"))
    new = _draw_free([tok.encode("ascii")], rng, False)[0].decode("ascii")
    if not sh.mark:
        return new
    width = len(sh.exp)
    lo = -(10 ** width - 1) if sh.esign else 0
    hi = -1 if sh.esign == b"-" else 10 ** width - 1
    e = rng.choice([x for x in range(lo, hi + 1) if x != _exp10(sh)])
    sign = "-" if e < 0 else "+" if sh.esign else ""
    return new[:len(new) - len(sh.esign) - width] + sign + str(abs(e)).zfill(width)


def build_macro(lines: list[bytes], rng: random.Random) -> tuple[bytes, list, list]:
    """(copy bytes, [(line, stem, token)], [(stem, tokens, forced)]) for a gnuplot macro.

    Each line is cut the way the reader cuts it: a `#` comment outside quotes, then `;`-separated
    statements. A statement assigning a kept name is copied; in every other statement each number is
    replaced at its width (an assignment's value, the digits of quoted text), in a comment each digit run.
    Identifiers are never touched. A stem is the statement's first word, `comment` for comment text.
    """
    found: list = []
    written: dict[str, list[bytes]] = {}

    def record(n: int, stem: str, tok: str) -> str:
        found.append((n, stem, tok.encode("ascii")))
        written.setdefault(stem, []).append(tok.encode("ascii"))
        return tok

    def draw(n: int, stem: str, tok: str) -> str:
        return record(n, stem, _draw_number(tok, rng))

    def scramble(n: int, run: str) -> str:
        new = run
        while new == run:
            new = "".join(rng.choice("0123456789") for _ in run)
        return record(n, "comment", new)

    out = []
    for n, raw in enumerate(lines, 1):
        body = raw.rstrip(b"\r\n")
        text = body.decode("latin-1")              # one character per byte: non-ASCII passes through
        cut = next(_outside_quotes(text, "#"), len(text))
        code, comment = text[:cut], text[cut:]
        cuts = [-1, *_outside_quotes(code, ";"), len(code)]
        stmts = []
        for a, b in zip(cuts, cuts[1:]):
            stmt = code[a + 1:b]
            head = _MACRO_ASSIGN.match(stmt)
            if head and head[1] in MACRO_KEEP:
                stmts.append(stmt)
                continue
            first = _MACRO_IDENT.search(stmt)
            stem = first[0] if first else "code"
            parts = _MACRO_QUOTED.split(stmt)
            for k in range(1, len(parts), 2):
                parts[k] = _MACRO_QTEXT.sub(lambda m: draw(n, stem, m[0]), parts[k])
            for k in range(0, len(parts), 2):
                parts[k] = _MACRO_CODE.sub(lambda m: m[1] or draw(n, stem, m[2]), parts[k])
            stmts.append("".join(parts))
        new = ";".join(stmts) + _MACRO_RUN.sub(lambda m: scramble(n, m[0]), comment)
        if re.sub(r"\d", "#", new) != re.sub(r"\d", "#", text):
            raise Refuse(f"internal: line {n} changed beyond its numbers")
        out.append(new.encode("latin-1") + raw[len(body):])
    return b"".join(out), found, [(stem, toks, False) for stem, toks in written.items()]


def snapshot_macro(path: Path) -> dict:
    """A gnuplot macro as ISAR's reader sees it: each line with its digits masked (layout and kept names are
    compared, numbers are not), and which fields read, by name only (never a number)."""
    from isar.intake.gpi import read_gpi
    try:
        m = read_gpi(path)
    except Exception as exc:
        raise Refuse(f"ISAR's reader could not read {path.name}: {type(exc).__name__}: {exc}") from exc
    lines = [re.sub(r"\d", "#", x.decode("latin-1")) for x in path.read_bytes().splitlines()]
    return dict(header=None, blocks=[dict(lines=lines)], records=[dict(
        problem_name=m.problem_name, slit=m.slit, sarf_name=m.sarf_name,
        hkl={k: v is not None for k, v in m.hkl.items()},
        q_nm={k: v is not None for k, v in m.q_nm.items()},
        unread=[re.match(r"line \d+: (\w+)", u)[1] for u in m.unread])])


def _macro_copy(src: Path, out_dir: Path, target: Path, truth_path: Path, seed: int) -> tuple[Path, Path]:
    copy, found, written = build_macro(src.read_bytes().splitlines(keepends=True), random.Random(seed))
    with tempfile.TemporaryDirectory(prefix="artificial_copy_") as tmp:    # never the output folder
        probe = Path(tmp) / src.name
        probe.write_bytes(copy)
        problems = compare(snapshot_macro(src), snapshot_macro(probe), "gpi", None)
    if problems:
        raise Refuse("the copy does not parse like the real file; nothing written:\n  "
                     + "\n  ".join(problems))
    truth = build_truth_keyval("gnuplot_macro", src.name, seed, found, written)
    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(copy)
    with open(truth_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(truth, separators=(", ", ": ")) + "\n")
    return target, truth_path


# ---------------------------------------------------------------------------
# Parse real and copy with ISAR's own parser and compare
# ---------------------------------------------------------------------------
def _multiword(name: str):
    """The card's multi-word `#L` names the parser joins (raw S30 only), else None."""
    return registry.plugin_by_name(name).card_template().policy.multiword_columns \
        if name == "spec_s30_raw" else None


def snapshot(path: Path, name: str) -> dict:
    """What the plugin's parser makes of a file: file header, per-scan blocks and records."""
    plugin = registry.plugin_by_name(name)
    card = plugin.card_template()
    try:
        if name == "ill_in8":
            raw = numor_base.read_numor(path)
            header = None
            blocks = [dict(keywords=b.keywords, params=b.params, column_labels=b.column_labels,
                           rows=len(b.data)) for b in raw]
            starts = [b.line_range[0] for b in raw]
        else:
            hdr, raw = spec_base.read_spec(path, multiword_columns=_multiword(name))
            header = {k: v for k, v in dataclasses.asdict(hdr).items() if k != "raw"}
            blocks = [dict(scan_number=b.scan_number, command=b.command, D=b.D, T=b.T, G=b.G, Q=b.Q,
                           P_groups=b.P_groups, C=b.C, V_groups=b.V_groups, n_columns=b.n_columns,
                           column_labels=b.column_labels, rows=len(b.data)) for b in raw]
            starts = [b.line_range[0] for b in raw]
        records = [dict(scan_number=r.scan_number, scan_type=r.scan_type, raw_header=r.raw_header,
                        points=len(r.scan_axes[0].values)) for r in plugin.parse_file(path, card)]
    except Exception as exc:
        raise Refuse(f"ISAR's parser could not read {path.name}: {type(exc).__name__}: {exc}") from exc
    return dict(header=header, blocks=blocks, starts=starts, records=records)


def _masked_comment(raw: bytes) -> str:
    """A comment line with its measured number masked: where it sits is compared, its value is not."""
    for pat in _COMMENT_NUMBER:
        raw = pat.sub(rb"\1#", raw)
    return _text(raw)


def snapshot_table(path: Path, name: str | None) -> dict:
    """A whole-file table as ISAR reads it: the BL43LXU plugin's own file reader for its analyzer and
    Ba122 files; for a SARF curve (no reader yet) the comment lines, row count and width."""
    try:
        if name == "sp8_bl43":
            from isar.parsers import sp8_bl43
            m = sp8_bl43.read_member(path)
            # the trailer numbers are replaced by a copy: their presence and type are compared, not their value
            header = {k: type(v).__name__ if k in ("Q_nm", "T_K") else v for k, v in m.header.items()}
            return dict(header=None, records=[dict(header=header)], blocks=[dict(
                labelled=m.labelled, rows=len(m.energy), has_exposure=m.monitor is not None,
                has_npts=m.npts is not None)])
        lines = path.read_bytes().splitlines(keepends=True)
        tb = locate_table(lines)
        comments = [_masked_comment(x.rstrip(b"\r\n")) for x in lines if x.lstrip().startswith(b"#")]
        return dict(header=None, records=[], blocks=[dict(
            rows=len(tb.rows), width=len(_text(lines[tb.rows[0]]).split()) if tb.rows else 0,
            comments=comments)])
    except Exception as exc:
        raise Refuse(f"ISAR's reader could not read {path.name}: {type(exc).__name__}: {exc}") from exc


def snapshot_keyval(path: Path) -> dict:
    """A `name = value` file with its replaced values masked, so only structure is compared; a `.var`
    is also read by the plugin's own reader (the analyzer numbers and settings it kept must agree)."""
    ext = path.suffix.lower()
    lines = path.read_bytes().splitlines(keepends=True)
    _, found = locate_keyval(lines, ext)
    masked = {i for i, _, _ in found}
    text = [_KEYVAL_LINE.sub(rb"\1\2\4#\6", l.rstrip(b"\r\n")).decode("utf-8", "replace") if i in masked
            else l.rstrip(b"\r\n").decode("utf-8", "replace") for i, l in enumerate(lines)]
    records = []
    if ext == ".var":
        try:
            from isar.parsers import sp8_bl43
            var = sp8_bl43.read_var(path)
        except Exception as exc:
            raise Refuse(f"ISAR's reader could not read {path.name}: {type(exc).__name__}: {exc}") from exc
        records = [dict(scalars=sorted(var["scalars"]),
                        arrays={k: sorted(v) for k, v in var["arrays"].items()},
                        analyzers=var["arrays"].get("ano"))]
    return dict(header=None, records=records, blocks=[dict(lines=text)])


def _diff(a, b, path: str, out: list) -> None:
    if len(out) >= 25:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only in the {'copy' if k in b else 'real file'}")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            out.append(f"{path}: {len(a)} entries in the real file, {len(b)} in the copy")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out)
    elif isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        pass
    elif a != b:
        out.append(f"{path}: real {a!r} != copy {b!r}")


def compare(real: dict, copy: dict, name: str, kept_numbers: set | None) -> list[str]:
    """Differences between the real file and the copy beyond trimming and replaced numbers."""
    if kept_numbers is not None:                          # SPEC: only the scans the copy kept
        real = dict(real,
                    blocks=[b for b in real["blocks"] if b["scan_number"] in kept_numbers],
                    records=[r for r in real["records"] if r["scan_number"] in kept_numbers])
    out: list[str] = []
    for part in ("header", "blocks", "records"):
        _diff(real[part], copy[part], part, out)
    return out


# ---------------------------------------------------------------------------
# Truth file
# ---------------------------------------------------------------------------
def _header_values(lines: list[bytes], kind: str) -> dict:
    """Header text by tag (SPEC `#V4`, numor `PARAM`), as written in the file, repeats in order."""
    out: dict = {}
    for raw in lines:
        t = _text(raw).strip()
        if not t:
            continue
        if kind == "numor":
            if t.startswith("DATA_:"):
                break
            key, sep, val = _text(raw).rstrip("\r\n").partition(":")
            if sep and key.strip() and " " not in key.strip():
                out.setdefault(key.strip(), []).append(val.strip())
        elif t.startswith("#"):
            tag = t.split(maxsplit=1)[0]
            out.setdefault(tag, []).append(t[len(tag):].strip())
    return out


def _label_names(raw: bytes, kind: str, multiword) -> list[str]:
    """The column names in a kept label line, split the way the parser splits it."""
    if kind == "numor":
        return _text(raw).split()
    t = _text(raw).strip()
    body = t[len(t.split(maxsplit=1)[0]):].strip()        # drop the `#L` tag
    return spec_base._join_multiword(body, multiword) if multiword else body.split()


def build_truth(name: str, kind: str, copy_name: str, seed: int, lines: list[bytes],
                tables: list[Table], kept: list[Table], written: dict) -> dict:
    """The truth file, from what the tool located and wrote: the data rows and label line it kept,
    never the parser's reading of the copy. Refuses if a column's tokens do not number the rows."""
    scans = []
    for tb in kept:
        hv = _header_values(lines[tb.start:tb.end], kind)
        labels = (tb.names or [] if kind == "table" else
                  _label_names(lines[tb.label], kind, _multiword(name)) if tb.label is not None else [])
        cols, forced_cols = written[tb.start]
        points = len(tb.rows)
        for j, col in enumerate(cols):
            if len(col) != points:
                raise Refuse(f"scan {tb.number if tb.number is not None else tb.start}: {points} data "
                             f"rows located but column {j} holds {len(col)} values (a ragged or "
                             f"truncated row); pick other --scans")
        name_of = lambda j: labels[j] if j < len(labels) else f"col{j}"
        scans.append(dict(
            scan_number=(tb.number if kind == "spec" else 0 if kind == "table"
                         else int((hv.get("FILE_") or ["0"])[0] or 0)),
            points=points,
            column_names=list(labels),
            forced_columns=[name_of(j) for j in forced_cols],
            header_values=hv,
            data_columns=[dict(name=name_of(j), tokens=[t.decode("ascii") for t in col])
                          for j, col in enumerate(cols)]))
    return dict(tool="tools/artificial_copy.py", format=name, copy=copy_name, seed=seed,
                number_of_scans=len(kept),
                file_header_values=(_header_values(lines[:tables[0].start], kind)
                                    if kind == "spec" else {}),
                scans=scans)


def build_truth_keyval(format_name: str, copy_name: str, seed: int, found: list, written: list) -> dict:
    """The truth file of a `name = value` file: each replaced stem with the tokens as written (an
    indexed stem lists one token per index, in file order). Refuses if the counts disagree."""
    for stem, new, _ in written:
        n = sum(1 for _, s, _ in found if s == stem)
        if len(new) != n:
            raise Refuse(f"{stem}: {n} lines located but {len(new)} values written")
    return dict(tool="tools/artificial_copy.py", format=format_name, copy=copy_name, seed=seed,
                number_of_scans=1, file_header_values={}, scans=[dict(
                    scan_number=0, points=len(found), column_names=[s for s, _, _ in written],
                    forced_columns=[s for s, _, f in written if f], header_values={},
                    data_columns=[dict(name=s, tokens=[t.decode("ascii") for t in new])
                                  for s, new, _ in written])])


def classify(src: Path) -> tuple[str | None, str]:
    """(plugin name or None, kind). Sidecars and SARF files are told by name (no header to detect);
    everything else by the registry, like the parsers themselves."""
    if src.suffix.lower() in KEYVAL:
        return None, "keyval"
    if src.name.lower().startswith("gpi."):           # a gnuplot macro: read as text, see build_macro
        return None, "macro"
    if src.name.lower().startswith("sarf_") and not src.suffix:
        return None, "table"
    try:
        name = registry.default_plugin_name(*registry.detect(src))
    except (ValueError, OSError) as exc:
        raise Refuse(f"unrecognised file format: {exc}") from exc
    if name not in SUPPORTED:
        raise Refuse(f"no copy support for parser {name!r} (supported: {sorted(SUPPORTED)})")
    return name, SUPPORTED[name]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------
def make_copy(src, out_dir, scans: str | None = None, seed: int = 1) -> tuple[Path, Path]:
    src, out_dir = Path(src).resolve(), Path(out_dir).resolve()
    if not src.is_file():
        raise Refuse(f"not a file: {src}")
    bad = FORBIDDEN_DIRS & {p.lower() for p in out_dir.parts}
    if bad:
        raise Refuse(f"--out is inside a {sorted(bad)[0]} folder (real data never receives output): {out_dir}")
    target, truth_path = out_dir / src.name, out_dir / (src.name + ".truth.json")
    if target == src or target.exists() or truth_path.exists():
        raise Refuse(f"{target} (or its truth file) already exists; the tool never overwrites")
    name, kind = classify(src)
    if kind == "macro":
        return _macro_copy(src, out_dir, target, truth_path, seed)
    if kind in ("numor", "table", "keyval") and scans:
        log.warning("--scans ignored: a %s is kept whole", kind)

    lines = src.read_bytes().splitlines(keepends=True)
    found: list = []
    if kind == "keyval":
        tb, found = locate_keyval(lines, src.suffix.lower())
        tables = kept = [tb]
    elif kind == "table":
        tables = kept = [locate_table(lines)]
    else:
        tables = locate_spec(lines) if kind == "spec" else locate_numor(lines)
    if not any(t.rows for t in tables):
        raise Refuse("no data rows found by the parser's structure markers")
    if kind == "spec":
        kept = choose_scans(tables, scans)
    elif kind == "numor":
        kept = tables
    if not any(t.rows for t in kept):
        raise Refuse("the chosen scans hold no data rows")
    rng = random.Random(seed)
    if kind == "keyval":
        copy, written = build_keyval(lines, found, rng)
    else:
        copy, written = build_copy(lines, kind, tables, kept, rng, name is None)

    with tempfile.TemporaryDirectory(prefix="artificial_copy_") as tmp:    # never the output folder
        probe = Path(tmp) / src.name
        probe.write_bytes(copy)
        if kind == "keyval":
            real_snap, copy_snap = snapshot_keyval(src), snapshot_keyval(probe)
        elif kind == "table":
            real_snap, copy_snap = snapshot_table(src, name), snapshot_table(probe, name)
        else:
            real_snap, copy_snap = snapshot(src, name), snapshot(probe, name)
    numbers = {t.number for t in kept} if kind == "spec" else None
    problems = compare(real_snap, copy_snap, name, numbers)
    if problems:
        raise Refuse("the copy does not parse like the real file; nothing written:\n  "
                     + "\n  ".join(problems))
    if kind == "keyval":
        truth = build_truth_keyval(f"keyval{src.suffix.lower()}", src.name, seed, found, written)
    else:                                                 # a table's comment numbers are replaced: read the copy
        shown = copy.splitlines(keepends=True) if kind == "table" else lines
        truth = build_truth(name or "sarf_curve", kind, src.name, seed, shown, tables, kept, written)

    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(copy)
    with open(truth_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(truth, separators=(", ", ": ")) + "\n")
    return target, truth_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="the real facility file")
    ap.add_argument("--out", required=True, help="artificial_data subfolder to write the copy into")
    ap.add_argument("--scans", help=f"SPEC only: N = first N scans, 5,12 = those scan numbers "
                                    f"(default {DEFAULT_SCANS})")
    ap.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        target, truth = make_copy(args.source, args.out, args.scans, args.seed)
    except Refuse as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {target}\nwrote {truth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
