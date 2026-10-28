"""Which passes are current (C-current): the read side answers from what is stored, by the same predicate a
queued item applies at its start, and the two agree on every case below.

Generated BL43 workspaces (``tests/intake_fixtures.py``) at a mode ceiling of 1, so a real fast pass takes
well under a second; nested sampling is never run. Nothing reads ``real_data/``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import intake_fixtures as fx
import isar
from isar.analyze import deep_pass, errors
from isar.intake import values
from isar.session import currentness, ingest
from isar.session.deep_delivery import SCHEMA as DEEP_SCHEMA, DeepRequest
from isar.session.file_fit import FileItem, fit_file
from isar.session.writer import save_results
from isar.workspace_api.currentness import current_passes, deep_calibration_note, result_warning

FAST, BOTH, NONE = frozenset({"fast"}), frozenset({"fast", "deep"}), frozenset()


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_sampling(monkeypatch):
    """A deep stage that runs samples nothing: every stage task returns no runs, errors attach instantly."""
    monkeypatch.setattr(deep_pass, "_stage_task", lambda job: (job[0], []))
    monkeypatch.setattr(errors, "attach", lambda *a, **k: None)


def _ingested(tmp_path, **kw):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",), **kw)
    text = ws.policy_path.read_text(encoding="utf-8")
    ws.policy_path.write_text(text.replace("min_fit_points = 6", "min_fit_points = 6\nmax_modes = 1"),
                              encoding="utf-8", newline="\n")
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    (scan,) = {k.rsplit(":", 1)[0] for k in session.scans}
    return ws, scan


def _fit(ws, unit, pass_kind="fast"):
    out = []
    fit_file(ws.root, FileItem("q1", unit, pass_kind, 1), progress=None, emit=out.append)
    return [r for r in out if r.status not in ("attempt", "cores")]


def _saved(ws, unit):
    out = _fit(ws, unit)
    assert save_results(ws.root, [r.payload for r in out if r.payload is not None]) == [None]
    return out


def _agrees(ws, cur, units, pass_kind="fast"):
    """Example 7: each unit's item finishes as already current exactly when current_passes says so."""
    for unit in units:
        first = _fit(ws, unit, pass_kind)[0]
        assert (first.status == "already_current") == (pass_kind in cur[unit]), (unit, first.status, first.reason)


def _store_deep(ws, unit, **record):
    card, session = ws.load()
    entry = session.get(unit)
    entry.detectors[unit.rsplit(":", 1)[1]]["deep_pass"] = {
        "schema": DEEP_SCHEMA, "state": "search_limited", "settings_signature": DeepRequest().signature(),
        **record}
    session.put(entry)
    ws.save(card, session)


def test_an_ingested_unit_has_no_current_pass_and_a_saved_fast_pass_is_current_until_its_inputs_change(tmp_path):
    """Examples 1 and 2, with the agreement of example 7."""
    ws, scan = _ingested(tmp_path)
    units = [f"{scan}:{a}" for a in fx.ANALYZERS]
    assert current_passes(ws.root) == {u: NONE for u in units}
    _saved(ws, units[1])
    cur = current_passes(ws.root, units)
    assert cur == {units[0]: NONE, units[1]: FAST, units[2]: NONE}
    _agrees(ws, cur, units)
    assert current_passes(ws.root, ["Smp:1:a99"]) == {"Smp:1:a99": NONE}
    assert values.save_value(ws.root, "temperature", 120.0, [scan]).ok         # the inputs hash changes
    cur = current_passes(ws.root, [units[1]])
    assert cur == {units[1]: NONE}
    _agrees(ws, cur, [units[1]])


def test_a_curve_library_changed_since_the_calibration_makes_the_unit_not_current(tmp_path):
    ws, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    _saved(ws, unit)
    fx.write_library(ws.root / "rf", settings={"80x80": 0.7})                # a curve added to the folder
    cur = current_passes(ws.root, [unit])
    assert cur == {unit: NONE}
    _agrees(ws, cur, [unit])


def test_the_newer_inputs_calibration_is_kept_when_the_older_item_saves_first(tmp_path):
    """Two analyzers of one scan; the slit is corrected between their starts; the older one saves first."""
    ws, scan = _ingested(tmp_path)
    old, new = f"{scan}:a03", f"{scan}:a04"
    (first,) = _fit(ws, old)
    assert values.save_value(ws.root, "slit", "60x60", [scan]).ok
    (second,) = _fit(ws, new)
    assert first.payload["calibration"] is not None and second.payload["calibration"] is not None
    assert save_results(ws.root, [first.payload]) == [None]
    assert save_results(ws.root, [second.payload]) == [None]
    stored = ws.load()[0].state.tabulated_calibration[scan]
    assert stored["inputs_key"] == second.payload["calibration"]["state"]["inputs_key"]
    cur = current_passes(ws.root, [old, new])
    assert cur == {old: NONE, new: FAST}
    _agrees(ws, cur, [old, new])


