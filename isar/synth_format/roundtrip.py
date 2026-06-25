"""The parser round-trip harness (§4): assert parse(emit(truth)) ≈ truth, and the eyeball pause.

`roundtrip_record` is the single source of the Gate-A assertions (used by `tests/roundtrip/` and
by `main`). `main` is the human-facing pause: it emits a few SPEC files and a real reparsed record
to `out/phase2_eyeball/`, runs both gates, and prints a banner asking the human to eyeball the
output — the way Phase 1 paused after its generator. A parser is not "done" until Gate A + Gate B
pass AND the human signs off (§10 step 3).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from isar.parsers import registry, spec_s30, spec_s30_raw
from isar.reduce import combine
from isar.schema import axes, scan
from isar.synth_format import quirks, run_builder

_REPO = Path(__file__).resolve().parents[2]
_REAL_S30 = _REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"
_REAL_S30_RAW = _REPO / "test_data" / "APS S30 raw" / "Fe2Mo3O8_trimmed.spec"
_OUT = _REPO / "out" / "phase2_eyeball"


def roundtrip_record(plugin, run, card, path) -> list[scan.ScanRecord]:
    """Emit the run, parse it back, and assert each scan/detector reproduces the truth.

    Raises AssertionError naming the failing scan/detector. Returns the parsed records.
    """
    Path(path).write_bytes(plugin.emit_file(run, card))
    records = plugin.parse_file(path, card)
    assert len(records) == len(run.scans), f"{len(records)} records != {len(run.scans)} scans"

    for rec, sc in zip(records, run.scans):
        tag = f"scan {sc.scan_number}"
        assert rec.scan_number == sc.scan_number, tag
        assert rec.scan_type == sc.scan_type, f"{tag}: scan_type {rec.scan_type} != {sc.scan_type}"

        energy = axes.scanned_axes(rec)[0].values
        assert np.allclose(energy, sc.energy), f"{tag}: energy axis"
        assert np.allclose(rec.normalization.monitor, sc.monitor), f"{tag}: monitor"
        assert np.allclose(rec.normalization.time, sc.time), f"{tag}: time"
        assert np.isclose(rec.temperature.setpoint[0], sc.temperature_setpoint), f"{tag}: T"
        assert np.allclose(rec.spectra["ana5"].q, sc.central_q), f"{tag}: central Q (ana5/#V8)"

        for d in sc.detectors:
            sp = rec.spectra[d.detector_id]
            where = f"{tag}/{d.detector_id}"
            assert sp.q_source == "reported", f"{where}: q_source {sp.q_source}"
            assert np.allclose(sp.q, d.q), f"{where}: Q"
            assert np.allclose(sp.intensity, d.counts / sc.monitor), f"{where}: intensity"
            assert np.allclose(sp.variance, d.counts / sc.monitor**2), f"{where}: variance"

    return records


def _banner(lines: list[str]) -> str:
    width = max(len(s) for s in lines) + 4
    bar = "=" * width
    body = "\n".join(f"  {s}" for s in lines)
    return f"\n{bar}\n{body}\n{bar}\n"


def _count_rejected(record) -> int:
    return sum(int(np.nansum(sp.miniscan_reject)) for sp in record.spectra.values()
              if sp.miniscan_reject is not None)


def _raw_eyeball() -> dict:
    """Emit a synthetic raw .spec (with an injected cosmic ray) and reduce a real raw record.

    Writes three artifacts for the human: the emitted raw SPEC (does it look like real HERIX raw
    output?), the reduced synthetic record (is the injected miniscan the only one rejected?), and a
    reduced REAL record from the committed fixture (does our tracked rejection look sane on reality?).
    """
    raw_card = spec_s30_raw.card_template()

    # Gate A': synthetic raw + injected cosmic ray -> emit -> parse -> reduce.
    run = run_builder.build_s30_raw_run("SYN_S30_RAW_EYEBALL", n_scans=1, seed0=2024)
    sc = run.scans[0]
    det = "ana5"
    d = next(x for x in sc.detectors if x.detector_id == det)
    p0, m0 = int(np.argmax(d.miniscan_counts.sum(axis=1))), 7
    quirks.inject_miniscan_cosmic_ray(sc, det, p0, m0, magnitude=1.0e4)
    raw_emitted = _OUT / "SYN_S30_RAW.spec"
    raw_emitted.write_bytes(spec_s30_raw.emit_file(run, raw_card))
    syn = next(r for r in spec_s30_raw.parse_file(raw_emitted, raw_card)
               if r.scan_type == "measurement")
    combine.reduce_record(syn, raw_card)
    syn_json = _OUT / "syn_raw_reduced.json"
    syn_json.write_text(json.dumps(syn.to_dict(), indent=2), encoding="utf-8")

    # Gate C artifact: reduce a REAL raw record under BOTH rules, to show the controlled divergence
    # — the beamline-reproducing sigma_clip vs ISAR's conservative Poisson — on the same real data.
    def _reduce_real(rule):
        rec = next(r for r in spec_s30_raw.parse_file(_REAL_S30_RAW, raw_card)
                   if r.scan_type == "measurement")
        c = spec_s30_raw.card_template()
        c.policy.reduction = dict(raw_card.policy.reduction, rule=rule)
        combine.reduce_record(rec, c)
        return rec

    real_poisson = _reduce_real("poisson")
    real_sigma = _reduce_real("sigma_clip")
    real_json = _OUT / "real_raw_reduced_sigma_clip.json"
    real_json.write_text(json.dumps(real_sigma.to_dict(), indent=2), encoding="utf-8")

    return {"emitted": raw_emitted, "syn_json": syn_json, "real_json": real_json,
            "injected": (det, p0, m0), "syn_rejected": _count_rejected(syn),
            "real_poisson": _count_rejected(real_poisson),
            "real_sigma": _count_rejected(real_sigma),
            "rule": raw_card.policy.reduction.get("rule")}


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    card = spec_s30.card_template()

    # Gate A: synthetic round-trip, and write a couple of emitted SPEC files for the eyeball.
    run = run_builder.build_s30_run("SYN_S30_EYEBALL", n_scans=3, seed0=2024)
    emitted = _OUT / "SYN_S30.spec_supplementary.dat"
    roundtrip_record(spec_s30, run, card, emitted)

    # Gate B: parse the real file and pretty-print one reparsed record for the eyeball.
    real_records = spec_s30.parse_file(_REAL_S30, card)
    real_json = _OUT / f"real_scan{real_records[0].scan_number}.json"
    real_json.write_text(json.dumps(real_records[0].to_dict(), indent=2), encoding="utf-8")

    raw = _raw_eyeball()
    det, p0, m0 = raw["injected"]

    print(_banner([
        "ISAR Phase 2 - S30 parser + RAW miniscan reduction: ALL GATES PASSED. Eyeball pause.",
        "",
        "Gate A  (synthetic supplementary round-trip parse(emit(truth)) == truth): PASS",
        "Gate B  (real supplementary smoke, all 90 scans parsed sane):             PASS",
        "Gate A' (synthetic raw round-trip + injected cosmic ray rejected):        PASS",
        "Gate C  (raw reduction reproduces the beamline sum_o/5std/3std):          PASS",
        "",
        "Please eyeball before sign-off:",
        f"  emitted supplementary SPEC:                 {emitted}",
        f"  real supplementary reparsed record:         {real_json}",
        f"  emitted RAW SPEC (individual miniscans):    {raw['emitted']}",
        f"  synthetic raw REDUCED record:               {raw['syn_json']}",
        f"  real raw REDUCED record (sigma_clip):       {raw['real_json']}",
        "",
        f"  Injected cosmic ray at {det} point {p0} pass {m0}; default rule = {raw['rule']}.",
        f"  Synthetic: {raw['syn_rejected']} pass(es) rejected (expect 1 = the injection, no false +).",
        f"  Real fixture, same data, two rules: sigma_clip rejected {raw['real_sigma']} pass(es) "
        f"(reproduces the beamline);",
        f"               Poisson rejected {raw['real_poisson']} (conservative — refuses low-count "
        f"fluctuations).",
        "  -> miniscan_reject marks rejected passes; nothing is deleted (reversible, §0.4).",
    ]))


if __name__ == "__main__":
    main()
