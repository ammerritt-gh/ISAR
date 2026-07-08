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
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from isar.analyze import errors, loop, select, verdict
from isar.analyze import gold as gold_mod
from isar.analyze.context import ScanContext
from isar.analyze.descriptor import describe_spectrum
from isar.analyze.structures import enumerate_multimode, enumerate_structures
from isar.reduce import collate as collate_mod
from isar.reduce import combine, qc
from isar.schema.card import Card
from isar.schema.steering import IntensityMeasure, ParamConstraints
from isar.session import branch_model, calibrations, guidelines as guide_mod, ingest, router, session_prior

# Steering Phase 2c: apply the low-q branch soft-constraint when the dispersion prior predicts a
# phonon within this many resolution FWHM of the elastic line (the merging regime where Γ/A go
# unidentifiable). Above it the peak is well separated and the fit is left unconstrained.
LOW_Q_FWHM_FACTOR = 2.0
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
    # Exact 1-D profile-likelihood interval on ω₀ (errors/profile.omega0_profile_interval). Unlike
    # the parametric-bootstrap `interval` it stays well-defined when Γ→0 (the bound corner where the
    # bootstrap collapses), so the driver prefers it when `interval_reliable` is False.
    omega0_profile: tuple | None = None
    # Fast (HESSE) tier: the delta-method ω₀ ± 1σ_HESSE interval and whether it is trusted as the
    # convergence interval (Γ off-bound + PD covariance + counts). None/False on other tiers.
    hesse_interval: tuple | None = None
    hesse_trusted: bool = False
    # A "master" fit of the combine-before-divide pool of all scans at this (Q,T): pooled=True with
    # n_members = how many scans were summed. Per-scan fits leave these at their defaults.
    pooled: bool = False
    n_members: int | None = None
    # Portable, provenance-bearing intensity for the primary phonon (steering contract 2). A dict
    # (IntensityMeasure.to_dict) or None; carried onto the dispersion prior so a later scan can
    # predict expected counts. Absent on masters (no single-record normalization) and older ledgers.
    intensity: dict | None = None
    # Model-free L0 description of the spectrum (ScanDescriptor.to_dict): peaks + significance,
    # independent of the phonon model. Attached to every scan; nothing decides on it yet (the Phase-4
    # falsification gate compares its peak count to the fitted mode count). None on older ledgers.
    descriptor: dict | None = None
    # Fit provenance (steering contract 4): unconstrained | soft_prior | branch_conditional |
    # stokes_only | descriptor_only. Anything but "unconstrained" is not an ordinary phonon fit.
    fit_kind: str = "unconstrained"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["interval"] = list(self.interval) if self.interval is not None else None
        d["omega0_profile"] = list(self.omega0_profile) if self.omega0_profile is not None else None
        d["hesse_interval"] = list(self.hesse_interval) if self.hesse_interval is not None else None
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


# In-window detector counts floor for trusting the delta-method HESSE ω₀ interval. Calibrated on
# synthetic peaks: HESSE σ tracks the bootstrap half-width (ratio ≈ 1.0, slightly conservative) down
# to ~100 counts for a well-separated peak — the real HESSE-failure regime is Γ→0 (near-elastic /
# broad), which `gamma_at_bound` already gates. So this is only a sparsity floor against fit
# instability at genuinely few counts; kept low (200) to trust weaker peaks too — a q that converges
# on a trusted HESSE interval is re-checked by the full bootstrap in the certify pass anyway, so this
# gate need not be perfect, only avoid the degenerate ultra-sparse case.
_HESSE_MIN_COUNTS = 200.0


def _in_window_counts(ctx) -> float:
    """Total detector counts over the fit window (the kept points)."""
    try:
        counts = np.asarray(ctx.counts, dtype=float)
        mask = np.asarray(ctx.mask, dtype=bool)
        return float(counts[mask].sum())
    except Exception:  # noqa: BLE001 — a malformed ctx just fails the counts gate
        return 0.0


def _hesse_interval_and_trust(best, ctx) -> tuple:
    """(interval, trusted) for the fast tier.

    ``interval`` is the delta-method ω₀ ± 1σ_HESSE (0.68, matching the bootstrap level), or None
    when the HESSE σ is not a finite positive number (a non-positive-definite covariance on the
    derived ω₀). ``trusted`` additionally requires the covariance to be valid, Γ off its zero-bound,
    and enough in-window counts — the conditions under which HESSE σ ≈ the bootstrap half-width."""
    if best is None:
        return None, False
    w0, sig = best.omega0, best.omega0_err_hesse
    if w0 is None or sig is None or not np.isfinite(w0) or not np.isfinite(sig) or sig <= 0:
        return None, False           # non-PD / degenerate → no usable HESSE interval
    interval = (float(w0 - sig), float(w0 + sig))
    trusted = (bool(getattr(best, "covariance_ok", False))
               and not bool(getattr(best, "gamma_at_bound", False))
               and _in_window_counts(ctx) >= _HESSE_MIN_COUNTS)
    return interval, trusted


