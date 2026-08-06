"""Development-only STAT-2b process-scaling preflight.

This module deliberately has no phase argument.  It derives a public scaling
seed namespace from the development specification, executes two realizations
from every campaign cell, and never reads a held-out gate or held-out seed
namespace.  Scaling output is performance evidence only; it is not a
selection-criterion decision artifact.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
import csv
import json
import logging
import os
from pathlib import Path
import shutil
import time
import traceback
from typing import Any, Mapping, Sequence

from isar.schema.statistics import semantics_fingerprint

from . import campaign as campaign_mod
from .spec import CampaignSpec, load_campaign_spec


SCALING_SCHEMA = "isar.stat2b_scaling_preflight/1"
SCALING_RUN_SCHEMA = "isar.stat2b_scaling_run/1"
SCALING_NAMESPACE = "stat2b-scaling-open-v1"
SCALING_DISABLED_NAMESPACE = "stat2b-scaling-heldout-disabled"
WORKER_LADDER = (4, 8, 16, 24, 28)
REALIZATIONS_PER_CELL = 2
EXPECTED_SCENARIO_COUNT = 21
EXPECTED_DATASET_COUNT = EXPECTED_SCENARIO_COUNT * REALIZATIONS_PER_CELL
THROUGHPUT_FRACTION = 0.90
THREAD_ENVIRONMENT_KEYS = campaign_mod.THREAD_ENVIRONMENT_VARIABLES

_NON_RUNTIME_KEYS = {
    "record_fingerprint",
    "runtime_seconds",
    "numerical_runtime_fingerprint",
    "two_stage_seconds",
    "errors_attach_seconds",
}
_GENERATED_ROOT_FILES = (
    "scaling-summary.json",
    "scaling-summary.csv",
)


def _strict_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strict_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_strict_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not (float("-inf") < value < float("inf")):
            raise ValueError("scaling output contains a non-finite float")
        return value
    raise TypeError(f"unsupported scaling JSON value {type(value).__name__}")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _strict_json_value(value),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, _json_bytes(value))


def _thread_environment() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in THREAD_ENVIRONMENT_KEYS}


def _require_serial_numerical_threads() -> dict[str, str]:
    values = _thread_environment()
    invalid = {key: value for key, value in values.items() if value != "1"}
    if invalid:
        detail = ", ".join(f"{key}={value!r}" for key, value in invalid.items())
        raise RuntimeError(
            "STAT-2b scaling refused: numerical thread variables must all be "
            f"set to '1' before Python starts ({detail})"
        )
    return {key: "1" for key in THREAD_ENVIRONMENT_KEYS}


def _scaling_spec(spec: CampaignSpec) -> CampaignSpec:
    """Return a spec that contains no route to the sealed seed namespace."""
    if len(spec.scenarios) != EXPECTED_SCENARIO_COUNT:
        raise ValueError(
            "STAT-2b scaling requires the complete 21-cell development matrix"
        )
    return replace(
        spec,
        campaign_id=f"{spec.campaign_id}-scaling-open-v1",
        phase_seed_namespaces={
            "development": SCALING_NAMESPACE,
            "heldout": SCALING_DISABLED_NAMESPACE,
        },
        heldout_realizations=0,
        heldout_gate_path=None,
    )


def _without_runtime(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_runtime(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _NON_RUNTIME_KEYS
            and not str(key).endswith("_runtime_seconds")
        }
    if isinstance(value, (list, tuple)):
        return [_without_runtime(item) for item in value]
    return value


def scientific_record_fingerprint(record: Mapping[str, Any]) -> str:
    """Fingerprint all persisted scientific content, excluding timing/runtime."""
    return semantics_fingerprint(
        "isar.stat2b_scaling_scientific_record/1",
        _without_runtime(record),
    )


def _task_key(scenario_id: str, realization_index: int) -> str:
    return f"{scenario_id}:{realization_index}"


def _checkpoint_name(scenario_id: str, realization_index: int) -> str:
    return semantics_fingerprint(
        "isar.stat2b_scaling_checkpoint/1",
        {
            "namespace": SCALING_NAMESPACE,
            "scenario_id": scenario_id,
            "realization_index": realization_index,
        },
    )


def _run_scaling_task(args: tuple[Any, ...]) -> dict[str, Any]:
    scenario_id = str(args[1])
    realization_index = int(args[2])
    try:
        record = campaign_mod._run_realization(*args)
        return {
            "status": "complete",
            "scenario_id": scenario_id,
            "realization_index": realization_index,
            "worker_pid": os.getpid(),
            "record": record,
        }
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "STAT-2b scaling realization failed: %s[%d]",
            scenario_id,
            realization_index,
        )
        return {
            "status": "failed",
            "scenario_id": scenario_id,
            "realization_index": realization_index,
            "worker_pid": os.getpid(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def _prepare_output_root(output_root: Path, *, recreate: bool) -> None:
    existing_worker_dirs = [
        output_root / f"workers-{workers:02d}" for workers in WORKER_LADDER
    ]
    generated_paths = existing_worker_dirs + [
        output_root / name for name in _GENERATED_ROOT_FILES
    ]
    existing = [path for path in generated_paths if path.exists()]
    if existing and not recreate:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(
            "STAT-2b scaling refuses checkpoint reuse; remove the isolated "
            f"scaling output or pass recreate=True ({names})"
        )
    if recreate:
        for path in existing_worker_dirs:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        for name in _GENERATED_ROOT_FILES:
            path = output_root / name
            if path.exists():
                path.unlink()
    output_root.mkdir(parents=True, exist_ok=True)


def _run_worker_count(
    spec: CampaignSpec,
    *,
    output_dir: Path,
    workers: int,
    source_fingerprint: str,
    thread_environment: Mapping[str, str],
) -> dict[str, Any]:
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(
            f"scaling worker output already exists and cannot be resumed: {output_dir}"
        ) from None
    checkpoint_dir = output_dir / "checkpoints"
    checkpoint_dir.mkdir()
    runtime_provenance = campaign_mod._runtime_provenance(workers)
    campaign_mod._validate_parallel_runtime(runtime_provenance, workers)
    runtime_fingerprint = campaign_mod._runtime_fingerprint(runtime_provenance)
    tasks = [
        (
            spec,
            scenario.id,
            realization_index,
            "development",
            source_fingerprint,
            runtime_fingerprint,
        )
        for scenario in spec.scenarios
        for realization_index in range(REALIZATIONS_PER_CELL)
    ]
    if len(tasks) != EXPECTED_DATASET_COUNT:
        raise AssertionError("scaling task matrix is not exactly 42 datasets")

    started = time.perf_counter()
    outcomes: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_scaling_task, task): task for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                outcome = future.result()
            except Exception as exc:
                logging.getLogger(__name__).exception(
                    "STAT-2b scaling worker process failed: %s[%d]",
                    task[1],
                    task[2],
                )
                outcome = {
                    "status": "failed",
                    "scenario_id": str(task[1]),
                    "realization_index": int(task[2]),
                    "worker_pid": None,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            outcomes.append(outcome)
            checkpoint = checkpoint_dir / (
                _checkpoint_name(
                    outcome["scenario_id"], outcome["realization_index"]
                )
                + ".json"
            )
            _write_json(checkpoint, outcome)
    wall_seconds = time.perf_counter() - started

    outcomes.sort(key=lambda item: (
        item["scenario_id"], item["realization_index"]
    ))
    records = [
        outcome["record"]
        for outcome in outcomes
        if outcome["status"] == "complete"
    ]
    failures = [
        {
            key: value
            for key, value in outcome.items()
            if key != "record"
        }
        for outcome in outcomes
        if outcome["status"] != "complete"
    ]
    scientific_fingerprints = {
        _task_key(record["scenario_id"], int(record["realization_index"])):
            scientific_record_fingerprint(record)
        for record in records
    }
    jsonl = b"".join(
        json.dumps(
            _strict_json_value(record),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )
    _atomic_write(output_dir / "records.jsonl", jsonl)
    peak_worker_count = len({
        int(outcome["worker_pid"])
        for outcome in outcomes
        if outcome.get("worker_pid") is not None
    })
    throughput = (
        len(records) * 60.0 / wall_seconds if wall_seconds > 0.0 else 0.0
    )
    manifest = {
        "schema": SCALING_RUN_SCHEMA,
        "evidence_eligible": False,
        "phase_namespace": SCALING_NAMESPACE,
        "campaign_spec_fingerprint": spec.fingerprint,
        "campaign_source_fingerprint": source_fingerprint,
        "worker_count": workers,
        "inner_bootstrap_workers": 1,
        "thread_environment": dict(thread_environment),
        "numerical_runtime": runtime_provenance,
        "numerical_runtime_fingerprint": runtime_fingerprint,
        "expected_scenario_count": EXPECTED_SCENARIO_COUNT,
        "realizations_per_cell": REALIZATIONS_PER_CELL,
        "expected_dataset_count": EXPECTED_DATASET_COUNT,
        "completed_dataset_count": len(records),
        "failure_count": len(failures),
        "failures": failures,
        "wall_seconds": wall_seconds,
        "datasets_per_minute": throughput,
        "peak_worker_count_reached": peak_worker_count,
        "scientific_record_fingerprints": scientific_fingerprints,
    }
    manifest["manifest_fingerprint"] = semantics_fingerprint(
        SCALING_RUN_SCHEMA,
        {
            key: value
            for key, value in manifest.items()
            if key not in {"wall_seconds", "datasets_per_minute", "manifest_fingerprint"}
        },
    )
    _write_json(output_dir / "run_manifest.json", manifest)
    return manifest


def _identity_differences(
    baseline: Mapping[str, str],
    candidate: Mapping[str, str],
) -> list[str]:
    keys = sorted(set(baseline) | set(candidate))
    return [
        key for key in keys
        if baseline.get(key) != candidate.get(key)
    ]


def select_worker_count(rows: Sequence[Mapping[str, Any]]) -> int:
    """Apply the frozen smallest-near-best, non-oversubscribed rule."""
    if tuple(int(row["worker_count"]) for row in rows) != WORKER_LADDER:
        raise ValueError(f"worker rows must use the exact ladder {WORKER_LADDER}")
    successful = [
        row for row in rows
        if int(row["failure_count"]) == 0
        and bool(row["scientific_identity"])
        and int(row["completed_dataset_count"]) == EXPECTED_DATASET_COUNT
    ]
    if not successful:
        raise RuntimeError("no scaling worker count completed valid scientific records")
    best_throughput = max(float(row["datasets_per_minute"]) for row in successful)
    threshold = THROUGHPUT_FRACTION * best_throughput

    best_lower = 0.0
    eligible: list[int] = []
    for row in rows:
        throughput = float(row["datasets_per_minute"])
        valid = row in successful
        oversubscription_regression = (
            valid
            and best_lower > 0.0
            and throughput < THROUGHPUT_FRACTION * best_lower
        )
        if valid and throughput >= threshold and not oversubscription_regression:
            eligible.append(int(row["worker_count"]))
        if valid:
            best_lower = max(best_lower, throughput)
    if not eligible:
        raise RuntimeError(
            "no worker count satisfies the throughput and oversubscription rule"
        )
    return min(eligible)


def _summary_rows(manifests: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baseline = dict(manifests[0]["scientific_record_fingerprints"])
    baseline_throughput = float(manifests[0]["datasets_per_minute"])
    rows: list[dict[str, Any]] = []
    best_lower = 0.0
    for manifest in manifests:
        worker_count = int(manifest["worker_count"])
        differences = _identity_differences(
            baseline,
            manifest["scientific_record_fingerprints"],
        )
        throughput = float(manifest["datasets_per_minute"])
        scientific_identity = (
            not differences
            and int(manifest["completed_dataset_count"]) == EXPECTED_DATASET_COUNT
        )
        valid_for_regression = (
            scientific_identity and int(manifest["failure_count"]) == 0
        )
        oversubscription_regression = (
            valid_for_regression
            and best_lower > 0.0
            and throughput < THROUGHPUT_FRACTION * best_lower
        )
        row = {
            "worker_count": worker_count,
            "wall_seconds": float(manifest["wall_seconds"]),
            "datasets_per_minute": throughput,
            "relative_speedup": (
                throughput / baseline_throughput
                if baseline_throughput > 0.0 else 0.0
            ),
            "parallel_efficiency": (
                (throughput / baseline_throughput) / (worker_count / WORKER_LADDER[0])
                if baseline_throughput > 0.0 else 0.0
            ),
            "completed_dataset_count": int(manifest["completed_dataset_count"]),
            "failure_count": int(manifest["failure_count"]),
            "peak_worker_count_reached": int(manifest["peak_worker_count_reached"]),
            "scientific_identity": scientific_identity,
            "scientific_difference_count": len(differences),
            "scientific_difference_keys": differences,
            "oversubscription_regression": oversubscription_regression,
        }
        rows.append(row)
        if valid_for_regression:
            best_lower = max(best_lower, throughput)
    return rows


def _write_summary_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    fields = (
        "worker_count",
        "wall_seconds",
        "datasets_per_minute",
        "relative_speedup",
        "parallel_efficiency",
        "completed_dataset_count",
        "failure_count",
        "peak_worker_count_reached",
        "scientific_identity",
        "scientific_difference_count",
        "oversubscription_regression",
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row[field] for field in fields})
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_scaling_preflight(
    spec: CampaignSpec,
    *,
    output_root: str | Path,
    recreate: bool = False,
) -> dict[str, Any]:
    """Run the sealed-safe 42-dataset scaling ladder and select worker count."""
    if not isinstance(spec, CampaignSpec):
        raise TypeError("spec must be a CampaignSpec")
    thread_environment = _require_serial_numerical_threads()
    scaling_spec = _scaling_spec(spec)
    root = Path(output_root)
    _prepare_output_root(root, recreate=recreate)

    repo_root = Path(__file__).resolve().parents[3]
    source_hashes = campaign_mod._source_hashes(repo_root)
    source_fingerprint = campaign_mod._source_fingerprint(source_hashes)
    manifests = [
        _run_worker_count(
            scaling_spec,
            output_dir=root / f"workers-{workers:02d}",
            workers=workers,
            source_fingerprint=source_fingerprint,
            thread_environment=thread_environment,
        )
        for workers in WORKER_LADDER
    ]
    rows = _summary_rows(manifests)
    selected_workers = select_worker_count(rows)
    best_throughput = max(float(row["datasets_per_minute"]) for row in rows)
    summary = {
        "schema": SCALING_SCHEMA,
        "evidence_eligible": False,
        "phase_namespace": SCALING_NAMESPACE,
        "worker_ladder": list(WORKER_LADDER),
        "scenario_count": EXPECTED_SCENARIO_COUNT,
        "realizations_per_cell": REALIZATIONS_PER_CELL,
        "dataset_count_per_worker": EXPECTED_DATASET_COUNT,
        "campaign_spec_fingerprint": scaling_spec.fingerprint,
        "campaign_source_fingerprint": source_fingerprint,
        "thread_environment": thread_environment,
        "numerical_runtime_by_worker": {
            str(manifest["worker_count"]): manifest["numerical_runtime"]
            for manifest in manifests
        },
        "numerical_runtime_fingerprint_by_worker": {
            str(manifest["worker_count"]): manifest[
                "numerical_runtime_fingerprint"
            ]
            for manifest in manifests
        },
        "inner_bootstrap_workers": 1,
        "selection_rule": {
            "minimum_fraction_of_best_throughput": THROUGHPUT_FRACTION,
            "selection": "smallest_eligible_worker_count",
            "substantial_oversubscription_regression_fraction": (
                1.0 - THROUGHPUT_FRACTION
            ),
        },
        "best_datasets_per_minute": best_throughput,
        "selected_worker_count": selected_workers,
        "runs": rows,
    }
    summary["summary_fingerprint"] = semantics_fingerprint(
        SCALING_SCHEMA,
        {
            key: value
            for key, value in summary.items()
            if key != "summary_fingerprint"
        },
    )
    _write_json(root / "scaling-summary.json", summary)
    _write_summary_csv(root / "scaling-summary.csv", rows)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the development-only STAT-2b process scaling preflight."
    )
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output-root", default="out/stat2b-scaling")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="discard only prior isolated scaling outputs and run every count fresh",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_scaling_preflight(
        load_campaign_spec(args.spec),
        output_root=args.output_root,
        recreate=args.recreate,
    )
    print(
        "STAT-2b scaling preflight complete: "
        f"selected {summary['selected_worker_count']} workers"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
