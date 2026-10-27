"""Experiment settings (C-settings and C-save-result): the worked examples and the checks they guard."""
import pytest

from isar.intake import settings, store
from isar.intake.settings import ExperimentSettings, current_settings, save_experiment_metadata, save_resolution_folder

BUSY = "the intake store is busy (another ISAR process is saving); try again"


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    return w


def _folder(tmp_path, name):
    d = tmp_path / name / "rf"
    d.mkdir(parents=True)
    return d


def test_empty_workspace_has_nothing_saved(ws):
    assert current_settings(ws) == ExperimentSettings(None, {})


def test_discovered_folder_is_saved_and_never_replaced(ws, tmp_path):
    set1, set2 = _folder(tmp_path, "set1"), _folder(tmp_path, "set2")
    r = save_resolution_folder(ws, set1, source="discovered", found_beside=str(set1.parent))
    assert r.ok and r.reason is None and r.targets == ("experiment",)
    assert r.value == str(set1.resolve()) and r.entry_id
    assert current_settings(ws).resolution_folder.source == "discovered"

    r2 = save_resolution_folder(ws, set2, source="discovered", found_beside=str(set2.parent))
    assert not r2.ok and r2.value is None and r2.entry_id is None
    assert r2.reason == (f"a resolution folder is already saved ({set1.resolve()}, discovered); "
                         "discovery never replaces it")
    assert current_settings(ws).resolution_folder.path == str(set1.resolve())


def test_user_folder_always_appends_and_wins(ws, tmp_path):
    set1 = _folder(tmp_path, "set1")
    curves = tmp_path / "curves"
    curves.mkdir()
    save_resolution_folder(ws, set1, source="discovered", found_beside=str(set1.parent))
    assert save_resolution_folder(ws, curves).ok
    assert save_resolution_folder(ws, curves).ok  # the same folder again still records a save
    assert len(store.read(ws, store.SETTINGS)) == 3
    folder = current_settings(ws).resolution_folder
    assert (folder.source, folder.path) == ("user", str(curves.resolve()))


def test_refused_folders_say_why_and_write_nothing(ws, tmp_path):
    a_file = tmp_path / "not_a_folder.txt"
    a_file.write_text("x", encoding="utf-8")
    cases = {
        None: "no folder given",
        "rf": "the resolution folder must be an absolute path: rf",
        str(tmp_path / "nope"): f"resolution folder {tmp_path / 'nope'} does not exist",
        str(a_file): f"{a_file} is not a folder",
    }
    for given, reason in cases.items():
        r = save_resolution_folder(ws, given)
        assert (r.ok, r.reason, r.value) == (False, reason, None)
    assert store.read(ws, store.SETTINGS) == []


def test_metadata_replaces_the_whole_record_and_refuses_bad_fields(ws):
    r = save_experiment_metadata(ws, {"proposal": "2026B1234", "sample": "CsV3Sb5"})
    assert r.ok and r.value == {"proposal": "2026B1234", "sample": "CsV3Sb5"}
    bad = save_experiment_metadata(ws, {"sample": 5})
    assert (bad.ok, bad.reason) == (False, "metadata field 'sample' must be text (got int)")
    assert current_settings(ws).metadata == {"proposal": "2026B1234", "sample": "CsV3Sb5"}
    assert save_experiment_metadata(ws, {}).ok and current_settings(ws).metadata == {}


def test_busy_store_returns_not_ok_and_writes_nothing(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCK_TIMEOUT_S", 0.1)
    d = _folder(tmp_path, "set1")
    with store._locked(ws):  # another handle holds the intake lock
        r = save_resolution_folder(ws, d)
    assert (r.ok, r.reason) == (False, BUSY)
    assert store.read(ws, store.SETTINGS) == []


def test_unknown_settings_kind_is_visible(ws):
    store.append(ws, store.SETTINGS, {"kind": "mystery"})
    with pytest.raises(ValueError, match="mystery"):
        current_settings(ws)
