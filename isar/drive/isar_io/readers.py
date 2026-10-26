"""Read ISAR's file contract back (WORKING — LLM_HARNESS_DESIGN §4).

ISAR writes, once per ``isar run``, a stable set of files the driver consumes. These readers parse
them with the stdlib only and never import ISAR internals:

- :func:`read_results_csv` — ``report/results.csv`` (one row per scan×detector). Numeric columns the
  acquisition engine and stopping criterion need (``omega0``, ``interval_lo``/``interval_hi``,
  ``confidence``, ``omega0_err``, the Q components) are coerced to floats; everything else stays a
  string. The columns are ISAR's ``session.outcome._CSV_COLUMNS``.
- :func:`read_modes_csv` — ``report/modes.csv``, the linked mode-per-row table (same coercion).
- :func:`read_report_json` — ``report/report.json`` (a light run summary, cheap to poll for progress).
- :func:`read_card_state` — ``card.state.json`` (the accumulated ``dispersion_prior`` +
  ``resolution_versions``; the running physics model the ω(q) model mirrors).

**Guard rail (LLM_HARNESS_DESIGN §4 WARNING).** ``prior_sigma`` is read as an opaque value only; it
is NOT a measured uncertainty (it floors at 0.3 meV by design). The convergence test consumes
``interval_lo``/``interval_hi``, never ``prior_sigma`` — enforced in
:mod:`isar.drive.acquisition.calibration`.
"""
from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from isar.drive.session_log import log_event, read_log
from isar.predictive import PredictiveArtifactError, validate_master_predictive_artifact

# Columns coerced to float when present and non-empty. Everything else stays a string (or None).
_NUMERIC_COLUMNS = frozenset({
    "T", "h", "k", "l", "confidence", "omega0", "omega0_err",
    "interval_lo", "interval_hi", "omega0_profile_lo", "omega0_profile_hi",
    "hesse_lo", "hesse_hi",
    "bayes_lo", "bayes_med", "bayes_hi", "bayes_ess", "bayes_freq_ratio", "hesse_freq_ratio",
    "log_evidence", "log_evidence_err",
    "gamma", "cost_per_dof",
    "zero", "gauss_fwhm", "lorentz_fwhm", "prior_omega", "prior_sigma",
    "effective_exposure", "consistency_statistic", "consistency_pvalue",
    "consistency_alpha", "model_interval_lo", "model_interval_med", "model_interval_hi",
    "model_entropy",
    "target_probability", "identity_entropy",
    "best_model_posterior",
    "energy_lo", "energy_hi",
    "count_probability", "deep_p_best",
    # report/modes.csv (read_modes_csv)
    "peak_position", "peak_position_err", "gamma_err", "amplitude", "amplitude_intrinsic",
    "height", "height_err", "area_stokes", "area_stokes_err", "corr_omega0_gamma",
    "deletion_impact", "Q_nm",
})
_INT_COLUMNS = frozenset({"scan", "prior_n", "n_modes", "n_members",
                          "selected_candidate_index", "track_revision", "consistency_seed",
                          "n_confident", "n_tentative", "mode_index", "deep_best_count"})
# Coerced to bool ("True"/"False" → True/False; "" → None). ISAR writes Python bool reprs.
_BOOL_COLUMNS = frozenset({"interval_reliable", "pooled", "hesse_trusted", "resolvable",
                           "gamma_at_bound", "pool_consistent", "model_robust",
                           "local_identity_clear", "target_delivery_eligible",
                           "probabilities_conditional", "numerically_marginal",
                           "deep_pass_candidate", "confident", "delta", "manual_fit",
                           "deep_numerically_marginal"})
# Split on ";" into a list of labels ("" → None; missing column → key absent via DictReader).
_LIST_COLUMNS = frozenset({"members"})
# JSON-encoded structured columns (GUI Phase 0): the amplitudes dict and candidate table. Parsed
# with json.loads; "" / None → None; a malformed cell is left as the raw string (never raises).
_JSON_COLUMNS = frozenset({"amplitudes", "candidates", "model_manifest",
                           "exposure_provenance", "branch_posterior",
                           "branch_candidates", "fit_selection_semantics",
                           "interval_semantics", "probability_semantics",
                           "shadow_model_selection", "energy_bands", "count_probabilities"})


def _report_dir(workspace: str | Path) -> Path:
    return Path(workspace) / "report"


