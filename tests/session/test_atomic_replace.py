"""The session writers' atomic swap survives a concurrent reader (the 2026-09-05 campaign death).

A reader holding the live file for milliseconds must not kill the writer; a hold that outlives the
retry budget must still fail loudly rather than be hidden.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time

import pytest

from isar.session import atomic
from isar.session.atomic import replace_retrying


def test_swap_waits_out_a_reader_that_holds_the_target(tmp_path, caplog):
    """Deterministic stand-in for the Windows share-mode lock: the first two swaps are refused,
    the third succeeds, and every refusal is logged (a silent retry would hide a slow reader)."""
    target = tmp_path / "results.csv"
    target.write_text("old", encoding="utf-8")
    temporary = tmp_path / "results.csv.tmp"
    temporary.write_text("new", encoding="utf-8")
    refusals = {"left": 2}
    real_replace = os.replace

    def flaky_replace(src, dst):
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(5, "Access is denied", str(dst))
        real_replace(src, dst)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(atomic.os, "replace", flaky_replace)
        with caplog.at_level(logging.WARNING, logger="isar.session.atomic"):
            replace_retrying(temporary, target, attempts=5, delay_seconds=0.0)
    assert target.read_text(encoding="utf-8") == "new"
    assert not temporary.exists()
    assert sum("blocked by a concurrent reader" in r.message for r in caplog.records) == 2


def test_a_hold_that_outlives_the_budget_still_fails_loudly(tmp_path, caplog):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    temporary = tmp_path / "state.json.tmp"
    temporary.write_text("new", encoding="utf-8")

    def always_locked(src, dst):
        raise PermissionError(5, "Access is denied", str(dst))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(atomic.os, "replace", always_locked)
        with caplog.at_level(logging.WARNING, logger="isar.session.atomic"):
            with pytest.raises(PermissionError):
                replace_retrying(temporary, target, attempts=3, delay_seconds=0.0)
    assert target.read_text(encoding="utf-8") == "old"      # the live file is untouched
    assert any("after 3 attempts" in r.message for r in caplog.records)


@pytest.mark.skipif(sys.platform != "win32", reason="the share-mode lock is a Windows behaviour")
def test_real_windows_reader_hold_is_waited_out(tmp_path):
    """The failure as it happened: a plain ``open()`` for reading holds the target, ``os.replace``
    is denied while it does, and the swap lands once the reader closes."""
    target = tmp_path / "results.csv"
    target.write_text("old", encoding="utf-8")
    temporary = tmp_path / "results.csv.tmp"
    temporary.write_text("new", encoding="utf-8")
    handle = open(target, encoding="utf-8")   # noqa: SIM115 — the hold IS the test
    with pytest.raises(PermissionError):
        os.replace(temporary, target)          # bare swap: exactly the crash of 2026-09-05
    release = threading.Timer(0.15, handle.close)
    release.start()
    started = time.perf_counter()
    replace_retrying(temporary, target)        # default budget (0.8 s) outlasts the 0.15 s hold
    assert time.perf_counter() - started < 0.8
    assert target.read_text(encoding="utf-8") == "new"
