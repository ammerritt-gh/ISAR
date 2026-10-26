"""`isar profile` — session-log profiler (plan §6)."""
import argparse

import pytest

from isar.cli import profile_cmd
from isar.drive.session_log import log_event


def _args(workspace, *, log=None, top=None, iterations=False):
    return argparse.Namespace(workspace=str(workspace), log=log, top=top, iterations=iterations)


def _write_campaign(log_path):
    """A small synthetic campaign: 2 iterations of timing + propose/analyze/exception events."""
    t = 1_000_000.0
    for it in range(2):
        # propose (proposal kind + hkl land in the per-iteration table)
        log_event(log_path, "propose",
                  {"hkl": [2.0, 0.0, float(it)], "kind": "production",
                   "reason": "eig", "score": 0.9, "iter": it}, ts=t)
        t += 0.1
        # driver-side timing spans
        log_event(log_path, "timing",
                  {"stage": "model_fit", "iter": it, "seconds": 1.5, "ok": True}, ts=t)
        t += 1.5
        log_event(log_path, "timing",
                  {"stage": "isar_run", "iter": it, "seconds": 40.0, "ok": True,
                   "errors_tier": "full", "attempt": 0}, ts=t)
        t += 40.0
        # analyze carrying embedded subprocess timings
        log_event(log_path, "analyze",
                  {"job_id": f"j-{it}", "errors_tier": "full",
                   "scan_errors_tier": "fast", "master_errors_tier": "full", "n_rows": 3,
                   "dispositions": {"ok": 3}, "iter": it,
                   "timings": {"total_seconds": 35.0, "argv": ["isar", "run"],
                               "errors_tier": "full",
                               "stages": [{"stage": "ingest_parse", "seconds": 20.0},
                                          {"stage": "report_render", "seconds": 10.0},
                                          {"stage": "fit_scans", "seconds": 5.0}]}}, ts=t)
        t += 0.1
        if it == 1:
            log_event(log_path, "exception",
                      {"action": "hold", "reason": "missed_window", "iter": it}, ts=t)
            t += 0.1
        # whole-iteration span
        log_event(log_path, "timing",
                  {"stage": "iteration", "iter": it, "seconds": 42.0, "ok": True}, ts=t)
        t += 0.1
    log_event(log_path, "stop", {"state": "met", "reasons": ["sigma <= tol"], "iter": None}, ts=t)


def _write_campaign_with_error_methods(log_path):
    """A single-iteration campaign whose embedded ``fit_scans`` stage carries ``per_task`` entries
    with the four error-method *_seconds keys (Stage 4 instrumentation)."""
    t = 1_000_000.0
    log_event(log_path, "timing",
              {"stage": "isar_run", "iter": 0, "seconds": 10.0, "ok": True,
               "errors_tier": "full", "attempt": 0}, ts=t)
    t += 10.0
    log_event(log_path, "analyze",
              {"job_id": "j-0", "errors_tier": "full", "n_rows": 2,
               "dispositions": {"ok": 2}, "iter": 0,
               "timings": {"total_seconds": 9.0, "argv": ["isar", "run"],
                           "errors_tier": "full",
                           "stages": [
                               {"stage": "fit_scans", "seconds": 6.0,
                                "per_task": [
                                    {"scan_id": "E:1:ana7", "seconds": 3.0,
                                     "bootstrap_seconds": 1.0, "minos_seconds": 0.5,
                                     "profile_seconds": 0.5, "bayes_seconds": 0.0},
                                    {"scan_id": "E:2:ana7", "seconds": 3.0,
                                     "bootstrap_seconds": 2.0, "minos_seconds": 0.3,
                                     "profile_seconds": 0.2, "bayes_seconds": 0.0},
                                ]},
                               {"stage": "fit_masters", "seconds": 3.0,
                                "per_master": [
                                    {"master_id": "m-1", "seconds": 3.0,
                                     "bootstrap_seconds": 1.5, "minos_seconds": 0.4,
                                     "profile_seconds": 0.1, "bayes_seconds": 1.0},
                                ]},
                           ]}}, ts=t)
    t += 0.1
    log_event(log_path, "timing",
              {"stage": "iteration", "iter": 0, "seconds": 10.1, "ok": True}, ts=t)
    t += 0.1
    log_event(log_path, "stop", {"state": "met", "reasons": ["done"], "iter": None}, ts=t)


