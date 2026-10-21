"""Ingestion (the retired unification plan §2.3, git history; current contract: docs/WORKSPACE_AND_CLI.md; also docs/ARCHITECTURE.md §7.1) — discover, classify, parse, dedup, order.

Files are the source of truth (U4/§0.5). `discover` walks the data paths, classifies each file
(measurement / calibration-companion / foreign / unrecognized), and `parse_all` parses the
measurement files with the card-pinned plugin, dedups by `(experiment_id, scan_number)` keeping the
last occurrence (growing-file resend), and orders by scan number. `file_gaps` reports holes in the
scan-number sequence — the file-level gap detection `collate.detect_gaps` (value space) does not do.

A card with a `[files]` table (opt-in) also groups the matching files into `FileSet`s — one scan,
many per-detector files, keyed by the regex `set` group within one folder — and a plugin with
`parse_set` parses each set once; everything else is the one-file-one-parse path described above.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from isar.parsers import registry
from isar.parsers.base import FileSet
from isar.schema.card import Card

log = logging.getLogger(__name__)

# Suffixes that are never instrument data — skip without opening (binaries, logs, our own output,
# and the `.UNUSED` relabel marker for retired datasets). `.json` is NOT skipped: TAVI simulator
# scans are JSON and `registry.detect` recognizes them structurally; workspace state JSONs are
# excluded by name below, and unrelated JSON fails detection and lands in `unrecognized`.
SKIP_SUFFIXES = {".nxs", ".log", ".md", ".png", ".csv", ".tmp", ".unused"}
# Our own workspace files — never ingest them even if a data path points at a workspace.
# `gui.state.json` is a GUI-owned per-workspace file (Phase E) — it must never be classified as data.
WORKSPACE_NAMES = {"card.policy.toml", "card.state.json", "session.json", "report.json",
                   "gui.state.json"}


@dataclass
class IngestPlan:
    measurement_files: list[Path] = field(default_factory=list)
    companion_files: list[Path] = field(default_factory=list)   # calibration sources (§3)
    foreign: list[Path] = field(default_factory=list)           # recognized, different instrument
    unrecognized: list[Path] = field(default_factory=list)      # detect() could not classify
    # Card `[files]` only. Set members are ALSO in `measurement_files` (the manifest and the
    # driver's source map read that list); `parse_all`/`ingest_batch` parse them as sets.
    file_sets: list[FileSet] = field(default_factory=list)
    sidecar_files: list[Path] = field(default_factory=list)     # named by `[files] sidecar`, never parsed
    ignored_files: list[Path] = field(default_factory=list)     # matched `[files] ignore`


def _iter_files(data_paths) -> list[Path]:
    out: list[Path] = []
    for raw in data_paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file()))
        elif p.is_file():
            out.append(p)
    # de-dup while preserving order
    seen, uniq = set(), []
    for f in out:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            uniq.append(f)
    return uniq


def _skip(path: Path) -> bool:
    return path.suffix.lower() in SKIP_SUFFIXES or path.name in WORKSPACE_NAMES


def discover(data_paths, card: Card) -> IngestPlan:
    pol = card.policy
    companion_glob = (pol.calibration or {}).get("companion_glob")
    plan = IngestPlan()
    files_cfg = pol.files or {}
    meas_res = [re.compile(p) for p in files_cfg.get("measurement", [])]
    ignore_globs = files_cfg.get("ignore", [])
    sets: dict[tuple, FileSet] = {}            # (folder, `set` group): one folder's files, one set
    rest: list[Path] = []                      # files the `[files]` name rules did not claim
    for f in _iter_files(data_paths):
        if _skip(f):
            continue
        # `[files]` classifies by NAME before anything else, so a processed file with no signature
        # (BL43LXU per-analyzer text) never reaches `registry.detect`. `re.fullmatch` on the name.
        if any(fnmatch.fnmatch(f.name, g) for g in ignore_globs):
            plan.ignored_files.append(f)
            continue
        m = next((m for m in (r.fullmatch(f.name) for r in meas_res) if m), None)
        if m is not None:
            plan.measurement_files.append(f)
            fs = sets.setdefault((f.parent, m.group("set")),
                                 FileSet(key=m.group("set"), folder=f.parent))
            fs.measurements.append((f, m.groupdict()))
            continue
        rest.append(f)
    plan.file_sets = list(sets.values())
    # Sidecars are the `{set}`-templated names of the sets found, beside the set's own folder.
    sidecar_owner: dict[tuple, FileSet] = {}
    for fs in plan.file_sets:
        for template in files_cfg.get("sidecar", []):
            sidecar_owner.setdefault((fs.folder, template.replace("{set}", fs.key)), fs)
    for f in rest:
        owner = sidecar_owner.get((f.parent, f.name))
        if owner is not None:
            owner.sidecars.append(f)
            plan.sidecar_files.append(f)
            continue
        # Companion calibration files are classified by NAME before detection — the S30 companion
        # raw .spec detects as ("APS","S30",...) too, but must never be parsed as measurements (R2).
        if companion_glob and fnmatch.fnmatch(f.name, companion_glob):
            plan.companion_files.append(f)
            continue
        try:
            facility, instrument, _ver = registry.detect(f)
        except (ValueError, OSError):
            plan.unrecognized.append(f)
            continue
        if (facility, instrument) == (pol.facility, pol.instrument):
            plan.measurement_files.append(f)
        else:
            plan.foreign.append(f)
    return plan


def parse_all(plan: IngestPlan, card: Card, session=None, set_of: dict | None = None) -> list:
    """Parse the measurement files with the card-pinned plugin; dedup + order.

    Dedup key is `(experiment_id, scan_number)`; the LAST occurrence wins (a resent growing file
    supersedes an earlier copy). Records are returned sorted by `(experiment_id, scan_number)`.
    ``session`` (the run path) lets a summed set be passed over for individual scans already in the
    ledger, as `ingest_batch` does; nothing else of the ledger is consulted. ``set_of`` receives
    (experiment, scan) -> FileSet for the scans read from a set, for `set_provenance`.
    """
    got = _collect(plan, card, isolate=False, ledger=session)
    if set_of is not None:
        set_of.update(got.set_of)
    return [got.by_key[k] for k in sorted(got.by_key, key=lambda k: (str(k[0]), k[1]))]


@dataclass
class _Collected:
    by_key: dict = field(default_factory=dict)           # (exp, scan) -> record (last wins)
    sources: dict = field(default_factory=dict)          # (exp, scan) -> ordered distinct source paths
    file_scan_keys: dict = field(default_factory=dict)   # resolved path -> [(exp, scan), ...]
    file_errors: dict = field(default_factory=dict)      # resolved path -> "ExcType: msg" / refusal
    set_of: dict = field(default_factory=dict)           # (exp, scan) -> FileSet, for set-sourced scans
    refused: list = field(default_factory=list)          # {"scan","sets":[earlier, later],"reason"}
    passed_over: list = field(default_factory=list)      # {"set","scans","reason"}: summed sets not read


def _listed_scans(fs: FileSet) -> list[int]:
    """The scan numbers a set's file names list, from the card's `scan_numbers` regex group."""
    raw = next((g["scan_numbers"] for _p, g in fs.measurements if g.get("scan_numbers")), "")
    return [int(n) for n in re.findall(r"\d+", raw)]


def _ledger_sets(session) -> dict[str, tuple[str, list[int]]]:
    """file_set stem -> (experiment, scan numbers the set lists), for every set the ledger holds.

    A set's listed scans are its `scan_numbers` fact (stamped at ingest); a set that lists none is
    taken to hold its own scan number. Sets are told apart by the stem, never by scan number."""
    out: dict = {}
    for entry in session.scans.values():
        prov = entry.provenance or {}
        if prov.get("file_set") is None:
            continue
        listed = ((prov.get("scan_facts") or {}).get("scan_numbers") or {}).get("value")
        ok = isinstance(listed, list) and listed and all(isinstance(n, int) for n in listed)
        out[prov["file_set"]] = (entry.scan_id.rsplit(":", 2)[0],
                                 list(listed) if ok else [entry.scan_number])
    return out


def _summed_sets(plan: IngestPlan, ledger=None) -> dict[int, list[int]]:
    """id(set) -> its scans, for each set listing several scans when every one of them is present as
    its own set of the same experiment (the `specfile` group; none means one experiment) in the batch
    or, given ``ledger``, already in that session's ledger.
    Their counts are in those sets already; reading the sum too would use them twice (design R10)."""
    def experiment(fs):
        return next((g["specfile"] for _p, g in fs.measurements if g.get("specfile")), None)

    single = {(experiment(fs), n[0]) for fs in plan.file_sets if len(n := _listed_scans(fs)) == 1}
    if ledger is not None:
        single |= {(exp, n[0]) for exp, n in _ledger_sets(ledger).values() if len(n) == 1}
    return {id(fs): n for fs in plan.file_sets
            if len(n := _listed_scans(fs)) > 1 and all((experiment(fs), s) in single for s in n)}


def _scan_label(rec) -> str | None:
    """The record's `scan_label` fact, read defensively (the `facts` field arrives with the
    schema's fact-source work; a record without it simply has no label)."""
    entry = (getattr(rec, "facts", None) or {}).get("scan_label")
    val = entry.get("value") if isinstance(entry, dict) else entry
    return None if val is None else str(val)


