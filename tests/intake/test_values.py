"""The checked writers (C-writers, C-entries): all or nothing, the exact reason, one line per save."""
import pytest

from isar.intake import scans, store, values
from isar.intake.values import physical_analyzers, save_condition, save_value, use_file_value

BUSY = "the intake store is busy (another ISAR process is saving); try again"


@pytest.fixture
def root(tmp_path):
    """A workspace whose import record holds spec1:42 and spec1:43 (file T 97 K)."""
    scans.record_import(tmp_path, [{"scan": k, "facts": {"T_K": {"value": 97.0, "source": "trailer"}}}
                                   for k in ("spec1:42", "spec1:43")])
    return tmp_path


def _entries_bytes(root):
    path = root / store.ENTRIES
    return path.read_bytes() if path.exists() else None


def test_physical_analyzers_are_the_28_card_ids():
    ids = physical_analyzers()
    assert len(ids) == 28 and ids[:2] == ("a03", "a04") and ids[-1] == "a41"
    assert "a10" not in ids and "a14" in ids


def test_temperature_saved_on_ticked_scans_as_one_line(root):
    r = save_value(root, "temperature", "100", ["spec1:42", "spec1:43", "spec1:42"], reason=" thermometer offset ")
    assert (r.ok, r.reason, r.value, r.targets) == (True, None, 100.0, ("spec1:42", "spec1:43"))
    (e,) = store.read(root, store.ENTRIES)
    assert e["id"] == r.entry_id
    assert {k: e[k] for k in ("kind", "fact", "value", "targets", "reason")} == {
        "kind": "value", "fact": "temperature", "value": 100.0, "targets": ["spec1:42", "spec1:43"],
        "reason": "thermometer offset"}


@pytest.mark.parametrize("call, reason", [
    (lambda r: save_value(r, "temperature", 0, ["spec1:42"]),
     "temperature must be a finite number greater than 0 K (got 0)"),
    (lambda r: save_value(r, "temperature", float("nan"), ["spec1:42"]),
     "temperature must be a finite number greater than 0 K (got nan)"),
    (lambda r: save_value(r, "temperature", True, ["spec1:42"]),
     "temperature must be a finite number greater than 0 K (got True)"),
    (lambda r: save_value(r, "temperature", 100.0, ["spec1:42", "spec1:99"]), "unknown scan spec1:99"),
    (lambda r: save_value(r, "temperature", 100.0, "spec1:42"), "targets must be a list of scan keys, not a string"),
    (lambda r: save_value(r, "temperature", 100.0, []), "no targets given"),
    (lambda r: save_value(r, "pressure", 1.0, ["spec1:42"]), "unknown fact 'pressure' (enterable: temperature, slit)"),
    (lambda r: save_value(r, "slit", (60, -1), ["spec1:42"]),
     "slit must be two finite positive numbers in mm, horizontal then vertical (got (60, -1))"),
    (lambda r: save_value(r, "slit", "-1x40", ["spec1:42"]),
     "slit must be two finite positive numbers in mm, horizontal then vertical (got '-1x40')"),
    (lambda r: save_value(r, "slit", "60x-1", ["spec1:42"]),
     "slit must be two finite positive numbers in mm, horizontal then vertical (got '60x-1')"),
    (lambda r: save_value(r, "slit", "0x40", ["spec1:42"]),
     "slit must be two finite positive numbers in mm, horizontal then vertical (got '0x40')"),
    (lambda r: save_value(r, "temperature", 100.0, ["spec1:42"], reason=5), "reason must be text (got int)"),
    (lambda r: save_condition(r, "dead", ["spec1:42:a10"]), "a10 is not a BL43 analyzer (spec1:42:a10)"),
    (lambda r: save_condition(r, "dead", ["spec1:42:a14", "spec1:99:a14"]), "unknown scan spec1:99 (in spec1:99:a14)"),
    (lambda r: save_condition(r, "dead", ["spec1:42"]),
     "spec1:42 names a scan, not an analyzer (a target is <scan key>:aNN)"),
    (lambda r: save_condition(r, "excluded", ["spec1:99"]), "unknown scan spec1:99"),
    (lambda r: save_condition(r, "broken", ["spec1:42"]),
     "unknown condition 'broken' (one of: excluded, dead, counts_accepted)"),
])
def test_a_refused_save_writes_nothing_and_says_why(root, call, reason):
    save_value(root, "temperature", 90.0, ["spec1:43"])      # something already on record
    before = _entries_bytes(root)
    r = call(root)
    assert (r.ok, r.reason, r.value, r.entry_id) == (False, reason, None, None)
    assert _entries_bytes(root) == before                   # byte-identical: no ticked scan changed


