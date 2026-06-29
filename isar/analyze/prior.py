"""The dispersion-prior expectation injected into step-4 verdict/confidence.

`PhononPrior` is the seam the future *running* dispersion prior (built from already-fitted
neighbours, §11) will fill. Step 4 only ever consumes it — it is an input, never built here.
`ScanContext.prior_omega` is the scalar shortcut (= `PhononPrior.omega0`); the richer object
adds the expected peak strength the detection-power test needs and the spread the confidence
distance term uses.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhononPrior:
    """Expected phonon at a scan's Q.

    omega0: expected peak position (meV).
    amplitude: expected peak strength (counts above background) for the detection-power test;
        ``None`` = unknown, so detection power abstains (no underpowered verdict can be raised).
    omega0_sigma: prior spread on omega0 (meV) for the confidence distance term.
    """

    omega0: float
    amplitude: float | None = None
    omega0_sigma: float = 1.0