def _exposure_per_point(rec, ctx) -> tuple[float, str]:
    """Median per-point counting exposure over the kept points, and the counting mode.

    The portable normaliser for the fitted amplitude (steering contract 2): monitor counts (or
    seconds) per point over the fit window (``ctx.mask``). NaN when unavailable → no IntensityMeasure.
    """
    norm = getattr(rec, "normalization", None)
    if norm is None:
        return float("nan"), ""
    mode = getattr(norm, "counting_mode", "") or ""
    arr = norm.monitor if mode == "monitor" else norm.time
    try:
        a = np.asarray(arr, dtype=float)
        m = np.asarray(ctx.mask, dtype=bool)
        if a.size == m.size and m.any():
            a = a[m]
        a = a[np.isfinite(a) & (a > 0)]
        return (float(np.median(a)) if a.size else float("nan")), mode
    except Exception:  # noqa: BLE001 — a malformed normalization just yields no intensity measure
        return float("nan"), mode


def _primary_amplitude(fit) -> float | None:
    """Fitted amplitude of the primary phonon (the mode nearest the reported ω₀)."""
    modes = fit.get("modes")
    w0 = fit.get("omega0")
    if not modes or w0 is None:
        return None
    prim = min(modes, key=lambda m: abs(float(m.get("omega0", 1e18)) - float(w0)))
    a = prim.get("amplitude")
    return float(a) if a is not None else None


def _intensity_measure(rec, ctx, fit) -> dict | None:
    """Portable :class:`IntensityMeasure` dict for the primary phonon, or None when not computable."""
    amp = _primary_amplitude(fit)
    if amp is None:
        return None
    exposure, mode = _exposure_per_point(rec, ctx)
    if not np.isfinite(exposure) or exposure <= 0:
        return None
    return IntensityMeasure(
        value=amp / exposure, exposure=exposure, counting_mode=(mode or "monitor"),
        gauss_fwhm=float(ctx.gauss_fwhm), lorentz_fwhm=float(ctx.lorentz_fwhm),
        instrument=str(ctx.instrument)).to_dict()


def _build_descriptor(ctx):
    """The model-free L0 :class:`ScanDescriptor` for this scan, or None on any failure — a report
    aid + Phase-4 falsification input, never a reason to fail a fit. Built once per scan and reused
    for both the verdict (`classify`) and storage (the fit's mask/counts don't change across the
    hard-fix retry, so one object serves both)."""
    try:
        return describe_spectrum(ctx.x, ctx.counts, center=ctx.center, gauss_fwhm=ctx.gauss_fwhm,
                                 lorentz_fwhm=ctx.lorentz_fwhm, mask=ctx.mask)
    except Exception:  # noqa: BLE001
        return None


def _scan_descriptor(ctx) -> dict | None:
    """`ScanDescriptor.to_dict()` for storage, or None on failure. See :func:`_build_descriptor`."""
    d = _build_descriptor(ctx)
    return d.to_dict() if d is not None else None


