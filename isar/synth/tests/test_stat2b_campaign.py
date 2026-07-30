from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.structures import enumerate_structures
from isar.schema.steering import ParamConstraints
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


def _fake_record(spec, scenario_id, index, phase, source_fingerprint):
    record = {
        "schema": campaign.RECORD_SCHEMA,
        "campaign_id": spec.campaign_id,
        "campaign_spec_fingerprint": spec.fingerprint,
        "campaign_source_fingerprint": source_fingerprint,
        "phase": phase,
        "scenario_id": scenario_id,
        "scenario_fingerprint": spec.scenario(scenario_id).fingerprint,
        "realization_index": index,
        "dataset_id": f"{phase}-{scenario_id}-{index}",
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
    assert heldout.heldout_realizations == 0
    assert [cell.fingerprint for cell in heldout.scenarios] == [
        cell.fingerprint for cell in development.scenarios
    ]
    assert development.fingerprint != heldout.fingerprint


def test_checkpoint_resume_is_scheduling_independent(tmp_path, monkeypatch):
    spec = _spec()
    calls = []

    def fake_realization(
        spec_arg, scenario_id, realization_index, phase, source_fingerprint
    ):
        calls.append((scenario_id, realization_index))
        return _fake_record(
            spec_arg, scenario_id, realization_index, phase, source_fingerprint
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


def test_heldout_refuses_unfrozen_count_before_opening_seeds(tmp_path):
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
        "source_hashes": {"campaign.py": "hash"},
        "heldout_realizations": 20,
        "margins": {"predictive_loss_superiority": {"maximum": 0.0}},
        "runtime_ceiling_seconds": 100.0,
    }
    path = tmp_path / "gate.json"
    path.write_text(json.dumps(gate), encoding="utf-8")
    assert campaign._validate_gate(
        spec, path, source_hashes=gate["source_hashes"]
    ) == gate

    changed_spec = replace(spec, heldout_realizations=21)
    with pytest.raises(RuntimeError, match="specification changed"):
        campaign._validate_gate(
            changed_spec, path, source_hashes=gate["source_hashes"]
        )
    with pytest.raises(RuntimeError, match="source hashes changed"):
        campaign._validate_gate(
            spec, path, source_hashes={"campaign.py": "changed"}
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
        for arm_id in ("legacy_aic", "unconstrained_aicc"):
            improved = arm_id == "unconstrained_aicc"
            arms.append(
                {
                    "arm_id": arm_id,
                    "comparison_status": "available",
                    "prediction_kl": 0.5 if improved else 1.0,
                    "realized_log_score": -1.0,
                    "candidate_route_regret": 0.0,
                    "target_squared_error": 0.01,
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
        manifest={"run_fingerprint": "run", "runtime_seconds": 1.0},
    )
    summary = campaign.summarize_campaign(run, spec)
    gates = summary["proposed_owner_gate"]["margins"]
    assert len(gates) == 6
    assert set(summary["critical_strata_vetoes"]) == set(CRITICAL_STRATA)
    assert summary["cell_weighting"] == "equal"
    assert summary["proposed_owner_gate"]["owner_approved"] is False
    assert summary["proposed_owner_gate"]["schema"] == campaign.GATE_SCHEMA
    assert summary["proposed_owner_gate"]["status"] == "authority_withheld"
    assert (tmp_path / "decision_summary.json").is_file()
    assert (tmp_path / "cell_summaries.csv").is_file()


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
    evaluation = campaign._evaluate_frozen_gate(gate, overall, strata)
    assert evaluation["status"] == "authority_withheld"
    assert evaluation["checks"]["prediction"]["target_absent"]["status"] == "fail"


def _candidate(ctx, label="stokes+bg", cost=1.0):
    hypothesis = enumerate_structures(ctx)[0]
    result = SimpleNamespace(
        label=label,
        k=4,
        ndof=ctx.x_used.size - 4,
        cost=cost,
        optimizer_converged=True,
        params={},
        n_modes=1,
        modes=[{"omega0": 4.0}],
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
    monkeypatch.setattr(campaign, "generate_dataset", lambda *a, **k: dataset)
    monkeypatch.setattr(
        campaign, "_legacy_evidence", lambda _ctx: ([candidate], provenance)
    )
    monkeypatch.setattr(
        campaign, "_standard_evidence", lambda _ctx: ([candidate], provenance)
    )
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
    assert record["delivery_deduplication"]["unique_delivery_count"] == 1
    assert record["delivery_deduplication"]["arm_count_requiring_delivery"] == 6


def test_fixed_target_rank_never_rematches_nearest_mode():
    fit = {
        "omega0": 8.1,
        "modes": [{"omega0": 3.9}, {"omega0": 8.1}],
    }
    assert campaign._fixed_target_estimate(fit, {"target_rank": "lower"}) == 3.9
    assert campaign._fixed_target_estimate(fit, {"target_rank": "upper"}) == 8.1


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
    fit = campaign._full_delivery(dataset, candidate, spec, seed=123)
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
