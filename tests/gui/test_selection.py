"""The shared window selection and its spectrum identity labels."""
from __future__ import annotations

import weakref
import hashlib

import pytest

from isar.workspace_api.scan_list import (Fact, PooledRow, ScanListSnapshot, ScanSetRow,
                                          SpectrumRow, Status)
from isar.workspace_api import CACHE_SIZE, SpectraCache
from isar_gui.selection import (ORIGIN_GROUP, ORIGIN_LIST, ORIGIN_REFRESH, ORIGIN_STEP,
                                Selection, SelectionChange, identity, unique_labels)

pytestmark = pytest.mark.usefixtures("qapp")


def test_set_emits_state_first_and_tracks_active_position():
    selection = Selection()
    selection_ref = weakref.ref(selection)
    deliveries = []

    def on_changed(change):
        current = selection_ref()
        deliveries.append(("changed", change, current.keys, current.active))

    def on_active_changed(key):
        current = selection_ref()
        deliveries.append(("active", key, current.keys, current.active))

    selection.changed.connect(on_changed)
    selection.active_changed.connect(on_active_changed)

    selection.set(["b", "a", "b"], clicked="a", origin=ORIGIN_LIST)

    assert selection.keys == ("b", "a")
    assert selection.active == "a"
    assert selection.position() == (2, 2)
    assert [entry[0] for entry in deliveries] == ["changed", "active"]
    assert deliveries[0][1] == SelectionChange(("b", "a"), "a", ORIGIN_LIST, True, True)
    assert all(entry[-2:] == (("b", "a"), "a") for entry in deliveries)


def test_order_resync_preserves_active_and_step_clamps():
    order = ["c", "b", "a"]
    selection = Selection(order=lambda keys: [key for key in order if key in keys])
    changes = []
    selection.changed.connect(changes.append)
    selection.set(["a", "b", "c"], clicked="b")
    assert selection.keys == ("c", "b", "a")
    assert selection.position() == (2, 3)

    order[:] = ["a", "c", "b"]
    selection.set_order(lambda keys: [key for key in order if key in keys])
    before = len(changes)
    selection.resync()
    assert selection.keys == ("a", "c", "b")
    assert selection.active == "b"
    assert selection.position() == (3, 3)
    assert changes[-1].origin == ORIGIN_REFRESH
    assert len(changes) == before + 1

    selection.step(1)
    assert selection.active == "b"
    assert len(changes) == before + 1
    selection.step(-99)
    assert selection.active == "a"
    assert changes[-1].origin == ORIGIN_STEP
    assert selection.set_active("missing") is False
    assert selection.set_active("c") is True
    assert selection.active == "c"


def test_group_noop_emits_and_other_noop_does_not():
    selection = Selection()
    changes = []
    selection.changed.connect(changes.append)
    selection.set(["a"], clicked="a")
    changes.clear()

    selection.set(["a"], clicked="a", origin=ORIGIN_GROUP)
    assert len(changes) == 1
    assert changes[0].keys_changed is False
    assert changes[0].active_changed is False
    selection.set(["a"], clicked="a", origin=ORIGIN_LIST)
    assert len(changes) == 1


def test_reentrant_mutations_wait_for_both_signals_to_finish():
    selection = Selection()
    selection_ref = weakref.ref(selection)
    seen = []

    def mutate_during_delivery(change):
        current = selection_ref()
        seen.append(("first", change.keys, current.keys, current.active))
        if change.keys == ("a",):
            current.set(["b"], clicked="b", origin=ORIGIN_LIST)

    def record_changed(change):
        current = selection_ref()
        seen.append(("second", change.keys, current.keys, current.active))

    def record_active(key):
        current = selection_ref()
        seen.append(("active", key, current.keys, current.active))

    selection.changed.connect(mutate_during_delivery)
    selection.changed.connect(record_changed)
    selection.active_changed.connect(record_active)

    selection.set(["a"], clicked="a", origin=ORIGIN_LIST)

    assert seen == [
        ("first", ("a",), ("a",), "a"),
        ("second", ("a",), ("a",), "a"),
        ("active", "a", ("a",), "a"),
        ("first", ("b",), ("b",), "b"),
        ("second", ("b",), ("b",), "b"),
        ("active", "b", ("b",), "b"),
    ]