def fit_one(ctx, card: Card, *, prior, M, seed, structure_labels=None, peaks=None,
            errors_tier="full", boot_workers=1, boot_executor=None, boot_adaptive=False,
            descriptor=None):
    """Two-stage structure fit (§ multiplicity), optionally narrowed by guideline labels, then
    select + verdict. Mirrors docs/phase3_gold*_check.run_pipeline; returns extracted fields.

    ``peaks`` (guideline hints, ≥2 forces the 2-mode offer and seeds the greedy IC) are threaded
    into :func:`loop.fit_structures_two_stage`. The whitelist narrows the union of 1- and 2-mode
    labels (``stokes2+bg`` etc. pass through) — an empty match keeps all + warns.

    ``errors_tier`` selects the uncertainty depth. ``"full"`` (default, unchanged) runs MINOS +
    parametric bootstrap + ω₀ profile — the authoritative interval the stop rule consumes.
    ``"fast"`` runs the MIGRAD+HESSE fit only and reports the delta-method ω₀ interval
    (``ω₀ ± 1σ_HESSE``) plus a ``hesse_trusted`` flag: in the asymptotic regime (Γ off-bound,
    positive-definite HESSE σ, enough counts) that interval ≈ the bootstrap half-width, so the
    driver can converge on it without paying for a bootstrap; untrusted → escalate to ``full``.
    ``"cheap"`` is the quick-look tier: HESSE only, no interval at all (verdict + rough ω₀ to steer
    acquisition, ~an order of magnitude faster). ``boot_workers`` sets the parametric-bootstrap
    process-pool size (1 = serial; see :func:`run_batch`)."""
    run_full = errors_tier == "full"     # MINOS + bootstrap + ω₀ profile (authoritative interval)
    run_fast = errors_tier == "fast"     # HESSE ω₀ interval + trust flag, no bootstrap (fast loop)
    results = loop.fit_structures_two_stage(ctx, minos=run_full, peaks=peaks)
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
    if run_full:
        for label in prelim.survivors:
            r = fits.get(label)
            if r is not None and r.optimizer_converged and label in by_hyp:
                errors.attach(r, by_hyp[label], ctx, M=M, seed=seed, with_profile=True,
                              boot_workers=boot_workers, boot_executor=boot_executor,
                              boot_adaptive=boot_adaptive)
    sel = select.select_models(results)
    sv = verdict.classify(results, ctx, sel, prior=prior, descriptor=descriptor)

    best = fits.get(sel.best) if sel.best else None
    omega0 = best.omega0 if best is not None else None
    interval = None
    if best is not None and best.within_model and best.within_model.get("freq_interval") is not None:
        interval = best.within_model["freq_interval"]
    elif sel.pooled is not None and sel.pooled.modality == "fold":
        interval = sel.pooled.interval
    # Exact ω₀ profile interval (robust to Γ→0); the driver prefers it when the bootstrap is
    # flagged unreliable below. Absent on the cheap tier (no profile ran).
    omega0_profile = None
    if best is not None and best.within_model:
        omega0_profile = best.within_model.get("profile_interval")
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
    # Fast (HESSE) tier: no bootstrap/profile ran; report the delta-method ω₀ interval + trust gate.
    # The bootstrap `interval`/profile are absent (interval_reliable=False); the loop keys off
    # hesse_trusted instead, escalating to `full` when the asymptotic approximation does not hold.
    hesse_interval = None
    hesse_trusted = False
    if run_fast:
        hesse_interval, hesse_trusted = _hesse_interval_and_trust(best, ctx)
        interval = None
        omega0_profile = None
        interval_reliable = False
        reasons.append("fast-errors tier: HESSE ω₀ interval only "
                       + ("(trusted)" if hesse_trusted else "(untrusted → escalate to full)"))
    # Cheap quick-look tier: no interval at all, only a rough HESSE ω₀ — mark it untrustworthy so
    # the stop rule never consumes it.
    if errors_tier == "cheap":
        interval = None
        omega0_profile = None
        interval_reliable = False
        reasons.append("cheap-errors quick-look: verdict + rough HESSE ω₀ only "
                       "(no bootstrap/profile interval)")

    n_modes = int(best.n_modes) if best is not None else 1
    modes = list(best.modes) if (best is not None and best.modes) else None

    # Phase 4a — under-model falsification: the descriptor localised real signal the phonon model
    # can't fit. This is NOT an ω₀ measurement (machine contract): null the number/intervals and
    # tag descriptor_only, so the stop rule / gold / truth never count it as a delivered ω₀. The
    # descriptor peaks (stored separately) are the deliverable; the verdict routes to "unmodeled".
    omega0_err = best.omega0_err_hesse if best else None
    fit_kind = best.fit_kind if best else "unconstrained"
    if sv.verdict == "signal_unmodeled":
        omega0 = omega0_err = interval = omega0_profile = hesse_interval = None
        interval_reliable = False
        fit_kind = "descriptor_only"

    return {
        "verdict": sv.verdict, "confidence": sv.confidence, "reasons": reasons,
        "omega0": omega0, "omega0_err": omega0_err,
        "interval": (tuple(interval) if interval is not None else None),
        "omega0_profile": (tuple(omega0_profile) if omega0_profile is not None else None),
        "interval_reliable": interval_reliable,
        "hesse_interval": (tuple(hesse_interval) if hesse_interval is not None else None),
        "hesse_trusted": hesse_trusted,
        "modality": sel.modality, "best_label": sel.best,
        "gamma": (best.gamma if best else None),
        "cost_per_dof": (best.cost_per_dof if best else None),
        "n_modes": n_modes, "modes": modes,
        "fit_kind": fit_kind,
        "gamma_at_bound": bool(getattr(best, "gamma_at_bound", False)) if best else False,
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


# ===========================================================================
# worker-budget resolution (performance §; owner approved up to 30 workers)
# ===========================================================================
def _resolve_workers(cli_workers=None) -> int:
    """Scan-level worker count: explicit ``--workers`` (>0) wins, then ``ISAR_WORKERS``, then a
    default of ``min(30, cpu-2)`` (leave two threads for the OS / this parent process)."""
    for src in (cli_workers, os.environ.get("ISAR_WORKERS")):
        if src is None or src == "":
            continue
        try:
            n = int(src)
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    cpu = os.cpu_count() or 1
    return max(1, min(30, cpu - 2))


def _resolve_boot_workers(default_n) -> int:
    """Parametric-bootstrap worker count. ``ISAR_BOOTSTRAP_WORKERS`` (>0) overrides; otherwise the
    caller's ``default_n`` (1 inside a scan-pool worker to avoid nested oversubscription; the full
    budget when scans run serially — e.g. the drive loop's one-scan-per-``isar run``)."""
    env = os.environ.get("ISAR_BOOTSTRAP_WORKERS")
    if env:
        try:
            n = int(env)
            if n > 0:
                return n
        except ValueError:
            pass
    return max(1, int(default_n))


def _process_one_task(args):
    """Top-level (picklable) entry point for a scan-pool worker: unpack plain data and fit.

    On Windows spawn the worker re-imports this module; every argument is picklable (verified:
    ScanRecord, Card, Guidelines, RunCalibration). No shared session/card mutation happens here —
    the returned :class:`ScanEntry` carries everything; the parent applies ledger + prior mutations
    serially in submission order."""
    (rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash,
     M, errors_tier, seed, boot_workers, boot_adaptive) = args
    return _process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                        chash, ghash, M=M, seed=seed, errors_tier=errors_tier,
                        boot_workers=boot_workers, boot_adaptive=boot_adaptive)


