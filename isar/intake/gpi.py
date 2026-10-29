"""The BL43 gpi files: the beamline's per-set gnuplot macro ``gpi_files/gpi.<set>`` beside the data.

The analyzer files carry only ``#Q= <|Q|> /nm``; the hand-edited macro of their set carries what they lack:
each analyzer's HKL, the analyzer slit (``agaph`` x ``agapv``) and the SARF curve set the scientist used
(``sarf_name``). ``read_gpi`` reads those few literal assignments; ``judge`` accepts or rejects the whole
file on one scalar check (every imported analyzer's ``#Q=`` against the file's ``Q_aNN``, within
``Q_TOLERANCE_NM``), because a stale or pasted file may carry a wrong slit and curve set too.

``check_gpi`` runs at every input check (import, run start, a queued single-file fit): it discovers a
``gpi_files/`` beside the data when no gpi folder is saved, judges every imported scan whose set has a file,
and rewrites ``report/gpi.json`` whole. That report is a check, never a stored value: every reader takes
the latest one (``entries_in_force``), so a file that later fails, disappears or changes simply contributes
what it now gives. Imports only the stdlib and ``isar.intake`` at module level; a parser is imported inside
the functions that need it, so the read side never loads one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from isar.intake import curves, scans
from isar.intake.settings import current_settings, save_gpi_folder

log = logging.getLogger(__name__)

Q_TOLERANCE_NM = 0.15     # required agreement, ruling C1: |#Q= - Q_aNN| <= this, in 1/nm
GPI_CHECK = "report/gpi.json"
SCHEMA = "isar.gpi-check/1"
FOLDER_NAME = "gpi_files"

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUM = re.compile(_NUMBER)
_Q_TEXT = re.compile(rf"\s*({_NUMBER})\s*(?:/\s*nm)?\s*")
_ASSIGN = re.compile(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.*?)\s*")
_HKL = re.compile(r"hkl_a(\d+)_([HKL])")
_Q_NAME = re.compile(r"Q_a(\d+)")
_PLAIN = ("problem_name1", "agaph", "agapv", "sarf_name")


@dataclass(frozen=True)
class GpiMacro:
    path: str                                  # absolute
    sha256: str                                # of the file's bytes
    problem_name: str | None                   # last `problem_name1`
    hkl: dict[str, list[float] | None]         # "a03" -> [h, k, l] from hkl_a03_H/_K/_L; None: partial or unreadable
    q_nm: dict[str, float | None]              # "a03" -> |Q| in 1/nm from Q_a03; None: unreadable
    slit: str | None                           # metadata.slit_label(agaph, agapv); None unless both finite and > 0
    sarf_name: str | None
    unread: tuple[str, ...]                    # one line per known name whose last assignment did not read


def _statements(text: str):
    """``(line number, statement)`` for every ``;``-separated statement, a ``#`` comment outside quotes cut off."""
    for n, line in enumerate(text.splitlines(), start=1):
        quote, start, end, parts = None, 0, len(line), []
        for i, ch in enumerate(line):
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "#":
                end = i
                break
            elif ch == ";":
                parts.append(line[start:i])
                start = i + 1
        parts.append(line[start:end])
        for p in parts:
            yield n, p


def _number(raw: str) -> float | None:
    if not _NUM.fullmatch(raw):
        return None
    v = float(raw)
    return v if math.isfinite(v) else None


def _positive(raw: str) -> float | None:
    v = _number(raw)
    return v if v is not None and v > 0 else None


def _string(raw: str) -> str | None:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"" and raw[0] not in raw[1:-1]:
        return raw[1:-1]
    return None


def _q(raw: str) -> float | None:
    v = _number(raw)
    if v is not None:
        return v
    s = _string(raw)
    m = None if s is None else _Q_TEXT.fullmatch(s)
    return None if m is None else _number(m.group(1))


