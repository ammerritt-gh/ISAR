"""`isar export`: a synthetic workspace exported and read back (hard rule 10: hand-built outcomes,
unit data and a curve table generated here; no fitting).

Spectra A to I cover fitted, zero-mode, held, never-fitted, a changed curve, and the deep-pass
shapes: resolved with MINOS bounds, refused, no successful refit, an older fit with no fast-pass
record, and a chosen refit whose errors failed. Both passes are exported side by side; nothing picks one.
"""
import csv
import json
import shutil
import types
from datetime import datetime
from pathlib import Path

import pytest

from isar.analyze.resolution import file_stamp
from isar.report import export as export_mod
from isar.session import ingest
from isar.session.state import ScanEntry, SessionState
from isar.session.unit_data import unit_data_path
from isar.session.workspace import Workspace

CARD = Path(__file__).resolve().parents[2] / "isar" / "parsers" / "cards" / "bl43lxu.policy.toml"
NOW = datetime(2026, 10, 27, 12, 0, 0)
CENTER = 0.5
_X = [-1.0, 0.0, 1.0, 2.0, 3.0]
_COUNTS = [4.0, 0.0, 9.0, 16.0, 25.0]


def _fast(state, n, *, preferred=None):
    peaks = [{"index": i, "confident": True, "height": 10.0 + i, "status": {}} for i in range(n)]
    return {"schema": "isar.fast-pass/1", "state": state,
            "preferred_count": n if preferred is None else preferred,
            "count_probabilities": {str(n): 0.9}, "peaks": peaks, "flags": [], "models": [],
            "deep_pass_candidate": state != "resolved"}


def _mode(omega0, err=0.1):
    return {"omega0": omega0, "omega0_err": err, "gamma": 0.4, "gamma_err": 0.05,
            "amplitude": 50.0, "amplitude_intrinsic": 25.0, "hesse_trusted": True}


def _deep(state, chosen, *, trusted=False, reason="r"):
    return {"schema": "isar.deep-pass/1", "state": state, "reason": reason, "trusted": trusted,
            "calibration": "provisional: test", "best_count": 1, "p_best": 0.9,
            "count_probabilities": {"1": 0.9}, "close_call": {"numerical": False},
            "chosen": chosen}


def _oc(det, *, modes, fast=None, deep=None, tabulated=None, exposure=1e5, source="target_window"):
    oc = {"detector_id": det, "verdict": "resolved", "disposition": "resolved", "T": 50.0,
          "q": [1.0, 0.0, 0.0], "sample": "S", "best_label": f"{len(modes or [])}dho-fast",
          "n_modes": len(modes or []), "modes": modes,
          "calib": {"center": CENTER, **({"tabulated": tabulated} if tabulated else {})},
          "effective_exposure": exposure,
          "exposure_provenance": {"value": exposure, "source": source,
                                  "normalization_source": "monitor"}}
    if fast is not None:
        oc["fast_pass"] = fast
    if deep is not None:
        oc["deep_pass"] = deep
    return oc


def _curve(folder: Path, name: str) -> dict:
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text("# energy value\n-2 0.1\n-1 0.5\n0 1.0\n1 0.5\n2 0.1\n",
                    encoding="utf-8", newline="\n")
    return {"folder": str(folder), "file": name, "stamp": file_stamp(path), "detector": "ana",
            "setting": "60x60"}


def _unit(data_dir: Path, n: int, status="needs_zero"):
    path = unit_data_path(data_dir, "E", n, "ana")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "isar.unit_data/1", "scan_id": f"E:{n}:ana", "experiment_id": "E",
               "scan_number": n, "detector_id": "ana", "x": _X, "counts": _COUNTS,
               "variance": _COUNTS, "mask": [True, True, False, True, True],
               "axis": {"name": "meV", "kind": "energy", "units": "meV",
                        "raw_abscissa_kind": "energy", "calibration_status": status}}
    path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")


A_PARAMS = {"phonon2.omega0": 9.0, "phonon2.Gamma": 0.6, "phonon2.h": 3.0,
            "phonon0.omega0": 3.0, "phonon0.Gamma": 0.2, "phonon0.h": 10.0,
            "phonon1.omega0": 6.0, "phonon1.Gamma": 0.4, "phonon1.h": 5.0,
            "elastic.h": 100.0, "bg.c": 1.0}
