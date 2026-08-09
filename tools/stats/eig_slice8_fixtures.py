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
import shutil
import sys
import time
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
from isar.drive.acquisition.acquire import ScanProposal, ShadowScoringSource
from isar.drive.acquisition.eig_shadow import (
    _resolved_scoring_inputs,
    score_proposal_realized_pair,
    score_proposal_shadow_inventory,
)
from isar.drive.acquisition.inventory import (
    MeasurementIntent,
    scientific_fingerprint_from_mapping,
)
from isar.drive.acquisition.realized_comparison import (
    build_realized_comparison,
    construct_realized_pair,
    load_scan_design_realized_comparison,
    persist_realized_comparison,
)
from isar.drive.acquisition.scan_design import (
    ScanDesignContext,
    design_scan_windows,
    load_scan_design_inventory,
)
from isar.drive.objective import ObjectiveSpec, current_objective_comparison_scope
from isar.drive.instrument.tavi_client import TaviClient
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import run_isar
from isar.drive.isar_io.scan_writer import save_scan_json
from isar.drive.session_log import log_event, read_log
from isar.eig import EligibilityInput, adapt_omega0_target_projection
from isar.eig.shadow_inventory import score_shadow_inventory
from isar.schema.action_identity import canonical_json
from isar.schema.card import CardState
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowEIGConfig,
    ScanWindowShadowScoringConfig,
)
from isar.schema.eig_shadow import thaw
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    authority_scopes,
    semantics_fingerprint,
    validate_for_write,
)
from isar.session import driver, evidence_update, predictive_artifact, scan_delivery
from isar.session.state import ScanEntry, SessionState
from isar.session.predictive_artifact import build_target_projection
from isar.drive import loop as drive_loop
from isar.drive.acquisition import eig_runtime
from tools.stats import eig_slice8_gate as gate


log = logging.getLogger(__name__)

DEVELOPMENT_SPEC_CONTRACT = "isar.eig4_slice8_fixture_development/o36.8"
CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8"
REPAIR_DEVELOPMENT_SPEC_CONTRACT = "isar.eig4_slice8_fixture_development/o36.8.1"
REPAIR_CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8.1"
REPLACEMENT_CAPTURE_SPEC_CONTRACT = "isar.eig4_slice8_capture_spec/o36.8.2"
CASE2_CALIBRATION_PROTOCOL_CONTRACT = (
    "isar.eig4_slice8_case2_amplitude_calibration_protocol/o36.8.2")
CASE2_CALIBRATION_TABLE_CONTRACT = (
    "isar.eig4_slice8_case2_amplitude_calibration_table/o36.8.2")
REPLACEMENT_DEVELOPMENT_SPEC_CONTRACT = "isar.eig4_slice8_fixture_development/o36.8.2"


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
        label=label, cost=10.0, k=4, aic=18.0, weight=1.0, akaike_weight=1.0,
        posterior_probability=1.0, candidate_prior=1.0, log_evidence=-5.0,
        log_evidence_err=None, evidence_method="laplace", evidence_validity={},
        survives=True, fit_semantics=semantics,
    )
    route = {
        "route": "full_two_stage", "labels_before_guideline": [label],
        "guideline_structure_filter": [label], "offered_labels": [label],
        "scored_labels": [label],
    }
    return scan_delivery._build_selection_semantics(
        [result],
        SimpleNamespace(
            scores=[score], best=label, pooled=object(), selection_fallback=None,
            evidence=SimpleNamespace(
                arena_fingerprint=semantics["data_likelihood_fingerprint"]),
        ),
        route,
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


def _intent_mapping(intent: MeasurementIntent) -> dict[str, Any]:
    return {
        "action_class": intent.action_class,
        "hkl": list(intent.hkl),
        "energy_lo": intent.energy_lo,
        "energy_hi": intent.energy_hi,
        "window_family": intent.window_family,
        "target_branch_role": intent.target_branch_role,
        "rival_branch_role": intent.rival_branch_role,
        "evidence_basis_fingerprint": intent.evidence_basis_fingerprint,
        "hypotheses_fingerprint": intent.hypotheses_fingerprint,
        "campaign_slice_fingerprint": intent.campaign_slice_fingerprint,
        "evidence_sink": intent.evidence_sink,
        "window_center_meV": intent.window_center_meV,
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
    }


def _canonical_intent(*, q: tuple[float, float, float], target_energy: float,
                      case_id: str) -> MeasurementIntent:
    # ``case_id`` is deliberately not part of scientific identity.  It is an administrative fixture
    # label; including it would alter scorer seed derivation for an otherwise unchanged action.
    _ = case_id
    return MeasurementIntent(
        action_class="production",
        hkl=q,
        energy_lo=-2.0,
        energy_hi=7.0,
        window_family="target",
        target_branch_role=None,
        rival_branch_role=None,
        evidence_basis_fingerprint="slice8-basis",
        hypotheses_fingerprint="slice8-hypotheses",
        campaign_slice_fingerprint="slice8-campaign",
        evidence_sink="canonical_master",
        window_center_meV=target_energy,
    )


def _eligibility(qoi: Any, selection: Mapping[str, Any], *, q: tuple[float, float, float],
                 target_energy: float, modality: str = "fold",
                 intent: MeasurementIntent | None = None) -> EligibilityInput:
    canonical_intent = intent or _canonical_intent(
        q=q, target_energy=target_energy, case_id="legacy-fixture")
    intent_mapping = _intent_mapping(canonical_intent)
    fingerprint = canonical_intent.scientific_action_fingerprint
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
        action_intent=intent_mapping, action_provenance=provenance, target_outcome=target,
        selection_record=selection, qoi_evidence=qoi, shadow_record=_neutral_shadow())


def _predictive_bundle(*, q: tuple[float, float, float], draw_energies: Sequence[float],
                       target_energy: float, amplitude: float,
                       constrained: bool = False, seed: int = 11,
                       intent: MeasurementIntent | None = None) -> tuple[dict[str, Any], EligibilityInput]:
    if len(draw_energies) < 2 or any(not math.isfinite(float(item)) for item in draw_energies):
        raise ValueError("fixture draw energies must contain at least two finite values")
    label = "phonon-constrained" if constrained else "phonon"
    selection = _selection(label, constrained=constrained)
    # The declared case ``amplitude`` is the MEASURED Stokes strength (the physically tuned
    # regime knob, unchanged since the gate froze). The model's amplitude slot is INTRINSIC
    # since H4 (the evaluator applies (n+1)(ω,T) itself), so each draw converts at its own
    # energy — the evaluated expected counts are exactly the pre-H4 ones, and the frozen
    # calibration regimes hold by construction. T matches the fixture context below (100 K).
    from isar.analyze.phonon import bose_occupation, omega0_from

    fixture_T = 100.0                       # must match the ScanContext below

    def _intrinsic(energy: float) -> float:
        return float(amplitude) / bose_occupation(omega0_from(float(energy), 0.3), fixture_T)

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
            [float(energy), 0.3, _intrinsic(energy), 4.0] for energy in draw_energies
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
        T=fixture_T, center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1,
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
            {label: object()}, context, seed=int(seed), n_members=1,
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
        qoi, selection, q=q, target_energy=target_energy, intent=intent)


def _context(*, q: tuple[float, float, float], target_energy: float,
             prediction_sigma: float, ncount: int, survey: bool,
             scientific_action_fingerprint: str | None = None) -> ScanDesignContext:
    return ScanDesignContext(
        scientific_action_fingerprint=(scientific_action_fingerprint
            or semantics_fingerprint(
                "isar.eig4_slice8.fixture.action", {"q": q, "energy": target_energy})),
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


def _content_addressed_json(root: Path, subdirectory: str,
                            value: Mapping[str, Any]) -> str:
    encoded = canonical_json(value).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    relative = f"data/{subdirectory}/{digest}.json"
    _write_json(root / relative, value)
    return relative


def _plain_json(value: Any) -> Any:
    return json.loads(canonical_json(thaw(value)))


def _repair_objective(q: tuple[float, float, float], target_energy: float) -> ObjectiveSpec:
    objective = ObjectiveSpec(
        path=[q], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1.0e12},
        target_branch="acoustic", target_label="acoustic",
        target_selector="nearest_energy", target_energy_meV=target_energy,
        energy_search_meV=(-2.0, 7.0),
    )
    scope = current_objective_comparison_scope(objective)["fingerprint"]
    objective.scan_window_eig = ScanWindowEIGConfig(
        policy_set=_policy(scope), scoring_config=ScanWindowShadowScoringConfig())
    return objective


