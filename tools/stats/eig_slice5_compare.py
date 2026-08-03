"""Discover and score completed EIG-4 Slice 5 comparison corpora.

This is deliberately an offline, filesystem-only runner.  It accepts only a
reviewed campaign-set TOML rooted in this checkout; it neither loads a live
workspace by arbitrary path nor invokes the steering runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Direct script execution places this directory, rather than the checkout, on
# sys.path.  The reviewed census module intentionally has no package wrapper.
from _eig_eligibility_census import ConfigError, analyze_campaign_set, load_campaign_set  # noqa: E402


_LOG = logging.getLogger(__name__)
_TERMINAL_STOP_STATES = frozenset({"met", "incomplete", "infeasible", "operator_stopped"})
_NONTERMINAL_STOP_STATES = frozenset({"missing", "pending", "continue", "unknown"})


class OfflineRunnerError(RuntimeError):
    """A read-only corpus cannot be safely discovered or published."""


class ComparisonUnavailableError(OfflineRunnerError):
    """Authenticated evidence whose counterfactual comparison cannot be constructed."""


def _strict_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OfflineRunnerError(f"cannot read JSON {path.name}: {type(error).__name__}") from error


def _strict_toml(path: Path) -> Mapping[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise OfflineRunnerError(f"cannot read TOML {path.name}: {type(error).__name__}") from error
    if not isinstance(value, Mapping):
        raise OfflineRunnerError(f"campaign set {path.name} is not a TOML table")
    return value


def _raw_campaign_roots(campaign_set_path: Path) -> dict[str, str]:
    """Return literal TOML roots without normalising their separators or spelling."""
    raw = _strict_toml(campaign_set_path)
    entries = raw.get("campaign")
    if not isinstance(entries, list):
        raise OfflineRunnerError("campaign set has no campaign tables")
    roots: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise OfflineRunnerError("campaign set has a non-table campaign entry")
        campaign_id, root = entry.get("id"), entry.get("root")
        if not isinstance(campaign_id, str) or not isinstance(root, str):
            raise OfflineRunnerError("campaign set has an invalid id or root")
        roots[campaign_id] = root
    return roots


def _final_stop_state(campaign_root: Path) -> dict[str, Any]:
    """Read only the final durable stop record; never infer completion from scans."""
    path = campaign_root / "report" / "session_log.jsonl"
    if not path.is_file():
        return {"status": "nonterminal_or_unclassified", "state": "missing", "reasons": []}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise OfflineRunnerError(f"cannot read session log {path.name}: {type(error).__name__}") from error
    last_stop: Mapping[str, Any] | None = None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise OfflineRunnerError(f"malformed session log {path.name}:{line_number}") from error
        if not isinstance(event, Mapping):
            raise OfflineRunnerError(f"malformed session log {path.name}:{line_number}")
        if event.get("kind") == "stop" and isinstance(event.get("payload"), Mapping):
            last_stop = event["payload"]
    if last_stop is None:
        return {"status": "nonterminal_or_unclassified", "state": "missing", "reasons": []}
    state = last_stop.get("state")
    reasons = last_stop.get("reasons")
    if not isinstance(state, str):
        state = "unknown"
    if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
        reasons = []
    return {
        "status": "terminal" if state in _TERMINAL_STOP_STATES else "nonterminal_or_unclassified",
        "state": state if state in _TERMINAL_STOP_STATES | _NONTERMINAL_STOP_STATES else "unknown",
        "reasons": list(reasons),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, overwrite: bool) -> None:
    """Publish one complete JSON artifact, preserving an old artifact on failure."""
    if path.exists() and not overwrite:
        raise OfflineRunnerError(f"output already exists: {path.name} (pass --overwrite to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (_canonical_json(payload) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        _LOG.exception("offline comparison output failed: %s", error)
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError as cleanup_error:
            _LOG.error("could not clean failed output stage: %s", cleanup_error)
        raise OfflineRunnerError(f"cannot publish {path.name}: {type(error).__name__}") from error


def _offline_core():
    """Import the pure core lazily so `--help` never touches scientific code."""
    try:
        from isar.eig import offline_compare
    except ImportError as error:
        raise OfflineRunnerError("isar.eig.offline_compare is unavailable") from error
    return offline_compare


def _stop_cases(campaign_set, census_result, raw_roots: Mapping[str, str]) -> list[dict[str, Any]]:
    """Project only census-authenticated pooled-master snapshots into core inputs.

    The core deliberately decides scientific eligibility.  This runner adds
    completed-campaign state and source provenance, and keeps missing evidence
    distinct from a scientific exclusion instead of retrospectively joining old
    acquisitions to a later master.
    """
    stops = {spec.campaign_id: _final_stop_state(spec.root) for spec in campaign_set.campaigns}
    cases: list[dict[str, Any]] = []
    for row in census_result.rows:
        if row.get("row_kind") != "pooled_outcome":
            continue
        campaign_id = row.get("campaign_id")
        if not isinstance(campaign_id, str) or campaign_id not in stops:
            continue
        stop = stops[campaign_id]
        case = {
            "campaign_id": campaign_id,
            "campaign_root": raw_roots[campaign_id],
            "campaign_stop": stop,
            "master_session_key": row.get("master_session_key"),
            "detector_id": row.get("detector_id"),
            "cohort": row.get("cohort"),
            "evidence_tier": row.get("evidence_tier"),
            "lineage": row.get("lineage"),
            "outcome": row.get("outcome"),
            "member_join": row.get("member_join"),
            "action_context": row.get("action_context"),
            "predictive": row.get("predictive"),
            "projection": row.get("projection"),
            "selection": row.get("selection"),
            "shadow": row.get("shadow"),
            "scan_design": row.get("scan_design"),
            "qoi_evidence": row.get("qoi_evidence"),
            "qoi_evidence_fingerprint": row.get("qoi_evidence_fingerprint"),
            "eligibility": row.get("eligibility"),
            "scientific_exclusion": row.get("primary_scientific_exclusion"),
            "observability_gap": row.get("primary_observability_gap"),
        }
        # A non-terminal source is never upgraded by its otherwise complete
        # current-contract evidence.  The distinction remains auditable.
        if stop["status"] != "terminal":
            case["observability_gap"] = "nonterminal_or_unclassified"
        cases.append(case)
    return cases


def _current_objective_comparison_scope(objective: Any) -> Mapping[str, str]:
    """Adapt the current objective to an opaque, versioned applicability identity."""
    from isar.drive.objective import OBJECTIVE_SCHEMA, ObjectiveSpec
    from isar.schema.statistics import semantics_fingerprint

    if not isinstance(objective, ObjectiveSpec):
        raise TypeError("objective must be an ObjectiveSpec")
    raw = objective.to_dict()
    if raw.get("schema") != OBJECTIVE_SCHEMA:
        raise ValueError("objective schema is unsupported")
    payload = {
        "schema": raw["schema"],
        "path": raw["path"],
        "tolerance_meV": raw["tolerance_meV"],
        "target_branch": raw["target_branch"],
        "target_label": raw["target_label"],
        "target_selector": raw["target_selector"],
        "target_energy_meV": raw["target_energy_meV"],
        "energy_search_meV": raw["energy_search_meV"],
        "identity_search_meV": raw["identity_search_meV"],
        "target_temperature_K": raw["target_temperature_K"],
        "hypotheses": raw["hypotheses"],
        "target_hypothesis": raw["target_hypothesis"],
        "hypothesis_fingerprint": raw["hypothesis_fingerprint"],
    }
    revision = "isar.current_objective_comparison_scope/1"
    return {
        "producer_revision": revision,
        "fingerprint": semantics_fingerprint(revision, payload),
    }


def _campaign_configuration(
    campaign_root: Path,
    core: Any,
) -> tuple[Mapping[str, str] | None, str | None]:
    """Read the frozen campaign-start objective, never mutable card state."""
    path = campaign_root / "report" / "session_log.jsonl"
    if not path.is_file():
        return None, "campaign_configuration_missing"
    starts = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise OfflineRunnerError(f"malformed session log {path.name}:{line_number}") from error
        payload = event.get("payload") if isinstance(event, Mapping) else None
        if (event.get("kind") == "decide" and isinstance(payload, Mapping)
                and payload.get("event") == "campaign_start"):
            starts.append(payload.get("objective"))
    if len(starts) != 1 or not isinstance(starts[0], Mapping):
        return None, "campaign_configuration_missing"
    try:
        from isar.drive.objective import ObjectiveSpec
        del core
        return _current_objective_comparison_scope(
            ObjectiveSpec.from_dict(dict(starts[0]))), None
    except (TypeError, ValueError) as error:
        _LOG.warning("campaign configuration is unavailable for %s: %s", campaign_root.name, error)
        return None, "campaign_configuration_invalid"


def _build_discovery_config(core: Any, campaign_set_path: Path, raw_roots: Mapping[str, str]):
    """Single seam for the pure-core config contract."""
    del campaign_set_path
    # The raw root strings are deliberately excluded: they are operator
    # provenance, not scientific configuration, and preserving them byte-for-
    # byte in the case diagnostic must not make a corpus path-dependent.
    del raw_roots
    return core.OfflineDiscoveryConfig()


def _context_from_record(value: Mapping[str, Any]):
    """Rehydrate the exact persisted context before regenerating its inventory."""
    from isar.drive.acquisition.scan_design import ScanDesignContext
    count_policy = value.get("count_policy") if isinstance(value.get("count_policy"), Mapping) else {}
    count_state = value.get("count_state") if isinstance(value.get("count_state"), Mapping) else {}
    limits = value.get("runtime_limits") if isinstance(value.get("runtime_limits"), Mapping) else {}
    budget = value.get("budget") if isinstance(value.get("budget"), Mapping) else {}
    pairs = lambda name: tuple(value[name]) if value.get(name) is not None else None
    return ScanDesignContext(
        scientific_action_fingerprint=str(value["scientific_action_fingerprint"]), hkl=tuple(value["hkl"]),
        q=float(value["q"]), kind=str(value["kind"]), reason=str(value["reason"]),
        window_family=str(value["window_family"]), declared_energy_lo=value.get("declared_energy_lo"),
        declared_energy_hi=value.get("declared_energy_hi"),
        target_prediction_mean_meV=float(value["target_prediction_mean_meV"]),
        target_prediction_sigma_meV=float(value["target_prediction_sigma_meV"]),
        design_prediction_mean_meV=float(value["design_prediction_mean_meV"]),
        design_prediction_sigma_meV=float(value["design_prediction_sigma_meV"]),
        resolution_fwhm_meV=float(value["resolution_fwhm_meV"]), survey=bool(value["survey"]),
        energy_search_meV=pairs("energy_search_meV"), target_band=pairs("target_band"),
        rival_band=pairs("rival_band"), widen_factor=float(value["widen_factor"]),
        positive_only=bool(value["positive_only"]), policy_max_points=int(value["policy_max_points"]),
        runtime_max_points=int(value["runtime_max_points"]), tolerance_meV=float(value["tolerance_meV"]),
        scan_window_eig_mode=str(value.get("scan_window_eig_mode", "off")),
        minimum_target_support_points=int(value.get("minimum_target_support_points", 6)),
        quicklook_ncount=int(count_policy.get("quicklook_ncount")),
        production_ncount=int(count_policy.get("production_ncount")),
        identity_detection_ncount=int(count_policy.get("identity_detection_ncount")),
        dynamic_ncount=bool(count_policy.get("dynamic_ncount")),
        tolerance_margin=float(count_policy.get("tolerance_margin")),
        ncount_growth_cap=float(count_policy.get("ncount_growth_cap")),
        max_ncount_per_scan=int(count_policy.get("max_ncount_per_scan")),
        diagnostic_halfwidth_meV=count_state.get("diagnostic_halfwidth_meV"),
        certification_halfwidth_meV=count_state.get("certification_halfwidth_meV"),
        effective_exposure=count_state.get("effective_exposure"), submitted_exposure=count_state.get("submitted_exposure"),
        production_scans=int(count_state.get("production_scans", 0)),
        last_submitted_ncount=count_state.get("last_submitted_ncount"),
        runtime_max_neutrons_per_point=limits.get("max_neutrons_per_point"),
        runtime_queue_neutron_budget=limits.get("queue_neutron_budget"), budget_kind=budget.get("kind"),
        budget_amount=budget.get("amount"), budget_spent=float(budget.get("spent", 0.0)),
    )


def _candidate_view(candidate: Any) -> dict[str, Any]:
    return {"candidate_id": candidate.candidate_id, "family": candidate.candidate_family,
            "energy_meV": list(candidate.energies), "exposure_per_point": float(candidate.ncount)}


def _persisted_candidate_view(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {"candidate_id": candidate.get("candidate_id"), "family": candidate.get("candidate_family"),
            "energy_meV": candidate.get("scoring_grid"), "exposure_per_point": candidate.get("ncount")}


def _shadow_pair_candidates(shadow: Any):
    """Find the frozen comparison pair, preserving explicit generator refusals."""
    shadow_legacy = next((candidate for candidate in shadow.candidates
                          if candidate.candidate_family == "legacy_uniform/1"), None)
    target = next((candidate for candidate in shadow.candidates
                   if candidate.candidate_family == "target_core/1"), None)
    if target is None:
        if shadow_legacy is not None and "target_core/1" in shadow_legacy.alias_families:
            raise OfflineRunnerError("candidate_pair_degenerate")
        refusal = next((item.reason_code for item in shadow.refusals
                        if item.candidate_family == "target_core/1"), None)
        raise OfflineRunnerError(str(refusal or "candidate_family_missing"))
    if shadow_legacy is None:
        raise OfflineRunnerError("candidate_family_missing")
    return shadow_legacy, target


def _load_inventory(campaign_root: Path, reference: Mapping[str, Any]) -> Mapping[str, Any]:
    """Load one content-addressed inventory under its allowed campaign directory."""
    if reference.get("kind") != "scan_design_inventory":
        raise OfflineRunnerError("scan_design_reference_invalid")
    digest, relative = reference.get("sha256"), reference.get("path")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(relative, str):
        raise OfflineRunnerError("scan_design_reference_invalid")
    path = (campaign_root / relative).resolve()
    permitted = (campaign_root / "report" / "scan-design").resolve()
    try:
        path.relative_to(permitted)
    except ValueError as error:
        raise OfflineRunnerError("scan_design_reference_escaped") from error
    record = _strict_json(path)
    from isar.drive.acquisition.inventory import canonical_json
    if path.name != f"{digest}.json" or hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest() != digest:
        raise OfflineRunnerError("scan_design_content_address_mismatch")
    if not isinstance(record, Mapping) or record.get("inventory_version") != "isar.scan_design_inventory/2":
        raise OfflineRunnerError("scan_design_schema_unsupported")
    return record


def _raw_outcome(campaign_root: Path, row: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    session = _strict_json(campaign_root / "session.json")
    scans = session.get("scans") if isinstance(session, Mapping) else None
    entry = scans.get(row.get("master_session_key")) if isinstance(scans, Mapping) else None
    detectors = entry.get("detectors") if isinstance(entry, Mapping) else None
    outcome = detectors.get(row.get("detector_id")) if isinstance(detectors, Mapping) else None
    if not isinstance(entry, Mapping) or not isinstance(outcome, Mapping):
        raise OfflineRunnerError("pooled_outcome_missing")
    return entry, outcome


def _load_sidecar(campaign_root: Path, reference: str, directory: str) -> Mapping[str, Any]:
    path = (campaign_root / reference).resolve()
    allowed = (campaign_root / directory).resolve()
    try:
        path.relative_to(allowed)
    except ValueError as error:
        raise OfflineRunnerError("sidecar_reference_escaped") from error
    value = _strict_json(path)
    if not isinstance(value, Mapping):
        raise OfflineRunnerError("sidecar_record_malformed")
    digest = _file_sha256(path)
    if path.suffix != ".json" or path.stem != digest:
        raise OfflineRunnerError("sidecar_content_address_mismatch")
    return value


def _persisted_generator(record: Mapping[str, Any]):
    """Authenticate and rehydrate the generator envelope persisted by scan design."""
    from isar.drive.acquisition.scan_design import (
        PHYSICAL_WINDOW_GENERATOR_ID,
        PHYSICAL_WINDOW_GENERATOR_VERSION,
        PhysicalWindowGeneratorConfig,
    )

    generator = record.get("generator")
    if not isinstance(generator, Mapping) or set(generator) != {
            "id", "version", "config", "config_fingerprint"}:
        raise OfflineRunnerError("generator_config_invalid")
    if (generator["id"] != PHYSICAL_WINDOW_GENERATOR_ID
            or generator["version"] != PHYSICAL_WINDOW_GENERATOR_VERSION):
        raise ComparisonUnavailableError("generator_config_unsupported")
    try:
        config = PhysicalWindowGeneratorConfig.from_dict(generator["config"])
    except (TypeError, ValueError) as error:
        raise OfflineRunnerError("generator_config_invalid") from error
    if generator["config_fingerprint"] != config.fingerprint:
        raise OfflineRunnerError("generator_config_fingerprint_mismatch")
    return config, {
        "id": generator["id"],
        "version": generator["version"],
        "config": config.to_dict(),
        "config_fingerprint": config.fingerprint,
    }


def _prepare_discovery_case(core: Any, campaign_root: Path, row: Mapping[str, Any]):
    """Authenticate one comparison case without evaluating either candidate grid."""
    from isar.eig.design_measure import build_scan_window_design_measure
    from isar.eig.eligibility import EligibilityInput
    from isar.eig.qoi import adapt_omega0_target_projection
    from isar.predictive import validate_master_predictive_artifact
    from isar.drive.acquisition.scan_design import design_scan_windows

    entry, outcome = _raw_outcome(campaign_root, row)
    action = row.get("action_context") if isinstance(row.get("action_context"), Mapping) else {}
    reference = action.get("scan_design_ref")
    if not isinstance(reference, Mapping):
        raise OfflineRunnerError("scan_design_provenance_missing")
    persisted = _load_inventory(campaign_root, reference)
    context_raw = persisted.get("context")
    candidates = persisted.get("candidates")
    selected = (persisted.get("selection") or {}).get("selected_candidate_id")
    if not isinstance(context_raw, Mapping) or not isinstance(candidates, list) or not isinstance(selected, str):
        raise OfflineRunnerError("scan_design_inventory_malformed")
    persisted_candidate = next((item for item in candidates if isinstance(item, Mapping)
                                and item.get("candidate_id") == selected), None)
    if not isinstance(persisted_candidate, Mapping):
        raise ComparisonUnavailableError("persisted_legacy_candidate_missing")
    generator_config, generator_identity = _persisted_generator(persisted)
    context = _context_from_record(context_raw)
    regenerated = design_scan_windows(context, generator_config)
    legacy = regenerated.selected_candidate
    shadow_transform = core.offline_shadow_context_transform(
        context, generator_config.fingerprint)
    shadow_context = _context_from_record(shadow_transform["offline_context"])
    shadow = design_scan_windows(shadow_context, generator_config)
    try:
        shadow_legacy, target = _shadow_pair_candidates(shadow)
        pair = core.validate_offline_candidate_pair(
            _persisted_candidate_view(persisted_candidate),
            _candidate_view(shadow_legacy),
            _candidate_view(target),
            regenerated_persisted_legacy=_candidate_view(legacy),
            generator_identity=generator_identity,
        )
    except (OfflineRunnerError, TypeError, ValueError) as error:
        raise ComparisonUnavailableError(
            str(error) or "offline_candidate_reconstruction_failed") from error

    predictive_ref = outcome.get("predictive_ref")
    projection_ref = outcome.get("target_projection_ref")
    if not isinstance(predictive_ref, str) or not isinstance(projection_ref, str):
        raise OfflineRunnerError("predictive_or_projection_reference_missing")
    predictive = _load_sidecar(campaign_root, predictive_ref, "data/master_predictive")
    projection = _load_sidecar(campaign_root, projection_ref, "data/target_projection")
    validate_master_predictive_artifact(predictive)
    provenance = entry.get("provenance") if isinstance(entry.get("provenance"), Mapping) else {}
    authority_source = (provenance.get("target_projection_authority")
                        if isinstance(provenance.get("target_projection_authority"), Mapping) else {})
    from isar.predictive import json_fingerprint
    from isar.schema.card import Card
    policy_fingerprint = json_fingerprint(Card.load(campaign_root / "card.policy.toml").policy)
    expected_authority = {
        "action_role": authority_source.get("action_role"),
        "master_revision": authority_source.get("master_revision", outcome.get("master_revision")),
        "track_revision": outcome.get("track_revision"), "track_state": outcome.get("track_state"),
        "candidate_id": outcome.get("candidate_id"), "candidate_state": outcome.get("candidate_state"),
        "selected_candidate_index": outcome.get("selected_candidate_index"),
        "calibration_fingerprint": authority_source.get("calibration_fingerprint"),
        "policy_fingerprint": policy_fingerprint,
    }
    qoi = adapt_omega0_target_projection(predictive, projection, expected_authority=expected_authority)
    eligibility_input = EligibilityInput.from_current_contract(
        action_intent=action.get("intent"), action_provenance=action.get("provenance"),
        target_outcome=row.get("outcome"), selection_record=outcome.get("fit_selection_semantics"),
        qoi_evidence=qoi, shadow_record=outcome.get("shadow_model_selection"),
        scan_design_provenance=row.get("scan_design"),
        scan_design_events=row.get("scan_design", {}).get("realization_events", ()),
    )
    measure = build_scan_window_design_measure(predictive, eligibility_input)
    if measure.status != "available":
        codes = tuple(measure.reason_codes) + tuple(measure.construction_codes)
        raise ComparisonUnavailableError(codes[0] if codes else "design_measure_unavailable")
    recipe = {
        "measure": measure,
        "predictive": predictive,
        "candidates": (shadow_legacy, target),
    }
    return pair, shadow_transform, recipe


def _prepare_scoring_case(recipe: Mapping[str, Any]):
    """Evaluate exactly the two authenticated grids for one selected case."""
    from isar.eig.scorer import CandidateEvaluationInput
    from isar.predictive import evaluate_master_predictive

    evaluations = []
    for candidate in recipe["candidates"]:
        evaluation = evaluate_master_predictive(
            recipe["predictive"], candidate.energies, candidate.ncount)
        evaluations.append(CandidateEvaluationInput(candidate.candidate_id, evaluation))
    return recipe["measure"], tuple(evaluations)


def _build_cases(
    core: Any,
    case_payloads: list[dict[str, Any]],
    configurations: Mapping[str, tuple[Mapping[str, str] | None, str | None]],
    roots: Mapping[str, Path],
):
    """Turn census rows into fail-closed, root-independent core snapshots."""
    cases, scoring_recipes = [], {}
    for row in case_payloads:
        outcome = row.get("outcome") if isinstance(row.get("outcome"), Mapping) else {}
        qoi = row.get("qoi_evidence") if isinstance(row.get("qoi_evidence"), Mapping) else {}
        eligibility = row.get("eligibility") if isinstance(row.get("eligibility"), Mapping) else {}
        campaign_id = str(row["campaign_id"])
        scientific = tuple(str(code) for code in eligibility.get("scientific_codes", ())
                           if isinstance(code, str))
        observable = tuple(str(code) for code in eligibility.get("observability_codes", ())
                           if isinstance(code, str))
        stop = row["campaign_stop"]
        configuration_scope, configuration_problem = configurations[campaign_id]
        candidate_pair, context_transform, scoring_recipe = {}, {}, None
        if stop["status"] != "terminal":
            status, reasons = "nonterminal_or_unclassified", ("nonterminal_or_unclassified",)
        elif configuration_problem:
            status, reasons = "unassessable", (configuration_problem,)
        elif scientific:
            status, reasons = "scientifically_ineligible", scientific
        elif observable:
            status, reasons = "unassessable", observable
        else:
            try:
                candidate_pair, context_transform, scoring_recipe = _prepare_discovery_case(
                    core, roots[campaign_id], row)
                status, reasons = "ready", ()
            except ComparisonUnavailableError as error:
                _LOG.info("Slice 5.1 case %s comparison is unavailable: %s", campaign_id, error)
                status, reasons = "comparison_unavailable", (
                    str(error) or "offline_comparison_reconstruction_failed",)
                candidate_pair, context_transform, scoring_recipe = {}, {}, None
            except (OfflineRunnerError, OSError, TypeError, ValueError, KeyError) as error:
                _LOG.info("Slice 5.1 case %s is unassessable: %s", campaign_id, error)
                status, reasons = "unassessable", (str(error) or "offline_reconstruction_failed",)
                candidate_pair, context_transform, scoring_recipe = {}, {}, None
        action = row.get("action_context") if isinstance(row.get("action_context"), Mapping) else {}
        scan_design = row.get("scan_design") if isinstance(row.get("scan_design"), Mapping) else {}
        projection = row.get("projection") if isinstance(row.get("projection"), Mapping) else {}
        predictive = row.get("predictive") if isinstance(row.get("predictive"), Mapping) else {}
        source_fingerprints = {
            "qoi_evidence": row.get("qoi_evidence_fingerprint"),
            "eligibility_input": eligibility.get("input_fingerprint"),
            "eligibility_result": eligibility.get("result_fingerprint"),
            "master_revision": outcome.get("master_revision"),
            "predictive_basis": predictive.get("predictive_basis_fingerprint"),
            "predictive_retained_set": (qoi.get("predictive_retained_set_fingerprint")),
            "predictive_semantics": qoi.get("predictive_statistical_semantics_fingerprint"),
            "projection": projection.get("projection_set_fingerprint"),
            "projection_semantics": qoi.get("projection_statistical_semantics_fingerprint"),
            "projection_authority": qoi.get("projection_authority_fingerprint"),
            "selection": (row.get("selection_record") or {}).get("selection_fingerprint")
                         if isinstance(row.get("selection_record"), Mapping) else None,
            "scan_design_inventory": scan_design.get("inventory_fingerprint"),
            "scan_design_context": scan_design.get("context_fingerprint"),
        }
        if scoring_recipe is not None:
            measure = scoring_recipe["measure"]
            source_fingerprints.update({
                "design_measure": measure.design_measure_fingerprint,
                "source_draw_identity": measure.source_draw_identity_fingerprint,
                "predictive_basis": dict(measure.source_identities).get(
                    "predictive_basis_fingerprint"),
                "generator_config": candidate_pair["generator"]["config_fingerprint"],
            })
        decision_identity = hashlib.sha256(_canonical_json({
            "terminal": {"state": stop.get("state"), "reasons": stop.get("reasons")},
            "campaign_configuration": configuration_scope,
            "outcome": {key: outcome.get(key) for key in ("master_revision", "hkl", "evidence_sink",
                        "track_revision", "track_state", "candidate_id", "candidate_state",
                        "selected_candidate_index")},
            "action": {key: action.get(key) for key in ("intent", "provenance")},
            "selection": row.get("selection_record"), "shadow": row.get("shadow"),
            "predictive": {key: predictive.get(key) for key in ("predictive_basis_fingerprint", "sha256")},
            "projection": {key: projection.get(key) for key in ("projection_set_fingerprint", "authority_fields",
                         "currentness", "sha256")},
            "qoi_evidence": row.get("qoi_evidence_fingerprint"),
            "eligibility": {key: eligibility.get(key) for key in ("input_fingerprint", "result_fingerprint")},
            "scan_design": {key: scan_design.get(key) for key in ("inventory_fingerprint", "context_fingerprint",
                             "selected_candidate_id", "selected_candidate_family", "realization_events")},
            "context_transform": context_transform.get("offline_context_fingerprint"),
            "candidate_pair": candidate_pair.get("candidate_pair_fingerprint"),
            "design_measure": source_fingerprints.get("design_measure"),
            "source_draw_identity": source_fingerprints.get("source_draw_identity"),
        }).encode("utf-8")).hexdigest()
        source_provenance = core.make_source_provenance(
            reviewed_campaign_id=campaign_id,
            master_session_key=str(row["master_session_key"]),
            detector_id=str(row["detector_id"]),
            cohort=str(row["cohort"]),
            evidence_tier=str(row["evidence_tier"]),
        )
        case_identity = source_provenance["source_provenance_fingerprint"]
        cases.append(core.OfflineCaseSnapshot(
            case_identity=case_identity,
            decision_snapshot_identity=decision_identity,
            source_provenance=source_provenance,
            campaign_configuration_scope=configuration_scope,
            qoi_id=(str(qoi["qoi_id"])
                    if qoi.get("status") in {"valid", "qualified"}
                    and isinstance(qoi.get("qoi_id"), str) and qoi.get("qoi_id") else None),
            qoi_unit=(str(qoi["qoi_unit"])
                      if qoi.get("status") in {"valid", "qualified"}
                      and isinstance(qoi.get("qoi_unit"), str) and qoi.get("qoi_unit") else None),
            status=status,
            reason_codes=tuple(dict.fromkeys(reasons)),
            source_fingerprints=source_fingerprints,
            context_transform=context_transform,
            candidate_pair=candidate_pair,
            authority_scopes=(
                (("acquisition_selection", "ineligible"), ("eig_authoritative", "ineligible"),
                 ("eig_shadow", "diagnostic")) if status == "ready" else
                (("acquisition_selection", "ineligible"), ("eig_authoritative", "ineligible"),
                 ("eig_shadow", "ineligible"))
            ),
        ))
        if scoring_recipe is not None:
            scoring_recipes[case_identity] = scoring_recipe
    return tuple(sorted(cases, key=lambda item: item.case_identity)), scoring_recipes


def discover(campaign_set_path: Path, output: Path, *, overwrite: bool = False) -> Mapping[str, Any]:
    """Discover a stable offline corpus index from the reviewed campaign population."""
    campaign_set = load_campaign_set(campaign_set_path, _REPO_ROOT)
    output_path = _output_outside_campaign_roots(output, campaign_set)
    raw_roots = _raw_campaign_roots(campaign_set.source)
    result = analyze_campaign_set(campaign_set, _REPO_ROOT)
    core = _offline_core()
    configurations = {spec.campaign_id: _campaign_configuration(spec.root, core)
                      for spec in campaign_set.campaigns}
    cases, _ = _build_cases(core, _stop_cases(campaign_set, result, raw_roots), configurations,
                            {spec.campaign_id: spec.root for spec in campaign_set.campaigns})
    index = core.OfflineCorpusIndex(
        _build_discovery_config(core, campaign_set.source, raw_roots),
        cases,
        diagnostics={"campaign_roots": dict(sorted(raw_roots.items())),
                     "campaign_stop_states": {spec.campaign_id: _final_stop_state(spec.root)
                                              for spec in campaign_set.campaigns}},
    )
    payload = index.to_dict()
    _atomic_write_json(output_path, payload, overwrite=overwrite)
    return payload


def _resolve_output(output: Path) -> Path:
    path = output if output.is_absolute() else _REPO_ROOT / output
    return path.resolve()


def _output_outside_campaign_roots(output: Path, campaign_set: Any) -> Path:
    """Resolve an artifact path and refuse every scientist-owned campaign tree."""
    path = _resolve_output(output)
    for campaign in campaign_set.campaigns:
        try:
            path.relative_to(campaign.root.resolve())
        except ValueError:
            continue
        raise OfflineRunnerError(
            f"output must be outside campaign roots: {campaign.campaign_id}")
    return path


def load_offline_corpus_index(path: Path, core: Any | None = None):
    """Strictly load and validate one persisted corpus index."""
    core = _offline_core() if core is None else core
    return core.OfflineCorpusIndex.from_dict(_strict_json(_resolve_output(path)))


def load_qoi_distinction_policies(path: Path | None, core: Any | None = None):
    """Load hand-authored TOML; fingerprints are optional but authenticated when supplied."""
    core = _offline_core() if core is None else core
    if path is None:
        return core.QoIDistinctionPolicySet(())
    raw = _strict_toml(_resolve_output(path))
    if not isinstance(raw, Mapping) or set(raw) - {
            "contract", "policies", "policy_set_fingerprint"}:
        raise OfflineRunnerError("qoi_policy_file_invalid")
    if raw.get("contract") != core.QOI_DISTINCTION_POLICY_SET_CONTRACT:
        raise OfflineRunnerError("qoi_policy_file_contract_unsupported")
    policy_records = raw.get("policies")
    if not isinstance(policy_records, list):
        raise OfflineRunnerError("qoi_policy_file_invalid")
    policies = []
    try:
        for policy_raw in policy_records:
            if not isinstance(policy_raw, Mapping) or set(policy_raw) - {
                    "contract", "policy_id", "policy_kind", "source_qoi", "scope",
                    "provenance", "variants", "policy_fingerprint"}:
                raise ValueError("policy fields are invalid")
            if policy_raw.get("contract") != core.QOI_DISTINCTION_POLICY_CONTRACT:
                raise ValueError("policy contract is unsupported")
            variant_records = policy_raw.get("variants")
            if not isinstance(variant_records, list):
                raise ValueError("policy variants are invalid")
            variants = []
            for variant_raw in variant_records:
                if not isinstance(variant_raw, Mapping) or set(variant_raw) - {
                        "variant_id", "parameters", "variant_fingerprint"}:
                    raise ValueError("policy variant fields are invalid")
                variant = core.QoIDistinctionVariant(
                    variant_raw.get("variant_id"), variant_raw.get("parameters"))
                supplied = variant_raw.get("variant_fingerprint")
                if supplied is not None and supplied != variant.variant_fingerprint:
                    raise ValueError("variant_fingerprint does not authenticate policy variant")
                variants.append(variant)
            policy = core.QoIDistinctionPolicy(
                policy_id=policy_raw.get("policy_id"),
                policy_kind=policy_raw.get("policy_kind"),
                source_qoi=policy_raw.get("source_qoi"),
                scope=policy_raw.get("scope"),
                provenance=policy_raw.get("provenance"),
                variants=tuple(variants),
            )
            supplied = policy_raw.get("policy_fingerprint")
            if supplied is not None and supplied != policy.policy_fingerprint:
                raise ValueError("policy_fingerprint does not authenticate policy")
            if policy.policy_kind == "isar.scalar_absolute_partitions/o36.5":
                for variant in policy.variants:
                    core.compile_qoi_distinction_policy(policy, variant)
            policies.append(policy)
        policy_set = core.QoIDistinctionPolicySet(tuple(policies))
        supplied = raw.get("policy_set_fingerprint")
        if supplied is not None and supplied != policy_set.policy_set_fingerprint:
            raise ValueError("policy_set_fingerprint does not authenticate policy set")
        return core.validate_qoi_distinction_policy_set(policy_set)
    except (KeyError, TypeError, ValueError) as error:
        raise OfflineRunnerError(f"qoi_policy_file_invalid: {error}") from error


def score(campaign_set_path: Path, corpus_index_path: Path, output: Path, *, policy_file: Path | None,
          overwrite: bool = False, seed: int | None = None, simulation_count: int | None = None,
          block_count: int | None = None, prepared_scientific_cases: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Validate an index against a fresh discovery, then score its cases once."""
    core = _offline_core()
    supplied = load_offline_corpus_index(corpus_index_path, core)
    campaign_set = load_campaign_set(campaign_set_path, _REPO_ROOT)
    output_path = _output_outside_campaign_roots(output, campaign_set)
    raw_roots = _raw_campaign_roots(campaign_set.source)
    fresh = analyze_campaign_set(campaign_set, _REPO_ROOT)
    configurations = {spec.campaign_id: _campaign_configuration(spec.root, core)
                      for spec in campaign_set.campaigns}
    cases, discovered_scoring_recipes = _build_cases(
        core, _stop_cases(campaign_set, fresh, raw_roots), configurations,
        {spec.campaign_id: spec.root for spec in campaign_set.campaigns})
    rediscovered = core.OfflineCorpusIndex(
        _build_discovery_config(core, campaign_set.source, raw_roots),
        cases,
    )
    policy_set = load_qoi_distinction_policies(policy_file, core)
    if seed is None or simulation_count is None or block_count is None:
        raise OfflineRunnerError("--seed, --simulation-count, and --block-count are required for score")
    scoring_config = core.OfflineScoringConfig(seed=seed, simulation_count=simulation_count,
                                               block_count=block_count)
    if supplied.corpus_fingerprint != rediscovered.corpus_fingerprint:
        report = core.OfflineComparisonReport(
            supplied.corpus_fingerprint, policy_set.policy_set_fingerprint,
            scoring_config.scoring_config_fingerprint, "unavailable", (),
            diagnostics={"reason_codes": ["corpus_index_stale"],
                         "supplied_corpus_fingerprint": supplied.corpus_fingerprint,
                         "rediscovered_corpus_fingerprint": rediscovered.corpus_fingerprint},
        )
    else:
        if prepared_scientific_cases is None:
            def scientific_case_provider(case):
                recipe = discovered_scoring_recipes.get(case.case_identity)
                return None if recipe is None else _prepare_scoring_case(recipe)
        else:
            def scientific_case_provider(case):
                return prepared_scientific_cases.get(case.case_identity)
        report = core.score_offline_comparison(
            supplied, policy_set, scoring_config, scientific_case_provider)
    payload = report.to_dict()
    _atomic_write_json(output_path, payload, overwrite=overwrite)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    discover_parser = commands.add_parser("discover", help="build an offline corpus index")
    score_parser = commands.add_parser("score", help="validate and score an offline corpus index")
    for child in (discover_parser, score_parser):
        child.add_argument("--campaign-set", required=True, type=Path,
                           help="reviewed TOML campaign population relative to this checkout")
        child.add_argument("--output", required=True, type=Path, help="JSON artifact to create")
        child.add_argument("--overwrite", action="store_true", help="replace an existing output artifact")
    score_parser.add_argument("--corpus-index", required=True, type=Path, help="discovered corpus JSON")
    score_parser.add_argument("--policy-file", type=Path, help="optional QoI distinction policy TOML")
    score_parser.add_argument("--seed", type=int, required=True, help="explicit scoring common-random seed")
    score_parser.add_argument("--simulation-count", type=int, required=True, help="explicit simulation count")
    score_parser.add_argument("--block-count", type=int, required=True, help="explicit paired uncertainty block count")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "discover":
            discover(args.campaign_set, args.output, overwrite=args.overwrite)
        else:
            score(args.campaign_set, args.corpus_index, args.output, policy_file=args.policy_file,
                  overwrite=args.overwrite, seed=args.seed, simulation_count=args.simulation_count,
                  block_count=args.block_count)
    except (ConfigError, OfflineRunnerError, ValueError, TypeError) as error:
        _LOG.error("offline comparison failed: %s", error)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
