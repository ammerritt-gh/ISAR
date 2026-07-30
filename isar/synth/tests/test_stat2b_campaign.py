from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import multiprocessing
import pickle
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.structures import enumerate_structures
from isar.schema.steering import ParamConstraints
from isar.session import branch_model
from isar.synth.stat2b import campaign
from isar.synth.stat2b.criteria import CandidateEvidence, NULL_LABELS
from isar.synth.stat2b.spec import (
    CAMPAIGN_SCHEMA,
    CRITICAL_STRATA,
    CampaignSpec,
    ScenarioSpec,
    load_campaign_spec,
)
from isar.synth.stat2b.truth import TruthDataset, generate_dataset


def _scenario(scenario_id, stratum, **changes):
    values = dict(
        id=scenario_id,
        truth_kind="one_mode",
        target_present=True,
        target_rank="single",
        peak_counts=30,
        x_min=-5.0,
        x_max=8.0,
        x_step=0.5,
        gauss_fwhm=0.961,
        lorentz_fwhm=0.596,
        T=100.0,
        gamma=0.5,
        energies=(4.0,),
        critical_strata=(stratum,),
        oracle_required=False,
    )
    values.update(changes)
    return ScenarioSpec(**values)


def _spec(*, heldout=0):
    scenarios = (
        _scenario("weak", "weak_signal", peak_counts=15),
        _scenario("near", "near_elastic", energies=(0.75,)),
        _scenario(
            "partial",
            "partially_resolved_two_mode",
            truth_kind="two_mode",
            target_rank="lower",
            energies=(3.0, 4.25),
            oracle_required=True,
        ),
        _scenario(
            "absent",
            "target_absent",
            truth_kind="target_absent",
            target_present=False,
            target_rank="none",
            energies=(),
            oracle_required=True,
        ),
        _scenario(
            "slope",
            "misspecified_background",
            truth_kind="misspecified_background",
            background_slope_fraction=0.25,
            oracle_required=True,
        ),
    )
    return CampaignSpec(
        schema=CAMPAIGN_SCHEMA,
        campaign_id="stat2b-test",
        base_seed=17,
        phase_seed_namespaces={"development": "dev", "heldout": "sealed"},
        development_realizations=20,
        heldout_realizations=heldout,
        delivery_replicates=200,
        certification_tolerance_mev=0.1,
        cell_weighting="equal",
        critical_strata=tuple(CRITICAL_STRATA),
        scenarios=scenarios,
    )


def _worker_spec_fingerprint(spec):
    return spec.fingerprint


def _fake_record(
    spec, scenario_id, index, phase, source_fingerprint, runtime_fingerprint
):
    record = {
        "schema": campaign.RECORD_SCHEMA,
        "campaign_id": spec.campaign_id,
        "campaign_spec_fingerprint": spec.fingerprint,
        "campaign_source_fingerprint": source_fingerprint,
        "numerical_runtime_fingerprint": runtime_fingerprint,
        "phase": phase,
        "scenario_id": scenario_id,
        "scenario_fingerprint": spec.scenario(scenario_id).fingerprint,
        "realization_index": index,
        "dataset_id": campaign._dataset_id(
            spec, spec.scenario(scenario_id), phase, index
        ),
    }
    record["record_fingerprint"] = campaign.semantics_fingerprint(
        campaign.RECORD_SCHEMA, campaign._record_fingerprint_payload(record)
    )
    return record


