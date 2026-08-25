"""SCENE-5 5.4 slow-lane world: optic-pivot (H22(f)'s causal sequence, now real).

``fcc_no_acoustic`` carries the FCC Bragg line and a flat ~9 meV optic band ONLY — no acoustic
branch exists on this path at all (``isar/synth/worlds.py``). A short (<= 4 round) real
closed-loop run over it must show the placement authority pivot organically: the FIRST rounds
are blind opening-book coverage (nothing measured yet, so there is nothing to anchor a targeted
question on); once one of those coverage scans lands on the domain's one real feature (the
optic mode), the NEXT decision pivots away from blind coverage onto refining that found mode —
with no temperature or identity branch anywhere in steering code, exactly the map-comparison
razor (SCENE-5 §1).

Bounded per the plan's lane resolution: <= 4 rounds, real fits, slow-marked.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from drive._closed_loop import fcc_sample_knowledge, fcc_step, run_closed_loop

#: Coarser than the battery's own step (0.1): four opening-book nodes across [2, 3] is enough for
#: one to land on H=2.333, where the real optic mode fits cleanly (no dynesty escalation needed —
#: see the harness docstring's measured one-round cost), keeping this world's wall time trivial.
_SPACING = 0.34


def _decisions(report_dir: Path) -> list[dict]:
    path = report_dir / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.slow
def test_optic_pivot_decision_sequence(tmp_path: Path):
    t0 = time.time()
    run = run_closed_loop(tmp_path, "fcc_no_acoustic", sample_knowledge=fcc_sample_knowledge(),
                          max_rounds=4, step=fcc_step(spacing=_SPACING))
    wall = time.time() - t0
    print(f"\noptic-pivot: {len(run.rounds)} rounds, {wall:.1f}s wall "
         f"(harness-reported {run.wall_seconds:.1f}s)")

    assert not run.wall_ceiling_hit
    assert len(run.rounds) == 4
    assert all(r.plan.outcome == "selected" for r in run.rounds)

    records = _decisions(tmp_path / "report")
    assert len(records) == 4
    generators = [r["selection"]["supplying_association"]["generator"] for r in records]
    reasons = [r["selection"]["supplying_association"]["reason"] for r in records]

    # Rounds 0-1: nothing has been measured yet, so the winning decision is blind opening-book
    # coverage — there is no found feature to target a question at.
    assert generators[0] == generators[1] == "opening_book_survey"
    # By round 2, one of those two coverage scans has landed on the domain's one real feature
    # (the optic mode, since no acoustic branch exists to compete for it): the decision pivots
    # organically to REFINING that found mode, never back to blind coverage.
    assert generators[2] == generators[3] == "parameter_refine"
    assert generators[2] != generators[0]
    # The pivot is causally anchored on the round-0 measurement, not a coincidence: the refine
    # reason names the exact arena the found mode came from.
    assert "round0" in reasons[2] and "round0" in reasons[3]

    # The refined node is a real domain node this run actually measured (round 0's), not an
    # arbitrary pick — the organic causal link H22(f) names.
    refined_h = {r.node_h for r in run.rounds[2:]}
    assert refined_h == {run.rounds[0].node_h}
