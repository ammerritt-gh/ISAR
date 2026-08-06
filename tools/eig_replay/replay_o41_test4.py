"""Read-only EIG-4 Slice R1-E replay over the real O41_Test4 campaign state.

Answers three questions per real pooled master, against retained campaign evidence rather than
synthetic fixtures:

1. **Eligibility** — the persisted decision-point verdict (recorded in each scan-design inventory's
   ``shadow_scoring.eligibility``) versus the verdict the current merged policy returns for the same
   master today.
2. **Geometry** — the ``target_core/1`` window the generator produces from the persisted global
   ω(q) dispersion prediction versus the window it produces from the master's own same-Q evidence.
3. **Score** — where a design measure is constructible, the full ``legacy_uniform/1`` versus
   ``target_core/1`` comparison, its paired uncertainty, the O41 selector's role, and whether the
   selected role survives scoring under each of the campaign policy's bin offsets separately.

Nothing here writes into the workspace: every artifact is read, and all output goes to
``--output``.  The scoring policy, partition parameters and scoring configuration are compiled from
the campaign's own card and goal, never restated as constants.

Usage::

    python tools/eig_replay/replay_o41_test4.py \\
        --workspace test_workspaces/O41_Test4/isar-eig4-o41-authoritative \\
        --output <report directory>
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import logging
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from isar.drive.acquisition.calibration import load_calibration  # noqa: E402
from isar.drive.acquisition.scan_design import (  # noqa: E402
    ScanDesignContext,
    design_scan_windows,
)
from isar.drive.objective import current_objective_comparison_scope  # noqa: E402
from isar.drive.supervisor.goal_to_spec import spec_from_dict  # noqa: E402
from isar.eig import adapt_omega0_target_projection  # noqa: E402
from isar.eig.eligibility import EligibilityInput, evaluate_eig_eligibility  # noqa: E402
from isar.eig.o41_authority import (  # noqa: E402
    O41CandidateEvidence,
    O41PartitionOffsetEvidence,
    O41ScoreEvidence,
    O41SelectorInput,
    select_o41_authority,
    validate_o41_authoritative_profile,
)
from isar.eig.policy import compile_card_eig_policy_set  # noqa: E402
from isar.eig.shadow_inventory import score_shadow_inventory  # noqa: E402
from isar.drive.acquisition.inventory import (  # noqa: E402
    scientific_fingerprint_from_mapping,
)
from isar.predictive import json_fingerprint  # noqa: E402
from isar.schema.card import Card  # noqa: E402

CALIBRATION_PATH = REPO_ROOT / "calibration" / "phonon_dft_deterministic.json"
CENSUS_ENGINE_PATH = REPO_ROOT / "tools" / "stats" / "_eig_eligibility_census.py"
LEGACY_FAMILY = "legacy_uniform/1"
TARGET_FAMILY = "target_core/1"

log = logging.getLogger("eig_replay")


def _load_census_engine():
    """Import the read-only census engine for its authenticated artifact readers."""
    spec = importlib.util.spec_from_file_location(
        "_eig_replay_census_engine", CENSUS_ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"census engine is not importable at {CENSUS_ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _finite(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _round(value: Any, digits: int = 6) -> Any:
    return round(float(value), digits) if _finite(value) else value


# --------------------------------------------------------------------- persisted campaign evidence

def _persisted_decision_points(workspace: Path,
                               failures: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Every retained scan-design inventory, with its persisted eligibility and geometry.

    A record that cannot be read is never silently dropped: it is logged *and* appended to
    ``failures``, which the report publishes, so "no persisted context at this q" can always be
    distinguished from "the context at this q was unreadable".
    """
    records: list[dict[str, Any]] = []
    directory = workspace / "report" / "scan-design"
    for path in sorted(directory.glob("*.json")):
        try:
            payload = _read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            log.warning("scan-design record %s unreadable: %s", path.name, error)
            failures.append({"kind": "scan_design", "record": path.name,
                             "error": f"{type(error).__name__}: {error}"})
            continue
        context = payload.get("context")
        if not isinstance(context, Mapping):
            log.warning("scan-design record %s has no context block", path.name)
            failures.append({"kind": "scan_design", "record": path.name,
                             "error": "context_block_missing"})
            continue
        shadow = payload.get("shadow_scoring") or {}
        eligibility = shadow.get("eligibility") or {}
        families = {
            candidate.get("candidate_family"): candidate
            for candidate in payload.get("candidates") or []
            if isinstance(candidate, Mapping)
        }
        refusals = {
            item.get("candidate_family"): item.get("reason_code")
            for item in payload.get("refused_candidates") or []
            if isinstance(item, Mapping)
        }
        records.append({
            "record": path.name,
            "context": dict(context),
            "hkl": list(context.get("hkl") or ()),
            "kind": context.get("kind"),
            "window_family": context.get("window_family"),
            "persisted_eligibility_status": eligibility.get("status"),
            "persisted_scientific_codes": list(eligibility.get("scientific_codes") or ()),
            "persisted_observability_codes": list(eligibility.get("observability_codes") or ()),
            "persisted_shadow_status": shadow.get("status"),
            "persisted_target_core": families.get(TARGET_FAMILY),
            "persisted_target_core_refusal": refusals.get(TARGET_FAMILY),
        })
    return records


