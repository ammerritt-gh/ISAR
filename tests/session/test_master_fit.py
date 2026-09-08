"""Stage C: fitting the merged "master scan" per (Q,T) group.

Orchestration + idempotency are tested with the per-group fit STUBBED (real pooled fitting is slow
and is exercised live); a real S30 batch confirms the ``merge="separate"`` gate produces no masters.
"""
import copy
import json
import types
from concurrent.futures import Future
from pathlib import Path

import pytest

from isar.predictive import PREDICTIVE_CONTRACT
from isar.session import (certification, driver, master_delivery,
                          outcome as outcome_mod, scan_delivery)
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
                              "members": master_delivery._member_labels(ms.members)}
        # Stamped by _stamp_master_analysis on every real path; the carry gate requires it, so a
        # predictive-contract bump regenerates the artifact instead of carrying a stale one.
        e.provenance = {"predictive_contract": PREDICTIVE_CONTRACT}
        return e

    monkeypatch.setattr(master_delivery, "_fit_master_group", fake)
    return calls


def test_no_masters_when_card_says_separate(monkeypatch):
    calls = _stub(monkeypatch)
    col = _Col("separate", [_Grp(("tavi", "42", 200.0), _MS([("tavi", 1, "det"), ("tavi", 2, "det")]))])
    s = SessionState()
    n = master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000)
    assert n == 0 and not calls
    assert not any(k.startswith("master:") for k in s.scans)


def test_master_fit_and_member_set_idempotency(monkeypatch):
    _stub(monkeypatch)
    ms = _MS([("tavi", 1, "det"), ("tavi", 2, "det")])
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), ms)])
    s = SessionState()

    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    mid = next(k for k in s.scans if k.startswith("master:"))
    assert s.scans[mid].detectors["det"]["n_members"] == 2
    assert s.scans[mid].scan_number >= master_delivery._MASTER_SCAN_BASE   # sorts after real scans

    # Same member set → carried, no refit.
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 0

    # A predictive-contract bump refits the same member set exactly once, so an unchanged pool
    # regenerates its predictive artifact under the new contract (ADR-006) instead of carrying a
    # stale one forever.
    s.scans[mid].provenance["predictive_contract"] = "isar.master_predictive/1"
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 0

    # A new member arrives → the master refits and reflects the larger pool.
    ms.members.append(("tavi", 3, "det"))
    ms.n_members = 3
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
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

    monkeypatch.setattr(scan_delivery, "_process_one", fake_process_one)
    sentinel = object()
    tasks = [(None, "det", [0.0, 0.0, 0.0], 0.0, "S", "E:1:det", "ch", "gh", 6001)]
    out = scan_delivery._run_tasks(tasks, None, None, None, set(), M=20, errors_tier="full",
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
    monkeypatch.setattr(master_delivery, "ProcessPoolExecutor", _FakePool)
    calls = _stub(monkeypatch)
    col = _two_groups([("tavi", 1, "det"), ("tavi", 2, "det")], [("tavi", 3, "det")])
    s = SessionState()
    per = []
    n = master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
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

    monkeypatch.setattr(master_delivery, "ProcessPoolExecutor", _boom)
    _stub(monkeypatch)
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), _MS([("tavi", 1, "det")]))])
    s = SessionState()
    # boot_workers=1 so the serial path never opens its own bootstrap pool either.
    n = master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                            scan_workers=8, boot_workers=1)
    assert n == 1


def test_two_refits_single_worker_stays_serial(monkeypatch):
    # >1 group but scan_workers==1 → serial path, no pool.
    def _boom(*a, **k):
        raise AssertionError("ProcessPoolExecutor must not be constructed when scan_workers == 1")

    monkeypatch.setattr(master_delivery, "ProcessPoolExecutor", _boom)
    _stub(monkeypatch)
    col = _two_groups([("tavi", 1, "det")], [("tavi", 3, "det")])
    s = SessionState()
    n = master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                            scan_workers=1, boot_workers=1)
    assert n == 2


