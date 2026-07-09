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

from pathlib import Path
from typing import Any, Callable

import json
import math

import numpy as np

from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.acquire import points_in_window, propose_scan
from isar.drive.acquisition.calibration import Calibration, load_calibration
from isar.drive.instrument.tavi_client import TaviClient
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import init_isar as _default_init_isar
from isar.drive.isar_io.runner import run_isar as _default_run_isar
from isar.drive.isar_io.scan_writer import save_scan_json
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import log_event, read_log, timed
from isar.drive.supervisor import exceptions as exc
from isar.drive.supervisor.stopping import (
    PRODUCTION_NCOUNT,
    USABLE_DISPOSITIONS as _USABLE,
    evaluate_stop,
    model_points,
    summarize_grid,
    systematic_floor,
)

DEFAULT_MAX_ITERS = 200
DEFAULT_WAIT_TIMEOUT = 600.0
_DEFAULT_CALIBRATION = (Path(__file__).resolve().parents[2] / "calibration"
                        / "phonon_dft_deterministic.json")


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


def _make_fisher_provider(card_state: dict, resolution_cb):
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
        exposure = float(PRODUCTION_NCOUNT)
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


def _validate_body(proposal, *, allow_partial: bool = False) -> dict[str, Any]:
    """The ``POST /validate`` body — ``parameters`` only (engine/seed/isolated are scan-only fields)."""
    body: dict[str, Any] = {
        "parameters": {
            "H": proposal.h, "K": proposal.k, "L": proposal.l,
            "scan_command1": proposal.scan_command(),
            "number_neutrons": int(proposal.ncount),
        },
    }
    if allow_partial:
        body["allow_partial"] = True
    return body


def _scan_body(proposal, engine: str, seed: int, *, allow_partial: bool = False) -> dict[str, Any]:
    """The ``POST /scan`` body — the validate body plus the isolated/seeded engine selection."""
    body = _validate_body(proposal, allow_partial=allow_partial)
    body.update({"engine": engine, "seed": int(seed), "isolated": True})
    return body


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


def _observation_from_row(row: dict, kind: str, ncount: int, calibration: Calibration,
                          proposal_cost: float = 0.0) -> dict:
    disp = row.get("disposition")
    verdict = row.get("verdict")
    # `flagged` (model_incomplete) still carries a usable ω₀ — for an ω₀-tolerance objective the flag
    # documents peak-shape imperfection, not an ω₀ error (see stopping.USABLE_DISPOSITIONS).
    usable = disp in _USABLE
    hw = None
    lo, hi, ok = _pick_interval(row)
    if usable and ok:
        try:
            hw = calibration.calibrated_halfwidth(lo, hi, bucket=str(ncount))
        except Exception:   # noqa: BLE001  (bad interval → treat as un-calibratable, not a crash)
            hw = None
    elif usable and kind == "quicklook":
        # Cheap-tier quicklooks carry only a HESSE omega0_err (no bootstrap interval). Good enough to
        # SEED the ω(q) acquisition model (steering only); it never reaches the stop rule, which
        # consumes the master (pooled) half-width built from a real interval.
        err, w0 = row.get("omega0_err"), row.get("omega0")
        if err is not None and w0 is not None and float(err) > 0:
            try:
                hw = calibration.calibrated_halfwidth(float(w0) - float(err), float(w0) + float(err),
                                                      bucket=str(ncount))
            except Exception:   # noqa: BLE001
                hw = None
    return {
        "hkl": _row_hkl(row), "kind": kind, "calibrated_halfwidth": hw,
        "resolved": usable, "disposition": disp, "verdict": verdict,
        "omega0": row.get("omega0"), "scan": row.get("scan"), "detector": row.get("detector"),
        "proposal_cost": float(proposal_cost), "fit_kind": row.get("fit_kind"),
        "branch_id": row.get("branch_id"),   # Phase A: only acoustic (or null) rows steer ω(q)
    }


