"""Compare the coupled pricing's gradient path with the finite-difference path on real-shaped jobs (S1U5).

    python tools/inter_gradient_compare.py <workspace COPY> --anchors KEY KEY --direction q --workers 16 --out DIR

Opens the anchored batch as the GUI would (``tools/inter_ring1.py``), stops at ring 1's stage 1, builds each ring-1
spectrum's seed jobs and, from the seeds' prices, its first step jobs, and picks per class (ordinary, on a bound,
hidden pair) the first ``--per-class`` jobs whose basins show that class. A flat-or-legacy-width hidden pair is a
fourth required class: it comes from the experiment when its prior is flat, else from a synthetic case built as in
``tests/inter_hidden_case.py`` (marked ``synthetic`` in the table). Every picked job is priced at ``depth="full"`` with
``gradients=False`` (the finite-difference path of 12bb8f4) and with the default, in parallel, and compared basin by
basin: status, ln Z, the posterior mode in posterior sd, the objective F at the mode, the mask.

A basin that differs is judged CORRECTION, not a miss, when the finite-difference optimiser stopped short: it is
undetermined / unpriced there, or the gradient path reached a lower F (by more than 1e-6) at a positive-definite
mode. Corrections are listed. A MISS is anything else: worse or different with no better optimum, or
|delta ln Z| > 0.02 nat (S1U3's agreement, the plan's reading of "far below the 0.1-nat hysteresis").

Exit code: 1 when any case misses; else 2 when a required class is absent (named); else 0. Writes ``table.md``,
``compare.json`` and ``summary.txt`` under --out. --timeout-s kills the run and writes TIMEOUT.txt.

Run on a scratch COPY of the experiment only (the batch records are written into the workspace). Deliberate
measurement, never part of the routine suite.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):                      # run as a script from anywhere in the repo
    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

from inter_ring1 import arm_timeout, ring1  # noqa: E402
from isar.analyze.coupled import refit as R  # noqa: E402
from isar.analyze.coupled.types import PricingJob, Start  # noqa: E402

DLN_Z = 0.02                  # nats: S1U3's agreement (the plan's reading, not an operator number)
F_TIE = 1e-6                  # an objective lower by more than this is a better optimum
CLASSES = ("ordinary", "bound", "hidden", "flat_hidden")
THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")


# ---- the worker side -------------------------------------------------------------------------------------
def _summary(b, slots) -> dict:
    """One engine basin as plain numbers (the worker returns these; an engine does not travel)."""
    m = b.model
    d = {"label": m.hyp.label, "delta": list(m.delta), "index": int(b.index), "status": b.status,
         "reason": b.reason or "", "ln_z": None if b.ln_z is None else float(b.ln_z),
         "split": getattr(b, "boundary_method", "") == "split", "pinned": list(b.pinned or ()), "priced": False}
    if b.status in R._PRICED and b.ln_z is not None and b.theta_map is not None and not R._free_width_at_floor(b):
        d["priced"] = True
        free = np.asarray(m.free, int)
        d["theta"] = [float(x) for x in b.theta_map[free]]
        d["F"] = float(b.objective(b.theta_map[free])) if b.objective is not None else None
        cov = b.cov_free
        d["sd"] = None if cov is None else [float(x) for x in np.sqrt(np.abs(np.diag(cov)))]
        d["posdef"] = bool(cov is not None and np.linalg.eigvalsh((cov + cov.T) / 2.0).min() > 0.0)
    return d


def price_one(snap, job, gradients: bool):
    """-> (Priced, [basin summary], seconds), the engine seen through R._Labelled as the S1U3 test does."""
    engines, orig = [], R._Labelled

    class Spy(orig):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            engines.append(self)
    R._Labelled = Spy
    t = time.perf_counter()
    try:
        p = R.price(snap, job, gradients=gradients)
    finally:
        R._Labelled = orig
    secs = time.perf_counter() - t
    basins = [] if not engines else [_summary(b, job.slots) for b in engines[0].basins() if b.count == job.count]
    return p, basins, secs


# ---- classes ---------------------------------------------------------------------------------------------
def _flat(snap) -> bool:
    prior = getattr(getattr(snap.context, "settings", None), "prior", None)
    return bool(prior is not None and (getattr(prior, "width_kind", "") == "flat" or getattr(prior, "legacy_v4", False)))


def classes_of(snap, basins) -> list[str]:
    """The classes a priced job's basins show."""
    ok = [b for b in basins if b["priced"]]
    out = []
    if any(b["split"] for b in ok):
        out.append("hidden")
        if _flat(snap):
            out.append("flat_hidden")
    if any(b["status"] == "ok_boundary" or b["pinned"] for b in ok):
        out.append("bound")
    if any(not b["split"] and not b["pinned"] and b["status"] == "ok" for b in ok):
        out.append("ordinary")
    return out