def test_error_method_breakdown_renders_with_keys(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign_with_error_methods(log)
    rc = profile_cmd.run(_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "error-method breakdown" in out
    assert "bootstrap_seconds" in out
    assert "minos_seconds" in out
    assert "profile_seconds" in out
    assert "bayes_seconds" in out
    # bootstrap_seconds: 1.0 + 2.0 + 1.5 = 4.5 total over 3 entries
    section = out.split("error-method breakdown")[1]
    lines = [ln for ln in section.splitlines() if "bootstrap_seconds" in ln]
    assert lines
    toks = lines[0].split()
    assert int(toks[1]) == 3           # count
    assert float(toks[2]) == pytest.approx(4.5)   # total_s
    assert "covariance (HESSE-equivalent) cost is inside the fast pass" in out


def test_error_method_breakdown_skipped_without_keys(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign(log)
    rc = profile_cmd.run(_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "error-method breakdown" not in out
    assert "covariance (HESSE-equivalent) cost is inside the fast pass" not in out


def test_missing_log_exits_2(tmp_path, capsys):
    rc = profile_cmd.run(_args(tmp_path / "no_ws"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "no session log" in err


def test_empty_log_exits_2(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("", encoding="utf-8")
    rc = profile_cmd.run(_args(tmp_path))
    assert rc == 2
    assert "empty" in capsys.readouterr().err


def test_stage_and_subprocess_tables(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign(log)
    rc = profile_cmd.run(_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    # stage names from timing events
    assert "isar_run" in out
    assert "model_fit" in out
    # the wrapping "iteration" span is reported as a summary line, not a stage row (it would
    # otherwise double-count against %wall), and %wall of the child rows must not exceed 100%.
    stage_section = out.split("isar_run: subprocess")[0]
    assert "Iterations: 2" in stage_section
    assert "total iteration time" in stage_section
    pcts = [float(tok.rstrip("%")) for tok in stage_section.split()
            if tok.endswith("%") and tok.rstrip("%").replace(".", "", 1).isdigit()]
    assert pcts and sum(pcts) <= 100.0 + 1e-6
    # embedded subprocess sub-stages
    assert "ingest_parse" in out
    assert "report_render" in out
    assert "subprocess overhead" in out
    # campaign wall reported
    assert "campaign wall" in out
    # a percentage-of-wall column is rendered
    assert "%wall" in out


def test_subprocess_overhead_includes_certification_parent(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    log_event(log, "timing", {"stage": "isar_run", "seconds": 10.0}, ts=100.0)
    log_event(log, "analyze", {
        "iter": 0, "timings": {"total_seconds": 9.0, "stages": []}}, ts=110.0)
    log_event(log, "timing", {"stage": "certify", "seconds": 20.0}, ts=111.0)
    log_event(log, "analyze", {
        "iter": None, "timings": {"total_seconds": 19.0, "stages": []}}, ts=131.0)
    log_event(log, "stop", {"state": "met", "reasons": []}, ts=132.0)

    assert profile_cmd.run(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "subprocess overhead (interpreter + analysis process startup): 2.000s" in out
    assert "-18.000s" not in out


def test_certification_table_reports_critical_path_not_summed_work(
        tmp_path, capsys, monkeypatch):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign(log)
    monkeypatch.setattr(profile_cmd, "certification_pass_breakdown", lambda _workspace, _events: [{
        "requested_points": 21, "carried_points": 18, "newly_fitted": 3,
        "reused_checkpoint": 0, "wall_seconds": 40.0, "unit_work_seconds": 102.0,
        "critical_unit_seconds": 35.0, "slowest_hkl": [2.7, 0.0, 0.0],
        "slowest_master_id": "master:q2.7",
        "critical_method_seconds": {"bootstrap_seconds": 25.0, "profile_seconds": 8.0},
    }])

    assert profile_cmd.run(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Certification passes (parallel-unit critical path)" in out
    assert "21" in out and "18" in out and "40.000" in out and "35.000" in out
    assert "master:q2.7" in out
    assert "fitted-unit seconds overlap" in out


def test_top_limits_rows(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign(log)
    profile_cmd.run(_args(tmp_path, top=1))
    out = capsys.readouterr().out
    # "iteration" wraps the whole body and is now a summary line, not a stage peer row, so
    # "isar_run" (80s) is the single largest stage; smaller stages like model_fit must be
    # dropped from the top-1 stage table. (They can still appear as sub-stages below, so
    # assert on the stage-table section only.)
    stage_section = out.split("isar_run: subprocess")[0]
    assert "isar_run" in stage_section
    assert "model_fit" not in stage_section


def test_iterations_table(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign(log)
    rc = profile_cmd.run(_args(tmp_path, iterations=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Per-iteration breakdown" in out
    # proposal kind + hkl and errors tier surface in the table
    assert "production" in out
    assert "fast/full" in out
    # dominant stage of each iteration is isar_run (40s >> model_fit 1.5s)
    assert "isar_run" in out


def test_log_override(tmp_path, capsys):
    log = tmp_path / "elsewhere.jsonl"
    _write_campaign(log)
    rc = profile_cmd.run(_args(tmp_path / "unused_ws", log=str(log)))
    assert rc == 0
    assert "isar_run" in capsys.readouterr().out


def test_no_timing_events_gap_estimate(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    log.parent.mkdir(parents=True)
    log_event(log, "propose", {"hkl": [1, 0, 0]}, ts=100.0)
    log_event(log, "run", {"job_id": "j", "state": "done"}, ts=140.0)
    log_event(log, "analyze", {"job_id": "j", "n_rows": 2}, ts=145.0)
    rc = profile_cmd.run(_args(tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "No timing events" in out
    assert "approximate" in out.lower()
    # gap attributed to the later event's kind: run gets the 40s gap
    assert "run" in out


def test_compare_workspace_reports_contract_differences(tmp_path, capsys):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _write_campaign(left / "report" / "session_log.jsonl")
    _write_campaign(right / "report" / "session_log.jsonl")

    args = _args(left)
    args.compare_workspace = str(right)
    assert profile_cmd.run(args) == 0
    assert "identical scientific decision stream" in capsys.readouterr().out

    log_event(right / "report" / "session_log.jsonl", "propose",
              {"hkl": [9, 0, 0], "kind": "production", "ncount": 1}, ts=999.0)
    assert profile_cmd.run(args) == 1
    assert "DIFFERENT: decisions" in capsys.readouterr().out
