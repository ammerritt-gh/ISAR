"""Step-6 gate (UNIFICATION_DESIGN §10.6): the run report renders from the ledger with no gold CSV
and no refitting, for any instrument (the code has zero instrument branches)."""
from isar.report import run_report
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace
from isar.schema import card as card_mod


def _card(instrument="S30", energy_status="needs_zero"):
    pol = card_mod.CardPolicy(
        facility="APS", instrument=instrument, format_version="2025", plugin="spec_s30",
        detectors=[], energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status=energy_status,
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={})
    return card_mod.Card(pol, card_mod.CardState())


def _session_with_outcomes():
    st = SessionState()
    st.last_run = {"scan_gaps": ["E: 170->172"]}
    # two fitted + one held + one excluded
    for scan, om, disp, verdict, k in [(168, 5.70, "resolved", "resolved", 0.2),
                                       (198, 0.88, "flagged", "model_incomplete", 0.05)]:
        st.put(ScanEntry(
            scan_id=f"E:{scan}:ana7", scan_number=scan, scan_type="measurement", status="fitted",
            detectors={"ana7": {
                "detector_id": "ana7", "verdict": verdict, "disposition": disp,
                "confidence": 0.8, "omega0": om, "omega0_err": 0.03,
                "interval": [om - 0.03, om, om + 0.03], "modality": "fold", "gamma": 1.0,
                "gamma_provisional": False, "best_label": "elastic+stokes+bg",
                "cost_per_dof": 1.2, "q": [3.0, k, -0.1], "T": 100.0, "sample": "S",
                "prior_used": {"omega0": om, "sigma": 0.3, "n_neighbors": 3, "source": "auto"},
                "calib": {"center": -0.1, "gauss_fwhm": 0.9, "lorentz_fwhm": 0.5,
                          "zero_source": "interpolated", "resolution_source": "measured_slit_18",
                          "resolution_version": "v1"},
                "reasons": []}}))
    st.put(ScanEntry(scan_id="E:343:ana7", scan_number=343, scan_type="measurement",
                     status="held_for_calibration", reasons=["no energy zero in calibration coverage"]))
    return st


def test_run_report_writes_all_formats(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    card, session = _card(), _session_with_outcomes()
    written = run_report.write_run_report(session, card, ws, formats=("html", "markdown"))

    assert written["markdown"].exists() and written["html"].exists() and written["json"].exists()
    assert (ws.report_dir / "figures" / "dispersion.png").exists()

    md = written["markdown"].read_text(encoding="utf-8")
    assert "Triage table" in md
    assert "5.70" in md and "0.88" in md            # both fitted omega0 in the table
    assert "held_for_calibration" in md              # the held scan is surfaced
    assert "170->172" in md                          # the gap is reported

    html = written["html"].read_text(encoding="utf-8")
    assert "<table>" in html and "dispersion.png" in html


def test_run_report_tas_gamma_caveat(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    card = _card(instrument="IN8", energy_status="absolute")
    run_report.write_run_report(card=card, session=SessionState(), workspace=ws, formats=("markdown",))
    md = (ws.report_dir / "report.md").read_text(encoding="utf-8")
    assert "Γ" in md and "statistical-only" in md    # the TAS honesty caveat fires on absolute-axis cards


def _master_oc(disposition, verdict, *, members, n_members):
    oc = {"detector_id": "merged", "verdict": verdict, "disposition": disposition,
          "confidence": 0.9, "omega0": 1.0, "omega0_err": 0.02, "interval": [0.9, 1.0, 1.1],
          "modality": "single", "gamma": 0.3, "gamma_provisional": False,
          "best_label": "elastic+stokes+bg", "cost_per_dof": 1.0, "q": [2.2, 0.0, 0.0],
          "T": 100.0, "sample": "S", "prior_used": None,
          "calib": {"resolution_source": "measured"}, "reasons": [],
          "pooled": True, "n_members": n_members}
    if members is not None:
        oc["members"] = members
    return oc


def _session_with_master(oc):
    st = SessionState()
    st.put(ScanEntry(scan_id="master:S_2.2_100.0", scan_number=9_000_001, scan_type="measurement",
                     status="fitted", detectors={"merged": oc}))
    return st


def test_pooled_reject_renders_uncertifiable_not_junk(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    st = _session_with_master(_master_oc("reject", "junk", members=["473", "493"], n_members=2))
    run_report.write_run_report(st, _card(), ws, formats=("markdown",))
    md = (ws.report_dir / "report.md").read_text(encoding="utf-8")
    assert "pooled — uncertifiable" in md
    assert "junk" not in md                              # the raw reject/junk label is overridden


def test_delivered_pooled_renders_member_count_and_chain(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    st = _session_with_master(_master_oc("resolved", "clear_phonon",
                                         members=["1", "2", "3"], n_members=3))
    written = run_report.write_run_report(st, _card(), ws, formats=("markdown",))
    md = (ws.report_dir / "report.md").read_text(encoding="utf-8")
    assert "pooled (master, 3 members)" in md            # triage display
    assert "Pooled masters (chain of custody)" in md     # the new section
    assert "1; 2; 3" in md                               # member list in the chain-of-custody table
    import json
    rep = json.loads(written["json"].read_text(encoding="utf-8"))
    entry = next(s for s in rep["scans"] if s["scan"] == 9_000_001)
    assert entry["members"] == ["1", "2", "3"] and entry["n_members"] == 3


def test_delivered_pooled_without_members_raises(tmp_path):
    import pytest
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    st = _session_with_master(_master_oc("resolved", "clear_phonon", members=None, n_members=3))
    with pytest.raises(ValueError, match="no member provenance"):
        run_report.write_run_report(st, _card(), ws, formats=("markdown",))


def test_run_report_empty_ledger_ok(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    run_report.write_run_report(session=SessionState(), card=_card(), workspace=ws, formats=("markdown",))
    md = (ws.report_dir / "report.md").read_text(encoding="utf-8")
    assert "No fitted scans this run." in md
