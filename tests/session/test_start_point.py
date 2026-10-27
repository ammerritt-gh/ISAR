"""The BL43 start point (C-start-point): each scan's saved inputs are read when its first unit is handed
to a worker, and exactly that effective record reaches calibration and every fit of the scan.

Observed through a controlled worker: ``scan_delivery._process_one`` is replaced by a recorder that
resolves the unit's calibration as the real gate does and returns without fitting (no fit, no deep
sampling). Everything is generated at test time (``tests/intake_fixtures.py``); nothing reads
``real_data/``. Every run passes its worker count explicitly.
"""
from __future__ import annotations

import hashlib
import json
import pickle
import time
from concurrent.futures import Future
from types import SimpleNamespace

import numpy as np
import pytest

import intake_fixtures as fx
import isar
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import bose_numerator
from isar.intake import curves, fit_inputs, inputs, scans, settings, store, values
from isar.parsers import sarf, sp8_bl43
from isar.schema import _json
from isar.session import calibrations, driver, ingest, measurement_store, scan_delivery
from isar.session.state import ScanEntry, canonical_hash

PATTERN = sp8_bl43.card_template().policy.calibration["resolution_pattern"]


def test_import_root_is_the_worktree():
    from pathlib import Path
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# the controlled worker and its helpers
# ---------------------------------------------------------------------------
class _Worker:
    """Stands in for ``_process_one``: records what it receives, never fits. ``fresh_process`` empties the
    curve cache first, as a spawned pool worker starts."""

    def __init__(self, monkeypatch, on_call=None, fresh_process=False):
        self.calls, self.on_call, self.fresh_process = [], on_call, fresh_process
        monkeypatch.setattr(scan_delivery, "_process_one", self)

    def __call__(self, rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash, **_kw):
        if self.fresh_process:
            sarf._LIBRARIES.clear()
        cal = calibrations.resolve(rec, det, card, runcal)
        ctx_T = None if cal.held else ScanContext.from_scan(
            rec, det, card, center=cal.center, gauss_fwhm=cal.gauss_fwhm, lorentz_fwhm=cal.lorentz_fwhm,
            resolution_curve=cal.curve).T
        call = SimpleNamespace(unit=scan_id, scan=scan_id.rsplit(":", 1)[0], det=det, T=T,
                               setpoint=float(np.median(rec.temperature.setpoint)), ctx_T=ctx_T,
                               slit=(rec.facts.get("slit") or {}).get("value"), spectra=sorted(rec.spectra),
                               cal=cal, runcal=runcal,
                               counts=np.array(rec.spectra[det].intensity, dtype=float),
                               flags=list(rec.spectra[det].spectrum_flags))
        self.calls.append(call)
        if self.on_call is not None:
            self.on_call(call)
        return ScanEntry(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                         status="held_for_calibration" if cal.held else "non_result",
                         content_hash=chash, guidelines_hash=ghash,
                         reasons=[cal.held_reason] if cal.held else ["controlled worker"],
                         provenance=cal.provenance())

    def of(self, scan):
        return [c for c in self.calls if c.scan == scan]

    def units(self):
        return [c.unit for c in self.calls]


class _InlinePool:
    """An in-process ProcessPoolExecutor: ``submit`` pickles the task as the real feeder does, then runs it."""

    def __init__(self, max_workers):
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, args):
        fut = Future()
        fut.set_result(fn(pickle.loads(pickle.dumps(args))))
        return fut


def _run(ws, data, *, workers=1, **kw):
    return driver.run_batch(ws, [data], M=10, scan_M=10, errors_tier="fast", workers=workers,
                            formats=("markdown",), **kw)


def _order(ws, data) -> list[str]:
    """The scan keys in run order."""
    return [f"{r.experiment_id}:{r.scan_number}" for r in fx.records_for(ws, data)]


def _import(ws, data):
    """`isar ingest` of the folder, so the scans exist for a save before any run."""
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)


def _log(ws):
    return fit_inputs.read_log(ws.root)


def _latest(ws):
    return fit_inputs.latest_passes(_log(ws))


