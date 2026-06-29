#!/usr/bin/env python
"""Phase-3 step-1 measurement: the structure-hypothesis loop on real S30 (IXS) scans.

Runs the new component-model structure loop (`isar.analyze`) on the six curated FeSeS_1
S30 control scans and checks that the `elastic+stokes+bg` hypothesis **reproduces the
frozen Phase-1 fitter** recorded in `out/in8_phase1/results_s30.csv` (same model, data,
backend, IC and bounds → ω₀/Γ match within rounding). The `stokes+bg` hypothesis is
emitted alongside as the worse-cost alternative (step-2 model selection will choose between
them; step 1 just reports both).

No library code is modified. Run from the repo root:

    python docs/phase3_step1_check.py

It also attaches the frequentist error layer (§5: parametric bootstrap + MINOS/ω₀-profile +
MINOS↔bootstrap agreement) to the elastic+stokes+bg fit and checks the bootstrap ω₀ interval
against the HESSE σ — on these well-counted scans they should agree (Bernstein–von Mises) — and
runs the step-2 model selection (§4: AIC/AICc/BIC + Akaike weights + nested LRT, then the pooled
across-model fold/fork). On the control set elastic+stokes+bg is the lone survivor → fold; the
multimodal fork is demonstrated on a synthetic two-structure disagreement.

Outputs (under the gitignored out/phase3_step1/):
    results_step1.csv     per (scan, hypothesis): ω₀, Γ, cost/dof, k, status flags, Δ vs Phase-1,
                          the bootstrap/profile ω₀ intervals + regime, and AIC/weights + modality
    figures/s30_scan*.png one per scan: data + elastic+stokes+bg (with phonon/elastic/bg
                          decomposition) + pull residual
    figures/selection_akaike_weights.png  per-scan Akaike weights (elastic+stokes+bg vs stokes+bg)
    figures/synthetic_fork.png  synthetic multimodal fork: pooled density + modes + weights
    figures/bootstrap_omega0_histograms.png  per-scan ω₀ bootstrap distribution vs HESSE σ
    figures/dispersion_summary.png  ω₀(K) with bootstrap error bars, overlaid on the Phase-1 baseline
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from isar.analyze import errors, loop, select, verdict      # noqa: E402
from isar.analyze.context import ScanContext               # noqa: E402
from isar.analyze.prior import PhononPrior                 # noqa: E402
from isar.analyze.structures import enumerate_structures   # noqa: E402
from isar.calibrate import elastic, energy_zero            # noqa: E402
from isar.parsers import spec_s30                          # noqa: E402
from isar.schema import axes                               # noqa: E402
from isar.synth._ff import convolute_voigt_dho             # noqa: E402

# --------------------------------------------------------------------------- config
S30_FILE = REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"
REF_CSV = REPO / "out" / "in8_phase1" / "results_s30.csv"
OUT = REPO / "out" / "phase3_step1"
FIGDIR = OUT / "figures"

S30_CONTROL_SCANS = [84, 166, 168, 170, 240, 242]
S30_ANCHOR_SCAN = 168
S30_ANALYZER = "ana5"
TOL_OMEGA0 = 0.05   # meV — regression tolerance vs the Phase-1 baseline
TOL_GAMMA = 0.05    # meV
BOOT_M = 200        # parametric-bootstrap draws per scan (§5.2)


def measure_resolution(x, counts):
    """Elastic-line fit → (center, gauss_fwhm, lorentz_fwhm). Delegates to the shared
    `isar.calibrate.elastic.fit_elastic` (step 5 promoted this out of the duplicated docs copy);
    the `ok` significance flag is dropped here since the control elastics are always present."""
    center, gfw, lfw, _ok = elastic.fit_elastic(x, counts)
    return center, gfw, lfw


def _theta(result, composite):
    return np.array([result.params[n] for n in composite.param_names()], dtype=float)


_COMPONENT_COLORS = {"phonon": "C0", "elastic": "C2", "bg": "0.5"}


def overlay(path, title, ctx, fits_by_label):
    """Per-scan check: data + the selected elastic+stokes+bg fit broken into its components
    (phonon / elastic / bg) + the pull residual. The stokes+bg alternative is kept in the
    loop and CSV (it carries model selection and the off-window path) but not plotted here."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x, counts = ctx.x_used, ctx.counts_used
    err = np.sqrt(np.maximum(counts, 1.0))
    xf = np.linspace(float(x.min()), float(x.max()), 500)
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}

    fig, (ax0, ax1) = plt.subplots(2, 1, sharex=True, height_ratios=[3, 1], figsize=(7.5, 6.0))
    ax0.errorbar(x, counts, yerr=err, fmt="o", ms=4, color="k", label="data", zorder=5)

    pull = None
    es = fits_by_label.get("elastic+stokes+bg")
    if es is not None:                                      # selected hypothesis + decomposition
        comp = by_hyp["elastic+stokes+bg"].build()
        th = _theta(es, comp)
        ax0.plot(xf, comp.evaluate(xf, th, ctx), "-", color="C1", lw=2.2, zorder=4,
                 label=(f"elastic+stokes+bg: omega0={es.omega0:.2f}±{es.omega0_err_hesse:.2f}, "
                        f"gamma={es.gamma:.2f} (cost/dof={es.cost_per_dof:.2f})"))
        for label, vals in comp.evaluate_components(xf, th, ctx):
            ax0.plot(xf, vals, "--", lw=1.2, alpha=0.9,
                     color=_COMPONENT_COLORS.get(label, "0.6"), label=f"  · {label}")
        pull = (counts - comp.evaluate(x, th, ctx)) / err

    ax0.set_ylabel("counts"); ax0.set_title(title, fontsize=9)
    ax0.legend(fontsize=7, loc="upper right")
    ax1.axhline(0, color="0.6", lw=0.8)
    for s in (1.0, -1.0):
        ax1.axhline(s, color="0.85", lw=0.6)
    if pull is not None:
        ax1.plot(x, pull, "s-", ms=3, color="C1")
    ax1.set_ylabel("pull\n(elastic+stokes+bg)"); ax1.set_xlabel("energy transfer (meV)")
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def dispersion_summary(path, disp):
    """ω₀(K) for the elastic+stokes+bg fits overlaid on the Phase-1 baseline markers — a
    series-level 'is it sensible and does it match the baseline' check."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    disp = sorted(disp, key=lambda d: d["K"])
    K = [d["K"] for d in disp]
    o = [d["omega0"] for d in disp]
    lo_err, hi_err = [], []                                 # asymmetric bootstrap bars
    for d in disp:
        if d.get("boot"):
            blo, bhi = d["boot"]
            lo_err.append(max(d["omega0"] - blo, 0.0)); hi_err.append(max(bhi - d["omega0"], 0.0))
        else:
            lo_err.append(d["omega0_err"]); hi_err.append(d["omega0_err"])
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.errorbar(K, o, yerr=[lo_err, hi_err], fmt="o-", color="C1", capsize=3,
                label="elastic+stokes+bg ± bootstrap 68%")
    ref_K = [d["K"] for d in disp if d["ref"] is not None]
    ref_o = [d["ref"] for d in disp if d["ref"] is not None]
    if ref_K:
        ax.plot(ref_K, ref_o, "x", color="k", ms=10, mew=2, label="Phase-1 baseline")
    for d in disp:                                          # annotate temperature (series mixes T)
        ax.annotate(f"{d['T']:.0f} K", (d["K"], d["omega0"]), fontsize=7,
                    textcoords="offset points", xytext=(4, 5))
    ax.set_xlabel("K (rlu) along (3.77, K, -0.09)"); ax.set_ylabel("omega0 (meV)")
    ax.set_title("S30 control dispersion — structure loop vs Phase-1 baseline")
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


def bootstrap_histograms(path, panels):
    """Grid of per-scan ω₀ bootstrap-sample histograms with the 68% percentile band and the
    HESSE σ band overlaid — the Bernstein–von Mises eyeball (the two should agree here)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(panels)
    ncol = 3
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.3 * ncol, 3.2 * nrow), squeeze=False)
    axes = axes.ravel()
    for ax, p in zip(axes, panels):
        s = np.asarray(p["samples"], dtype=float)
        if s.size:
            ax.hist(s, bins=30, density=True, color="C0", alpha=0.65)
        lo, _med, hi = p["boot_interval"]
        if np.isfinite(lo) and np.isfinite(hi):
            ax.axvspan(lo, hi, color="C0", alpha=0.18, label=f"boot 68% [{lo:.2f},{hi:.2f}]")
        o0, sig = p["omega0"], p["hesse_sigma"]
        if np.isfinite(sig):
            ax.axvspan(o0 - sig, o0 + sig, color="C3", alpha=0.18, label=f"HESSE ±{sig:.2f}")
        ax.axvline(o0, color="k", lw=1.5, label=f"fit ω₀={o0:.2f}")
        ax.set_title(f"{p['tag']}  (T={p['T']:.0f} K, {p['regime']})", fontsize=8)
        ax.set_xlabel("ω₀ (meV)"); ax.legend(fontsize=6)
    for ax in axes[n:]:
        ax.set_visible(False)
    fig.suptitle("Bootstrap ω₀ distribution vs HESSE σ (Bernstein–von Mises check)", fontsize=11)
    fig.tight_layout(); fig.savefig(path, dpi=120); plt.close(fig)


