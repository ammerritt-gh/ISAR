"""The queue's per-file fit (C-file-fit): one analyzer file fitted fast or deep from the latest saved inputs,
with the batch's preparation and hashes, no dispersion prior, results only through ``emit``.

Generated BL43 workspaces (``tests/intake_fixtures.py``) at a mode ceiling of 1, so a real fast pass takes
well under a second; nested sampling is never run (the stage task returns no runs). Nothing reads
``real_data/``.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import intake_fixtures as fx
import isar
from isar.analyze import deep_pass, errors
from isar.intake import values
from isar.session import calibrations, deep_delivery, driver, file_fit, ingest, measurement_store, scan_delivery
from isar.session.file_fit import FileItem, fit_file
from isar.session.state import ScanEntry, canonical_hash, content_hash
from isar.session.writer import save_results


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ingested(tmp_path, *, policy=(), **kw):
    """``(ws, data, scan key)``: one generated BL43 scan at mode ceiling 1, imported, never fitted."""
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), **kw)
    text = ws.policy_path.read_text(encoding="utf-8").replace("min_fit_points = 6", "min_fit_points = 6\nmax_modes = 1")
    for old, new in policy:
        assert old in text
        text = text.replace(old, new)
    ws.policy_path.write_text(text, encoding="utf-8", newline="\n")
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    (scan,) = {k.rsplit(":", 1)[0] for k in session.scans}
    return ws, data, scan


def _fit(ws, unit, pass_kind="fast", cores=1, item_id="q1"):
    out = []
    fit_file(ws.root, FileItem(item_id, unit, pass_kind, cores), progress=None, emit=out.append)
    return out


def _results(out):
    """The results proper: no attempt record, no cores notice."""
    return [r for r in out if r.status not in ("attempt", "cores")]


def _save(ws, out):
    verdicts = save_results(ws.root, [r.payload for r in out if r.payload is not None])
    assert verdicts == [None] * len(verdicts)


class _Worker:
    """Stands in for ``_process_one``: records its arguments and returns a fitted entry as the real gate
    would stamp it (calibration provenance), with no fit."""

    def __init__(self, monkeypatch):
        self.calls, self.fitted = [], True        # fitted False: a non_result, which run_batch can collect
        monkeypatch.setattr(scan_delivery, "_process_one", self)

    def __call__(self, rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash, **kw):
        cal = calibrations.resolve(rec, det, card, runcal)
        self.calls.append(SimpleNamespace(unit=scan_id, rec_hash=content_hash(rec), det=det, q=q,
                                          T=T, chash=chash, ghash=ghash, card=card, runcal=runcal, kw=kw,
                                          spectra=sorted(rec.spectra)))
        return ScanEntry(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                         status="fitted" if self.fitted else "non_result",
                         content_hash=chash, guidelines_hash=ghash, reasons=["controlled worker"],
                         provenance=cal.provenance(),
                         detectors={det: {"fast_pass": {"search_limit_reason": ""}, "reasons": []}}
                         if self.fitted else {})


@pytest.fixture
def no_sampling(monkeypatch):
    """No nested sampling and no bootstrap: every stage task returns no runs, errors attach instantly."""
    monkeypatch.setattr(deep_pass, "_stage_task", lambda job, **kw: (job[0], []))
    monkeypatch.setattr(errors, "attach", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# C-file-fit examples
# ---------------------------------------------------------------------------
def test_a_fast_item_prepares_the_unit_as_the_batch_does_and_fits_without_a_prior(tmp_path, monkeypatch):
    """Examples 1 and 7: the arguments equal run_batch's for the same unit; the attempt record is its stub."""
    ws, data, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    worker = _Worker(monkeypatch)
    out = _fit(ws, unit)
    assert [(r.pass_kind, r.status) for r in out] == [("attempt", "attempt"), ("fast", "done")]
    mine = worker.calls[0]
    assert mine.card.state.dispersion_prior == []
    worker.calls.clear()
    worker.fitted = False
    driver.run_batch(ws, [data], M=10, scan_M=50, errors_tier="fast", workers=1, formats=("markdown",))
    batch = next(c for c in worker.calls if c.unit == unit)
    for name in ("rec_hash", "det", "q", "T", "chash", "ghash", "spectra"):
        assert getattr(mine, name) == getattr(batch, name), name
    assert mine.runcal.tabulated == batch.runcal.tabulated and mine.runcal.tabulated
    assert mine.runcal.curves.keys() == batch.runcal.curves.keys() and mine.runcal.curves
    assert mine.runcal.curve_stamps == batch.runcal.curve_stamps
    assert (mine.kw["M"], mine.kw["errors_tier"], mine.kw["boot_workers"]) == (50, "fast", 1)
    lines = (ws.report_dir / measurement_store.FILENAME).read_text(encoding="utf-8").splitlines()
    stubs = [m for m in map(json.loads, lines) if m["scan_ref"] == unit and m["count_trust"] == "attempted"]
    attempt = out[0].payload
    assert attempt == {"schema": file_fit.SCHEMA, "unit": unit, "pass": "attempt",
                       "measurement_record": attempt["measurement_record"]}
    assert {**attempt["measurement_record"], "written_at": None} == {**stubs[0], "written_at": None}


