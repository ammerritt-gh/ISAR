"""`isar answer` — the operator's side of the consultation channel (SCENE-4.3 C9, D6).

C8 built the channel and left the operator holding a fingerprint: prompts go out on the campaign
feed and answers come back as `report/control/prompt-<id>.answer.json`, but the only writers were
the GUI-to-be and the test suite. This command is that missing surface, and it is deliberately thin
— it discovers what is open, refuses an option that is not offered, and then calls
:func:`isar.drive.consultation.write_answer`, which stays THE one writer of the file shape.

Two modes:

``isar answer --list --workspace DIR``
    What is open right now, read from the durable records the campaign already writes: the
    ``prompt_issued`` events on the session log, minus every prompt id named in
    ``report/control/consultation-settlements.jsonl``. Nothing is recomputed and no campaign state
    is touched — a settled prompt disappears from the list because a settlement record says it
    settled, not because this command decided so.

``isar answer <prompt_id> <option> --workspace DIR [--note TEXT]``
    Write the answer. An id nothing has issued, or an option the prompt does not offer, is refused
    with exit 2 and NOTHING is written: a typo that lands in the control directory costs the
    operator a delivery of waiting and shows up only as a ``prompt_answer_invalid`` event they may
    never read. The channel's own validation is still the authority — it re-checks everything on
    the next poll, because a file can also arrive from a hand-rolled script — this is the cheap
    check that stops the common mistake at the keyboard.

The campaign is never blocked on this command, and running it never starts or stops anything: the
beam does not idle on a human (D6), so an answer that arrives late simply takes effect on the next
delivery, and one that arrives after the declared default was applied is recorded stale.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _settled_ids(workspace: Path) -> set[str]:
    from isar.drive.consultation import settlements_path

    path = settlements_path(workspace)
    if not path.is_file():
        return set()
    settled: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue        # a torn final line from a killed run — the rest of the log still counts
        if isinstance(record, dict) and isinstance(record.get("prompt_id"), str):
            settled.add(record["prompt_id"])
    return settled


def open_prompts(workspace: Path) -> tuple[list[dict], int | None]:
    """``(open prompt records, the last delivery index seen)``, from the persisted feed.

    The delivery index is what makes "deliveries remaining" derivable at all: prompt timeouts are
    delivery-counted, never wall-clock (D6), so "you have three deliveries left to answer" is a
    real statement and "you have ten minutes" would not be.
    """
    from isar.drive.session_log import read_log

    issued: dict[str, dict] = {}
    last_delivery: int | None = None
    # `iter` is counted only off DISCOVERY-path events (prompt lifecycle + the per-delivery
    # scene_build stage, which only the discovery runner logs): the targeted loop writes its own
    # iteration index under the same key at ~40 sites, so a workspace that ran both would
    # inflate the count and understate deliveries-remaining (C9 review finding 3).
    _PROMPT_EVENTS = {"prompt_issued", "prompt_answered", "prompt_default_applied",
                      "prompt_answer_invalid", "prompt_answer_unmatched", "stale_answer"}
    for event in read_log(workspace / "report" / "session_log.jsonl"):
        payload = event.get("payload") or {}
        if payload.get("event") in _PROMPT_EVENTS or payload.get("stage") == "scene_build":
            delivery = payload.get("iter")
            if isinstance(delivery, int):
                last_delivery = delivery if last_delivery is None else max(last_delivery, delivery)
        if payload.get("event") == "prompt_issued" and isinstance(payload.get("prompt_id"), str):
            issued[payload["prompt_id"]] = payload
    settled = _settled_ids(workspace)
    return [record for prompt_id, record in issued.items() if prompt_id not in settled], \
        last_delivery


def _remaining(prompt: dict, last_delivery: int | None) -> int | None:
    """Deliveries left before the declared default is taken, when both ends are known."""
    issued_delivery = prompt.get("issued_delivery")
    timeout = prompt.get("timeout_deliveries")
    if last_delivery is None or not isinstance(issued_delivery, int) or not isinstance(timeout, int):
        return None
    return max(0, timeout - (last_delivery - issued_delivery))


def _print_list(prompts: list[dict], last_delivery: int | None) -> None:
    # A prompt from a campaign that ended without settling stays listed: the answer file is
    # durable, and the next run of the same step honors it (epoch identity). The listing is
    # honest either way; it cannot tell "open now" from "open when the campaign resumes".
    if not prompts:
        print("no open prompts")
        return
    for prompt in sorted(prompts, key=lambda p: (p.get("issued_delivery") or 0, p["prompt_id"])):
        remaining = _remaining(prompt, last_delivery)
        print(f"{prompt['prompt_id']}  {prompt.get('kind')}")
        print(f"    subject:   {prompt.get('subject')}")
        print(f"    options:   {', '.join(prompt.get('options') or ())}"
              f"   (default {prompt.get('default')})")
        if remaining is not None:
            print(f"    deliveries remaining before the default applies: {remaining}")


def run(args) -> int:
    workspace = Path(args.workspace)
    if not workspace.is_dir():
        print(f"isar answer: no workspace at {workspace}", file=sys.stderr)
        return 2

    prompts, last_delivery = open_prompts(workspace)
    if args.list:
        if args.prompt_id or args.option:
            print("isar answer: --list takes no PROMPT_ID/OPTION", file=sys.stderr)
            return 2
        _print_list(prompts, last_delivery)
        return 0

    if not args.prompt_id or not args.option:
        print("isar answer: give PROMPT_ID and OPTION, or --list to see what is open",
              file=sys.stderr)
        return 2

    prompt = next((p for p in prompts if p["prompt_id"] == args.prompt_id), None)
    if prompt is None:
        print(f"isar answer: no OPEN prompt {args.prompt_id!r} in {workspace} — "
              "run `isar answer --list --workspace DIR` (a settled prompt is gone for good; a "
              "question that closes again is re-asked under a new id)", file=sys.stderr)
        return 2
    options = list(prompt.get("options") or ())
    if args.option not in options:
        print(f"isar answer: {args.option!r} is not an option for prompt {args.prompt_id}; "
              f"it offers {', '.join(options)}", file=sys.stderr)
        return 2

    from isar.drive.consultation import write_answer

    path = write_answer(workspace, args.prompt_id, args.option, note=args.note)
    print(f"isar answer: wrote {args.option} to {path} — it takes effect on the next delivery")
    return 0
