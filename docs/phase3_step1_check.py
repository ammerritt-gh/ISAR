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
against the HESSE σ — on these well-counted scans they should agree (Bernstein–von Mises).

Outputs (under the gitignored out/phase3_step1/):
    results_step1.csv     per (scan, hypothesis): ω₀, Γ, cost/dof, k, status flags, Δ vs Phase-1,
                          and for elastic+stokes+bg the bootstrap/profile ω₀ intervals + regime
    figures/s30_scan*.png one per scan: data + elastic+stokes+bg (with phonon/elastic/bg
                          decomposition) + pull residual
    figures/bootstrap_omega0_histograms.png  per-scan ω₀ bootstrap distribution vs HESSE σ
    figures/dispersion_summary.png  ω₀(K) with bootstrap error bars, overlaid on the Phase-1 baseline
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from isar.analyze import errors, loop                      # noqa: E402
from isar.analyze.context import ScanContext               # noqa: E402
from isar.analyze.structures import enumerate_structures   # noqa: E402
from isar.parsers import spec_s30                          # noqa: E402
from isar.schema import axes                               # noqa: E402
from isar.synth._ff import Voigt                           # noqa: E402

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
    """Fit a Voigt (+flat BG) to the elastic peak -> (center, gauss_fwhm, lorentz_fwhm).

    Ported verbatim from docs/in8_phase1_check.py so the resolution/energy-zero inputs are
    identical to the Phase-1 baseline (promotion to isar/synth/resolution is §14 step 8).
    """
    from scipy.optimize import curve_fit
    x = np.asarray(x, dtype="float64"); counts = np.asarray(counts, dtype="float64")
    near = np.abs(x) < 2.5
    c0 = float(x[near][int(np.argmax(counts[near]))]) if near.any() else float(x[int(np.argmax(counts))])
    win = np.abs(x - c0) <= 1.5
    xs, ys = x[win], counts[win]
    bg0 = float(np.min(ys)); height = float(np.max(ys) - bg0)

    def model(xx, area, gfw, lfw, cen, bg):
        return Voigt(xx, area, gfw, lfw, cen) + bg

    try:
        popt, _ = curve_fit(model, xs, ys, p0=[max(height, 1.0) * 1.5, 1.3, 0.3, c0, bg0],
                            bounds=([0, 0.5, 0.0, c0 - 1.0, 0.0],
                                    [np.inf, 2.5, 1.0, c0 + 1.0, max(bg0 * 3 + 1, 1)]),
                            maxfev=20000)
        _area, gfw, lfw, cen, _bg = popt
        if 0.5 <= gfw <= 2.5 and 0.0 <= lfw <= 1.0:
            return float(cen), float(gfw), float(lfw)
    except Exception:                                      # noqa: BLE001
        pass
    half = bg0 + 0.5 * height
    above = xs[ys >= half]
    fwhm = float(above.max() - above.min()) if above.size >= 2 else 1.3
    return c0, min(max(fwhm, 0.5), 2.5), 0.0


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

    rows, deltas, disp, boot_panels = [], [], [], []
    print("== Phase-3 step-3: structure loop + frequentist errors on S30 control ==")
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

        tag = f"s30_scan{scan_no}_{S30_ANALYZER}"
        ref_row = ref.get(tag)
        es_do = es_dg = None
        if ref_row is not None and es is not None:
            es_do = round(es.omega0 - float(ref_row["omega0"]), 4)
            es_dg = round(es.gamma - float(ref_row["gamma"]), 4)
            deltas.append((tag, es_do, es_dg))
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
            rows.append(row)
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
        overlay(FIGDIR / f"{tag}.png", f"S30 scan#{scan_no} {S30_ANALYZER} (T={ctx.T:.0f} K)",
                ctx, fits_by_label)
        print(f"  figure: {tag}.png")

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