def _run_tasks(tasks, card, g, runcal, exclusions, *, M, errors_tier, boot_workers,
               use_pool, scan_workers, boot_executor=None, boot_adaptive=False) -> list:
    """Fit the fresh scan-detectors, serially or over a process pool, preserving submission order.

    ``boot_executor`` (a shared bootstrap pool) is threaded only through the SERIAL path — an
    executor is not picklable and must never enter a scan-pool worker (nested pools). The pool path
    only runs when ``boot_workers == 1`` (Layer A owns the workers), so it never wants a shared pool.
    """
    arg_list = [(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions, chash, ghash,
                 M, errors_tier, seed, boot_workers, boot_adaptive)
                for (rec, det, q, T, sample, scan_id, chash, ghash, seed) in tasks]
    if not arg_list:
        return []
    if not use_pool:
        out = []
        for a in arg_list:
            (rec, det, q, T, sample, scan_id, card_, g_, runcal_, exclusions_, chash, ghash,
             M_, tier_, seed, bw, adaptive_) = a
            out.append(_process_one(rec, det, q, T, sample, scan_id, card_, g_, runcal_,
                                    exclusions_, chash, ghash, M=M_, seed=seed, errors_tier=tier_,
                                    boot_workers=bw, boot_executor=boot_executor,
                                    boot_adaptive=adaptive_))
        return out
    max_workers = min(scan_workers, len(arg_list))
    # map preserves order → results align with `tasks`, keeping prior-seeding + row order deterministic.
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_process_one_task, arg_list))


