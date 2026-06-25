"""Gate A — the synthetic round-trip (§4): parse(emit(truth)) ≈ truth, no quirks.

This proves the emitter and parser are consistent *inverses*: Q reconstructs per analyzer
(including the central one), the energy axis survives, intensity/variance reproduce the
normalization, both monitor and time survive (§5.3), and scan_type classifies. It does NOT prove
either matches real bytes — that is Gate B's job (the inverse-crime split, §4.1). Both required.
"""
import numpy as np

from isar.parsers import registry, spec_s30
from isar.synth_format import roundtrip, run_builder


def test_parse_emit_truth_round_trip(tmp_path):
    # The detailed per-scan/per-detector truth assertions live in the shared harness
    # `roundtrip.roundtrip_record`, which raises AssertionError naming any mismatch.
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN_S30", n_scans=3, seed0=2024)
    path = tmp_path / "SYN_S30.spec_supplementary.dat"
    records = roundtrip.roundtrip_record(spec_s30, run, card, path)
    assert len(records) == len(run.scans)


def test_round_trip_recovers_per_analyzer_intensity(tmp_path):
    # A direct spot-check of the normalization (intensity = counts / monitor) at the central analyzer.
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN_S30", n_scans=1, seed0=99)
    path = tmp_path / "SYN_S30.spec_supplementary.dat"
    rec = roundtrip.roundtrip_record(spec_s30, run, card, path)[0]
    sc = run.scans[0]
    ana5 = next(d for d in sc.detectors if d.detector_id == "ana5")
    assert np.allclose(rec.spectra["ana5"].intensity, ana5.counts / sc.monitor)


def test_emitted_file_detects_as_s30(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN", n_scans=1)
    path = tmp_path / "SYN.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    assert registry.detect(path) == ("APS", "S30", "2025")
