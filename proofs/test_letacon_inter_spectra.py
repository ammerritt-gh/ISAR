"""PROOF (real data, run on purpose): the inter-spectra second pass on the le Tacon BL43LXU CsV3Sb5 spectra, the
302 K and 97 K sets in ONE workspace, driven through the analysis service as the GUI drives it.

Gated like the other le Tacon proofs: the module is skipped unless ``ISAR_LETACON_INTER=1``. Run it with
``tools/run-isar-proofs.bat inter`` or

    ISAR_LETACON_INTER=1 python -m pytest proofs/test_letacon_inter_spectra.py -q -s

What it does, in the order the tests report it:

1. Both temperature folders go through ``isar init`` / ``isar ingest`` and ``isar run`` as one workspace: the fast
   pass of 56 spectra. Each spectrum's fast-pass record and ``report/results.csv`` are hashed before any batch.
2. The user's anchors (the expectation file) are written through the intake anchor writer, as the GUI does.
3. An anchored batch (spread in T from the anchors) and then a simultaneous batch (every spectrum) each run as an
   ``inter`` item of ``python -m isar.session.analysis_service``, started with the BatchSpec the GUI sends.
4. Checks: the T-links exist (precondition), accounting per batch, the fast solo fits untouched, the authoritative
   fits follow the rules, the moving mode (B at the analyzer named in the expectation file) is flagged or held by its
   neighbours, the flat modes are quiet, the flag map, and the timing.

The 30-minute figure is a target, not a limit: timing is printed and written to ``summary.json``, never asserted.
Numbers the operator sets in the expectation file are asserted only when they are not null. A missing real file or
expectation key is an error naming its path, never a skip.

Expectation file ``real_data/leTacon_SP8_CsV3Sb5/test_letacon_inter_spectra.expect.json`` (not committed):
``{analyzer, moving_mode, flat_modes, anchors{temperature, analyzers: "all" or [detector names]},
paper{source, T: {A..D: {omega0, ...}}}, match_tolerance_meV|null, flat_shift_tolerance_meV|null,
max_quiet_flagged_fraction|null}``. Results land in ``out/letacon_inter_spectra/<timestamp>/`` (gitignored):
the workspace, the service's log and ``summary.json``.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from isar.intake import inter_values
from isar.intake.inter_spectra import read_inter_settings
from isar.schema.card import load_policy
from isar.session.inter_spectra import records, runner
from isar.session.inter_spectra.graph import anchored_scope, build_graph
from isar.session.inter_spectra.layout import grid_for
from isar.session.inter_spectra.nodes import read_nodes
from isar.session.inter_spectra.preview import preview_anchored, preview_simultaneous
from isar.session.inter_spectra.q_provider import q_coordinates
from isar.session.inter_spectra.records import Flag
from isar.session.inter_spectra.rings import plan_anchored
from isar.session.inter_spectra.schedule import plan_simultaneous
from isar.session.workspace import POLICY_NAME
from isar.workspace_api.inter_spectra import inter_overview, is_flagged
from proofs import inter_spectra_checks as chk
from proofs.conftest import REAL_DATA
from proofs.letacon_common import (BATCH_WORKERS, FOLDER, N_ANALYZERS, ROOT, TEMPERATURES, _child_env, _fits,
                                   _init_workspace, _isar, _timings)

if os.environ.get("ISAR_LETACON_INTER") != "1":
    pytest.skip("the inter-spectra proof runs only when named: tools/run-isar-proofs.bat inter "
                "(sets ISAR_LETACON_INTER=1)", allow_module_level=True)

# The batches run for as long as they take; every subprocess here is stopped by its own mechanism, so the global
# per-test timeout is switched off for this proof (as in the fast-pass proof).
pytestmark = pytest.mark.timeout(0)

EXPECT = "test_letacon_inter_spectra.expect.json"
TARGET_S = 1800.0                       # the design's 30-minute target: printed against, never asserted
WAIT_LIMIT_S = 6 * 3600.0               # silence from the service this long fails the proof (a stall, not a target)
RESULTS: dict = {}                      # filled by the tests in file order; the last test writes summary.json


@dataclass(frozen=True)
class _Batch:
    label: str; spec: runner.BatchSpec; end: dict; wall: float; segments: list


# ---------------------------------------------------------------- helpers
def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _snapshot(root: Path) -> dict:
    """SHA-256 of each spectrum's fast-pass record and of report/results.csv: the frozen Stage II/III contract."""
    return {"fast": {f"{t}:{det}": _digest(fp) for (t, det), (_entry, fp) in _fits(root).items()},
            "results_csv": hashlib.sha256((root / "report" / "results.csv").read_bytes()).hexdigest()}