def run_batch(workspace: Workspace, data_paths, *, force=False, llm=False, M=200,
              detectors=None, formats=("html", "markdown"), workers=None,
              errors_tier="full") -> RunSummary:
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

    # --- Pass 1: split fresh scan-detectors (to fit) from carried ones, preserving report order. ---
    from isar.session.state import canonical_hash
    tasks = []          # (rec, det, q, T, sample, scan_id, chash, ghash, seed)
    plan = []           # ordered: ("carry", scan, outcome) | ("task", scan, det, task_idx)
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
            ghash = canonical_hash(gsig)

            prev = session.get(scan_id)
            fresh = (prev is None or prev.content_hash != chash
                     or prev.guidelines_hash != ghash or prev.status.startswith("held") or force)
            if not fresh:
                # Unchanged & already resolved: keep the ledger entry, re-list its outcome for the report.
                if prev.detectors.get(det):
                    plan.append(("carry", scan, _outcome_from_entry(prev, det)))
                continue
            task_idx = len(tasks)
            tasks.append((rec, det, q, T, sample, scan_id, chash, ghash, seed_base + scan))
            plan.append(("task", scan, det, task_idx))

    # --- Fit the fresh scans (parallel across scans when it pays; §performance). ---
    # Only the `full` tier bootstraps. When a full batch is small enough that the Layer-A scan pool
    # would idle cores (each per-scan bootstrap forced serial), bypass Layer A: fit serially in the
    # parent and funnel every bootstrap (scans + masters) through ONE shared pool at full budget
    # (the workorder — one spawn, not N). Large full reprocess batches (tasks > workers, where Layer
    # A already saturates) and the fast/cheap tiers (no bootstrap) keep the Layer-A parallel-fit path.
    scan_workers = _resolve_workers(workers)
    is_full = errors_tier == "full"
    use_workorder = is_full and len(tasks) <= scan_workers
    if use_workorder:
        use_pool = False
        boot_workers = _resolve_boot_workers(scan_workers)
    else:
        use_pool = scan_workers > 1 and len(tasks) > 1
        boot_workers = 1 if use_pool else _resolve_boot_workers(scan_workers)
    boot_adaptive = is_full
    master_boot_workers = _resolve_boot_workers(scan_workers)

    shared_pool = None
    if is_full and boot_workers > 1 and not use_pool:
        shared_pool = ProcessPoolExecutor(max_workers=boot_workers)   # scans + masters, one spawn
    try:
        entries = _run_tasks(tasks, card, g, runcal, exclusions, M=M, errors_tier=errors_tier,
                             boot_workers=boot_workers, use_pool=use_pool, scan_workers=scan_workers,
                             boot_executor=shared_pool, boot_adaptive=boot_adaptive)
        _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
                          workspace, force=force, M=M, seed_base=seed_base, errors_tier=errors_tier,
                          master_boot_workers=master_boot_workers, boot_executor=shared_pool,
                          boot_adaptive=boot_adaptive)
    finally:
        if shared_pool is not None:
            shared_pool.shutdown()
    return summary


def _apply_and_finish(summary, session, session_prior, card, records, runcal, g, plan, entries,
                      workspace, *, force, M, seed_base, errors_tier, master_boot_workers,
                      boot_executor, boot_adaptive):
    """Pass 2 (ledger + prior) → collate → master fits → tally → write. Split out so the shared
    bootstrap pool from :func:`run_batch` stays open across the whole full-tier batch."""
    # --- Pass 2: apply ledger + prior mutations serially, in submission order (determinism). ---
    # Prior seeding is START-OF-BATCH: a scan is fit against the dispersion prior as of the batch
    # start, never a same-batch neighbour's fresh point (this is what makes serial == parallel).
    # The re-fit idempotency makes it self-healing across runs — a later `isar run` re-seeds from
    # the now-complete prior only for scans that actually drifted.
    t_round = float((card.policy.collation or {}).get("t_round", 5.0))
    for item in plan:
        if item[0] == "carry":
            _, scan, outcome = item
            summary.outcomes.append((scan, outcome))
            continue
        _, scan, det, task_idx = item
        entry = entries[task_idx]
        session.put(entry)
        summary.n_reprocessed += 1
        summary.n_fitted_this_run += 1 if entry.status in ("fitted", "non_result") else 0
        oc = entry.detectors.get(det)
        if oc:
            g_err = None
            _modes, _w0 = oc.get("modes"), oc.get("omega0")
            if _modes and _w0 is not None:
                _pm = min(_modes, key=lambda m: abs(float(m.get("omega0", 1e18)) - float(_w0)))
                g_err = _pm.get("gamma_err")
            session_prior.add_point(card.state, scan_id=entry.scan_id, sample=oc.get("sample"),
                                    T=oc.get("T"), q=oc.get("q"), omega0=oc.get("omega0"),
                                    modality=oc.get("modality"), verdict=oc.get("verdict"),
                                    gamma=oc.get("gamma"), gamma_err=g_err,
                                    intensity=oc.get("intensity"),
                                    fit_kind=oc.get("fit_kind"), t_round=t_round)
            summary.outcomes.append((scan, FitOutcome(**oc) if not isinstance(oc, FitOutcome) else oc))

    # Collate (all records, whole-experiment view).
    try:
        summary.collation = collate_mod.collate(records, card)
    except Exception as exc:  # noqa: BLE001 — collation is a report aid, never fail the batch on it
        summary.context_lines.append(f"collation skipped: {exc}")

    # Fit the merged "master scan" per (Q,T) group (combine-before-divide pooling), when the card
    # opts in with collation.merge == "merge". These pooled fits are what let a campaign accumulate
    # statistics by repeating a measurement; s30 (merge="separate") produces none. Never fatal.
    try:
        # Masters run after the scan phase; their bootstraps take the full worker budget and reuse
        # the batch's shared pool when one is open (else _fit_masters opens its own). Masters fit
        # serially w.r.t. each other; only their refits hit the pool.
        n_masters = _fit_masters(summary.collation, records, card, runcal, g, session,
                                 M=M, seed_base=seed_base, errors_tier=errors_tier, force=force,
                                 boot_workers=master_boot_workers, boot_executor=boot_executor,
                                 boot_adaptive=boot_adaptive)
        if n_masters:
            summary.context_lines.append(f"fit {n_masters} merged master scan(s)")
    except Exception as exc:  # noqa: BLE001 — master fitting is additive; a failure never kills the batch
        summary.context_lines.append(f"master fitting skipped: {exc}")

    summary.counts = _tally(session)
    session.last_run = {"counts": summary.counts,
                        "scan_gaps": [f"{e}: {a}->{b}" for (e, a, b) in summary.gaps],
                        "n_fitted_this_run": summary.n_fitted_this_run}

    _write_results_csv(workspace, session)
    workspace.save(card, session)


