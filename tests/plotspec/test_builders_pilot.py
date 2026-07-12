"""Pilot migration: `spectrum_spec` re-expresses the spectrum/fit overlay as a PlotSpec whose DATA
series matches the record's kept (energy, counts), and `spectrum_figure` still returns PNG bytes
(now via spec -> backend)."""
import json

import numpy as np

from isar.parsers import spec_s30_raw
from isar.plotspec import builders
from isar.plotspec import spec as ps
from isar.reduce import combine
from isar.report import figures
from isar.schema import axes
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


def test_spectrum_spec_data_series_matches_record(tmp_path):
    rec, _p0, _card = _reduced_with_ray(tmp_path)
    sp = builders.spectrum_spec(rec, "ana5")
    spectrum = rec.spectra["ana5"]
    energy = axes.scanned_axes(rec)[0].values
    mask = spectrum.mask.astype(bool)

    data = next(s for s in sp.series if s.role == ps.Role.DATA)
    np.testing.assert_allclose(data.x, energy[mask])
    np.testing.assert_allclose(data.y, spectrum.intensity[mask])
    np.testing.assert_allclose(data.yerr, np.sqrt(np.clip(spectrum.variance, 0.0, None))[mask])


def test_spectrum_spec_to_dict_is_stable_json(tmp_path):
    rec, _p0, _card = _reduced_with_ray(tmp_path)
    sp = builders.spectrum_spec(rec, "ana5")
    d = sp.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["projection"] is not None


def test_spectrum_figure_still_returns_png(tmp_path):
    rec, _p0, _card = _reduced_with_ray(tmp_path)
    png = figures.spectrum_figure(rec, "ana5")
    assert png.startswith(_PNG) and len(png) > 1000