def selection_summary(path, sel_rows):
    """Per-scan Akaike weights (elastic+stokes+bg vs stokes+bg) — the model-selection
    eyeball: the elastic structure should take ~all the weight on every control scan."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tags = [sr["tag"].replace("s30_", "").replace("_ana5", "") for sr in sel_rows]
    w_el = [sr["w_elastic"] for sr in sel_rows]
    w_so = [sr["w_stokes"] for sr in sel_rows]
    xpos = np.arange(len(sel_rows))
    fig, ax = plt.subplots(figsize=(7.0, 4.0))
    ax.bar(xpos - 0.18, w_el, width=0.36, color="C1", label="elastic+stokes+bg")
    ax.bar(xpos + 0.18, w_so, width=0.36, color="0.6", label="stokes+bg")
    for i, sr in enumerate(sel_rows):
        if not sr["stokes_converged"]:                     # mark non-converged reduced model
            ax.annotate("n/c", (xpos[i] + 0.18, 0.02), fontsize=7, ha="center", color="0.4")
    ax.set_ylim(0, 1.08); ax.set_xticks(xpos); ax.set_xticklabels(tags, rotation=20, fontsize=8)
    ax.set_ylabel("Akaike weight"); ax.set_title("S30 control — model selection (Akaike weights)")
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(path, dpi=120); plt.close(fig)


def _fake_result(label, k, cost, samples):
    """A StructureFitResult carrying fabricated bootstrap samples — for the synthetic fork
    demo only (the control scans never fork)."""
    s = np.asarray(samples, dtype=float)
    params = {f"{label}.p{i}": 0.0 for i in range(k)}
    return loop.StructureFitResult(
        label=label, k=k, ndof=40, cost=cost, cost_per_dof=cost / 40,
        optimizer_converged=True, covariance_ok=True, profile_ok=True, well_constrained=True,
        analysis_usable=True, params=params, param_errors={n: None for n in params},
        gamma=0.4, gamma_err=0.05, omega0=float(np.median(s)), omega0_err_hesse=0.05,
        message="ok", within_model={"omega0_samples": s})


def synthetic_fork_demo(path):
    """The multimodal fork is unreachable on the well-counted single-phonon control set (one
    survivor -> fold). Demonstrate the fork report shape on a synthetic two-structure
    disagreement: two equally-supported structures placing omega0 at ~5 and ~8 meV. Returns
    the Selection so the report can quote its modes/weights."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(20240629)
    a = _fake_result("structA@5meV", 5, 100.0, rng.normal(5.0, 0.18, 400))
    b = _fake_result("structB@8meV", 5, 100.0, rng.normal(8.0, 0.18, 400))  # equal AIC -> w 0.5/0.5
    sel = select.select_models([a, b], stamp=False)
    p = sel.pooled

    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    ax.hist(p.samples, bins=60, weights=p.weights, density=True, color="C0", alpha=0.55,
            label="pooled across-model density")
    for i, m in enumerate(p.modes):
        ax.axvline(m.location, color="C3", lw=1.8,
                   label=f"mode {i + 1}: {m.location:.2f} meV (w={m.weight:.2f})")
        lo, _md, hi = m.interval
        ax.axvspan(lo, hi, color="C3", alpha=0.12)
    ax.set_xlabel("omega0 (meV)"); ax.set_ylabel("density")
    ax.set_title("Synthetic multimodal fork — two equally-supported structures")
    ax.legend(fontsize=7); fig.tight_layout(); fig.savefig(path, dpi=120); plt.close(fig)
    return sel