A_DEEP = _deep("resolved", {
    "label": "3dho", "status": "reference", "source": "nested_refit", "params": A_PARAMS,
    "errors": {"minos": {"phonon0.omega0": [2.9, 3.1, True], "phonon0.Gamma": [0.1, 0.3, False]}},
    "errors_failure": None}, reason="resolved: count 3 (P 0.90); provisional")
A_DEEP["best_count"] = 3
F_DEEP = {"schema": "isar.deep-pass/1", "state": "refused", "reason": "no fast-pass record",
          "trusted": False, "calibration": "provisional: test"}
G_DEEP = _deep("resolved", {"label": "1dho", "status": "reference", "source": None, "params": None,
                            "errors": None, "errors_failure": "no refit of the chosen model succeeded"})
I_DEEP = _deep("close_call", {"label": "1dho", "status": "reference", "source": "nested_refit",
                              "params": {"p.omega0": 4.0, "p.Gamma": 0.3, "p.h": 2.0},
                              "errors": None, "errors_failure": "refit did not converge"},
               trusted=True)


def _entry(n, status="fitted", det_oc=None, **kw):
    return ScanEntry(scan_id=f"E:{n}:ana", scan_number=n, scan_type="measurement", status=status,
                     content_hash=f"c{n}", detectors={"ana": det_oc} if det_oc else {}, **kw)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    shutil.copyfile(CARD, root / "card.policy.toml")
    (root / "card.state.json").write_text("{}", encoding="utf-8", newline="\n")
    rf = tmp_path / "rf"
    ref = _curve(rf, "a.dat")
    ref_e = _curve(rf, "e.dat")
    data = root / "data"
    st = SessionState()
    st.put(_entry(1, det_oc=_oc("ana", modes=[_mode(5.0, err=None), _mode(2.0)],
                                fast=_fast("close_call", 2), deep=A_DEEP, tabulated=ref),
                  measurement_record={"content_hash": "h1"}))
    st.scans["E:1:ana"].content_hash = "h1"
    st.put(_entry(2, det_oc=_oc("ana", modes=None, fast=_fast("resolved", 0), exposure=None,
                                source="unavailable", tabulated=ref)))
    st.put(_entry(3, status="held_for_calibration", reasons=["no energy zero"]))
    st.put(_entry(4, status="ingested"))
    st.put(_entry(5, det_oc=_oc("ana", modes=[_mode(4.0)], fast=_fast("resolved", 1),
                                tabulated=ref_e)))
    st.put(_entry(6, det_oc=_oc("ana", modes=[_mode(4.0)], fast=_fast("search_limited", 1),
                                deep=F_DEEP)))
    st.put(_entry(7, det_oc=_oc("ana", modes=[_mode(4.0)], fast=_fast("unresolved", 1),
                                deep=G_DEEP)))
    st.put(_entry(8, det_oc=_oc("ana", modes=[_mode(4.0)])))
    st.put(_entry(9, det_oc=_oc("ana", modes=[_mode(4.0)], fast=_fast("close_call", 1),
                                deep=I_DEEP)))
    st.save(root / "session.json")
    for n in range(1, 10):
        if n != 3:
            _unit(data, n)
    (rf / "e.dat").write_text("# changed\n-2 0.2\n0 1.0\n2 0.2\n1 0.4\n",
                              encoding="utf-8", newline="\n")
    return root