def _now(ws, unit):
    """The unit's inputs in force now (C-effective), against the calibration state on disk."""
    scan, _, det = unit.rpartition(":")
    row = scans.read_scans(ws.root)[scan]
    read = store.read_all(ws.root, store.ENTRIES)
    si = inputs.scan_inputs(row, inputs.index_entries(read.entries, lines=read.lines))
    card, _s = ws.load()
    return inputs.unit_inputs(row, si, det, card.state.tabulated_calibration.get(scan))


def _lines(ws, kind, run=None):
    return [ln for ln in _log(ws) if ln["kind"] == kind and (run is None or ln["run"] == run)]


def _runs(ws):
    return [ln["run"] for ln in _log(ws) if ln["kind"] == "run" and ln["event"] == "start"]


def _measurements(ws) -> list[dict]:
    path = ws.report_dir / measurement_store.FILENAME
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _touch_curve(path):
    """Replace a curve file in place with different bytes."""
    path.write_text(path.read_text(encoding="utf-8") + "# replaced\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 1, 11: a save is used from the scan's own start point, not the run's
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("pooled", [False, True], ids=["serial", "pooled"])
def test_a_save_before_a_queued_scans_start_is_used(tmp_path, monkeypatch, pooled):
    ws, data = fx.bl43_workspace(tmp_path)
    a, b = _order(ws, data)

    def save_once(call):
        if call.scan == a and len(worker.calls) == 1:
            assert values.save_value(ws.root, "temperature", 120.0, [a, b]).ok
    worker = _Worker(monkeypatch, on_call=save_once)
    if pooled:
        monkeypatch.setattr(scan_delivery, "ProcessPoolExecutor", _InlinePool)
    _run(ws, data, workers=2 if pooled else 1)
    assert {(c.T, c.setpoint, c.ctx_T) for c in worker.of(a)} == {(100.0, 100.0, 100.0)}
    assert {(c.T, c.setpoint, c.ctx_T) for c in worker.of(b)} == {(120.0, 120.0, 120.0)}
    assert len(worker.calls) == 6
    latest = _latest(ws)
    for det in fx.ANALYZERS:
        assert inputs.changes(latest[f"{a}:{det}"]["fast"].inputs, _now(ws, f"{a}:{det}")) == (
            inputs.Change("temperature", 100.0, 120.0, "user"),)
        assert inputs.changes(latest[f"{b}:{det}"]["fast"].inputs, _now(ws, f"{b}:{det}")) == ()
    _card, session = ws.load()       # the ledger and its provenance say what the fit used
    assert session.get(f"{b}:a03").provenance["T"] == 120.0 and session.get(f"{a}:a03").provenance["T"] == 100.0


def test_carry_or_fit_is_decided_at_each_scans_start_point(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A", "B", "C"))
    a, b, c = _order(ws, data)
    _Worker(monkeypatch)
    _run(ws, data)                                                  # everything fitted at the file's 100 K
    assert values.save_value(ws.root, "temperature", 110.0, [a]).ok     # a is taken: its worker saves
    assert values.save_value(ws.root, "temperature", 120.0, [c]).ok     # c differs from its fit at run start
    _card, before = ws.load()
    c_before = {det: _json.to_jsonable(before.get(f"{c}:{det}")) for det in fx.ANALYZERS}
    records_before = [r for r in _measurements(ws) if r["scan_ref"].startswith(c)]

    def save_once(call):
        if call.scan == a and len(worker.calls) == 1:
            assert values.save_value(ws.root, "temperature", 120.0, [b]).ok
            assert values.use_file_value(ws.root, "temperature", [c]).ok
    worker = _Worker(monkeypatch, on_call=save_once)
    _run(ws, data)
    run2 = _runs(ws)[-1]
    assert {x.T for x in worker.of(b)} == {120.0} and len(worker.of(b)) == 3     # current at run start, fitted
    assert worker.of(c) == []                                                   # back to its fit's inputs: carried
    _card, after = ws.load()
    assert {det: _json.to_jsonable(after.get(f"{c}:{det}")) for det in fx.ANALYZERS} == c_before
    assert not [ln for ln in _lines(ws, "started", run2) if ln["scan"] == c]
    assert [r for r in _measurements(ws) if r["scan_ref"].startswith(c)] == records_before     # no stub
    assert not [ln for ln in fit_inputs.read_history(ws.root) if ln["scan"] == c]


@pytest.mark.parametrize("saved", [True, False], ids=["saved-refits", "nothing-saved-reuses"])
def test_a_deadline_resume_judges_the_checkpoint_at_the_start_point(tmp_path, monkeypatch, saved):
    ws, data = fx.bl43_workspace(tmp_path, stems=("B",))
    (b,) = _order(ws, data)
    worker = _Worker(monkeypatch)
    cut = _run(ws, data, analysis_allowance_seconds=0.0, finish_initial_wave=True)
    assert cut.analysis_incomplete and worker.units() == [f"{b}:a03"]         # one unit, checkpointed at 100 K
    if saved:
        assert values.save_value(ws.root, "temperature", 120.0, [b]).ok
    worker.calls.clear()
    _run(ws, data, analysis_allowance_seconds=3600.0)
    resumed = _runs(ws)[-1]
    if saved:
        assert worker.units() == [f"{b}:{det}" for det in fx.ANALYZERS] and {x.T for x in worker.calls} == {120.0}
    else:
        assert worker.units() == [f"{b}:a04", f"{b}:a05"]                   # a03's checkpoint stands
        (fin,) = [ln for ln in _lines(ws, "finished") if ln["unit"] == f"{b}:a03" and ln["run"] == resumed]
        _card, session = ws.load()
        assert session.get(f"{b}:a03").provenance["fit_inputs"]["run"] == resumed
    assert _latest(ws)[f"{b}:a03"]["fast"].run == resumed


# ---------------------------------------------------------------------------
# 2, 4, 7, 14: the calibration follows the frozen inputs and the run's one curve snapshot
# ---------------------------------------------------------------------------
def test_a_slit_saved_after_the_run_start_calibration_reaches_the_next_scan(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path)
    a, b = _order(ws, data)

    def save_once(call):
        if call.scan == a and len(worker.calls) == 1:
            assert values.save_value(ws.root, "slit", "60x60", [b]).ok
    worker = _Worker(monkeypatch, on_call=save_once)
    summary = _run(ws, data)
    assert {(x.cal.reference["setting"], x.slit) for x in worker.of(a)} == {(fx.TRUE_SLIT, None)}
    for x in worker.of(b):
        assert x.slit == "60x60" and x.cal.reference["setting"] == "60x60" and "_60_60_" in x.cal.curve.source
    card, _s = ws.load()
    check = curves.read_curve_check(ws.root)
    row = scans.read_scans(ws.root)[b]
    assert card.state.tabulated_calibration[b]["inputs_key"] == inputs.calibration_inputs_key(
        data_revision=row["data_revision"], folder=check.folder, pattern=PATTERN, library=check.library,
        declared_slit="60x60", analyzers=list(fx.ANALYZERS), counts_accepted=[])
    assert card.state.tabulated_calibration[a]["setting"] == fx.TRUE_SLIT
    assert summary.context_lines            # the run reported its calibration


def test_no_slit_and_weak_elastic_lines_use_the_broadest_curve_and_say_so(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), weak=fx.ANALYZERS)
    worker = _Worker(monkeypatch)
    _run(ws, data)
    broadest = max(fx.SETTINGS, key=fx.SETTINGS.get)
    assert {x.cal.reference["setting"] for x in worker.calls} == {broadest}
    card, session = ws.load()
    (state,) = card.state.tabulated_calibration.values()
    assert state["setting_basis"] == "broadest_curve_fallback"
    for e in session.scans.values():
        assert "slit_broadest_curve" in e.provenance["fit_inputs"]["fallbacks"]


def test_a_curve_replaced_at_the_same_path_is_taken_up_at_the_next_run_start(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path)
    a, b = _order(ws, data)
    _import(ws, data)
    assert values.save_value(ws.root, "slit", "60x60", [b]).ok          # b declares another slit's curves
    worker = _Worker(monkeypatch)
    _run(ws, data)
    path = ws.root / "rf" / "sarf_t_s2_40_40_a03"                       # a's a03 curve (inferred 40x40)
    _touch_curve(path)
    latest = _latest(ws)
    for unit in (f"{a}:a03", f"{b}:a03"):
        used = latest[unit]["fast"].inputs
        now = _sha(ws.root / "rf" / used["calibration"]["curve"]["file"])
        got = [ch.field for ch in inputs.changes(used, _now(ws, unit), curve_sha_now=now)]
        assert got == (["curve"] if unit.startswith(a) else [])
    worker.calls.clear()
    _run(ws, data)
    assert {x.scan for x in worker.calls} == {a}           # b's declared-slit units use none of the changed files
    (x,) = [x for x in worker.calls if x.det == "a03"]
    assert x.runcal.tabulated[a]["detectors"]["a03"]["curve"]["sha256"] == _sha(path)
    assert not x.cal.held and x.cal.reference["fingerprint"] == curves.read_curve_check(ws.root).library


def test_the_run_loads_its_curves_once_and_keeps_that_snapshot(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path)
    a, b = _order(ws, data)
    real = curves.load_curves
    loads = []

    def spy(folder, pattern):
        loads.append(folder)
        got = real(folder, pattern)
        _touch_curve(ws.root / "rf" / "sarf_t_s1_60_60_a04")              # replaced right after the load
        return got
    monkeypatch.setattr(curves, "load_curves", spy)
    monkeypatch.setattr(calibrations, "load_curves", spy)
    path = ws.root / "rf" / "sarf_t_s2_40_40_a03"
    before = _sha(path)

    def replace_during_fit(call):
        if call.scan == a and len(worker.calls) == 1:
            _touch_curve(path)
    worker = _Worker(monkeypatch, on_call=replace_during_fit)
    _run(ws, data)
    assert len(loads) == 1
    card, _s = ws.load()
    library = curves.read_curve_check(ws.root).library
    assert {s["library"] for s in card.state.tabulated_calibration.values()} == {library}
    for x in worker.of(b):                     # replaced during the fit stage: b stays on the snapshot
        assert not x.cal.held and x.cal.reference["fingerprint"] == library
    (x,) = [x for x in worker.of(b) if x.det == "a03"]
    assert x.runcal.tabulated[b]["detectors"]["a03"]["curve"]["sha256"] == before
    worker.calls.clear()
    _run(ws, data)                                                         # the next run recalibrates
    card, _s = ws.load()
    assert {s["library"] for s in card.state.tabulated_calibration.values()} == {curves.read_curve_check(ws.root).library}
    assert curves.read_curve_check(ws.root).library != library and len(worker.calls) == 6


def test_a_pooled_worker_fits_with_the_snapshot_curve_after_a_curve_is_added(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path)
    a, b = _order(ws, data)
    check_sha = {}

    def add_curve(call):
        if not check_sha:
            check_sha.update({(c.detector, c.setting): c.sha256 for c in curves.read_curve_check(ws.root).curves})
            fx.write_library(ws.root / "rf", settings={"80x80": 0.7})      # a new curve file in the folder
    worker = _Worker(monkeypatch, on_call=add_curve, fresh_process=True)
    monkeypatch.setattr(scan_delivery, "ProcessPoolExecutor", _InlinePool)
    _run(ws, data, workers=2)
    assert len(worker.calls) == 6
    for x in worker.calls:
        assert not x.cal.held, x.cal.held_reason
        assert x.runcal.tabulated[x.scan]["detectors"][x.det]["curve"]["sha256"] == check_sha[(x.det, fx.TRUE_SLIT)]


# ---------------------------------------------------------------------------
# 3, 5, 6: what the effective record carries
# ---------------------------------------------------------------------------
def test_a_scan_without_a_temperature_fits_at_the_zero_temperature_limit_and_says_so(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    real = driver.ingest.parse_all

    def no_temperature(*args, **kwargs):
        recs = real(*args, **kwargs)
        for r in recs:
            r.facts["T_K"] = {"value": None, "source": "missing"}
            r.temperature.setpoint[:] = np.nan
        return recs
    monkeypatch.setattr(driver.ingest, "parse_all", no_temperature)
    worker = _Worker(monkeypatch)
    _run(ws, data)
    assert worker.calls and all(x.T is None and np.isnan(x.ctx_T) for x in worker.calls)
    e = np.array([-1.0, 0.5, 2.0])
    assert np.array_equal(bose_numerator(e, worker.calls[0].ctx_T), np.maximum(e, 0.0))
    (started,) = [ln for ln in _lines(ws, "started") if ln["unit"].endswith("a03")]
    assert started["inputs"]["temperature"]["value"] is None and started["inputs"]["temperature"]["source"] == "missing"
    (finished,) = [ln for ln in _lines(ws, "finished") if ln["unit"].endswith("a03")]
    _card, session = ws.load()
    assert "bose_t0_limit" in finished["fallbacks"]
    assert "bose_t0_limit" in session.get(started["unit"]).provenance["fit_inputs"]["fallbacks"]


def test_a_dead_analyzer_is_never_fitted_and_its_declaration_is_carried_until_reversed(tmp_path, monkeypatch):
    ws = fx.workspace(tmp_path, sample="Smp")
    fx.write_library(ws.root / "rf")
    data = tmp_path / "data"
    stem = f"{fx.STEM}_A"
    fx.write_scan(data, stem=stem, analyzers=("a03", "a04"), energy=fx.ENERGY_120)     # a05's file missing
    worker = _Worker(monkeypatch)
    _run(ws, data)
    (s,) = _order(ws, data)
    dead05 = values.save_condition(ws.root, "dead", [f"{s}:a05"], reason="no counts")
    assert dead05.ok
    fx.write_scan(data, stem=stem, analyzers=("a05",), energy=fx.ENERGY_120, seed=3)    # the file arrives
    worker.calls.clear()
    _run(ws, data)
    card, session = ws.load()
    e05 = session.get(f"{s}:a05")
    assert e05.status == "excluded" and dead05.entry_id in e05.reasons[0] and "no counts" in e05.reasons[0]
    assert f"{s}:a05" not in worker.units() and {x.det for x in worker.calls} == {"a03", "a04"}
    assert all(x.spectra == ["a03", "a04"] for x in worker.calls)
    state = card.state.tabulated_calibration[s]
    assert "a05" not in state["comparison_set"] and "a05" not in state["detectors"]
    assert f"{s}:a05" not in _latest(ws)                                   # a declaration is not a fit

    dead04 = values.save_condition(ws.root, "dead", [f"{s}:a04"])        # a fitted analyzer declared dead
    fitted04 = _latest(ws)[f"{s}:a04"]["fast"]
    worker.calls.clear()
    _run(ws, data)
    assert worker.units() == [f"{s}:a03"]                     # a03 refits: its scan lost a sibling (dead_in_scan)
    _card, session = ws.load()
    assert session.get(f"{s}:a04").status == "excluded" and dead04.entry_id in session.get(f"{s}:a04").reasons[0]
    latest04 = _latest(ws)[f"{s}:a04"]["fast"]
    assert latest04 == fitted04
    assert [ch.field for ch in inputs.changes(latest04.inputs, _now(ws, f"{s}:a04"))] == ["dead"]   # "made before"
    history = len(fit_inputs.read_history(ws.root))
    worker.calls.clear()
    _run(ws, data)                                            # the declarations stand: everything carried
    assert worker.calls == [] and len(fit_inputs.read_history(ws.root)) == history
    assert values.save_condition(ws.root, "dead", [f"{s}:a04"], declared=False).ok
    _run(ws, data)
    assert f"{s}:a04" in worker.units() and ws.load()[1].get(f"{s}:a04").status == "non_result"


def test_reimported_counts_change_only_the_data_and_reach_the_worker(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    worker = _Worker(monkeypatch)
    _run(ws, data)
    (s,) = _order(ws, data)
    old = scans.read_scans(ws.root)[s]["data_revision"]
    fx.write_scan(data, stem=f"{fx.STEM}_A", energy=fx.ENERGY_120, seed=11)        # new counts, same facts
    _import(ws, data)
    new = scans.read_scans(ws.root)[s]["data_revision"]
    assert new != old
    for det in fx.ANALYZERS:
        assert inputs.changes(_latest(ws)[f"{s}:{det}"]["fast"].inputs, _now(ws, f"{s}:{det}")) == (
            inputs.Change("data", old, new, None),)
    (rec,) = fx.records_for(ws, data)
    worker.calls.clear()
    _run(ws, data)
    assert [x.det for x in worker.calls] == list(fx.ANALYZERS)
    for x in worker.calls:
        assert np.array_equal(x.counts, np.asarray(rec.spectra[x.det].intensity, dtype=float), equal_nan=True)
    assert {ln["inputs"]["data"] for ln in _lines(ws, "started", _runs(ws)[-1])} == {new}


def test_an_excluded_scan_is_never_fitted_and_stays_excluded_while_declared(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path)
    a, b = _order(ws, data)
    _import(ws, data)
    excl = values.save_condition(ws.root, "excluded", [b], reason="sample moved")
    worker = _Worker(monkeypatch)
    _run(ws, data)
    assert {x.scan for x in worker.calls} == {a}
    _card, session = ws.load()
    for det in fx.ANALYZERS:
        e = session.get(f"{b}:{det}")
        assert e.status == "excluded" and excl.entry_id in e.reasons[0] and "sample moved" in e.reasons[0]
    assert not [ln for ln in _log(ws) if ln.get("scan") == b]             # no fit, so no fit-inputs line
    worker.calls.clear()
    _run(ws, data)
    assert worker.calls == []                                             # carried while the exclusion stands


# ---------------------------------------------------------------------------
# 8, 9, 10: the lazy pool, a real pool, the run's end line
# ---------------------------------------------------------------------------
def _pool_tasks():
    return [(None, "det", [0.0, 0.0, 0.0], 100.0, "S", f"E:{scan}:{det}", f"c{scan}{det}", "g", 6000 + scan)
            for scan, det in ((1, "a"), (1, "b"), (2, "a"), (2, "b"), (3, "a"))]


def test_the_scan_pool_refills_lazily_and_reduces_in_submission_order(monkeypatch):
    submitted, pool, starts, finished, completed = [], [], [], [], []

    class _Pool:                  # futures stay pending until wait() completes one, as worker processes would
        def __init__(self, max_workers):
            assert max_workers == 2

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, args):
            submitted.append((args[5], sum(not f.done() for f, *_ in pool)))
            fut = Future()
            pool.append((fut, fn, args))
            return fut

    def fake_wait(fs, return_when):
        ready = [p for p in pool if not p[0].done()
                 and (p[2][5] != "E:1:a" or any(u == "E:2:a" for u, _n in submitted))]   # unit 1 waits for unit 3
        fut, fn, args = ready[0]
        fut.set_result(fn(args))

    monkeypatch.setattr(scan_delivery, "ProcessPoolExecutor", _Pool)
    monkeypatch.setattr(scan_delivery, "wait", fake_wait)
    monkeypatch.setattr(scan_delivery, "_process_one", lambda rec, det, q, T, sample, scan_id, *a, **k: ScanEntry(
        scan_id=scan_id, scan_number=1, scan_type="measurement", status="non_result"))

    def start(group):
        starts.append((group[0][5], sum(not f.done() for f, *_ in pool)))
        return scan_delivery.ScanStart(list(group))

    out = scan_delivery._run_tasks(
        _pool_tasks(), None, None, None, set(), M=10, errors_tier="fast", boot_workers=1, use_pool=True,
        scan_workers=2, start_scan=start, on_finish=lambda e: finished.append(e.scan_id),
        on_complete=lambda e: completed.append(e.scan_id))
    order = [t[5] for t in _pool_tasks()]
    assert max(n for _u, n in submitted) < 2                         # never more than 2 pending
    assert [u for u, _n in starts] == ["E:1:a", "E:2:a", "E:3:a"] and all(n < 2 for _u, n in starts)
    assert completed == order and [e.scan_id for e in out] == order
    assert finished == ["E:1:b", "E:1:a", "E:2:a", "E:2:b", "E:3:a"]      # completion order
    assert finished != completed


def test_the_scan_pool_waves_under_a_deadline_and_stops_after_the_first_refused_wave(monkeypatch):
    events = []
    monkeypatch.setattr(scan_delivery, "ProcessPoolExecutor", _InlinePool)
    monkeypatch.setattr(scan_delivery, "_process_one", lambda rec, det, q, T, sample, scan_id, *a, **k: (
        events.append("s") or ScanEntry(scan_id=scan_id, scan_number=1, scan_type="measurement", status="fitted")))

    class _Gate:
        deadline = 1.0

        def __init__(self, allowed):
            self.allowed = allowed

        def begin_wave(self):
            events.append("w")
            self.allowed -= 1
            return self.allowed >= 0

    kw = dict(M=10, errors_tier="fast", boot_workers=1, use_pool=True, scan_workers=2)
    assert len(scan_delivery._run_tasks(_pool_tasks(), None, None, None, set(), deadline_gate=_Gate(9), **kw)) == 5
    assert "".join(events) == "wsswssws"                                                   # waves [2, 2, 1]
    events.clear()
    with pytest.raises(scan_delivery.AnalysisWorkIncomplete, match="4/5 scan fit units"):
        scan_delivery._run_tasks(_pool_tasks(), None, None, None, set(), deadline_gate=_Gate(2), **kw)
    assert "".join(events) == "wsswssw"


def test_a_real_pool_carries_the_effective_record_to_its_workers(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, library_skip=[(d, s) for d in fx.ANALYZERS for s in fx.SETTINGS])
    a, b = _order(ws, data)
    _import(ws, data)
    assert values.save_value(ws.root, "temperature", 120.0, [a, b]).ok
    _run(ws, data, workers=2)                        # spawned workers: every unit held, nothing fitted
    _card, session = ws.load()
    assert len(session.scans) == 6
    latest = {}                                      # last line wins (measurement_store's contract)
    for r in _measurements(ws):
        latest[r["scan_ref"]] = r
    for e in session.scans.values():
        assert e.status == "held_for_calibration"
        assert latest[e.scan_id]["count_trust"] == "held_for_calibration"
        assert [list(c) for c in latest[e.scan_id]["conditions"]] == [["temperature_K", 120.0]]


def test_a_run_ends_with_its_outcome(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    _Worker(monkeypatch)
    _run(ws, data)
    _run(ws, data, analysis_allowance_seconds=0.0, finish_initial_wave=True, force=True)
    ends = [ln["outcome"] for ln in _log(ws) if ln["kind"] == "run" and ln["event"] == "end"]
    assert ends == ["complete", "incomplete"]
    assert _log(ws)[-1]["event"] == "end" and not any(r.running for r in fit_inputs.run_states(_log(ws)).values())


# ---------------------------------------------------------------------------
# 12: a replaced fit stays on record
# ---------------------------------------------------------------------------
def test_every_fit_a_refit_replaces_is_kept_whole_in_the_history(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("B", "C"))
    b, c = sorted(_order(ws, data), key=lambda k: k != _order(ws, data)[0])
    _Worker(monkeypatch)
    _run(ws, data)
    run1 = _runs(ws)[-1]
    _card, before = ws.load()
    assert values.save_value(ws.root, "temperature", 120.0, [b]).ok
    _run(ws, data)
    run2 = _runs(ws)[-1]
    history = fit_inputs.read_history(ws.root)
    assert sorted(ln["unit"] for ln in history) == [f"{b}:{det}" for det in fx.ANALYZERS]      # c carried: no line
    started1 = {ln["unit"] for ln in _lines(ws, "started", run1)}
    for ln in history:
        assert ln["by"] == "refit" and ln["run"] == run2 and ln["fit_inputs"]["run"] == run1
        assert ln["entry_hash"] == canonical_hash(_json.to_jsonable(before.get(ln["unit"])))
        assert ln["unit"] in started1


# ---------------------------------------------------------------------------
# 13: counts that may not be raw hold the fit until accepted (_i only)
# ---------------------------------------------------------------------------
def test_off_integer_counts_hold_the_fit_until_accepted(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), off_integer=("a05",))
    worker = _Worker(monkeypatch)
    _run(ws, data)
    (s,) = _order(ws, data)
    unit = f"{s}:a05"
    (x,) = [x for x in worker.calls if x.det == "a05"]
    assert x.cal.held and "counts may not be raw" in x.cal.held_reason and "40 of 120" in x.cal.held_reason
    card, session = ws.load()
    assert session.get(unit).status == "held_for_calibration"
    assert {session.get(f"{s}:{d}").status for d in ("a03", "a04")} == {"non_result"}
    assert "a05" not in card.state.tabulated_calibration[s]["comparison_set"]
    (st1,) = [ln for ln in _lines(ws, "started") if ln["unit"] == unit]
    counts = st1["inputs"]["counts"]
    assert counts["level"] == "warning" and counts["bins_off"] == 40 and counts["override_applied"] is False

    acc = values.save_condition(ws.root, "counts_accepted", [unit], reason="checked with the beamline")
    worker.calls.clear()
    _run(ws, data)
    run2 = _runs(ws)[-1]
    assert worker.units() == [unit]                                        # the siblings are carried
    (x,) = worker.calls
    assert not x.cal.held and x.cal.curve is not None
    assert any(f.type == "counts_not_integer" and f.severity == "warning" for f in x.flags)   # the flag stays
    (st2,) = _lines(ws, "started", run2)
    c2 = st2["inputs"]["counts"]
    assert c2["accepted"] == {"value": True, "entry": acc.entry_id, "reason": "checked with the beamline"}
    assert c2["override_applied"] is True and "counts_accepted" in st2["fallbacks"] and st2["hash"] != st1["hash"]
    (fin2,) = _lines(ws, "finished", run2)
    assert "counts_accepted" in fin2["fallbacks"]
    (rec,) = fx.records_for(ws, data)
    assert np.array_equal(x.counts, np.asarray(rec.spectra["a05"].intensity, dtype=float), equal_nan=True)

    assert values.save_condition(ws.root, "counts_accepted", [unit], declared=False).ok
    worker.calls.clear()
    _run(ws, data)
    (x,) = worker.calls
    assert x.cal.held and "counts may not be raw" in x.cal.held_reason


def test_counts_accepted_by_a_running_scans_worker_reach_the_next_scan(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, off_integer=("a05",))
    a, b = _order(ws, data)

    def accept_once(call):
        if call.scan == a and len(worker.calls) == 1:
            assert values.save_condition(ws.root, "counts_accepted", [f"{b}:a05"]).ok
    worker = _Worker(monkeypatch, on_call=accept_once)
    _run(ws, data)
    held = {x.unit: x.cal.held for x in worker.calls if x.det == "a05"}
    assert held == {f"{a}:a05": True, f"{b}:a05": False}


def test_an_lr_spectrum_stays_held_whatever_is_accepted(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), off_integer=("a05",))
    real = driver.ingest.parse_all

    def as_lr(*args, **kwargs):                      # what the reader records for an _lr spectrum
        recs = real(*args, **kwargs)
        for r in recs:
            r.spectra["a05"].facts["count_recovery"]["value"]["family"] = "_lr"
        return recs
    monkeypatch.setattr(driver.ingest, "parse_all", as_lr)
    _import(ws, data)
    (s,) = _order(ws, data)
    assert values.save_condition(ws.root, "counts_accepted", [f"{s}:a05"]).ok
    worker = _Worker(monkeypatch)
    _run(ws, data)
    (x,) = [x for x in worker.calls if x.det == "a05"]
    assert x.cal.held and x.cal.held_reason == calibrations.NON_RAW_REASON


# ---------------------------------------------------------------------------
# 15: damaged intake lines never stop the run
# ---------------------------------------------------------------------------
def test_damaged_intake_lines_are_reported_and_never_stop_the_run(tmp_path, monkeypatch):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    _import(ws, data)
    (s,) = _order(ws, data)
    assert settings.save_resolution_folder(ws.root, ws.root / "rf").ok
    assert values.save_value(ws.root, "temperature", 120.0, [s]).ok
    for name in (store.ENTRIES, store.SETTINGS):
        path = ws.root / name
        valid = path.read_bytes()
        path.write_bytes(valid + b"[1, 2]\n" + valid.splitlines(keepends=True)[-1] + b'{"kind": "val')
    worker = _Worker(monkeypatch)
    summary = _run(ws, data)
    assert _log(ws)[-1]["outcome"] == "complete" and worker.calls
    assert {x.T for x in worker.calls} == {120.0}
    damaged = [ln for ln in summary.context_lines if " skipped: " in ln]
    for name in (store.ENTRIES, store.SETTINGS):
        assert any(ln.startswith(f"{name} line 2 skipped") for ln in damaged)
        assert any(ln.startswith(f"{name} line 4 skipped") for ln in damaged)
