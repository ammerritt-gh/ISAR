"""Fake fit and save targets for the analysis service's tests (``--fit-target`` / ``--save-target``).

Stdlib only, so an item process starts in a fraction of a second. The tests put this folder on the
service's ``PYTHONPATH`` and name the module ``_service_fakes`` (the repo has a sibling ``tests`` package on
some machines' paths, so ``tests.session._service_fakes`` is not used); spawned item processes and their
pool workers inherit the path.

``fit_file`` reads ``<root>/fake_plan.json``: ``{"<unit_key>" or "<unit_key>|<pass>": spec}``, where spec may
hold ``steps`` / ``step_s`` (progress events, one per step), ``status`` / ``reason`` (the item's result;
default done), ``fast_stage`` (a deep item first emits a fast result, unless that unit's fast marker
exists), ``cores_notice`` (a deep item's cores notice, after any fast result), ``attempt`` (an attempt record
first), ``pool`` (a ProcessPoolExecutor of ``item.cores`` busy workers, pids written to
``<root>/pids-<item_id>.json``), ``hang`` (seconds to sleep before the result), ``raise``, ``exit_code``
(``os._exit``), ``print`` (stray output on sys.stdout and fd 1), and ``save_sleep`` / ``save_fail`` (copied
into the payload for ``save_results``).
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileItem:
    item_id: str
    unit_key: str
    pass_kind: str
    cores: int


@dataclass(frozen=True)
class FileResult:
    item_id: str
    unit_key: str
    pass_kind: str
    status: str
    reason: str | None
    payload: dict | None
    cores: int | None = None


def _marker(root: Path, unit_key: str) -> Path:
    return root / ("fast-" + unit_key.replace(":", "_") + ".done")


def _busy(pid_dir: str) -> None:
    Path(pid_dir, f"pool-{os.getpid()}").write_text("", encoding="utf-8")
    while True:                          # a pool worker that keeps its core until killed
        time.sleep(0.05)


def _payload(item: FileItem, pass_kind: str, spec: dict) -> dict:
    p = {"unit": item.unit_key, "pass": pass_kind, "item": item.item_id}
    if spec.get("save_sleep"):
        p["sleep"] = spec["save_sleep"]
    if spec.get("save_fail"):
        p["fail"] = spec["save_fail"]
    return p


def fit_file(root, item, *, progress, emit) -> None:
    root = Path(root)
    plan = json.loads((root / "fake_plan.json").read_text(encoding="utf-8"))
    spec = plan.get(f"{item.unit_key}|{item.pass_kind}", plan.get(item.unit_key, {}))
    if spec.get("print"):
        print("a stray print from a fit")
        os.write(1, b"a stray fd-1 write from a fit\n")
    if spec.get("attempt"):
        emit(FileResult(item.item_id, item.unit_key, "attempt", "attempt", None,
                        {"unit": item.unit_key, "pass": "attempt", "measurement_record": {"scan_ref": item.unit_key}}))
    if item.pass_kind == "deep" and spec.get("fast_stage") and not _marker(root, item.unit_key).exists():
        progress({"pass": "fast", "phase": "seed", "step": None, "of": None, "stage": None, "stages": None})
        emit(FileResult(item.item_id, item.unit_key, "fast", "done", None, _payload(item, "fast", spec)))
    if item.pass_kind == "deep" and spec.get("cores_notice"):
        emit(FileResult(item.item_id, item.unit_key, "cores", "cores", None, None, spec["cores_notice"]))
    pool = None
    if spec.get("pool"):
        pid_dir = root / f"pool-{item.item_id}"
        pid_dir.mkdir()
        pool = ProcessPoolExecutor(item.cores)
        for _ in range(item.cores):
            pool.submit(_busy, str(pid_dir))
        deadline = time.monotonic() + 60
        while len(list(pid_dir.iterdir())) < item.cores and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = [int(f.name.split("-")[1]) for f in pid_dir.iterdir()]
        (root / f"pids-{item.item_id}.json").write_text(json.dumps({"root": os.getpid(), "pool": pids}),
                                                         encoding="utf-8")
    phase = "ladder" if item.pass_kind == "fast" else "sampling"
    for i in range(1, int(spec.get("steps", 0)) + 1):
        progress({"pass": item.pass_kind, "phase": phase, "step": i, "of": spec["steps"],
                  "stage": None if phase == "ladder" else 1, "stages": None if phase == "ladder" else 3})
        time.sleep(float(spec.get("step_s", 0)))
    if spec.get("hang"):
        time.sleep(float(spec["hang"]))
    if spec.get("raise"):
        raise RuntimeError(spec["raise"])
    if "exit_code" in spec:
        os._exit(int(spec["exit_code"]))
    status = spec.get("status", "done")
    payload = None if status == "already_current" else _payload(item, item.pass_kind, spec)
    emit(FileResult(item.item_id, item.unit_key, item.pass_kind, status, spec.get("reason"), payload))


def save_results(root, payloads) -> list:
    """Appends each saved payload to ``<root>/saved.jsonl`` (an attempt to ``measurements.jsonl``) and writes a
    saved fast pass's marker; ``sleep`` delays and ``fail`` refuses a payload with that reason."""
    root = Path(root)
    verdicts = []
    for p in payloads:
        time.sleep(float(p.get("sleep", 0)))
        if p.get("fail"):
            verdicts.append(p["fail"])
            continue
        name = "measurements.jsonl" if p["pass"] == "attempt" else "saved.jsonl"
        with open(root / name, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(p) + "\n")
        if p["pass"] == "fast":
            _marker(root, p["unit"]).write_text("", encoding="utf-8")
        verdicts.append(None)
    return verdicts
