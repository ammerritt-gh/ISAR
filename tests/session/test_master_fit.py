"""Stage C: fitting the merged "master scan" per (Q,T) group.

Orchestration + idempotency are tested with the per-group fit STUBBED (real pooled fitting is slow
and is exercised live); a real S30 batch confirms the ``merge="separate"`` gate produces no masters.
"""
from pathlib import Path

import pytest

from isar.session import driver
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace
from isar.synth_format import run_builder
from isar.parsers import spec_s30


class _MS:
    def __init__(self, members):
        self.members = members
        self.n_members = len(members)


class _Grp:
    def __init__(self, key, merged):
        self.key = key
        self.key_fields = {"sample": "tavi", "q": [2.1, 0.0, 0.0], "T": 200.0}
        self.merged = merged


class _Col:
    def __init__(self, merge, groups):
        self.merge = merge
        self.groups = groups


def _stub(monkeypatch):
    calls = []

    def fake(ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number, chash,
             *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False):
        calls.append(master_id)
        e = ScanEntry(scan_id=master_id, scan_number=int(scan_number), scan_type="measurement",
                      content_hash=chash, status="fitted")
        e.detectors["det"] = {"pooled": True, "n_members": ms.n_members, "omega0": 0.95}
        return e

    monkeypatch.setattr(driver, "_fit_master_group", fake)
    return calls


def test_no_masters_when_card_says_separate(monkeypatch):
    calls = _stub(monkeypatch)
    col = _Col("separate", [_Grp(("tavi", "42", 200.0), _MS([("tavi", 1, "det"), ("tavi", 2, "det")]))])
    s = SessionState()
    n = driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000)
    assert n == 0 and not calls
    assert not any(k.startswith("master:") for k in s.scans)


def test_master_fit_and_member_set_idempotency(monkeypatch):
    _stub(monkeypatch)
    ms = _MS([("tavi", 1, "det"), ("tavi", 2, "det")])
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), ms)])
    s = SessionState()

    assert driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    mid = next(k for k in s.scans if k.startswith("master:"))
    assert s.scans[mid].detectors["det"]["n_members"] == 2
    assert s.scans[mid].scan_number >= driver._MASTER_SCAN_BASE   # sorts after real scans

    # Same member set → carried, no refit.
    assert driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 0

    # A new member arrives → the master refits and reflects the larger pool.
    ms.members.append(("tavi", 3, "det"))
    ms.n_members = 3
    assert driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    assert s.scans[mid].detectors["det"]["n_members"] == 3


def test_run_tasks_serial_threads_shared_executor_and_adaptive(monkeypatch):
    # The workorder's contract: the SERIAL path threads the shared bootstrap pool + adaptive flag
    # into each fit (an executor is not picklable → it must never enter the pool path).
    captured = {}

    def fake_process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                         chash, ghash, *, M, seed, errors_tier="full", boot_workers=1,
                         boot_executor=None, boot_adaptive=False):
        captured.update(executor=boot_executor, adaptive=boot_adaptive, boot_workers=boot_workers)
        return ScanEntry(scan_id=scan_id, scan_number=1, scan_type="measurement",
                         content_hash=chash, status="fitted")

    monkeypatch.setattr(driver, "_process_one", fake_process_one)
    sentinel = object()
    tasks = [(None, "det", [0.0, 0.0, 0.0], 0.0, "S", "E:1:det", "ch", "gh", 6001)]
    out = driver._run_tasks(tasks, None, None, None, set(), M=20, errors_tier="full",
                            boot_workers=8, use_pool=False, scan_workers=8,
                            boot_executor=sentinel, boot_adaptive=True)
    assert len(out) == 1
    assert captured["executor"] is sentinel
    assert captured["adaptive"] is True and captured["boot_workers"] == 8


def test_s30_separate_card_produces_no_master_rows(tmp_path):
    # Real S30 batch (held, merge="separate"): the master pass must be a no-op — per-scan path intact.
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(spec_s30._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    run = run_builder.build_s30_run("SYN", n_scans=3, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(spec_s30.emit_file(run, spec_s30.card_template()))
    driver.run_batch(ws, [data], M=10, detectors="ana5")
    _card, session = ws.load()
    assert not any(k.startswith("master:") for k in session.scans)