# Synthetic scan_number range for master (pooled) ledger entries so they sort AFTER real scans and
# never collide with a real scan_number. The real identity is the scan_id + member-set content_hash.
_MASTER_SCAN_BASE = 9_000_000


def _fit_masters(collation, records, card, runcal, g, session, *, M, seed_base,
                 errors_tier="full", force=False, boot_workers=1, boot_executor=None,
                 boot_adaptive=False) -> int:
    """Fit the merged master scan for each (Q,T) group; returns how many were (re)fit this run.

    Runs only when the card opts in (``collation.merge == "merge"``). Each group's pooled
    ``MergedSpectrum`` (combine-before-divide ΣN counts) is fit through the same ``fit_one`` path as a
    single scan and stored as a ``master:<key>`` ledger entry marked ``pooled=True``. Idempotent on the
    member SET: the entry's ``content_hash`` is a hash of ``ms.members``, so a master refits exactly
    when a member scan arrives or drifts (``collate`` re-runs over all records every batch, so masters
    cannot piggyback on the per-scan freshness flags). Never fatal — a bad group yields a
    ``non_result`` entry, never a crash.
    """
    if collation is None or getattr(collation, "merge", "separate") != "merge":
        return 0
    from isar.session.state import canonical_hash

    rec_by_scan = {rec.scan_number: rec for rec in records}
    # Reuse the batch's shared bootstrap pool when given; else open one for the master pass (only the
    # full tier bootstraps, so a pool is pointless otherwise). Masters fit serially; refits hit the pool.
    own_pool = None
    ex = boot_executor
    if ex is None and errors_tier == "full" and boot_workers > 1:
        own_pool = ProcessPoolExecutor(max_workers=boot_workers)
        ex = own_pool
    n_fit = 0
    try:
        for idx, grp in enumerate(sorted(collation.groups, key=lambda gg: str(gg.key))):
            ms = getattr(grp, "merged", None)
            if ms is None or ms.n_members < 1:
                continue
            master_id = "master:" + "_".join(str(k) for k in grp.key)
            chash = canonical_hash([list(m) for m in ms.members])
            prev = session.get(master_id)
            if not force and prev is not None and prev.content_hash == chash:
                continue                               # member set unchanged → carry the prior master
            member_scan, member_det = ms.members[0][1], ms.members[0][2]
            entry = _fit_master_group(ms, grp, rec_by_scan.get(member_scan), member_det, card, runcal,
                                      g, master_id, _MASTER_SCAN_BASE + idx, chash,
                                      M=M, seed=seed_base + _MASTER_SCAN_BASE + idx,
                                      errors_tier=errors_tier, boot_workers=boot_workers,
                                      boot_executor=ex, boot_adaptive=boot_adaptive)
            session.put(entry)
            n_fit += 1
    finally:
        if own_pool is not None:
            own_pool.shutdown()
    return n_fit