def test_a_saved_fast_pass_is_already_current_and_not_refitted(tmp_path, monkeypatch):
    """Example 2."""
    ws, _data, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    worker = _Worker(monkeypatch)
    _save(ws, _fit(ws, unit))
    worker.calls.clear()
    out = _fit(ws, unit)
    assert [(r.status, r.reason, r.payload) for r in out] == [("already_current", "already has a current fast pass", None)]
    assert worker.calls == []


def test_a_deep_item_builds_on_the_stored_fast_pass_without_refitting_it(tmp_path, monkeypatch, no_sampling):
    """Example 3."""
    ws, _data, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    _save(ws, _fit(ws, unit))
    stored = ws.load()[1].get(unit).detectors["a04"]["fast_pass"]
    monkeypatch.setattr(scan_delivery, "fit_one", lambda *a, **k: pytest.fail("the fast stage was rerun"))
    out = _fit(ws, unit, "deep")
    (deep,) = out
    assert (deep.pass_kind, deep.status) == ("deep", "done_warning"), deep.reason
    assert deep.payload["base"]["fast_id"] == canonical_hash(stored)
    assert deep.payload["deep"]["record"]["state"] == "search_limited"
    assert deep.payload["entry"] is None and [ln["pass"] for ln in deep.payload["fit_inputs"]["started"]] == ["deep"]
    _save(ws, out)
    assert ws.load()[1].get(unit).detectors["a04"]["deep_pass"]["state"] == "search_limited"


def test_a_deep_item_on_a_fresh_unit_runs_the_fast_stage_once_and_first(tmp_path, monkeypatch, no_sampling):
    """Example 4: fast then deep, the deep stage on the fast stage's in-memory record; one save takes both."""
    ws, _data, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    seen = []
    real = deep_delivery.deliver
    monkeypatch.setattr(deep_delivery, "deliver", lambda fit, ctx, request, **kw: (seen.append(fit["fast_pass"]), real(fit, ctx, request, **kw))[1])
    out = _results(_fit(ws, unit, "deep"))
    assert [(r.pass_kind, r.status) for r in out] == [("fast", "done_warning"), ("deep", "done_warning")]
    fast_record = out[0].payload["entry"]["detectors"]["a04"]["fast_pass"]
    assert seen == [fast_record] and out[1].payload["base"]["fast_id"] == canonical_hash(fast_record)
    assert save_results(ws.root, [r.payload for r in out]) == [None, None]
    entry = ws.load()[1].get(unit)
    assert entry.status == "fitted" and entry.detectors["a04"]["deep_pass"]["state"] == "search_limited"


def test_a_dead_analyzer_is_held_and_its_deep_pass_is_not_run(tmp_path):
    """Example 5."""
    ws, _data, scan = _ingested(tmp_path)
    unit = f"{scan}:a05"
    dead = values.save_condition(ws.root, "dead", [unit], reason="no counts")
    assert dead.ok
    (fast,) = _fit(ws, unit)                  # no attempt record: a declaration is not a fit
    assert fast.status == "held" and fast.reason.startswith("dead analyzer (declared, entry ")
    assert fast.payload["entry"]["status"] == "excluded" and fast.payload["fit_inputs"]["condition"] is True
    first, second = _fit(ws, unit, "deep")
    assert (first.pass_kind, first.status, first.reason) == ("fast", "held", fast.reason)
    assert (second.pass_kind, second.status, second.payload) == ("deep", "held", None)
    assert second.reason == f"Deep pass not run: no fast-pass fit to build on ({fast.reason})"


def test_a_unit_without_its_resolution_curve_is_held_with_the_engines_reason(tmp_path):
    """Example 6."""
    ws, _data, scan = _ingested(tmp_path, library_skip=[("a05", fx.TRUE_SLIT)])
    out = _results(_fit(ws, f"{scan}:a05"))
    (held,) = out
    assert held.status == "held" and held.payload is not None
    assert held.reason == f"a05: no resolution curve for a05 at slit {fx.TRUE_SLIT}"
    _save(ws, out)
    assert ws.load()[1].get(f"{scan}:a05").status == "held_for_calibration"


class _Pool:
    """Records the worker count it was opened with; maps in this process."""
    opened: list = []

    def __init__(self, max_workers):
        _Pool.opened.append(max_workers)
        self.map = map

    def shutdown(self, *a, **k):
        pass


