"""``run_campaign`` — the adaptive-loop orchestrator (LLM_HARNESS_DESIGN §8, the worked MVP loop).

Wires the three parts into the §8 loop:

    objective spec → (σ-calibration is milestone 0, loaded here) → adaptive loop {
        acquisition proposes a scan → POST /validate → POST /scan (isolated, seeded) → wait →
        save scan JSON → `isar run` (subprocess) → read results.csv / card.state.json →
        fold into the ω(q) model → stopping check + exception branches
    } → stop (met / unmeetable) → campaign summary

The loop is **decision-only**: numbers come only from ISAR's ``results.csv`` (read through the file
contract), never from a fit imported here. It LOOKS at raw counts solely to re-center a missed window
(§1). Every decision is written to the session log (:mod:`isar.drive.session_log`).

Seams are injectable so the loop is unit-testable with no network and no real ISAR: pass ``client`` (a
:class:`~isar.drive.instrument.tavi_client.TaviClient`-shaped object), ``run_isar`` (a callable that
writes ``report/results.csv``), and ``resolution`` (a float / dict / callable). Defaults build the real
TAVI client and shell out to ``isar run``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import json
import math
import subprocess
import sys
import uuid

import numpy as np

from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.acquire import (
    MAX_POINTS,
    BudgetInsufficientForMinimumBatch,
    points_in_window,
    propose_scan,
)
from isar.drive.acquisition.calibration import Calibration, load_calibration
from isar.drive.instrument.tavi_client import TaviClient, TaviError
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import SubprocessRunner, as_runner
from isar.drive.isar_io.runner import init_isar as _default_init_isar
from isar.drive.isar_io.worker_client import WorkerRunner
from isar.drive.isar_io.scan_writer import save_scan_json
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import log_event, read_log, timed
from isar.drive.supervisor import exceptions as exc
from isar.drive.supervisor.stopping import (
    FIRST_CROSSING_MARGIN,
    PRODUCTION_KINDS,
    PRODUCTION_NCOUNT,
    StopDecision,
    USABLE_DISPOSITIONS as _USABLE,
    evaluate_stop,
    model_points,
    summarize_grid,
    systematic_floor,
)
from isar.schema.card import Card

DEFAULT_MAX_ITERS = 200
DEFAULT_WAIT_TIMEOUT = 600.0
# The instrument point-count ceiling (TAVI enforces max_points=200 on a scan command). An over-limit
# scan latches TAVI's validator state and blocks all later API calls until restart (see
# docs/campaigns/2026-07_MVP_ZONE_RETEST.md), so ISAR must never submit/validate one. Used only as the
# fallback when the live limit cannot be read from the instrument once per campaign.
DEFAULT_MAX_POINTS = 200
# Candidate keys a TAVI /state or /schema payload might carry the point-count limit under.
_MAX_POINTS_KEYS = ("max_points", "maxPoints", "scan_max_points", "max_scan_points", "points_max")
_DEFAULT_CALIBRATION = (Path(__file__).resolve().parents[2] / "calibration"
                        / "phonon_dft_deterministic.json")


def _extract_max_points(payload: Any) -> int | None:
    """Pull a positive integer point-count limit out of a TAVI /state or /schema body, checking the
    known key names at the top level and inside a nested ``limits`` dict. Returns ``None`` when no
    recognizable limit is present (the caller then falls back to :data:`DEFAULT_MAX_POINTS`)."""
    if not isinstance(payload, dict):
        return None
    nested = payload.get("limits")
    for scope in (payload, nested if isinstance(nested, dict) else {}):
        for k in _MAX_POINTS_KEYS:
            v = scope.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return int(v)
    return None


def _preflight_max_points(client) -> tuple[int, str]:
    """Read the instrument's max-points limit ONCE per campaign, returning ``(limit, source)``.

    Tries ``GET /state`` then ``GET /schema`` (whichever first carries a recognizable limit). A
    preflight failure — endpoint unreachable, method absent on the client seam, or no limit present —
    must never break the campaign: it falls back silently to :data:`DEFAULT_MAX_POINTS`, recording the
    fallback via the ``source`` so the eventual clamp event states where the limit came from."""
    for method in ("get_state", "get_schema"):
        fn = getattr(client, method, None)
        if not callable(fn):
            continue
        try:
            payload = fn()
        except Exception:   # noqa: BLE001 — preflight is best-effort; never fail a campaign on it
            continue
        mp = _extract_max_points(payload)
        if mp is not None:
            return mp, method
    return DEFAULT_MAX_POINTS, "default"


def _emitted_point_count(e_lo: float, e_hi: float, e_step: float) -> int:
    """The TRUE (unclamped) number of points TAVI computes from a ``deltaE lo hi step`` command.

    :func:`~isar.drive.acquisition.acquire.points_in_window` clamps to the acquisition budget
    (``MAX_POINTS``); TAVI does not — it counts the command literally. A missed-window recenter widens
    ``e_lo``/``e_hi`` without recomputing the (tight) ``e_step``, so the emitted command can carry far
    more points than the clamped ledger shows (the 594-pt over-limit scan). This is what the guard
    checks, so a wide-window/tight-step proposal cannot slip past the ledger clamp to TAVI."""
    step = max(float(e_step), 1e-9)
    span = max(float(e_hi) - float(e_lo), 0.0)
    return int(span / step) + 1


def _guard_point_count(proposal, max_points: int, source: str, log_path, it: int) -> bool:
    """Clamp ``proposal`` to the instrument's ``max_points`` limit BEFORE any validate/submit call.

    When the emitted point count exceeds the limit, COARSEN ``e_step`` to keep the FULL energy window
    (never truncate it) so the scan fits, then recompute ``n_points`` via the acquisition helper. A
    greppable ``proposal_clamped`` exception event records the old/new step + point counts, the hkl,
    the limit, and where the limit came from. No-op (returns ``False``) when the proposal already fits.
    Returns ``True`` when it clamped."""
    n_emitted = _emitted_point_count(proposal.e_lo, proposal.e_hi, proposal.e_step)
    if n_emitted <= max_points:
        return False
    old_step = float(proposal.e_step)
    span = max(float(proposal.e_hi) - float(proposal.e_lo), 0.0)
    new_step = span / max(int(max_points) - 1, 1)   # exactly max_points points across the kept window
    proposal.e_step = new_step
    proposal.n_points = points_in_window(proposal.e_lo, proposal.e_hi, new_step)
    log_event(log_path, "exception", {
        "action": "proposal_clamped",
        "hkl": [proposal.h, proposal.k, proposal.l],
        "limit": int(max_points), "limit_source": source,
        "old_step": old_step, "new_step": new_step,
        "old_n_points": int(n_emitted),
        "new_n_points": _emitted_point_count(proposal.e_lo, proposal.e_hi, new_step),
        "iter": it,
    })
    return True


def _resolve_calibration(cfg_value) -> Calibration:
    if isinstance(cfg_value, Calibration):
        return cfg_value
    if cfg_value:
        return load_calibration(cfg_value)
    return load_calibration(_DEFAULT_CALIBRATION)


def _make_resolution_cb(client, override):
    if override is not None:
        return override
    def cb(h, k, l, deltaE):
        try:
            return client.get_resolution(h, k, l, deltaE)
        except Exception:   # noqa: BLE001  (any transport/refusal → advisory default FWHM)
            return {"ok": False}
    return cb


def _make_fisher_provider(card_state: dict, resolution_cb, *,
                          production_ncount: int = PRODUCTION_NCOUNT):
    """The 5b Poisson-Fisher screen provider ``(gridq, omega0_pred, sigma_prior) -> σ_scan|None`` built
    from the branch(es) the session persisted into ``card.state.json`` (durable / crash-safe — the drive
    reads the file, never a live session object). Returns ``None`` when no branch exists yet (cold
    campaign) → the acquisition falls back to the 5a counts tier.

    A candidate q is **Fisher-grounded** only when its last fit was ``unconstrained`` AND the branch
    covers its predicted ω₀ *in* the anchor range (the misspecification guard: a mistrusted/extrapolated
    rate never drives a confident Fisher EIG). Given that, it predicts the scan's λ(E) from the branch
    (Γ, A∝1/ω) at production exposure, adds the branch soft-constraint precision the low-q fit will use,
    and returns the identifiability-aware σ(ω₀) — or ``None`` when ω₀ is unidentifiable even with the
    prior (σ=∞). Boundary-clean: imports only isar.drive + isar.synth (both permitted)."""
    branch_models = (card_state or {}).get("branch_models") or []
    if not branch_models:
        return None

    from isar.drive.acquisition.acquire import _energy_window, _resolve_fwhm
    from isar.drive.acquisition.branch_view import predict_branch, select_branch
    from isar.drive.acquisition.eig import fisher_information, sigma_omega0
    from isar.synth.fit import omega_d_from      # isar.synth is boundary-allowed

    def provider(g, omega0_pred, sigma_prior):
        if getattr(g, "last_fit_kind", None) != "unconstrained":
            return None
        if omega0_pred is None or not math.isfinite(omega0_pred) or omega0_pred <= 0:
            return None
        branch = select_branch(branch_models, omega0_pred)
        if branch is None:
            return None
        pred = predict_branch(branch, float(omega0_pred))
        if not pred.in_anchor_range or pred.gamma <= 0:
            return None
        T = float(branch.get("t_bin", 0.0))         # the branch carries its own T-bin
        # Match the frozen campaign policy's incremental production batch.  Using the legacy 1e8
        # exposure here would overstate information for hypothesis campaigns that submit 2.5e7.
        exposure = float(production_ncount)
        amp = float(pred.amplitude_norm) * exposure
        gamma = float(pred.gamma)
        od = omega_d_from(float(omega0_pred), gamma)
        h, k, l = g.hkl
        fwhm = _resolve_fwhm(resolution_cb, h, k, l, float(omega0_pred))
        e_lo, e_hi, _step, n_points = _energy_window(float(omega0_pred), fwhm)
        x_grid = np.linspace(e_lo, e_hi, max(int(n_points), 2))
        g_sig = max(float(pred.gamma_sigma), 1e-6)
        a_sig = max(float(pred.amplitude_rel_sigma) * amp, 1e-6)
        prior_prec = {1: 1.0 / g_sig ** 2, 2: 1.0 / a_sig ** 2}   # branch soft precision on Γ, A
        theta = (od, gamma, amp, 0.0, 1.0)                        # E=0, nominal flat background
        info = fisher_information(theta, x_grid, fwhm, 0.0, T, center=0.0, prior_precision=prior_prec)
        ss = sigma_omega0(theta, info)
        return ss if math.isfinite(ss) else None

    return provider


def _make_classification_providers(card_state: dict, objective, resolution_cb):
    """Build drive-side MI and rival-band providers from durable belief rows/frozen priors."""
    from isar.drive.acquisition.acquire import _resolve_fwhm
    from isar.drive.acquisition.classify_eig import assignment_mi, belief_predict

    rows = [dict(row) for row in (card_state.get("branch_belief_models") or [])
            if isinstance(row, dict)]
    if not rows:
        rows = [{"branch_id": h.get("name"), "anchors": [], "n_anchors": 0,
                 "parameter_prior": h, "period": None}
                for h in (getattr(objective, "hypotheses", []) or [])
                if h.get("name") not in (None, "null")]
    if len(rows) < 2:
        return None, None
    # Prefer the most recently persisted sample/T slice without mixing independent experiments.
    keyed = [(str(row.get("sample", "")), float(row.get("t_bin", -math.inf))) for row in rows]
    selected_key = max(keyed)
    selected = [row for row, key in zip(rows, keyed) if key == selected_key]
    if len(selected) < 2:  # prior-only fallback has one empty key shared by every hypothesis
        selected = rows
    target = getattr(objective, "target_hypothesis", None)
    policy = dict(getattr(objective, "steering", {}) or {})

    def reduced(hkl, period):
        if not period:
            return None
        folded = [((float(v) + 0.5 * period) % period) - 0.5 * period for v in hkl]
        return math.sqrt(sum(v * v for v in folded))

    def components(g):
        out = []
        for row in selected:
            u = reduced(g.hkl, row.get("period"))
            mu, sigma = belief_predict(row, g.q if u is None else u)
            out.append({"branch_id": row.get("branch_id"), "mean": mu, "sigma": sigma,
                        "probability": float(row.get("n_anchors", 0)) + 1.0})
        total = sum(item["probability"] for item in out) or 1.0
        for item in out:
            item["probability"] /= total
        return out

    def classify(g):
        parts = components(g)
        target_part = next((part for part in parts if part["branch_id"] == target), parts[0])
        fwhm = _resolve_fwhm(resolution_cb, *g.hkl, max(target_part["mean"], 0.0))
        tolerance = policy.get("differentiation_tolerance_meV")
        if tolerance is None:
            tolerance = max(3.0 * float(objective.tolerance_meV), fwhm / 2.3548)
        return assignment_mi(parts, meas_sigma=fwhm / 2.3548,
                             differentiation_tolerance_meV=float(tolerance))

    def rival_band(g):
        parts = [part for part in components(g) if part["branch_id"] != target]
        if not parts:
            return None
        rival = max(parts, key=lambda part: part["probability"])
        return rival["mean"], rival["sigma"]

    return classify, rival_band


def _validate_body(proposal, *, sample: str) -> dict[str, Any]:
    """The ``POST /validate`` body — ``parameters`` only (engine/seed/isolated are scan-only fields)."""
    body: dict[str, Any] = {
        "parameters": {
            "H": proposal.h, "K": proposal.k, "L": proposal.l,
            "scan_command1": proposal.scan_command(),
            # A stale scan_command2 on the instrument (e.g. TAVI GUI residue)
            # multiplies the point grid and can scan the same axis twice; the
            # driver owns the scan definition, so clear it explicitly. The
            # instrument-side bleed is fixed (TAVI GUI-independence change); this
            # stays as belt-and-braces.
            "scan_command2": "",
            "number_neutrons": int(proposal.ncount),
            # TAVI's API is now GUI-independent: POST /scan|/validate build state from
            # instrument defaults (Al_bragg) + this request's parameters, no longer
            # inheriting the operator's GUI sample selection. ISAR measures phonons, so
            # the campaign's sample must be stamped explicitly.
            "sample": sample,
        },
    }
    return body


def _scan_body(proposal, engine: str, seed: int, *, sample: str) -> dict[str, Any]:
    """The ``POST /scan`` body — the validate body plus the isolated/seeded engine selection."""
    body = _validate_body(proposal, sample=sample)
    body.update({"engine": engine, "seed": int(seed), "isolated": True})
    return body


def _feasible_point_count(validation: dict, fallback: int) -> int:
    """Executable point count from TAVI's frozen feasibility manifest."""
    value = validation.get("feasible_points") if isinstance(validation, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    mask = validation.get("planned_feasible_mask") if isinstance(validation, dict) else None
    if isinstance(mask, list):
        return sum(bool(item) for item in mask)
    return max(int(fallback), 0)


def _executed_point_count(data: dict, fallback: int) -> int:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return max(int(fallback), 0)
    mask = result.get("executed_feasible_mask")
    if isinstance(mask, list) and mask:
        return sum(bool(item) for item in mask)
    return max(int(fallback), 0)


def _target_supporting_feasible_points(validation: dict, target_band) -> int | None:
    """Feasible bins inside the target's 95%-credible energy support; None without a manifest."""
    manifest = validation.get("point_manifest") if isinstance(validation, dict) else None
    if not isinstance(manifest, list) or target_band is None:
        return None
    mean, sigma = map(float, target_band)
    lo, hi = mean - 2.0 * sigma, mean + 2.0 * sigma
    count = 0
    for point in manifest:
        if not isinstance(point, dict) or not point.get("feasible", False):
            continue
        values = point.get("values") or {}
        energy = values.get("deltaE") if isinstance(values, dict) else None
        if (isinstance(energy, (int, float)) and not isinstance(energy, bool)
                and math.isfinite(float(energy)) and lo <= float(energy) <= hi):
            count += 1
    return count


def _tavi_error_code(error: TaviError) -> str | None:
    """Best-effort structured code from a TAVI error envelope."""
    try:
        payload = json.loads(error.body)
    except (TypeError, ValueError):
        return None
    detail = payload.get("error") if isinstance(payload, dict) else None
    return detail.get("code") if isinstance(detail, dict) else None


def _row_hkl(row: dict):
    h, k, l = row.get("h"), row.get("k"), row.get("l")
    if h is None or k is None or l is None:
        return None
    return (float(h), float(k), float(l))


def _pick_interval(row: dict) -> tuple:
    """The (lo, hi) the convergence test should use, and whether it is trustworthy.

    Precedence, most-authoritative first: the parametric-bootstrap ``interval_lo/hi`` (full tier)
    when ISAR does not flag it unreliable; else the exact ω₀ **profile** interval (full tier, robust
    in the Γ→0 corner where the bootstrap collapses to a spurious spike); else the delta-method
    **HESSE** interval (fast tier) when ISAR marks it ``hesse_trusted`` — the asymptotic regime where
    HESSE σ ≈ the bootstrap half-width, so the loop can converge without paying for a bootstrap.
    Returns ``(lo, hi, ok)``; ``ok`` is False when none is available (→ no calibrated half-width, q
    not converged: an untrusted HESSE fit escalates to the full tier rather than certifying).
    """
    lo, hi = row.get("interval_lo"), row.get("interval_hi")
    plo, phi = row.get("omega0_profile_lo"), row.get("omega0_profile_hi")
    hlo, hhi = row.get("hesse_lo"), row.get("hesse_hi")
    reliable = row.get("interval_reliable")            # bool | None (None = older ledger → trust it)
    if reliable is not False and lo is not None and hi is not None:
        return lo, hi, True                            # bootstrap (well-constrained, full tier)
    if plo is not None and phi is not None:            # bootstrap flagged → the profile interval
        return plo, phi, True                          # profile (Γ-at-bound robust, full tier)
    if hlo is not None and hhi is not None and row.get("hesse_trusted"):
        return hlo, hhi, True                          # trusted HESSE (fast tier)
    return None, None, False


def _is_certifiable(row: dict, ok: bool) -> bool:
    """Whether ``row``'s ω₀ interval may SEAT convergence (S3 / DECISIONS S3).

    A calibrated interval exists (``ok`` from :func:`_pick_interval`) AND it is not a flagged one: an
    ``interval_reliable=False`` fit (γ-at-bound — the bootstrap/profile is degenerate, zero-width is
    degeneracy not precision) is uncertifiable EVEN when a profile fallback makes ``_pick_interval``
    return ``ok`` — the flag is authoritative. The sole carve-out is the fast tier's *trusted* HESSE
    interval (``hesse_trusted``), which is calibrated-equivalent by construction and is allowed to
    certify without paying for a bootstrap. ``interval_reliable`` absent (older ledger → None) is
    treated as reliable (additive contract)."""
    if not ok:
        return False
    if row.get("interval_reliable") is False and not row.get("hesse_trusted"):
        return False
    return True


def _row_certifiable(row: dict) -> bool:
    """The S3 certifiability predicate a folded row must pass to SEAT convergence — exactly what
    :func:`_observation_from_row` stamps into its ``certifiable`` field, factored out so the
    ``escalate_fit`` outcome classifier and the observation builder cannot drift apart."""
    return _is_certifiable(row, _pick_interval(row)[2])


def _uncertifiable_kind(row: dict) -> str:
    """Why a usable-but-uncertifiable delivery has no certifiable interval (S3 stop-state taxonomy):
    ``"fast_tier"`` when the only interval is an untrusted fast-tier HESSE (no bootstrap/profile ever
    ran — the q never left the fast tier); ``"gamma_at_bound"`` otherwise (a full-tier bootstrap/
    profile ran but flagged ``interval_reliable=False``)."""
    has_boot = row.get("interval_lo") is not None
    has_prof = row.get("omega0_profile_lo") is not None
    has_hesse = row.get("hesse_lo") is not None
    if has_hesse and not row.get("hesse_trusted") and not has_boot and not has_prof:
        return "fast_tier"
    return "gamma_at_bound"


def _calibrated_hw(row: dict, calibration: Calibration, bucket: str) -> float | None:
    """Shared interval→half-width core of the two observation builders below.

    Takes the reliable ``(lo, hi)`` from :func:`_pick_interval` (bootstrap → profile → trusted HESSE)
    and returns its calibrated half-width at the given count ``bucket``, or ``None`` when there is no
    trustworthy interval (``ok`` False) *or* the calibration rejects the interval. Callers keep their
    distinct output shapes; the quicklook ``omega0_err`` fallback (a different interval source) stays
    inline in :func:`_observation_from_row`."""
    lo, hi, ok = _pick_interval(row)
    if not ok:
        return None
    try:
        return calibration.calibrated_halfwidth(lo, hi, bucket=bucket)
    except Exception:   # noqa: BLE001  (bad interval -> treat as un-calibratable, not a crash)
        return None


def _diagnostic_interval(row: dict) -> tuple[Any, Any, str | None]:
    """Best finite interval available before the certifiability gate, with method provenance."""
    for lo_key, hi_key, method in (
            ("interval_lo", "interval_hi", "bootstrap"),
            ("omega0_profile_lo", "omega0_profile_hi", "profile"),
            ("hesse_lo", "hesse_hi", "hesse"),
            ("bayes_lo", "bayes_hi", "bayesian")):
        lo, hi = row.get(lo_key), row.get(hi_key)
        if lo is not None and hi is not None:
            try:
                if math.isfinite(float(lo)) and math.isfinite(float(hi)) and float(hi) >= float(lo):
                    return lo, hi, method
            except (TypeError, ValueError):
                continue
    return None, None, None


def _calibration_version(calibration: Calibration) -> str:
    raw = calibration.raw or {}
    return str(raw.get("version") or raw.get("schema") or calibration.truth_source or "unversioned")


def _diagnostic_halfwidth(row: dict, calibration: Calibration,
                          bucket: str) -> tuple[float | None, str | None]:
    lo, hi, method = _diagnostic_interval(row)
    if method is None:
        return None, None
    try:
        value = calibration.calibrated_halfwidth(lo, hi, bucket=bucket)
    except Exception:  # noqa: BLE001 - diagnostic absence must not become a delivery interval
        return None, method
    return (float(value) if math.isfinite(float(value)) else None), method


def _effective_exposure_bucket(row: dict, submitted_ncount: int) -> tuple[str, float, str]:
    """Deterministic calibration/steering exposure key with a submitted-count fallback."""
    value = row.get("effective_exposure")
    if value is None:
        fallback = float(submitted_ncount)
        return str(int(round(fallback))), fallback, "submitted_ncount"
    try:
        exposure = float(value)
    except (TypeError, ValueError):
        return "invalid", 0.0, "invalid_effective_exposure"
    if math.isfinite(exposure) and exposure > 0.0:
        return str(int(round(exposure))), exposure, "effective_exposure"
    return "invalid", exposure, "invalid_effective_exposure"


def _calibration_bucket(calibration: Calibration, exposure_key: str) -> tuple[str, str | None]:
    """Record the bucket actually used; unmatched exposures use the global calibration."""
    if exposure_key in calibration.buckets:
        return exposure_key, exposure_key
    return "global", None


def _observation_from_row(row: dict, kind: str, ncount: int, calibration: Calibration,
                          proposal_cost: float = 0.0, *, driver_inserted: bool = False,
                          certification_eligible: bool = True,
                          acquisition_id: str | None = None) -> dict:
    disp = row.get("disposition")
    verdict = row.get("verdict")
    # `flagged` (model_incomplete) still carries a usable ω₀ — for an ω₀-tolerance objective the flag
    # documents peak-shape imperfection, not an ω₀ error (see stopping.USABLE_DISPOSITIONS).
    usable = disp in _USABLE
    hw = None
    ok = _pick_interval(row)[2]
    certifiable = _row_certifiable(row)
    # S3 third state: a usable delivery with no certifiable interval. Classify WHY (γ-at-bound vs
    # fast-tier-only) so the stop taxonomy can name it; only meaningful for a usable, uncertifiable fit.
    uncertifiable_kind = _uncertifiable_kind(row) if (usable and not certifiable) else None
    exposure_key, effective_exposure, exposure_source = _effective_exposure_bucket(row, ncount)
    bucket, calibration_arg = _calibration_bucket(calibration, exposure_key)
    if usable and ok:
        hw = _calibrated_hw(row, calibration, calibration_arg)
    elif usable and kind == "quicklook":
        # Cheap-tier quicklooks carry only a HESSE omega0_err (no bootstrap interval). Good enough to
        # SEED the ω(q) acquisition model (steering only); it never reaches the stop rule, which
        # consumes the master (pooled) half-width built from a real interval.
        err, w0 = row.get("omega0_err"), row.get("omega0")
        if err is not None and w0 is not None and float(err) > 0:
            try:
                hw = calibration.calibrated_halfwidth(float(w0) - float(err), float(w0) + float(err),
                                                      bucket=calibration_arg)
            except Exception:   # noqa: BLE001
                hw = None
    return {
        "hkl": _row_hkl(row), "kind": kind, "calibrated_halfwidth": hw,
        "resolved": usable, "disposition": disp, "verdict": verdict,
        # S3: certifiable gates convergence seating; uncertifiable_kind names the third-state reason.
        "certifiable": certifiable, "uncertifiable_kind": uncertifiable_kind,
        "omega0": row.get("omega0"), "scan": row.get("scan"), "detector": row.get("detector"),
        "proposal_cost": float(proposal_cost), "fit_kind": row.get("fit_kind"),
        "submitted_ncount": int(ncount), "acquisition_id": acquisition_id,
        "effective_exposure": effective_exposure, "exposure_source": exposure_source,
        "exposure_bucket": bucket,
        "branch_id": row.get("branch_id"),   # Phase A: only acoustic (or null) rows steer ω(q)
        # Driver-inserted probes inform branch classification but never occupy a requested grid point
        # or contribute to its production/certification counters.  Persisted run-event provenance
        # restores these flags after a crash rather than relying on an hkl-nearness inference.
        "driver_inserted": bool(driver_inserted),
        "certification_eligible": bool(certification_eligible),
    }


def _master_obs_from_row(row: dict, calibration: Calibration) -> dict:
    """A master (pooled) fit → the convergence observation for its q. The calibrated half-width uses
    the reliable interval (profile when the bootstrap is Γ-at-bound) at the production count bucket."""
    ok = _pick_interval(row)[2]
    exposure_key, effective_exposure, exposure_source = _effective_exposure_bucket(
        row, PRODUCTION_NCOUNT)
    bucket, calibration_arg = _calibration_bucket(calibration, exposure_key)
    hw = _calibrated_hw(row, calibration, calibration_arg)
    diagnostic_hw, diagnostic_method = _diagnostic_halfwidth(row, calibration, calibration_arg)
    disp = row.get("disposition")
    return {
        "hkl": _row_hkl(row), "omega0": row.get("omega0"), "halfwidth": hw,
        # S3: `reliable` (== ok) kept for back-compat; `certifiable` is the convergence gate — a
        # γ-at-bound master (interval_reliable=False) never seats, even via a profile fallback.
        "reliable": bool(ok), "certifiable": _is_certifiable(row, ok),
        "usable": (disp in _USABLE and row.get("omega0") is not None),
        "disposition": disp, "n_members": row.get("n_members"),
        "effective_exposure": effective_exposure, "exposure_source": exposure_source,
        "exposure_bucket": bucket,
        "diagnostic_halfwidth": diagnostic_hw, "interval_method": diagnostic_method,
        "calibration_version": _calibration_version(calibration),
        "branch_id": row.get("branch_id"), "branch_posterior": row.get("branch_posterior"),
        "target_branch": row.get("target_branch"),
        "candidate_id": row.get("candidate_id"), "candidate_state": row.get("candidate_state"),
        # Chain of custody: the member scan labels behind this pooled value (readers coerce the CSV
        # cell to a list). members_field_present distinguishes "old results.csv lacked the column"
        # (guard skipped) from "column present but empty" (guard fires).
        "members": row.get("members"), "members_field_present": ("members" in row),
    }


def _candidate_evidence_state(observations: list[dict], update: dict) -> tuple[str, dict]:
    """Apply the two-acquisition + neighboring-q Bayesian rejection rule."""
    candidate_id = update.get("candidate_id")
    evidence = [item for item in observations
                if item.get("kind") == "pooled_master_update"
                and item.get("candidate_id") == candidate_id]
    evidence.append(update)
    acquisitions = {item.get("acquisition_id") for item in evidence
                    if item.get("acquisition_id") is not None}
    positions = {tuple(round(float(value), 8) for value in item.get("hkl", ()))
                 for item in evidence if item.get("hkl") is not None}
    posterior = update.get("branch_posterior")
    target_branch = update.get("target_branch")
    state = str(update.get("candidate_state") or "provisional")
    p_target = competitor = None
    if isinstance(posterior, dict) and target_branch:
        p_target = float(posterior.get(target_branch, 0.0))
        competitor = max(float(posterior.get("acoustic", 0.0)),
                         float(posterior.get("new", posterior.get("unmodeled", 0.0))))
        if (state == "provisional" and p_target <= 0.05 and competitor >= 0.6
                and len(acquisitions) >= 2 and len(positions) >= 2):
            state = "rejected"
    diagnostics = {"n_logical_acquisitions": len(acquisitions),
                   "n_q_positions": len(positions), "p_target": p_target,
                   "p_competitor": competitor}
    return state, diagnostics


def _record_pooled_master_update(workspace, calibration: Calibration, proposal, *,
                                 log_path, observations: list[dict]) -> dict | None:
    """Persist the current pooled master for the submitted q as chronological replay evidence."""
    matching = [row for row in _read_master_rows(workspace, calibration)
                if _hkl_close(row.get("hkl"), (proposal.h, proposal.k, proposal.l))]
    if not matching:
        return None
    master = max(matching, key=lambda row: int(row.get("n_members") or 0))
    payload = {**master, "kind": "pooled_master_update",
               "acquisition_id": proposal.acquisition_id,
               "acquisition_target_energy": float(proposal.e_pred)}
    payload["candidate_state"], payload["candidate_evidence"] = _candidate_evidence_state(
        observations, payload)
    observations.append(payload)
    log_event(log_path, "pooled_master_update", payload)
    if payload["candidate_state"] == "rejected":
        log_event(log_path, "candidate_update", {
            "candidate_id": payload.get("candidate_id"), "state": "rejected",
            "target_branch": payload.get("target_branch"), "hkl": payload.get("hkl"),
            "branch_posterior": payload.get("branch_posterior"),
            "evidence": payload.get("candidate_evidence")})
    return payload


def _read_master_rows(workspace, calibration: Calibration) -> list[dict]:
    """All current master (pooled=True) rows in results.csv → convergence observations. Read FRESH
    every iteration (not consume-once): a master's interval shrinks as scans accumulate into it."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        if r.get("pooled") and _row_hkl(r) is not None:
            out.append(_master_obs_from_row(r, calibration))
    return out


def _grid_state(objective, observations: list[dict], workspace, calibration: Calibration):
    """The ``(master_rows, grid)`` pair the loop rebuilds at three points (stop eval, model update,
    final re-eval). Reads the pooled master rows FRESH (their intervals shrink as scans accumulate),
    then folds observations + masters into the per-q grid. The three call sites were verified to be
    identical calls; the caller wraps this in whatever ``timed`` span (or none) belongs at that site."""
    master_rows = _read_master_rows(workspace, calibration)
    grid = summarize_grid(objective, observations, master_rows)
    return master_rows, grid


def _record_eligible_crossings(grid, observations: list[dict], log_path=None) -> int:
    """Record each qualifying pooled crossing once; unchanged pools can never cross twice."""
    added = 0
    for g in grid:
        if not g.requires_crossing_guard:
            continue
        master = g.master or {}
        fingerprint = master.get("production_batch_fingerprint")
        halfwidth = g._master_hw
        if (not fingerprint or halfwidth is None
                or FIRST_CROSSING_MARGIN * halfwidth > g.tolerance
                or fingerprint in g.crossing_fingerprints):
            continue
        marker = {"hkl": g.hkl, "kind": "certification_crossing",
                  "production_batch_fingerprint": fingerprint,
                  "calibrated_halfwidth": halfwidth,
                  "n_members": master.get("n_members")}
        observations.append(marker)
        if log_path is not None:
            log_event(log_path, "certification_crossing", marker)
        added += 1
    return added


def _hkl_close(a, b, tol: float = 1e-4) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _q_needs_full_errors(workspace, hkl) -> bool:
    """Whether this production q needs the full (bootstrap+profile) tier rather than fast (HESSE).

    True when the q already has a usable fit whose only error is an UNtrusted HESSE interval and no
    trustworthy interval yet — the Γ-at-bound / low-count corner where the fast HESSE σ cannot be
    trusted and only the exact profile interval certifies. False when the q has a trusted HESSE (or an
    existing reliable bootstrap/profile), so it stays on the cheap fast tier."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return False
    saw_untrusted = False
    for r in rows:
        if not _hkl_close(_row_hkl(r), hkl):
            continue
        if r.get("disposition") not in _USABLE or r.get("omega0") is None:
            continue
        if _pick_interval(r)[2]:
            return False                              # already have a trustworthy interval → fast is fine
        if r.get("hesse_lo") is not None and not r.get("hesse_trusted"):
            saw_untrusted = True
    return saw_untrusted


def _full_tier(cfg) -> str:
    """The tier standing in for "full" (bootstrap+profile) errors: ``bayes`` (superset of ``full`` —
    adds MINOS + posterior as CSV diagnostics, bootstrap stays the authoritative interval) when the
    campaign opted in via ``cfg.bayes_errors``, else ``full`` itself. ``cfg`` may be a
    :class:`CampaignConfig` or anything exposing a ``bayes_errors`` attribute (falls back to ``full``
    when absent, e.g. ``None``)."""
    return "bayes" if getattr(cfg, "bayes_errors", False) else "full"


def _errors_tier_for(proposal, workspace, cfg=None) -> str:
    """The ``isar run --errors`` tier for this proposal (LLM_HARNESS_DESIGN §8 escalation ladder):

    - quicklook → ``cheap`` (HESSE only, no interval — steers the ω(q) model);
    - production → ``fast`` (HESSE interval + trust flag, no bootstrap) by default, escalating to
      ``full`` (bootstrap+profile) — or ``bayes`` when ``cfg.bayes_errors`` is set, see
      :func:`_full_tier` — once the q shows an untrusted-HESSE fit that needs the reliable interval.
      The bootstrap thus fires only where the fast HESSE approximation cannot be trusted.

    Returns the bare tier string (``cheap`` | ``fast`` | ``full`` | ``bayes``) — the runner seam takes
    it as a structured ``errors_tier`` kwarg.
    """
    if proposal.kind == "quicklook":
        return "cheap"
    if _q_needs_full_errors(workspace, (proposal.h, proposal.k, proposal.l)):
        return _full_tier(cfg)
    return "fast"


def _errors_arg_for(proposal, workspace, cfg=None) -> list[str]:
    """Back-compat wrapper: the tier from :func:`_errors_tier_for` as ``["--errors", <tier>]`` argv."""
    return ["--errors", _errors_tier_for(proposal, workspace, cfg)]


def _recertified_observations(workspace, calibration: Calibration, scan_kind: dict) -> list[dict]:
    """Rebuild the per-scan observations from the (now bootstrap-certified) results.csv so the campaign
    summary reports certified intervals. Uses the recorded ``(kind, ncount)`` per (scan, detector);
    pooled/master rows are excluded (read separately via :func:`_read_master_rows`)."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        if r.get("pooled") or _row_hkl(r) is None:
            continue
        submitted = scan_kind.get((r.get("scan"), r.get("detector")),
                                  ("production", PRODUCTION_NCOUNT, None))
        kind, ncount = submitted[:2]
        acquisition_id = submitted[2] if len(submitted) > 2 else None
        out.append(_observation_from_row(
            r, kind, ncount, calibration, acquisition_id=acquisition_id))
    return out


def _resume_detailed_events(workspace, events: list[dict], calibration: Calibration) -> dict:
    """Replay additive physical-submission/master-update events in chronological order."""
    def hkl_key(hkl) -> tuple:
        return tuple(round(float(value), 8) for value in hkl)

    physical_job_ids = {
        str((event.get("payload") or {}).get("job_id"))
        for event in events if event.get("kind") == "physical_submission"
        and (event.get("payload") or {}).get("job_id") is not None
    }
    submissions = []
    by_job: dict[str, dict] = {}
    for order, event in enumerate(events):
        kind = event.get("kind")
        payload = dict(event.get("payload") or {})
        is_detailed = kind == "physical_submission"
        is_legacy_prefix = (kind == "run" and
                            (payload.get("job_id") is None
                             or str(payload.get("job_id")) not in physical_job_ids))
        if not (is_detailed or is_legacy_prefix):
            continue
        record = {"order": order, "payload": payload,
                  "cost": float(payload.get("cost") or 0.0), "seconds_delta": 0.0}
        submissions.append(record)
        if payload.get("job_id") is not None:
            by_job[str(payload["job_id"])] = record
    for event in events:
        payload = event.get("payload") or {}
        if (event.get("kind") != "exception"
                or payload.get("action") != "feasibility_reconciled"):
            continue
        record = by_job.get(str(payload.get("job_id")))
        if record is not None:
            record["cost"] += float(payload.get("neutron_delta") or 0.0)
            record["seconds_delta"] += float(payload.get("seconds_delta") or 0.0)

    neutrons = sum(record["cost"] for record in submissions)
    seconds = 0.0
    submissions_by_hkl: dict[tuple, list[dict]] = {}
    for record in submissions:
        payload = record["payload"]
        eta = payload.get("eta")
        if isinstance(eta, dict) and eta.get("estimated_seconds") is not None:
            seconds += float(eta["estimated_seconds"])
        seconds += record["seconds_delta"]
        if payload.get("hkl") is not None:
            submissions_by_hkl.setdefault(hkl_key(payload["hkl"]), []).append(record)

    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        rows = []
    result_rows = [row for row in rows if not row.get("pooled") and _row_hkl(row) is not None]
    scan_groups: dict[tuple, list[dict]] = {}
    scan_group_order: list[tuple] = []
    for row in result_rows:
        key = (hkl_key(_row_hkl(row)), str(row.get("scan")))
        if key not in scan_groups:
            scan_groups[key] = []
            scan_group_order.append(key)
        scan_groups[key].append(row)
    groups_by_hkl: dict[tuple, list[tuple]] = {}
    for key in scan_group_order:
        groups_by_hkl.setdefault(key[0], []).append(key)

    ordered_observations: list[tuple[float, int, dict]] = []
    consumed: set = set()
    scan_kind: dict = {}
    sequence = 0
    for key, group_keys in groups_by_hkl.items():
        ledger = submissions_by_hkl.get(key, [])
        offset = len(ledger) - len(group_keys)
        for group_index, group_key in enumerate(group_keys):
            ledger_index = offset + group_index
            record = ledger[ledger_index] if 0 <= ledger_index < len(ledger) else None
            payload = record["payload"] if record is not None else {}
            kind = payload.get("kind") or "production"
            ncount = int(payload.get("submitted_ncount") or payload.get("ncount")
                         or PRODUCTION_NCOUNT)
            acquisition_id = payload.get("acquisition_id")
            provenance = {
                "driver_inserted": bool(payload.get("driver_inserted", False)),
                "certification_eligible": bool(payload.get("certification_eligible", True)),
            }
            cost = record["cost"] if record is not None else 0.0
            order = float(record["order"]) if record is not None else -1.0 + sequence * 1e-6
            for detector_index, row in enumerate(scan_groups[group_key]):
                observation = _observation_from_row(
                    row, kind, ncount, calibration,
                    proposal_cost=cost if detector_index == 0 else 0.0,
                    acquisition_id=acquisition_id, **provenance)
                ordered_observations.append((order, sequence, observation))
                sequence += 1
                scan_detector = (row.get("scan"), row.get("detector"))
                consumed.add(scan_detector)
                scan_kind[scan_detector] = (kind, ncount, acquisition_id)

    for order, event in enumerate(events):
        if event.get("kind") == "pooled_master_update":
            marker = dict(event.get("payload") or {})
            marker["kind"] = "pooled_master_update"
            ordered_observations.append((float(order), sequence, marker))
            sequence += 1
        elif event.get("kind") == "certification_crossing":
            marker = dict(event.get("payload") or {})
            if marker.get("hkl") is not None and marker.get("production_batch_fingerprint"):
                marker["kind"] = "certification_crossing"
                ordered_observations.append((float(order), sequence, marker))
                sequence += 1
    ordered_observations.sort(key=lambda item: (item[0], item[1]))
    return {"observations": [item[2] for item in ordered_observations],
            "consumed": consumed, "scan_kind": scan_kind,
            "scan_counter": len(submissions), "neutrons": neutrons, "seconds": seconds}


def _resume_state(workspace, log_path, calibration: Calibration) -> dict:
    """Rehydrate the loop's in-memory state from the durable files after a crash/restart.

    Convergence needs no resume (the pooled master rows are read fresh every iteration); this recovers
    what ``run_campaign`` would otherwise reset to zero — the **spend ledger** (``scan_counter`` +
    ``budget`` neutrons/seconds, exact from the ``run`` events, so a restart neither re-spends beamtime
    nor reuses a seed / collides an idempotency key), the **folded-row set** (``consumed`` = every
    ``(scan, detector)`` already in ``results.csv``, so old rows are never re-folded as new), and
    **best-effort per-scan observations**. Each observation carries its OWN submission's kind (not an
    hkl-wide lock): a results.csv row cannot be joined back to its run event other than by ``q`` +
    order, so a q's rows are tail-aligned against its per-q kind ledger — surviving rows take their
    most-recent matching submission (so a resumed quicklook stays a quicklook and does not inflate
    ``n_production``); a q whose ledger is shorter than its rows falls back to ``"production"`` for the
    leading rows, as before. Each q's total cost is
    lumped onto one representative row so ``counts_spent`` sums to the true per-q spend — a PRODUCTION
    row when the q has one (``summarize_grid`` reads cost>0 rows to set ``last_prod_cost``), else the
    first row (quicklook-only q). A cold workspace (no log / no results.csv) returns the fresh-start
    tuple."""
    events = read_log(log_path)
    if any(event.get("kind") == "physical_submission" for event in events):
        return _resume_detailed_events(workspace, events, calibration)

    def resume_hkl_key(hkl) -> tuple:
        # Preserve enough precision to keep nearby diagnostic probes distinct while absorbing only
        # serialization-level float noise.  Four decimals merged legitimate sub-1e-4 scan points.
        return tuple(round(float(x), 8) for x in hkl)

    runs = [e.get("payload", {}) for e in events if e.get("kind") == "run"]
    scan_counter = len(runs)                        # one run event per submitted scan
    neutrons = seconds = 0.0
    cost_by_hkl: dict = {}
    kinds_by_hkl: dict = {}                         # per-q kind ledger, in submission order
    provenance_by_hkl: dict = {}                    # aligned driver-insertion/certification ledger
    ncount_by_hkl: dict = {}
    for r in runs:
        cost = float(r.get("cost")) if r.get("cost") is not None else 0.0
        neutrons += cost
        eta = r.get("eta")
        if isinstance(eta, dict) and eta.get("estimated_seconds") is not None:
            seconds += float(eta["estimated_seconds"])
        hkl = r.get("hkl")
        if hkl is None:
            continue
        key = resume_hkl_key(hkl)
        cost_by_hkl[key] = cost_by_hkl.get(key, 0.0) + cost
        kinds_by_hkl.setdefault(key, []).append(r.get("kind") or "production")
        provenance_by_hkl.setdefault(key, []).append({
            "driver_inserted": bool(r.get("driver_inserted", False)),
            "certification_eligible": bool(r.get("certification_eligible", True)),
        })
        ncount_by_hkl[key] = r.get("ncount") or ncount_by_hkl.get(key)

    reconciliations = [e.get("payload", {}) for e in events
                       if e.get("kind") == "exception"
                       and (e.get("payload") or {}).get("action") == "feasibility_reconciled"]
    for reconciliation in reconciliations:
        neutron_delta = float(reconciliation.get("neutron_delta") or 0.0)
        seconds_delta = float(reconciliation.get("seconds_delta") or 0.0)
        neutrons += neutron_delta
        seconds += seconds_delta
        hkl = reconciliation.get("hkl")
        if hkl is not None:
            key = resume_hkl_key(hkl)
            cost_by_hkl[key] = cost_by_hkl.get(key, 0.0) + neutron_delta

    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        rows = []
    result_rows = [row for row in rows if not row.get("pooled") and _row_hkl(row) is not None]
    # Attribute each result row its own submission's kind. In steady single-detector operation a q has
    # one persisted row per submission, so rows align 1:1 with its ledger; align at the TAIL so when
    # fewer rows survived than submissions (an older/unpersisted quicklook left no row), each surviving
    # row takes its most-recent matching submission (a q's rows are its latest results) rather than an
    # old quicklook. A ledger shorter than the rows defaults the leading rows to "production", as before.
    count_by_hkl: dict = {}
    for row in result_rows:
        key = resume_hkl_key(_row_hkl(row))
        count_by_hkl[key] = count_by_hkl.get(key, 0) + 1
    row_kinds: list = []
    row_provenance: list[dict] = []
    cursor: dict = {}
    for row in result_rows:
        key = resume_hkl_key(_row_hkl(row))
        ledger = kinds_by_hkl.get(key, [])
        i = cursor.get(key, 0)
        li = (len(ledger) - count_by_hkl[key]) + i     # tail-align rows against the per-q ledger
        row_kinds.append(ledger[li] if 0 <= li < len(ledger) else "production")
        provenance = provenance_by_hkl.get(key, [])
        row_provenance.append(provenance[li] if 0 <= li < len(provenance) else {
            "driver_inserted": False, "certification_eligible": True,
        })
        cursor[key] = i + 1
    # Choose the row each q's summed cost lands on: the first PRODUCTION row if the q has one, else its
    # first row (stamping the full sum onto a quicklook row would mis-set last_prod_cost).
    cost_target: dict = {}
    for idx, (row, kind, provenance) in enumerate(zip(result_rows, row_kinds, row_provenance)):
        key = resume_hkl_key(_row_hkl(row))
        if key not in cost_target:
            cost_target[key] = idx
        elif kind in PRODUCTION_KINDS and row_kinds[cost_target[key]] not in PRODUCTION_KINDS:
            cost_target[key] = idx                  # prefer a production row over an earlier quicklook

    observations: list[dict] = []
    consumed: set = set()
    scan_kind: dict = {}
    for idx, (row, kind, provenance) in enumerate(zip(result_rows, row_kinds, row_provenance)):
        key = resume_hkl_key(_row_hkl(row))
        ncount = int(ncount_by_hkl.get(key) or PRODUCTION_NCOUNT)
        pc = cost_by_hkl.get(key, 0.0) if cost_target.get(key) == idx else 0.0
        observations.append(_observation_from_row(
            row, kind, ncount, calibration, proposal_cost=pc, **provenance))
        sd = (row.get("scan"), row.get("detector"))
        consumed.add(sd)
        scan_kind[sd] = (kind, ncount)

    for event in events:
        if event.get("kind") == "certification_crossing":
            marker = dict(event.get("payload") or {})
            if marker.get("hkl") is not None and marker.get("production_batch_fingerprint"):
                marker["kind"] = "certification_crossing"
                observations.append(marker)

    return {"observations": observations, "consumed": consumed, "scan_kind": scan_kind,
            "scan_counter": scan_counter, "neutrons": neutrons, "seconds": seconds}


def _read_run_timings(workspace) -> dict | None:
    """Best-effort read of the subprocess stage-timing file ``isar run`` drops per invocation
    (``workspace/report/run_timings.json``). Returns ``None`` when the file is absent or unparseable —
    profiling is advisory and must never fail (or alter) the campaign. Read right after ``run_isar``
    returns (the subprocess has exited), so it snapshots that invocation's own timings."""
    p = Path(workspace) / "report" / "run_timings.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001  (missing / malformed timing file → no embedded timings, never a crash)
        return None