def _master_obs_from_row(row: dict, calibration: Calibration) -> dict:
    """A master (pooled) fit → the convergence observation for its q. The calibrated half-width uses
    the reliable interval (profile when the bootstrap is Γ-at-bound) at the production count bucket."""
    lo, hi, ok = _pick_interval(row)
    hw = None
    if ok:
        try:
            hw = calibration.calibrated_halfwidth(lo, hi, bucket=str(PRODUCTION_NCOUNT))
        except Exception:   # noqa: BLE001
            hw = None
    disp = row.get("disposition")
    return {
        "hkl": _row_hkl(row), "omega0": row.get("omega0"), "halfwidth": hw,
        "reliable": bool(ok), "usable": (disp in _USABLE and row.get("omega0") is not None),
        "disposition": disp, "n_members": row.get("n_members"),
    }


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


def _errors_arg_for(proposal, workspace) -> list[str]:
    """The ``isar run --errors`` tier for this proposal (LLM_HARNESS_DESIGN §8 escalation ladder):

    - quicklook → ``cheap`` (HESSE only, no interval — steers the ω(q) model);
    - production → ``fast`` (HESSE interval + trust flag, no bootstrap) by default, escalating to
      ``full`` (bootstrap+profile) once the q shows an untrusted-HESSE fit that needs the reliable
      interval. The bootstrap thus fires only where the fast HESSE approximation cannot be trusted.
    """
    if proposal.kind == "quicklook":
        return ["--errors", "cheap"]
    if _q_needs_full_errors(workspace, (proposal.h, proposal.k, proposal.l)):
        return ["--errors", "full"]
    return ["--errors", "fast"]


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
        kind, ncount = scan_kind.get((r.get("scan"), r.get("detector")),
                                     ("production", PRODUCTION_NCOUNT))
        out.append(_observation_from_row(r, kind, ncount, calibration))
    return out


def _resume_state(workspace, log_path, calibration: Calibration) -> dict:
    """Rehydrate the loop's in-memory state from the durable files after a crash/restart.

    Convergence needs no resume (the pooled master rows are read fresh every iteration); this recovers
    what ``run_campaign`` would otherwise reset to zero — the **spend ledger** (``scan_counter`` +
    ``budget`` neutrons/seconds, exact from the ``run`` events, so a restart neither re-spends beamtime
    nor reuses a seed / collides an idempotency key), the **folded-row set** (``consumed`` = every
    ``(scan, detector)`` already in ``results.csv``, so old rows are never re-folded as new), and
    **best-effort per-scan observations** (kind/ncount by hkl from the run ledger; each q's total cost
    attributed to one representative row so ``counts_spent`` sums to the true per-q spend). A cold
    workspace (no log / no results.csv) returns the fresh-start tuple."""
    runs = [e.get("payload", {}) for e in read_log(log_path) if e.get("kind") == "run"]
    scan_counter = len(runs)                        # one run event per submitted scan
    neutrons = seconds = 0.0
    cost_by_hkl: dict = {}
    kind_by_hkl: dict = {}
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
        key = tuple(round(float(x), 4) for x in hkl)
        cost_by_hkl[key] = cost_by_hkl.get(key, 0.0) + cost
        k = r.get("kind")
        if k in ("production", "confirm"):
            kind_by_hkl[key] = "production"
        elif key not in kind_by_hkl:
            kind_by_hkl[key] = k or "production"
        ncount_by_hkl[key] = r.get("ncount") or ncount_by_hkl.get(key)

    observations: list[dict] = []
    consumed: set = set()
    scan_kind: dict = {}
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        rows = []
    stamped: set = set()
    for row in rows:
        if row.get("pooled") or _row_hkl(row) is None:
            continue
        key = tuple(round(float(x), 4) for x in _row_hkl(row))
        kind = kind_by_hkl.get(key, "production")
        ncount = int(ncount_by_hkl.get(key) or PRODUCTION_NCOUNT)
        pc = 0.0
        if key not in stamped:                      # attribute the q's whole spend to one row
            pc = cost_by_hkl.get(key, 0.0)
            stamped.add(key)
        observations.append(_observation_from_row(row, kind, ncount, calibration, proposal_cost=pc))
        sd = (row.get("scan"), row.get("detector"))
        consumed.add(sd)
        scan_kind[sd] = (kind, ncount)

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