def test_selection_examples_cover_clicked_group_refresh_step_and_clear_rules():
    list_order = ["A3", "A4", "A5", "B3", "B4"]
    available = set(list_order)
    selection_order = lambda keys: [key for key in list_order if key in available and key in set(keys)]

    def selected(keys=(), active=None):
        selection = Selection(order=selection_order)
        if keys:
            selection.set(keys, clicked=active)
        changes, actives = [], []
        selection.changed.connect(changes.append)
        selection.active_changed.connect(actives.append)
        return selection, changes, actives

    # S2: a clicked spectrum becomes active, with keys in list order.
    selection, changes, actives = selected(("A4",), "A4")
    selection.set(("A4", "B3"), clicked="B3", origin=ORIGIN_LIST)
    assert selection.keys == ("A4", "B3") and selection.active == "B3"
    assert changes[-1].keys_changed and changes[-1].active_changed and actives == ["B3"]

    # S3: recalling a group uses list order and the first spectrum as active.
    selection, changes, actives = selected(("A4", "B3"), "B3")
    selection.set(("B3", "A3", "A4"), origin=ORIGIN_GROUP)
    assert selection.keys == ("A3", "A4", "B3") and selection.active == "A3"

    # S4: removing the active spectrum by clicking it falls back to the first selected key.
    selection, changes, actives = selected(("A3", "A4", "B3"), "A3")
    assert selection.set_active("A4")
    changes.clear(); actives.clear()
    selection.set(("A3", "A4"), clicked="B3", origin=ORIGIN_LIST)
    assert selection.keys == ("A3", "A4") and selection.active == "A3"
    assert changes[-1].keys_changed and changes[-1].active_changed and actives == ["A3"]

    # S5/S6: stepping moves within the ordered selection and clamps at the end.
    selection, changes, actives = selected(("A3", "A4", "B3"), "A3")
    selection.step(+1)
    assert selection.active == "A4" and not changes[-1].keys_changed
    assert changes[-1].origin == ORIGIN_STEP and actives == ["A4"]
    selection.set_active("B3")
    changes.clear(); actives.clear()
    selection.step(+1)
    assert selection.active == "B3" and not changes and not actives

    # S7/S8: an outside active key is refused; setting the same selection is quiet.
    selection, changes, actives = selected(("A3", "A4", "B3"), "A4")
    assert selection.set_active("B4") is False
    selection.set(("A3", "A4", "B3"), clicked="A4", origin=ORIGIN_LIST)
    assert not changes and not actives

    # S9: a group recall of the current keys still emits once for the list to reveal them.
    selection, changes, actives = selected(("A3", "A4"), "A3")
    selection.set(("A3", "A4"), origin=ORIGIN_GROUP)
    assert changes == [SelectionChange(("A3", "A4"), "A3", ORIGIN_GROUP, False, False)]
    assert not actives

    # S10: resync drops rows absent from the snapshot, reorders, and replaces a removed active key.
    selection, changes, actives = selected(("A3", "A4", "B3"), "B3")
    list_order[:] = ["B3", "A3", "A4", "A5", "B4"]
    available.clear(); available.update(("A3", "A4"))
    selection.resync()
    assert selection.keys == ("A3", "A4") and selection.active == "A3"
    assert changes[-1].origin == ORIGIN_REFRESH and changes[-1].keys_changed
    assert changes[-1].active_changed and actives == ["A3"]

    # S11: a set-row click has no clicked spectrum, so the first list item stays active.
    list_order[:] = ["A3", "A4", "A5", "B3", "B4"]
    available.update(list_order)
    selection, changes, actives = selected(("A3",), "A3")
    selection.set(("A3", "A4"), origin=ORIGIN_LIST)
    assert selection.keys == ("A3", "A4") and selection.active == "A3"
    assert changes[-1].keys_changed and not changes[-1].active_changed and not actives

    # S13: clear emits the empty selection and an active_changed(None).
    selection.clear()
    assert selection.keys == () and selection.active is None and selection.position() is None
    assert changes[-1].keys == () and changes[-1].active is None
    assert actives == [None]


def _spectrum(key: str, set_key: str, analyzer: str, q=None) -> SpectrumRow:
    missing = Status("not run", False)
    return SpectrumRow(key, set_key, analyzer, Fact(None, "rlu", "missing", "missing"),
                       missing, missing, missing, None, None, None,
                       q_abs=Fact(q, "nm⁻¹" if q is not None else None, "header", "file"))