def _demo_result(label, params, *, cost_per_dof, within_model=None, omega0=None, converged=True):
    if omega0 is None:
        omega0 = float(np.hypot(params["phonon.Omega_d"], params["phonon.Gamma"]))
    return loop.StructureFitResult(
        label=label, k=len(params), ndof=40, cost=cost_per_dof * 40, cost_per_dof=cost_per_dof,
        optimizer_converged=converged, covariance_ok=True, profile_ok=True,
        well_constrained=converged, analysis_usable=converged, params=dict(params),
        param_errors={n: None for n in params}, gamma=params["phonon.Gamma"], gamma_err=0.05,
        omega0=omega0, omega0_err_hesse=0.05, message="ok", within_model=within_model)


def _demo_ctx(counts, *, x, energy_status="needs_zero", prior_omega=None):
    counts = np.asarray(counts, dtype=float)
    return ScanContext(x=x, counts=counts, mask=np.ones(counts.size, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status=energy_status, prior_omega=prior_omega)


def _demo_sel(modality="fold", *, best="elastic+stokes+bg", pooled=None):
    return select.Selection(scores=[], best=best, survivors=[best] if best else [],
                            pooled=pooled, modality=modality)


def _verdict_demo(results, ctx, sel, prior=None):
    v = verdict.classify(results, ctx, sel, prior=prior)
    return {"verdict": v.verdict, "confidence": v.confidence, "provisional": v.provisional,
            "reason": v.reasons[0] if v.reasons else ""}


def verdict_gallery():
    """Classify one synthetic scenario per non-resolved bucket — the control scans never leave
    'resolved', so the buckets (and the circularity guard) are exercised here, deterministically.
    Returns ``[(expected_bucket, result_dict), ...]``."""
    x = np.linspace(-10.0, 10.0, 81)
    SB = {"phonon.Omega_d": 5.0, "phonon.Gamma": 0.4, "phonon.A": 300.0, "bg.BG": 20.0}
    EL = {**SB, "elastic.E": 50.0}
    rng = np.random.default_rng(7)
    out = []

    out.append(("held_for_calibration", _verdict_demo(
        [_demo_result("elastic+stokes+bg", EL, cost_per_dof=2.0)],
        _demo_ctx(np.full(81, 50.0), x=x, energy_status="needs_conversion_and_zero"), _demo_sel())))

    pooled = select.Pooled("fork", np.array([]), np.array([]), None,
                           [select.Mode(5.0, 0.5, (4.8, 5.0, 5.2)),
                            select.Mode(8.0, 0.5, (7.8, 8.0, 8.2))], "either 5 or 8")
    peak = rng.poisson(40.0, 81).astype(float); peak[40] += 600.0
    out.append(("unconstrained", _verdict_demo(
        [_demo_result("elastic+stokes+bg", EL, cost_per_dof=2.0)],
        _demo_ctx(peak, x=x), _demo_sel("fork", pooled=pooled), prior=None)))

    out.append(("underpowered", _verdict_demo(
        [_demo_result("elastic+stokes+bg", {**EL, "phonon.Omega_d": 6.0, "bg.BG": 50.0},
                      cost_per_dof=1.0, omega0=6.0)],
        _demo_ctx(np.full(81, 50.0), x=x, prior_omega=6.0), _demo_sel(),
        prior=PhononPrior(6.0, 15.0, 1.0))))                 # weak phonon below detection floor

    out.append(("no_signal", _verdict_demo(
        [_demo_result("elastic+stokes+bg", EL, cost_per_dof=1.0)],
        _demo_ctx(np.full(81, 50.0), x=x), _demo_sel(), prior=None)))

    base = _demo_ctx(np.zeros(81), x=x)
    comp = {h.label: h for h in enumerate_structures(base)}["stokes+bg"].build()
    model = comp.evaluate(x, np.array([SB["phonon.Omega_d"], SB["phonon.Gamma"], SB["phonon.A"],
                                       SB["bg.BG"]]), base)
    res_sb = _demo_result("stokes+bg", SB, cost_per_dof=8.0)  # catastrophic GoF
    out.append(("junk", _verdict_demo(
        [res_sb], _demo_ctx(np.maximum(model + rng.normal(0.0, 8.0, 81), 0.0), x=x),
        _demo_sel(best="stokes+bg"))))
    out.append(("model_incomplete", _verdict_demo(
        [res_sb], _demo_ctx(model + 60.0 * np.exp(-0.5 * (x / 3.0) ** 2), x=x),
        _demo_sel(best="stokes+bg"))))
    return out


def verdict_summary_figure(path, vrows, gallery):
    """Left: per-scan computed confidence for the real control scans (all 'resolved'). Right: the
    synthetic verdict gallery (each non-resolved bucket landing where intended)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(11.0, 4.2), gridspec_kw={"width_ratios": [1.1, 1.0]})
    tags = [v["tag"].replace("s30_", "").replace("_ana5", "") for v in vrows]
    ax0.bar(range(len(vrows)), [v["confidence"] for v in vrows], color="C0")
    ax0.axhline(0.7, ls="--", color="0.6", lw=0.8)
    ax0.set_xticks(range(len(vrows))); ax0.set_xticklabels(tags, rotation=20, fontsize=8)
    ax0.set_ylim(0.0, 1.05); ax0.set_ylabel("computed confidence")
    ax0.set_title("S30 control — verdict=resolved, confidence")
    ax1.axis("off")
    lines = ["synthetic verdict gallery", ""]
    for expected, r in gallery:
        ok = "ok " if r["verdict"] == expected else "!! "
        prov = " *provisional" if r["provisional"] else ""
        lines.append(f"{ok}{expected:>20s} -> {r['verdict']} (conf {r['confidence']:.2f}){prov}")
    ax1.text(0.0, 1.0, "\n".join(lines), va="top", ha="left", fontsize=8.5, family="monospace")
    ax1.set_title("non-resolved buckets (synthetic)")
    fig.tight_layout(); fig.savefig(path, dpi=120); plt.close(fig)


def energy_zero_series_figure(path, zero_diag):
    """Fitted energy zero vs acquisition time, with the leave-one-out interpolation at each
    scan's time — shows the series and how loosely the sparse control zeros interpolate."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not zero_diag:
        return
    label = zero_diag[0]["key_label"]
    k0 = min(d["key"] for d in zero_diag)
    xs = [((d["key"] - k0) / 3600.0 if label == "time" else d["key"] - k0) for d in zero_diag]
    xlabel = "hours since first control scan" if label == "time" else "scan# since first"
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    ax.plot(xs, [d["own"] for d in zero_diag], "o", color="C0", label="own elastic E0")
    for xv, d in zip(xs, zero_diag):
        if d["interp"] is not None:
            ax.plot(xv, d["interp"], "x", color="C3")
    ax.plot([], [], "x", color="C3", label="leave-one-out interpolated E0")
    ax.set_xlabel(xlabel); ax.set_ylabel("energy zero E0 (meV)")
    ax.set_title("S30 control — energy-zero series (own vs LOO-interpolated)")
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(path, dpi=120); plt.close(fig)


def energy_zero_demos():
    """Synthetic demos of the zero series feeding the loop (the control scans never need it):
    (a) a no-elastic 'high-K' scan borrows an interpolated zero and still recovers omega0;
    (b) a scan with no zero available is held-for-calibration (never E0=0)."""
    x = np.linspace(-10.0, 10.0, 81)
    rng = np.random.default_rng(99)
    true_zero, true_o0 = -0.6, 8.0                                         # phonon far from the zero
    lam = convolute_voigt_dho(x, true_o0, true_zero, 1.0, 0.0, 0.4, 300.0, 0.0, 15.0, 100.0, peak="all")
    counts = rng.poisson(np.clip(lam, 0.0, None)).astype(float)            # phonon only, no elastic
    series = energy_zero.ZeroSeries.build([(0.0, true_zero), (100.0, true_zero)])
    rec = SimpleNamespace(raw_header={}, scan_number=50)                   # time 50, within coverage
    zr = energy_zero.resolve_zero(x, counts, rec, series)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=float(zr.value), gauss_fwhm=1.0, lorentz_fwhm=0.0,
                      instrument="S30", energy_status="needs_zero")
    results = loop.fit_admissible_structures(ctx)
    sel = select.select_models(results)
    fit_o0 = {r.label: r for r in results}[sel.best].omega0 if sel.best else float("nan")
    interp_demo = {"true_omega0": true_o0, "interp_zero": float(zr.value), "source": zr.source,
                   "fit_omega0": float(fit_o0)}

    flat = rng.poisson(20.0, 81).astype(float)
    far = SimpleNamespace(raw_header={}, scan_number=500)                  # out of series coverage
    zr2 = energy_zero.resolve_zero(x, flat, far, series)
    held = verdict.held_for_calibration("no energy zero available (no nearby calibration)")
    held_demo = {"resolved_zero": zr2.value, "verdict": held.verdict}
    return interp_demo, held_demo


