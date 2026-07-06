"""The acquisition function — propose the next SCAN (STUB — LLM_HARNESS_DESIGN §2.2).

Contract: given the current ω(q) :mod:`model` and the remaining budget, choose the next measurement
to maximize information per unit cost. **The acquisition unit is a SCAN, not a point** — a complete
scan request:

- a **Q position** (or a short segment) along the objective path;
- an **energy window** — center = predicted ω(q), half-width = predicted uncertainty **plus a
  margin**, so the peak lands inside;
- a **neutron count** — escalating from a cheap quick-look (seed the count rate / ETA) to a
  production count, chosen from the target σ(ω₀) and the observed rate.

"Peak outside the chosen window" is a first-class outcome, not a failure (recover: widen / re-center
— a LOOK, §1). Cost is movement-aware once TAVI's virtual instrument clock exists; until then the
function uses counting time alone. Not implemented this round.
"""
from __future__ import annotations


def propose_scan(model, objective, budget_state):
    """Propose the next scan request (Q, energy window, neutron count) (NOT YET IMPLEMENTED).

    See LLM_HARNESS_DESIGN §2.2, §8 step 4.1.
    """
    raise NotImplementedError(
        "acquisition function not yet implemented (next round); see LLM_HARNESS_DESIGN §2.2. "
        "The acquisition unit is a SCAN (Q + energy window + neutron count), not a point."
    )