def read_gpi(path) -> GpiMacro:
    """The facts of one gpi file. OSError when it cannot be read; never ValueError for its content: an
    unreadable known line leaves its field unavailable and is listed in ``unread`` (and logged)."""
    from isar.parsers.metadata import slit_label      # imported here: the read side never loads a parser
    from isar.parsers.sp8_bl43 import detector_id
    path = Path(path).resolve()
    data = path.read_bytes()
    last: dict[str, tuple[int, str]] = {}               # known name -> (line, raw value) of its LAST assignment
    for n, stmt in _statements(data.decode("utf-8", errors="replace")):
        m = _ASSIGN.fullmatch(stmt)
        if m is None:
            continue
        name = m.group(1)
        if name in _PLAIN or _HKL.fullmatch(name) or _Q_NAME.fullmatch(name):
            last[name] = (n, m.group(2))
    unread: list[str] = []

    def take(name, parse):
        if name not in last:
            return None
        n, raw = last[name]
        v = parse(raw)
        if v is None:
            unread.append(f"line {n}: {name} = {raw} does not read")
        return v

    problem_name = take("problem_name1", _string)
    sarf = take("sarf_name", _string)
    sarf = (sarf.strip() or None) if sarf is not None else None      # a blank name is no name
    gaps = [take(k, _positive) for k in ("agaph", "agapv")]
    slit = slit_label(*gaps) if None not in gaps else None
    parts: dict[str, dict[str, float | None]] = {}
    q_nm: dict[str, float | None] = {}
    for name in last:
        if (m := _HKL.fullmatch(name)) is not None:
            parts.setdefault(detector_id(int(m.group(1))), {})[m.group(2)] = take(name, _number)
        elif (m := _Q_NAME.fullmatch(name)) is not None:
            q_nm[detector_id(int(m.group(1)))] = take(name, _q)
    hkl = {det: ([p[a] for a in "HKL"] if all(p.get(a) is not None for a in "HKL") else None)
           for det, p in sorted(parts.items())}
    for line in unread:
        log.warning("gpi: %s: %s; that value is not used", path.name, line)
    return GpiMacro(path=str(path), sha256=hashlib.sha256(data).hexdigest(), problem_name=problem_name,
                    hkl=hkl, q_nm=dict(sorted(q_nm.items())), slit=slit, sarf_name=sarf, unread=tuple(unread))


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def judge(macro: GpiMacro, data_q_nm: Mapping[str, float | None]) -> str | None:
    """None when every imported analyzer's data |Q| and the file's ``Q_aNN`` are both present and agree within
    ``Q_TOLERANCE_NM``; else one line naming the file and the worst analyzers (missing values first, then the
    largest disagreement). Analyzers in the file without a data file are ignored."""
    bad = []
    for det, data in sorted(data_q_nm.items()):
        gpi = macro.q_nm.get(det)
        if not _finite(data):
            bad.append((0, 0.0, f"{det} (data has no |Q|)"))
        elif not _finite(gpi):
            bad.append((0, 0.0, f"{det} (gpi file has no readable Q_{det})"))
        elif abs(data - gpi) > Q_TOLERANCE_NM:
            bad.append((1, -abs(data - gpi), f"{det} (data {data:.2f}, gpi {gpi:.2f} /nm)"))
    if not bad:
        return None
    bad.sort(key=lambda b: (b[0], b[1]))
    named = ", ".join(b[2] for b in bad[:3]) + (f" and {len(bad) - 3} more" if len(bad) > 3 else "")
    return (f"{Path(macro.path).name}: |Q| disagrees with the data for {named} "
            f"(tolerance {Q_TOLERANCE_NM} /nm); nothing from this file is used")


@dataclass(frozen=True)
class GpiScan:
    scan: str                          # import-row key "<experiment_id>:<scan_number>"
    set: str                           # the row's "set"
    file: str                          # absolute path of <gpi folder>/gpi.<set>
    sha256: str | None                 # None when the file could not be read
    accepted: bool
    problem: str | None                # judge()'s line, or "gpi.<set> could not be read (...)"; None when accepted
    q: dict[str, list[float]]          # analyzer -> HKL, only full triplets; {} when rejected
    slit: str | None                   # None when rejected or unavailable
    sarf_name: str | None              # None when rejected or absent
    sarf_setting: str | None           # the named set's slit setting (from its file names); None when unknown
    curves: dict[str, dict | None]     # imported analyzer -> {"file", "sha256"} of the named curve, None: absent; {} without sarf_name
    notes: tuple[str, ...]             # non-rejecting: problem_name1 differs, unread lines, analyzers without HKL, ...