def _new_rows(workspace, consumed: set) -> list[dict]:
    """Rows in results.csv not yet consumed, keyed by (scan, detector); skips status-only rows."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        if r.get("pooled"):            # master (pooled) rows drive convergence, read fresh elsewhere
            continue
        key = (r.get("scan"), r.get("detector"))
        if key in consumed or _row_hkl(r) is None:
            continue
        consumed.add(key)
        out.append(r)
    return out


def _rows_missed(rows: list[dict]) -> bool:
    """True when a scan produced no usable detector (peak outside the window / no signal). A
    ``branch_continuation`` (Phase A: the near-Γ acoustic, confidently identified but not resolvable
    by position) is NOT a miss — widening the window would only chase the higher optic branch, never
    resolve a peak sitting at the elastic origin — so it does not trigger the recenter/widen."""
    if not rows:
        return True
    return not any(r.get("disposition") in _USABLE or r.get("disposition") == "branch_continuation"
                   for r in rows)


@dataclass(frozen=True)
class CampaignConfig:
    """The resolved configuration for one :func:`run_campaign` invocation — every ``config`` key
    turned into the concrete seam / scalar the loop consumes, resolved once by :func:`_campaign_config`
    so ``run_campaign`` reads only resolved fields.

    Fields (config key → resolved field; type; default; when used):

    - ``workspace`` (:class:`Path`, config key ``workspace``, **required**): the workspace directory;
      root of all file I/O. If it has no ``card.policy.toml`` the loop runs ``isar init`` after the
      first scan lands (init needs a data file).
    - ``data_dir`` (:class:`Path`, config key ``data_dir``, default ``workspace/"data"``): where each
      scan's JSON is saved before ``isar run`` reads it.
    - ``log_path`` (:class:`Path`, config key ``session_log``, default
      ``workspace/"report"/"session_log.jsonl"``): the durable decision/timing log every event lands in.
    - ``calibration`` (:class:`Calibration`, config key ``calibration`` accepting a ``Calibration`` /
      path / ``None``, default the repo artifact): σ-inflation + systematic floor source; drives every
      calibrated half-width.
    - ``client`` (TAVI-client-shaped, config keys ``client`` then ``tavi_url``, default
      :class:`TaviClient` at ``tavi_url`` or its own default): the validate/submit/wait/fetch seam.
    - ``runner`` (runner object, config key ``run_isar`` — a bare callable/runner adapted via
      :func:`as_runner`): runs the ISAR analysis; reused across the campaign and closed in its
      ``finally``. Resolution: an explicit ``config["run_isar"]`` wins; else the persistent
      :class:`WorkerRunner` (default, config key ``worker`` — one warm process per campaign, cold
      :class:`SubprocessRunner` fallback); else a fresh-subprocess :class:`SubprocessRunner` when
      ``worker`` is off.
    - ``worker`` (:class:`bool`, config key ``worker``, default ``True``): use the persistent analysis
      worker (``isar drive --no-worker`` disables it). Ignored when ``run_isar`` is supplied.
    - ``run_timeout`` (:class:`float`, config key ``run_timeout``, default 900.0): the worker's per-job
      wall-clock ceiling (seconds) before it falls back cold. Ignored when ``run_isar`` is supplied.
    - ``init_isar`` (:class:`Callable` ``(workspace, probe) -> Any``, config key ``init_isar``, default
      the real ``isar init`` seam): scaffolds the instrument card lazily after the first scan lands.
    - ``resolution_cb`` (:class:`Callable`, built from config key ``resolution`` — a float / dict /
      callable, default wraps ``GET /resolution``): the energy-resolution FWHM source for acquisition
      and the Fisher screen.
    - ``engine`` (:class:`str`, ``= objective.tier`` — NOT a config key, derived from the objective):
      the scan engine tier stamped into every ``POST /scan`` body.
    - ``base_seed`` (:class:`int`, config key ``seed``, default 0): scan ``n`` uses ``base_seed + n``.
    - ``max_iters`` (:class:`int`, config key ``max_iters``, default :data:`DEFAULT_MAX_ITERS`): the
      adaptive-loop iteration cap.
    - ``wait_timeout`` (:class:`float`, config key ``wait_timeout``, default
      :data:`DEFAULT_WAIT_TIMEOUT`): the per-scan ``wait_scan`` timeout in seconds.
    - ``floor`` (:class:`float`, ``= systematic_floor(calibration, config["floor"])`` — config key
      ``floor`` overrides): the systematic ω₀ floor added to every convergence gate.
    - ``sample`` (:class:`str`, config key ``sample``, default ``"Al_phonon_DFT"``): stamped into every
      validate/scan body's ``parameters`` because the instrument API no longer inherits the GUI's
      sample selection.
    - ``bayes_errors`` (:class:`bool`, config key ``bayes_errors``, default ``False``): campaign-level
      opt-in for the ``bayes`` errors tier (superset of ``full``: bootstrap + MINOS + profile +
      posterior). When set, every place that would otherwise use the ``full`` tier (escalation in
      :func:`_errors_tier_for`, the certify pass) uses ``bayes`` instead — see :func:`_full_tier`. The
      bootstrap/profile intervals ``full`` produces are unchanged; the posterior rides along as CSV
      diagnostics only and never feeds ``_pick_interval``/``_is_certifiable``/certification.
    """
    workspace: Path
    data_dir: Path
    log_path: Path
    calibration: Calibration
    client: Any
    runner: Any
    init_isar: Callable
    resolution_cb: Callable
    engine: str
    base_seed: int
    max_iters: int
    wait_timeout: float
    floor: float
    worker: bool = True
    run_timeout: float = 900.0
    run_nonce: str = ""
    # Instrument point-count ceiling, read once per campaign (GET /state|/schema), else DEFAULT_MAX_POINTS.
    max_points: int = DEFAULT_MAX_POINTS
    max_points_source: str = "default"
    sample: str = "Al_phonon_DFT"
    bayes_errors: bool = False
    # Cooperative operator stop (GUI subprocess control). When set, each iteration's top checks for
    # ``<workspace>/report/control/stop-<control_token>.json`` and, if present, breaks with an
    # ``operator_stopped`` terminal state. ``None`` (default) → behaviour byte-identical to today.
    control_token: str | None = None
    # Campaign provenance fingerprint stamped into the campaign_start event, plumbed from the CLI:
    # ``{"kind": "goal"|"spec"|"resume", "sha256": <hex>}``. ``None`` → field omitted (back-compat).
    source: dict | None = None


def _run_nonce(workspace: Path) -> str:
    """The campaign's idempotency nonce — minted once per workspace and persisted, so a crash-resume
    replays the SAME ``Idempotency-Key`` per scan (dedupe still works) while a fresh campaign can
    never collide with a previous run's instrument-side idempotency cache (the TAVI cache-poisoning
    replay: a poisoned/truncated cached result served to every later run that reuses the key)."""
    path = workspace / ".run_nonce"
    try:
        nonce = path.read_text(encoding="utf-8").strip()
        if nonce:
            return nonce
    except OSError:
        pass
    nonce = uuid.uuid4().hex[:12]
    workspace.mkdir(parents=True, exist_ok=True)
    path.write_text(nonce + "\n", encoding="utf-8")
    return nonce


def _campaign_config(objective: ObjectiveSpec, config: dict | None) -> CampaignConfig:
    """Resolve the raw ``config`` dict (plus ``objective``) into a frozen :class:`CampaignConfig`.

    Centralizes every default and every seam construction (calibration, client, runner, resolution
    callback, systematic floor). See :class:`CampaignConfig` for the per-key documentation."""
    config = dict(config or {})
    workspace = Path(config["workspace"])
    log_path = Path(config.get("session_log", workspace / "report" / "session_log.jsonl"))
    client = config.get("client")
    if client is None:
        client = TaviClient(config["tavi_url"]) if config.get("tavi_url") else TaviClient()
    # Runner seam: an explicit ``config["run_isar"]`` (callable hook or runner object) is adapted via
    # ``as_runner`` and always wins; otherwise the persistent WorkerRunner (one warm process per
    # campaign, cold SubprocessRunner fallback) unless ``worker`` is off, in which case a
    # fresh-subprocess SubprocessRunner per job.
    worker = bool(config.get("worker", True))
    run_timeout = float(config.get("run_timeout", 900.0))
    if config.get("run_isar") is not None:
        runner = as_runner(config["run_isar"])
    elif worker:
        runner = WorkerRunner(
            workspace, job_timeout=run_timeout, fallback=SubprocessRunner(),
            on_degraded=lambda: log_event(
                log_path, "exception",
                {"action": "worker_degraded",
                 "note": "persistent analysis worker failed repeatedly; permanent cold-subprocess fallback"}))
    else:
        runner = SubprocessRunner()
    calibration = _resolve_calibration(config.get("calibration"))
    # Read the instrument's point-count ceiling once per campaign (falls back silently to
    # DEFAULT_MAX_POINTS if the endpoint is unreachable / carries no limit).
    max_points, max_points_source = _preflight_max_points(client)
    return CampaignConfig(
        workspace=workspace,
        data_dir=Path(config.get("data_dir", workspace / "data")),
        log_path=log_path,
        calibration=calibration,
        client=client,
        runner=runner,
        init_isar=config.get("init_isar", _default_init_isar),
        resolution_cb=_make_resolution_cb(client, config.get("resolution")),
        engine=objective.tier,
        base_seed=int(config.get("seed", 0)),
        max_iters=int(config.get("max_iters", DEFAULT_MAX_ITERS)),
        wait_timeout=float(config.get("wait_timeout", DEFAULT_WAIT_TIMEOUT)),
        floor=systematic_floor(calibration, config.get("floor")),
        worker=worker,
        run_timeout=run_timeout,
        run_nonce=_run_nonce(workspace),
        max_points=max_points,
        max_points_source=max_points_source,
        sample=str(config.get("sample", "Al_phonon_DFT")),
        bayes_errors=bool(config.get("bayes_errors", False)),
        control_token=(str(config["control_token"]) if config.get("control_token") else None),
        source=(dict(config["source"]) if config.get("source") else None),
    )


def _declined_observation(proposal, verdict: str) -> dict:
    """The observation appended when a proposed scan yields no fit — validation rejected it, or the
    scan job never reached ``done``. Identical shape at both sites; only ``verdict`` differs
    (``"validation_rejected"`` vs the terminal job state)."""
    meta = proposal.meta or {}
    return {"hkl": (proposal.h, proposal.k, proposal.l), "kind": proposal.kind,
            "calibrated_halfwidth": None, "resolved": False, "disposition": "declined",
            "verdict": verdict, "omega0": None,
            "submitted_ncount": int(proposal.ncount),
            "acquisition_id": proposal.acquisition_id,
            "driver_inserted": bool(meta.get("driver_inserted", False)),
            "certification_eligible": bool(meta.get("certification_eligible", True))}


def _persist_campaign_target(workspace: Path, objective) -> None:
    """Copy the frozen objective's active analysis target into machine-owned card state."""
    policy_path = workspace / "card.policy.toml"
    state_path = workspace / "card.state.json"
    # Runner seams used by dry-run/notebook consumers may deliberately provide a no-op initializer.
    # There is then no machine state to extend; the real runner creates the policy immediately above.
    if not policy_path.exists():
        return
    card = Card.load(policy_path, state_path)
    target = {"branch": getattr(objective, "target_branch", "acoustic"),
              "label": getattr(objective, "target_label", "acoustic"),
              "selector": getattr(objective, "target_selector", "lowest_energy"),
              "target_energy_meV": getattr(objective, "target_energy_meV", None),
              "energy_search_meV": list(getattr(objective, "energy_search_meV", (-2.0, 7.0)))}
    snapshot = ({"hypotheses": list(getattr(objective, "hypotheses", []) or []),
                 "target_hypothesis": getattr(objective, "target_hypothesis", None),
                 "source": getattr(objective, "hypothesis_source", None),
                 "fingerprint": getattr(objective, "hypothesis_fingerprint", None),
                 "steering": dict(getattr(objective, "steering", {}) or {})}
                if getattr(objective, "hypotheses", None) else {})
    if card.state.campaign_target != target or card.state.hypothesis_snapshot != snapshot:
        card.state.campaign_target = target
        card.state.hypothesis_snapshot = snapshot
        card.save_state(state_path)


