"""tools/calibrate_deep_pass.py: the one-pass calibration harness, on synthetic rows only.

No campaign, no data loading: the sampler is real only for a 2-D analytic Gaussian (the worker's
adapter against the real `deep_pass.run_nested`); the orchestration runs on fabricated rows through a
patched `run_jobs`; the spawned-process timeout and wall cap run the real `run_jobs` with a sleeping
worker. The checks run in a subprocess (the sleeping worker must be a `__main__` function that spawn can
re-import, and the tool is loaded by path), so nothing leaks into the pytest process. The tool's own
`--self-check` arithmetic runs first.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import math
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
TRUE = {"c0": 0.0, "c1": 10.0, "c2": 6.0}        # reference lnZ per core model (count 0, 1, 2)
FAST = {"c0": 0.3, "c1": 10.4, "c2": 5.8}        # the Laplace values the entries carry
NDIM = {"c0": 3, "c1": 4, "c2": 4}


def test_calibration_harness(tmp_path):
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--inner", str(tmp_path)],
                            cwd=REPO, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True)
class FakeBundle:
    params: tuple = ()

    def spec(self):
        return [{"test_only": True}]


@dataclass(frozen=True)
class FakeProblem:
    """A 2-D Gaussian on the unit square; Z = (0.1 sqrt(2 pi))^2."""
    bundle: FakeBundle = field(default_factory=FakeBundle)

    def __call__(self):
        return (lambda theta: -0.5 * sum(((t - 0.5) / 0.1) ** 2 for t in theta)), (lambda u: u), 2


class CapturedPipe:
    def send(self, value):
        self.row = value

    def close(self):
        pass


def slow_worker(job, connection):          # a __main__ function: spawn re-imports this file
    time.sleep(3)
    connection.send({"never": True})
    connection.close()


def inner(artifacts: Path):
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("calibrate_deep_pass", REPO / "tools" / "calibrate_deep_pass.py")
    h = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = h
    spec.loader.exec_module(h)
    from isar.analyze import deep_pass
    print(f"harness: {h.__file__}\ndeep_pass: {deep_pass.__file__}")
    assert Path(h.__file__).resolve().is_relative_to(REPO)
    assert Path(deep_pass.__file__).resolve().is_relative_to(REPO)        # the checkout under test, not a sibling
    h.self_check()

    def entry(label, count, ndim=None):
        return deep_pass.ModelEntry(label=label, count=count, ndim=ndim or NDIM[label],
                                    fastpass_ln_z=FAST.get(label, -50.0), fastpass_lnl_opt=-1.0,
                                    problem=FakeProblem(), log_model_prior=0.0)

    entries = [entry("c0", 0), entry("c1", 1), entry("c2", 2)]
    assert [e.label for e in h.core_set(entries + [entry("c3", 3, 5)], 1, 6)] == ["c0", "c1", "c2", "c3"]
    assert h.spectrum_summary({"state": "close_call", "preferred_count": 2, "models": [
        {"ln_z": -1.0, "ln_model_prior": 0.0}, {"ln_z": -9.0, "ln_model_prior": 0.0},
        {"ln_z": -30.0, "ln_model_prior": 0.0}, {"ln_z": None, "ln_model_prior": 0.0}]}) == ("close_call", 2, 2)

    # --- the worker adapter against the REAL engine, and its refusals
    job = dict(model=entry("c1", 1, 2), stage="worker_check", settings=h._settings(40, 1, dlogz=0.5),
               seed=11, engine="dynesty")
    pipe = CapturedPipe()
    h._worker(job, pipe)
    assert h.clean(pipe.row), pipe.row
    assert pipe.row["n_like_calls"] > 0
    assert abs(pipe.row["log_z"] - 2 * math.log(0.1 * math.sqrt(2 * math.pi))) < 1.5     # analytic lnZ = -2.77
    real_run = deep_pass.run_nested
    try:
        def fake(label, problem, *, settings, seed, reference_lnl=None, **changes):
            base = dict(label=label, seed=seed, settings=settings, ndim=2, log_z=1.0, log_z_err=.1,
                        information_h=3.0, n_like_calls=7, n_iter=5, lnl_max=0.0, wall_s=.1,
                        reference_lnl=reference_lnl, evidence_converged=True)
            base.update(changes)
            return deep_pass.RunRecord(**base)
        deep_pass.run_nested = lambda *a, **k: fake(*a, **k)
        h._worker(job, pipe)
        assert h.clean(pipe.row) and pipe.row["n_like_calls"] == 7
        deep_pass.run_nested = lambda label, *a, **k: fake("wrong", *a, **k)
        h._worker(job, pipe)
        assert not h.clean(pipe.row) and "identity does not match" in pipe.row["error"]
        deep_pass.run_nested = lambda *a, **k: fake(*a, **k, log_z=float("nan"), failure="run failed: Boom",
                                                    evidence_converged=False)
        h._worker(job, pipe)
        assert pipe.row["status"] == "failed" and "Boom" in pipe.row["error"] and not h.clean(pipe.row)
    finally:
        deep_pass.run_nested = real_run
    print("worker adapter: real engine, identity refusal, failure record: passed")

    # --- the orchestration on fabricated rows (patched run_jobs)
    seen, state = [], {}
    original_jobs, original_find = h.run_jobs, h.importlib.util.find_spec

    def make_row(j):
        r = h._base_row(j)
        lb, n, f = j["model"].label, j["settings"]["n_live"], j["settings"].get("step_factor", 1)
        z = TRUE[lb] + (j["seed"] % 3 - 1) * .01
        extra = {}
        if j["stage"] == "screen":
            z += state["bias"](lb, n, f)
            if state.get("deficit") and (lb, n, f) == (state["deficit"][0], 400, 1) and j["seed"] % 3 == 0:
                extra = dict(lnl_max=-1.0 - state["deficit"][1],
                             suspect=["best lnL %.1f nats below the fast-pass optimum" % state["deficit"][1]])
        r.update(dict(dict(log_z=z, log_z_err=.1, information_h=40., n_like_calls=123, n_iter=50,
                           lnl_max=-1.5, wall_s=n * f / 1000.0, evidence_converged=True, status="ok"), **extra))
        return r

    def simulate(jobs, *, workers, timeout_s, on_result, deadline=None, skipped=None):
        seen.append(deadline)
        pending, done = deque(jobs), []
        while pending:
            j = pending.popleft()
            if state["cap"] and j["stage"] == "reference":
                r = h._base_row(j)
                r.update(error=h.WALL_CAP, wall_s=1.0)
                skipped.extend(pending)
                pending.clear()
            else:
                r = make_row(j)
            done.append(r)
            pending.extend(on_result(r) or ())
        return done

    cases = {   # name -> (screen bias by (label, n_live, step factor), ultranest installed, cap, gap deficit)
        "clear": (lambda lb, n, f: 2.5 if (lb == "c0" and n == 128) else 0.0, True, False, None),
        "miss": (lambda lb, n, f: {128: 3.0, 400: 2.4 if f == 1 else 2.2}[n] if lb == "c0" else 0.0,
                 False, False, ("c2", 9.0)),
        "cap": (lambda lb, n, f: 0.0, False, True, None)}
    h.run_jobs = simulate
    try:
        for name, (bias, un, cap, deficit) in cases.items():
            state.update(bias=bias, cap=cap, deficit=deficit)
            h.importlib.util.find_spec = lambda mod, un=un: object() if (mod == "ultranest" and un) else (
                None if mod == "ultranest" else original_find(mod))
            with contextlib.redirect_stdout(io.StringIO()):
                r = h.calibrate(entries, preferred_count=1, output=artifacts / name,
                                metadata={"spectrum": "synthetic", "SIMULATED_ROWS_NOT_MEASURED": True})
            assert seen[-1] is not None                                  # the wall cap reaches run_jobs
            seeds = [row["seed"] for row in r["runs"]]
            assert len(seeds) == len(set(seeds)), name
            with (artifacts / name / "calibration.json").open(encoding="utf-8", newline="") as stream:
                loaded = json.load(stream, parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))
            assert loaded["status"] == r["status"]
            md = (artifacts / name / "calibration.md").read_text(encoding="utf-8")
            assert len(md) > 500
            a = r["analysis"]
            assert "analysis_error" not in r, r.get("analysis_error")
            if name == "clear":
                assert r["status"] == "complete" and len(r["runs"]) == 36 + 9 + 6
                ch = a["chosen"]["accepted"]
                assert (ch["n_live"], ch["step_factor"]) == (400, 1) and a["chosen"]["within_limit"]
                assert all(row["settings"]["n_live"] == 1600 and row["settings"]["step_factor"] == 1
                           for row in r["runs"] if row["stage"] == "reference")
                assert r["ultranest_labels"] == ["c1", "c2"] and a["ultranest"] == "agrees"
                assert all(v["reached"] for v in a["aims"].values()), a["aims"]
                assert ch["calibrated_sigma"] < 0.05 and a["chosen"]["exploratory"]["n_live"] == 128
                assert a["chosen"]["exploratory"]["calibrated_sigma"] > 2.5 + .5 - .1     # the 128 bias + early stop
                assert r["installed"].startswith("provisional: one-pass calibration on synthetic")
                assert "2-nat limit reached" in r["installed"]
            elif name == "miss":
                assert r["status"] == "complete" and a["ultranest"] == "not run: ultranest not installed"
                ch = a["chosen"]
                assert (ch["accepted"]["n_live"], ch["accepted"]["step_factor"]) == (400, 4)
                assert not ch["within_limit"] and abs(a["aims"]["difference_max"]["value"] - 2.2) < .03
                assert not a["aims"]["difference_max"]["reached"] and not a["aims"]["difference_target"]["reached"]
                assert "2-nat limit missed" in r["installed"] and "2-nat limit missed: 2.2" in md
                gap = a["suspect_gap"]                                   # deficit 9 on 4 parameters: 9 - 0.5*4 + 2
                assert gap["n_exceeding"] >= 1 and abs(gap["proposed_base"] - 9.0) < 1e-9
                flagged = next(c for c in a["configurations"] if (c["n_live"], c["step_factor"]) == (400, 1))
                assert flagged["complete"]                               # the gap-flagged run stayed in the choice
            else:
                assert r["status"] == "cut_by_wall_cap" and len(r["not_run"]) == 8
                assert a["chosen"] is None and "Nothing chosen" in md and "Never started" in md
                assert [row["error"] for row in r["runs"] if row["stage"] == "reference"] == [h.WALL_CAP]
            print(f"one pass, {name}: passed ({len(r['runs'])} rows)")
    finally:
        h.run_jobs, h.importlib.util.find_spec = original_jobs, original_find
    try:
        h.calibrate(entries, preferred_count=1, output=artifacts / "clear")
    except FileExistsError:
        pass
    else:
        raise AssertionError("a previous calibration was overwritten")

    # --- the real run_jobs: per-run timeout, whole-pass wall cap, queued jobs listed as not started
    old_worker = h._worker
    h._worker = slow_worker
    try:
        stub = SimpleNamespace(label="m", count=0, ndim=1, fastpass_lnl_opt=None)
        slow = lambda: dict(model=stub, stage="timeout_check", settings=h._settings(128, 1), seed=1,  # noqa: E731
                            engine="dynesty")
        started = time.monotonic()
        rows = h.run_jobs([slow()], workers=1, timeout_s=.15, on_result=lambda row: None)
        assert "wall timeout" in rows[0]["error"] and not h.clean(rows[0]) and time.monotonic() - started < 5
        started, queued = time.monotonic(), []
        rows = h.run_jobs([slow(), slow()], workers=1, timeout_s=60, on_result=lambda row: None,
                          deadline=time.monotonic() + .3, skipped=queued)
        assert [r["error"] for r in rows] == [h.WALL_CAP] and len(queued) == 1
        assert time.monotonic() - started < 5
        print("spawned-process timeout and wall cap: passed")
    finally:
        h._worker = old_worker


if __name__ == "__main__":
    if "--inner" in sys.argv:
        inner(Path(sys.argv[sys.argv.index("--inner") + 1]))
    else:
        raise SystemExit("run through pytest")