def test_repo_specs_load_and_heldout_matrix_is_fingerprinted():
    root = Path(__file__).resolve().parents[3]
    development = load_campaign_spec(root / "tools/stats/stat2b-development.toml")
    heldout = load_campaign_spec(root / "tools/stats/stat2b-heldout.toml")
    assert development.development_realizations == 20
    assert heldout.heldout_realizations == 103
    assert development.campaign_id == heldout.campaign_id == (
        "stat2b1-selection-criterion-2026-07"
    )
    assert development.phase_seed_namespaces["heldout"] == (
        heldout.phase_seed_namespaces["heldout"]
    ) == "stat2b-heldout-sealed-v2"
    assert heldout.heldout_gate_path == (
        "tools/stats/stat2b1-heldout-gate.json"
    )
    assert heldout.fingerprint == (
        "43b9fcbc7f6c2cbb57510c4801ac8084e96caecc7b511e6707240e257e974a54"
    )
    assert [cell.fingerprint for cell in heldout.scenarios] == [
        cell.fingerprint for cell in development.scenarios
    ]
    assert development.fingerprint != heldout.fingerprint
    active_gate_path = root / "tools/stats/stat2b1-heldout-gate.json"
    active_gate = json.loads(active_gate_path.read_text(encoding="utf-8"))
    assert active_gate["status"] == "owner_approved"
    assert active_gate["owner_approved"] is True
    assert active_gate["heldout_realizations"] == 103
    assert active_gate["workers"] == 24
    assert active_gate["campaign_spec_fingerprint"] == heldout.fingerprint
    assert campaign._validate_gate(
        heldout,
        active_gate_path,
        source_hashes=campaign._source_hashes(root),
        runtime_fingerprint=active_gate["numerical_runtime_fingerprint"],
        workers=24,
    ) == active_gate
    old_gate_path = root / "tools/stats/stat2b-heldout-gate.json"
    old_gate = json.loads(old_gate_path.read_text(encoding="utf-8"))
    old_status = json.loads(
        (
            root
            / "tools/stats/stat2b-heldout-gate.superseded-status.json"
        ).read_text(encoding="utf-8")
    )
    assert old_gate["owner_approved"] is True
    assert hashlib.sha256(old_gate_path.read_bytes()).hexdigest() == (
        "28d7c72d09d236a9092afca50b843b2ccaa195a1bf9e53897c3d57724fdfb868"
    )
    assert old_status["status"] == "superseded_by_stat2b_1"
    assert old_status["checkpoint_count_at_stop"] == 1254
    assert old_status["heldout_records_inspected"] is False
    assert old_status["scientific_eligibility"] == "ineligible"


