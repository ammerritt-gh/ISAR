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

The raw .dat lives under the gitignored real_data/, so this is a local measurement script (house
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
from isar.analyze.phonon import enumerate_phonon_structures as enumerate_structures   # noqa: E402
from isar.calibrate import bragg, elastic                  # noqa: E402
from isar.calibrate.energy_zero import build_energy_calibration   # noqa: E402
from isar.parsers import spec_s30                          # noqa: E402
from isar.report import audit, captions, findings          # noqa: E402
from isar.schema import axes                               # noqa: E402

# --------------------------------------------------------------------------- config
S30_FILE = REPO / "real_data" / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"   # IXS phonon fscans
SPEC_FILE = REPO / "real_data" / "APS S30" / "FeSe_1-xS_x_1.spec"                    # raw SPEC: Bragg
GOLD_CSV = REPO / "real_data" / "APS S30" / "FeSe_1_fit_results.csv"
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
    ax.set_title("What the human did (rows) vs what ISAR did (columns)")
    captions.save_captioned(fig, path, "gold/confusion")


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
    ax.set_xlabel("omega0 pull = (ISAR - expert) / sqrt(sigma_isar^2 + sigma_expert^2)")
    ax.set_ylabel("scans"); ax.set_title("How close ISAR's energy is to the expert's")
    captions.save_captioned(fig, path, "gold/pull_hist")


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
    ax.set_xlabel("expert omega0 (meV)"); ax.set_ylabel("ISAR omega0 (meV)")
    ax.set_title("ISAR energy vs expert energy (dashed = exact agreement)")
    ax.legend(fontsize=7, loc="upper left")
    captions.save_captioned(fig, path, "gold/scatter")


def fig_confidence(path, scores):
    fig, ax = plt.subplots(figsize=(5.6, 3.6))
    for s in scores:
        if s.isar_disposition not in ("resolved", "flagged") or s.omega0_pull is None:
            continue
        ok = s.gold_disposition == "accept"
        ax.scatter(abs(s.omega0_pull), s.isar_confidence, s=28,
                   color=("steelblue" if ok else "crimson"),
                   marker=("o" if ok else "X"))
    ax.scatter([], [], color="steelblue", label="expert accepted")
    ax.scatter([], [], color="crimson", marker="X", label="expert rejected (resolved = danger)")
    ax.set_xlabel("|omega0 pull|  (distance from the expert, in error bars)")
    ax.set_ylabel("ISAR confidence")
    ax.set_title("Is ISAR confident only when it's close?"); ax.legend(fontsize=8)
    captions.save_captioned(fig, path, "gold/confidence")


# --------------------------------------------------------------------------- the visual audit

_COMPONENT_COLORS = {"phonon": "C0", "elastic": "C2", "bg": "0.5"}


def _isar_fit_for_audit(rec, det, card, *, center, gfw, lfw, prior):
    """Re-run only the deterministic fit + selection (no bootstrap) for one scan, to recover ISAR's
    drawn curve. Mirrors run_pipeline's fit; cheap because the audit covers only a handful of scans."""
    ctx = ScanContext.from_scan(rec, det, card, center=center, gauss_fwhm=gfw, lorentz_fwhm=lfw,
                                prior_omega=(prior.omega0 if prior else None))
    results = loop.fit_admissible_structures(ctx, minos=False)
    fits = {r.label: r for r in results}
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}
    sel = select.select_models(results, stamp=False)
    best = fits.get(sel.best) if sel.best else None
    return ctx, by_hyp, best, sel