def _coerce(column: str, value: str | None):
    if value is None or value == "":
        return None
    if column in _JSON_COLUMNS:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    if column in _LIST_COLUMNS:
        return [s for s in value.split(";") if s]
    if column in _BOOL_COLUMNS:
        s = str(value).strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
        return value
    if column in _INT_COLUMNS:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return value
    if column in _NUMERIC_COLUMNS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return value


def read_results_csv(workspace: str | Path) -> list[dict[str, Any]]:
    """Parse ``<workspace>/report/results.csv`` into a list of row dicts with numeric coercion.

    Raises :class:`FileNotFoundError` if the run has not produced a ``results.csv`` yet.
    """
    path = _report_dir(workspace) / "results.csv"
    if not path.exists():
        raise FileNotFoundError(f"no results.csv at {path} (run ISAR first)")
    rows: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            rows.append({k: _coerce(k, v) for k, v in raw.items()})
    return rows


def read_modes_csv(workspace: str | Path) -> list[dict[str, Any]]:
    """Parse ``<workspace>/report/modes.csv``, results.csv's linked mode-per-row table (one row per
    fitted mode of each spectrum's selected count, ordered by energy), with the same coercion.

    Raises :class:`FileNotFoundError` if the run has not produced it yet.
    """
    path = _report_dir(workspace) / "modes.csv"
    if not path.exists():
        raise FileNotFoundError(f"no modes.csv at {path} (run ISAR first)")
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: _coerce(k, v) for k, v in raw.items()} for raw in csv.DictReader(f)]


def read_report_json(workspace: str | Path) -> dict[str, Any]:
    """Parse ``<workspace>/report/report.json`` (the light run summary)."""
    path = _report_dir(workspace) / "report.json"
    if not path.exists():
        raise FileNotFoundError(f"no report.json at {path} (run ISAR first)")
    return json.loads(path.read_text(encoding="utf-8"))


def read_card_state(workspace: str | Path) -> dict[str, Any]:
    """Parse ``<workspace>/card.state.json`` — the accumulated experiment state.

    Returns the raw dict; ``dispersion_prior`` is a list of PriorPoint dicts and ``resolution_versions``
    a list of version dicts. A missing or empty (``{}``) state file yields ``{}`` — a fresh workspace
    is not an error.
    """
    path = Path(workspace) / "card.state.json"
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def dispersion_prior(workspace: str | Path) -> list[dict[str, Any]]:
    """The ``dispersion_prior`` list from ``card.state.json`` (empty on a fresh workspace)."""
    return list(read_card_state(workspace).get("dispersion_prior") or [])


def read_run_timings(workspace) -> dict | None:
    """Best-effort read of the subprocess stage-timing file ``isar run`` drops per invocation
    (``workspace/report/run_timings.json``). Returns ``None`` when the file is absent or unparseable —
    profiling is advisory and must never fail (or alter) the campaign. Read right after ``run_isar``
    returns (the subprocess has exited), so it snapshots that invocation's own timings."""
    p = Path(workspace) / "report" / "run_timings.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001  (missing / malformed timing file → no embedded timings, never a crash)
        return None


def exact_pooled_master_sources(workspace) -> dict[tuple[str, str, str], dict[str, str]]:
    """Join pooled revisions to their originating physical request by durable identities only.

    ``results.csv`` deliberately identifies a pooled scientific result with ``pooled:<hash>``.  That
    is not the acquisition whose executed scan-design inventory supplied the data.  The session log
    records the missing join on ``pooled_master_update``: exact master key/detector/revision plus the
    physical acquisition and submitted request fingerprint.  Ambiguous, incomplete, or orphaned
    joins are omitted so callers fail closed instead of substituting a same-Q or newer master.
    """
    # ponytail: ``read_log`` parses only what was appended, but this still walks every record twice
    # on every call -- O(events), not O(new events). Measured at 6 ms per call for a 3000-event log
    # against a 6 s fit, so it is noise; make the join incremental too if a campaign ever runs long
    # enough for it not to be.
    events = read_log(Path(workspace) / "report" / "session_log.jsonl")
    submitted: set[tuple[str, str]] = set()
    for event in events:
        if event.get("kind") != "physical_submission":
            continue
        payload = event.get("payload") or {}
        acquisition_id = payload.get("acquisition_id")
        request_fingerprint = payload.get("proposal_fingerprint")
        if acquisition_id is not None and request_fingerprint is not None:
            submitted.add((str(acquisition_id), str(request_fingerprint)))

    candidates: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
    for event in events:
        if event.get("kind") != "pooled_master_update":
            continue
        payload = event.get("payload") or {}
        identity = (
            payload.get("master_session_key"),
            payload.get("detector_id"),
            payload.get("master_revision"),
        )
        source = (payload.get("acquisition_id"), payload.get("proposal_fingerprint"))
        if any(value is None for value in identity) or any(value is None for value in source):
            continue
        exact_identity = tuple(str(value) for value in identity)
        exact_source = tuple(str(value) for value in source)
        if exact_source in submitted:
            candidates.setdefault(exact_identity, set()).add(exact_source)

    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for identity, sources in candidates.items():
        if len(sources) != 1:
            continue
        acquisition_id, request_fingerprint = next(iter(sources))
        result[identity] = {
            "source_acquisition_id": acquisition_id,
            "source_submission_request_fingerprint": request_fingerprint,
        }
    return result