def test_checkpoint_resume_is_scheduling_independent(tmp_path, monkeypatch):
    spec = _spec()
    calls = []

    def fake_realization(
        spec_arg,
        scenario_id,
        realization_index,
        phase,
        source_fingerprint,
        runtime_fingerprint,
    ):
        calls.append((scenario_id, realization_index))
        return _fake_record(
            spec_arg,
            scenario_id,
            realization_index,
            phase,
            source_fingerprint,
            runtime_fingerprint,
        )

    monkeypatch.setattr(campaign, "_run_realization", fake_realization)
    first = campaign.run_campaign(
        spec, phase="development", output_root=tmp_path, workers=1
    )
    assert len(first.records) == len(spec.scenarios) * 20
    assert len(calls) == len(first.records) + len(spec.scenarios)
    calls.clear()
    second = campaign.run_campaign(
        spec, phase="development", output_root=tmp_path, workers=1
    )
    assert second.records == first.records
    assert second.manifest["run_fingerprint"] == first.manifest["run_fingerprint"]
    assert calls == []

    checkpoint = next(
        (tmp_path / spec.campaign_id / "development" / "checkpoints").glob("*.json")
    )
    tampered = json.loads(checkpoint.read_text(encoding="utf-8"))
    tampered["dataset_id"] = "tampered"
    checkpoint.write_text(json.dumps(tampered), encoding="utf-8")
    third = campaign.run_campaign(
        spec, phase="development", output_root=tmp_path, workers=1
    )
    assert len(calls) == 1
    assert third.manifest["run_fingerprint"] == first.manifest["run_fingerprint"]

    calls.clear()
    smoke = (
        tmp_path / spec.campaign_id / "development" / "smoke" / "weak.json"
    )
    moved = (
        tmp_path
        / spec.campaign_id
        / "development"
        / "checkpoints"
        / "weak-00000.json"
    )
    moved.write_bytes(smoke.read_bytes())
    campaign.run_campaign(
        spec, phase="development", output_root=tmp_path, workers=1
    )
    assert calls == [("weak", 0)]

    ledger_path = (
        tmp_path / spec.campaign_id / "development" / "runtime_ledger.json"
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    prior_runtime = float(ledger["cumulative_runtime_seconds"])
    ledger["status"] = "running"
    ledger["active_started_epoch"] = time.time() - 10.0
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    resumed = campaign.run_campaign(
        spec, phase="development", output_root=tmp_path, workers=1
    )
    assert resumed.manifest["cumulative_runtime_seconds"] >= prior_runtime + 9.0


def test_task_failure_closes_runtime_ledger_with_safe_identity(tmp_path, monkeypatch):
    spec = _spec()
    first_scenario_id = spec.scenarios[0].id

    def fail_after_smoke(
        spec_arg,
        scenario_id,
        realization_index,
        phase,
        source_fingerprint,
        runtime_fingerprint,
    ):
        if realization_index == spec_arg.development_realizations:
            return _fake_record(
                spec_arg,
                scenario_id,
                realization_index,
                phase,
                source_fingerprint,
                runtime_fingerprint,
            )
        raise ValueError("injected dataset failure")

    monkeypatch.setattr(campaign, "_run_realization", fail_after_smoke)
    with pytest.raises(
        campaign._CampaignTaskError,
        match=rf"{first_scenario_id}-00000",
    ):
        campaign.run_campaign(
            spec,
            phase="development",
            output_root=tmp_path,
            workers=1,
        )

    ledger_path = (
        tmp_path / spec.campaign_id / "development" / "runtime_ledger.json"
    )
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "failed"
    assert ledger["active_started_epoch"] is None
    assert ledger["failure_type"] == "ValueError"
    assert ledger["failed_scenario_id"] == first_scenario_id
    assert ledger["failed_realization_index"] == 0
    assert ledger["cumulative_runtime_seconds"] >= 0.0


def test_parallel_task_failure_cancels_queue_before_shutdown(tmp_path, monkeypatch):
    class FakeFuture:
        def __init__(self, outcome):
            self.outcome = outcome
            self.cancel_calls = 0

        def result(self):
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

        def cancel(self):
            self.cancel_calls += 1
            return True

    first = FakeFuture(
        {
            "scenario_id": "weak",
            "realization_index": 0,
            "dataset_id": "first",
        }
    )
    failed = FakeFuture(RuntimeError("injected parallel failure"))
    pending = FakeFuture(
        {
            "scenario_id": "weak",
            "realization_index": 2,
            "dataset_id": "pending",
        }
    )
    futures = [first, failed, pending]

    class FakeExecutor:
        def __init__(self, *, max_workers):
            self.max_workers = max_workers
            self.shutdown_calls = []
            self.submitted = 0

        def submit(self, function, task):
            future = futures[self.submitted]
            self.submitted += 1
            return future

        def shutdown(self, *, wait, cancel_futures=False):
            self.shutdown_calls.append((wait, cancel_futures))

    executor = FakeExecutor(max_workers=2)
    monkeypatch.setattr(
        campaign,
        "ProcessPoolExecutor",
        lambda *, max_workers: executor,
    )
    monkeypatch.setattr(campaign, "as_completed", lambda submitted: submitted)
    tasks = [
        (None, "weak", index, "development", "source", "runtime")
        for index in range(3)
    ]
    records_by_id = {}

    with pytest.raises(
        campaign._CampaignTaskError,
        match=r"weak-00001",
    ) as error:
        campaign._persist_completed_tasks(
            tasks,
            workers=2,
            checkpoints=tmp_path,
            records_by_id=records_by_id,
        )

    assert isinstance(error.value.__cause__, RuntimeError)
    assert records_by_id == {"first": first.outcome}
    assert (tmp_path / "weak-00000.json").is_file()
    assert [future.cancel_calls for future in futures] == [1, 1, 1]
    assert executor.shutdown_calls == [(True, True)]


def test_heldout_refuses_unfrozen_count_before_opening_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(
        campaign,
        "generate_dataset",
        lambda *args, **kwargs: pytest.fail("held-out seed was opened"),
    )
    with pytest.raises(RuntimeError, match="replication count is not frozen"):
        campaign.run_campaign(
            _spec(heldout=0), phase="heldout", output_root=tmp_path, workers=1
        )


def test_heldout_gate_refuses_changed_spec_or_source_hash(tmp_path):
    spec = _spec(heldout=20)
    gate = {
        "schema": campaign.GATE_SCHEMA,
        "owner_approved": True,
        "campaign_spec_fingerprint": spec.fingerprint,
        "development_summary_fingerprint": "development",
        "evaluation_statistic": "one-sided bounds",
        "source_hashes": {"campaign.py": "hash"},
        "numerical_runtime_fingerprint": "runtime",
        "heldout_realizations": 20,
        "workers": 1,
        "margins": {"predictive_loss_superiority": {"maximum": 0.0}},
        "runtime_ceiling_seconds": 100.0,
    }
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(gate), encoding="utf-8")
    assert campaign._validate_gate(
        spec,
        path,
        source_hashes=gate["source_hashes"],
        runtime_fingerprint="runtime",
        workers=1,
    ) == gate

    changed_spec = replace(spec, heldout_realizations=21)
    with pytest.raises(RuntimeError, match="specification changed"):
        campaign._validate_gate(
            changed_spec,
            path,
            source_hashes=gate["source_hashes"],
            runtime_fingerprint="runtime",
            workers=1,
        )
    with pytest.raises(RuntimeError, match="source hashes changed"):
        campaign._validate_gate(
            spec,
            path,
            source_hashes={"campaign.py": "changed"},
            runtime_fingerprint="runtime",
            workers=1,
        )
    with pytest.raises(RuntimeError, match="numerical runtime changed"):
        campaign._validate_gate(
            spec,
            path,
            source_hashes=gate["source_hashes"],
            runtime_fingerprint="changed",
            workers=1,
        )
    with pytest.raises(RuntimeError, match="worker count"):
        campaign._validate_gate(
            spec,
            path,
            source_hashes=gate["source_hashes"],
            runtime_fingerprint="runtime",
            workers=2,
        )