def test_an_older_library_saved_last_stays_behind_the_newer_one(tmp_path):
    """The curves change between two items' starts; the newer item saves first."""
    ws, scan = _ingested(tmp_path)
    old, new = f"{scan}:a03", f"{scan}:a04"
    fx.write_library(ws.root / "rf", settings={"80x80": 0.7})
    (first,) = _fit(ws, old)
    fx.write_library(ws.root / "rf", settings={"10x10": 3.0})
    (second,) = _fit(ws, new)
    library = second.payload["resolution_version"]["library"]
    assert first.payload["resolution_version"]["library"] != library
    assert save_results(ws.root, [second.payload]) == [None]
    assert save_results(ws.root, [first.payload]) == [None]
    state = ws.load()[0].state
    assert state.resolution_versions[-1]["library"] == library
    assert state.tabulated_calibration[scan]["library"] == library
    cur = current_passes(ws.root, [old, new])
    assert cur == {old: NONE, new: FAST}
    _agrees(ws, cur, [old, new])


def test_a_stored_deep_pass_is_current_under_the_queues_request_and_the_isolated_rule(tmp_path, monkeypatch):
    """Examples 3 and 4, with the agreement of example 7 for the deep pass."""
    ws, scan = _ingested(tmp_path)
    unit = f"{scan}:a04"
    _saved(ws, unit)
    _store_deep(ws, unit)
    cur = current_passes(ws.root, [unit])
    assert cur == {unit: BOTH}
    _agrees(ws, cur, [unit], "deep")
    with monkeypatch.context() as m:
        m.setattr(currentness, "deep_record_counts_as_current", lambda r: r["state"] != "search_limited")
        cur = current_passes(ws.root, [unit])
        assert cur == {unit: FAST}
        _agrees(ws, cur, [unit], "deep")
    _store_deep(ws, unit, state="refused")
    assert current_passes(ws.root, [unit]) == {unit: FAST}
    _store_deep(ws, unit, settings_signature=DeepRequest(budget_s=3600.0).signature())   # an old-budget record
    cur = current_passes(ws.root, [unit])
    assert cur == {unit: FAST}
    _agrees(ws, cur, [unit], "deep")


def test_a_held_unit_is_never_current_and_an_excluded_one_is_carried(tmp_path):
    """Example 5, with the agreement of example 7."""
    ws, scan = _ingested(tmp_path / "held", library_skip=[("a05", fx.TRUE_SLIT)])
    held = f"{scan}:a05"
    assert _saved(ws, held)[0].status == "held"
    cur = current_passes(ws.root, [held])
    assert cur == {held: NONE}
    _agrees(ws, cur, [held])

    ws, scan = _ingested(tmp_path / "excluded")
    text = ws.policy_path.read_text(encoding="utf-8")
    ws.policy_path.write_text(text.replace("exclusions_seed = []", f"exclusions_seed = [{scan.rsplit(':', 1)[1]}]"),
                              encoding="utf-8", newline="\n")
    excluded = f"{scan}:a03"
    out = _saved(ws, excluded)
    assert out[0].status == "held" and out[0].payload["entry"]["status"] == "excluded"
    cur = current_passes(ws.root, [excluded])
    assert cur == {excluded: FAST}
    _agrees(ws, cur, [excluded])


def test_the_warning_words_come_from_the_stored_record_and_the_calibration_note_is_said_once(monkeypatch):
    """Example 6."""
    limited = {"state": "search_limited", "search_limited": "suspect after restarts: 3p_bg", "trusted": False,
               "close_call": {"numerical": False}}
    assert (result_warning({"deep_pass": {**limited, "chosen": {"errors_failure": None}}}, "deep")
            == "Deep pass done: comparison incomplete, 1 model unresolved after restarts")
    resolved = {"state": "resolved", "trusted": False, "close_call": {"numerical": False},
                "chosen": {"errors_failure": None}}
    assert result_warning({"deep_pass": resolved}, "deep") is None
    assert (result_warning({"deep_pass": {**resolved, "chosen": {"errors_failure": "refit did not converge"}}}, "deep")
            == "Deep pass done: error bars missing (refit did not converge)")
    assert result_warning({"deep_pass": {**resolved, "chosen": None}}, "deep") == "Deep pass done: no chosen model stored"
    assert result_warning({"deep_pass": {"state": "refused", "reason": "x"}}, "deep") is None
    assert (result_warning({"fast_pass": {"search_limit_reason": "ceiling", "settings": {"mode_ceiling": 6}}}, "fast")
            == "Fast pass done: the search reached the peak-count ceiling (6); more peaks were not tried.")
    assert result_warning({"fast_pass": {"search_limit_reason": ""}}, "fast") is None
    assert deep_calibration_note() == "Deep-pass evidence calibration is provisional: all deep results carry this"
    monkeypatch.setattr(deep_pass, "CALIBRATION_STATUS", "accepted: measured")
    assert deep_calibration_note() is None


def test_there_is_no_answer_for_a_missing_workspace(tmp_path):
    with pytest.raises(FileNotFoundError):
        current_passes(tmp_path / "nowhere")
