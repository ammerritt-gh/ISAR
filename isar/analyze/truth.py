"""σ-calibration scoring against KNOWN analytic truth (closed-loop Phase 0; TODO.md
"σ-calibration"). The sibling of `isar.analyze.gold`: where `gold` scores dispositions +
ω against a curated *human* fit table (and enforces the safety property), this module scores
delivered ω against an *analytic* ground-truth function/table and reports **interval coverage** —
the empirical fraction of truths that land inside the reported 68% ω₀ interval — plus the
**inflation factor** needed to make that coverage 68%.

There is no safety-violation concept here (analytic truth is not a human REJECT), so this mode is
purely informational (CLI exit 0). It reads the run's `report/results.csv` (the stable file
contract) and never touches the run path.

Two error modes are separated deliberately (TODO.md / READING_GUIDE "when not to trust ISAR"):

- **bar optimism** — intervals too tight; fixable by an inflation factor k on the half-widths.
- **center pull** — a nonzero *signed* mean Δ = ω₀ − ω_true; a systematic shift of the fitted
  center that inflation cannot fix (widening a bar does not re-center it).

Disposition logic (which rows are "delivered") is reused verbatim from `isar.analyze.gold`.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from isar.analyze import gold as gold_mod


# --- truth functions (registry: name -> callable(h, k, l) -> ω_true meV) ---------------------
#
# `phonon_dft` mirrors TAVI's PhononSQW acoustic branch, which is the source of truth
# (..\TAVI\tavi\deterministic_engine.py::PhononSQW._fold/_s/_omega_branch). With the toy
# dispersion grid period 2 (Al_test_phonons_centered.dat spans [-1,1)), zone centers sit at EVEN
# integers, so per-axis q_i = ((x_i + 1) mod 2) - 1, s = sqrt(Σ sin²(π·q_i/2)), and the acoustic
# branch is E = 6·s meV. Anchors: Q=(2.15,0,0) → q=0.15 → 1.4004 meV; Q=(3,0,0) → |q|=1 → 6 meV.

_PHONON_DFT_ACOUSTIC_AMP = 6.0   # meV; matches _ACOUSTIC_AMP in the TAVI engine
_PHONON_DFT_GRID_PERIOD = 2.0    # zone centers at even integers


def _fold(x: float, period: float = _PHONON_DFT_GRID_PERIOD) -> float:
    half = 0.5 * period
    return ((x + half) % period) - half


def phonon_dft(h: float, k: float, l: float) -> float:
    """Analytic acoustic-phonon truth for the TAVI `Phonon_DFT` sample (T-independent center).

    E = 6·sqrt(Σ_i sin²(π·q_i/2)) meV with q_i the reduced coordinate folded into grid period 2.
    """
    half = 0.5 * _PHONON_DFT_GRID_PERIOD
    s = math.sqrt(sum(math.sin(math.pi * _fold(c) / (2.0 * half)) ** 2 for c in (h, k, l)))
    return _PHONON_DFT_ACOUSTIC_AMP * s


# name -> callable(h, k, l) -> ω_true; extend here to add analytic samples.
TRUTH_FNS = {
    "phonon_dft": phonon_dft,
}


# --- truth table (CSV: h,k,l,omega_true  OR  q,omega_true) -----------------------------------

@dataclass(frozen=True)
class TruthRow:
    omega_true: float
    hkl: tuple[float, float, float] | None = None   # matched by hkl within tolerance
    q: float | None = None                          # matched by |q| (single reduced coordinate)


def load_truth_csv(path: str | Path) -> list[TruthRow]:
    """Parse a truth table. Accepts (h,k,l,omega_true) or (q,omega_true); extra columns ignored."""
    rows: list[TruthRow] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            omega = gold_mod._get(r, "omega_true", "omega0_true", "omega", "E", "energy")
            if omega in (None, ""):
                continue
            h = gold_mod._get(r, "h", "H")
            if h not in (None, ""):
                rows.append(TruthRow(
                    omega_true=float(omega),
                    hkl=(float(h), float(gold_mod._get(r, "k", "K", default=0.0) or 0.0),
                         float(gold_mod._get(r, "l", "L", default=0.0) or 0.0))))
            else:
                q = gold_mod._get(r, "q", "Q")
                rows.append(TruthRow(omega_true=float(omega),
                                     q=(float(q) if q not in (None, "") else None)))
    return rows


def truth_from_csv(rows: list[TruthRow], hkl, *, tol: float = 1e-3) -> float | None:
    """ω_true for an (h,k,l) result row, matched to a truth table within `tol`.

    hkl rows match on Euclidean hkl distance; q-only rows match on the row's folded |q| magnitude
    (s·... is not invertible, so |q| is compared to sqrt(Σ q_i²) of the folded coordinate)."""
    h, k, l = hkl
    best = None
    best_d = tol
    for tr in rows:
        if tr.hkl is not None:
            d = math.dist((h, k, l), tr.hkl)
        elif tr.q is not None:
            qmag = math.sqrt(sum(_fold(c) ** 2 for c in (h, k, l)))
            d = abs(qmag - abs(tr.q))
        else:
            continue
        if d <= best_d:
            best_d, best = d, tr.omega_true
    return best


# --- results.csv rows ------------------------------------------------------------------------

@dataclass(frozen=True)
class ResultRow:
    scan: int | None
    detector: str | None
    hkl: tuple[float, float, float]
    verdict: str
    disposition: str            # from gold_mod.verdict_disposition(verdict)
    omega0: float | None
    interval_lo: float | None
    interval_hi: float | None
    interval_reliable: bool = True   # False → interval degenerate (γ-at-bound); excluded from
                                     # coverage/inflation but still scored for center pull
    branch_id: str | None = None     # Phase A: dispersion branch this ω₀ was assigned to. Only
                                     # acoustic (or null, older ledgers) rows are scored against the
                                     # acoustic truth — an optic ω₀ can never register as confident-wrong.
    resolvable: bool = True          # False → near-Γ unresolvable; not counted as an ω₀ delivery
    bucket: str | None = None   # value of the --bucket-by column, if any


def _fnum(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fbool(v, *, default: bool = True) -> bool:
    """Parse a results.csv boolean cell; absent/blank → ``default`` (back-compat with runs
    written before the column existed)."""
    if v in (None, ""):
        return default
    return str(v).strip().lower() in ("1", "true", "yes")


def load_results(path: str | Path, *, bucket_by: str | None = None) -> list[ResultRow]:
    """Parse the run's `report/results.csv` into `ResultRow`s (one per fitted detector row)."""
    rows: list[ResultRow] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            h, k, l = _fnum(r.get("h")), _fnum(r.get("k")), _fnum(r.get("l"))
            if h is None or k is None or l is None:
                continue    # a held/no-detector ledger row: nothing to score against truth
            verdict = (r.get("verdict") or "").strip()
            rows.append(ResultRow(
                scan=(int(float(r["scan"])) if r.get("scan") not in (None, "") else None),
                detector=(r.get("detector") or None),
                hkl=(h, k, l),
                verdict=verdict,
                disposition=gold_mod.verdict_disposition(verdict),
                omega0=_fnum(r.get("omega0")),
                interval_lo=_fnum(r.get("interval_lo")),
                interval_hi=_fnum(r.get("interval_hi")),
                interval_reliable=_fbool(r.get("interval_reliable")),
                branch_id=((r.get("branch_id") or None)),
                resolvable=_fbool(r.get("resolvable")),
                bucket=((r.get(bucket_by) or None) if bucket_by else None)))
    return rows