def _fit_master_group(ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number,
                      chash, *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None,
                      boot_adaptive=False) -> ScanEntry:
    """Fit one merged group's pooled spectrum → a ``pooled=True`` ScanEntry (or a non_result)."""
    det = member_det or "merged"
    base = dict(scan_id=master_id, scan_number=int(scan_number), scan_type="measurement",
                content_hash=chash, guidelines_hash="")
    q = list(grp.key_fields.get("q") or [0.0, 0.0, 0.0])
    T_disp = grp.key_fields.get("T")
    T = float(T_disp) if T_disp is not None else float("nan")
    if member_rec is None:
        return ScanEntry(status="non_result", reasons=[f"{master_id}: no member record for resolution"],
                         **base)
    try:
        calres = calibrations.resolve(member_rec, member_det, card, runcal)
    except Exception as exc:  # noqa: BLE001
        return ScanEntry(status="non_result", reasons=[f"{master_id}: resolution failed ({exc})"], **base)

    x = np.asarray(ms.energy, dtype=float)
    counts = np.asarray(ms.intensity, dtype=float)
    mask = np.asarray(ms.mask, dtype=bool)
    if int(mask.sum()) < g.min_fit_points:
        return ScanEntry(status="non_result",
                         reasons=[f"{master_id}: undersampled pool ({int(mask.sum())} < "
                                  f"{g.min_fit_points} kept points)"], **base)
    # Pooled spectrum → ScanContext directly (bypasses build_context: a merged group has no single
    # ScanRecord/normalization; MergedSpectrum.mask already encodes monitor validity).
    ctx = ScanContext(x=x, counts=counts, mask=mask, T=T, center=calres.center,
                      gauss_fwhm=calres.gauss_fwhm, lorentz_fwhm=calres.lorentz_fwhm,
                      instrument=card.policy.instrument, energy_status=card.policy.energy_status,
                      prior_omega=None, quasielastic_fwhm=card.policy.quasielastic_fwhm)
    descriptor = _build_descriptor(ctx)
    try:
        fit = fit_one(ctx, card, prior=None, M=M, seed=seed, errors_tier=errors_tier,
                      boot_workers=boot_workers, boot_executor=boot_executor,
                      boot_adaptive=boot_adaptive, descriptor=descriptor)
    except Exception as exc:  # noqa: BLE001 — one unfittable pool must not kill the batch (N=16 wrinkle)
        return ScanEntry(status="non_result",
                         reasons=[f"{master_id}: fit error ({type(exc).__name__}): {exc}"], **base)

    disposition = gold_mod.verdict_disposition(fit["verdict"])
    outcome = FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition=disposition,
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=(card.policy.energy_status == "absolute"),
        best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=q, T=T, sample=str(grp.key_fields.get("sample", "")), prior_used=None,
        calib=calres.summary(), reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        interval_reliable=fit.get("interval_reliable", True), omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        pooled=True, n_members=int(ms.n_members),
        descriptor=(descriptor.to_dict() if descriptor is not None else None),
        fit_kind=fit.get("fit_kind", "unconstrained"))
    entry = ScanEntry(status="fitted", **base)
    entry.detectors[det] = outcome.to_dict()
    entry.provenance = {"pooled": True, "n_members": int(ms.n_members),
                        "members": [list(m) for m in ms.members]}
    return entry