def set_provenance(rec, file_set) -> dict:
    """The ledger provenance a scan read from a set carries, the same for each of its detectors: its
    `scan_label` (identity guard for the next set) and the set's stem as `file_set` (a moved folder
    still matches; `supersede_summed` finds a summed set by it)."""
    extras = {}
    label = _scan_label(rec)
    if label is not None:
        extras["scan_label"] = label
    if file_set is not None:
        extras["file_set"] = file_set.key
    return extras


def _ledger_labels(session) -> dict:
    """(exp, scan) -> (scan_label, file_set) for ledger scans that were ingested from a set.

    The ledger keeps no scan-level row, so the label lives where ingest already stamps scan facts:
    each entry's provenance (``scan_label`` / ``file_set``, written by `ingest_batch`). A set with
    no scan_label is still recorded by its file_set."""
    out: dict = {}
    for entry in session.scans.values():
        prov = entry.provenance or {}
        if prov.get("scan_label") is not None or prov.get("file_set") is not None:
            exp = entry.scan_id.rsplit(":", 2)[0]
            out[(exp, entry.scan_number)] = (prov.get("scan_label"), prov.get("file_set"))
    return out


def _collision(fs: FileSet, name: str, parsed, got: _Collected, ledger_labels, names) -> dict | None:
    """Why a freshly parsed set must not be ingested, or None. Two different sets (the same stem in
    two folders is two) may not share a (experiment, scan) identity in a batch, nor may a set's
    `scan_label` differ from the one the ledger already holds for that identity, nor may the ledger
    hold another set's file_set (the stem) there. `name` is how the set is shown to a person."""
    for rec in parsed:
        key = (rec.experiment_id, rec.scan_number)
        scan = f"{key[0]}:{key[1]}"
        earlier = got.set_of.get(key)
        if earlier is not None and earlier is not fs:
            first = names[id(earlier)]
            return {"scan": scan, "sets": [first, name],
                    "reason": f"sets {first!r} and {name!r} both yield scan {scan}; "
                              f"{name!r} not ingested"}
        label, held = _scan_label(rec), ledger_labels.get(key)
        if held is not None and held[0] is not None and label is not None and held[0] != label:
            prior = held[1] if held[1] is not None else held[0]
            return {"scan": scan, "sets": [prior, name],
                    "reason": f"set {name!r} (scan_label {label!r}) collides with the ledger's "
                              f"{prior!r} (scan_label {held[0]!r}) on scan {scan}; "
                              f"{name!r} not ingested"}
        if held is not None and held[1] is not None and held[1] != fs.key:
            return {"scan": scan, "sets": [held[1], name],
                    "reason": f"set {name!r} collides with the ledger's set {held[1]!r} on scan "
                              f"{scan}; {name!r} not ingested"}
    return None