def test_equal_cell_aggregation_does_not_weight_by_dataset_count():
    rows = [
        {"scenario_id": "a", "arm_id": "x", "prediction_kl": 0.0},
        {"scenario_id": "b", "arm_id": "x", "prediction_kl": 10.0},
    ]
    for row in rows:
        row.update(
            complete_comparison_rate=1.0,
            realized_log_score=0.0,
            candidate_route_regret=0.0,
            target_rmse=0.0,
            target_bias=0.0,
            conditional_coverage=1.0,
            complete_delivery_coverage=1.0,
            missed_modes=0.0,
            extra_modes=0.0,
            false_certification=0.0,
        )
    aggregate = campaign._equal_cell_aggregate(rows)
    assert aggregate["x"]["prediction_kl"] == 5.0


def test_summary_emits_six_proposed_gates_and_all_critical_vetoes(tmp_path):
    spec = _spec()
    records = []
    for index, scenario in enumerate(spec.scenarios):
        arms = []
        for arm_id in (
            "legacy_aic",
            "unconstrained_aic",
            "unconstrained_aicc",
        ):
            improved = arm_id == "unconstrained_aicc"
            arms.append(
                {
                    "arm_id": arm_id,
                    "comparison_status": "available",
                    "prediction_kl": 0.5 if improved else 1.0,
                    "realized_log_score": -1.0,
                    "candidate_route_regret": 0.0,
                        "target_squared_error": 0.01,
                        "target_bias": 0.0,
                    "conditional_coverage": True,
                    "complete_delivery_coverage": True,
                    "missed_modes": 0,
                    "extra_modes": 0,
                    "false_certification": False,
                }
            )
        records.append(
            {
                "scenario_id": scenario.id,
                "realization_index": index,
                "critical_strata": list(scenario.critical_strata),
                "oracle_winner_available_in_standard": (
                    True if scenario.oracle_required else None
                ),
                "two_mode_omission": False,
                "arms": arms,
            }
        )
    run = campaign.CampaignRun(
        campaign_id=spec.campaign_id,
        phase="development",
        output_dir=tmp_path,
        spec_fingerprint=spec.fingerprint,
        source_hashes={"source": "hash"},
        records=tuple(records),
        manifest={
            "run_fingerprint": "run",
            "runtime_seconds": 1.0,
            "numerical_runtime": {"test": True},
            "numerical_runtime_fingerprint": "runtime",
        },
    )
    summary = campaign.summarize_campaign(run, spec)
    gates = summary["proposed_owner_gate"]["margins"]
    assert len(gates) == 6
    assert set(summary["critical_strata_vetoes"]) == set(CRITICAL_STRATA)
    assert summary["cell_weighting"] == "equal"
    assert summary["decision_comparison"] == {
        "contender": "unconstrained_aicc",
        "baseline": "unconstrained_aic",
    }
    assert {
        "target_bias",
        "conditional_coverage",
        "complete_delivery_coverage",
    }.issubset(summary["hierarchical_bootstrap_intervals"])
    assert summary["proposed_owner_gate"]["owner_approved"] is False
    assert summary["proposed_owner_gate"]["schema"] == campaign.GATE_SCHEMA
    assert summary["proposed_owner_gate"]["status"] == "authority_withheld"
    assert summary["proposed_owner_gate"]["campaign_spec_fingerprint"] is None
    assert (
        summary["proposed_owner_gate"]["development_campaign_spec_fingerprint"]
        == spec.fingerprint
    )
    assert (tmp_path / "decision_summary.json").is_file()
    assert (tmp_path / "cell_summaries.csv").is_file()

    heldout_spec = _spec(heldout=1)
    heldout_dir = tmp_path / "heldout"
    heldout_run = replace(
        run,
        phase="heldout",
        output_dir=heldout_dir,
        spec_fingerprint=heldout_spec.fingerprint,
        manifest={
            **run.manifest,
            "gate": {
                **summary["proposed_owner_gate"],
                "owner_approved": True,
                "campaign_spec_fingerprint": heldout_spec.fingerprint,
            },
        },
    )
    heldout_summary = campaign.summarize_campaign(
        heldout_run, heldout_spec
    )
    assert heldout_summary["schema"] == campaign.HELDOUT_SUMMARY_SCHEMA
    assert "heldout_summary_fingerprint" in heldout_summary
    assert "proposed_owner_gate" not in heldout_summary
    assert not (heldout_dir / "proposed_owner_gate.json").exists()


