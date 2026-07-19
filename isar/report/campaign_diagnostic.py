"""CAMPAIGN_DIAGNOSTIC — the architect's post-mortem of an `isar drive` campaign.

Part 2 of ``docs/CAMPAIGN_DIAGNOSTIC_SPEC.md``. A **deterministic** renderer: it assembles the whole
story of a campaign (objective → what the driver did → delivered dispersion vs truth → failure dossier
→ machinery defects → verdict) from the files the run already wrote. No refitting, no re-scoring, no
LLM. If a question cannot be answered from the record, that is a logging defect surfaced in §6, never
papered over.

Consumes ONLY the file contract:
``campaign_summary.json``, ``session_log.jsonl``, ``report/results.csv``, ``score.json`` (optional),
``card.state.json`` — plus a Stage-I re-parse of ``<workspace>/data`` for raw counts behind the
per-scan figures (the same card+ingest boundary ``isar run`` uses; deterministic, no fitting).

Old-log tolerance is mandatory: the renderer degrades gracefully against logs that predate the Part-1
events (``escalate_fit``, ``decide.action``, ``recenter_refused_elastic``) and the S3 per-q ``reason``
field, against a missing ``campaign_summary.json`` (crashed campaign), and against a missing
``score.json``.

Entry point: :func:`render_campaign_diagnostic`.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from isar.report import captions as _captions
from isar.report import fit_figures as _fit_figures
from isar.report.findings import FindingsDoc

# ---------------------------------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------------------------------


@dataclass
class DiagInputs:
    """Everything the renderer consumes, loaded once. Missing files degrade to empties, not errors."""
    workspace: Path
    summary: dict = field(default_factory=dict)      # campaign_summary.json (or {})
    events: list = field(default_factory=list)       # session_log.jsonl records
    results: list = field(default_factory=list)      # report/results.csv rows
    score: dict | None = None                        # score.json (or None)
    card_state: dict = field(default_factory=dict)   # card.state.json (or {})
    defects: list = field(default_factory=list)      # collected §6 predicate hits (str)
    render_failed: list = field(default_factory=list)  # (scan, det, repr(err)) per figure that raised


def _read_jsonl(path: Path) -> list:
    """Inline JSONL reader (do NOT import drive internals). Missing file → []; a truncated final line
    from a killed run is tolerated and dropped; blank lines skipped."""
    if not path.exists():
        return []
    out: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _load(workspace: str | Path) -> DiagInputs:
    ws = Path(workspace)
    report = ws / "report"
    summary = _read_json(report / "campaign_summary.json") or {}
    events = _read_jsonl(report / "session_log.jsonl")
    score = _read_json(ws / "score.json")
    card_state = _read_json(ws / "card.state.json") or {}

    results: list = []
    try:
        from isar.drive.isar_io.readers import read_results_csv
        results = read_results_csv(ws)
    except FileNotFoundError:
        results = []
    except Exception:  # noqa: BLE001 — a malformed CSV must not sink the report
        results = []

    return DiagInputs(workspace=ws, summary=summary, events=events, results=results,
                      score=score, card_state=card_state)


# ---------------------------------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------------------------------

# Master-assessment diagnostics. Order is used for the fixed diagnosis templates in §5.
_REASON_ENUMS = ("unmeasured", "interval_above_tolerance", "model_ambiguous",
                 "pool_inconsistent", "linewidth_resolution_limited", "analysis_failed",
                 "diminishing_returns", "infeasible")

# Static reason → TODO mapping (§5 / §7).
_REASON_TODO = {
    "unmeasured": "acquire the first feasible master member",
    "interval_above_tolerance": "continue by master-predictive information ranking",
    "model_ambiguous": "prefer model/branch-discriminating measurements",
    "pool_inconsistent": "review drift/normalization; acquire independent evidence without exclusion",
    "linewidth_resolution_limited": "retain Γ as resolution-limited; certify ω when model-robust",
    "analysis_failed": "resume the fingerprinted analysis revision",
    "diminishing_returns": "continue until a configured global limit",
    "infeasible": "verify the independent geometry/instrument/calibration proof",
}

# Dispositions that must never show a fitted ω₀ in a summary table (§4).
_REJECT_DISPOSITIONS = frozenset({"reject", "junk", "abstain", "held", "no_signal", "unmodeled"})
# Collate-only (internal) rows: never listed row-by-row (§4) — surfaced as per-q counts only.
_COLLATE_DISPOSITIONS = frozenset({"branch_continuation"})


@dataclass
class EventCounts:
    kind: Counter                # event kind → count
    decide_action: Counter       # decide payload.get("action") or payload.get("event") → count
    exception_action: Counter    # exception payload.get("action") → count
    kind_unknown: int            # records flagged kind_unknown (or with an out-of-vocab kind)
    total: int


_KNOWN_KINDS = frozenset({"propose", "validate", "run", "analyze", "decide", "escalate_fit",
                          "branch_track_update", "branch_reassignment", "frontier_blocked",
                          "exception", "stop", "timing", "activity_start",
                          "activity_progress", "activity_end", "analysis_clock",
                          "physical_submission", "pooled_master_update", "candidate_update",
                          "certification_crossing", "recovery_pending", "recovery_completed"})


def _event_counts(events: list) -> EventCounts:
    kind: Counter = Counter()
    decide_action: Counter = Counter()
    exception_action: Counter = Counter()
    kind_unknown = 0
    for ev in events:
        k = ev.get("kind")
        kind[k] += 1
        if ev.get("kind_unknown") or (k is not None and k not in _KNOWN_KINDS):
            kind_unknown += 1
        payload = ev.get("payload") or {}
        if k == "decide":
            decide_action[payload.get("action") or payload.get("event") or "?"] += 1
        elif k == "exception":
            exception_action[payload.get("action") or "?"] += 1
    return EventCounts(kind=kind, decide_action=decide_action, exception_action=exception_action,
                       kind_unknown=kind_unknown, total=len(events))


def _run_events(events: list) -> list:
    return [ev.get("payload") or {} for ev in events if ev.get("kind") == "run"]


def _timing_events(events: list) -> list:
    return [ev.get("payload") or {} for ev in events if ev.get("kind") == "timing"]


_FIT_STAGES = frozenset({"isar_run", "fold_rows", "certify"})
_INSTRUMENT_STAGES = frozenset({"wait_scan", "submit", "validate"})


def _wall_split(events: list) -> dict | None:
    """(fitting, instrument, overhead) seconds from timing spans. The ``iteration`` wrapper is
    excluded from overhead (it double-counts its children). Returns None when no timing records."""
    timings = _timing_events(events)
    if not timings:
        return None
    fitting = instrument = overhead = 0.0
    per_stage: Counter = Counter()
    for t in timings:
        stage = t.get("stage")
        secs = float(t.get("seconds") or 0.0)
        per_stage[stage] += secs
        if stage in _FIT_STAGES:
            fitting += secs
        elif stage in _INSTRUMENT_STAGES:
            instrument += secs
        elif stage != "iteration":
            overhead += secs
    return {"fitting": fitting, "instrument": instrument, "overhead": overhead,
            "per_stage": per_stage}


def _num(x: Any, nd: int = 4) -> str:
    """Stable numeric formatting for tables/golden — never a raw float repr."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int, float)):
        f = float(x)
        if f == 0.0:
            return "0"
        return f"{f:.{nd}g}"
    return str(x)


