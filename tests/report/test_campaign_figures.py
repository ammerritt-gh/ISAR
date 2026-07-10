"""Smoke tests for the campaign-diagnostic figures: each builder returns a savable matplotlib figure
on minimal synthetic inputs (Agg, headless). We assert valid PNGs via captions.save_captioned, not
pixels — including scan_fit_figure on a real (synthetic) ScanRecord reconstructed from stored params.
"""
import matplotlib

matplotlib.use("Agg")

import numpy as np

from isar.parsers import spec_s30_raw
from isar.reduce import combine
from isar.report import captions
from isar.report import campaign_diagnostic as cd
from isar.report.fit_figures import campaign_dispersion_figure
from isar.synth_format import run_builder

_PNG = b"\x89PNG\r\n\x1a\n"


def _save_ok(fig, tmp_path, fig_id):
    p = tmp_path / "fig.png"
    captions.save_captioned(fig, p, fig_id)
    assert p.exists() and p.read_bytes().startswith(_PNG) and p.stat().st_size > 1000


def _per_q_rows():
    return [
        {"hkl": [2.2, 0, 0], "q": 0.2, "omega0": 1.9, "calibrated_halfwidth": 0.05,
         "reason": "met", "converged": True, "interval_reliable": True, "n_production": 4,
         "n_decline": 0},
        {"hkl": [2.6, 0, 0], "q": 0.6, "omega0": 4.8, "calibrated_halfwidth": None,
         "reason": "uncertifiable_interval", "converged": False, "interval_reliable": False,
         "n_production": 3, "n_decline": 0},
        {"hkl": [3.0, 0, 0], "q": 1.0, "omega0": None, "reason": "no_signal", "converged": False,
         "interval_reliable": False, "n_production": 0, "n_decline": 4},
    ]


def _score_rows():
    return [{"hkl": [2.2, 0, 0], "omega_true": 1.85}, {"hkl": [2.6, 0, 0], "omega_true": 4.85},
            {"hkl": [3.0, 0, 0], "omega_true": 6.0}]


def test_dispersion_figure_with_truth(tmp_path):
    fig = campaign_dispersion_figure(_per_q_rows(), _score_rows())
    _save_ok(fig, tmp_path, "campaign/dispersion_truth")


def test_dispersion_figure_without_truth(tmp_path):
    fig = campaign_dispersion_figure(_per_q_rows(), None)
    _save_ok(fig, tmp_path, "campaign/dispersion_truth")


def test_scan_spend_figure_from_run_events(tmp_path):
    events = [{"kind": "run", "payload": {"hkl": [2.2, 0, 0], "kind": "quicklook"}},
              {"kind": "run", "payload": {"hkl": [2.2, 0, 0], "kind": "production"}},
              {"kind": "run", "payload": {"hkl": [3.0, 0, 0], "kind": "declined"}}]
    fig = cd.scan_spend_figure(events, _per_q_rows(), [])
    _save_ok(fig, tmp_path, "campaign/scan_spend")


def test_scan_spend_figure_fallback_no_run_events(tmp_path):
    fig = cd.scan_spend_figure([], _per_q_rows(), [])
    _save_ok(fig, tmp_path, "campaign/scan_spend")


def test_budget_timing_figure_with_timing(tmp_path):
    events = [{"kind": "run", "payload": {"cost": 5.0e9}},
              {"kind": "timing", "payload": {"stage": "isar_run", "seconds": 30.0}},
              {"kind": "timing", "payload": {"stage": "wait_scan", "seconds": 2.0}},
              {"kind": "timing", "payload": {"stage": "propose", "seconds": 1.0}}]
    summary = {"budget": {"kind": "counting_seconds", "amount": 28800.0, "spent": 10.5,
                          "neutrons": 3.4e11}}
    fig = cd.budget_timing_figure(events, summary)
    _save_ok(fig, tmp_path, "campaign/budget_timing")


def test_budget_timing_figure_no_timing_fallback(tmp_path):
    fig = cd.budget_timing_figure([], {"budget": {"kind": "counting_seconds", "amount": 100.0,
                                                  "spent": 1.0}})
    _save_ok(fig, tmp_path, "campaign/budget_timing")


def _synthetic_record(tmp_path):
    card = spec_s30_raw.card_template()
    run = run_builder.build_s30_raw_run("FIG", n_scans=1, seed0=3, include_ascans=False)
    path = tmp_path / "FIG.spec"
    path.write_bytes(spec_s30_raw.emit_file(run, card))
    rec = next(r for r in spec_s30_raw.parse_file(path, card) if r.scan_type == "measurement")
    combine.reduce_record(rec, card)
    det = next(iter(rec.spectra))
    return rec, det


def test_scan_fit_figure_smoke(tmp_path):
    rec, det = _synthetic_record(tmp_path)
    row = {"scan": rec.scan_number, "detector": det, "omega0": 3.0, "gamma": 0.5, "zero": 0.0,
           "gauss_fwhm": 0.7, "lorentz_fwhm": 0.3, "T": 200.0, "disposition": "resolved",
           "verdict": "resolved", "interval_lo": 2.9, "interval_hi": 3.1, "interval_reliable": True}
    fig = cd.scan_fit_figure(rec, det, row)
    _save_ok(fig, tmp_path, "campaign/scan")