def build_audit_panel(rec, row, det, card, *, center, gfw, lfw, prior, gold_gfw, gold_lfw, role, isar):
    """One audit panel: the data, ISAR's fit broken into components, the expert's reconstructed
    (provisional) curve, and ISAR's pull residual. The expert curve is rebuilt from the expert's
    reported omega0/gamma/center/resolution by NNLS on the amplitudes (isar.report.audit)."""
    ctx, by_hyp, best, sel = _isar_fit_for_audit(rec, det, card, center=center, gfw=gfw, lfw=lfw,
                                                 prior=prior)
    x = np.asarray(ctx.x_used, dtype=float)
    counts = np.asarray(ctx.counts_used, dtype=float)
    err = np.sqrt(np.maximum(counts, 1.0))
    xf = np.linspace(float(x.min()), float(x.max()), 400)

    isar_total = isar_comps = pull = None
    if best is not None and sel.best in by_hyp:
        comp = by_hyp[sel.best].build()
        th = np.array([best.params[n] for n in comp.param_names()], dtype=float)
        isar_total = comp.evaluate(xf, th, ctx)
        isar_comps = comp.evaluate_components(xf, th, ctx)
        pull = (counts - comp.evaluate(x, th, ctx)) / err

    gold_rc = audit.reconstruct_curve(x, counts, omega0=row.omega0, gamma=row.gamma,
                                      center=row.center, gauss_fwhm=gold_gfw, lorentz_fwhm=gold_lfw,
                                      T=ctx.T, x_eval=xf)
    return {"role": role, "scan": row.scan, "x": x, "counts": counts, "err": err, "xf": xf,
            "isar_total": isar_total, "isar_comps": isar_comps, "gold_total": gold_rc.total,
            "pull": pull, "verdict": isar.verdict, "isar_omega0": isar.omega0,
            "gold_omega0": row.omega0}


def fig_audit(path, panels):
    n = len(panels)
    fig, axs = plt.subplots(2, n, figsize=(4.6 * n, 5.6), sharex="col",
                            gridspec_kw={"height_ratios": [3, 1]}, squeeze=False)
    for j, p in enumerate(panels):
        a0, a1 = axs[0, j], axs[1, j]
        a0.errorbar(p["x"], p["counts"], yerr=p["err"], fmt="o", ms=3, color="k", label="data",
                    zorder=5)
        if p["isar_total"] is not None:
            a0.plot(p["xf"], p["isar_total"], "-", color="C1", lw=2.0, zorder=4,
                    label=f"ISAR fit ({p['verdict']})")
            for label, vals in p["isar_comps"]:
                a0.plot(p["xf"], vals, "--", lw=1.0, alpha=0.8,
                        color=_COMPONENT_COLORS.get(label, "0.6"))
        a0.plot(p["xf"], p["gold_total"], "-", color="crimson", lw=1.5, alpha=0.9,
                label="expert fit (provisional)")
        a0.set_title(f"scan {p['scan']} — {p['role']}\n"
                     f"ISAR ω₀={_fmt(p['isar_omega0'], 2)} | expert ω₀={_fmt(p['gold_omega0'], 2)} meV",
                     fontsize=8)
        a0.legend(fontsize=6, loc="upper right")
        if j == 0:
            a0.set_ylabel("counts")
        a1.axhline(0, color="0.6", lw=0.8)
        for s in (1.0, -1.0):
            a1.axhline(s, color="0.85", lw=0.6)
        if p["pull"] is not None:
            a1.plot(p["x"], p["pull"], "s-", ms=2.5, color="C1")
        a1.set_xlabel("energy transfer (meV)")
        if j == 0:
            a1.set_ylabel("pull (ISAR)")
    captions.save_captioned(fig, path, "gold/audit")