def _collect(plan: IngestPlan, card: Card, *, isolate: bool, session=None, ledger=None) -> _Collected:
    """Parse the plan's measurement files — the one place file sets are grouped into parse units.

    A plugin with ``parse_set`` gets one call per `FileSet`; every other measurement file is one
    ``parse_file`` call, as ever. The last occurrence of an identity wins, except that a second
    *set* on an identity another set already holds is refused (nothing merged, nothing overwritten).
    ``isolate`` keeps one failing unit from aborting the batch (``ingest_batch``); ``parse_all``
    lets the exception out as before. ``session`` adds the ledger's identity guard; ``ledger`` only lets
    a summed set be passed over for scans the ledger already holds.
    """
    plugin = registry.plugin_by_name(card.policy.plugin)
    parse_set = getattr(plugin, "parse_set", None)
    set_of_path = ({p: fs for fs in plan.file_sets for p, _g in fs.measurements}
                   if parse_set is not None else {})
    ledger_labels = _ledger_labels(session) if session is not None else {}
    # A stem shown to a person carries its folder only when another set shares the stem.
    stems = Counter(fs.key for fs in plan.file_sets)
    names = {id(fs): fs.key if stems[fs.key] == 1 else f"{fs.key} [{fs.folder}]"
             for fs in plan.file_sets}
    got = _Collected()
    summed = _summed_sets(plan, ledger) if parse_set is not None else {}
    done_sets: set[int] = set()
    for f in plan.measurement_files:
        fs = set_of_path.get(f)
        if fs is not None:
            if id(fs) in done_sets:
                continue
            done_sets.add(id(fs))
            if id(fs) in summed:
                scans = summed[id(fs)]
                reason = (f"set {names[id(fs)]!r} sums scans {', '.join(map(str, scans))}, which are present "
                          "as their own sets; not read, so no count is used twice")
                got.passed_over.append({"set": names[id(fs)], "scans": scans, "reason": reason})
                log.info("ingest: %s", reason)
                continue
            set_key, paths = names[id(fs)], [p for p, _g in fs.measurements]
            parse = lambda fs=fs: parse_set(fs, card)  # noqa: E731
        else:
            set_key, paths = None, [f]
            parse = lambda f=f: plugin.parse_file(f, card)  # noqa: E731
        rps = [str(Path(p).resolve()) for p in paths]
        try:
            parsed = parse()
        except Exception as exc:
            if not isolate:
                raise
            # One unparseable file/set must never abort the batch (or the operator loses every good
            # file behind it). Isolate it: log, stamp the manifest rows with the cause, continue.
            for rp in rps:
                got.file_errors[rp] = f"{type(exc).__name__}: {exc}"
            log.warning("ingest: failed to parse %s (%s); skipped, batch continues",
                        set_key if set_key is not None else f, got.file_errors[rps[0]])
            continue
        if set_key is not None:
            clash = _collision(fs, set_key, parsed, got, ledger_labels, names)
            if clash is not None:
                got.refused.append(clash)
                for rp in rps:
                    got.file_errors[rp] = "refused: " + clash["reason"]
                log.warning("ingest: %s", clash["reason"])
                continue
        # A set is ONE source for dedup (its first member), so dedup alerts are per set, not per file.
        src = rps[0]
        seen_here: set[tuple] = set()
        for rec in parsed:
            key = (rec.experiment_id, rec.scan_number)
            got.by_key[key] = rec                    # last occurrence wins (growing-file resend)
            if set_key is not None:
                got.set_of[key] = fs
            else:
                got.set_of.pop(key, None)
            paths_for_key = got.sources.setdefault(key, [])
            if src not in paths_for_key:
                paths_for_key.append(src)
            if key not in seen_here:
                seen_here.add(key)
                for rp in rps:
                    got.file_scan_keys.setdefault(rp, []).append(key)
    return got


