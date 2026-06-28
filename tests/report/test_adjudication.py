"""The report surfaces the LLM adjudication: a per-scan narrative + a decisions table (§7.3, §7.4).

Decisions are advisory and non-destructive, so they ride alongside the deterministic flags — the
report shows what the adjudicator decided and why, for the §8 human review.
"""
from isar.parsers import spec_s30
from isar.reduce import collate, qc
from isar.report import html, markdown
from isar.report import model as report_model
from isar.schema import scan
from isar.synth_format import quirks, run_builder


def _model_with_adjudication(tmp_path):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("ADJ", n_scans=1, seed0=7, target_peak_counts=200.0)
    quirks.inject_dead_detector(run.scans[0], "ana3")
    path = tmp_path / "ADJ.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(path, card)
    for r in records:
        qc.qc_record(r, card)
    meas = next(r for r in records if r.scan_type == "measurement")
    meas.adjudication = scan.Adjudication(
        decisions=[scan.Decision(
            target="spectrum:ana3", decision="drop_detector", shape="not_applicable",
            reason="flat at background while siblings show peaks", confidence=0.95)],
        narrative="ana3 dropped as a dead detector; rest of the scan is clean.",
        model_id="local_desktop/qwen3p5-9b",
    )
    col = collate.collate(records, card)
    return report_model.build_report_model(records, col, card)


def test_scan_section_carries_adjudication(tmp_path):
    model = _model_with_adjudication(tmp_path)
    s = model.scans[0]
    assert s.narrative == "ana3 dropped as a dead detector; rest of the scan is clean."
    assert s.decision                               # a non-empty rollup verdict
    assert s.adjudication[0]["target"] == "spectrum:ana3"
    assert s.adjudication[0]["decision"] == "drop_detector"
    assert s.adjudication[0]["confidence"] == 0.95


def test_adjudication_renders_in_html_and_markdown(tmp_path):
    model = _model_with_adjudication(tmp_path)
    doc = html.render_html(model, {})
    md, _ = markdown.render_markdown(model, {})
    for text in (doc, md):
        assert "drop_detector" in text              # the decision
        assert "spectrum:ana3" in text              # the target
        assert "dead detector" in text              # the narrative prose


def test_no_adjudication_leaves_slots_empty(tmp_path):
    # A scan that was never adjudicated keeps narrative/decision empty and renders without a table.
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("NOADJ", n_scans=1, seed0=3, target_peak_counts=200.0)
    path = tmp_path / "NOADJ.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    records = spec_s30.parse_file(path, card)
    col = collate.collate(records, card)
    model = report_model.build_report_model(records, col, card)
    s = model.scans[0]
    assert s.narrative is None
    assert s.decision is None
    assert s.adjudication == []