def _snapshot(*, scan_numbers=(12,), label="X", q=23.0234, temperature=97.0,
              analyzer="a06", key="exp:12:a06"):
    set_key = key.rsplit(":", 1)[0]
    spectrum = _spectrum(key, set_key, analyzer, q)
    scan_set = ScanSetRow(set_key, label, None, tuple(scan_numbers), False, None,
                          Fact(temperature, "K" if temperature is not None else None,
                               "header", "file"), Fact(None, None, "missing", "missing"),
                          (spectrum,))
    return ScanListSnapshot((scan_set,)), key


def test_identity_formats_numbered_summed_and_missing_facts():
    snapshot, key = _snapshot()
    found = identity(snapshot, key)
    assert found is not None
    assert (found.scan, found.numbered, found.analyzer, found.q, found.t, found.set_label) == (
        "12", True, "a06", "23.02 nm⁻¹", "97 K", "X")
    assert unique_labels(snapshot, (key,),
                         lambda i: (f"Scan {i.scan}" if i.numbered else i.scan,
                                    i.analyzer, i.t))[key] == "Scan 12 · a06 · 97 K"
    assert identity(snapshot, "missing:key") is None

    summed, summed_key = _snapshot(scan_numbers=(12, 13), label="CsV3Sb5_97K_sum")
    found = identity(summed, summed_key)
    assert found is not None and (found.scan, found.numbered) == ("CsV3Sb5_97K_sum", False)
    assert unique_labels(summed, (summed_key,),
                         lambda i: (f"Scan {i.scan}" if i.numbered else i.scan,
                                    i.analyzer, i.t))[summed_key] == "CsV3Sb5_97K_sum · a06 · 97 K"

    missing, missing_key = _snapshot(q=None, temperature=None)
    found = identity(missing, missing_key)
    assert found is not None and (found.q, found.t) == ("Q missing", "T missing")
    assert unique_labels(missing, (missing_key,),
                         lambda i: (i.scan, i.analyzer, i.q))[missing_key].endswith(
                             "a06 · Q missing")
    assert unique_labels(missing, (missing_key,),
                         lambda i: (f"Scan {i.scan}" if i.numbered else i.scan,
                                    i.analyzer, i.t))[missing_key] == "Scan 12 · a06 · T missing"


def test_unique_labels_append_set_then_stable_hash_only_on_remaining_collisions():
    x, key_x = _snapshot(label="X", key="expX:12:a06")
    y, key_y = _snapshot(label="Y", key="expY:12:a06")
    third, key_z = _snapshot(scan_numbers=(14,), label="Z", key="expZ:14:a03",
                             q=10.0, analyzer="a03")
    combined = ScanListSnapshot(x.sets + y.sets + third.sets)
    labels = unique_labels(combined, (key_x, key_y, key_z, "absent"),
                           lambda i: (f"Scan {i.scan}" if i.numbered else i.scan,
                                      i.analyzer, i.t))
    assert labels == {
        key_x: "Scan 12 · a06 · 97 K · X",
        key_y: "Scan 12 · a06 · 97 K · Y",
        key_z: "Scan 14 · a03 · 97 K",
    }

    same, same_key = _snapshot(label="X", key="expSame:12:a06")
    collision = ScanListSnapshot(x.sets + same.sets)
    collision_labels = unique_labels(collision, (key_x, same_key),
                                     lambda i: (f"Scan {i.scan}", i.analyzer, i.t))
    assert collision_labels[key_x].endswith("X · " + hashlib.sha256(
        key_x.encode()).hexdigest()[:6])
    assert collision_labels[same_key].endswith("X · " + hashlib.sha256(
        same_key.encode()).hexdigest()[:6])


def test_pooled_identity_uses_its_label_and_has_no_analyzer_or_set_suffix():
    row = PooledRow("master:900:det", "pooled master (2 members)", Fact(200.0, "K", "fit", "fit"),
                    Fact(2.0, "rlu", "fit", "fit"), 2, Status("done", True), Status("not run", False))
    found = identity(ScanListSnapshot(pooled=(row,)), row.key)
    assert found is not None
    assert found.scan == row.label and not found.numbered
    assert found.analyzer == "" and found.q == "2.00 rlu" and found.t == "200 K"
    assert found.set_label == ""


def test_spectra_read_public_stub_exports_and_cache_construct():
    cache = SpectraCache()
    assert isinstance(cache, SpectraCache)
    assert CACHE_SIZE == 64
