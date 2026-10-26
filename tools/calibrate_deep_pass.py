"""Deliberate calibration of the deep pass: ONE bounded pass on ONE problem spectrum.

A research tool run by hand (an agent or the operator), never by the product and never as a
routine test; tests/test_calibrate_deep_pass.py exercises it on synthetic rows only. It samples
nothing it was not told to and refits nothing: the models, their Laplace evidence and the
fast-pass optimum come from a saved fast-pass record through `deep_pass.models_from_record`.

The protocol is the operator's one-pass ruling of 2026-10-26 (a deliberate narrowing of the design's
D1 protocol; the report says which parts remain for a later calibration):

  * core set: per peak count 0..min(ceiling, preferred + 2), the fast pass's top model of that count
    (the report's count rows are therefore "top model per count", not variant sums);
  * screen: dynesty random walk, n_live {128, 400} x step factor {1, 4}, 3 independent seeds per core
    model, dlogz 0.05, no iteration cap (12 runs per core model);
  * reference: 3 seeds per core model at --reference-live (1600) live points, rwalk, the leading step
    factor (the one whose 400-point seed range plus 128->400 shift is smaller), dlogz 0.05;
  * UltraNest cross-check: the fast pass's preferred-count model and its strongest rival, 3 seeds at
    the reference live-point count, queued first; reported as agrees / disagrees by x nats / not run /
    not finished. Needs the optional `calibration` extra (pip install -e ".[calibration]");
  * choice: the cheapest screened configuration whose deciding-difference error stays within
    --difference-max (2 nats) of the reference; if none does, the smallest-error one, the miss
    reported. The 1-nat target is checked on the RMS. A missed aim never blocks anything;
  * every run is a spawned single-threaded process with its own kill timeout, the whole pass has a
    wall cap (--wall-cap-s, 7200), and calibration.json / calibration.md are rewritten after every run.
    A run still in flight at the cap is killed and recorded "wall cap"; nothing starts after it. There
    are no follow-up runs.

Command for the le Tacon pass (run in the calibration environment, from the job worktree; the
workspace is the one `isar run ... --detectors a05,a06,a20 --no-render --no-scene` made, see
--list-spectra for the keys and the fast-pass states):

    micromamba run -n isar-calibration python tools\\calibrate_deep_pass.py --list-spectra ^
        --workspace %LOCALAPPDATA%\\Temp\\claude\\j6420291926ab-s2u2\\ws_302
    micromamba run -n isar-calibration python tools\\calibrate_deep_pass.py ^
        --workspace %LOCALAPPDATA%\\Temp\\claude\\j6420291926ab-s2u2\\ws_302 ^
        --spectrum <EXPERIMENT>:<SCAN>:<aNN of the chosen problem spectrum> ^
        --output <main checkout>\\out\\j6420291926ab\\deep-calibration\\<spectrum>

Fallback problem spectrum, when models_from_record refuses the rebuilt workspace context (the fast
pass runs here as the record's producer, about a minute, and seeds nothing):

    micromamba run -n isar-calibration python tools\\calibrate_deep_pass.py ^
        --bench-case close_doublet --bench-level high --bench-draw 0 ^
        --output <main checkout>\\out\\j6420291926ab\\deep-calibration\\close_doublet_high

Defaults fit the dedicated machine: --workers 16 --run-timeout-s 3600 --wall-cap-s 7200
--reference-live 1600 --reference-tolerance 0.5 --difference-max 2.0 --difference-target 1.0.
The output folder must be new. Exit 0: the pass ran and wrote its report (aims reached or missed);
1: a failure (the report still holds whatever finished). `--self-check` runs the arithmetic checks.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import importlib.util
import json
import math
import multiprocessing as mp
import os
import platform
import statistics
import subprocess
import sys
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

# Spawned runs import numpy only after these limits have been inherited.
for _variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                  "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS"):
    os.environ[_variable] = "1"

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:        # the checkout this tool sits in, also for the spawned workers
    sys.path.insert(0, str(REPO))
BENCH_SEED = 20261023            # count_bench's default --seed
SEEDS = 3                        # per model and configuration (ruling)
SCREEN_LIVE = (128, 400)
SCREEN_FACTORS = (1, 4)
DLOGZ = 0.05
EXPLORATORY_DLOGZ = 0.5
EXPLORATORY_EXTRA = 0.5          # nats added for the early stop: the evidence left out is at most dlogz
STEP_UNSETTLED = 0.5             # |400-point median shift between factors 1 and 4| reported above this
ULTRANEST_AGREE = 1.0            # nats, median offset and range for "agrees"
GAP_MARGIN = 2.0                 # nats added to the observed maximum excess for a proposed gap base
WALL_CAP = "wall cap"
NOT_FINISHED = "not finished within the wall cap"


# --------------------------------------------------------------------------- small numerics
def _last_line(text):
    lines = str(text).strip().splitlines()
    return lines[-1] if lines else ""


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def clean(row):
    """A run whose evidence can be used. The sampler's lnL-gap check is a provisional constant this
    pass measures, so a run flagged only by it stays clean here and is counted in the gap section."""
    return (row.get("status") == "ok" and row.get("evidence_converged") is True
            and finite(row.get("log_z")) and finite(row.get("lnl_max"))
            and (row.get("engine") == "ultranest" or finite(row.get("information_h"))))


def summary(rows):
    good = [r for r in rows if clean(r)]
    z = [r["log_z"] for r in good]
    return dict(n=len(rows), n_clean=len(good),
                median_log_z=statistics.median(z) if z else None,
                spread=max(z) - min(z) if z else None,
                mean_h=statistics.mean(r["information_h"] for r in good) if good else None,
                real_calls=sum(r.get("n_like_calls") or 0 for r in rows),
                total_wall_s=sum(r["wall_s"] for r in rows),
                mean_wall_s=statistics.mean(r["wall_s"] for r in rows) if rows else None)


def rms(values):
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else None


def _logsumexp(values):
    top = max(values)
    return top + math.log(sum(math.exp(x - top) for x in values))


def count_probabilities(models, values, radius=0.0):
    """Count probabilities from lnZ + log_model_prior (each count's prior shared over its models), and
    extremal bands for independent +/- radius errors. Never renormalised over a missing model; the
    bands are sensitivity envelopes, not credible intervals."""
    if any(not finite(values.get(m.label)) for m in models):
        return {"status": "incomplete", "counts": {}, "decision": None}
    groups = defaultdict(list)
    for m in models:
        groups[m.count].append(values[m.label] + m.log_model_prior)
    weights = {k: _logsumexp(v) for k, v in groups.items()}
    total = _logsumexp(list(weights.values()))
    out = {}
    for k, z in weights.items():
        others = [v for c, v in weights.items() if c != k]
        if others:
            other = _logsumexp(others)
            low = math.exp(z - radius - _logsumexp([z - radius, other + radius]))
            high = math.exp(z + radius - _logsumexp([z + radius, other - radius]))
        else:
            low = high = 1.0
        out[k] = {"p": math.exp(z - total), "low": low, "high": high}
    best = max(out, key=lambda k: out[k]["p"])
    if out[best]["low"] >= 0.80:
        decision = f"clear:{best}"
    elif all(v["high"] < 0.80 for v in out.values()):
        decision = "close_call"
    else:
        decision = "numerically_unresolved"
    return {"status": "complete", "counts": out, "best_count": best,
            "decision": decision, "radius_nats": radius}


# --------------------------------------------------------------------------- the core set
def _fast_weight(e):
    return e.fastpass_ln_z + e.log_model_prior if finite(e.fastpass_ln_z) else -math.inf


def core_set(entries, preferred_count, ceiling):
    """Per count 0..min(ceiling, preferred + 2) the entry with the highest fast-pass weight
    (fastpass_ln_z + log_model_prior), or, for a count with no priced entry, the highest fitted
    optimum (its twin parent). Costly: the pass scales with the set, so it is one model per count."""
    out = []
    for k in range(min(int(ceiling), int(preferred_count) + 2) + 1):
        same = [e for e in entries if e.count == k]
        priced = [e for e in same if finite(e.fastpass_ln_z)]
        fitted = [e for e in same if finite(e.fastpass_lnl_opt)]
        if priced:
            out.append(max(priced, key=_fast_weight))
        elif fitted:
            out.append(max(fitted, key=lambda e: e.fastpass_lnl_opt))
    return out


def fast_deciding_labels(core, preferred_count):
    """The fast pass's preferred-count model and its strongest rival: the UltraNest models, chosen
    from the record because the cross-check is queued before any dynesty reference exists."""
    top = next((e for e in core if e.count == preferred_count), None) or max(core, key=_fast_weight)
    rivals = sorted((e for e in core if e is not top),
                    key=lambda e: (-_fast_weight(e),
                                   -(e.fastpass_lnl_opt if finite(e.fastpass_lnl_opt) else -math.inf)))
    return [top.label] + [e.label for e in rivals[:1]]


# --------------------------------------------------------------------------- one run, in its own process
def _deep_pass():
    from isar.analyze import deep_pass
    return deep_pass


def _settings(n_live, step_factor, dlogz=DLOGZ):
    return dict(n_live=n_live, sample="rwalk", step_factor=step_factor, dlogz=dlogz,
                max_iter=None, bound="multi")


def _base_row(job):
    m = job["model"]
    return dict(label=m.label, count=m.count, ndim=m.ndim, stage=job["stage"],
                seed=job["seed"], settings=job["settings"], engine=job["engine"],
                reference_lnl=m.fastpass_lnl_opt, log_z=None, log_z_err=None,
                information_h=None, n_like_calls=None, n_iter=None, lnl_max=None,
                wall_s=0.0, suspect=[], evidence_converged=False, status="failed", error=None)


def _ultranest(job):
    import numpy as np
    import ultranest

    m = job["model"]
    loglike, transform, ndim = m.problem()
    names = [p.name for p in m.problem.bundle.params]
    calls = 0

    def counted(theta):
        nonlocal calls
        calls += 1
        return loglike(theta)

    np.random.seed(job["seed"])
    start = time.perf_counter()
    sampler = ultranest.ReactiveNestedSampler(names, counted, transform=transform,
                                              log_dir=None, vectorized=False)
    result = sampler.run(min_num_live_points=job["settings"]["n_live"], dlogz=DLOGZ,
                         show_status=False, viz_callback=False)
    suspect = []
    if not result.get("insertion_order_MWW_test", {}).get("converged", False):
        suspect.append("UltraNest insertion-order check missing or failed")
    return dict(log_z=float(result["logz"]), log_z_err=float(result["logzerr"]),
                information_h=float(result["H"]) if "H" in result else float("nan"),
                n_like_calls=calls, n_iter=int(result["niter"]),
                lnl_max=float(result["maximum_likelihood"]["logl"]),
                wall_s=time.perf_counter() - start, suspect=suspect,
                evidence_converged=not suspect, status="ok")


def _worker(job, connection):
    row = _base_row(job)
    started = time.perf_counter()
    try:
        if job["engine"] == "ultranest":
            row.update(_ultranest(job))
        else:
            deep_pass = _deep_pass()
            m = job["model"]
            settings = deep_pass.SamplerSettings(**job["settings"])
            rec = deep_pass.run_nested(m.label, m.problem, settings=settings, seed=job["seed"],
                                       reference_lnl=m.fastpass_lnl_opt)
            if rec.label != m.label or rec.ndim != m.ndim or rec.seed != job["seed"]:
                raise ValueError("RunRecord identity does not match calibration job")
            if dataclasses.asdict(rec.settings) != dataclasses.asdict(settings):
                raise ValueError("engine changed calibration settings")
            for key in ("log_z", "log_z_err", "information_h", "n_like_calls", "n_iter",
                        "lnl_max", "wall_s", "evidence_converged"):
                row[key] = getattr(rec, key)
            row["suspect"] = list(rec.suspect)
            row["settings"] = dataclasses.asdict(rec.settings)
            if rec.failure is None:
                row["status"] = "ok"
            else:
                row["error"] = rec.failure
        row["process_wall_s"] = time.perf_counter() - started
    except Exception:
        row.update(status="failed", error=traceback.format_exc(),
                   wall_s=time.perf_counter() - started, evidence_converged=False)
    try:
        connection.send(row)
    finally:
        connection.close()


def run_jobs(jobs, *, workers, timeout_s, on_result, deadline=None, skipped=None):
    """One spawned process per run: independent RNG, no nested pool, killable timeout.

    ``on_result(row)`` may return more jobs to queue (the reference waits for the screen). After
    ``deadline`` (a time.monotonic() value) nothing starts, queued jobs go to ``skipped``, and a run
    still in flight is killed and recorded with error "wall cap"."""
    ctx = mp.get_context("spawn")
    pending, active, completed = deque(jobs), [], []
    try:
        while pending or active:
            capped = deadline is not None and time.monotonic() >= deadline
            if capped and pending:
                if skipped is not None:
                    skipped.extend(pending)
                pending.clear()
            while pending and len(active) < workers:
                job = pending.popleft()
                reader, writer = ctx.Pipe(duplex=False)
                proc = ctx.Process(target=_worker, args=(job, writer))
                proc.start()
                writer.close()
                active.append((proc, reader, job, time.monotonic()))
            for item in active[:]:
                proc, reader, job, start = item
                expired = time.monotonic() - start >= timeout_s
                row = None
                if reader.poll():
                    try:
                        row = reader.recv()
                    except EOFError:
                        row = _base_row(job)
                        row["error"] = "worker closed its result pipe without a record"
                elif capped or expired or not proc.is_alive():
                    row = _base_row(job)
                    row.update(wall_s=time.monotonic() - start,
                               error=(WALL_CAP if capped else f"wall timeout after {timeout_s:g}s"
                                      if expired else f"worker exited {proc.exitcode} without a record"))
                if row is not None:
                    proc.join(timeout=0.2)
                    if proc.is_alive():
                        proc.terminate()
                        proc.join(timeout=2)
                        if proc.is_alive():
                            proc.kill()
                            proc.join(timeout=2)
                    reader.close()
                    active.remove(item)
                    completed.append(row)
                    pending.extend(on_result(row) or ())
            if active:
                time.sleep(0.05)
    finally:
        for proc, reader, _job, _start in active:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=2)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
            reader.close()
    return completed


# --------------------------------------------------------------------------- analysis (pure, on run rows)
def _group(runs, stage, label, n_live, factor):
    return sorted((r for r in runs if r["stage"] == stage and r["engine"] == "dynesty"
                   and r["label"] == label and r["settings"]["n_live"] == n_live
                   and r["settings"]["step_factor"] == factor), key=lambda r: r["seed"])


def leading_factor(runs, labels):
    """(factor, scores, unsettled): the step factor with the smaller sum over models of (the 400-point
    3-seed range + the |128 -> 400| median shift); ties to the smaller factor. ``None`` when no factor
    has every group complete. ``unsettled``: some model's 400-point median moves more than 0.5 nat
    between the factors (diagnostic only, never followed)."""
    scores, medians = {}, {}
    for f in SCREEN_FACTORS:
        total = 0.0
        for lb in labels:
            lo, hi = (summary(_group(runs, "screen", lb, n, f)) for n in SCREEN_LIVE)
            if lo["n_clean"] < SEEDS or hi["n_clean"] < SEEDS:
                total = math.inf
                break
            total += hi["spread"] + abs(lo["median_log_z"] - hi["median_log_z"])
            medians[(f, lb)] = hi["median_log_z"]
        scores[f] = total
    best = min(SCREEN_FACTORS, key=lambda f: (scores[f], f))
    unsettled = any(abs(medians[(1, lb)] - medians[(4, lb)]) > STEP_UNSETTLED
                    for lb in labels if (1, lb) in medians and (4, lb) in medians)
    return (best if math.isfinite(scores[best]) else None), scores, unsettled


def evaluate_config(runs, labels, ref, top, n_live, factor):
    """Deciding-difference error of one screened configuration: for the pairs (top, other) with a
    reference, max over seeds of |(z_a - z_b) - (ref_a - ref_b)|. ``complete``: every core model has
    all its runs clean here and a finite reference, and at least one pair exists."""
    groups = {lb: _group(runs, "screen", lb, n_live, factor) for lb in labels}
    all_clean = all(len(g) == SEEDS and all(clean(r) for r in g) for g in groups.values())
    all_ref = all(finite(ref.get(lb)) for lb in labels)
    pairs = [(top, lb) for lb in labels
             if lb != top and finite(ref.get(top)) and finite(ref.get(lb))]
    errors = {}
    if all_clean:
        for a, b in pairs:
            errors[f"{a} | {b}"] = [abs((ra["log_z"] - rb["log_z"]) - (ref[a] - ref[b]))
                                    for ra, rb in zip(groups[a], groups[b])]
    flat = [e for v in errors.values() for e in v]
    walls = [r["wall_s"] for g in groups.values() for r in g]
    return dict(n_live=n_live, step_factor=factor, complete=bool(all_clean and all_ref and flat),
                largest=max(flat) if flat else None, rms=rms(flat), pair_errors=errors,
                mean_wall_s=statistics.mean(walls) if walls else None)


def choose(evals, difference_max):
    """(key, within_limit): the cheapest complete configuration within the limit; else the complete
    one with the smallest largest error (ties by cost), flagged as a miss; else (None, False)."""
    ok = [(e["mean_wall_s"], k) for k, e in evals.items()
          if e["complete"] and e["largest"] <= difference_max]
    if ok:
        return min(ok)[1], True
    cand = [(e["largest"], e["mean_wall_s"], k) for k, e in evals.items() if e["complete"]]
    return (min(cand)[2], False) if cand else (None, False)


def sigma_of(runs, labels, ref, n_live, factor):
    """Max over core models of the RMS of (z - reference) over the screen runs of one configuration
    (spread and bias together), and the largest single |offset|. (None, None) with no usable run."""
    per_model, offsets = [], []
    for lb in labels:
        if not finite(ref.get(lb)):
            continue
        d = [r["log_z"] - ref[lb] for r in _group(runs, "screen", lb, n_live, factor) if clean(r)]
        if d:
            per_model.append(rms(d))
            offsets.extend(abs(v) for v in d)
    return (max(per_model), max(offsets)) if per_model else (None, None)


def gap_table(runs, gap_base, gap_per_dim):
    """Suspect-gap evidence over every completed dynesty run: deficit = fast-pass optimum minus the
    run's best lnL, by number of parameters; and the base that would cover the observed excess."""
    by_ndim, worst = defaultdict(list), None
    exceed = 0
    for r in runs:
        if r["engine"] != "dynesty" or r["status"] != "ok" or not finite(r.get("lnl_max")) \
                or not finite(r.get("reference_lnl")):
            continue
        deficit = r["reference_lnl"] - r["lnl_max"]
        by_ndim[r["ndim"]].append(deficit)
        excess = deficit - gap_per_dim * r["ndim"]
        worst = excess if worst is None else max(worst, excess)
        exceed += deficit > gap_base + gap_per_dim * r["ndim"]
    rows = [dict(ndim=d, n=len(v), max_deficit=max(v), mean_deficit=statistics.mean(v),
                 provisional_gap=gap_base + gap_per_dim * d) for d, v in sorted(by_ndim.items())]
    return dict(rows=rows, n_exceeding=exceed, max_excess=worst,
                proposed_base=(worst + GAP_MARGIN) if exceed else None)


def laplace_comparison(entries, ref, anchor_count):
    """Fast-pass Laplace lnZ against the reference, per model and per count (W = lnZ + log prior of
    the top core model of the count, differences taken to the reference's best count)."""
    per_model = [dict(label=e.label, count=e.count, fast_log_z=e.fastpass_ln_z,
                      reference_log_z=ref.get(e.label),
                      offset=(e.fastpass_ln_z - ref[e.label]
                              if finite(e.fastpass_ln_z) and finite(ref.get(e.label)) else None),
                      note="" if finite(e.fastpass_ln_z) else "no Laplace (twin parent)")
                 for e in entries]
    w_fast = {e.count: e.fastpass_ln_z + e.log_model_prior for e in entries if finite(e.fastpass_ln_z)}
    w_ref = {e.count: ref[e.label] + e.log_model_prior for e in entries if finite(ref.get(e.label))}
    per_count = []
    for k in sorted({e.count for e in entries}):
        df = w_fast[k] - w_fast[anchor_count] if k in w_fast and anchor_count in w_fast else None
        dr = w_ref[k] - w_ref[anchor_count] if k in w_ref and anchor_count in w_ref else None
        per_count.append(dict(count=k, fast_delta_w=df, reference_delta_w=dr,
                              difference=(df - dr) if df is not None and dr is not None else None))
    diffs = [abs(c["difference"]) for c in per_count if c["difference"] is not None and c["count"] != anchor_count]
    return dict(per_model=per_model, per_count=per_count, anchor_count=anchor_count,
                largest_difference=max(diffs) if diffs else None)


def ultranest_outcome(rows, labels, ref, installed):
    """One of: agrees / disagrees by x nats / not run / not finished within the wall cap / failed."""
    if not installed:
        return "not run: ultranest not installed"
    if not labels:
        return "not run: no deciding models"
    by = {lb: [r for r in rows if r["label"] == lb] for lb in labels}
    done = {lb: [r for r in v if clean(r)] for lb, v in by.items()}      # a failed insertion-order check is not clean
    if any(len(done[lb]) < SEEDS for lb in labels):
        failed = [r for v in by.values() for r in v if r["status"] != "ok"
                  and r.get("error") not in (None, WALL_CAP) and "wall timeout" not in r["error"]]
        if failed and all(len(by[lb]) >= SEEDS for lb in labels):
            return "failed: " + _last_line(failed[0]["error"])
        return NOT_FINISHED
    if any(not finite(ref.get(lb)) for lb in labels):
        return NOT_FINISHED
    offset = max(abs(statistics.median(r["log_z"] for r in done[lb]) - ref[lb]) for lb in labels)
    spread = max(max(r["log_z"] for r in done[lb]) - min(r["log_z"] for r in done[lb]) for lb in labels)
    if offset <= ULTRANEST_AGREE and spread <= ULTRANEST_AGREE:
        return "agrees"
    return f"disagrees by {max(offset, spread):.2f} nats (median offset {offset:.2f}, range {spread:.2f})"


def analyse(entries, preferred_count, runs, *, reference_live, reference_tolerance, difference_max,
            difference_target, gap_base, gap_per_dim, ultranest_installed, ultranest_labels):
    labels = [e.label for e in entries]
    out = {}
    factor, scores, unsettled = leading_factor(runs, labels)
    out["leading_step_factor"] = dict(factor=factor, scores={str(k): v for k, v in scores.items()},
                                      step_length_not_settled=unsettled)
    refs = {}
    for lb in labels:
        s = summary([r for r in runs if r["stage"] == "reference" and r["label"] == lb])
        refs[lb] = dict(s, settled=bool(s["n_clean"] == SEEDS and s["spread"] <= reference_tolerance))
    ref = {lb: refs[lb]["median_log_z"] for lb in labels}
    out["references"] = refs
    settled = all(refs[lb]["settled"] for lb in labels)
    max_range = max((refs[lb]["spread"] for lb in labels if refs[lb]["spread"] is not None), default=None)
    p_ref = count_probabilities(entries, ref, reference_tolerance)
    anchor = p_ref["best_count"] if p_ref["status"] == "complete" else preferred_count
    out["preferred_count_source"] = "reference" if p_ref["status"] == "complete" else "fast pass (reference incomplete)"
    out["preferred_count"] = anchor
    top = next((e.label for e in entries if e.count == anchor), labels[0])
    out["deciding_top_model"] = top
    evals = {(n, f): evaluate_config(runs, labels, ref, top, n, f)
             for n in SCREEN_LIVE for f in SCREEN_FACTORS}
    out["configurations"] = [dict(e, chosen=False) for e in evals.values()]
    key, within = choose(evals, difference_max)
    out["chosen"] = None
    probabilities = {"reference": p_ref, "laplace": count_probabilities(
        entries, {e.label: e.fastpass_ln_z for e in entries}, 0.0)}
    largest = rms_value = None
    if key is not None:
        n, f = key
        for c in out["configurations"]:
            c["chosen"] = (c["n_live"], c["step_factor"]) == key
        largest, rms_value = evals[key]["largest"], evals[key]["rms"]
        sigma, offset = sigma_of(runs, labels, ref, n, f)
        sig_x, off_x = sigma_of(runs, labels, ref, SCREEN_LIVE[0], f)
        out["chosen"] = dict(
            accepted=dict(sample="rwalk", n_live=n, step_factor=f, dlogz=DLOGZ, max_iter=None,
                          bound="multi", calibrated_sigma=sigma, max_abs_offset=offset,
                          mean_wall_s=evals[key]["mean_wall_s"]),
            exploratory=dict(sample="rwalk", n_live=SCREEN_LIVE[0], step_factor=f,
                             dlogz=EXPLORATORY_DLOGZ, max_iter=None, bound="multi",
                             calibrated_sigma=None if sig_x is None else sig_x + EXPLORATORY_EXTRA,
                             max_abs_offset=off_x),
            within_limit=within,
            per_model=[dict(label=lb, reference=ref[lb],
                            runs=[r["log_z"] for r in _group(runs, "screen", lb, n, f)],
                            max_abs_offset=max((abs(r["log_z"] - ref[lb])
                                                for r in _group(runs, "screen", lb, n, f)
                                                if clean(r) and finite(ref[lb])), default=None))
                       for lb in labels])
        med = {lb: summary(_group(runs, "screen", lb, n, f))["median_log_z"] for lb in labels}
        probabilities["accepted"] = count_probabilities(entries, med, sigma or 0.0)
    out["probabilities"] = probabilities
    out["laplace"] = laplace_comparison(entries, ref, anchor)
    out["suspect_gap"] = gap_table(runs, gap_base, gap_per_dim)
    ultra = ultranest_outcome([r for r in runs if r["engine"] == "ultranest"],
                              ultranest_labels, ref, ultranest_installed)
    out["ultranest"] = ultra
    limit = out["laplace"]["largest_difference"]
    out["aims"] = {
        "difference_max": dict(limit=difference_max, value=largest,
                               reached=bool(largest is not None and largest <= difference_max)),
        "difference_target": dict(limit=difference_target, value=rms_value,
                                  reached=bool(rms_value is not None and rms_value <= difference_target)),
        "reference_settled": dict(limit=reference_tolerance, value=max_range, reached=bool(settled)),
        "ultranest": dict(limit="agrees", value=ultra, reached=ultra == "agrees"),
        "laplace_within_limit": dict(limit=difference_max, value=limit,
                                     reached=bool(limit is not None and limit <= difference_max)),
        "laplace_within_target": dict(limit=difference_target, value=limit,
                                      reached=bool(limit is not None and limit <= difference_target))}
    for source, p in probabilities.items():
        if p["status"] == "complete" and p["decision"] != f"clear:{p['best_count']}":
            out.setdefault("close_call_notes", []).append(
                f"{source}: decision {p['decision']} (best count {p['best_count']}, "
                f"P {p['counts'][p['best_count']]['p']:.3f}, band "
                f"{p['counts'][p['best_count']]['low']:.3f}-{p['counts'][p['best_count']]['high']:.3f}): "
                "the calibrated uncertainty can change the winner or cross the 0.80 close-call threshold")
    out["reference_live"] = reference_live
    return out


# --------------------------------------------------------------------------- report
def _json_safe(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _cell(value):
    if value is None:
        return "not available"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers, rows):
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"] + [
        "| " + " | ".join(_cell(x) for x in row) + " |" for row in rows]