def run_campaign(objective: ObjectiveSpec, config: dict | None = None) -> dict[str, Any]:
    """Run an adaptive measurement campaign to satisfy ``objective`` (LLM_HARNESS_DESIGN §8).

    Returns a campaign summary dict (see the bottom of this function). ``config`` keys:

    - ``workspace`` (required): the workspace directory. If it has no ``card.policy.toml`` the
      driver runs ``isar init`` itself after the first scan lands (init needs a data file).
    - ``client``: a TAVI client (default :class:`TaviClient` at ``tavi_url``).
    - ``tavi_url``: base URL for the default client.
    - ``run_isar``: callable ``(workspace, data_paths) -> Any`` (default the ``isar run`` subprocess).
    - ``resolution``: a float / dict / callable energy-resolution source (default wraps
      ``GET /resolution``).
    - ``calibration``: a :class:`Calibration`, a path, or ``None`` (default the repo artifact).
    - ``seed``: base seed; scan ``n`` uses ``seed + n``.
    - ``max_iters``, ``wait_timeout``, ``floor`` (systematic-floor override), ``session_log`` (path).
    """
    config = dict(config or {})
    workspace = Path(config["workspace"])
    data_dir = Path(config.get("data_dir", workspace / "data"))
    log_path = Path(config.get("session_log", workspace / "report" / "session_log.jsonl"))
    calibration = _resolve_calibration(config.get("calibration"))
    client = config.get("client")
    if client is None:
        client = TaviClient(config["tavi_url"]) if config.get("tavi_url") else TaviClient()
    run_isar = config.get("run_isar", _default_run_isar)
    init_isar = config.get("init_isar", _default_init_isar)
    resolution_cb = _make_resolution_cb(client, config.get("resolution"))
    engine = objective.tier
    base_seed = int(config.get("seed", 0))
    max_iters = int(config.get("max_iters", DEFAULT_MAX_ITERS))
    wait_timeout = float(config.get("wait_timeout", DEFAULT_WAIT_TIMEOUT))
    floor = systematic_floor(calibration, config.get("floor"))

    # Crash-safe resume: recover the spend ledger + folded rows + observations from the durable files
    # (session_log + results.csv). A cold workspace yields the fresh-start values.
    resumed = _resume_state(workspace, log_path, calibration)
    budget = dict(objective.budget)
    budget_state = {"kind": budget["kind"], "amount": budget["amount"],
                    "neutrons": resumed["neutrons"], "seconds": resumed["seconds"], "spent": 0.0}
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons"
                             else budget_state["seconds"])

    observations: list[dict] = resumed["observations"]
    consumed: set = resumed["consumed"]
    scan_kind: dict = resumed["scan_kind"]   # (scan, detector) -> (kind, ncount), to rebuild obs after certify
    scan_counter = resumed["scan_counter"]

    log_event(log_path, "decide", {"event": "campaign_start", "objective": objective.to_dict(),
                                   "calibration": {"inflation": calibration.inflation,
                                                   "floor_meV": floor},
                                   "resumed": {"scan_counter": scan_counter,
                                               "n_observations": len(observations),
                                               "spent": budget_state["spent"]}})

    decision = None
    # Instrumentation only (no logic change): each stage runs inside a ``timed`` span that appends a
    # "timing" event (resume-inert — _resume_state reads only "run" events), and every in-loop
    # log_event carries the iteration index ``it``. The outer ``timed(... "iteration")`` covers the
    # whole body including all break paths (a break exits the span normally, ok=True).
    for it in range(max_iters):
        with timed(log_path, "iteration", iteration=it):
            with timed(log_path, "stop_eval", iteration=it):
                master_rows = _read_master_rows(workspace, calibration)
                grid = summarize_grid(objective, observations, master_rows)
                decision = evaluate_stop(objective, grid, budget_state, calibration=calibration, floor=floor)
            if decision.state != "continue":
                break

            with timed(log_path, "model_fit", iteration=it):
                model = acq_model.fit(model_points(grid))
            # 5b Fisher tier: read the branch the session persisted into card.state.json FRESH each
            # iteration (durable / crash-safe — a file, never a live session object). No branch yet →
            # provider is None → propose_scan falls back to the 5a counts tier.
            with timed(log_path, "fisher_provider", iteration=it):
                fisher = _make_fisher_provider(read_card_state(workspace), resolution_cb)
            with timed(log_path, "propose", iteration=it):
                proposal = propose_scan(objective, model, observations, resolution_cb, budget_state,
                                        master_rows=master_rows, fisher=fisher)
            if proposal is None:
                decision = evaluate_stop(objective, grid, budget_state, calibration=calibration, floor=floor)
                break
            log_event(log_path, "propose", {**proposal.to_dict(), "iter": it})

            b_act = exc.handle_budget(budget_state)
            if b_act is not None:
                log_event(log_path, "exception", {**b_act.to_dict(), "iter": it})
                if b_act.action == "exhausted":
                    break

            # ---- run this proposal, with bounded missed-window re-centering (a LOOK, §1) ----------
            allow_partial = False
            attempt = 0
            rows: list[dict] = []
            while True:
                with timed(log_path, "validate", iteration=it, attempt=attempt):
                    val = client.validate(_validate_body(proposal, allow_partial=allow_partial))
                log_event(log_path, "validate", {"would_queue": val.get("would_queue"),
                                                  "blockers": val.get("blockers"), "eta": val.get("eta"),
                                                  "iter": it})
                if not val.get("would_queue", False):
                    v_act = exc.handle_validation_rejection(val.get("blockers") or [])
                    log_event(log_path, "exception", {**v_act.to_dict(), "iter": it})
                    if v_act.action == "repair" and not allow_partial:
                        allow_partial = True
                        continue
                    observations.append({"hkl": (proposal.h, proposal.k, proposal.l),
                                         "kind": proposal.kind, "calibrated_halfwidth": None,
                                         "resolved": False, "disposition": "declined",
                                         "verdict": "validation_rejected", "omega0": None})
                    break

                with timed(log_path, "submit", iteration=it, attempt=attempt) as _m:
                    body = _scan_body(proposal, engine, base_seed + scan_counter, allow_partial=allow_partial)
                    submit = client.submit_scan(body, idempotency_key=f"{objective.name or 'campaign'}-{scan_counter}")
                    scan_counter += 1
                    job_id = submit.get("job_id")
                    _m["job_id"] = job_id
                eta = (submit.get("eta") or {}).get("estimated_seconds")
                _charge_budget(budget_state, proposal, eta)
                # The run event doubles as the durable SPEND LEDGER for crash resume: kind/ncount/n_points/
                # hkl + the charged cost (= n_points×ncount) let a restart recover budget + scan_counter.
                log_event(log_path, "run", {"job_id": job_id, "state": submit.get("state"),
                                            "eta": submit.get("eta"), "attempt": attempt,
                                            "kind": proposal.kind, "ncount": int(proposal.ncount),
                                            "n_points": int(proposal.n_points or 0),
                                            "hkl": [proposal.h, proposal.k, proposal.l],
                                            "cost": float(proposal.n_points or 1) * float(proposal.ncount),
                                            "iter": it})

                with timed(log_path, "wait_scan", iteration=it, job_id=job_id) as _m:
                    snap = client.wait_scan(job_id, timeout=wait_timeout)
                    _m["state"] = snap.get("state")
                if snap.get("state") != "done":
                    log_event(log_path, "exception", {"action": "scan_not_done", "job_id": job_id,
                                                      "state": snap.get("state"), "error": snap.get("error"),
                                                      "iter": it})
                    observations.append({"hkl": (proposal.h, proposal.k, proposal.l),
                                         "kind": proposal.kind, "calibrated_halfwidth": None,
                                         "resolved": False, "disposition": "declined",
                                         "verdict": str(snap.get("state")), "omega0": None})
                    break

                with timed(log_path, "fetch_save", iteration=it):
                    data = client.get_scan_data(job_id)
                    scan_path = save_scan_json(data, data_dir)
                # Lazy `isar init`: the instrument card can only be scaffolded from a recognizable
                # data file, which first exists NOW. Subprocess keeps the analysis boundary.
                if not (workspace / "card.policy.toml").exists():
                    with timed(log_path, "isar_init", iteration=it):
                        init_isar(workspace, scan_path)
                    log_event(log_path, "decide", {"event": "workspace_init", "probe": str(scan_path),
                                                   "iter": it})
                # Error tier per the escalation ladder: quicklook → cheap (steer only), production →
                # fast (HESSE interval + trust flag), escalating to full (bootstrap+profile) only where an
                # untrusted-HESSE q needs the reliable interval. The bootstrap fires only where needed.
                errors_arg = _errors_arg_for(proposal, workspace)
                with timed(log_path, "isar_run", iteration=it, errors_tier=errors_arg[-1], attempt=attempt):
                    run_isar(workspace, [data_dir], extra_args=errors_arg)

                with timed(log_path, "fold_rows", iteration=it) as _m:
                    rows = _new_rows(workspace, consumed)
                    # The scan's actual monitor cost (n_points reflects any recenter; charged once per scan,
                    # onto the first detector row so counts_spent per q is not multiplied by detector count).
                    scan_cost = float(proposal.n_points or 1) * float(proposal.ncount)
                    for j, r in enumerate(rows):
                        observations.append(_observation_from_row(
                            r, proposal.kind, proposal.ncount, calibration,
                            proposal_cost=scan_cost if j == 0 else 0.0))
                        scan_kind[(r.get("scan"), r.get("detector"))] = (proposal.kind, proposal.ncount)
                    _m["n_rows"] = len(rows)
                # Embed the subprocess stage-timing snapshot (best-effort) + the folded rows' outcomes.
                timings = _read_run_timings(workspace)
                analyze_payload = {"job_id": job_id, "errors_tier": errors_arg[-1], "iter": it,
                                   "n_rows": len(rows),
                                   "dispositions": [r.get("disposition") or r.get("verdict") for r in rows]}
                if timings is not None:
                    analyze_payload["timings"] = timings
                log_event(log_path, "analyze", analyze_payload)

                if not _rows_missed(rows):
                    break
                # Missed window: LOOK at the counts to re-center + widen, up to the attempt cap.
                with timed(log_path, "missed_window", iteration=it):
                    result = data.get("result") or {}
                    act = exc.handle_missed_window(
                        result.get("counts") or [], result.get("scan_values_1") or [],
                        e_lo=proposal.e_lo, e_hi=proposal.e_hi, attempt=attempt)
                log_event(log_path, "exception", {**act.to_dict(), "iter": it})
                if act.action != "recenter":
                    break
                proposal.e_lo = act.params["e_lo"]
                proposal.e_hi = act.params["e_hi"]
                proposal.n_points = points_in_window(proposal.e_lo, proposal.e_hi, proposal.e_step)
                attempt += 1

            # ---- exception branch on the final verdict (flagged / held) ----------------------------
            with timed(log_path, "verdict_exceptions", iteration=it):
                for r in rows:
                    disp = r.get("disposition")
                    status = r.get("status")
                    if status in exc.HELD_STATUSES:
                        log_event(log_path, "exception", {**exc.handle_held(status).to_dict(), "iter": it})
                    elif disp not in (None, "resolved") and disp not in exc.MISSED_VERDICTS:
                        log_event(log_path, "exception",
                                  {**exc.handle_flagged(disp, verdict=r.get("verdict")).to_dict(), "iter": it})

            with timed(log_path, "model_update", iteration=it):
                master_rows = _read_master_rows(workspace, calibration)
                grid = summarize_grid(objective, observations, master_rows)
            log_event(log_path, "decide", {"event": "model_update",
                                           "n_observations": len(observations),
                                           "converged": sum(1 for g in grid if g.converged),
                                           "n_grid": len(grid), "iter": it})

    # Certification: the loop converged on fast HESSE intervals; run ONE full bootstrap+profile pass
    # (forced) so every reported ω₀ interval is bootstrap-backed (the "one bootstrap to certify"
    # decision). Bounded — once, at the end, only when the objective is met. Then re-evaluate honestly:
    # if a q's bootstrap-certified interval crosses tolerance the summary reflects that, not the
    # optimistic fast-tier stop (the triage tool reports the certified truth).
    ran_certify = decision is not None and decision.state == "met"
    if ran_certify:
        log_event(log_path, "decide", {"event": "certify_start", "tier": "full"})
        try:
            with timed(log_path, "certify", iteration=None):
                run_isar(workspace, [data_dir], extra_args=["--errors", "full", "--force"])
            observations = _recertified_observations(workspace, calibration, scan_kind)
            # Embed the certification subprocess timings (iter=None — the certify pass is reported
            # separately, never averaged into per-iteration stats).
            timings = _read_run_timings(workspace)
            analyze_payload = {"job_id": None, "errors_tier": "full", "iter": None,
                               "n_rows": len(observations),
                               "dispositions": [o.get("disposition") or o.get("verdict")
                                                for o in observations]}
            if timings is not None:
                analyze_payload["timings"] = timings
            log_event(log_path, "analyze", analyze_payload)
            log_event(log_path, "decide", {"event": "certify_done"})
        except Exception as e:   # noqa: BLE001 — never lose a met campaign to a certification hiccup
            log_event(log_path, "exception", {"action": "certify_failed", "error": str(e)})
            ran_certify = False

    master_rows = _read_master_rows(workspace, calibration)
    grid = summarize_grid(objective, observations, master_rows)
    if decision is None or ran_certify:
        decision = evaluate_stop(objective, grid, budget_state, calibration=calibration, floor=floor)
        if ran_certify and decision.state != "met":
            log_event(log_path, "exception",
                      {"action": "certify_regression", "state": decision.state,
                       "note": "a q's bootstrap-certified interval exceeded the fast-tier stop"})

    summary = _campaign_summary(objective, grid, decision, budget_state, workspace, log_path,
                                len(observations))
    log_event(log_path, "stop", {"state": decision.state, "reasons": decision.reasons})
    (workspace / "report").mkdir(parents=True, exist_ok=True)
    (workspace / "report" / "campaign_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _charge_budget(budget_state: dict, proposal, eta) -> None:
    budget_state["neutrons"] += float(proposal.ncount) * float(proposal.n_points or 1)
    if eta is not None:
        budget_state["seconds"] += float(eta)
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons"
                             else budget_state["seconds"])