def _read(folder: Path):
    doc = json.loads((folder / "fits.json").read_text(encoding="utf-8"))
    with open(folder / "modes.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return doc, {r["spectrum_id"]: r for r in doc["spectra"]}, rows


def _spectrum(folder: Path, rel: str):
    lines = (folder / rel).read_text(encoding="utf-8").splitlines()
    header = [ln[2:] for ln in lines if ln.startswith("# ")]
    body = list(csv.DictReader([ln for ln in lines if not ln.startswith("#")]))
    return header, lines[len(header)], body


def _rows(rows, n, pass_=None):
    return [r for r in rows if r["scan_id"] == f"E:{n}:ana" and (pass_ is None or r["pass"] == pass_)]


def test_every_spectrum_has_a_record_and_both_passes_are_labelled(ws):
    summary = export_mod.export_workspace(ws, now=NOW)
    doc, rec, rows = _read(summary.folder)
    # (a) one record per spectrum, the fast state as built, deep null exactly where none is stored
    assert len(doc["spectra"]) == 9 and summary.n_spectra == 9
    assert [rec[f"E:{n}:ana"]["fast"]["state"] for n in range(1, 10)] == [
        "close_call", "resolved", "held", "fitting", "resolved", "search_limited", "unresolved",
        "not recorded", "close_call"]
    assert [n for n in range(1, 10) if rec[f"E:{n}:ana"]["deep"] is None] == [2, 3, 4, 5, 8]
    counts = doc["counts"]
    assert counts["by_state"]["fast"] == {"close_call": 2, "resolved": 2, "held": 1, "fitting": 1,
                                          "search_limited": 1, "unresolved": 1, "not recorded": 1}
    assert counts["by_state"]["deep"] == {"resolved": 2, "refused": 1, "close_call": 1}
    assert counts["with_deep_pass"] == 4
    assert counts["modes"] == counts["modes_fast"] + counts["modes_deep"] == summary.n_modes

    # (b) the rows
    assert len(_rows(rows, 1, "fast")) == 2 and len(_rows(rows, 1, "deep")) == 3
    for n in (2, 3, 4):
        assert _rows(rows, n) == []
    for n in (5, 6, 7, 8, 9):
        assert len(_rows(rows, n, "fast")) == 1
    assert _rows(rows, 6, "deep") == [] and _rows(rows, 7, "deep") == []
    assert _rows(rows, 8)[0]["count_state"] == "not recorded"
    assert len(_rows(rows, 9, "deep")) == 1
    assert all(r["pass"] in ("fast", "deep") and r["count_state"] for r in rows)
    assert [r["omega0_err"] for r in _rows(rows, 1, "fast")] == ["0.1", ""]   # missing stays empty

    # (c) both passes, no winner
    for r in _rows(rows, 1, "fast"):
        assert (r["count_state"], r["trust"]) == ("close_call", "not assessed")
        assert r["omega0_lo"] == r["omega0_hi"] == r["gamma_lo"] == r["gamma_hi"] == ""
    deep = _rows(rows, 1, "deep")
    assert [r["mode_index"] for r in deep] == ["0", "1", "2"]
    assert [float(r["omega0"]) for r in deep] == [3.0, 6.0, 9.0]
    assert [float(r["gamma"]) for r in deep] == [0.2, 0.4, 0.6]
    assert all((r["count_state"], r["trust"], r["best_model"]) == ("resolved", "untrusted", "3dho")
               for r in deep)
    assert (float(deep[0]["omega0_lo"]), float(deep[0]["omega0_hi"])) == (2.9, 3.1)
    assert deep[0]["gamma_lo"] == deep[0]["gamma_hi"] == "" and deep[0]["uncertainty_kind"] == "minos"
    assert deep[1]["uncertainty_kind"] == ""
    assert all(r["amplitude"] == r["height"] == r["peak_position"] == "" for r in deep)
    a = rec["E:1:ana"]
    stored = SessionState.load(ws / "session.json").scans["E:1:ana"].detectors["ana"]
    assert a["fast_pass"] == stored["fast_pass"] and a["deep_pass"] == stored["deep_pass"]
    assert a["deep"]["chosen_label"] == "3dho" and a["deep"]["trust"] == "untrusted"
    text = (summary.folder / "fits.json").read_text(encoding="utf-8")
    assert not any(k in text for k in ("result_rule", "second_opinion", "result_source"))
    assert "result_source" not in rows[0] and doc["passes"]["exported"] == ["fast", "deep"]

    # (k) deep with no rows, reason recorded; trust read from the record
    f, g, i = rec["E:6:ana"]["deep"], rec["E:7:ana"]["deep"], rec["E:9:ana"]["deep"]
    assert (f["state"], f["state_reason"], f["n_modes_exported"]) == (
        "refused", "no fast-pass record", 0)
    assert (g["chosen_failure"], g["n_modes_exported"]) == ("no refit of the chosen model succeeded", 0)
    i_row, = _rows(rows, 9, "deep")
    assert i_row["trust"] == "trusted" and i_row["uncertainty_kind"] == ""
    assert i_row["omega0_lo"] == i_row["gamma_hi"] == ""
    assert i["chosen_failure"] == "refit did not converge"


def test_spectrum_files_curves_exposure_and_problems(ws):
    summary = export_mod.export_workspace(ws, now=NOW)
    folder = summary.folder
    doc, rec, _rows_ = _read(folder)
    # (d) every named file exists inside the folder
    for r in doc["spectra"]:
        if r["spectrum_file"]:
            assert (folder / r["spectrum_file"]).is_file()
    assert doc["resolution_curves"] and all((folder / c["file"]).is_file()
                                            for c in doc["resolution_curves"].values())
    a_head, a_cols, a_body = _spectrum(folder, rec["E:1:ana"]["spectrum_file"])
    assert [float(r["energy_meV"]) for r in a_body] == [x - CENTER for x in _X]
    assert [r["mask"] for r in a_body] == ["1", "1", "0", "1", "1"]
    assert a_body[1]["sigma"] == "0.0"                     # sigma(0) = 0, no floor
    d_head, d_cols, d_body = _spectrum(folder, rec["E:4:ana"]["spectrum_file"])
    assert all(r["energy_meV"] == "" for r in d_body)    # needs_zero and no fit: never E0 = 0
    # (e) exposure is one header value, never a column
    for r in doc["spectra"]:
        if r["spectrum_file"]:
            assert _spectrum(folder, r["spectrum_file"])[1] == "x_raw,energy_meV,counts,sigma,mask"
    assert "exposure: 100000.0" in a_head and "exposure_source: target_window" in a_head
    assert rec["E:1:ana"]["exposure"]["value"] == 1e5
    b_head = _spectrum(folder, rec["E:2:ana"]["spectrum_file"])[0]
    assert "exposure: not stored" in b_head and "exposure_reason: unavailable" in b_head
    assert "exposure: not stored" in d_head and "exposure_reason: no stored fit" in d_head
    assert rec["E:4:ana"]["exposure"] is None
    # (f) a curve changed since the fit: a problem, no CSV, the export still succeeds
    e = rec["E:5:ana"]
    assert "changed since the fit" in e["resolution_problem"] and e["resolution_curve"] is None
    assert any(p["spectrum_id"] == "E:5:ana" for p in doc["problems"])
    assert len(doc["resolution_curves"]) == 1 == summary.n_curves
    # the held spectrum has no unit data: no file, and its problem named
    assert rec["E:3:ana"]["spectrum_file"] is None and rec["E:3:ana"]["spectrum_problem"]
    assert rec["E:3:ana"]["resolution_curve"] is None and "resolution_problem" not in rec["E:3:ana"]
    assert (folder / "README.txt").read_bytes().count(b"\r\n") > 10


def test_exports_never_overwrite_and_leave_no_partial(ws):
    first = export_mod.export_workspace(ws, now=NOW).folder
    before = {p: p.read_bytes() for p in first.rglob("*") if p.is_file()}
    second = export_mod.export_workspace(ws, now=NOW).folder
    assert first.name == "export-20261027-120000" and second.name == "export-20261027-120000-2"
    assert {p: p.read_bytes() for p in first.rglob("*") if p.is_file()} == before
    assert json.loads((second / "fits.json").read_text(encoding="utf-8"))["folder"] == second.name
    assert not list((ws / "report" / "exports").glob(".partial-*"))


def _no_bare_constants(name):
    raise AssertionError(f"fits.json holds a bare {name} token")


def test_fits_json_holds_null_where_a_stored_value_is_not_finite(tmp_path):
    export_mod._write_fits(tmp_path, {"a": float("nan"), "b": [1.0, float("inf")],
                                      "c": {"d": (float("-inf"), 2.0)}, "e": 3})
    doc = json.loads((tmp_path / "fits.json").read_text(encoding="utf-8"),
                     parse_constant=_no_bare_constants)
    assert doc == {"a": None, "b": [1.0, None], "c": {"d": [None, 2.0]}, "e": 3}


def test_an_export_of_a_stored_nan_is_valid_json(ws):
    state = SessionState.load(ws / "session.json")
    state.scans["E:5:ana"].detectors["ana"]["exposure_provenance"]["value"] = float("nan")
    state.save(ws / "session.json")
    folder = export_mod.export_workspace(ws, now=NOW).folder
    text = (folder / "fits.json").read_text(encoding="utf-8")
    assert "NaN" not in text
    doc = json.loads(text, parse_constant=_no_bare_constants)
    assert doc["spectra"]  # the export still holds its spectra


def test_a_failed_export_leaves_no_new_folder(ws, monkeypatch):
    def boom(folder, bl43):
        raise RuntimeError("disk full")
    monkeypatch.setattr(export_mod, "_write_readme", boom)
    with pytest.raises(RuntimeError, match="disk full"):
        export_mod.export_workspace(ws, now=NOW)
    assert list((ws / "report" / "exports").iterdir()) == []


def test_export_reads_only(ws):
    session, state = (ws / "session.json").read_bytes(), (ws / "card.state.json").read_bytes()
    export_mod.export_workspace(ws, now=NOW)
    assert (ws / "session.json").read_bytes() == session
    assert (ws / "card.state.json").read_bytes() == state


def test_the_ledger_is_read_before_the_first_progress_call(ws):
    # The GUI's write hold ends at the first progress line, so a result saved then must not reach this export.
    smaller = SessionState.load(ws / "session.json")
    del smaller.scans["E:9:ana"]
    calls = []

    def progress(i, n):
        if not calls:
            smaller.save(ws / "session.json")       # the queue's writer lands a ledger without one spectrum
        calls.append(i)
    folder = export_mod.export_workspace(ws, now=NOW, progress=progress).folder
    doc, rec, _rows = _read(folder)
    assert set(rec) == {f"E:{n}:ana" for n in range(1, 10)} and len(calls) == 9


def test_fit_provenance_survives_a_reingest(ws):
    # (j) a re-ingest with new content after the fit: the fit input stays h1, the data is h2
    w = Workspace.open(ws)
    card, session = w.load()
    rec_ns = types.SimpleNamespace(experiment_id="E", scan_number=1, scan_type="measurement")
    assert ingest.upsert_ingested_entry(session, rec_ns, "ana", chash="h2", prov={}) == "updated"
    w.save(card, session)
    folder = export_mod.export_workspace(ws, now=NOW).folder
    doc, rec, rows = _read(folder)
    a = rec["E:1:ana"]
    assert a["fit_input"]["recorded"] is True and a["fit_input"]["content_hash"] == "h1"
    assert a["current_data"]["content_hash"] == "h2" and a["fit_data_mismatch"] is True
    assert all(r["content_hash"] == "h1" and r["fit_data_mismatch"] == "True"
               for r in _rows(rows, 1))
    head = _spectrum(folder, a["spectrum_file"])[0]
    assert "fitted_against: h1" in head and "fit_data_mismatch: true" in head
    b = rec["E:2:ana"]
    assert b["fit_input"]["recorded"] is False and b["fit_input"]["note"]
    assert b["fit_data_mismatch"] is None
    for r in doc["spectra"]:
        assert r["fit_input"]["content_hash"] != "h2"
    assert all(r["content_hash"] != "h2" for r in rows)


def test_bl43_workspace_two_same_temperature_sets_stay_distinct(bl43_ws):
    folder = export_mod.export_workspace(bl43_ws, now=NOW).folder
    doc = json.loads((folder / "fits.json").read_text(encoding="utf-8"))
    _card, session = Workspace.open(bl43_ws).load()
    n_ledger = sum(max(1, len(e.detectors)) for e in session.scans.values())
    assert len(doc["spectra"]) == n_ledger
    ids = [r["spectrum_id"] for r in doc["spectra"]]
    assert len(set(ids)) == len(ids)
    files = [r["spectrum_file"] for r in doc["spectra"] if r["spectrum_file"]]
    assert len(set(files)) == len(files)
    a03 = [r for r in doc["spectra"] if r["detector"] == "a03" and r["scan_label"]]
    assert {r["scan_label"] for r in a03} >= {"CsV3Sb5_97K_0p5_0p5_3_sum",
                                              "CsV3Sb5_97K_0p5_0p5_3_rep_sum"}
    assert len({r["spectrum_file"] for r in a03}) == len(a03)
