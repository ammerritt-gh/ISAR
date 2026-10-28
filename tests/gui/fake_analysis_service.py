"""A scripted stand-in for the analysis service (C-service), spoken over the same JSONL stdin/stdout.

The queue controller's tests run it as a real subprocess through ``ProcessRunner``, so the pipe, the framing
and the process lifecycle are the real ones; only the fitting is absent. The fake decides nothing about the
queue: it answers the few commands the real service answers by itself and otherwise does what the test says.

    python fake_analysis_service.py --workspace <root> [--log <path>] [--record <path>]

By itself it:

* prints ``ready`` at start, and ``pong`` for ``ping``;
* answers ``start`` with ``started`` (the real service's first event for an item), unless told not to;
* on ``shutdown`` (and on stdin EOF) prints the lines it was given for that moment, then ``bye``, and exits 0.

It does not answer ``stop``, ``hold_writes`` or ``release_writes``: the test sends the events it wants.
Every command the controller sends is appended, as received, to ``--record`` (one JSON object per line).

Test-only commands (never sent by the controller):

* ``{"op": "_emit", "lines": [event, ...]}``       print these events, in order, now;
* ``{"op": "_on_shutdown", "lines": [event, ...]}`` print these events before ``bye`` when shutdown comes;
* ``{"op": "_auto_started", "on": false}``           stop answering ``start`` with ``started``;
* ``{"op": "_hang_on_shutdown"}``                    ignore ``shutdown`` from now on (a service that went silent);
* ``{"op": "_exit", "code": 3}``                     die at once, as a crash would.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--log")
    parser.add_argument("--record")
    args = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    record = open(args.record, "a", encoding="utf-8", newline="\n") if args.record else None

    def emit(event: dict) -> None:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    on_shutdown: list[dict] = []
    auto_started = True
    hang = False
    emit({"ev": "ready", "workspace": args.workspace, "pid": os.getpid()})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        cmd = json.loads(raw)
        op = cmd.get("op")
        if record is not None and not op.startswith("_"):
            record.write(raw + "\n")
            record.flush()
        if op == "start" and auto_started:
            emit({"ev": "started", "item_id": cmd["item_id"], "pid": os.getpid()})
        elif op == "ping":
            emit({"ev": "pong"})
        elif op == "shutdown" and not hang:
            break
        elif op == "_emit":
            for event in cmd["lines"]:
                emit(event)
        elif op == "_on_shutdown":
            on_shutdown = list(cmd["lines"])
        elif op == "_auto_started":
            auto_started = bool(cmd["on"])
        elif op == "_hang_on_shutdown":
            hang = True
        elif op == "_exit":
            os._exit(int(cmd.get("code", 1)))
    for event in on_shutdown:
        emit(event)
    emit({"ev": "bye"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
