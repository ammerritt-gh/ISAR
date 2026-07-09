#!/usr/bin/env python
"""Phase-3 step-8: second-instrument gold-standard scoring on IN8 (TAS) — the generalization guard (§10).

Step 6 scored the full Phase-3 pipeline against 83 curated FeSe IXS fits on ONE instrument (S30).
`archive/PHASE_3_DESIGN.md` §10 is explicit that this is necessary but not sufficient: the "generalizes across
instruments" claim is untested for the analysis layer until it is run on a structurally-different
instrument. This is that test — the SAME pipeline (structure loop → frequentist errors → model selection
→ verdict → confidence) on real ILL IN8 triple-axis numors, scored against a small hand-vetted anchor set.

What is different on TAS (and handled here):
  - **energy zero** is absolute (fixed-kf), so `center=0` — no Bragg self-calibration (unlike S30).
  - **resolution** is computed per scan by Cooper-Nathans from the numor header (isar.synth.resolution),
    not measured from an elastic line.
  - **a broad quasi-elastic component** is admitted (the IN8 card declares `quasielastic_fwhm`): Fe2Mo3O8
    has a real magnetic quasi-elastic tail (§8) that a single DHO leaves in the residual. This driver
    reports the fit cost with the QE component available vs suppressed, as the evidence it earns its place.
  - **ω₀ before Γ**: the 1-D Voigt omits the 4-D TAS focusing (open decision D5), so Γ is reported as
    resolution-limited. The pass condition is ω₀, never Γ.
  - **prior-free**: the Fe2Mo3O8 scans are scattered Q at different T (no dense dispersion branch), so no
    leave-one-out prior is built — the deterministic grid+NNLS carries the fit (§3).

HONEST SCOPE: this is a generalization *smoke test*, not a statistical validation. The anchor set is a
handful of scans and only 055182 (≈4.2 meV, hand-vetted) and 055183 (aborted) are vetted truth; the other
`phonon_energy`/`Quality` in IN8_fit_results.csv are ISAR Phase-1 placeholders the DIR-397 owner must
confirm. The 83-scan IXS gold (step 6) remains the statistical gate. The scoring *logic* is unit-tested in
tests/analyze/test_gold.py; this is a local measurement script (house practice), not a CI pytest.

Run from the repo root:

    python docs/phase3_gold_in8_check.py            # full anchor set + sweep, M=200
    python docs/phase3_gold_in8_check.py --smoke    # M=60 (fast)

Outputs (under the gitignored out/phase3_gold_in8/):
    results_in8_gold.csv   per scan: gold omega0/quality, ISAR verdict/confidence/omega0/interval, cost/dof
    figures/anchors.png     data + ISAR fit (components) + pull for the anchors
    figures/omega0_scatter.png   gold vs ISAR omega0 on the scored set
    FINDINGS.md            recovery, safety, the QE-earns-its-parameter evidence, honest caveats, gate
"""
from __future__ import annotations

import csv
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

for _stream in (sys.stdout, sys.stderr):        # Windows consoles default to cp1252; the report uses ω/Δ/≤
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:                           # noqa: BLE001
        pass

import matplotlib                                          # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt                            # noqa: E402

from isar.analyze import errors, gold, loop, select, verdict   # noqa: E402
from isar.analyze.context import ScanContext               # noqa: E402
from isar.analyze.structures import enumerate_structures   # noqa: E402
from isar.parsers import ill_in8                           # noqa: E402
from isar.report import captions, findings                # noqa: E402
from isar.schema import axes                               # noqa: E402
from isar.synth import resolution                          # noqa: E402

# --------------------------------------------------------------------------- config
IN8_DIR = REPO / "test_data" / "ILL IN8"
GOLD_CSV = IN8_DIR / "IN8_fit_results.csv"
OUT = REPO / "out" / "phase3_gold_in8"
FIGDIR = OUT / "figures"
BOOT_M = 200

MONITOR_FRAC = 0.9      # drop a point whose monitor counted short (truncated tail; e.g. 055182 pt 25)
MIN_FIT_POINTS = 6      # need > len(params)=5 kept points for a positive dof (e.g. 055183 is 3 pts)

