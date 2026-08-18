"""The consultation channel v1 (SCENE-4.3 C8, D6) — prompts out on the feed, answers in on files.

The channel is NON-BLOCKING by construction: nothing here waits, nothing here sleeps, and the beam
never idles on a human (binding ruling). A prompt is issued, the step keeps measuring, and the
answer — if one ever comes — is picked up on a later delivery. Silence takes the DECLARED DEFAULT
after a delivery-counted timeout, and the campaign carries on.

Two surfaces, both already established:

* **out** — ``prompt_issued`` / ``prompt_answered`` / ``prompt_default_applied`` /
  ``prompt_answer_invalid`` / ``stale_answer`` on the session log's ``decide`` stream, the same
  event vocabulary ``broken_scan`` and ``durable_blocker_emitted`` already use.
* **in** — ``<workspace>/report/control/prompt-<prompt_id>.answer.json``, beside the cooperative
  stop token and under the same atomicity discipline: a writer builds the file under a temporary
  name and ``os.replace``s it in, so a reader never sees half an answer. Unlike the stop token,
  existence is NOT the whole protocol — the body carries the chosen option, so the file is read.
  It is never modified and never deleted (nothing in ISAR is).

Failure is always recorded and never fatal. An unreadable answer, an answer for the wrong prompt,
or an answer naming something that is not an option is recorded once as ``prompt_answer_invalid``
and the prompt STAYS OPEN — a typo must not crash a campaign, and must not silently count as an
answer either. An answer that arrives after the default was applied is recorded as ``stale_answer``
and NOT applied; re-applying it is explicitly out of v1 scope (a human re-issues the intent). An
answer file naming an id nothing here recognises — a fat-fingered id, or an answer to a settled
occurrence of a question that has since re-opened under a new epoch — is recorded once per distinct
file content as ``prompt_answer_unmatched`` rather than sitting in the control directory unread.

**Settlement is DURABLE and EPOCHED.** Every prompt that stops being open appends one line to
``report/control/consultation-settlements.jsonl`` — base identity, full prompt id, epoch,
resolution, the program fingerprint it was settled under, and the delivery. Those records are the
only place the epoch comes from, which is what makes the ids replay-identical: the same settlements
read back give the same epochs give the same prompt ids. They carry NO clock, for the same reason
the timeouts are delivery-counted.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from isar.drive.session_log import log_event
from isar.schema.consultation import (
    ANSWER_SCHEMA, ConsultationPrompt, base_identity, prompt_identity, prompt_kind)

__all__ = ["ConsultationChannel", "Resolution", "answer_path", "settlements_path", "write_answer"]

#: Where the operator's answer lands. Same directory as ``stop-<token>.json``.
CONTROL_DIR = ("report", "control")

#: One consolidated append-only settlement log rather than a file per base identity: the channel
#: reads ALL of them on construction anyway (it needs every epoch, not one), and one open-append is
#: cheaper and easier to audit than a directory scan whose file names encode the key.
SETTLEMENTS_NAME = "consultation-settlements.jsonl"
SETTLEMENT_SCHEMA = "isar.consultation-settlement/1"

ANSWERED = "answer"
DEFAULTED = "default"


def answer_path(workspace, prompt_id: str) -> Path:
    return Path(workspace).joinpath(*CONTROL_DIR) / f"prompt-{prompt_id}.answer.json"


def settlements_path(workspace) -> Path:
    return Path(workspace).joinpath(*CONTROL_DIR) / SETTLEMENTS_NAME


def write_answer(workspace, prompt_id: str, option: str, *, note: str | None = None,
                 by: str = "operator") -> Path:
    """Answer a prompt, atomically. THE writer for this surface — the GUI's answer button, an
    operator's script, and every test go through here so the file shape has one definition."""
    target = answer_path(workspace, prompt_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {"schema": ANSWER_SCHEMA, "prompt_id": prompt_id, "option": option,
                              "by": by}
    if note:
        record["note"] = note
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


class Resolution(NamedTuple):
    """One prompt that stopped being open: by ``answer`` or by ``default``."""

    prompt: ConsultationPrompt
    option: str
    source: str


@dataclass(frozen=True, slots=True)
class _Answer:
    option: str | None      # None when the file could not be read at all
    raw: str
    error: str | None = None


class ConsultationChannel:
    """One step's open prompts. Delivery-indexed; holds no clock and no thread."""

    def __init__(self, workspace, log_path, *, step_id: str, timeout_deliveries: int,
                 policy_defaults: Mapping[str, str] | None = None) -> None:
        self.workspace = Path(workspace)
        self.log_path = log_path
        self.step_id = step_id
        self.timeout_deliveries = int(timeout_deliveries)
        self.policy_defaults = dict(policy_defaults or {})
        self._open: dict[str, ConsultationPrompt] = {}
        self._history: list[ConsultationPrompt] = []
        self._by_id: dict[str, ConsultationPrompt] = {}
        self._settled: dict[str, str] = {}          # prompt_id -> the option that took effect
        self._defaulted: set[str] = set()
        self._reported_invalid: dict[str, str] = {}
        self._reported_stale: set[str] = set()
        self._reported_unmatched: set[str] = set()
        # Durable settlement state, read back from disk so a resumed step (and a replay) mints the
        # same ids this workspace already used. ``_epochs`` counts settlements per BASE identity.
        self._epochs: dict[str, int] = {}
        self._settled_ids: set[str] = set()
        self._load_settlements()

    # ---------------------------------------------------------------- settlement records
    def _load_settlements(self) -> None:
        path = settlements_path(self.workspace)
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        except OSError as error:
            log_event(self.log_path, "decide",
                      {"event": "consultation_settlements_unreadable",
                       "action": "consultation_settlements_unreadable",
                       "step_id": self.step_id, "path": str(path), "error": str(error)})
            return
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                log_event(self.log_path, "decide",
                          {"event": "consultation_settlement_unreadable",
                           "action": "consultation_settlement_unreadable",
                           "step_id": self.step_id, "path": str(path), "error": str(error)})
                continue
            base = record.get("base_id") if isinstance(record, Mapping) else None
            if not isinstance(base, str):
                continue
            self._epochs[base] = self._epochs.get(base, 0) + 1
            if isinstance(record.get("prompt_id"), str):
                self._settled_ids.add(record["prompt_id"])

    def _append_settlement(self, prompt: ConsultationPrompt, option: str, source: str,
                           delivery: int, program_fingerprint: str | None) -> None:
        """One durable line per settlement. Append-only and clock-free; written whole, flushed and
        fsynced, the same durability rule the revision stores' history uses."""
        record = {"schema": SETTLEMENT_SCHEMA, "base_id": prompt.base_id,
                  "prompt_id": prompt.prompt_id, "epoch": prompt.epoch, "kind": prompt.kind,
                  "step_id": prompt.step_id, "subject": prompt.subject, "resolution": option,
                  "source": source, "delivery": delivery,
                  "program_fingerprint": program_fingerprint
                  or prompt.expected_program_revision}
        path = settlements_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._epochs[prompt.base_id] = prompt.epoch + 1
        self._settled_ids.add(prompt.prompt_id)

    # ---------------------------------------------------------------- issuing
    def issue(self, requests, *, delivery: int, program_fingerprint: str,
              agenda_revision: str | None = None, scene_digest: str | None = None
              ) -> list[ConsultationPrompt]:
        """Mint a prompt per NEW consultation request. Dedup is by ``prompt_id``: an already-open
        prompt is not re-issued, and neither is one already settled AT THIS EPOCH — which is what
        makes a request the agenda repeats on every delivery cost one prompt.

        A request whose subject re-appears AFTER a settlement is a new occurrence and mints
        epoch+1: the operator answered about the question closing then, and it has closed again.
        """
        minted: list[ConsultationPrompt] = []
        for request in requests or ():
            record = dict(request or {})
            kind = prompt_kind(record.get("kind"))
            if kind is None:
                log_event(self.log_path, "decide",
                          {"event": "prompt_kind_unknown", "action": "prompt_kind_unknown",
                           "step_id": self.step_id, "kind": record.get("kind"), "iter": delivery})
                continue
            subject = record.get(kind.subject_field)
            if not isinstance(subject, str) or not subject:
                log_event(self.log_path, "decide",
                          {"event": "prompt_subject_missing", "action": "prompt_subject_missing",
                           "step_id": self.step_id, "kind": kind.kind,
                           "subject_field": kind.subject_field, "iter": delivery})
                continue
            base_id = base_identity(kind=kind.kind, step_id=self.step_id, subject=subject,
                                    options=kind.options)
            epoch = self._epochs.get(base_id, 0)
            prompt_id = prompt_identity(kind=kind.kind, step_id=self.step_id, subject=subject,
                                        options=kind.options, epoch=epoch)
            if prompt_id in self._open or prompt_id in self._settled_ids:
                continue
            prompt = ConsultationPrompt(
                prompt_id=prompt_id, base_id=base_id, epoch=epoch, kind=kind.kind,
                step_id=self.step_id, subject=subject,
                options=kind.options, default_option=self._default_for(kind),
                timeout_deliveries=self.timeout_deliveries, hard=kind.hard,
                expected_program_revision=program_fingerprint, issued_delivery=delivery,
                agenda_revision=agenda_revision, scene_digest=scene_digest,
                question_id=record.get("question_id"), detail=record)
            self._open[prompt_id] = prompt
            self._history.append(prompt)
            self._by_id[prompt_id] = prompt
            minted.append(prompt)
            log_event(self.log_path, "decide",
                      {"event": "prompt_issued", "action": "prompt_issued",
                       **prompt.to_dict(), "iter": delivery})
        return minted

    def _default_for(self, kind) -> str:
        """The DECLARED default: the frozen program's ``prompt_defaults`` where the kind names one
        and it is a real option, else the kind's own fallback. Never the originating request's
        ``default`` field — that is the producer describing itself, not policy."""
        declared = self.policy_defaults.get(kind.policy_default_key) if kind.policy_default_key \
            else None
        return declared if declared in kind.options else kind.fallback_default

    # ---------------------------------------------------------------- resolving
    def poll(self, delivery: int, *, program_fingerprint: str | None = None) -> list[Resolution]:
        """Read answers, apply defaults, record staleness. Called once per pass of the step loop.

        ``delivery`` is the step's DELIVERY COUNT, not an iteration count: a decline or a transport
        failure must not age a prompt, or the timeout would depend on how badly the instrument was
        behaving rather than on how much measuring went by.

        ``program_fingerprint`` is the revision the campaign is planning under RIGHT NOW; it is
        recorded on each settlement so the lineage says which program the operator's answer (or the
        default) landed against. Absent, the prompt's issue-time revision is recorded instead.
        """
        resolutions: list[Resolution] = []
        for prompt_id in sorted(self._open):
            prompt = self._open[prompt_id]
            answer = self._read_answer(prompt_id)
            if answer is not None and answer.option in prompt.options:
                resolutions.append(self._settle(prompt, answer.option, ANSWERED, delivery,
                                                program_fingerprint, note=answer.raw))
                continue
            if answer is not None:
                self._record_invalid(prompt, answer, delivery)
            if delivery - prompt.issued_delivery >= prompt.timeout_deliveries:
                resolutions.append(self._settle(prompt, prompt.default_option, DEFAULTED,
                                                delivery, program_fingerprint))
        self._record_stale(delivery)
        self._record_unmatched(delivery)
        return resolutions

    def _settle(self, prompt: ConsultationPrompt, option: str, source: str, delivery: int,
                program_fingerprint: str | None = None, note: str | None = None) -> Resolution:
        del self._open[prompt.prompt_id]
        self._settled[prompt.prompt_id] = option
        self._append_settlement(prompt, option, source, delivery, program_fingerprint)
        event = "prompt_answered" if source == ANSWERED else "prompt_default_applied"
        if source == DEFAULTED:
            self._defaulted.add(prompt.prompt_id)
        log_event(self.log_path, "decide",
                  {"event": event, "action": event, "step_id": self.step_id,
                   "prompt_id": prompt.prompt_id, "kind": prompt.kind, "subject": prompt.subject,
                   "option": option, "issued_delivery": prompt.issued_delivery,
                   "timeout_deliveries": prompt.timeout_deliveries, "iter": delivery})
        return Resolution(prompt=prompt, option=option, source=source)

    def _read_answer(self, prompt_id: str) -> _Answer | None:
        path = answer_path(self.workspace, prompt_id)
        try:
            if not path.is_file():
                return None
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            return _Answer(option=None, raw="", error=f"unreadable: {error}")
        try:
            record = json.loads(raw)
        except ValueError as error:
            return _Answer(option=None, raw=raw, error=f"not JSON: {error}")
        if not isinstance(record, Mapping):
            return _Answer(option=None, raw=raw, error="answer is not an object")
        if record.get("schema") != ANSWER_SCHEMA:
            return _Answer(option=None, raw=raw,
                           error=f"schema {record.get('schema')!r} is not {ANSWER_SCHEMA!r}")
        if record.get("prompt_id") != prompt_id:
            return _Answer(option=None, raw=raw,
                           error=f"answers prompt {record.get('prompt_id')!r}, not {prompt_id!r}")
        option = record.get("option")
        return _Answer(option=option if isinstance(option, str) else None, raw=raw,
                       error=None if isinstance(option, str) else "no 'option' string")

    def _record_invalid(self, prompt: ConsultationPrompt, answer: _Answer, delivery: int) -> None:
        """Once per distinct file content — the prompt stays open, so the file is re-read every
        pass and an unconditional event would fill the log with one operator's typo."""
        if self._reported_invalid.get(prompt.prompt_id) == answer.raw:
            return
        self._reported_invalid[prompt.prompt_id] = answer.raw
        log_event(self.log_path, "decide",
                  {"event": "prompt_answer_invalid", "action": "prompt_answer_invalid",
                   "step_id": self.step_id, "prompt_id": prompt.prompt_id, "kind": prompt.kind,
                   "option": answer.option, "options": list(prompt.options),
                   "error": answer.error or "not one of the offered options",
                   "iter": delivery})

    def _record_stale(self, delivery: int) -> None:
        """An answer that arrived after its default was applied. Recorded, NOT applied (D6): an
        explicit re-apply is out of v1 scope, and silently acting on it would make the campaign's
        behaviour depend on a race the operator cannot see.

        A file that is not a VALID answer is not a stale answer — it is an invalid one, and it is
        recorded as such. It also does not consume the one-shot stale slot: a corrupt file arriving
        first must not silence the real late answer the operator writes over it afterwards.
        """
        for prompt_id in sorted(self._defaulted - self._reported_stale):
            answer = self._read_answer(prompt_id)
            if answer is None:
                continue
            prompt = self._by_id[prompt_id]
            if answer.option not in prompt.options:
                self._record_invalid(prompt, answer, delivery)
                continue
            self._reported_stale.add(prompt_id)
            log_event(self.log_path, "decide",
                      {"event": "stale_answer", "action": "stale_answer",
                       "step_id": self.step_id, "prompt_id": prompt_id,
                       "option": answer.option, "applied": self._settled.get(prompt_id),
                       "iter": delivery})

    def _record_unmatched(self, delivery: int) -> None:
        """Answer files naming an id nothing here knows — once per distinct file content.

        The failure this closes: an operator mistypes an id, or answers an occurrence that has
        since settled and re-opened under a new epoch, and the file sits in the control directory
        unread while they wait for a campaign that never saw it. Bounded the same way
        ``prompt_answer_invalid`` is (one event per distinct content), because the directory is
        re-scanned every delivery.
        """
        known = set(self._open) | self._settled_ids
        directory = Path(self.workspace).joinpath(*CONTROL_DIR)
        try:
            files = sorted(directory.glob("prompt-*.answer.json"))
        except OSError:
            return
        for path in files:
            prompt_id = path.name[len("prompt-"):-len(".answer.json")]
            if prompt_id in known:
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as error:
                raw = f"unreadable: {error}"
            if raw in self._reported_unmatched:
                continue
            self._reported_unmatched.add(raw)
            log_event(self.log_path, "decide",
                      {"event": "prompt_answer_unmatched", "action": "prompt_answer_unmatched",
                       "step_id": self.step_id, "prompt_id": prompt_id, "path": path.name,
                       "iter": delivery})

    # ---------------------------------------------------------------- queries
    def hard_open(self) -> list[ConsultationPrompt]:
        """Open prompts declaring ``hard``. v1 has no hard-gate mechanism of its own: the driver
        maps these to the operator-requested pause the stop token already implements."""
        return [self._open[pid] for pid in sorted(self._open) if self._open[pid].hard]

    @property
    def issued(self) -> tuple[dict, ...]:
        """Every prompt this step minted, in issue order — the step result's record of the
        consultation, alongside the events."""
        return tuple(prompt.to_dict() for prompt in self._history)