def test_frozen_gate_applies_each_margin_to_each_critical_stratum():
    gate = {
        "margins": {
            "prediction": {
                "metric": "prediction_kl_difference",
                "maximum": 0.0,
            },
            "comparison": {
                "metric": "complete_comparison_rate",
                "minimum": 0.9,
            },
        }
    }
    overall = {
        "prediction_kl_difference": -0.1,
        "complete_comparison_rate": 1.0,
    }
    strata = {
        "weak_signal": {
            "prediction_kl_difference": -0.05,
            "complete_comparison_rate": 1.0,
        },
        "target_absent": {
            "prediction_kl_difference": 0.01,
            "complete_comparison_rate": 1.0,
        },
    }
    gate["runtime_ceiling_seconds"] = 10.0
    evaluation = campaign._evaluate_frozen_gate(
        gate, overall, strata, runtime_seconds=1.0
    )
    assert evaluation["status"] == "authority_withheld"
    assert evaluation["checks"]["prediction"]["target_absent"]["status"] == "fail"
    runtime_failure = campaign._evaluate_frozen_gate(
        gate,
        {
            "prediction_kl_difference": -0.1,
            "complete_comparison_rate": 1.0,
        },
        {
            "weak_signal": {
                "prediction_kl_difference": -0.05,
                "complete_comparison_rate": 1.0,
            }
        },
        runtime_seconds=11.0,
    )
    assert runtime_failure["runtime_ceiling"]["status"] == "fail"
    assert runtime_failure["status"] == "authority_withheld"


def _candidate(ctx, label="stokes+bg", cost=1.0, modes=None):
    modes = list(modes or [{"omega0": 4.0}])
    hypothesis = enumerate_structures(ctx)[0]
    result = SimpleNamespace(
        label=label,
        k=4,
        ndof=ctx.x_used.size - 4,
        cost=cost,
        optimizer_converged=True,
        params={},
        n_modes=len(modes),
        modes=modes,
    )
    return CandidateEvidence(
        label=label,
        k=4,
        n=ctx.x_used.size,
        cost=cost,
        optimizer_converged=True,
        prediction=np.full(ctx.x_used.size, 10.0),
        result=result,
        hypothesis=hypothesis,
        source="test",
    )


def test_one_delivery_is_reused_across_same_winner_arms(monkeypatch):
    spec = _spec()
    scenario = spec.scenario("weak")
    x = np.linspace(-5.0, 8.0, 27)
    ctx = ScanContext(
        x=x,
        counts=np.full(x.size, 10.0),
        mask=np.ones(x.size, dtype=bool),
        T=100.0,
        center=0.0,
        gauss_fwhm=0.961,
        lorentz_fwhm=0.596,
        instrument="STAT2B_SYNTH",
        energy_status="absolute",
        constraints=ParamConstraints(),
    )
    dataset = TruthDataset(
        fit_context=ctx,
        lambda_counts=np.full(x.size, 10.0),
        heldout_counts=np.full(x.size, 10.0),
        truth_manifest={
            "target_present": True,
            "target_rank": "single",
            "target_mode_index": 0,
            "modes": [{"omega0": 4.0}],
        },
        dataset_id="dataset",
        seed=1,
    )
    candidate = _candidate(ctx)
    provenance = {
        "execution_kind": "fresh",
        "offered_labels": [candidate.label],
        "attempted_labels": [candidate.label],
        "converged_labels": [candidate.label],
        "failed_labels": [],
        "reused_labels": [],
        "freshly_fitted_labels": [candidate.label],
        "multimode_route": "not_offered",
    }
    deliveries = []
    standard_reuse = []
    monkeypatch.setattr(campaign, "generate_dataset", lambda *a, **k: dataset)
    monkeypatch.setattr(
        campaign, "_legacy_evidence", lambda _ctx: ([candidate], provenance)
    )
    def fake_standard(_ctx, *, reusable_results=()):
        standard_reuse.extend(reusable_results)
        return [candidate], provenance

    monkeypatch.setattr(campaign, "_standard_evidence", fake_standard)
    monkeypatch.setattr(campaign, "fit_null_candidates", lambda _ctx: [])

    def fake_delivery(*args, **kwargs):
        deliveries.append(1)
        return {
            "best_label": candidate.label,
            "omega0": 4.0,
            "interval": (3.9, 4.0, 4.1),
            "interval_reliable": True,
            "modality": "fold",
            "n_modes": 1,
            "modes": [{"omega0": 4.0}],
        }

    monkeypatch.setattr(campaign, "_full_delivery", fake_delivery)
    monkeypatch.setattr(
        campaign,
        "_certification_projection",
        lambda *a, **k: {"certified": True},
    )
    record = campaign._run_realization(spec, scenario.id, 0, "development")
    assert len(deliveries) == 1
    assert standard_reuse == [candidate.result]
    assert record["delivery_deduplication"]["unique_delivery_count"] == 1
    assert record["delivery_deduplication"]["arm_count_requiring_delivery"] == 6
    delivered = [arm for arm in record["arms"] if arm["delivery"] is not None]
    assert {arm["delivery_input_fingerprint"] for arm in delivered} == {
        delivered[0]["delivery_reuse_group"]
    }
    assert sum(not arm["delivery_reused"] for arm in delivered) == 1
    assert {arm["delivery_source_arm"] for arm in delivered} == {
        min(arm["arm_id"] for arm in delivered)
    }
    for arm in delivered:
        assert arm["delivery_input_fingerprint"] == (
            campaign._delivery_input_fingerprint(arm["delivery_inputs"])
        )