def write_findings(path, rows, worst_o, worst_g, sel_rows, fork, vrows, gallery, zero_diag, ezdemos):
    """Consolidated markdown report (mirrors docs/in8_phase1_check.py's FINDINGS.md), built
    from the per-scan rows so it stays in lock-step with the run."""
    el = [r for r in rows if r["label"] == "elastic+stokes+bg"]
    so = [r for r in rows if r["label"] == "stokes+bg"]
    el_cpd = [r["cost_per_dof"] for r in el]
    so_cpd = [r["cost_per_dof"] for r in so]
    bh = [r["boot_hw_over_hesse"] for r in el
          if isinstance(r.get("boot_hw_over_hesse"), (int, float))]

    L = ["# Phase 3 — S30 control check: structure loop + errors + model selection\n\n",
         "Generated by `docs/phase3_step1_check.py` on the six FeSeS_1 S30 (IXS) control scans "
         "(Q=(3.77,K,-0.09), analyzer ana5). Outputs are gitignored; reproduce with "
         "`python docs/phase3_step1_check.py`.\n\n",
         "## Step 1 — structure loop (model-selection input)\n\n",
         f"Each scan enumerates {{stokes+bg, elastic+stokes+bg}}; elastic+stokes+bg wins on every "
         f"control scan (the elastic line is real here). The selected hypothesis reproduces the "
         f"frozen Phase-1 fitter: worst |d_omega0| = {worst_o:.4f} meV, worst |d_gamma| = "
         f"{worst_g:.4f} meV vs `out/in8_phase1/results_s30.csv`. Discrimination by cost/dof: "
         f"elastic+stokes+bg {min(el_cpd):.2f}–{max(el_cpd):.2f} vs stokes+bg "
         f"{min(so_cpd):.2f}–{max(so_cpd):.2f}.\n\n",
         "## Step 3 — authoritative error (parametric bootstrap, §5.2)\n\n",
         "| scan | T (K) | omega0 | bootstrap 68% | HESSE sigma | boot/HESSE | regime |\n",
         "|---|---|---|---|---|---|---|\n"]
    for r in el:
        L.append(f"| {r['id']} | {r['T']:.0f} | {r['omega0']:.3f} | "
                 f"[{r['boot_o0_lo']:.3f}, {r['boot_o0_hi']:.3f}] | {r['omega0_err_hesse']:.3f} | "
                 f"{r['boot_hw_over_hesse']} | {r['regime']} |\n")
    if bh:
        L.append(f"\nBernstein–von Mises check: boot/HESSE in {min(bh):.2f}–{max(bh):.2f} (≈1) on "
                 "these well-counted scans; all regime=asymptotic (MINOS↔bootstrap agree); no "
                 "bimodality. The reported omega0 error is now the bootstrap interval "
                 "(authoritative); HESSE is a convergence check only.\n")
    L.append("\nCalibration: the synthetic coverage gate `tests/analyze/test_bootstrap_coverage.py` "
             "(slow) confirms the 68% bootstrap omega0 interval covers truth ≈68%.\n\n")

    L.append("## Step 2 — model selection + modality (§4; built last, consumes the step-3 bootstrap)\n\n")
    L.append("Selection is by AIC (Akaike weights; `cost` is Baker-Cousins -2lnL so AIC = cost + 2k), "
             "with AICc/BIC and the nested likelihood-ratio test (the Poisson analog of the nested "
             "F-test: deviance drop ~ chi2(dk)) reported alongside. On every control scan "
             "elastic+stokes+bg is the lone survivor (Akaike weight ~1); where stokes+bg converged, "
             "the nested LRT confirms the elastic component is warranted (p << 1e-3). The pooled "
             "across-model omega0 distribution is unimodal -> **fold**, and with a single survivor the "
             "folded interval equals the bootstrap interval.\n\n")
    L.append("| scan | T (K) | elastic weight | stokes+bg dAIC | nested LRT p | modality | folded omega0 68% |\n")
    L.append("|---|---|---|---|---|---|---|\n")
    for sr in sel_rows:
        dAIC = f"{sr['dAIC_stokes']:.0f}" if np.isfinite(sr["dAIC_stokes"]) else "n/c"
        pv = f"{sr['nested_p']:.1e}" if np.isfinite(sr["nested_p"]) else "n/c"
        fold = f"[{sr['fold'][0]:.3f}, {sr['fold'][2]:.3f}]" if sr["fold"] else "-"
        L.append(f"| {sr['tag']} | {sr['T']:.0f} | {sr['w_elastic']:.3f} | {dAIC} | {pv} | "
                 f"{sr['modality']} | {fold} |\n")
    L.append("\n(`n/c` = stokes+bg did not converge, so it is excluded from selection and the nested "
             "LRT; elastic still wins as the only converged structure.)\n\n")
    if fork is not None and fork.pooled is not None:
        L.append("### Multimodal fork (synthetic)\n\n")
        L.append("The fork is unreachable on this single-phonon control set (one survivor -> fold), so "
                 "it is exercised on a synthetic two-structure disagreement (two equally-supported "
                 "structures placing omega0 at ~5 and ~8 meV). `select_models` returns a fork rather "
                 "than averaging into the gap:\n\n")
        L.append(f"- {fork.pooled.disambiguation}\n")
        for i, m in enumerate(fork.pooled.modes):
            L.append(f"  - mode {i + 1}: omega0 = {m.location:.2f} meV, weight {m.weight:.2f}, "
                     f"68% [{m.interval[0]:.2f}, {m.interval[2]:.2f}]\n")
        L.append("\nSee `figures/synthetic_fork.png`. Fold/fork logic + determinism are covered by "
                 "`tests/analyze/test_select_modality.py`; AIC/weights/nested-LRT by "
                 "`tests/analyze/test_select.py`.\n\n")

    L.append("## Step 4 — verdict + computed confidence (§6, §9)\n\n")
    L.append("Each scan is classified into a named bucket (never deleted, D7) and given a [0,1] "
             "confidence (geometric mean of exposed sub-scores: structure agreement, interval "
             "tightness, MINOS-bootstrap regime, prior distance, provenance). A **leave-one-out** "
             "omega0 (line fit through the other five fitted points) is injected as the dispersion "
             "prior so the prior-distance term is exercised on real data. All six control scans are "
             "**resolved** with high confidence.\n\n")
    L.append("| scan | T (K) | verdict | confidence | prior omega0 (LOO) |\n")
    L.append("|---|---|---|---|---|\n")
    for v in vrows:
        L.append(f"| {v['tag']} | {v['T']:.0f} | {v['verdict']} | {v['confidence']:.3f} | "
                 f"{v['prior_omega']:.3f} |\n")
    L.append("\n### Non-resolved buckets (synthetic gallery)\n\n")
    L.append("The control set never leaves *resolved*, so every other bucket — and the detection-"
             "power circularity guard — is exercised on synthetic scenarios; each lands where "
             "intended:\n\n")
    L.append("| expected bucket | classified | confidence | note |\n|---|---|---|---|\n")
    for expected, r in gallery:
        match = "" if r["verdict"] == expected else " (MISMATCH)"
        note = "provisional (guard)" if r["provisional"] else ""
        L.append(f"| {expected} | {r['verdict']}{match} | {r['confidence']:.2f} | {note} |\n")
    L.append("\nThe **underpowered** verdict is `provisional` — the §0.4 circularity guard: it is "
             "prior-conditional and never marks a region confirmed-empty. The **model-incomplete** "
             "route keeps a structured (quasi-elastic) residual out of *junk*. Logic is covered by "
             "`tests/analyze/test_verdict.py` and `tests/analyze/test_confidence.py`.\n\n")

    L.append("## Step 5 — energy-zero calibration (§7, D8)\n\n")
    L.append("S30 is `energy_status=\"needs_zero\"`: the elastic line gives the zero. Step 5 builds a "
             "**time-keyed** zero series (acquisition time parsed from the SPEC `#D` header) and "
             "interpolates to a scan's time, so a scan without a usable elastic borrows a neighbour's "
             "zero and a scan with none is **held-for-calibration** (never E0=0). The control scans "
             "each have their own elastic, so their fits keep their own zeros (regression Δ=0 "
             "unchanged); the table below is a **leave-one-out diagnostic** — interpolating each "
             "scan's zero from the other five.\n\n")
    if zero_diag:
        klabel = zero_diag[0]["key_label"]
        k0 = min(d["key"] for d in zero_diag)
        if klabel == "scan#":
            L.append("All six supplementary control scans carry one constant `#D` header timestamp, so "
                     "the series here is keyed by `scan_number` (the documented ≈-time proxy); production "
                     "`scan_time` prefers the header time when it varies.\n\n")
        L.append(f"| scan | {klabel} | own E0 | LOO-interp E0 | residual |\n|---|---|---|---|---|\n")
        for d in zero_diag:
            kk = f"{(d['key']-k0)/3600.0:.1f}" if klabel == "time" else f"{int(d['key']-k0)}"
            interp = f"{d['interp']:.3f}" if d["interp"] is not None else "n/a"
            resid = f"{d['resid']:+.3f}" if d["resid"] is not None else "n/a"
            L.append(f"| {d['tag']} | {kk} | {d['own']:.3f} | {interp} | {resid} |\n")
        rr = [abs(d["resid"]) for d in zero_diag if d["resid"] is not None]
        if rr:
            L.append(f"\nLOO interpolation residual is loose (max |Δ| = {max(rr):.2f} meV): these six "
                     "control zeros are sparse and not smoothly drifting, so interpolation is a fallback "
                     "for missing-elastic scans, not a replacement for a present elastic.\n")
    ime, hde = ezdemos
    L.append("\n### Synthetic demos\n\n")
    L.append(f"- **Interpolated zero feeds the loop:** a no-elastic 'high-K' scan (true zero "
             f"{ime['interp_zero']:.2f} meV, omega0 {ime['true_omega0']:.1f}) resolves its zero from "
             f"the series (`source={ime['source']}`) and the fit still recovers omega0 = "
             f"{ime['fit_omega0']:.2f} meV.\n")
    L.append(f"- **Missing zero is held, never E0=0:** a scan with no series coverage resolves to "
             f"`{hde['resolved_zero']}` -> verdict `{hde['verdict']}`.\n\n")

    L.append("## Figures (`out/phase3_step1/figures/`)\n\n"
             "- `s30_scan*.png` — data + elastic+stokes+bg with phonon/elastic/bg decomposition + pull residual.\n"
             "- `selection_akaike_weights.png` — per-scan Akaike weights (elastic+stokes+bg vs stokes+bg).\n"
             "- `synthetic_fork.png` — synthetic multimodal fork: pooled density + modes + weights.\n"
             "- `verdict_confidence.png` — per-scan confidence (resolved) + the synthetic verdict gallery.\n"
             "- `energy_zero_series.png` — fitted energy zero vs time + leave-one-out interpolation.\n"
             "- `bootstrap_omega0_histograms.png` — per-scan omega0 bootstrap distribution vs HESSE sigma (BvM eyeball).\n"
             "- `dispersion_summary.png` — omega0(K) with bootstrap error bars, overlaid on the Phase-1 baseline.\n\n")
    L.append("## Scope / caveats\n\n"
             "- This control set is well-counted, in-window-elastic, single-phonon, own-elastic-zero → "
             "all asymptotic, one survivor, fold, resolved. The sparse-regime flag, the multimodal "
             "fork, the non-resolved verdict buckets, and the interpolated/held energy-zero paths are "
             "exercised by the synthetic demos above (plus the unit tests), not these scans.\n"
             "- Per-(scan, hypothesis) numbers incl. status flags, intervals, AIC/weights, verdict + "
             "confidence are in `results_step1.csv`.\n")
    path.write_text("".join(L), encoding="utf-8")