def _persist_prior_source(
        root: Path, *, case_id: str, q: tuple[float, float, float],
        target_energy: float, prediction_sigma: float, ncount: int, survey: bool,
        artifact: Mapping[str, Any], eligibility: EligibilityInput,
) -> dict[str, Any]:
    """Persist one completed acquisition that is strictly prior to the candidate inventory."""
    _init_tavi_workspace(root)
    master_revision = "slice8-master-r1"
    target = {
        **dict(eligibility.target_outcome),
        "master_revision": master_revision,
    }
    projection = build_target_projection(
        artifact, target, master_revision=master_revision,
        calibration_fingerprint="slice8-cal-r1",
        policy_fingerprint="slice8-policy-r1", action_role="certification")
    predictive_ref = _content_addressed_json(root, "master_predictive", artifact)
    projection_ref = _content_addressed_json(root, "target_projection", projection)

    prior_action = semantics_fingerprint(
        "isar.eig4_slice8.repair.prior_action/o36.8.1", {
            "hkl": list(q), "target_energy_meV": target_energy,
            "prediction_sigma_meV": prediction_sigma, "ncount": ncount,
            "survey": survey,
        })
    prior_inventory = design_scan_windows(_context(
        q=q, target_energy=target_energy, prediction_sigma=prediction_sigma,
        ncount=ncount, survey=survey,
        scientific_action_fingerprint=prior_action))
    prior_digest = prior_inventory.content_hash()
    prior_relative = f"report/scan-design/{prior_digest}.json"
    _write_json(root / prior_relative, prior_inventory.to_full_dict())
    prior_ref = {
        "kind": "scan_design_inventory", "sha256": prior_digest,
        "path": prior_relative,
    }
    source_acquisition_id = f"{case_id}:prior-source"
    source_request_fingerprint = semantics_fingerprint(
        "isar.eig4_slice8.repair.prior_request/o36.8.1", {
            "hkl": list(q),
            "candidate_id": prior_inventory.selected_candidate_id,
            "grid_fingerprint": prior_inventory.selected_candidate.grid.fingerprint(
                prior_inventory.selected_candidate.ncount),
        })
    source_submission = {
        "job_id": f"{case_id}-prior-job",
        "state": "done",
        "attempt": 0,
        "acquisition_id": source_acquisition_id,
        "proposal_fingerprint": source_request_fingerprint,
        "scientific_action_fingerprint": prior_action,
        "scan_design_inventory_ref": prior_ref,
        "scan_design_candidate_id": prior_inventory.selected_candidate_id,
        "scan_design_context_fingerprint": prior_inventory.context.fingerprint,
        "hkl": list(q),
    }
    log_event(root / "report" / "session_log.jsonl", "physical_submission", source_submission)
    log_event(root / "report" / "session_log.jsonl", "analyze", {
        "job_id": source_submission["job_id"],
        "acquisition_id": source_acquisition_id,
        "status": "completed",
    })

    outcome = {
        **target,
        "predictive_ref": predictive_ref,
        "target_projection_ref": projection_ref,
        "fit_selection_semantics": _plain_json(eligibility.selection_record),
        "shadow_model_selection": _plain_json(eligibility.shadow_record),
    }
    source_key = f"{case_id}:source-master"
    detector_id = "pooled"
    state = SessionState()
    state.put(ScanEntry(
        scan_id=source_key, scan_number=9_800_000, scan_type="pooled_master",
        status="fitted", detectors={detector_id: outcome},
        provenance={"target_projection_authority": projection["authority"]},
    ))
    state.save(root / "session.json")
    _write_json(root / "report" / "prior-source-record.json", {
        "contract": "isar.eig4_slice8_prior_source/o36.8.1",
        "case_id": case_id,
        "source_acquisition_id": source_acquisition_id,
        "source_submission_request_fingerprint": source_request_fingerprint,
        "source_master_session_key": source_key,
        "source_detector_id": detector_id,
        "source_master_revision": master_revision,
        "predictive_ref": predictive_ref,
        "target_projection_ref": projection_ref,
        "prior_scan_design_inventory_ref": prior_ref,
        "prior_scan_design_context_fingerprint": prior_inventory.context.fingerprint,
        "prior_scan_design_candidate_id": prior_inventory.selected_candidate_id,
        "source_submission": source_submission,
    })
    return {
        "master_session_key": source_key,
        "detector_id": detector_id,
        "master_revision": master_revision,
        "source_acquisition_id": source_acquisition_id,
        "source_submission_request_fingerprint": source_request_fingerprint,
        "predictive_ref": predictive_ref,
        "target_projection_ref": projection_ref,
        "target_outcome": outcome,
        "target_projection_authority": projection["authority"],
        "prior_inventory_ref": prior_ref,
        "prior_context_fingerprint": prior_inventory.context.fingerprint,
        "prior_candidate_id": prior_inventory.selected_candidate_id,
        "source_submission": source_submission,
    }