def _build(root: Path, ns, scope, kinds):
    """The link graph of a scope, built as the GUI's preview builds it (the q provider only when q-links are asked)."""
    policy = load_policy(root / POLICY_NAME)
    qc = q_coordinates(root, scope) if "q" in kinds else None
    return build_graph(ns.nodes, ns, scope=scope, kinds=kinds, grid=grid_for(policy.plugin),
                       coords=qc.points if qc else {}, collinearity_tol=read_inter_settings(root).collinearity_tol,
                       q_reasons=qc.reasons if qc else None)


def _key(nodes, T: int, det: str) -> str:
    for k, n in nodes.nodes.items():
        if n.T is not None and round(n.T) == T and n.detector == det:
            return k
    pytest.fail(f"no spectrum of analyzer {det} at {T} K in the import record", pytrace=False)


def _as_flag(info, line) -> Flag | None:
    """A facade flag (what the scan list shows) as the proof's Flag. A reason the refit line stores keeps its branch
    ids; one the facade adds (a seam, a drift, a stale lock) names no branch: ids None."""
    if info is None:
        return None
    stored = {(r.get("code"), r.get("text", "")): r["ids"] for r in (line.flag.reasons if line and line.flag else ())}
    return Flag(info.score, info.flagged,
                tuple({"code": r.code, "severity": r.severity, "text": r.detail,
                       "ids": stored.get((r.code, r.detail))} for r in info.reasons))


def _authoritative_line(log, by_key, key):
    """The stored line of a spectrum's authoritative refit; None when its fast solo fit stands."""
    fit = by_key[key].fit_id if key in by_key else "fast"
    return next((r for r in log.refits if r.fit == fit), None)


def _peaks_of(root: Path, line, fast_fp: dict) -> list[dict]:
    """The modes a spectrum shows: its authoritative refit's (from the stored overlay), else the fast solo's."""
    if line is None:
        return fast_fp.get("peaks") or []
    payload = records.read_payload(root, line)
    if payload is None or payload.get("overlay") is None:
        pytest.fail(f"refit {line.fit} of {line.unit} has no drawable overlay; its modes cannot be read", pytrace=False)
    return payload["overlay"]["fast_pass"]["peaks"]


def _segments(events: list[dict]) -> list:
    """(stage, index, first t) at each change of ring or sweep in the batch's progress: where its time went."""
    out: list = []
    for e in events:
        if e["ev"] == "progress" and (not out or out[-1][:2] != (e.get("stage"), e.get("index"))):
            out.append((e.get("stage"), e.get("index"), round(e.get("t", 0.0), 1)))
    return out


