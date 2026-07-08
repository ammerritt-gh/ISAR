"""Step-6 gold-standard scoring (§10, §14): score Phase-3 verdicts against a curated human-fit table
(`FeSe_1_fit_results.csv`), "exactly as the harness scores synthetic recovery against truth." This is
the pure scoring library — CSV loading, channel selection, verdict→disposition mapping, per-scan
scoring, and the confusion/aggregate roll-up. It holds no pipeline state and reads no raw data; the
real-data driver (docs/phase3_gold_check.py) feeds it `IsarResult`s and writes the report.

Two properties are scored (§10):
- **Which discard bucket** — does ISAR accept the scans the human accepted and decline the rest? The
  load-bearing safety property is that a human-REJECT is never returned as a confident ISAR `resolved`.
- **Which ω** — on the agreed-accept set, ISAR's ω must be statistically consistent with the gold ω
  (pull, and 68%-interval coverage). Gold used a fixed resolution and a different fitter, so agreement
  is statistical, not bit-exact.

Channel selection (the owner-flagged configurable seam): each gold row carries its own `K(rlu)`, and a
scan holds nine analyzers at nine K's, so a row is matched to the analyzer whose *reported* K equals the
row's K — which resolves to ana7 for FeSe_1, generalizes to a different fixed analyzer, and supports
experiments that use several analyzers (each row → its own channel). An explicit override wins.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Verdict-label -> disposition. "Score the number, flag the fit": a fit that delivers a usable omega
# is a success even if its model is imperfect, so `resolved` (clean) and `model_incomplete` (flagged —
# real peak, imperfect model) both count as *delivered*; only `junk`/`unconstrained` (no usable number)
# are rejects; the provisional/calibration buckets abstain.
_RESOLVED = ("resolved",)
_FLAGGED = ("model_incomplete",)
_REJECT = ("junk", "unconstrained")
_ABSTAIN = ("held_for_calibration", "underpowered", "no_signal")
# Phase 4a: under-model falsification. `signal_unmodeled` = real signal the phonon model can't fit;
# delivered to the human as a flagged-for-attention DESCRIPTOR result, but deliberately NOT an omega0
# measurement — its own disposition so it stays out of _DELIVERED (gold/truth omega0 count) and
# stopping.USABLE_DISPOSITIONS (the drive stop rule). The omega0 is nulled upstream (driver).
_UNMODELED = ("signal_unmodeled",)
_DELIVERED = ("resolved", "flagged")


# --- gold table ------------------------------------------------------------------------------

@dataclass(frozen=True)
class GoldRow:
    scan: int
    sample: int
    T: float
    K: float
    omega0: float            # phonon_energy
    omega0_err: float        # phonon_energy_err
    gamma: float             # damping
    gamma_err: float         # damping_err
    center: float            # elastic zero
    chi2: float
    quality: str             # "OK" | "REJECT"
    slit: float | None = None
    analyzer: str | None = None   # optional explicit channel override column
    label: str | None = None


def _get(row: dict, *names, default=None):
    """First present, non-empty value among candidate column names (order/extra tolerant)."""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return default


def _f(row, *names, default=None):
    v = _get(row, *names)
    return float(v) if v not in (None, "") else default


def load_gold(path: str | Path) -> list[GoldRow]:
    """Parse a gold fit table. Column-name driven; unknown extra columns are ignored."""
    rows: list[GoldRow] = []
    with Path(path).open("r", newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            rows.append(GoldRow(
                scan=int(float(_get(r, "Scan", "scan"))),
                sample=int(float(_get(r, "Sample", "sample", default=1))),
                T=_f(r, "T(K)", "T", default=float("nan")),
                K=_f(r, "K(rlu)", "K", default=float("nan")),
                omega0=_f(r, "phonon_energy", "omega0"),
                omega0_err=_f(r, "phonon_energy_err", "omega0_err", default=0.0),
                gamma=_f(r, "damping", "gamma", default=float("nan")),
                gamma_err=_f(r, "damping_err", "gamma_err", default=0.0),
                center=_f(r, "center", default=float("nan")),
                chi2=_f(r, "Chi2", "chi2", default=float("nan")),
                quality=str(_get(r, "Quality", "quality", default="")).strip(),
                slit=_f(r, "Slit(mm)", "slit"),
                analyzer=_get(r, "Analyzer", "analyzer"),
                label=_get(r, "Label", "label"),
            ))
    return rows


# --- channel selection -----------------------------------------------------------------------

@dataclass(frozen=True)
class AnalyzerChoice:
    detector_id: str
    k_residual: float        # |reported K - gold K| for the chosen channel
    source: str              # "override" | "matched"
    flagged: bool            # residual beyond tolerance -> mapping is suspect, shown not hidden


def _analyzer_ks(record) -> dict[str, float]:
    """detector_id -> reported K (q[1]) for every analyzer present on the record."""
    return {det: float(sp.q[1]) for det, sp in record.spectra.items()}


def _default_tol(ks: dict[str, float]) -> float:
    """Half the smallest gap between adjacent analyzer K's (so a match to the wrong neighbour is
    flagged); +inf when fewer than two analyzers exist (nothing to be ambiguous about)."""
    vals = sorted(ks.values())
    gaps = [b - a for a, b in zip(vals, vals[1:]) if b > a]
    return 0.5 * min(gaps) if gaps else float("inf")


def select_analyzer(record, gold_row: GoldRow, *, override: str | None = None,
                    tol: float | None = None) -> AnalyzerChoice:
    """Pick the analyzer channel a gold row corresponds to. Explicit override (argument or the row's
    `analyzer` column) wins; otherwise the analyzer whose reported K is closest to the row's K."""
    ks = _analyzer_ks(record)
    if tol is None:
        tol = _default_tol(ks)
    chosen = override or gold_row.analyzer
    if chosen is not None:
        resid = abs(ks.get(chosen, float("nan")) - gold_row.K)
        return AnalyzerChoice(chosen, resid, "override", bool(resid > tol))
    det = min(ks, key=lambda d: abs(ks[d] - gold_row.K))
    resid = abs(ks[det] - gold_row.K)
    return AnalyzerChoice(det, resid, "matched", bool(resid > tol))


