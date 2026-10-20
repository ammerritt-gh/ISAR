"""Make an artificial copy of a real facility file (SPEC .dat/.spec, ILL numor) for parser tests.

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
  3. write the copy and `<copy name>.truth.json` (scans, points, column names, header values as text,
     and each data column's replacement tokens as written) beside it.
A failed check writes nothing and prints what differed. `--scans` only applies to SPEC files: `3` keeps
the first three scans, `5,12` (or `5,`) keeps scans numbered 5 and 12; the default is the first three.
The tool refuses an output folder inside `real_data` / `test_data` and never overwrites a file.
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
from isar.parsers import numor_base, registry  # noqa: E402

log = logging.getLogger("artificial_copy")

FORBIDDEN_DIRS = {"real_data", "test_data"}
SUPPORTED = {"spec_s30": "spec", "spec_s30_raw": "spec", "ill_in8": "numor"}
DEFAULT_SCANS = 3
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


def replace_column(toks: list[bytes], rng: random.Random,
                   label: str = "column") -> tuple[list[bytes], set[int]]:
    """(replacement tokens, rows kept as forced); every other row is changed."""
    if not toks:
        return [], set()
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
    rows: list = dataclasses.field(default_factory=list)   # line indices of data rows


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


def build_copy(lines: list[bytes], kind: str, tables: list[Table], kept: list[Table],
               rng: random.Random) -> tuple[bytes, dict, dict]:
    """(copy bytes, {table.start: (column tokens as written, forced column indexes)},
    {table.start: line index in the copy}).

    Only data-row numbers change; every other byte, line ending included, is copied as it was.
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
            old = [body[sp[j][0]:sp[j][1]] for _, body, _, sp in rows if len(sp) > j]
            new, forced = replace_column(old, rng, f"scan {tb.number if tb.number is not None else tb.start}"
                                                   f" column {j}")
            cols.append(new)
            if forced:
                forced_cols.append(j)
        pos = [0] * width
        for li, body, eol, spans in rows:
            ba = bytearray(body)
            for j, (a, b) in enumerate(spans):
                ba[a:b] = cols[j][pos[j]]
                pos[j] += 1
            if _skeleton(bytes(ba)) != _skeleton(body):
                raise Refuse(f"internal: a non-number byte changed on line {li + 1}")
            new_lines[li] = bytes(ba) + eol
        written[tb.start] = (cols, forced_cols)
    if kind == "numor":                                   # a numor is kept whole
        return b"".join(new_lines), written, {t.start: t.start for t in kept}
    out = new_lines[:tables[0].start]                     # the file-level header
    out_start = {}
    for tb in kept:
        out_start[tb.start] = len(out)
        out += new_lines[tb.start:tb.end]
    return b"".join(out), written, out_start


# ---------------------------------------------------------------------------
# Parse real and copy with ISAR's own parser and compare
# ---------------------------------------------------------------------------
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
            from isar.parsers import spec_base
            mw = card.policy.multiword_columns if name == "spec_s30_raw" else None
            hdr, raw = spec_base.read_spec(path, multiword_columns=mw)
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


def build_truth(name: str, kind: str, copy_name: str, seed: int, lines: list[bytes],
                tables: list[Table], kept: list[Table], written: dict, out_start: dict,
                copy_snap: dict) -> dict:
    scans = []
    for tb in kept:
        key = out_start[tb.start] + 1 if kind == "spec" else tb.start   # block.line_range[0]
        idx = copy_snap["starts"].index(key) if key in copy_snap["starts"] else None
        blk = copy_snap["blocks"][idx] if idx is not None else None
        labels = blk["column_labels"] if blk else []
        cols, forced_cols = written[tb.start]
        scans.append(dict(
            scan_number=tb.number if kind == "spec" else int(
                (blk or {}).get("keywords", {}).get("FILE_", 0) or 0),
            parsed=blk is not None,
            points=blk["rows"] if blk else 0,
            column_names=list(labels),
            forced_columns=[labels[j] if j < len(labels) else f"col{j}" for j in forced_cols],
            header_values=_header_values(lines[tb.start:tb.end], kind),
            data_columns=[dict(name=labels[j] if j < len(labels) else f"col{j}",
                               tokens=[t.decode("ascii") for t in cols[j]]) for j in range(len(cols))]))
    return dict(tool="tools/artificial_copy.py", format=name, copy=copy_name, seed=seed,
                number_of_scans=len(kept),
                file_header_values=(_header_values(lines[:tables[0].start], kind)
                                    if kind == "spec" else {}),
                scans=scans)


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
    try:
        name = registry.default_plugin_name(*registry.detect(src))
    except (ValueError, OSError) as exc:
        raise Refuse(f"unrecognised file format: {exc}") from exc
    if name not in SUPPORTED:
        raise Refuse(f"no copy support for parser {name!r} (supported: {sorted(SUPPORTED)})")
    kind = SUPPORTED[name]
    if kind == "numor" and scans:
        log.warning("--scans ignored: a numor is kept whole")

    lines = src.read_bytes().splitlines(keepends=True)
    tables = locate_spec(lines) if kind == "spec" else locate_numor(lines)
    if not any(t.rows for t in tables):
        raise Refuse("no data rows found by the parser's structure markers")
    kept = choose_scans(tables, scans) if kind == "spec" else tables
    if not any(t.rows for t in kept):
        raise Refuse("the chosen scans hold no data rows")
    copy, written, out_start = build_copy(lines, kind, tables, kept, random.Random(seed))

    with tempfile.TemporaryDirectory(prefix="artificial_copy_") as tmp:    # never the output folder
        probe = Path(tmp) / src.name
        probe.write_bytes(copy)
        real_snap, copy_snap = snapshot(src, name), snapshot(probe, name)
    numbers = {t.number for t in kept} if kind == "spec" else None
    problems = compare(real_snap, copy_snap, name, numbers)
    if problems:
        raise Refuse("the copy does not parse like the real file; nothing written:\n  "
                     + "\n  ".join(problems))
    truth = build_truth(name, kind, src.name, seed, lines, tables, kept, written, out_start, copy_snap)

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