class _Service:
    """One real analysis-service process, driven as the GUI drives it: JSON lines on its stdin and stdout. A thread
    collects the protocol lines as they arrive; stderr goes to a log file in the output folder."""

    def __init__(self, root: Path, log: Path) -> None:
        self.log = log
        self._err = open(log, "w", encoding="utf-8", newline="\n")
        self.proc = subprocess.Popen([sys.executable, "-m", "isar.session.analysis_service", "--workspace", str(root)],
                                     cwd=ROOT, env=_child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._err)
        self.events: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.wait_for(lambda e: e.get("ev") == "ready", "the service to start")

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {"ev": "<not json>", "line": raw.decode("utf-8", "replace")}
            with self._cv:
                self.events.append(ev)
                self._cv.notify_all()

    def send(self, cmd: dict) -> None:
        self.proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait_for(self, pred, what: str) -> dict:
        """The first event matching ``pred``. A service that exits meanwhile fails the proof, and so does one that
        stays silent for WAIT_LIMIT_S: it is stopped first, so the run ends with a named failure, not a hang."""
        deadline = time.monotonic() + WAIT_LIMIT_S
        with self._cv:
            while True:
                hit = next((e for e in self.events if pred(e)), None)
                if hit is not None:
                    return hit
                if self.proc.poll() is not None:
                    pytest.fail(f"the analysis service exited ({self.proc.returncode}) waiting for {what}; "
                                f"log: {self.log}", pytrace=False)
                left = deadline - time.monotonic()
                if left <= 0:
                    self._kill()
                    pytest.fail(f"the analysis service gave no answer for {WAIT_LIMIT_S / 3600:g} h while waiting for "
                                f"{what}; it was stopped; log: {self.log}", pytrace=False)
                self._cv.wait(timeout=min(5.0, left))

    def _kill(self) -> None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True)

    def run(self, item_id: str, spec: runner.BatchSpec) -> tuple[dict, list[dict], float]:
        """Start one batch as an ``inter`` item (the GUI's start command) and wait for its finish."""
        t0 = time.monotonic()
        self.send({"op": "start", "item_id": item_id, "unit_key": f"inter:{spec.batch_id}:batch", "pass": "inter",
                   "cores": BATCH_WORKERS, "batch": spec.to_json()})
        end = self.wait_for(lambda e: e.get("ev") == "finished" and e.get("item_id") == item_id, f"batch {item_id}")
        with self._cv:
            events = [e for e in self.events if e.get("item_id") == item_id]
        return end, events, time.monotonic() - t0

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.send({"op": "shutdown"})
                self.proc.wait(timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"\nthe analysis service did not shut down ({exc}); stopping its process tree")
                self._kill()
        self._err.close()


def _run_batch(service: _Service, item_id: str, spec: runner.BatchSpec, label: str) -> _Batch:
    end, events, wall = service.run(item_id, spec)
    if end.get("end") != "completed":
        pytest.fail(f"the {label} batch ended {end.get('end')}: {end.get('reason')}; service log: {service.log}",
                    pytrace=False)
    names = [e["ev"] for e in events]
    order = [names.index(n) for n in ("started", "progress", "saved", "finished") if n in names]
    assert {"started", "progress", "saved", "finished"} <= set(names) and order == sorted(order), \
        f"the {label} batch's protocol events are out of order or missing: {names[:30]}"
    print(f"\n{label} batch ({spec.batch_id}): {len(spec.members)} spectra, {len(spec.anchors)} anchors, "
          f"wall {wall:.1f} s, {BATCH_WORKERS} cores")
    return _Batch(label, spec, end, wall, _segments(events))


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def real():
    folder, rf = REAL_DATA / FOLDER, REAL_DATA / FOLDER / "rf"
    for path in (folder, rf):
        if not path.is_dir():
            pytest.fail(f"proof needs real data that is missing: {path}", pytrace=False)
    return folder, rf


@pytest.fixture(scope="module")
def expectation(real):
    path = REAL_DATA / FOLDER / EXPECT
    if not path.is_file():
        pytest.fail(f"proof needs its expectation file, which is missing: {path}", pytrace=False)
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def out_dir():
    path = ROOT / "out" / "letacon_inter_spectra" / datetime.now().strftime("%Y%m%d-%H%M%S")
    path.mkdir(parents=True)
    print(f"\nle Tacon inter-spectra proof output: {path}")
    return path


@pytest.fixture(scope="module")
def ws(real, out_dir):
    """Both temperatures in one workspace, then the fast pass of all 56 spectra (``isar run`` on both folders)."""
    folder, rf = real
    dirs = {}
    for t in TEMPERATURES:
        dirs[t] = out_dir / f"in_{t}"
        dirs[t].mkdir()
        files = sorted(folder.glob(f"CsV3Sb5_{t}K_*"))
        assert files, f"no CsV3Sb5_{t}K_* files in {folder}"
        for f in files:
            shutil.copy2(f, dirs[t] / f.name)
    root = out_dir / "ws"
    _init_workspace([dirs[t] for t in TEMPERATURES], root, rf, out_dir / "init.log")
    code, wall = _isar(["run", *[dirs[t] for t in TEMPERATURES], "--workspace", root, "--workers", BATCH_WORKERS],
                       out_dir / "run.log", "fast pass of 302 K and 97 K")
    assert code == 0, f"isar run exited {code}; log: {out_dir / 'run.log'}"
    fits = _fits(root)
    fitted = [k for k, (_e, fp) in fits.items() if fp.get("schema")]
    assert len(fits) == len(TEMPERATURES) * N_ANALYZERS and len(fitted) == len(fits), \
        f"{len(fits)} fitted ledger entries, {len(fitted)} with a fast-pass record (expected 56)"
    print(f"fast pass: {len(fitted)} spectra fitted, isar run wall {wall:.1f} s")
    return root


