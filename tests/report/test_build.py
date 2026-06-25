"""generate_report end-to-end: collate -> model -> figures -> write HTML + Markdown(+PNGs) + JSON."""
import json

import numpy as np

from isar.parsers import spec_s30_raw
from isar.reduce import combine
from isar.report import build
from isar.synth_format import quirks, run_builder


def _reduced(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("E2E", n_scans=1, seed0=11, include_ascans=True)
    sc = run.scans[0]
    d = next(x for x in sc.detectors if x.detector_id == "ana5")
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, "ana5", p0, 7, magnitude=1.0e4)
    path = tmp_path / "E2E.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    records = spec_s30_raw.parse_file(path, card)
    for r in records:
        if r.scan_type == "measurement":
            combine.reduce_record(r, card)
    return records, card, p0


def test_generate_report_writes_all_three_formats(tmp_path):
    records, card, p0 = _reduced(tmp_path)
    out = tmp_path / "report"
    written = build.generate_report(records, card, out)

    assert written["html"].exists() and written["markdown"].exists() and written["json"].exists()

    # HTML self-contained, shows the removal
    doc = written["html"].read_text(encoding="utf-8")
    assert "data:image/png;base64," in doc and f"ana5:p{p0}" in doc

    # Markdown's referenced PNGs were actually written to disk
    md = written["markdown"].read_text(encoding="utf-8")
    refs = [ln.split("](")[1].rstrip(")") for ln in md.splitlines() if "](" in ln and ".png)" in ln]
    assert refs
    for rel in refs:
        assert (out / rel).exists()

    # JSON sidecar is machine-readable and carries the removal ledger (the §8 hook)
    blob = json.loads(written["json"].read_text(encoding="utf-8"))
    removals = [rm for s in blob["scans"] for sp in s["spectra"] for rm in sp["removals"]]
    assert any(rm["kind"] == "cosmic_ray_rejected" and rm["rule"] == "poisson" for rm in removals)


def test_generate_report_html_only(tmp_path):
    records, card, _p0 = _reduced(tmp_path)
    written = build.generate_report(records, card, tmp_path / "r2", formats=("html",))
    assert "html" in written and "markdown" not in written
    assert written["json"].exists()       # sidecar is always written