def _reached(aim):
    return "reached" if aim.get("reached") else "missed"


def render_markdown(report):
    a = report.get("analysis") or {}
    meta = report.get("metadata") or {}
    lines = ["# Deep-pass calibration (one bounded pass)", "",
             f"State: {report['status']}; elapsed {report.get('elapsed_wall_s', 0):.0f} s of the "
             f"{report['wall_cap_s']:g} s cap; {len(report['runs'])} runs finished, "
             f"{len(report.get('not_run', []))} never started.", "",
             f"Problem spectrum: {meta.get('source')} {meta.get('spectrum')}; fast-pass state "
             f"{meta.get('fast_pass_state')}, preferred count {meta.get('preferred_count')}, "
             f"revision {meta.get('revision')}. Timings are measured on this invocation "
             f"({report['machine']['workers']} single-threaded workers, other load not measured).", "",
             "What this pass is: the operator's one bounded pass (rwalk 2 x 2 screen on one spectrum, "
             "one reference level, one UltraNest cross-check). NOT run, left for a later calibration: "
             "the rslice comparison, the follow-the-change steps, the 800/1600/3200 ladder, 8-10-run "
             "uncertainty, and the timed cases. Three seeds per cell measure no tails, and agreement "
             "between repeated runs is a lower bound on the error, not proof of accuracy. Count rows "
             "are the top model per count of the core set, not variant sums.", ""]
    if report.get("error"):
        lines += ["## Failure", "", "```text", report["error"], "```", ""]
    if report.get("analysis_error"):
        lines += ["## Analysis error (raw runs below are intact)", "", "```text",
                  report["analysis_error"], "```", ""]
    aims = a.get("aims") or {}
    if aims:
        lines += ["## Aims", ""] + _table(["Aim", "Limit", "Value", "Result"], [
            [name, v.get("limit"), v.get("value"), _reached(v)] for name, v in aims.items()]) + [""]
        for note in a.get("close_call_notes", []):
            lines += [f"- {note}"]
        if a["leading_step_factor"]["step_length_not_settled"]:
            lines += ["- step length not settled at factor 4 (a core model's 400-point median moved "
                      "more than 0.5 nat between factors 1 and 4)"]
        lines += [""]
    ch = a.get("chosen")
    lines += ["## Chosen settings", ""]
    if ch:
        lines += _table(["", "n_live", "step factor", "dlogz", "calibrated sigma", "max |offset|"], [
            ["ACCEPTED", ch["accepted"]["n_live"], ch["accepted"]["step_factor"], DLOGZ,
             ch["accepted"]["calibrated_sigma"], ch["accepted"]["max_abs_offset"]],
            ["EXPLORATORY", ch["exploratory"]["n_live"], ch["exploratory"]["step_factor"],
             EXPLORATORY_DLOGZ, ch["exploratory"]["calibrated_sigma"], ch["exploratory"]["max_abs_offset"]]])
        lines += ["", "2-nat limit reached." if ch["within_limit"] else
                  f"2-nat limit missed: {aims['difference_max']['value']:.3g} nats."]
    else:
        lines += ["Nothing chosen: no configuration finished clean with a usable reference, so nothing "
                  "is to be installed."]
    lines += ["", f"Leading step factor: {a.get('leading_step_factor', {}).get('factor')}. "
              f"Deciding pairs: {a.get('deciding_top_model')} against each other core model; preferred "
              f"count {a.get('preferred_count')} ({a.get('preferred_count_source')}).", "",
              "## Screened configurations (deciding-difference error against the reference)", ""]
    lines += _table(["n_live", "Step factor", "All runs clean", "Largest pair error", "RMS", "Mean s/run",
                     "Chosen"], [[c["n_live"], c["step_factor"], c["complete"], c["largest"], c["rms"],
                                  c["mean_wall_s"], "yes" if c["chosen"] else ""]
                                 for c in a.get("configurations", [])])
    if ch:
        lines += ["", "Per-model screen offsets at the chosen configuration (diagnostic against 0.5 nat):", ""]
        lines += _table(["Model", "Reference lnZ", "Run lnZ", "Max |z - ref|"], [
            [p["label"], p["reference"], ", ".join(f"{z:.3f}" for z in p["runs"]), p["max_abs_offset"]]
            for p in ch["per_model"]])
    lines += ["", "## Reference", ""]
    lines += _table(["Model", "Clean/all", "Median lnZ", "Range", "Settled (<= tolerance)", "Mean H",
                     "Calls total", "Mean s"], [
        [lb, f"{s['n_clean']}/{s['n']}", s["median_log_z"], s["spread"], s["settled"], s["mean_h"],
         s["real_calls"], s["mean_wall_s"]] for lb, s in (a.get("references") or {}).items()])
    lines += ["", "## Screen groups", ""]
    groups = defaultdict(list)
    for r in report["runs"]:
        if r["engine"] == "dynesty":
            groups[(r["label"], r["stage"], r["settings"]["n_live"], r["settings"]["step_factor"])].append(r)
    lines += _table(["Model", "Stage", "n_live", "Step factor", "Clean/all", "Median lnZ", "Range",
                     "Mean H", "Calls total", "Mean s", "Total s"], [
        [lb, st, n, f, f"{s['n_clean']}/{s['n']}", s["median_log_z"], s["spread"], s["mean_h"],
         s["real_calls"], s["mean_wall_s"], s["total_wall_s"]]
        for (lb, st, n, f), rr in groups.items() for s in [summary(rr)]])
    gap = a.get("suspect_gap") or {}
    lines += ["", "## Suspect gap (fast-pass optimum minus the run's best lnL)", ""]
    lines += _table(["Parameters", "Runs", "Max deficit", "Mean deficit", "Provisional gap"], [
        [g["ndim"], g["n"], g["max_deficit"], g["mean_deficit"], g["provisional_gap"]]
        for g in gap.get("rows", [])])
    lines += ["", f"Runs beyond the provisional gap: {gap.get('n_exceeding')}; proposed base: "
              f"{gap.get('proposed_base') if gap.get('proposed_base') is not None else 'provisional constants stay'}."]
    lap = a.get("laplace") or {}
    lines += ["", "## Fast-pass Laplace comparison (sampler -cost/2 scale)", ""]
    lines += _table(["Model", "Count", "Laplace lnZ", "Reference lnZ", "Laplace minus reference", "Note"], [
        [m["label"], m["count"], m["fast_log_z"], m["reference_log_z"], m["offset"], m["note"]]
        for m in lap.get("per_model", [])])
    lines += ["", f"Per count, W = lnZ + log prior of the top core model, relative to count "
              f"{lap.get('anchor_count')}:", ""]
    lines += _table(["Count", "Fast delta W", "Reference delta W", "Difference"], [
        [c["count"], c["fast_delta_w"], c["reference_delta_w"], c["difference"]]
        for c in lap.get("per_count", [])])
    lines += ["", f"Largest count-level difference: {lap.get('largest_difference')} nats."]
    lines += ["", "## Count probabilities (top model per count)", ""]
    rows = []
    for source, result in (a.get("probabilities") or {}).items():
        if not result["counts"]:
            rows.append([source, "all", None, None, None, result["status"]])
        for count, p in result["counts"].items():
            rows.append([source, count, p["p"], p["low"], p["high"], result["decision"]])
    lines += _table(["Source", "Count", "P", "Lower", "Upper", "Decision"], rows)
    lines += ["", "## UltraNest", "", a.get("ultranest", "pending"), ""]
    if report.get("not_run"):
        lines += ["## Never started (wall cap)", ""] + _table(
            ["Model", "Stage", "Engine", "n_live", "Step factor"], [
                [j["label"], j["stage"], j["engine"], j["n_live"], j["step_factor"]]
                for j in report["not_run"]]) + [""]
    inst = report.get("installed")
    if inst:
        lines += ["## For the install", "", "```text", inst, "```", ""]
    lines += ["## Every run", ""] + _table(
        ["Model", "Stage", "Engine", "Seed", "n_live", "Step", "lnZ", "err", "H", "Calls", "Iterations",
         "lnL max", "Seconds", "Converged", "Suspect/error"], [
        [r["label"], r["stage"], r["engine"], r["seed"], r["settings"].get("n_live"),
         r["settings"].get("step_factor"), r["log_z"], r["log_z_err"], r["information_h"],
         r["n_like_calls"], r["n_iter"], r["lnl_max"], r["wall_s"], r["evidence_converged"],
         "; ".join(r["suspect"]) or r.get("error") or ""] for r in report["runs"]])
    return "\n".join(lines) + "\n"


