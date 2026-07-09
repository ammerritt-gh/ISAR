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
                  {"job_id": f"j-{it}", "errors_tier": "full", "n_rows": 3,
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
    assert "iteration" in out
    # embedded subprocess sub-stages
    assert "ingest_parse" in out
    assert "report_render" in out
    assert "subprocess overhead" in out
    # campaign wall reported
    assert "campaign wall" in out
    # a percentage-of-wall column is rendered
    assert "%wall" in out


def test_top_limits_rows(tmp_path, capsys):
    log = tmp_path / "report" / "session_log.jsonl"
    _write_campaign(log)
    profile_cmd.run(_args(tmp_path, top=1))
    out = capsys.readouterr().out
    # "iteration" (84s, wraps the whole body) is the single largest stage; smaller stages
    # like model_fit must be dropped from the top-1 stage table. (They can still appear as
    # sub-stages below, so assert on the stage-table section only.)
    stage_section = out.split("isar_run: subprocess")[0]
    assert "iteration" in stage_section
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
    assert "full" in out
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
