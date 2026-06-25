"""Figure smoke tests: the report renders real PNGs (Agg, headless). We assert valid, non-empty
PNGs — not pixels — including the per-pass miniscan drill-down at a rejected point."""
import numpy as np

from isar.parsers import spec_s30_raw
from isar.reduce import collate, combine
from isar.report import figures
from isar.synth_format import quirks, run_builder

_PNG = b"\x89PNG\r\n\x1a\n"


def _reduced_with_ray(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("FIG", n_scans=1, seed0=11, include_ascans=False)
    sc = run.scans[0]
    d = next(x for x in sc.detectors if x.detector_id == "ana5")
    p0 = int(np.argmax(d.miniscan_counts.sum(axis=1)))
    quirks.inject_miniscan_cosmic_ray(sc, "ana5", p0, 7, magnitude=1.0e4)
    path = tmp_path / "FIG.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    rec = next(r for r in spec_s30_raw.parse_file(path, card) if r.scan_type == "measurement")
    combine.reduce_record(rec, card)
    return rec, p0, card


def test_spectrum_figure_is_a_valid_png(tmp_path):
    rec, _p0, _card = _reduced_with_ray(tmp_path)
    png = figures.spectrum_figure(rec, "ana5")
    assert png.startswith(_PNG) and len(png) > 1000


def test_miniscan_figure_at_rejected_point_is_a_valid_png(tmp_path):
    rec, p0, _card = _reduced_with_ray(tmp_path)
    png = figures.miniscan_figure(rec, "ana5", p0)
    assert png.startswith(_PNG) and len(png) > 1000


def test_coverage_figure_is_a_valid_png(tmp_path):
    rec, _p0, card = _reduced_with_ray(tmp_path)
    col = collate.collate([rec], card)
    png = figures.coverage_figure(col)
    assert png.startswith(_PNG) and len(png) > 500
