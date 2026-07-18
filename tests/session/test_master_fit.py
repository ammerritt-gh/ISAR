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
from isar.parsers import ill_in8, spec_s30


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
             *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False,
             bayes_opts=None):
        calls.append(master_id)
        e = ScanEntry(scan_id=master_id, scan_number=int(scan_number), scan_type="measurement",
                      content_hash=chash, status="fitted")
        e.detectors["det"] = {"pooled": True, "n_members": ms.n_members, "omega0": 0.95,
                              "members": driver._member_labels(ms.members)}
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
                         boot_executor=None, boot_adaptive=False, bayes_opts=None):
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


class _FakePool:
    """In-process stand-in for ProcessPoolExecutor: runs ``map`` serially so the parallel branch of
    ``_fit_masters`` can be exercised deterministically (real pool children re-import the module and
    would not see the stubbed ``_fit_master_group``)."""
    constructed: list = []

    def __init__(self, max_workers=None):
        type(self).constructed.append(max_workers)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, iterable):
        return [fn(a) for a in iterable]


def _two_groups(ms_members_a, ms_members_b):
    ga = _Grp(("tavi", "42", 200.0), _MS(list(ms_members_a)))
    gb = _Grp(("tavi", "43", 200.0), _MS(list(ms_members_b)))
    gb.key_fields = {"sample": "tavi", "q": [2.3, 0.0, 0.0], "T": 200.0}
    return _Col("merge", [ga, gb])


def test_parallel_branch_preserves_order_and_timings(monkeypatch):
    # >1 group + scan_workers>1 → the parallel branch. Fake pool proves order-preserving map,
    # serial session.put in sorted-group order, per_master timings carried home, and that the
    # pool is sized min(scan_workers, n_refits).
    _FakePool.constructed = []
    monkeypatch.setattr(driver, "ProcessPoolExecutor", _FakePool)
    calls = _stub(monkeypatch)
    col = _two_groups([("tavi", 1, "det"), ("tavi", 2, "det")], [("tavi", 3, "det")])
    s = SessionState()
    per = []
    n = driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                            scan_workers=4, per_master=per)
    assert n == 2
    assert _FakePool.constructed == [2]                       # min(scan_workers=4, n_refits=2)
    assert [c for c in calls] == sorted(calls)                # sorted-group order preserved
    mids = [k for k in s.scans if k.startswith("master:")]
    assert len(mids) == 2
    assert [p["master_id"] for p in per] == sorted(p["master_id"] for p in per)


def test_single_refit_does_not_construct_pool(monkeypatch):
    # ≤1 refit → today's serial in-process path; the pool must never be constructed even with a
    # generous worker budget. A raising ProcessPoolExecutor makes any construction a hard failure.
    def _boom(*a, **k):
        raise AssertionError("ProcessPoolExecutor must not be constructed for a single master refit")

    monkeypatch.setattr(driver, "ProcessPoolExecutor", _boom)
    _stub(monkeypatch)
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), _MS([("tavi", 1, "det")]))])
    s = SessionState()
    # boot_workers=1 so the serial path never opens its own bootstrap pool either.
    n = driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                            scan_workers=8, boot_workers=1)
    assert n == 1


def test_two_refits_single_worker_stays_serial(monkeypatch):
    # >1 group but scan_workers==1 → serial path, no pool.
    def _boom(*a, **k):
        raise AssertionError("ProcessPoolExecutor must not be constructed when scan_workers == 1")

    monkeypatch.setattr(driver, "ProcessPoolExecutor", _boom)
    _stub(monkeypatch)
    col = _two_groups([("tavi", 1, "det")], [("tavi", 3, "det")])
    s = SessionState()
    n = driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                            scan_workers=1, boot_workers=1)
    assert n == 2


def _in8_file(tmp_path, exp_id, first_numor, h):
    card = ill_in8.card_template()
    run = run_builder.build_in8_run(exp_id, n_scans=2, seed0=first_numor, first_numor=first_numor,
                                    q_point=[h, 0.05, 0.0])
    path = tmp_path / f"{exp_id}.numor"
    path.write_bytes(ill_in8.emit_file(run, card))
    return path


def _master_entries(session):
    out = {}
    for mid, e in session.scans.items():
        if mid.startswith("master:"):
            out[mid] = e
    return out


def _prov_no_timing(prov):
    return {k: v for k, v in (prov or {}).items()
            if k not in ("fit_seconds", "two_stage_seconds", "errors_attach_seconds")}


