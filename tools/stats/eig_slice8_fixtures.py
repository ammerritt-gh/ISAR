"""Build the six frozen EIG-4 Slice 8 fixture inputs through current ISAR contracts.

This is a bounded evidence harness, not a campaign engine.  It constructs three authenticated
eligible producer chains with the current predictive, projection, scorer, inventory, and realized
comparison implementations; the remaining three cases are authentic mutations at the existing
eligibility seam.  Development output is disposable.  Final output is consumed by
``eig_slice8_cases.py`` only after the exact physical parameters have been frozen.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
from unittest.mock import patch

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from isar.analyze.context import ScanContext
from isar.analyze.shadow_selection import unavailable_shadow_selection
from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.inventory import scientific_fingerprint_from_mapping
from isar.drive.acquisition.realized_comparison import (
    build_realized_comparison,
    construct_realized_pair,
    load_scan_design_realized_comparison,
    persist_realized_comparison,
)
from isar.drive.acquisition.scan_design import ScanDesignContext, design_scan_windows
from isar.drive.instrument.tavi_client import TaviClient
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import run_isar
from isar.drive.isar_io.scan_writer import save_scan_json
from isar.eig import EligibilityInput, adapt_omega0_target_projection
from isar.eig.shadow_inventory import score_shadow_inventory
from isar.schema.action_identity import canonical_json
from isar.schema.card import CardState
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowShadowScoringConfig,
)
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    authority_scopes,
    semantics_fingerprint,
    validate_for_write,
)
from isar.session import driver, predictive_artifact
from isar.session.state import SessionState
from isar.session.predictive_artifact import build_target_projection
from tools.stats import eig_slice8_gate as gate


log = logging.getLogger(__name__)

DEVELOPMENT_SPEC_CONTRACT = "isar.eig4_slice8_fixture_development/o36.8"
CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8"


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = canonical_json(value).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _fit_semantics(label: str, *, constrained: bool = False) -> dict[str, Any]:
    data = semantics_fingerprint("isar.eig4_slice8.fixture.data", {"label": label})
    model = semantics_fingerprint("isar.eig4_slice8.fixture.model", {"label": label})
    effective_constraints = ([{
        "parameter": "phonon.Gamma", "constraint_kind": "hard",
        "origin": "prespecified_scientific", "value": 0.3,
    }] if constrained else [])
    constraints = semantics_fingerprint(
        "isar.eig4_slice8.fixture.constraints", effective_constraints)
    comparison = semantics_fingerprint("isar.eig4_slice8.fixture.comparison", {
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "estimator_kind": "data_likelihood_mle",
    })
    return validate_for_write({
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": "data_likelihood_mle",
        "fit_kind": "unconstrained",
        "effective_constraints": effective_constraints,
        "aggregate_constraint_origin": "prespecified_scientific" if constrained else "none",
        "selection_eligibility": (
            "ineligible_unapproved_conditioning" if constrained else "eligible"),
        "selection_ineligibility_reasons": (
            ["effective_hard_constraint"] if constrained else []),
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "comparison_context_fingerprint": comparison,
        "conditioning_fingerprint": comparison,
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }, FIT_SELECTION_SCHEMA)


def _selection(label: str, *, constrained: bool = False) -> dict[str, Any]:
    semantics = _fit_semantics(label, constrained=constrained)
    result = SimpleNamespace(
        label=label, omega0=3.0, fit_semantics=semantics,
        within_model={"omega0_samples": [3.0, 3.1]},
    )
    score = SimpleNamespace(
        label=label, cost=10.0, k=4, aic=18.0, weight=1.0, survives=True,
        fit_semantics=semantics,
    )
    route = {
        "route": "full_two_stage", "labels_before_guideline": [label],
        "guideline_structure_filter": [label], "offered_labels": [label],
        "scored_labels": [label],
    }
    return driver._build_selection_semantics(
        [result], SimpleNamespace(scores=[score], pooled=object()), route,
        error_attachment_labels=[label])


def _neutral_shadow() -> dict[str, Any]:
    return unavailable_shadow_selection(
        "shadow_skipped_soft_deadline", stage="execution_policy",
        selection_context_fingerprint=semantics_fingerprint(
            "isar.eig4_slice8.fixture.shadow", {}),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None, legacy_selected_label="phonon",
        legacy_model_weights={"phonon": 1.0}, runtime_increment_seconds=0.0,
        shadow_execution_kind="skipped")


def _eligibility(qoi: Any, selection: Mapping[str, Any], *, q: tuple[float, float, float],
                 target_energy: float, modality: str = "fold") -> EligibilityInput:
    intent = {
        "action_class": "production", "hkl": list(q),
        "energy_lo": -2.0, "energy_hi": 7.0, "window_family": "target",
        "target_branch_role": None, "rival_branch_role": None,
        "evidence_basis_fingerprint": "slice8-basis",
        "hypotheses_fingerprint": "slice8-hypotheses",
        "campaign_slice_fingerprint": "slice8-campaign",
        "evidence_sink": "canonical_master",
    }
    fingerprint = scientific_fingerprint_from_mapping(intent)
    intent["scientific_action_fingerprint"] = fingerprint
    provenance = {
        "action_class": "production", "scientific_action_fingerprint": fingerprint,
        "proposal_scientific_action_fingerprint": fingerprint,
        "acquisition_action_role": "production", "evidence_sink": "canonical_master",
        "driver_inserted": False, "insertion_purpose": None,
        "authoritative_evidence_revision": "slice8-master-r1", "hkl": list(q),
    }
    target = {
        "omega0": target_energy, "target_delivery_eligible": True,
        "track_revision": 1, "track_state": "confirmed", "candidate_state": "confirmed",
        "candidate_id": "slice8-target", "selected_candidate_index": 0,
        "hkl": list(q), "pooled": True, "pool_consistent": True,
        "modality": modality, "master_revision": "slice8-master-r1",
    }
    return EligibilityInput.from_current_contract(
        action_intent=intent, action_provenance=provenance, target_outcome=target,
        selection_record=selection, qoi_evidence=qoi, shadow_record=_neutral_shadow())


def _predictive_bundle(*, q: tuple[float, float, float], draw_energies: Sequence[float],
                       target_energy: float, amplitude: float,
                       constrained: bool = False) -> tuple[dict[str, Any], EligibilityInput]:
    if len(draw_energies) < 2 or any(not math.isfinite(float(item)) for item in draw_energies):
        raise ValueError("fixture draw energies must contain at least two finite values")
    label = "phonon-constrained" if constrained else "phonon"
    selection = _selection(label, constrained=constrained)
    manifest = {
        "schema": "isar.model_manifest/1",
        "components": [
            {"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
            {"type": "constant_background", "prefix": "bg"},
        ],
        "parameters": {
            "phonon.Omega_d": target_energy, "phonon.Gamma": 0.3,
            "phonon.A": amplitude, "bg.BG": 4.0,
        },
    }
    draw_ids = [f"slice8-draw-{index:02d}" for index in range(len(draw_energies))]

    def manifest_of(_result: Any, _by_hyp: Any) -> Mapping[str, Any]:
        return manifest

    def draws_of(_result: Any, **_kwargs: Any) -> tuple[Any, ...]:
        rows = np.asarray([
            [float(energy), 0.3, float(amplitude), 4.0] for energy in draw_energies
        ])
        return ("phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"), rows, (
            "parametric_bootstrap"), {
                "draw_weighting": "equal_weight_resampled", "draw_weights": None,
                "draw_ids": draw_ids, "source_draw_count": len(draw_ids),
                "retained_draw_count": len(draw_ids), "dropped_draws": [],
                "draw_mass_retained": 1.0, "draw_cap": 64,
                "thinned_from_draw_count": len(draw_ids),
            }

    context = ScanContext(
        x=np.linspace(-2.0, 7.0, 10), counts=np.zeros(10), mask=np.ones(10, bool),
        T=100.0, center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1,
        instrument="IN8", energy_status="absolute", exposure_scale=np.ones(10),
        exposure_reference=1.0)
    result = SimpleNamespace(
        label=label, omega0=target_energy, fit_semantics=_fit_semantics(
            label, constrained=constrained),
        within_model={"omega0_samples": list(draw_energies)})
    score = SimpleNamespace(
        label=label, cost=10.0, k=4, aic=18.0, weight=1.0, survives=True,
        fit_semantics=result.fit_semantics)
    with (patch.object(predictive_artifact, "model_manifest_of", manifest_of),
          patch.object(predictive_artifact, "bounded_parameter_draws", draws_of)):
        artifact = predictive_artifact.build_master_predictive(
            SimpleNamespace(omega0=target_energy), [score], {label: result},
            {label: object()}, context, seed=11, n_members=1,
            fit_selection_semantics=selection)
    target = {
        "omega0": target_energy, "target_delivery_eligible": True,
        "track_revision": 1, "track_state": "confirmed", "candidate_state": "confirmed",
        "candidate_id": "slice8-target", "selected_candidate_index": 0,
    }
    projection = build_target_projection(
        artifact, target, master_revision="slice8-master-r1",
        calibration_fingerprint="slice8-cal-r1", policy_fingerprint="slice8-policy-r1",
        action_role="certification")
    qoi = adapt_omega0_target_projection(
        artifact, projection, expected_authority=projection["authority"])
    return artifact, _eligibility(
        qoi, selection, q=q, target_energy=target_energy)


def _context(*, q: tuple[float, float, float], target_energy: float,
             prediction_sigma: float, ncount: int, survey: bool) -> ScanDesignContext:
    return ScanDesignContext(
        scientific_action_fingerprint=semantics_fingerprint(
            "isar.eig4_slice8.fixture.action", {"q": q, "energy": target_energy}),
        hkl=q, q=abs(q[0] - round(q[0])), kind="production",
        reason="frozen Slice 8 target confirmation", window_family="target",
        declared_energy_lo=None, declared_energy_hi=None,
        target_prediction_mean_meV=target_energy,
        target_prediction_sigma_meV=prediction_sigma,
        design_prediction_mean_meV=target_energy,
        design_prediction_sigma_meV=prediction_sigma,
        resolution_fwhm_meV=0.8, survey=survey, energy_search_meV=(-2.0, 7.0),
        target_band=(target_energy, prediction_sigma), rival_band=None, widen_factor=1.0,
        positive_only=False, policy_max_points=100, runtime_max_points=200,
        tolerance_meV=0.5, quicklook_ncount=ncount, production_ncount=ncount,
        identity_detection_ncount=ncount, dynamic_ncount=False, tolerance_margin=0.8,
        ncount_growth_cap=4.0, max_ncount_per_scan=max(1_000_000, ncount),
        diagnostic_halfwidth_meV=None, certification_halfwidth_meV=None,
        effective_exposure=None, submitted_exposure=None, production_scans=1,
        last_submitted_ncount=ncount, runtime_max_neutrons_per_point=None,
        runtime_queue_neutron_budget=None, budget_kind="neutrons", budget_amount=1e12,
        budget_spent=0.0, scan_window_eig_mode="shadow")


def _policy(scope: str) -> QoIDistinctionPolicySet:
    variant = QoIDistinctionVariant(gate.PRIMARY_VARIANT_ID, gate.PRIMARY_PARAMETERS)
    policy = QoIDistinctionPolicy(
        gate.PRIMARY_POLICY_ID, gate.PRIMARY_POLICY_KIND,
        {"id": "excitation.energy", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": scope,
         "annotation": {"campaign_name": "EIG-4 Slice 8",
                        "experiment_description": "bounded shadow gate", "date": "2026-08-04"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "frozen two-offset Slice 8 distinction scale"}, (variant,))
    return QoIDistinctionPolicySet((policy,))


def _persist_eligible_case(root: Path, *, case_id: str, q: tuple[float, float, float],
                           draw_energies: Sequence[float], target_energy: float,
                           prediction_sigma: float, amplitude: float, ncount: int,
                           survey: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact, eligibility = _predictive_bundle(
        q=q, draw_energies=draw_energies, target_energy=target_energy, amplitude=amplitude)
    inventory = design_scan_windows(_context(
        q=q, target_energy=target_energy, prediction_sigma=prediction_sigma,
        ncount=ncount, survey=survey))
    scope = semantics_fingerprint("isar.eig4_slice8.fixture.scope", {"case_id": case_id})
    scoring = score_shadow_inventory(
        inventory, predictive_artifact=artifact, eligibility_input=eligibility,
        policy_set=_policy(scope), scoring_config=ScanWindowShadowScoringConfig(),
        scope_kind="campaign_configuration", scope_fingerprint=scope)
    if scoring.status != "complete":
        raise gate.GateEvidenceError(
            f"{case_id} scoring is {scoring.status}: {scoring.reason_codes}")
    inventory = inventory.with_shadow_scoring(scoring)
    digest = inventory.content_hash()
    inventory_path = root / "report" / "scan-design" / f"{digest}.json"
    _write_json(inventory_path, inventory.to_full_dict())
    inventory_ref = {
        "kind": "scan_design_inventory", "sha256": digest,
        "path": f"report/scan-design/{digest}.json",
    }
    legacy = inventory.selected_candidate
    proposal_fingerprint = semantics_fingerprint(
        "isar.eig4_slice8.fixture.legacy_request", {"case_id": case_id})
    proposal = ScanProposal(
        h=q[0], k=q[1], l=q[2], e_lo=legacy.e_lo, e_hi=legacy.e_hi,
        e_step=legacy.e_step, ncount=legacy.ncount, kind="production",
        reason="frozen Slice 8 legacy control", e_pred=target_energy,
        fwhm_dE=0.8, n_points=legacy.n_points,
        meta={"proposal_fingerprint": proposal_fingerprint,
              "scientific_action_fingerprint": inventory.context.scientific_action_fingerprint},
        scan_design_inventory=inventory)

    def finalize(target: ScanProposal, *_args: Any, **_kwargs: Any) -> None:
        target.meta["proposal_fingerprint"] = semantics_fingerprint(
            "isar.eig4_slice8.fixture.target_request", {"case_id": case_id})

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {}, it=1,
        finalizer=finalize)
    comparison = build_realized_comparison(
        inventory, inventory_ref, pair, remaining_seconds=5.0, rescorer=None)
    comparison_ref = persist_realized_comparison(root, comparison)
    primary = next(item for item in comparison.variants
                   if item.variant_id == gate.PRIMARY_VARIANT_ID)
    score_set, legacy_id, target_id = gate._score_set_payload(
        comparison, inventory, primary)
    blocks = list(gate._paired_blocks(score_set, legacy_id, target_id))
    raw = {
        "case_id": case_id,
        "eligibility_source": gate.serialize_eligibility_input(eligibility),
        "source_workspace": str(root.resolve()),
        "realized_comparison_ref": comparison_ref,
        "applicability": "applicable", "executed_grid_status": "matched",
        "submission_provenance": {
            "scan_design_realized_comparison_fingerprint": (
                comparison.realized_comparison_fingerprint),
            "scan_design_realized_selected_request_fingerprint": (
                comparison.selected_request_fingerprint),
            "scan_design_realized_selected_grid_fingerprint": (
                comparison.selected_grid_fingerprint),
        },
        "analysis_outcome": {},
    }
    diagnostics = {
        "blocks": blocks,
        "legacy_points": pair.legacy.executed_point_count,
        "target_points": pair.target_core.executed_point_count,
        "legacy_window": [pair.legacy.e_lo, pair.legacy.e_hi],
        "target_window": [pair.target_core.e_lo, pair.target_core.e_hi],
    }
    return raw, diagnostics


def _fixture_definition(case: Mapping[str, Any], physical: Mapping[str, Any],
                        *, source_kind: str = "deterministic_synthetic") -> dict[str, Any]:
    return {
        "case_id": case["case_id"], "source_kind": source_kind,
        "generation_recipe": {
            "tool": "tools/stats/eig_slice8_fixtures.py",
            "case_generation_seed": case["case_generation_seed"],
        },
        "physical_parameters": dict(physical),
        "ordinary_analysis_acceptance": {
            "ordinary_pipeline_required": case["case_id"] == "s8-c01-strong-confirmed",
            "eligibility_status": case["expected_eligibility"],
        },
        "fixture_rationale": (
            "parameters selected from the declared physical regime before final gate evaluation; "
            "a contrary frozen disposition fails the gate and does not trigger retuning"),
        "truth_use": "post_run_evaluation_only",
    }


def _tree_fingerprint(root: Path) -> str:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return semantics_fingerprint("isar.eig4_slice8.fixture.workspace_tree", rows)


def _init_tavi_workspace(root: Path) -> Path:
    from isar.parsers import tavi as tavi_mod

    root.mkdir(parents=True, exist_ok=True)
    (root / "card.policy.toml").write_text(
        Path(tavi_mod._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    # This is an isolated copy of an already-running campaign, not a blank campaign.  Seed only
    # ordinary, clean fold anchors so the normal branch-assignment path can decide whether the
    # target-core confirmation rejoins the declared acoustic target.  No EIG output enters state.
    prior = []
    for index, (h, energy) in enumerate(((2.0, 0.2), (2.1, 0.94), (2.2, 1.85))):
        prior.append({
            "scan_id": f"slice8-seed:{index}", "sample": "tavi", "T": 200.0,
            "t_bin": 200.0, "q": [h, 0.0, 0.0], "omega0": energy,
            "branch_id": "acoustic", "weight": 1.0, "modality": "fold",
            "verdict": "resolved", "gamma": 0.25, "fit_kind": "unconstrained",
            "intensity": {
                "value": 0.2, "gauss_fwhm": 0.8, "lorentz_fwhm": 0.1,
                "exposure": 1.0e8, "counting_mode": "monitor", "instrument": "tavi",
            },
        })
    state = CardState(dispersion_prior=prior)
    driver._store_branch_beliefs(state, t_round=5.0, period=2.0)
    _write_json(root / "card.state.json", state.to_dict())
    SessionState().save(root / "session.json")
    return root


def run_live_positive_analysis(development: Mapping[str, Any], output: Path) -> dict[str, Any]:
    strong = next(item for item in development["cases"]
                  if item["case_id"] == "s8-c01-strong-confirmed")
    source_root = Path(strong["source_workspace"])
    before = _tree_fingerprint(source_root)
    comparison = load_scan_design_realized_comparison(
        strong["realized_comparison_ref"], source_root)
    target = comparison.realized_pair.target_core
    if not hasattr(target, "scan_command"):
        raise gate.GateEvidenceError("strong case has no target-core request to evaluate")
    client = TaviClient()
    schema = client.get_schema()
    if "deterministic" not in schema.get("engines", []):
        raise gate.GateEvidenceError("live TAVI does not advertise the deterministic engine")
    background = {
        "catalog_version": schema["background"]["catalog_version"],
        "enabled": False,
        "sources": {},
    }
    body = {
        "parameters": {
            "H": target.hkl[0], "K": target.hkl[1], "L": target.hkl[2],
            "scan_command1": target.scan_command, "number_neutrons": target.ncount,
            "sample": "Al_phonon_DFT",
        },
        "engine": "deterministic", "seed": 8040801, "isolated": True,
        "background": background,
    }
    job_id = client.submit_scan(body)["job_id"]
    snapshot = client.wait_scan(job_id, timeout=120.0, poll_wait=5.0)
    if snapshot.get("state") != "done":
        raise gate.GateEvidenceError(
            f"live target-core scan ended {snapshot.get('state')}: {snapshot.get('error')}")
    data = client.get_scan_data(job_id)
    workspace = _init_tavi_workspace(output / "live-positive-workspace")
    data_dir = workspace / "data"
    saved = save_scan_json(data, data_dir)
    process = run_isar(workspace, [data_dir])
    rows = read_results_csv(workspace) if process.returncode == 0 else []
    state = read_card_state(workspace) if process.returncode == 0 else {}
    fitted = [row for row in rows if row.get("status") == "fitted"]
    usable = [row for row in rows if row.get("omega0") not in (None, "")
              and row.get("disposition") not in {"declined", "held"}]
    pooled = [row for row in rows if str(row.get("pooled", "")).lower() in {"true", "1"}]
    after = _tree_fingerprint(source_root)
    outcome = {
        "normal_ingestion": saved.exists() and bool(rows),
        "normal_reduction": process.returncode == 0 and bool(rows),
        "normal_fit": bool(fitted),
        "pooled_master_updated": bool(pooled or state.get("dispersion_prior")),
        "usable_target_delivery": bool(usable),
        "no_eig_specific_fit_route": True,
        "original_workspace_unchanged": after == before,
    }
    row_evidence = [{
        "scan_id": row.get("scan_id"), "status": row.get("status"),
        "verdict": row.get("verdict"), "disposition": row.get("disposition"),
        "omega0": row.get("omega0"), "branch_id": row.get("branch_id"),
        "pooled": row.get("pooled"),
        "target_delivery_eligible": row.get("target_delivery_eligible"),
    } for row in rows]
    evidence = {
        "job_id": job_id, "request": body, "returncode": process.returncode,
        "result_row_count": len(rows), "fitted_row_count": len(fitted),
        "usable_row_count": len(usable), "pooled_row_count": len(pooled),
        "dispersion_prior_count": len(state.get("dispersion_prior", [])),
        "ordinary_analysis_command": "isar run",
        "input_scan_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(),
        "source_workspace_fingerprint_before": before,
        "source_workspace_fingerprint_after": after,
        "isolated_workspace_fingerprint": _tree_fingerprint(workspace),
        "result_rows": row_evidence,
        "analysis_outcome": outcome,
    }
    _write_json(output / "positive-analysis.json", evidence)
    return evidence


def build_development_inputs(output: Path, parameters: Mapping[str, Any]) -> dict[str, Any]:
    if parameters.get("contract") != DEVELOPMENT_SPEC_CONTRACT:
        raise gate.GateEvidenceError("fixture development specification is unsupported")
    required = {"contract", "eligible_cases", "positive_analysis_outcome"}
    if set(parameters) != required or not isinstance(parameters["eligible_cases"], Mapping):
        raise gate.GateEvidenceError("fixture development specification fields are invalid")
    case_by_id = {item["case_id"]: item for item in gate.CASE_DEFINITIONS}
    raw_cases: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, Any] = {}
    fixture_rows = []
    eligible_inputs: dict[str, EligibilityInput] = {}
    for case_id in (
            "s8-c01-strong-confirmed", "s8-c02-weak-unresolved-confirmed",
            "s8-c03-near-elastic-selection-time"):
        physical = parameters["eligible_cases"].get(case_id)
        if not isinstance(physical, Mapping):
            raise gate.GateEvidenceError(f"missing physical definition for {case_id}")
        q = tuple(float(item) for item in physical["q"])
        raw, detail = _persist_eligible_case(
            output / "source-workspaces" / case_id, case_id=case_id, q=q,
            draw_energies=physical["draw_energies_meV"],
            target_energy=float(physical["target_energy_meV"]),
            prediction_sigma=float(physical["prediction_sigma_meV"]),
            amplitude=float(physical["amplitude"]), ncount=int(physical["ncount"]),
            survey=bool(physical["survey"]))
        raw_cases[case_id] = raw
        diagnostics[case_id] = detail
        _, eligible_inputs[case_id] = _predictive_bundle(
            q=q, draw_energies=physical["draw_energies_meV"],
            target_energy=float(physical["target_energy_meV"]),
            amplitude=float(physical["amplitude"]))
        source_kind = (
            "deterministic_tavi_with_synthetic_predictive_fixture"
            if case_id == "s8-c01-strong-confirmed" else "deterministic_synthetic")
        fixture_rows.append(_fixture_definition(
            case_by_id[case_id], physical, source_kind=source_kind))

    strong = eligible_inputs["s8-c01-strong-confirmed"]
    forked = EligibilityInput.from_current_contract(
        action_intent=strong.action_intent, action_provenance=strong.action_provenance,
        target_outcome={**dict(strong.target_outcome), "modality": "fork"},
        selection_record=strong.selection_record, qoi_evidence=strong.qoi_evidence,
        shadow_record=strong.shadow_record)
    mismatched_selection = _selection("alternate-phonon")
    mismatched = EligibilityInput.from_current_contract(
        action_intent=strong.action_intent, action_provenance=strong.action_provenance,
        target_outcome=strong.target_outcome,
        selection_record=mismatched_selection,
        qoi_evidence=strong.qoi_evidence, shadow_record=strong.shadow_record)
    strong_parameters = parameters["eligible_cases"]["s8-c01-strong-confirmed"]
    _, constrained = _predictive_bundle(
        q=tuple(float(item) for item in strong_parameters["q"]),
        draw_energies=strong_parameters["draw_energies_meV"],
        target_energy=float(strong_parameters["target_energy_meV"]),
        amplitude=float(strong_parameters["amplitude"]), constrained=True)
    excluded = {
        "s8-c04-forked-target": forked,
        "s8-c05-projection-join-mismatch": mismatched,
        "s8-c06-constrained-selection": constrained,
    }
    for case_id, source in excluded.items():
        raw_cases[case_id] = {"case_id": case_id,
                              "eligibility_source": gate.serialize_eligibility_input(source)}
        physical = {"authenticated_mutation": case_by_id[case_id]["required_eligibility_code"],
                    "source_case_id": "s8-c01-strong-confirmed"}
        fixture_rows.append(_fixture_definition(case_by_id[case_id], physical))

    fixture_rows.sort(key=lambda item: list(case_by_id).index(item["case_id"]))
    fixture_definitions: dict[str, Any] = {
        "contract": gate.FIXTURE_DEFINITIONS_CONTRACT, "cases": fixture_rows,
        "fixture_definitions_fingerprint": "",
    }
    fixture_definitions["fixture_definitions_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_definitions_fingerprint/o36.8",
        fixture_definitions, "fixture_definitions_fingerprint")
    by_fixture = {item["case_id"]: item for item in fixture_rows}
    cases = []
    for definition in gate.CASE_DEFINITIONS:
        raw = raw_cases[definition["case_id"]]
        raw.update({
            "case_generation_seed": definition["case_generation_seed"],
            "classification": definition["classification"],
            "fixture_definition_fingerprint": gate.fixture_definition_fingerprint(
                by_fixture[definition["case_id"]]),
        })
        if definition["case_id"] == "s8-c01-strong-confirmed":
            raw["analysis_outcome"] = parameters["positive_analysis_outcome"]
        cases.append(raw)
    development = {
        "fixture_definitions": fixture_definitions, "cases": cases,
        "faults": [{
            "fault_id": "s8-f01-grid-order-mismatch",
            "source_case_id": "s8-c01-strong-confirmed",
            "executed_grid_status": "mismatch", "reason_code": "executed_grid_mismatch",
            "ordinary_shadow_analysis_continued": True,
        }],
        "diagnostics": diagnostics,
    }
    _write_json(output / "fixture-definitions.json", fixture_definitions)
    _write_json(output / "development-inputs.json", development)
    return development


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-live-positive", action="store_true")
    parser.add_argument(
        "--capture-metadata", type=Path,
        help="optional verified run metadata used to emit capture-spec.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        parameters = json.loads(args.parameters.read_text(encoding="utf-8"))
        if not isinstance(parameters, Mapping):
            raise gate.GateEvidenceError("fixture parameter file must contain an object")
        result = build_development_inputs(args.output, parameters)
        live = None
        if args.run_live_positive:
            live = run_live_positive_analysis(result, args.output)
            for case in result["cases"]:
                if case["case_id"] == "s8-c01-strong-confirmed":
                    case["analysis_outcome"] = live["analysis_outcome"]
                    case["analysis_evidence"] = live
            _write_json(args.output / "development-inputs.json", result)
        if args.capture_metadata is not None:
            metadata = json.loads(args.capture_metadata.read_text(encoding="utf-8"))
            metadata_fields = {
                "implementation_anchor", "evidence_generation_anchor", "runtime_invariants",
                "verification", "commands", "durations_seconds",
            }
            if not isinstance(metadata, Mapping) or set(metadata) != metadata_fields:
                raise gate.GateEvidenceError("capture metadata fields are invalid")
            capture_spec = {
                "contract": CAPTURE_SPEC_CONTRACT,
                "implementation_anchor": metadata["implementation_anchor"],
                "evidence_generation_anchor": metadata["evidence_generation_anchor"],
                "cases": result["cases"], "faults": result["faults"],
                "runtime_invariants": metadata["runtime_invariants"],
                "verification": metadata["verification"],
                "commands": metadata["commands"],
                "durations_seconds": metadata["durations_seconds"],
            }
            _write_json(args.output / "capture-spec.json", capture_spec)
        print(canonical_json({"fixture_definitions_fingerprint": result[
            "fixture_definitions"]["fixture_definitions_fingerprint"],
            "diagnostics": result["diagnostics"],
            "live_positive": None if live is None else live["analysis_outcome"]}))
        return 0
    except (gate.GateEvidenceError, OSError, ValueError, TypeError, KeyError) as error:
        log.error("Slice 8 fixture generation failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