def _persist_repair_eligible_case(
        root: Path, *, case_id: str, case_seed: int,
        q: tuple[float, float, float], draw_energies: Sequence[float],
        target_energy: float, prediction_sigma: float, amplitude: float,
        ncount: int, survey: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Construct one coherent eligible fixture through the production scoring adapter."""
    intent = _canonical_intent(q=q, target_energy=target_energy, case_id=case_id)
    artifact, seed_eligibility = _predictive_bundle(
        q=q, draw_energies=draw_energies, target_energy=target_energy,
        amplitude=amplitude, seed=case_seed, intent=intent)
    source = _persist_prior_source(
        root, case_id=case_id, q=q, target_energy=target_energy,
        prediction_sigma=prediction_sigma, ncount=ncount, survey=survey,
        artifact=artifact, eligibility=seed_eligibility)
    objective = _repair_objective(q, target_energy)
    inventory = design_scan_windows(_context(
        q=q, target_energy=target_energy, prediction_sigma=prediction_sigma,
        ncount=ncount, survey=survey,
        scientific_action_fingerprint=intent.scientific_action_fingerprint))
    legacy = inventory.selected_candidate
    proposal = ScanProposal(
        h=q[0], k=q[1], l=q[2], e_lo=legacy.e_lo, e_hi=legacy.e_hi,
        e_step=legacy.e_step, ncount=legacy.ncount, kind="production",
        reason="frozen Slice 8 repair legacy control", e_pred=target_energy,
        fwhm_dE=0.8, n_points=legacy.n_points,
        meta={
            "proposal_fingerprint": semantics_fingerprint(
                "isar.eig4_slice8.repair.legacy_request/o36.8.1", {"case_id": case_id}),
            "scientific_action_fingerprint": intent.scientific_action_fingerprint,
            "acquisition_action_role": "production",
            "target_branch_role": "acoustic",
            "rival_branch_role": "optic",
            "evidence_sink": "canonical_master",
            "driver_inserted": False,
            "certification_eligible": True,
        },
        intent=intent,
        scan_design_inventory=inventory,
    )
    proposal.shadow_scoring_source = ShadowScoringSource(
        master_session_key=source["master_session_key"],
        detector_id=source["detector_id"],
        master_revision=source["master_revision"],
        source_acquisition_id=source["source_acquisition_id"],
        source_submission_request_fingerprint=source[
            "source_submission_request_fingerprint"],
        scientific_action_fingerprint=intent.scientific_action_fingerprint,
        predictive_ref=source["predictive_ref"],
        target_projection_ref=source["target_projection_ref"],
        target_outcome=dict(source["target_outcome"]),
        selection_record=dict(seed_eligibility.selection_record),
        shadow_record=dict(seed_eligibility.shadow_record),
        target_projection_authority=dict(source["target_projection_authority"]),
        action_intent=intent,
        action_provenance=dict(seed_eligibility.action_provenance),
    )
    drive_loop._stamp_proposal_provenance(proposal)
    _write_json(root / "report" / "current-proposal.json", {
        "contract": "isar.eig4_slice8_current_proposal/o36.8.1",
        "request": proposal.to_dict(),
        "meta": _plain_json(proposal.meta),
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
    })
    _write_json(root / "report" / "scoring-source.json", {
        "contract": "isar.eig4_slice8_scoring_source/o36.8.1",
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
        "source_acquisition_id": source["source_acquisition_id"],
        "source_submission_request_fingerprint": source[
            "source_submission_request_fingerprint"],
        "source_master_session_key": source["master_session_key"],
        "source_detector_id": source["detector_id"],
        "source_master_revision": source["master_revision"],
        "predictive_ref": source["predictive_ref"],
        "target_projection_ref": source["target_projection_ref"],
    })
    score_proposal_shadow_inventory(proposal, objective, root)
    scoring = proposal.scan_design_inventory.shadow_scoring
    if scoring is None or scoring.status != "complete":
        raise gate.GateEvidenceError(
            f"{case_id} production-adapter scoring is unavailable: "
            f"{None if scoring is None else scoring.reason_codes}")
    # Authenticate the completed prior acquisition before constructing any current realized pair.
    _predictive, adapter_eligibility, _config, scope = _resolved_scoring_inputs(
        proposal, objective, root, lambda: None)
    inventory = proposal.scan_design_inventory
    digest = inventory.content_hash()
    inventory_relative = f"report/scan-design/{digest}.json"
    _write_json(root / inventory_relative, inventory.to_full_dict())
    inventory_ref = {
        "kind": "scan_design_inventory", "sha256": digest,
        "path": inventory_relative,
    }

    def finalize(target: ScanProposal, *_args: Any, **_kwargs: Any) -> None:
        drive_loop._stamp_proposal_provenance(target)

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {}, it=1,
        finalizer=finalize)
    comparison = build_realized_comparison(
        inventory, inventory_ref, pair, remaining_seconds=5.0,
        rescorer=lambda realized_pair, **_kwargs: score_proposal_realized_pair(
            proposal, objective, root, realized_pair,
            deadline_monotonic=time.monotonic() + 5.0))
    comparison_ref = persist_realized_comparison(root, comparison)
    primary = next(item for item in comparison.variants
                   if item.variant_id == gate.PRIMARY_VARIANT_ID)
    score_set, legacy_id, target_id = gate._score_set_payload(
        comparison, inventory, primary)
    blocks = list(gate._paired_blocks(score_set, legacy_id, target_id))
    selected = pair.legacy
    submission = {
        "scan_design_realized_comparison_fingerprint": (
            comparison.realized_comparison_fingerprint),
        "scan_design_realized_selected_request_fingerprint": (
            comparison.selected_request_fingerprint),
        "scan_design_realized_selected_grid_fingerprint": (
            comparison.selected_grid_fingerprint),
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
        "proposal_fingerprint": comparison.selected_request_fingerprint,
        "scan_design_inventory_ref": inventory_ref,
        "scan_design_context_fingerprint": inventory.context.fingerprint,
        "scan_design_candidate_id": inventory.selected_candidate_id,
        "hkl": list(q),
        "returned_grid": list(selected.executed_energies),
    }
    acquisition_provenance = dict(submission)
    acquisition_provenance["acquisition_id"] = f"{case_id}:current-candidate"
    _write_json(root / "report" / "current-submission.json", submission)
    _write_json(root / "report" / "current-acquisition-provenance.json",
                acquisition_provenance)
    raw = {
        "case_id": case_id,
        "eligibility_source": gate.serialize_repair_eligibility_input(adapter_eligibility),
        "source_workspace": str(root.resolve()),
        "realized_comparison_ref": comparison_ref,
        "applicability": "applicable",
        "executed_grid_status": "matched",
        "submission_provenance": submission,
        "acquisition_provenance": acquisition_provenance,
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
        "proposal_metadata_ref": "report/current-proposal.json",
        "scoring_source_ref": "report/scoring-source.json",
        "prior_source_record_ref": "report/prior-source-record.json",
        "prior_scan_design_inventory_ref": source["prior_inventory_ref"],
        "current_scan_design_inventory_ref": inventory_ref,
        "analysis_outcome": {},
    }
    diagnostics = {
        "blocks": blocks,
        "legacy_points": pair.legacy.executed_point_count,
        "target_points": pair.target_core.executed_point_count,
        "legacy_window": [pair.legacy.e_lo, pair.legacy.e_hi],
        "target_window": [pair.target_core.e_lo, pair.target_core.e_hi],
        "scope_fingerprint": scope["fingerprint"],
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
        "predictive_seed": case_seed,
        "scorer_seed_evidence": scoring.seed_evidence.to_dict(),
        "score_set_fingerprint": score_set["score_set_fingerprint"],
    }
    return raw, diagnostics


def validate_case2_calibration_protocol(value: Mapping[str, Any]) -> None:
    """Authenticate the predeclared one-dimensional o36.8.2 calibration protocol."""
    required = {
        "contract", "authorized_from", "case_id", "case_generation_seed",
        "fixed_physical_parameters", "amplitude_ladder", "ordinary_regime_contract",
        "scoring_contract", "policy_contract", "selection_rule", "protocol_fingerprint",
    }
    if set(value) != required or value.get("contract") != CASE2_CALIBRATION_PROTOCOL_CONTRACT:
        raise gate.GateEvidenceError("Case 2 calibration protocol fields are invalid")
    if (value.get("case_id") != "s8-c02-weak-unresolved-confirmed"
            or value.get("case_generation_seed") != 8040802):
        raise gate.GateEvidenceError("Case 2 calibration identity or seed changed")
    fixed = value.get("fixed_physical_parameters")
    if fixed != {
        "q": [2.15, 0.0, 0.0], "draw_energies_meV": [1.9, 2.1],
        "target_energy_meV": 2.0, "prediction_sigma_meV": 0.4,
        "ncount": 1, "survey": True,
    }:
        raise gate.GateEvidenceError("Case 2 calibration changed a fixed physical parameter")
    ladder = value.get("amplitude_ladder")
    if (not isinstance(ladder, list) or not ladder
            or any(not isinstance(item, (int, float)) or isinstance(item, bool)
                   or not math.isfinite(float(item)) or float(item) <= 0.0
                   for item in ladder)
            or len({float(item) for item in ladder}) != len(ladder)
            or any(float(left) <= float(right) for left, right in zip(ladder, ladder[1:]))):
        raise gate.GateEvidenceError(
            "Case 2 amplitude ladder must contain unique descending positive values")
    config = ScanWindowShadowScoringConfig()
    if value.get("scoring_contract") != {
        "simulation_count": 128, "block_count": 8,
        "scoring_config_fingerprint": config.scoring_config_fingerprint,
        "seed_derivation": "production",
    }:
        raise gate.GateEvidenceError("Case 2 calibration scoring contract changed")
    if value.get("policy_contract") != {
        "policy_id": gate.PRIMARY_POLICY_ID,
        "variant_id": gate.PRIMARY_VARIANT_ID,
        "bin_width_meV": 0.5, "offsets_meV": [0.0, 0.25],
    }:
        raise gate.GateEvidenceError("Case 2 calibration policy changed")
    if value.get("ordinary_regime_contract") != {
        "eligibility_status": "eligible", "target_modality": "fold",
        "target_state": "confirmed", "pool_consistent": True,
        "target_delivery_eligible": True, "ncount": 1,
        "maximum_amplitude": 0.3,
    }:
        raise gate.GateEvidenceError("Case 2 ordinary weak-target contract changed")
    if value.get("selection_rule") != {
        "order": "first_ladder_value",
        "requirements": [
            "ordinary_weak_target_regime_valid",
            "delta_information_per_executed_point_nats > 0",
            "delta_information_per_executed_point_nats - 2 * standard_error <= 0",
        ],
        "no_fallback_retuning": True,
    }:
        raise gate.GateEvidenceError("Case 2 calibration selection rule changed")
    if value.get("protocol_fingerprint") != gate._fingerprint(
            "isar.eig4_slice8_case2_amplitude_calibration_protocol_fingerprint/o36.8.2",
            value, "protocol_fingerprint"):
        raise gate.GateEvidenceError("Case 2 calibration protocol fingerprint is invalid")


def build_case2_amplitude_calibration(
        output: Path, protocol: Mapping[str, Any], *,
        workspace_root: Path | None = None) -> dict[str, Any]:
    """Execute every predeclared amplitude and select the first qualifying row."""
    validate_case2_calibration_protocol(protocol)
    fixed = protocol["fixed_physical_parameters"]
    calibration_workspace_root = workspace_root or output / "calibration-workspaces"
    rows: list[dict[str, Any]] = []
    for index, amplitude_raw in enumerate(protocol["amplitude_ladder"]):
        amplitude = float(amplitude_raw)
        root = calibration_workspace_root / f"amplitude-{index:02d}"
        raw, detail = _persist_repair_eligible_case(
            root, case_id="s8-c02-weak-unresolved-confirmed", case_seed=8040802,
            q=tuple(float(item) for item in fixed["q"]),
            draw_energies=fixed["draw_energies_meV"],
            target_energy=float(fixed["target_energy_meV"]),
            prediction_sigma=float(fixed["prediction_sigma_meV"]),
            amplitude=amplitude, ncount=int(fixed["ncount"]),
            survey=bool(fixed["survey"]))
        eligibility, summary = gate.deserialize_repair_eligibility_input(
            raw["eligibility_source"])
        target = eligibility.target_outcome
        ordinary_valid = (
            summary["status"] == "eligible"
            and target.get("modality") == "fold"
            and target.get("candidate_state") == "confirmed"
            and target.get("pool_consistent") is True
            and target.get("target_delivery_eligible") is True
            and int(fixed["ncount"]) == 1
            and 0.0 < amplitude <= 0.3)
        delta, standard_error, lower = gate._paired_summary(detail["blocks"])
        qualifies = ordinary_valid and delta > 0.0 and lower <= 0.0
        rows.append({
            "ladder_index": index, "amplitude": amplitude,
            "ordinary_weak_target_regime_valid": ordinary_valid,
            "eligibility_status": summary["status"],
            "delta_information_per_executed_point_nats": delta,
            "delta_standard_error_per_executed_point_nats": standard_error,
            "two_standard_error_lower_margin_nats": lower,
            "qualifies": qualifies,
            "case_generation_seed": 8040802,
            "scorer_seed_evidence": detail["scorer_seed_evidence"],
            "score_set_fingerprint": detail["score_set_fingerprint"],
            "paired_block_deltas_per_executed_point_nats": list(detail["blocks"]),
        })
    selected = next((row for row in rows if row["qualifies"]), None)
    if selected is None:
        raise gate.GateEvidenceError(
            "predeclared Case 2 amplitude ladder contains no qualifying value")
    table: dict[str, Any] = {
        "contract": CASE2_CALIBRATION_TABLE_CONTRACT,
        "protocol_fingerprint": protocol["protocol_fingerprint"],
        "rows": rows,
        "selected_ladder_index": selected["ladder_index"],
        "selected_amplitude": selected["amplitude"],
        "selection_outcome": "first_predeclared_value_satisfying_rule",
        "calibration_table_fingerprint": "",
    }
    table["calibration_table_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case2_amplitude_calibration_table_fingerprint/o36.8.2",
        table, "calibration_table_fingerprint")
    _write_json(output / "case2-amplitude-calibration-table.json", table)
    return table


def validate_case2_calibration_table(
        value: Mapping[str, Any], protocol: Mapping[str, Any]) -> Mapping[str, Any]:
    validate_case2_calibration_protocol(protocol)
    _exact_fields = {
        "contract", "protocol_fingerprint", "rows", "selected_ladder_index",
        "selected_amplitude", "selection_outcome", "calibration_table_fingerprint",
    }
    if (set(value) != _exact_fields
            or value.get("contract") != CASE2_CALIBRATION_TABLE_CONTRACT
            or value.get("protocol_fingerprint") != protocol["protocol_fingerprint"]
            or value.get("selection_outcome")
            != "first_predeclared_value_satisfying_rule"):
        raise gate.GateEvidenceError("Case 2 calibration table fields are invalid")
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) != len(protocol["amplitude_ladder"]):
        raise gate.GateEvidenceError("Case 2 calibration table does not retain the full ladder")
    qualifiers: list[int] = []
    expected_row_fields = {
        "ladder_index", "amplitude", "ordinary_weak_target_regime_valid",
        "eligibility_status", "delta_information_per_executed_point_nats",
        "delta_standard_error_per_executed_point_nats",
        "two_standard_error_lower_margin_nats", "qualifies", "case_generation_seed",
        "scorer_seed_evidence", "score_set_fingerprint",
        "paired_block_deltas_per_executed_point_nats",
    }
    for index, (row, amplitude) in enumerate(zip(rows, protocol["amplitude_ladder"])):
        if not isinstance(row, Mapping) or set(row) != expected_row_fields:
            raise gate.GateEvidenceError("Case 2 calibration row fields are invalid")
        blocks = row["paired_block_deltas_per_executed_point_nats"]
        delta, standard_error, lower = gate._paired_summary(blocks)
        seed = row["scorer_seed_evidence"]
        qualifies = (row["ordinary_weak_target_regime_valid"] is True
                     and delta > 0.0 and lower <= 0.0)
        if (row["ladder_index"] != index or row["amplitude"] != amplitude
                or row["case_generation_seed"] != 8040802
                or row["eligibility_status"] != "eligible"
                or delta != row["delta_information_per_executed_point_nats"]
                or standard_error
                != row["delta_standard_error_per_executed_point_nats"]
                or lower != row["two_standard_error_lower_margin_nats"]
                or row["qualifies"] is not qualifies
                or not isinstance(seed, Mapping)
                or seed.get("scoring_config_fingerprint")
                != ScanWindowShadowScoringConfig().scoring_config_fingerprint
                or not isinstance(seed.get("seed"), int)
                or isinstance(seed.get("seed"), bool)
                or not gate._valid_sha256(seed.get("seed_evidence_fingerprint"))
                or not gate._valid_sha256(row["score_set_fingerprint"])):
            raise gate.GateEvidenceError("Case 2 calibration row is internally inconsistent")
        if qualifies:
            qualifiers.append(index)
    if (not qualifiers or value["selected_ladder_index"] != qualifiers[0]
            or value["selected_amplitude"]
            != protocol["amplitude_ladder"][qualifiers[0]]):
        raise gate.GateEvidenceError("Case 2 calibration did not select the first qualifying row")
    if value["calibration_table_fingerprint"] != gate._fingerprint(
            "isar.eig4_slice8_case2_amplitude_calibration_table_fingerprint/o36.8.2",
            value, "calibration_table_fingerprint"):
        raise gate.GateEvidenceError("Case 2 calibration table fingerprint is invalid")
    return value


def validate_case2_reproducibility(
        value: Mapping[str, Any], table: Mapping[str, Any]) -> Mapping[str, Any]:
    fingerprint = value.get("reproducibility_fingerprint")
    if (value.get("contract")
            != "isar.eig4_slice8_case2_amplitude_reproducibility/o36.8.2"
            or fingerprint != gate._fingerprint(
                "isar.eig4_slice8_case2_amplitude_reproducibility_fingerprint/o36.8.2",
                value, "reproducibility_fingerprint")
            or value.get("calibration_table_fingerprint")
            != table["calibration_table_fingerprint"]
            or value.get("selected_ladder_index") != table["selected_ladder_index"]
            or value.get("selected_amplitude") != table["selected_amplitude"]
            or value.get("independent_rerun_count") != 1
            or not isinstance(value.get("exact_match"), Mapping)
            or not value["exact_match"]
            or not all(item is True for item in value["exact_match"].values())):
        raise gate.GateEvidenceError("Case 2 reproducibility evidence is invalid")
    selected = table["rows"][table["selected_ladder_index"]]
    if (value.get("delta_information_per_executed_point_nats")
            != selected["delta_information_per_executed_point_nats"]
            or value.get("delta_standard_error_per_executed_point_nats")
            != selected["delta_standard_error_per_executed_point_nats"]
            or value.get("two_standard_error_lower_margin_nats")
            != selected["two_standard_error_lower_margin_nats"]
            or value.get("scorer_seed")
            != selected["scorer_seed_evidence"]["seed"]
            or value.get("scorer_seed_evidence_fingerprint")
            != selected["scorer_seed_evidence"]["seed_evidence_fingerprint"]
            or value.get("score_set_fingerprint") != selected["score_set_fingerprint"]
            or value.get("paired_block_deltas_per_executed_point_nats")
            != selected["paired_block_deltas_per_executed_point_nats"]):
        raise gate.GateEvidenceError("Case 2 reproducibility evidence does not match selection")
    return value


def _repair_proposal_from_retained(raw: Mapping[str, Any], root: Path
                                   ) -> tuple[ScanProposal, ObjectiveSpec]:
    eligibility, _summary = gate.deserialize_repair_eligibility_input(
        raw["eligibility_source"])
    intent_raw = dict(eligibility.action_intent)
    intent_raw.pop("scientific_action_fingerprint", None)
    intent = MeasurementIntent(
        action_class=intent_raw["action_class"], hkl=tuple(intent_raw["hkl"]),
        energy_lo=intent_raw["energy_lo"], energy_hi=intent_raw["energy_hi"],
        window_family=intent_raw["window_family"],
        target_branch_role=intent_raw["target_branch_role"],
        rival_branch_role=intent_raw["rival_branch_role"],
        evidence_basis_fingerprint=intent_raw["evidence_basis_fingerprint"],
        hypotheses_fingerprint=intent_raw["hypotheses_fingerprint"],
        campaign_slice_fingerprint=intent_raw["campaign_slice_fingerprint"],
        evidence_sink=intent_raw["evidence_sink"],
        window_center_meV=intent_raw.get("window_center_meV"),
    )
    inventory = load_scan_design_inventory(raw["current_scan_design_inventory_ref"], root)
    source = json.loads((root / raw["prior_source_record_ref"]).read_text(encoding="utf-8"))
    scoring_source = json.loads(
        (root / raw["scoring_source_ref"]).read_text(encoding="utf-8"))
    legacy = inventory.selected_candidate
    proposal = ScanProposal(
        h=float(intent.hkl[0]), k=float(intent.hkl[1]), l=float(intent.hkl[2]),
        e_lo=legacy.e_lo, e_hi=legacy.e_hi, e_step=legacy.e_step,
        ncount=legacy.ncount, kind="production",
        reason="frozen Slice 8 repair legacy control",
        e_pred=float(intent.window_center_meV), fwhm_dE=0.8,
        n_points=legacy.n_points,
        meta={
            "scientific_action_fingerprint": intent.scientific_action_fingerprint,
            "acquisition_action_role": "production",
            "target_branch_role": "acoustic", "rival_branch_role": "optic",
            "evidence_sink": "canonical_master", "driver_inserted": False,
            "certification_eligible": True,
            "target_band": [float(intent.window_center_meV), 0.35],
        },
        intent=intent, scan_design_inventory=inventory,
    )
    proposal.shadow_scoring_source = ShadowScoringSource(
        master_session_key=scoring_source["source_master_session_key"],
        detector_id=scoring_source["source_detector_id"],
        master_revision=scoring_source["source_master_revision"],
        source_acquisition_id=scoring_source["source_acquisition_id"],
        source_submission_request_fingerprint=scoring_source[
            "source_submission_request_fingerprint"],
        scientific_action_fingerprint=intent.scientific_action_fingerprint,
        predictive_ref=scoring_source["predictive_ref"],
        target_projection_ref=scoring_source["target_projection_ref"],
        target_outcome=dict(eligibility.target_outcome),
        selection_record=dict(eligibility.selection_record),
        shadow_record=dict(eligibility.shadow_record),
        target_projection_authority=dict(
            json.loads((root / scoring_source["target_projection_ref"]).read_text(
                encoding="utf-8"))["authority"]),
        action_intent=intent,
        action_provenance=dict(eligibility.action_provenance),
    )
    return proposal, _repair_objective(tuple(intent.hkl), float(intent.window_center_meV))


class _CaptureTavi:
    """Transparent client wrapper that snapshots the authenticated pre-submit evidence."""

    def __init__(self, inner: TaviClient, proposal: ScanProposal, capture: Path,
                 *, reverse_returned_grid: bool = False) -> None:
        self.inner = inner
        self.proposal = proposal
        self.capture = capture
        self.reverse_returned_grid = reverse_returned_grid
        self.submitted_body: dict[str, Any] | None = None
        self.returned_data: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def submit_scan(self, body: Mapping[str, Any], *, idempotency_key: str | None = None):
        meta = self.proposal.meta or {}
        comparison_ref = meta.get("scan_design_realized_comparison_ref")
        inventory_ref = meta.get("scan_design_inventory_ref")
        if not isinstance(comparison_ref, Mapping) or not isinstance(inventory_ref, Mapping):
            raise gate.GateEvidenceError("pre-submit capture lacks authenticated realized evidence")
        self.capture.mkdir(parents=True, exist_ok=True)
        _write_json(self.capture / "captured-comparison-ref.json", dict(comparison_ref))
        _write_json(self.capture / "captured-inventory-ref.json", dict(inventory_ref))
        _write_json(self.capture / "captured-sidecar-metadata.json", {
            "comparison_fingerprint": meta.get(
                "scan_design_realized_comparison_fingerprint"),
            "selected_request_fingerprint": meta.get(
                "scan_design_realized_selected_request_fingerprint"),
            "selected_grid_fingerprint": meta.get(
                "scan_design_realized_selected_grid_fingerprint"),
            "scientific_action_fingerprint": meta.get("scientific_action_fingerprint"),
        })
        self.submitted_body = _plain_json(body)
        _write_json(self.capture / "submitted-body.json", self.submitted_body)
        return self.inner.submit_scan(body, idempotency_key=idempotency_key)

    def get_scan_data(self, job_id: str):
        value = _plain_json(self.inner.get_scan_data(job_id))
        if self.reverse_returned_grid:
            result = value.get("result") or {}
            result["scan_values_1"] = list(reversed(result.get("scan_values_1") or []))
            value["result"] = result
        self.returned_data = value
        _write_json(self.capture / "returned-data.json", value)
        return value


def _run_controller_completion(raw: Mapping[str, Any], workspace: Path,
                               capture: Path, *, case_seed: int,
                               reverse_returned_grid: bool = False
                               ) -> tuple[ScanProposal, _CaptureTavi, list[dict[str, Any]]]:
    proposal, objective = _repair_proposal_from_retained(raw, workspace)
    wrapper = _CaptureTavi(
        TaviClient(), proposal, capture, reverse_returned_grid=reverse_returned_grid)
    cfg = drive_loop._campaign_config(objective, {
        "workspace": workspace, "client": wrapper, "run_isar": run_isar,
        "resolution": {"fwhm_dE": 0.8}, "seed": int(case_seed),
        "max_iters": 1, "wait_timeout": 120.0,
    })
    budget = {"kind": "neutrons", "amount": 1.0e12,
              "neutrons": 0.0, "seconds": 0.0, "spent": 0.0}
    eig_runtime._score_shadow_proposal(proposal, objective, cfg, it=0)
    scoring = proposal.scan_design_inventory.shadow_scoring
    if scoring is None or scoring.status != "complete":
        raise gate.GateEvidenceError("production controller did not complete shadow scoring")
    drive_loop._finalize_proposal(proposal, objective, cfg, budget, it=0)
    drive_loop._persist_scan_design_inventory(cfg, proposal, it=0)
    eig_runtime._refresh_realized_comparison(
        cfg, objective, proposal, budget, it=0,
        legacy_adjustments=("repair_pre_submit",), finalizer=drive_loop._finalize_proposal)
    proposal.acquisition_id = "s8-c01-current-candidate"
    observations: list[dict[str, Any]] = []
    drive_loop._execute_proposal(
        cfg=cfg, objective=objective, proposal=proposal, it=0, scan_counter=0,
        budget_state=budget, observations=observations, consumed=set(), scan_kind={})
    return proposal, wrapper, read_log(workspace / "report" / "session_log.jsonl")


def retain_case1_production_comparison(
        raw: dict[str, Any], *, source: Path, production_workspace: Path,
        comparison_ref: Mapping[str, Any], physical_submission: Mapping[str, Any],
        acquisition_provenance: Mapping[str, Any]) -> None:
    """Make Case 1 classify the exact comparison attached by production."""
    relative = comparison_ref.get("path")
    if not isinstance(relative, str):
        raise gate.GateEvidenceError("Case 1 production comparison reference is invalid")
    production_path = (production_workspace / relative).resolve()
    production_root = production_workspace.resolve()
    try:
        production_path.relative_to(production_root)
    except ValueError as error:
        raise gate.GateEvidenceError(
            "Case 1 production comparison escapes its workspace") from error
    if not production_path.is_file():
        raise gate.GateEvidenceError("Case 1 production comparison is missing")
    retained_path = source / relative
    retained_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(production_path, retained_path)
    loaded = load_scan_design_realized_comparison(comparison_ref, source)
    if (loaded.realized_comparison_fingerprint
            != physical_submission.get("scan_design_realized_comparison_fingerprint")):
        raise gate.GateEvidenceError(
            "Case 1 retained production comparison does not match physical submission")
    _write_json(source / "report" / "current-submission.json", physical_submission)
    _write_json(
        source / "report" / "current-acquisition-provenance.json",
        acquisition_provenance)
    raw["realized_comparison_ref"] = dict(comparison_ref)


def run_case1_counterfactual_fork(raw: dict[str, Any], *, case_seed: int) -> dict[str, Any]:
    """Execute the strong case's legacy and target requests from byte-identical source clones."""
    source = Path(raw["source_workspace"])
    source_before = _tree_fingerprint(source)
    branch_root = source.parent.parent / "case1-counterfactual-branches"
    if branch_root.exists():
        shutil.rmtree(branch_root)
    branch_a = branch_root / "legacy"
    branch_b = branch_root / "target-core"
    shutil.copytree(source, branch_a)
    shutil.copytree(source, branch_b)
    branch_a_initial = _tree_fingerprint(branch_a)
    branch_b_initial = _tree_fingerprint(branch_b)
    if branch_a_initial != source_before or branch_b_initial != source_before:
        raise gate.GateEvidenceError("Case 1 counterfactual clones do not match their source tree")

    legacy_proposal, legacy_client, legacy_events = _run_controller_completion(
        raw, branch_a, branch_root / "captured-pre-submit", case_seed=case_seed)
    comparison = load_scan_design_realized_comparison(
        legacy_proposal.meta["scan_design_realized_comparison_ref"], branch_a)
    legacy = comparison.realized_pair.legacy
    target = comparison.realized_pair.target_core
    if not hasattr(target, "scan_command"):
        raise gate.GateEvidenceError("Case 1 target-core request was not realized")
    physical = next(event["payload"] for event in legacy_events
                    if event["kind"] == "physical_submission"
                    and event["payload"].get("acquisition_id") == legacy_proposal.acquisition_id)
    state_a = read_card_state(branch_a)
    acquisition = next(item for item in reversed(state_a.get("acquisition_provenance") or [])
                       if item.get("acquisition_id") == legacy_proposal.acquisition_id)
    returned_legacy = (legacy_client.returned_data.get("result") or {}).get("scan_values_1")
    retain_case1_production_comparison(
        raw, source=source, production_workspace=branch_a,
        comparison_ref=legacy_proposal.meta["scan_design_realized_comparison_ref"],
        physical_submission=physical, acquisition_provenance=acquisition)

    target_client = TaviClient()
    schema = target_client.get_schema()
    background = {
        "catalog_version": schema["background"]["catalog_version"],
        "enabled": False, "sources": {},
    }
    target_body = {
        "parameters": {
            "H": target.hkl[0], "K": target.hkl[1], "L": target.hkl[2],
            "scan_command1": target.scan_command, "number_neutrons": target.ncount,
            "sample": "Al_phonon_DFT",
        },
        "engine": "deterministic", "seed": int(case_seed), "isolated": True,
        "background": background,
    }
    target_submit = target_client.submit_scan(target_body)
    target_snapshot = target_client.wait_scan(
        target_submit["job_id"], timeout=120.0, poll_wait=5.0)
    if target_snapshot.get("state") != "done":
        raise gate.GateEvidenceError("Case 1 target-core counterfactual did not complete")
    target_data = target_client.get_scan_data(target_submit["job_id"])
    saved = save_scan_json(target_data, branch_b / "data")
    process = run_isar(branch_b, [branch_b / "data"])
    rows = read_results_csv(branch_b) if process.returncode == 0 else []
    state_b = read_card_state(branch_b) if process.returncode == 0 else {}
    fitted = [row for row in rows if row.get("status") == "fitted"]
    pooled = [row for row in rows if str(row.get("pooled", "")).lower() in {"true", "1"}]
    usable = [row for row in rows if row.get("omega0") not in (None, "")
              and row.get("disposition") not in {"declined", "held"}]
    expected_target = [row for row in usable
                       if row.get("branch_id") in {None, "", "acoustic"}]
    source_after = _tree_fingerprint(source)
    evidence_root = source / "report" / "fork-evidence"
    target_raw_ref = "report/fork-evidence/target-raw-tavi.json"
    _write_json(source / target_raw_ref, target_data)
    row_evidence = [{
        "scan_id": row.get("scan_id") or row.get("scan"),
        "status": row.get("status"), "verdict": row.get("verdict"),
        "disposition": row.get("disposition"), "omega0": row.get("omega0"),
        "branch_id": row.get("branch_id"), "pooled": row.get("pooled"),
        "target_delivery_eligible": row.get("target_delivery_eligible"),
    } for row in rows]
    analysis = {
        "contract": "isar.eig4_slice8_positive_analysis/o36.8.1",
        "job_id": target_submit["job_id"], "exact_request": target_body,
        "raw_tavi_json_ref": target_raw_ref,
        "returned_scan_values_1": (target_data.get("result") or {}).get("scan_values_1"),
        "ordinary_analysis_command": "isar run", "returncode": process.returncode,
        "result_rows": row_evidence,
        "pooled_rows": [row for row in row_evidence if str(row.get("pooled")).lower() in {"true", "1"}],
        "card_state_excerpt": {
            "dispersion_prior_count": len(state_b.get("dispersion_prior") or []),
            "acquisition_provenance_count": len(state_b.get("acquisition_provenance") or []),
        },
        "usable_target_delivery": bool(usable),
        "expected_acoustic_target_delivered": bool(expected_target),
        "source_workspace_fingerprint_before": source_before,
        "source_workspace_fingerprint_after": source_after,
        "isolated_workspace_fingerprint": _tree_fingerprint(branch_b),
        "input_scan_sha256": hashlib.sha256(saved.read_bytes()).hexdigest(),
    }
    fork = {
        "contract": "isar.eig4_slice8_case1_counterfactual_fork/o36.8.1",
        "source_tree_fingerprint": source_before,
        "branch_a_initial_tree_fingerprint": branch_a_initial,
        "branch_b_initial_tree_fingerprint": branch_b_initial,
        "source_master_revision": json.loads(
            (source / raw["prior_source_record_ref"]).read_text(encoding="utf-8"))[
                "source_master_revision"],
        "scientific_action_fingerprint": raw["scientific_action_fingerprint"],
        "realized_comparison_fingerprint": comparison.realized_comparison_fingerprint,
        "classified_realized_comparison_ref": dict(raw["realized_comparison_ref"]),
        "legacy_submission_exact": (
            physical.get("proposal_fingerprint") == comparison.selected_request_fingerprint
            and (legacy_client.submitted_body.get("parameters") or {}).get("scan_command1")
            == legacy.scan_command),
        "legacy_returned_grid_exact": canonical_json(returned_legacy)
        == canonical_json(list(legacy.executed_energies)),
        # Card-state acquisition provenance is the analysis-facing half of the
        # production record.  The physical-submission event is the half that
        # authenticates scientific action identity.  Join the two by their
        # acquisition/job identities and then require comparison, request,
        # grid, and action identity across the composed record.
        "legacy_acquisition_provenance_exact": (
            acquisition.get("acquisition_id") == physical.get("acquisition_id")
            and acquisition.get("job_id") == physical.get("job_id")
            and acquisition.get("scan_design_realized_comparison_fingerprint")
            == physical.get("scan_design_realized_comparison_fingerprint")
            == comparison.realized_comparison_fingerprint
            and acquisition.get("proposal_fingerprint")
            == physical.get("proposal_fingerprint")
            == comparison.selected_request_fingerprint
            and acquisition.get("scan_design_realized_selected_request_fingerprint")
            == physical.get("scan_design_realized_selected_request_fingerprint")
            == comparison.selected_request_fingerprint
            and acquisition.get("scan_design_realized_selected_grid_fingerprint")
            == physical.get("scan_design_realized_selected_grid_fingerprint")
            == comparison.selected_grid_fingerprint
            and physical.get("scientific_action_fingerprint")
            == raw["scientific_action_fingerprint"]),
        "target_request_from_captured_sidecar": (
            target_body["parameters"]["scan_command1"] == target.scan_command),
        "target_returned_grid_exact": canonical_json(
            (target_data.get("result") or {}).get("scan_values_1"))
            == canonical_json(list(target.executed_energies)),
        "target_rejoined_only_in_branch_b": saved.exists() and not any(
            str(item.get("job_id")) == str(target_submit["job_id"])
            for item in state_a.get("acquisition_provenance") or []),
        "legacy_physical_submission": physical,
        "legacy_acquisition_provenance": acquisition,
    }
    _write_json(evidence_root / "positive-analysis.json", analysis)
    _write_json(evidence_root / "case1-fork.json", fork)
    raw["analysis_evidence_ref"] = "report/fork-evidence/positive-analysis.json"
    raw["fork_evidence_ref"] = "report/fork-evidence/case1-fork.json"
    return {"analysis": analysis, "fork": fork}


def _grid_fault_provenance(
        state: Mapping[str, Any], emitted: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select the acquisition record for the exact mismatching physical job."""
    payload = emitted.get("payload")
    job_id = payload.get("job_id") if isinstance(payload, Mapping) else None
    matches = [
        item for item in state.get("acquisition_provenance") or []
        if isinstance(item, Mapping) and item.get("job_id") == job_id
    ]
    if job_id is None or len(matches) != 1:
        raise gate.GateEvidenceError(
            "grid-order fault did not resolve to one exact acquisition-provenance job")
    return matches[0]


def run_grid_order_fault(raw: Mapping[str, Any], *, case_seed: int) -> dict[str, Any]:
    """Inject a returned-grid order fault through the production completion seam."""
    source = Path(raw["source_workspace"])
    fault_workspace = source.parent.parent / "grid-order-fault-workspace"
    if fault_workspace.exists():
        shutil.rmtree(fault_workspace)
    shutil.copytree(source, fault_workspace)
    _proposal, wrapper, events = _run_controller_completion(
        raw, fault_workspace, fault_workspace.parent / "grid-order-fault-capture",
        case_seed=case_seed, reverse_returned_grid=True)
    emitted = next(event for event in events if event["kind"] == "exception"
                   and (event.get("payload") or {}).get("action")
                   == "executed_grid_mismatch")
    state = read_card_state(fault_workspace)
    provenance = _grid_fault_provenance(state, emitted)
    rows = read_results_csv(fault_workspace)
    fitted = [row for row in rows if row.get("status") == "fitted"]
    pooled = [row for row in rows if str(row.get("pooled", "")).lower() in {"true", "1"}]
    evidence_root = source / "report" / "fault-evidence"
    injected = {
        "contract": "isar.eig4_slice8_injected_grid/o36.8.1",
        "scan_values_1": (wrapper.returned_data.get("result") or {}).get("scan_values_1"),
    }
    analysis = {
        "contract": "isar.eig4_slice8_fault_post_analysis/o36.8.1",
        "ordinary_analysis_continued": True,
        "fitted_rows": [{"scan_id": row.get("scan_id") or row.get("scan"),
                          "status": row.get("status"), "omega0": row.get("omega0")}
                         for row in fitted],
        "pooled_rows": [{"scan_id": row.get("scan_id") or row.get("scan"),
                          "status": row.get("status"), "omega0": row.get("omega0")}
                         for row in pooled],
    }
    _write_json(evidence_root / "injected-grid.json", injected)
    _write_json(evidence_root / "emitted-event.json", emitted)
    _write_json(evidence_root / "mismatch-provenance.json", provenance)
    _write_json(evidence_root / "post-analysis.json", analysis)
    return {
        "contract": gate.REPAIR_FAULT_EVIDENCE_CONTRACT,
        "fault_id": "s8-f01-grid-order-mismatch",
        "source_case_id": "s8-c01-strong-confirmed",
        "source_workspace": str(source.resolve()),
        "injected_grid_ref": "report/fault-evidence/injected-grid.json",
        "event_ref": "report/fault-evidence/emitted-event.json",
        "mismatch_provenance_ref": "report/fault-evidence/mismatch-provenance.json",
        "post_analysis_ref": "report/fault-evidence/post-analysis.json",
    }


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
    evidence_update._store_branch_beliefs(state, t_round=5.0, period=2.0)
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


def build_repair_development_inputs(output: Path, parameters: Mapping[str, Any], *,
                                    execute_live: bool = False) -> dict[str, Any]:
    """Build the o36.8.1 fixtures; live mode executes the Case 1 fork and grid fault."""
    if parameters.get("contract") != DEVELOPMENT_SPEC_CONTRACT:
        raise gate.GateEvidenceError("repair uses the unchanged Slice 8 fixture parameters")
    case_by_id = {item["case_id"]: item for item in gate.CASE_DEFINITIONS}
    raw_cases: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, Any] = {}
    fixture_rows: list[dict[str, Any]] = []
    eligible_inputs: dict[str, EligibilityInput] = {}
    for case_id in (
            "s8-c01-strong-confirmed", "s8-c02-weak-unresolved-confirmed",
            "s8-c03-near-elastic-selection-time"):
        definition = case_by_id[case_id]
        physical = parameters["eligible_cases"].get(case_id)
        if not isinstance(physical, Mapping):
            raise gate.GateEvidenceError(f"repair fixture lacks {case_id}")
        q = tuple(float(item) for item in physical["q"])
        raw, detail = _persist_repair_eligible_case(
            output / "source-workspaces" / case_id,
            case_id=case_id, case_seed=definition["case_generation_seed"], q=q,
            draw_energies=physical["draw_energies_meV"],
            target_energy=float(physical["target_energy_meV"]),
            prediction_sigma=float(physical["prediction_sigma_meV"]),
            amplitude=float(physical["amplitude"]), ncount=int(physical["ncount"]),
            survey=bool(physical["survey"]))
        raw["analysis_evidence_ref"] = None
        raw["fork_evidence_ref"] = None
        raw["paired_block_deltas_per_executed_point_nats"] = list(detail["blocks"])
        raw_cases[case_id] = raw
        diagnostics[case_id] = detail
        eligible_inputs[case_id], _summary = gate.deserialize_repair_eligibility_input(
            raw["eligibility_source"])
        fixture_rows.append(_fixture_definition(
            definition, physical,
            source_kind=("deterministic_tavi_with_synthetic_predictive_fixture"
                         if case_id == "s8-c01-strong-confirmed"
                         else "deterministic_synthetic")))

    if execute_live:
        run_case1_counterfactual_fork(
            raw_cases["s8-c01-strong-confirmed"], case_seed=8040801)
        fault = run_grid_order_fault(
            raw_cases["s8-c01-strong-confirmed"], case_seed=8040801)
    else:
        fault = {
            "contract": gate.REPAIR_FAULT_EVIDENCE_CONTRACT,
            "fault_id": "s8-f01-grid-order-mismatch",
            "source_case_id": "s8-c01-strong-confirmed",
            "source_workspace": raw_cases["s8-c01-strong-confirmed"]["source_workspace"],
            "injected_grid_ref": None, "event_ref": None,
            "mismatch_provenance_ref": None, "post_analysis_ref": None,
        }

    strong_physical = parameters["eligible_cases"]["s8-c01-strong-confirmed"]
    strong_q = tuple(float(item) for item in strong_physical["q"])
    for case_id, mutation in (
            ("s8-c04-forked-target", "fork"),
            ("s8-c05-projection-join-mismatch", "projection"),
            ("s8-c06-constrained-selection", "constrained")):
        definition = case_by_id[case_id]
        intent = _canonical_intent(
            q=strong_q, target_energy=float(strong_physical["target_energy_meV"]),
            case_id=case_id)
        _artifact, source = _predictive_bundle(
            q=strong_q, draw_energies=strong_physical["draw_energies_meV"],
            target_energy=float(strong_physical["target_energy_meV"]),
            amplitude=float(strong_physical["amplitude"]),
            constrained=mutation == "constrained",
            seed=definition["case_generation_seed"], intent=intent)
        if mutation == "fork":
            source = EligibilityInput.from_current_contract(
                action_intent=source.action_intent,
                action_provenance=source.action_provenance,
                target_outcome={**dict(source.target_outcome), "modality": "fork"},
                selection_record=source.selection_record, qoi_evidence=source.qoi_evidence,
                shadow_record=source.shadow_record)
        elif mutation == "projection":
            source = EligibilityInput.from_current_contract(
                action_intent=source.action_intent,
                action_provenance=source.action_provenance,
                target_outcome=source.target_outcome,
                selection_record=_selection("alternate-phonon"),
                qoi_evidence=source.qoi_evidence, shadow_record=source.shadow_record)
        raw_cases[case_id] = {
            "case_id": case_id,
            "eligibility_source": gate.serialize_repair_eligibility_input(source),
            "source_workspace": None,
            "analysis_evidence_ref": None, "fork_evidence_ref": None,
        }
        fixture_rows.append(_fixture_definition(definition, {
            "authenticated_mutation": definition["required_eligibility_code"],
            "source_case_id": "s8-c01-strong-confirmed",
            "predictive_seed": definition["case_generation_seed"],
        }))

    fixture_rows.sort(key=lambda item: [row["case_id"] for row in gate.CASE_DEFINITIONS].index(
        item["case_id"]))
    physical = {
        item["case_id"]: item["physical_parameters"] for item in fixture_rows
        if item["case_id"] in {
            "s8-c01-strong-confirmed", "s8-c02-weak-unresolved-confirmed",
            "s8-c03-near-elastic-selection-time",
        }
    }
    strong_delta, _strong_se, strong_lower = gate._paired_summary(
        diagnostics["s8-c01-strong-confirmed"]["blocks"])
    weak_delta, _weak_se, weak_lower = gate._paired_summary(
        diagnostics["s8-c02-weak-unresolved-confirmed"]["blocks"])
    regime: dict[str, Any] = {
        "contract": "isar.eig4_slice8_fixture_regime_assessment/o36.8.1",
        "parameter_set_fingerprint": semantics_fingerprint(
            "isar.eig4_slice8_fixture_parameter_set/o36.8.1", physical),
        "scoring_config_fingerprint": (
            ScanWindowShadowScoringConfig().scoring_config_fingerprint),
        "case_1": {"delta_positive": strong_delta > 0.0,
                   "lower_margin_positive": strong_lower > 0.0},
        "case_2": {"delta_positive": weak_delta > 0.0,
                   "lower_margin_positive": weak_lower > 0.0},
        "no_retuning_claim": "assessment cannot rescue a contrary frozen repaired result",
        "regime_assessment_fingerprint": "",
    }
    regime["regime_assessment_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_regime_assessment_fingerprint/o36.8.1", regime,
        "regime_assessment_fingerprint")
    fixture_definitions: dict[str, Any] = {
        "contract": gate.REPAIR_FIXTURE_DEFINITIONS_CONTRACT,
        "cases": fixture_rows, "regime_assessment": regime,
        "fixture_definitions_fingerprint": "",
    }
    fixture_definitions["fixture_definitions_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_definitions_fingerprint/o36.8.1",
        fixture_definitions, "fixture_definitions_fingerprint")
    gate.validate_repair_fixture_definitions(fixture_definitions)
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
        cases.append(raw)
    development = {
        "contract": REPAIR_DEVELOPMENT_SPEC_CONTRACT,
        "fixture_definitions": fixture_definitions,
        "cases": cases,
        "faults": [fault],
        "diagnostics": diagnostics,
    }
    _write_json(output / "repair-fixture-definitions.json", fixture_definitions)
    _write_json(output / "repair-development-inputs.json", development)
    return development


def validate_replacement_parameters(
        parameters: Mapping[str, Any], protocol: Mapping[str, Any],
        table: Mapping[str, Any]) -> None:
    validate_case2_calibration_table(table, protocol)
    if set(parameters) != {
            "contract", "eligible_cases", "positive_analysis_outcome",
            "calibration_binding"} or parameters.get(
                "contract") != REPLACEMENT_DEVELOPMENT_SPEC_CONTRACT:
        raise gate.GateEvidenceError("o36.8.2 fixture parameter fields are invalid")
    original = json.loads((_REPO_ROOT / "tools" / "stats" /
                           "eig_slice8_fixture_parameters.json").read_text(
                               encoding="utf-8"))
    if (parameters["positive_analysis_outcome"] != original["positive_analysis_outcome"]
            or set(parameters["eligible_cases"]) != set(original["eligible_cases"])):
        raise gate.GateEvidenceError("o36.8.2 changed an unapproved fixture field")
    for case_id, original_physical in original["eligible_cases"].items():
        actual = parameters["eligible_cases"][case_id]
        if case_id == "s8-c02-weak-unresolved-confirmed":
            expected = {**original_physical, "amplitude": table["selected_amplitude"]}
        else:
            expected = original_physical
        if actual != expected:
            raise gate.GateEvidenceError(
                f"o36.8.2 changed unapproved physical parameters for {case_id}")
    if parameters["calibration_binding"] != {
            "protocol_fingerprint": protocol["protocol_fingerprint"],
            "calibration_table_fingerprint": table["calibration_table_fingerprint"],
            "reproducibility_fingerprint": (
                gate.CASE2_CALIBRATION_REPRODUCIBILITY_FINGERPRINT),
            "selected_ladder_index": table["selected_ladder_index"],
            "selected_amplitude": table["selected_amplitude"]}:
        raise gate.GateEvidenceError("o36.8.2 fixture parameters do not bind calibration")


def build_replacement_development_inputs(
        output: Path, parameters: Mapping[str, Any], protocol: Mapping[str, Any],
        calibration_table: Mapping[str, Any], reproducibility: Mapping[str, Any], *,
        execute_live: bool = False) -> dict[str, Any]:
    """Build the o36.8.2 pre-freeze fixtures with only Case 2 amplitude revised."""
    validate_replacement_parameters(parameters, protocol, calibration_table)
    validate_case2_reproducibility(reproducibility, calibration_table)
    repair_parameters = {
        "contract": DEVELOPMENT_SPEC_CONTRACT,
        "eligible_cases": parameters["eligible_cases"],
        "positive_analysis_outcome": parameters["positive_analysis_outcome"],
    }
    development = build_repair_development_inputs(
        output, repair_parameters, execute_live=execute_live)
    selected = calibration_table["rows"][calibration_table["selected_ladder_index"]]
    observed = development["diagnostics"]["s8-c02-weak-unresolved-confirmed"]
    observed_delta, observed_se, observed_lower = gate._paired_summary(observed["blocks"])
    if (observed["blocks"]
            != selected["paired_block_deltas_per_executed_point_nats"]
            or observed_delta != selected["delta_information_per_executed_point_nats"]
            or observed_se != selected["delta_standard_error_per_executed_point_nats"]
            or observed_lower != selected["two_standard_error_lower_margin_nats"]
            or observed["scorer_seed_evidence"] != selected["scorer_seed_evidence"]
            or observed["score_set_fingerprint"] != selected["score_set_fingerprint"]):
        raise gate.GateEvidenceError(
            "o36.8.2 pre-freeze Case 2 does not reproduce the selected calibration row")
    cases = development["fixture_definitions"]["cases"]
    physical = {
        item["case_id"]: item["physical_parameters"] for item in cases
        if item["case_id"] in {
            "s8-c01-strong-confirmed", "s8-c02-weak-unresolved-confirmed",
            "s8-c03-near-elastic-selection-time",
        }
    }
    strong_delta, _strong_se, strong_lower = gate._paired_summary(
        development["diagnostics"]["s8-c01-strong-confirmed"]["blocks"])
    regime: dict[str, Any] = {
        "contract": "isar.eig4_slice8_fixture_regime_assessment/o36.8.2",
        "parameter_set_fingerprint": semantics_fingerprint(
            "isar.eig4_slice8_fixture_parameter_set/o36.8.2", physical),
        "scoring_config_fingerprint": (
            ScanWindowShadowScoringConfig().scoring_config_fingerprint),
        "case_1": {"delta_positive": strong_delta > 0.0,
                   "lower_margin_positive": strong_lower > 0.0},
        "case_2": {"delta_positive": observed_delta > 0.0,
                   "lower_margin_positive": observed_lower > 0.0},
        "calibration_table_fingerprint": calibration_table[
            "calibration_table_fingerprint"],
        "no_post_selection_retuning_claim": (
            "first qualifying predeclared amplitude selected; no later row can replace it"),
        "regime_assessment_fingerprint": "",
    }
    regime["regime_assessment_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_regime_assessment_fingerprint/o36.8.2",
        regime, "regime_assessment_fingerprint")
    fixture_definitions: dict[str, Any] = {
        "contract": gate.REPLACEMENT_FIXTURE_DEFINITIONS_CONTRACT,
        "cases": cases,
        "regime_assessment": regime,
        "calibration_binding": dict(parameters["calibration_binding"]),
        "fixture_definitions_fingerprint": "",
    }
    fixture_definitions["fixture_definitions_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_definitions_fingerprint/o36.8.2",
        fixture_definitions, "fixture_definitions_fingerprint")
    gate.validate_replacement_fixture_definitions(fixture_definitions)
    development.update({
        "contract": REPLACEMENT_DEVELOPMENT_SPEC_CONTRACT,
        "fixture_definitions": fixture_definitions,
        "calibration_binding": dict(parameters["calibration_binding"]),
        "calibration_reproduced_at_prefreeze": True,
    })
    _write_json(output / "replacement-fixture-definitions.json", fixture_definitions)
    _write_json(output / "replacement-development-inputs.json", development)
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
