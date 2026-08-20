"""`isar score --workspace DIR --gold CSV` — score a run against a gold-fit CSV (the retired unification plan §5, git history; current contract: docs/WORKSPACE_AND_CLI.md).

Optional, off the run path (U6): wraps the existing `isar.analyze.gold` harness. Builds an
IsarResult per gold scan from the ledger and reports the confusion matrix, recovery accuracy, and
— the load-bearing property — safety violations (ISAR cleanly resolving a human-REJECT).
"""
from __future__ import annotations

import json
import sys

from isar.analyze import gold as gold_mod
from isar.session.workspace import Workspace


def run(args) -> int:
    # Truth mode (σ-calibration, off the run path) is a sibling of gold scoring: known analytic
    # truth instead of a human gold CSV, reporting interval coverage + the inflation factor.
    if getattr(args, "truth_fn", None) or getattr(args, "truth_csv", None):
        return run_truth(args)
    if not getattr(args, "gold", None):
        print("error: score needs one of --gold, --truth-fn, or --truth-csv", file=sys.stderr)
        return 2

    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 2

    _card, session = ws.load()
    # Index the ledger by scan number (first fitted detector wins — S30 gold uses one analyzer).
    by_scan = {}
    for e in session.scans.values():
        for _det, oc in (e.detectors or {}).items():
            by_scan.setdefault(e.scan_number, (e.status, oc))

    rows = gold_mod.load_gold(args.gold)
    scores, missing = [], []
    for row in rows:
        hit = by_scan.get(row.scan)
        if hit is None:
            missing.append(row.scan)
            isar = gold_mod.IsarResult(verdict="held_for_calibration", confidence=0.0, omega0=None)
        else:
            _status, oc = hit
            iv = oc.get("interval")
            isar = gold_mod.IsarResult(
                verdict=oc.get("verdict", "held_for_calibration"),
                confidence=float(oc.get("confidence") or 0.0),
                omega0=oc.get("omega0"), omega0_err=oc.get("omega0_err"),
                interval=(tuple(iv) if iv else None), modality=(oc.get("modality") or "fold"))
        scores.append(gold_mod.score_row(row, isar))

    report = gold_mod.aggregate(scores)
    print(f"Scored {len(scores)} gold scans against {args.gold}")
    if missing:
        print(f"  {len(missing)} gold scans absent from the run ledger (scored as abstain)")
    print(f"  delivered: {report.n_delivered}   recovery (delivered & gold-OK): {report.n_recovery}")
    if report.median_abs_resid is not None:
        print(f"  median |Δω₀| (recovery): {report.median_abs_resid:.3f} meV")
    if report.pull_mean is not None:
        print(f"  ω₀ pull: mean {report.pull_mean:+.2f}"
              + (f", sd {report.pull_std:.2f}" if report.pull_std is not None else ""))
    if report.coverage68 is not None:
        print(f"  68% interval coverage: {report.coverage68:.2f}")
    print(f"  confidence when gold-OK: {_f(report.conf_agree)}   when gold-REJECT: {_f(report.conf_disagree)}")
    viol = report.safety_violations
    print(f"  SAFETY violations (resolved a gold-REJECT): {len(viol)}"
          + (f" -> scans {viol}" if viol else "  ✓"))
    return 0 if not viol else 1


def _f(x):
    return "—" if x is None else f"{x:.3f}"


# --- truth mode (σ-calibration) --------------------------------------------------------------

def run_truth(args) -> int:
    """Score a run's delivered ω against KNOWN analytic truth and report interval coverage +
    inflation. Informational only (no safety concept vs analytic truth): always exit 0."""
    from isar.analyze import truth as truth_mod

    ws = Workspace.open(args.workspace)
    if not ws.exists():
        print(f"error: no workspace at {ws.root} (run `isar init` first)", file=sys.stderr)
        return 2
    results_csv = ws.report_dir / "results.csv"
    if not results_csv.exists():
        print(f"error: no results.csv at {results_csv} (run `isar run` first)", file=sys.stderr)
        return 2

    if args.truth_fn:
        fn = truth_mod.TRUTH_FNS.get(args.truth_fn)
        if fn is None:
            known = ", ".join(sorted(truth_mod.TRUTH_FNS)) or "(none)"
            print(f"error: unknown --truth-fn '{args.truth_fn}' (known: {known})", file=sys.stderr)
            return 2
        truth_lookup = lambda hkl: fn(*hkl)  # noqa: E731
        source = f"fn:{args.truth_fn}"
    else:
        trows = truth_mod.load_truth_csv(args.truth_csv)
        truth_lookup = lambda hkl: truth_mod.truth_from_csv(trows, hkl)  # noqa: E731
        source = f"csv:{args.truth_csv}"

    bucket_by = getattr(args, "bucket_by", None)
    interval_source = getattr(args, "interval", None) or "bootstrap"
    rows = truth_mod.load_results(results_csv, bucket_by=bucket_by)
    rep = truth_mod.score_results(rows, truth_lookup, bucket_by=bucket_by, truth_source=source,
                                  interval_source=interval_source)

    _iv_label = ("bayes (posterior credible)" if interval_source == "bayes"
                 else "bootstrap (68% bar)")
    print(f"σ-calibration: scored {rep.n_scored} delivered rows against {source}")
    print(f"  interval source: {_iv_label}")
    if rep.n_not_delivered or rep.n_delivered_no_truth:
        print(f"  skipped: {rep.n_not_delivered} not-delivered, "
              f"{rep.n_delivered_no_truth} delivered without a truth match")
    if rep.n_scored == 0:
        print("  nothing to score.")
    else:
        print(f"  68% interval coverage: {_f(rep.coverage68)}  "
              f"(target 0.68; scored against the {_iv_label} interval)")
        print(f"  ω₀ pull: mean {_pm(rep.pull_mean)}"
              + (f", sd {rep.pull_std:.2f}" if rep.pull_std is not None else ""))
        print(f"  median |Δω₀|: {_f(rep.median_abs_delta)} meV")
        if rep.n_zero_halfwidth:
            print(f"  ({rep.n_zero_halfwidth} rows skipped from the inflation factor: halfwidth 0)")
        print(f"  suggested INFLATION FACTOR k: {_f(rep.inflation)}  "
              "(multiply interval half-widths by k so 68% of truths fall within k·halfwidth)")
        cp = rep.center_pull_mean
        print(f"  center pull (mean signed Δ): {_pm(cp)} meV — "
              + ("systematic center shift; inflation cannot fix this"
                 if cp is not None and abs(cp) > 0 else "≈0, no systematic center shift"))
        if rep.buckets:
            print(f"  by {rep.bucket_by}:")
            for b in rep.buckets:
                print(f"    {b.label}: n={b.n}  cov68={_f(b.coverage68)}  "
                      f"k={_f(b.inflation)}  center_pull={_pm(b.center_pull_mean)}")

    if getattr(args, "json", None):
        from pathlib import Path
        Path(args.json).write_text(json.dumps(truth_mod.report_to_dict(rep), indent=2),
                                   encoding="utf-8")
        print(f"  wrote {args.json}")
    return 0


def _pm(x):
    return "—" if x is None else f"{x:+.3f}"
