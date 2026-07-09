"""session_log — append-only JSONL decision log."""
import json

import pytest

from isar.drive import session_log
from isar.drive.session_log import EVENT_KINDS, log_event, read_log, timed


def test_append_and_read(tmp_path):
    p = tmp_path / "log.jsonl"
    log_event(p, "propose", {"q": [2.0, 0.0, 0.0], "window": [4.0, 6.0]})
    log_event(p, "run", {"job_id": "j-0001", "state": "done"})
    log_event(p, "stop", {"decision": "met", "reasons": ["sigma <= tol"]})
    events = read_log(p)
    assert [e["kind"] for e in events] == ["propose", "run", "stop"]
    assert events[0]["payload"]["q"] == [2.0, 0.0, 0.0]
    assert all("ts" in e for e in events)


def test_timestamp_is_stamped_and_monotonic(tmp_path):
    p = tmp_path / "log.jsonl"
    log_event(p, "propose", {}, ts=100.0)
    log_event(p, "analyze", {}, ts=200.0)
    events = read_log(p)
    assert events[0]["ts"] == 100.0
    assert events[1]["ts"] == 200.0


def test_all_documented_kinds_accepted(tmp_path):
    p = tmp_path / "log.jsonl"
    for k in EVENT_KINDS:
        rec = log_event(p, k, {"k": k})
        assert "kind_unknown" not in rec
    assert len(read_log(p)) == len(EVENT_KINDS)


def test_unknown_kind_is_marked_not_dropped(tmp_path):
    p = tmp_path / "log.jsonl"
    rec = log_event(p, "banana", {})
    assert rec["kind_unknown"] is True
    assert len(read_log(p)) == 1


def test_each_event_is_its_own_line(tmp_path):
    p = tmp_path / "log.jsonl"
    log_event(p, "propose", {})
    log_event(p, "decide", {})
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # each line independently valid JSON


def test_read_missing_log_is_empty(tmp_path):
    assert read_log(tmp_path / "nope.jsonl") == []


def test_read_tolerates_truncated_final_line(tmp_path):
    p = tmp_path / "log.jsonl"
    log_event(p, "propose", {})
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"ts": 1.0, "kind": "run", "payl')  # interrupted write
    events = read_log(p)
    assert len(events) == 1
    assert events[0]["kind"] == "propose"


def test_creates_parent_dir(tmp_path):
    p = tmp_path / "sub" / "dir" / "log.jsonl"
    log_event(p, "propose", {})
    assert p.exists()


# --------------------------------------------------------------------------- timed() span
def test_timed_writes_timing_record(tmp_path):
    p = tmp_path / "log.jsonl"
    with timed(p, "isar_run", iteration=7):
        pass
    events = read_log(p)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "timing"
    assert "kind_unknown" not in ev  # "timing" is a documented kind
    pay = ev["payload"]
    assert pay["stage"] == "isar_run"
    assert pay["iter"] == 7
    assert pay["ok"] is True
    assert isinstance(pay["seconds"], float) and pay["seconds"] >= 0.0


def test_timed_records_failure_and_reraises(tmp_path):
    p = tmp_path / "log.jsonl"
    with pytest.raises(ValueError):
        with timed(p, "model_fit"):
            raise ValueError("boom")
    events = read_log(p)
    assert len(events) == 1
    pay = events[0]["payload"]
    assert pay["stage"] == "model_fit"
    assert pay["iter"] is None
    assert pay["ok"] is False


def test_timed_meta_mutation_is_logged(tmp_path):
    p = tmp_path / "log.jsonl"
    with timed(p, "fold_rows", iteration=3, attempt=0) as meta:
        meta["n_rows"] = 12
    pay = read_log(p)[0]["payload"]
    assert pay["attempt"] == 0
    assert pay["n_rows"] == 12  # mutation inside the with-block rides into the payload
