"""outside_references: what an experiment folder still needs from outside itself (Qt-free, read-only).

Workspaces are stamped with the shared synthetic builder; the manifest rows and the recorded
resolution folders are the only inputs, so every example of the plan is a small stamped workspace.
"""
from __future__ import annotations

from isar.workspace_api import Workspace
from isar.workspace_api.references import OutsideReferences, outside_references


def _refs(make_ws, files=None, state=None):
    root = make_ws(files={str(p): {"format": "x"} for p in (files or [])}, state=state or {})
    return root, outside_references(Workspace.open(root))


def test_nothing_recorded_yet(make_ws):
    _root, refs = _refs(make_ws)
    assert refs == OutsideReferences((), (), (), False, False)


def test_data_outside_the_root_and_present(make_ws, tmp_path):
    data = tmp_path / "beam" / "data"
    data.mkdir(parents=True)
    # The files themselves are never created: only their folder is stat-ed, so an existing folder
    # is "present" however its files fare (a network share must not be walked file by file).
    root, refs = _refs(make_ws, files=[data / "a03", data / "a04", data / "a05"])
    assert refs.source_folders == (str(data),)
    assert refs.missing_source_folders == ()
    assert refs.recorded_resolution_folders == ()
    assert refs.resolution_inside is False and refs.sources_inside is False


def test_a_source_file_inside_the_root_is_flagged(make_ws, tmp_path):
    root = make_ws()
    (root / "raw").mkdir()
    ws = _reopen_with_files(root, [root / "raw" / "x.dat", tmp_path / "elsewhere" / "y.dat"])
    refs = outside_references(ws)
    assert refs.sources_inside is True


def _reopen_with_files(root, files):
    from synthetic_ws import write_session

    write_session(root, files={str(p): {"format": "x"} for p in files})
    return Workspace.open(root)


def test_a_sibling_folder_sharing_the_root_prefix_is_not_inside(make_ws):
    root = make_ws()
    sibling = root.parent / (root.name + "2") / "data"
    ws = _reopen_with_files(root, [sibling / "x.dat"])
    assert outside_references(ws).sources_inside is False


def test_missing_source_folders_count_their_files(make_ws, tmp_path):
    present = tmp_path / "here"
    present.mkdir()
    gone = tmp_path / "D" / "beam" / "data"                  # never created: the opened-elsewhere case
    _root, refs = _refs(make_ws, files=[gone / f"f{i}" for i in range(58)] + [present / "p"])
    assert refs.missing_source_folders == ((str(gone), 58),)
    assert refs.source_folders == tuple(sorted((str(gone), str(present))))


def test_resolution_folder_inside_the_root_is_flagged(make_ws):
    root = make_ws()
    rf = root / "rf"
    state = {"resolution_versions": [{"version": 1, "method": "tabulated_elastic", "folder": str(rf)}]}
    from synthetic_ws import write_state

    write_state(root, **state)
    refs = outside_references(Workspace.open(root))
    assert refs.recorded_resolution_folders == (str(rf),)
    assert refs.resolution_inside is True and refs.sources_inside is False


def test_resolution_folder_outside_or_unrecorded_is_not_inside(make_ws, tmp_path):
    elsewhere = tmp_path / "curves"
    state = {
        "resolution_versions": [{"version": 1, "folder": str(elsewhere)}, {"version": 2, "folder": None}],
        # the per-scan record also names a folder; one without a folder (an error entry) is skipped
        "tabulated_calibration": {"e:1": {"folder": str(elsewhere / "b")}, "e:2": {"error": "boom"}},
    }
    _root, refs = _refs(make_ws, state=state)
    assert refs.recorded_resolution_folders == (str(elsewhere), str(elsewhere / "b"))
    assert refs.resolution_inside is False


def test_the_per_scan_calibration_record_alone_counts(make_ws):
    root = make_ws()
    from synthetic_ws import write_state

    write_state(root, tabulated_calibration={"e:1": {"folder": str(root / "rf")}})
    assert outside_references(Workspace.open(root)).resolution_inside is True