# The vetted anchors and their gate (the only evidentiary rows; see the module docstring).
VETTED_RECOVERY = 55182         # hand-vetted ~4.2 meV phonon — the recovery anchor
VETTED_REJECT = 55183           # 3-point aborted — the safety anchor (ISAR must not resolve)
GATE_ANCHOR_RESID = 0.5         # meV, |ISAR - gold| tolerance on the vetted recovery anchor
GATE_MAX_SAFETY_VIOLATIONS = 0
ANCHOR_OVERLAYS = (55182, 55181, 55289)   # clean / two-feature / low-T QE-collapse


# --------------------------------------------------------------------------- context + pipeline

def tas_context(rec, card, *, prior=None):
    """A ScanContext for one IN8 measurement: center=0 (absolute axis), Cooper-Nathans resolution
    from the header, and the monitor-truncation folded into the mask (never destroyed, just skipped)."""
    gfw = resolution.cn_energy_fwhm(rec, w=0.0)
    sp = rec.spectra["det0"]
    mask = np.asarray(sp.mask, dtype=bool)
    mon = np.asarray(rec.normalization.monitor, dtype=float)
    preset = float(rec.normalization.preset)
    if rec.normalization.counting_mode == "monitor" and preset > 0:
        mask = mask & (mon >= MONITOR_FRAC * preset)
    ctx = ScanContext.from_scan(rec, "det0", card, center=0.0, gauss_fwhm=gfw, lorentz_fwhm=0.0,
                                prior_omega=(prior.omega0 if prior else None))
    return replace(ctx, mask=mask), gfw


def run_pipeline(ctx, *, M=BOOT_M, seed=0, prior=None):
    """The full Phase-3 chain on one context → (gold.IsarResult, extras). Mirrors the step-6 driver:
    fit admissible structures → attach frequentist errors to the survivors → model selection → verdict."""
    results = loop.fit_admissible_structures(ctx, minos=True)
    fits = {r.label: r for r in results}
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}

    prelim = select.select_models(results, stamp=False)
    for label in prelim.survivors:
        r = fits.get(label)
        if r is not None and r.optimizer_converged and label in by_hyp:
            errors.attach(r, by_hyp[label], ctx, M=M, seed=seed, with_profile=True)
    sel = select.select_models(results)
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
    extras = {"cost_per_dof": (best.cost_per_dof if best else float("nan")),
              "best_label": sel.best, "gamma": (best.gamma if best else float("nan")),
              "reasons": "; ".join(sv.reasons)}
    return isar, extras


def best_fit_summary(ctx):
    """Deterministic best-model summary (no bootstrap): (best_label, cost_per_dof, omega0). Used for
    the QE-on-vs-off evidence — does admitting the quasi-elastic component earn its parameter?"""
    results = loop.fit_admissible_structures(ctx, minos=False)
    fits = {r.label: r for r in results}
    sel = select.select_models(results, stamp=False)
    best = fits.get(sel.best) if sel.best else None
    return (sel.best,
            best.cost_per_dof if best is not None else float("nan"),
            best.omega0 if best is not None else None)


def undersampled_result(ctx):
    """A non-result for a scan with too few kept points to fit (e.g. the 3-point 055183)."""
    return gold.IsarResult(verdict="underpowered", confidence=0.0, omega0=None), \
        {"cost_per_dof": float("nan"), "best_label": None, "gamma": float("nan"),
         "reasons": f"undersampled: <{MIN_FIT_POINTS} kept points"}


# --------------------------------------------------------------------------- figures

_COMPONENT_COLORS = {"phonon": "C0", "elastic": "C2", "qe": "C4", "bg": "0.5"}


def _reconstruct(ctx, xf):
    """ISAR's best-fit total, per-component curves, and in-window pull (no bootstrap)."""
    results = loop.fit_admissible_structures(ctx, minos=False)
    fits = {r.label: r for r in results}
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}
    sel = select.select_models(results, stamp=False)
    best = fits.get(sel.best) if sel.best else None
    if best is None or sel.best not in by_hyp:
        return None
    comp = by_hyp[sel.best].build()
    th = np.array([best.params[n] for n in comp.param_names()], dtype=float)
    x = np.asarray(ctx.x_used, dtype=float)
    counts = np.asarray(ctx.counts_used, dtype=float)
    err = np.sqrt(np.maximum(counts, 1.0))
    return {"x": x, "counts": counts, "err": err, "total": comp.evaluate(xf, th, ctx),
            "comps": comp.evaluate_components(xf, th, ctx),
            "pull": (counts - comp.evaluate(x, th, ctx)) / err,
            "label": sel.best, "omega0": best.omega0}


