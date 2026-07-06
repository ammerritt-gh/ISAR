"""The stopping criterion (STUB — LLM_HARNESS_DESIGN §2.1, the field's untested gap).

Contract: hold the first-class, testable decision that the objective is one of

- **met** — the CALIBRATED σ(ω₀) ≤ tolerance everywhere along the path. The calibrated half-width
  comes from ISAR's per-scan ``interval_lo``/``interval_hi`` (``results.csv``) inflated by the
  σ-calibration (:mod:`isar.drive.acquisition.calibration`) — NEVER ISAR's prior sigma, which floors
  at 0.3 meV by design and would make a sub-0.3 meV objective declare "met" falsely.
- **unmeetable** — with reasons: the target is below the instrument's resolution here; the budget is
  exhausted with the path still under-determined; the sample/config cannot deliver the statistics.

Teixeira Parente et al. (2022) name the stopping criterion as the untested gap in autonomous TAS;
the driver makes it an explicit output, not an afterthought. Not implemented this round.
"""
from __future__ import annotations


def evaluate_stop(objective, model_state, budget_state):
    """Return a stop decision (``met`` / ``unmeetable`` / ``continue``) with reasons.

    NOT YET IMPLEMENTED — see LLM_HARNESS_DESIGN §2.1, §8 step 5.
    """
    raise NotImplementedError(
        "stopping criterion not yet implemented (next round); see LLM_HARNESS_DESIGN §2.1. It must "
        "consume the CALIBRATED interval half-width, never ISAR's floored prior sigma."
    )