@pytest.mark.parametrize(
    ("legacy_modes", "standard_modes", "difference"),
    (
        ([{"omega0": 3.0}], [{"omega0": 4.0}], "target"),
        (
            [{"omega0": 3.0}, {"omega0": 7.0}],
            [{"omega0": 3.0}, {"omega0": 8.0}],
            "peaks",
        ),
    ),
)
def test_same_label_candidate_hints_separate_delivery_and_ignore_arm_order(
    monkeypatch, legacy_modes, standard_modes, difference
):
    spec = _spec()
    scenario = spec.scenario("weak")
    x = np.linspace(-5.0, 8.0, 27)
    ctx = ScanContext(
        x=x,
        counts=np.full(x.size, 10.0),
        mask=np.ones(x.size, dtype=bool),
        T=100.0,
        center=0.0,
        gauss_fwhm=0.961,
        lorentz_fwhm=0.596,
        instrument="STAT2B_SYNTH",
        energy_status="absolute",
        constraints=ParamConstraints(),
    )
    dataset = TruthDataset(
        fit_context=ctx,
        lambda_counts=np.full(x.size, 10.0),
        heldout_counts=np.full(x.size, 10.0),
        truth_manifest={
            "target_present": True,
            "target_rank": "lower",
            "target_mode_index": 0,
            "modes": [{"omega0": 3.0}],
        },
        dataset_id="dataset",
        seed=1,
    )
    legacy = _candidate(ctx, modes=legacy_modes)
    standard = _candidate(ctx, modes=standard_modes)

    def provenance(candidate):
        return {
            "execution_kind": "fresh",
            "offered_labels": [candidate.label],
            "attempted_labels": [candidate.label],
            "converged_labels": [candidate.label],
            "failed_labels": [],
            "reused_labels": [],
            "freshly_fitted_labels": [candidate.label],
            "multimode_route": "not_offered",
        }

    monkeypatch.setattr(campaign, "generate_dataset", lambda *a, **k: dataset)
    monkeypatch.setattr(
        campaign, "_legacy_evidence", lambda _ctx: ([legacy], provenance(legacy))
    )
    monkeypatch.setattr(
        campaign,
        "_standard_evidence",
        lambda _ctx, *, reusable_results=(): ([standard], provenance(standard)),
    )
    monkeypatch.setattr(campaign, "fit_null_candidates", lambda _ctx: [])
    deliveries = []

    def fake_delivery(_dataset, _spec, *, delivery_inputs):
        deliveries.append(dict(delivery_inputs))
        return {
            "best_label": delivery_inputs["selected_label"],
            "omega0": delivery_inputs["candidate_target_hint"],
            "interval": (2.9, 3.0, 4.1),
            "interval_reliable": True,
            "modality": "fold",
            "n_modes": len(
                delivery_inputs["candidate_multimode_peak_hints"] or [None]
            ),
            "modes": delivery_inputs["candidate_multimode_peak_hints"] or [],
        }

    monkeypatch.setattr(campaign, "_full_delivery", fake_delivery)
    monkeypatch.setattr(
        campaign,
        "_certification_projection",
        lambda *a, **k: {"certified": True},
    )

    original_arms = campaign.STANDARD_ARMS
    first = campaign._run_realization(spec, scenario.id, 0, "development")
    monkeypatch.setattr(campaign, "STANDARD_ARMS", tuple(reversed(original_arms)))
    second = campaign._run_realization(spec, scenario.id, 0, "development")

    assert len(deliveries) == 4
    assert first["delivery_deduplication"]["unique_delivery_count"] == 2
    assert first["delivery_deduplication"] == second["delivery_deduplication"]

    def delivery_audit(record):
        return {
            arm["arm_id"]: {
                key: arm[key]
                for key in (
                    "delivery_inputs",
                    "delivery_input_fingerprint",
                    "delivery_reused",
                    "delivery_reuse_group",
                    "delivery_source_arm",
                    "delivery",
                )
            }
            for arm in record["arms"]
        }

    first_audit = delivery_audit(first)
    assert first_audit == delivery_audit(second)
    for arm in first["arms"]:
        assert arm["delivery_input_fingerprint"] == (
            campaign._delivery_input_fingerprint(arm["delivery_inputs"])
        )

    legacy_inputs = first_audit["legacy_aic"]["delivery_inputs"]
    standard_inputs = first_audit["unconstrained_aic"]["delivery_inputs"]
    assert legacy_inputs["selected_label"] == standard_inputs["selected_label"]
    if difference == "target":
        assert legacy_inputs["candidate_multimode_peak_hints"] is None
        assert standard_inputs["candidate_multimode_peak_hints"] is None
        assert (
            legacy_inputs["candidate_target_hint"]
            != standard_inputs["candidate_target_hint"]
        )
    else:
        assert (
            legacy_inputs["candidate_target_hint"]
            == standard_inputs["candidate_target_hint"]
        )
        assert (
            legacy_inputs["candidate_multimode_peak_hints"]
            != standard_inputs["candidate_multimode_peak_hints"]
        )