@dataclass
class IngestReport:
    """The result of :func:`ingest_batch` — what was seen and how the ledger changed.

    Scan-level ids in ``added``/``updated``/``unchanged`` are the ``"exp:scan"`` form (each scan
    appears in exactly one list, precedence added > updated > unchanged); the ledger itself is keyed
    per-detector (``exp:scan:det``). This is the GUI's parse-preview feed (Phase E consumes it via
    ``--json``). ``dirty`` is False iff nothing changed on the session — the caller SKIPs ``save()``
    then, so a re-ingest no-op does not tick the workspace watch.
    """
    files: list = field(default_factory=list)       # {"path","class","n_scans","scan_ids","n_context"}
    added: list = field(default_factory=list)        # scan-level ids "exp:scan" newly entered
    updated: list = field(default_factory=list)      # existing entries whose content_hash drifted
    unchanged: list = field(default_factory=list)    # re-ingest no-ops
    deduped: list = field(default_factory=list)      # {"scan","kept","superseded":[...]}
    refused: list = field(default_factory=list)      # {"scan","sets":[held, refused],"reason"}: identity collisions
    passed_over: list = field(default_factory=list)  # {"set","scans","reason"}: summed sets whose scans are present
    gaps: list = field(default_factory=list)         # file_gaps output
    last_ingest: dict = field(default_factory=dict)  # persisted, additive operator summary
    dirty: bool = False

    def to_jsonable(self) -> dict:
        return {"files": [dict(f) for f in self.files],
                "added": list(self.added), "updated": list(self.updated),
                "unchanged": list(self.unchanged),
                "deduped": [dict(d) for d in self.deduped],
                "refused": [dict(r) for r in self.refused],
                "passed_over": [dict(p) for p in self.passed_over],
                "gaps": [list(g) for g in self.gaps],
                "last_ingest": dict(self.last_ingest)}


