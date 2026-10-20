"""PROOF (real data, run on purpose): Gate B — the real-file smoke test (§4.1): byte-level fidelity against genuine S30 output.

The synthetic round-trip (Gate A) proves the emitter and parser are consistent *inverses*; it
cannot prove either matches reality. This test parses one real facility file and asserts the
record is sane — the inverse-crime guard. It checks *sanity*, not exact truth: right detector
count, monotonic energy axis, monitor/time populated, Q reported per analyzer, scan_type correct,
no exception across every scan.
"""
import numpy as np

from isar.parsers import registry, spec_s30
from isar.schema import axes

FOLDER = "APS S30"
NAME = "FeSe_1-xS_x_1.spec_supplementary.dat"


def _strictly_monotonic(a) -> bool:
    d = np.diff(a)
    return bool(np.all(d > 0) or np.all(d < 0))


def test_detect_resolves_s30(real_file):
    assert registry.detect(real_file(FOLDER, NAME)) == ("APS", "S30", "2025")


def test_parses_every_scan_into_a_nine_spectrum_record(real_file, expect):
    card = spec_s30.card_template()
    records = spec_s30.parse_file(real_file(FOLDER, NAME), card)

    assert len(records) == expect(FOLDER)["n_scans"]
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

        # Both monitor and time are stored and sane (§5.3).
        norm = rec.normalization
        assert norm.monitor.shape == energy.shape
        assert np.all(np.isfinite(norm.monitor)) and np.median(norm.monitor) > 0
        assert np.all(norm.time > 0)

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


def test_central_analyzer_carries_the_scan_Q(real_file):
    # ana5 ↔ #V8 == #Q (the D2 mapping check on real bytes).
    card = spec_s30.card_template()
    rec = spec_s30.parse_file(real_file(FOLDER, NAME), card)[0]
    q_consts = {a.name: a.values[0] for a in rec.scan_axes if a.role == "constant"}
    ana5_q = rec.spectra["ana5"].q
    assert np.isclose(ana5_q[0], q_consts["q_h"])
    assert np.isclose(ana5_q[1], q_consts["q_k"])
    assert np.isclose(ana5_q[2], q_consts["q_l"])


def test_record_survives_json_round_trip(real_file):
    # A parsed real record must serialize to the canonical JSON store and back unchanged.
    import json
    from isar.schema import scan

    card = spec_s30.card_template()
    rec = spec_s30.parse_file(real_file(FOLDER, NAME), card)[0]
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
