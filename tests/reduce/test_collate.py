"""Deterministic collation (§7.5): organize reduced spectra into a catalogue by (sample, Q, T), map
coverage + gaps, and roll up what was flagged/removed. No LLM. S30 keeps duplicates separate; the
combine-before-divide primitive (for IN8/future merges) sums counts AND monitors, then divides.
"""
import json

import numpy as np
import pytest

from isar.parsers import ill_in8, spec_s30_raw
from isar.reduce import collate, combine
from isar.synth_format import quirks, run_builder


def _in8_records(run, tmp_path):
    card = ill_in8.card_template()
    path = tmp_path / f"{run.experiment_id}.numor"
    path.write_bytes(ill_in8.emit_file(run, card))
    return ill_in8.parse_file(path, card), card


def _reduced_measurements(run, card, tmp_path):
    """Emit -> parse -> reduce a synthetic raw run; return its reduced measurement records."""
    path = tmp_path / f"{run.experiment_id}.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    records = spec_s30_raw.parse_file(path, card)
    measurements = [r for r in records if r.scan_type == "measurement"]
    for rec in measurements:
        combine.reduce_record(rec, card)
    return measurements


def test_collate_groups_spectra_by_rounded_q_and_temperature(tmp_path):
    card = spec_s30_raw.card_template()
    # Two scans share the same 9-analyzer Q fan and T -> nine groups, two members each.
    run = run_builder.build_s30_raw_run("COLL", n_scans=2, seed0=5, include_ascans=True)
    records = _reduced_measurements(run, card, tmp_path)

    col = collate.collate(records, card)
    assert len(col.groups) == 9                       # nine distinct analyzer Qs
    assert all(len(g.members) == 2 for g in col.groups)   # one per scan
    # every member carries its identity + rounded key
    g0 = col.groups[0]
    assert {m.scan_number for m in g0.members} == {1, 2}
    assert all(m.detector_id == g0.members[0].detector_id for m in g0.members)
    assert g0.merge == "separate"                     # S30 keeps duplicates separate


