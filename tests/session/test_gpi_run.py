"""The run path of the BL43 gpi files: the check at import, run start and a queued single-file fit, and the
named curve set in calibration and fitting.

Observed through a controlled worker: ``scan_delivery._process_one`` is replaced by a recorder that resolves the
unit's calibration as the real gate does and returns without fitting. Everything is generated at test time
(``tests/intake_fixtures.py``); every number is made up, none measured (hard rule 10).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import isar
import intake_fixtures as fx
from isar.intake import fit_inputs, gpi, inputs, scans, settings, values
from isar.parsers import sp8_bl43
from isar.session import calibrations, currentness, driver, file_fit, ingest, scan_delivery
from isar.session.state import ScanEntry

PATTERN = sp8_bl43.card_template().policy.calibration["resolution_pattern"]
STEM = f"{fx.STEM}_A"
Q = {"a03": 10.00, "a04": 11.46, "a05": 12.92}
HKL = {"a03": (0.5, 0.5, 3.0), "a04": (0.5, 0.5, 3.5), "a05": (0.5, 0.5, 4.0)}
NAMED = "sarf_z_s1_40_40"


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


class _Worker:
    """Stands in for ``_process_one``: records what it receives, never fits."""

    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(scan_delivery, "_process_one", self)

    def __call__(self, rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash, **_kw):
        cal = calibrations.resolve(rec, det, card, runcal)
        self.calls.append(SimpleNamespace(unit=scan_id, det=det, q=q, rec=rec, cal=cal))
        return ScanEntry(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                         status="held_for_calibration" if cal.held else "non_result",
                         content_hash=chash, guidelines_hash=ghash,
                         reasons=[cal.held_reason] if cal.held else ["controlled worker"],
                         provenance=cal.provenance())

    def by_det(self):
        return {c.det: c for c in self.calls}


def _experiment(tmp_path, *, data_q=Q, gpi_q=Q, gpi=True, named_skip=(), sarf_name=NAMED, other_named=False):
    """A fresh BL43 workspace (nothing saved) and its data folder holding one scan, ``rf/`` (the slit library
    ``sarf_t`` plus the named set ``sarf_z`` at 40x40, minus ``named_skip``) and, with ``gpi``, ``gpi_files/``."""
    ws = fx.workspace(tmp_path, sample="Smp")
    data = tmp_path / "data"
    fx.write_scan(data, stem=STEM, energy=fx.ENERGY_120, q_nm=data_q)
    fx.write_library(data / "rf")
    if gpi:
        fx.write_library(data / "rf", {"40x40": 1.25}, skip=[(d, "40x40") for d in named_skip], prefix="sarf_z")
        if other_named:
            fx.write_library(data / "rf", {"40x40": 1.35}, prefix="sarf_y")
        fx.write_gpi(data, STEM, q_nm=gpi_q, hkl=HKL, sarf_name=sarf_name)
    return ws, data


def _run(ws, data):
    return driver.run_batch(ws, [data], M=10, scan_M=10, errors_tier="fast", workers=1, formats=("markdown",))


def _import(ws, data):
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    return report


def _scan(ws):
    (key,) = scans.read_scans(ws.root)
    return key


def test_an_accepted_gpi_file_gives_a_fresh_workspaces_first_run_its_q_slit_and_named_curves(tmp_path, monkeypatch):
    ws, data = _experiment(tmp_path)
    worker = _Worker(monkeypatch)
    _run(ws, data)
    key = _scan(ws)
    check = gpi.read_gpi_check(ws.root)
    assert check.checked_by == "run" and check.scans[key].accepted
    s = settings.current_settings(ws.root)       # both folders discovered at this first run start
    assert Path(s.resolution_folder.path) == (data / "rf").resolve()
    assert Path(s.gpi_folder.path) == (data / "gpi_files").resolve()
    calls = worker.by_det()
    assert sorted(calls) == list(fx.ANALYZERS)
    for det, c in calls.items():
        assert c.q == list(HKL[det])                                            # the fit task's own Q
        assert (c.rec.spectra[det].q, c.rec.spectra[det].q_source) == (list(HKL[det]), "gpi")
        assert c.rec.facts["slit"]["value"] == "40x40" and c.rec.facts["slit"]["source"] == "gpi"
        assert c.rec.facts["resolution_set"]["value"] == NAMED
        assert not c.cal.held and c.cal.curve.source == f"{NAMED}_{det}" and c.cal.reference["named"] == NAMED
    card, session = ws.load()
    state = card.state.tabulated_calibration[key]
    assert state["named"] == NAMED and state["setting"] == "40x40" and state["setting_source"] == "declared"
    assert {d["curve"]["file"] for d in state["detectors"].values()} == {f"{NAMED}_{d}" for d in fx.ANALYZERS}
    assert session.get(f"{key}:a03").provenance["q"] == list(HKL["a03"])
    started = [ln for ln in fit_inputs.read_log(ws.root) if ln["kind"] == "started"]
    assert {ln["inputs"]["q"]["source"] for ln in started} == {"gpi"}


def test_import_checks_the_gpi_files_after_recording_the_rows_and_does_not_list_them_as_unread(tmp_path):
    ws, data = _experiment(tmp_path)
    report = _import(ws, data)
    key = _scan(ws)
    assert report.gpi["checked_by"] == "import" and report.gpi["scans"][key]["accepted"]
    assert gpi.read_gpi_check(ws.root).scans[key].q == {d: list(h) for d, h in HKL.items()}
    record = json.loads((ws.root / scans.INTAKE_SCANS).read_text(encoding="utf-8"))
    assert not [p for p in record.get("files", {}) if "gpi_files" in p]


def test_a_q_beyond_tolerance_takes_nothing_from_the_gpi_file(tmp_path, monkeypatch):
    ws, data = _experiment(tmp_path, data_q={**Q, "a04": Q["a04"] + 0.3})
    worker = _Worker(monkeypatch)
    _run(ws, data)
    entry = gpi.read_gpi_check(ws.root).scans[_scan(ws)]
    assert not entry.accepted and "a04" in entry.problem
    for det, c in worker.by_det().items():
        assert c.q is None and c.rec.spectra[det].q_source != "gpi"
        assert c.rec.facts["slit"].get("source") != "gpi" and "resolution_set" not in c.rec.facts
        assert "named" not in c.cal.reference and c.cal.curve.source.startswith("sarf_t_")


def test_the_later_sorting_named_set_is_used_and_its_missing_curve_holds_only_that_analyzer(tmp_path, monkeypatch):
    # sarf_t and sarf_z both hold 40x40 curves; the slit lookup keeps sarf_t's (first wins), the gpi names sarf_z
    ws, data = _experiment(tmp_path, named_skip=("a05",))
    worker = _Worker(monkeypatch)
    _run(ws, data)
    calls = worker.by_det()
    for det in ("a03", "a04"):
        assert not calls[det].cal.held and calls[det].cal.curve.source == f"{NAMED}_{det}"
    assert calls["a05"].cal.held and f"{NAMED}_a05" in calls["a05"].cal.held_reason     # held, never sarf_t's
    assert calls["a05"].q == list(HKL["a05"])                         # its Q and the slit are still taken
    assert calls["a05"].rec.facts["slit"]["source"] == "gpi"


def test_changing_only_sarf_name_makes_the_calibration_and_the_fit_stale(tmp_path, monkeypatch):
    ws, data = _experiment(tmp_path, other_named=True)
    worker = _Worker(monkeypatch)
    _run(ws, data)
    key = _scan(ws)
    units = [f"{key}:{d}" for d in fx.ANALYZERS]
    assert all("fast" in v for v in currentness.current_passes(ws.root, units).values())
    card, _s = ws.load()
    stored_key = card.state.tabulated_calibration[key]["inputs_key"]
    fx.write_gpi(data, STEM, q_nm=Q, hkl=HKL, sarf_name="sarf_y_s1_40_40")    # rf/ itself is unchanged
    _import(ws, data)
    assert currentness.current_passes(ws.root, units) == {u: frozenset() for u in units}
    worker.calls.clear()
    _run(ws, data)
    card, _s = ws.load()
    state = card.state.tabulated_calibration[key]
    assert state["inputs_key"] != stored_key and state["named"] == "sarf_y_s1_40_40"
    assert {c.cal.curve.source for c in worker.calls} == {f"sarf_y_s1_40_40_{d}" for d in fx.ANALYZERS}


def test_a_user_slit_beats_the_gpi_slit_and_sets_the_named_set_aside(tmp_path, monkeypatch):
    ws, data = _experiment(tmp_path)
    _import(ws, data)
    key = _scan(ws)
    assert values.save_value(ws.root, "slit", "60x60", [key]).ok
    worker = _Worker(monkeypatch)
    _run(ws, data)
    card, _s = ws.load()
    assert "named" not in card.state.tabulated_calibration[key]
    for det, c in worker.by_det().items():
        assert c.rec.facts["slit"]["value"] == "60x60" and c.rec.facts["slit"]["source"] == "user"
        assert "resolution_set" not in c.rec.facts
        assert c.cal.curve.source == f"sarf_t_s1_60_60_{det}"              # slit matching on the user's slit
        assert c.q == list(HKL[det])                                         # the gpi Q still stands


def test_no_gpi_folder_is_todays_run(tmp_path, monkeypatch):
    ws, data = _experiment(tmp_path, gpi=False)
    worker = _Worker(monkeypatch)
    _run(ws, data)
    key = _scan(ws)
    check = gpi.read_gpi_check(ws.root)
    assert (check.folder, check.scans) == (None, {}) and settings.current_settings(ws.root).gpi_folder is None
    card, _s = ws.load()
    state = card.state.tabulated_calibration[key]
    row = scans.read_scans(ws.root)[key]
    assert "named" not in state and state["inputs_key"] == inputs.calibration_inputs_key(
        data_revision=row["data_revision"], folder=state["folder"], pattern=PATTERN, library=state["library"],
        declared_slit=None, analyzers=list(fx.ANALYZERS), counts_accepted=[])
    for det, c in worker.by_det().items():
        assert c.q is None and c.rec.spectra[det].q_source == "none" and "resolution_set" not in c.rec.facts
        assert "named" not in c.cal.reference


def test_a_queued_fit_rechecks_and_uses_no_gpi_value_after_a_q_changed_in_the_file(tmp_path, monkeypatch):
    ws, data = _experiment(tmp_path)
    _import(ws, data)                                       # the queue fits imported files
    worker = _Worker(monkeypatch)
    _run(ws, data)
    key = _scan(ws)
    assert gpi.read_gpi_check(ws.root).scans[key].accepted
    fx.write_scan(data, stem=STEM, energy=fx.ENERGY_120, q_nm={**Q, "a04": Q["a04"] + 0.3})   # no re-import
    worker.calls.clear()
    got = []
    file_fit.fit_file(ws.root, file_fit.FileItem("i1", f"{key}:a03", "fast", 1), progress=None, emit=got.append)
    (c,) = worker.calls
    assert c.q is None and c.rec.spectra["a03"].q_source != "gpi" and "resolution_set" not in c.rec.facts
    assert c.rec.facts["slit"].get("source") != "gpi" and c.cal.curve.source.startswith("sarf_t_")
    check = gpi.read_gpi_check(ws.root)
    assert check.checked_by == "fit" and not check.scans[key].accepted and "a04" in check.scans[key].problem
    assert [r.status for r in got if r.pass_kind == "fast"]