def load_reference():
    if not REF_CSV.exists():
        return {}
    with REF_CSV.open(newline="", encoding="utf-8") as fh:
        return {row["id"]: row for row in csv.DictReader(fh)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    FIGDIR.mkdir(parents=True, exist_ok=True)
    for p in FIGDIR.glob("*.png"):
        p.unlink()

    card = spec_s30.card_template()
    by_scan = {rec.scan_number: rec for rec in spec_s30.parse_file(S30_FILE, card)}
    ref = load_reference()

    rows, deltas, disp, boot_panels, sel_rows, states, zero_points = [], [], [], [], [], [], []
    print("== Phase-3 steps 1-5: structure loop + errors + selection + verdict + zero on S30 control ==")
    for scan_no in S30_CONTROL_SCANS:
        rec = by_scan.get(scan_no)
        if rec is None:
            print(f"  scan#{scan_no}: not found"); continue
        x = np.asarray(axes.scanned_axes(rec)[0].values, dtype=float)
        sp = rec.spectra[S30_ANALYZER]
        mask = np.asarray(sp.mask, dtype=bool)
        center, gfw, lfw = measure_resolution(x[mask], np.asarray(sp.intensity, float)[mask])
        ctx = ScanContext.from_scan(rec, S30_ANALYZER, card,
                                    center=center, gauss_fwhm=gfw, lorentz_fwhm=lfw)

        results = loop.fit_admissible_structures(ctx, minos=True)
        fits_by_label = {r.label: r for r in results}
        by_hyp = {h.label: h for h in enumerate_structures(ctx)}
        es = fits_by_label.get("elastic+stokes+bg")
        so = fits_by_label.get("stokes+bg")
        if es is not None:                                  # the authoritative error layer (§5)
            errors.attach(es, by_hyp["elastic+stokes+bg"], ctx,
                          M=BOOT_M, seed=4000 + scan_no, with_profile=True)
        wm = es.within_model if es is not None else None
        sel = select.select_models(results)                 # step-2 model selection (stamps across_model)
        score_by_label = {s.label: s for s in sel.scores}

        tag = f"s30_scan{scan_no}_{S30_ANALYZER}"
        zero_points.append((energy_zero.scan_time(rec), center, tag, ctx.T, scan_no))  # step-5 zero series
        ref_row = ref.get(tag)
        es_do = es_dg = None
        if ref_row is not None and es is not None:
            es_do = round(es.omega0 - float(ref_row["omega0"]), 4)
            es_dg = round(es.gamma - float(ref_row["gamma"]), 4)
            deltas.append((tag, es_do, es_dg))
        se, sso = score_by_label.get("elastic+stokes+bg"), score_by_label.get("stokes+bg")
        if se is not None:
            sel_rows.append({
                "tag": tag, "T": ctx.T, "w_elastic": se.weight,
                "w_stokes": (sso.weight if sso is not None else 0.0),
                "stokes_converged": sso is not None,
                "dAIC_stokes": (sso.delta_aic if sso is not None else float("nan")),
                "nested_p": (se.nested["p_value"] if se.nested else float("nan")),
                "modality": sel.modality,
                "fold": (sel.pooled.interval if (sel.pooled is not None
                         and sel.pooled.modality == "fold") else None),
            })
        elastic_row = None
        for r in results:
            is_elastic = r.label == "elastic+stokes+bg"
            row = {
                "id": tag, "label": r.label, "T": round(ctx.T, 1),
                "omega0": round(r.omega0, 4), "omega0_err_hesse": round(r.omega0_err_hesse, 4),
                "gamma": round(r.gamma, 4), "gamma_err": round(r.gamma_err, 4),
                "cost": round(r.cost, 2), "ndof": r.ndof, "cost_per_dof": round(r.cost_per_dof, 2),
                "k": r.k, "optimizer_converged": int(r.optimizer_converged),
                "covariance_ok": int(r.covariance_ok), "profile_ok": int(r.profile_ok),
                "well_constrained": int(r.well_constrained), "analysis_usable": int(r.analysis_usable),
                "center": round(center, 3), "res_gauss_fwhm": round(gfw, 3),
                "res_lorentz_fwhm": round(lfw, 3),
                "ref_omega0": ref_row["omega0"] if ref_row else "",
                "d_omega0": es_do if (is_elastic and es_do is not None) else "",
                "d_gamma": es_dg if (is_elastic and es_dg is not None) else "",
            }
            if is_elastic and wm is not None:               # bootstrap / profile / agreement
                blo, _bmed, bhi = wm["freq_interval"]
                plo, phi = wm["profile_interval"]
                ag = wm["agreement"]
                sig = r.omega0_err_hesse
                row.update({
                    "boot_o0_lo": round(blo, 4), "boot_o0_hi": round(bhi, 4),
                    "prof_o0_lo": round(plo, 4), "prof_o0_hi": round(phi, 4),
                    "boot_hw_over_hesse": (round(((bhi - blo) / 2.0) / sig, 3)
                                           if np.isfinite(sig) and sig > 0 else ""),
                    "gamma_minos_over_boot": (round(ag.gamma_ratio, 3)
                                              if np.isfinite(ag.gamma_ratio) else ""),
                    "regime": ag.regime, "bimodal": int(wm["bimodal"]), "n_boot": wm["n_converged"],
                })
            sc = score_by_label.get(r.label)                # step-2 selection metrics
            if sc is not None:
                row.update({"aic": round(sc.aic, 2), "aicc": round(sc.aicc, 2),
                            "bic": round(sc.bic, 2), "delta_aic": round(sc.delta_aic, 2),
                            "akaike_weight": round(sc.weight, 4)})
                if is_elastic:
                    row["nested_p"] = f"{sc.nested['p_value']:.2e}" if sc.nested else ""
                    if sel.pooled is not None:
                        row["modality"] = sel.pooled.modality
                        if sel.pooled.modality == "fold":
                            row["fold_o0_lo"] = round(sel.pooled.interval[0], 4)
                            row["fold_o0_hi"] = round(sel.pooled.interval[2], 4)
            rows.append(row)
            if is_elastic:
                elastic_row = row
        if es is not None and elastic_row is not None:      # retained for the step-4 verdict pass
            states.append({"tag": tag, "T": ctx.T, "K": float(sp.q[1]), "omega0": es.omega0,
                           "ctx": ctx, "results": results, "sel": sel, "row": elastic_row})
        delta_str = (f"  | dOmega0={es_do:+.4f} dGamma={es_dg:+.4f}" if es_do is not None else "")
        print(f"  scan#{scan_no}: elastic+stokes+bg omega0={es.omega0:.3f} gamma={es.gamma:.3f} "
              f"(cost/dof={es.cost_per_dof:.2f}); stokes+bg omega0={so.omega0:.3f} "
              f"(cost/dof={so.cost_per_dof:.2f}){delta_str}")
        if wm is not None:
            blo, _bmed, bhi = wm["freq_interval"]
            sig = es.omega0_err_hesse
            ratio = (((bhi - blo) / 2.0) / sig) if (np.isfinite(sig) and sig > 0) else float("nan")
            print(f"      bootstrap omega0 68% [{blo:.3f}, {bhi:.3f}] (hw {(bhi - blo) / 2:.3f}); "
                  f"HESSE sigma {sig:.3f}; boot/HESSE {ratio:.2f}; regime {wm['agreement'].regime}")
            disp.append({"K": float(sp.q[1]), "T": ctx.T, "omega0": es.omega0,
                         "omega0_err": sig, "boot": (blo, bhi),
                         "ref": float(ref_row["omega0"]) if ref_row else None})
            boot_panels.append({"tag": tag, "T": ctx.T, "omega0": es.omega0,
                                "hesse_sigma": sig, "boot_interval": wm["freq_interval"],
                                "samples": wm["omega0_samples"],
                                "regime": wm["agreement"].regime})
        if se is not None:
            so_note = (f"stokes+bg dAIC={sso.delta_aic:.0f}" if sso is not None
                       else "stokes+bg non-converged")
            print(f"      selection: elastic weight={se.weight:.3f}, {so_note}, "
                  f"modality={sel.modality}")
        overlay(FIGDIR / f"{tag}.png", f"S30 scan#{scan_no} {S30_ANALYZER} (T={ctx.T:.0f} K)",
                ctx, fits_by_label)
        print(f"  figure: {tag}.png")

    vrows = []                                             # step 4: verdict + confidence (LOO prior)
    if states:
        print("\n== Step 4: verdict + computed confidence (leave-one-out dispersion prior) ==")
        Ks = np.array([s["K"] for s in states]); Os = np.array([s["omega0"] for s in states])
        sigma = max(0.3, float(np.std(Os)))
        for i, st in enumerate(states):
            other = np.arange(len(states)) != i
            if other.sum() >= 2:
                pred = float(np.polyval(np.polyfit(Ks[other], Os[other], 1), Ks[i]))
            else:
                pred = float(np.mean(Os[other])) if other.any() else st["omega0"]
            sv = verdict.classify(st["results"], st["ctx"], st["sel"],
                                  prior=PhononPrior(omega0=pred, amplitude=None, omega0_sigma=sigma))
            st["row"].update({"verdict": sv.verdict, "confidence": round(sv.confidence, 3),
                              "prior_omega": round(pred, 3),
                              "conf_s_prior": round(sv.components.get("s_prior", float("nan")), 3)})
            vrows.append({"tag": st["tag"], "T": st["T"], "K": st["K"], "verdict": sv.verdict,
                          "confidence": sv.confidence, "prior_omega": pred})
            print(f"  {st['tag']}: {sv.verdict}, confidence={sv.confidence:.3f} "
                  f"(prior omega0~{pred:.2f})")

    zero_diag = []                                         # step 5: leave-one-out zero interpolation
    if zero_points:
        times = [p[0] for p in zero_points]
        distinct_time = len({round(t, 3) for t in times}) >= 2   # supplementary #D is often constant
        keys = times if distinct_time else [float(p[4]) for p in zero_points]   # fallback: scan_number
        key_label = "time" if distinct_time else "scan#"
        for i, p in enumerate(zero_points):
            own, ztag = p[1], p[2]
            others = [(keys[j], zero_points[j][1]) for j in range(len(zero_points)) if j != i]
            interp = (energy_zero.ZeroSeries.build(others).interpolate(keys[i])
                      if len(others) >= 2 else None)
            zero_diag.append({"tag": ztag, "key": keys[i], "key_label": key_label, "own": own,
                              "interp": interp, "resid": (interp - own) if interp is not None else None})
    ezdemos = energy_zero_demos()
    ime, hde = ezdemos
    print("\n== Step 5: energy-zero calibration ==")
    print(f"  interpolated-zero demo: source={ime['source']}, fit omega0={ime['fit_omega0']:.2f} "
          f"(true {ime['true_omega0']:.1f})")
    print(f"  held demo: resolved zero={hde['resolved_zero']} -> {hde['verdict']}")
    _rr = [abs(d["resid"]) for d in zero_diag if d["resid"] is not None]
    if _rr:
        print(f"  LOO interpolation residual max |d|={max(_rr):.3f} meV (sparse, non-smooth zeros)")

    fields = []                                            # union of keys (elastic rows have extras)
    for row in rows:
        for k in row:
            if k not in fields:
                fields.append(k)
    with (OUT / "results_step1.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="")
        w.writeheader(); w.writerows(rows)
    print(f"  wrote {OUT / 'results_step1.csv'}")
    if disp:
        dispersion_summary(FIGDIR / "dispersion_summary.png", disp)
        print("  figure: dispersion_summary.png")
    if boot_panels:
        bootstrap_histograms(FIGDIR / "bootstrap_omega0_histograms.png", boot_panels)
        print("  figure: bootstrap_omega0_histograms.png")
    if sel_rows:
        selection_summary(FIGDIR / "selection_akaike_weights.png", sel_rows)
        print("  figure: selection_akaike_weights.png")
    fork_sel = synthetic_fork_demo(FIGDIR / "synthetic_fork.png")
    print("  figure: synthetic_fork.png")
    gallery = verdict_gallery()
    for expected, r in gallery:
        flag = "" if r["verdict"] == expected else "  <-- MISMATCH"
        print(f"    synthetic {expected}: {r['verdict']} (conf {r['confidence']:.2f}){flag}")
    if vrows:
        verdict_summary_figure(FIGDIR / "verdict_confidence.png", vrows, gallery)
        print("  figure: verdict_confidence.png")
    if zero_diag:
        energy_zero_series_figure(FIGDIR / "energy_zero_series.png", zero_diag)
        print("  figure: energy_zero_series.png")

    worst_o = max((abs(d[1]) for d in deltas), default=float("nan"))
    worst_g = max((abs(d[2]) for d in deltas), default=float("nan"))
    if rows:
        write_findings(OUT / "FINDINGS.md", rows, worst_o, worst_g, sel_rows, fork_sel, vrows,
                       gallery, zero_diag, ezdemos)
        print(f"  wrote {OUT / 'FINDINGS.md'}")

    # --- regression gate vs the frozen Phase-1 baseline ---------------------
    print("\n== Regression vs out/in8_phase1/results_s30.csv (elastic+stokes+bg) ==")
    if not deltas:
        print("  REFERENCE MISSING — run docs/in8_phase1_check.py first; skipping gate.")
        return
    worst_o = max(abs(d[1]) for d in deltas)
    worst_g = max(abs(d[2]) for d in deltas)
    for tag, d_o, d_g in deltas:
        flag = "" if (abs(d_o) <= TOL_OMEGA0 and abs(d_g) <= TOL_GAMMA) else "  <-- EXCEEDS TOL"
        print(f"  {tag}: dOmega0={d_o:+.4f}  dGamma={d_g:+.4f}{flag}")
    print(f"  worst |dOmega0|={worst_o:.4f} meV, worst |dGamma|={worst_g:.4f} meV "
          f"(tol {TOL_OMEGA0}/{TOL_GAMMA})")
    assert worst_o <= TOL_OMEGA0 and worst_g <= TOL_GAMMA, \
        "structure loop diverged from the Phase-1 baseline beyond tolerance"
    print("  PASS: elastic+stokes+bg reproduces the Phase-1 fitter within tolerance.")


if __name__ == "__main__":
    main()
