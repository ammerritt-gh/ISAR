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

from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.acquire import propose_scan
from isar.drive.acquisition.calibration import Calibration, load_calibration
from isar.drive.instrument.tavi_client import TaviClient
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import run_isar as _default_run_isar
from isar.drive.isar_io.scan_writer import save_scan_json
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import log_event
from isar.drive.supervisor import exceptions as exc
from isar.drive.supervisor.stopping import (
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


def _observation_from_row(row: dict, proposal, calibration: Calibration) -> dict:
    disp = row.get("disposition")
    verdict = row.get("verdict")
    resolved = disp == "resolved"
    hw = None
    lo, hi = row.get("interval_lo"), row.get("interval_hi")
    if resolved and lo is not None and hi is not None:
        try:
            hw = calibration.calibrated_halfwidth(lo, hi, bucket=str(proposal.ncount))
        except Exception:   # noqa: BLE001  (bad interval → treat as un-calibratable, not a crash)
            hw = None
    return {
        "hkl": _row_hkl(row), "kind": proposal.kind, "calibrated_halfwidth": hw,
        "resolved": resolved, "disposition": disp, "verdict": verdict,
        "omega0": row.get("omega0"), "scan": row.get("scan"), "detector": row.get("detector"),
    }


def _new_rows(workspace, consumed: set) -> list[dict]:
    """Rows in results.csv not yet consumed, keyed by (scan, detector); skips status-only rows."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        key = (r.get("scan"), r.get("detector"))
        if key in consumed or _row_hkl(r) is None:
            continue
        consumed.add(key)
        out.append(r)
    return out


def _rows_missed(rows: list[dict]) -> bool:
    """True when a scan produced no resolved detector (peak outside the window / no signal)."""
    if not rows:
        return True
    return not any(r.get("disposition") == "resolved" for r in rows)


def run_campaign(objective: ObjectiveSpec, config: dict | None = None) -> dict[str, Any]:
    """Run an adaptive measurement campaign to satisfy ``objective`` (LLM_HARNESS_DESIGN §8).

    Returns a campaign summary dict (see the bottom of this function). ``config`` keys:

    - ``workspace`` (required): an ``isar init``-ed workspace directory.
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
    resolution_cb = _make_resolution_cb(client, config.get("resolution"))
    engine = objective.tier
    base_seed = int(config.get("seed", 0))
    max_iters = int(config.get("max_iters", DEFAULT_MAX_ITERS))
    wait_timeout = float(config.get("wait_timeout", DEFAULT_WAIT_TIMEOUT))
    floor = systematic_floor(calibration, config.get("floor"))

    budget = dict(objective.budget)
    budget_state = {"kind": budget["kind"], "amount": budget["amount"], "spent": 0.0,
                    "neutrons": 0.0, "seconds": 0.0}

    observations: list[dict] = []
    consumed: set = set()
    scan_counter = 0

    log_event(log_path, "decide", {"event": "campaign_start", "objective": objective.to_dict(),
                                   "calibration": {"inflation": calibration.inflation,
                                                   "floor_meV": floor}})

    decision = None
    for _ in range(max_iters):
        grid = summarize_grid(objective, observations)
        decision = evaluate_stop(objective, grid, budget_state, calibration=calibration, floor=floor)
        if decision.state != "continue":
            break

        model = acq_model.fit(model_points(grid))
        proposal = propose_scan(objective, model, observations, resolution_cb, budget_state)
        if proposal is None:
            decision = evaluate_stop(objective, grid, budget_state, calibration=calibration, floor=floor)
            break
        log_event(log_path, "propose", proposal.to_dict())

        b_act = exc.handle_budget(budget_state)
        if b_act is not None:
            log_event(log_path, "exception", b_act.to_dict())
            if b_act.action == "exhausted":
                break

        # ---- run this proposal, with bounded missed-window re-centering (a LOOK, §1) ----------
        allow_partial = False
        attempt = 0
        rows: list[dict] = []
        while True:
            val = client.validate(_validate_body(proposal, allow_partial=allow_partial))
            log_event(log_path, "validate", {"would_queue": val.get("would_queue"),
                                              "blockers": val.get("blockers"), "eta": val.get("eta")})
            if not val.get("would_queue", False):
                v_act = exc.handle_validation_rejection(val.get("blockers") or [])
                log_event(log_path, "exception", v_act.to_dict())
                if v_act.action == "repair" and not allow_partial:
                    allow_partial = True
                    continue
                observations.append({"hkl": (proposal.h, proposal.k, proposal.l),
                                     "kind": proposal.kind, "calibrated_halfwidth": None,
                                     "resolved": False, "disposition": "declined",
                                     "verdict": "validation_rejected", "omega0": None})
                break

            body = _scan_body(proposal, engine, base_seed + scan_counter, allow_partial=allow_partial)
            submit = client.submit_scan(body, idempotency_key=f"{objective.name or 'campaign'}-{scan_counter}")
            scan_counter += 1
            job_id = submit.get("job_id")
            eta = (submit.get("eta") or {}).get("estimated_seconds")
            _charge_budget(budget_state, proposal, eta)
            log_event(log_path, "run", {"job_id": job_id, "state": submit.get("state"),
                                        "eta": submit.get("eta"), "attempt": attempt})

            snap = client.wait_scan(job_id, timeout=wait_timeout)
            if snap.get("state") != "done":
                log_event(log_path, "exception", {"action": "scan_not_done", "job_id": job_id,
                                                  "state": snap.get("state"), "error": snap.get("error")})
                observations.append({"hkl": (proposal.h, proposal.k, proposal.l),
                                     "kind": proposal.kind, "calibrated_halfwidth": None,
                                     "resolved": False, "disposition": "declined",
                                     "verdict": str(snap.get("state")), "omega0": None})
                break

            data = client.get_scan_data(job_id)
            save_scan_json(data, data_dir)
            # Quick-look scans need only a verdict + rough ω₀ to steer acquisition — fit them
            # cheap (HESSE, no bootstrap/profile). Production/confirm scans, whose interval the
            # stop rule consumes, get the full uncertainty layer.
            errors_arg = ["--errors", "cheap"] if proposal.kind == "quicklook" else None
            run_isar(workspace, [data_dir], extra_args=errors_arg)
            log_event(log_path, "analyze", {"job_id": job_id})

            rows = _new_rows(workspace, consumed)
            for r in rows:
                observations.append(_observation_from_row(r, proposal, calibration))

            if not _rows_missed(rows):
                break
            # Missed window: LOOK at the counts to re-center + widen, up to the attempt cap.
            result = data.get("result") or {}
            act = exc.handle_missed_window(
                result.get("counts") or [], result.get("scan_values_1") or [],
                e_lo=proposal.e_lo, e_hi=proposal.e_hi, attempt=attempt)
            log_event(log_path, "exception", act.to_dict())
            if act.action != "recenter":
                break
            proposal.e_lo = act.params["e_lo"]
            proposal.e_hi = act.params["e_hi"]
            attempt += 1

        # ---- exception branch on the final verdict (flagged / held) ----------------------------
        for r in rows:
            disp = r.get("disposition")
            status = r.get("status")
            if status in exc.HELD_STATUSES:
                log_event(log_path, "exception", exc.handle_held(status).to_dict())
            elif disp not in (None, "resolved") and disp not in exc.MISSED_VERDICTS:
                log_event(log_path, "exception",
                          exc.handle_flagged(disp, verdict=r.get("verdict")).to_dict())

        grid = summarize_grid(objective, observations)
        log_event(log_path, "decide", {"event": "model_update",
                                       "n_observations": len(observations),
                                       "converged": sum(1 for g in grid if g.converged),
                                       "n_grid": len(grid)})

    if decision is None:
        grid = summarize_grid(objective, observations)
        decision = evaluate_stop(objective, grid, budget_state, calibration=calibration, floor=floor)

    grid = summarize_grid(objective, observations)
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
    dispersion = [{"hkl": list(g.hkl), "q": g.q, "omega0": g.best_omega0,
                   "calibrated_halfwidth": g.best_halfwidth, "converged": g.converged}
                  for g in grid if g.best_resolved]
    try:
        card = read_card_state(workspace)
        prior_points = card.get("dispersion_prior") or []
    except Exception:   # noqa: BLE001
        prior_points = []
    return {
        "objective": objective.to_dict(),
        "stop": decision.to_dict(),
        "grid": [{"hkl": list(g.hkl), "q": g.q, "converged": g.converged,
                  "calibrated_halfwidth": g.best_halfwidth, "omega0": g.best_omega0,
                  "resolved": g.best_resolved, "n_decline": g.n_decline,
                  "n_production_fits": len(g.prod_fits)} for g in grid],
        "dispersion": dispersion,
        "dispersion_prior_points": len(prior_points),
        "budget": dict(budget_state),
        "n_observations": n_obs,
        "session_log": str(log_path),
    }
