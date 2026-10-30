"""Bounded rslice screen planning and reporting, with synthetic rows and no sampling."""
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def harness(monkeypatch):
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
        monkeypatch.setenv(name, os.environ.get(name, "1"))
    path = Path(__file__).resolve().parents[1] / "tools" / "calibrate_deep_pass.py"
    spec = importlib.util.spec_from_file_location("calibrate_rslice_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_write_retries_transient_reader_locks(harness, monkeypatch, tmp_path, caplog):
    report = {"status": "running", "runs": []}
    monkeypatch.setattr(harness, "render_markdown", lambda report: "synthetic report\n")
    real_replace = os.replace
    calls = {"calibration.json": 0, "calibration.md": 0}

    def temporarily_locked(source, target):
        calls[target.name] += 1
        if calls[target.name] == 1:
            raise PermissionError(5, "synthetic transient reader hold", str(target))
        real_replace(source, target)

    monkeypatch.setattr(harness.os, "replace", temporarily_locked)
    with caplog.at_level("WARNING", logger="isar.session.atomic"):
        harness.write_report(report, tmp_path)
    assert all(n >= 2 for n in calls.values())
    assert sum("synthetic transient reader hold" in r.message for r in caplog.records) == 2
    with (tmp_path / "calibration.json").open(encoding="utf-8", newline="") as stream:
        assert json.load(stream) == report
    with (tmp_path / "calibration.md").open(encoding="utf-8", newline="") as stream:
        assert stream.read() == "synthetic report\n"
    assert not list(tmp_path.glob("*.tmp"))


def entries():
    return [SimpleNamespace(label=f"m{i}", count=i, ndim=i + 2, fastpass_ln_z=-5.0,
                            fastpass_lnl_opt=-1.0, log_model_prior=-2.0, problem=None)
            for i in range(4)]


META = {"arena_fingerprint": "synthetic-arena", "prior_provenance": {"recipe": "test"}}
LABELS = ["m0", "m1", "m2"]


@pytest.mark.parametrize("protocol", ["calibrate", "rslice_screen"])
@pytest.mark.parametrize("first_write_fails", [False, True])
def test_final_report_failure_preserves_original_exception(
        harness, monkeypatch, tmp_path, caplog, protocol, first_write_fails):
    original = RuntimeError("original calculation failure")
    calls = []

    def write(report, output):
        calls.append(report["status"])
        if first_write_fails and len(calls) == 1:
            raise original
        if len(calls) > 1:
            raise PermissionError("secondary report writer failure")

    def run(*args, **kwargs):
        raise original

    monkeypatch.setattr(harness, "write_report", write)
    monkeypatch.setattr(harness, "run_jobs", run)
    kwargs = {"preferred_count": 1} if protocol == "calibrate" else {"labels": LABELS}
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as error:
        getattr(harness, protocol)(entries(), output=tmp_path, **kwargs)
    assert error.value is original
    assert calls[-1] == "interrupted_or_failed"
    assert "secondary report writer failure" in caplog.text


def fake_runner(h, seen, *, capped=False):
    def run(jobs, *, workers, timeout_s, on_result, deadline=None, skipped=None):
        seen.update(jobs=jobs, workers=workers, timeout_s=timeout_s, deadline=deadline)
        selected = jobs[:1] if capped else jobs
        for job in selected:
            row = h._base_row(job)
            row.update(log_z=-10.0 + job["seed"] % 3, lnl_max=-1.0, information_h=12.0,
                       n_like_calls=123, wall_s=2.0, status="ok", evidence_converged=True)
            if capped:
                row.update(status="failed", error=h.WALL_CAP, evidence_converged=False)
            assert not on_result(row)
        if capped:
            skipped.extend(jobs[1:])
    return run


@pytest.mark.parametrize("include_baseline, expected", [(False, 18), (True, 27)])
def test_screen_plans_only_selected_models_and_reports_every_seed(
        harness, monkeypatch, tmp_path, include_baseline, expected):
    h, seen = harness, {}
    monkeypatch.setattr(h, "run_jobs", fake_runner(h, seen))
    result = h.rslice_screen(entries(), labels=LABELS, output=tmp_path,
                            include_rwalk_baseline=include_baseline, metadata=META)
    jobs = seen["jobs"]
    assert len(jobs) == expected
    assert {j["model"].label for j in jobs} == set(LABELS)
    assert len({j["seed"] for j in jobs}) == expected
    assert min(j["seed"] for j in jobs) > 20261026 + 100_000
    configs = {(j["settings"]["sample"], j["settings"]["n_live"],
                j["settings"]["step_factor"]) for j in jobs}
    assert configs == ({("rslice", 400, 1), ("rslice", 400, 2)} |
                       ({("rwalk", 400, 4)} if include_baseline else set()))
    assert seen["workers"] <= 18 and seen["timeout_s"] == 3600 and seen["deadline"] is not None
    assert result["status"] == "complete"
    assert all(g["complete"] and g["seed_std"] == pytest.approx(1.0) and
               g["spread"] == pytest.approx(2.0) for g in result["analysis"]["groups"])
    assert "installed" not in result and "chosen" not in result["analysis"]
    with (tmp_path / "calibration.json").open(encoding="utf-8", newline="") as stream:
        saved = json.load(stream)
    assert len(saved["runs"]) == expected
    with (tmp_path / "calibration.md").open(encoding="utf-8", newline="") as stream:
        rendered = stream.read()
    assert "ddof=1" in rendered and "Every seed" in rendered and "report only" in rendered


def test_wall_cap_reports_unstarted_jobs_and_incomplete_groups(harness, monkeypatch, tmp_path):
    h, seen = harness, {}
    monkeypatch.setattr(h, "run_jobs", fake_runner(h, seen, capped=True))
    result = h.rslice_screen(entries(), labels=LABELS, output=tmp_path, metadata=META)
    assert result["status"] == "cut_by_wall_cap"
    assert len(result["runs"]) == 1 and len(result["not_run"]) == 17
    assert {r["seed"] for r in result["runs"] + result["not_run"]} == {
        job["seed"] for job in seen["jobs"]}
    assert all(not g["complete"] and g["seed_std"] is None for g in result["analysis"]["groups"])


@pytest.mark.parametrize("changes", [{"workers": 19}, {"labels": ["m0", "m1"]},
                                     {"labels": ["m0", "m1", "unknown"]}])
def test_invalid_screen_refuses_before_any_sampling(harness, monkeypatch, tmp_path, changes):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid request reached sampling")
    monkeypatch.setattr(harness, "run_jobs", forbidden)
    args = dict(labels=LABELS, output=tmp_path, metadata=META)
    args.update(changes)
    with pytest.raises(ValueError):
        harness.rslice_screen(entries(), **args)


def test_external_baseline_checks_identity_before_comparing(harness, monkeypatch, tmp_path):
    h, seen = harness, {}
    monkeypatch.setattr(h, "run_jobs", fake_runner(h, seen))
    baseline = h.rslice_screen(entries(), labels=LABELS, output=tmp_path / "base",
                              include_rwalk_baseline=True, metadata=META)
    path = tmp_path / "base" / "calibration.json"
    result = h.rslice_screen(entries(), labels=LABELS, output=tmp_path / "screen",
                            baseline=path, metadata=META)
    assert len(result["runs"]) == 18 and len(result["baseline"]["runs"]) == 9
    assert all(c["mean_wall_ratio"] == pytest.approx(1.0)
               for c in result["analysis"]["comparison"])
    with pytest.raises(ValueError, match="arena_fingerprint"):
        h.rslice_screen(entries(), labels=LABELS, output=tmp_path / "bad",
                        baseline=path, metadata={**META, "arena_fingerprint": "other"})
    assert baseline["status"] == "complete"


@pytest.mark.parametrize("refresh", [False, True])
def test_screen_cli_bypasses_automatic_core_selection(harness, monkeypatch, tmp_path, refresh):
    h, called, loader = harness, {}, {}

    def load(*args, **kwargs):
        loader.update(kwargs)
        return {"entries": entries(), "metadata": META}

    monkeypatch.setattr(h, "load_workspace", load)
    monkeypatch.setattr(h, "core_set", lambda *args: pytest.fail("screen selected an automatic core"))

    def screen(loaded, **kwargs):
        called.update(kwargs)
        assert len(loaded) == 4
        return {"status": "complete", "elapsed_wall_s": 0.0}

    monkeypatch.setattr(h, "rslice_screen", screen)
    argv = ["calibrate_deep_pass.py", "--workspace", str(tmp_path), "--spectrum", "synthetic:1:a01",
            "--output", str(tmp_path / "out"), "--rslice-screen",
            "--model", "m0", "--model", "m1", "--model", "m2"]
    monkeypatch.setattr(sys, "argv", argv + (["--refresh-fast-pass", "--include-rwalk-baseline"] if refresh else []))
    assert h.main() == 0
    assert called["labels"] == LABELS and called["run_timeout_s"] == 3600 and called["wall_cap_s"] == 7200
    assert loader["refresh_fast_pass"] == refresh and called["include_rwalk_baseline"] == refresh


def test_refresh_cli_requires_a_new_baseline_before_loading(harness, monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "load_workspace", lambda *args, **kwargs: pytest.fail("loaded before refusal"))
    monkeypatch.setattr(sys, "argv", ["calibrate_deep_pass.py", "--workspace", str(tmp_path),
                                     "--spectrum", "synthetic:1:a01", "--output", str(tmp_path / "out"),
                                     "--rslice-screen", "--refresh-fast-pass",
                                     "--model", "m0", "--model", "m1", "--model", "m2"])
    with pytest.raises(SystemExit) as error:
        harness.main()
    assert error.value.code == 2


def test_refresh_uses_standard_settings_hints_and_whitelist(harness, monkeypatch):
    from isar.analyze import fast_pass
    from isar.session import guidelines, scan_delivery

    ctx, card, settings = object(), object(), SimpleNamespace(mode_ceiling=4)
    peaks, labels, structures = ("peak hint",), ("guideline model",), ("resolved model",)
    g = SimpleNamespace(peaks_for=lambda **kwargs: peaks, structure_labels_for=lambda **kwargs: labels)
    called = {}
    monkeypatch.setattr(guidelines, "fast_pass_settings", lambda supplied: settings if supplied is card else None)

    def whitelist(supplied, supplied_ctx, ceiling):
        assert (supplied, supplied_ctx, ceiling) == (labels, ctx, 4)
        return structures, structures, None

    def run(supplied_ctx, **kwargs):
        assert supplied_ctx is ctx
        called.update(kwargs)
        return SimpleNamespace(timing={"total": 0.1})

    monkeypatch.setattr(scan_delivery, "_structure_whitelist", whitelist)
    monkeypatch.setattr(fast_pass, "run_fast_pass", run)
    monkeypatch.setattr(scan_delivery, "_fast_pass_record", lambda result: {"refreshed": True})
    record, timing = harness._refresh_fast_record(ctx, card, g, scan_number=1, q=None, T=100.0)
    assert record == {"refreshed": True} and timing == {"total": 0.1}
    assert called == {"settings": settings, "peaks": peaks, "structures": structures}


@pytest.mark.parametrize("refresh", [False, True])
def test_workspace_refresh_is_explicit_and_only_changes_card_copy(harness, monkeypatch, tmp_path, refresh):
    from isar.reduce import combine, qc
    from isar.session import calibrations, ingest, outcome, router, scan_delivery, unit_data
    from isar.session import guidelines
    from isar.session.workspace import Workspace

    card = SimpleNamespace(state={"refreshed": False})
    stored = {"arena_fingerprint": "old-arena"}
    rec = SimpleNamespace(experiment_id="synthetic", scan_number=1, facts={},
                          spectra={"a01": SimpleNamespace(q=None)})
    session = SimpleNamespace(data_paths=["synthetic-source"],
                              scans={"synthetic:1:a01": SimpleNamespace(detectors={"a01": stored})})
    monkeypatch.setattr(Workspace, "open", lambda path: SimpleNamespace(load=lambda: (card, session)))
    monkeypatch.setattr(outcome, "fast_pass_of", lambda value: value)
    monkeypatch.setattr("isar.session.file_fit.given_files", lambda *args: [tmp_path / "synthetic-source"])  # which files is not under test here
    monkeypatch.setattr(ingest, "discover", lambda *args: SimpleNamespace(companion_files=[]))
    monkeypatch.setattr(ingest, "parse_all", lambda *args: [rec])
    monkeypatch.setattr(combine, "reduce_record", lambda *args: None)
    monkeypatch.setattr(qc, "qc_record", lambda *args: None)
    monkeypatch.setattr(router, "route", lambda records: SimpleNamespace(measurements=records))
    monkeypatch.setattr(router, "apply_context_updates", lambda *args, **kwargs: None)
    monkeypatch.setattr(guidelines, "resolve", lambda card: object())
    monkeypatch.setattr(unit_data, "scan_temperature", lambda rec: 100.0)
    monkeypatch.setattr(calibrations, "calibration_view", lambda supplied: supplied)

    def refresh_card(supplied, records, *, base_dir):
        assert supplied is not card and records == [rec] and base_dir == tmp_path
        supplied.state["refreshed"] = True
        return ["synthetic calibration refreshed"]

    monkeypatch.setattr(calibrations, "refresh_tabulated", refresh_card)
    monkeypatch.setattr(calibrations, "resolve", lambda rec, det, supplied, view:
                        SimpleNamespace(held_reason=None if supplied.state["refreshed"] else "changed curve"))
    monkeypatch.setattr(scan_delivery, "build_context", lambda *args, **kwargs: ("ctx", 20, []))
    monkeypatch.setattr(harness, "_refresh_fast_record", lambda *args, **kwargs:
                        ({"arena_fingerprint": "new-arena"}, {"total": 0.1}))
    monkeypatch.setattr(harness, "_loaded", lambda ctx, record, **source:
                        {"record": record, "metadata": source})
    if refresh:
        loaded = harness.load_workspace(tmp_path, "synthetic:1:a01", refresh_fast_pass=True)
        assert loaded["record"]["arena_fingerprint"] == "new-arena"
        assert loaded["metadata"]["refreshed_fast_pass_record"] == loaded["record"]
        assert loaded["metadata"]["original_arena_fingerprint"] == "old-arena"
    else:
        with pytest.raises(ValueError, match="changed curve"):
            harness.load_workspace(tmp_path, "synthetic:1:a01")
    assert card.state == {"refreshed": False} and stored == {"arena_fingerprint": "old-arena"}


@pytest.mark.parametrize("condition", [None, "excluded", "dead"])
def test_refresh_uses_saved_inputs_and_refuses_saved_conditions(harness, monkeypatch, tmp_path, condition):
    import numpy as np
    from isar.intake import store
    from isar.reduce import combine, qc
    from isar.session import calibrations, ingest, outcome, router, scan_delivery, guidelines
    from isar.session.workspace import Workspace

    @dataclass
    class Temperature:
        setpoint: object
        sensors: dict
        valid_mask: object

    @dataclass
    class Record:
        experiment_id: str
        scan_number: int
        facts: dict
        temperature: Temperature
        spectra: dict

    rec = Record("synthetic", 1, {"T_K": {"value": 100.0, "source": "file"},
                                "slit": {"value": "0.1x0.1", "source": "file"}},
                 Temperature(np.array([100.0]), {}, np.array([True])),
                 {"a01": SimpleNamespace(q=None)})
    for fact, value in (("temperature", 300.0), ("slit", "0.4x0.4")):
        store.append(tmp_path, store.ENTRIES,
                     {"kind": "value", "fact": fact, "value": value, "targets": ["synthetic:1"]})
    if condition:
        store.append(tmp_path, store.ENTRIES,
                     {"kind": "condition", "condition": condition, "declared": True,
                      "targets": ["synthetic:1" + (":a01" if condition == "dead" else "")]})
    before = (tmp_path / store.ENTRIES).read_bytes()
    card = SimpleNamespace(state={})
    session = SimpleNamespace(data_paths=["synthetic-source"], scans={"synthetic:1:a01":
                              SimpleNamespace(detectors={"a01": {"arena_fingerprint": "old"}})})
    monkeypatch.setattr(Workspace, "open", lambda path: SimpleNamespace(load=lambda: (card, session)))
    monkeypatch.setattr(outcome, "fast_pass_of", lambda value: value)
    monkeypatch.setattr("isar.session.file_fit.given_files", lambda *args: [tmp_path / "synthetic-source"])  # which files is not under test here
    monkeypatch.setattr(ingest, "discover", lambda *args: SimpleNamespace(companion_files=[]))
    monkeypatch.setattr(ingest, "parse_all", lambda *args: [rec])
    monkeypatch.setattr(combine, "reduce_record", lambda *args: None)
    monkeypatch.setattr(qc, "qc_record", lambda *args: None)
    monkeypatch.setattr(router, "route", lambda records: SimpleNamespace(measurements=records))
    monkeypatch.setattr(router, "apply_context_updates", lambda *args, **kwargs: None)
    monkeypatch.setattr(guidelines, "resolve", lambda card: object())
    seen = {}

    def refresh_card(card, records, **kwargs):
        assert not condition, "a saved condition reached calibration"
        seen["calibration"] = records[0]
        return []

    def context(effective, *args, T, **kwargs):
        seen.update(context=effective, temperature=T)
        return "ctx", 20, []

    monkeypatch.setattr(calibrations, "refresh_tabulated", refresh_card)
    monkeypatch.setattr(calibrations, "calibration_view", lambda card: card)
    monkeypatch.setattr(calibrations, "resolve", lambda *args: SimpleNamespace(held_reason=None))
    monkeypatch.setattr(scan_delivery, "build_context", context)
    monkeypatch.setattr(harness, "_refresh_fast_record", lambda *args, **kwargs: ({"new": True}, {}))
    monkeypatch.setattr(harness, "_loaded", lambda *args, **kwargs: kwargs)
    if condition:
        with pytest.raises(ValueError, match=condition):
            harness.load_workspace(tmp_path, "synthetic:1:a01", refresh_fast_pass=True)
    else:
        harness.load_workspace(tmp_path, "synthetic:1:a01", refresh_fast_pass=True)
        assert seen["calibration"] is seen["context"]
        assert seen["calibration"].facts["slit"]["value"] == "0.4x0.4"
        assert seen["temperature"] == 300.0
        assert rec.facts["slit"]["value"] == "0.1x0.1" and rec.temperature.setpoint[0] == 100.0
    assert (tmp_path / store.ENTRIES).read_bytes() == before


@pytest.mark.parametrize("setup_failure", [False, True])
def test_retry_worker_uses_production_ladder_and_streams_attempts(harness, monkeypatch, setup_failure):
    from isar.analyze import deep_pass

    messages = []
    model = entries()[0]
    job = dict(model=model, stage="accepted", seed=123, engine="dynesty",
               production_retry=True, settings=vars(deep_pass.ACCEPTED))

    def nested(label, problem, *, settings, seed, reference_lnl):
        if setup_failure and seed == 123:
            return deep_pass.RunRecord(label, seed, settings, 0, float("nan"), float("nan"), float("nan"),
                                       0, 0, float("nan"), .1, reference_lnl,
                                       failure="run failed: ValueError: synthetic problem setup failure")
        return deep_pass.RunRecord(label, seed, settings, model.ndim, -5.0, .2, 10.0,
                                   30, 20, -1.0, .1, reference_lnl,
                                   suspect=("synthetic missed mode",) if seed == 123 else (),
                                   evidence_converged=True)

    monkeypatch.setattr(deep_pass, "run_nested", nested)
    row = harness._retry_worker(job, SimpleNamespace(send=messages.append))
    assert deep_pass.run_nested is nested
    assert [m["event"] for m in messages] == ["attempt_started", "attempt_completed"] * 2
    attempts = [m for m in messages if m["event"] == "attempt_completed"]
    assert [m["seed"] for m in attempts] == [123, 123 + 7919]
    assert [m["settings"]["step_factor"] for m in attempts] == [4, 8]
    assert [m["usable"] for m in attempts] == [False, True]
    assert row["seed"] == 123 and row["final_seed"] == 123 + 7919
    assert row["retry_count"] == 1 and row["usable"] and row["total_like_calls"] == (30 if setup_failure else 60)
    if setup_failure:
        assert attempts[0]["status"] == "failed" and "setup failure" in attempts[0]["error"]
        assert attempts[0]["ndim"] == 0 and attempts[0]["n_like_calls"] == 0


@pytest.mark.parametrize("dimension", [0, 1])
def test_retry_worker_still_refuses_wrong_usable_dimensions(harness, monkeypatch, dimension):
    from isar.analyze import deep_pass

    model = entries()[0]
    job = dict(model=model, stage="accepted", seed=123, engine="dynesty",
               production_retry=True, settings=vars(deep_pass.ACCEPTED))
    monkeypatch.setattr(deep_pass, "run_nested", lambda label, problem, *, settings, seed, reference_lnl:
                        deep_pass.RunRecord(label, seed, settings, dimension, -5.0, .2, 10.0,
                                            30, 20, -1.0, .1, reference_lnl, evidence_converged=True))
    with pytest.raises(ValueError, match="identity"):
        harness._retry_worker(job, SimpleNamespace(send=lambda row: None))


@pytest.mark.parametrize("broken_poll", [False, True])
def test_retry_cleanup_drains_buffered_attempts_without_masking_failure(harness, monkeypatch, broken_poll):
    from collections import deque

    jobs = [dict(model=e, stage="accepted", seed=10 + i, engine="dynesty", production_retry=True,
                 settings=harness._settings(400, 4)) for i, e in enumerate(entries()[:2])]
    readers = []
    for job in jobs:
        identity = dict(label=job["model"].label, stage=job["stage"], job_seed=job["seed"],
                        seed=job["seed"], settings=job["settings"], attempt=0)
        messages = deque([dict(event="attempt_started", **identity, started_monotonic=harness.time.monotonic()),
                          dict(event="attempt_completed", **identity, log_z=-2.0, wall_s=.1),
                          dict(event="attempt_started", **{**identity, "attempt": 1, "seed": job["seed"] + 7919},
                               started_monotonic=harness.time.monotonic())])
        def poll(messages=messages, first=not readers):
            if broken_poll and first and not messages:
                raise BrokenPipeError(109, "synthetic closed Windows pipe")
            return bool(messages)

        readers.append(SimpleNamespace(poll=poll,
                                       recv=lambda messages=messages: messages.popleft(), close=lambda: None))
    pipes = iter(readers)
    processes = []

    class Process:
        exitcode = 0

        def __init__(self, **kwargs):
            self.alive = True
            processes.append(self)

        def start(self):
            pass

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False

        kill = terminate

        def join(self, **kwargs):
            pass

    monkeypatch.setattr(harness.mp, "get_context", lambda method: SimpleNamespace(
        Pipe=lambda **kwargs: (next(pipes), SimpleNamespace(close=lambda: None)), Process=Process))
    events, results = [], []
    original = RuntimeError("first report failure")

    def receive(event):
        events.append(event)
        if len(events) == 1:
            raise original

    with pytest.raises(RuntimeError) as error:
        harness.run_jobs(jobs, workers=2, timeout_s=60, on_result=results.append, on_attempt=receive)
    assert error.value is original
    assert {e["label"] for e in events if e["event"] == "attempt_completed"} == {"m0", "m1"}
    interrupted = [e for e in events if e["event"] == "attempt_interrupted"]
    assert len(interrupted) == 2 and all(e["attempt"] == 1 for e in interrupted)
    assert len(results) == 2 and all(len(r["attempts"]) == 1 for r in results)
    assert len(processes) == 2 and all(not p.alive for p in processes)


def test_completed_job_does_not_poll_closed_windows_pipe_again(harness, monkeypatch):
    job = dict(model=entries()[0], stage="accepted", seed=10, engine="dynesty", production_retry=True,
               settings=harness._settings(400, 4))
    calls = []

    def poll():
        calls.append("poll")
        if len(calls) > 1:
            raise BrokenPipeError(109, "closed after the final result")
        return True

    final = {**harness._base_row(job), "status": "ok", "log_z": -2., "usable": True,
             "evidence_converged": True}
    reader = SimpleNamespace(poll=poll, recv=lambda: final, close=lambda: None)
    process = SimpleNamespace(start=lambda: None, is_alive=lambda: False, join=lambda **kwargs: None)
    monkeypatch.setattr(harness.mp, "get_context", lambda method: SimpleNamespace(
        Pipe=lambda **kwargs: (reader, SimpleNamespace(close=lambda: None)), Process=lambda **kwargs: process))
    rows = harness.run_jobs([job], workers=1, timeout_s=60, on_result=lambda row: None)
    assert len(rows) == 1 and rows[0]["log_z"] == -2. and rows[0]["usable"]
    assert calls == ["poll"]


def test_windows_pipe_break_before_result_is_retained_as_failure(harness, monkeypatch, caplog):
    job = dict(model=entries()[0], stage="accepted", seed=10, engine="dynesty", production_retry=True,
               settings=harness._settings(400, 4))

    def poll():
        raise BrokenPipeError(109, "closed without a result")

    reader = SimpleNamespace(poll=poll, close=lambda: None)
    process = SimpleNamespace(start=lambda: None, is_alive=lambda: False, join=lambda **kwargs: None)
    monkeypatch.setattr(harness.mp, "get_context", lambda method: SimpleNamespace(
        Pipe=lambda **kwargs: (reader, SimpleNamespace(close=lambda: None)), Process=lambda **kwargs: process))
    rows = harness.run_jobs([job], workers=1, timeout_s=60, on_result=lambda row: None)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "closed without a result" in rows[0]["error"] and "pipe closed" in caplog.text


def test_retry_calibration_plans_54_jobs_and_uses_only_final_usable_values(harness, monkeypatch, tmp_path):
    from scipy.stats import chi2
    from isar.analyze import deep_pass
    import math

    seen = {}

    def runner(jobs, *, on_result, on_attempt, **kwargs):
        seen.update(jobs=jobs, **kwargs)
        indices = {}
        for job in jobs:
            key = (job["stage"], job["model"].label)
            index = indices.get(key, 0)
            indices[key] = index + 1
            # Each job failed once far below the optimum, then production recovered.
            bad = {**harness._base_row(job), "event": "attempt_completed", "job_seed": job["seed"],
                   "attempt": 0, "log_z": -1000.0, "suspect": ["missed mode"], "wall_s": 1.0}
            on_attempt(bad)
            good = {**harness._base_row(job), "status": "ok", "log_z": float(index), "usable": True,
                    "retry_count": 1, "wall_s": 2.0, "evidence_converged": True}
            on_attempt({**good, "event": "attempt_completed", "job_seed": job["seed"], "attempt": 1})
            on_result(good)

    monkeypatch.setattr(harness, "run_jobs", runner)
    report = harness.retry_calibration(entries(), labels=LABELS, output=tmp_path)
    jobs = seen["jobs"]
    assert len(jobs) == 54 and len({j["seed"] for j in jobs}) == 54
    assert min(j["seed"] for j in jobs) > 20261026 + 200_000
    assert {j["stage"] for j in jobs} == {"accepted", "exploratory"}
    for job in jobs:
        assert job["settings"] == vars(getattr(deep_pass, job["stage"].upper()))
    assert seen["timeout_s"] == 3600 and seen["workers"] <= 18 and seen["deadline"] is not None
    assert report["status"] == "complete" and len(report["attempts"]) == 108
    for group in report["analysis"]["groups"]:
        assert group["n_usable"] == 9 and group["retries"] == 9
        assert group["seed_std"] == pytest.approx(math.sqrt(7.5))
        assert group["upper_95"] == pytest.approx(math.sqrt(7.5 * 8 / chi2.ppf(.05, 8)))
    sigma = report["analysis"]["proposed_sigma"]
    assert sigma["exploratory"] == pytest.approx(sigma["accepted"] + .5)
    assert "installed" not in report


def test_retry_incomplete_groups_never_propose_new_sigma(harness):
    report = dict(core=[{"label": lb} for lb in LABELS], runs=[
        dict(stage="accepted", label="m0", usable=False, log_z=-1000.0, wall_s=1.0, retry_count=2),
        dict(stage="accepted", label="m0", usable=True, evidence_converged=True, log_z=-1.0, wall_s=1.0, retry_count=0),
        dict(stage="accepted", label="m0", usable=True, evidence_converged=False, log_z=-20.0, wall_s=1.0, retry_count=0)])
    analysis = harness._retry_analysis(report)
    group = analysis["groups"][0]
    assert group["n_jobs"] == 3 and group["n_usable"] == 1 and group["seed_std"] is None
    assert analysis["proposed_sigma"] == {"accepted": None, "exploratory": None}


@pytest.mark.parametrize("changes", [{"run_timeout_s": 3601}, {"wall_cap_s": 7201}, {"workers": 19}])
def test_retry_refuses_excess_caps_before_sampling(harness, monkeypatch, tmp_path, changes):
    monkeypatch.setattr(harness, "run_jobs", lambda *args, **kwargs: pytest.fail("sampled past protocol cap"))
    with pytest.raises(ValueError, match="permits"):
        harness.retry_calibration(entries(), labels=LABELS, output=tmp_path, **changes)


def test_retry_cli_dispatches_without_automatic_model_selection(harness, monkeypatch, tmp_path):
    called = {}
    monkeypatch.setattr(harness, "load_workspace", lambda *args, **kwargs: {"entries": entries(), "metadata": META})
    monkeypatch.setattr(harness, "core_set", lambda *args: pytest.fail("unexpected core selection"))

    def retry(entries, **kwargs):
        called.update(kwargs)
        return {"status": "complete", "elapsed_wall_s": 0.0}

    monkeypatch.setattr(harness, "retry_calibration", retry)
    monkeypatch.setattr(sys, "argv", ["calibrate_deep_pass.py", "--workspace", str(tmp_path),
                                     "--spectrum", "synthetic:1:a01", "--output", str(tmp_path / "out"),
                                     "--production-retry-calibration", "--model", "m0", "--model", "m1", "--model", "m2"])
    assert harness.main() == 0 and called["labels"] == LABELS