@pytest.fixture(scope="module")
def snapshot(ws):
    return _snapshot(ws)


@pytest.fixture(scope="module")
def nodes(ws):
    return read_nodes(ws)


@pytest.fixture(scope="module")
def anchors(ws, nodes, expectation):
    spec = expectation["anchors"]
    keys = sorted(k for k, n in nodes.nodes.items()
                  if n.T == spec["temperature"] and (spec["analyzers"] == "all" or n.detector in spec["analyzers"]))
    assert keys, f"the expectation file's anchors name no spectrum at {spec['temperature']} K"
    saved = inter_values.set_anchors(ws, keys, anchored=True)
    assert saved.ok, saved
    print(f"anchors: {len(keys)} spectra at {spec['temperature']} K, written through the intake anchor writer")
    return tuple(keys)


@pytest.fixture(scope="module")
def t_graph(ws, nodes, anchors):
    scope, kinds = anchored_scope(anchors, "T", nodes)
    g = _build(ws, nodes, scope, kinds)
    n_T = sum(1 for link in g.links if link.kind == "T")
    if n_T != N_ANALYZERS:
        pytest.fail(f"the 302 K and 97 K sets were not grouped into one scan set: {n_T} T-links, "
                    f"expected {N_ANALYZERS}", pytrace=False)
    return g


@pytest.fixture(scope="module")
def service(ws, anchors, out_dir):
    svc = _Service(ws, out_dir / "service.stderr.log")
    yield svc
    svc.close()


@pytest.fixture(scope="module")
def anchored_run(snapshot, service, anchors, t_graph):
    spec = runner.BatchSpec(records.new_batch_id(), "anchored", "T", anchors, tuple(sorted(t_graph.nodes)))
    pv = preview_anchored(t_graph, plan_anchored(t_graph, anchors, set()), "T")
    print(f"anchored preview: {pv.text}")
    for note in pv.notes:
        print(f"  note: {note}")
    assert pv.can_start, f"the anchored batch cannot start: {pv.blocked}"
    return _run_batch(service, "inter-anchored", spec, "anchored")


@pytest.fixture(scope="module")
def simultaneous_run(anchored_run, service, ws, nodes):
    keys = tuple(sorted(nodes.nodes))
    assert len(keys) == len(TEMPERATURES) * N_ANALYZERS, f"the import record holds {len(keys)} spectra"
    g = _build(ws, nodes, frozenset(keys), frozenset({"q", "T"}))
    pv = preview_simultaneous(g, plan_simultaneous(g, set()))
    print(f"simultaneous preview: {pv.text}")
    assert pv.can_start, f"the simultaneous batch cannot start: {pv.blocked}"
    spec = runner.BatchSpec(records.new_batch_id(), "simultaneous", None, (), keys)
    return _run_batch(service, "inter-simultaneous", spec, "simultaneous")


# ---------------------------------------------------------------- tests
def test_precondition_t_links_exist(t_graph):
    n_T = sum(1 for link in t_graph.links if link.kind == "T")
    print(f"\nT-links over the 302 K and 97 K scan set: {n_T} (expected {N_ANALYZERS})")
    assert n_T == N_ANALYZERS


