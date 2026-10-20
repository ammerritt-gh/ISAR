"""Gate B — the file smoke test (§4.1): fidelity against the layout of genuine S30 output.

The synthetic round-trip (Gate A) proves the emitter and parser are consistent *inverses*; it
cannot prove either matches the facility's format. This test parses the committed artificial copy of
a real S30 file (`artificial_data/`: the facility's own header, comments, column names, spacing and
line endings, every data-row number replaced) — the inverse-crime guard. It checks *sanity* and
column mapping: right detector count, monotonic energy axis, monitor/time populated, Q reported per
analyzer, scan_type correct, no exception across all kept scans, and every value the parser takes
from a data column equals what the copy tool wrote there (its truth file). Statistics of measured
counts are not asserted here; the real-file check lives in `proofs/`.
"""
import json

import numpy as np

from artificial import S30 as S30_FILE, truth, values
from isar.parsers import registry, spec_s30
from isar.schema import axes, scan

TRUTH = truth(S30_FILE)


def _strictly_monotonic(a) -> bool:
    d = np.diff(a)
    return bool(np.all(d > 0) or np.all(d < 0))


def test_detect_resolves_s30():
    assert registry.detect(S30_FILE) == ("APS", "S30", "2025")


def test_parses_every_scan_into_a_nine_spectrum_record():
    card = spec_s30.card_template()
    records = spec_s30.parse_file(S30_FILE, card)

    assert [r.scan_number for r in records] == [s["scan_number"] for s in TRUTH["scans"]]
    assert len(records) == TRUTH["number_of_scans"]
    for rec in records:
        # One physical scan → one record holding nine analyzer spectra (§3).
        assert len(rec.spectra) == 9
        assert set(rec.spectra) == {f"ana{i}" for i in range(1, 10)}
        assert rec.scan_type == "measurement"
        assert rec.scan_type_source == "inferred"

        # Exactly one scanned energy axis, and it is monotonic.
        axes.assert_constant_q_energy(rec)
        energy = axes.scanned_axes(rec)[0].values
        assert _strictly_monotonic(energy)

        # Both monitor and time are stored and finite (§5.3).
        norm = rec.normalization
        assert norm.monitor.shape == energy.shape
        assert np.all(np.isfinite(norm.monitor)) and np.all(np.isfinite(norm.time))

        # Provenance stamped.
        assert rec.source.plugin == "spec_s30"
        assert rec.source.format_version == "2025"
        assert rec.energy_calibration.status == "needs_zero"

        for det_id, sp in rec.spectra.items():
            assert sp.detector_id == det_id
            assert sp.q_source == "reported"           # D2: Q read from the file, not card offset
            assert len(sp.q) == 3 and all(np.isfinite(sp.q))
            assert sp.intensity.shape == energy.shape
            assert np.all(np.isfinite(sp.intensity))
            assert np.all(np.isfinite(sp.variance))
            assert sp.mask.dtype == np.bool_


def test_every_value_taken_from_a_data_column_is_the_one_written_there():
    """The card's column map is right iff each parsed array equals its truth column: a wrong
    energy/monitor/time/signal column name (or a shifted index) reads another column's numbers."""
    card = spec_s30.card_template()
    for rec, s in zip(spec_s30.parse_file(S30_FILE, card), TRUTH["scans"]):
        assert rec.scan_number == s["scan_number"]
        assert np.allclose(axes.scanned_axes(rec)[0].values, values(s, card.policy.energy_column))
        assert np.allclose(rec.normalization.monitor, values(s, card.policy.monitor_column))
        assert np.allclose(rec.normalization.time, values(s, card.policy.time_column))
        for det in card.policy.detectors:
            counts = values(s, det.signal_column)
            assert len(counts) == s["points"]
            assert np.allclose(rec.spectra[det.detector_id].intensity, counts)
            assert np.allclose(rec.spectra[det.detector_id].variance, counts)   # D3: σ² = N, no floor


def test_default_card_names_the_3sigma_facility_columns():
    """The column map above reads whatever the card names, so a card pointing a detector at another
    existing column (Ana5_3std -> Ana5_5std) would pass it. Pinned here as literals instead."""
    pol = spec_s30.card_template().policy
    assert (pol.energy_column, pol.monitor_column, pol.time_column) == ("HerixE", "ICO-C", "Seconds")
    assert {d.detector_id: d.signal_column for d in pol.detectors} == {
        f"ana{i}": f"Ana{i}_3std" for i in range(1, 10)}


def test_central_analyzer_carries_the_scan_Q():
    # ana5 ↔ #V8 == #Q (the D2 mapping check on the facility's layout).
    card = spec_s30.card_template()
    rec = spec_s30.parse_file(S30_FILE, card)[0]
    q_consts = {a.name: a.values[0] for a in rec.scan_axes if a.role == "constant"}
    ana5_q = rec.spectra["ana5"].q
    assert np.isclose(ana5_q[0], q_consts["q_h"])
    assert np.isclose(ana5_q[1], q_consts["q_k"])
    assert np.isclose(ana5_q[2], q_consts["q_l"])


def test_record_survives_json_round_trip():
    # A parsed record must serialize to the canonical JSON store and back unchanged.
    card = spec_s30.card_template()
    rec = spec_s30.parse_file(S30_FILE, card)[0]
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