def _hkl_str(hkl) -> str:
    if not hkl:
        return "—"
    return "(" + ", ".join(_num(v, 4) for v in hkl) + ")"


def _fmt_interval(row: dict) -> str:
    """§4 interval honesty — the single presentation gate every table routes through.

    - ``interval_reliable is False`` → literal ``(unreliable)`` (never a numeric range).
    - no interval derivable → ``—``.
    - otherwise ``[lo, hi]``.

    Accepts either a results.csv-style row (``interval_lo``/``interval_hi``/``interval_reliable``) or a
    per-q row (``omega0`` ± ``calibrated_halfwidth`` with an ``interval_reliable`` flag we set)."""
    if row.get("interval_reliable") is False:
        return "(unreliable)"
    lo = row.get("interval_lo")
    hi = row.get("interval_hi")
    if lo is None or hi is None:
        o = row.get("omega0")
        hw = row.get("calibrated_halfwidth")
        if o is not None and hw is not None:
            lo, hi = o - hw, o + hw
    if lo is None or hi is None:
        return "—"
    return f"[{_num(lo)}, {_num(hi)}]"


def _dominant_component(hkls: list) -> int:
    """Index of the hkl component with the widest spread along the path (the scan direction)."""
    if not hkls:
        return 0
    cols = list(zip(*hkls))
    spreads = [max(c) - min(c) for c in cols]
    return int(max(range(len(spreads)), key=lambda i: spreads[i]))


# ---- per-q rollup with reason derivation (S3 back-fill for pre-S3 logs) ----------------------------


def _results_by_hkl(results: list) -> dict:
    """Group non-pooled production/confirm results.csv rows by rounded hkl key."""
    by: dict = {}
    for r in results:
        h, k, l = r.get("h"), r.get("k"), r.get("l")
        if h is None or k is None or l is None:
            continue
        key = (round(float(h), 4), round(float(k), 4), round(float(l), 4))
        by.setdefault(key, []).append(r)
    return by


def _hkl_key(hkl) -> tuple:
    return tuple(round(float(v), 4) for v in hkl)


def _per_q(summary: dict, results: list, defects: list) -> list:
    """Per-q rows classified exclusively from the persisted master assessment snapshot."""
    stop = summary.get("stop") or {}
    rows = list(stop.get("per_q") or [])
    if not rows:
        rows = list(summary.get("grid") or [])
    by_hkl = _results_by_hkl(results)

    out: list = []
    for row in rows:
        r = dict(row)
        hkl = r.get("hkl") or []
        key = _hkl_key(hkl) if hkl else None
        prod_rows = [x for x in by_hkl.get(key, []) if not x.get("pooled")] if key else []
        # delivered rows (have an omega0) whose interval is explicitly flagged unreliable
        flagged = [x for x in prod_rows
                   if x.get("omega0") is not None and x.get("interval_reliable") is False]

        detail = r.get("reason")
        diagnostics = list(r.get("diagnostics") or [])
        if r.get("converged") or r.get("assessment_state") == "certified":
            reason = "met"
        elif r.get("assessment_state") == "infeasible":
            reason = "infeasible"
        elif r.get("assessment_state") == "unmeasured" or r.get("source") is None:
            reason = "unmeasured"
        elif r.get("pool_consistent") is False or "pool_inconsistent" in diagnostics:
            reason = "pool_inconsistent"
        elif "analysis_failed" in diagnostics:
            reason = "analysis_failed"
        elif ("model_ambiguous" in diagnostics
              or r.get("track_state") in {"ambiguous", "provisional", "missing", "blocked"}):
            reason = "model_ambiguous"
        elif "diminishing_returns" in diagnostics or "interval_nonshrinking" in diagnostics:
            reason = "diminishing_returns"
        elif r.get("linewidth_state") == "resolution_limited":
            reason = "linewidth_resolution_limited"
        else:
            reason = "interval_above_tolerance"
        r["reason_detail"] = detail
        r["reason"] = reason
        r["reason_source"] = "master_assessment"

        # Interval-honesty flags used by _fmt_interval in downstream tables.
        r["interval_reliable"] = (reason == "met" and r.get("calibrated_halfwidth") is not None)
        r["_n_flagged"] = len(flagged)
        r["_n_collate"] = sum(1 for x in by_hkl.get(key, []) if x.get("pooled")) if key else 0
        out.append(r)

    return out


# ---------------------------------------------------------------------------------------------------
# Figures (matplotlib imported inside the builders; the renderer saves via captions.save_captioned)
# ---------------------------------------------------------------------------------------------------


