"""σ-calibration application (WORKING — LLM_HARNESS_DESIGN §5, milestone 0).

ISAR's per-scan intervals are statistical-only and measured **overtight** (the human value lands
inside ISAR's 68% interval only ~20% of the time). Before any convergence decision, the driver
inflates ISAR's interval by a calibration derived on TAVI, where the true ω₀ is known. That
calibration is produced by ``isar score --truth --json`` (:func:`isar.analyze.truth.report_to_dict`)
and consumed here.

The application is deliberately trivial and explicit::

    calibrated_halfwidth = inflation * (interval_hi - interval_lo) / 2

**Load-bearing guard.** This function takes ``interval_lo``/``interval_hi`` EXPLICITLY and refuses a
``prior_sigma`` (or ``sigma``) input. ISAR's ``prior_for(...)`` floors its returned sigma at 0.3 meV
by design (it is an IC-seeding prior, ``UNIFICATION_DESIGN §5``); feeding that floor to the
convergence test makes a sub-0.3 meV objective unreachable by construction. The prior is for
*seeding*; the per-scan interval is for *deciding done* — so the calibrated half-width is built only
from the interval, never from a prior sigma.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Calibration:
    """A loaded σ-calibration: a global inflation factor plus an optional per-bucket table.

    ``inflation`` is ``k`` such that 68% of ``|ω_true − ω_ISAR|`` fall within ``k·halfwidth``. A bucket
    (e.g. by ``verdict`` or count rate) overrides the global ``k`` when present and non-null.
    """

    inflation: float | None
    coverage68: float | None = None
    center_pull_mean: float | None = None
    bucket_by: str | None = None
    # label -> {"inflation": k|None, "coverage68": .., "center_pull_mean": .., "n": ..}
    buckets: dict[str, dict[str, Any]] = field(default_factory=dict)
    truth_source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Calibration":
        buckets = {}
        for b in d.get("buckets") or []:
            label = str(b.get("label"))
            buckets[label] = {
                "inflation": b.get("inflation"),
                "coverage68": b.get("coverage68"),
                "center_pull_mean": b.get("center_pull_mean"),
                "n": b.get("n"),
            }
        return cls(
            inflation=d.get("inflation"),
            coverage68=d.get("coverage68"),
            center_pull_mean=d.get("center_pull_mean"),
            bucket_by=d.get("bucket_by"),
            buckets=buckets,
            truth_source=d.get("truth_source", ""),
            raw=dict(d),
        )

    def inflation_for(self, bucket: str | None = None) -> float:
        """The inflation factor to use, preferring a bucket's ``k`` when available.

        Falls back to the global ``k``. Raises if neither the bucket nor the global factor supplies a
        usable (positive, finite) number — the caller must not silently proceed uncalibrated.
        """
        k = None
        if bucket is not None and bucket in self.buckets:
            k = self.buckets[bucket].get("inflation")
        if k is None:
            k = self.inflation
        if k is None:
            raise ValueError(
                "calibration has no inflation factor"
                + (f" for bucket {bucket!r} and no global factor" if bucket is not None else "")
                + " — cannot calibrate an interval (re-run `isar score --truth --json`)"
            )
        k = float(k)
        if not (k > 0.0) or k != k:  # non-positive or NaN
            raise ValueError(f"calibration inflation factor {k!r} is not a positive finite number")
        return k

    def calibrated_halfwidth(self, interval_lo, interval_hi, *, bucket: str | None = None,
                             **forbidden) -> float:
        """The calibrated σ(ω₀) half-width = ``inflation * (interval_hi - interval_lo) / 2``.

        Takes the per-scan MINOS interval bounds EXPLICITLY. Passing ``prior_sigma``/``sigma`` (or any
        other keyword) raises: the prior sigma floors at 0.3 meV and must never enter the convergence
        test — see this module's docstring. ``interval_lo``/``interval_hi`` must both be present and
        ordered.
        """
        _reject_prior_sigma(forbidden)
        if interval_lo is None or interval_hi is None:
            raise ValueError("both interval_lo and interval_hi are required (no prior-sigma fallback)")
        lo, hi = float(interval_lo), float(interval_hi)
        if hi < lo:
            raise ValueError(f"interval_hi ({hi}) < interval_lo ({lo})")
        raw_halfwidth = (hi - lo) / 2.0
        return self.inflation_for(bucket) * raw_halfwidth


def _reject_prior_sigma(forbidden: dict[str, Any]) -> None:
    if forbidden:
        bad = ", ".join(sorted(forbidden))
        raise TypeError(
            f"calibrated_halfwidth() got unexpected keyword(s): {bad}. The σ-calibration is applied to "
            "the per-scan interval only — pass interval_lo/interval_hi, never a prior_sigma "
            "(it floors at 0.3 meV by design; see LLM_HARNESS_DESIGN §4 WARNING)."
        )


def load_calibration(path: str | Path) -> Calibration:
    """Load a σ-calibration JSON (the ``isar score --truth --json`` output) into a :class:`Calibration`."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        raise ValueError(f"{path}: calibration JSON must be an object, got {type(d).__name__}")
    return Calibration.from_dict(d)