def test_batches_account_for_every_spectrum(anchored_run, simultaneous_run, ws):
    log = records.read_log(ws)
    problems = []
    for b in (anchored_run, simultaneous_run):
        info = log.batches.get(b.spec.batch_id)
        if info is None or info.end is None:
            problems.append(f"{b.label}: the log has no end line for {b.spec.batch_id}")
            continue
        progress = {"done": info.end["done"], "failed": info.end["failed"],
                    "not_reached": len(info.end["not_reached"]), "fixed": len(info.fixed), "total": len(b.spec.members)}
        verdict = chk.accounting_ok(progress)
        print(f"{b.label}: {verdict.why}")
        RESULTS.setdefault("accounting", {})[b.label] = {**progress, "ok": verdict.ok}
        if not verdict.ok:
            problems.append(f"{b.label}: {verdict.why}")
    assert not problems, "; ".join(problems)


def test_fast_solo_and_results_csv_are_untouched(snapshot, simultaneous_run, ws):
    after = _snapshot(ws)
    changed = sorted(k for k, d in snapshot["fast"].items() if after["fast"].get(k) != d)
    print(f"\nfast-pass records hashed before: {len(snapshot['fast'])}, changed by the batches: {len(changed)}; "
          f"results.csv {'unchanged' if after['results_csv'] == snapshot['results_csv'] else 'CHANGED'}")
    assert after["results_csv"] == snapshot["results_csv"], "report/results.csv changed during the batches"
    assert not changed, f"fast-pass records changed: {changed}"


def test_authority_follows_the_rules(simultaneous_run, ws, nodes):
    log = records.read_log(ws)
    newest = {}
    for line in log.refits:                          # file order: a later line is a newer refit
        if line.accept is not None and line.accept.decision == "authoritative":
            newest[line.unit] = line.fit
    by_key = inter_overview(ws).by_key
    problems = []
    for key in sorted(nodes.nodes):
        got = by_key[key].fit_id if key in by_key else "fast"
        want = newest.get(key, "fast")
        if got != want:
            problems.append(f"{key}: authoritative {got}, its newest authoritative refit is {want}")
    print(f"\nauthority: {len(newest)} spectra stand on a refit, {len(nodes.nodes) - len(newest)} on the fast solo fit")
    assert not problems, "; ".join(problems[:10])


def test_moving_mode_is_flagged_and_flat_modes_are_quiet(simultaneous_run, ws, nodes, expectation):
    exp = expectation
    analyzer, moving = exp["analyzer"], exp["moving_mode"]
    flats, tol = tuple(exp["flat_modes"]), exp.get("match_tolerance_meV")
    log, by_key, fast = records.read_log(ws), inter_overview(ws).by_key, _fits(ws)
    spectrum_flags, matches, shifts = {}, {}, {}
    for T in TEMPERATURES:
        key = _key(nodes, T, analyzer)
        line = _authoritative_line(log, by_key, key)
        fast_fp = fast[(T, analyzer)][1]
        paper = {name: ref["omega0"] for name, ref in exp["paper"][str(T)].items()}
        matches[T] = chk.match_modes(_peaks_of(ws, line, fast_fp), paper, tol)
        spectrum_flags[T] = _as_flag(by_key[key].flag, line) if key in by_key else None
        fast_match = chk.match_modes(fast_fp.get("peaks") or [], paper, tol)
        for name in flats:
            if matches[T].get(name) and fast_match.get(name):
                shifts.setdefault(name, {})[T] = abs(matches[T][name]["omega0"] - fast_match[name]["omega0"])
        print(f"\n{T} K  {analyzer}: authoritative fit {line.fit if line else 'fast solo'}; "
              f"flag reasons {[r['code'] for r in (spectrum_flags[T].reasons if spectrum_flags[T] else ())]}")
        for name, ref in sorted(exp["paper"][str(T)].items()):
            m = matches[T].get(name)
            print(f"  paper {name}: E0 {ref['omega0']:6.2f}  matched "
                  f"{'-' if m is None else format(m['omega0'], '6.2f') + ' ' + m.get('mode_status', '')}")
    moving_v = chk.moving_is_flagged(spectrum_flags, matches, tol, moving=moving)
    flats_v = chk.flats_are_quiet(spectrum_flags, matches, flats=flats)     # the flags the user sees (facade)
    worst = max((v for per in shifts.values() for v in per.values()), default=None)
    print(f"\nmoving: {moving_v.why}\nflats: {flats_v.why}\nflat-mode shift from the fast solo, worst: "
          f"{'none matched' if worst is None else format(worst, '.2f') + ' meV'}")
    RESULTS["moving"] = {"ok": moving_v.ok, "why": moving_v.why}
    RESULTS["flats"] = {"ok": flats_v.ok, "why": flats_v.why, "worst_shift_meV": worst}
    assert moving_v.ok, f"moving mode: {moving_v.why}"
    assert flats_v.ok, f"flat modes: {flats_v.why}"
    shift_tol = exp.get("flat_shift_tolerance_meV")
    if shift_tol is not None:
        assert worst is not None and worst <= shift_tol, \
            f"flat-mode shift {worst} meV exceeds {shift_tol} meV, or no flat mode matched"


