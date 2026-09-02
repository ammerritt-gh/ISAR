"""Offline EIG campaign simulator: the planner against the analytic truth, with no instrument.

The world here is TAVI's ``Phonon_DFT`` sample reduced to arithmetic -- acoustic
``E = 6 sin(pi q / 2)``, optic ``E = 6 + 2 sin(pi q / 2)`` (``isar.analyze.truth.phonon_dft``), zone
period 2, T = 200 K, a 0.8 meV Gaussian resolution, and the count scale measured in the
demo rehearsal.  It exists to answer one question cheaply: does
:func:`isar.drive.acquisition.eig_planner.plan_next` map a branch to the requested bar, and at what
cost in neutrons?

The split of authority matters.  The *planner* knows only the class priors and what it has been
told was fitted.  The *world* owns detection (a peak is fitted only when it puts at least
``DETECTION_COUNTS`` counts in the window), the measurement noise, and the true amplitudes.  The
planner's intensity map starts at the class-prior scale and is replaced by the world scale after the
first scan, standing in for "read the amplitudes off the fits".

    python tools/eig_sim.py --target acoustic --seed 1
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np

if __package__ in (None, ""):                      # run as a script from anywhere in the repo
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from isar.analyze.truth import _fold
from isar.drive.acquisition.eig_planner import (
    Candidate, ExecutedScan, IntensityScale, PlannerInput, anchor_admitted, branch_counts,
    default_intensity_scale, fisher_sigma_meV, joint_assign, merge_candidate, plan_next,
    upsert_anchor,
)
from isar.schema.hypotheses import preset

BRANCHES = ("acoustic", "optic")
ENERGY_DOMAIN = (-9.0, 9.0)          # the authorized band: the optic tops out at 8 meV
TEMPERATURE_K = 200.0
FWHM_MEV = 0.8
PERIOD = 2.0
MAX_SCANS = 200
DETECTION_COUNTS = 5.0               # the world's detection floor, not the planner's

#: The truth's intensity scale, per 1e7 neutrons (``test_workspaces/demo-rehearsal/F-final``).
WORLD_NCOUNT_REFERENCE = 1.0e7
WORLD_PEAK_COUNTS_MEV = 0.65  # PRE-BOSE Stokes area per 1e7, as the live TAVI runs measured it (the
                              # 17 counts*meV seen in the rehearsal data is the lobe at ~1 meV, n+1 ~ 20)
WORLD_ELASTIC_COUNTS_MEV = 30.0
WORLD_BACKGROUND_PER_POINT = 0.3

WORLD_SCALE = IntensityScale(
    peak_counts_meV={b: WORLD_PEAK_COUNTS_MEV / WORLD_NCOUNT_REFERENCE for b in BRANCHES},
    elastic_counts_meV=WORLD_ELASTIC_COUNTS_MEV / WORLD_NCOUNT_REFERENCE,
    background_per_point=WORLD_BACKGROUND_PER_POINT / WORLD_NCOUNT_REFERENCE)


def truth_energies(u: float) -> dict[str, float]:
    s = math.sin(math.pi * float(u) / 2.0)
    return {"acoustic": 6.0 * s, "optic": 6.0 + 2.0 * s}


def belief_row(name: str) -> dict:
    """A cold belief row: the shipped class prior and no anchors."""
    h = preset(name, ENERGY_DOMAIN).to_dict()
    return {"branch_id": name, "anchors": [], "n_anchors": 0, "period": PERIOD,
            "sample": "Phonon_DFT", "t_bin": TEMPERATURE_K,
            "parameter_prior": {"zc_energy": h["zc_energy"], "slope_zc": h["slope_zc"],
                                "confidence": h["confidence"]}}


def _fmt_pred(design, branch: str) -> str:
    mu, sigma = design.e_pred_meV[branch]
    return f"{mu:.2f}+-{sigma:.2f}"


def run(args) -> int:
    hs = [args.h_start + args.step_h * i
          for i in range(int(round((args.h_end - args.h_start) / args.step_h)) + 1)]
    path = tuple((h, 0.0, 0.0) for h in hs)
    us = tuple(abs(_fold(h, PERIOD)) for h in hs)
    targets = BRANCHES if args.target == "both" else (args.target,)
    rng = np.random.default_rng(args.seed)

    beliefs = {b: belief_row(b) for b in BRANCHES}
    measured: dict[int, tuple[Candidate, ...]] = {}
    executed_scans: dict[int, tuple[ExecutedScan, ...]] = {}
    executed: set[tuple] = set()
    scale = default_intensity_scale(BRANCHES)
    total_neutrons = 0.0
    timings: list[float] = []
    decision = None
    scans = 0

    while scans < MAX_SCANS:
        inp = PlannerInput(
            path=path, u=us, targets=targets, beliefs=beliefs, measured=measured,
            scans=executed_scans,
            energy_domain_meV=ENERGY_DOMAIN, fwhm_meV=tuple(FWHM_MEV for _ in hs),
            temperature_K=TEMPERATURE_K, scale=scale, tolerance_meV=args.tolerance)
        started = time.perf_counter()
        decision = plan_next(inp)
        timings.append(time.perf_counter() - started)
        if decision.stop:
            break

        design = decision.chosen
        i = design.grid_index
        state = decision.per_point[i]
        if state.within_bar:
            raise AssertionError(f"grid point {i} (u={state.u:.3f}) is already within the bar "
                                 "and must never be scanned again")
        scans += 1
        total_neutrons += design.cost_neutrons
        fingerprint = (i, round(design.e_lo, 6), round(design.e_hi, 6), design.ncount)
        if args.inject_null and fingerprint in executed:
            raise AssertionError(f"scan {scans} repeats {fingerprint} after a null at that point")
        executed.add(fingerprint)
        hidden = args.inject_null and scans == 1

        truth = truth_energies(us[i])
        sigmas = fisher_sigma_meV([truth[b] for b in BRANCHES], e_lo=design.e_lo,
                                  e_step=design.e_step, n_points=design.n_points,
                                  fwhm_meV=FWHM_MEV, temperature_K=TEMPERATURE_K,
                                  scale=WORLD_SCALE, branches=BRANCHES, ncount=design.ncount)
        counts = branch_counts([truth[b] for b in BRANCHES], e_lo=design.e_lo,
                               e_step=design.e_step, n_points=design.n_points,
                               fwhm_meV=FWHM_MEV, temperature_K=TEMPERATURE_K, scale=WORLD_SCALE,
                               branches=BRANCHES, ncount=design.ncount)
        # The world fits the peaks the scan actually delivered, then identifies them jointly --
        # the same rule the planner priced the design with.
        found = []
        seen: list[str] = []
        for bi, branch in enumerate(BRANCHES):
            # The world fits a peak only if it is inside the scanned window and bright enough there.
            # Without the position test a peak just outside the window is still "fitted", with a
            # Cramer-Rao sigma of several meV, and that meaningless number enters the candidate list.
            if not design.e_lo <= truth[branch] <= design.e_hi:
                continue
            if counts[bi] < DETECTION_COUNTS or not math.isfinite(sigmas[bi]):
                continue
            if hidden and branch == "acoustic":
                seen.append("acou HIDDEN by --inject-null")
                continue
            found.append((branch, truth[branch] + float(rng.normal(0.0, sigmas[bi])), sigmas[bi]))
        identities = joint_assign([(e, s) for _b, e, s in found], us[i], beliefs, BRANCHES,
                                  window=(design.e_lo, design.e_hi),
                                  domain_hi=ENERGY_DOMAIN[1])
        target = targets[0]
        for (branch, observed, sigma), probs in zip(found, identities):
            measured[i] = merge_candidate(measured.get(i, ()), Candidate(observed, sigma, probs))
            admitted = anchor_admitted(probs, branch)
            if admitted:
                beliefs[branch] = upsert_anchor(beliefs[branch], us[i], observed, probs[branch])
            seen.append(f"{branch[:4]} {observed:.3f}+-{sigma:.3f} "
                        f"P({target})={probs.get(target, 0.0):.2f}"
                        f"{'' if admitted else ' [ambiguous, no anchor]'}")
        # Every executed scan is negative evidence about the branches it did NOT deliver, so all
        # of them are recorded -- not only the ones that found nothing.
        executed_scans[i] = tuple(executed_scans.get(i, ())) + (
            ExecutedScan(design.e_lo, design.e_hi, design.ncount,
                         frozenset(branch for branch, _e, _s in found)),)
        if not found:
            seen = seen or ["nothing detected"]
        scale = WORLD_SCALE

        prize = [p for p in decision.per_point if not p.exempt]
        done = sum(1 for p in prize if p.within_bar)
        print(f"{scans:3d}  h={design.hkl[0]:5.2f} u={us[i]:4.2f}  "
              f"[{design.e_lo:6.2f},{design.e_hi:6.2f}] d={design.e_step:.2f} n={design.n_points:3d} "
              f"nc={design.ncount:>9d}  N={total_neutrons:.3e}  score={design.score:.3e}  "
              f"pred({targets[0]})={_fmt_pred(design, targets[0])}  "
              f"| {'; '.join(seen) if seen else 'nothing detected'}  "
              f"| bar {done}/{len(prize)}")

    prize = [p for p in decision.per_point if not p.exempt]
    done = sum(1 for p in prize if p.within_bar)
    print(f"\nstop={decision.stop} reason={decision.stop_reason or '-'}  "
          f"scans={scans}  neutrons={total_neutrons:.4e}  bar {done}/{len(prize)}")
    print(f"decision time: max {max(timings):.2f} s, mean {sum(timings)/len(timings):.2f} s "
          f"over {len(timings)} decisions")

    target = targets[0]
    print(f"\n{'h':>6} {'u':>5} {'exempt':>7} {'truth':>8} {'measured':>9} {'sigma':>7} "
          f"{'within':>7} {'peaks':>6}")
    for m, state in enumerate(decision.per_point):
        best = _best_candidate(measured.get(m, ()), target, us[m], beliefs)
        truth = truth_energies(us[m])[target]
        got = f"{best.energy_meV:9.3f}" if best else f"{'-':>9}"
        sig = f"{best.sigma_meV:7.3f}" if best else f"{'-':>7}"
        print(f"{hs[m]:6.2f} {us[m]:5.2f} {str(state.exempt):>7} {truth:8.3f} {got} {sig} "
              f"{str(state.within_bar):>7} {state.n_candidates:6d}")

    if args.plot:
        _plot(args.plot, hs, us, measured, beliefs, targets, target)
        print(f"\nplot written to {args.plot}")
    return 0


def _best_candidate(cands, target: str, u: float, beliefs):
    """The candidate most strongly identified as ``target`` at this grid point."""
    best, best_p = None, 0.0
    for c, probs in zip(cands, joint_assign([(c.energy_meV, c.sigma_meV) for c in cands], u,
                                            beliefs, BRANCHES,
                                            domain_hi=ENERGY_DOMAIN[1])):
        if probs.get(target, 0.0) > best_p:
            best, best_p = c, probs[target]
    return best


def _plot(out: str, hs, us, measured, beliefs, targets, target) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from isar.drive.acquisition.classify_eig import belief_predict

    fig, ax = plt.subplots(figsize=(8.0, 5.0))
    fine = np.linspace(min(hs), max(hs), 400)
    fine_u = np.array([abs(_fold(float(h), PERIOD)) for h in fine])
    for branch in BRANCHES:
        ax.plot(fine, [truth_energies(u)[branch] for u in fine_u], lw=1.0, ls="--",
                label=f"{branch} truth")
        pred = np.array([belief_predict(beliefs[branch], float(u),
                                        domain_hi=ENERGY_DOMAIN[1]) for u in fine_u])
        ax.plot(fine, pred[:, 0], lw=1.2, label=f"{branch} belief")
        ax.fill_between(fine, pred[:, 0] - pred[:, 1], pred[:, 0] + pred[:, 1], alpha=0.15)
    xs, ys, es = [], [], []
    for m, cands in sorted(measured.items()):
        for c in cands:
            xs.append(hs[m]); ys.append(c.energy_meV); es.append(c.sigma_meV)
    ax.errorbar(xs, ys, yerr=es, fmt="k.", ms=4, lw=0.8, capsize=2, label="measured")
    ax.set_xlabel("h (r.l.u.)")
    ax.set_ylabel("E (meV)")
    ax.set_title(f"EIG campaign - target {'+'.join(targets)}")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=130)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", choices=("acoustic", "optic", "both"), default="acoustic")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--h-start", type=float, default=2.0)
    p.add_argument("--h-end", type=float, default=3.0)
    p.add_argument("--step-h", type=float, default=0.05)
    p.add_argument("--tolerance", type=float, default=0.05)
    p.add_argument("--plot", default=None, help="write a PNG (matplotlib, Agg)")
    p.add_argument("--inject-null", action="store_true",
                   help="the world hides the acoustic peak for the first scan")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