# --- per-row truth score ---------------------------------------------------------------------

@dataclass(frozen=True)
class TruthScore:
    scan: int | None
    detector: str | None
    hkl: tuple[float, float, float]
    omega0: float
    omega_true: float
    delta: float                 # signed ω₀ − ω_true (center pull)
    halfwidth: float | None      # 0.5·(hi − lo); None when no interval
    pull: float | None           # delta / halfwidth (None when halfwidth 0/missing)
    in_interval: bool | None     # ω_true ∈ [lo, hi]
    bucket: str | None = None


def score_row(row: ResultRow, omega_true: float) -> TruthScore:
    delta = row.omega0 - omega_true
    # A flagged-unreliable interval (γ-at-bound degenerate bootstrap) is treated as *no*
    # interval: it must not poison coverage or the inflation factor. The center pull (Δ) is
    # still scored — the reported ω₀ is a real point estimate even when its bar is untrustworthy.
    have_interval = (row.interval_lo is not None and row.interval_hi is not None
                     and row.interval_reliable)
    hw = 0.5 * (row.interval_hi - row.interval_lo) if have_interval else None
    pull = (delta / hw) if (hw is not None and hw > 0) else None
    in_interval = bool(row.interval_lo <= omega_true <= row.interval_hi) if have_interval else None
    return TruthScore(scan=row.scan, detector=row.detector, hkl=row.hkl, omega0=row.omega0,
                      omega_true=omega_true, delta=delta, halfwidth=hw, pull=pull,
                      in_interval=in_interval, bucket=row.bucket)


# --- aggregate -------------------------------------------------------------------------------

@dataclass
class BucketReport:
    label: str
    n: int
    coverage68: float | None
    inflation: float | None
    center_pull_mean: float | None


@dataclass
class TruthReport:
    n_scored: int                       # delivered rows with a truth value and an omega0
    n_delivered_no_truth: int           # delivered rows with no truth match (skipped)
    n_not_delivered: int                # non-delivered rows (skipped)
    n_zero_halfwidth: int               # scored rows skipped from inflation (halfwidth 0/missing)
    coverage68: float | None            # fraction of ω_true inside [lo, hi]
    inflation: float | None             # k s.t. 68% of |Δ| ≤ k·halfwidth (68th pct of |Δ|/hw)
    pull_mean: float | None
    pull_std: float | None
    center_pull_mean: float | None      # mean SIGNED Δ — systematic; inflation cannot fix it
    median_abs_delta: float | None
    buckets: list[BucketReport] = field(default_factory=list)
    bucket_by: str | None = None
    truth_source: str = ""
    scores: list[TruthScore] = field(default_factory=list)