def synthetic_flat_hidden():
    """(snapshot, job): the hidden-pair case of tests/inter_hidden_case.py under the flat width prior."""
    sys.path.insert(0, str(REPO / "tests"))
    from inter_hidden_case import Prepared, hidden_snapshot
    from isar.analyze import prior
    snap = hidden_snapshot(3.0, asimov=True)
    prep = Prepared(snap.context.ctx, 2)
    prep.settings = replace(prep.settings, prior=prior.PriorSettings(width_kind="flat"))
    snap = replace(snap, context=prep)
    spec = R._Spec(snap)
    nb = next(m for m in spec.held_all["x"].modes if m.branch == "b2")
    own = spec.own.modes[0]
    start = [(own.omega0, own.gamma, own.height), (float(np.mean(spec.pred["b2"])), nb.gamma, nb.height)]
    return snap, PricingJob(snap.unit, snap.snapshot_id, 2, ("b1", "b2"), "synthetic-flat-hidden",
                            (Start("own", tuple(start)),))


# ---- the comparison --------------------------------------------------------------------------------------
def compare_job(fd, new, mode_tol: float) -> dict:
    """fd, new: (Priced, basins, seconds). -> {"basins": [row], "verdict": agree | correction | miss, "job": {...}}."""
    (pf, bf, sf), (pn, bn, sn) = fd, new
    kf = {(b["label"], tuple(b["delta"]), b["index"]): b for b in bf}
    kn = {(b["label"], tuple(b["delta"]), b["index"]): b for b in bn}
    rows, notes = [], []
    for key in sorted(set(kf) | set(kn), key=str):
        f, n = kf.get(key), kn.get(key)
        row = {"label": key[0], "delta": list(key[1]), "index": key[2],
               "status_fd": None if f is None else f["status"], "status_new": None if n is None else n["status"],
               "split": bool((f or n)["split"]), "ln_z_fd": None, "ln_z_new": None, "d_ln_z": None, "F_fd": None,
               "F_new": None, "mode_sd": None, "verdict": "agree", "why": ""}
        pf_ok, pn_ok = bool(f and f["priced"]), bool(n and n["priced"])
        if pf_ok:
            row.update(ln_z_fd=f["ln_z"], F_fd=f.get("F"))
        if pn_ok:
            row.update(ln_z_new=n["ln_z"], F_new=n.get("F"))
        if pf_ok and pn_ok:
            row["d_ln_z"] = n["ln_z"] - f["ln_z"]
            if f["sd"] and n.get("theta") and len(f["theta"]) == len(n["theta"]):
                row["mode_sd"] = max(abs(a - b) / s for a, b, s in zip(n["theta"], f["theta"], f["sd"]) if s > 0)
            why = []
            if f["split"] != n["split"]:
                why.append("split differs")
            if f["status"] != n["status"]:
                why.append(f"status {f['status']} -> {n['status']}")
            if abs(row["d_ln_z"]) > DLN_Z:
                why.append(f"|d ln Z| {abs(row['d_ln_z']):.4f} > {DLN_Z}")
            if row["mode_sd"] is not None and row["mode_sd"] > mode_tol:
                why.append(f"mode {row['mode_sd']:.2g} sd > {mode_tol:g}")
            if why:
                better = (row["F_fd"] is not None and row["F_new"] is not None and not n["split"]
                          and row["F_new"] < row["F_fd"] - F_TIE and n["posdef"])
                row["verdict"] = "correction" if better else "miss"
                row["why"] = "; ".join(why) + (f"; F {row['F_fd']:.6g} -> {row['F_new']:.6g}" if better else
                                              "; no better optimum")
        elif pn_ok and not pf_ok:
            row["why"] = f"fd path {'absent' if f is None else f['status']}; gradient path priced it"
            row["verdict"] = "correction" if n["split"] or n["posdef"] else "miss"
            if row["verdict"] == "miss":
                row["why"] += " at a mode that is not positive definite"
        elif pf_ok and not pn_ok:
            row["why"] = f"gradient path {'absent' if n is None else n['status']}; fd path priced it"
            row["verdict"] = "miss"
        rows.append(row)
    job = {"d_ln_z": None if pf.ln_z is None or pn.ln_z is None else pn.ln_z - pf.ln_z,
           "status_fd": pf.status, "status_new": pn.status,
           "mask_fd": None if pf.mode is None else list(pf.mode[3]),
           "mask_new": None if pn.mode is None else list(pn.mode[3]), "seconds_fd": sf, "seconds_new": sn}
    verdicts = {r["verdict"] for r in rows}
    jv = "agree"
    if "miss" in verdicts:
        jv = "miss"
    elif "correction" in verdicts:
        jv = "correction"
    else:                                              # basins agree: the job-level words must too
        if job["status_fd"] != job["status_new"]:
            jv, notes = "miss", notes + [f"job status {job['status_fd']} -> {job['status_new']}"]
        if job["mask_fd"] != job["mask_new"]:
            jv, notes = "miss", notes + [f"width mask {job['mask_fd']} -> {job['mask_new']}"]
        if job["d_ln_z"] is not None and abs(job["d_ln_z"]) > DLN_Z:
            jv, notes = "miss", notes + [f"job |d ln Z| {abs(job['d_ln_z']):.4f} > {DLN_Z}"]
        if (pf.ln_z is None) != (pn.ln_z is None):
            jv, notes = "miss", notes + ["priced on one path only"]
    return {"basins": rows, "verdict": jv, "job": job, "notes": notes}


