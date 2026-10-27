"""The fit-inputs log and the fit history (C-fit-inputs, C-fit-history): formats, writers and readers."""
import json
from types import SimpleNamespace

import pytest

from intake_fixtures import log_lines
from isar.intake import fit_inputs
from isar.intake.fit_inputs import (FIT_HISTORY, FIT_INPUTS, begin_run, end_run, latest_passes, read_history,
                                    read_log, run_states, superseded_line, write_finished, write_started,
                                    write_superseded)
from isar.session.ingest import upsert_ingested_entry
from isar.session.state import ScanEntry, SessionState, _scan_entry_from_dict

U = "spec1:42:a03"


def test_writers_write_the_line_formats(tmp_path):
    run = begin_run(tmp_path, passes=["fast"])
    write_started(tmp_path, run, [{"unit": U, "scan": "spec1:42", "pass": "fast", "inputs": {"data": "aa"},
                                   "hash": "7c", "fallbacks": ("slit_inferred",)}])
    write_finished(tmp_path, run, [{"unit": U, "scan": "spec1:42", "pass": "fast", "status": "fitted",
                                    "fallbacks": ["slit_inferred"], "result": {"verdict": "resolved"}}])
    end_run(tmp_path, run, "complete")
    start, started, finished, end = read_log(tmp_path)
    assert run.startswith("r") and len(run) == 13
    assert (start["kind"], start["event"], start["passes"], start["run"]) == ("run", "start", ["fast"], run)
    assert isinstance(start["pid"], int) and start["at"].endswith("+00:00")
    assert (started["kind"], started["fallbacks"], started["inputs"]) == ("started", ["slit_inferred"], {"data": "aa"})
    assert (finished["kind"], finished["status"]) == ("finished", "fitted")
    assert (end["event"], end["outcome"]) == ("end", "complete")
    (rec,) = latest_passes(read_log(tmp_path))[U].values()
    assert (rec.state, rec.status, rec.fallbacks, rec.result) == ("finished", "fitted", ("slit_inferred",),
                                                                   {"verdict": "resolved"})


def test_a_bad_line_writes_nothing(tmp_path):
    run = begin_run(tmp_path, passes=["fast", "deep"])
    before = (tmp_path / FIT_INPUTS).read_bytes()
    with pytest.raises(ValueError):
        write_started(tmp_path, run, [{"unit": U, "pass": "fast", "inputs": {"t": float("nan")}}])
    with pytest.raises(ValueError):
        write_started(tmp_path, run, [{"unit": U, "pass": "slow"}])
    with pytest.raises(ValueError):
        end_run(tmp_path, run, "done")
    with pytest.raises(ValueError):
        begin_run(tmp_path, passes=[])
    assert (tmp_path / FIT_INPUTS).read_bytes() == before


def test_a_torn_tail_is_ended_and_skipped(tmp_path):
    path = tmp_path / FIT_INPUTS
    path.parent.mkdir()
    path.write_bytes(b'{"kind": "run", "event": "sta')
    run = begin_run(tmp_path, passes=["fast"])
    assert [ln["run"] for ln in read_log(tmp_path)] == [run]


@pytest.mark.parametrize("events, expect", [
    ([("start", "r1", ["fast"]), ("started", "r1", U, "fast"), ("finished", "r1", U, "fast"), ("end", "r1", "complete")],
     {"fast": ("r1", "finished")}),
    ([("start", "r1", ["fast"]), ("started", "r1", U, "fast"), ("finished", "r1", U, "fast"), ("end", "r1", "complete"),
      ("start", "r2", ["fast"]), ("started", "r2", U, "fast")],
     {"fast": ("r2", "running")}),
    ([("start", "r1", ["fast"]), ("started", "r1", U, "fast"), ("finished", "r1", U, "fast"), ("end", "r1", "complete"),
      ("start", "r2", ["fast"]), ("started", "r2", U, "fast"), ("start", "r3", ["fast"])],
     {"fast": ("r1", "finished")}),
    ([("start", "r1", ["fast", "deep"]), ("started", "r1", U, "fast"), ("started", "r1", U, "deep"),
      ("finished", "r1", U, "fast"), ("finished", "r1", U, "deep"), ("end", "r1", "complete")],
     {"fast": ("r1", "finished"), "deep": ("r1", "finished")}),
    ([("start", "r1", ["fast"]), ("started", "r1", U, "fast"), ("finished", "r1", U, "fast"), ("end", "r1", "complete"),
      ("start", "r4", ["fast"]), ("started", "r4", U, "fast"), ("finished", "r4", U, "fast"), ("end", "r4", "failed")],
     {"fast": ("r1", "finished")}),
    ([("start", "r1", ["fast"]), ("started", "r1", U, "fast"), ("end", "r1", "complete")], None),
    ([], None),
])
def test_latest_passes_count_only_running_or_completed_runs(events, expect):
    got = latest_passes(log_lines(*events)).get(U)
    assert (None if got is None else {p: (r.run, r.state) for p, r in got.items()}) == expect