def test_collate_excludes_alignment_and_calibration_scans(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("COLL", n_scans=1, seed0=5, include_ascans=True)
    # pass ALL parsed records (incl. ascans), not just measurements
    path = tmp_path / "COLL.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    all_records = spec_s30_raw.parse_file(path, card)
    for r in all_records:
        if r.scan_type == "measurement":
            combine.reduce_record(r, card)
    col = collate.collate(all_records, card)
    # only the measurement scan's spectra are catalogued
    assert all(m.scan_type == "measurement" for g in col.groups for m in g.members)


def test_detect_gaps_finds_a_hole_in_the_coverage():
    # regular spacing 0.1 with one missing point between 2.2 and 2.5
    values = [2.0, 2.1, 2.2, 2.5, 2.6, 2.7]
    gaps = collate.detect_gaps(values)
    assert len(gaps) == 1
    lo, hi = gaps[0]
    assert np.isclose(lo, 2.2) and np.isclose(hi, 2.5)


def test_collate_coverage_lists_measured_q_component(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("COV", n_scans=1, seed0=5, include_ascans=False)
    records = _reduced_measurements(run, card, tmp_path)
    col = collate.collate(records, card)
    # h is the varying component of the analyzer fan (2.05..2.45)
    assert col.coverage.component == "h"
    assert min(col.coverage.values) < 2.1 and max(col.coverage.values) > 2.4
    assert col.coverage.gaps == []                    # the fan is regularly spaced


def test_collate_rollup_counts_the_injected_cosmic_ray(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("ROLL", n_scans=1, seed0=11, include_ascans=False)
    sc = run.scans[0]
    d = next(x for x in sc.detectors if x.detector_id == "ana5")
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, "ana5", p0, 7, magnitude=1.0e4)
    records = _reduced_measurements(run, card, tmp_path)

    col = collate.collate(records, card)
    assert col.rollup.passes_rejected == 1
    assert col.rollup.points_with_rejection == 1
    assert col.rollup.counts_removed > 9000           # ~1e4 injected
    assert col.rollup.point_flags.get("cosmic_ray_rejected", 0) == 1


def test_s30_separate_groups_have_no_merged_spectrum(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("SEP", n_scans=2, seed0=5, include_ascans=False)
    col = collate.collate(_reduced_measurements(run, card, tmp_path), card)
    assert col.merge == "separate"
    assert all(g.merged is None for g in col.groups)   # separate = catalogue only, no physical combine


def test_in8_merge_sums_repeat_counts_and_monitors(tmp_path):
    # Two IN8 scans at the same (Q, T) on the same grid -> one group whose merged spectrum is the
    # point-wise sum of counts AND monitors (combine-before-divide, raw counts kept; §7.5).
    run = run_builder.build_in8_run("DIR-M", n_scans=2, seed0=7)
    records, card = _in8_records(run, tmp_path)
    col = collate.collate(records, card)
    assert col.merge == "merge"
    assert len(col.groups) == 1
    g = col.groups[0]
    assert g.merged is not None
    s0, s1 = records[0].spectra["det0"], records[1].spectra["det0"]
    assert np.allclose(g.merged.intensity, s0.intensity + s1.intensity)
    assert np.allclose(g.merged.variance, s0.intensity + s1.intensity)   # variance = ΣN (Poisson)
    assert np.allclose(g.merged.monitor,
                       records[0].normalization.monitor + records[1].normalization.monitor)
    assert {m[1] for m in g.merged.members} == {55000, 55001}            # both numors listed


def test_in8_t_round_groups_drifting_setpoints(tmp_path):
    # Same Q, header RT drifting 54.6 vs 55.2 -> grouped by the card's t_round=5 K bin.
    run = run_builder.build_in8_run("DIR-T", n_scans=2, seed0=7)
    run.scans[0].temperature_setpoint = 54.6
    run.scans[1].temperature_setpoint = 55.2
    records, card = _in8_records(run, tmp_path)
    col = collate.collate(records, card)
    assert len(col.groups) == 1                          # both fall in the same T bin
    assert col.groups[0].merged.n_members == 2


def test_merge_before_divide_sums_counts_and_monitors_then_divides():
    # The IN8/future merge primitive: never average rates — add N, add M, divide once (no floor).
    N = [np.array([2.0, 0.0]), np.array([3.0, 0.0])]
    M = [np.array([10.0, 10.0]), np.array([20.0, 5.0])]
    intensity, variance, mask = collate.merge_before_divide(N, M)
    assert np.isclose(intensity[0], 5.0 / 30.0)       # (2+3)/(10+20)
    assert np.isclose(variance[0], 5.0 / 30.0 ** 2)   # honest Poisson, no floor
    assert intensity[1] == 0.0 and variance[1] == 0.0 and bool(mask[1]) is True


def test_persist_merged_spectrum_is_content_addressed_and_keeps_zero_uncertainty(tmp_path):
    run = run_builder.build_in8_run("DIR-P", n_scans=2, seed0=7)
    records, card = _in8_records(run, tmp_path)
    merged = collate.collate(records, card).groups[0].merged
    data_dir = tmp_path / "workspace" / "data"

    first = collate.persist_merged_spectrum(data_dir, merged)
    second = collate.persist_merged_spectrum(data_dir, merged)
    assert first == second and first.startswith("data/pooled/")
    payload = json.loads((data_dir.parent / first).read_text(encoding="utf-8"))
    assert payload["schema"] == "isar.pooled_spectrum/1"
    assert payload["members"] == [list(member) for member in merged.members]
    assert payload["variance"] == np.asarray(merged.variance, dtype=float).tolist()


def test_master_consistency_bootstrap_is_seeded_zero_safe_and_detects_drift():
    monitors = np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]])
    homogeneous = np.array([[10.0, 0.0, 20.0], [20.0, 0.0, 40.0]])
    first = collate._member_consistency(
        homogeneous, monitors, seed=12345, alpha=0.01, bootstrap_samples=256)
    second = collate._member_consistency(
        homogeneous, monitors, seed=12345, alpha=0.01, bootstrap_samples=256)
    assert first == second
    assert np.isfinite(first[0]) and first[2] is True

    drifted = np.array([[10.0, 0.0, 20.0], [200.0, 0.0, 1.0]])
    statistic, pvalue, consistent = collate._member_consistency(
        drifted, monitors, seed=12345, alpha=0.01, bootstrap_samples=256)
    assert statistic > first[0] and pvalue < 0.01 and consistent is False


def test_master_merge_applies_qc_masks_without_deleting_energy_bins(tmp_path):
    run = run_builder.build_in8_run("DIR-MASK", n_scans=2, seed0=7)
    records, card = _in8_records(run, tmp_path)
    records[0].spectra["det0"].mask[0] = False
    expected = float(records[1].spectra["det0"].intensity[0])
    merged = collate.collate(records, card).groups[0].merged
    assert merged.intensity[0] == pytest.approx(expected)
    assert len(merged.energy) == len(records[0].spectra["det0"].intensity)