def _persisted_action_intents(workspace: Path,
                              failures: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Every retained action-inventory intent, the campaign's own canonical action vocabulary."""
    intents: list[dict[str, Any]] = []
    for path in sorted((workspace / "report" / "inventory").glob("*.json")):
        try:
            payload = _read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            log.warning("action inventory %s unreadable: %s", path.name, error)
            failures.append({"kind": "action_inventory", "record": path.name,
                             "error": f"{type(error).__name__}: {error}"})
            continue
        for evaluation in payload.get("evaluations") or []:
            intent = evaluation.get("intent") if isinstance(evaluation, Mapping) else None
            if isinstance(intent, Mapping):
                intents.append(dict(intent))
    return intents


def _match_by_hkl(items: list[dict[str, Any]], hkl: list[float], *,
                  key: str = "hkl") -> list[dict[str, Any]]:
    def same(value: Any) -> bool:
        try:
            return all(math.isclose(float(a), float(b), abs_tol=1e-9)
                       for a, b in zip(value, hkl)) and len(value) == len(hkl)
        except (TypeError, ValueError):
            return False

    return [item for item in items if same(item.get(key))]


# ------------------------------------------------------------------------------ same-Q prediction

def _same_q_basis(outcome: Mapping[str, Any], inflation: float) -> dict[str, Any]:
    """Reconstruct this master's own centre and spread, mirroring the runtime same-Q rule.

    The runtime reads them off the driver's live master view; from retained session state the same
    two numbers are the pooled ω₀ and the calibrated half-width of the certifying model interval,
    falling back to the diagnostic (bootstrap) interval exactly as the runtime does.
    """
    result: dict[str, Any] = {
        "mean_meV": None, "sigma_meV": None, "source": None, "usable": False,
        "calibration_inflation": inflation,
    }
    omega0 = outcome.get("omega0")
    if not _finite(omega0):
        result["source"] = "omega0_not_finite"
        return result
    result["mean_meV"] = float(omega0)
    model_interval = outcome.get("model_interval")
    certifiable = (outcome.get("model_robust") is True
                   and outcome.get("target_delivery_eligible") is not False
                   and isinstance(model_interval, (list, tuple)) and len(model_interval) == 3)
    if certifiable and _finite(model_interval[0]) and _finite(model_interval[2]):
        halfwidth = inflation * (float(model_interval[2]) - float(model_interval[0])) / 2.0
        if halfwidth > 0.0:
            result.update(sigma_meV=halfwidth, source="model_interval", usable=True)
            return result
    interval = outcome.get("interval")
    if isinstance(interval, (list, tuple)) and len(interval) == 3 and _finite(
            interval[0]) and _finite(interval[2]):
        halfwidth = inflation * (float(interval[2]) - float(interval[0])) / 2.0
        if halfwidth > 0.0:
            result.update(sigma_meV=halfwidth, source="diagnostic_interval", usable=True)
            return result
    result["source"] = "no_local_spread_evidence"
    return result


def _target_core_view(inventory: Any) -> dict[str, Any]:
    """``target_core/1`` geometry, or the generator's own refusal code."""
    for candidate in inventory.candidates:
        if candidate.candidate_family == TARGET_FAMILY:
            return {
                "present": True,
                "e_lo": _round(candidate.e_lo),
                "e_hi": _round(candidate.e_hi),
                "centre_meV": _round(0.5 * (float(candidate.e_lo) + float(candidate.e_hi))),
                "halfwidth_meV": _round(0.5 * (float(candidate.e_hi) - float(candidate.e_lo))),
                "n_points": int(candidate.n_points),
                "refusal": None,
            }
    for refusal in inventory.refusals:
        if refusal.candidate_family == TARGET_FAMILY:
            return {"present": False, "refusal": refusal.reason_code}
    return {"present": False, "refusal": "target_core_absent_without_refusal"}


def _contained(core: Mapping[str, Any], domain: tuple[float, float]) -> bool | None:
    if not core.get("present"):
        return None
    return float(core["e_lo"]) >= domain[0] - 1e-9 and float(core["e_hi"]) <= domain[1] + 1e-9


# --------------------------------------------------------------------------- eligibility rebuild

def _replay_action_context(intent_template: Mapping[str, Any], outcome: Mapping[str, Any],
                           hkl: list[float]) -> dict[str, Any]:
    """The canonical same-Q production action this master would next receive.

    Every field is taken from the campaign's own retained intent for this q; only the action class
    and the fields that name *this* master (its Q and evidence revision) are set, because the
    replayed decision point is a production action at the master's own Q.
    """
    intent = {key: value for key, value in intent_template.items()
              if key != "scientific_action_fingerprint"}
    derived_class = intent.get("action_class") not in {"production", "confirm"}
    intent["action_class"] = "production"
    intent["hkl"] = [float(value) for value in hkl]
    intent["evidence_sink"] = "canonical_master"
    intent["window_family"] = "target"
    fingerprint = scientific_fingerprint_from_mapping(intent)
    provenance = {
        "action_class": "production",
        "scientific_action_fingerprint": fingerprint,
        "proposal_scientific_action_fingerprint": fingerprint,
        "acquisition_action_role": "production",
        "evidence_sink": "canonical_master",
        "driver_inserted": False,
        "insertion_purpose": None,
        "authoritative_evidence_revision": outcome.get("master_revision"),
        "hkl": [float(value) for value in hkl],
    }
    return {
        "intent": {**intent, "scientific_action_fingerprint": fingerprint},
        "provenance": provenance,
        "action_class_derived_from_quicklook": derived_class,
    }


def _scan_design_provenance(inventory: Any) -> dict[str, Any]:
    return {
        "inventory_fingerprint": inventory.content_hash(),
        "context_fingerprint": inventory.context.fingerprint,
        "selected_candidate_id": inventory.selected_candidate_id,
        "status": "passed",
        "recovery_state": None,
    }


# ------------------------------------------------------------------------------------- scoring

def _offset_policy_sets(authoring: Mapping[str, Any], scope: Mapping[str, str],
                        max_runtime_variants: int) -> dict[str, Any]:
    """One policy set per declared bin offset, derived from the card — never from constants."""
    result: dict[str, Any] = {}
    policies = authoring.get("policies") or []
    if len(policies) != 1 or len(policies[0].get("variants") or []) != 1:
        log.warning("offset probe skipped: card declares %d policies, not a single-variant policy",
                    len(policies))
        return result
    offsets = list(policies[0]["variants"][0]["parameters"].get("offsets") or ())
    for offset in offsets:
        derived = copy.deepcopy(dict(authoring))
        variant = derived["policies"][0]["variants"][0]
        variant["parameters"]["offsets"] = [offset]
        variant["variant_id"] = f"{variant['variant_id']}_offset_{str(offset).replace('.', 'p')}"
        result[str(offset)] = compile_card_eig_policy_set(
            derived, dict(scope), max_runtime_variants=max_runtime_variants)
    return result


def _oriented_blocks(score_set: Mapping[str, Any], target_id: str | None) -> list[float]:
    """Paired per-block deltas, re-oriented to ``target_core − legacy`` as the selector requires."""
    for comparison in score_set.get("pairwise_comparisons") or ():
        blocks = comparison.get("paired_block_deltas_per_executed_point_nats")
        if not blocks:
            continue
        if target_id not in {comparison.get("candidate_a_id"), comparison.get("candidate_b_id")}:
            continue
        sign = 1.0 if comparison.get("candidate_b_id") == target_id else -1.0
        return [_round(sign * float(value), 12) for value in blocks]
    return []


def _score_once(inventory: Any, predictive_artifact: Mapping[str, Any] | None,
                eligibility_input: EligibilityInput, policy_set: Any, scoring_config: Any,
                scope_fingerprint: str) -> dict[str, Any]:
    target_id = next((candidate.candidate_id for candidate in inventory.candidates
                      if candidate.candidate_family == TARGET_FAMILY), None)
    scoring = score_shadow_inventory(
        inventory, predictive_artifact=predictive_artifact,
        eligibility_input=eligibility_input, policy_set=policy_set,
        scoring_config=scoring_config, scope_kind="campaign_configuration",
        scope_fingerprint=scope_fingerprint)
    summary: dict[str, Any] = {
        "status": scoring.status,
        "reason_codes": list(scoring.reason_codes),
        "variants": [],
    }
    for variant in scoring.variants:
        delta = variant.target_core_minus_legacy
        entry = {
            "variant_id": variant.variant_id,
            "status": variant.status,
            "reason_codes": list(variant.reason_codes),
            "partition": dict(variant.compiled_partition or {}),
            "delta_information_per_executed_point_nats": None,
            "delta_information_per_executed_point_standard_error_nats": None,
            "delta_information_nats": None,
            "paired_blocks": [],
            "candidate_points": {},
        }
        score_set = variant.score_set or {}
        if delta:
            entry["delta_information_nats"] = _round(delta.get("delta_information_nats"), 9)
            entry["delta_information_per_executed_point_standard_error_nats"] = _round(
                delta.get("delta_information_per_executed_point_standard_error_nats"), 9)
            entry["delta_information_per_executed_point_nats"] = _round(
                delta.get("delta_information_per_executed_point_nats"), 9)
            entry["paired_blocks"] = _oriented_blocks(score_set, target_id)
        for candidate in score_set.get("candidate_scores") or ():
            entry["candidate_points"][candidate["candidate_id"]] = {
                "executed_point_count": candidate.get("executed_point_count"),
                "information_nats": _round(
                    candidate.get("conditional_target_qoi_information_nats"), 9),
                "information_standard_error_nats": _round(
                    candidate.get("conditional_target_qoi_information_standard_error_nats"), 9),
                "partition_information_nats": [
                    _round(value, 9)
                    for value in candidate.get("partition_information_nats") or ()],
            }
        summary["variants"].append(entry)
    return summary


def _partition_offsets(variant: Mapping[str, Any], legacy_id: str, target_id: str
                       ) -> tuple[O41PartitionOffsetEvidence, ...]:
    """Per-offset information the scoring run already retained, in the partition's offset order."""
    offsets = list((variant.get("partition") or {}).get("offsets") or ())
    points = variant.get("candidate_points") or {}
    legacy = (points.get(legacy_id) or {}).get("partition_information_nats") or []
    target = (points.get(target_id) or {}).get("partition_information_nats") or []
    if len(legacy) != len(offsets) or len(target) != len(offsets):
        return ()
    return tuple(
        O41PartitionOffsetEvidence(
            offset=float(offset), legacy_information_nats=float(legacy_value),
            target_core_information_nats=float(target_value))
        for offset, legacy_value, target_value in zip(offsets, legacy, target))


def _selector_role(profile: Any, inventory: Any, blocks: list[float],
                   eligibility_status: str,
                   partition_offsets: tuple[O41PartitionOffsetEvidence, ...] = ()
                   ) -> dict[str, Any]:
    """Run the real O41 selector over the replayed candidate pair and paired block deltas."""
    families = {candidate.candidate_family: candidate for candidate in inventory.candidates}
    legacy, target = families.get(LEGACY_FAMILY), families.get(TARGET_FAMILY)
    if legacy is None or target is None:
        return {"available": False, "reason": "candidate_family_missing"}

    def evidence(candidate: Any, role: str) -> O41CandidateEvidence:
        # The replay has no wire request, so the request identity is the content address of the
        # exact command and exposure this candidate would submit.
        command = candidate.grid.scan_command()
        request = hashlib.sha256(
            f"{command}|{candidate.ncount}".encode("utf-8")).hexdigest()
        return O41CandidateEvidence(
            role=role, candidate_id=candidate.candidate_id, request_fingerprint=request,
            grid_fingerprint=candidate.grid.fingerprint(candidate.ncount),
            e_lo=float(candidate.e_lo), e_hi=float(candidate.e_hi),
            executed_energies=tuple(float(value) for value in candidate.energies),
            ncount=int(candidate.ncount), scan_command=str(command))

    complete = bool(blocks) and bool(partition_offsets)
    score = O41ScoreEvidence(
        status="complete" if complete else "unavailable",
        orientation="target_core_minus_legacy",
        paired_block_deltas_per_executed_point_nats=tuple(blocks) if complete else (),
        partition_offsets=partition_offsets if complete else ())
    result = select_o41_authority(O41SelectorInput(
        profile=profile, eligibility_status=eligibility_status,
        applicability_status="applicable", legacy=evidence(legacy, "legacy"),
        target_core=evidence(target, "target_core"), score=score))
    payload = result.to_dict()
    payload["available"] = True
    return payload


# ------------------------------------------------------------------------------------ the replay

def replay(workspace: Path) -> dict[str, Any]:
    engine = _load_census_engine()
    calibration = load_calibration(CALIBRATION_PATH)
    inflation = calibration.inflation_for(None)

    card = Card.load(workspace / "card.policy.toml")
    policy_fingerprint = json_fingerprint(card.policy)
    goal = _read_json(workspace / "compact-goal.json")
    objective = spec_from_dict(
        goal, card_hypotheses=card.policy.hypotheses,
        card_eig_policy_set=card.policy.scan_window_eig_policy_set)
    eig_config = objective.scan_window_eig
    profile = validate_o41_authoritative_profile(eig_config)
    scope = current_objective_comparison_scope(objective)
    offset_policy_sets = _offset_policy_sets(
        card.policy.scan_window_eig_policy_set, scope,
        eig_config.scoring_config.max_runtime_variants)

    session = engine.SessionState.load(workspace / "session.json")
    card_state = engine.load_state(workspace / "card.state.json")
    evidence_read_failures: list[dict[str, str]] = []
    decision_points = _persisted_decision_points(workspace, evidence_read_failures)
    action_intents = _persisted_action_intents(workspace, evidence_read_failures)

    masters: list[dict[str, Any]] = []
    predictive_cache: dict = {}
    projection_cache: dict = {}
    for key, entry in sorted(session.scans.items()):
        if not key.startswith("master:"):
            continue
        for detector, outcome in sorted(entry.detectors.items()):
            if not (isinstance(outcome, Mapping) and outcome.get("pooled") is True):
                continue
            try:
                masters.append(_replay_master(
                    engine=engine, workspace=workspace, key=key, detector=str(detector),
                    entry=entry, outcome=outcome, session=session, card_state=card_state,
                    policy_fingerprint=policy_fingerprint, inflation=inflation,
                    decision_points=decision_points, action_intents=action_intents,
                    eig_config=eig_config, profile=profile, scope=scope,
                    offset_policy_sets=offset_policy_sets,
                    predictive_cache=predictive_cache, projection_cache=projection_cache))
            except Exception as error:  # noqa: BLE001 — a failed master is evidence, not a stop
                log.exception("master %s/%s replay failed: %s", key, detector, error)
                masters.append({
                    "master_session_key": key, "detector_id": str(detector),
                    "replay_error": f"{type(error).__name__}: {error}"})
    return {
        "contract": "isar.eig4_r1e_replay/1",
        "workspace": workspace.name,
        "evidence_read_failures": evidence_read_failures,
        "campaign_policy": {
            "policy_set_fingerprint": eig_config.policy_set.policy_set_fingerprint,
            "scoring_config_fingerprint": eig_config.scoring_config.scoring_config_fingerprint,
            "objective_scope_fingerprint": scope["fingerprint"],
            "partition_parameters": {
                key: (list(value) if isinstance(value, (list, tuple)) else value)
                for key, value in
                dict(eig_config.policy_set.policies[0].variants[0].parameters).items()},
            "simulation_count": eig_config.scoring_config.simulation_count,
            "block_count": eig_config.scoring_config.block_count,
            "calibration_inflation": inflation,
        },
        "masters": masters,
    }


def _replay_master(*, engine, workspace: Path, key: str, detector: str, entry: Any,
                   outcome: Mapping[str, Any], session: Any, card_state: Any,
                   policy_fingerprint: str | None, inflation: float,
                   decision_points: list[dict[str, Any]], action_intents: list[dict[str, Any]],
                   eig_config: Any, profile: Any, scope: Mapping[str, str],
                   offset_policy_sets: Mapping[str, Any], predictive_cache: dict,
                   projection_cache: dict) -> dict[str, Any]:
    outcome_view = engine._outcome_view(outcome, entry)
    hkl = [float(value) for value in (outcome_view.get("hkl") or ())]
    row: dict[str, Any] = {
        "master_session_key": key, "detector_id": detector, "hkl": hkl,
        "omega0_meV": _round(outcome.get("omega0")), "modality": outcome.get("modality"),
        "track_state": outcome.get("track_state"),
    }

    # --- authenticated predictive / projection / QoI evidence, exactly as the census reads them
    predictive = engine._predictive_details(
        workspace, outcome.get("predictive_ref"), predictive_cache)
    projection_raw = engine._projection_raw(
        workspace, outcome.get("target_projection_ref"), projection_cache)
    authority_source = (entry.provenance.get("target_projection_authority", {})
                        if isinstance(entry.provenance, Mapping) else {})
    authority_source = authority_source if isinstance(authority_source, Mapping) else {}
    expected_authority = {
        "action_role": authority_source.get("action_role"),
        "master_revision": authority_source.get("master_revision", outcome.get("master_revision")),
        "track_revision": outcome.get("track_revision"),
        "track_state": outcome.get("track_state"),
        "candidate_id": outcome.get("candidate_id"),
        "candidate_state": outcome.get("candidate_state"),
        "selected_candidate_index": outcome.get("selected_candidate_index"),
        "calibration_fingerprint": authority_source.get("calibration_fingerprint"),
        "policy_fingerprint": policy_fingerprint,
    }
    qoi_evidence = adapt_omega0_target_projection(
        predictive.get("_record"), projection_raw.get("record"),
        expected_authority=expected_authority)

    # --- persisted decision point for this q (the "before" column)
    same_hkl = _match_by_hkl(decision_points, hkl)
    production = [item for item in same_hkl if item["kind"] == "production"
                  and item["window_family"] == "target"]
    targets = [item for item in same_hkl if item["window_family"] == "target"]
    before = (production or targets or same_hkl or [None])[0]
    row["persisted_decision_point"] = None if before is None else {
        "record": before["record"], "kind": before["kind"],
        "eligibility_status": before["persisted_eligibility_status"],
        "scientific_codes": before["persisted_scientific_codes"],
        "observability_codes": before["persisted_observability_codes"],
        "shadow_status": before["persisted_shadow_status"],
    }
    if before is None:
        row["replay_error"] = "no_persisted_scan_design_context_at_this_q"
        return row

    before_context = ScanDesignContext.from_dict(before["context"])
    domain = (float(before_context.energy_search_meV[0]),
              float(before_context.energy_search_meV[1])) if (
                  before_context.energy_search_meV is not None) else (
                      float(before_context.declared_energy_lo or 0.0),
                      float(before_context.declared_energy_hi or 0.0))

    # --- same-Q basis and the "after" context
    same_q = _same_q_basis(outcome, inflation)
    row["same_q_basis"] = {
        "mean_meV": _round(same_q["mean_meV"]), "sigma_meV": _round(same_q["sigma_meV"]),
        "source": same_q["source"], "usable": same_q["usable"],
    }
    row["global_basis"] = {
        "mean_meV": _round(before_context.design_prediction_mean_meV),
        "sigma_meV": _round(before_context.design_prediction_sigma_meV),
        "resolution_fwhm_meV": _round(before_context.resolution_fwhm_meV),
        "authorized_domain": [domain[0], domain[1]],
    }

    before_inventory = design_scan_windows(before_context)
    before_core = _target_core_view(before_inventory)
    before_core["contained_in_authorized_domain"] = _contained(before_core, domain)
    row["target_core_global_basis"] = before_core

    if not same_q["usable"]:
        row["target_core_same_q_basis"] = {
            "present": False, "refusal": f"same_q_basis_unavailable:{same_q['source']}"}
        row["eligibility_replay"] = {"status": "not_attempted",
                                     "reason": "same_q_basis_unavailable"}
        return row

    fwhm = float(before_context.resolution_fwhm_meV)
    after_context = replace(
        before_context,
        design_prediction_mean_meV=float(same_q["mean_meV"]),
        design_prediction_sigma_meV=float(same_q["sigma_meV"]),
        target_band=(max(float(same_q["mean_meV"]), 0.0),
                     max(float(same_q["sigma_meV"]), 0.5 * fwhm)),
        scan_window_eig_mode="authoritative",
    )
    after_inventory = design_scan_windows(after_context)
    after_core = _target_core_view(after_inventory)
    after_core["contained_in_authorized_domain"] = _contained(after_core, domain)
    row["target_core_same_q_basis"] = after_core

    # --- eligibility under the current merged policy, at a replayed canonical production action
    intents = _match_by_hkl(action_intents, hkl)
    canonical = [item for item in intents if item.get("action_class") in {"production", "confirm"}]
    template = (canonical or intents or [None])[0]
    if template is None:
        row["eligibility_replay"] = {"status": "not_attempted",
                                     "reason": "no_retained_action_intent_at_this_q"}
        return row
    action = _replay_action_context(template, outcome, hkl)
    row["action_context"] = {
        "source_action_class": template.get("action_class"),
        "action_class_derived_from_quicklook": action["action_class_derived_from_quicklook"],
    }
    eligibility_input = EligibilityInput.from_current_contract(
        action_intent=action["intent"], action_provenance=action["provenance"],
        target_outcome=outcome_view, selection_record=outcome.get("fit_selection_semantics"),
        qoi_evidence=qoi_evidence, shadow_record=outcome.get("shadow_model_selection"),
        scan_design_provenance=_scan_design_provenance(after_inventory), scan_design_events=())
    eligibility = evaluate_eig_eligibility(eligibility_input)
    row["eligibility_replay"] = {
        "status": eligibility.status,
        "scientific_codes": list(eligibility.scientific_codes),
        "observability_codes": list(eligibility.observability_codes),
    }
    if eligibility.status != "eligible":
        return row

    # --- the full legacy versus target-core comparison under the campaign's own policy
    primary = _score_once(
        after_inventory, predictive.get("_record"), eligibility_input,
        eig_config.policy_set, eig_config.scoring_config, scope["fingerprint"])
    row["score_primary"] = primary
    families = {candidate.candidate_family: candidate for candidate in after_inventory.candidates}
    legacy_id = getattr(families.get(LEGACY_FAMILY), "candidate_id", "")
    target_id = getattr(families.get(TARGET_FAMILY), "candidate_id", "")
    complete = [variant for variant in primary["variants"] if variant["status"] == "complete"]
    if complete:
        row["selector"] = _selector_role(
            profile, after_inventory, complete[0]["paired_blocks"], eligibility.status,
            _partition_offsets(complete[0], legacy_id, target_id))

    offsets: dict[str, Any] = {}
    for offset, policy_set in offset_policy_sets.items():
        probe = _score_once(
            after_inventory, predictive.get("_record"), eligibility_input,
            policy_set, eig_config.scoring_config, scope["fingerprint"])
        variants = [variant for variant in probe["variants"] if variant["status"] == "complete"]
        selector = (_selector_role(profile, after_inventory, variants[0]["paired_blocks"],
                                   eligibility.status,
                                   _partition_offsets(variants[0], legacy_id, target_id))
                    if variants else {"available": False})
        offsets[offset] = {"score": probe, "selector": selector}
    row["score_offset_probe"] = offsets
    roles = {offset: value["selector"].get("physical_role")
             for offset, value in offsets.items() if value["selector"].get("available")}
    # A single-offset partition can place the whole predictive inside one cell; both candidates then
    # carry exactly zero information and the comparison, while "complete", distinguishes nothing.
    zero_offsets = sorted(
        offset for offset, value in offsets.items()
        if any(item["status"] == "complete"
               and all(float(point["information_nats"] or 0.0) == 0.0
                       for point in item["candidate_points"].values())
               for item in value["score"]["variants"]))
    row["offset_role_stability"] = {
        "roles": roles,
        "stable": (len(set(roles.values())) <= 1) if len(roles) == len(offsets) else None,
        "zero_information_offsets": zero_offsets,
    }
    return row


# ------------------------------------------------------------------------------------- reporting

def _table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    body = ["  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
            for row in rows]
    rule = "-" * len(line)
    return "\n".join([title, rule, line, rule, *body, ""])


def _codes(values: Any) -> str:
    items = list(values or ())
    return ",".join(items) if items else "-"


def _short(key: str) -> str:
    return key.replace("master:tavi_", "").replace("_200.0", "")


def render(report: Mapping[str, Any]) -> str:
    masters = [row for row in report["masters"] if "replay_error" not in row]
    failed = [row for row in report["masters"] if "replay_error" in row]

    eligibility_rows = []
    for row in report["masters"]:
        before = row.get("persisted_decision_point") or {}
        after = row.get("eligibility_replay") or {}
        eligibility_rows.append([
            _short(row["master_session_key"]),
            str(before.get("eligibility_status") or "-"),
            _codes(before.get("scientific_codes")),
            str(after.get("status") or row.get("replay_error", "-")),
            _codes(after.get("scientific_codes")),
            _codes(after.get("observability_codes")),
        ])

    geometry_rows = []
    for row in masters:
        glob = row.get("global_basis") or {}
        same = row.get("same_q_basis") or {}
        before = row.get("target_core_global_basis") or {}
        after = row.get("target_core_same_q_basis") or {}

        def window(core: Mapping[str, Any]) -> str:
            if not core.get("present"):
                return f"REFUSED:{core.get('refusal')}"
            return (f"[{core['e_lo']:.3f},{core['e_hi']:.3f}] "
                    f"c={core['centre_meV']:.3f} hw={core['halfwidth_meV']:.3f} "
                    f"n={core['n_points']}")

        geometry_rows.append([
            _short(row["master_session_key"]),
            f"{glob.get('mean_meV')}/{glob.get('sigma_meV')}",
            f"{same.get('mean_meV')}/{same.get('sigma_meV')}",
            window(before), str(before.get("contained_in_authorized_domain")),
            window(after), str(after.get("contained_in_authorized_domain")),
        ])

    score_rows = []
    for row in masters:
        primary = row.get("score_primary")
        if primary is None:
            continue
        variants = [item for item in primary["variants"] if item["status"] == "complete"]
        selector = row.get("selector") or {}
        stability = row.get("offset_role_stability") or {}
        if not variants:
            codes = _codes(primary["variants"][0]["reason_codes"] if primary["variants"]
                           else primary["reason_codes"])
            score_rows.append([_short(row["master_session_key"]), primary["status"], codes,
                               "-", "-", "-", "-", "-", "-"])
            continue
        variant = variants[0]
        points = variant["candidate_points"]
        score_rows.append([
            _short(row["master_session_key"]), primary["status"], "-",
            "/".join(f"{item['information_nats']:.6f}" for item in points.values()),
            f"{variant['delta_information_per_executed_point_nats']}",
            f"{variant['delta_information_per_executed_point_standard_error_nats']}",
            "/".join(str(item["executed_point_count"]) for item in points.values()),
            str(selector.get("physical_role", "-")),
            f"{stability.get('roles')} stable={stability.get('stable')}"
            + (" ZERO-INFO-OFFSET" if stability.get("zero_information_offsets") else ""),
        ])

    parts = [
        _table("ELIGIBILITY  (persisted decision point -> current merged policy)",
               ["master", "before", "before_sci", "after", "after_sci", "after_obs"],
               eligibility_rows),
        _table("GEOMETRY  target_core/1 (global omega(q) basis -> same-Q basis)",
               ["master", "global mean/sig", "same-Q mean/sig", "core (global)", "in dom",
                "core (same-Q)", "in dom"], geometry_rows),
        _table("SCORE  legacy_uniform/1 versus target_core/1",
               ["master", "status", "codes", "info nats L/T", "delta/pt nats", "se/pt nats",
                "pts L/T", "role", "offset probe"], score_rows),
    ]
    if failed:
        parts.append("REPLAY FAILURES\n" + "\n".join(
            f"  {_short(row['master_session_key'])}: {row['replay_error']}" for row in failed))
    unreadable = list(report.get("evidence_read_failures") or ())
    if unreadable:
        parts.append("UNREADABLE CAMPAIGN EVIDENCE (a missing match below may be this, not absence)\n"
                     + "\n".join(f"  {item['kind']} {item['record']}: {item['error']}"
                                 for item in unreadable))

    def complete_variants(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        return [item for item in (row.get("score_primary") or {}).get("variants", [])
                if item["status"] == "complete"]

    scored = [row for row in masters if complete_variants(row)]
    nonzero = [row for row in scored
               if any(any(float(point["information_nats"] or 0.0) > 0.0
                          for point in item["candidate_points"].values())
                      for item in complete_variants(row))]
    stable = [row for row in scored if (row.get("offset_role_stability") or {}).get("stable")]
    clean = [row for row in nonzero
             if row in stable
             and not (row.get("offset_role_stability") or {}).get("zero_information_offsets")]
    verdict = "PASS" if clean else "PASS (QUALIFIED)" if (nonzero and stable) else "FAIL"
    lines = [
        "GATE VERDICT: " + verdict,
        f"  masters replayed                        : {len(masters)}",
        "  eligible under merged policy            : "
        + str(sum(1 for row in masters
                  if (row.get("eligibility_replay") or {}).get("status") == "eligible")),
        f"  complete score comparisons              : {len(scored)}",
        f"  nonzero-information comparisons         : {len(nonzero)}",
        f"  offset-stable selected roles            : {len(stable)}",
        f"  nonzero under BOTH offsets separately   : {len(clean)}",
    ]
    flipped = [row for row in scored
               if (row.get("offset_role_stability") or {}).get("stable") is False]
    if flipped:
        lines.append(
            "  OFFSET-INSTABILITY: the selected role flips with the bin offset for "
            + ", ".join(f"{_short(row['master_session_key'])} "
                        f"{(row['offset_role_stability'] or {}).get('roles')}"
                        for row in flipped)
            + ".")
    if verdict == "FAIL":
        lines.append("  *** NO real master produces a complete, nonzero, offset-stable "
                     "comparison. R2 IS BLOCKED. ***")
    elif verdict == "PASS (QUALIFIED)":
        degenerate = sorted(
            f"{_short(row['master_session_key'])}"
            f"@{(row['offset_role_stability'] or {}).get('zero_information_offsets')}"
            for row in nonzero
            if (row.get("offset_role_stability") or {}).get("zero_information_offsets"))
        lines.append(
            "  At least one real master produces a complete, nonzero comparison whose selected "
            "role does not flip with the bin offset.\n"
            "  QUALIFICATION: with a SINGLE offset the predictive can fall inside one partition "
            "cell and both candidates score exactly zero: " + ", ".join(degenerate) + ".\n"
            "  The role does not flip only because a zero comparison is never decisive: the "
            "combined two-offset partition is what carries the signal.")
    else:
        lines.append("  At least one real master produces a complete, nonzero, offset-stable "
                     "comparison under every offset separately.")
    parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--workspace", type=Path, required=True,
                        help="campaign workspace root (read-only)")
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for replay-report.json and replay-report.txt")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")

    workspace = args.workspace.resolve()
    output = args.output.resolve()
    if output == workspace or workspace in output.parents:
        raise SystemExit("refusing to write inside the evidence workspace")
    output.mkdir(parents=True, exist_ok=True)

    report = replay(workspace)
    text = render(report)
    (output / "replay-report.json").write_text(
        json.dumps(report, indent=1, sort_keys=True, default=str), encoding="utf-8")
    (output / "replay-report.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {output / 'replay-report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
