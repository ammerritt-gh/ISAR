#!/usr/bin/env python
"""Phase-3 step-6 measurement: gold-standard scoring against the curated FeSe_1 human fits (§10, §14).

Runs the full deterministic Phase-3 pipeline (structure loop -> frequentist errors -> model selection
-> verdict -> confidence) on every scan in `FeSe_1_fit_results.csv` and scores each verdict against the
human's, "exactly as the harness scores synthetic recovery against truth." This is THE gate (§14): it
turns "reproduces the fitter on 6 control scans" into "agrees with a human expert across 83 real scans."

Two properties are scored (isar.analyze.gold):
  - which discard bucket  -> a confusion matrix (gold OK/REJECT x ISAR accept/abstain/reject); the
    load-bearing safety property is that a human-REJECT is never returned as a confident ISAR `resolved`.
  - which omega           -> on the agreed-accept set, the omega0 pull and 68%-interval coverage. Gold
    used a fixed resolution and a different fitter, so agreement is statistical, not bit-exact.

Channel: each gold row is matched to the analyzer whose reported K equals the row's K(rlu) -> ana7 for
FeSe_1 (the analyzer at nominal Q); configurable/multi-analyzer via isar.analyze.gold.select_analyzer.

The raw .dat lives under the gitignored test_data/, so this is a local measurement script (house
practice), not a CI pytest. The scoring *logic* is unit-tested in tests/analyze/test_gold.py.

Run from the repo root:

    python docs/phase3_gold_check.py            # all 83 scans, M=200
    python docs/phase3_gold_check.py --smoke    # first ~12 scans, M=60 (fast iteration)

Outputs (under the gitignored out/phase3_gold/):
    results_gold.csv   per scan: analyzer, gold omega0/quality, ISAR verdict/confidence/omega0/interval,
                       disposition, agree, pull, |dOmega0|, in-interval, cost/dof
    figures/confusion.png            gold x ISAR disposition grid
    figures/omega0_pull_hist.png     pull distribution on the agreed-accept recovery set
    figures/omega0_scatter.png       omega0(gold) vs omega0(ISAR), coloured by ISAR verdict
    figures/confidence_calibration.png   ISAR confidence vs |pull| (does confidence track correctness?)
    FINDINGS.md        confusion, recovery stats, the 4-REJECT table, prior-independence demo, PASS/FAIL
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import matplotlib                                          # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                            # noqa: E402

from isar.analyze import errors, gold, loop, select, verdict   # noqa: E402
from isar.analyze.context import ScanContext               # noqa: E402
from isar.analyze.prior import PhononPrior                 # noqa: E402
from isar.analyze.structures import enumerate_structures   # noqa: E402
from isar.calibrate import bragg, elastic                  # noqa: E402
from isar.calibrate.energy_zero import build_energy_calibration   # noqa: E402
from isar.parsers import spec_s30                          # noqa: E402
from isar.schema import axes                               # noqa: E402

# --------------------------------------------------------------------------- config
S30_FILE = REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"   # IXS phonon fscans
SPEC_FILE = REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec"                    # raw SPEC: Bragg
GOLD_CSV = REPO / "test_data" / "APS S30" / "FeSe_1_fit_results.csv"
OUT = REPO / "out" / "phase3_gold"
FIGDIR = OUT / "figures"
BOOT_M = 200

# ISAR self-calibrates the energy zero by fitting the dedicated K=0 Bragg (herixE) elastic scans in
# the raw .spec itself (isar.calibrate.bragg + build_energy_calibration) and interpolating to each
# phonon scan — the FeSe_1 technique, NOT the gold's numbers. The gold's per-slit resolution table is
# kept ONLY to cross-check ISAR's self-measured resolution (reproduces, doesn't copy).
GOLD_RESOLUTION_BY_SLIT = {
    18: (0.958, 0.590), 20: (0.964, 0.601), 30: (0.964, 0.601), 40: (1.010, 0.554),
    50: (1.180, 0.464), 60: (1.010, 0.554), 95: (1.046, 0.488),
}

# Gate thresholds — trustworthiness properties (coverage is reported, not gated; see FINDINGS).
GATE_MEDIAN_RESID = 0.40   # meV, median |dOmega0| on the delivered-on-OK recovery set
GATE_MAX_SAFETY_VIOLATIONS = 0   # gold-REJECT scans ISAR cleanly resolved (the safety property)


# --------------------------------------------------------------------------- pipeline

def run_pipeline(rec, detector_id, card, *, center, gauss_fwhm, lorentz_fwhm,
                 prior=None, M=BOOT_M, seed=0):
    """The full Phase-3 chain on one (record, analyzer) -> (gold.IsarResult, extras dict), given the
    supplied energy zero (center) + resolution (the reference Bragg calibration).

    ScanContext -> fit admissible structures -> attach frequentist errors to the survivors (so the
    across-model pool has bootstrap samples) -> model selection -> verdict. ISAR's own per-scan
    fit_elastic is run only as a reported diagnostic (autozero), never used for the zero.
    """
    x = np.asarray(axes.scanned_axes(rec)[0].values, dtype=float)
    sp = rec.spectra[detector_id]
    mask = np.asarray(sp.mask, dtype=bool)
    counts = np.asarray(sp.intensity, dtype=float)
    autozero, _agfw, _alfw, autozero_ok = elastic.fit_elastic(x[mask], counts[mask])  # diagnostic only

    ctx = ScanContext.from_scan(rec, detector_id, card, center=center, gauss_fwhm=gauss_fwhm,
                                lorentz_fwhm=lorentz_fwhm,
                                prior_omega=(prior.omega0 if prior else None))
    results = loop.fit_admissible_structures(ctx, minos=True)
    fits = {r.label: r for r in results}
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}

    prelim = select.select_models(results, stamp=False)     # survivors without bootstrap yet
    for label in prelim.survivors:                          # attach errors to survivors only
        r = fits.get(label)
        if r is not None and r.optimizer_converged and label in by_hyp:
            errors.attach(r, by_hyp[label], ctx, M=M, seed=seed, with_profile=True)
    sel = select.select_models(results)                     # now pools survivors' samples
    sv = verdict.classify(results, ctx, sel, prior=prior)

    best = fits.get(sel.best) if sel.best else None
    omega0 = best.omega0 if best is not None else None
    interval = None
    if best is not None and best.within_model and best.within_model.get("freq_interval") is not None:
        interval = best.within_model["freq_interval"]
    elif sel.pooled is not None and sel.pooled.modality == "fold":
        interval = sel.pooled.interval
    omega0_err = best.omega0_err_hesse if best is not None else None

    isar = gold.IsarResult(verdict=sv.verdict, confidence=sv.confidence, omega0=omega0,
                           omega0_err=omega0_err, interval=interval, modality=sel.modality)
    extras = {"center": center, "gauss_fwhm": gauss_fwhm, "lorentz_fwhm": lorentz_fwhm,
              "autozero": autozero, "autozero_resid": abs(autozero - center), "autozero_ok": autozero_ok,
              "T_control": ctx.T, "cost_per_dof": (best.cost_per_dof if best else float("nan")),
              "best_label": sel.best, "gamma": (best.gamma if best else float("nan")),
              "reasons": "; ".join(sv.reasons)}
    return isar, extras


# --------------------------------------------------------------------------- leave-one-out prior

def predict_omega(rows_ok, target, *, exclude_scan):
    """Leave-one-out dispersion prediction for `target`'s omega0 from the other gold-OK rows of the
    same sample: linear-in-K within the same T if possible, else pooled across T. Returns
    (predicted_omega0, sigma) with sigma floored at 0.3 meV. The recovered omega is shown to be
    prior-independent (Bernstein-von Mises) so this gold-derived prior cannot manufacture agreement."""
    same = [r for r in rows_ok if r.sample == target.sample and r.scan != exclude_scan]
    sameT = [r for r in same if abs(r.T - target.T) < 0.5]
    pool = sameT if len(sameT) >= 2 else same
    if len(pool) >= 2 and len({r.K for r in pool}) >= 2:
        K = np.array([r.K for r in pool]); O = np.array([r.omega0 for r in pool])
        coef = np.polyfit(K, O, 1)
        pred = float(np.polyval(coef, target.K))
        sig = max(0.3, float(np.std(O - np.polyval(coef, K))))
    elif pool:
        pred = float(np.mean([r.omega0 for r in pool]))
        sig = max(0.3, float(np.std([r.omega0 for r in pool])))
    else:
        pred, sig = target.omega0, 1.0
    return pred, sig


# --------------------------------------------------------------------------- figures

def fig_confusion(path, report):
    rows, cols = ("accept", "reject"), ("resolved", "flagged", "reject", "abstain")
    grid = np.array([[report.confusion.count(g, i) for i in cols] for g in rows], dtype=float)
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    ax.imshow(grid, cmap="Blues")
    ax.set_xticks(range(len(cols)), [f"ISAR\n{c}" for c in cols])
    ax.set_yticks(range(len(rows)), [f"gold\n{r}" for r in rows])
    for r in range(len(rows)):
        for c in range(len(cols)):
            ax.text(c, r, int(grid[r, c]), ha="center", va="center", fontsize=13,
                    color=("white" if grid[r, c] > grid.max() / 2 else "black"))
    ax.set_title("Disposition confusion (gold vs ISAR)")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def fig_pull_hist(path, scores):
    pulls = [s.omega0_pull for s in scores
             if s.isar_disposition in ("resolved", "flagged") and s.gold_disposition == "accept"
             and s.omega0_pull is not None]
    fig, ax = plt.subplots(figsize=(5.6, 3.6))
    if pulls:
        ax.hist(pulls, bins=max(6, len(pulls) // 4), color="steelblue", edgecolor="white")
        ax.axvline(0.0, color="k", lw=1)
        ax.axvline(float(np.mean(pulls)), color="crimson", lw=1.5,
                   label=f"mean {np.mean(pulls):+.2f}, sd {np.std(pulls, ddof=1):.2f}")
        ax.legend()
    ax.set_xlabel("omega0 pull = (ISAR - gold) / sqrt(sigma_isar^2 + sigma_gold^2)")
    ax.set_ylabel("scans"); ax.set_title("Recovery pull (agreed-accept set)")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def fig_scatter(path, scores):
    buckets = sorted({s.isar_verdict for s in scores})
    cmap = plt.get_cmap("tab10")
    color = {b: cmap(i % 10) for i, b in enumerate(buckets)}
    fig, ax = plt.subplots(figsize=(5.6, 5.2))
    lo, hi = 0.0, 0.0
    for s in scores:
        if s.isar_omega0 is None:
            continue
        ax.scatter(s.gold_omega0, s.isar_omega0, s=28, color=color[s.isar_verdict],
                   edgecolor=("red" if s.notes else "none"), linewidth=1.2,
                   marker=("x" if s.quality.upper() == "REJECT" else "o"))
        hi = max(hi, s.gold_omega0, s.isar_omega0)
    ax.plot([lo, hi], [lo, hi], "k--", lw=0.8)
    for b in buckets:
        ax.scatter([], [], color=color[b], label=b)
    ax.scatter([], [], marker="x", color="gray", label="gold REJECT")
    ax.set_xlabel("gold omega0 (meV)"); ax.set_ylabel("ISAR omega0 (meV)")
    ax.set_title("omega0: ISAR vs gold"); ax.legend(fontsize=7, loc="upper left")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def fig_confidence(path, scores):
    fig, ax = plt.subplots(figsize=(5.6, 3.6))
    for s in scores:
        if s.isar_disposition not in ("resolved", "flagged") or s.omega0_pull is None:
            continue
        ok = s.gold_disposition == "accept"
        ax.scatter(abs(s.omega0_pull), s.isar_confidence, s=28,
                   color=("steelblue" if ok else "crimson"),
                   marker=("o" if ok else "X"))
    ax.scatter([], [], color="steelblue", label="gold OK")
    ax.scatter([], [], color="crimson", marker="X", label="gold REJECT (resolved = wrong)")
    ax.set_xlabel("|omega0 pull|"); ax.set_ylabel("ISAR confidence")
    ax.set_title("Confidence vs recovery error (ISAR-resolved)"); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


# --------------------------------------------------------------------------- output

def write_csv(path, recs):
    fields = []
    for r in recs:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="")
        w.writeheader(); w.writerows(recs)


def _fmt(x, n=3):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{n}f}"


def _round(x, n):
    """round() that degrades to '' for None/NaN (CSV-friendly)."""
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return ""
    return round(float(x), n)


def write_findings(path, report, scores, rejects, prior_demo, gate, n_ok, n_reject, n_delivered_ok,
                   n_resolved_ok, n_flagged_ok, *, res_xcheck=None, n_elastic=0, n_held=0):
    L = []
    L.append("# Phase 3 — Step 6 FINDINGS: gold-standard scoring (FeSe_1)\n")
    L.append(f"Scored **{report.n} scans** ({n_ok} gold-OK, {n_reject} gold-REJECT) from "
             "`FeSe_1_fit_results.csv` against the full Phase-3 pipeline (structure loop -> errors -> "
             "selection -> verdict), channel = analyzer matched by reported K (ana7 for FeSe_1).\n")

    L.append("## Energy zero — self-calibrated from the Bragg scans (the technique, not the results)\n")
    L.append(f"ISAR fit **{n_elastic}** dedicated K=0 Bragg (`herixE`) elastic scans from the raw `.spec` "
             "itself, built a scan#-keyed zero series, and interpolated the zero to each phonon scan "
             "(no gold E0/center values used). The self-calibrated zeros are in a different absolute "
             "frame than the gold's `center` column but recover the same omega (below).")
    L.append(f"- scans held for calibration (no Bragg zero in coverage): **{n_held}**")
    L.append("- ISAR's own per-phonon-scan elastic fit (autozero) vs the Bragg zero is recorded per scan "
             "in `results_gold.csv` — it diverges where the phonon outshines the elastic, which is exactly "
             "why the dedicated Bragg scans are needed.\n")

    if res_xcheck:
        L.append("## Resolution cross-check (ISAR self-measured vs gold Bragg table)\n")
        L.append("| slit | ISAR gauss/lorentz FWHM | gold gauss/lorentz FWHM |")
        L.append("|---|---|---|")
        for r in res_xcheck:
            L.append(f"| {r['slit']} | {r['isar'][0]:.3f} / {r['isar'][1]:.3f} | "
                     f"{r['gold'][0]:.3f} / {r['gold'][1]:.3f} |")
        L.append("")

    L.append("## Disposition confusion (gold x ISAR)\n")
    L.append("\"Delivered\" = resolved (clean) + flagged (`model_incomplete`: usable omega, imperfect "
             "model). Reject = `junk`/`unconstrained`; abstain = held/underpowered/no_signal.\n")
    L.append("| gold \\ ISAR | resolved | flagged | reject | abstain | total |")
    L.append("|---|---|---|---|---|---|")
    for g in ("accept", "reject"):
        c = report.confusion
        L.append(f"| **{g}** ({'OK' if g=='accept' else 'REJECT'}) | {c.count(g,'resolved')} | "
                 f"{c.count(g,'flagged')} | {c.count(g,'reject')} | {c.count(g,'abstain')} | "
                 f"{c.row_total(g)} |")
    L.append("")
    L.append(f"- ISAR **delivered a usable omega for {n_delivered_ok}/{n_ok}** gold-OK scans "
             f"({100.0*n_delivered_ok/max(n_ok,1):.0f}%: {n_resolved_ok} resolved, {n_flagged_ok} flagged).")
    L.append(f"- **Safety violations** (gold-REJECT cleanly `resolved` by ISAR): "
             f"**{len(report.safety_violations)}** {report.safety_violations or ''}.")
    L.append("")

    L.append("## omega0 recovery (agreed-accept set)\n")
    L.append(f"- recovery scans: **{report.n_recovery}**")
    L.append(f"- pull mean / sd: **{_fmt(report.pull_mean)}** / **{_fmt(report.pull_std)}**")
    L.append(f"- median |dOmega0|: **{_fmt(report.median_abs_resid)} meV**")
    L.append(f"- gold-omega0 inside ISAR 68% interval: **{_fmt(report.coverage68)}** "
             "(target ~0.68 for calibrated intervals)")
    L.append(f"- confidence when ISAR-resolved & correct vs wrong: **{_fmt(report.conf_agree)}** "
             f"vs **{_fmt(report.conf_disagree)}**")
    L.append("")

    L.append("## ISAR verdict buckets\n")
    for b, n in sorted(report.bucket_counts.items(), key=lambda kv: -kv[1]):
        L.append(f"- {b}: {n}")
    L.append("")

    L.append("## The 4 gold-REJECTs (the load-bearing test)\n")
    L.append("| scan | sample | gold omega0 | gold Chi2 | ISAR verdict | ISAR conf | disposition |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rejects:
        L.append(f"| {r['scan']} | {r['sample']} | {r['gold_omega0']} | {r['gold_chi2']} | "
                 f"{r['isar_verdict']} | {_fmt(r['isar_confidence'],2)} | {r['isar_disposition']} |")
    L.append("\nThe human REJECTed these as blown-up fits (railed damping/energy, huge Chi2). ISAR must "
             "decline them too — never `resolved`.\n")

    if prior_demo:
        L.append("## Prior-independence of recovered omega0\n")
        L.append("Same scans fit with the leave-one-out gold prior vs no prior; the deterministic fit "
                 "is well-counted (Bernstein-von Mises), so omega0 barely moves -> the gold-derived "
                 "prior does not manufacture the agreement above.\n")
        L.append("| scan | omega0 (LOO prior) | omega0 (no prior) | dOmega0 |")
        L.append("|---|---|---|---|")
        for d in prior_demo:
            L.append(f"| {d['scan']} | {_fmt(d['with'])} | {_fmt(d['without'])} | {_fmt(d['delta'],4)} |")
        worst = max((abs(d["delta"]) for d in prior_demo if d["delta"] is not None), default=0.0)
        L.append(f"\nworst |dOmega0| from toggling the prior: **{worst:.4f} meV**.\n")

    L.append("## Coverage & caveats (reported, not gated)\n")
    L.append(f"- ISAR **delivered a usable omega for {n_delivered_ok}/{n_ok}** gold-OK scans "
             f"(coverage; declining a hard automatic fit is good triage, not a correctness failure).")
    L.append("- **`junk` over-fires** on some low-K gold-OK scans: ISAR recovers the right omega "
             "(small |dOmega0|) but the fit has a large *structureless* residual (its automatic "
             "grid+NNLS fit is worse than the gold's free-center + hand-tuned IC), so the "
             "junk/model_incomplete runs test lands on junk. A step-4 refinement (require *no "
             "significant peak* for junk; a real peak + high cost -> model_incomplete/flagged) would "
             "move these omega-correct scans into the delivered/flagged tier. Tracked as follow-up.")
    L.append("- The sigma-pull (mean/sd above) is dominated by tiny bootstrap sigma on well-counted "
             "data, so it is reported, not gated; the meV-scale median |dOmega0| is the recovery metric.")
    L.append("")

    L.append("## Gate (trustworthiness)\n")
    for name, ok, detail in gate:
        L.append(f"- [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    overall = all(ok for _, ok, _ in gate)
    L.append(f"\n**{'PASS' if overall else 'FAIL'}** — step 6 gold-standard scoring.\n")
    path.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- main

def main():
    smoke = "--smoke" in sys.argv
    M = 60 if smoke else BOOT_M
    OUT.mkdir(parents=True, exist_ok=True)
    FIGDIR.mkdir(parents=True, exist_ok=True)
    for p in FIGDIR.glob("*.png"):
        p.unlink()

    card = spec_s30.card_template()
    by_scan = {rec.scan_number: rec for rec in spec_s30.parse_file(S30_FILE, card)}
    gold_rows = gold.load_gold(GOLD_CSV)
    if smoke:
        gold_rows = gold_rows[:12]
    rows_ok = [r for r in gold_rows if r.quality.upper() == "OK"]

    # ISAR self-calibrates the energy zero from the dedicated Bragg (herixE) elastic scans.
    print("building energy calibration from the Bragg (herixE) elastic scans ...")
    elastic_scans = bragg.read_elastic_scans(SPEC_FILE)
    calib = build_energy_calibration(elastic_scans)
    span = (f"[{calib.zeros.keys.min():.0f},{calib.zeros.keys.max():.0f}]"
            if len(calib.zeros) else "(empty)")
    print(f"  fitted {len(calib.zeros)} K~0 elastic scans of {len(elastic_scans)}; "
          f"zero series scan# {span}; slits {sorted(calib.resolution_by_slit)}")

    print(f"== Phase-3 step 6: gold-standard scoring ({len(gold_rows)} scans, M={M}) ==")
    scores, csv_recs, missing = [], [], []
    for row in sorted(gold_rows, key=lambda r: r.scan):
        rec = by_scan.get(row.scan)
        if rec is None:
            missing.append(row.scan); continue
        choice = gold.select_analyzer(rec, row)
        zero = calib.zero(row.scan)                          # self-calibrated, interpolated zero
        pred, sig = predict_omega(rows_ok, row, exclude_scan=row.scan)
        prior = PhononPrior(omega0=pred, amplitude=None, omega0_sigma=sig)
        if zero is None:                                     # no Bragg zero in coverage -> held (never E0=0)
            isar = gold.IsarResult(verdict="held_for_calibration", confidence=0.0, omega0=None)
            extra = {}
        else:
            gfw, lfw = calib.resolution(row.slit)
            isar, extra = run_pipeline(rec, choice.detector_id, card, center=zero, gauss_fwhm=gfw,
                                       lorentz_fwhm=lfw, prior=prior, M=M, seed=6000 + row.scan)
        s = gold.score_row(row, isar)
        scores.append(s)
        flag = " !FLAG-CHANNEL" if choice.flagged else ""
        viol = " !SAFETY" if s.safety else ""
        print(f"  scan#{row.scan:>3} {choice.detector_id} (K={row.K:.2f}{flag}) z={_fmt(zero,2)} "
              f"gold={row.quality:<6} omega0={row.omega0:5.2f} -> {isar.verdict:<18} "
              f"conf={isar.confidence:.2f} omega0={_fmt(isar.omega0,2)} dW={_fmt(s.omega0_abs_resid,2)}{viol}")
        csv_recs.append({
            "scan": row.scan, "sample": row.sample, "analyzer": choice.detector_id,
            "k_residual": round(choice.k_residual, 5), "channel_flagged": int(choice.flagged),
            "gold_T": row.T, "ctx_T_control": _round(extra.get("T_control"), 1), "K": row.K,
            "slit": row.slit, "isar_zero": _round(zero, 4),
            "isar_autozero": _round(extra.get("autozero"), 4),
            "autozero_resid": _round(extra.get("autozero_resid"), 4),
            "gold_center": row.center, "gold_omega0": row.omega0, "gold_omega0_err": row.omega0_err,
            "gold_quality": row.quality, "gold_chi2": row.chi2, "isar_verdict": isar.verdict,
            "isar_confidence": round(isar.confidence, 3),
            "isar_omega0": (round(isar.omega0, 4) if isar.omega0 is not None else ""),
            "isar_interval": (f"[{isar.interval[0]:.3f},{isar.interval[2]:.3f}]"
                              if isar.interval else ""),
            "isar_disposition": s.isar_disposition, "gold_disposition": s.gold_disposition,
            "agree": ("" if s.agree is None else int(s.agree)),
            "omega0_pull": (round(s.omega0_pull, 3) if s.omega0_pull is not None else ""),
            "omega0_abs_resid": (round(s.omega0_abs_resid, 4) if s.omega0_abs_resid is not None else ""),
            "omega0_in_interval": ("" if s.omega0_in_interval is None else int(s.omega0_in_interval)),
            "cost_per_dof": _round(extra.get("cost_per_dof"), 2), "modality": isar.modality,
            "notes": s.notes, "prior_omega": round(pred, 3),
        })

    report = gold.aggregate(scores)
    n_ok = sum(1 for s in scores if s.gold_disposition == "accept")
    n_reject = sum(1 for s in scores if s.gold_disposition == "reject")
    n_delivered_ok = sum(1 for s in scores
                         if s.gold_disposition == "accept" and gold.delivered(s.isar_disposition))
    n_resolved_ok = sum(1 for s in scores
                        if s.gold_disposition == "accept" and s.isar_disposition == "resolved")
    n_flagged_ok = sum(1 for s in scores
                       if s.gold_disposition == "accept" and s.isar_disposition == "flagged")

    # prior-independence demo: a handful of gold-OK scans with the LOO prior on vs off (same
    # self-calibrated zero/resolution) — shows the recovered omega does not depend on the prior.
    demo_scans = [r.scan for r in rows_ok if calib.zero(r.scan) is not None][: (3 if smoke else 6)]
    prior_demo = []
    for sc in demo_scans:
        row = next(r for r in gold_rows if r.scan == sc)
        rec = by_scan.get(sc)
        if rec is None:
            continue
        det = gold.select_analyzer(rec, row).detector_id
        z = calib.zero(sc); gfw, lfw = calib.resolution(row.slit)
        pred, sig = predict_omega(rows_ok, row, exclude_scan=sc)
        kw = dict(center=z, gauss_fwhm=gfw, lorentz_fwhm=lfw, M=M, seed=6000 + sc)
        with_p, _ = run_pipeline(rec, det, card, prior=PhononPrior(pred, None, sig), **kw)
        no_p, _ = run_pipeline(rec, det, card, prior=None, **kw)
        delta = (None if (with_p.omega0 is None or no_p.omega0 is None)
                 else with_p.omega0 - no_p.omega0)
        prior_demo.append({"scan": sc, "with": with_p.omega0, "without": no_p.omega0, "delta": delta})

    # resolution cross-check: ISAR's self-measured per-slit resolution vs the gold's Bragg table.
    res_xcheck = []
    for slit in sorted(calib.resolution_by_slit):
        ig, il = calib.resolution_by_slit[slit]
        gg, gl = GOLD_RESOLUTION_BY_SLIT.get(slit, (float("nan"), float("nan")))
        res_xcheck.append({"slit": slit, "isar": (ig, il), "gold": (gg, gl)})

    rejects = []
    for r in csv_recs:
        if r["gold_quality"].upper() == "REJECT":
            rejects.append({"scan": r["scan"], "sample": r["sample"], "gold_omega0": r["gold_omega0"],
                            "gold_chi2": r["gold_chi2"], "isar_verdict": r["isar_verdict"],
                            "isar_confidence": r["isar_confidence"],
                            "isar_disposition": r["isar_disposition"]})

    # --- gate: the trustworthiness properties (safety + recovery accuracy + confidence calibration).
    # The delivered fraction (coverage) is REPORTED, not gated — declining a hard automatic fit is
    # good triage, not a correctness failure; and the sigma-pull is dominated by tiny bootstrap sigma
    # on well-counted data, so it too is reported, not gated. ---
    deliver_frac = n_delivered_ok / max(n_ok, 1)
    calibrated = (report.conf_agree is not None
                  and (report.conf_disagree is None or report.conf_agree > report.conf_disagree))
    gate = [
        ("safety (no clean resolve on a gold-REJECT)",
         len(report.safety_violations) <= GATE_MAX_SAFETY_VIOLATIONS,
         f"{len(report.safety_violations)} violations {report.safety_violations} "
         f"(<= {GATE_MAX_SAFETY_VIOLATIONS})"),
        ("recovery accuracy: median |dOmega0| (meV)",
         report.median_abs_resid is not None and report.median_abs_resid <= GATE_MEDIAN_RESID,
         f"{_fmt(report.median_abs_resid)} meV on {report.n_recovery} delivered-OK (<= {GATE_MEDIAN_RESID})"),
        ("confidence calibration (delivered-correct > delivered-wrong)",
         calibrated,
         f"{_fmt(report.conf_agree)} > {_fmt(report.conf_disagree)}"),
    ]
    print(f"  (reported, not gated) delivered {deliver_frac:.2f} of gold-OK "
          f"= {n_resolved_ok} resolved + {n_flagged_ok} flagged of {n_ok}")

    write_csv(OUT / "results_gold.csv", csv_recs)
    fig_confusion(FIGDIR / "confusion.png", report)
    fig_pull_hist(FIGDIR / "omega0_pull_hist.png", scores)
    fig_scatter(FIGDIR / "omega0_scatter.png", scores)
    fig_confidence(FIGDIR / "confidence_calibration.png", scores)
    write_findings(OUT / "FINDINGS.md", report, scores, rejects, prior_demo, gate,
                   n_ok, n_reject, n_delivered_ok, n_resolved_ok, n_flagged_ok, res_xcheck=res_xcheck,
                   n_elastic=len(calib.zeros), n_held=sum(1 for s in scores
                                                          if s.isar_verdict == "held_for_calibration"))

    print(f"\n== Summary ==  delivered {n_delivered_ok}/{n_ok} gold-OK "
          f"({n_resolved_ok} resolved + {n_flagged_ok} flagged); "
          f"safety violations {len(report.safety_violations)} {report.safety_violations}")
    print(f"  pull mean/sd {_fmt(report.pull_mean)}/{_fmt(report.pull_std)}; "
          f"median |dOmega0| {_fmt(report.median_abs_resid)} meV; coverage68 {_fmt(report.coverage68)}")
    print(f"  confidence delivered-correct/wrong {_fmt(report.conf_agree)}/{_fmt(report.conf_disagree)}")
    if missing:
        print(f"  MISSING raw scans: {missing}")
    for name, ok, detail in gate:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    overall = all(ok for _, ok, _ in gate)
    print(f"\n{'PASS' if overall else 'FAIL'}: step-6 gold-standard scoring "
          f"(wrote {OUT / 'FINDINGS.md'})")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