def _execute_proposal(*, cfg: CampaignConfig, objective, proposal, it: int, scan_counter: int,
                      budget_state: dict, observations: list[dict], consumed: set,
                      scan_kind: dict) -> tuple[list[dict], int]:
    """Run one proposed scan to a folded result, with bounded missed-window re-centering (a LOOK, §1).

    Owns the inner validate → submit → wait → fetch/save → (lazy init) → ``isar run`` → fold → analyze
    loop and its recenter/widen retries. Mutates ``observations`` (appends folded or declined rows),
    ``consumed`` (via :func:`_new_rows`), ``scan_kind``, ``budget_state``, and ``proposal`` (e_lo/e_hi/
    n_points on recenter) in place; returns ``(rows, scan_counter)`` — the final folded rows (empty on
    a decline) plus the advanced scan counter. Every ``timed`` span and ``log_event`` payload is
    verbatim from the original loop body (the profiling / event contract is part of the identity gate)."""
    client, runner = cfg.client, cfg.runner
    log_path, workspace, data_dir = cfg.log_path, cfg.workspace, cfg.data_dir
    # ---- run this proposal, with bounded missed-window re-centering (a LOOK, §1) ----------
    attempt = 0
    rows: list[dict] = []
    # Tracks an in-flight full-tier promotion whose outcome has not yet been logged (escalate_fit,
    # spec §5.1): set when a production q is promoted to the full error tier, cleared the moment its
    # outcome record is written (at the fold, or as `superseded` if the retry declines first).
    promoted = False
    support_adjusted = False
    while True:
        # Point-count guard (single checkpoint before ANY validate/submit): a missed-window recenter
        # widens e_lo/e_hi without recomputing the tight e_step, so the emitted `deltaE lo hi step`
        # command can exceed TAVI's max_points and latch its validator. Coarsen the step to keep the
        # full window and fit the limit; a no-op on a proposal that already fits.
        _guard_point_count(proposal, cfg.max_points, cfg.max_points_source, log_path, it)
        with timed(log_path, "validate", iteration=it, attempt=attempt):
            val = client.validate(_validate_body(proposal, sample=cfg.sample))
        feasible_points = _feasible_point_count(val, proposal.n_points or 1)
        target_band = (proposal.meta or {}).get("target_band")
        min_support = int((getattr(objective, "steering", None) or {}).get(
            "minimum_target_support_points", 0))
        target_support = _target_supporting_feasible_points(val, target_band)
        log_event(log_path, "validate", {"would_queue": val.get("would_queue"),
                                          "blockers": val.get("blockers"), "eta": val.get("eta"),
                                          "requested_points": val.get("requested_points"),
                                          "feasible_points": feasible_points,
                                          "target_supporting_feasible_points": target_support,
                                          "partial": val.get("partial"),
                                          "feasible_segments": val.get("feasible_segments"),
                                          "infeasible": val.get("infeasible"),
                                          "iter": it})
        if (min_support > 0 and target_support is not None and target_support < min_support
                and val.get("would_queue", False)):
            mean, sigma = map(float, target_band)
            desired_step = max((4.0 * sigma) / max(min_support - 1, 1), 1e-6)
            if not support_adjusted and desired_step < proposal.e_step:
                old_step = proposal.e_step
                proposal.e_step = desired_step
                proposal.n_points = points_in_window(proposal.e_lo, proposal.e_hi, proposal.e_step)
                support_adjusted = True
                log_event(log_path, "exception", {
                    "action": "increase_target_support", "hkl": [proposal.h, proposal.k, proposal.l],
                    "old_step": old_step, "new_step": proposal.e_step,
                    "target_supporting_feasible_points": target_support,
                    "required": min_support, "iter": it})
                continue
            log_event(log_path, "exception", {
                "action": "insufficient_target_support", "hkl": [proposal.h, proposal.k, proposal.l],
                "target_supporting_feasible_points": target_support,
                "required": min_support, "iter": it})
            observations.append(_declined_observation(proposal, "insufficient_target_support"))
            break
        if not val.get("would_queue", False):
            if feasible_points <= 0 and int(val.get("requested_points") or proposal.n_points or 0) > 0:
                log_event(log_path, "exception", {
                    "action": "geometry_unreachable", "phase": "validation",
                    "hkl": [proposal.h, proposal.k, proposal.l], "blockers": val.get("blockers"),
                    "iter": it})
                observations.append(_declined_observation(proposal, "geometry_unreachable"))
                break
            v_act = exc.handle_validation_rejection(val.get("blockers") or [])
            log_event(log_path, "exception", {**v_act.to_dict(), "iter": it})
            if promoted:   # a promoted retry that now declines before folding — supersede the promotion
                log_event(log_path, "escalate_fit",
                          {"phase": "outcome", "iter": it, "job_id": None,
                           "hkl": [proposal.h, proposal.k, proposal.l], "to_tier": "full",
                           "outcome": "superseded", "n_rows": 0, "dispositions": []})
                promoted = False
            observations.append(_declined_observation(proposal, "validation_rejected"))
            break

        with timed(log_path, "submit", iteration=it, attempt=attempt) as _m:
            body = _scan_body(proposal, cfg.engine, cfg.base_seed + scan_counter,
                               sample=cfg.sample)
            try:
                submit = client.submit_scan(
                    body,
                    idempotency_key=(
                        f"{objective.name or 'campaign'}-{cfg.run_nonce}-{scan_counter}"))
            except TaviError as error:
                code = _tavi_error_code(error)
                if error.status == 400 and code == "all_points_infeasible":
                    log_event(log_path, "exception", {
                        "action": "geometry_unreachable",
                        "error": str(error),
                        "hkl": [proposal.h, proposal.k, proposal.l],
                        "iter": it,
                    })
                    observations.append(
                        _declined_observation(proposal, "geometry_unreachable"))
                    break
                raise
            scan_counter += 1
            job_id = submit.get("job_id")
            _m["job_id"] = job_id
        eta = (submit.get("eta") or {}).get("estimated_seconds")
        _charge_budget(budget_state, proposal, eta, n_points=feasible_points)
        # The run event doubles as the durable SPEND LEDGER for crash resume: kind/ncount/n_points/
        # hkl + the charged cost (= n_points×ncount) let a restart recover budget + scan_counter.
        submission_payload = {
            "job_id": job_id, "state": submit.get("state"), "eta": submit.get("eta"),
            "attempt": attempt, "acquisition_id": proposal.acquisition_id,
            "kind": proposal.kind, "ncount": int(proposal.ncount),
            "submitted_ncount": int(proposal.ncount),
            "requested_points": int(proposal.n_points or 0), "n_points": feasible_points,
            "hkl": [proposal.h, proposal.k, proposal.l],
            "acquisition_target_energy": float(proposal.e_pred),
            "driver_inserted": bool((proposal.meta or {}).get("driver_inserted", False)),
            "certification_eligible": bool((proposal.meta or {}).get(
                "certification_eligible", True)),
            "insertion_purpose": (proposal.meta or {}).get("insertion_purpose"),
            "cost": float(feasible_points) * float(proposal.ncount), "iter": it,
        }
        log_event(log_path, "run", submission_payload)
        log_event(log_path, "physical_submission", submission_payload)

        with timed(log_path, "wait_scan", iteration=it, job_id=job_id) as _m:
            snap = client.wait_scan(job_id, timeout=cfg.wait_timeout)
            _m["state"] = snap.get("state")
        if snap.get("state") != "done":
            log_event(log_path, "exception", {"action": "scan_not_done", "job_id": job_id,
                                              "state": snap.get("state"), "error": snap.get("error"),
                                              "iter": it})
            if promoted:   # a promoted retry whose scan never reached done — supersede the promotion
                log_event(log_path, "escalate_fit",
                          {"phase": "outcome", "iter": it, "job_id": job_id,
                           "hkl": [proposal.h, proposal.k, proposal.l], "to_tier": "full",
                           "outcome": "superseded", "n_rows": 0, "dispositions": []})
                promoted = False
            observations.append(_declined_observation(proposal, str(snap.get("state"))))
            break

        with timed(log_path, "fetch_save", iteration=it):
            data = client.get_scan_data(job_id)
            scan_path = save_scan_json(data, data_dir)
        executed_points = _executed_point_count(data, feasible_points)
        if executed_points != feasible_points:
            neutron_delta, seconds_delta = _reconcile_budget_points(
                budget_state, proposal.ncount, feasible_points, executed_points, eta=eta)
            log_event(log_path, "exception", {
                "action": "feasibility_reconciled",
                "job_id": job_id,
                "hkl": [proposal.h, proposal.k, proposal.l],
                "planned_points": feasible_points,
                "executed_points": executed_points,
                "ncount": int(proposal.ncount),
                "executed_cost": float(executed_points) * float(proposal.ncount),
                "neutron_delta": neutron_delta,
                "seconds_delta": seconds_delta,
                "iter": it,
            })
        # Lazy `isar init`: the instrument card can only be scaffolded from a recognizable
        # data file, which first exists NOW. Subprocess keeps the analysis boundary.
        if not (workspace / "card.policy.toml").exists():
            with timed(log_path, "isar_init", iteration=it):
                cfg.init_isar(workspace, scan_path)
            log_event(log_path, "decide", {"event": "workspace_init", "action": "workspace_init",
                                           "probe": str(scan_path), "iter": it})
        _persist_campaign_target(workspace, objective)
        # Error tier per the escalation ladder: quicklook → cheap (steer only), production →
        # fast (HESSE interval + trust flag), escalating to full (bootstrap+profile) only where an
        # untrusted-HESSE q needs the reliable interval. The bootstrap fires only where needed.
        tier = _errors_tier_for(proposal, workspace, cfg)
        # escalate_fit PROMOTE (spec §5.1): a production/confirm q bumped to the full (bootstrap+
        # profile) tier because its only interval is an untrusted HESSE one. Paired with an OUTCOME
        # record after the fold so the promotion and its result are both in the log.
        if proposal.kind in PRODUCTION_KINDS and tier == "full":
            promoted = True
            log_event(log_path, "escalate_fit",
                      {"phase": "promote", "iter": it, "attempt": attempt,
                       "hkl": [proposal.h, proposal.k, proposal.l], "from_tier": "fast",
                       "to_tier": "full", "reason": "untrusted_hesse_no_reliable_interval"})
        with timed(log_path, "isar_run", iteration=it, errors_tier=tier, attempt=attempt):
            # render=False: skip the per-invocation report (report layer / matplotlib import is the
            # dominant cost and nothing mid-campaign reads it); the campaign re-renders once at the end.
            runner.run(workspace, [data_dir], errors_tier=tier, render=False)

        with timed(log_path, "fold_rows", iteration=it) as _m:
            rows = _new_rows(workspace, consumed)
            # The scan's actual monitor cost (n_points reflects any recenter; charged once per scan,
            # onto the first detector row so counts_spent per q is not multiplied by detector count).
            scan_cost = float(executed_points) * float(proposal.ncount)
            for j, r in enumerate(rows):
                observations.append(_observation_from_row(
                    r, proposal.kind, proposal.ncount, cfg.calibration,
                    proposal_cost=scan_cost if j == 0 else 0.0,
                    driver_inserted=bool((proposal.meta or {}).get("driver_inserted", False)),
                    certification_eligible=bool((proposal.meta or {}).get(
                        "certification_eligible", True)),
                    acquisition_id=proposal.acquisition_id))
                scan_kind[(r.get("scan"), r.get("detector"))] = (
                    proposal.kind, proposal.ncount, proposal.acquisition_id)
            _m["n_rows"] = len(rows)
        # Embed the subprocess stage-timing snapshot (best-effort) + the folded rows' outcomes.
        timings = _read_run_timings(workspace)
        analyze_payload = {"job_id": job_id, "errors_tier": tier, "iter": it,
                           "n_rows": len(rows),
                           "dispositions": [r.get("disposition") or r.get("verdict") for r in rows]}
        if timings is not None:
            analyze_payload["timings"] = timings
        log_event(log_path, "analyze", analyze_payload)
        _record_pooled_master_update(
            workspace, cfg.calibration, proposal, log_path=log_path,
            observations=observations)

        if promoted:
            # escalate_fit OUTCOME (spec §5.1): did the full-tier promotion seat a certifiable
            # interval? `seated` (≥1 folded row certifiable), `failed` (rows folded, none certifiable),
            # `superseded` (no rows folded at all). `_row_certifiable` is the SAME predicate that seats
            # convergence in `_observation_from_row`, so promotion accounting cannot drift from it.
            if not rows:
                outcome = "superseded"
            elif any(_row_certifiable(r) for r in rows):
                outcome = "seated"
            else:
                outcome = "failed"
            log_event(log_path, "escalate_fit",
                      {"phase": "outcome", "iter": it, "job_id": job_id,
                       "hkl": [proposal.h, proposal.k, proposal.l], "to_tier": "full",
                       "outcome": outcome, "n_rows": len(rows),
                       "dispositions": [r.get("disposition") or r.get("verdict") for r in rows]})
            promoted = False

        if not _rows_missed(rows):
            break
        # Missed window: LOOK at the counts to re-center + widen, up to the attempt cap.
        with timed(log_path, "missed_window", iteration=it):
            result = data.get("result") or {}
            act = exc.handle_missed_window(
                result.get("counts") or [], result.get("scan_values_1") or [],
                e_lo=proposal.e_lo, e_hi=proposal.e_hi, attempt=attempt,
                resolution_fwhm=(proposal.fwhm_dE or None))
        log_event(log_path, "exception", {**act.to_dict(), "iter": it})
        # Both `recenter` and `recenter_refused_elastic` mutate the window (act.params carry e_lo/e_hi)
        # and re-enter the loop; the elastic refusal widens about the OLD center rather than chasing
        # ΔE ≈ 0. Any other action (escalate) ends the retry loop.
        if act.action not in ("recenter", "recenter_refused_elastic"):
            break
        proposal.e_lo = act.params["e_lo"]
        proposal.e_hi = act.params["e_hi"]
        proposal.n_points = points_in_window(
            proposal.e_lo, proposal.e_hi, proposal.e_step,
            max_points=int((proposal.meta or {}).get("max_scan_points", MAX_POINTS)))
        attempt += 1

    return rows, scan_counter


