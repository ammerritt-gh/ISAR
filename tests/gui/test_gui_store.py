"""GuiStore tests — the Qt-free per-workspace GUI state (watched folders slice, D6).

Covers the durability contract: defaults on a missing file, defaults (no raise) on a corrupt file,
atomic save leaving no ``.tmp`` behind, and unknown top-level keys preserved on round-trip (so a
Phase-E ``groups`` section written by a newer build survives an older one).
"""
from __future__ import annotations

import json

from isar_gui.gui_store import GuiStore


def test_defaults_on_missing_file(tmp_path):
    store = GuiStore.load(tmp_path)
    assert store.watched_folders() == []
    assert store.watch_paused() is False


def test_corrupt_file_falls_back_to_defaults_without_raising(tmp_path):
    (tmp_path / "gui.state.json").write_text("{ this is not json ]", encoding="utf-8")
    store = GuiStore.load(tmp_path)   # must not raise
    assert store.watched_folders() == []
    assert store.watch_paused() is False


def test_non_object_json_falls_back_to_defaults(tmp_path):
    (tmp_path / "gui.state.json").write_text("[1, 2, 3]", encoding="utf-8")
    store = GuiStore.load(tmp_path)
    assert store.watched_folders() == []


def test_add_remove_and_pause_round_trip(tmp_path):
    store = GuiStore.load(tmp_path)
    assert store.add_watched(r"D:\beam\numors") is True
    assert store.add_watched(r"D:\beam\numors") is False   # dedup: no change
    store.set_watch_paused(True)
    store.save()

    reloaded = GuiStore.load(tmp_path)
    assert reloaded.watched_folders() == [str(__import__("pathlib").Path(r"D:\beam\numors"))]
    assert reloaded.watch_paused() is True
    assert reloaded.remove_watched(r"D:\beam\numors") is True
    assert reloaded.watched_folders() == []


def test_atomic_save_leaves_no_tmp(tmp_path):
    store = GuiStore.load(tmp_path)
    store.add_watched("x")
    store.save()
    assert (tmp_path / "gui.state.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_unknown_keys_preserved_on_round_trip(tmp_path):
    # A newer build wrote a "groups" section this (Phase-D) build does not understand.
    path = tmp_path / "gui.state.json"
    path.write_text(json.dumps({
        "version": 1, "watched_folders": ["/data/a"], "watch_paused": False,
        "groups": {"custom": [1, 2, 3]}}), encoding="utf-8")

    store = GuiStore.load(tmp_path)
    store.add_watched("/data/b")   # mutate the section this build owns
    store.save()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["groups"] == {"custom": [1, 2, 3]}          # untouched
    assert "/data/b" in raw["watched_folders"]


# --------------------------------------------------------------------------
# Phase E: manual groups slice
# --------------------------------------------------------------------------
def test_group_crud_and_membership_round_trip(tmp_path):
    store = GuiStore.load(tmp_path)
    gid = store.create_group("Cold sweep")
    assert isinstance(gid, str) and gid
    store.add_member(gid, "tavi:469")
    store.add_member(gid, "tavi:470")
    store.add_member(gid, "tavi:469")   # dedup, order-preserving
    store.rename_group(gid, "300 K")
    store.save()

    reloaded = GuiStore.load(tmp_path)
    groups = reloaded.groups()
    assert len(groups) == 1
    g = groups[0]
    assert g.id == gid
    assert g.name == "300 K"
    assert g.members == ["tavi:469", "tavi:470"]
    assert reloaded.memberships_of("tavi:469") == [gid]
    assert reloaded.memberships_of("tavi:999") == []

    reloaded.remove_member(gid, "tavi:469")
    assert reloaded.groups()[0].members == ["tavi:470"]


def test_delete_group_removes_memberships_order_and_collapsed(tmp_path):
    store = GuiStore.load(tmp_path)
    gid = store.create_group("A")
    other = store.create_group("B")
    store.add_member(gid, "tavi:1")
    store.set_collapsed(gid, True)
    assert gid in store.group_order()
    assert gid in store.collapsed()

    store.delete_group(gid)
    assert store.groups() == [] or all(g.id != gid for g in store.groups())
    assert store.memberships_of("tavi:1") == []
    assert gid not in store.group_order()
    assert gid not in store.collapsed()
    # The untouched group survives.
    assert any(g.id == other for g in store.groups())


def test_group_order_and_collapsed_round_trip(tmp_path):
    store = GuiStore.load(tmp_path)
    a = store.create_group("A")
    b = store.create_group("B")
    store.set_group_order([b, a])
    store.set_collapsed(a, True)
    store.set_collapsed("auto:T:300", True)   # auto-group ids are allowed as ui scraps
    store.save()

    reloaded = GuiStore.load(tmp_path)
    assert reloaded.group_order() == [b, a]
    assert reloaded.collapsed() == {a, "auto:T:300"}
    reloaded.set_collapsed(a, False)
    assert a not in reloaded.collapsed()


def test_groups_coexist_with_watched_folders(tmp_path):
    store = GuiStore.load(tmp_path)
    store.add_watched(r"D:\beam")
    gid = store.create_group("G")
    store.add_member(gid, "s:1")
    store.save()

    reloaded = GuiStore.load(tmp_path)
    assert reloaded.watched_folders() == [str(__import__("pathlib").Path(r"D:\beam"))]
    assert reloaded.groups()[0].members == ["s:1"]