def test_fixed_target_rank_binds_production_primary_before_delivery():
    candidate = SimpleNamespace(
        result=SimpleNamespace(
            modes=[{"omega0": 3.9}, {"omega0": 8.1}]
        )
    )
    assert campaign._target_rank_hint(candidate, {"target_rank": "lower"}) == 3.9
    assert campaign._target_rank_hint(candidate, {"target_rank": "upper"}) == 8.1
    fit = {"omega0": 3.9, "modes": candidate.result.modes}
    assert campaign._fixed_target_estimate(fit, {"target_rank": "lower"}) == 3.9


def test_campaign_spec_is_picklable_for_windows_process_workers():
    assert pickle.loads(pickle.dumps(_spec())).fingerprint == _spec().fingerprint
    with ProcessPoolExecutor(
        max_workers=1, mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        assert executor.submit(_worker_spec_fingerprint, _spec()).result() == (
            _spec().fingerprint
        )


def test_delivery_input_fingerprint_is_worker_count_independent():
    inputs = {
        "selected_label": "stokes+bg",
        "delivery_context_fingerprint": "context",
        "predeclared_target_rank": "single",
        "candidate_target_hint": 4.0,
        "candidate_multimode_peak_hints": None,
        "errors_tier": "full",
        "bootstrap_replicates": 200,
        "delivery_seed": 123,
    }
    expected = campaign._delivery_input_fingerprint(inputs)
    for worker_count in (1, 2):
        with ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=multiprocessing.get_context("spawn"),
        ) as executor:
            observed = list(
                executor.map(
                    campaign._delivery_input_fingerprint,
                    [inputs] * (worker_count + 1),
                )
            )
        assert observed == [expected] * (worker_count + 1)


def test_package_public_surface_does_not_open_sealed_truth_generation():
    import isar.synth.stat2b as stat2b

    assert stat2b.__all__ == [
        "load_campaign_spec",
        "run_campaign",
        "summarize_campaign",
    ]
    assert not hasattr(stat2b, "generate_dataset")


def test_rescue_cell_builds_a_real_in_range_production_branch():
    base = _spec()
    weak = base.scenario("weak")
    rescue = replace(
        weak,
        id="rescue",
        energies=(0.72075,),
        gamma=0.05,
        constraint_kind="post_rail_rescue",
        constraint_origin="data_dependent_rescue",
        constraint_offset_sign=1,
        constraint_offset_mev=0.0,
    )
    spec = replace(
        base,
        scenarios=tuple(
            rescue if scenario.id == "weak" else scenario
            for scenario in base.scenarios
        ),
    )
    dataset = generate_dataset(spec, "rescue", 0)
    prior, card = campaign._delivery_prior_and_card(dataset, rescue)
    prediction = branch_model.branch_for(
        card.state,
        sample="STAT2B",
        T=rescue.T,
        omega0=prior.omega0,
    )
    assert prediction is not None
    assert prediction.in_anchor_range