def scan_spend_figure(events: list, per_q_rows: list, results: list):
    """Per-q stacked bar of physical acquisition spend by kind.

    Primary source: ``run`` events (each carries ``hkl`` + ``kind``). Fallback (logs without run
    events): results.csv rows + per-q ``n_production``."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    kinds = ["quicklook", "production", "confirm"]
    colors = {"quicklook": "#9ecae1", "production": "#2266cc", "confirm": "#08519c"}

    runs = _run_events(events)
    counts: dict = {}
    if runs:
        for p in runs:
            hkl = p.get("hkl")
            if not hkl:
                continue
            key = _hkl_key(hkl)
            kind = p.get("kind") or "production"
            if kind not in kinds:
                kind = "production"
            counts.setdefault(key, Counter())[kind] += 1
        source = "run events"
    else:
        for r in per_q_rows:
            hkl = r.get("hkl")
            if not hkl:
                continue
            key = _hkl_key(hkl)
            c = counts.setdefault(key, Counter())
            c["production"] += int(r.get("n_production") or 0)
        source = "results.csv + per-q counts"

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    if not counts:
        ax.text(0.5, 0.5, "no scan-spend records", ha="center", va="center", transform=ax.transAxes)
        ax.set_xlabel("q"); ax.set_ylabel("scans")
        return fig

    hkls = sorted(counts.keys())
    ci = _dominant_component([list(h) for h in hkls])
    xs = [h[ci] for h in hkls]
    bottom = [0.0] * len(hkls)
    for kind in kinds:
        vals = [counts[h].get(kind, 0) for h in hkls]
        if not any(vals):
            continue
        ax.bar(xs, vals, width=0.03, bottom=bottom, color=colors[kind], label=kind)
        bottom = [b + v for b, v in zip(bottom, vals)]
    ax.set_xlabel(f"q component {'hkl'[ci]} (rlu)")
    ax.set_ylabel("scans")
    ax.set_title(f"Per-q scan spend by kind  ·  source: {source}")
    ax.legend(fontsize=8, loc="best")
    return fig


def budget_timing_figure(events: list, summary: dict):
    """Two panels — (left) neutron & counting-second ledgers vs budget, (right) wall split by stage.

    Falls back to a text panel when there are no timing records."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    budget = summary.get("budget") or {}
    runs = _run_events(events)
    neutrons_run = sum(float(p.get("cost") or 0.0) for p in runs)
    split = _wall_split(events)

    fig, (a0, a1) = plt.subplots(1, 2, figsize=(10.5, 4.2))

    # -- left: ledgers ------------------------------------------------------------------------------
    amount = budget.get("amount")
    kind = budget.get("kind")
    spent = budget.get("spent")
    labels, vals, colors = [], [], []
    if budget.get("neutrons") is not None or neutrons_run:
        labels.append("neutrons\n(Σ run cost)")
        vals.append(neutrons_run or float(budget.get("neutrons") or 0.0))
        colors.append("#2266cc")
    if amount is not None:
        labels.append(f"seconds spent\n/ {_num(amount)} {kind or ''}")
        vals.append(float(spent or 0.0))
        colors.append("#e08a00")
    if labels:
        a0.bar(range(len(labels)), vals, color=colors)
        a0.set_xticks(range(len(labels)))
        a0.set_xticklabels(labels, fontsize=8)
        a0.set_ylabel("count")
        a0.set_yscale("log")
    a0.set_title("Budget ledgers")

    # -- right: wall split --------------------------------------------------------------------------
    if split is None:
        a1.text(0.5, 0.5, "no timing records", ha="center", va="center", transform=a1.transAxes)
        a1.set_title("Wall time")
    else:
        per_stage = split["per_stage"]
        top = [(s, v) for s, v in per_stage.most_common() if s != "iteration"][:8]
        a1.barh([s for s, _ in top][::-1], [v for _, v in top][::-1], color="#4477aa")
        a1.set_xlabel("seconds")
        a1.set_title("Wall time by stage (top 8)")
    fig.tight_layout()
    return fig


