"""``isar.workspace_api.decision_view.latest_decision`` — the operator-alert read door (SCENE-5
5.2, D7): absence graceful, corruption loud, tail-read only.

``decisions.jsonl`` is written line-by-line by the real planner (``isar/drive/steering/planner.py``
``_append_decision``); these tests write raw JSONL directly, since this reader has no dependency on
how a line was produced — only that it is one JSON object per line.
"""
from __future__ import annotations

import json

from isar.workspace_api import Workspace, latest_decision


def _decisions_path(root):
    path = root / "report"
    path.mkdir(parents=True, exist_ok=True)
    return path / "decisions.jsonl"


def _write_lines(root, records):
    path = _decisions_path(root)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def test_absent_file_reads_as_none(make_ws):
    root = make_ws()
    ws = Workspace.open(root)
    assert latest_decision(ws) is None


def test_empty_file_reads_as_none(make_ws):
    root = make_ws()
    _decisions_path(root).touch()
    ws = Workspace.open(root)
    assert latest_decision(ws) is None


def test_reads_the_last_line_of_a_multiline_file(make_ws):
    root = make_ws()
    _write_lines(root, [
        {"schema": "isar.discovery-decision/1", "selection": {"n": 1}},
        {"schema": "isar.discovery-decision/1", "selection": {"n": 2}},
        {"schema": "isar.discovery-decision/1", "selection": {"n": 3},
         "decision_basis": {"fallback": {"reason": "scoring_exception", "pool": "canonical"}}},
    ])
    ws = Workspace.open(root)
    decision = latest_decision(ws)
    assert decision["selection"]["n"] == 3
    assert decision["decision_basis"]["fallback"]["reason"] == "scoring_exception"


def test_corrupt_last_line_raises(make_ws):
    root = make_ws()
    path = _decisions_path(root)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"schema": "isar.discovery-decision/1", "selection": {"n": 1}}) + "\n")
        f.write("{not valid json\n")
    ws = Workspace.open(root)
    try:
        latest_decision(ws)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on an unparseable last line")


def test_tail_read_on_a_large_file_returns_the_last_record(make_ws):
    root = make_ws()
    # Pad with many lines well past the reader's chunk size so it must actually seek backward
    # rather than reading the whole file.
    records = [{"schema": "isar.discovery-decision/1", "selection": {"n": i},
               "padding": "x" * 500} for i in range(200)]
    records.append({"schema": "isar.discovery-decision/1", "selection": {"n": "last"}})
    _write_lines(root, records)
    ws = Workspace.open(root)
    decision = latest_decision(ws)
    assert decision["selection"]["n"] == "last"


def test_valid_json_that_is_not_an_object_raises(make_ws):
    """PR #36 review (Codex P2-2): every decision record is a JSON object, so a bare array,
    string, or number on the ledger's last line is corruption wearing valid JSON. Returning it
    would violate the ``dict | None`` contract and explode a consumer far from the cause (the
    viewmodel's first ``.get()``); the reader refuses loudly instead, and the GUI's existing
    ``ValueError`` handling then degrades the poll gracefully."""
    import pytest

    root = make_ws()
    path = _decisions_path(root)
    for bad in ("[1]", '"a string"', "3.5", "null", "true"):
        path.write_text(json.dumps({"schema": "x"}) + "\n" + bad + "\n", encoding="utf-8")
        ws = Workspace.open(root)
        with pytest.raises(ValueError, match="not an object|not a decision record"):
            latest_decision(ws)
