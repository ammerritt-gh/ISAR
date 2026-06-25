"""Renderers: a self-contained HTML (plots embedded) and Markdown + PNG files. Both must surface the
key facts — what got removed, why, the rule, and the reversibility note — nothing hidden."""
import numpy as np

from isar.parsers import ill_in8, spec_s30, spec_s30_raw
from isar.reduce import collate, combine, qc
from isar.report import figures, html, markdown
from isar.report import model as report_model
from isar.synth_format import quirks, run_builder


def _model_and_figs(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("REND", n_scans=1, seed0=11, include_ascans=True)
    sc = run.scans[0]
    d = next(x for x in sc.detectors if x.detector_id == "ana5")
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, "ana5", p0, 7, magnitude=1.0e4)
    path = tmp_path / "REND.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    records = spec_s30_raw.parse_file(path, card)
    for r in records:
        if r.scan_type == "measurement":
            combine.reduce_record(r, card)
    col = collate.collate(records, card)
    model = report_model.build_report_model(records, col, card)
    figs = figures.report_figures(records, col)
    return model, figs, p0


def test_html_is_self_contained_and_shows_removals(tmp_path):
    model, figs, p0 = _model_and_figs(tmp_path)
    doc = html.render_html(model, figs)
    assert "<html" in doc.lower()
    assert "reversible" in doc.lower()           # the never-deleted assurance
    assert "poisson" in doc                       # the rule that decided
    assert "Scan 1" in doc or "scan 1" in doc
    assert f"ana5:p{p0}" in doc                    # the cosmic-ray removal's stable id
    assert "data:image/png;base64," in doc         # plots embedded inline
    assert 'src="figures/' not in doc              # ...not referenced as external files


def test_qc_flags_render_in_html_and_markdown(tmp_path):
    # A supplementary scan carrying one of each QC flag must show each by name in both renderers.
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QCR", n_scans=1, seed0=7, target_peak_counts=200.0)
    quirks.inject_dead_detector(run.scans[0], "ana3")          # spectrum-level
    quirks.inject_sensor_divergence(run.scans[0], value=350.0)  # scan-level
    quirks.inject_stale_head(run.scans[0], "ana5", n=3)         # point-level (masked → ledger)
    path = tmp_path / "QCR.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(path, card)
    for r in records:
        qc.qc_record(r, card)
    col = collate.collate(records, card)
    model = report_model.build_report_model(records, col, card)

    doc = html.render_html(model, {})            # figures omitted; flags come from the model
    md, _ = markdown.render_markdown(model, {})
    for text in (doc, md):
        assert "dead_detector" in text
        assert "sensor_divergence" in text
        assert "stale_head" in text


def test_in8_merged_section_renders_in_html_and_markdown(tmp_path):
    card = ill_in8.card_template()
    run = run_builder.build_in8_run("DIR-REND", n_scans=2, seed0=7)
    path = tmp_path / "DIR-REND.numor"
    path.write_bytes(ill_in8.emit_file(run, card))
    records = ill_in8.parse_file(path, card)
    col = collate.collate(records, card)
    model = report_model.build_report_model(records, col, card)
    figs = figures.report_figures(records, col)

    doc = html.render_html(model, figs)
    md, _ = markdown.render_markdown(model, figs)
    for text in (doc, md):
        assert "Merged" in text
        assert "55000" in text and "55001" in text       # the contributing numors are listed


def test_markdown_references_png_files_that_exist(tmp_path):
    model, figs, p0 = _model_and_figs(tmp_path)
    md, files = markdown.render_markdown(model, figs)
    assert "reversible" in md.lower()
    assert "poisson" in md
    assert f"ana5:p{p0}" in md
    assert "data:image/png" not in md              # markdown links files, doesn't embed
    # every image the markdown references is provided as a real PNG in `files`
    refs = [ln for ln in md.splitlines() if "](" in ln and ".png)" in ln]
    assert refs
    assert any(path.endswith(".png") for path in files)
    assert all(png.startswith(b"\x89PNG") for png in files.values())
