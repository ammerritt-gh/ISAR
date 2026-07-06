"""The pipeline driver (UNIFICATION_DESIGN §2.5) — parse → reduce → route → calibrate → fit → collate.

`run_batch` is the whole pipeline behind `isar run`: idempotent (re-parses everything, re-fits only
new / content-drifted / guideline-drifted / previously-held scans), context-updates before
measurements, calibration-gated fits seeded by the prior/guidelines, verdict + confidence per scan,
results written to the ledger + `results.csv`.

`build_context` and `fit_one` generalize the two gold-check `run_pipeline` recipes — same analysis
calls, same order — with the inputs (zero, resolution, slit, prior, guidelines) derived from card +
state instead of hardcoded. Nothing in `isar/analyze/` is modified (§4).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from isar.analyze import errors, loop, select, verdict
from isar.analyze import gold as gold_mod
from isar.analyze.context import ScanContext
from isar.analyze.structures import enumerate_multimode, enumerate_structures
from isar.reduce import collate as collate_mod
from isar.reduce import combine, qc
from isar.schema.card import Card
from isar.session import calibrations, guidelines as guide_mod, ingest, router, session_prior
from isar.session.state import ScanEntry, SessionState, content_hash
from isar.session.workspace import Workspace


# ===========================================================================
# per-scan outcome
# ===========================================================================
@dataclass
class FitOutcome:
    detector_id: str
    verdict: str
    disposition: str
    confidence: float
    omega0: float | None
    omega0_err: float | None
    interval: tuple | None
    modality: str | None
    gamma: float | None
    gamma_provisional: bool
    best_label: str | None
    cost_per_dof: float | None
    q: list
    T: float
    sample: str
    prior_used: dict | None
    calib: dict
    reasons: list = field(default_factory=list)
    n_modes: int = 1
    modes: list | None = None    # per-mode dicts (canonicalized ascending by ω₀) when n_modes > 1
    # FIX 2: False when a phonon Γ railed out to its bound → the reported ω₀ interval is
    # degenerate/untrustworthy (see fit_one). Defaulted True for back-compat with older ledgers.
    interval_reliable: bool = True

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["interval"] = list(self.interval) if self.interval is not None else None
        return d


@dataclass
class RunSummary:
    outcomes: list = field(default_factory=list)     # (scan_number, FitOutcome)
    counts: dict = field(default_factory=dict)       # status/disposition tallies
    gaps: list = field(default_factory=list)
    context_lines: list = field(default_factory=list)
    n_fitted_this_run: int = 0     # scans that reached the fit/non-result stage this run
    n_reprocessed: int = 0         # scans (re)processed this run, any outcome (incl. excluded/held)
    collation: object = None


# ===========================================================================
# context + single-scan fit (the gold-check recipe, generalized)
# ===========================================================================
def build_context(record, detector_id, card: Card, calres, prior, g: guide_mod.Guidelines,
                  *, scan_number=None, q=None, T=None):
    """A ScanContext with the monitor-short mask and the guideline fit-window mask folded in.

    Returns ``(ctx, n_used, region_reasons)`` or ``(None, n_used, region_reasons)`` when too few
    points survive (undersampled non-result). P5: only ``ctx.mask`` is ever narrowed — the raw
    intensity arrays are untouched. ``region_reasons`` records the guideline masking provenance
    (empty when no ``fit_range``/``exclude_regions`` applied)."""
    ctx = ScanContext.from_scan(
        record, detector_id, card, center=calres.center,
        gauss_fwhm=calres.gauss_fwhm, lorentz_fwhm=calres.lorentz_fwhm,
        prior_omega=(prior.omega0 if prior else None))
    mask = np.asarray(ctx.mask, dtype=bool)
    norm = record.normalization
    if norm.counting_mode == "monitor" and float(norm.preset) > 0:
        mon = np.asarray(norm.monitor, dtype=float)
        mask = mask & (mon >= g.monitor_frac * float(norm.preset))

    # Guideline fit-window mask (P5: mask only; intensity never touched).
    region_reasons: list = []
    if scan_number is not None:
        fit_range, excl = g.mask_regions_for(scan_number=scan_number, q=q, T=T)
        x = np.asarray(ctx.x, dtype=float)
        if fit_range is not None:
            lo, hi = float(fit_range[0]), float(fit_range[1])
            keep = (x >= lo) & (x <= hi)
            dropped = int((mask & ~keep).sum())
            mask = mask & keep
            if dropped:
                region_reasons.append(f"fit_range [{lo:g}, {hi:g}] masked {dropped} points")
        for r in excl:
            lo, hi = float(r[0]), float(r[1])
            inside = (x >= lo) & (x <= hi)
            dropped = int((mask & inside).sum())
            mask = mask & ~inside
            if dropped:
                region_reasons.append(f"exclude_region [{lo:g}, {hi:g}] masked {dropped} points")

    ctx = replace(ctx, mask=mask)
    n_used = int(mask.sum())
    if n_used < g.min_fit_points:
        return None, n_used, region_reasons
    return ctx, n_used, region_reasons


def fit_one(ctx, card: Card, *, prior, M, seed, structure_labels=None, peaks=None):
    """Two-stage structure fit (§ multiplicity), optionally narrowed by guideline labels, then
    select + verdict. Mirrors docs/phase3_gold*_check.run_pipeline; returns extracted fields.

    ``peaks`` (guideline hints, ≥2 forces the 2-mode offer and seeds the greedy IC) are threaded
    into :func:`loop.fit_structures_two_stage`. The whitelist narrows the union of 1- and 2-mode
    labels (``stokes2+bg`` etc. pass through) — an empty match keeps all + warns."""
    results = loop.fit_structures_two_stage(ctx, minos=True, peaks=peaks)
    # by_hyp spans both stages so errors.attach can find any surviving label's hypothesis.
    by_hyp = {h.label: h for h in enumerate_structures(ctx)}
    by_hyp.update({h.label: h for h in enumerate_multimode(ctx)})

    warn = None
    if structure_labels:
        kept = [r for r in results if r.label in structure_labels]
        if kept:
            results = kept
        else:
            warn = (f"guideline structures {list(structure_labels)} match no admissible "
                    f"{[r.label for r in results]}; kept all")

    fits = {r.label: r for r in results}

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
    reasons = list(sv.reasons)
    if warn:
        reasons.append(warn)

    # FIX 2: a phonon Γ railed out to its bound makes the derived ω₀ interval degenerate (a
    # spuriously tight bootstrap ~1e-3 meV). Flag the interval unreliable + record why, rather
    # than silently trusting it — `isar score --truth` and downstream filter on this flag.
    interval_reliable = True
    if best is not None and getattr(best, "gamma_at_bound", False) and interval is not None:
        interval_reliable = False
        reasons.append("phonon Γ at bound: ω₀ interval unreliable (resolution-limited spike, "
                       "degenerate bootstrap)")

    n_modes = int(best.n_modes) if best is not None else 1
    modes = list(best.modes) if (best is not None and best.modes) else None
    return {
        "verdict": sv.verdict, "confidence": sv.confidence, "reasons": reasons,
        "omega0": omega0, "omega0_err": (best.omega0_err_hesse if best else None),
        "interval": (tuple(interval) if interval is not None else None),
        "interval_reliable": interval_reliable,
        "modality": sel.modality, "best_label": sel.best,
        "gamma": (best.gamma if best else None),
        "cost_per_dof": (best.cost_per_dof if best else None),
        "n_modes": n_modes, "modes": modes,
    }


# ===========================================================================
# the batch loop
# ===========================================================================
def _sample_of(record, card: Card) -> str:
    return (card.policy.sample or {}).get("name") or record.experiment_id or ""


def _exclusion_ids(card: Card) -> set:
    ids = set(card.policy.exclusions_seed or [])
    for e in (card.state.exclusions or []):
        if isinstance(e, dict) and "scan" in e:
            ids.add(e["scan"])
    return ids


def run_batch(workspace: Workspace, data_paths, *, force=False, llm=False, M=200,
              detectors=None, formats=("html", "markdown")) -> RunSummary:
    card, session = workspace.load()
    for p in map(str, data_paths):
        if p not in session.data_paths:
            session.data_paths.append(p)

    plan = ingest.discover(data_paths, card)
    records = ingest.parse_all(plan, card)

    # Reduce (both no-ops where not applicable; QC never raises).
    for rec in records:
        combine.reduce_record(rec, card)
        qc.qc_record(rec, card)

    # File-manifest bookkeeping.
    for f in plan.measurement_files + plan.companion_files:
        rp = str(Path(f).resolve())
        try:
            stat = Path(f).stat()
            session.files[rp] = {"size": stat.st_size, "mtime": stat.st_mtime,
                                 "format": "companion" if f in plan.companion_files else "measurement"}
        except OSError:
            pass

    # Context updates BEFORE measurements (calibration/alignment/environment).
    routed = router.route(records)
    ctx_lines = router.apply_context_updates(
        routed, card, session, companion_files=plan.companion_files)

    # Optional LLM adjudication (off critical path; degrade, never stall).
    if llm or (card.policy.adjudication or {}).get("enabled"):
        ctx_lines += _maybe_adjudicate(routed.measurements, card)

    runcal = calibrations.calibration_view(card)
    g = guide_mod.resolve(card)
    fit_dets = guide_mod.fit_detectors(g, card, cli_detectors=_split_csv(detectors))
    exclusions = _exclusion_ids(card)
    seed_base = 6000

    summary = RunSummary(gaps=ingest.file_gaps(records), context_lines=ctx_lines)

    for rec in routed.measurements:
        scan = rec.scan_number
        chash = content_hash(rec)
        for det in fit_dets:
            if det not in rec.spectra:
                continue
            sp = rec.spectra[det]
            q = [float(v) for v in (sp.q if sp.q is not None else [0.0, 0.0, 0.0])]
            T = float(np.median(np.asarray(rec.temperature.setpoint, dtype=float)))
            sample = _sample_of(rec, card)
            scan_id = f"{rec.experiment_id}:{scan}:{det}"

            gsig = guide_mod.guidelines_signature(g, scan_number=scan, q=q, T=T)
            from isar.session.state import canonical_hash
            ghash = canonical_hash(gsig)

            prev = session.get(scan_id)
            fresh = (prev is None or prev.content_hash != chash
                     or prev.guidelines_hash != ghash or prev.status.startswith("held") or force)
            if not fresh:
                # Unchanged & already resolved: keep the ledger entry, re-list its outcome for the report.
                if prev.detectors.get(det):
                    summary.outcomes.append((scan, _outcome_from_entry(prev, det)))
                continue

            entry = _process_one(rec, det, q, T, sample, scan_id, card, session, g, runcal,
                                  exclusions, chash, ghash, M=M, seed=seed_base + scan)
            session.put(entry)
            summary.n_reprocessed += 1
            summary.n_fitted_this_run += 1 if entry.status in ("fitted", "non_result") else 0
            oc = entry.detectors.get(det)
            if oc:
                summary.outcomes.append((scan, FitOutcome(**oc) if not isinstance(oc, FitOutcome) else oc))

    # Collate (all records, whole-experiment view).
    try:
        summary.collation = collate_mod.collate(records, card)
    except Exception as exc:  # noqa: BLE001 — collation is a report aid, never fail the batch on it
        summary.context_lines.append(f"collation skipped: {exc}")

    summary.counts = _tally(session)
    session.last_run = {"counts": summary.counts,
                        "scan_gaps": [f"{e}: {a}->{b}" for (e, a, b) in summary.gaps],
                        "n_fitted_this_run": summary.n_fitted_this_run}

    _write_results_csv(workspace, session)
    workspace.save(card, session)
    return summary


def _process_one(rec, det, q, T, sample, scan_id, card, session, g, runcal, exclusions,
                 chash, ghash, *, M, seed) -> ScanEntry:
    base = dict(scan_id=scan_id, scan_number=rec.scan_number, scan_type=rec.scan_type,
                content_hash=chash, guidelines_hash=ghash)

    # Exclusions (guideline / seed / state) — never fitted.
    reason = g.excluded(rec.scan_number)
    if reason is None and rec.scan_number in exclusions:
        reason = "excluded (card exclusions_seed / state)"
    if reason is not None:
        return ScanEntry(status="excluded", reasons=[reason], **base)

    # Calibration gate.
    calres = calibrations.resolve(rec, det, card, runcal)
    if calres.held:
        return ScanEntry(status="held_for_calibration", reasons=[calres.held_reason],
                         provenance={"resolution_version": calres.resolution_version}, **base)

    # Prior: user expectation beats the auto prior.
    prior = g.expectation_for(scan_number=rec.scan_number, q=q, T=T)
    prior_source = "user"
    n_neighbors = None
    if prior is None:
        prior = session_prior.prior_for(card.state, sample=sample, T=T, q=q, exclude_scan_id=scan_id)
        prior_source = "auto" if prior is not None else None
        if prior is not None:
            n_neighbors = sum(1 for p in card.state.dispersion_prior
                              if p.get("sample") == sample and p.get("scan_id") != scan_id)

    # Context + undersampled guard (min_fit_points re-checked AFTER guideline region masking).
    labels = g.structure_labels_for(scan_number=rec.scan_number, q=q, T=T)
    peaks = g.peaks_for(scan_number=rec.scan_number, q=q, T=T)
    ctx, n_used, region_reasons = build_context(
        rec, det, card, calres, prior, g, scan_number=rec.scan_number, q=q, T=T)
    if ctx is None:
        rs = [f"undersampled: {n_used} < {g.min_fit_points} kept points"] + region_reasons
        return ScanEntry(status="non_result", reasons=rs,
                         provenance={"resolution_version": calres.resolution_version}, **base)

    fit = fit_one(ctx, card, prior=prior, M=M, seed=seed, structure_labels=labels, peaks=peaks)
    fit["reasons"].extend(region_reasons)

    disposition = gold_mod.verdict_disposition(fit["verdict"])
    # omega_window demotion (report-level; verdict unchanged).
    if (g.omega_window is not None and disposition == "resolved" and fit["omega0"] is not None
            and not (g.omega_window[0] <= fit["omega0"] <= g.omega_window[1])):
        disposition = "flagged"
        fit["reasons"].append(f"omega0 {fit['omega0']:.2f} outside user window {g.omega_window}")

    prior_used = None
    if prior is not None:
        prior_used = {"omega0": prior.omega0, "sigma": prior.omega0_sigma,
                      "n_neighbors": n_neighbors, "source": prior_source}

    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=sample, prior_used=prior_used, calib=calres.summary(),
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        interval_reliable=fit.get("interval_reliable", True))

    # Feed the running prior (gated to resolved + fold).
    session_prior.add_point(card.state, scan_id=scan_id, sample=sample, T=T, q=q,
                            omega0=outcome.omega0, modality=outcome.modality,
                            verdict=outcome.verdict,
                            t_round=float((card.policy.collation or {}).get("t_round", 5.0)))

    return ScanEntry(status="fitted", reasons=outcome.reasons,
                     detectors={det: outcome.to_dict()},
                     provenance={"resolution_version": calres.resolution_version,
                                 "M": M, "seed": seed, "prior_source": prior_source}, **base)


# ===========================================================================
# helpers
# ===========================================================================
def _split_csv(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _outcome_from_entry(entry: ScanEntry, det: str) -> FitOutcome:
    return FitOutcome(**entry.detectors[det])


def _maybe_adjudicate(measurements, card) -> list[str]:
    try:
        from isar.reduce import adjudicate
        cfg = adjudicate.adjudication_config(card)
        client = adjudicate.make_client(cfg)
        adjudicate.adjudicate_records(measurements, card, client)
        return ["LLM adjudication: applied"]
    except Exception as exc:  # noqa: BLE001 — degrade, never stall the batch (DESIGN §9)
        return [f"LLM adjudication skipped (no consult): {exc}"]


def _tally(session: SessionState) -> dict:
    counts = {"by_status": {}, "by_disposition": {}}
    for e in session.scans.values():
        counts["by_status"][e.status] = counts["by_status"].get(e.status, 0) + 1
        for oc in e.detectors.values():
            d = oc.get("disposition")
            if d:
                counts["by_disposition"][d] = counts["by_disposition"].get(d, 0) + 1
    return counts


_CSV_COLUMNS = [
    "scan", "detector", "sample", "T", "h", "k", "l", "status", "verdict", "disposition",
    "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi", "interval_reliable",
    "gamma", "gamma_provisional", "n_modes", "secondary_omega0", "secondary_omega0_err",
    "best_model", "cost_per_dof", "zero", "zero_source", "gauss_fwhm",
    "lorentz_fwhm", "resolution_source", "resolution_version", "prior_omega", "prior_sigma",
    "prior_n", "prior_source", "reasons",
]


def _secondary_mode(oc: dict) -> tuple:
    """The (omega0, omega0_err) of the non-primary mode for a 2-mode fit, else (None, None).

    Primary = the mode whose ω₀ matches the reported scalar ``omega0``; secondary is the other.
    """
    if (oc.get("n_modes") or 1) != 2:
        return None, None
    modes = oc.get("modes") or []
    if len(modes) != 2:
        return None, None
    prim = oc.get("omega0")
    others = [m for m in modes if m.get("omega0") != prim]
    sec = others[0] if others else modes[-1]
    return sec.get("omega0"), sec.get("omega0_err")


def _write_results_csv(workspace: Workspace, session: SessionState) -> Path:
    workspace.report_dir.mkdir(parents=True, exist_ok=True)
    path = workspace.report_dir / "results.csv"
    rows = []
    for e in sorted(session.scans.values(), key=lambda e: e.scan_number):
        if not e.detectors:
            rows.append({"scan": e.scan_number, "status": e.status,
                         "reasons": "; ".join(e.reasons)})
            continue
        for det, oc in e.detectors.items():
            q = oc.get("q") or [None, None, None]
            iv = oc.get("interval")
            cal = oc.get("calib") or {}
            pr = oc.get("prior_used") or {}
            rows.append({
                "scan": e.scan_number, "detector": det, "sample": oc.get("sample"),
                "T": oc.get("T"), "h": q[0], "k": q[1], "l": q[2], "status": e.status,
                "verdict": oc.get("verdict"), "disposition": oc.get("disposition"),
                "confidence": oc.get("confidence"), "omega0": oc.get("omega0"),
                "omega0_err": oc.get("omega0_err"),
                "interval_lo": (iv[0] if iv else None), "interval_hi": (iv[-1] if iv else None),
                "interval_reliable": oc.get("interval_reliable", True),
                "gamma": oc.get("gamma"), "gamma_provisional": oc.get("gamma_provisional"),
                "n_modes": oc.get("n_modes") or 1,
                "secondary_omega0": _secondary_mode(oc)[0],
                "secondary_omega0_err": _secondary_mode(oc)[1],
                "best_model": oc.get("best_label"), "cost_per_dof": oc.get("cost_per_dof"),
                "zero": cal.get("center"), "zero_source": cal.get("zero_source"),
                "gauss_fwhm": cal.get("gauss_fwhm"), "lorentz_fwhm": cal.get("lorentz_fwhm"),
                "resolution_source": cal.get("resolution_source"),
                "resolution_version": cal.get("resolution_version"),
                "prior_omega": pr.get("omega0"), "prior_sigma": pr.get("sigma"),
                "prior_n": pr.get("n_neighbors"), "prior_source": pr.get("source"),
                "reasons": "; ".join(oc.get("reasons") or []),
            })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in _CSV_COLUMNS})
    return path
