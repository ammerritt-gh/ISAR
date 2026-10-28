"""Bounded rslice screen planning and reporting, with synthetic rows and no sampling."""
import importlib.util
import json
import os
import sys
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
    rec = SimpleNamespace(experiment_id="synthetic", scan_number=1,
                          spectra={"a01": SimpleNamespace(q=None)})
    session = SimpleNamespace(data_paths=["synthetic-source"],
                              scans={"synthetic:1:a01": SimpleNamespace(detectors={"a01": stored})})
    monkeypatch.setattr(Workspace, "open", lambda path: SimpleNamespace(load=lambda: (card, session)))
    monkeypatch.setattr(outcome, "fast_pass_of", lambda value: value)
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
