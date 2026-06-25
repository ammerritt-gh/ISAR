"""Tracked, reversible cosmic-ray rejection + combine-before-divide for the raw S30 miniscans.

The raw parser keeps every acquisition pass as a `[nP, n_mini]` ensemble; this module decides which
passes to reject and combines the survivors. Two rules, card-selectable:

* `reject_sigma` — the beamline's mean ± Nσ clip. `n_sigma=inf` keeps everything (the `sum_o`
  level); finite N reproduces the `Nstd` columns. Used to *validate* against the supplementary
  (Gate C). It is blind to Poisson statistics, so at low counts it nukes legitimate fluctuations
  (e.g. a lone count at an otherwise-empty point → discarded).

* `reject_poisson` — ISAR's default. Leave-one-out: a pass is a cosmic ray only if it exceeds the
  *other* passes by `k·√level` AND those others are themselves at a non-trivial level
  (`≥ min_level`). At near-empty points it rejects nothing — it refuses to delete what it cannot
  distinguish from a weak real feature, leaving that call to later adjudication (§8).

Rejection only ever *marks* passes (`miniscan_reject`); nothing is deleted (§0.4). `combine_spectrum`
sums kept counts AND kept monitors, THEN divides (§7.5), and stores honest Poisson variance with no
zero-count floor (D3). NaN entries in the ensemble are padding (a truncated final group) and are
always excluded.
"""
from __future__ import annotations

import numpy as np

from isar.schema import scan


def reject_sigma(counts_2d, n_sigma, *, ddof: int = 1) -> np.ndarray:
    """Per-point mean ± `n_sigma`·std clip → `[nP, n_mini]` bool reject mask (True = rejected).

    `n_sigma=inf` rejects nothing (the `sum_o` level). NaN padding is always rejected. `ddof` selects
    the population (0) vs sample (1) std; Gate C pins **ddof=1** — the sample std reproduces the
    beamline's `5std` decisions exactly (and `3std` for every analyzer except the anomalous ana4).
    """
    c = np.asarray(counts_2d, dtype="float64")
    valid = ~np.isnan(c)
    with np.errstate(invalid="ignore"):
        mu = np.nanmean(np.where(valid, c, np.nan), axis=1, keepdims=True)
        sd = np.nanstd(np.where(valid, c, np.nan), axis=1, ddof=ddof, keepdims=True)
        within = np.abs(c - mu) <= n_sigma * sd
    keep = valid & (within | (sd == 0))      # zero spread → keep every real pass
    return ~keep


def reject_poisson(counts_2d, *, k: float, min_level: float) -> np.ndarray:
    """Leave-one-out Poisson outlier test → `[nP, n_mini]` bool reject mask (True = rejected).

    Pass i is rejected iff the mean of the *other* valid passes is at least `min_level` AND
    `c_i − μ_others > k·√max(μ_others, 1)`. Near-empty points (others below `min_level`) reject
    nothing — the deliberate divergence from the blind σ-clip. NaN padding is always rejected.
    """
    c = np.asarray(counts_2d, dtype="float64")
    valid = ~np.isnan(c)
    nvalid = valid.sum(axis=1, keepdims=True)
    total = np.nansum(np.where(valid, c, 0.0), axis=1, keepdims=True)
    with np.errstate(invalid="ignore"):
        mu_others = (total - np.where(valid, c, 0.0)) / np.maximum(nvalid - 1, 1)
        excess = c - mu_others
        thresh = k * np.sqrt(np.maximum(mu_others, 1.0))
        is_ray = valid & (mu_others >= min_level) & (excess > thresh)
    return is_ray | ~valid


def combine_spectrum(counts_2d, reject_2d, monitor_2d):
    """Combine surviving passes (§7.5): sum kept counts AND kept monitors, THEN divide.

    Returns `(intensity[nP], variance[nP], mask[nP])`. `intensity = N/M`, `variance = N/M²` with no
    zero-count floor (D3); a point whose kept monitor sums to zero is masked (kept in the array as
    0, never deleted, §0.4). The reject mask is per-analyzer, so the same shared monitor yields
    different denominators for different analyzers — physically correct.
    """
    c = np.asarray(counts_2d, dtype="float64")
    rej = np.asarray(reject_2d, dtype=bool)
    mon = np.asarray(monitor_2d, dtype="float64")
    keep = (~rej) & (~np.isnan(c)) & (~np.isnan(mon))
    N = np.where(keep, c, 0.0).sum(axis=1)
    M = np.where(keep, mon, 0.0).sum(axis=1)
    valid = M != 0
    intensity = np.zeros_like(N)
    variance = np.zeros_like(N)
    np.divide(N, M, out=intensity, where=valid)
    np.divide(N, M * M, out=variance, where=valid)
    return intensity, variance, valid


def _reject_for(counts, reduction: dict):
    """Pick and run the configured rejection rule; return (reject_mask, source_label)."""
    rule = (reduction or {}).get("rule", "poisson")
    if rule == "sigma_clip":
        return reject_sigma(counts, float(reduction.get("sigma_n", 3.0))), "sigma_clip"
    return (reject_poisson(counts, k=float(reduction.get("poisson_k", 5.0)),
                           min_level=float(reduction.get("poisson_min_level", 10.0))),
            "poisson")


def reduce_record(record, card):
    """Combine every miniscan-bearing spectrum in `record` in place (cosmic-ray reject → divide).

    For each enabled raw spectrum: run the card's rule, store the reject mask + its source, combine
    to `intensity/variance/mask`, append a `cosmic_ray_rejected` point flag (count + magnitude) per
    point where a real pass was rejected, and drop the `unreduced` flag. Spectra without a miniscan
    ensemble (e.g. the supplementary path, or ascans) are left untouched. Returns the record.

    The record-level `normalization.monitor/time` are the full per-point sums (total flux), left as
    the parser set them; the per-analyzer effective denominators live inside each spectrum's
    intensity/variance because rejection is per-analyzer (downstream collation must recompute from
    the per-spectrum ensemble, not the record-level monitor).
    """
    mini_monitor = record.normalization.miniscan_monitor
    for sp in record.spectra.values():
        if sp.miniscan_counts is None:
            continue
        counts = sp.miniscan_counts
        reject, source = _reject_for(counts, card.policy.reduction)
        intensity, variance, valid = combine_spectrum(counts, reject, mini_monitor)
        sp.intensity = intensity
        sp.variance = variance
        sp.mask = valid.astype(bool)
        sp.miniscan_reject = reject
        sp.miniscan_reject_source = source

        rejected_real = reject & ~np.isnan(counts)
        sp.point_flags = [f for f in sp.point_flags if f.type != "cosmic_ray_rejected"]
        for i in np.nonzero(rejected_real.any(axis=1))[0]:
            n_rej = int(rejected_real[i].sum())
            magnitude = float(np.nansum(np.where(rejected_real[i], counts[i], 0.0)))
            sp.point_flags.append(scan.PointFlag(
                int(i), "cosmic_ray_rejected",
                f"{n_rej} pass(es) rejected ({source}); rejected counts={magnitude:g}"))
        sp.spectrum_flags = [f for f in sp.spectrum_flags if f.type != "unreduced"]
    return record
