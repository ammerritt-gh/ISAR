"""`run_campaign` — the adaptive-loop orchestrator (STUB this round).

This is the top of the driver: it wires the three parts (LLM supervisor §2.1, deterministic
acquisition engine §2.2, instrument client §2.3) into the MVP loop of LLM_HARNESS_DESIGN §8:

    objective spec  ->  σ-calibration (milestone 0, §5)  ->  warm-start layout  ->
      repeat { acquisition proposes a scan -> validate -> run on TAVI -> save + `isar run` ->
               read results.csv/card.state.json -> fold into the ω(q) model -> supervisor checks
               met / unmeetable / anomaly } -> stop

None of that loop logic exists yet. This round ships only the plumbing the loop will call: the
:class:`~isar.drive.objective.ObjectiveSpec`, the TAVI client, the ISAR file I/O, the σ-calibration
application, and the session log. `run_campaign` therefore raises with a pointer to the design.
"""
from __future__ import annotations

from isar.drive.objective import ObjectiveSpec


def run_campaign(objective: ObjectiveSpec, config: dict | None = None):
    """Run an adaptive measurement campaign to satisfy ``objective`` (NOT YET IMPLEMENTED).

    See LLM_HARNESS_DESIGN §8 (the worked MVP loop) for the intended orchestration. The acquisition
    engine (§2.2) and the LLM supervisor (§2.1) land next round; this round is skeleton + plumbing.
    """
    raise NotImplementedError(
        "driver loop not yet implemented (next round); see TAVI docs/LLM_HARNESS_DESIGN.md §8 for "
        "the intended objective→calibrate→warm-start→adaptive-loop→stop orchestration. The plumbing "
        "(objective, TAVI client, ISAR file I/O, σ-calibration, session log) is live."
    )
