"""Export reviewed STAT-2b.1 aggregate evidence for documentation promotion."""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Mapping


REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "out"
OLD_DEVELOPMENT = (
    OUT
    / "stat2b"
    / "stat2b-selection-criterion-2026-07"
    / "development-pre-stat2b1-95d2296"
)
NEW_DEVELOPMENT = (
    OUT
    / "stat2b"
    / "stat2b1-selection-criterion-2026-07"
    / "development"
)
SCALING = OUT / "stat2b-scaling"
TARGET = OUT / "stat2b_evidence"
VERSION = "2026-07-30-stat2b1"

ARM_AUDIT_FIELDS = {
    "delivery_input_fingerprint",
    "delivery_inputs",
    "delivery_reuse_group",
    "delivery_reused",
    "delivery_source_arm",
}
DELIVERY_TIMING_FIELDS = {
    "errors_attach_seconds",
    "runtime_seconds",
    "two_stage_seconds",
}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _read_records(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    records: dict[tuple[str, int], dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            key = (str(value["scenario_id"]), int(value["realization_index"]))
            if key in records:
                raise ValueError(f"duplicate dataset key in {path}: {key}")
            records[key] = value
    return records


def _without_keys(value: Any, excluded: set[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_keys(item, excluded)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in excluded
        }
    if isinstance(value, list):
        return [_without_keys(item, excluded) for item in value]
    return value


def _arm_map(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(arm["arm_id"]): dict(arm) for arm in record["arms"]}


def _comparison() -> dict[str, Any]:
    old_summary = _read_json(OLD_DEVELOPMENT / "decision_summary.json")
    new_summary = _read_json(NEW_DEVELOPMENT / "decision_summary.json")
    old_records = _read_records(OLD_DEVELOPMENT / "datasets.jsonl")
    new_records = _read_records(NEW_DEVELOPMENT / "datasets.jsonl")
    if old_records.keys() != new_records.keys():
        raise ValueError("pre- and post-repair development dataset keys differ")

    changed_hint_by_arm: Counter[str] = Counter()
    changed_hint_by_scenario: Counter[str] = Counter()
    changed_hint_datasets: set[tuple[str, int]] = set()
    selection_and_metrics_unchanged = True
    delivery_except_hint_and_timing_unchanged = True
    for dataset_key in sorted(old_records):
        old_arms = _arm_map(old_records[dataset_key])
        new_arms = _arm_map(new_records[dataset_key])
        if old_arms.keys() != new_arms.keys():
            raise ValueError(f"arm set changed for {dataset_key}")
        for arm_id in old_arms:
            old_arm = old_arms[arm_id]
            new_arm = new_arms[arm_id]
            old_outer = {
                key: value
                for key, value in old_arm.items()
                if key not in {"delivery", "execution_provenance"}
            }
            new_outer = {
                key: value
                for key, value in new_arm.items()
                if key
                not in {
                    "delivery",
                    "execution_provenance",
                    *ARM_AUDIT_FIELDS,
                }
            }
            selection_and_metrics_unchanged &= old_outer == new_outer

            old_delivery = old_arm.get("delivery")
            new_delivery = new_arm.get("delivery")
            if old_delivery is None or new_delivery is None:
                delivery_except_hint_and_timing_unchanged &= (
                    old_delivery is None and new_delivery is None
                )
                continue
            old_hint = old_delivery.get("campaign_target_hint")
            new_hint = new_delivery.get("campaign_target_hint")
            if old_hint != new_hint:
                changed_hint_by_arm[arm_id] += 1
                changed_hint_by_scenario[dataset_key[0]] += 1
                changed_hint_datasets.add(dataset_key)
            excluded = {
                *DELIVERY_TIMING_FIELDS,
                "campaign_target_hint",
            }
            delivery_except_hint_and_timing_unchanged &= (
                _without_keys(old_delivery, excluded)
                == _without_keys(new_delivery, excluded)
            )

    old_gate = old_summary["proposed_owner_gate"]
    new_gate = new_summary["proposed_owner_gate"]
    scaling = _read_json(SCALING / "scaling-summary.json")
    if scaling.get("selected_worker_count") != 24:
        raise ValueError("reviewed scaling summary did not select 24 workers")
    for row in scaling.get("runs", []):
        if (
            row.get("failure_count") != 0
            or row.get("scientific_identity") is not True
            or row.get("scientific_difference_count") != 0
        ):
            raise ValueError("scaling summary contains an ineligible worker tier")
    if new_gate.get("owner_approved") is not False:
        raise ValueError("corrected proposed owner gate must remain unapproved")

    return {
        "schema": "isar.stat2b_development_comparison/1",
        "pre_stat2b1": {
            "development_summary_fingerprint": old_summary[
                "development_summary_fingerprint"
            ],
            "unique_delivery_count": old_summary["delivery_deduplication"][
                "unique_delivery_count"
            ],
            "worker_count": old_gate["workers"],
            "projected_heldout_runtime_seconds": old_summary["runtime"][
                "projected_heldout_runtime_seconds"
            ],
            "runtime_ceiling_seconds": old_gate["runtime_ceiling_seconds"],
        },
        "corrected_stat2b1": {
            "development_summary_fingerprint": new_summary[
                "development_summary_fingerprint"
            ],
            "unique_delivery_count": new_summary["delivery_deduplication"][
                "unique_delivery_count"
            ],
            "worker_count": new_gate["workers"],
            "projected_heldout_runtime_seconds": new_summary["runtime"][
                "projected_heldout_runtime_seconds"
            ],
            "runtime_ceiling_seconds": new_gate["runtime_ceiling_seconds"],
        },
        "dataset_key_count": len(new_records),
        "identical_open_dataset_keys": True,
        "selection_and_scored_arm_outputs_unchanged": (
            selection_and_metrics_unchanged
        ),
        "delivery_outputs_except_declared_hint_and_timing_unchanged": (
            delivery_except_hint_and_timing_unchanged
        ),
        "delivery_target_hint_change": {
            "dataset_count": len(changed_hint_datasets),
            "dataset_arm_count": sum(changed_hint_by_arm.values()),
            "by_arm": dict(sorted(changed_hint_by_arm.items())),
            "by_scenario": dict(sorted(changed_hint_by_scenario.items())),
        },
        "unique_delivery_count_change": (
            new_summary["delivery_deduplication"]["unique_delivery_count"]
            - old_summary["delivery_deduplication"]["unique_delivery_count"]
        ),
        "arm_count_requiring_delivery_unchanged": (
            old_summary["delivery_deduplication"]["arm_count_requiring_delivery"]
            == new_summary["delivery_deduplication"][
                "arm_count_requiring_delivery"
            ]
        ),
        "proposed_heldout_realizations_unchanged": (
            old_gate["heldout_realizations"] == new_gate["heldout_realizations"]
        ),
        "proposed_numerical_margins_unchanged": (
            old_gate["margins"] == new_gate["margins"]
        ),
        "scaling_summary_fingerprint": scaling["summary_fingerprint"],
        "selected_worker_count": scaling["selected_worker_count"],
        "aborted_heldout": {
            "checkpoint_count_at_stop": 1254,
            "heldout_records_inspected": False,
            "scientific_eligibility": "ineligible",
        },
    }


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def _copy(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".csv":
        destination.write_text(
            source.read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    else:
        shutil.copy2(source, destination)


def main() -> int:
    comparison = _comparison()
    stage = Path(tempfile.mkdtemp(prefix=".stat2b-evidence-", dir=OUT))
    versioned = stage / VERSION
    try:
        for source, relative in (
            (
                REPO / "tools" / "stats" / "stat2b-development.toml",
                "campaign-spec.toml",
            ),
            (NEW_DEVELOPMENT / "run_manifest.json", "run_manifest.json"),
            (
                NEW_DEVELOPMENT / "decision_summary.json",
                "decision_summary.json",
            ),
            (NEW_DEVELOPMENT / "cell_summaries.csv", "cell_summaries.csv"),
            (NEW_DEVELOPMENT / "generated_findings.md", "FINDINGS.md"),
            (
                NEW_DEVELOPMENT / "proposed_owner_gate.json",
                "proposed_owner_gate.json",
            ),
            (SCALING / "scaling-summary.json", "scaling-summary.json"),
            (SCALING / "scaling-summary.csv", "scaling-summary.csv"),
            (
                NEW_DEVELOPMENT
                / "figures"
                / "predictive_loss_by_arm.png",
                "figures/predictive_loss_by_arm.png",
            ),
            (
                NEW_DEVELOPMENT
                / "figures"
                / "critical_strata_predictive_difference.png",
                "figures/critical_strata_predictive_difference.png",
            ),
        ):
            _copy(source, versioned / relative)
        _write_json(versioned / "development-comparison.json", comparison)
        captions = {
            "predictive_loss_by_arm.caption.md": (
                "**Equal-cell predictive loss by campaign arm** — mean analytic "
                "Poisson KL per kept bin for every frozen selection arm. Lower is "
                "better; this is development evidence, not a production ruling.\n"
            ),
            "critical_strata_predictive_difference.caption.md": (
                "**Critical-stratum AICc minus AIC predictive loss** — paired "
                "development differences for every independent veto stratum. "
                "Values at or below zero favor AICc; held-out authority remains "
                "sealed pending owner approval.\n"
            ),
        }
        for name, text in captions.items():
            destination = versioned / "figures" / name
            destination.write_text(text, encoding="utf-8", newline="\n")
        if TARGET.exists():
            shutil.rmtree(TARGET)
        os.replace(stage, TARGET)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"wrote {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
