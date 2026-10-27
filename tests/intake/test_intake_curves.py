"""The BL43 resolution-curve folder (S1U2): location, the shared coverage helpers, and the persisted check.

Curve files are SARF-shaped and written at test time; nothing here reads `real_data/`. The calibration
state (setting basis, curve identity) is checked beside the other BL43 calibration tests.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

import isar
from isar.analyze.resolution import ResolutionLibrary, TabulatedResolution
from isar.calibrate import tabulated
from isar.calibrate.tabulated import SettingChoice
from isar.intake import curves
from isar.intake.settings import save_resolution_folder
from isar.parsers import sarf, sp8_bl43, spec_s30
from isar.session import ingest
from isar.session.workspace import Workspace

PATTERN = sp8_bl43.card_template().policy.calibration["resolution_pattern"]
GRID = np.round(np.arange(-8.0, 8.0001, 0.2), 4)
ALL_CURVES = (("a03", "20x20"), ("a03", "60x60"), ("a04", "60x60"))


def test_import_root_is_the_worktree():
    # A sibling checkout on sys.path would make every assertion below test the wrong code.
    assert Path(isar.__file__).resolve().parent.parent == Path(__file__).resolve().parents[2]


def _write_curves(folder: Path, pairs=ALL_CURVES) -> None:
    """One SARF-shaped curve per (analyzer, slit): a Gaussian table with its header comments."""
    folder.mkdir(parents=True, exist_ok=True)
    y = np.exp(-0.5 * (GRID / 0.9) ** 2)
    for det, setting in pairs:
        h, v = setting.split("x")
        rows = ["# SARF generated for the test", "#E_meV SARF fit"]
        rows += [f" {e:8.3f} {c:12.5E} {c:12.5E}" for e, c in zip(GRID, y)]
        (folder / f"sarf_t_s1_{h}_{v}_{det}").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


def _card(*, folder=True):
    card = sp8_bl43.card_template()
    card.policy.calibration = dict(card.policy.calibration, resolution_pattern=PATTERN)
    if folder:      # the shipped file names no folder; an in-memory card still can (C-location's card branch)
        card.policy.calibration["resolution_folder"] = "rf"
    return card


def _curve(det, setting):
    return TabulatedResolution(GRID, np.exp(-0.5 * (GRID / 0.9) ** 2), det, setting)


def _lib(*pairs):
    return ResolutionLibrary([_curve(d, s) for d, s in pairs])


# ---------------------------------------------------------------------------
# location: the folder in force
# ---------------------------------------------------------------------------
def test_location_is_the_saved_folder_else_the_card_folder_else_nothing(tmp_path):
    card = _card()
    root = tmp_path / "ws"
    root.mkdir()
    assert curves.resolution_location(card, root) == (root / "rf", PATTERN, "card")   # card: relative to the workspace
    saved = tmp_path / "set1" / "rf"
    saved.mkdir(parents=True)
    assert save_resolution_folder(root, saved).ok
    assert curves.resolution_location(card, root) == (saved.resolve(), PATTERN, "setting")
    # Without a workspace no setting is read, and with no card folder there is no folder at all.
    assert curves.resolution_location(_card(folder=False), None) == (None, PATTERN, "none")
    assert curves.resolution_location(card, None) == (Path("rf"), PATTERN, "card")


# ---------------------------------------------------------------------------
# the coverage helpers (C-curve-need): the plan's worked examples
# ---------------------------------------------------------------------------
def test_curve_need_names_the_missing_curve_in_the_stages_words():
    lib = _lib(("a03", "20x20"), ("a03", "60x60"), ("a04", "60x60"))
    assert tabulated.curve_need(lib, "a03", "60x60") is None
    assert tabulated.curve_need(lib, "a04", "20x20") == "no resolution curve for a04 at slit 20x20"
    assert tabulated.curve_need(lib, "a03", None) == "no slit setting is known for the scan"
    problem = "resolution library unavailable (resolution folder D:\\x not found)"
    assert tabulated.curve_need(ResolutionLibrary(), "a03", "60x60", problem) == problem


def test_candidate_gaps_are_the_settings_an_analyzer_lacks():
    lib = _lib(("a03", "20x20"), ("a03", "60x60"), ("a04", "60x60"))
    assert tabulated.candidate_gaps(lib, "a03") == []
    assert tabulated.candidate_gaps(lib, "a04") == ["20x20"]
    assert tabulated.candidate_gaps(lib, "a05") == ["20x20", "60x60"]
    # The persisted check answers the same questions (readiness reads it, never the library).
    index = curves.CurveIndex([curves.CurveFile(d, s, "f", "0" * 64) for d, s in ALL_CURVES])
    assert tabulated.candidate_gaps(index, "a04") == ["20x20"]
    assert tabulated.curve_need(index, "a04", "20x20") == "no resolution curve for a04 at slit 20x20"


def test_setting_basis_says_how_the_slit_was_chosen():
    assert tabulated.setting_basis(SettingChoice("40x40", "declared", {}, [], [])) == "declared"
    assert tabulated.setting_basis(SettingChoice("40x40", "inferred", {"40x40": 12.0, "60x60": 40.0},
                                                 ["a03"], [])) == "inferred"
    assert tabulated.setting_basis(SettingChoice("60x60", "inferred", {}, [], [])) == "broadest_curve_fallback"
    assert tabulated.setting_basis(SettingChoice(None, "inferred", {}, [], [])) == "none"


def test_calibration_reuse_needs_the_same_data_and_the_same_library():
    prev = {"content_hash": "h", "library": "L"}
    assert tabulated.calibration_reusable(None, content_hash="h", library="L") is False
    assert tabulated.calibration_reusable(prev, content_hash="h", library="L") is True
    assert tabulated.calibration_reusable(prev, content_hash="h", library="L2") is False
    assert tabulated.calibration_reusable(prev, content_hash="h2", library="L") is False
    # No folder (library None) and unchanged data is reused, as before.
    assert tabulated.calibration_reusable({"content_hash": "h", "library": None}, content_hash="h", library=None)


# ---------------------------------------------------------------------------
# the persisted check (C-curve-check)
# ---------------------------------------------------------------------------
def test_first_import_discovers_the_rf_beside_the_data_and_records_it(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    set1 = tmp_path / "set1"
    _write_curves(set1 / "rf")
    check = curves.check_curves(root, _card(), [set1], by="import")
    assert check.discovered == str((set1 / "rf").resolve())
    assert (check.source, check.setting_source, check.found_beside, check.problem) == (
        "setting", "discovered", str(set1.resolve()), None)
    assert [(c.detector, c.setting) for c in check.curves] == list(ALL_CURVES)
    assert check.library == sarf.fingerprint(set1 / "rf", PATTERN) and check.other_folders == ()
    assert check.lines() == [
        f"resolution curves: found {set1.resolve() / 'rf'} beside {set1.resolve()} and saved it as the experiment's folder",
        f"resolution curves: 3 curves for 2 analyzers in {set1.resolve() / 'rf'}"]
    assert curves.read_curve_check(root) == check
    # The saved folder is the experiment's now: the next location reads it, whatever the card says.
    assert curves.resolution_location(_card(), root)[0] == (set1 / "rf").resolve()


def test_a_later_folder_with_curves_is_recorded_and_the_saved_one_is_kept(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    card = _card()
    set1, set2 = tmp_path / "set1", tmp_path / "set2"
    _write_curves(set1 / "rf")
    first = curves.check_curves(root, card, [set1], by="import")
    _write_curves(set2 / "rf", pairs=(("a05", "60x60"),))
    again = curves.check_curves(root, card, [set2], by="import")
    assert again.folder == first.folder and again.discovered is None and again.source == "setting"
    (other,) = again.other_folders
    assert (other.path, other.beside) == (str((set2 / "rf").resolve()), str(set2.resolve()))
    assert again.lines()[-1] == (f"resolution curves: {other.path} beside {other.beside} also holds curves; "
                                 f"the experiment keeps {first.folder} (change it in the GUI)")
    later = curves.check_curves(root, card, [set2], by="run")
    assert later.other_folders[0].first_seen == other.first_seen          # an existing path keeps its first_seen


def test_no_rf_and_no_card_folder_is_a_stated_gap(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    set3 = tmp_path / "set3"
    set3.mkdir()
    check = curves.check_curves(root, _card(folder=False), [set3], by="import")
    assert (check.folder, check.source, check.library, check.curves) == (None, "none", None, ())
    problem = f"no resolution folder is saved, and no rf/ folder holding curves was found beside {set3.resolve()}"
    assert check.problem == problem and check.lines() == [f"resolution curves: {problem}"]


def test_the_card_folder_that_is_missing_is_reported_as_the_gap_to_locate(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    set4 = tmp_path / "set4"
    set4.mkdir()
    check = curves.check_curves(root, _card(), [set4], by="run")
    assert check.source == "card" and check.folder == str(root / "rf") and check.library is None
    assert check.problem == (f"no resolution folder is saved, and no rf/ folder holding curves was found "
                             f"beside {set4.resolve()}")


def test_a_curve_replaced_at_the_same_path_changes_its_identity(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    card = _card()
    set1 = tmp_path / "set1"
    _write_curves(set1 / "rf")
    first = curves.check_curves(root, card, [set1], by="import")
    path = set1 / "rf" / "sarf_t_s1_60_60_a03"
    st = path.stat()
    path.write_bytes(path.read_bytes().replace(b"1.00000E+00", b"2.00000E+00", 1))   # same size, same mtime
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    second = curves.check_curves(root, card, [set1], by="run")
    assert second.library != first.library
    before = {(c.detector, c.setting): c.sha256 for c in first.curves}
    after = {(c.detector, c.setting): c.sha256 for c in second.curves}
    assert after[("a03", "60x60")] != before[("a03", "60x60")]
    assert after[("a03", "20x20")] == before[("a03", "20x20")] and after[("a04", "60x60")] == before[("a04", "60x60")]


def test_a_check_is_refused_for_other_methods_and_writes_nothing(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    s30 = spec_s30.card_template()
    assert curves.check_curves(root, s30, [tmp_path], by="import") is None
    assert not (root / curves.CURVE_CHECK).exists() and not (root / "intake").exists()


def test_a_malformed_check_file_is_refused_by_name(tmp_path):
    (tmp_path / "report").mkdir()
    (tmp_path / curves.CURVE_CHECK).write_text('{"schema": "isar.curve-check/1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a curve check"):
        curves.read_curve_check(tmp_path)
    assert curves.read_curve_check(tmp_path / "nowhere") is None


# ---------------------------------------------------------------------------
# the import and run wiring
# ---------------------------------------------------------------------------
def _bl43_workspace(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text((Path(sp8_bl43.__file__).parent / "cards" / "bl43lxu.policy.toml")
                              .read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return ws


def test_ingest_with_a_workspace_records_the_check_and_without_one_does_not(tmp_path):
    ws = _bl43_workspace(tmp_path)
    data = tmp_path / "data"
    _write_curves(data / "rf")                       # no scans: the check is what is under test
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, [data], data_dir=ws.root / "data", workspace_root=ws.root)
    assert report.curves["discovered"] == str((data / "rf").resolve())
    assert report.to_jsonable()["curves"]["schema"] == curves.SCHEMA
    assert report.files == []                        # the curves are never listed as unrecognized data
    assert (ws.root / curves.CURVE_CHECK).is_file()
    assert ingest.ingest_batch(card, session, [data]).curves is None


def test_run_checks_the_curves_and_records_them_before_any_calibration(tmp_path):
    from isar.session import driver
    ws = _bl43_workspace(tmp_path)
    data = tmp_path / "data"
    _write_curves(data / "rf")
    summary = driver.run_batch(ws, [data], M=10)
    assert (ws.root / curves.CURVE_CHECK).is_file()
    assert any(line.startswith("resolution curves: found ") for line in summary.context_lines)
