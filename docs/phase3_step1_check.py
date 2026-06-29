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

Outputs (under the gitignored out/phase3_step1/):
    results_step1.csv     per (scan, hypothesis): ω₀, Γ, cost/dof, k, status flags, Δ vs Phase-1
    figures/s30_scan*.png one per scan: data + elastic+stokes+bg (with phonon/elastic/bg
                          decomposition) + pull residual
    figures/dispersion_summary.png  ω₀(K) for this run overlaid on the Phase-1 baseline
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from isar.analyze import loop                              # noqa: E402
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
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.errorbar(K, [d["omega0"] for d in disp], yerr=[d["omega0_err"] for d in disp],
                fmt="o-", color="C1", capsize=3, label="elastic+stokes+bg (this run)")
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

    rows, deltas, disp = [], [], []
    print("== Phase-3 step-1: structure loop on S30 control ==")
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
        es = fits_by_label.get("elastic+stokes+bg")
        so = fits_by_label.get("stokes+bg")

        tag = f"s30_scan{scan_no}_{S30_ANALYZER}"
        ref_row = ref.get(tag)
        es_do = es_dg = None
        if ref_row is not None and es is not None:
            es_do = round(es.omega0 - float(ref_row["omega0"]), 4)
            es_dg = round(es.gamma - float(ref_row["gamma"]), 4)
            deltas.append((tag, es_do, es_dg))
        for r in results:
            is_elastic = r.label == "elastic+stokes+bg"
            rows.append({
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
            })
        delta_str = (f"  | dOmega0={es_do:+.4f} dGamma={es_dg:+.4f}" if es_do is not None else "")
        print(f"  scan#{scan_no}: elastic+stokes+bg omega0={es.omega0:.3f} gamma={es.gamma:.3f} "
              f"(cost/dof={es.cost_per_dof:.2f}); stokes+bg omega0={so.omega0:.3f} "
              f"(cost/dof={so.cost_per_dof:.2f}){delta_str}")
        if es is not None:
            disp.append({"K": float(sp.q[1]), "T": ctx.T, "omega0": es.omega0,
                         "omega0_err": es.omega0_err_hesse,
                         "ref": float(ref_row["omega0"]) if ref_row else None})
        overlay(FIGDIR / f"{tag}.png", f"S30 scan#{scan_no} {S30_ANALYZER} (T={ctx.T:.0f} K)",
                ctx, fits_by_label)
        print(f"  figure: {tag}.png")

    fields = list(rows[0].keys())
    with (OUT / "results_step1.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    print(f"  wrote {OUT / 'results_step1.csv'}")
    if disp:
        dispersion_summary(FIGDIR / "dispersion_summary.png", disp)
        print("  figure: dispersion_summary.png")

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
