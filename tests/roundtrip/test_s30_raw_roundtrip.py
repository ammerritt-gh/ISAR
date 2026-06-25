"""Gate A' — synthetic raw round-trip (§4, raw ingestion): build → emit → parse → reduce.

The raw-ingestion analogue of Gate A. It proves (a) the emitter and raw parser are consistent
inverses for the full per-miniscan ensemble, and (b) ISAR's tracked rejection rejects an injected
cosmic ray — and *only* that pass — then recovers the clean combined truth. Together with Gate C
(reproduce the beamline on real data) this is the pair the plan requires.
"""
import numpy as np

from isar.parsers import spec_s30_raw
from isar.reduce import combine
from isar.synth_format import quirks, run_builder


def _emit_parse(run, card, tmp_path):
    path = tmp_path / f"{run.experiment_id}.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    return spec_s30_raw.parse_file(path, card)


def test_raw_emit_parse_preserves_miniscan_ensemble(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("SYN_RAW", n_scans=1, seed0=7)
    records = _emit_parse(run, card, tmp_path)
    fscans = [r for r in records if r.scan_type == "measurement"]
    assert len(fscans) == 1
    rec, sc = fscans[0], run.scans[0]
    energy = next(a for a in rec.scan_axes if a.role == "scanned").values
    assert np.allclose(energy, sc.energy)
    for d in sc.detectors:
        parsed = rec.spectra[d.detector_id].miniscan_counts
        assert parsed.shape == d.miniscan_counts.shape
        assert np.allclose(parsed, d.miniscan_counts)        # the ensemble round-trips exactly
        assert np.allclose(rec.spectra[d.detector_id].q, d.q)
    assert np.allclose(rec.normalization.miniscan_monitor, sc.miniscan_monitor)


def test_raw_emit_includes_classified_ascans(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("SYN_RAW", n_scans=1, seed0=7, include_ascans=True)
    records = _emit_parse(run, card, tmp_path)
    assert sorted(r.scan_type for r in records) == ["alignment", "calibration", "measurement"]


def test_injected_cosmic_ray_rejected_and_only_it_poisson(tmp_path):
    card = spec_s30_raw.card_template()                       # default rule = poisson
    run = run_builder.build_s30_raw_run("SYN_CR", n_scans=1, seed0=11, include_ascans=False)
    sc = run.scans[0]
    det = "ana5"
    d = next(x for x in sc.detectors if x.detector_id == det)
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))       # peak point (highest per-pass level)
    m0 = 7
    clean = d.miniscan_counts.copy()
    quirks.inject_miniscan_cosmic_ray(sc, det, p0, m0, magnitude=1.0e4)

    rec = next(r for r in _emit_parse(run, card, tmp_path) if r.scan_type == "measurement")
    combine.reduce_record(rec, card)
    sp = rec.spectra[det]

    assert bool(sp.miniscan_reject[p0, m0]) is True           # the ray is caught
    assert int(sp.miniscan_reject.sum()) == 1                 # ...and nothing else (no false +)
    assert any(f.type == "cosmic_ray_rejected" and f.index == p0 for f in sp.point_flags)

    # The reduced intensity equals combining every pass EXCEPT the rejected one (ray removed).
    mon = rec.normalization.miniscan_monitor
    keep = np.ones_like(clean, dtype=bool)
    keep[p0, m0] = False
    expected = np.where(keep, clean, 0.0).sum(axis=1) / np.where(keep, mon, 0.0).sum(axis=1)
    assert np.allclose(sp.intensity, expected)

    # Sanity that the test bites: without rejection the 1e4-count ray dominates the point's rate.
    naive = (clean.sum(axis=1) + np.where(np.arange(clean.shape[0]) == p0, 1.0e4, 0.0)) \
        / mon.sum(axis=1)
    assert naive[p0] > 5 * sp.intensity[p0]


def test_injected_cosmic_ray_rejected_under_sigma_clip(tmp_path):
    card = spec_s30_raw.card_template()
    card.policy.reduction = {"rule": "sigma_clip", "sigma_n": 3.0}
    run = run_builder.build_s30_raw_run("SYN_CR2", n_scans=1, seed0=13, include_ascans=False)
    sc = run.scans[0]
    det = "ana3"
    d = next(x for x in sc.detectors if x.detector_id == det)
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, det, p0, 5, magnitude=1.0e4)

    rec = next(r for r in _emit_parse(run, card, tmp_path) if r.scan_type == "measurement")
    combine.reduce_record(rec, card)
    assert bool(rec.spectra[det].miniscan_reject[p0, 5]) is True
    assert rec.spectra[det].miniscan_reject_source == "sigma_clip"