def read_master_rows(workspace, calibration, *, row_hkl, master_obs_from_row) -> list[dict]:
    """All current master (pooled=True) rows in results.csv → convergence observations. Read FRESH
    every iteration (not consume-once): a master's interval shrinks as scans accumulate into it.

    ``row_hkl`` and ``master_obs_from_row`` are the caller's row-interpretation functions, injected
    rather than imported: interpreting a fit row is the loop's science, and this reader is only the
    file mechanics around it (importing them here would also close an ``isar_io``→``loop`` cycle).
    """
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    source_index = exact_pooled_master_sources(workspace)
    out = []
    for r in rows:
        if r.get("pooled") and row_hkl(r) is not None:
            master = master_obs_from_row(r, calibration)
            session_key = r.get("scan_id")
            detector_id = r.get("detector")
            if session_key is not None:
                master["master_session_key"] = str(session_key)
            if detector_id is not None:
                master["detector_id"] = str(detector_id)
            revision = master.get("master_revision")
            if session_key is not None and detector_id is not None and revision is not None:
                exact_source = source_index.get(
                    (str(session_key), str(detector_id), str(revision)))
                if exact_source is not None:
                    master.update(exact_source)
            ref = r.get("predictive_ref")
            if ref:
                candidate = (Path(workspace) / str(ref)).resolve()
                root = (Path(workspace) / "data" / "master_predictive").resolve()
                try:
                    candidate.relative_to(root)
                    predictive = json.loads(candidate.read_text(encoding="utf-8"))
                    # ADR-020: the SAME strict validator the workspace facade and the arbitrary-grid
                    # evaluator run — a schema check alone let a malformed or stale-fingerprinted
                    # basis reach the loop that the other two readers would have refused.
                    validate_master_predictive_artifact(predictive)
                    # The artifact itself is NOT attached. It is a few hundred kilobytes of
                    # parameter draws, it is already content-addressed on disk and named by
                    # ``predictive_ref`` beside this row, and nothing reads it any more -- the
                    # predictive-promotion machinery that did was retired with the legislated
                    # placement rules. Carrying it made every ``pooled_master_update`` event 413 KB
                    # and the session log 87% parameter draws (34 of 38 MB on a 113-scan campaign),
                    # which every reader of that log then paid for on every iteration.
                    master["expected_next_information"] = predictive.get("diagnostics")
                except PredictiveArtifactError as error:
                    # An unusable predictive artifact is no predictive basis at all (ADR-006): the
                    # master keeps every other property and the loop takes its ordinary
                    # no-predictive path, refit or legacy, never a campaign abort.
                    #
                    # The declared schema is read defensively because the refusal being handled is
                    # exactly the one that fires when the payload is not an object at all: a JSON
                    # array, string, number or ``null`` validates to ``artifact_schema_unsupported``,
                    # and ``predictive.get`` on it raised an AttributeError that escaped this whole
                    # function — turning a degrade-and-continue into a campaign abort.
                    log_event(Path(workspace) / "report" / "session_log.jsonl", "exception", {
                        "action": "predictive_artifact_unsupported", "reference": str(ref),
                        "reason": error.reason_code, "message": error.message,
                        "schema": (predictive.get("schema")
                                   if isinstance(predictive, Mapping) else None)})
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    log_event(Path(workspace) / "report" / "session_log.jsonl", "exception", {
                        "action": "predictive_artifact_unavailable", "reference": str(ref),
                        "error": str(error)})
            out.append(master)
    return out
