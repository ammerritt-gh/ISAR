"""`isar answer` — the operator surface over the consultation channel (SCENE-4.3 C9, D6).

The command is thin on purpose, so what is worth pinning is exactly the thin part: what it refuses
before writing anything, and that what it does write is the SAME file
:func:`isar.drive.consultation.write_answer` produces — one writer, one file shape, so a hand-rolled
script, the GUI, and this command cannot drift.

The channel's own validation is not re-tested here (``tests/drive/test_consultation.py`` owns it);
these tests own the keyboard-level refusals it would otherwise catch a delivery too late.
"""
from __future__ import annotations

import json
from pathlib import Path

from isar.cli.main import main
from isar.drive.consultation import ANSWERED, SETTLEMENT_SCHEMA, answer_path, settlements_path
from isar.drive.session_log import log_event
from isar.schema.consultation import (
    ACCEPT_CLOSE, ANSWER_SCHEMA, KEEP_SPENDING, PROMPT_SCHEMA, UNRESOLVABLE_CLOSURE)


def issue(workspace: Path, prompt_id: str, *, delivery: int = 0, subject: str = "q-1",
          timeout: int = 10) -> dict:
    """One ``prompt_issued`` event, exactly as the channel writes it."""
    record = {"schema": PROMPT_SCHEMA, "event": "prompt_issued", "action": "prompt_issued",
              "prompt_id": prompt_id, "base_id": "b-" + prompt_id, "epoch": 0,
              "kind": UNRESOLVABLE_CLOSURE, "step_id": "discover-1", "subject": subject,
              "options": [ACCEPT_CLOSE, KEEP_SPENDING], "default": ACCEPT_CLOSE,
              "timeout_deliveries": timeout, "hard": False, "issued_delivery": delivery,
              "iter": delivery}
    log_event(workspace / "report" / "session_log.jsonl", "decide", record)
    return record


def settle(workspace: Path, prompt_id: str) -> None:
    path = settlements_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"schema": SETTLEMENT_SCHEMA, "base_id": "b-" + prompt_id,
                                 "prompt_id": prompt_id, "epoch": 0, "resolution": ACCEPT_CLOSE,
                                 "source": ANSWERED, "delivery": 3}) + "\n")


def test_an_answer_is_written_through_the_one_writer(tmp_path, capsys):
    """The whole point of the command: the file is byte-shaped by ``write_answer``, not by here."""
    issue(tmp_path, "abc123")
    code = main(["answer", "abc123", KEEP_SPENDING, "--workspace", str(tmp_path),
                 "--note", "operator says so"])
    assert code == 0
    written = json.loads(answer_path(tmp_path, "abc123").read_text(encoding="utf-8"))
    assert written == {"schema": ANSWER_SCHEMA, "prompt_id": "abc123", "option": KEEP_SPENDING,
                       "by": "operator", "note": "operator says so"}
    assert "prompt-abc123.answer.json" in capsys.readouterr().out


def test_an_option_the_prompt_does_not_offer_is_refused_before_anything_is_written(tmp_path):
    """The refusal that earns this command its existence. The channel would record the typo as
    ``prompt_answer_invalid`` a delivery later, in a log the operator may never read; here it is a
    non-zero exit at the keyboard and NO file in the control directory to confuse a later reader."""
    issue(tmp_path, "abc123")
    assert main(["answer", "abc123", "spend_more_maybe", "--workspace", str(tmp_path)]) != 0
    assert not answer_path(tmp_path, "abc123").exists()


def test_an_id_that_is_not_open_is_refused_and_names_the_way_to_look(tmp_path):
    """A settled prompt is gone for good — its subject re-closing mints a NEW id under epoch+1
    (D6) — so answering the old id must fail loudly rather than leave an orphan file behind."""
    issue(tmp_path, "abc123")
    settle(tmp_path, "abc123")
    assert main(["answer", "abc123", KEEP_SPENDING, "--workspace", str(tmp_path)]) != 0
    assert main(["answer", "deadbeef", KEEP_SPENDING, "--workspace", str(tmp_path)]) != 0
    assert not answer_path(tmp_path, "abc123").exists()
    assert not answer_path(tmp_path, "deadbeef").exists()


def test_list_shows_open_prompts_and_hides_settled_ones(tmp_path, capsys):
    """Discovery without a running campaign: the durable records the campaign already wrote are
    the whole source, and a settlement record is what removes a prompt from the list."""
    issue(tmp_path, "open01", delivery=1, subject="q-open", timeout=10)
    issue(tmp_path, "gone01", delivery=0, subject="q-gone")
    settle(tmp_path, "gone01")
    # Some later delivery has happened, so the remaining-deliveries count is derivable.
    log_event(tmp_path / "report" / "session_log.jsonl", "timing",
              {"stage": "scene_build", "iter": 4, "seconds": 0.1, "ok": True})

    assert main(["answer", "--list", "--workspace", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "open01" in out and "gone01" not in out
    assert "q-open" in out
    assert f"{ACCEPT_CLOSE}, {KEEP_SPENDING}" in out
    assert "remaining before the default applies: 7" in out      # 10 - (4 - 1)


def test_list_on_a_quiet_workspace_says_so_rather_than_failing(tmp_path, capsys):
    (tmp_path / "report").mkdir()
    assert main(["answer", "--list", "--workspace", str(tmp_path)]) == 0
    assert "no open prompts" in capsys.readouterr().out


def test_a_missing_workspace_and_a_missing_argument_are_both_usage_errors(tmp_path):
    assert main(["answer", "--list", "--workspace", str(tmp_path / "nope")]) == 2
    (tmp_path / "report").mkdir()
    assert main(["answer", "abc123", "--workspace", str(tmp_path)]) == 2
