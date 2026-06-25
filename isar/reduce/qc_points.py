"""Point-level QC (§7.1) — stale_head: the HERIX leading stale-first-points signature.

A leading run of near-equal, high, low-scatter points is the deterministic fingerprint of HERIX's
stale-first-points bug. `qc_points(record, card)` masks that run and flags each point `stale_head`
(point-level QC masks — but never deletes, §0.4: the arrays stay full length, the mask is the
reversible keep/drop layer). Card-tunable; conservative defaults so a clean rising edge (monotonic,
not flat) never trips it — the discriminator is "flat AND high", which a real peak edge is not, and a
flat-but-low background fails the "high" gate.
"""
from __future__ import annotations

import numpy as np

from isar.schema import scan

_DEFAULTS = {"stale_min_run": 3, "stale_equal_tol": 0.02, "stale_high_frac": 0.5}


def _leading_stale_run(intensity, *, min_run, equal_tol, high_frac) -> list[int]:
    """Indices of the leading near-equal, high, low-scatter run; empty if there isn't one."""
    y = np.asarray(intensity, dtype="float64")
    n = y.size
    if n < min_run:
        return []
    finite = np.isfinite(y)
    if not finite.any():
        return []
    ymax = float(np.max(y[finite]))
    if ymax <= 0:
        return []
    high = high_frac * ymax
    v0 = float(y[0])
    if not np.isfinite(v0) or v0 < high:        # leading points aren't high → background, not stale
        return []
    tol = equal_tol * max(abs(v0), 1.0)
    run = 1
    for i in range(1, n):
        if np.isfinite(y[i]) and abs(float(y[i]) - v0) <= tol and float(y[i]) >= high:
            run += 1
        else:
            break
    if run < min_run or run >= n:        # too short, or the whole spectrum is flat (that's 'empty')
        return []
    tail = y[run:][np.isfinite(y[run:])]
    rest_med = float(np.median(tail)) if tail.size else 0.0
    if rest_med >= high:                 # the continuing data is itself high → flat, not a stale head
        return []
    return list(range(run))


def qc_points(record, card):
    """Mask + flag the leading stale run on each spectrum in place; never deletes, never raises."""
    qc = card.policy.qc or {}
    min_run = int(qc.get("stale_min_run", _DEFAULTS["stale_min_run"]))
    equal_tol = float(qc.get("stale_equal_tol", _DEFAULTS["stale_equal_tol"]))
    high_frac = float(qc.get("stale_high_frac", _DEFAULTS["stale_high_frac"]))
    for sp in record.spectra.values():
        try:
            idx = _leading_stale_run(sp.intensity, min_run=min_run,
                                     equal_tol=equal_tol, high_frac=high_frac)
            for i in idx:
                sp.mask[i] = False
                sp.point_flags.append(scan.PointFlag(
                    int(i), "stale_head",
                    "leading stale point (near-equal, high, low-scatter — HERIX stale-first-points)"))
        except Exception:  # §0.5: a QC failure is a flag-or-skip, never a crashed batch
            sp.spectrum_flags.append(scan.Flag("qc_error", "info", "stale_head check failed"))
    return record