def upsert_ingested_entry(session, rec, det, *, chash, prov) -> str:
    """Upsert one ``(experiment_id, scan_number, det)`` ledger entry at status ``"ingested"``.

    Shared by :func:`ingest_batch` and ``driver.run_batch``'s early-visibility registration (so a
    GUI watcher sees a scan with its counts while its fit still runs). ``prov`` is merged into
    provenance on add/update. Never downgrades a fitted/held/excluded entry whose content is
    unchanged. Returns ``"added" | "updated" | "unchanged"``.
    """
    from isar.session.state import ScanEntry

    key = f"{rec.experiment_id}:{rec.scan_number}:{det}"
    prev = session.get(key)
    if prev is None:
        session.put(ScanEntry(
            scan_id=key, scan_number=rec.scan_number, scan_type=rec.scan_type,
            status="ingested", content_hash=chash, guidelines_hash="",
            detectors={}, provenance=dict(prov)))
        return "added"
    if prev.content_hash == chash:
        # Same content: leave the entry COMPLETELY untouched — never downgrade a
        # fitted/held/excluded status, never re-stamp provenance.
        return "unchanged"
    prev.content_hash = chash
    prev.status = "ingested"
    prev.provenance = {**(prev.provenance or {}), **prov}
    return "updated"


def supersede_summed(session) -> bool:
    """Mark `excluded` every ledger entry of a summed set whose individual scans are all in the ledger.

    R10: ISAR combines scans itself, so once the individual scans of a set the beamline already summed
    are present as their own sets of the same experiment, the sum is a second copy of the same counts.
    A set is told by its stem (`file_set`), never by scan number; one with only some of its scans present
    stays active. Nothing is deleted: the entry, its provenance and its unit data stay. The fit outcome is
    cleared, as for any entry the exclusion gate re-gates, so no table or report counts it again. Returns
    whether anything changed; a second call changes nothing."""
    sets = _ledger_sets(session)
    singles = {(exp, n[0]) for exp, n in sets.values() if len(n) == 1}
    changed = False
    for stem, (exp, scans) in sets.items():
        if len(scans) < 2 or not all((exp, s) in singles for s in scans):
            continue
        reason = (f"summed set {stem!r} lists {exp} scans {', '.join(map(str, scans))}, which are in the "
                  "ledger as their own sets; excluded so no count is used twice")
        for entry in session.scans.values():
            if (entry.provenance or {}).get("file_set") != stem or reason in entry.reasons:
                continue
            entry.reasons = [*entry.reasons, reason] if entry.status == "excluded" else [reason]
            entry.status, entry.detectors, changed = "excluded", {}, True
            log.info("ingest: %s: %s", entry.scan_id, reason)
    return changed