def test_run_states(tmp_path):
    lines = log_lines(("start", "r1", ["fast"]), ("end", "r1", "complete"), ("start", "r2", ["deep"]),
                      ("start", "r3", ["fast"]))
    s = run_states(lines)
    assert (s["r1"].outcome, s["r1"].running) == ("complete", False)
    assert (s["r2"].outcome, s["r2"].running) == (None, False)          # cut by a later run start
    assert (s["r3"].running, s["r3"].passes) == (True, ("fast",))


def _fitted(scan_id=U):
    return ScanEntry(scan_id=scan_id, scan_number=42, scan_type="measurement", status="fitted",
                     content_hash="aa", guidelines_hash="g", detectors={"a03": {"omega0": 4.0, "chi2": float("nan")}},
                     reasons=[], provenance={"fit_inputs": {"run": "r1", "hash": "7c", "fallbacks": []}})


def test_superseded_line_round_trips_and_ties_the_entry_to_its_inputs():
    entry = _fitted()
    ln = superseded_line(entry, by="refit", run="r2")
    assert (ln["unit"], ln["scan"], ln["by"], ln["run"]) == (U, "spec1:42", "refit", "r2")
    assert ln["fit_inputs"] == {"run": "r1", "hash": "7c", "fallbacks": []}
    back = _scan_entry_from_dict(ln["entry"])
    assert back.scan_id == entry.scan_id and back.detectors["a03"]["omega0"] == 4.0 and back.status == "fitted"
    assert superseded_line(ScanEntry(U, 42, "measurement", "fitted"), by="reimport", run=None)["fit_inputs"] is None
    with pytest.raises(ValueError):
        superseded_line(entry, by="cleanup", run=None)


def test_history_is_appended_and_a_repeat_collapses(tmp_path):
    a, b = superseded_line(_fitted(), by="reimport", run=None), superseded_line(_fitted("spec1:42:a04"), by="refit", run="r2")
    write_superseded(tmp_path, [a])
    write_superseded(tmp_path, [a, b])                        # a crash before the ledger save repeats a line
    raw = (tmp_path / FIT_HISTORY).read_text(encoding="utf-8").splitlines()
    assert len(raw) == 3 and all(json.loads(x)["kind"] == "superseded" for x in raw)   # NaN kept as the ledger has it
    hist = read_history(tmp_path)
    assert [h["unit"] for h in hist] == [U, "spec1:42:a04"]
    write_superseded(tmp_path, [])
    assert len((tmp_path / FIT_HISTORY).read_text(encoding="utf-8").splitlines()) == 3
    assert fit_inputs.read_history(tmp_path / "nowhere") == []


@pytest.mark.parametrize("status, superseded", [("excluded", False), ("fitted", True)])
def test_reimport_records_a_superseded_fit_but_not_an_excluded_entry(status, superseded):
    entry = _fitted()
    entry.status = status
    session = SessionState()
    session.put(entry)
    seen = []
    rec = SimpleNamespace(experiment_id="spec1", scan_number=42, scan_type="measurement")
    assert upsert_ingested_entry(session, rec, "a03", chash="new", prov={}, on_supersede=seen.append) == "updated"
    assert [e.scan_id for e in seen] == ([U] if superseded else [])