def fig_anchors(path, panels):
    n = len(panels)
    fig, axs = plt.subplots(2, n, figsize=(4.6 * n, 5.6), sharex="col",
                            gridspec_kw={"height_ratios": [3, 1]}, squeeze=False)
    for j, p in enumerate(panels):
        a0, a1 = axs[0, j], axs[1, j]
        rc, xf = p["rc"], p["xf"]
        a0.errorbar(rc["x"], rc["counts"], yerr=rc["err"], fmt="o", ms=3, color="k", label="data", zorder=5)
        a0.plot(xf, rc["total"], "-", color="C1", lw=2.0, zorder=4, label=f"ISAR fit ({rc['label']})")
        for label, vals in rc["comps"]:
            a0.plot(xf, vals, "--", lw=1.0, alpha=0.8, color=_COMPONENT_COLORS.get(label, "0.6"),
                    label=label)
        a0.set_title(f"{p['scan']} — {p['role']}\nISAR ω₀={_fmt(rc['omega0'], 2)} meV", fontsize=8)
        a0.legend(fontsize=6, loc="upper right")
        if j == 0:
            a0.set_ylabel("counts")
        a1.axhline(0, color="0.6", lw=0.8)
        for s in (1.0, -1.0):
            a1.axhline(s, color="0.85", lw=0.6)
        a1.plot(rc["x"], rc["pull"], "s-", ms=2.5, color="C1")
        a1.set_xlabel("energy transfer (meV)")
        if j == 0:
            a1.set_ylabel("pull")
    captions.save_captioned(fig, path, "in8_gold/anchors")


def fig_scatter(path, scores):
    fig, ax = plt.subplots(figsize=(5.2, 5.0))
    hi = 1.0
    for s in scores:
        if s.isar_omega0 is None or s.gold_omega0 is None or not np.isfinite(s.gold_omega0):
            continue
        ax.scatter(s.gold_omega0, s.isar_omega0, s=40,
                   color=("crimson" if s.quality.upper() == "REJECT" else "steelblue"),
                   marker=("x" if s.quality.upper() == "REJECT" else "o"))
        ax.annotate(str(s.scan), (s.gold_omega0, s.isar_omega0), fontsize=6,
                    textcoords="offset points", xytext=(4, 4))
        hi = max(hi, s.gold_omega0, s.isar_omega0)
    ax.plot([0, hi], [0, hi], "k--", lw=0.8)
    ax.set_xlabel("gold ω₀ (meV)"); ax.set_ylabel("ISAR ω₀ (meV)")
    ax.set_title("ISAR vs gold energy (dashed = exact); only 055182 is vetted")
    captions.save_captioned(fig, path, "in8_gold/scatter")


# --------------------------------------------------------------------------- output helpers

def _fmt(x, n=3):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{n}f}"


def _round(x, n):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return ""
    return round(float(x), n)


def write_csv(path, recs):
    fields = []
    for r in recs:
        for k in r:
            if k not in fields:
                fields.append(k)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="")
        w.writeheader(); w.writerows(recs)