def write_report(report, output):
    for name, text in (("calibration.json", json.dumps(_json_safe(report), indent=2,
                                                       allow_nan=False) + "\n"),
                       ("calibration.md", render_markdown(report))):
        target = output / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, target)


def installed_text(report, analysis):
    """The CALIBRATION_STATUS string the install commit would carry (P2); empty when nothing is chosen."""
    if not analysis.get("chosen"):
        return ""
    aims, meta = analysis["aims"], report["metadata"]
    return ("provisional: one-pass calibration on %s, %s; 2-nat limit %s; UltraNest %s; report %s"
            % (meta.get("spectrum"), datetime.now(timezone.utc).date().isoformat(),
               _reached(aims["difference_max"]), aims["ultranest"]["value"], report["output"]))


# --------------------------------------------------------------------------- the pass
def calibrate(entries, *, preferred_count, output, workers=16, run_timeout_s=3600, wall_cap_s=7200,
              reference_live=1600, reference_tolerance=0.5, difference_max=2.0, difference_target=1.0,
              seed=20261026, metadata=None):
    """Run the one pass on the core ``entries`` (deep_pass.ModelEntry); returns the report dict."""
    entries = list(entries)
    labels = [e.label for e in entries]
    if len(entries) < 2 or len(set(labels)) != len(labels):
        raise ValueError("need at least two core models with unique labels")
    if (workers < 1 or not finite(run_timeout_s) or run_timeout_s <= 0 or not finite(wall_cap_s)
            or wall_cap_s <= 0 or reference_live < 1 or not finite(reference_tolerance)):
        raise ValueError("positive workers, timeouts and wall cap required")
    for e in entries:
        if e.ndim < 1 or not finite(e.log_model_prior):
            raise ValueError(f"model {e.label!r} needs sampled parameters and a finite model prior")
        for v in (e.fastpass_ln_z, e.fastpass_lnl_opt):
            if v is not None and not finite(v):
                raise ValueError(f"model {e.label!r}: unavailable fast-pass numbers must be None, not NaN")
    deep_pass = _deep_pass()
    if "evidence_converged" not in {f.name for f in dataclasses.fields(deep_pass.RunRecord)}:
        raise ImportError("deep_pass.RunRecord must expose evidence_converged; usable is not a substitute")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("calibration.json", "calibration.md")):
        raise FileExistsError("choose a fresh output folder; the previous calibration is preserved")
    print(f"Calibration output: {output}", flush=True)
    versions = {}
    for package in ("numpy", "scipy", "dynesty", "ultranest"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not installed"
    ultranest_installed = importlib.util.find_spec("ultranest") is not None
    ultranest_labels = fast_deciding_labels(entries, preferred_count) if ultranest_installed else []
    start = time.monotonic()
    deadline = start + wall_cap_s
    report = dict(status="running", started_utc=datetime.now(timezone.utc).isoformat(),
                  output=str(output), machine=dict(platform=platform.platform(),
                                                   processor=platform.processor(),
                                                   logical_cpus=os.cpu_count(), workers=workers,
                                                   threads_per_run=1),
                  versions=versions, metadata=metadata or {}, run_timeout_s=run_timeout_s,
                  wall_cap_s=wall_cap_s, reference_live=reference_live, seed=seed,
                  core=[dict(label=e.label, count=e.count, ndim=e.ndim, fastpass_ln_z=e.fastpass_ln_z,
                             fastpass_lnl_opt=e.fastpass_lnl_opt, log_model_prior=e.log_model_prior)
                        for e in entries],
                  ultranest_labels=ultranest_labels, runs=[], not_run=[], analysis={})
    next_seed = seed
    by_label = {e.label: e for e in entries}

    def job(m, stage, settings, engine="dynesty"):
        nonlocal next_seed
        next_seed += 1
        if not 0 <= next_seed < 2**32:
            raise ValueError("seed sequence exceeds uint32 range")
        return dict(model=m, stage=stage, settings=settings, seed=next_seed, engine=engine)

    def costliest_first(jobs):
        return sorted(jobs, key=lambda j: -j["settings"]["n_live"] * j["settings"].get("step_factor", 1)
                      * (j["model"].ndim + 20))

    def refresh():
        try:
            report["analysis"] = analyse(
                entries, preferred_count, report["runs"], reference_live=reference_live,
                reference_tolerance=reference_tolerance, difference_max=difference_max,
                difference_target=difference_target, gap_base=deep_pass.SUSPECT_GAP_BASE,
                gap_per_dim=deep_pass.SUSPECT_GAP_PER_DIM, ultranest_installed=ultranest_installed,
                ultranest_labels=ultranest_labels)
            report.pop("analysis_error", None)
            report["installed"] = installed_text(report, report["analysis"])
        except Exception:
            report["analysis_error"] = traceback.format_exc()
            print("analysis failed (raw runs are kept):\n" + report["analysis_error"],
                  file=sys.stderr, flush=True)
        report["elapsed_wall_s"] = time.monotonic() - start
        write_report(report, output)

    screen = [job(m, "screen", _settings(n, f)) for m in entries for n in SCREEN_LIVE
              for f in SCREEN_FACTORS for _ in range(SEEDS)]
    ultranest = [job(by_label[lb], "ultranest", dict(n_live=reference_live, sample="MLFriends"),
                     "ultranest") for lb in ultranest_labels for _ in range(SEEDS)]
    state = dict(screen_done=0)

    def keep(row):
        report["runs"].append(row)
        print(f"{row['stage']} {row['label']} seed={row['seed']} {row['settings'].get('n_live')}"
              f"x{row['settings'].get('step_factor')}: {row['status']}, lnZ={row['log_z']}, "
              f"{row['wall_s']:.1f}s{' ' + _last_line(row['error']) if row.get('error') else ''}",
              flush=True)
        new = []
        if row["stage"] == "screen":
            state["screen_done"] += 1
            if state["screen_done"] == len(screen):
                new = plan_reference()
        refresh()
        return new

    def plan_reference():
        try:
            factor = leading_factor(report["runs"], labels)[0]
            if factor is None:
                report["reference_note"] = "screen incomplete or not clean: no reference queued"
                return []
            report["leading_step_factor"] = factor
            return costliest_first([job(m, "reference", _settings(reference_live, factor))
                                    for m in entries for _ in range(SEEDS)])
        except Exception:
            report["reference_note"] = "reference planning failed:\n" + traceback.format_exc()
            return []

    skipped = []
    try:
        refresh()
        run_jobs(ultranest + costliest_first(screen), workers=workers, timeout_s=run_timeout_s,
                 on_result=keep, deadline=deadline, skipped=skipped)
        report["not_run"] = [dict(label=j["model"].label, stage=j["stage"], engine=j["engine"],
                                  n_live=j["settings"].get("n_live"),
                                  step_factor=j["settings"].get("step_factor")) for j in skipped]
        capped = bool(skipped) or any(r.get("error") == WALL_CAP for r in report["runs"])
        report["status"] = "cut_by_wall_cap" if capped else "complete"
    except BaseException:
        report.update(status="interrupted_or_failed", error=traceback.format_exc())
        raise
    finally:
        refresh()
    return report


# --------------------------------------------------------------------------- the problem spectrum
def _revision():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True,
                              text=True, timeout=20, check=True).stdout.strip()
    except Exception as exc:  # noqa: BLE001 - provenance only; the pass does not depend on it
        print(f"git revision unavailable: {exc}", file=sys.stderr)
        return "unknown"