def test_other_analyzers_flag_map_and_numbers(simultaneous_run, ws, nodes, expectation):
    by_key = inter_overview(ws).by_key
    keys = {(round(n.T), n.detector): k for k, n in nodes.nodes.items() if n.T is not None}
    flags = {}
    for pos, key in keys.items():
        flags[pos] = _as_flag(by_key[key].flag, None) if key in by_key and by_key[key].flag is not None \
            else Flag(None, False, ())
    grid = grid_for(load_policy(ws / POLICY_NAME).plugin)
    text = chk.flag_map(flags, grid.position) if grid is not None else "(no analyzer layout for this instrument)"
    print(f"\nflag map (! serious, ? warn, i info, . not flagged, blank no fit):\n{text}")
    others = [key for (T, det), key in keys.items() if det != expectation["analyzer"]]
    flagged = [key for key in others if key in by_key and is_flagged(by_key[key].flag)]
    fraction = len(flagged) / len(others) if others else 0.0
    print(f"flagged among the other analyzers: {len(flagged)} of {len(others)} ({fraction:.3f})")
    RESULTS["flag_map"] = text
    RESULTS["other_flagged"] = {"flagged": len(flagged), "of": len(others), "fraction": fraction}
    cap = expectation.get("max_quiet_flagged_fraction")
    if cap is not None:
        assert fraction <= cap, f"{fraction:.3f} of the other analyzers are flagged; the expectation allows {cap}"


def test_timing_is_reported_never_asserted(anchored_run, simultaneous_run, ws, out_dir):
    fast_round = next((s["seconds"] for s in _timings(ws)["stages"] if s["stage"] == "fit_scans"), None)
    log = records.read_log(ws)
    reports = {}
    for b in (anchored_run, simultaneous_run):
        timings = []
        for line in log.refits:
            if line.batch == b.spec.batch_id and line.payload is not None:
                came_out = (records.read_payload(ws, line) or {}).get("came_out") or {}
                if isinstance(came_out.get("timing"), dict):
                    timings.append(came_out["timing"])
        view = {"wall_s": b.end["timing"]["wall_s"], "rings": b.end.get("rings_done"), "sweeps": b.end.get("sweeps"),
                "refit_timings": timings}
        rep = chk.timing_report(view, fast_round, cores=BATCH_WORKERS, target_s=TARGET_S)
        rep["segments"] = b.segments
        reports[b.label] = rep
        print(f"\n{b.label}: wall {rep['wall_s']:.1f} s, {rep['refits']} refits, rings {rep['rings']}, "
              f"sweeps {rep['sweeps']}, cores {rep['cores']}, fast round {fast_round} s")
        print(f"  split (core s): {({k: round(v, 1) for k, v in rep['split_core_s'].items()})}")
        print(f"  per refit: {rep['refit_total_s']}")
        print(f"  projection {rep['projection_s']} s: {rep['verdict']}")
    RESULTS["timing"] = reports
    summary = {"workspace": str(ws), "settings": inter_overview(ws).settings,
               "batches": {b.label: {"batch_id": b.spec.batch_id, "kind": b.spec.kind, "direction": b.spec.direction,
                                     "anchors": list(b.spec.anchors), "members": len(b.spec.members),
                                     "end": b.end, "wall_s": b.wall} for b in (anchored_run, simultaneous_run)},
               **RESULTS}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8", newline="\n")
    print(f"\nsummary: {out_dir / 'summary.json'}")
    for label, rep in reports.items():
        assert rep["refits"] > 0 and rep["split_core_s"], f"{label}: the batch's refit timings are missing from its records"
