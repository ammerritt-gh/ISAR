"""Point-level QC (§7.1): stale_head — the HERIX leading stale-first-points signature.

Point-level QC MASKS the offending points (and flags them) — it never deletes (§0.4): the arrays stay
full length, the mask is the reversible keep/drop layer. We score against the injected truth: the
leading run is masked + flagged, and a clean spectrum (a real rising edge, not a flat-high run) trips
nothing.
"""
import numpy as np

from isar.parsers import spec_s30
from isar.reduce import qc_points
from isar.synth_format import quirks, run_builder


def _parse(run, card, tmp_path):
    path = tmp_path / f"{run.experiment_id}.spec_supplementary.dat"
    path.write_bytes(spec_s30.emit_file(run, card))
    return spec_s30.parse_file(path, card)


def _clean(seed0=7, target=200.0):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("QCP", n_scans=1, seed0=seed0, target_peak_counts=target)
    return run, card


def test_stale_head_masks_and_flags_the_leading_run(tmp_path):
    run, card = _clean()
    quirks.inject_stale_head(run.scans[0], "ana5", n=3)
    rec = _parse(run, card, tmp_path)[0]
    nP = len(rec.spectra["ana5"].intensity)
    qc_points.qc_points(rec, card)
    sp = rec.spectra["ana5"]
    stale = sorted(pf.index for pf in sp.point_flags if pf.type == "stale_head")
    assert stale == [0, 1, 2]
    assert not sp.mask[:3].any()            # leading run masked...
    assert sp.mask[3:].all()                # ...and nothing after it
    assert len(sp.intensity) == nP          # nothing deleted — full length retained


def test_clean_spectrum_has_no_stale_head(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    qc_points.qc_points(rec, card)
    for sp in rec.spectra.values():
        assert not any(pf.type == "stale_head" for pf in sp.point_flags)


def test_qc_points_survives_a_degenerate_record(tmp_path):
    run, card = _clean()
    rec = _parse(run, card, tmp_path)[0]
    rec.spectra["ana1"].intensity = np.array([])
    rec.spectra["ana1"].mask = np.array([], dtype=bool)
    qc_points.qc_points(rec, card)          # must not raise
    rec.spectra = {}
    qc_points.qc_points(rec, card)          # must not raise