def _coverage(scores) -> float | None:
    xs = [s.in_interval for s in scores if s.in_interval is not None]
    return float(np.mean(xs)) if xs else None


def _inflation(scores) -> tuple[float | None, int]:
    """k = 68th percentile of |Δ|/halfwidth over rows with halfwidth > 0. Returns (k, n_skipped)."""
    ratios, skipped = [], 0
    for s in scores:
        if s.halfwidth and s.halfwidth > 0:
            ratios.append(abs(s.delta) / s.halfwidth)
        else:
            skipped += 1
    k = float(np.percentile(ratios, 68.0)) if ratios else None
    return k, skipped


def _center_pull(scores) -> float | None:
    xs = [s.delta for s in scores]
    return float(np.mean(xs)) if xs else None


def aggregate(scores, *, n_delivered_no_truth=0, n_not_delivered=0,
              bucket_by=None, truth_source="") -> TruthReport:
    pulls = [s.pull for s in scores if s.pull is not None]
    deltas = [abs(s.delta) for s in scores]
    inflation, n_zero_hw = _inflation(scores)

    buckets: list[BucketReport] = []
    if bucket_by is not None:
        by: dict[str, list] = {}
        for s in scores:
            by.setdefault(s.bucket if s.bucket is not None else "(none)", []).append(s)
        for label in sorted(by):
            grp = by[label]
            infl, _ = _inflation(grp)
            buckets.append(BucketReport(label=label, n=len(grp), coverage68=_coverage(grp),
                                        inflation=infl, center_pull_mean=_center_pull(grp)))

    return TruthReport(
        n_scored=len(scores),
        n_delivered_no_truth=n_delivered_no_truth,
        n_not_delivered=n_not_delivered,
        n_zero_halfwidth=n_zero_hw,
        coverage68=_coverage(scores),
        inflation=inflation,
        pull_mean=(float(np.mean(pulls)) if pulls else None),
        pull_std=(float(np.std(pulls, ddof=1)) if len(pulls) > 1 else None),
        center_pull_mean=_center_pull(scores),
        median_abs_delta=(float(np.median(deltas)) if deltas else None),
        buckets=buckets,
        bucket_by=bucket_by,
        truth_source=truth_source,
        scores=list(scores),
    )


# --- driver: results.csv + truth -> report ---------------------------------------------------

def score_results(rows, truth_lookup, *, bucket_by=None, truth_source="",
                  csv_tol: float = 1e-3) -> TruthReport:
    """Score delivered `ResultRow`s against a truth lookup.

    `truth_lookup` is a callable(hkl) -> ω_true or None (both the truth-fn and truth-csv paths
    reduce to this). Non-delivered rows and delivered rows with no truth match are counted, not
    scored."""
    scores, no_truth, not_delivered = [], 0, 0
    for row in rows:
        if not gold_mod.delivered(row.disposition) or row.omega0 is None:
            not_delivered += 1
            continue
        # Phase A: only the ACOUSTIC branch is the quantity of interest. A non-acoustic (optic) ω₀ or a
        # near-Γ unresolvable row is recorded but NOT scored against the acoustic truth — so a wrong-
        # branch fit can never register as a confident-wrong acoustic delivery. Null = older ledger → scored.
        if (row.branch_id is not None and row.branch_id != "acoustic") or not row.resolvable:
            not_delivered += 1
            continue
        omega_true = truth_lookup(row.hkl)
        if omega_true is None:
            no_truth += 1
            continue
        scores.append(score_row(row, omega_true))
    return aggregate(scores, n_delivered_no_truth=no_truth, n_not_delivered=not_delivered,
                     bucket_by=bucket_by, truth_source=truth_source)


def report_to_dict(rep: TruthReport) -> dict:
    """Machine-readable scoring dict for the σ-calibration campaign (`--json`)."""
    return {
        "truth_source": rep.truth_source,
        "bucket_by": rep.bucket_by,
        "n_scored": rep.n_scored,
        "n_delivered_no_truth": rep.n_delivered_no_truth,
        "n_not_delivered": rep.n_not_delivered,
        "n_zero_halfwidth": rep.n_zero_halfwidth,
        "coverage68": rep.coverage68,
        "inflation": rep.inflation,
        "pull_mean": rep.pull_mean,
        "pull_std": rep.pull_std,
        "center_pull_mean": rep.center_pull_mean,
        "median_abs_delta": rep.median_abs_delta,
        "buckets": [
            {"label": b.label, "n": b.n, "coverage68": b.coverage68,
             "inflation": b.inflation, "center_pull_mean": b.center_pull_mean}
            for b in rep.buckets
        ],
        "rows": [
            {"scan": s.scan, "detector": s.detector, "hkl": list(s.hkl),
             "omega0": s.omega0, "omega_true": s.omega_true, "delta": s.delta,
             "halfwidth": s.halfwidth, "pull": s.pull, "in_interval": s.in_interval,
             "bucket": s.bucket}
            for s in rep.scores
        ],
    }
