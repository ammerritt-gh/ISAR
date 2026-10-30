"""Fake inter-spectra runner and save for the analysis service's tests (``--inter-target`` /
``--inter-save-target``). Stdlib only, like ``_service_fakes``; the tests name the module ``_inter_service_fakes``.

``run_batch`` reads ``<root>/fake_inter_plan.json``: ``{"<batch_id>": spec}``; spec may hold ``stages`` (ring
payloads after the start, default 3), ``refuse`` (``{"<stage index>": "reason"}`` the save refuses for that
ring's unit), ``save_fail`` (stage index whose payload the save refuses whole), ``end`` (False: no ``end``
payload, the item just ends; "hang" is the stop tests' way to keep it alive), ``hang`` (seconds to sleep after
the last stage), ``pre_hang`` (the same before the start payload), ``raise``, ``cores_notice``, ``member_events``
(true: a ``members`` event after the start and after each ring, when the runner was given ``members``). Every
payload is acked; the verdict each ack returned goes to ``<root>/acks-<batch_id>.jsonl`` the moment it arrives.

``save_inter`` appends each payload to ``<root>/inter_saved.jsonl`` and answers the real writer's verdict
words: a stage or end for a batch with no start is the str ``"unknown batch <id>"``, an ``end`` for a batch
that already ended is accepted and not written again.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BatchItem:
    item_id: str
    unit_key: str
    pass_kind: str
    cores: int
    spec: dict


@dataclass(frozen=True)
class InterResult:
    item_id: str
    unit_key: str
    pass_kind: str
    status: str
    reason: str | None
    payload: dict | None
    cores: int | None = None
    ack: int | None = None


def _payload(item: BatchItem, event: str, index: int | None, spec: dict) -> dict:
    p = {"schema": "isar.inter-result/1", "unit": item.unit_key, "pass": "inter",
         "batch": item.spec["batch_id"], "event": event, "index": index}
    if index is not None and spec.get("save_fail") == index:
        p["fail"] = "the fake disk is full"
    if str(index) in spec.get("refuse", {}):
        p["refuse"] = spec["refuse"][str(index)]
    return p


def run_batch(root, item, *, progress, emit, ack, members=None) -> None:
    root = Path(root)
    bid = item.spec["batch_id"]
    spec = json.loads((root / "fake_inter_plan.json").read_text(encoding="utf-8")).get(bid, {})
    if spec.get("cores_notice"):
        emit(InterResult(item.item_id, item.unit_key, "cores", "cores", None, None, spec["cores_notice"]))
    token = 0
    n = int(spec.get("stages", 3))

    def send(event: str, index: int | None, status: str = "done", reason: str | None = None) -> None:
        nonlocal token
        token += 1
        emit(InterResult(item.item_id, item.unit_key, "inter", status, reason,
                         _payload(item, event, index, spec), ack=token))
        verdict = ack(token)
        with open(root / f"acks-{bid}.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"token": token, "event": event, "index": index, "verdict": verdict}) + "\n")

    def member_map(k: int) -> None:
        # the runner's map after k rings (0: at the plan): the first k members done, the rest waiting
        if members is None or not spec.get("member_events"):
            return
        names = item.spec["members"]
        members({"batch": bid, "kind": item.spec["kind"], "stage": "plan" if k == 0 else "ring", "index": k,
                 "of": n, "round": None,
                 "counts": {"done": k, "failed": 0, "not_reached": len(names) - k, "fixed": 0, "total": len(names)},
                 "states": {m: "done" if i < k else "waiting" for i, m in enumerate(names)}, "reasons": {}})

    if spec.get("pre_hang"):
        time.sleep(float(spec["pre_hang"]))
    send("start", None)
    member_map(0)
    for k in range(1, n + 1):
        progress({"pass": "inter", "kind": "anchored", "stage": "ring", "index": k, "of": n, "round": None,
                  "phase": "save", "done": k, "failed": 0, "not_reached": n - k, "fixed": 0, "total": n})
        send("stage", k)
        member_map(k)
    if spec.get("hang"):
        time.sleep(float(spec["hang"]))
    if spec.get("raise"):
        raise RuntimeError(spec["raise"])
    if spec.get("end", True):
        send("end", None, "done")


def save_inter(root, payloads) -> list:
    root = Path(root)
    log = root / "inter_saved.jsonl"
    seen = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    verdicts = []
    for p in payloads:
        started = any(s["batch"] == p["batch"] and s["event"] == "start" for s in seen)
        ended = any(s["batch"] == p["batch"] and s["event"] == "end" for s in seen)
        if p.get("fail"):
            verdicts.append(p["fail"])
        elif p["event"] != "start" and not started:
            verdicts.append(f"unknown batch {p['batch']}")
        elif p["event"] == "end" and ended:
            verdicts.append({"saved": True, "accepted": {}, "refused": {}})
        else:
            seen.append(p)
            with open(log, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(p) + "\n")
            unit = f"E:1:r{p['index']}"
            refused = {unit: p["refuse"]} if p.get("refuse") else {}
            accepted = {} if p["event"] != "stage" or refused else {unit: f"f{p['index']:012x}"}
            verdicts.append({"saved": True, "accepted": accepted, "refused": refused})
    return verdicts
