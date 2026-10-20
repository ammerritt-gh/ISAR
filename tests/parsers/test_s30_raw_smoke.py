"""Gate B' — file smoke for the RAW S30 parser (§4.1, raw ingestion).

The supplementary Gate B parses the beamline-cleaned file; this parses the *raw* `.spec` with its
individual miniscans, on the committed artificial copy (`artificial_data/`: the facility's own
layout, every data-row number replaced; values asserted against its truth file). It asserts the
record is sane (not yet exact truth — that is Gate C):

  * detect() tells raw apart from supplementary by `#L` content (distinct format_version);
  * each fscan becomes a 9-spectrum *measurement* record carrying the full `[nP, 15]` miniscan
    ensemble + per-miniscan monitor/time, still *unreduced* (intensity 0, flagged), so cosmic-ray
    rejection is a later, reversible step (§0.4);
  * ascans are classified alignment/calibration with their scanned motor captured (§5.2);
  * the 2-D ensemble survives the canonical JSON store.
"""
from itertools import groupby
import json

import numpy as np

from artificial import RAW, RAW_SUPP, scan_truth, tokens, truth
from isar.parsers import registry, spec_s30_raw
from isar.schema import axes, scan

TRUTH = truth(RAW)
FSCANS = [49, 50, 53]
ASCANS = {1: "calibration", 3: "alignment", 4: "alignment", 5: "alignment"}


def _strictly_monotonic(a) -> bool:
    d = np.diff(a)
    return bool(np.all(d > 0) or np.all(d < 0))


def _stack(toks: list[str], runs: list[int]) -> np.ndarray:
    """The truth column cut into the miniscan runs the copy tool wrote (equal-energy tokens)."""
    out, i = np.full((len(runs), max(runs)), np.nan), 0
    for k, n in enumerate(runs):
        out[k, :n] = [float(t) for t in toks[i:i + n]]
        i += n
    return out


def _runs(s) -> list[int]:
    return [len(list(g)) for _, g in groupby(tokens(s, "HerixE"))]


def test_truth_lists_the_scans_these_tests_rely_on():
    assert [s["scan_number"] for s in TRUTH["scans"]] == sorted([*ASCANS, *FSCANS])
    assert all(s["points"] == 300 for s in TRUTH["scans"] if s["scan_number"] in FSCANS)


def test_detect_distinguishes_raw_from_supplementary():
    # Both are S30 SPEC files; the #L content (raw has bare AnaN, supp has AnaN_sum_o) splits them.
    assert registry.detect(RAW) == ("APS", "S30", "2025-raw")
    assert registry.detect(RAW_SUPP) == ("APS", "S30", "2025")


def test_fscans_become_nine_spectrum_measurement_records_with_miniscan_ensemble():
    card = spec_s30_raw.card_template()
    records = spec_s30_raw.parse_file(RAW, card)
    fscans = [r for r in records if r.scan_type == "measurement"]
    assert [r.scan_number for r in fscans] == FSCANS
    for rec in fscans:
        s = scan_truth(RAW, rec.scan_number)
        runs = _runs(s)
        assert max(runs) == 15 and len(set(runs)) == 1           # 15 miniscans per energy point
        assert set(rec.spectra) == {f"ana{i}" for i in range(1, 10)}
        assert rec.scan_type_source == "inferred"

        axes.assert_constant_q_energy(rec)
        energy = axes.scanned_axes(rec)[0].values
        assert _strictly_monotonic(energy)
        nP = len(energy)
        assert nP == len(runs) == s["points"] // 15

        for sp in rec.spectra.values():
            assert sp.miniscan_counts.shape == (nP, 15)
            assert np.all(np.isfinite(sp.miniscan_counts))
            assert sp.miniscan_reject is None        # rejection is the later reduce step
            assert np.all(sp.intensity == 0.0)       # unreduced: no intensity yet
            assert any(f.type == "unreduced" for f in sp.spectrum_flags)
            assert sp.q_source == "reported" and len(sp.q) == 3

        # Per-miniscan monitor/time are shared across the nine analyzers (ICO-C is one column).
        assert rec.normalization.miniscan_monitor.shape == (nP, 15)
        assert rec.normalization.miniscan_time.shape == (nP, 15)

        assert rec.source.plugin == "spec_s30_raw"
        assert rec.source.format_version == "2025-raw"


def test_every_miniscan_value_is_the_one_written_in_its_column():
    """Wrong column names or a mis-grouped miniscan ensemble put other numbers in the arrays."""
    card = spec_s30_raw.card_template()
    by_no = {r.scan_number: r for r in spec_s30_raw.parse_file(RAW, card)}
    pol = card.policy
    for n in FSCANS:
        rec, s = by_no[n], scan_truth(RAW, n)
        runs = _runs(s)
        first = np.cumsum([0, *runs[:-1]])
        assert np.allclose(axes.scanned_axes(rec)[0].values,
                           [float(tokens(s, pol.energy_column)[i]) for i in first])
        assert np.allclose(rec.normalization.miniscan_monitor, _stack(tokens(s, pol.monitor_column), runs))
        assert np.allclose(rec.normalization.miniscan_time, _stack(tokens(s, pol.time_column), runs))
        for det in pol.detectors:
            assert np.allclose(rec.spectra[det.detector_id].miniscan_counts,
                               _stack(tokens(s, det.signal_column), runs))


def test_ascans_classified_with_their_scanned_motor():
    card = spec_s30_raw.card_template()
    by_no = {r.scan_number: r for r in spec_s30_raw.parse_file(RAW, card)}
    # ascan herixE is an energy-calibration scan; motor ascans are alignment.
    for n, kind in ASCANS.items():
        assert by_no[n].scan_type == kind
    # The scanned axis is captured by name (the data column 0 the motor drives).
    assert [a.name for a in axes.scanned_axes(by_no[1])] == ["HerixE"]
    assert [a.name for a in axes.scanned_axes(by_no[3])] == ["micro_z"]
    assert len(axes.scanned_axes(by_no[4])) == 1
    for n in ASCANS:
        s = scan_truth(RAW, n)
        axis = axes.scanned_axes(by_no[n])[0]
        assert axis.name == s["column_names"][0]
        assert np.allclose(axis.values, [float(t) for t in tokens(s, s["column_names"][0])])


def test_raw_record_survives_json_round_trip_with_2d_arrays():
    card = spec_s30_raw.card_template()
    rec = next(r for r in spec_s30_raw.parse_file(RAW, card) if r.scan_type == "measurement")
    nP = scan_truth(RAW, rec.scan_number)["points"] // 15
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
    assert restored.spectra["ana1"].miniscan_counts.shape == (nP, 15)
    assert restored.normalization.miniscan_monitor.shape == (nP, 15)