def test_campaign_only_candidates_never_enter_production_enumeration():
    dataset = generate_dataset(_spec(), "weak", 0)
    labels = {hypothesis.label for hypothesis in enumerate_structures(dataset.fit_context)}
    assert labels.isdisjoint(NULL_LABELS)
    assert labels == {
        "stokes+bg",
        "elastic+stokes+bg",
        "stokes+qe+bg",
        "elastic+stokes+qe+bg",
    }


@pytest.mark.slow
def test_full_tier_delivery_smoke_uses_authoritative_m200():
    spec = _spec()
    dataset = generate_dataset(spec, "weak", 0)
    standard, _ = campaign._standard_evidence(dataset.fit_context)
    selected = campaign._selection(standard, "aic")
    candidate = next(
        item for item in standard if item.label == selected.selected_label
    )
    fit = campaign._full_delivery(
        dataset,
        spec,
        delivery_inputs=campaign._delivery_inputs(dataset, candidate, spec),
    )
    assert fit["errors_tier_effective"] == "full"
    assert fit["best_label"] == candidate.label
    assert fit["bootstrap_M_requested"] == 200


@pytest.mark.slow
def test_tiny_end_to_end_full_tier_campaign_realization():
    spec = _spec()
    record = campaign._run_realization(spec, "weak", 0, "development")
    assert record["schema"] == campaign.RECORD_SCHEMA
    assert [arm["arm_id"] for arm in record["arms"]] == [
        item[0] for item in campaign.STANDARD_ARMS
    ]
    assert record["delivery_deduplication"]["unique_delivery_count"] >= 1
    delivered = [arm["delivery"] for arm in record["arms"] if arm["delivery"]]
    assert delivered
    assert all(item["errors_tier_effective"] == "full" for item in delivered)
    assert all(item["bootstrap_M_requested"] == 200 for item in delivered)


@pytest.mark.slow
def test_oracle_realization_is_complete_for_multimode_cell():
    record = campaign._run_realization(
        _spec(), "partial", 0, "development"
    )
    arms = {arm["arm_id"]: arm for arm in record["arms"]}
    assert set(campaign.ORACLE_ARMS[i][0] for i in range(3)).issubset(arms)
    oracle_labels = set(arms["oracle_aic"]["candidate_labels"])
    assert any("stokes2" in label for label in oracle_labels)
    assert set(NULL_LABELS).issubset(oracle_labels)
    assert arms["oracle_aic"]["execution_provenance"]["multimode_route"] == (
        "oracle_complete"
    )
    for arm in arms.values():
        delivery = arm["delivery"]
        if delivery and delivery["n_modes"] == 2:
            assert delivery["campaign_target_rank"] == "lower"
            assert delivery["omega0"] == pytest.approx(
                min(mode["omega0"] for mode in delivery["modes"])
            )


@pytest.mark.slow
@pytest.mark.parametrize(
    ("constraint_kind", "constraint_origin"),
    [
        ("hard_prior_predictive", "prior_predictive"),
        ("post_rail_rescue", "data_dependent_rescue"),
    ],
)
def test_constrained_and_rescue_cells_complete_real_delivery(
    constraint_kind, constraint_origin
):
    base = _spec()
    weak = base.scenario("weak")
    constrained = replace(
        weak,
        id="constrained",
        energies=(0.72075,),
        gamma=0.05,
        constraint_kind=constraint_kind,
        constraint_origin=constraint_origin,
        constraint_offset_sign=1,
        constraint_offset_mev=0.0,
    )
    spec = replace(
        base,
        scenarios=tuple(
            constrained if scenario.id == "weak" else scenario
            for scenario in base.scenarios
        ),
    )
    record = campaign._run_realization(
        spec, constrained.id, 0, "development"
    )
    deliveries = [
        arm["delivery"] for arm in record["arms"] if arm["delivery"] is not None
    ]
    assert deliveries
    assert all(item["bootstrap_M_requested"] == 200 for item in deliveries)
    if constraint_kind == "hard_prior_predictive":
        assert any(item["fit_kind"] == "branch_conditional" for item in deliveries)
