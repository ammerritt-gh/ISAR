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
