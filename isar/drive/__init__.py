"""`isar drive` — the measurement driver (LLM_HARNESS_DESIGN, TAVI repo).

The driver is the **third component** of a settled three-part architecture:

1. instrument control surface (TAVI now, a real TAS later) — executes/validates, never analyzes;
2. analysis engine (**ISAR**) — the only thing that produces a physics number;
3. **the driver** (this package) — the decision layer between a human goal and the other two.

The driver decides WHAT to measure; it never computes a physics number. Every number comes from
ISAR, reached ONLY by running the `isar run` CLI as a subprocess and reading the file contract
(`report/results.csv`, `report/report.json`, `card.state.json`) — see LLM_HARNESS_DESIGN §4.

**Architectural boundary (enforced by test `tests/drive/test_boundary.py`).** `isar.drive` imports
NOTHING from `isar.analyze`, `isar.session`, `isar.calibrate`, `isar.parsers`, or `isar.reduce`
— the single exception is `isar.reduce.llm_client`, the shared LLMM HTTP plumbing. Analysis lives
behind the subprocess boundary so the driver can never learn to lean on a fit internal that will not
transfer to a real instrument.

This round is PLUMBING ONLY: the objective spec, the TAVI REST client, the ISAR file I/O, the
σ-calibration application, and the session log are live. The acquisition engine, the LLM supervisor,
and the `run_campaign` orchestrator are documented stubs for the next round.
"""
from __future__ import annotations

from isar.drive.objective import ObjectiveSpec

__all__ = ["ObjectiveSpec"]
