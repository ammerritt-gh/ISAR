"""Policy A (owner ruling 2026-09-05): the campaign never stops on an analysis failure; it yells.

Live on 2026-09-05 a Windows file-lock race on ``results.csv`` made one ``isar run`` exit 1 at
iteration 174, and the loop, with no handler, ended the campaign as "campaign failed". The scan was
on disk and in the ledger; the next analysis pass would have picked it up. These tests pin the
survival and the shout.
"""
from __future__ import annotations

from isar.drive.isar_io.runner import IsarRunError
from isar.drive.loop import _consecutive_alerts, _raise_campaign_alert
from isar.drive.session_log import EVENT_KINDS, log_event, read_log

from .test_loop import MockTavi, _objective, _run, make_mock_runner


def test_one_failed_analysis_raises_an_alert_and_the_campaign_continues(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    inner = make_mock_runner(omega)
    analyses = {"n": 0}

    def runner(workspace, data_paths, **kw):
        # A bare callable runner only ever receives analysis calls (ingest needs ``.ingest``,
        # which a callable double lacks), so every call here is one ``isar run``.
        analyses["n"] += 1
        if analyses["n"] == 2:                     # the second scan's fit dies, as on 2026-09-05
            raise IsarRunError(1, "", "PermissionError: [WinError 5] Access is denied: "
                               "results.csv.tmp -> results.csv",
                               ["python", "-m", "isar.session.worker", "run"])
        return inner(workspace, data_paths, **kw)

    summary, _ws = _run(tmp_path, _objective(), MockTavi(omega), runner)   # must not raise
    events = read_log(summary["session_log"])
    alerts = [e for e in events if e["kind"] == "campaign_alert"]
    assert len(alerts) == 1
    alert = alerts[0]["payload"]
    assert alert["alert"] == "analysis_failed" and alert["policy"] == "continue"
    assert alert["consecutive"] == 1
    assert "Access is denied" in alert["error"] and "Traceback" in alert["traceback"]
    # The campaign went on: analyses succeeded AFTER the alert and the run reached a stop state
    # of its own, never "failed".
    alert_index = events.index(alerts[0])
    assert any(e["kind"] == "analyze" for e in events[alert_index + 1:])
    assert summary["stop"]["state"] in ("met", "incomplete", "converged")
    assert analyses["n"] >= 3


def test_consecutive_count_tracks_the_trailing_run_since_the_last_good_analysis(tmp_path):
    log_path = tmp_path / "session_log.jsonl"
    assert "campaign_alert" in EVENT_KINDS
    first = _raise_campaign_alert(log_path, "analysis_failed", RuntimeError("one"), it=1)
    second = _raise_campaign_alert(log_path, "analysis_failed", RuntimeError("two"), it=2)
    assert (first["payload"]["consecutive"], second["payload"]["consecutive"]) == (1, 2)
    log_event(log_path, "analyze", {"iter": 3, "n_rows": 1})          # a good analysis resets
    assert _consecutive_alerts(log_path, "analysis_failed") == 0
    third = _raise_campaign_alert(log_path, "analysis_failed", RuntimeError("three"), it=4)
    assert third["payload"]["consecutive"] == 1
    assert not third.get("kind_unknown")                              # registered vocabulary


def test_planner_exposure_base_is_the_objectives_production_count():
    """Owner, 2026-09-05: 2-4 counts per point per scan is not an experiment. The ladder's base is
    the declared production count, never the quicklook constant; absent or junk -> the production
    default."""
    from types import SimpleNamespace
    from isar.drive.loop import _production_ncount
    from isar.drive.supervisor.stopping import PRODUCTION_NCOUNT, QUICKLOOK_NCOUNT
    assert _production_ncount(SimpleNamespace(steering={"production_batch_ncount": 300_000_000})) == 300_000_000
    assert _production_ncount(SimpleNamespace(steering={})) == PRODUCTION_NCOUNT
    assert _production_ncount(SimpleNamespace(steering={"production_batch_ncount": "junk"})) == PRODUCTION_NCOUNT
    assert _production_ncount(SimpleNamespace(steering=None)) > QUICKLOOK_NCOUNT