def _certify(*, cfg: CampaignConfig, decision, observations: list[dict],
             scan_kind: dict) -> tuple[list[dict], bool]:
    """The end-of-campaign certification pass (the "one bootstrap to certify" decision).

    When the loop stopped ``met``, run ONE forced full (bootstrap+profile) ``isar run`` so every
    reported ω₀ interval is bootstrap-backed, then rebuild ``observations`` from the certified
    ``results.csv``. Best-effort: a certification hiccup logs ``certify_failed`` and reports
    ``ran_certify=False`` rather than losing a met campaign. Returns ``(observations, ran_certify)`` —
    the (possibly rebuilt) observations plus whether certification actually completed."""
    log_path, workspace, data_dir = cfg.log_path, cfg.workspace, cfg.data_dir
    ran_certify = decision is not None and decision.state == "met"
    if not ran_certify:
        return observations, False
    full_tier = _full_tier(cfg)
    log_event(log_path, "decide", {"event": "certify_start", "action": "certify_start", "tier": full_tier})
    # escalate_fit PROMOTE (spec §5.1): the end-of-campaign certify pass forces every q to the full
    # (bootstrap+profile) tier — or `bayes` when the campaign opted in, see _full_tier. Campaign-wide,
    # so no single hkl/attempt — reason names the pass.
    log_event(log_path, "escalate_fit",
              {"phase": "promote", "iter": None, "hkl": None, "from_tier": "fast",
               "to_tier": full_tier, "reason": "certify_pass"})
    try:
        with timed(log_path, "certify", iteration=None):
            cfg.runner.run(workspace, [data_dir], errors_tier=full_tier, force=True, render=False)
        # Rebuilding folded rows must not erase steering-only provenance or the two-crossing evidence
        # that made the hypothesis campaign eligible for certification in the first place.
        provenance = {
            (observation.get("scan"), observation.get("detector")): {
                "driver_inserted": bool(observation.get("driver_inserted", False)),
                "certification_eligible": bool(observation.get("certification_eligible", True)),
            }
            for observation in observations
            if observation.get("scan") is not None and observation.get("detector") is not None
        }
        crossing_markers = [dict(observation) for observation in observations
                            if observation.get("kind") == "certification_crossing"]
        certified = _recertified_observations(workspace, cfg.calibration, scan_kind)
        for observation in certified:
            observation.update(provenance.get(
                (observation.get("scan"), observation.get("detector")), {}))
        observations = [*certified, *crossing_markers]
        # Embed the certification subprocess timings (iter=None — the certify pass is reported
        # separately, never averaged into per-iteration stats).
        timings = _read_run_timings(workspace)
        analyze_payload = {"job_id": None, "errors_tier": full_tier, "iter": None,
                           "n_rows": len(observations),
                           "dispositions": [o.get("disposition") or o.get("verdict")
                                            for o in observations]}
        if timings is not None:
            analyze_payload["timings"] = timings
        log_event(log_path, "analyze", analyze_payload)
        log_event(log_path, "decide", {"event": "certify_done", "action": "certify_done"})
        return observations, True
    except Exception as e:   # noqa: BLE001 — never lose a met campaign to a certification hiccup
        log_event(log_path, "exception", {"action": "certify_failed", "error": str(e)})
        return observations, False


