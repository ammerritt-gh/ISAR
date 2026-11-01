"""Fake link target and save for the analysis service's tests (``--link-target`` / ``--link-save-target``).

Stdlib only, like ``_inter_service_fakes``; the tests put this folder on the service's PYTHONPATH and name the
module ``_link_service_fakes``. The link object a test sends carries its own script under ``"fake"``:
``status`` and ``reason`` (the result, default done), ``hang`` (seconds to sleep before the result) and
``save_fail`` (the reason the save refuses the payload with).

``save_link_payloads`` appends each accepted payload to ``<root>/link_saved.jsonl`` and answers the real save's
verdict shape: ``{"saved": True, "link": <link_id>, "new": True}``, or the refusal reason as a str.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LinkItem:
    item_id: str
    unit_key: str
    pass_kind: str
    cores: int
    link: dict


@dataclass(frozen=True)
class LinkResult:
    item_id: str
    unit_key: str
    pass_kind: str
    status: str
    reason: str | None
    payload: dict | None


def run_link(root, item, *, progress, emit) -> None:
    script = item.link.get("fake", {})
    progress({"pass": "link", "phase": "link"})
    if script.get("hang"):
        time.sleep(script["hang"])
    emit(LinkResult(item.item_id, item.unit_key, "link", script.get("status", "done"),
                    script.get("reason", "one summary line"),
                    {"schema": "isar.link-result/1", "link_id": item.link["link_id"],
                     "fail": script.get("save_fail")}))


def save_link_payloads(root, payloads) -> list[dict | str]:
    out: list[dict | str] = []
    with (Path(root) / "link_saved.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        for p in payloads:
            if p.get("fail"):
                out.append(p["fail"])
                continue
            f.write(json.dumps(p) + "\n")
            out.append({"saved": True, "link": p["link_id"], "new": True})
    return out
