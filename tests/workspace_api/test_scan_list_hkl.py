"""The scan list's hkl column and its gpi / rf marks, the folder status, and the shared hkl bin (S1U1).

The BL43 cases run on a generated, imported workspace (``build_bl43_gpi_workspace``): the real gpi check, the
real curve check and the real readiness, nothing stubbed (hard rule 10: every number is generated here).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import bl43_fixture
import intake_fixtures as fx
from bl43_fixture import GPI_SET, build_bl43_gpi_workspace, pooled_workspace
from isar.intake import settings, values
from isar.intake.gpi import read_gpi_check
from isar.intake.readiness import ImportSummary
from isar.workspace_api import FactRow, Workspace, import_summary, needs_attention
from isar.workspace_api.intake_view import folder_status
from isar.workspace_api.scan_list import (ATTENTION_NOT_READ, Fact, Mark, _pooled_hkl, _summary_mark,
                                          effective_hkl, hkl_bin, hkl_step, scan_list)

ANALYZERS = ("a03", "a04", "a05")
HKL = {a: (0.5, 0.5, 3.0 + 0.5 * i) for i, a in enumerate(ANALYZERS)}


def _spectra(snap):
    return {sp.analyzer: sp for sp in snap.sets[0].spectra}


def _snap(root, **kw):
    return scan_list(Workspace.open(root), **kw)


# ---------------------------------------------------------------------------
# effective_hkl: the four worked examples
# ---------------------------------------------------------------------------
def _qrow(scan, det, *, value, used, file_source="missing"):
    return FactRow(scan=scan, analyzer=det, fact="q", unit=None, enterable=False, file_value=None,
                   file_source=file_source, file_conflicts=(), user_value=None, user_entry=None, user_reason=None,
                   used_value=value, used_source=used, disagreement=False, fallback=None)


def _sv(scan_id, hkl, q_source=None):
    prov = {} if q_source is None else {"q_source": q_source}
    return SimpleNamespace(scan_id=scan_id, hkl=hkl, provenance=prov)


def test_effective_hkl_worked_examples():
    scans = [_sv("e:7:a06", None), _sv("e:8:a03", None), _sv("tas:1:det", [1.0, 0.0, 0.0], "reported"),
             _sv("tas:2:det", [0.0, 0.0, 0.0], "none"), _sv("tas:3:det", [1.0, 1.0, 0.0])]
    rows = [_qrow("e:7", "a06", value=[0.5, 0.5, 3.0], used="gpi"),
            _qrow("e:8", "a03", value=None, used="missing")]            # the gpi file rejected
    got = effective_hkl(scans, rows)
    assert got["e:7:a06"] == Fact((0.5, 0.5, 3.0), "rlu", "gpi", "file")
    assert got["e:8:a03"].value is None and got["e:8:a03"].reason and got["e:8:a03"].unit == "rlu"
    assert got["tas:1:det"] == Fact((1.0, 0.0, 0.0), "rlu", "reported", "file")
    assert got["tas:2:det"].value is None                                # q_source "none" is no hkl
    assert got["tas:3:det"] == Fact((1.0, 1.0, 0.0), "rlu", "file", "file")   # no stored source: "file"
    assert effective_hkl(scans, rows) is not got and effective_hkl(scans, rows) == got   # fresh, equal


def test_effective_hkl_never_raises_on_a_malformed_entry():
    got = effective_hkl([_sv("x:1:det", ["a", "b", "c"])], [])
    assert got["x:1:det"].value is None and got["x:1:det"].reason


def test_hkl_bin_and_step():
    assert hkl_bin((0.5, 0.5, 3.0), 0.001) == (500, 500, 3000)
    assert hkl_bin((0.5004, 0.5, 3.0), 0.001) == (500, 500, 3000)
    assert hkl_bin((0.5006, 0.5, 3.0), 0.001) != (500, 500, 3000)
    assert hkl_bin(None, 0.001) is None and hkl_bin((), 0.001) is None
    assert hkl_bin((1.0, None, 0.0), 0.001) is None and hkl_bin((1.0, "x", 0.0), 0.001) is None
    assert hkl_bin((1.0, 0.0, 0.0), 0) is None
    card = lambda v: SimpleNamespace(card=SimpleNamespace(policy=SimpleNamespace(hkl_round_step=v)))  # noqa: E731
    assert hkl_step(card(0.01)) == 0.01
    assert hkl_step(card(None)) == hkl_step(card(-1)) == hkl_step(card("x")) == hkl_step(SimpleNamespace()) == 0.001


# ---------------------------------------------------------------------------
# the real read on a generated BL43 set
# ---------------------------------------------------------------------------
def test_accepted_gpi_file_shows_each_analyzers_hkl_and_checks(tmp_path):
    snap = _snap(build_bl43_gpi_workspace(tmp_path))
    assert snap.has_gpi and snap.has_rf
    for a, sp in _spectra(snap).items():
        assert sp.q == Fact(HKL[a], "rlu", "gpi", "file")
        assert sp.q_abs.unit == "nm⁻¹" and isinstance(sp.q_abs.value, float)    # |Q| is still there
        assert sp.parsed.word == "ok"
        assert sp.gpi == Mark(True, "gpi: accepted", f"gpi.{GPI_SET}")
        assert sp.rf.good is True and sp.rf.text == "rf: curve in force" and sp.rf.detail.startswith("sarf_z_")
    row = snap.sets[0]
    assert row.gpi.good is True and row.gpi.text == "gpi: all analyzers good"
    assert row.rf.good is True and row.rf.text == "rf: all analyzers good"


def test_one_curve_missing_is_a_cross_on_that_analyzer_and_the_scan(tmp_path):
    snap = _snap(build_bl43_gpi_workspace(tmp_path, drop_curve="a04"))
    sp = _spectra(snap)
    assert (sp["a03"].rf.good, sp["a04"].rf.good, sp["a05"].rf.good) == (True, False, True)
    assert sp["a04"].rf.text == "rf: curve missing" and "sarf_z_s1_40_40_a04" in sp["a04"].rf.detail
    assert snap.sets[0].rf == Mark(False, "rf: 1 of 3 analyzers not good", sp["a04"].rf.detail)
    assert snap.sets[0].gpi.good is True                                  # gpi is untouched by a curve


def test_a_rejected_gpi_file_blanks_hkl_and_crosses_every_analyzer(tmp_path):
    root = build_bl43_gpi_workspace(tmp_path, gpi="rejected")
    snap = _snap(root)
    for sp in _spectra(snap).values():
        assert sp.q.value is None and sp.q.reason                         # BL43 files carry no hkl of their own
        assert sp.q_abs.value is not None                                 # |Q| is still available
        assert sp.gpi.good is False and sp.gpi.text == "gpi: not used" and "a04" in sp.gpi.detail
        assert sp.parsed.word == "ok"                                     # "no Q" keeps its meaning: |Q| is there
        assert sp.rf.good is True                                         # slit matching: the ordinary library
    assert snap.sets[0].gpi.good is False and snap.sets[0].gpi.text == "gpi: 3 of 3 analyzers not good"


def test_without_a_gpi_folder_the_cross_says_no_folder(tmp_path):
    snap = _snap(build_bl43_gpi_workspace(tmp_path, gpi="none"))
    for sp in _spectra(snap).values():
        assert sp.q.value is None
        assert (sp.gpi.good, sp.gpi.text) == (False, "gpi: no folder")


def test_a_gpi_folder_with_no_matching_file_says_no_file(tmp_path):
    root = build_bl43_gpi_workspace(tmp_path, gpi="none")
    (tmp_path / "data" / "gpi_files").mkdir()
    (tmp_path / "data" / "gpi_files" / "gpi.Other_Set").write_text("x\n", encoding="utf-8", newline="\n")
    assert settings.save_gpi_folder(root, tmp_path / "data" / "gpi_files").ok
    from isar.intake.gpi import check_gpi
    card, _ = fx.Workspace.open(root).load()
    check_gpi(root, card, [tmp_path / "data"], by="run")
    sp = _spectra(_snap(root))["a03"]
    assert (sp.gpi.good, sp.gpi.text) == (False, "gpi: no file")
    assert sp.gpi.detail == "no gpi file matches this scan set"


def test_a_folder_chosen_since_the_check_says_folder_changed(tmp_path):
    root = build_bl43_gpi_workspace(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert settings.save_gpi_folder(root, other).ok                        # saved, not yet checked
    sp = _spectra(_snap(root))["a03"]
    assert (sp.gpi.good, sp.gpi.text) == (False, "gpi: folder changed") and sp.q.value is None


def test_no_slit_yet_is_a_cross_until_a_slit_is_entered(tmp_path):
    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))                   # no slit in the files, no gpi file
    from isar.session import ingest
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    sp = _spectra(_snap(ws.root))["a03"]
    assert (sp.rf.good, sp.rf.text) == (False, "rf: no slit yet")
    key = next(iter(Workspace.open(ws.root).fact_rows())).scan
    assert values.save_value(ws.root, "slit", "40x40", [key]).ok
    sp = _spectra(_snap(ws.root))["a03"]
    assert (sp.rf.good, sp.rf.text) == (True, "rf: curve in force")        # before any calibration has run


def test_rf_shows_no_icon_while_the_readiness_is_not_read(tmp_path):
    snap = _snap(build_bl43_gpi_workspace(tmp_path), needs_attention=ATTENTION_NOT_READ)
    sp = _spectra(snap)["a03"]
    assert (sp.rf.good, sp.rf.text) == (None, "rf: not read yet")
    assert (snap.sets[0].rf.good, snap.sets[0].rf.text) == (None, "rf: not read yet")
    assert sp.gpi.good is True                                             # gpi needs no readiness read


def test_a_tas_workspace_has_neither_column(tmp_path):
    snap = scan_list(Workspace.open(pooled_workspace(tmp_path)))
    assert not snap.has_gpi and not snap.has_rf
    assert all(sp.gpi is None and sp.rf is None for s in snap.sets for sp in s.spectra)
    assert snap.pooled[0].gpi is None and snap.pooled[0].rf is None


def test_the_pool_of_agreeing_members_shows_their_mean(tmp_path):
    snap = scan_list(Workspace.open(pooled_workspace(tmp_path)))
    assert snap.pooled[0].q == Fact((2.0, 0.0, 0.0), "rlu", "members", "file")


# ---------------------------------------------------------------------------
# the pooled rule and the summaries, on their own
# ---------------------------------------------------------------------------
def test_pooled_hkl_rule():
    hkl = {"u1": Fact((0.5, 0.5, 3.0), "rlu", "gpi", "file"), "u2": Fact((0.5004, 0.5, 3.0), "rlu", "gpi", "file"),
           "u3": Fact((0.5, 0.5, 3.5), "rlu", "gpi", "file"), "u4": Fact(None, "rlu", "missing", "missing", "x")}
    got = _pooled_hkl(["u1", "u2"], hkl, 0.001)                            # one bin: the component-wise mean
    assert got.value == pytest.approx((0.5002, 0.5, 3.0)) and got.source == "members"
    differ = _pooled_hkl(["u1", "u3"], hkl, 0.001)                         # never one member standing for the pool
    assert differ.value is None and differ.reason == "the members' hkl differ"
    assert _pooled_hkl(["u1", "u4"], hkl, 0.001).reason == "a member has no hkl"
    assert _pooled_hkl(None, hkl, 0.001).reason == "the pool's members are not known"
    assert _pooled_hkl(["u1", "u3"], hkl, 2.0).value is not None           # a coarser step bins them together


def test_summary_mark_rules():
    good, bad, unread = Mark(True, "t", "file"), Mark(False, "t", "why"), Mark(None, "t", "")
    assert _summary_mark("rf", [good, good]) == Mark(True, "rf: all analyzers good", "every analyzer's rf is good")
    assert _summary_mark("rf", [good, bad, bad]) == Mark(False, "rf: 2 of 3 analyzers not good", "why")
    assert _summary_mark("rf", [good, unread]).good is None                # not read yet beats a check
    assert _summary_mark("rf", [bad, unread]).text == "rf: not read yet"
    assert _summary_mark("gpi", [], unknown=True) == Mark(
        False, "gpi: members not known", "the pooled master's members cannot be matched to scans")


# ---------------------------------------------------------------------------
# folder_status
# ---------------------------------------------------------------------------
def _status(root):
    return folder_status(import_summary(root), settings.current_settings(root), needs_attention(root))


def test_folder_status_good_rf_and_gpi(tmp_path):
    root = build_bl43_gpi_workspace(tmp_path)
    rf, gpi = _status(root)
    assert rf.good and rf.reason is None and rf.how == "chosen by you" and Path(rf.path) == root / "rf"
    assert rf.accessible == "Resolution curves folder: good"
    assert gpi.good and gpi.how == "found beside the data" and Path(gpi.path).name == "gpi_files"
    assert gpi.accessible == "gpi files folder: good"


def test_folder_status_missing_curves_rejected_gpi_and_no_folder(tmp_path):
    rf, gpi = _status(build_bl43_gpi_workspace(tmp_path / "a", drop_curve="a04"))
    assert not rf.good and rf.reason == "1 analyzer has no resolution curve" and gpi.good
    rf, gpi = _status(build_bl43_gpi_workspace(tmp_path / "b", gpi="rejected"))
    assert rf.good and not gpi.good and gpi.reason.startswith("1 gpi file not used: ") and "a04" in gpi.reason
    assert gpi.accessible == "gpi files folder: not good"
    rf, gpi = _status(build_bl43_gpi_workspace(tmp_path / "c", gpi="none"))
    assert not gpi.good and gpi.path is None and gpi.how is None and gpi.reason == "no gpi folder is chosen"


def test_folder_status_a_held_analyzer_with_no_curve_is_not_a_good_rf_folder(tmp_path):
    # its counts are held before calibrate reaches the curve, so no curve_missing group exists; the rf column crosses
    root = build_bl43_gpi_workspace(tmp_path, drop_curve="a04", off_integer=("a04",))
    assert _spectra(_snap(root))["a04"].rf.good is False
    rf, _gpi = _status(root)
    assert not rf.good and rf.reason == "1 analyzer has no resolution curve"


def test_folder_status_a_new_folder_is_not_good_until_checked(tmp_path):
    root = build_bl43_gpi_workspace(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert settings.save_resolution_folder(root, other).ok and settings.save_gpi_folder(root, other).ok
    rf, gpi = _status(root)
    assert not rf.good and rf.reason.startswith("the resolution folder changed")
    assert not gpi.good and gpi.reason.startswith("the gpi folder changed")
    assert Path(rf.path) == other and Path(gpi.path) == other


def test_folder_status_a_check_that_lists_no_scan(tmp_path):
    root = build_bl43_gpi_workspace(tmp_path, gpi="none")
    (tmp_path / "data" / "gpi_files").mkdir()
    (tmp_path / "data" / "gpi_files" / "gpi.Other_Set").write_text("x\n", encoding="utf-8", newline="\n")
    assert settings.save_gpi_folder(root, tmp_path / "data" / "gpi_files").ok
    from isar.intake.gpi import check_gpi
    card, _ = fx.Workspace.open(root).load()
    check_gpi(root, card, [tmp_path / "data"], by="run")
    assert read_gpi_check(root).scans == {}
    assert _status(root)[1].reason == "no gpi file matches the imported scans"


def test_folder_status_not_bl43_has_neither(tmp_path):
    summary = ImportSummary(0, 0, (), (), {}, None, None)
    assert folder_status(summary, None, None) == (None, None) == folder_status(None, None, None)