def _loaded(ctx, record, **source):
    """Both loaders end here: every record model as a ModelEntry (models_from_record refuses a ctx
    whose arena fingerprint differs from the record's, which certifies a rebuilt context)."""
    deep_pass = _deep_pass()
    if not record or record.get("preferred_count") is None:
        raise ValueError("the fast-pass record has no preferred count")
    ceiling = int(record["settings"]["mode_ceiling"])
    entries = deep_pass.models_from_record(ctx, record, deep_pass.plain_hypotheses(ctx, ceiling))
    metadata = dict(source, revision=_revision(), prior_provenance=record.get("prior_provenance"),
                    arena_fingerprint=record.get("arena_fingerprint"),
                    CALIBRATION_STATUS=deep_pass.CALIBRATION_STATUS, fast_pass_state=record.get("state"),
                    preferred_count=record["preferred_count"], mode_ceiling=ceiling,
                    search_limit_reason=record.get("search_limit_reason"), n_record_models=len(entries))
    return dict(entries=entries, record=record, metadata=metadata)


def load_bench_case(name, level="high", draw=0):
    """A count-bench named case on a development draw; the fast pass runs here as the record's
    producer (the production fit, a minute or so), never as a seed for the sampler."""
    from isar.synth import count_bench
    truth = next((t for t in count_bench.named_cases(BENCH_SEED) if t.name == f"{name}_{level}"), None)
    if truth is None:
        raise ValueError(f"no named case {name}_{level}")
    obs = count_bench.observe(truth, draw, BENCH_SEED, count_bench.STREAM_DRAW_FIT)
    fit_seed = int(count_bench.rng_for(BENCH_SEED, count_bench.STREAM_DRAW_FIT, truth.stream, truth.index,
                                       draw, 1).integers(2 ** 31))
    ctx = count_bench.context_of(obs)
    fit = count_bench.fit_observation(obs, seed=fit_seed, ceiling=truth.ceiling)
    return _loaded(ctx, fit.get("fast_pass"), source="bench case", spectrum=f"{name}_{level}",
                   detector=truth.resolution, draw=draw)


