"""Q-less, per-analyzer-exposure spectra (BL43LXU shape) through collation and `isar run`.

The collation tests use records built from a generated S30 scan by dropping each spectrum's Q (rung 4),
giving it its own monitor/time, and storing NaN at record level -- exactly what the BL43LXU reader
promises. The run tests go through the real reader: files written at test time with
`sp8_bl43.emit_file`, the shipped `bl43lxu.policy.toml`, real discovery and grouping.
Nothing here reads `real_data/`.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

import isar
from isar.parsers import sp8_bl43, spec_s30
from isar.reduce import collate
from isar.schema import axes, scan
from isar.session import driver
from isar.session.workspace import Workspace
from isar.synth_format import run_builder

# Stand-in policy for the collation tests only: S30 records carry the BL43 shape, so collate can be
# driven without files. The run tests below use the shipped template instead.
_POLICY = """\
facility = "SPring-8"
instrument = "BL43LXU"
format_version = "1"
plugin = "spec_s30"
energy_column = "HerixE"
monitor_column = "ICO-C"
time_column = "Seconds"
counting_mode = "time"
normalize_by = "monitor"
raw_abscissa_kind = "energy"
energy_status = "needs_zero"
variance_convention = "none"
min_points = 5
hkl_round_step = 0.05
measurement_pattern = "fscan"
alignment_pattern = "ascan"
exclusions_seed = []

[temperature_rule]
setpoint_field = "T_Control"
sensor_fields = ["T_Sample"]
divergence_tol = 5.0

[collation]
key = ["sample", "q", "T"]
merge = "{merge}"

[calibration]
method = "tabulated_elastic"
zero_fallback = "file_axis"

[fitting]
min_fit_points = 6

[[detectors]]
detector_id = "ana1"
value_index = 4
signal_column = "Ana1_3std"
enabled = true

