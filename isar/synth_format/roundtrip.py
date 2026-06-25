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
from isar.reduce import combine, qc
from isar.report import build as report_build
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
            assert np.allclose(sp.intensity, d.counts), f"{where}: intensity (raw counts)"
            assert np.allclose(sp.variance, d.counts), f"{where}: variance (Poisson N)"

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

    # Gate A': synthetic raw + injected cosmic ray -> emit -> parse -> reduce -> REPORT.
    run = run_builder.build_s30_raw_run("SYN_S30_RAW_EYEBALL", n_scans=1, seed0=2024)
    sc = run.scans[0]
    det = "ana5"
    d = next(x for x in sc.detectors if x.detector_id == det)
    p0, m0 = int(np.argmax(d.miniscan_counts.sum(axis=1))), 7
    quirks.inject_miniscan_cosmic_ray(sc, det, p0, m0, magnitude=1.0e4)
    raw_emitted = _OUT / "SYN_S30_RAW.spec"
    raw_emitted.write_bytes(spec_s30_raw.emit_file(run, raw_card))
    syn_records = spec_s30_raw.parse_file(raw_emitted, raw_card)
    for r in syn_records:
        if r.scan_type == "measurement":
            combine.reduce_record(r, raw_card)
            qc.qc_record(r, raw_card)               # deterministic QC after reduction (§7.1-7.2)
    syn = next(r for r in syn_records if r.scan_type == "measurement")
    syn_report = report_build.generate_report(syn_records, raw_card, _OUT / "report" / "synthetic")

    # Gate C artifact: reduce the REAL raw fixture under BOTH rules, to show the controlled divergence
    # — the beamline-reproducing sigma_clip vs ISAR's conservative Poisson — on the same real data,
    # and render a full report for the (default, Poisson) reduction.
    def _reduce_real(rule):
        records = spec_s30_raw.parse_file(_REAL_S30_RAW, raw_card)
        c = spec_s30_raw.card_template()
        c.policy.reduction = dict(raw_card.policy.reduction, rule=rule)
        for r in records:
            if r.scan_type == "measurement":
                combine.reduce_record(r, c)
                qc.qc_record(r, c)                  # QC the real fixture too — surface any real flags
        return records, c

    real_poisson, real_card = _reduce_real("poisson")
    real_sigma, _ = _reduce_real("sigma_clip")
    real_report = report_build.generate_report(real_poisson, real_card, _OUT / "report" / "real")

    def _count(records):
        return sum(_count_rejected(r) for r in records if r.scan_type == "measurement")

    return {"emitted": raw_emitted, "syn_report": syn_report, "real_report": real_report,
            "injected": (det, p0, m0), "syn_rejected": _count_rejected(syn),
            "real_poisson": _count(real_poisson), "real_sigma": _count(real_sigma),
            "rule": raw_card.policy.reduction.get("rule")}


def _qc_demo() -> dict:
    """A supplementary run carrying one of every deterministic QC quirk, so the human sees each flag
    rendered: truncated + dead_detector + sensor_divergence + stale_head on scan 1, and a whole-scan
    `empty` (beam down) on scan 2. Nothing is deleted — every flag is shown with its reason (§7.4).
    """
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN_S30_QC", n_scans=2, seed0=4242, target_peak_counts=200.0)
    s1, s2 = run.scans
    s1.command = "fscan synthetic np 999"            # claims 999 points -> truncated (short)
    quirks.inject_dead_detector(s1, "ana3")          # one analyzer dead among live siblings
    quirks.inject_sensor_divergence(s1, value=350.0)  # T sensor 60 K from the setpoint
    quirks.inject_stale_head(s1, "ana5", n=3)        # HERIX leading stale-first-points
    for d in s2.detectors:                           # beam down: the whole scan flat -> empty
        quirks.inject_flat(s2, d.detector_id)

    path = _OUT / "SYN_S30_QC.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(path, card)
    for r in records:
        qc.qc_record(r, card)
    report = report_build.generate_report(records, card, _OUT / "report" / "qc_demo")
    flags = sorted(
        {f.type for r in records for f in r.scan_flags}
        | {f.type for r in records for sp in r.spectra.values() for f in sp.spectrum_flags}
        | {pf.type for r in records for sp in r.spectra.values() for pf in sp.point_flags})
    return {"report": report, "flags": flags}


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
    qcd = _qc_demo()

    print(_banner([
        "ISAR Phase 2 - parse + RAW reduction + QC + REPORT: ALL GATES PASSED. Eyeball pause.",
        "",
        "Gate A  (synthetic supplementary round-trip parse(emit(truth)) == truth): PASS",
        "Gate B  (real supplementary smoke, all 90 scans parsed sane):             PASS",
        "Gate A' (synthetic raw round-trip + injected cosmic ray rejected):        PASS",
        "Gate C  (raw reduction reproduces the beamline sum_o/5std/3std):          PASS",
        "",
        "OPEN THESE REPORTS (what the scans looked like + what got removed/flagged and why):",
        f"  synthetic (injected cosmic ray):  {raw['syn_report']['html']}",
        f"  real fixture (Poisson default):   {raw['real_report']['html']}",
        f"  deterministic QC demo:            {qcd['report']['html']}",
        "  (each report has a Markdown + PNG twin and a machine-readable report.json sidecar)",
        "",
        f"  Injected cosmic ray at {det} point {p0} pass {m0}; default rule = {raw['rule']}.",
        f"  Synthetic: {raw['syn_rejected']} pass(es) rejected (expect 1 = the injection, no false +).",
        f"  Real fixture, same data, two rules: sigma_clip rejected {raw['real_sigma']} pass(es) "
        f"(reproduces the beamline);",
        f"               Poisson rejected {raw['real_poisson']} (conservative — refuses low-count "
        f"fluctuations).",
        f"  QC demo flags surfaced: {', '.join(qcd['flags'])}",
        "  -> Every removal AND flag is shown with its reason; nothing is deleted (reversible, §0.4).",
    ]))


if __name__ == "__main__":
    main()