# --- verdict -> disposition ------------------------------------------------------------------

def verdict_disposition(label: str) -> str:
    """Collapse a §6 verdict bucket onto the scoring axis: resolved / flagged / reject / abstain.

    `resolved` = a clean usable number; `model_incomplete` = `flagged` (a real peak with a usable
    omega but an imperfect model — delivered, flag for review); `junk`/`unconstrained` = `reject` (no
    usable number, matching a human REJECT); the provisional/calibration buckets
    (`held_for_calibration`, `underpowered`, `no_signal`) `abstain` — reported, not counted as a
    disagreement with a human OK ("couldn't try", not "this fit is bad")."""
    if label in _RESOLVED:
        return "resolved"
    if label in _FLAGGED:
        return "flagged"
    if label in _UNMODELED:
        return "unmodeled"
    if label in _REJECT:
        return "reject"
    return "abstain"


def delivered(disposition: str) -> bool:
    """True when ISAR produced a usable omega (`resolved` or `flagged`)."""
    return disposition in _DELIVERED


# --- per-scan score --------------------------------------------------------------------------

@dataclass(frozen=True)
class IsarResult:
    """The pipeline's per-scan output, reduced to what scoring needs."""
    verdict: str
    confidence: float
    omega0: float | None
    omega0_err: float | None = None    # used for pull only when no bootstrap interval exists
    interval: tuple | None = None      # (lo, med, hi) 68% ω₀ interval
    modality: str = "fold"


@dataclass(frozen=True)
class GoldScore:
    scan: int
    sample: int
    quality: str
    gold_omega0: float
    gold_disposition: str              # "accept" | "reject"
    isar_verdict: str
    isar_confidence: float
    isar_omega0: float | None
    isar_disposition: str              # "resolved" | "flagged" | "reject" | "abstain"
    modality: str
    agree: bool | None                 # delivered<->OK match; None when ISAR abstains
    safety: bool                       # True iff ISAR cleanly `resolved` a human-REJECT
    omega0_pull: float | None
    omega0_abs_resid: float | None
    omega0_in_interval: bool | None
    notes: str = ""


def _sigma_isar(isar: IsarResult) -> float | None:
    if isar.interval is not None:
        return 0.5 * (isar.interval[2] - isar.interval[0])
    return isar.omega0_err