def _render_campaign_report(cfg: CampaignConfig) -> None:
    """End-of-campaign report render (Stage 3 report deferral).

    Every ``isar run`` in the loop passes ``render=False`` (report layer / matplotlib import is the
    dominant per-invocation cost and nothing mid-campaign consumes report.md/.html/.json/figures), so
    the report is stale on disk. Render it ONCE here from persisted state via ``isar report`` (the same
    re-render path a human would use), in a subprocess (matplotlib stays out of the driver process, and
    the analysis boundary is preserved). Uses the runner's own subprocess mechanism directly rather than
    the runner seam — this is an ``isar report``, not an ``isar run``.

    Best-effort: skipped when no analysis ran (no ``results.csv``); any failure logs an ``exception``
    event and never fails a completed campaign. The ``report_render`` timing span is always recorded."""
    if not (cfg.workspace / "report" / "results.csv").exists():
        return   # no analysis ran (no scans) → nothing to render
    cmd = [sys.executable, "-m", "isar.cli.main", "report", "--workspace", str(cfg.workspace)]
    try:
        with timed(cfg.log_path, "report_render", iteration=None):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"isar report exited {proc.returncode}: {(proc.stderr or '').strip()}")
    except Exception as e:   # noqa: BLE001 — a render hiccup never fails an otherwise-complete campaign
        log_event(cfg.log_path, "exception", {"action": "report_render_failed", "error": str(e)})