def ingest_batch(card: Card, session, data_paths, *, data_dir=None) -> IngestReport:
    """Parse ``data_paths`` into the session ledger WITHOUT fitting (the retired unification plan §2.3, git history; current contract: docs/WORKSPACE_AND_CLI.md).

    Discovers/classifies files (reusing :func:`discover`), parses the measurement files with the
    card-pinned plugin, dedups by ``(experiment_id, scan_number)`` last-wins, and upserts one ledger
    entry per ``(experiment_id, scan_number, detector)`` at status ``"ingested"``. Never fits, never
    deletes, never writes ``card.policy.toml``. Precedence contract (the load-bearing part): an
    unchanged re-ingest leaves a fitted/held/excluded entry COMPLETELY untouched — status is only
    ever driven up to ``"ingested"`` when the parsed content actually drifted.

    When ``data_dir`` is given, a per-unit data file (``isar.unit_data/1``, see
    :mod:`isar.session.unit_data`) is written under ``data_dir/ingest/`` for every added/updated
    entry, AND backfilled for any ``unchanged`` entry whose canonical file is missing, corrupt,
    identity-mismatched, or legacy-only (cheap, no ledger mutation — lets a re-ingest repair a
    pre-Phase-2 workspace without ticking ``dirty``). A unit
    write failure is logged and skipped; it never aborts the batch.

    Mutates ``session`` in place; the caller persists it when ``report.dirty`` is true. Every call
    returns additive ``last_ingest`` evidence on the report (so an operator can distinguish a
    successful no-op from no ingest at all), but that evidence is only written onto ``session`` on a
    changing ingest — an idempotent re-ingest leaves ``session`` byte-identical and ``dirty`` False.
    """
    from isar.reduce import combine, qc
    from isar.session import router, unit_data
    from isar.session.state import content_hash

    def _write_unit(rec, det, det_state):
        """Write changed data, or repair a non-canonical unchanged unit; isolate failures."""
        if data_dir is None:
            return
        try:
            if det_state in ("added", "updated"):
                unit_data.write_unit_data(rec, det, data_dir)
            else:  # unchanged: repair absent/torn/mismatched/legacy-only canonical data
                if not unit_data.canonical_unit_data_is_valid(
                    data_dir, rec.experiment_id, rec.scan_number, det,
                ):
                    unit_data.write_unit_data(rec, det, data_dir)
        except Exception as exc:  # noqa: BLE001 — one bad unit must never abort the batch
            log.warning("ingest: failed to write unit data for %s:%s:%s (%s); continuing",
                        rec.experiment_id, rec.scan_number, det, exc)

    report = IngestReport()
    plan = discover(data_paths, card)

    # --- parse measurement files/sets, tracking record -> source file(s) for dedup reporting ---
    got = _collect(plan, card, isolate=True, session=session, ledger=session)
    by_key, sources, file_scan_keys = got.by_key, got.sources, got.file_scan_keys
    file_errors = got.file_errors                    # resolved path -> "ExcType: msg" / refusal
    report.refused = got.refused
    report.passed_over = got.passed_over

    records = [by_key[k] for k in sorted(by_key, key=lambda k: (str(k[0]), k[1]))]

    # Partition exactly like the driver (driver.py: `router.route` then ledger only
    # `routed.measurements`): calibration/alignment/environment/unknown scans are CONTEXT, not
    # results — ledgering them would strand phantom `ingested` entries that never fit and re-queue
    # every run. We do NOT apply context updates here (that mutates card.state — a run-time job).
    routed = router.route(records)
    measurement_keys = {(r.experiment_id, r.scan_number) for r in routed.measurements}

    # Dedup alerts: any scan whose surviving record was sourced from more than one file.
    for key in sorted(sources, key=lambda k: (str(k[0]), k[1])):
        paths = sources[key]
        if len(paths) > 1:
            report.deduped.append({"scan": f"{key[0]}:{key[1]}",
                                   "kept": paths[-1], "superseded": paths[:-1]})

    # --- upsert one ledger entry per (exp, scan, det), MEASUREMENTS ONLY ---
    now_iso = datetime.now(timezone.utc).isoformat()
    ledgered_keys: set[tuple] = set()                # measurement scans that produced ≥1 entry
    analyzer_unit_count = 0                           # atomic scan × analyzer units in this ingest
    for rec in routed.measurements:
        scan_short = f"{rec.experiment_id}:{rec.scan_number}"
        # Hash the SAME canonical form the driver hashes (reduced + QC'd, both in-place no-ops where
        # not applicable and neither raises) so a re-ingest after `isar run` reads as "unchanged" and
        # never downgrades a fitted/held/excluded entry. content_hash on the raw record would drift
        # from the fit's stored hash and silently re-queue the whole workspace on every parse-preview.
        combine.reduce_record(rec, card)
        qc.qc_record(rec, card)
        chash = content_hash(rec)
        T = unit_data.scan_temperature(rec)
        src = sources[(rec.experiment_id, rec.scan_number)][-1]   # the kept (last-wins) source file
        scan_extras = set_provenance(rec, got.set_of.get((rec.experiment_id, rec.scan_number)))
        det_states: list[str] = []                                # per-detector upsert outcomes
        touched = False                                          # any detector entry produced?
        for det, sp in rec.spectra.items():
            touched = True
            analyzer_unit_count += 1
            q = [float(v) for v in sp.q] if sp.q is not None else None
            extras = unit_data._provenance_extras(rec, det)       # scan + analyzer, additive + tolerant
            extras = {**extras, **scan_extras}
            prov_add = {"source_file": src, "T": T, "q": q, "ingested_at": now_iso, **extras}
            det_state = upsert_ingested_entry(session, rec, det, chash=chash, prov=prov_add)
            if det_state != "unchanged":
                report.dirty = True
            _write_unit(rec, det, det_state)
            det_states.append(det_state)
        if touched:
            # A no-spectra measurement produces no ledger entry — keep it out of all three lists.
            ledgered_keys.add((rec.experiment_id, rec.scan_number))
            # Collapse per-detector states: "added" only when every detector entry is new; any other
            # change to a known scan (a late set member's new detector, or a drifted entry) is "updated".
            if all(s == "added" for s in det_states):
                report.added.append(scan_short)
            elif any(s != "unchanged" for s in det_states):
                report.updated.append(scan_short)
            else:
                report.unchanged.append(scan_short)

    # Summed sets from earlier batches whose individual scans are now in the ledger (this batch's included).
    if supersede_summed(session):
        report.dirty = True

    # --- file manifest for ALL classified files (merge, never replace) ---
    # For a measurement-class FILE, its scans still split into measurement scans (ledgered, listed
    # in scan_ids) and CONTEXT scans routed out (counted as n_context, never ledgered). scan_ids
    # therefore maps 1:1 to ledger entries; n_scans == len(scan_ids); n_context is the honest home
    # for the in-file calibration/alignment/environment scans a run would consume as context.
    _classes = [(plan.measurement_files, "measurement"), (plan.companion_files, "companion"),
                (plan.foreign, "foreign"), (plan.unrecognized, "unrecognized")]
    for files, klass in _classes:
        for f in files:
            rp = str(Path(f).resolve())
            try:
                stat = Path(f).stat()
            except OSError as exc:
                log.warning("ingest: could not stat %s (%s); manifest row skipped", f, exc)
                continue
            keys = file_scan_keys.get(rp, []) if klass == "measurement" else []
            scan_ids = [f"{e}:{s}" for (e, s) in keys if (e, s) in ledgered_keys]
            n_context = sum(1 for k in keys if k not in measurement_keys)
            err = file_errors.get(rp)                # a measurement file that failed to parse
            row = {"size": stat.st_size, "mtime": stat.st_mtime, "format": klass}
            if klass == "measurement":
                row["n_scans"] = len(scan_ids)
                row["scan_ids"] = list(scan_ids)
                row["n_context"] = n_context
                if err is not None:
                    row["error"] = err               # additive key; class stays "measurement"
            target = session.files.setdefault(rp, {})
            before = dict(target)
            target.update(row)
            # A file that once errored and now parses clean must lose its stale error key.
            if err is None and "error" in target:
                del target["error"]
            if target != before:
                report.dirty = True
            entry = {"path": rp, "class": klass, "n_scans": len(scan_ids),
                     "scan_ids": list(scan_ids), "n_context": n_context}
            if err is not None:
                entry["error"] = err
            report.files.append(entry)

    # --- remember new data locations (mirror run_batch) ---
    for p in map(str, data_paths):
        if p not in session.data_paths:
            session.data_paths.append(p)
            report.dirty = True

    report.gaps = file_gaps(records)
    # This is a snapshot of *this* operation, not a count reconstructed from the whole ledger.
    # Incremental and watched ingest must not imply old scans arrived just now.
    report.last_ingest = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "logical_scan_count": len(ledgered_keys),
        "analyzer_unit_count": analyzer_unit_count,
        "failed_file_count": len(file_errors),
        "gaps": [list(gap) for gap in report.gaps],
    }
    # ``last_ingest`` is operation evidence carried on the report (--json / GUI parse-preview) for
    # THIS call regardless. Persist it onto the session only when the ingest actually changed the
    # ledger: an idempotent re-ingest must stay a true no-op (``dirty`` False, session byte-identical)
    # so the watched loop never ticks the workspace on unchanged data — see the ``IngestReport.dirty``
    # contract above and tests/session/test_ingest_batch.py.
    if report.dirty:
        session.last_ingest = dict(report.last_ingest)
    return report


def file_gaps(records) -> list[tuple[str, int, int]]:
    """Holes in the scan-number sequence per experiment: `(experiment_id, lo, hi)` for each gap
    where scan numbers jump from `lo` to `hi` (hi > lo + 1). A silent hole in the dispersion is the
    worst failure for a speed tool, so this is surfaced in status and the report header.
    """
    by_exp: dict[str, list[int]] = {}
    for rec in records:
        by_exp.setdefault(str(rec.experiment_id), []).append(rec.scan_number)
    gaps: list[tuple[str, int, int]] = []
    for exp, nums in by_exp.items():
        nums = sorted(set(nums))
        for a, b in zip(nums, nums[1:]):
            if b > a + 1:
                gaps.append((exp, a, b))
    return gaps