def scan_fit_figure(record, det_id: str, row: dict):
    """Per-scan data + reconstructed fit + pull-residual strip (docs/phase3_gold_check.py::fig_audit).

    ``record`` is a parsed ScanRecord; the fit is rebuilt from stored params via
    :func:`isar.report.audit.reconstruct_curve` (an NNLS amplitude solve — NOT a refit). Returns a
    matplotlib Figure; the caller saves it through :func:`captions.save_captioned`."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    from isar.report.audit import reconstruct_curve
    from isar.schema import axes as _axes

    energy = np.asarray(_axes.scanned_axes(record)[0].values, dtype=float)
    sp = record.spectra[det_id]
    counts_all = np.asarray(sp.intensity, dtype=float)
    # The stored intensity carries NaN at masked / invalid points (the QC mask is honored, never
    # applied — §0.4). The NNLS amplitude solve rejects non-finite input, so reconstruct on the
    # KEPT, finite points only (matching what the fit itself used) and plot those.
    mask = np.asarray(sp.mask, dtype=bool) if sp.mask is not None else np.ones(counts_all.shape, bool)
    keep = mask & np.isfinite(energy) & np.isfinite(counts_all)
    energy = energy[keep]
    counts = counts_all[keep]
    if energy.size == 0:
        raise ValueError("no finite kept points to reconstruct")
    err = np.sqrt(np.clip(counts, 1.0, None))
    xf = np.linspace(float(energy.min()), float(energy.max()), 240)

    rc = reconstruct_curve(energy, counts, omega0=row["omega0"], gamma=row["gamma"],
                           center=row["zero"], gauss_fwhm=row["gauss_fwhm"],
                           lorentz_fwhm=row["lorentz_fwhm"], T=row.get("T") or 0.0, x_eval=xf)
    total_at_x = reconstruct_curve(energy, counts, omega0=row["omega0"], gamma=row["gamma"],
                                   center=row["zero"], gauss_fwhm=row["gauss_fwhm"],
                                   lorentz_fwhm=row["lorentz_fwhm"], T=row.get("T") or 0.0).total
    pull = (counts - total_at_x) / err

    fig, (a0, a1) = plt.subplots(2, 1, figsize=(5.4, 5.2), sharex=True,
                                 gridspec_kw={"height_ratios": [3, 1]})
    a0.errorbar(energy, counts, yerr=err, fmt="o", ms=3, color="k", label="data", zorder=5)
    a0.plot(rc.x, rc.total, "-", color="C1", lw=2.0, zorder=4, label="reconstructed fit")
    for label, vals in rc.components:
        a0.plot(rc.x, vals, "--", lw=1.0, alpha=0.8, label=label)
    disp = row.get("disposition") or "—"
    verdict = row.get("verdict") or "—"
    a0.set_title(f"scan {row.get('scan')} · {det_id} · {disp}/{verdict}\n"
                 f"ω₀={_num(row.get('omega0'), 3)} meV · interval {_fmt_interval(row)}", fontsize=8)
    a0.set_ylabel("counts")
    a0.legend(fontsize=6, loc="best")
    a1.axhline(0, color="0.6", lw=0.8)
    for s in (1.0, -1.0):
        a1.axhline(s, color="0.85", lw=0.6)
    a1.plot(energy, pull, "s-", ms=2.5, color="C1")
    a1.set_xlabel("energy transfer (meV)")
    a1.set_ylabel("pull")
    return fig


# ---- Stage-I re-parse of <workspace>/data (raw counts behind the figures) --------------------------


def _load_records(workspace: Path) -> dict:
    """Card + ingest re-parse of ``<workspace>/data`` — the SAME boundary ``isar run`` uses. Returns
    ``{scan_number: ScanRecord}``. Deterministic; no fitting. Any failure → ``{}`` (figures degrade)."""
    data_dir = workspace / "data"
    if not data_dir.exists():
        return {}
    try:
        from isar.reduce import combine
        from isar.session import ingest
        from isar.session.workspace import Workspace
        ws = Workspace.open(workspace)
        card, _session = ws.load()
        plan = ingest.discover([str(data_dir)], card)
        records = ingest.parse_all(plan, card)
        for rec in records:
            try:
                combine.reduce_record(rec, card)
            except Exception:  # noqa: BLE001 — a reduce hiccup on one scan must not sink the set
                pass
        return {int(rec.scan_number): rec for rec in records}
    except Exception:  # noqa: BLE001
        return {}


def _reconstructable(row: dict) -> bool:
    """A results.csv row can be figured iff it carries a fitted ω₀ and all reconstruction params."""
    if row.get("omega0") is None:
        return False
    if row.get("pooled"):
        return False
    for k in ("gamma", "zero", "gauss_fwhm", "lorentz_fwhm"):
        if row.get(k) is None:
            return False
    return True


def _render_scan_figure(records: dict, row: dict, out_path: Path) -> tuple[str, str | None]:
    """Render one per-scan figure. Returns a truthful status:

    - ``("ok", None)``   — figure rendered and saved;
    - ``("skip", None)`` — genuinely non-reconstructable (params missing / no parsed record);
    - ``("fail", msg)``  — reconstruction params were present but the render RAISED (a reconstruction
      bug, not missing data). ``msg`` carries ``scan/det: <repr>`` for the §6 defect.

    The two skip categories are kept separate on purpose: a missing-param skip is an honest gap, a
    render failure is a defect the report must surface (§8 non-goal: never paper over)."""
    if not _reconstructable(row):
        return ("skip", None)
    scan = row.get("scan")
    det = row.get("detector")
    if scan is None or det is None:
        return ("skip", None)
    rec = records.get(int(scan))
    if rec is None or det not in getattr(rec, "spectra", {}):
        return ("skip", None)
    try:
        fig = scan_fit_figure(rec, det, row)
        _captions.save_captioned(fig, out_path, "campaign/scan")
        return ("ok", None)
    except Exception as e:  # noqa: BLE001 — capture the cause; never sink the report on one bad scan
        return ("fail", f"{scan}/{det}: {e!r}")


# ---------------------------------------------------------------------------------------------------
# The eight sections
# ---------------------------------------------------------------------------------------------------


def _fig_rel(name: str) -> str:
    return f"figures/{name}.png"


def _section_objective(doc: FindingsDoc, di: DiagInputs) -> None:
    doc.section("1. Objective")
    obj = di.summary.get("objective") or {}
    meta = obj.get("meta") or {}
    goal = meta.get("goal") or meta.get("goal_text") or obj.get("name") or "(objective unrecorded)"
    path = obj.get("path") or []
    endpoints = f"{_hkl_str(path[0])} → {_hkl_str(path[-1])}" if path else "—"
    budget = obj.get("budget") or di.summary.get("budget") or {}
    rows = [
        ("natural-language goal", goal),
        ("path (endpoints)", f"{endpoints} · {len(path)} grid points"),
        ("tolerance", f"{_num(obj.get('tolerance_meV'))} meV (calibrated 68% halfwidth)"),
        ("budget", f"{_num(budget.get('amount'))} {budget.get('kind', '')}"),
        ("tier", obj.get("tier") or "—"),
    ]
    doc.table(["field", "value"], rows)
    doc.para("**Pass criteria:** deliver a converged ω₀ (calibrated halfwidth ≤ tolerance) at every "
             "reachable grid q, refuse structurally unmeasurable q, and stop with an honest, "
             "machine-legible reason.")


def _section_outcome(doc: FindingsDoc, di: DiagInputs, per_q: list, ec: EventCounts) -> None:
    doc.section("2. Outcome at a glance")
    stop = di.summary.get("stop") or {}
    n_conv = sum(1 for r in per_q if r.get("converged"))
    n_total = len(per_q)
    state = stop.get("state") or "(no summary — crashed campaign)"

    # budget
    budget = di.summary.get("budget") or (di.summary.get("objective") or {}).get("budget") or {}
    amount = budget.get("amount")
    spent = budget.get("spent")
    kind = budget.get("kind")
    budget_cell = f"{_num(spent)} / {_num(amount)} {kind or ''}"
    verify_units = (kind == "counting_seconds" and amount and spent is not None
                    and float(spent) < 0.01 * float(amount) and ec.kind["run"] > 0)
    if verify_units:
        budget_cell += "  ⚠ verify-units"
        di.defects.append("budget ledger reports counting-seconds spent < 1% of the amount while "
                          f"{ec.kind['run']} run events exist ({_num(spent)} / {_num(amount)} s) — "
                          "flag verify-units until the deterministic-tier virtual clock is confirmed")

    # wall split
    split = _wall_split(di.events)
    if split is None:
        wall_cell = "n/a (no timing records)"
    else:
        tot = split["fitting"] + split["instrument"] + split["overhead"]
        wall_cell = (f"{_num(tot / 60.0, 3)} min total — fitting {_num(split['fitting'] / 60.0, 3)} / "
                     f"instrument {_num(split['instrument'], 3)} s / "
                     f"overhead {_num(split['overhead'], 3)} s")

    # center accuracy
    if di.score is not None:
        acc_cell = (f"median |Δω₀| = {_num(di.score.get('median_abs_delta'), 3)} meV · "
                    f"coverage68 = {_num(di.score.get('coverage68'), 3)} "
                    f"({di.score.get('n_scored', 0)} scored)")
    else:
        acc_cell = "no truth source (score.json absent)"

    # interval health over non-pooled production rows
    certified = unreliable = no_interval = 0
    for r in di.results:
        if r.get("pooled") or r.get("omega0") is None:
            continue
        if r.get("interval_reliable") is False:
            unreliable += 1
        elif r.get("interval_lo") is not None and r.get("interval_hi") is not None:
            certified += 1
        else:
            no_interval += 1
    health_cell = f"{certified} certified / {unreliable} unreliable / {no_interval} no-interval"

    rows = [
        ("grid q converged", f"{n_conv} / {n_total}"),
        ("stop state", f"{state} — {ec.kind['run']} scans, {ec.kind['propose']} proposals"),
        ("budget spent", budget_cell),
        ("wall time", wall_cell),
        ("center accuracy vs truth", acc_cell),
        ("interval health", health_cell),
    ]
    doc.table(["metric", "value"], rows)
    doc.figure_block("campaign/dispersion_truth", _fig_rel("campaign_dispersion_truth"))
    doc.figure_block("campaign/scan_spend", _fig_rel("campaign_scan_spend"))
    doc.figure_block("campaign/budget_timing", _fig_rel("campaign_budget_timing"))


def _first_last(events: list, kind: str, pred=None):
    got = [ev for ev in events if ev.get("kind") == kind and (pred is None or pred(ev.get("payload") or {}))]
    if not got:
        return None, None
    return got[0].get("payload") or {}, got[-1].get("payload") or {}


def _section_driver(doc: FindingsDoc, di: DiagInputs, per_q: list, ec: EventCounts) -> None:
    doc.section("3. What the driver did")
    events = di.events
    steps: list = []

    # 1 campaign start
    start_p, _ = _first_last(events, "decide", lambda p: (p.get("action") or p.get("event")) == "campaign_start")
    if start_p is not None:
        resumed = start_p.get("resumed") or {}
        r = f" (resumed: {resumed.get('n_observations', 0)} obs)" if resumed.get("n_observations") else ""
        steps.append(f"**Campaign start.** Objective logged; calibration seeded{r}.")

    # 2 workspace init
    if ec.decide_action.get("workspace_init"):
        steps.append(f"**Workspace init.** Auto-init'd after the first scan "
                     f"({ec.decide_action['workspace_init']} event).")

    # 3 proposals by kind (from run events)
    runs = _run_events(events)
    kind_counter = Counter(p.get("kind") or "production" for p in runs)
    if kind_counter:
        parts = ", ".join(f"{v} {k}" for k, v in sorted(kind_counter.items()))
        steps.append(f"**Proposals & runs.** {ec.kind['propose']} proposals → {ec.kind['run']} scans "
                     f"submitted ({parts}).")

    # 4 validations + rejections
    val_reject = sum(1 for ev in events if ev.get("kind") == "validate"
                     and not (ev.get("payload") or {}).get("would_queue", True))
    if ec.kind["validate"]:
        steps.append(f"**Validation.** {ec.kind['validate']} TAVI validations, "
                     f"{val_reject} rejected as infeasible.")

    # 5 analyze + errors_tier breakdown
    tiers = Counter((ev.get("payload") or {}).get("errors_tier")
                    for ev in events if ev.get("kind") == "analyze")
    tiers = Counter({k: v for k, v in tiers.items() if k})
    if ec.kind["analyze"]:
        tier_str = ", ".join(f"{v} {k}" for k, v in sorted(tiers.items())) or "tier unrecorded"
        steps.append(f"**Analyze.** {ec.kind['analyze']} ISAR analyses ({tier_str}).")

    # 6 escalations (escalate_fit) — Part-1 event; pre-event log fallback
    n_promote = sum(1 for ev in events if ev.get("kind") == "escalate_fit"
                    and (ev.get("payload") or {}).get("phase") == "promote")
    outcomes = Counter((ev.get("payload") or {}).get("outcome")
                       for ev in events if ev.get("kind") == "escalate_fit"
                       and (ev.get("payload") or {}).get("phase") == "outcome")
    if ec.kind["escalate_fit"]:
        obreak = ", ".join(f"{v} {k}" for k, v in outcomes.items() if k) or "no outcomes recorded"
        steps.append(f"**Tier escalation.** {n_promote} full-tier promotions ({obreak}).")
    else:
        full_analyze = sum(1 for ev in events if ev.get("kind") == "analyze"
                           and (ev.get("payload") or {}).get("errors_tier") == "full")
        steps.append("**Tier escalation.** no escalate_fit records (pre-event log; tier changes "
                     f"inferred from analyze.errors_tier only — {full_analyze} full-tier analyses).")

    # 7 recenter attempts
    n_recenter = ec.exception_action.get("recenter", 0)
    n_refused = ec.exception_action.get("recenter_refused_elastic", 0)
    if "recenter" in ec.exception_action or "recenter_refused_elastic" in ec.exception_action:
        steps.append(f"**Recenter.** {n_recenter} window recenters, {n_refused} refused onto the "
                     "elastic line.")
    else:
        steps.append("**Recenter.** 0 recenter records (pre-event log — recenter_refused_elastic "
                     "guard predates this run).")

    # 8 model updates + converged trajectory
    mu = ec.decide_action.get("model_update", 0)
    mu_first, mu_last = _first_last(events, "decide", lambda p: (p.get("action") or p.get("event")) == "model_update")
    if mu:
        first_c = (mu_first or {}).get("converged")
        last_c = (mu_last or {}).get("converged")
        n_grid = (mu_last or {}).get("n_grid")
        steps.append(f"**Model updates.** {mu} updates; converged {first_c} → {last_c} of "
                     f"{n_grid} grid q over the run.")

    # 9 certify presence
    ran_certify = bool(ec.decide_action.get("certify_start") or ec.decide_action.get("certify_done"))
    steps.append("**Certification.** " + ("one forced full bootstrap+profile certify pass ran."
                 if ran_certify else "no certify pass (campaign did not stop 'met')."))

    # 10 stop
    stop = di.summary.get("stop") or {}
    if stop:
        steps.append(f"**Stop.** state `{stop.get('state')}` — {len(per_q)} grid q evaluated; "
                     f"reasons: {'; '.join(str(x) for x in (stop.get('reasons') or [])[:2]) or '—'}.")
    else:
        steps.append("**Stop.** no campaign_summary.json (crashed campaign — see §6).")

    for i, s in enumerate(steps[:10], 1):
        doc.raw(f"{i}. {s}")
    doc.raw("")


def _q_omega_cell(row: dict) -> str:
    """ω₀ for §4 — honesty: reject/junk/no-signal q never show a fitted number."""
    reason = row.get("reason")
    if reason in ("unmeasured", "infeasible", "analysis_failed") or row.get("omega0") is None:
        return "—"
    return _num(row.get("omega0"), 4)


def _truth_lookup(score: dict | None) -> dict:
    """{rounded hkl → (omega_true, ...)} from score.json rows."""
    out: dict = {}
    if not score:
        return out
    for r in score.get("rows") or []:
        hkl = r.get("hkl")
        if hkl is None:
            continue
        out[_hkl_key(hkl)] = r
    return out


def _section_dispersion(doc: FindingsDoc, di: DiagInputs, per_q: list) -> None:
    doc.section("4. Delivered dispersion vs truth")
    tracks = (di.card_state or {}).get("branch_tracks") or {}
    if tracks:
        frontier = tracks.get("frontiers") or {}
        assignments = tracks.get("assignments") or []
        has_acoustic = any(
            isinstance(assignment.get("acoustic_candidate_index"), int)
            and 0 <= assignment["acoustic_candidate_index"]
            < len(assignment.get("candidates") or [])
            for assignment in assignments)
        doc.para(
            f"Tracked-branch revision {tracks.get('revision', '—')}; anchor path index "
            f"{tracks.get('anchor_index', '—')}; connected target points: "
            f"{len(frontier.get('confirmed_indices') or [])}. "
            + ("The connected frontier is blocked by unresolved identity. "
               if frontier.get("frontier_blocked") else "The reachable identity frontier is connected. ")
            + ("Acoustic identified; retained as non-target context." if has_acoustic else ""))
        assignment_rows = []
        for assignment in assignments:
            candidates = assignment.get("candidates") or []
            oi = assignment.get("optic_candidate_index")
            ai = assignment.get("acoustic_candidate_index")
            optic = candidates[oi] if isinstance(oi, int) and 0 <= oi < len(candidates) else {}
            acoustic = candidates[ai] if isinstance(ai, int) and 0 <= ai < len(candidates) else {}
            assignment_rows.append([
                assignment.get("path_index"), _hkl_str(assignment.get("q")),
                _num(optic.get("energy_meV")),
                _num((optic.get("probabilities") or {}).get("optic"), 3),
                _num(acoustic.get("energy_meV")),
                _num((acoustic.get("probabilities") or {}).get("acoustic"), 3),
                assignment.get("track_state") or "—",
            ])
        if assignment_rows:
            doc.table(["path", "hkl", "target ω", "P(target)", "rival ω", "P(acoustic)",
                       "frontier state"], assignment_rows)
    truth = _truth_lookup(di.score)
    have_truth = bool(truth)
    headers = ["q", "hkl", "ω₀ (meV)", "halfwidth", "source", "reason", "n_prod", "collate-only"]
    if have_truth:
        headers += ["ω_true", "Δ", "in interval"]
    rows = []
    for r in per_q:
        hw = r.get("calibrated_halfwidth")
        if r.get("interval_reliable"):
            hw_cell = _num(hw)
        elif r.get("omega0") is not None:
            hw_cell = _fmt_interval(r)   # delivered a centre but the bar is flagged → (unreliable)
        else:
            hw_cell = "—"               # nothing delivered here
        base = [
            _num(r.get("q"), 3), _hkl_str(r.get("hkl")), _q_omega_cell(r), hw_cell,
            r.get("source") or "—", r.get("reason") or "—",
            r.get("n_production") or 0, r.get("_n_collate") or 0,
        ]
        if have_truth:
            key = _hkl_key(r.get("hkl")) if r.get("hkl") else None
            tr = truth.get(key)
            if tr is not None and r.get("omega0") is not None:
                ot = tr.get("omega_true")
                delta = float(r["omega0"]) - float(ot)
                inside = (hw is not None and r.get("interval_reliable")
                          and abs(delta) <= hw)
                base += [_num(ot, 4), _num(delta, 3), "yes" if inside else "no"]
            else:
                base += ["—", "—", "—"]
        rows.append(base)
    doc.table(headers, rows)


# ---- §5 failure dossier ----------------------------------------------------------------------------


def _zones(per_q: list) -> list:
    """Maximal runs of CONSECUTIVE grid-path indices sharing the same reason enum (not None/'met')."""
    zones: list = []
    cur: list = []
    cur_reason = None
    for r in per_q:
        reason = r.get("reason")
        if reason in (None, "met"):
            if cur:
                zones.append((cur_reason, cur))
                cur, cur_reason = [], None
            continue
        if reason == cur_reason:
            cur.append(r)
        else:
            if cur:
                zones.append((cur_reason, cur))
            cur, cur_reason = [r], reason
    if cur:
        zones.append((cur_reason, cur))
    return zones


_DIAGNOSIS = {
    "unmeasured": ("No pooled master has been published at {n} q. Individual scan fits cannot stand in "
                   "for it; acquire/collate at least one feasible member."),
    "interval_above_tolerance": ("The model-robust pooled ω interval remains above tolerance at {n} q. "
                                 "Continue with the best master-predictive acquisition."),
    "model_ambiguous": ("Pooled evidence at {n} q leaves model or branch identity unresolved. Prefer "
                        "measurements that reduce model/branch entropy."),
    "pool_inconsistent": ("Member spectra fail the seeded exposure-scaled Poisson consistency check at "
                          "{n} q. No data were excluded; review drift/normalization and gather evidence."),
    "linewidth_resolution_limited": ("Γ is resolution-limited at {n} q. This is diagnostic rather than "
                                     "terminal; ω may certify when its model-marginal interval is robust."),
    "analysis_failed": ("Master analysis failed at {n} q. Resume the fingerprinted checkpoint revision "
                        "before requesting another scan when no predictive basis exists."),
    "diminishing_returns": ("Expected information is small or the master interval is not shrinking at "
                            "{n} q. This remains nonterminal until a global limit is reached."),
    "infeasible": ("An independent instrument/geometry/calibration proof marks {n} q infeasible; poor "
                   "fits or target absence alone are insufficient for this state."),
}


def _adjacent_success(per_q: list, zone_rows: list):
    """A converged q adjacent to the zone, for contrast."""
    idxs = [i for i, r in enumerate(per_q) if r in zone_rows]
    if not idxs:
        return None
    for j in (min(idxs) - 1, max(idxs) + 1):
        if 0 <= j < len(per_q) and per_q[j].get("converged"):
            return per_q[j]
    return None


def _section_dossier(doc: FindingsDoc, di: DiagInputs, per_q: list, records: dict,
                     scan_fig_dir: Path) -> tuple[list, int]:
    """Render one subsection per failure zone. Returns (reasons seen, dossier figures rendered)."""
    doc.section("5. Failure dossier")
    zones = _zones(per_q)
    if not zones:
        doc.para("No failure zones — every grid q either converged or is still in progress.")
        return [], 0

    by_hkl = _results_by_hkl(di.results)
    reasons_seen: list = []
    n_fig = 0
    for zi, (reason, zrows) in enumerate(zones, 1):
        reasons_seen.append(reason)
        qs = [r.get("q") for r in zrows if r.get("q") is not None]
        qa = _num(min(qs), 3) if qs else "?"
        qb = _num(max(qs), 3) if qs else "?"
        pt = "point" if len(zrows) == 1 else "points"
        doc.raw(f"### 5.{zi} q={qa}..{qb} ({len(zrows)} {pt}): {reason}")
        doc.raw("")

        # evidence table: per-q rows + the specific results.csv rows at those hkl. Collate-only
        # (branch_continuation) rows are NOT listed row-by-row (§4) — counted per zone instead.
        ev_rows = []
        n_collate = 0
        for r in zrows:
            key = _hkl_key(r.get("hkl")) if r.get("hkl") else None
            for x in by_hkl.get(key, []):
                if x.get("pooled"):
                    continue
                disp = x.get("disposition")
                if disp in _COLLATE_DISPOSITIONS:
                    n_collate += 1
                    continue
                show_omega = disp not in _REJECT_DISPOSITIONS
                ev_rows.append([
                    _num(r.get("q"), 3), x.get("scan"), x.get("detector") or "—",
                    disp or "—", x.get("verdict") or "—",
                    _num(x.get("omega0"), 3) if show_omega else "—",
                    _fmt_interval(x),
                ])
        if ev_rows:
            doc.table(["q", "scan", "det", "disposition", "verdict", "ω₀", "interval"], ev_rows[:24])
        if n_collate:
            doc.para(f"*(+{n_collate} collate-only branch_continuation rows at these q — see "
                     "results.csv; not listed individually.)*")

        # figures — up to 3 worst-first (reject/unreliable first) + 1 contrast success
        cand = []
        for r in zrows:
            key = _hkl_key(r.get("hkl")) if r.get("hkl") else None
            for x in by_hkl.get(key, []):
                if _reconstructable(x):
                    bad = (x.get("interval_reliable") is False
                           or (x.get("disposition") in _REJECT_DISPOSITIONS))
                    cand.append((0 if bad else 1, x))
        cand.sort(key=lambda t: t[0])
        for _, x in cand[:3]:
            name = f"scan_{x.get('scan')}_{x.get('detector')}"
            path = scan_fig_dir / f"{name}.png"
            status, msg = _render_scan_figure(records, x, path)
            if status == "ok":
                n_fig += 1
                doc.figure_block("campaign/scan", f"figures/dossier/{name}.png",
                                 alt=f"scan {x.get('scan')} evidence")
            elif status == "fail":
                di.render_failed.append(msg)

        succ = _adjacent_success(per_q, zrows)
        if succ is not None:
            key = _hkl_key(succ.get("hkl")) if succ.get("hkl") else None
            for x in by_hkl.get(key, []):
                if _reconstructable(x):
                    name = f"scan_{x.get('scan')}_{x.get('detector')}_contrast"
                    path = scan_fig_dir / f"{name}.png"
                    status, msg = _render_scan_figure(records, x, path)
                    if status == "ok":
                        n_fig += 1
                        doc.figure_block("campaign/scan", f"figures/dossier/{name}.png",
                                         alt=f"adjacent converged q={_num(succ.get('q'), 3)} (contrast)")
                    elif status == "fail":
                        di.render_failed.append(msg)
                    break

        # diagnosis (fixed per-enum template)
        tmpl = _DIAGNOSIS.get(reason)
        if tmpl:
            doc.para("**Diagnosis.** " + tmpl.format(n=len(zrows)))
        todo = _REASON_TODO.get(reason, "(new)")
        doc.para(f"**Fix →** TODO: {todo}")

    return reasons_seen, n_fig


# ---- §6 defects ------------------------------------------------------------------------------------


def _section_defects(doc: FindingsDoc, di: DiagInputs, per_q: list, ec: EventCounts,
                     reasons_seen: list) -> None:
    doc.section("6. Defects uncovered")

    # additional predicates (budget verify-units + stop-reason contradiction already appended)
    # A master analysis failure with no escalation record is a genuine observability defect.
    if "analysis_failed" in reasons_seen and ec.kind["escalate_fit"] == 0:
        di.defects.append("an analysis_failed master zone exists but the log has zero escalate_fit "
                          "records — the master-analysis escalation is not observable")
    # kind_unknown
    if ec.kind_unknown > 0:
        di.defects.append(f"{ec.kind_unknown} session-log events carry an out-of-vocabulary kind "
                          "(kind_unknown) — a logging-vocabulary drift")
    # per_q rows missing reason (pre-S3)
    n_derived = sum(1 for r in per_q if r.get("reason_source", "").startswith("derived"))
    if n_derived:
        di.defects.append(f"{n_derived} per-q rows lack a stop reason in campaign_summary.json "
                          "(pre-S3 summary) — reasons back-derived from results.csv evidence here")
    # decide events missing action (pre-Part-1)
    n_decide = ec.kind["decide"]
    n_decide_action = sum(1 for ev in di.events if ev.get("kind") == "decide"
                          and (ev.get("payload") or {}).get("action") is not None)
    if n_decide and n_decide_action == 0:
        di.defects.append("decide events carry no 'action' field (pre-Part-1 log) — actions were "
                          "fingerprinted from the legacy 'event' field")
    # ledger cross-check
    runs = _run_events(di.events)
    budget = di.summary.get("budget") or {}
    if budget.get("kind") == "neutrons" and budget.get("spent") is not None and runs:
        derived = sum(float(p.get("cost") or 0.0) for p in runs)
        spent = float(budget["spent"])
        if spent > 0 and abs(derived - spent) / spent > 0.05:
            di.defects.append(f"ledger cross-check: Σ run-event cost ({_num(derived)}) vs budget.spent "
                              f"({_num(spent)}) disagree by >5%")
    # score absent while tier is deterministic
    tier = (di.summary.get("objective") or {}).get("tier")
    if di.score is None and tier == "deterministic":
        di.defects.append("truth is available for the deterministic tier but score.json is absent — "
                          "the run went unscored")

    # crashed-campaign note
    if not di.summary:
        di.defects.append("campaign_summary.json is absent — this report was assembled from the "
                          "session log + results.csv alone (crashed or interrupted campaign)")

    # per-scan figure render failures: reconstruction params WERE present but the render raised — a
    # reconstruction bug, not missing data. Surfaced here (never papered over as a soft §8 footnote).
    if di.render_failed:
        uniq = sorted(dict.fromkeys(di.render_failed))
        scans = ", ".join(m.split(":", 1)[0].split("/")[0] for m in uniq)
        di.defects.append(f"per-scan figure render failed for {len(uniq)} scan(s) [{scans}] despite "
                          f"complete reconstruction params (first: {uniq[0]}) — reconstruction bug, "
                          "not missing data")

    numbered = [d for d in di.defects if not isinstance(d, tuple)]
    if not numbered:
        doc.para("No machinery defects fired.")
        return
    doc.caveat("The items below are machinery (logging / accounting / presentation) defects surfaced "
               "mechanically — not physics failures.")
    for i, d in enumerate(numbered, 1):
        doc.raw(f"{i}. {d}")
    doc.raw("")


# ---- §7 verdict ------------------------------------------------------------------------------------


def _section_verdict(doc: FindingsDoc, di: DiagInputs, per_q: list, reasons_seen: list) -> None:
    doc.section("7. Verdict & recommended actions")
    stop = di.summary.get("stop") or {}
    state = stop.get("state") or "unknown (crashed)"
    n_conv = sum(1 for r in per_q if r.get("converged"))
    doc.para(f"Stop state **{state}**; {n_conv}/{len(per_q)} grid q converged. Priority actions, most "
             "impactful first:")
    actions: list = []
    seen = set()
    for reason in reasons_seen:
        if reason in seen:
            continue
        seen.add(reason)
        todo = _REASON_TODO.get(reason, "(new)")
        actions.append(f"Resolve `{reason}` zones → TODO: {todo}")
    for d in di.defects:
        if isinstance(d, tuple):
            continue
        head = d.split(" — ")[0]
        actions.append(f"{head} → TODO: logging/accounting fix")
    if not actions:
        actions.append("No outstanding failure zones or defects — campaign is clean.")
    doc.bullets(actions)


# ---- §8 provenance ---------------------------------------------------------------------------------


def _section_provenance(doc: FindingsDoc, di: DiagInputs, n_dossier_figs: int, n_buried: int,
                        n_nonreconstructable: int) -> None:
    doc.section("8. Provenance")
    checkpoint_root = di.workspace / "report" / "certification"
    checkpoint_files = ([path for path in checkpoint_root.rglob("*") if path.is_file()]
                        if checkpoint_root.exists() else [])
    checkpoint_bytes = sum(path.stat().st_size for path in checkpoint_files)
    analysis_root = di.workspace / "report" / "analysis"
    analysis_files = ([path for path in analysis_root.rglob("*") if path.is_file()]
                      if analysis_root.exists() else [])
    analysis_bytes = sum(path.stat().st_size for path in analysis_files)
    rows = [
        ("§1 objective, §2 budget/stop, §4 per-q status", "report/campaign_summary.json"),
        ("§2 wall split, §3 driver narrative, event counts", "report/session_log.jsonl"),
        ("§2 interval health, §4/§5 per-scan verdicts & params", "report/results.csv"),
        ("§2 accuracy, §4 truth columns", "score.json" if di.score is not None else "score.json (absent)"),
        ("physics model / dispersion prior", "card.state.json"),
        ("§5 dossier figures — results.csv params + data/*.json re-parse, reconstruct_curve",
         f"{n_dossier_figs} rendered under report/figures/dossier/"),
        ("buried per-scan set — every non-pooled fitted row, same reconstruct_curve path",
         f"{n_buried} rendered under report/figures/scans/"),
        ("final-certification manifests and derived checkpoints",
         f"report/certification/ — {len(checkpoint_files)} files, "
         f"{checkpoint_bytes / (1024 * 1024):.2f} MiB"),
        ("ordinary analysis manifests and derived checkpoints",
         f"report/analysis/ — {len(analysis_files)} files, "
         f"{analysis_bytes / (1024 * 1024):.2f} MiB"),
    ]
    doc.table(["section", "source"], rows)
    doc.para("No number in this report was produced by refitting or re-scoring; every value is a "
             "re-presentation of what the campaign already recorded.")
    doc.para("Certification and ordinary-analysis revisions are retained indefinitely. They are "
             "derived artifacts; an "
             "operator may manually prune an old revision only after confirming that no published "
             "result references it. ISAR performs no automatic deletion.")
    if n_nonreconstructable:
        doc.para(f"*{n_nonreconstructable} fitted rows were genuinely non-reconstructable (a "
                 "reconstruction parameter — γ / zero / resolution — was absent) and were skipped "
                 "from the buried set; see §6 for any render failures (params present but the render "
                 "raised).*")


# ---------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------


def render_campaign_diagnostic(workspace: str | Path) -> Path:
    """Render ``<workspace>/report/CAMPAIGN_DIAGNOSTIC.md`` + figures. Returns the .md path."""
    import matplotlib
    matplotlib.use("Agg")

    di = _load(workspace)
    ws = di.workspace
    report = ws / "report"
    fig_dir = report / "figures"
    dossier_dir = fig_dir / "dossier"
    scans_dir = fig_dir / "scans"
    for d in (fig_dir, dossier_dir, scans_dir):
        d.mkdir(parents=True, exist_ok=True)

    ec = _event_counts(di.events)
    per_q = _per_q(di.summary, di.results, di.defects)

    # ---- always-on figures ------------------------------------------------------------------------
    score_rows = (di.score or {}).get("rows") if di.score else None
    _captions.save_captioned(_fit_figures.campaign_dispersion_figure(per_q, score_rows),
                             fig_dir / "campaign_dispersion_truth.png", "campaign/dispersion_truth")
    _captions.save_captioned(scan_spend_figure(di.events, per_q, di.results),
                             fig_dir / "campaign_scan_spend.png", "campaign/scan_spend")
    _captions.save_captioned(budget_timing_figure(di.events, di.summary),
                             fig_dir / "campaign_budget_timing.png", "campaign/budget_timing")

    # ---- Stage-I re-parse (shared by dossier + buried set) ----------------------------------------
    records = _load_records(ws)

    # ---- build the document -----------------------------------------------------------------------
    doc = FindingsDoc("Campaign diagnostic")
    _section_objective(doc, di)
    _section_outcome(doc, di, per_q, ec)
    _section_driver(doc, di, per_q, ec)
    _section_dispersion(doc, di, per_q)
    reasons_seen, n_dossier_figs = _section_dossier(doc, di, per_q, records, dossier_dir)

    # ---- buried set: every non-pooled fitted row (rendered BEFORE §6 so render failures feed it) ---
    n_buried = 0
    n_nonreconstructable = 0
    for r in di.results:
        if r.get("omega0") is None or r.get("pooled") or (r.get("disposition") in ("held", "abstain")):
            continue
        scan, det = r.get("scan"), r.get("detector")
        if scan is None or det is None:
            continue
        path = scans_dir / f"{scan}_{det}.png"
        status, msg = _render_scan_figure(records, r, path)
        if status == "ok":
            n_buried += 1
        elif status == "fail":
            di.render_failed.append(msg)
        else:
            n_nonreconstructable += 1

    _section_defects(doc, di, per_q, ec, reasons_seen)
    _section_verdict(doc, di, per_q, reasons_seen)
    _section_provenance(doc, di, n_dossier_figs, n_buried, n_nonreconstructable)

    out_path = report / "CAMPAIGN_DIAGNOSTIC.md"
    out_path.write_text(doc.render(), encoding="utf-8")
    return out_path