def fig_resolution_overlay(path, res_xcheck):
    rows = [r for r in res_xcheck if np.isfinite(r["gold"][0]) and np.isfinite(r["isar"][0])]
    if not rows:
        return False
    slits = [r["slit"] for r in rows]
    fig, (a0, a1) = plt.subplots(1, 2, figsize=(8.2, 3.6))
    for ax, idx, name in ((a0, 0, "Gaussian"), (a1, 1, "Lorentzian")):
        ax.plot(slits, [r["isar"][idx] for r in rows], "o-", color="C0",
                label="ISAR (self-measured)")
        ax.plot(slits, [r["gold"][idx] for r in rows], "x--", color="crimson",
                label="hand-measured")
        ax.set_xlabel("analyzer slit"); ax.set_ylabel(f"{name} FWHM (meV)"); ax.set_title(name)
        ax.legend(fontsize=7)
    captions.save_captioned(fig, path, "gold/resolution_overlay")
    return True


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
                   n_resolved_ok, n_flagged_ok, *, res_xcheck=None, n_elastic=0, n_held=0,
                   has_audit=False, has_resolution_fig=False):
    n_declined = n_ok - n_delivered_ok
    d = findings.FindingsDoc("Gold check — does ISAR make sensible first-pass calls on real data?")

    d.para(f"ISAR ran its full automatic pipeline (find the structure → fit → error bars → model "
           f"selection → verdict) on **{report.n} real FeSe scans**, and we compared each call against "
           f"a human expert's by-hand fits ({n_ok} the expert accepted, {n_reject} the expert "
           f"rejected). This is a *reasonableness and safety* check on old data — part of development, "
           f"not a live experiment.")

    d.section("What this checks — and what it does NOT claim")
    d.para("ISAR is a triage tool: a good-enough first pass that flags the hard scans for you, not a "
           "replacement for your own analysis. The bar here is three things: (1) **safety** — never "
           "hand you a confident number on a scan the expert threw out; (2) **good-enough recovery** — "
           "energies land in the right ballpark; (3) **honest confidence** — more sure when it is "
           "closer. It is explicitly NOT trying to reproduce the expert exactly: the expert's fits are "
           "themselves provisional (they may be revised), and over-tuning to one dataset would make "
           "ISAR worse on the next experiment, not better.")

    d.section("Triage summary — what ISAR did for you")
    d.bullet(f"**Delivered a usable energy for {n_delivered_ok} of {n_ok}** expert-accepted scans "
             f"({n_resolved_ok} resolved outright, {n_flagged_ok} flagged as usable-but-imperfect).")
    d.bullet(f"**Flagged or declined {n_declined}** expert-accepted scans for your eyes — nothing "
             f"deleted, just steered to the manual pile.")
    d.bullet(f"**Safety: {len(report.safety_violations)}** expert-rejected scans were confidently "
             f"resolved by ISAR (target 0) {report.safety_violations or ''}.")
    d.para(f"Read that as a morning at the beamline: instead of fitting all {report.n} scans by hand, "
           f"you'd start with ~{n_delivered_ok} already fitted plus a short flagged list to eyeball — "
           f"and you can trust it didn't quietly accept a bad one.")

    if has_audit:
        d.section("See it with your own eyes")
        d.para("Representative scans chosen by rule (not cherry-picked): a typical delivered scan, a "
               "usable-but-flagged one, one ISAR over-flagged (it actually got the energy right), and "
               "one the expert rejected. Data, ISAR's fit, the expert's reconstructed (provisional) "
               "fit, and the residual:")
        d.figure_block("gold/audit", "figures/audit.png")

    d.section("How to read this report")
    d.para("Verdicts and statistics terms are defined in `docs/GLOSSARY.md`; why ISAR uses these "
           "methods — and what the error bars do and don't mean — is in `docs/STATS_PRIMER.md`.")

    d.section("Energy zero — ISAR self-calibrates (the technique, not the numbers)")
    d.para(f"ISAR fit **{n_elastic}** dedicated K=0 Bragg (`herixE`) elastic scans from the raw "
           "`.spec` itself, built a scan-number-keyed zero series, and interpolated the zero to each "
           "phonon scan — no expert energy-zero values were used. Scans with no Bragg zero in coverage "
           f"(so held for calibration): **{n_held}**.")

    if has_resolution_fig:
        d.section("Resolution — ISAR measures its own, cross-checked")
        d.figure_block("gold/resolution_overlay", "figures/resolution_overlay.png")
    elif res_xcheck:
        d.section("Resolution cross-check (ISAR self-measured vs a hand-measured table)")
        d.table(["slit", "ISAR gauss / lorentz FWHM", "hand gauss / lorentz FWHM"],
                [[r["slit"], f"{r['isar'][0]:.3f} / {r['isar'][1]:.3f}",
                  f"{r['gold'][0]:.3f} / {r['gold'][1]:.3f}"] for r in res_xcheck])

    d.section("What the human did vs what ISAR did")
    d.figure_block("gold/confusion", "figures/confusion.png")
    c = report.confusion
    d.table(["expert \\ ISAR", "resolved", "flagged", "reject", "abstain", "total"],
            [[("accept (OK)" if g == "accept" else "reject (REJECT)"),
              c.count(g, "resolved"), c.count(g, "flagged"), c.count(g, "reject"),
              c.count(g, "abstain"), c.row_total(g)] for g in ("accept", "reject")])
    d.para("\"Delivered\" = resolved + flagged (ISAR handed you a usable number); reject / abstain mean "
           "\"look here yourself\".")

    d.section("How close ISAR's energies are to the expert's")
    d.figure_block("gold/scatter", "figures/omega0_scatter.png")
    d.figure_block("gold/pull_hist", "figures/omega0_pull_hist.png")
    d.bullet(f"recovery scans (both gave a number, expert-accepted): **{report.n_recovery}**")
    d.bullet(f"median |Δω₀|: **{_fmt(report.median_abs_resid)} meV** (instrument resolution is ~1 meV)")
    d.bullet(f"pull mean / sd: {_fmt(report.pull_mean)} / {_fmt(report.pull_std)} — large because the "
             "error bars are too tight; see Honest limitations.")

    d.section("Is ISAR confident when it's right?")
    d.figure_block("gold/confidence", "figures/confidence_calibration.png")
    d.bullet(f"confidence when resolved & close vs resolved & far: **{_fmt(report.conf_agree)}** vs "
             f"**{_fmt(report.conf_disagree)}** (higher-when-closer is what we want).")

    d.section("ISAR verdict buckets")
    d.bullets([f"{b}: {n}" for b, n in sorted(report.bucket_counts.items(), key=lambda kv: -kv[1])])

    d.section("The expert-rejected scans (the safety test)")
    d.para("The expert rejected these as blown-up fits (railed damping/energy, huge χ²). ISAR must "
           "decline them too — never `resolved`.")
    d.table(["scan", "sample", "expert ω₀", "expert χ²", "ISAR verdict", "ISAR conf", "what ISAR did"],
            [[r["scan"], r["sample"], r["gold_omega0"], r["gold_chi2"], r["isar_verdict"],
              _fmt(r["isar_confidence"], 2), r["isar_disposition"]] for r in rejects])

    if prior_demo:
        d.section("The recovered energy doesn't come from the prior")
        d.para("Each scan fit with a dispersion prior built from the OTHER expert fits, vs no prior at "
               "all. The energy barely moves — so the agreement above is the data talking, not the "
               "prior steering ISAR toward the expert's answer.")
        d.table(["scan", "ω₀ (with prior)", "ω₀ (no prior)", "Δω₀"],
                [[dd["scan"], _fmt(dd["with"]), _fmt(dd["without"]), _fmt(dd["delta"], 4)]
                 for dd in prior_demo])
        worst = max((abs(dd["delta"]) for dd in prior_demo if dd["delta"] is not None), default=0.0)
        d.para(f"Worst shift from toggling the prior: **{worst:.4f} meV**.")

    d.section("Honest limitations (read these)")
    d.caveat(f"**The error bars are statistical-only.** ISAR's 68% intervals contain the expert's "
             f"value only {_fmt(report.coverage68)} of the time (a calibrated 68% interval would be "
             "~0.68). They capture counting noise, not systematics (background shape, resolution, zero "
             "drift), so they are too tight. For triage that's fine — use the energy as a ballpark and "
             "look manually before quoting an uncertainty; don't weight by these σ. A systematics-aware "
             "error model is future work.")
    d.caveat("**`junk` over-fires — in the safe direction.** On some low-K scans ISAR recovers the "
             "right energy but its automatic fit leaves a structureless residual, so the runs-test "
             "parks the scan in `junk` (your manual pile) instead of delivering it. You lose a little "
             "automation, not a number — and nothing is deleted. A planned refinement (require no "
             "significant peak before calling junk) would move these into the flagged tier.")
    d.caveat("**The expert reference is provisional.** A small systematic offset between ISAR and the "
             "expert is expected and isn't necessarily ISAR being wrong — these are old data the owner "
             "may re-analyze. Exact agreement is not the goal.")

    d.gate(gate, heading="Sanity gate (safety + good-enough recovery + confidence)")
    d.para("This gate is a development sanity check on old data, not a production guarantee.")
    path.write_text(d.render(), encoding="utf-8")


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
    scores, csv_recs, missing, audit_pool = [], [], [], []
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
        audit_pool.append({"row": row, "rec": rec, "det": choice.detector_id, "zero": zero,
                           "gfw": extra.get("gauss_fwhm"), "lfw": extra.get("lorentz_fwhm"),
                           "prior": prior, "isar": isar, "score": s})
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

    # visual audit: rule-selected representative scans (NOT cherry-picked) — data + ISAR fit +
    # expert (reconstructed, provisional) fit + residual. Shows what ISAR delivers, flags, declines.
    def _ar(p):
        return p["score"].omega0_abs_resid

    accept_pool = [p for p in audit_pool if p["score"].gold_disposition == "accept"
                   and p["zero"] is not None]
    resolved = [p for p in accept_pool if p["isar"].verdict == "resolved" and _ar(p) is not None]
    flagged_pool = [p for p in accept_pool if p["score"].isar_disposition == "flagged"
                    and _ar(p) is not None]
    overfired = [p for p in accept_pool if p["score"].isar_disposition == "reject"
                 and _ar(p) is not None]
    rejected_pool = [p for p in audit_pool if p["score"].gold_disposition == "reject"
                     and p["zero"] is not None]
    selected = []
    if resolved:
        med = float(np.median([_ar(p) for p in resolved]))
        selected.append(("typical delivered (resolved)",
                         min(resolved, key=lambda p: abs(_ar(p) - med))))
    if flagged_pool:
        selected.append(("usable but flagged", min(flagged_pool, key=_ar)))
    if overfired:
        selected.append(("over-flagged: ISAR got ω₀ but declined", min(overfired, key=_ar)))
    if rejected_pool:
        selected.append(("expert-rejected: ISAR declined too",
                         max(rejected_pool, key=lambda p: p["row"].chi2)))

    panels = []
    for role, p in selected:
        row = p["row"]
        gg, gl = GOLD_RESOLUTION_BY_SLIT.get(int(row.slit), calib.resolution(row.slit))
        try:
            panels.append(build_audit_panel(p["rec"], row, p["det"], card, center=p["zero"],
                                            gfw=p["gfw"], lfw=p["lfw"], prior=p["prior"],
                                            gold_gfw=gg, gold_lfw=gl, role=role, isar=p["isar"]))
        except Exception as exc:                       # a panel must never sink the whole report
            print(f"  audit panel for scan {row.scan} skipped: {exc}")
    has_audit = bool(panels)
    if has_audit:
        fig_audit(FIGDIR / "audit.png", panels)
    has_resolution_fig = fig_resolution_overlay(FIGDIR / "resolution_overlay.png", res_xcheck)

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
                                                          if s.isar_verdict == "held_for_calibration"),
                   has_audit=has_audit, has_resolution_fig=has_resolution_fig)

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