# ---- the run ---------------------------------------------------------------------------------------------
def _gather(ex, calls):
    """calls: {tag: (snap, job, gradients)} -> {tag: result or the exception}, all submitted at once."""
    futs = {tag: ex.submit(price_one, *args) for tag, args in calls.items()}
    out = {}
    for tag, f in futs.items():
        try:
            out[tag] = f.result()
        except Exception as exc:  # noqa: BLE001 - one job's failure is that case's result, reported below
            print(f"job {tag} failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            out[tag] = exc
    return out


def collect(snaps, ex, per_class: int):
    """The seed and first-step jobs of every ring-1 spectrum, priced once (default path), and the picked cases."""
    searches = {u: R.open_search(s) for u, s in snaps.items()}
    seeds = {u: se.next_jobs() for u, se in searches.items()}
    got = _gather(ex, {(u, j.key): (snaps[u], j, True) for u, js in seeds.items() for j in js})
    steps = {}
    for u, se in searches.items():
        res = [got[(u, j.key)] for j in seeds[u]]
        if any(isinstance(r, Exception) for r in res):
            print(f"{u}: a seed failed to price; its step jobs are skipped", file=sys.stderr, flush=True)
            continue
        se.feed([r[0] for r in res])
        steps[u] = [] if se.done else list(se.next_jobs())
    got2 = _gather(ex, {(u, j.key): (snaps[u], j, True) for u, js in steps.items() for j in js})
    pool = []                                           # (unit, job, role, basins) in the search's order
    for u in snaps:
        for role, js, src in (("seed", seeds.get(u, ()), got), ("step", steps.get(u, ()), got2)):
            for j in js:
                r = src.get((u, j.key))
                if r is not None and not isinstance(r, Exception):
                    pool.append((u, j, role, r[1]))
    picked: dict = {}                                   # (unit, key) -> (job, [classes])
    shown = {c: [] for c in CLASSES}
    for u, j, role, basins in pool:
        for c in classes_of(snaps[u], basins):
            shown[c].append((u, j))
    for c in CLASSES:
        fresh = [x for x in shown[c] if (x[0], x[1].key) not in picked]
        for u, j in (fresh + [x for x in shown[c] if x not in fresh])[:per_class]:
            picked.setdefault((u, j.key), (j, []))[1].append(c)
    return picked, {c: len(v) for c, v in shown.items()}


def _table(cases) -> str:
    lines = ["| case | class | source | basin | split | status fd / new | ln Z fd | ln Z new | d ln Z | F fd | F new "
             "| mode (sd) | verdict | why |", "|" + "---|" * 14]
    f = lambda x, s: "-" if x is None else format(x, s)  # noqa: E731
    for c in cases:
        for r in c["result"]["basins"]:
            lines.append(f"| {c['id']} | {','.join(c['classes'])} | {c['source']} | {r['label']} d={r['delta']} "
                         f"#{r['index']} | {r['split']} | {r['status_fd']} / {r['status_new']} | {f(r['ln_z_fd'], '.4f')}"
                         f" | {f(r['ln_z_new'], '.4f')} | {f(r['d_ln_z'], '+.5f')} | {f(r['F_fd'], '.6f')} | "
                         f"{f(r['F_new'], '.6f')} | {f(r['mode_sd'], '.2g')} | {r['verdict']} | {r['why']} |")
    return "\n".join(lines)


def _summary_text(cases, shown, missing, exit_code) -> str:
    lines = ["class | cases | max |d ln Z| (basins, agreed or corrected) | corrections | misses | seconds fd | seconds new"]
    for c in CLASSES:
        mine = [x for x in cases if c in x["classes"]]
        d = [abs(r["d_ln_z"]) for x in mine for r in x["result"]["basins"] if r["d_ln_z"] is not None]
        lines.append(f"{c} | {len(mine)} | {max(d) if d else float('nan'):.5f} | "
                     f"{sum(x['result']['verdict'] == 'correction' for x in mine)} | "
                     f"{sum(x['result']['verdict'] == 'miss' for x in mine)} | "
                     f"{sum(x['result']['job']['seconds_fd'] for x in mine):.1f} | "
                     f"{sum(x['result']['job']['seconds_new'] for x in mine):.1f}")
    lines.append(f"jobs showing each class among the seeds and first steps: {json.dumps(shown)}")
    lines.append(f"missing classes: {missing or 'none'}")
    for x in cases:
        if x["result"]["verdict"] != "agree":
            lines.append(f"{x['result']['verdict'].upper()}: {x['id']} {x['classes']}: " +
                         " | ".join(f"{r['label']}#{r['index']}: {r['why']}" for r in x["result"]["basins"]
                                    if r["verdict"] != "agree") + " " + "; ".join(x["result"]["notes"]))
    lines.append(f"exit code {exit_code} (1 = a miss, 2 = a required class absent, 0 = complete and agreeing)")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Compare the gradient pricing path with finite differences (S1U5).")
    p.add_argument("workspace", help="a scratch COPY of the experiment (batch records are written into it)")
    p.add_argument("--anchors", nargs="+", required=True, metavar="KEY", help="full spectrum keys")
    p.add_argument("--direction", required=True, choices=("q", "T", "qT"))
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--out", required=True, help="folder for table.md, compare.json, summary.txt")
    p.add_argument("--per-class", type=int, default=2, help="jobs picked per class")
    p.add_argument("--mode-sd-tol", type=float, default=1e-3, help="posterior-mode agreement, in posterior sd (S1U3)")
    p.add_argument("--timeout-s", type=float, default=3600.0, help="wall-clock guard: kills the run and reports")
    args = p.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    timer = arm_timeout(args.timeout_s, out, "inter_gradient_compare")
    for k in THREAD_ENV:
        os.environ[k] = "1"                              # N processes of one BLAS thread each, not N x cores
    t0 = time.perf_counter()
    root = Path(args.workspace).resolve()
    print(f"opening the batch on {root}", flush=True)
    got = ring1(root, args.anchors, args.direction, 1, search=False)
    print(got["line"], f"ring-1 spectra: {len(got['snaps'])}", flush=True)
    with ProcessPoolExecutor(args.workers, mp_context=mp.get_context("spawn")) as ex:
        picked, shown = collect(got["snaps"], ex, args.per_class)
        cases = [{"id": f"{u[-3:]}:{j.key[:8]}", "unit": u, "key": j.key, "slots": list(map(str, j.slots)),
                  "classes": cl, "source": "experiment", "snap": got["snaps"][u], "job": j}
                 for (u, _k), (j, cl) in picked.items()]
        if not shown["flat_hidden"]:                      # the one class the experiment may lack: a synthetic case
            snap, job = synthetic_flat_hidden()
            cases.append({"id": "synthetic-flat-hidden", "unit": snap.unit, "key": job.key, "slots": ["b1", "b2"],
                          "classes": ["flat_hidden", "hidden"], "source": "synthetic", "snap": snap, "job": job})
        print(f"pricing {len(cases)} picked cases on both paths at depth full", flush=True)
        calls = {}
        for c in cases:
            job = replace(c["job"], depth="full")
            calls[(c["id"], "fd")] = (c["snap"], job, False)
            calls[(c["id"], "new")] = (c["snap"], job, True)
        res = _gather(ex, calls)
    done = []
    for c in cases:
        fd, new = res[(c["id"], "fd")], res[(c["id"], "new")]
        if isinstance(fd, Exception) or isinstance(new, Exception):
            result = {"basins": [], "verdict": "miss", "notes": [f"a path raised: fd {fd!r} new {new!r}"],
                      "job": {"seconds_fd": 0.0, "seconds_new": 0.0}}
        else:
            result = compare_job(fd, new, args.mode_sd_tol)
        done.append({k: v for k, v in c.items() if k not in ("snap", "job")} | {"result": result})
    present = {cl for c in done for cl in c["classes"]}
    missing = [c for c in CLASSES if c not in present]
    misses = [c for c in done if c["result"]["verdict"] == "miss"]
    code = 1 if misses else 2 if missing else 0
    summary = _summary_text(done, shown, missing, code) + f"\nwall {time.perf_counter() - t0:.0f} s"
    (out / "table.md").write_text(_table(done) + "\n", encoding="utf-8", newline="\n")
    (out / "compare.json").write_text(json.dumps({"cases": done, "shown": shown, "missing": missing},
                                                 indent=1, default=str) + "\n", encoding="utf-8", newline="\n")
    (out / "summary.txt").write_text(summary + "\n", encoding="utf-8", newline="\n")
    print(summary)
    print(f"wrote {out / 'table.md'}, {out / 'compare.json'}, {out / 'summary.txt'}")
    timer.cancel()
    return code


if __name__ == "__main__":
    sys.exit(main())
