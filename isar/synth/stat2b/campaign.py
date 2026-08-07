"""STAT-2b evidence campaign orchestration.

The production runtime never imports this module.  Its public surface is intentionally the
three-function interface re-exported from :mod:`isar.synth.stat2b`.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
import ctypes
import csv
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from isar.analyze import loop
from isar.analyze.phonon import (
    enumerate_phonon_multimode,
    enumerate_phonon_structures,
    greedy_multimode_ic,
)
from isar.analyze.prior import PhononPrior
from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor.stopping import evaluate_stop, summarize_grid
from isar.schema import card as card_mod
from isar.schema.statistics import semantics_fingerprint
from isar.schema.steering import ParamConstraints
from isar.session import scan_delivery
from isar.session import session_prior
from isar.synth.stat2b.criteria import (
    NULL_LABELS,
    CandidateEvidence,
    CriterionSelection,
    evidence_from_results,
    fit_null_candidates,
    mean_poisson_kl,
    mean_poisson_log_score,
    oracle_hypotheses,
    select_by_criterion,
)
from isar.synth.stat2b.spec import CampaignSpec
from isar.synth.stat2b.truth import TruthDataset, _dataset_id, generate_dataset


RUN_SCHEMA = "isar.stat2b_run/1"
RECORD_SCHEMA = "isar.stat2b_dataset_result/1"
DELIVERY_INPUT_SCHEMA = "isar.stat2b_delivery_inputs/1"
DELIVERY_CONTEXT_SCHEMA = "isar.stat2b_delivery_context/2"
SUMMARY_SCHEMA = "isar.stat2b_development_summary/1"
HELDOUT_SUMMARY_SCHEMA = "isar.stat2b_heldout_summary/1"
GATE_SCHEMA = "isar.stat2b_heldout_gate/1"
RUNTIME_LEDGER_SCHEMA = "isar.stat2b_runtime_ledger/1"
THREAD_ENVIRONMENT_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "BLIS_NUM_THREADS",
)

STANDARD_ARMS = (
    ("legacy_aic", "legacy", "aic"),
    ("unconstrained_aic", "standard", "aic"),
    ("unconstrained_aicc", "standard", "aicc"),
    ("unconstrained_bic", "standard", "bic"),
    ("unconstrained_aic_null", "standard_null", "aic"),
    ("unconstrained_aicc_null", "standard_null", "aicc"),
)
ORACLE_ARMS = (
    ("oracle_aic", "oracle", "aic"),
    ("oracle_aicc", "oracle", "aicc"),
    ("oracle_bic", "oracle", "bic"),
)
CAMPAIGN_SOURCE_PATHS = (
    "isar/synth/stat2b/__init__.py",
    "isar/synth/stat2b/__main__.py",
    "isar/synth/stat2b/campaign.py",
    "isar/synth/stat2b/criteria.py",
    "isar/synth/stat2b/spec.py",
    "isar/synth/stat2b/truth.py",
    "isar/analyze/components.py",
    "isar/analyze/confidence.py",
    "isar/analyze/context.py",
    "isar/analyze/descriptor.py",
    "isar/analyze/errors/__init__.py",
    "isar/analyze/errors/agree.py",
    "isar/analyze/errors/bayes.py",
    "isar/analyze/errors/bootstrap.py",
    "isar/analyze/errors/profile.py",
    "isar/analyze/ic.py",
    "isar/analyze/loop.py",
    "isar/analyze/model_manifest.py",
    "isar/analyze/phonon.py",
    "isar/analyze/prior.py",
    "isar/analyze/select.py",
    "isar/analyze/shadow_selection.py",
    "isar/analyze/structures.py",
    "isar/analyze/verdict.py",
    "isar/drive/objective.py",
    "isar/drive/supervisor/stopping.py",
    "isar/schema/card.py",
    "isar/schema/statistics.py",
    "isar/schema/steering.py",
    "isar/session/driver.py",
    "isar/session/evidence_update.py",
    "isar/session/scan_delivery.py",
    "isar/session/master_delivery.py",
    "isar/session/branch_model.py",
    "isar/session/session_prior.py",
    "isar/synth/fit.py",
)
CAMPAIGN_SOURCE_ROOTS = (
    "isar/analyze",
    "isar/schema",
    "isar/session",
    "isar/synth/stat2b",
)


@dataclass(frozen=True)
class CampaignRun:
    campaign_id: str
    phase: str
    output_dir: Path
    spec_fingerprint: str
    source_hashes: Mapping[str, str]
    records: tuple[dict[str, Any], ...]
    manifest: Mapping[str, Any]


class _CampaignTaskError(RuntimeError):
    """A dataset task failed before its checkpoint could be persisted."""

    def __init__(self, scenario_id: str, realization_index: int):
        self.scenario_id = scenario_id
        self.realization_index = realization_index
        super().__init__(
            "STAT-2b dataset task failed before checkpoint persistence: "
            f"{scenario_id}-{realization_index:05d}"
        )


def _strict_json_value(value):
    if isinstance(value, np.generic):
        return _strict_json_value(value.item())
    if isinstance(value, np.ndarray):
        return [_strict_json_value(item) for item in value.tolist()]
    if isinstance(value, Mapping):
        return {str(key): _strict_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _json_bytes(value) -> bytes:
    return (
        json.dumps(
            _strict_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_json(path: Path, value) -> None:
    _atomic_write(path, _json_bytes(value))


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _source_hashes(repo_root: Path) -> dict[str, str]:
    hashes = {}
    relatives = set(CAMPAIGN_SOURCE_PATHS)
    for root in CAMPAIGN_SOURCE_ROOTS:
        relatives.update(
            path.relative_to(repo_root).as_posix()
            for path in (repo_root / root).rglob("*.py")
        )
    for relative in sorted(relatives):
        path = repo_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"campaign source is missing: {relative}")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _source_fingerprint(source_hashes: Mapping[str, str]) -> str:
    return semantics_fingerprint(
        "isar.stat2b_campaign_sources/1", dict(sorted(source_hashes.items()))
    )


def _process_affinity() -> dict[str, Any]:
    if os.name != "nt":
        affinity = (
            sorted(int(cpu) for cpu in os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else None
        )
        return {
            "kind": "sched_getaffinity" if affinity is not None else "unavailable",
            "logical_processor_count": len(affinity) if affinity is not None else None,
            "logical_processors": affinity,
        }
    process_mask = ctypes.c_size_t()
    system_mask = ctypes.c_size_t()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    available = bool(
        kernel32.GetProcessAffinityMask(
            kernel32.GetCurrentProcess(),
            ctypes.byref(process_mask),
            ctypes.byref(system_mask),
        )
    )
    return {
        "kind": "windows_process_affinity_mask",
        "available": available,
        "process_mask_hex": hex(process_mask.value) if available else None,
        "system_mask_hex": hex(system_mask.value) if available else None,
        "logical_processor_count": (
            int(process_mask.value).bit_count() if available else None
        ),
    }


def _blas_implementation() -> dict[str, Any]:
    config = np.__config__.show(mode="dicts")
    blas = config.get("Build Dependencies", {}).get("blas", {})
    return {
        key: blas.get(key)
        for key in (
            "name",
            "version",
            "detection method",
            "openblas configuration",
        )
    }


def _runtime_provenance(dataset_workers: int = 1) -> dict[str, Any]:
    physical_cores = os.environ.get("ISAR_PHYSICAL_CORES")
    try:
        physical_core_count = (
            int(physical_cores) if physical_cores is not None else None
        )
    except ValueError:
        physical_core_count = None
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "physical_core_count": physical_core_count,
        "logical_processor_count": os.cpu_count(),
        "dataset_worker_count": int(dataset_workers),
        "blas_implementation": _blas_implementation(),
        "thread_environment": {
            name: os.environ.get(name) for name in THREAD_ENVIRONMENT_VARIABLES
        },
        "process_affinity": _process_affinity(),
        "packages": {
            package: metadata.version(package)
            for package in ("numpy", "scipy", "iminuit", "lmfit", "dynesty")
        },
    }


def _runtime_fingerprint(provenance: Mapping[str, Any]) -> str:
    return semantics_fingerprint("isar.stat2b_numerical_runtime/2", provenance)


def _validate_parallel_runtime(
    provenance: Mapping[str, Any], dataset_workers: int
) -> None:
    if dataset_workers <= 1:
        return
    thread_environment = provenance["thread_environment"]
    invalid = [
        name
        for name in THREAD_ENVIRONMENT_VARIABLES
        if thread_environment.get(name) != "1"
    ]
    if invalid:
        raise RuntimeError(
            "STAT-2b parallel execution requires numerical thread caps before "
            f"Python starts: {', '.join(invalid)}"
        )
    physical_cores = provenance.get("physical_core_count")
    if not isinstance(physical_cores, int) or physical_cores <= 0:
        raise RuntimeError(
            "STAT-2b parallel execution requires ISAR_PHYSICAL_CORES provenance"
        )
    affinity_count = provenance.get("process_affinity", {}).get(
        "logical_processor_count"
    )
    if isinstance(affinity_count, int) and dataset_workers > affinity_count:
        raise RuntimeError(
            "STAT-2b dataset workers exceed the process-affinity processor count"
        )


def _record_fingerprint_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"record_fingerprint", "runtime_seconds"}
    }


def _record_is_reusable(
    record: Mapping[str, Any],
    *,
    spec: CampaignSpec,
    scenario_id: str,
    realization_index: int,
    phase: str,
    source_fingerprint: str,
    runtime_fingerprint: str,
) -> bool:
    scenario = spec.scenario(scenario_id)
    fingerprint = record.get("record_fingerprint")
    return bool(
        record.get("schema") == RECORD_SCHEMA
        and record.get("campaign_id") == spec.campaign_id
        and record.get("campaign_spec_fingerprint") == spec.fingerprint
        and record.get("campaign_source_fingerprint") == source_fingerprint
        and record.get("numerical_runtime_fingerprint") == runtime_fingerprint
        and record.get("phase") == phase
        and record.get("scenario_id") == scenario_id
        and record.get("scenario_fingerprint") == scenario.fingerprint
        and record.get("realization_index") == realization_index
        and record.get("dataset_id")
        == _dataset_id(spec, scenario, phase, realization_index)
        and isinstance(fingerprint, str)
        and fingerprint
        == semantics_fingerprint(
            RECORD_SCHEMA, _record_fingerprint_payload(record)
        )
    )


def _campaign_card() -> card_mod.Card:
    policy = card_mod.CardPolicy(
        facility="SYNTH",
        instrument="STAT2B_SYNTH",
        format_version="2026",
        plugin="stat2b_evidence_only",
        detectors=[card_mod.DetectorSpec("stat2b", 0, "stat2b", True)],
        energy_column="energy",
        monitor_column="monitor",
        counting_mode="time",
        normalize_by="none",
        raw_abscissa_kind="energy",
        energy_status="absolute",
        measurement_pattern="stat2b",
        alignment_pattern="stat2b_alignment",
        temperature_rule={},
        collation={},
        fitting={},
    )
    return card_mod.Card(policy, card_mod.CardState())


def _delivery_prior_and_card(
    dataset: TruthDataset, scenario
) -> tuple[PhononPrior | None, card_mod.Card]:
    card = _campaign_card()
    design = dataset.delivery_design
    if design is None:
        return None, card
    prior_omega = float(design["prior_omega"])
    prior = PhononPrior(
        omega0=prior_omega,
        amplitude=float(design["amplitude"]),
        omega0_sigma=float(design["omega0_sigma"]),
    )
    # Populate the real running-branch state with clean synthetic anchors.
    # The stored resolution belongs to the anchor scans and is intentionally
    # narrower than the target scan, so a near-elastic target can be in-range.
    amplitude_scale = float(design["amplitude_norm"]) * prior_omega
    for index, factor in enumerate((0.7, 0.9, 1.1, 1.3)):
        omega0 = prior_omega * factor
        session_prior.add_point(
            card.state,
            scan_id=f"STAT2B:anchor:{index}",
            sample="STAT2B",
            T=float(scenario.T),
            q=[float(index), 0.0, 0.0],
            omega0=omega0,
            modality="fold",
            verdict="resolved",
            gamma=float(design["gamma"]),
            intensity={
                "value": amplitude_scale / omega0,
                "exposure": 1.0,
                "counting_mode": "time",
                "gauss_fwhm": 0.05,
                "lorentz_fwhm": 0.0,
                "instrument": "STAT2B_SYNTH",
            },
            branch_id="acoustic",
            weight=1.0,
        )
    return prior, card


def _all_production_hypotheses(ctx) -> list:
    return list(enumerate_phonon_structures(ctx)) + list(enumerate_phonon_multimode(ctx))


def _standard_evidence(
    ctx, *, reusable_results: Sequence[Any] = ()
) -> tuple[list[CandidateEvidence], dict[str, Any]]:
    unconstrained = replace(ctx, constraints=ParamConstraints())
    bundle = loop.fit_shadow_candidates(
        unconstrained, reusable_results=reusable_results
    )
    hypotheses = _all_production_hypotheses(unconstrained)
    evidence = evidence_from_results(
        bundle.results, hypotheses, unconstrained, source="standard"
    )
    provenance = {
        "execution_kind": (
            "exact_reuse"
            if bundle.results and len(bundle.reused_labels) == len(bundle.results)
            else "partial_reuse"
            if bundle.reused_labels
            else "fresh"
        ),
        "offered_labels": list(bundle.offered_labels),
        "attempted_labels": list(bundle.attempted_labels),
        "converged_labels": list(bundle.converged_labels),
        "failed_labels": list(bundle.failed_labels),
        "reused_labels": list(bundle.reused_labels),
        "freshly_fitted_labels": list(bundle.freshly_fitted_labels),
        "multimode_route": bundle.multimode_route,
    }
    return evidence, provenance


def _legacy_evidence(ctx) -> tuple[list[CandidateEvidence], dict[str, Any]]:
    results = loop.fit_structures_two_stage(ctx)
    hypotheses = _all_production_hypotheses(ctx)
    evidence = evidence_from_results(results, hypotheses, ctx, source="legacy")
    labels = [candidate.label for candidate in evidence]
    return evidence, {
        "execution_kind": "fresh",
        "offered_labels": labels,
        "attempted_labels": labels,
        "converged_labels": [
            candidate.label for candidate in evidence if candidate.optimizer_converged
        ],
        "failed_labels": [
            candidate.label for candidate in evidence if not candidate.optimizer_converged
        ],
        "reused_labels": [],
        "freshly_fitted_labels": labels,
        "multimode_route": (
            "residual_triggered"
            if any(int(getattr(candidate.result, "n_modes", 1)) > 1 for candidate in evidence)
            else "not_offered"
        ),
    }


def _oracle_evidence(
    ctx, standard: Sequence[CandidateEvidence], nulls: Sequence[CandidateEvidence]
) -> tuple[list[CandidateEvidence], dict[str, Any]]:
    unconstrained = replace(ctx, constraints=ParamConstraints())
    by_label = {candidate.label: candidate for candidate in standard}
    one_results = [
        candidate.result
        for candidate in standard
        if int(getattr(candidate.result, "n_modes", 1)) == 1
        and candidate.optimizer_converged
    ]
    best_one = loop._best_converged(one_results)
    fresh = []
    for hypothesis in oracle_hypotheses(unconstrained):
        if hypothesis.label in by_label:
            continue
        x0 = (
            greedy_multimode_ic(hypothesis.build(), unconstrained, best_one)
            if best_one is not None
            else None
        )
        result = loop.fit_hypothesis(hypothesis, unconstrained, x0=x0)
        fresh.extend(
            evidence_from_results(
                [result], [hypothesis], unconstrained, source="oracle_missing"
            )
        )
    evidence = list(standard) + fresh + list(nulls)
    return evidence, {
        "execution_kind": "partial_reuse" if fresh else "exact_reuse",
        "reused_labels": [
            candidate.label for candidate in list(standard) + list(nulls)
        ],
        "freshly_fitted_labels": [candidate.label for candidate in fresh],
        "offered_labels": [candidate.label for candidate in evidence],
        "attempted_labels": [candidate.label for candidate in evidence],
        "converged_labels": [
            candidate.label for candidate in evidence if candidate.optimizer_converged
        ],
        "failed_labels": [
            candidate.label for candidate in evidence if not candidate.optimizer_converged
        ],
        "multimode_route": "oracle_complete",
    }


def _selection(
    evidence: Sequence[CandidateEvidence], criterion: str
) -> CriterionSelection:
    return select_by_criterion(
        evidence, criterion, required_labels=[candidate.label for candidate in evidence]
    )


def _peaks_for_selected(candidate: CandidateEvidence | None):
    if candidate is None or int(getattr(candidate.result, "n_modes", 1)) < 2:
        return None
    modes = list(getattr(candidate.result, "modes", ()) or ())
    return tuple(
        {"omega0": float(mode["omega0"]), "sigma": 0.5}
        for mode in sorted(modes, key=lambda item: float(item["omega0"]))
    )


def _target_rank_hint(
    candidate: CandidateEvidence, manifest: Mapping[str, Any]
) -> float | None:
    modes = sorted(
        list(getattr(candidate.result, "modes", ()) or ()),
        key=lambda item: float(item["omega0"]),
    )
    if not modes:
        return None
    rank = manifest.get("target_rank")
    if rank == "lower":
        return float(modes[0]["omega0"])
    if rank == "upper":
        return float(modes[-1]["omega0"])
    return float(modes[0]["omega0"])


def _delivery_context_fingerprint(dataset: TruthDataset) -> str:
    ctx = dataset.fit_context
    constraints = getattr(ctx, "constraints", None)
    return semantics_fingerprint(
        DELIVERY_CONTEXT_SCHEMA,
        {
            "data_likelihood_fingerprint": loop.data_likelihood_fingerprint(ctx),
            "constraints": {
                "soft": dict(sorted((getattr(constraints, "soft", None) or {}).items())),
                "hard": dict(sorted((getattr(constraints, "hard", None) or {}).items())),
                "origins": dict(
                    sorted((getattr(constraints, "origins", None) or {}).items())
                ),
            },
            "delivery_design": _strict_json_value(dataset.delivery_design),
        },
    )


def _delivery_inputs(
    dataset: TruthDataset,
    candidate: CandidateEvidence,
    spec: CampaignSpec,
) -> dict[str, Any]:
    """Declare exactly the candidate-specific inputs consumed by delivery."""

    return {
        "selected_label": str(candidate.label),
        "delivery_context_fingerprint": _delivery_context_fingerprint(dataset),
        "predeclared_target_rank": dataset.truth_manifest.get("target_rank"),
        "candidate_target_hint": _target_rank_hint(candidate, dataset.truth_manifest),
        "candidate_multimode_peak_hints": _strict_json_value(
            _peaks_for_selected(candidate)
        ),
        "errors_tier": "full",
        "bootstrap_replicates": int(spec.delivery_replicates),
        "delivery_seed": int(_realization_seed(dataset, candidate.label)),
    }


def _delivery_input_fingerprint(inputs: Mapping[str, Any]) -> str:
    return semantics_fingerprint(
        DELIVERY_INPUT_SCHEMA, _strict_json_value(dict(inputs))
    )


def _full_delivery(
    dataset: TruthDataset,
    spec: CampaignSpec,
    *,
    delivery_inputs: Mapping[str, Any],
) -> dict[str, Any]:
    scenario = spec.scenario(str(dataset.truth_manifest["scenario_id"]))
    expected_keys = {
        "selected_label",
        "delivery_context_fingerprint",
        "predeclared_target_rank",
        "candidate_target_hint",
        "candidate_multimode_peak_hints",
        "errors_tier",
        "bootstrap_replicates",
        "delivery_seed",
    }
    if set(delivery_inputs) != expected_keys:
        raise ValueError("STAT-2b delivery inputs do not match the canonical record")
    if (
        delivery_inputs["delivery_context_fingerprint"]
        != _delivery_context_fingerprint(dataset)
    ):
        raise ValueError("STAT-2b delivery context does not match the declared inputs")
    if (
        delivery_inputs["predeclared_target_rank"]
        != dataset.truth_manifest.get("target_rank")
    ):
        raise ValueError("STAT-2b target rank does not match the declared inputs")
    if delivery_inputs["errors_tier"] != "full":
        raise ValueError("STAT-2b delivery requires the full errors tier")
    if int(delivery_inputs["bootstrap_replicates"]) != spec.delivery_replicates:
        raise ValueError("STAT-2b bootstrap count does not match the campaign spec")

    selected_label = str(delivery_inputs["selected_label"])
    target_hint = delivery_inputs["candidate_target_hint"]
    peaks = delivery_inputs["candidate_multimode_peak_hints"]
    errors_tier = str(delivery_inputs["errors_tier"])
    bootstrap_replicates = int(delivery_inputs["bootstrap_replicates"])
    seed = int(delivery_inputs["delivery_seed"])
    ctx = replace(dataset.fit_context, acoustic_omega=target_hint)
    prior, card = _delivery_prior_and_card(dataset, scenario)
    descriptor = scan_delivery._build_descriptor(ctx)
    region_reasons: list[str] = []
    fit = scan_delivery.fit_one(
        ctx,
        card,
        prior=prior,
        M=bootstrap_replicates,
        seed=seed,
        structure_labels=[selected_label],
        peaks=peaks,
        errors_tier=errors_tier,
        boot_workers=1,
        descriptor=descriptor,
        shadow_enabled=False,
    )
    fit = scan_delivery._hard_fix_escalation(
        fit,
        ctx,
        prior,
        card,
        "STAT2B",
        ctx.T,
        [selected_label],
        peaks,
        descriptor,
        region_reasons,
        M=bootstrap_replicates,
        seed=seed,
        errors_tier=errors_tier,
        boot_workers=1,
        boot_executor=None,
        boot_adaptive=False,
    )
    fit["reasons"] = list(fit.get("reasons") or ()) + region_reasons
    fit["campaign_target_rank"] = dataset.truth_manifest.get("target_rank")
    fit["campaign_target_hint"] = target_hint
    return fit


def _certification_projection(fit: Mapping[str, Any], tolerance: float) -> dict[str, Any]:
    interval = fit.get("interval")
    omega0 = fit.get("omega0")
    halfwidth = (
        max(float(omega0) - float(interval[0]), float(interval[-1]) - float(omega0))
        if interval is not None and omega0 is not None
        else None
    )
    certifiable = bool(
        omega0 is not None
        and halfwidth is not None
        and fit.get("interval_reliable") is not False
        and fit.get("modality") != "fork"
    )
    objective = ObjectiveSpec(
        path=[(0.0, 0.0, 0.0)],
        tolerance_meV=float(tolerance),
        budget=None,
        target_branch="acoustic",
        target_selector="lowest_energy",
        analysis_limit_seconds=None,
    )
    master = {
        "hkl": [0.0, 0.0, 0.0],
        "omega0": omega0,
        "halfwidth": halfwidth,
        "usable": omega0 is not None,
        "reliable": certifiable,
        "certifiable": certifiable,
        "modality": fit.get("modality"),
        "pool_consistent": True,
        "model_robust": fit.get("modality") != "fork",
        "n_members": 1,
        "members": ["stat2b"],
        "branch_id": "acoustic",
    }
    grid = summarize_grid(objective, [], [master])
    decision = evaluate_stop(
        objective,
        grid,
        {"kind": "counting_seconds", "amount": None, "spent": 0.0},
    )
    return {
        "certified": bool(grid and grid[0].converged),
        "stop_decision": decision.state,
        "halfwidth": halfwidth,
        "certifiable": certifiable,
    }


def _target_truth(manifest: Mapping[str, Any]) -> float | None:
    index = manifest.get("target_mode_index")
    modes = manifest.get("modes") or []
    if index is None:
        return None
    return float(modes[int(index)]["omega0"])


def _fixed_target_estimate(
    fit: Mapping[str, Any], manifest: Mapping[str, Any]
) -> float | None:
    # _full_delivery binds the production scalar QoI to the predeclared target
    # rank through acoustic_omega. Its scalar interval is therefore aligned.
    return float(fit["omega0"]) if fit.get("omega0") is not None else None


def _arm_record(
    *,
    arm_id: str,
    family: str,
    criterion: str,
    selection: CriterionSelection,
    evidence: Sequence[CandidateEvidence],
    provenance: Mapping[str, Any],
    dataset: TruthDataset,
    delivery: Mapping[str, Any] | None,
    certification: Mapping[str, Any] | None,
    certification_tolerance: float,
    delivery_inputs: Mapping[str, Any] | None,
    delivery_input_fingerprint: str | None,
    delivery_reused: bool,
    delivery_reuse_group: str | None,
    delivery_source_arm: str | None,
) -> dict[str, Any]:
    by_label = {candidate.label: candidate for candidate in evidence}
    selected = by_label.get(selection.selected_label)
    manifest = dataset.truth_manifest
    target_present = bool(manifest["target_present"])
    null_selected = selection.selected_label in NULL_LABELS
    target_truth = _target_truth(manifest)
    target_estimate = (
        _fixed_target_estimate(delivery, manifest)
        if delivery is not None and not null_selected
        else None
    )
    interval = delivery.get("interval") if delivery is not None else None
    interval_covers = (
        bool(float(interval[0]) <= target_truth <= float(interval[-1]))
        if interval is not None and target_truth is not None
        else None
    )
    interval_reliable = bool(
        delivery is not None and delivery.get("interval_reliable") is not False
    )
    prediction_kl = (
        mean_poisson_kl(
            np.asarray(dataset.lambda_counts)[np.asarray(dataset.fit_context.mask, dtype=bool)],
            selected.prediction,
        )
        if selected is not None
        else None
    )
    realized_log_score = (
        mean_poisson_log_score(
            np.asarray(dataset.heldout_counts)[
                np.asarray(dataset.fit_context.mask, dtype=bool)
            ],
            selected.prediction,
        )
        if selected is not None
        else None
    )
    candidate_kls = [
        mean_poisson_kl(
            np.asarray(dataset.lambda_counts)[
                np.asarray(dataset.fit_context.mask, dtype=bool)
            ],
            candidate.prediction,
        )
        for candidate in evidence
        if candidate.optimizer_converged
    ]
    selected_n_modes = (
        0
        if null_selected
        else int((delivery or {}).get("n_modes", getattr(selected.result, "n_modes", 0)))
        if selected is not None
        else 0
    )
    truth_n_modes = len(manifest.get("modes") or ())
    certified = bool((certification or {}).get("certified", False))
    false_certification = bool(
        certified
        and (
            not target_present
            or target_truth is None
            or interval_covers is not True
            or target_estimate is None
            or abs(target_estimate - target_truth) > certification_tolerance
        )
    )
    return {
        "arm_id": arm_id,
        "family": family,
        "criterion": criterion,
        "comparison_status": selection.status,
        "failure_reason": selection.reason,
        "selected_label": selection.selected_label,
        "null_abstention": bool(null_selected),
        "selection_rows": [asdict(row) for row in selection.rows],
        "candidate_labels": [candidate.label for candidate in evidence],
        "prediction_kl": prediction_kl,
        "realized_log_score": realized_log_score,
        "candidate_route_regret": (
            max(prediction_kl - min(candidate_kls), 0.0)
            if prediction_kl is not None and candidate_kls
            else None
        ),
        "target_truth": target_truth,
        "target_estimate": target_estimate,
        "target_squared_error": (
            (target_estimate - target_truth) ** 2
            if target_estimate is not None and target_truth is not None
            else None
        ),
        "target_bias": (
            target_estimate - target_truth
            if target_estimate is not None and target_truth is not None
            else None
        ),
        "interval": interval,
        "interval_reliable": (
            delivery.get("interval_reliable") if delivery is not None else None
        ),
        "conditional_coverage": (
            interval_covers if interval_covers is not None and interval_reliable else None
        ),
        "complete_delivery_coverage": (
            bool(interval_covers and interval_reliable) if target_present else None
        ),
        "truth_n_modes": truth_n_modes,
        "selected_n_modes": selected_n_modes,
        "missed_modes": max(truth_n_modes - selected_n_modes, 0),
        "extra_modes": max(selected_n_modes - truth_n_modes, 0),
        "certified": certified,
        "false_certification": false_certification,
        "delivery": (
            {
                key: delivery.get(key)
                for key in (
                    "best_label",
                    "verdict",
                    "confidence",
                    "omega0",
                    "omega0_err",
                    "interval",
                    "interval_reliable",
                    "modality",
                    "gamma",
                    "gamma_at_bound",
                    "fit_kind",
                    "n_modes",
                    "modes",
                    "errors_tier_effective",
                    "bootstrap_M_requested",
                    "bootstrap_M_effective",
                    "campaign_target_rank",
                    "campaign_target_hint",
                    "two_stage_seconds",
                    "errors_attach_seconds",
                    "reasons",
                )
            }
            if delivery is not None
            else None
        ),
        "delivery_inputs": (
            dict(delivery_inputs) if delivery_inputs is not None else None
        ),
        "delivery_input_fingerprint": delivery_input_fingerprint,
        "delivery_reused": bool(delivery_reused),
        "delivery_reuse_group": delivery_reuse_group,
        "delivery_source_arm": delivery_source_arm,
        "certification": certification,
        "execution_provenance": dict(provenance),
    }


def _realization_seed(dataset: TruthDataset, label: str) -> int:
    digest = hashlib.sha256(f"{dataset.seed}:{label}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % (2**32 - 1)


def _run_realization(
    spec: CampaignSpec,
    scenario_id: str,
    realization_index: int,
    phase: str,
    source_fingerprint: str | None = None,
    runtime_fingerprint: str | None = None,
) -> dict[str, Any]:
    if source_fingerprint is None:
        repo_root = Path(__file__).resolve().parents[3]
        source_fingerprint = _source_fingerprint(_source_hashes(repo_root))
    if runtime_fingerprint is None:
        runtime_fingerprint = _runtime_fingerprint(_runtime_provenance())
    started = time.perf_counter()
    dataset = generate_dataset(
        spec, scenario_id, realization_index, phase=phase
    )
    scenario = spec.scenario(scenario_id)
    ctx = dataset.fit_context
    family_started = time.perf_counter()
    legacy, legacy_provenance = _legacy_evidence(ctx)
    legacy_provenance["runtime_seconds"] = time.perf_counter() - family_started
    family_started = time.perf_counter()
    reusable_results = (
        [candidate.result for candidate in legacy]
        if ctx.constraints is None or ctx.constraints.is_empty()
        else []
    )
    standard, standard_provenance = _standard_evidence(
        ctx, reusable_results=reusable_results
    )
    standard_provenance["runtime_seconds"] = time.perf_counter() - family_started
    family_started = time.perf_counter()
    nulls = fit_null_candidates(replace(ctx, constraints=ParamConstraints()))
    null_runtime = time.perf_counter() - family_started
    families: dict[str, tuple[list[CandidateEvidence], dict[str, Any]]] = {
        "legacy": (legacy, legacy_provenance),
        "standard": (standard, standard_provenance),
        "standard_null": (
            standard + nulls,
            {
                **standard_provenance,
                "freshly_fitted_labels": list(
                    standard_provenance["freshly_fitted_labels"]
                )
                + [candidate.label for candidate in nulls],
                "offered_labels": [candidate.label for candidate in standard + nulls],
                "attempted_labels": [candidate.label for candidate in standard + nulls],
                "runtime_seconds": (
                    float(standard_provenance["runtime_seconds"]) + null_runtime
                ),
            },
        ),
    }
    if scenario.oracle_required:
        family_started = time.perf_counter()
        families["oracle"] = _oracle_evidence(ctx, standard, nulls)
        families["oracle"][1]["runtime_seconds"] = (
            time.perf_counter() - family_started
        )

    arm_specs = list(STANDARD_ARMS)
    if scenario.oracle_required:
        arm_specs.extend(ORACLE_ARMS)
    selections = {
        arm_id: _selection(families[family][0], criterion)
        for arm_id, family, criterion in arm_specs
    }

    arm_candidates: dict[str, CandidateEvidence | None] = {}
    delivery_requests: dict[str, tuple[dict[str, Any], str]] = {}
    delivery_reuse: dict[str, list[str]] = {}
    delivery_group_inputs: dict[str, dict[str, Any]] = {}
    for arm_id, family, criterion in arm_specs:
        evidence, provenance = families[family]
        selection = selections[arm_id]
        selected = next(
            (
                candidate
                for candidate in evidence
                if candidate.label == selection.selected_label
            ),
            None,
        )
        arm_candidates[arm_id] = selected
        if (
            selected is not None
            and selection.status == "available"
            and selected.label not in NULL_LABELS
        ):
            inputs = _delivery_inputs(dataset, selected, spec)
            key = _delivery_input_fingerprint(inputs)
            if key in delivery_group_inputs and delivery_group_inputs[key] != inputs:
                raise RuntimeError("STAT-2b delivery-input fingerprint collision")
            delivery_group_inputs[key] = inputs
            delivery_requests[arm_id] = (inputs, key)
            delivery_reuse.setdefault(key, []).append(arm_id)

    delivery_cache: dict[str, dict[str, Any]] = {}
    delivery_sources = {
        key: min(arm_ids) for key, arm_ids in delivery_reuse.items()
    }
    for key in sorted(delivery_reuse):
        delivery_cache[key] = _full_delivery(
            dataset,
            spec,
            delivery_inputs=delivery_group_inputs[key],
        )

    arm_rows = []
    for arm_id, family, criterion in arm_specs:
        evidence, provenance = families[family]
        selection = selections[arm_id]
        selected = arm_candidates[arm_id]
        delivery = None
        certification = None
        inputs = None
        key = None
        source_arm = None
        if arm_id in delivery_requests:
            inputs, key = delivery_requests[arm_id]
            source_arm = delivery_sources[key]
            delivery = delivery_cache[key]
            certification = _certification_projection(
                delivery, spec.certification_tolerance_mev
            )
        arm_rows.append(
            _arm_record(
                arm_id=arm_id,
                family=family,
                criterion=criterion,
                selection=selection,
                evidence=evidence,
                provenance=provenance,
                dataset=dataset,
                delivery=delivery,
                certification=certification,
                certification_tolerance=spec.certification_tolerance_mev,
                delivery_inputs=inputs,
                delivery_input_fingerprint=key,
                delivery_reused=bool(source_arm and arm_id != source_arm),
                delivery_reuse_group=key,
                delivery_source_arm=source_arm,
            )
        )

    standard_labels = set(families["standard"][0][i].label for i in range(len(standard)))
    oracle_aic = selections.get("oracle_aic")
    standard_aic = selections["unconstrained_aic"]
    record = {
        "schema": RECORD_SCHEMA,
        "campaign_id": spec.campaign_id,
        "campaign_spec_fingerprint": spec.fingerprint,
        "campaign_source_fingerprint": source_fingerprint,
        "numerical_runtime_fingerprint": runtime_fingerprint,
        "phase": phase,
        "dataset_id": dataset.dataset_id,
        "scenario_id": scenario_id,
        "scenario_fingerprint": scenario.fingerprint,
        "realization_index": realization_index,
        "seed": dataset.seed,
        "truth_manifest": dataset.truth_manifest,
        "critical_strata": list(scenario.critical_strata),
        "arms": arm_rows,
        "delivery_deduplication": {
            "unique_delivery_count": len(delivery_cache),
            "arm_count_requiring_delivery": sum(len(items) for items in delivery_reuse.values()),
            "keys": {
                key: sorted(items) for key, items in sorted(delivery_reuse.items())
            },
        },
        "oracle_winner_available_in_standard": (
            oracle_aic.selected_label in standard_labels
            if oracle_aic is not None and oracle_aic.selected_label is not None
            else None
        ),
        "two_mode_omission": bool(
            scenario.oracle_required
            and oracle_aic is not None
            and oracle_aic.selected_label is not None
            and "stokes2" in oracle_aic.selected_label
            and oracle_aic.selected_label not in standard_labels
        ),
        "same_set_selection_changes": {
            "standard": {
                "aicc_vs_aic": (
                    selections["unconstrained_aicc"].selected_label
                    != standard_aic.selected_label
                ),
                "bic_vs_aic": (
                    selections["unconstrained_bic"].selected_label
                    != standard_aic.selected_label
                ),
            },
            "standard_null": {
                "aicc_vs_aic": (
                    selections["unconstrained_aicc_null"].selected_label
                    != selections["unconstrained_aic_null"].selected_label
                ),
            },
            "oracle": (
                {
                    "aicc_vs_aic": (
                        selections["oracle_aicc"].selected_label
                        != selections["oracle_aic"].selected_label
                    ),
                    "bic_vs_aic": (
                        selections["oracle_bic"].selected_label
                        != selections["oracle_aic"].selected_label
                    ),
                }
                if scenario.oracle_required
                else None
            ),
        },
        "runtime_seconds": time.perf_counter() - started,
    }
    record["record_fingerprint"] = semantics_fingerprint(
        RECORD_SCHEMA, _record_fingerprint_payload(record)
    )
    return _strict_json_value(record)


def _run_task(args):
    return _run_realization(*args)


def _persist_completed_tasks(
    tasks,
    *,
    workers: int,
    checkpoints: Path,
    records_by_id: dict[str, dict[str, Any]],
) -> None:
    """Run dataset tasks and atomically persist every successful completion."""

    def persist(record) -> None:
        checkpoint = checkpoints / (
            f"{record['scenario_id']}-{record['realization_index']:05d}.json"
        )
        _write_json(checkpoint, record)
        records_by_id[record["dataset_id"]] = record

    if workers == 1:
        for task in tasks:
            try:
                record = _run_task(task)
            except BaseException as exc:
                raise _CampaignTaskError(task[1], task[2]) from exc
            persist(record)
        return

    executor = ProcessPoolExecutor(max_workers=workers)
    futures = {}
    try:
        for task in tasks:
            futures[executor.submit(_run_task, task)] = task
        for future in as_completed(futures):
            task = futures[future]
            try:
                record = future.result()
            except BaseException as exc:
                raise _CampaignTaskError(task[1], task[2]) from exc
            persist(record)
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)


def _write_failed_runtime_ledger(
    path: Path,
    *,
    identity: Mapping[str, Any],
    prior_runtime: float,
    started: float,
    error: BaseException,
) -> None:
    cause = error.__cause__ if isinstance(error, _CampaignTaskError) else error
    payload = {
        "schema": RUNTIME_LEDGER_SCHEMA,
        **identity,
        "status": (
            "interrupted"
            if isinstance(cause, (KeyboardInterrupt, SystemExit))
            else "failed"
        ),
        "cumulative_runtime_seconds": (
            prior_runtime + max(time.perf_counter() - started, 0.0)
        ),
        "active_started_epoch": None,
        "failure_type": type(cause).__name__,
    }
    if isinstance(error, _CampaignTaskError):
        payload.update(
            failed_scenario_id=error.scenario_id,
            failed_realization_index=error.realization_index,
        )
    _write_json(path, payload)


def _validate_gate(
    spec: CampaignSpec,
    gate_path: Path,
    *,
    source_hashes: Mapping[str, str],
    runtime_fingerprint: str,
    workers: int,
) -> dict[str, Any]:
    if not gate_path.is_file():
        raise RuntimeError("held-out execution refused: owner-approved gate file is missing")
    gate = _read_json(gate_path)
    if gate.get("schema") != GATE_SCHEMA or gate.get("owner_approved") is not True:
        raise RuntimeError("held-out execution refused: gate is not owner approved")
    if gate.get("campaign_spec_fingerprint") != spec.fingerprint:
        raise RuntimeError("held-out execution refused: campaign specification changed")
    if gate.get("source_hashes") != dict(source_hashes):
        raise RuntimeError("held-out execution refused: campaign source hashes changed")
    if gate.get("numerical_runtime_fingerprint") != runtime_fingerprint:
        raise RuntimeError("held-out execution refused: numerical runtime changed")
    if int(gate.get("workers", -1)) != workers:
        raise RuntimeError("held-out execution refused: worker count does not match the gate")
    if int(gate.get("heldout_realizations", -1)) != spec.heldout_realizations:
        raise RuntimeError("held-out execution refused: sample size does not match the gate")
    for required in (
        "development_summary_fingerprint",
        "evaluation_statistic",
        "margins",
        "runtime_ceiling_seconds",
    ):
        if gate.get(required) in (None, {}, ""):
            raise RuntimeError(f"held-out execution refused: gate lacks {required}")
    return gate


def run_campaign(
    spec: CampaignSpec,
    phase: str,
    output_root,
    workers: int = 1,
) -> CampaignRun:
    """Run or resume one frozen campaign phase."""

    if phase not in ("development", "heldout"):
        raise ValueError("phase must be 'development' or 'heldout'")
    output_root = Path(output_root)
    output_dir = output_root / spec.campaign_id / phase
    checkpoints = output_dir / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[3]
    source_hashes = _source_hashes(repo_root)
    source_fingerprint = _source_fingerprint(source_hashes)
    max_workers = max(1, int(workers))
    runtime_provenance = _runtime_provenance(max_workers)
    _validate_parallel_runtime(runtime_provenance, max_workers)
    runtime_fingerprint = _runtime_fingerprint(runtime_provenance)
    started = time.perf_counter()
    wall_started = time.time()
    runtime_ledger_path = output_dir / "runtime_ledger.json"
    ledger_identity = {
        "campaign_id": spec.campaign_id,
        "phase": phase,
        "campaign_spec_fingerprint": spec.fingerprint,
        "campaign_source_fingerprint": source_fingerprint,
        "numerical_runtime_fingerprint": runtime_fingerprint,
        "workers": max_workers,
    }
    prior_runtime = 0.0
    if runtime_ledger_path.is_file():
        previous_ledger = _read_json(runtime_ledger_path)
        if (
            previous_ledger.get("schema") == RUNTIME_LEDGER_SCHEMA
            and all(
                previous_ledger.get(key) == value
                for key, value in ledger_identity.items()
            )
        ):
            prior_runtime = float(
                previous_ledger.get("cumulative_runtime_seconds", 0.0)
            )
            if previous_ledger.get("status") == "running":
                prior_runtime += max(
                    wall_started
                    - float(previous_ledger.get("active_started_epoch", wall_started)),
                    0.0,
                )
    gate = None
    realizations = spec.development_realizations
    if phase == "heldout":
        if spec.heldout_realizations <= 0:
            raise RuntimeError("held-out execution refused: replication count is not frozen")
        gate_path = (
            Path(spec.heldout_gate_path)
            if spec.heldout_gate_path is not None
            else repo_root / "tools" / "stats" / "stat2b-heldout-gate.json"
        )
        if not gate_path.is_absolute():
            gate_path = repo_root / gate_path
        gate = _validate_gate(
            spec,
            gate_path,
            source_hashes=source_hashes,
            runtime_fingerprint=runtime_fingerprint,
            workers=max_workers,
        )
        realizations = spec.heldout_realizations
    _write_json(
        runtime_ledger_path,
        {
            "schema": RUNTIME_LEDGER_SCHEMA,
            **ledger_identity,
            "status": "running",
            "cumulative_runtime_seconds": prior_runtime,
            "active_started_epoch": wall_started,
        },
    )

    smoke_records = []
    smoke_new_count = 0
    if phase == "development":
        smoke_dir = output_dir / "smoke"
        smoke_dir.mkdir(parents=True, exist_ok=True)
        for scenario in spec.scenarios:
            smoke_path = smoke_dir / f"{scenario.id}.json"
            smoke = None
            if smoke_path.is_file():
                candidate = _read_json(smoke_path)
                if _record_is_reusable(
                    candidate,
                    spec=spec,
                    scenario_id=scenario.id,
                    realization_index=spec.development_realizations,
                    phase=phase,
                    source_fingerprint=source_fingerprint,
                    runtime_fingerprint=runtime_fingerprint,
                ):
                    smoke = candidate
            if smoke is None:
                # A distinct, deterministic realization that is not part of the 20-row
                # development estimate.  All cells clear the full path before bulk work opens.
                smoke = _run_realization(
                    spec,
                    scenario.id,
                    spec.development_realizations,
                    phase,
                    source_fingerprint,
                    runtime_fingerprint,
                )
                _write_json(smoke_path, smoke)
                smoke_new_count += 1
            smoke_records.append(smoke)

    tasks = []
    records_by_id: dict[str, dict[str, Any]] = {}
    for scenario in spec.scenarios:
        for realization_index in range(realizations):
            key = f"{scenario.id}-{realization_index:05d}"
            checkpoint = checkpoints / f"{key}.json"
            if checkpoint.is_file():
                record = _read_json(checkpoint)
                if _record_is_reusable(
                    record,
                    spec=spec,
                    scenario_id=scenario.id,
                    realization_index=realization_index,
                    phase=phase,
                    source_fingerprint=source_fingerprint,
                    runtime_fingerprint=runtime_fingerprint,
                ):
                    records_by_id[record["dataset_id"]] = record
                    continue
            tasks.append(
                (
                    spec,
                    scenario.id,
                    realization_index,
                    phase,
                    source_fingerprint,
                    runtime_fingerprint,
                )
            )

    try:
        _persist_completed_tasks(
            tasks,
            workers=max_workers,
            checkpoints=checkpoints,
            records_by_id=records_by_id,
        )
    except BaseException as exc:
        _write_failed_runtime_ledger(
            runtime_ledger_path,
            identity=ledger_identity,
            prior_runtime=prior_runtime,
            started=started,
            error=exc,
        )
        raise

    records = tuple(
        sorted(
            records_by_id.values(),
            key=lambda item: (item["scenario_id"], item["realization_index"]),
        )
    )
    jsonl = b"".join(_json_bytes(record) for record in records)
    _atomic_write(output_dir / "datasets.jsonl", jsonl)
    invocation_runtime = time.perf_counter() - started
    cumulative_runtime = prior_runtime + invocation_runtime
    _write_json(
        runtime_ledger_path,
        {
            "schema": RUNTIME_LEDGER_SCHEMA,
            **ledger_identity,
            "status": "complete",
            "cumulative_runtime_seconds": cumulative_runtime,
            "active_started_epoch": None,
        },
    )
    manifest = {
        "schema": RUN_SCHEMA,
        "campaign_id": spec.campaign_id,
        "phase": phase,
        "campaign_spec_fingerprint": spec.fingerprint,
        "source_hashes": source_hashes,
        "campaign_source_fingerprint": source_fingerprint,
        "numerical_runtime": runtime_provenance,
        "numerical_runtime_fingerprint": runtime_fingerprint,
        "python": sys.version,
        "platform": platform.platform(),
        "workers": max_workers,
        "inner_bootstrap_workers": 1,
        "delivery_replicates": spec.delivery_replicates,
        "scenario_count": len(spec.scenarios),
        "realizations_per_scenario": realizations,
        "dataset_count": len(records),
        "new_dataset_count": len(tasks),
        "smoke_dataset_count": len(smoke_records),
        "new_smoke_dataset_count": smoke_new_count,
        "record_fingerprints": [
            record["record_fingerprint"] for record in records
        ],
        "smoke_record_fingerprints": [
            record["record_fingerprint"] for record in smoke_records
        ],
        "runtime_seconds": cumulative_runtime,
        "invocation_runtime_seconds": invocation_runtime,
        "cumulative_runtime_seconds": cumulative_runtime,
        "gate": gate,
    }
    manifest["run_fingerprint"] = semantics_fingerprint(
        RUN_SCHEMA,
        {
            key: value
            for key, value in manifest.items()
            if key
            not in {
                "runtime_seconds",
                "invocation_runtime_seconds",
                "cumulative_runtime_seconds",
                "python",
                "platform",
                "workers",
                "new_dataset_count",
                "new_smoke_dataset_count",
            }
        },
    )
    _write_json(output_dir / "run_manifest.json", manifest)
    return CampaignRun(
        campaign_id=spec.campaign_id,
        phase=phase,
        output_dir=output_dir,
        spec_fingerprint=spec.fingerprint,
        source_hashes=source_hashes,
        records=records,
        manifest=manifest,
    )


def _mean(values: Iterable[float | None]) -> float | None:
    finite = [
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value))
    ]
    return float(np.mean(finite)) if finite else None


def _arm_cell_metrics(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        for arm in record["arms"]:
            grouped.setdefault((record["scenario_id"], arm["arm_id"]), []).append(arm)
    rows = []
    for (scenario_id, arm_id), arms in sorted(grouped.items()):
        target_errors = [arm["target_squared_error"] for arm in arms]
        rows.append(
            {
                "scenario_id": scenario_id,
                "arm_id": arm_id,
                "n": len(arms),
                "complete_comparison_rate": _mean(
                    arm["comparison_status"] == "available" for arm in arms
                ),
                "prediction_kl": _mean(arm["prediction_kl"] for arm in arms),
                "realized_log_score": _mean(
                    arm["realized_log_score"] for arm in arms
                ),
                "candidate_route_regret": _mean(
                    arm["candidate_route_regret"] for arm in arms
                ),
                "target_rmse": (
                    math.sqrt(_mean(target_errors))
                    if _mean(target_errors) is not None
                    else None
                ),
                "target_bias": _mean(arm["target_bias"] for arm in arms),
                "conditional_coverage": _mean(
                    arm["conditional_coverage"]
                    for arm in arms
                    if arm["conditional_coverage"] is not None
                ),
                "complete_delivery_coverage": _mean(
                    arm["complete_delivery_coverage"]
                    for arm in arms
                    if arm["complete_delivery_coverage"] is not None
                ),
                "missed_modes": _mean(arm["missed_modes"] for arm in arms),
                "extra_modes": _mean(arm["extra_modes"] for arm in arms),
                "false_certification": _mean(
                    arm["false_certification"] for arm in arms
                ),
            }
        )
    return rows


def _equal_cell_aggregate(cell_rows: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in cell_rows:
        grouped.setdefault(row["arm_id"], []).append(row)
    metrics = (
        "complete_comparison_rate",
        "prediction_kl",
        "realized_log_score",
        "candidate_route_regret",
        "target_rmse",
        "target_bias",
        "conditional_coverage",
        "complete_delivery_coverage",
        "missed_modes",
        "extra_modes",
        "false_certification",
    )
    return {
        arm_id: {
            "cell_count": len(rows),
            **{metric: _mean(row[metric] for row in rows) for metric in metrics},
        }
        for arm_id, rows in sorted(grouped.items())
    }


def _paired_cell_differences(
    records: Sequence[Mapping[str, Any]],
    contender: str,
    baseline: str,
) -> dict[str, dict[str, float | None]]:
    metrics = (
        "prediction_kl",
        "target_bias",
        "conditional_coverage",
        "complete_delivery_coverage",
        "missed_modes",
        "extra_modes",
        "false_certification",
    )
    by_cell: dict[str, dict[str, list[float]]] = {}
    target_errors: dict[str, dict[str, list[float]]] = {}
    for record in records:
        arms = {arm["arm_id"]: arm for arm in record["arms"]}
        if contender not in arms or baseline not in arms:
            continue
        lhs_error = arms[contender]["target_squared_error"]
        rhs_error = arms[baseline]["target_squared_error"]
        if lhs_error is not None and rhs_error is not None:
            grouped = target_errors.setdefault(
                record["scenario_id"], {"contender": [], "baseline": []}
            )
            grouped["contender"].append(float(lhs_error))
            grouped["baseline"].append(float(rhs_error))
        for metric in metrics:
            lhs, rhs = arms[contender][metric], arms[baseline][metric]
            if lhs is None or rhs is None:
                continue
            by_cell.setdefault(record["scenario_id"], {}).setdefault(metric, []).append(
                float(lhs) - float(rhs)
            )
    result = {
        cell: {metric: _mean(values) for metric, values in grouped.items()}
        for cell, grouped in sorted(by_cell.items())
    }
    for cell, grouped in target_errors.items():
        result.setdefault(cell, {})["target_rmse"] = (
            math.sqrt(float(np.mean(grouped["contender"])))
            - math.sqrt(float(np.mean(grouped["baseline"])))
        )
    return result


def _hierarchical_bootstrap(
    records: Sequence[Mapping[str, Any]],
    contender: str,
    baseline: str,
    *,
    repetitions: int = 2000,
    seed: int = 20260730,
) -> dict[str, dict[str, float | None]]:
    cells = sorted({record["scenario_id"] for record in records})
    by_cell = {
        cell: [record for record in records if record["scenario_id"] == cell]
        for cell in cells
    }
    metrics = (
        "prediction_kl",
        "target_rmse",
        "target_bias",
        "conditional_coverage",
        "complete_delivery_coverage",
        "missed_modes",
        "extra_modes",
        "false_certification",
        "complete_comparison_rate",
    )
    rng = np.random.default_rng(seed)
    distributions = {metric: [] for metric in metrics}
    for _ in range(repetitions):
        sampled_cells = rng.choice(cells, size=len(cells), replace=True)
        for metric in metrics:
            cell_means = []
            for cell in sampled_cells:
                source = by_cell[str(cell)]
                sampled = [
                    source[int(index)]
                    for index in rng.integers(0, len(source), size=len(source))
                ]
                diffs = []
                contender_errors = []
                baseline_errors = []
                for record in sampled:
                    arms = {arm["arm_id"]: arm for arm in record["arms"]}
                    if contender not in arms or baseline not in arms:
                        continue
                    if metric == "complete_comparison_rate":
                        diffs.append(
                            float(
                                arms[contender]["comparison_status"]
                                == "available"
                            )
                        )
                        continue
                    if metric == "target_rmse":
                        lhs = arms[contender]["target_squared_error"]
                        rhs = arms[baseline]["target_squared_error"]
                        if lhs is not None and rhs is not None:
                            contender_errors.append(float(lhs))
                            baseline_errors.append(float(rhs))
                        continue
                    lhs = arms[contender][metric]
                    rhs = arms[baseline][metric]
                    if lhs is not None and rhs is not None:
                        diffs.append(float(lhs) - float(rhs))
                if (
                    metric == "target_rmse"
                    and contender_errors
                    and baseline_errors
                ):
                    diffs = [
                        math.sqrt(float(np.mean(contender_errors)))
                        - math.sqrt(float(np.mean(baseline_errors)))
                    ]
                if diffs:
                    cell_means.append(float(np.mean(diffs)))
            if cell_means:
                distributions[metric].append(float(np.mean(cell_means)))
    intervals = {}
    for metric, values in distributions.items():
        intervals[metric] = (
            {
                "low": float(np.quantile(values, 0.025)),
                "median": float(np.quantile(values, 0.5)),
                "high": float(np.quantile(values, 0.975)),
            }
            if values
            else {"low": None, "median": None, "high": None}
        )
    return intervals


def _critical_strata(
    records: Sequence[Mapping[str, Any]], contender: str, baseline: str
) -> dict[str, dict[str, Any]]:
    strata = sorted(
        {
            stratum
            for record in records
            for stratum in record.get("critical_strata", ())
        }
    )
    out = {}
    for stratum in strata:
        selected = [
            record
            for record in records
            if stratum in record.get("critical_strata", ())
        ]
        paired = _paired_cell_differences(selected, contender, baseline)
        stratum_seed = int.from_bytes(
            hashlib.sha256(stratum.encode("utf-8")).digest()[:4], "big"
        )
        bootstrap = _hierarchical_bootstrap(
            selected,
            contender,
            baseline,
            seed=20260730 + stratum_seed,
        )
        selected_cells = _arm_cell_metrics(selected)
        selected_aggregate = _equal_cell_aggregate(selected_cells).get(contender, {})
        out[stratum] = {
            "dataset_count": len(selected),
            "cell_count": len(paired),
            "prediction_kl_difference": _mean(
                values.get("prediction_kl") for values in paired.values()
            ),
            "target_rmse_difference": (
                _mean(values.get("target_rmse") for values in paired.values())
            ),
            "target_bias_difference": _mean(
                values.get("target_bias") for values in paired.values()
            ),
            "conditional_coverage_difference": _mean(
                values.get("conditional_coverage")
                for values in paired.values()
            ),
            "complete_delivery_coverage_difference": _mean(
                values.get("complete_delivery_coverage")
                for values in paired.values()
            ),
            "missed_mode_difference": _mean(
                values.get("missed_modes") for values in paired.values()
            ),
            "false_extra_mode_difference": _mean(
                values.get("extra_modes") for values in paired.values()
            ),
            "false_certification_difference": _mean(
                values.get("false_certification") for values in paired.values()
            ),
            "complete_comparison_rate": selected_aggregate.get(
                "complete_comparison_rate"
            ),
            "hierarchical_bootstrap_intervals": bootstrap,
        }
    return out


def _write_cell_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["scenario_id", "arm_id"]
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(_strict_json_value(rows))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_figures(
    output_dir: Path,
    aggregate: Mapping[str, Mapping[str, Any]],
    critical: Mapping[str, Mapping[str, Any]],
    *,
    contender: str,
    baseline: str,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    figure_dir = output_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    arms = [
        arm_id
        for arm_id, values in aggregate.items()
        if values.get("prediction_kl") is not None
    ]
    values = [float(aggregate[arm_id]["prediction_kl"]) for arm_id in arms]
    fig, ax = plt.subplots(figsize=(max(7.0, 0.8 * len(arms)), 4.5))
    ax.bar(np.arange(len(arms)), values, color="#3b6f9f")
    ax.set_xticks(np.arange(len(arms)), arms, rotation=35, ha="right")
    ax.set_ylabel("Mean Poisson KL per kept bin")
    ax.set_title("STAT-2b equal-cell predictive loss")
    fig.tight_layout()
    fig.savefig(figure_dir / "predictive_loss_by_arm.png", dpi=160)
    plt.close(fig)

    strata = [
        name
        for name, values in critical.items()
        if values.get("prediction_kl_difference") is not None
    ]
    differences = [
        float(critical[name]["prediction_kl_difference"]) for name in strata
    ]
    fig, ax = plt.subplots(figsize=(max(7.0, 0.9 * len(strata)), 4.5))
    colors = ["#438a5e" if value <= 0.0 else "#b04a4a" for value in differences]
    ax.bar(np.arange(len(strata)), differences, color=colors)
    ax.axhline(0.0, color="black", linewidth=0.8)
    ax.set_xticks(np.arange(len(strata)), strata, rotation=30, ha="right")
    ax.set_ylabel(f"{contender} − {baseline} Poisson KL")
    ax.set_title("STAT-2b critical-stratum veto evidence")
    fig.tight_layout()
    fig.savefig(figure_dir / "critical_strata_predictive_difference.png", dpi=160)
    plt.close(fig)


def _evidence_completeness(
    run: CampaignRun,
    spec: CampaignSpec,
    bootstrap: Mapping[str, Mapping[str, float | None]],
    critical: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    expected_realizations = (
        spec.development_realizations
        if run.phase == "development"
        else spec.heldout_realizations
    )
    expected_count = len(spec.scenarios) * expected_realizations
    counts = Counter(record.get("scenario_id") for record in run.records)
    expected_indices = set(range(expected_realizations))
    observed_indices = {
        scenario.id: {
            int(record["realization_index"])
            for record in run.records
            if record.get("scenario_id") == scenario.id
        }
        for scenario in spec.scenarios
    }
    missing_or_miscounted = [
        scenario.id
        for scenario in spec.scenarios
        if counts.get(scenario.id, 0) != expected_realizations
        or observed_indices[scenario.id] != expected_indices
    ]
    missing_intervals = [
        metric
        for metric, interval in bootstrap.items()
        if interval.get("low") is None
        or interval.get("median") is None
        or interval.get("high") is None
    ]
    missing_strata = sorted(set(spec.critical_strata) - set(critical))
    complete = bool(
        len(run.records) == expected_count
        and not missing_or_miscounted
        and not missing_intervals
        and not missing_strata
    )
    return {
        "complete": complete,
        "expected_dataset_count": expected_count,
        "observed_dataset_count": len(run.records),
        "missing_or_miscounted_cells": missing_or_miscounted,
        "missing_bootstrap_intervals": missing_intervals,
        "missing_critical_strata": missing_strata,
    }


def _gate_observations(
    critical: Mapping[str, Mapping[str, Any]],
    bootstrap: Mapping[str, Mapping[str, float | None]],
) -> tuple[dict[str, float | None], dict[str, dict[str, float | None]]]:
    mapping = {
        "prediction_kl_difference": "prediction_kl",
        "target_rmse_difference": "target_rmse",
        "missed_mode_difference": "missed_modes",
        "false_extra_mode_difference": "extra_modes",
        "false_certification_difference": "false_certification",
    }
    overall = {
        observed_name: bootstrap[paired_name]["high"]
        for observed_name, paired_name in mapping.items()
    }
    overall["complete_comparison_rate"] = bootstrap[
        "complete_comparison_rate"
    ]["low"]
    return overall, {
        stratum: {
            observed_name: values["hierarchical_bootstrap_intervals"][
                paired_name
            ]["high"]
            for observed_name, paired_name in mapping.items()
        }
        | {
            "complete_comparison_rate": values[
                "hierarchical_bootstrap_intervals"
            ]["complete_comparison_rate"]["low"]
        }
        for stratum, values in critical.items()
    }


def _same_set_change_rates(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, float | None]]:
    observations: dict[str, dict[str, list[bool]]] = {}
    for record in records:
        for family, comparisons in record.get(
            "same_set_selection_changes", {}
        ).items():
            if comparisons is None:
                continue
            for comparison, changed in comparisons.items():
                observations.setdefault(family, {}).setdefault(
                    comparison, []
                ).append(bool(changed))
    return {
        family: {
            comparison: _mean(values)
            for comparison, values in sorted(comparisons.items())
        }
        for family, comparisons in sorted(observations.items())
    }


def _evaluate_frozen_gate(
    gate: Mapping[str, Any],
    overall: Mapping[str, float | None],
    strata: Mapping[str, Mapping[str, float | None]],
    *,
    runtime_seconds: float,
) -> dict[str, Any]:
    results = {}
    all_pass = True
    for gate_name, rule in gate["margins"].items():
        metric = str(rule["metric"])
        checks = {}
        applicable = set(rule.get("applicable_strata", strata))
        scoped = [("overall", overall.get(metric))] + [
            (name, values.get(metric))
            for name, values in sorted(strata.items())
            if name in applicable
        ]
        for scope, value in scoped:
            if value is None:
                checks[scope] = {"status": "unavailable", "value": None}
                all_pass = False
                continue
            passed = (
                float(value) <= float(rule["maximum"])
                if "maximum" in rule
                else float(value) >= float(rule["minimum"])
            )
            checks[scope] = {
                "status": "pass" if passed else "fail",
                "value": float(value),
            }
            all_pass = all_pass and passed
        results[gate_name] = checks
    runtime_ceiling = float(gate["runtime_ceiling_seconds"])
    runtime_pass = float(runtime_seconds) <= runtime_ceiling
    all_pass = all_pass and runtime_pass
    return {
        "status": "pass" if all_pass else "authority_withheld",
        "all_required_gates_pass": all_pass,
        "checks": results,
        "runtime_ceiling": {
            "status": "pass" if runtime_pass else "fail",
            "value": float(runtime_seconds),
            "maximum": runtime_ceiling,
        },
    }


def summarize_campaign(run: CampaignRun, spec: CampaignSpec) -> dict[str, Any]:
    """Aggregate paired datasets, then cells, then equally weighted cells."""

    if run.spec_fingerprint != spec.fingerprint:
        raise ValueError("run and specification fingerprints do not match")
    cell_rows = _arm_cell_metrics(run.records)
    aggregate = _equal_cell_aggregate(cell_rows)
    contender = "unconstrained_aicc"
    baseline = "unconstrained_aic"
    paired = _paired_cell_differences(run.records, contender, baseline)
    legacy_paired = {
        arm_id: _paired_cell_differences(run.records, arm_id, "legacy_aic")
        for arm_id in (
            "unconstrained_aic",
            "unconstrained_aicc",
            "unconstrained_bic",
            "unconstrained_aic_null",
            "unconstrained_aicc_null",
        )
    }
    null_family_paired = {
        "aic_null_vs_standard": _paired_cell_differences(
            run.records, "unconstrained_aic_null", "unconstrained_aic"
        ),
        "aicc_null_vs_standard": _paired_cell_differences(
            run.records, "unconstrained_aicc_null", "unconstrained_aicc"
        ),
    }
    bootstrap = _hierarchical_bootstrap(run.records, contender, baseline)
    critical = _critical_strata(run.records, contender, baseline)
    completeness = _evidence_completeness(run, spec, bootstrap, critical)
    contender_metrics = aggregate.get(contender, {})
    predictive_ci = bootstrap["prediction_kl"]
    if all(predictive_ci[key] is not None for key in ("low", "median", "high")):
        proposed_reps = max(
            spec.development_realizations,
            int(
                math.ceil(
                    spec.development_realizations
                    * (
                        max(
                            float(predictive_ci["high"])
                            - float(predictive_ci["low"]),
                            1e-6,
                        )
                        / max(abs(float(predictive_ci["median"])), 1e-6)
                    )
                    ** 2
                )
            ),
        )
    else:
        proposed_reps = spec.development_realizations
    proposed_reps = max(proposed_reps, spec.development_realizations)
    prediction_high = predictive_ci["high"]
    rmse_high = bootstrap["target_rmse"]["high"]
    margins = {
        "predictive_loss_superiority": {
            "metric": "prediction_kl_difference",
            "applicable_strata": sorted(spec.critical_strata),
            "maximum": min(float(prediction_high), 0.0)
            if prediction_high is not None
            else 0.0,
        },
        "omega_rmse_noninferiority": {
            "metric": "target_rmse_difference",
            "applicable_strata": sorted(
                set(spec.critical_strata) - {"target_absent"}
            ),
            "maximum": max(float(rmse_high), 0.0)
            if rmse_high is not None
            else 0.0,
        },
        "missed_mode_noninferiority": {
            "metric": "missed_mode_difference",
            "applicable_strata": sorted(
                set(spec.critical_strata) - {"target_absent"}
            ),
            "maximum": 0.0,
        },
        "false_extra_mode_noninferiority": {
            "metric": "false_extra_mode_difference",
            "applicable_strata": sorted(spec.critical_strata),
            "maximum": 0.0,
        },
        "comparative_false_certification_noninferiority": {
            "metric": "false_certification_difference",
            "applicable_strata": sorted(spec.critical_strata),
            "maximum": 0.0,
        },
        "minimum_complete_comparison_rate": {
            "metric": "complete_comparison_rate",
            "applicable_strata": sorted(spec.critical_strata),
            "minimum": float(contender_metrics.get("complete_comparison_rate") or 0.0),
        },
    }
    observed_gate_metrics, observed_stratum_metrics = _gate_observations(
        critical, bootstrap
    )
    runtime_values = [
        float(record["runtime_seconds"])
        for record in run.records
        if record.get("runtime_seconds") is not None
    ]
    mean_dataset_runtime = (
        float(np.mean(runtime_values))
        if runtime_values
        else float(run.manifest["runtime_seconds"]) / max(len(run.records), 1)
    )
    projected_heldout_runtime = (
        mean_dataset_runtime
        * proposed_reps
        * len(spec.scenarios)
        / max(int(run.manifest.get("workers", 1)), 1)
    )
    failure_reasons = Counter(
        arm["failure_reason"]
        for record in run.records
        for arm in record["arms"]
        if arm.get("failure_reason")
    )
    execution_kinds = Counter(
        arm.get("execution_provenance", {}).get("execution_kind", "unknown")
        for record in run.records
        for arm in record["arms"]
    )
    delivery_unique = sum(
        int(record.get("delivery_deduplication", {}).get("unique_delivery_count", 0))
        for record in run.records
    )
    delivery_required = sum(
        int(
            record.get("delivery_deduplication", {}).get(
                "arm_count_requiring_delivery", 0
            )
        )
        for record in run.records
    )
    summary_schema = (
        SUMMARY_SCHEMA if run.phase == "development" else HELDOUT_SUMMARY_SCHEMA
    )
    summary = {
        "schema": summary_schema,
        "campaign_id": run.campaign_id,
        "phase": run.phase,
        "campaign_spec_fingerprint": spec.fingerprint,
        "run_fingerprint": run.manifest["run_fingerprint"],
        "source_hashes": dict(run.source_hashes),
        "dataset_count": len(run.records),
        "cell_count": len(spec.scenarios),
        "cell_weighting": "equal",
        "evidence_completeness": completeness,
        "cell_summaries": cell_rows,
        "equal_cell_aggregate": aggregate,
        "decision_comparison": {
            "contender": contender,
            "baseline": baseline,
        },
        "paired_cell_differences_aicc_vs_aic": paired,
        "paired_cell_differences_vs_legacy": legacy_paired,
        "null_family_adequacy": {
            "paired_cell_differences": null_family_paired,
            "production_enumeration_unchanged": True,
        },
        "same_set_selection_change_rates": _same_set_change_rates(run.records),
        "hierarchical_bootstrap_intervals": bootstrap,
        "critical_strata_vetoes": critical,
        "observed_gate_metrics": observed_gate_metrics,
        "observed_critical_stratum_metrics": observed_stratum_metrics,
        "failure_reasons": dict(sorted(failure_reasons.items())),
        "execution_provenance": {
            "execution_kind_counts": dict(sorted(execution_kinds.items())),
        },
        "delivery_deduplication": {
            "unique_delivery_count": delivery_unique,
            "arm_count_requiring_delivery": delivery_required,
            "reused_delivery_count": max(delivery_required - delivery_unique, 0),
        },
        "runtime": {
            "phase_wall_seconds": float(run.manifest["runtime_seconds"]),
            "mean_dataset_runtime_seconds": mean_dataset_runtime,
            "projected_heldout_runtime_seconds": projected_heldout_runtime,
        },
        "route_trigger_adequacy": {
            "oracle_required_dataset_count": sum(
                record["oracle_winner_available_in_standard"] is not None
                for record in run.records
            ),
            "oracle_winner_availability_rate": _mean(
                record["oracle_winner_available_in_standard"]
                for record in run.records
                if record["oracle_winner_available_in_standard"] is not None
            ),
            "two_mode_omission_rate": _mean(
                record["two_mode_omission"] for record in run.records
            ),
        },
    }
    proposed_owner_gate = {
            "schema": GATE_SCHEMA,
            "status": (
                "ready_for_owner_review"
                if completeness["complete"]
                else "authority_withheld"
            ),
            "owner_approved": False,
            "evaluation_statistic": (
                "one_sided_95_percent_bound; upper for maximum rules, "
                "lower for minimum rules"
            ),
            "heldout_realizations": proposed_reps,
            "workers": int(run.manifest.get("workers", 1)),
            "runtime_ceiling_seconds": projected_heldout_runtime * 1.5,
            "margins": margins,
            "development_campaign_spec_fingerprint": spec.fingerprint,
            # Filled only after the owner freezes heldout_realizations in the
            # held-out TOML; that edit intentionally changes its fingerprint.
            "campaign_spec_fingerprint": None,
            "source_hashes": dict(run.source_hashes),
            "numerical_runtime": run.manifest["numerical_runtime"],
            "numerical_runtime_fingerprint": run.manifest[
                "numerical_runtime_fingerprint"
            ],
    }
    if run.phase == "development":
        summary["development_summary_fingerprint"] = semantics_fingerprint(
            SUMMARY_SCHEMA, summary
        )
        proposed_owner_gate["development_summary_fingerprint"] = summary[
            "development_summary_fingerprint"
        ]
        summary["proposed_owner_gate"] = proposed_owner_gate
    else:
        gate = run.manifest.get("gate")
        if not isinstance(gate, Mapping):
            raise RuntimeError("held-out summary lacks its frozen owner gate")
        summary["frozen_owner_gate_fingerprint"] = semantics_fingerprint(
            GATE_SCHEMA, gate
        )
        summary["heldout_gate_evaluation"] = _evaluate_frozen_gate(
            gate,
            observed_gate_metrics,
            observed_stratum_metrics,
            runtime_seconds=float(run.manifest["runtime_seconds"]),
        )
        summary["heldout_summary_fingerprint"] = semantics_fingerprint(
            HELDOUT_SUMMARY_SCHEMA, summary
        )
    _write_figures(
        run.output_dir,
        aggregate,
        critical,
        contender=contender,
        baseline=baseline,
    )
    _write_json(run.output_dir / "decision_summary.json", summary)
    if run.phase == "development":
        _write_json(run.output_dir / "proposed_owner_gate.json", proposed_owner_gate)
    _write_cell_csv(run.output_dir / "cell_summaries.csv", cell_rows)
    findings = (
        "# STAT-2b Generated Findings\n\n"
        "> **Lifecycle:** Historical evidence\n"
        ">\n"
        "> **Provenance:** generated by `python -m isar.synth.stat2b`\n\n"
        f"- Campaign: `{run.campaign_id}`\n"
        f"- Phase: `{run.phase}`\n"
        f"- Dataset count: {len(run.records)}\n"
        f"- Frozen criterion comparison: `{contender}` versus `{baseline}`\n"
        f"- Summary fingerprint: "
        f"`{summary.get('development_summary_fingerprint') or summary.get('heldout_summary_fingerprint')}`\n"
        + (
            "- Authority remains withheld until the owner approves numerical margins, "
            "runtime ceiling, and held-out replication counts.\n"
            if run.phase == "development"
            else f"- Held-out gate status: "
            f"`{summary['heldout_gate_evaluation']['status']}`.\n"
        )
    )
    _atomic_write(run.output_dir / "generated_findings.md", findings.encode("utf-8"))
    return summary
