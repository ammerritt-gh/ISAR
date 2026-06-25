"""Cosmic-ray rejection + combine-before-divide (§7.3, §7.5, D3).

ISAR replicates the beamline's miniscan rejection itself — tracked and reversible — and adds a
Poisson-aware rule that refuses to nuke the legitimate low-count fluctuations the blind 3-sigma clip
discards. These unit tests pin both rules and the combine arithmetic (sum kept counts AND kept
monitors, THEN divide; honest zero variance, no floor).
"""
from pathlib import Path
import json

import numpy as np

from isar.reduce import combine

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "test_data" / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec"


# ---------------------------------------------------------------------------
# Beamline mean ± Nσ clip
# ---------------------------------------------------------------------------
def test_reject_sigma_flags_a_clear_outlier():
    counts = np.array([[0.0] * 14 + [18.0]])
    reject = combine.reject_sigma(counts, 3.0)
    assert reject.shape == (1, 15)
    assert bool(reject[0, 14]) is True
    assert reject[0, :14].sum() == 0           # the fourteen zeros are kept


def test_reject_sigma_inf_rejects_nothing_reproducing_sum_o():
    # n_sigma = inf is the sum_o level: keep every pass.
    counts = np.array([[0.0] * 14 + [29.0]])
    assert not combine.reject_sigma(counts, np.inf).any()


def test_reject_sigma_5_keeps_what_3_rejects_on_a_single_count():
    # The over-aggressive beamline 3-sigma nukes a lone count; 5-sigma keeps it (both observed real).
    counts = np.array([[0.0] * 14 + [1.0]])
    assert bool(combine.reject_sigma(counts, 3.0)[0, 14]) is True
    assert bool(combine.reject_sigma(counts, 5.0)[0, 14]) is False


def test_reject_sigma_marks_nan_padding_rejected():
    counts = np.array([[5.0, 5.0, 5.0, np.nan, np.nan]])
    reject = combine.reject_sigma(counts, 3.0)
    assert bool(reject[0, 3]) and bool(reject[0, 4])     # padding is not a real pass
    assert reject[0, :3].sum() == 0


# ---------------------------------------------------------------------------
# ISAR Poisson-aware rule — the deliberate divergence from blind 3σ
# ---------------------------------------------------------------------------
def test_reject_poisson_keeps_single_count_at_an_empty_point():
    # [0×14, 1]: the others are sub-level, so nothing is rejected — the opposite of 3-sigma.
    counts = np.array([[0.0] * 14 + [1.0]])
    assert not combine.reject_poisson(counts, k=5.0, min_level=10.0).any()


def test_reject_poisson_keeps_a_lone_large_count_at_an_empty_point():
    # [0×14, 29]: still sub-level others → ISAR keeps it (flag-for-adjudication, not auto-delete).
    counts = np.array([[0.0] * 14 + [29.0]])
    assert not combine.reject_poisson(counts, k=5.0, min_level=10.0).any()


def test_reject_poisson_rejects_a_true_cosmic_ray_on_a_real_peak():
    # A genuine ~100-count-per-pass peak with one 5000 spike: that spike IS a cosmic ray.
    counts = np.array([[100.0] * 14 + [5000.0]])
    reject = combine.reject_poisson(counts, k=5.0, min_level=10.0)
    assert bool(reject[0, 14]) is True
    assert reject[0, :14].sum() == 0


# ---------------------------------------------------------------------------
# combine-before-divide (§7.5), no zero-count floor (D3)
# ---------------------------------------------------------------------------
def test_combine_spectrum_sums_kept_passes_into_raw_counts():
    counts = np.array([[2.0, 3.0, 0.0]])
    reject = np.zeros((1, 3), dtype=bool)
    monitor = np.array([[10.0, 10.0, 10.0]])
    I, V, mask = combine.combine_spectrum(counts, reject, monitor)
    assert bool(mask[0]) is True
    assert np.isclose(I[0], 5.0)               # N = 2+3 raw counts — NOT divided by the monitor
    assert np.isclose(V[0], 5.0)               # honest Poisson variance = N (σ=√N)


def test_combine_spectrum_zero_counts_give_honest_zero_variance():
    counts = np.zeros((1, 3))
    I, V, mask = combine.combine_spectrum(counts, np.zeros((1, 3), bool), np.full((1, 3), 10.0))
    assert I[0] == 0.0 and V[0] == 0.0 and bool(mask[0]) is True   # D3: zero, never a floor


def test_combine_spectrum_zero_monitor_masks_the_point_but_keeps_the_count():
    # No flux → masked (normalization undefined), but the raw count is shown, not hidden (§0.4).
    I, V, mask = combine.combine_spectrum(
        np.array([[5.0, 5.0]]), np.zeros((1, 2), bool), np.zeros((1, 2)))
    assert bool(mask[0]) is False and I[0] == 10.0


def test_combine_per_analyzer_reject_changes_the_kept_counts():
    # Different per-analyzer reject masks → different kept raw counts (the signal, not a ratio).
    counts = np.array([[10.0, 10.0, 10.0]])
    monitor = np.array([[4.0, 4.0, 4.0]])
    NA, _, _ = combine.combine_spectrum(counts, np.zeros((1, 3), bool), monitor)
    NB, _, _ = combine.combine_spectrum(counts, np.array([[False, False, True]]), monitor)
    assert np.isclose(NA[0], 30.0)             # keep all three passes
    assert np.isclose(NB[0], 20.0)             # drop one pass


def test_combine_spectrum_nan_padding_excluded():
    counts = np.array([[4.0, 6.0, np.nan]])
    monitor = np.array([[10.0, 10.0, np.nan]])
    I, _, mask = combine.combine_spectrum(counts, np.zeros((1, 3), bool), monitor)
    assert bool(mask[0]) and np.isclose(I[0], 10.0)   # padding ignored; N = 4+6 raw counts


# ---------------------------------------------------------------------------
# reduce_record — wire a parsed raw record through the chosen rule
# ---------------------------------------------------------------------------
def test_reduce_record_fills_combined_signal_and_reject_mask():
    from isar.parsers import spec_s30_raw
    from isar.schema import scan

    card = spec_s30_raw.card_template()
    rec = next(r for r in spec_s30_raw.parse_file(RAW, card) if r.scan_type == "measurement")
    assert all(np.all(sp.intensity == 0.0) for sp in rec.spectra.values())   # unreduced pre

    combine.reduce_record(rec, card)

    for sp in rec.spectra.values():
        assert sp.miniscan_reject is not None
        assert sp.miniscan_reject.shape == sp.miniscan_counts.shape
        assert sp.miniscan_reject.dtype == np.bool_
        assert sp.miniscan_reject_source == "poisson"
        assert not any(f.type == "unreduced" for f in sp.spectrum_flags)
        assert sp.intensity.shape == rec.normalization.monitor.shape
        assert np.all(np.isfinite(sp.intensity)) and np.all(np.isfinite(sp.variance))

    # Still survives the canonical JSON store after reduction (2-D reject mask included).
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)


def test_reduce_record_sigma_clip_rule_selectable():
    from isar.parsers import spec_s30_raw

    card = spec_s30_raw.card_template()
    card.policy.reduction = {"rule": "sigma_clip", "sigma_n": 3.0}
    rec = next(r for r in spec_s30_raw.parse_file(RAW, card) if r.scan_type == "measurement")
    combine.reduce_record(rec, card)
    assert rec.spectra["ana1"].miniscan_reject_source == "sigma_clip"
