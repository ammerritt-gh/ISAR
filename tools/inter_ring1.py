"""Open an anchored batch headless and stop at the end of ring 1's stage 1 (shared by the two S1U5 measurement tools).

``tools/inter_gradient_compare.py`` and ``tools/inter_screen_calibration.py`` both start the batch the GUI would
start (``batch_preview``, ``runner.run_batch`` with the service's emit and ack played here) and wrap
``runner._Run.assign`` to take what they need from the first stage-1 search, then stop the batch. Run these tools
on a scratch COPY of the experiment only: the batch records are written into the workspace.

Also here: the wall-clock guard (``arm_timeout``), which writes a note and kills the whole process tree.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

if __package__ in (None, ""):                      # run as a script from anywhere in the repo
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isar.session import writer  # noqa: E402
from isar.session.inter_spectra import records, runner  # noqa: E402
from isar.workspace_api.inter_spectra import BatchRequest, batch_preview  # noqa: E402

log = logging.getLogger("inter_ring1")


class Stop(BaseException):
    """Raised out of the wrapped ``assign`` to end the batch once ring 1's stage 1 is in hand."""


class _Service:
    """The service's side in this process: each payload is saved at once and its verdict handed back."""

    def __init__(self, root: Path) -> None:
        self.root, self.verdicts = root, {}

    def emit(self, r) -> None:
        if r.payload is None or r.ack is None:
            return
        v = writer.save_inter(self.root, [r.payload])[0]
        self.verdicts[r.ack] = v if isinstance(v, dict) else {"saved": False, "error": v}

    def ack(self, token: int) -> dict:
        return self.verdicts.pop(token)


def arm_timeout(seconds: float, out: Path, what: str) -> threading.Timer:
    """After ``seconds`` write ``TIMEOUT.txt`` into ``out`` and kill this process and its children."""
    def fire() -> None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "TIMEOUT.txt").write_text(f"{what} killed after {seconds:.0f} s of wall clock\n", encoding="utf-8",
                                         newline="\n")
        print(f"TIMEOUT: {what} killed after {seconds:.0f} s; see {out / 'TIMEOUT.txt'}", file=sys.stderr, flush=True)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())], check=False)
        os._exit(3)
    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    return t


def ring1(root: Path, anchors, direction: str, workers: int, *, search: bool) -> dict:
    """Start the anchored batch on ``root`` and stop at the first ``assign`` (ring 1's stage 1). ``search`` False
    stops before any search runs; True runs the real search through the batch's pool first. Returns ``{"line":
    the preview line, "snaps": {unit: Snapshot}, "cache": {unit: {job key: Priced}}, "A": {unit: Assignment},
    "errors": {unit: exception}}``; ``A`` and ``errors`` are empty when ``search`` is False."""
    pv = batch_preview(root, BatchRequest("anchored", direction, tuple(anchors), ()))
    if not pv.can_start:
        raise SystemExit(f"cannot start: {pv.reason}")
    spec = runner.BatchSpec.from_json({"batch_id": records.new_batch_id(), "kind": "anchored", "direction": direction,
                                       "anchors": list(anchors), "members": list(pv.members)})
    got: dict = {"line": pv.line, "snaps": {}, "cache": {}, "A": {}, "errors": {}}
    orig = runner._Run.assign

    def assign(self, snaps, cache):
        got["snaps"], got["cache"] = dict(snaps), cache
        if search:
            got["A"], got["errors"] = orig(self, snaps, cache)
        raise Stop()
    runner._Run.assign = assign
    svc = _Service(root)
    item = runner.BatchItem("ring1-tool", f"inter:{spec.batch_id}:batch", "inter", workers, spec.to_json())
    try:
        runner.run_batch(root, item, progress=lambda p: None, emit=svc.emit, ack=svc.ack)
    except Stop:
        pass
    finally:
        runner._Run.assign = orig
    return got