@dataclass(frozen=True)
class GpiCheck:
    checked_at: str                    # UTC ISO-8601
    checked_by: str                    # "import" | "run" | "fit" (a queued single-file fit's start)
    folder: str | None                 # the gpi folder in force, absolute; None: no folder
    setting_source: str | None         # "user" | "discovered"
    found_beside: str | None
    discovered: str | None             # the folder THIS check saved as discovered, else None
    scans: dict[str, GpiScan]          # only scans whose set has a gpi.<set> file in the folder

    def to_dict(self) -> dict:
        return {"schema": SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "GpiCheck":
        d = dict(d)
        if d.pop("schema", None) != SCHEMA:
            raise ValueError(f"not a {SCHEMA} record")
        return cls(**{**d, "scans": {k: GpiScan(**{**s, "notes": tuple(s["notes"])}) for k, s in d["scans"].items()}})

    def lines(self) -> list[str]:
        out = []
        if self.discovered:
            out.append(f"gpi files: found {self.discovered} beside {self.found_beside} "
                       "and saved it as the experiment's gpi folder")
        for s in self.scans.values():
            if s.accepted:
                parts = [f"Q for {len(s.q)} analyzers"] + ([f"slit {s.slit}"] if s.slit else []) + \
                        ([f"curve set {s.sarf_name}"] if s.sarf_name else [])
                out.append(f"gpi files: {Path(s.file).name}: used ({', '.join(parts)})")
            else:
                out.append(f"gpi files: {s.problem}")
            out += [f"gpi files: {n}" for n in s.notes]
        return list(dict.fromkeys(out))            # one line per gpi file, however many scans share it


def _discover(root, data_paths) -> str | None:
    """Save the first ``gpi_files/`` holding a ``gpi.*`` file beside the data as the experiment's gpi folder,
    only when none is saved; the saved path, else None."""
    if current_settings(root).gpi_folder is not None:
        return None
    seen = set()
    for raw in data_paths:
        p = Path(raw)
        d = (p if p.is_dir() else p.parent).resolve()
        g = d / FOLDER_NAME
        if g in seen or not g.is_dir():
            continue
        seen.add(g)
        if not any(f.is_file() and f.name.startswith("gpi.") for f in g.iterdir()):
            continue
        res = save_gpi_folder(root, g, source="discovered", found_beside=str(d))
        if res.ok:
            return res.value
        log.warning("gpi files: %s not saved as the experiment's gpi folder (%s)", g, res.reason)
        return None
    return None


def gpi_folder_in_force(root, data_paths) -> tuple[Path | None, str | None]:
    """The saved gpi folder, after saving a ``gpi_files/`` beside the data as ``discovered`` when none is saved
    (a saved folder is never switched); and the folder this call saved, for the check to report."""
    discovered = _discover(root, data_paths)
    saved = current_settings(root).gpi_folder
    return (None if saved is None else Path(saved.path)), discovered


def _named_curves(card, root, name: str) -> tuple[dict[str, dict], str | None]:
    """Each analyzer's curve of the named set in the resolution folder in force (an absent one is not a key), and
    why none could be looked up at all (None when the folder was read). Independent of any scan's analyzers."""
    folder, pattern, _source = curves.resolution_location(card, root)
    if folder is None or not pattern:
        return {}, "no resolution folder or pattern is in force"
    from isar.parsers import sarf   # imported here: the read side never loads a parser
    try:
        lib = sarf.load_named(folder, pattern, name)
    except (OSError, ValueError, re.error) as exc:
        log.warning("gpi files: the named set %s could not be loaded from %s (%s)", name, folder, exc)
        return {}, f"the resolution folder {folder} could not be read ({exc})"
    return {det: v for (det, _setting), v in sarf.curve_identity(folder, lib).items()}, None


def _setting(card, name: str, first: str | None) -> str | None:
    """The named set's slit setting, read from ``<name>_<first analyzer>`` by the card's pattern."""
    from isar.parsers.metadata import normalise_slit
    pattern = (card.policy.calibration or {}).get("resolution_pattern")
    if not pattern or first is None:
        return None
    try:
        m = re.fullmatch(pattern, f"{name}_{first}")
    except re.error as exc:
        log.warning("gpi files: the card's resolution_pattern does not compile (%s)", exc)
        return None
    setting = None if m is None else m.groupdict().get("setting")
    return None if setting is None else normalise_slit(setting)


def _entry(card, root, key: str, row: dict, file: Path, named_cache: dict) -> GpiScan:
    analyzers = sorted(row.get("analyzers") or {})
    set_name = row["set"]

    def rejected(sha, problem):
        log.warning("gpi files: %s", problem)
        return GpiScan(key, set_name, str(file), sha, False, problem, {}, None, None, None, {}, ())
    try:
        macro = read_gpi(file)
    except OSError as exc:
        return rejected(None, f"gpi.{set_name} could not be read ({exc}); nothing from this file is used")
    problem = judge(macro, {det: (row["analyzers"][det] or {}).get("Q_nm") for det in analyzers})
    if problem is not None:
        return rejected(macro.sha256, problem)
    notes = []
    if macro.problem_name is not None and macro.problem_name != set_name:
        notes.append(f"{file.name}: problem_name1 is {macro.problem_name!r}, not the set {set_name!r}; "
                     "matched by file name")
    notes += [f"{file.name}: {u}; that value is not used" for u in macro.unread]
    q = {det: list(h) for det in analyzers if (h := macro.hkl.get(det)) is not None}
    if missing := [det for det in analyzers if det not in q]:
        notes.append(f"{file.name}: no full HKL (hkl_aNN_H/_K/_L) for {', '.join(missing)}; they keep no Q from it")
    if macro.slit is None:
        notes.append(f"{file.name}: no usable slit (agaph x agapv)")
    setting, named = None, {}
    if macro.sarf_name is not None:
        setting = _setting(card, macro.sarf_name, analyzers[0] if analyzers else None)
        if macro.sarf_name not in named_cache:
            named_cache[macro.sarf_name] = _named_curves(card, root, macro.sarf_name)
        got, why = named_cache[macro.sarf_name]
        named = {det: got.get(det) for det in analyzers}
        if why is not None:
            notes.append(f"{file.name}: curve set {macro.sarf_name}: {why}")
        elif absent := [det for det, c in named.items() if c is None]:
            notes.append(f"{file.name}: curve set {macro.sarf_name} has no curve for {', '.join(absent)}; "
                         "held as a missing curve")
        if setting is None:
            notes.append(f"{file.name}: the slit of curve set {macro.sarf_name} is not in its name")
        elif macro.slit is not None and setting != macro.slit:
            notes.append(f"{file.name}: slit {macro.slit} differs from curve set {macro.sarf_name}'s {setting}")
    return GpiScan(key, set_name, str(file), macro.sha256, True, None, q, macro.slit, macro.sarf_name,
                   setting, named, tuple(notes))


def check_gpi(root, card, data_paths, *, by: str, rows: Mapping[str, dict] | None = None,
              discovered: str | None = None) -> GpiCheck | None:
    """Discover the gpi folder, judge every imported scan whose set has a ``gpi.<set>`` file, and rewrite
    ``report/gpi.json`` whole. None unless the card is BL43's. ``rows`` defaults to every imported scan.
    ``discovered``: the folder an earlier call of this batch saved (``gpi_folder_in_force``), reported here as
    this check's. A failure reading one file is that file's entry; only a failure of the whole check propagates."""
    if not scans.applies(card):
        return None
    root = Path(root)
    discovered = discovered or _discover(root, data_paths)
    saved = current_settings(root).gpi_folder
    rows = scans.read_scans(root) if rows is None else rows
    entries: dict[str, GpiScan] = {}
    if saved is not None:
        folder, named_cache = Path(saved.path), {}
        for key, row in sorted(rows.items()):
            file = folder / f"gpi.{row['set']}"
            if file.is_file():
                entries[key] = _entry(card, root, key, row, file, named_cache)
    check = GpiCheck(checked_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"), checked_by=by,
                     folder=None if saved is None else saved.path,
                     setting_source=None if saved is None else saved.source,
                     found_beside=None if saved is None else saved.found_beside,
                     discovered=discovered, scans=entries)
    _write(root, check)
    return check


def _write(root: Path, check: GpiCheck) -> None:
    from isar.session.atomic import replace_retrying   # imported here: the read side never loads isar.session
    path = root / GPI_CHECK
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(check.to_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n",
                   encoding="utf-8", newline="\n")
    replace_retrying(tmp, path)


def read_gpi_check(root) -> GpiCheck | None:
    """The last written check; None when never checked. ValueError when the file is malformed."""
    path = Path(root) / GPI_CHECK
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return GpiCheck.from_dict(json.loads(text))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"{path} is not a gpi check ({exc})") from exc


def entries_in_force(root) -> dict[str, GpiScan]:
    """What a reader outside a running check hands ``scan_inputs``: the latest check's entries, or {} when there
    is no check, it is malformed (logged), or it was made for another folder than the one saved now (a folder
    chosen since contributes nothing until the next check)."""
    try:
        check = read_gpi_check(root)
    except ValueError as exc:
        log.warning("gpi files: %s; no gpi value is used until the next check", exc)
        return {}
    if check is None:
        return {}
    saved = current_settings(root).gpi_folder
    if check.folder != (None if saved is None else saved.path):
        return {}
    return dict(check.scans)