def test_force_masters_refits_only_the_named_master(monkeypatch):
    # The predictive-escalation contract: an unchanged member set is carried on a plain rerun, and
    # ``force_masters`` exempts exactly the named master from that carry — nothing else refits.
    calls = _stub(monkeypatch)
    col = _two_groups([("tavi", 1, "det"), ("tavi", 2, "det")], [("tavi", 3, "det")])
    s = SessionState()
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 2
    target = sorted(k for k in s.scans if k.startswith("master:"))[0]

    calls.clear()
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 0
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                               force_masters=frozenset({target})) == 1
    assert calls == [target]


def test_force_masters_unknown_id_fails_loud(monkeypatch):
    # A typo'd/stale id must be a hard failure: the escalation charge that requested it is durable,
    # so a silent no-op would leave the master unpromotable forever with a "completed" outcome on
    # file. The run failing turns that into a visible "failed" promotion instead.
    _stub(monkeypatch)
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), _MS([("tavi", 1, "det")]))])
    s = SessionState()
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    with pytest.raises(ValueError, match="match no collation group"):
        master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000,
                            force_masters=frozenset({"master:nope"}))


# The run_batch campaign family below is slow-marked (owner rule 2026-08-08): each test drives
# real fits over synthetic IN8 files, and under evidence selection every clean synthetic scan
# tends to offer a junk 2-mode superset whose railed member escalates to dynesty — per-scan
# escalation cost × scans × batches made the worst of these (~drains_wave) run 30+ minutes in
# the fast lane. The full lane still runs them pre-merge; pytest-timeout (900 s) bounds each.
@pytest.mark.slow
def test_force_masters_accepts_a_bare_string_as_one_id(tmp_path, monkeypatch):
    # A bare string is one ledger id, not an iterable of characters; iterating it would produce
    # one-character ids that match nothing and fail the targeted refit loudly but wrongly.
    data = _in8_file(tmp_path, "STRM", 54700, 2.25)
    ws = Workspace.open(tmp_path / "string-ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    captured = {}

    def fake_fit_masters(*a, **kw):
        captured["force_masters"] = kw.get("force_masters")
        return 0

    monkeypatch.setattr(master_delivery, "_fit_masters", fake_fit_masters)
    driver.run_batch(ws, [data], force_masters="master:tavi_(43, 0, 0)_200.0",
                     detectors="det0", formats=(), workers=1, errors_tier="cheap")
    assert captured["force_masters"] == frozenset({"master:tavi_(43, 0, 0)_200.0"})


def test_force_masters_without_merged_collation_fails_loud(monkeypatch):
    # merge="separate" (or no collation at all) can never satisfy a targeted refit; failing beats
    # returning 0 and letting the caller record a completed promotion.
    _stub(monkeypatch)
    col = _Col("separate", [_Grp(("tavi", "42", 200.0), _MS([("tavi", 1, "det")]))])
    with pytest.raises(ValueError, match="no merged collation"):
        master_delivery._fit_masters(col, [], None, None, None, SessionState(), M=10, seed_base=6000,
                            force_masters=frozenset({"master:tavi_('42',)_200.0"}))


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


def _detectors_no_timing(detectors):
    out = copy.deepcopy(detectors)
    for outcome in out.values():
        shadow = outcome.get("shadow_model_selection")
        if isinstance(shadow, dict):
            shadow.pop("runtime_increment_seconds", None)
    return out


@pytest.mark.slow
def test_run_batch_routes_scan_and_master_bootstrap_caps_by_role(tmp_path, monkeypatch):
    data = _in8_file(tmp_path, "ROLEM", 54500, 2.25)
    ws = Workspace.open(tmp_path / "role-ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    observed = {}
    real_run_tasks = scan_delivery._run_tasks
    real_fit_masters = master_delivery._fit_masters

    def scan_spy(*args, **kwargs):
        observed["scan_M"] = kwargs["M"]
        return real_run_tasks(*args, **kwargs)

    def master_spy(*args, **kwargs):
        observed["master_M"] = kwargs["M"]
        return real_fit_masters(*args, **kwargs)

    monkeypatch.setattr(scan_delivery, "_run_tasks", scan_spy)
    monkeypatch.setattr(master_delivery, "_fit_masters", master_spy)
    driver.run_batch(ws, [data], M=123, scan_M=17, detectors="det0", formats=(),
                     workers=1, errors_tier="cheap")

    assert observed == {"scan_M": 17, "master_M": 123}


@pytest.mark.slow
def test_run_batch_routes_optional_error_tiers_by_role_and_keeps_legacy_default(
        tmp_path, monkeypatch):
    data = _in8_file(tmp_path, "ROLET", 54600, 2.25)

    def execute(name, **tier_kwargs):
        ws = Workspace.open(tmp_path / name)
        ws.root.mkdir(parents=True, exist_ok=True)
        ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                                  encoding="utf-8")
        observed = {}
        real_run_tasks = scan_delivery._run_tasks
        real_fit_masters = master_delivery._fit_masters

        def scan_spy(*args, **kwargs):
            observed["scan"] = kwargs["errors_tier"]
            return real_run_tasks(*args, **kwargs)

        def master_spy(*args, **kwargs):
            observed["master"] = kwargs["errors_tier"]
            return real_fit_masters(*args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(scan_delivery, "_run_tasks", scan_spy)
            scoped.setattr(master_delivery, "_fit_masters", master_spy)
            driver.run_batch(ws, [data], M=10, detectors="det0", formats=(), workers=1,
                             errors_tier="cheap", **tier_kwargs)
        return observed

    assert execute("legacy") == {"scan": "cheap", "master": "cheap"}
    assert execute("split", scan_errors_tier="fast", master_errors_tier="cheap") == {
        "scan": "fast", "master": "cheap"}


# The three heaviest campaign tests once exceeded the global 900 s ceiling (the full lane's
# worker-kill failures of 2026-08-08 are exactly these three): explicit budgets so the full lane
# completes with named results. Verified by full-lane rerun 2026-08-09 — the budgets hold, and
# under the suite's ISAR_ESCALATION_N_LIVE=32 cap these now run 614 s / 405 s / 385 s against a
# 26:16 -> 11:25 whole-lane time, so the budgets are headroom rather than a live constraint.
# They stay: the cap is a test-side measure, and the real cost reduction is still the N-mode
# growth policy (H8, design recorded, not implemented).
@pytest.mark.timeout(2700)
@pytest.mark.slow
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
        assert _detectors_no_timing(se.detectors) == _detectors_no_timing(pe.detectors)
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


@pytest.mark.slow
def test_master_only_certification_fits_no_physical_rows_and_reuses_publication(
        tmp_path, monkeypatch):
    from isar.drive import loop as drive_loop

    data = _in8_file(tmp_path, "CERT", 57000, 2.25)
    ws = Workspace.open(tmp_path / "cert-ws")
    ws.root.mkdir(parents=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver.run_batch(ws, [data], M=25, detectors="det0", formats=(), workers=1,
                     errors_tier="fast")
    _card, before = ws.load()
    masters = [entry for entry in before.scans.values() if entry.scan_id.startswith("master:")]
    assert len(masters) == 1
    master = masters[0]
    outcome = next(value for value in master.detectors.values() if value.get("pooled"))
    physical_before = {
        key: copy.deepcopy(entry) for key, entry in before.scans.items()
        if not key.startswith("master:")
    }
    decision = types.SimpleNamespace(per_q=[{"hkl": list(outcome["q"])}])
    objective = types.SimpleNamespace(path=[tuple(outcome["q"])])
    cfg = types.SimpleNamespace(
        workspace=ws.root, log_path=ws.report_dir / "session_log.jsonl",
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0)
    request_path = drive_loop._certification_request(
        cfg, objective, decision, "full", activity_id="test-certification")
    manifest_path = certification.create_master_certification_manifest(ws.root, request_path)
    resumed_request = drive_loop._certification_request(
        cfg, objective, decision, "full", activity_id="test-certification-resumed")
    assert resumed_request == request_path
    assert certification.create_master_certification_manifest(
        ws.root, resumed_request) == manifest_path
    assert json.loads((manifest_path.parent / "runtime.json").read_text(
        encoding="utf-8"))["activity_id"] == "test-certification-resumed"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    unit = manifest["worklist"][0]
    calls = []

    def fake_master_task(args):
        calls.append(args[7])
        entry = copy.deepcopy(master)
        entry.content_hash = args[9]
        entry.provenance = {
            **entry.provenance,
            "analysis_tier": "full",
            "master_fit_contract": "isar.master_fit/2",
            "master_fit_fingerprint": unit["master_fit_fingerprint"],
            "spectrum_digest": unit["spectrum_digest"],
        }
        return entry

    monkeypatch.setattr(master_delivery, "_fit_master_group_task", fake_master_task)
    with pytest.raises(ValueError, match="force=True"):
        driver.run_batch(
            ws, [data], M=200, detectors="det0", formats=(), workers=1,
            errors_tier="full", certification_manifest=manifest_path, force=True)
    summary = driver.run_batch(
        ws, [data], M=200, detectors="det0", formats=(), workers=1,
        errors_tier="full", certification_manifest=manifest_path)
    assert not summary.analysis_incomplete
    assert calls == [master.scan_id]
    _card, after = ws.load()
    assert {key: entry for key, entry in after.scans.items()
            if not key.startswith("master:")} == physical_before
    assert after.scans[master.scan_id].provenance["analysis_tier"] == "full"
    current = json.loads(
        (ws.report_dir / "certification" / "current.json").read_text(encoding="utf-8"))
    assert current["fingerprint"] == manifest["fingerprint"]
    assert set(current["published_hashes"]) == {
        "session.json", "card.state.json", "results.csv", "card.policy.toml"}

    calls.clear()
    second = driver.run_batch(
        ws, [data], M=200, detectors="det0", formats=(), workers=1,
        errors_tier="full", certification_manifest=manifest_path)
    assert not second.analysis_incomplete
    assert calls == []
    progress = json.loads((manifest_path.parent / "progress.json").read_text(encoding="utf-8"))
    assert progress["completed"] == 1 and progress["reused_workspace"] == 1


@pytest.mark.timeout(2700)
@pytest.mark.slow
def test_master_certification_subset_refits_only_dirty_q_and_preserves_clean_master(
        tmp_path, monkeypatch):
    from isar.drive import loop as drive_loop

    first_data = _in8_file(tmp_path, "SUBA", 57500, 2.15)
    second_data = _in8_file(tmp_path, "SUBB", 57600, 2.55)
    ws = Workspace.open(tmp_path / "subset-ws")
    ws.root.mkdir(parents=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver.run_batch(ws, [first_data, second_data], M=25, detectors="det0", formats=(),
                     workers=1, errors_tier="fast")
    _card, before = ws.load()
    masters = sorted(
        (entry for entry in before.scans.values() if entry.scan_id.startswith("master:")),
        key=lambda entry: next(iter(entry.detectors.values()))["q"][0])
    assert len(masters) == 2
    clean_before = copy.deepcopy(masters[0])
    outcomes = [next(value for value in entry.detectors.values() if value.get("pooled"))
                for entry in masters]
    decision = types.SimpleNamespace(per_q=[{"hkl": list(outcome["q"])}
                                             for outcome in outcomes])
    objective = types.SimpleNamespace(path=[tuple(outcome["q"]) for outcome in outcomes])
    cfg = types.SimpleNamespace(
        workspace=ws.root, log_path=ws.report_dir / "session_log.jsonl",
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0)
    request_path = drive_loop._certification_request(
        cfg, objective, decision, "full", activity_id="test-subset",
        requested_indices=[1])
    manifest_path = certification.create_master_certification_manifest(ws.root, request_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [unit["master_id"] for unit in manifest["worklist"]] == [masters[1].scan_id]
    unit = manifest["worklist"][0]
    calls = []

    def fake_master_task(args):
        calls.append(args[7])
        entry = copy.deepcopy(masters[1])
        entry.content_hash = args[9]
        entry.provenance.update({
            "analysis_tier": "full", "master_fit_contract": "isar.master_fit/2",
            "master_fit_fingerprint": unit["master_fit_fingerprint"],
            "spectrum_digest": unit["spectrum_digest"],
        })
        return entry

    monkeypatch.setattr(master_delivery, "_fit_master_group_task", fake_master_task)
    summary = driver.run_batch(
        ws, [first_data, second_data], M=200, detectors="det0", formats=(), workers=1,
        errors_tier="full", certification_manifest=manifest_path)

    assert not summary.analysis_incomplete and calls == [masters[1].scan_id]
    _card, after = ws.load()
    assert after.scans[masters[0].scan_id] == clean_before
    assert after.scans[masters[1].scan_id].provenance["analysis_tier"] == "full"


def test_master_certification_survives_a_master_born_out_of_key_order(tmp_path, monkeypatch):
    """A master keeps the synthetic scan number it was born with; a later master with a lower
    collation key shifts every sorted position after it. The final certification compared the
    ledger-derived group index with the run's sorted position and died on "group-index drift"
    after the planner had met the goal (acoustic path measured h = 2.6 before 2.55, 2026-09-08).
    The ledger number is the identity; the sorted position is an enumeration artefact."""
    from isar.drive import loop as drive_loop

    later_key = _in8_file(tmp_path, "OOA", 57500, 2.55)   # measured first, born as 9000000
    earlier_key = _in8_file(tmp_path, "OOB", 57600, 2.15)  # measured second, sorts first
    ws = Workspace.open(tmp_path / "order-ws")
    ws.root.mkdir(parents=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver.run_batch(ws, [later_key], M=25, detectors="det0", formats=(), workers=1,
                     errors_tier="fast")
    driver.run_batch(ws, [later_key, earlier_key], M=25, detectors="det0", formats=(),
                     workers=1, errors_tier="fast")
    _card, before = ws.load()
    masters = sorted(
        (entry for entry in before.scans.values() if entry.scan_id.startswith("master:")),
        key=lambda entry: next(iter(entry.detectors.values()))["q"][0])
    assert len(masters) == 2
    born_first = masters[1]                      # h = 2.55: sorted position 1, ledger number 9000000
    assert born_first.scan_number == master_delivery._MASTER_SCAN_BASE
    outcomes = [next(value for value in entry.detectors.values() if value.get("pooled"))
                for entry in masters]
    decision = types.SimpleNamespace(per_q=[{"hkl": list(outcome["q"])} for outcome in outcomes])
    objective = types.SimpleNamespace(path=[tuple(outcome["q"]) for outcome in outcomes])
    cfg = types.SimpleNamespace(
        workspace=ws.root, log_path=ws.report_dir / "session_log.jsonl",
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0)
    request_path = drive_loop._certification_request(
        cfg, objective, decision, "full", activity_id="test-order")
    manifest_path = certification.create_master_certification_manifest(ws.root, request_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    units = {unit["master_id"]: unit for unit in manifest["worklist"]}
    assert units[born_first.scan_id]["group_index"] == 0
    seen = []

    def fake_master_task(args):
        seen.append((args[7], args[8]))
        entry = copy.deepcopy(before.scans[args[7]])
        entry.content_hash = args[9]
        entry.provenance.update({
            "analysis_tier": "full", "master_fit_contract": "isar.master_fit/2",
            "master_fit_fingerprint": units[args[7]]["master_fit_fingerprint"],
            "spectrum_digest": units[args[7]]["spectrum_digest"],
        })
        return entry

    monkeypatch.setattr(master_delivery, "_fit_master_group_task", fake_master_task)
    summary = driver.run_batch(
        ws, [later_key, earlier_key], M=200, detectors="det0", formats=(), workers=1,
        errors_tier="full", certification_manifest=manifest_path)
    assert not summary.analysis_incomplete
    # The certified entry is written under the master's ledger number, not its sorted position.
    assert dict(seen)[born_first.scan_id] == master_delivery._MASTER_SCAN_BASE


@pytest.mark.timeout(2700)
@pytest.mark.slow
def test_master_certification_drains_wave_and_checkpoints_success_after_worker_failure(
        tmp_path, monkeypatch):
    from isar.drive import loop as drive_loop

    first_data = _in8_file(tmp_path, "FAILA", 58000, 2.15)
    second_data = _in8_file(tmp_path, "FAILB", 59000, 2.55)
    ws = Workspace.open(tmp_path / "failure-ws")
    ws.root.mkdir(parents=True)
    ws.policy_path.write_text(Path(ill_in8._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    driver.run_batch(ws, [first_data, second_data], M=25, detectors="det0", formats=(),
                     workers=1, errors_tier="fast")
    _card, session = ws.load()
    masters = sorted(
        (entry for entry in session.scans.values() if entry.scan_id.startswith("master:")),
        key=lambda entry: next(iter(entry.detectors.values()))["q"][0])
    decision = types.SimpleNamespace(per_q=[
        {"hkl": list(next(iter(entry.detectors.values()))["q"])} for entry in masters])
    objective = types.SimpleNamespace(path=[
        tuple(next(iter(entry.detectors.values()))["q"]) for entry in masters])
    cfg = types.SimpleNamespace(
        workspace=ws.root, log_path=ws.report_dir / "session_log.jsonl",
        calibration=types.SimpleNamespace(inflation=1.0), floor=0.0)
    request_path = drive_loop._certification_request(
        cfg, objective, decision, "full", activity_id="test-certification-failure")
    manifest_path = certification.create_master_certification_manifest(ws.root, request_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    units = {unit["master_id"]: unit for unit in manifest["worklist"]}
    by_id = {entry.scan_id: entry for entry in masters}

    def fake_master_task(args):
        master_id = args[7]
        if master_id == masters[0].scan_id:
            raise RuntimeError("deliberate worker failure")
        entry = copy.deepcopy(by_id[master_id])
        unit = units[master_id]
        entry.content_hash = args[9]
        entry.provenance.update({
            "analysis_tier": "full", "master_fit_contract": "isar.master_fit/2",
            "master_fit_fingerprint": unit["master_fit_fingerprint"],
            "spectrum_digest": unit["spectrum_digest"],
        })
        return entry

    class FakePool:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def submit(self, fn, arg):
            future = Future()
            try:
                future.set_result(fn(arg))
            except BaseException as exc:
                future.set_exception(exc)
            return future

        def shutdown(self):
            pass

    monkeypatch.setattr(master_delivery, "_fit_master_group_task", fake_master_task)
    monkeypatch.setattr(driver, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(
        driver, "as_completed",
        lambda futures: sorted(futures, key=lambda future: future.exception() is None))
    with pytest.raises(RuntimeError, match="deliberate worker failure"):
        driver.run_batch(
            ws, [first_data, second_data], M=200, detectors="det0", formats=(), workers=2,
            errors_tier="full", certification_manifest=manifest_path)

    progress = json.loads((manifest_path.parent / "progress.json").read_text(encoding="utf-8"))
    assert progress["completed"] == 1 and progress["phase"] == "failed"
    assert len(list((manifest_path.parent / "units").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# Member provenance (chain of custody)
# ---------------------------------------------------------------------------
def test_member_labels_are_always_fully_qualified():
    # Every label is the full ledger key exp:scan:det, in ms.members order — never a bare scan
    # number. A bare label names no detector, and the collation key (sample, q, T) can span
    # detectors, so a reader could not tell which arena the member came from.
    members = [("tavi", 473, "det"), ("tavi", 493, "det"), ("tavi", 494, "det")]
    assert master_delivery._member_labels(members) == ["tavi:473:det", "tavi:493:det",
                                                       "tavi:494:det"]


def test_member_labels_keep_a_repeated_scan_number_distinct():
    members = [("tavi", 473, "a"), ("tavi", 473, "b"), ("tavi", 500, "a")]
    assert master_delivery._member_labels(members) == ["tavi:473:a", "tavi:473:b", "tavi:500:a"]


def test_carry_backfills_members_without_refit(monkeypatch):
    # An older ledger master (fit before the members field existed) is backfilled on the carry path:
    # the content-hash match proves the member set is identical, so labels are recomputed exactly —
    # in the CURRENT fully-qualified exp:scan:det form, since backfill runs today's _member_labels —
    # no refit (the fit stub must NOT be called again).
    calls = _stub(monkeypatch)
    ms = _MS([("tavi", 473, "det"), ("tavi", 493, "det"), ("tavi", 494, "det")])
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), ms)])
    s = SessionState()
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 1
    mid = next(k for k in s.scans if k.startswith("master:"))
    assert len(calls) == 1

    # Simulate an old ledger: strip members from the detector dict + a provenance lacking it.
    del s.scans[mid].detectors["det"]["members"]
    s.scans[mid].provenance = {"pooled": True, "n_members": 3,
                               "predictive_contract": PREDICTIVE_CONTRACT}

    # Same member set → carry path; the stub is not called again but members are restored.
    assert master_delivery._fit_masters(col, [], None, None, None, s, M=10, seed_base=6000) == 0
    assert len(calls) == 1                                     # no refit
    restored = s.scans[mid].detectors["det"]["members"]
    assert restored == ["tavi:473:det", "tavi:493:det", "tavi:494:det"]
    assert len(restored) == s.scans[mid].detectors["det"]["n_members"]
    assert s.scans[mid].provenance["members"] == [["tavi", 473, "det"], ["tavi", 493, "det"],
                                                  ["tavi", 494, "det"]]


def _master_entry(members, n_members=None):
    oc = outcome_mod.FitOutcome(
        detector_id="merged", verdict="clear_phonon", disposition="resolved", confidence=0.9,
        omega0=0.95, omega0_err=0.02, interval=(0.9, 1.0), modality="single", gamma=0.3,
        gamma_provisional=False, best_label="elastic+stokes+bg", cost_per_dof=1.0,
        q=[2.1, 0.0, 0.0], T=200.0, sample="tavi", prior_used=None, calib={},
        pooled=True, n_members=(n_members if n_members is not None else len(members)),
        members=members)
    e = ScanEntry(scan_id="master:tavi_42_200.0", scan_number=master_delivery._MASTER_SCAN_BASE + 1,
                  scan_type="measurement", content_hash="c", status="fitted")
    e.detectors["merged"] = oc.to_dict()
    return e


def _unpooled_entry():
    oc = outcome_mod.FitOutcome(
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
    assert rows[master_delivery._MASTER_SCAN_BASE + 1]["members"] == ["473", "493", "494"]
    assert rows[5]["members"] is None                          # unpooled → empty cell → None


def test_write_results_csv_raises_on_member_count_mismatch(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    s = SessionState()
    s.put(_master_entry(["473", "493"], n_members=3))          # 2 members, claims 3
    with pytest.raises(ValueError, match="member provenance corrupt"):
        driver._write_results_csv(ws, s)
