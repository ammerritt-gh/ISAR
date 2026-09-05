"""A campaign's per-iteration cost must not grow with its own log.

The session log is append-only and is read at least once per scan from nine call sites, so
re-parsing it whole made wall time quadratic in scan count: measured, the six-point short path did
10 scans in 98 s while the full path timed out at 45 min after 113 scans, its log 115 MB and 87%
parameter draws. These pin both halves of the fix — read only what was appended, and do not write
the draws at all.
"""
from __future__ import annotations

import json

import pytest

from isar.drive import session_log
from isar.drive.session_log import forget_parsed_logs, log_event, read_log


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The parse cache is process-global; these tests own it."""
    forget_parsed_logs()
    yield
    forget_parsed_logs()


def _parsed_lines(monkeypatch, call):
    """Run ``call`` and report how many JSON lines the log reader actually parsed."""
    seen = {"n": 0}
    real = session_log.json.loads

    def counting(text, *args, **kwargs):
        seen["n"] += 1
        return real(text, *args, **kwargs)

    monkeypatch.setattr(session_log.json, "loads", counting)
    try:
        call()
    finally:
        monkeypatch.setattr(session_log.json, "loads", real)
    return seen["n"]


def test_reading_a_growing_log_parses_only_what_was_appended(tmp_path, monkeypatch):
    """The defect, directly: the cost of a read must track the APPEND, not the history."""
    path = tmp_path / "session_log.jsonl"
    for index in range(200):
        log_event(path, "decide", {"index": index})

    first = _parsed_lines(monkeypatch, lambda: read_log(path))
    assert first == 200, "the first read parses the whole log, once"

    later = _parsed_lines(monkeypatch, lambda: read_log(path))
    assert later == 0, "a read that appended nothing parses nothing"

    log_event(path, "decide", {"index": 200})
    incremental = _parsed_lines(monkeypatch, lambda: read_log(path))
    assert incremental == 1, "one appended event costs one parse, whatever the history"


def test_the_per_read_cost_is_flat_in_scan_count(tmp_path, monkeypatch):
    """The same, stated as the campaign shape: a hundred scans in and three thousand scans in, one
    more scan costs the same to read back."""
    def cost_after(events: int) -> int:
        forget_parsed_logs()
        path = tmp_path / f"log-{events}.jsonl"
        for index in range(events):
            log_event(path, "pooled_master_update", {"index": index})
        read_log(path)                       # the campaign has been reading all along
        log_event(path, "pooled_master_update", {"index": events})
        return _parsed_lines(monkeypatch, lambda: read_log(path))

    assert cost_after(100) == 1
    assert cost_after(3000) == 1


def test_a_partial_trailing_write_is_kept_not_dropped(tmp_path):
    """A killed run leaves half a line. The whole-file parse discarded it on every read; the
    incremental one leaves it unconsumed, so the record it becomes is read once it is complete."""
    path = tmp_path / "session_log.jsonl"
    log_event(path, "decide", {"index": 0})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "decide", "payl')
    assert len(read_log(path)) == 1, "the half-written line is not an event yet"

    with path.open("a", encoding="utf-8") as handle:
        handle.write('oad": {"index": 1}}\n')
    assert [event["payload"]["index"] for event in read_log(path)] == [0, 1]


def test_a_replaced_log_is_not_served_from_the_old_parse(tmp_path):
    """A fresh workspace at the same path must not inherit the previous campaign's events."""
    path = tmp_path / "session_log.jsonl"
    for index in range(5):
        log_event(path, "decide", {"index": index})
    assert len(read_log(path)) == 5

    path.write_text("", encoding="utf-8")
    assert read_log(path) == []
    log_event(path, "decide", {"index": 99})
    assert [event["payload"]["index"] for event in read_log(path)] == [99]


def test_the_predictive_artifact_is_not_carried_on_the_master_row(tmp_path):
    """It is hundreds of kilobytes of parameter draws, already content-addressed on disk and named
    by ``predictive_ref`` beside the row, and nothing reads it. Carried, it made every
    ``pooled_master_update`` event 413 KB and the log 87% draws."""
    from isar.drive.isar_io.readers import read_master_rows

    workspace = tmp_path / "ws"
    (workspace / "report").mkdir(parents=True)
    artifact = workspace / "data" / "master_predictive"
    artifact.mkdir(parents=True)
    (artifact / "p.json").write_text(json.dumps({
        "schema": "isar.master_predictive/1", "draws": [[1.0, 2.0]] * 500,
        "diagnostics": {"n_draws": 500}}), encoding="utf-8")
    (workspace / "report" / "results.csv").write_text(
        "scan,detector,h,k,l,pooled,omega0,predictive_ref\n"
        "1,det,2.1,0.0,0.0,True,1.0,data/master_predictive/p.json\n", encoding="utf-8")

    rows = read_master_rows(
        workspace, _Calibration(),
        row_hkl=lambda row: (row.get("h"), row.get("k"), row.get("l")),
        master_obs_from_row=lambda row, _cal: dict(row))

    assert rows, "the row itself is still read"
    assert "predictive" not in rows[0], "the draws are not carried into the row"


class _Calibration:
    """The two members ``read_master_rows`` touches on the calibration it is handed."""

    buckets: dict = {}

    def calibrated_halfwidth(self, lo, hi, *, bucket=None):
        return (float(hi) - float(lo)) / 2.0
