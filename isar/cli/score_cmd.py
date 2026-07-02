"""`isar score --workspace DIR --gold CSV` — score a run against a gold-fit CSV (UNIFICATION_DESIGN §5).

Optional, off the run path (U6): wraps the existing `isar.analyze.gold` harness. Builds an
IsarResult per gold scan from the ledger and reports the confusion matrix, recovery accuracy, and
— the load-bearing property — safety violations (ISAR cleanly resolving a human-REJECT).
"""
from __future__ import annotations

import sys

from isar.analyze import gold as gold_mod
from isar.session.workspace import Workspace


def run(args) -> int:
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
