"""The interprocess store lock (PR #33 review B-2): two real processes racing
``write_revision`` from one snapshot, exactly one wins; lock timeout names the lock path;
a stale lock is taken over rather than wedging the workspace forever.

The two-process test is the reviewer's demonstrated failure, reproduced as a real
``multiprocessing`` regression (spawn-safe: the worker is a module-level function, picklable
args only) — run it against pre-lock code first to see it fail (see the session report), then
against the fix.
"""
from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from isar.scene.model import canonical_bytes, digest_bytes
from isar.scene.store import ConcurrentRevisionWrite, RevisionStore, StoreLockTimeout
import isar.scene.store as store_module

_LOCK_TEST_STORE = RevisionStore("lock_test", "isar.lock-test-pointer/1", "lock_test")


@dataclass(frozen=True, slots=True)
class _Rev:
    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "_Rev":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))


def _write_worker(report_dir: str, payload: dict, barrier, queue) -> None:
    """Module-level (spawn-picklable): both processes race to publish DIFFERENT content from
    the SAME empty-store snapshot (``expected_parent=None``) -- the reviewer's exact scenario."""
    revision = _Rev.of(payload)
    barrier.wait()
    try:
        _LOCK_TEST_STORE.write_revision(Path(report_dir), revision, expected_parent=None)
        queue.put("ok")
    except ConcurrentRevisionWrite:
        queue.put("concurrent")
    except Exception as error:  # noqa: BLE001 -- report every failure, never swallow one
        queue.put(f"error:{type(error).__name__}:{error}")


def test_two_processes_racing_one_snapshot_exactly_one_wins(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_write_worker, args=(str(tmp_path), {"writer": "a"}, barrier, queue)),
        ctx.Process(target=_write_worker, args=(str(tmp_path), {"writer": "b"}, barrier, queue)),
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)
        assert w.exitcode == 0, f"worker crashed: exitcode={w.exitcode}"

    results = sorted(queue.get(timeout=1) for _ in workers)
    assert results == ["concurrent", "ok"], (
        f"expected exactly one winner and one ConcurrentRevisionWrite loser, got {results}")

    # The store itself ends up consistent: current.json names whichever digest won, and reads
    # back clean (a torn or double write would fail this).
    pointer, payload = _LOCK_TEST_STORE.read_current(tmp_path)
    assert payload["writer"] in ("a", "b")
    assert pointer["digest"] == digest_bytes(canonical_bytes(payload))


# ----------------------------------------------------------------------------
# Lock timeout
# ----------------------------------------------------------------------------
def test_lock_timeout_raises_naming_the_lock_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_LOCK_WAIT_TOTAL_S", 0.2)
    monkeypatch.setattr(store_module, "_LOCK_RETRY_INTERVAL_S", 0.02)
    root = _LOCK_TEST_STORE._root(tmp_path)
    root.mkdir(parents=True)
    lock_path = root / store_module._LOCK_NAME
    lock_path.write_text("held by nobody real", encoding="utf-8")  # fresh -- never stale here

    revision = _Rev(canonical=b'{"x":1}', digest="c" * 64)
    with pytest.raises(StoreLockTimeout, match=r"lock_test.*current\.lock") as excinfo:
        _LOCK_TEST_STORE.write_revision(tmp_path, revision)
    assert str(lock_path) in str(excinfo.value)


# ----------------------------------------------------------------------------
# Stale-lock takeover
# ----------------------------------------------------------------------------
def _dead_pid() -> int:
    """A PID that verifiably belonged to a process that has exited."""
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_stale_lock_from_a_dead_owner_is_taken_over_with_a_warning(
        tmp_path, monkeypatch, caplog):
    """Takeover needs BOTH: stale by age AND a verifiably dead owner (review W2-1 — age
    alone could evict a live writer paused past the threshold and reintroduce the race)."""
    monkeypatch.setattr(store_module, "_LOCK_STALE_AGE_S", 0.05)
    root = _LOCK_TEST_STORE._root(tmp_path)
    root.mkdir(parents=True)
    lock_path = root / store_module._LOCK_NAME
    lock_path.write_text(str(_dead_pid()), encoding="ascii")
    time.sleep(0.15)                                  # older than the (shrunk) stale threshold

    revision = _Rev(canonical=b'{"x":1}', digest="e" * 64)
    with caplog.at_level("WARNING"):
        result = _LOCK_TEST_STORE.write_revision(tmp_path, revision)
    assert result.unchanged is False and result.digest == "e" * 64
    assert any("abandoned" in record.message for record in caplog.records)
    assert not lock_path.exists()                      # released after the writer finished


def test_a_stale_lock_from_a_live_owner_is_never_taken_over(tmp_path, monkeypatch):
    """A lock past the age threshold whose recorded owner is STILL ALIVE stays untouched:
    the waiter times out (a stall, operator-visible) rather than racing a live writer."""
    monkeypatch.setattr(store_module, "_LOCK_STALE_AGE_S", 0.05)
    monkeypatch.setattr(store_module, "_LOCK_WAIT_TOTAL_S", 0.3)
    monkeypatch.setattr(store_module, "_LOCK_RETRY_INTERVAL_S", 0.02)
    root = _LOCK_TEST_STORE._root(tmp_path)
    root.mkdir(parents=True)
    lock_path = root / store_module._LOCK_NAME
    lock_path.write_text(str(os.getpid()), encoding="ascii")   # this test process: alive
    time.sleep(0.15)

    revision = _Rev(canonical=b'{"x":1}', digest="a1" * 32)
    with pytest.raises(StoreLockTimeout, match="pid"):
        _LOCK_TEST_STORE.write_revision(tmp_path, revision)
    assert lock_path.exists()                          # never deleted from under a live owner


def test_an_unreadable_lock_owner_counts_as_alive(tmp_path, monkeypatch):
    """'We could not tell' is not proof of death: garbage owner records block takeover and
    route to the timeout path, which stalls but never races."""
    monkeypatch.setattr(store_module, "_LOCK_STALE_AGE_S", 0.05)
    monkeypatch.setattr(store_module, "_LOCK_WAIT_TOTAL_S", 0.3)
    monkeypatch.setattr(store_module, "_LOCK_RETRY_INTERVAL_S", 0.02)
    root = _LOCK_TEST_STORE._root(tmp_path)
    root.mkdir(parents=True)
    (root / store_module._LOCK_NAME).write_text("not a pid", encoding="utf-8")
    time.sleep(0.15)

    revision = _Rev(canonical=b'{"x":1}', digest="b2" * 32)
    with pytest.raises(StoreLockTimeout):
        _LOCK_TEST_STORE.write_revision(tmp_path, revision)


def test_a_fresh_lock_is_never_taken_over(tmp_path, monkeypatch):
    """The stale-age check must not fire on an ordinary, recently-created lock."""
    monkeypatch.setattr(store_module, "_LOCK_STALE_AGE_S", 30.0)
    monkeypatch.setattr(store_module, "_LOCK_WAIT_TOTAL_S", 0.2)
    monkeypatch.setattr(store_module, "_LOCK_RETRY_INTERVAL_S", 0.02)
    root = _LOCK_TEST_STORE._root(tmp_path)
    root.mkdir(parents=True)
    (root / store_module._LOCK_NAME).write_text(str(_dead_pid()), encoding="ascii")

    revision = _Rev(canonical=b'{"x":1}', digest="f" * 64)
    with pytest.raises(StoreLockTimeout):
        _LOCK_TEST_STORE.write_revision(tmp_path, revision)
