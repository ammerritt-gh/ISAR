"""Progress events from the fast and deep pass (C-progress): one plain dict per step, sent as the step
starts (fast) or completes (deep), so a caller can show how far a fit has got. The events never change a result.

Qt-free (ISAR hard rule 6); no session import.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

log = logging.getLogger(__name__)

ProgressFn = Callable[[dict], None]

FAST_PHASES = ("seed", "ladder", "evidence", "audits", "classify", "errors")
DEEP_PHASES = ("sampling", "errors")
DEEP_STAGES = 2          # 1 combined initial models, 2 promoted models


def emitter(progress: ProgressFn | None, pass_kind: str):
    """``send(phase, *, step=None, of=None, stage=None, stages=None, timing=None)`` for one pass ("fast" or
    "deep"). ``None`` makes it a no-op. A callback that raises is logged once and dropped for the
    rest of the pass, so it never reaches the fit. Optional deep-attempt timings travel with the
    step to the retained service protocol log; displays still use the ordinary progress fields."""
    state = {"fn": progress}

    def send(phase, *, step=None, of=None, stage=None, stages=None, timing=None):
        fn = state["fn"]
        if fn is None:
            return
        try:
            event = {"pass": pass_kind, "phase": phase, "step": step, "of": of, "stage": stage,
                     "stages": stages}
            if timing:
                event["timing"] = timing
            fn(event)
        except Exception:  # noqa: BLE001 — a broken display must never change a fit
            log.warning("%s pass: progress callback failed; no further progress for this pass",
                        pass_kind, exc_info=True)
            state["fn"] = None

    return send


def relay(send):
    """A ProgressFn that forwards each event to ``send``, so a caller that runs an engine step inside
    its own pass keeps that pass's one emitter (a broken callback is dropped once, not per step)."""
    return lambda ev: send(ev["phase"], step=ev["step"], of=ev["of"], stage=ev["stage"],
                           stages=ev["stages"], timing=ev.get("timing"))