def write_findings(path, report, scores, csv_recs, qe_rows, gate, *, anchor_resid, has_anchor_fig):
    n_ok = sum(1 for s in scores if s.gold_disposition == "accept")
    n_delivered_ok = sum(1 for s in scores
                         if s.gold_disposition == "accept" and gold.delivered(s.isar_disposition))
    # scans where ISAR produced an energy at all (regardless of the verdict bucket) — the honest
    # "recovered ω₀" set, which includes scans the verdict layer over-flagged to junk.
    recovered = [s for s in scores if s.gold_disposition == "accept"
                 and s.isar_omega0 is not None and s.omega0_abs_resid is not None]
    max_recovered_resid = max((s.omega0_abs_resid for s in recovered), default=None)
    d = findings.FindingsDoc("Second-instrument check — does the SAME pipeline work on IN8 (triple-axis)?")

    d.para(f"ISAR ran its full automatic Phase-3 pipeline (find the structure → fit → error bars → model "
           f"selection → verdict) on **{report.n} real IN8 triple-axis scans** of Fe₂Mo₃O₈ (DIR-397), the "
           f"same pipeline scored against 83 FeSe IXS scans in step 6. The question is narrow and "
           f"important: does the analysis layer that works on one instrument (S30 IXS) also run and "
           f"recover ω₀ on a *structurally different* one (IN8 TAS)? A development check on old data.")

    d.section("What this checks — and what it does NOT claim")
    d.para("This is a **generalization smoke test, not a statistical validation.** The anchor set is a "
           "handful of scans, and only **055182** (≈4.2 meV, hand-vetted) and **055183** (a 3-point "
           "aborted scan) are vetted truth; the other gold energies are ISAR Phase-1 placeholders the "
           "experiment owner must confirm. The 83-scan IXS gold (step 6) stays the statistical gate. On "
           "TAS the pass condition is **ω₀, never Γ**: the 1-D resolution omits the 4-D triple-axis "
           "focusing (the 4-D engine is future work), so every Γ here is resolution-limited.")

    d.section("Did it run and recover ω₀ on the second instrument?")
    d.bullets([
        f"**Recovered ω₀ on the vetted anchor 055182: |ISAR − gold| = {_fmt(anchor_resid, 2)} meV** "
        f"(gate ≤ {GATE_ANCHOR_RESID}).",
        f"**All {len(recovered)} converging accepted scans recovered ω₀ to within "
        f"{_fmt(max_recovered_resid, 2)} meV** of the (mostly provisional) gold — including scans the "
        f"verdict layer over-flagged to junk (see limitations); the energy is right even there.",
        f"**Delivered (resolved/flagged) a usable energy for {n_delivered_ok} of {n_ok}** accepted "
        f"anchor scans; the rest recovered ω₀ but were parked in the manual pile.",
        f"**Safety: {len(report.safety_violations)}** rejected scans were confidently resolved "
        f"(target 0) {report.safety_violations or ''}.",
    ])

    if has_anchor_fig:
        d.section("See it on real triple-axis data")
        d.para("The clean anchor (055182), the messy two-feature scan (055181), and a low-T scan whose "
               "feature is really the broad magnetic quasi-elastic tail (055289). Data, ISAR's fit broken "
               "into components, and the pull residual:")
        d.figure_block("in8_gold/anchors", "figures/anchors.png")

    d.section("The quasi-elastic component earns its parameter (§8)")
    d.para("Fe₂Mo₃O₈ has a real broad magnetic quasi-elastic tail that a single damped phonon leaves in "
           "the residual (a Phase-1 finding). Step 8 admits a fixed-width quasi-elastic component on TAS "
           "and lets model selection decide per scan. Best-model fit cost with the component available vs "
           "suppressed (lower cost/dof = the tail is being modelled, not dumped in the residual):")
    def _cost(v):
        return "no usable fit" if v is None or not np.isfinite(v) else f"{v:.1f}"

    d.table(["scan", "cost/dof (no QE)", "cost/dof (QE available)", "model chosen", "ISAR ω₀ (meV)"],
            [[r["scan"], _cost(r["cost_no_qe"]), _cost(r["cost_qe"]), r["label_qe"],
              _fmt(r["omega0_qe"], 2)] for r in qe_rows])
    d.para("The fit cost drops (or a single DHO fails to fit at all) once the quasi-elastic component is "
           "available — the tail is being modelled, not dumped in the residual. ω₀ is stable regardless, "
           "consistent with '§8 ω₀ trustworthy before Γ': the component improves the fit quality (and so "
           "the verdict), not the energy.")

    d.section("Gold vs ISAR on the scored anchors")
    c = report.confusion
    d.table(["gold \\ ISAR", "resolved", "flagged", "reject", "abstain", "total"],
            [[("accept (OK)" if g == "accept" else "reject (REJECT)"),
              c.count(g, "resolved"), c.count(g, "flagged"), c.count(g, "reject"),
              c.count(g, "abstain"), c.row_total(g)] for g in ("accept", "reject")])
    d.figure_block("in8_gold/scatter", "figures/omega0_scatter.png")
    d.bullet(f"median |Δω₀| on delivered-OK: **{_fmt(report.median_abs_resid)} meV** "
             f"(includes provisional rows; only 055182 is vetted).")

    d.section("ISAR verdict buckets")
    d.bullets([f"{b}: {n}" for b, n in sorted(report.bucket_counts.items(), key=lambda kv: -kv[1])])

    d.section("Honest limitations (read these)")
    d.caveat("**Only two rows are vetted truth.** 055182 (≈4.2 meV) and 055183 (aborted) are the "
             "evidentiary anchors; the other gold energies are ISAR Phase-1 placeholders, so agreement on "
             "them is NOT independent evidence. Confirm/correct IN8_fit_results.csv before quoting any "
             "cross-instrument recovery statistic.")
    d.caveat("**Γ is resolution-limited on TAS.** The 1-D Voigt omits the 4-D triple-axis focusing "
             "(open decision D5), so every Γ here means 'did it converge', not an intrinsic linewidth. "
             "The 4-D ellipsoid + local dispersion is the remaining Step-8 gap.")
    d.caveat("**Prior-free.** The Fe₂Mo₃O₈ scans are scattered Q at different T, not a dense dispersion "
             "branch, so no leave-one-out prior was built — the deterministic grid+NNLS carries the fits.")
    d.caveat("**The verdict layer over-flags on TAS ('junk' over-fires).** On some scans ISAR recovered "
             "the right ω₀ but, because the broad magnetic tail leaves residual structure the fixed-width "
             "quasi-elastic doesn't fully absorb, the runs-test parked the scan in 'junk' (your manual "
             "pile) rather than delivering it — you lose a little automation, not a number, and nothing "
             "is deleted. This is the same safe-direction over-fire documented in the step-6 IXS gold, "
             "more frequent on TAS; routing a high cost from a known-missing component to "
             "'model-incomplete' instead of 'junk' (§6) is the planned refinement, not step 8.")

    d.gate(gate, heading="Sanity gate (ω₀ recovery on the vetted anchor + safety)")
    d.para("This gate is a development sanity check on old data with a thin, mostly-provisional anchor "
           "set — not a production guarantee. The IXS step-6 gold remains the statistical gate.")
    path.write_text(d.render(), encoding="utf-8")