def _render_campaign_diagnostic(cfg: CampaignConfig) -> None:
    """End-of-campaign CAMPAIGN_DIAGNOSTIC.md render (Part 2).

    The architect's post-mortem — assembled deterministically from the file contract (see
    ``isar.report.campaign_diagnostic``). Rendered in a subprocess (matplotlib / report layer stays
    out of the driver process, analysis boundary preserved), right after ``_render_campaign_report``
    and BEFORE the terminal ``stop`` event so ``stop`` remains the last logged decision.

    Best-effort: skipped when no analysis ran (no ``results.csv``); any failure logs an ``exception``
    event and never fails a completed campaign. The ``diagnostic_render`` timing span is recorded."""
    if not (cfg.workspace / "report" / "results.csv").exists():
        return   # no analysis ran (no scans) → nothing to diagnose
    cmd = [sys.executable, "-m", "isar.cli.main", "diagnose", "--workspace", str(cfg.workspace)]
    try:
        with timed(cfg.log_path, "diagnostic_render", iteration=None):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"isar diagnose exited {proc.returncode}: {(proc.stderr or '').strip()}")
    except Exception as e:   # noqa: BLE001 — a render hiccup never fails an otherwise-complete campaign
        log_event(cfg.log_path, "exception", {"action": "diagnostic_render_failed", "error": str(e)})