def _campaign_summary(objective, grid, decision, budget_state, workspace, log_path, n_obs) -> dict:
    # The certified number per q is the tightest trustworthy fit (master pooled OR best single
    # production, whichever is smaller); fall back to the best quick-look estimate for an un-pooled q.
    def _omega0(g):
        return g.cert_omega0 if g.cert_omega0 is not None else g.best_omega0

    def _hw(g):
        return g.cert_halfwidth if g.cert_halfwidth is not None else g.best_halfwidth

    dispersion = [{"hkl": list(g.hkl), "q": g.q, "omega0": _omega0(g),
                   "calibrated_halfwidth": _hw(g), "n_members": (g.master or {}).get("n_members"),
                   "converged": g.converged}
                  for g in grid if _omega0(g) is not None]
    try:
        card = read_card_state(workspace)
        prior_points = card.get("dispersion_prior") or []
    except Exception:   # noqa: BLE001
        prior_points = []
    return {
        "objective": objective.to_dict(),
        "stop": decision.to_dict(),
        "grid": [{"hkl": list(g.hkl), "q": g.q, "converged": g.converged,
                  "calibrated_halfwidth": g.cert_halfwidth, "omega0": _omega0(g),
                  "master_halfwidth": (g.master or {}).get("halfwidth"),
                  "single_prod_halfwidth": g.best_prod_halfwidth,
                  "n_members": (g.master or {}).get("n_members"),
                  "n_production": g.n_production, "n_decline": g.n_decline,
                  "stuck": g.stuck} for g in grid],
        "dispersion": dispersion,
        "dispersion_prior_points": len(prior_points),
        "budget": dict(budget_state),
        "n_observations": n_obs,
        "session_log": str(log_path),
    }