def _process_one(rec, det, q, T, sample, scan_id, card, g, runcal, exclusions,
                 chash, ghash, *, M, seed, errors_tier="full", boot_workers=1,
                 boot_executor=None, boot_adaptive=False) -> ScanEntry:
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

    # Steering Phase 2c — LOW-q rescue: near the zone centre the acoustic peak merges with the
    # elastic line and Γ/A go unidentifiable. If the dispersion prior predicts a phonon within
    # LOW_Q_FWHM_FACTOR·resolution of the elastic line AND a branch model exists, softly pin Γ and A
    # to the branch (contract 1: penalty enters the objective only) and let ω₀ float. Off for
    # well-separated peaks and whenever no branch/prior is available (⇒ no behavior change there).
    if prior is not None and np.isfinite(prior.omega0) \
            and prior.omega0 < LOW_Q_FWHM_FACTOR * ctx.gauss_fwhm:
        pred = branch_model.branch_for(card.state, sample=sample, T=T, omega0=float(prior.omega0))
        if pred is not None:
            exposure, _mode = _exposure_per_point(rec, ctx)
            cons = pred.soft_constraints(exposure if np.isfinite(exposure) else 0.0)
            if not cons.is_empty():
                ctx = replace(ctx, constraints=cons)
                region_reasons.append(
                    f"low-q branch soft-constraint applied (Γ≈{pred.gamma:.2f}±{pred.gamma_sigma:.2f}, "
                    f"A from 1/ω{'' if pred.in_anchor_range else '; extrapolated'})")

    # L0 descriptor: built once here (mask/counts are now fixed) and reused for the verdict's
    # under-model falsification (Phase 4a) AND storage — including through the hard-fix retry below.
    descriptor = _build_descriptor(ctx)

    try:
        fit = fit_one(ctx, card, prior=prior, M=M, seed=seed, structure_labels=labels, peaks=peaks,
                      errors_tier=errors_tier, boot_workers=boot_workers,
                      boot_executor=boot_executor, boot_adaptive=boot_adaptive, descriptor=descriptor)
    except Exception as exc:  # noqa: BLE001 — one unfittable scan must not kill the batch
        return ScanEntry(status="non_result",
                         reasons=[f"fit error ({type(exc).__name__}): {exc}"] + region_reasons,
                         provenance={"resolution_version": calres.resolution_version}, **base)

    # Steering Phase 3b — hard-fix escalation: if the (soft / Stokes-only) fit STILL rails Γ to its
    # bound (true low-count degeneracy — Γ un-identifiable even under the soft prior) and the branch
    # predicts Γ within its anchor range, re-fit with Γ frozen to the branch value (k reduced,
    # tagged branch_conditional). Only in-range (never hard-fix an extrapolated branch); adopted
    # only if it converges and no longer rails. A soft A constraint, if any, is preserved.
    if fit.get("gamma_at_bound") and prior is not None and np.isfinite(prior.omega0):
        pred = branch_model.branch_for(card.state, sample=sample, T=T, omega0=float(prior.omega0))
        if pred is not None and pred.in_anchor_range and pred.gamma > 0:
            soft = dict(ctx.constraints.soft) if ctx.constraints is not None else {}
            soft.pop("phonon.Gamma", None)                 # Γ becomes hard, not soft
            hctx = replace(ctx, constraints=ParamConstraints(
                soft=soft, hard={"phonon.Gamma": float(pred.gamma)}))
            try:
                hfit = fit_one(hctx, card, prior=prior, M=M, seed=seed, structure_labels=labels,
                               peaks=peaks, errors_tier=errors_tier, boot_workers=boot_workers,
                               boot_executor=boot_executor, boot_adaptive=boot_adaptive,
                               descriptor=descriptor)
            except Exception:  # noqa: BLE001 — escalation is best-effort; keep the original fit
                hfit = None
            if hfit is not None and hfit.get("omega0") is not None \
                    and not hfit.get("gamma_at_bound"):
                fit = hfit
                region_reasons.append(
                    f"hard-fix escalation: Γ frozen to branch {pred.gamma:.2f} meV "
                    f"(branch_conditional; soft prior alone left Γ at bound)")

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
        interval_reliable=fit.get("interval_reliable", True),
        omega0_profile=fit.get("omega0_profile"),
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        intensity=_intensity_measure(rec, ctx, fit),
        descriptor=(descriptor.to_dict() if descriptor is not None else None),
        fit_kind=fit.get("fit_kind", "unconstrained"))

    # NB: the running dispersion prior (session_prior.add_point) is fed by the PARENT in
    # submission order after the fit returns — start-of-batch seeding, so a parallel batch is
    # bit-identical to a serial one. This worker never mutates shared card/session state.
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
    "omega0_profile_lo", "omega0_profile_hi", "hesse_lo", "hesse_hi", "hesse_trusted",
    "pooled", "n_members",
    "gamma", "gamma_provisional", "n_modes", "secondary_omega0", "secondary_omega0_err",
    "best_model", "cost_per_dof", "fit_kind", "descriptor_n_peaks", "descriptor_peaks",
    "zero", "zero_source", "gauss_fwhm",
    "lorentz_fwhm", "resolution_source", "resolution_version", "prior_omega", "prior_sigma",
    "prior_n", "prior_source", "reasons",
]


def _descriptor_summary(oc: dict) -> tuple:
    """(`n_peaks`, compact `"E@σ; …"` string) from the stored descriptor, else (None, None).
    Surfaces the model-free deliverable on results.csv (Phase 4a) — load-bearing for
    ``signal_unmodeled`` scans, whose ω₀ is deliberately absent."""
    desc = oc.get("descriptor")
    if not desc:
        return None, None
    peaks = desc.get("peaks") or []
    compact = "; ".join(f"{float(p.get('energy', 0.0)):.2f}@{float(p.get('significance', 0.0)):.1f}σ"
                        for p in peaks)
    return len(peaks), (compact or None)


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
            piv = oc.get("omega0_profile")
            hiv = oc.get("hesse_interval")
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
                "omega0_profile_lo": (piv[0] if piv else None),
                "omega0_profile_hi": (piv[-1] if piv else None),
                "hesse_lo": (hiv[0] if hiv else None), "hesse_hi": (hiv[-1] if hiv else None),
                "hesse_trusted": oc.get("hesse_trusted"),
                "pooled": oc.get("pooled") or None, "n_members": oc.get("n_members"),
                "gamma": oc.get("gamma"), "gamma_provisional": oc.get("gamma_provisional"),
                "n_modes": oc.get("n_modes") or 1,
                "secondary_omega0": _secondary_mode(oc)[0],
                "secondary_omega0_err": _secondary_mode(oc)[1],
                "best_model": oc.get("best_label"), "cost_per_dof": oc.get("cost_per_dof"),
                "fit_kind": oc.get("fit_kind"),
                "descriptor_n_peaks": _descriptor_summary(oc)[0],
                "descriptor_peaks": _descriptor_summary(oc)[1],
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