def test_a_deep_item_runs_on_the_cores_its_widest_stage_can_use_and_says_so(tmp_path, monkeypatch, no_sampling):
    """Example 8."""
    ws, _data, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    _save(ws, _fit(ws, unit))
    w = deep_pass.stage_width(ws.load()[1].get(unit).detectors["a04"]["fast_pass"])
    assert w > 1
    got = []
    real = deep_delivery.deliver
    monkeypatch.setattr(deep_delivery, "deliver", lambda fit, ctx, request, *, executor=None, **kw: (
        got.append((request.workers, executor)), real(fit, ctx, request, executor=executor, **kw))[1])
    monkeypatch.setattr(file_fit, "ProcessPoolExecutor", _Pool)
    _Pool.opened = []
    out = _fit(ws, unit, "deep", cores=w + 1)
    assert [(r.status, r.cores) for r in out] == [("cores", w), (out[1].status, None)] and out[1].pass_kind == "deep"
    assert got[0][0] == w and isinstance(got[0][1], _Pool) and _Pool.opened == [w]
    got.clear()
    out = _fit(ws, unit, "deep", cores=w)
    assert [r.status for r in out if r.status == "cores"] == [] and got[0][0] == w

    ws2, _d2, scan2 = _ingested(tmp_path / "fresh")
    out = _fit(ws2, f"{scan2}:a04", "deep", cores=50)
    assert [(r.pass_kind, r.status) for r in out if r.status != "attempt"][0][0] == "fast"
    kinds = [r.pass_kind for r in out if r.status != "attempt"]
    assert kinds == ["fast", "cores", "deep"]


def _record(models, saturated=-10.0):
    return {"ln_like_saturated": saturated,
            "models": [{"label": lb, "ln_z": z, "ln_model_prior": p} for lb, z, p in models]}


def _width_by_deep_pass(record) -> int:
    groups: dict = {}
    for m in record["models"]:
        groups.setdefault(m["label"], []).append(m)
    entries = []
    for label, basins in groups.items():
        import numpy as np
        priced = [b["ln_z"] for b in basins if b["ln_z"] is not None]
        entries.append(deep_pass.ModelEntry(
            label=label, count=1, ndim=1, problem=None, fastpass_lnl_opt=None,
            fastpass_ln_z=float(np.logaddexp.reduce(priced)) - record["ln_like_saturated"] if priced else None,
            log_model_prior=basins[0]["ln_model_prior"]))
    sizes = []

    def recording_map(fn, jobs):
        sizes.append(len(list(jobs)))
        return iter(())
    deep_pass.deep_pass(entries, chunk=1000, map_fn=recording_map)
    return max(sizes)


@pytest.mark.parametrize("models", [
    [("a", -10.0, -1.0), ("b", -12.0, -1.0), ("c", -13.0, -2.0), ("d", -40.0, -2.0), ("d", -38.0, -2.0),
     ("e", -60.0, -3.0), ("f", -70.0, -3.0)],                                       # every model priced
    [("a", -10.0, -1.0), ("b", None, -1.0), ("c", -11.0, -2.0), ("d", None, -2.0), ("e", -50.0, -2.0)],  # two unpriced
    [("a", -10.0, -1.0), ("b", -10.5, -1.0), ("c", -11.0, -1.0), ("d", -10.2, -1.0), ("e", -10.0, -1.0)],  # all P >= min
])
def test_the_deep_width_is_the_widest_stage_the_deep_pass_runs(models):
    """Example 9."""
    record = _record(models)
    assert deep_pass.stage_width(record) == _width_by_deep_pass(record)


# ---------------------------------------------------------------------------
# the real fit and the batch: the same hashes
# ---------------------------------------------------------------------------
def test_a_real_fast_pass_saved_from_fit_file_is_carried_by_the_batch(tmp_path):
    """The real fit through fit_file and save_results; run_batch then carries the unit, so its hashes and
    inputs stamp are the batch's own."""
    ws, data, scan = _ingested(tmp_path, analyzers=("a03",))
    out = _fit(ws, f"{scan}:a03")
    assert _results(out)[0].status in ("done", "done_warning")
    json.dumps([r.payload for r in out], allow_nan=True)
    imported = ws.load()[1].get(f"{scan}:a03").provenance["source_file"]
    assert _results(out)[0].payload["entry"]["provenance"]["source_file"] == imported   # the import's path
    _save(ws, out)
    summary = driver.run_batch(ws, [data], M=10, scan_M=50, errors_tier="fast", workers=1, formats=("markdown",))
    assert summary.n_fitted_this_run == 0
    # the batch stamps a file set's stem as source_file; the next item still finds the data
    entry = ws.load()[1].get(f"{scan}:a03")
    entry.provenance["source_file"] = entry.provenance["file_set"]
    card, session = ws.load()
    session.put(entry)
    ws.save(card, session)
    assert [r.status for r in _fit(ws, f"{scan}:a03")] == ["already_current"]