def load_workspace(workspace, spectrum, detector=None):
    """A saved workspace spectrum: its record from the ledger, its context rebuilt through the same
    front-end calls ``sandbox_api.preview_fit`` makes (nothing is written, the card is a copy)."""
    import copy
    from isar.reduce import combine, qc
    from isar.session import calibrations, ingest, outcome, router, scan_delivery, unit_data
    from isar.session import guidelines as guide_mod
    from isar.session.workspace import Workspace
    from isar.workspace_api.scan_keys import split_key

    experiment, scan_number, key_det = split_key(spectrum)
    det = detector or key_det
    if experiment is None or det is None:
        raise ValueError(f"{spectrum!r} is not a unit key (experiment:scan:detector); pass --detector")
    card_real, session = Workspace.open(workspace).load()
    entry = session.scans.get(spectrum) or session.scans.get(f"{experiment}:{scan_number}:{det}")
    record = outcome.fast_pass_of((entry.detectors if entry else {}).get(det))
    if not record:
        raise ValueError(f"no fast-pass record for {spectrum!r} detector {det!r} in {workspace}")
    card = copy.deepcopy(card_real)
    if not session.data_paths:
        raise ValueError("the workspace has no source data paths to re-parse")
    plan = ingest.discover(list(session.data_paths), card)
    records = ingest.parse_all(plan, card)
    for rec in records:
        combine.reduce_record(rec, card)
        qc.qc_record(rec, card)
    routed = router.route(records)
    router.apply_context_updates(routed, card, session, companion_files=plan.companion_files)
    runcal = calibrations.calibration_view(card)
    g = guide_mod.resolve(card)
    rec = next((r for r in routed.measurements if str(r.experiment_id) == str(experiment)
                and int(r.scan_number) == int(scan_number)), None)
    if rec is None or det not in rec.spectra:
        raise ValueError(f"scan {spectrum!r} / detector {det!r} not found in the re-parsed data")
    sp = rec.spectra[det]
    q = [float(v) for v in sp.q] if sp.q is not None else None     # the driver's way, so the fingerprint can match
    T = unit_data.scan_temperature(rec)
    calres = calibrations.resolve(rec, det, card, runcal)
    ctx, n_used, _reasons = scan_delivery.build_context(rec, det, card, calres, None, g,
                                                         scan_number=rec.scan_number, q=q, T=T)
    if ctx is None:
        raise ValueError(f"loader failure: only {n_used} points survive the masks for {spectrum!r}")
    return _loaded(ctx, record, source="workspace", spectrum=spectrum, detector=det,
                   workspace=str(workspace))


