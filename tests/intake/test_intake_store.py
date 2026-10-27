"""Intake store (C-store): each test is a worked example of the contract or the failure it guards."""
import logging
import multiprocessing as mp
import re
from datetime import datetime, timedelta

import pytest

from isar.intake import store

ENTRIES = store.ENTRIES


def _writer(root, n):  # module level so a spawned child can import it
    for i in range(n):
        store.append(root, ENTRIES, {"kind": "value", "i": i})


def test_first_append_writes_one_terminated_line(tmp_path):
    caller = {"kind": "value"}
    e = store.append(tmp_path, ENTRIES, caller)
    assert re.fullmatch(r"e[0-9a-f]{12}", e["id"]) and e["seq"] == 1
    assert datetime.fromisoformat(e["at"]).utcoffset() == timedelta(0)
    raw = (tmp_path / ENTRIES).read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert store.read(tmp_path, ENTRIES) == [e]
    assert caller == {"kind": "value"}  # the caller's dict is not mutated


def test_torn_tail_is_skipped_then_ended_by_the_next_append(tmp_path, caplog):
    store.append(tmp_path, ENTRIES, {"kind": "a"})
    store.append(tmp_path, ENTRIES, {"kind": "b"})
    with open(tmp_path / ENTRIES, "ab") as f:
        f.write(b'{"kind": "resol')  # a write cut off before its newline
    with caplog.at_level(logging.WARNING, logger="isar.intake.store"):
        assert [e["seq"] for e in store.read(tmp_path, ENTRIES)] == [1, 2]
    assert any("line 3" in r.getMessage() for r in caplog.records)
    stored = store.append(tmp_path, ENTRIES, {"kind": "c"})
    assert stored["seq"] == 3  # previous max + 1; the torn line carries no seq
    assert [e["seq"] for e in store.read(tmp_path, ENTRIES)] == [1, 2, 3]


@pytest.mark.parametrize("bad", [{"seq": 9}, {"id": "e1"}, {"at": "now"}, {"x": float("nan")}])
def test_reserved_keys_and_unserialisable_values_write_nothing(tmp_path, bad):
    with pytest.raises(ValueError):
        store.append(tmp_path, ENTRIES, bad)
    assert not (tmp_path / ENTRIES).exists()


def test_refused_check_writes_nothing(tmp_path):
    store.append(tmp_path, ENTRIES, {"kind": "resolution_folder"})
    before = (tmp_path / ENTRIES).read_bytes()

    def no_second_folder(entries):
        return "already saved" if any(e["kind"] == "resolution_folder" for e in entries) else None

    with pytest.raises(store.Refused) as info:
        store.append(tmp_path, ENTRIES, {"kind": "resolution_folder"}, check=no_second_folder)
    assert info.value.reason == "already saved"
    assert (tmp_path / ENTRIES).read_bytes() == before


def test_non_object_line_raises_naming_the_line(tmp_path):
    (tmp_path / "intake").mkdir()
    (tmp_path / ENTRIES).write_bytes(b'{"id": "e000000000001", "seq": 1}\n[1, 2]\n')
    with pytest.raises(ValueError, match="line 2"):
        store.read(tmp_path, ENTRIES)


def test_concurrent_appends_keep_every_line(tmp_path):
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(tmp_path), 50)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        if p.is_alive():
            p.terminate()
    assert [p.exitcode for p in procs] == [0, 0, 0, 0]
    assert sorted(e["seq"] for e in store.read(tmp_path, ENTRIES)) == list(range(1, 201))