def test_parallel_masters_match_serial_end_to_end(tmp_path):
    # Real multiprocessing gate: two IN8 files → two (Q,T) master groups, both refit on a cold run.
    # Serial (workers=1) vs parallel-masters (workers=4) on fresh workspaces must produce identical
    # results.csv bytes and identical master entries (modulo *_seconds timings). Uses the `fast`
    # (HESSE, no bootstrap) tier so the real spawn/pickle/order path is exercised cheaply; the
    # bootstrap's worker-count invariance is covered by errors.bootstrap + the scan-pool workorder.
    ill_in8.card_template()
    f1 = _in8_file(tmp_path, "SIMA", 55000, 2.15)
    f2 = _in8_file(tmp_path, "SIMB", 56000, 2.55)

    def _run(sub, workers):
        ws = Workspace.open(tmp_path / sub)
        ws.root.mkdir(parents=True, exist_ok=True)
        ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                                  encoding="utf-8")
        driver.run_batch(ws, [f1, f2], M=25, detectors="det0", formats=(), workers=workers,
                         errors_tier="fast")
        _card, session = ws.load()
        csv_bytes = (ws.report_dir / "results.csv").read_bytes()
        return session, csv_bytes

    ser_session, ser_csv = _run("serial", 1)
    par_session, par_csv = _run("parallel", 4)

    ser_masters = _master_entries(ser_session)
    par_masters = _master_entries(par_session)
    assert len(ser_masters) == 2 and set(ser_masters) == set(par_masters)

    for mid in ser_masters:
        se, pe = ser_masters[mid], par_masters[mid]
        assert se.status == pe.status
        assert se.content_hash == pe.content_hash
        assert se.scan_number == pe.scan_number
        assert se.detectors == pe.detectors            # omega0, intervals, modes, calib, ...
        assert _prov_no_timing(se.provenance) == _prov_no_timing(pe.provenance)

    assert ser_csv == par_csv                           # results.csv byte-identical

    # Chain of custody: every pooled results.csv row names its members, len == n_members.
    from isar.drive.isar_io.readers import read_results_csv
    pooled_rows = [r for r in read_results_csv(tmp_path / "serial") if r.get("pooled")]
    assert len(pooled_rows) == 2
    for r in pooled_rows:
        assert r["members"] and len(r["members"]) == r["n_members"]
        assert r["acquisition_id"].startswith("pooled:")

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


# ---------------------------------------------------------------------------
# Member provenance (chain of custody)
# ---------------------------------------------------------------------------
def test_member_labels_unique_are_bare_strings():
    # Distinct scan numbers → bare native scan numbers as strings, in ms.members order.
    members = [("tavi", 473, "det"), ("tavi", 493, "det"), ("tavi", 494, "det")]
    assert driver._member_labels(members) == ["473", "493", "494"]


def test_member_labels_repeated_scan_is_qualified():
    # A scan number that repeats within the list (multi-detector merge) is qualified exp:scan:det;
    # a non-repeating one stays bare.
    members = [("tavi", 473, "a"), ("tavi", 473, "b"), ("tavi", 500, "a")]
    assert driver._member_labels(members) == ["tavi:473:a", "tavi:473:b", "500"]


def test_carry_backfills_members_without_refit(monkeypatch):
    # An older ledger master (fit before the members field existed) is backfilled on the carry path:
    # the content-hash match proves the member set is identical, so labels are recomputed exactly —
    # no refit (the fit stub must NOT be called again).
    calls = _stub(monkeypatch)
    ms = _MS([("tavi", 473, "det"), ("tavi", 493, "det"), ("tavi", 494, "det")])
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), ms)])
    s = SessionState()
    assert driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    mid = next(k for k in s.scans if k.startswith("master:"))
    assert len(calls) == 1

    # Simulate an old ledger: strip members from the detector dict + a provenance lacking it.
    del s.scans[mid].detectors["det"]["members"]
    s.scans[mid].provenance = {"pooled": True, "n_members": 3}

    # Same member set → carry path; the stub is not called again but members are restored.
    assert driver._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 0
    assert len(calls) == 1                                     # no refit
    restored = s.scans[mid].detectors["det"]["members"]
    assert restored == ["473", "493", "494"]
    assert len(restored) == s.scans[mid].detectors["det"]["n_members"]
    assert s.scans[mid].provenance["members"] == [["tavi", 473, "det"], ["tavi", 493, "det"],
                                                  ["tavi", 494, "det"]]


def _master_entry(members, n_members=None):
    oc = driver.FitOutcome(
        detector_id="merged", verdict="clear_phonon", disposition="resolved", confidence=0.9,
        omega0=0.95, omega0_err=0.02, interval=(0.9, 1.0), modality="single", gamma=0.3,
        gamma_provisional=False, best_label="elastic+stokes+bg", cost_per_dof=1.0,
        q=[2.1, 0.0, 0.0], T=200.0, sample="tavi", prior_used=None, calib={},
        pooled=True, n_members=(n_members if n_members is not None else len(members)),
        members=members)
    e = ScanEntry(scan_id="master:tavi_42_200.0", scan_number=driver._MASTER_SCAN_BASE + 1,
                  scan_type="measurement", content_hash="c", status="fitted")
    e.detectors["merged"] = oc.to_dict()
    return e


def _unpooled_entry():
    oc = driver.FitOutcome(
        detector_id="det", verdict="clear_phonon", disposition="resolved", confidence=0.8,
        omega0=1.2, omega0_err=0.03, interval=(1.1, 1.3), modality="single", gamma=0.2,
        gamma_provisional=False, best_label="elastic+stokes+bg", cost_per_dof=1.1,
        q=[1.0, 0.0, 0.0], T=200.0, sample="tavi", prior_used=None, calib={})
    e = ScanEntry(scan_id="tavi:5:det", scan_number=5, scan_type="measurement",
                  content_hash="c", status="fitted")
    e.detectors["det"] = oc.to_dict()
    return e


def test_results_csv_round_trips_members(tmp_path):
    from isar.drive.isar_io.readers import read_results_csv
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    s = SessionState()
    s.put(_master_entry(["473", "493", "494"]))
    s.put(_unpooled_entry())
    driver._write_results_csv(ws, s)

    rows = {r["scan"]: r for r in read_results_csv(ws.root)}
    assert rows[driver._MASTER_SCAN_BASE + 1]["members"] == ["473", "493", "494"]
    assert rows[5]["members"] is None                          # unpooled → empty cell → None


def test_write_results_csv_raises_on_member_count_mismatch(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    s = SessionState()
    s.put(_master_entry(["473", "493"], n_members=3))          # 2 members, claims 3
    with pytest.raises(ValueError, match="member provenance corrupt"):
        driver._write_results_csv(ws, s)
