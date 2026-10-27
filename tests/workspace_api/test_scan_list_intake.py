"""The scan-list read on the intake backend (S2U4): facts from the fact rows, files not read and completeness
from the import summary, and each pass judged current or not. Qt-free, on the S2 fixture (hard rule 10: all
data generated here); every save goes through the real intake writers.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import bl43_fixture
import bl43_intake_fixture
from bl43_fixture import KEY_A, KEY_B, KEY_SINGLE
from isar.intake import values
from isar.workspace_api import Change, PassInputs, Workspace, import_summary
from isar.workspace_api.scan_list import Fact, ScanListSnapshot, Status, scan_list

REPO = Path(__file__).resolve().parents[2]
A03, A04, A05 = (f"{KEY_A}:a{n:02d}" for n in (3, 4, 5))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("sli"))


@pytest.fixture
def ws_root(built, tmp_path):
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "sli"
    shutil.copytree(built.parent, copy)
    return copy / built.name


def _snap(root) -> ScanListSnapshot:
    return scan_list(Workspace.open(root))


def test_the_worktree_is_the_import_root():
    import isar
    print("isar imported from", isar.__file__)
    assert Path(isar.__file__).resolve().is_relative_to(REPO)


def test_facts_come_from_the_fact_rows_and_a_user_value_wins(ws_root):
    snap = _snap(ws_root)
    assert snap.scan_set(KEY_A).temperature == Fact(97.0, "K", "header", "file")
    assert values.save_value(ws_root, "temperature", 100, [KEY_A]).ok
    after = _snap(ws_root)
    used = {r.scan: r.used_value for r in Workspace.open(ws_root).fact_rows([KEY_A]) if r.fact == "temperature"}
    assert used == {KEY_A: 100.0}
    assert after.scan_set(KEY_A).temperature == Fact(100.0, "K", "user", "user")
    assert after.scan_set(KEY_B).temperature == Fact(97.0, "K", "header", "file")     # another set: untouched


def test_a_slit_the_user_enters_is_the_sets_slit(ws_root):
    assert _snap(ws_root).scan_set(KEY_A).slit.value is None
    assert values.save_value(ws_root, "slit", "60x60", [KEY_A]).ok
    assert _snap(ws_root).scan_set(KEY_A).slit == Fact("60x60", None, "user", "user")


def test_the_reads_passed_in_give_the_same_snapshot_as_the_reads_made_here(ws_root):
    assert values.save_value(ws_root, "temperature", 100, [KEY_A]).ok
    ws = Workspace.open(ws_root)
    given = scan_list(ws, fact_rows=ws.fact_rows(), fit_inputs=ws.fit_inputs(),
                      import_summary=import_summary(ws_root))
    assert given == scan_list(ws)
    # not reading: empty reads give the ledger's facts, no marks and no summary lists
    bare = scan_list(ws, fact_rows=(), fit_inputs=(), import_summary=import_summary(ws_root))
    assert bare.scan_set(KEY_A).temperature == Fact(97.0, "K", "header", "file")
    assert all(sp.fast.current is None for s in bare.sets for sp in s.spectra)


def test_files_not_read_are_the_import_summarys(ws_root):
    snap = _snap(ws_root)
    summary = import_summary(ws_root)
    assert {(u.path, u.reason, u.set_key) for u in snap.files_not_read} == \
        {(f.path, f.reason, f.scan) for f in summary.files_not_read}
    names = [u.name for u in snap.files_not_read]
    assert len(names) == 3 and "notes.txt" in names and f"{bl43_fixture.STEM_B}_a07" in names


def test_completeness_is_the_summarys_statement_never_a_count_of_rows(ws_root):
    summary = import_summary(ws_root)
    assert [i.scan for i in summary.incomplete_sets] == [KEY_B]
    snap = _snap(ws_root)
    assert snap.scan_set(KEY_A).expected_analyzers == len(snap.scan_set(KEY_A).spectra) == 4
    b = snap.scan_set(KEY_B)
    assert b.expected_analyzers == len(b.spectra) + len(summary.incomplete_sets[0].missing_analyzers) == 5
    assert snap.scan_set(KEY_SINGLE).expected_analyzers == 1


def test_a_workspace_without_an_import_record_keeps_the_ledger_reads(bl43_ws_readonly):
    snap = scan_list(Workspace.open(bl43_ws_readonly))
    assert snap == bl43_fixture.sample_snapshot(bl43_ws_readonly)
    assert all(s.expected_analyzers is None for s in snap.sets)
    assert len(snap.files_not_read) == 3


def test_another_instruments_workspace_keeps_its_temperatures(make_ws):
    from synthetic_ws import scan_entry
    facts = {"scan_facts": {"T_K": {"value": 55.0, "source": "header"}}}
    ws = Workspace.open(make_ws(scans={"tavi:11:det": scan_entry(11, provenance=facts)}))
    assert ws.fact_rows() == []                      # no import record: the backend holds none of its sets
    assert scan_list(ws).scan_set("tavi:11").temperature == Fact(55.0, "K", "header", "file")


def test_a_pass_whose_inputs_changed_is_not_current_and_says_what(ws_root):
    assert all(_snap(ws_root).spectrum(u).fast.current is True for u in (A03, A04, f"{KEY_A}:a06"))
    assert values.save_value(ws_root, "temperature", 100, [KEY_A]).ok
    snap = _snap(ws_root)
    for unit in (A03, A04):
        fast = snap.spectrum(unit).fast
        assert fast.current is False
        assert fast.changed == (("temperature", "97 K (file)", "100 K (yours)"),)
    assert snap.spectrum(A04).fast.word == "close_call"                      # the stored word is unchanged
    assert snap.spectrum(A04).deep.current is None                           # no deep pass was judged


def test_a_unit_without_a_fit_inputs_record_is_not_judged(ws_root):
    assert values.save_value(ws_root, "temperature", 100, [KEY_A]).ok
    snap = _snap(ws_root)
    assert snap.spectrum(A05).fast == Status("not run", False)               # never fitted: no pass, no mark
    assert snap.spectrum(f"{KEY_B}:a03").fast.current is None                # held: no pass either


def test_saving_the_files_own_value_over_it_marks_nothing(ws_root):
    assert values.save_value(ws_root, "temperature", 97, [KEY_A]).ok
    assert all(_snap(ws_root).spectrum(u).fast.current is True for u in (A03, A04))


def test_a_running_pass_is_not_judged(ws_root, monkeypatch):
    from isar.intake import fit_inputs
    monkeypatch.setattr(fit_inputs, "write_finished", lambda *a, **k: None)   # started, not finished
    bl43_intake_fixture.write_run(ws_root, (A03,), end=False)                  # by this live process
    assert values.save_value(ws_root, "temperature", 100, [KEY_A]).ok
    pass_ = {u.unit: u for u in Workspace.open(ws_root).fit_inputs()}[A03].fast
    assert pass_.state == "running"                    # the read holds the refit's inputs, not the stored fit's
    fast = _snap(ws_root).spectrum(A03).fast
    assert (fast.word, fast.current, fast.changed) == ("resolved", None, ())
    assert _snap(ws_root).spectrum(A04).fast.current is False                # a finished pass is still judged


def _pass(*changes, state="finished", inputs=None) -> PassInputs:
    return PassInputs("fast", state, "r1", "t0", None, None, inputs or {}, (), None, tuple(changes))


def test_change_labels_and_values():
    from isar.workspace_api.scan_list import _judged, _change_line
    p = _pass(Change("calibration_zero", 0.15, 0.12, None))
    assert _change_line(p, p.changes[0]) == ("energy zero", "0.15 meV", "0.12 meV")
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    p = _pass(Change("curve", sha, "missing", None))
    assert _change_line(p, p.changes[0]) == ("resolution curve", "a1b2c3d4", "missing")
    p = _pass(Change("dead", False, True, None), Change("dead_in_scan", ["a04"], [], None),
              Change("novel_field", None, 3, None),
              Change("slit", None, "60x60", "user"), inputs={"slit": {"source": "missing", "value": None}})
    assert [_change_line(p, c) for c in p.changes] == [
        ("declared dead", "no", "yes"), ("dead analyzers in the scan", "a04", "none"),
        ("novel_field", "none", "3"), ("slit", "none (missing)", "60x60 mm (yours)")]
    stored = Status("resolved", True)
    assert _judged(stored, _pass()).current is True
    assert _judged(stored, _pass(Change("q", None, None, None))).current is False
    assert _judged(Status("not run", False), _pass(Change("q", 1, 2, None))).current is None   # no stored pass
    assert _judged(stored, None) is stored


def test_a_workspace_without_the_reads_never_raises():
    bare = SimpleNamespace(scans=[], files=[], card=None)
    assert scan_list(bare) == ScanListSnapshot()