def score_row(gold_row: GoldRow, isar: IsarResult) -> GoldScore:
    gold_disp = "accept" if gold_row.quality.upper() == "OK" else "reject"
    isar_disp = verdict_disposition(isar.verdict)
    # agree on the "did each produce a usable number?" axis: delivered <-> gold-OK.
    agree = None if isar_disp == "abstain" else (delivered(isar_disp) == (gold_disp == "accept"))
    safety = bool(gold_disp == "reject" and isar_disp == "resolved")   # the hard violation

    pull = abs_resid = None
    if isar.omega0 is not None:
        abs_resid = abs(isar.omega0 - gold_row.omega0)
        si = _sigma_isar(isar) or 0.0
        sg = gold_row.omega0_err or 0.0
        denom = float(np.hypot(si, sg))
        pull = (isar.omega0 - gold_row.omega0) / denom if denom > 0 else None

    in_interval = None
    if isar.interval is not None:
        lo, _, hi = isar.interval
        in_interval = bool(lo <= gold_row.omega0 <= hi)

    notes = "SAFETY: clean resolve on a human-REJECT" if safety else ""
    return GoldScore(
        scan=gold_row.scan, sample=gold_row.sample, quality=gold_row.quality,
        gold_omega0=gold_row.omega0, gold_disposition=gold_disp,
        isar_verdict=isar.verdict, isar_confidence=isar.confidence, isar_omega0=isar.omega0,
        isar_disposition=isar_disp, modality=isar.modality, agree=agree, safety=safety,
        omega0_pull=pull, omega0_abs_resid=abs_resid, omega0_in_interval=in_interval, notes=notes)


# --- confusion + aggregate -------------------------------------------------------------------

_GOLD_DISP = ("accept", "reject")
_ISAR_DISP = ("resolved", "flagged", "reject", "abstain")


@dataclass
class ConfusionTable:
    counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def count(self, gold_disp: str, isar_disp: str) -> int:
        return self.counts.get((gold_disp, isar_disp), 0)

    def row_total(self, gold_disp: str) -> int:
        return sum(self.count(gold_disp, j) for j in _ISAR_DISP)


def confusion(scores) -> ConfusionTable:
    c = Counter((s.gold_disposition, s.isar_disposition) for s in scores)
    return ConfusionTable(counts=dict(c))


@dataclass
class GoldReport:
    n: int
    confusion: ConfusionTable
    pull_mean: float | None
    pull_std: float | None
    median_abs_resid: float | None
    coverage68: float | None
    bucket_counts: dict[str, int]
    conf_agree: float | None           # mean confidence over delivered that match gold (OK)
    conf_disagree: float | None        # mean confidence over delivered that don't (gold REJECT)
    safety_violations: list[int]       # scan numbers a human REJECTed that ISAR cleanly resolved
    n_recovery: int                    # size of the delivered-on-OK recovery set
    n_delivered: int                   # scans where ISAR produced a usable omega (resolved+flagged)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


def aggregate(scores) -> GoldReport:
    recovery = [s for s in scores
                if delivered(s.isar_disposition) and s.gold_disposition == "accept"]
    pulls = [s.omega0_pull for s in recovery if s.omega0_pull is not None]
    resids = [s.omega0_abs_resid for s in recovery if s.omega0_abs_resid is not None]
    covers = [s.omega0_in_interval for s in recovery if s.omega0_in_interval is not None]

    deliv = [s for s in scores if delivered(s.isar_disposition)]
    conf_agree = _mean([s.isar_confidence for s in deliv if s.gold_disposition == "accept"])
    conf_disagree = _mean([s.isar_confidence for s in deliv if s.gold_disposition == "reject"])

    return GoldReport(
        n=len(scores),
        confusion=confusion(scores),
        pull_mean=_mean(pulls),
        pull_std=(float(np.std(pulls, ddof=1)) if len(pulls) > 1 else None),
        median_abs_resid=(float(np.median(resids)) if resids else None),
        coverage68=(float(np.mean(covers)) if covers else None),
        bucket_counts=dict(Counter(s.isar_verdict for s in scores)),
        conf_agree=conf_agree,
        conf_disagree=conf_disagree,
        safety_violations=[s.scan for s in scores if s.safety],
        n_recovery=len(recovery),
        n_delivered=len(deliv),
    )
