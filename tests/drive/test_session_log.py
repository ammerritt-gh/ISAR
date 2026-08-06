"""session_log — append-only JSONL decision log."""
import json

import pytest

from isar.drive import session_log
from isar.drive.session_log import (
    EVENT_KINDS, activity, analysis_clock_state, analysis_operation,
    close_interrupted_activities, close_interrupted_analysis, log_activity_progress,
    log_event, read_log, timed)


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


def test_activity_lifecycle_and_progress_are_additive(tmp_path):
    path = tmp_path / "log.jsonl"
    with activity(path, "full_fit", q=[2.5, 0, 0], fit_tier="full",
                  total=8, progress_unit="fits") as activity_id:
        log_activity_progress(path, activity_id, completed=3, total=8,
                              progress_unit="fits")
    events = read_log(path)
    assert [event["kind"] for event in events] == [
        "activity_start", "activity_progress", "activity_end"]
    assert events[1]["payload"]["completed"] == 3
    assert events[-1]["payload"]["status"] == "completed"


def test_resume_closes_unmatched_activity_as_interrupted(tmp_path):
    path = tmp_path / "log.jsonl"
    log_event(path, "activity_start", {
        "activity_id": "dangling", "stage": "scan", "q": [2.2, 0, 0]})
    close_interrupted_activities(path)
    end = read_log(path)[-1]
    assert end["kind"] == "activity_end"
    assert end["payload"] == {"activity_id": "dangling", "stage": "scan",
                               "q": [2.2, 0, 0], "status": "interrupted"}


def test_analysis_clock_replay_uses_max_per_operation_not_worker_sum(tmp_path):
    path = tmp_path / "log.jsonl"
    for operation_id, values in (("fit-a", (0.0, 10.0, 8.0)), ("fit-b", (0.0, 3.0, 3.0))):
        log_event(path, "analysis_clock", {"phase": "start", "operation_id": operation_id,
                                            "elapsed_seconds": values[0]})
        log_event(path, "analysis_clock", {"phase": "checkpoint", "operation_id": operation_id,
                                            "elapsed_seconds": values[1]})
        log_event(path, "analysis_clock", {"phase": "end", "operation_id": operation_id,
                                            "elapsed_seconds": values[2]})
    state = analysis_clock_state(path)
    assert state["operations"] == {"fit-a": 10.0, "fit-b": 3.0}
    assert state["spent_seconds"] == 13.0 and state["unmatched"] == []


def test_analysis_clock_resume_carries_elapsed_and_closes_latest_unmatched(tmp_path, monkeypatch):
    path = tmp_path / "log.jsonl"
    log_event(path, "analysis_clock", {"phase": "start", "operation_id": "same",
                                        "elapsed_seconds": 0.0})
    log_event(path, "analysis_clock", {"phase": "checkpoint", "operation_id": "same",
                                        "elapsed_seconds": 7.0})
    close_interrupted_analysis(path)
    assert analysis_clock_state(path)["unmatched"] == []

    ticks = iter((100.0, 105.0))
    monkeypatch.setattr(session_log.time, "monotonic", lambda: next(ticks))
    with analysis_operation(path, "same", "full_fit", limit_seconds=100.0,
                            checkpoint_seconds=60.0):
        pass
    assert analysis_clock_state(path)["operations"]["same"] == pytest.approx(12.0)

    # A later start for that same ID is unmatched even though an older end exists.
    log_event(path, "analysis_clock", {"phase": "start", "operation_id": "same",
                                        "elapsed_seconds": 12.0})
    assert analysis_clock_state(path)["unmatched"] == ["same"]
    close_interrupted_analysis(path)
    assert read_log(path)[-1]["payload"]["status"] == "interrupted"


# The seven kinds below are emitted by isar/drive/loop.py but are NOT in EVENT_KINDS, so every one of
# them is written with a "kind_unknown" marker. They pre-date PR #9 and are out of its scope; they are
# listed here so a NEW unregistered kind fails this test instead of joining them silently.
_KNOWN_UNREGISTERED_KINDS = frozenset({
    "action_suppressed", "differentiation_probe", "evidence_only_master", "exposure_saturation",
    "inventory", "scan_design", "strategy_ceiling",
})


def test_every_emitted_event_kind_is_registered():
    """An emitted kind that EVENT_KINDS does not know is logged as ``kind_unknown``.

    ``feasibility_trim`` was emitted, rendered by the GUI and asserted by four tests while missing
    from the vocabulary — an unregistered kind is silently degraded, never rejected, so nothing else
    catches it.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "isar"
    emitted: dict[str, str] = {}
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'log_event\(\s*[^,]+,\s*"([a-z_]+)"', source):
            emitted.setdefault(match.group(1),
                               f"{path.relative_to(root)}:{source[:match.start()].count(chr(10)) + 1}")

    unregistered = {kind: where for kind, where in emitted.items()
                    if kind not in EVENT_KINDS and kind not in _KNOWN_UNREGISTERED_KINDS}
    assert unregistered == {}, f"unregistered event kinds emitted: {unregistered}"
    assert "feasibility_trim" in emitted and "feasibility_trim" in EVENT_KINDS