[[detectors]]
detector_id = "ana2"
value_index = 5
signal_column = "Ana2_3std"
enabled = true
"""


def _base_records(tmp_path, n_scans=2):
    run = run_builder.build_s30_run("SYN", n_scans=n_scans, seed0=1000)
    p = tmp_path / "SYN.spec_supplementary.dat"
    p.write_bytes(spec_s30.emit_file(run, spec_s30.card_template()))
    recs = spec_s30.parse_file(p, spec_s30.card_template())
    for r in recs:
        r.spectra = {d: r.spectra[d] for d in ("ana1", "ana2")}
    return recs


def _qless(rec, *, nan_temperature=False):
    """Rewrite one parsed scan to the BL43LXU shape: no Q, per-analyzer exposure, NaN at record level."""
    n = len(rec.spectra["ana1"].intensity)
    for k, sp in enumerate(rec.spectra.values(), start=1):
        sp.q, sp.q_source = None, "none"
        sp.monitor = np.full(n, 1000.0 * k)
        sp.time = np.full(n, 10.0 * k)
    rec.normalization.monitor = np.full(n, np.nan)
    rec.normalization.time = np.full(n, np.nan)
    if nan_temperature:
        rec.temperature.setpoint = np.full(n, np.nan)
    return rec


def _card_for(tmp_path, merge="separate"):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(_POLICY.format(merge=merge), encoding="utf-8")
    return ws


def test_import_root_is_the_worktree():
    # A sibling checkout on sys.path would make every assertion below test the wrong code.
    assert Path(isar.__file__).resolve().parent.parent == Path(__file__).resolve().parents[2]


def test_qless_spectra_never_pool_across_scans(tmp_path):
    recs = [_qless(r) for r in _base_records(tmp_path)]
    ws = _card_for(tmp_path, merge="merge")
    card, _ = ws.load()
    col = collate.collate(recs, card)
    # 2 scans x 2 analyzers, none shares a group, and a lone Q-less spectrum gets no master.
    assert len(col.groups) == 4
    assert all(len(g.members) == 1 for g in col.groups)
    assert all(g.merged is None and g.pool_refusal == collate.NO_Q for g in col.groups)
    assert col.coverage.values == []          # no Q was measured, so there is nothing to map


def test_nan_temperature_forms_no_cross_scan_group(tmp_path):
    recs = _base_records(tmp_path)
    for r in recs:                            # keep the Q (identical in both scans); lose only T
        r.temperature.setpoint = np.full(len(r.spectra["ana1"].intensity), np.nan)
    ws = _card_for(tmp_path, merge="separate")
    card, _ = ws.load()
    col = collate.collate(recs, card)
    assert len(col.groups) == 4 and all(len(g.members) == 1 for g in col.groups)
    # Control: the same two scans with a temperature do share groups.
    recs2 = _base_records(tmp_path)
    assert len(collate.collate(recs2, card).groups) == 2


def test_pooled_master_uses_per_analyzer_monitor_and_skips_absent_bins(tmp_path):
    a, b = _base_records(tmp_path)
    n = len(a.spectra["ana1"].intensity)
    for r, k in ((a, 1.0), (b, 2.0)):
        r.spectra = {"ana1": r.spectra["ana1"]}
        r.spectra["ana1"].monitor = np.full(n, 100.0 * k)
        r.spectra["ana1"].time = np.full(n, 5.0 * k)
        r.normalization.monitor = np.full(n, np.nan)   # a missed consumer would see no measure
        r.normalization.time = np.full(n, np.nan)
    # Bin 3 is absent in scan b: zero exposure, masked, and a stored count of 0 (not measured).
    sp_b = b.spectra["ana1"]
    sp_b.monitor[3] = 0.0
    sp_b.mask[3] = False
    sp_b.intensity[3] = 0.0
    sp_b.variance[3] = 0.0
    ws = _card_for(tmp_path, merge="merge")
    card, _ = ws.load()
    col = collate.collate([a, b], card)
    (g,) = [g for g in col.groups if len(g.members) == 2]
    m = g.merged
    assert np.allclose(m.monitor[np.arange(n) != 3], 300.0)          # 100 + 200, the analyzers' own
    assert m.monitor[3] == 100.0                                      # only scan a measured bin 3
    assert m.intensity[3] == a.spectra["ana1"].intensity[3]           # b's absent zero adds nothing
    assert m.mask.all()


def test_exposure_for_is_what_collate_reads(tmp_path):
    (rec,) = [_qless(r) for r in _base_records(tmp_path, n_scans=1)]
    refs = collate._spectrum_refs(rec, 0.05, 0.1)
    for ref in refs:
        want = scan.exposure_for(rec, ref.detector_id)
        assert np.array_equal(ref.normalization.monitor, want.monitor)
        assert not np.isnan(ref.normalization.monitor).any()
        assert ref.q is None


def _bl43_folder(tmp_path, n_scans=2):
    """Write whole, aligned BL43LXU analyzers (a03, a04) per scan with `emit_file`; return the folder."""
    data = tmp_path / "data"
    for i, rec in enumerate(_qless(r) for r in _base_records(tmp_path, n_scans)):
        rec.spectra = {"a03": rec.spectra["ana1"], "a04": rec.spectra["ana2"]}
        ax = axes.scanned_axes(rec)[0]
        ax.values = np.round(np.arange(len(ax.values)) * 0.2 - 2.0, 4)   # the reader wants the 0.2 meV bin lattice
        rec.facts = {"scan_label": {"value": f"Smp_100K_1_0p5_m2_run{'AB'[i]}", "source": "filename"},
                     "T_K": {"value": 100.0, "source": "filename"}}
        sp8_bl43.emit_file(rec, sp8_bl43.card_template(), data)
    return data


def _bl43_ws(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(
        (Path(sp8_bl43.__file__).parent / "cards" / "bl43lxu.policy.toml").read_text(encoding="utf-8"),
        encoding="utf-8")
    return ws


def test_run_holds_qless_scan_for_want_of_a_zero(tmp_path):
    ws = _bl43_ws(tmp_path)
    summary = driver.run_batch(ws, [_bl43_folder(tmp_path)], M=10)
    _card, session = ws.load()
    assert len(session.scans) == 4                               # 2 scans x 2 analyzers
    for entry in session.scans.values():
        assert entry.status == "held_for_calibration"
        assert entry.reasons and "no energy zero" in entry.reasons[0].lower()
        assert entry.provenance["q"] is None                    # never a fake Q
        assert entry.provenance["q_source"] == "none" and entry.provenance["q_rung"] == 4
    assert summary.collation is not None                         # collation did not fail either
    assert not any("collation skipped" in line for line in summary.context_lines)


def test_second_run_is_idempotent_for_held_qless_scans(tmp_path):
    ws = _bl43_ws(tmp_path)
    data = _bl43_folder(tmp_path, n_scans=1)
    driver.run_batch(ws, [data], M=10)
    driver.run_batch(ws, [data], M=10)
    _card, session = ws.load()
    assert {e.status for e in session.scans.values()} == {"held_for_calibration"}


def test_report_and_scene_after_a_qless_run(tmp_path):
    # What `isar run` does after run_batch: the human report and the scene build must not fail.
    from isar.report import run_report
    from isar.session import scene_hook
    ws = _bl43_ws(tmp_path)
    driver.run_batch(ws, [_bl43_folder(tmp_path, n_scans=1)], M=10)
    card, session = ws.load()
    run_report.write_run_report(session, card, ws, formats=("markdown",))
    scene_hook.build_and_store(ws.root)
