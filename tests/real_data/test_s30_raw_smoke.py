"""Gate B' — real-file smoke for the RAW S30 parser (§4.1, raw ingestion).

The supplementary Gate B parses the beamline-cleaned file; this parses the *raw* `.spec` with its
individual miniscans. It asserts the record is sane (not yet exact truth — that is Gate C):

  * detect() tells raw apart from supplementary by `#L` content (distinct format_version);
  * each fscan becomes a 9-spectrum *measurement* record carrying the full `[nP, 15]` miniscan
    ensemble + per-miniscan monitor/time, still *unreduced* (intensity 0, flagged), so cosmic-ray
    rejection is a later, reversible step (§0.4);
  * ascans are classified alignment/calibration with their scanned motor captured (§5.2);
  * the 2-D ensemble survives the canonical JSON store.
"""
from pathlib import Path
import json

import numpy as np

from isar.parsers import registry, spec_s30_raw
from isar.schema import axes, scan

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "test_data" / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec"
SUPP = REPO / "test_data" / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec_supplementary.dat"


def _strictly_monotonic(a) -> bool:
    d = np.diff(a)
    return bool(np.all(d > 0) or np.all(d < 0))


def test_detect_distinguishes_raw_from_supplementary():
    # Both are S30 SPEC files; the #L content (raw has bare AnaN, supp has AnaN_sum_o) splits them.
    assert registry.detect(RAW) == ("APS", "S30", "2025-raw")
    assert registry.detect(SUPP) == ("APS", "S30", "2025")


def test_fscans_become_nine_spectrum_measurement_records_with_miniscan_ensemble():
    card = spec_s30_raw.card_template()
    records = spec_s30_raw.parse_file(RAW, card)
    fscans = [r for r in records if r.scan_type == "measurement"]
    assert len(fscans) == 3
    for rec in fscans:
        assert set(rec.spectra) == {f"ana{i}" for i in range(1, 10)}
        assert rec.scan_type_source == "inferred"

        axes.assert_constant_q_energy(rec)
        energy = axes.scanned_axes(rec)[0].values
        assert _strictly_monotonic(energy)
        nP = len(energy)
        assert nP == 20                              # trimmed to 20 energy points (300 / 15)

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
        assert np.all(rec.normalization.miniscan_monitor > 0)

        assert rec.source.plugin == "spec_s30_raw"
        assert rec.source.format_version == "2025-raw"


def test_ascans_classified_with_their_scanned_motor():
    card = spec_s30_raw.card_template()
    by_no = {r.scan_number: r for r in spec_s30_raw.parse_file(RAW, card)}
    # ascan herixE is an energy-calibration scan; motor ascans are alignment.
    assert by_no[1].scan_type == "calibration"
    assert by_no[3].scan_type == "alignment"
    assert by_no[4].scan_type == "alignment"
    assert by_no[5].scan_type == "alignment"
    # The scanned axis is captured by name (the data column 0 the motor drives).
    assert [a.name for a in axes.scanned_axes(by_no[1])] == ["HerixE"]
    assert [a.name for a in axes.scanned_axes(by_no[3])] == ["micro_z"]
    assert len(axes.scanned_axes(by_no[4])) == 1


def test_raw_record_survives_json_round_trip_with_2d_arrays():
    card = spec_s30_raw.card_template()
    rec = next(r for r in spec_s30_raw.parse_file(RAW, card) if r.scan_type == "measurement")
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
    assert restored.spectra["ana1"].miniscan_counts.shape == (20, 15)
    assert restored.normalization.miniscan_monitor.shape == (20, 15)