def test_a_refused_save_returns_its_targets_as_given(root):
    assert save_value(root, "temperature", 0, ["spec1:42", "spec1:42"]).targets == ("spec1:42", "spec1:42")
    assert save_value(root, "temperature", 1.0, "spec1:42").targets == ("spec1:42",)
    assert save_value(root, "temperature", 1.0, 7).targets == ()
    assert save_condition(root, "dead", ["spec1:42:a10"]).targets == ("spec1:42:a10",)


@pytest.mark.parametrize("call, what", [
    (lambda r, g: save_value(r, "temperature", 100.0, g), "scan keys"),
    (lambda r, g: save_condition(r, "dead", g), "unit keys"),
])
def test_an_unhashable_target_is_refused_with_the_targets_as_given(root, call, what):
    given = ["spec1:42:a14", ["bad"]]
    r = call(root, given)
    assert (r.ok, r.reason, r.targets) == (False, f"target ['bad'] is not a key (targets must be {what})", tuple(given))


def test_slit_is_canonical_and_a_missing_curve_is_never_a_refusal(root):
    assert save_value(root, "slit", "60 x 40", ["spec1:42"]).value == "60x40"
    assert save_value(root, "slit", (60, 60.0), ["spec1:42"]).value == "60x60"
    assert save_value(root, "slit", "35x35", ["spec1:42"]).value == "35x35"     # no curve: a hold later


def test_dead_analyzer_whose_file_is_absent_is_saved_and_persists(root):
    r = save_condition(root, "dead", ["spec1:42:a14"], reason="no counts")
    assert (r.ok, r.value, r.targets) == (True, None, ("spec1:42:a14",))
    (e,) = store.read(root, store.ENTRIES)                    # a fresh read of the store finds it
    assert (e["condition"], e["declared"], e["targets"], e["reason"]) == ("dead", True, ["spec1:42:a14"], "no counts")


def test_counts_accepted_saved_and_reversed(root):
    assert save_condition(root, "counts_accepted", ["spec1:42:a05"], reason="checked with the beamline").ok
    r = save_condition(root, "counts_accepted", ["spec1:42:a05"], declared=False)
    assert (r.ok, r.value, r.targets) == (True, None, ("spec1:42:a05",))
    assert [e["declared"] for e in store.read(root, store.ENTRIES)] == [True, False]


def test_withdrawal_with_nothing_in_force_is_accepted(root):
    r = use_file_value(root, "temperature", ["spec1:42"], reason="file was right")
    assert (r.ok, r.value) == (True, None)
    assert store.read(root, store.ENTRIES)[0]["kind"] == "file_value"


def test_busy_store_returns_not_ok_and_writes_nothing(root, monkeypatch):
    monkeypatch.setattr(store, "LOCK_TIMEOUT_S", 0.1)
    with store._locked(root):
        r = save_value(root, "temperature", 100.0, ["spec1:42"])
    assert (r.ok, r.reason, r.targets) == (False, BUSY, ("spec1:42",))
    assert _entries_bytes(root) is None


def test_os_error_is_a_refusal_naming_the_file(root, monkeypatch):
    def fail(*a, **k):
        raise PermissionError("denied")
    monkeypatch.setattr(values.store, "append", fail)
    r = save_condition(root, "excluded", ["spec1:42"])
    assert (r.ok, r.reason) == (False, "could not write intake/entries.jsonl: denied")


@pytest.mark.parametrize("damage", [
    b'{"kind": "value", "fact": "temper',          # a torn final line
    b"not json\n",                                  # garbage middle lines
    b"[1, 2]\n",
    b'{"kind": "value"}\n',
    b'{"id": "e000000000009", "seq": 9, "at": "2026-10-27T00:00:00+00:00", "kind": "mystery", "targets": []}\n',
])
def test_damaged_entries_line_never_stops_a_save(root, damage):
    assert save_value(root, "temperature", 90.0, ["spec1:43"]).ok
    path = root / store.ENTRIES
    path.write_bytes(path.read_bytes() + damage)
    before = path.read_bytes()
    for r in (save_value(root, "temperature", 100.0, ["spec1:42"]),
              save_condition(root, "dead", ["spec1:42:a14"])):
        assert r.ok, r.reason
    after = path.read_bytes()
    assert after.startswith(before)
    assert len([e for e in store.read(root, store.ENTRIES) if e["kind"] in ("value", "condition")]) == 3