def spectrum_summary(record):
    """(state, preferred count, priced models within 10 nats of the best) of a fast-pass record."""
    priced = [m["ln_z"] + m["ln_model_prior"] for m in record.get("models", []) if m.get("ln_z") is not None]
    near = sum(1 for w in priced if w >= max(priced) - 10.0) if priced else 0
    return record.get("state"), record.get("preferred_count"), near


def list_spectra(workspace):
    from isar.session import outcome
    from isar.session.workspace import Workspace
    _card, session = Workspace.open(workspace).load()
    rows = []
    for key, scan in session.scans.items():
        for det, oc in scan.detectors.items():
            record = outcome.fast_pass_of(oc)
            if record:
                rows.append((key, det, *spectrum_summary(record)))
    print(_table_text(["key (--spectrum)", "detector", "fast-pass state", "preferred count",
                       "priced models within 10 nats"], rows))


def _table_text(headers, rows):
    cells = [[str(c) for c in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in cells)


# --------------------------------------------------------------------------- checks and entry point
def self_check():
    from types import SimpleNamespace as NS

    def row(z, **changes):
        r = dict(status="ok", evidence_converged=True, suspect=[], log_z=z, information_h=4.0,
                 lnl_max=2.0, n_like_calls=9, wall_s=1.0, engine="dynesty")
        r.update(changes)
        return r
    assert clean(row(0.0)) and not clean(row(0.0, evidence_converged=False))
    assert not clean(row(float("nan"))) and not clean(row(0.0, status="failed"))
    assert clean(row(0.0, suspect=["best lnL 9 nats below"]))       # gap-only flag stays in the choice
    models = [NS(label=x, count=c, log_model_prior=0.0) for x, c in (("a", 0), ("b", 1), ("c", 1))]
    p = count_probabilities(models, {"a": 0., "b": 0., "c": 0.})
    assert abs(p["counts"][1]["p"] - 2 / 3) < 1e-12
    shared = [NS(label=x, count=c, log_model_prior=lp) for x, c, lp in
              (("a", 0, 0.0), ("b", 1, -math.log(2)), ("c", 1, -math.log(2)))]
    p = count_probabilities(shared, {"a": 0., "b": 0., "c": 0.})        # the prior is shared per count
    assert abs(p["counts"][0]["p"] - 0.5) < 1e-12 and abs(p["counts"][1]["p"] - 0.5) < 1e-12
    near = {"a": math.log(8), "b": 0., "c": 0.}
    assert count_probabilities(models, near, 0.5)["decision"] == "numerically_unresolved"
    assert count_probabilities(models, {"a": 8., "b": 0., "c": 0.}, .5)["decision"] == "clear:0"
    assert count_probabilities(models, {"a": 0., "b": 0., "c": 0.}, .1)["decision"] == "close_call"
    assert count_probabilities(models, {"a": 0., "b": 0.})["status"] == "incomplete"
    assert _json_safe(float("nan")) is None
    # deciding differences: equal offsets cancel in the pairs; a 2.1 pair error misses but is still chosen
    labels, ref = ["m0", "m1"], {"m0": 10.0, "m1": 0.0}
    runs = [row(ref[lb], label=lb, stage="screen", seed=100 * n + 10 * f + k,
                settings=dict(n_live=n, step_factor=f), wall_s=n * f / 100.0)
            for lb in labels for n in SCREEN_LIVE for f in SCREEN_FACTORS for k in range(SEEDS)]
    shifted = [dict(r, log_z=r["log_z"] + 0.9) for r in runs]      # +0.9 on every model, same direction
    e = evaluate_config(shifted, labels, ref, "m0", 400, 1)
    assert e["complete"] and e["largest"] < 1e-9, e
    missed = [dict(r, log_z=r["log_z"] + (2.1 if r["label"] == "m1" else 0.0)) for r in runs]
    evals = {(n, f): evaluate_config(missed, labels, ref, "m0", n, f) for n in SCREEN_LIVE for f in SCREEN_FACTORS}
    key, within = choose(evals, 2.0)
    assert key is not None and not within and abs(evals[key]["largest"] - 2.1) < 1e-9
    evals[(400, 1)] = dict(evals[(400, 1)], largest=1.9, mean_wall_s=5.0)
    evals[(400, 4)] = dict(evals[(400, 4)], largest=1.5, mean_wall_s=99.0)
    assert choose(evals, 2.0) == ((400, 1), True)                  # cheapest within the limit, not the smallest error
    bad = [dict(r, evidence_converged=False) if r["settings"]["n_live"] == 400 else r for r in missed]
    assert choose({(n, f): evaluate_config(bad, labels, ref, "m0", n, f)
                   for n in SCREEN_LIVE for f in SCREEN_FACTORS}, 2.0)[0][0] == 128    # unclean runs never chosen
    # a configuration is complete only when every core model has a finite reference (the decisive rival included)
    labels3 = ["m0", "m1", "m2"]
    ref3 = {"m0": 10.0, "m1": 6.0, "m2": 0.0}
    runs3 = [row(ref3[lb], label=lb, stage="screen", seed=100 * n + 10 * f + k,
                 settings=dict(n_live=n, step_factor=f)) for lb in labels3 for n in SCREEN_LIVE
             for f in SCREEN_FACTORS for k in range(SEEDS)]
    assert evaluate_config(runs3, labels3, ref3, "m0", 400, 1)["complete"]
    for gone in ("m1", "m2"):                     # m1 missing still leaves the pair (m0, m2) measured
        part = dict(ref3, **{gone: float("nan")})
        assert not evaluate_config(runs3, labels3, part, "m0", 400, 1)["complete"], gone
    # the UltraNest cross-check counts only usable runs: a failed insertion-order check never "agrees"
    un = lambda lb, z, **c: row(z, label=lb, engine="ultranest", information_h=float("nan"), **c)  # noqa: E731
    un_rows = [un(lb, ref3[lb] + 0.1 * k) for lb in ("m0", "m1") for k in range(SEEDS)]
    assert ultranest_outcome(un_rows, ["m0", "m1"], ref3, True) == "agrees"
    un_rows[0] = un("m0", ref3["m0"], evidence_converged=False,
                    suspect=["UltraNest insertion-order check missing or failed"])
    assert ultranest_outcome(un_rows, ["m0", "m1"], ref3, True) != "agrees"
    # core set: one model per count 0..preferred+2, a twin parent standing in for an unpriced count
    entry = lambda lb, k, z, o, lp=0.0: NS(label=lb, count=k, fastpass_ln_z=z, fastpass_lnl_opt=o,  # noqa: E731
                                           log_model_prior=lp)
    es = [entry("c0", 0, -9., -8.), entry("c1a", 1, -5., -4., -1.0), entry("c1b", 1, -3., -4., -1.0),
          entry("c2", 2, None, -2.), entry("c3", 3, -1., -1.), entry("c4", 4, -1., -1.)]
    assert [x.label for x in core_set(es, 1, 6)] == ["c0", "c1b", "c2", "c3"]
    assert [x.label for x in core_set(es, 1, 2)] == ["c0", "c1b", "c2"]
    assert fast_deciding_labels(core_set(es, 1, 6), 1) == ["c1b", "c3"]
    assert leading_factor([], ["m0"])[0] is None
    print("Self-check passed: clean rule, shared model prior, 0.80 sensitivity, deciding differences "
          "(cancellation, the 2.1-nat miss still chosen, the cheapest within the limit), core set.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--list-spectra", action="store_true",
                        help="print the fitted units of --workspace with their fast-pass states and exit")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--spectrum", help="the ledger key of a saved unit, experiment:scan:detector")
    parser.add_argument("--detector", help="analyzer id when the key does not end in one")
    parser.add_argument("--bench-case", help="a count_bench named case, e.g. close_doublet")
    parser.add_argument("--bench-level", default="high")
    parser.add_argument("--bench-draw", type=int, default=0)
    parser.add_argument("--output", type=Path, help="a NEW folder for calibration.json and calibration.md")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--run-timeout-s", type=float, default=3600)
    parser.add_argument("--wall-cap-s", type=float, default=7200)
    parser.add_argument("--reference-live", type=int, default=1600)
    parser.add_argument("--reference-tolerance", type=float, default=0.5)
    parser.add_argument("--difference-max", type=float, default=2.0)
    parser.add_argument("--difference-target", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=20261026)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if args.list_spectra:
        if not args.workspace:
            parser.error("--list-spectra needs --workspace")
        list_spectra(args.workspace.resolve())
        return 0
    if not args.output or bool(args.bench_case) == bool(args.spectrum) \
            or (args.spectrum and not args.workspace):
        parser.error("--output and exactly one source are required: --workspace W --spectrum KEY, "
                     "or --bench-case NAME")
    if args.bench_case:
        loaded = load_bench_case(args.bench_case, args.bench_level, args.bench_draw)
    else:
        loaded = load_workspace(args.workspace.resolve(), args.spectrum, args.detector)
    meta = loaded["metadata"]
    core = core_set(loaded["entries"], meta["preferred_count"], meta["mode_ceiling"])
    meta["core_labels"] = [e.label for e in core]
    print(f"Problem spectrum {meta['spectrum']}: fast-pass state {meta['fast_pass_state']}, preferred "
          f"count {meta['preferred_count']}, {len(core)} core models of {len(loaded['entries'])}: "
          f"{', '.join(meta['core_labels'])}", flush=True)
    report = calibrate(core, preferred_count=meta["preferred_count"], output=args.output,
                       workers=args.workers, run_timeout_s=args.run_timeout_s,
                       wall_cap_s=args.wall_cap_s, reference_live=args.reference_live,
                       reference_tolerance=args.reference_tolerance, difference_max=args.difference_max,
                       difference_target=args.difference_target, seed=args.seed, metadata=meta)
    aims = report["analysis"].get("aims", {})
    print(f"{report['status']}; {report['elapsed_wall_s']:.0f}s; aims: "
          + ", ".join(f"{k} {_reached(v)}" for k, v in aims.items()) + f"; output: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
