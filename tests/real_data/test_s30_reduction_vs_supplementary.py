"""Gate C — ISAR's reduction reproduces the beamline's miniscan decisions on real data (§4, §7.3).

The decisive raw-ingestion check: parse the raw `.spec`, group its miniscans, and assert that our
own cosmic-ray rejection reproduces the beamline's irreversible `sum_o`/`5std`/`3std` decisions —
*from the raw passes*, without ever reading the cleaned file. We compare **decisions** (which passes
get rejected), not magnitudes, because the supplementary stores per-point efficiency-scaled counts
(characterized below); the scale cancels in a decision/ratio comparison.

What the real data taught us (and this test pins):
  * `sum_o`  = keep every pass  → reproduced exactly (our σ-clip at n=∞ rejects nothing).
  * `5std`   = mean ± 5σ, sample std (ddof=1) → reproduced **exactly** for all nine analyzers.
  * `3std`   = mean ± 3σ → reproduced exactly for every analyzer **except ana4**, which the beamline
    rejects far more aggressively (an opaque per-detector special-case: it discards counts from
    smooth, outlier-free distributions). We deliberately do NOT mirror that — it is exactly the kind
    of irreversible, unauditable decision ISAR's tracked reduction replaces. The test asserts ana4
    *diverges* so the anomaly is documented, not silently absorbed.
"""
from pathlib import Path

import numpy as np

from isar.parsers import spec_base, spec_s30_raw
from isar.reduce import combine

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "test_data" / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec"
SUPP = REPO / "test_data" / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec_supplementary.dat"
TOL = 1e-4
ANOMALOUS = 4   # ana4: the beamline's aggressive per-detector special-case


def _raw_records():
    card = spec_s30_raw.card_template()
    return {r.scan_number: r for r in spec_s30_raw.parse_file(RAW, card)
            if r.scan_type == "measurement"}


def _supp_blocks():
    _, blocks = spec_base.read_spec(SUPP)
    return {b.scan_number: b for b in blocks}


def _cmp(record, supp_block, k, n_sigma):
    """Per-point (my_rejected, beamline_rejected) booleans for analyzer k at this σ level."""
    ens = record.spectra[f"ana{k}"].miniscan_counts
    sum_all = np.nansum(ens, axis=1)
    reject = combine.reject_sigma(ens, n_sigma)
    kept = np.nansum(np.where(~reject, ens, np.nan), axis=1)
    s_o = supp_block.data[:, spec_base.column_index(supp_block, f"Ana{k}_sum_o")]
    s_n = supp_block.data[:, spec_base.column_index(
        supp_block, f"Ana{k}_{'5std' if n_sigma == 5.0 else '3std'}")]
    n = min(len(sum_all), len(s_o))
    mine = kept[:n] < sum_all[:n] - TOL
    beam = s_n[:n] < s_o[:n] - TOL
    return mine, beam


def test_sum_o_level_keeps_every_pass():
    # n_sigma = ∞ is the sum_o level: nothing is rejected, for every analyzer.
    for rec in _raw_records().values():
        for k in range(1, 10):
            ens = rec.spectra[f"ana{k}"].miniscan_counts
            reject = combine.reject_sigma(ens, np.inf)
            # only NaN padding (if any) may be "rejected"; no real pass is.
            assert not (reject & ~np.isnan(ens)).any()


def test_5std_decisions_reproduced_exactly_for_all_analyzers():
    records, supp = _raw_records(), _supp_blocks()
    mismatches = 0
    for no, rec in records.items():
        for k in range(1, 10):
            mine, beam = _cmp(rec, supp[no], k, 5.0)
            mismatches += int(np.sum(mine != beam))
    assert mismatches == 0


def test_3std_decisions_reproduced_exactly_except_anomalous_ana4():
    records, supp = _raw_records(), _supp_blocks()
    mismatches = 0
    for no, rec in records.items():
        for k in range(1, 10):
            if k == ANOMALOUS:
                continue
            mine, beam = _cmp(rec, supp[no], k, 3.0)
            mismatches += int(np.sum(mine != beam))
    assert mismatches == 0


def test_ana4_3std_diverges_documenting_the_beamline_anomaly():
    # ana4 is rejected more aggressively by the beamline than any clean per-point clip — assert the
    # divergence exists so it is recorded, not mistaken for a parser bug.
    records, supp = _raw_records(), _supp_blocks()
    divergences = 0
    for no, rec in records.items():
        mine, beam = _cmp(rec, supp[no], ANOMALOUS, 3.0)
        # every divergence here is the beamline rejecting where our statistics say keep.
        divergences += int(np.sum(beam & ~mine))
    assert divergences > 0


def test_efficiency_scale_clusters_near_unity():
    # The supplementary stores per-point efficiency-scaled counts; characterize the scale rather
    # than assume 1.0. On real data it is a tight band about unity.
    records, supp = _raw_records(), _supp_blocks()
    scales = []
    for no, rec in records.items():
        for k in range(1, 10):
            ens = rec.spectra[f"ana{k}"].miniscan_counts
            sum_all = np.nansum(ens, axis=1)
            s_o = supp[no].data[:, spec_base.column_index(supp[no], f"Ana{k}_sum_o")]
            n = min(len(sum_all), len(s_o))
            nz = sum_all[:n] > 0
            scales.append(s_o[:n][nz] / sum_all[:n][nz])
    scale = np.concatenate(scales)
    assert 0.98 < np.median(scale) < 1.02
    assert np.all((scale > 0.95) & (scale < 1.05))   # no pathological points in this fixture