def _operator_stop_requested(cfg: CampaignConfig, it: int) -> bool:
    """True when the operator has requested a cooperative stop for THIS campaign's control token.

    The GUI launches ``isar drive`` as a subprocess and requests a stop by writing
    ``<workspace>/report/control/stop-<control_token>.json``. A hard kill during ``wait_scan`` would
    strand the in-flight scan (its ``run`` event is the spend ledger, so resume would re-propose and
    double-spend); the cooperative token lets the loop finish the current iteration and exit at the
    next boundary instead.

    Only THIS token's file is consulted, so a previous campaign's stale stop file is inert
    automatically. Existence is the whole protocol — the file's JSON body is provenance, not a command,
    so it is never read here and never modified/deleted (nothing is ever deleted). A stat error is
    logged (never swallowed) and treated as NOT a stop: a corrupt/racing request must not be silently
    honoured — the GUI retries by rewriting."""
    token = cfg.control_token
    if not token:
        return False
    marker = cfg.workspace / "report" / "control" / f"stop-{token}.json"
    try:
        return marker.exists()
    except OSError as e:
        log_event(cfg.log_path, "exception",
                  {"action": "operator_stop_check_failed", "token": token, "iter": it, "error": str(e)})
        return False


def run_campaign(objective: ObjectiveSpec, config: dict | None = None) -> dict[str, Any]:
    """Run an adaptive measurement campaign to satisfy ``objective`` (LLM_HARNESS_DESIGN §8).

    Returns a campaign summary dict (built by :func:`_campaign_summary`). Every ``config`` key, its
    type, its default, and where it is used are resolved and documented by :func:`_campaign_config` /
    :class:`CampaignConfig` — see there for the full key reference."""
    cfg = _campaign_config(objective, config)

    # Crash-safe resume: recover the spend ledger + folded rows + observations from the durable files
    # (session_log + results.csv). A cold workspace yields the fresh-start values.
    resumed = _resume_state(cfg.workspace, cfg.log_path, cfg.calibration)
    budget = dict(objective.budget)
    budget_state = {"kind": budget["kind"], "amount": budget["amount"],
                    "neutrons": resumed["neutrons"], "seconds": resumed["seconds"], "spent": 0.0}
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons"
                             else budget_state["seconds"])

    observations: list[dict] = resumed["observations"]
    consumed: set = resumed["consumed"]
    scan_kind: dict = resumed["scan_kind"]   # (scan, detector) -> (kind, ncount), to rebuild obs after certify
    scan_counter = resumed["scan_counter"]

    start_payload = {"event": "campaign_start", "action": "campaign_start",
                     "objective": objective.to_dict(),
                     "calibration": {"inflation": cfg.calibration.inflation,
                                     "floor_meV": cfg.floor},
                     "resumed": {"scan_counter": scan_counter,
                                 "n_observations": len(observations),
                                 "spent": budget_state["spent"]}}
    if cfg.source is not None:
        start_payload["source"] = cfg.source           # provenance fingerprint (goal/spec/resume + sha256)
    log_event(cfg.log_path, "decide", start_payload)

    decision = None
    operator_stop = False        # set when a cooperative operator-stop file is found at an iteration top
    op_stop_iter: int | None = None
    try:
        # Instrumentation only (no logic change): each stage runs inside a ``timed`` span that appends a
        # "timing" event (resume-inert — _resume_state reads only "run" events), and every in-loop
        # log_event carries the iteration index ``it``. The outer ``timed(... "iteration")`` covers the
        # whole body including all break paths (a break exits the span normally, ok=True).
        for it in range(cfg.max_iters):
            with timed(cfg.log_path, "iteration", iteration=it):
                # Cooperative operator stop: honoured at the iteration boundary so the previous
                # iteration's scan is already persisted/analysed (no stranded in-flight spend). Log ONE
                # acknowledgement and break; the terminal state becomes operator_stopped (part B).
                if _operator_stop_requested(cfg, it):
                    log_event(cfg.log_path, "decide",
                              {"event": "operator_stop", "action": "operator_stop",
                               "iter": it, "token": cfg.control_token})
                    operator_stop = True
                    op_stop_iter = it
                    break
                with timed(cfg.log_path, "stop_eval", iteration=it):
                    master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
                    if _record_eligible_crossings(grid, observations, cfg.log_path):
                        master_rows, grid = _grid_state(
                            objective, observations, cfg.workspace, cfg.calibration)
                    decision = evaluate_stop(objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
                if decision.state != "continue":
                    break

                with timed(cfg.log_path, "model_fit", iteration=it):
                    model = acq_model.fit(model_points(grid))
                # 5b Fisher tier: read the branch the session persisted into card.state.json FRESH each
                # iteration (durable / crash-safe — a file, never a live session object). No branch yet →
                # provider is None → propose_scan falls back to the 5a counts tier.
                with timed(cfg.log_path, "fisher_provider", iteration=it):
                    durable_state = read_card_state(cfg.workspace)
                    fisher = (None if objective.target_branch != "acoustic" else
                              _make_fisher_provider(
                                  durable_state, cfg.resolution_cb,
                                  production_ncount=int((getattr(objective, "steering", None) or {}).get(
                                      "production_batch_ncount", PRODUCTION_NCOUNT))))
                    classify, rival_band = _make_classification_providers(
                        durable_state, objective, cfg.resolution_cb)
                with timed(cfg.log_path, "propose", iteration=it):
                    try:
                        proposal = propose_scan(
                            objective, model, observations, cfg.resolution_cb, budget_state,
                            master_rows=master_rows, fisher=fisher,
                            classify=classify, rival_band=rival_band)
                    except BudgetInsufficientForMinimumBatch as error:
                        log_event(cfg.log_path, "exception", {
                            "action": "budget_insufficient_for_minimum_batch",
                            "error": str(error), "iter": it})
                        decision = StopDecision(
                            "unmeetable", [str(error)],
                            evaluate_stop(objective, grid, budget_state,
                                          calibration=cfg.calibration, floor=cfg.floor).per_q)
                        proposal = None
                if proposal is None:
                    if decision is None or decision.state == "continue":
                        decision = evaluate_stop(
                            objective, grid, budget_state,
                            calibration=cfg.calibration, floor=cfg.floor)
                    threshold = float((getattr(objective, "steering", None) or {}).get(
                        "minimum_information_per_cost", 0.0))
                    if decision.state == "continue" and threshold > 0.0:
                        decision = StopDecision(
                            "diminishing_returns",
                            [f"best available marginal information/cost is below {threshold:g}"],
                            decision.per_q)
                    break
                proposal.acquisition_id = (proposal.acquisition_id
                                           or f"{cfg.run_nonce}:{scan_counter:08d}")
                log_event(cfg.log_path, "propose", {**proposal.to_dict(), "iter": it})

                b_act = exc.handle_budget(budget_state)
                if b_act is not None:
                    log_event(cfg.log_path, "exception", {**b_act.to_dict(), "iter": it})
                    if b_act.action == "exhausted":
                        break

                rows, scan_counter = _execute_proposal(
                    cfg=cfg, objective=objective, proposal=proposal, it=it, scan_counter=scan_counter,
                    budget_state=budget_state, observations=observations, consumed=consumed,
                    scan_kind=scan_kind)

                # ---- exception branch on the final verdict (flagged / held) ----------------------------
                with timed(cfg.log_path, "verdict_exceptions", iteration=it):
                    for r in rows:
                        disp = r.get("disposition")
                        status = r.get("status")
                        if status in exc.HELD_STATUSES:
                            log_event(cfg.log_path, "exception", {**exc.handle_held(status).to_dict(), "iter": it})
                        elif disp not in (None, "resolved") and disp not in exc.MISSED_VERDICTS:
                            log_event(cfg.log_path, "exception",
                                      {**exc.handle_flagged(disp, verdict=r.get("verdict")).to_dict(), "iter": it})

                with timed(cfg.log_path, "model_update", iteration=it):
                    master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
                log_event(cfg.log_path, "decide", {"event": "model_update", "action": "model_update",
                                                   "n_observations": len(observations),
                                                   "converged": sum(1 for g in grid if g.converged),
                                                   "n_grid": len(grid), "iter": it})
        else:
            # The loop exhausted max_iters without a break, so the top-of-iteration check will never
            # run again — but the boundary AFTER the final allowed iteration is still an iteration
            # boundary, and a stop requested during that last scan must be honoured there (with
            # --max-iters 1 it is the ONLY boundary that can see it). Same ordering rule as in-loop:
            # the stop wins before any terminal bookkeeping (certification must not run). The break
            # paths (met/unmeetable/no-proposal/budget) deliberately get no post-check — their
            # boundary check already ran before the decision, and a campaign that ended on its own
            # reports its true state. ``iter`` is cfg.max_iters, the would-be next iteration (the
            # loop variable is unbound when max_iters == 0, so it is never referenced here).
            if _operator_stop_requested(cfg, cfg.max_iters):
                log_event(cfg.log_path, "decide",
                          {"event": "operator_stop", "action": "operator_stop",
                           "iter": cfg.max_iters, "token": cfg.control_token})
                operator_stop = True
                op_stop_iter = cfg.max_iters

        if operator_stop:
            # Operator-stop terminal state (part B): an honest snapshot WITHOUT certification — a stop
            # request must never trigger the end-of-campaign certify bootstrap. Recompute the final grid
            # + evaluate_stop for a truthful picture, but record it under stop.grid_state; the terminal
            # state itself is operator_stopped. The decision-None path (stop found before any iteration
            # completed) is safe here: the snapshot is computed fresh from the grid, never from
            # ``decision``, so no AttributeError.
            master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
            snapshot = evaluate_stop(objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
            stop_override = {
                "state": "operator_stopped",
                "reasons": [f"operator stop request honored at iteration {op_stop_iter}",
                            f"token {cfg.control_token}"],
                "per_q": list(snapshot.per_q),
                "grid_state": snapshot.to_dict(),
            }
            summary = _campaign_summary(objective, grid, snapshot, budget_state, cfg.workspace,
                                        cfg.log_path, len(observations), stop_override=stop_override)
            stop_payload = {"state": "operator_stopped", "reasons": stop_override["reasons"],
                            "grid_state": stop_override["grid_state"]}
        else:
            # Certification: the loop converged on fast HESSE intervals; run ONE full bootstrap+profile
            # pass (forced) so every reported ω₀ interval is bootstrap-backed (the "one bootstrap to
            # certify" decision). Bounded — once, at the end, only when the objective is met. Then
            # re-evaluate honestly: if a q's bootstrap-certified interval crosses tolerance the summary
            # reflects that, not the optimistic fast-tier stop (the triage tool reports the certified truth).
            observations, ran_certify = _certify(cfg=cfg, decision=decision, observations=observations,
                                                 scan_kind=scan_kind)

            # Recompute the stop decision on the FINAL grid unconditionally: the in-loop decision is from
            # the top of the last iteration, so a campaign that exhausts max_iters (or breaks on budget)
            # would otherwise report a stale pre-scan verdict in the summary.
            master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
            final_decision = evaluate_stop(
                objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
            # A prospective neutron-budget refusal occurs before spend reaches the nominal amount; a
            # plain final budget evaluation therefore still says "continue". Preserve that terminal
            # decision while retaining ordinary recomputation for every measured-scan exit path.
            if not (decision is not None and decision.state == "unmeetable"
                    and any("budget_insufficient_for_minimum_batch" in reason
                            for reason in decision.reasons)):
                decision = final_decision
            if ran_certify and decision.state != "met":
                log_event(cfg.log_path, "exception",
                          {"action": "certify_regression", "state": decision.state,
                           "note": "a q's bootstrap-certified interval exceeded the fast-tier stop"})

            summary = _campaign_summary(objective, grid, decision, budget_state, cfg.workspace,
                                        cfg.log_path, len(observations))
            stop_payload = {"state": decision.state, "reasons": decision.reasons}

        (cfg.workspace / "report").mkdir(parents=True, exist_ok=True)
        (cfg.workspace / "report" / "campaign_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        # Report deferral (Stage 3): the loop ran every `isar run` with --no-render, so refresh the
        # human report once now from persisted state (incl. any certify pass). Best-effort — reads only
        # persisted files, which are final here, so it precedes the terminal `stop` event (keeping
        # `stop` the last logged decision).
        _render_campaign_report(cfg)
        _render_campaign_diagnostic(cfg)
        log_event(cfg.log_path, "stop", stop_payload)
    finally:
        cfg.runner.close()
    return summary


def _charge_budget(budget_state: dict, proposal, eta, *, n_points: int | None = None) -> None:
    points = int(proposal.n_points or 1) if n_points is None else max(int(n_points), 0)
    budget_state["neutrons"] += float(proposal.ncount) * float(points)
    if eta is not None:
        budget_state["seconds"] += float(eta)
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons"
                             else budget_state["seconds"])


def _reconcile_budget_points(budget_state: dict, ncount: int, planned: int, executed: int,
                             *, eta: float | None = None) -> tuple[float, float]:
    """Replace planned point/time cost with executed cost; return durable ledger deltas."""
    delta = float(ncount) * float(int(executed) - int(planned))
    budget_state["neutrons"] = max(0.0, float(budget_state["neutrons"]) + delta)
    seconds_delta = 0.0
    if eta is not None and int(planned) > 0:
        seconds_delta = float(eta) * (float(executed) / float(planned) - 1.0)
        budget_state["seconds"] = max(0.0, float(budget_state["seconds"]) + seconds_delta)
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons"
                             else budget_state["seconds"])
    return delta, seconds_delta


def _campaign_summary(objective, grid, decision, budget_state, workspace, log_path, n_obs,
                      *, stop_override: dict | None = None) -> dict:
    # The certified number per q is the tightest trustworthy fit (master pooled OR best single
    # production, whichever is smaller); fall back to the best quick-look estimate for an un-pooled q.
    def _omega0(g):
        return g.cert_omega0 if g.cert_omega0 is not None else g.best_omega0

    def _hw(g):
        return g.cert_halfwidth if g.cert_halfwidth is not None else g.best_halfwidth

    dispersion = []
    for g in grid:
        if _omega0(g) is None:
            continue
        m = g.master or {}
        n_members = m.get("n_members")
        members = m.get("members")
        # Chain-of-custody guard: a pooled dispersion point reported without its member provenance
        # breaks the evidence chain. Skip only when the results.csv predates the members column
        # (members_field_present is False) — the transition-tolerance case.
        if (n_members is not None and m.get("members_field_present")
                and (not members or len(members) != n_members)):
            raise RuntimeError(
                f"dispersion point {list(g.hkl)}: pooled value reported without member provenance "
                f"(evidence chain broken)")
        dispersion.append({"hkl": list(g.hkl), "q": g.q, "omega0": _omega0(g),
                           "calibrated_halfwidth": _hw(g), "n_members": n_members,
                           "members": members, "converged": g.converged})
    try:
        card = read_card_state(workspace)
        prior_points = card.get("dispersion_prior") or []
        mismatch = dict(card.get("hypothesis_mismatch") or {})
        branch_beliefs = list(card.get("branch_belief") or [])
    except Exception as error:   # noqa: BLE001
        log_event(log_path, "exception", {"event": "summary_state_read_failed",
                                           "error": str(error)})
        prior_points = []
        mismatch = {}
        branch_beliefs = []
    return {
        "objective": objective.to_dict(),
        # ``stop_override`` carries the operator_stopped terminal state (+ grid_state snapshot); the
        # default is the honest StopDecision. Additive — the dict shape is otherwise unchanged.
        "stop": stop_override if stop_override is not None else decision.to_dict(),
        "grid": [{"hkl": list(g.hkl), "q": g.q, "converged": g.converged,
                  "calibrated_halfwidth": g.cert_halfwidth, "omega0": _omega0(g),
                  "master_halfwidth": (g.master or {}).get("halfwidth"),
                  "single_prod_halfwidth": g.best_prod_halfwidth,
                  "n_members": (g.master or {}).get("n_members"),
                  "members": (g.master or {}).get("members"),
                  "n_production": g.n_production, "n_decline": g.n_decline,
                  "stuck": g.stuck} for g in grid],
        "dispersion": dispersion,
        "dispersion_prior_points": len(prior_points),
        "branch_beliefs": branch_beliefs,
        "hypothesis_mismatch": mismatch,
        "budget": dict(budget_state),
        "n_observations": n_obs,
        "session_log": str(log_path),
    }