# --------------------------------------------------------------------------- main

def main():
    smoke = "--smoke" in sys.argv
    M = 60 if smoke else BOOT_M
    OUT.mkdir(parents=True, exist_ok=True)
    FIGDIR.mkdir(parents=True, exist_ok=True)
    for p in FIGDIR.glob("*.png"):
        p.unlink()

    card = ill_in8.card_template()
    gold_rows = gold.load_gold(GOLD_CSV)

    # Parse every bs-qh measurement numor once (by scan number).
    by_scan = {}
    for path in sorted(p for p in IN8_DIR.iterdir() if p.name.isdigit()):
        try:
            for rec in ill_in8.parse_file(path, card):
                if rec.scan_type == "measurement":
                    by_scan[rec.scan_number] = rec
        except Exception as exc:                              # noqa: BLE001
            print(f"  {path.name}: SKIP parse ({exc})")

    print(f"== Phase-3 step 8: IN8 gold-standard scoring ({len(gold_rows)} anchors, M={M}) ==")
    scores, csv_recs, missing = [], [], []
    for row in sorted(gold_rows, key=lambda r: r.scan):
        rec = by_scan.get(row.scan)
        if rec is None:
            missing.append(row.scan); continue
        choice = gold.select_analyzer(rec, row)              # single detector -> det0
        ctx, gfw = tas_context(rec, card)
        if int(ctx.mask.sum()) < MIN_FIT_POINTS:
            isar, extra = undersampled_result(ctx)
        else:
            isar, extra = run_pipeline(ctx, M=M, seed=8000 + row.scan)
        s = gold.score_row(row, isar)
        scores.append(s)
        viol = " !SAFETY" if s.safety else ""
        print(f"  scan#{row.scan} {choice.detector_id} res={gfw:.2f} gold={row.quality:<6} "
              f"omega0={_fmt(row.omega0, 2):>5} -> {isar.verdict:<18} conf={isar.confidence:.2f} "
              f"omega0={_fmt(isar.omega0, 2)} dW={_fmt(s.omega0_abs_resid, 2)}{viol}")
        csv_recs.append({
            "scan": row.scan, "label": row.label, "gold_omega0": _round(row.omega0, 3),
            "gold_quality": row.quality, "res_gauss_fwhm": round(gfw, 3),
            "isar_verdict": isar.verdict, "isar_confidence": round(isar.confidence, 3),
            "isar_omega0": _round(isar.omega0, 4),
            "isar_interval": (f"[{isar.interval[0]:.3f},{isar.interval[2]:.3f}]" if isar.interval else ""),
            "isar_gamma_reslimited": _round(extra.get("gamma"), 4),
            "isar_disposition": s.isar_disposition, "gold_disposition": s.gold_disposition,
            "agree": ("" if s.agree is None else int(s.agree)),
            "omega0_abs_resid": _round(s.omega0_abs_resid, 4),
            "cost_per_dof": _round(extra.get("cost_per_dof"), 2), "best_model": extra.get("best_label"),
            "notes": s.notes,
        })

    report = gold.aggregate(scores)

    # QE-earns-its-parameter evidence: best-model cost/dof with the quasi-elastic suppressed vs available.
    qe_rows = []
    for scan in (55182, 55181, 55186, 55194):
        rec = by_scan.get(scan)
        if rec is None:
            continue
        ctx, _ = tas_context(rec, card)
        if int(ctx.mask.sum()) < MIN_FIT_POINTS:
            continue
        label_no, cost_no, _ = best_fit_summary(replace(ctx, quasielastic_fwhm=None))
        label_qe, cost_qe, omega0_qe = best_fit_summary(ctx)
        qe_rows.append({"scan": scan, "cost_no_qe": cost_no, "cost_qe": cost_qe,
                        "label_qe": label_qe, "omega0_qe": omega0_qe})
        print(f"  QE evidence {scan}: no-QE cost/dof={_fmt(cost_no, 1)} ({label_no}) -> "
              f"QE cost/dof={_fmt(cost_qe, 1)} ({label_qe})")

    # Anchor overlays.
    panels = []
    roles = {55182: "clean anchor (vetted)", 55181: "messy two-feature",
             55289: "low-T (QE tail)"}
    for scan in ANCHOR_OVERLAYS:
        rec = by_scan.get(scan)
        if rec is None:
            continue
        ctx, _ = tas_context(rec, card)
        if int(ctx.mask.sum()) < MIN_FIT_POINTS:
            continue
        xu = np.asarray(ctx.x_used, dtype=float)
        xf = np.linspace(float(xu.min()), float(xu.max()), 400)
        rc = _reconstruct(ctx, xf)
        if rc is not None:
            panels.append({"scan": scan, "role": roles.get(scan, ""), "rc": rc, "xf": xf})
    has_anchor_fig = bool(panels)
    if has_anchor_fig:
        fig_anchors(FIGDIR / "anchors.png", panels)
    fig_scatter(FIGDIR / "omega0_scatter.png", scores)

    # Gate: recovery on the vetted anchor + safety.
    anchor = next((s for s in scores if s.scan == VETTED_RECOVERY), None)
    anchor_resid = anchor.omega0_abs_resid if anchor else None
    recovered = anchor_resid is not None and anchor_resid <= GATE_ANCHOR_RESID
    gate = [
        (f"ω₀ recovery on the vetted anchor {VETTED_RECOVERY} (|Δω₀| ≤ {GATE_ANCHOR_RESID} meV)",
         bool(recovered), f"|Δω₀| = {_fmt(anchor_resid, 2)} meV"),
        ("safety (no clean resolve on a gold-REJECT)",
         len(report.safety_violations) <= GATE_MAX_SAFETY_VIOLATIONS,
         f"{len(report.safety_violations)} violations {report.safety_violations}"),
    ]

    write_csv(OUT / "results_in8_gold.csv", csv_recs)
    write_findings(OUT / "FINDINGS.md", report, scores, csv_recs, qe_rows, gate,
                   anchor_resid=anchor_resid, has_anchor_fig=has_anchor_fig)

    print(f"\n== Summary ==  delivered "
          f"{sum(1 for s in scores if s.gold_disposition == 'accept' and gold.delivered(s.isar_disposition))}"
          f"/{sum(1 for s in scores if s.gold_disposition == 'accept')} gold-OK; "
          f"safety violations {len(report.safety_violations)}")
    if missing:
        print(f"  MISSING raw scans: {missing}")
    for name, ok, detail in gate:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    overall = all(ok for _, ok, _ in gate)
    print(f"\n{'PASS' if overall else 'FAIL'}: step-8 IN8 gold-standard scoring (wrote {OUT / 'FINDINGS.md'})")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
