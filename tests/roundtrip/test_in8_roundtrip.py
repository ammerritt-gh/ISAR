"""Gate A — the synthetic IN8 round-trip (§4): parse(emit(truth)) ≈ truth, no quirks.

The single-detector (det0) analogue of the S30 Gate A: emit a synthetic numor run, parse it back, and
assert the energy axis, constant Q, raw counts, monitor, time, and temperature setpoint all reproduce.
It does NOT prove the bytes match a real numor — that is Gate B's job (test_in8_smoke). Both required.
"""
from isar.parsers import ill_in8, registry
from isar.synth_format import roundtrip, run_builder


def test_in8_parse_emit_truth_round_trip(tmp_path):
    card = ill_in8.card_template()
    run = run_builder.build_in8_run("DIR-TEST", n_scans=3, seed0=2024)
    path = tmp_path / "DIR-TEST.numor"
    records = roundtrip.roundtrip_record(ill_in8, run, card, path, central_det="det0")
    assert len(records) == len(run.scans)
    assert all(set(r.spectra) == {"det0"} for r in records)
    assert all(r.scan_type == "measurement" for r in records)


def test_emitted_numor_detects_as_in8(tmp_path):
    card = ill_in8.card_template()
    run = run_builder.build_in8_run("DIR-TEST", n_scans=1)
    path = tmp_path / "one.numor"
    path.write_bytes(ill_in8.emit_file(run, card))
    assert registry.detect(path)[:2] == ("ILL", "IN8")
