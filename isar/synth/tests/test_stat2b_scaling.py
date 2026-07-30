from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from isar.synth.stat2b import scaling
from isar.synth.stat2b.spec import load_campaign_spec


SPEC_PATH = Path("tools/stats/stat2b-development.toml")


@pytest.fixture
def serial_thread_environment(monkeypatch):
    for key in scaling.THREAD_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("ISAR_PHYSICAL_CORES", "16")


def _manifest(
    workers: int,
    *,
    throughput: float,
    fingerprints: dict[str, str] | None = None,
    failures: int = 0,
) -> dict:
    scientific = fingerprints or {
        f"cell-{index // 2}:{index % 2}": f"fingerprint-{index}"
        for index in range(scaling.EXPECTED_DATASET_COUNT)
    }
    completed = scaling.EXPECTED_DATASET_COUNT - failures
    return {
        "worker_count": workers,
        "wall_seconds": completed * 60.0 / throughput,
        "datasets_per_minute": throughput,
        "completed_dataset_count": completed,
        "failure_count": failures,
        "peak_worker_count_reached": min(workers, completed),
        "scientific_record_fingerprints": scientific,
        "numerical_runtime": {
            "dataset_worker_count": workers,
            "thread_environment": {
                key: "1" for key in scaling.THREAD_ENVIRONMENT_KEYS
            },
        },
        "numerical_runtime_fingerprint": f"runtime-{workers}",
    }


def _selection_rows(
    throughputs: tuple[float, float, float, float, float],
) -> list[dict]:
    return [
        {
            "worker_count": workers,
            "datasets_per_minute": throughput,
            "completed_dataset_count": scaling.EXPECTED_DATASET_COUNT,
            "failure_count": 0,
            "scientific_identity": True,
        }
        for workers, throughput in zip(scaling.WORKER_LADDER, throughputs)
    ]


def test_scaling_ladder_is_exact_and_stops_at_twenty_eight():
    assert scaling.WORKER_LADDER == (4, 8, 16, 24, 28)
    assert max(scaling.WORKER_LADDER) == 28
    assert scaling.REALIZATIONS_PER_CELL == 2
    assert scaling.EXPECTED_DATASET_COUNT == 42


def test_scaling_spec_cannot_reach_sealed_phase():
    source = load_campaign_spec(SPEC_PATH)
    opened = scaling._scaling_spec(source)

    assert opened.phase_seed_namespaces["development"] == (
        "stat2b-scaling-open-v1"
    )
    assert opened.phase_seed_namespaces["heldout"] == (
        "stat2b-scaling-heldout-disabled"
    )
    assert opened.phase_seed_namespaces["heldout"] != (
        source.phase_seed_namespaces["heldout"]
    )
    assert opened.heldout_realizations == 0
    assert opened.heldout_gate_path is None


def test_scaling_refuses_uncontrolled_nested_threads(monkeypatch):
    for key in scaling.THREAD_ENVIRONMENT_KEYS:
        monkeypatch.setenv(key, "1")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "8")

    with pytest.raises(RuntimeError, match="OPENBLAS_NUM_THREADS='8'"):
        scaling._require_serial_numerical_threads()


def test_non_runtime_scientific_fingerprint_ignores_timing_only():
    left = {
        "dataset_id": "dataset",
        "numerical_runtime_fingerprint": "runtime-a",
        "runtime_seconds": 1.0,
        "record_fingerprint": "record-a",
        "arms": [{"label": "fit", "provenance": {"runtime_seconds": 0.2}}],
    }
    right = {
        "dataset_id": "dataset",
        "numerical_runtime_fingerprint": "runtime-b",
        "runtime_seconds": 99.0,
        "record_fingerprint": "record-b",
        "arms": [{"label": "fit", "provenance": {"runtime_seconds": 50.0}}],
    }
    assert (
        scaling.scientific_record_fingerprint(left)
        == scaling.scientific_record_fingerprint(right)
    )
    right["arms"][0]["label"] = "different-fit"
    assert (
        scaling.scientific_record_fingerprint(left)
        != scaling.scientific_record_fingerprint(right)
    )


def test_worker_selection_uses_smallest_count_within_ninety_percent():
    rows = _selection_rows((10.0, 18.0, 25.0, 26.0, 24.5))
    assert scaling.select_worker_count(rows) == 16


def test_worker_selection_refuses_identity_mismatch():
    manifests = [
        _manifest(workers, throughput=throughput)
        for workers, throughput in zip(
            scaling.WORKER_LADDER,
            (10.0, 16.0, 24.0, 25.0, 40.0),
        )
    ]
    altered = dict(manifests[-1]["scientific_record_fingerprints"])
    altered["cell-0:0"] = "changed"
    manifests[-1]["scientific_record_fingerprints"] = altered

    rows = scaling._summary_rows(manifests)

    assert rows[-1]["scientific_identity"] is False
    assert rows[-1]["scientific_difference_count"] == 1
    assert scaling.select_worker_count(rows) == 16


def test_worker_selection_refuses_failures_and_oversubscription():
    manifests = [
        _manifest(workers, throughput=throughput)
        for workers, throughput in zip(
            scaling.WORKER_LADDER,
            (10.0, 19.0, 27.0, 28.0, 20.0),
        )
    ]
    manifests[3] = _manifest(24, throughput=28.0, failures=1)
    rows = scaling._summary_rows(manifests)

    assert rows[3]["scientific_identity"] is False
    assert rows[4]["oversubscription_regression"] is True
    assert scaling.select_worker_count(rows) == 16


def test_one_worker_count_uses_all_cells_twice_without_smoke(
    tmp_path,
    monkeypatch,
    serial_thread_environment,
):
    spec = scaling._scaling_spec(load_campaign_spec(SPEC_PATH))
    captured: list[tuple] = []

    class ImmediateFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class ImmediateExecutor:
        def __init__(self, *, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, function, args):
            captured.append(args)
            return ImmediateFuture(function(args))

    def fake_task(args):
        scenario_id = args[1]
        realization_index = args[2]
        return {
            "status": "complete",
            "scenario_id": scenario_id,
            "realization_index": realization_index,
            "worker_pid": os.getpid(),
            "record": {
                "scenario_id": scenario_id,
                "realization_index": realization_index,
                "dataset_id": f"{scenario_id}-{realization_index}",
                "phase": args[3],
                "runtime_seconds": float(realization_index),
                "numerical_runtime_fingerprint": args[5],
            },
        }

    monkeypatch.setattr(scaling, "ProcessPoolExecutor", ImmediateExecutor)
    monkeypatch.setattr(scaling, "as_completed", lambda futures: futures)
    monkeypatch.setattr(scaling, "_run_scaling_task", fake_task)

    output_dir = tmp_path / "workers-04"
    manifest = scaling._run_worker_count(
        spec,
        output_dir=output_dir,
        workers=4,
        source_fingerprint="source",
        thread_environment=scaling._require_serial_numerical_threads(),
    )

    assert len(captured) == 42
    assert {args[3] for args in captured} == {"development"}
    assert {
        args[1] for args in captured
    } == {scenario.id for scenario in spec.scenarios}
    assert {
        args[2] for args in captured
    } == {0, 1}
    assert manifest["completed_dataset_count"] == 42
    assert manifest["inner_bootstrap_workers"] == 1
    assert len(list((output_dir / "checkpoints").glob("*.json"))) == 42
    assert (output_dir / "records.jsonl").read_text(
        encoding="utf-8"
    ).count("\n") == 42
    assert not (output_dir / "decision_summary.json").exists()


def test_preflight_writes_summary_and_never_reuses_worker_directories(
    tmp_path,
    monkeypatch,
    serial_thread_environment,
):
    spec = load_campaign_spec(SPEC_PATH)
    throughputs = {4: 10.0, 8: 18.0, 16: 25.0, 24: 26.0, 28: 24.5}

    monkeypatch.setattr(scaling.campaign_mod, "_source_hashes", lambda root: {})
    monkeypatch.setattr(
        scaling.campaign_mod, "_source_fingerprint", lambda hashes: "source"
    )

    def fake_run(spec, *, output_dir, workers, **kwargs):
        output_dir.mkdir(parents=True)
        manifest = _manifest(workers, throughput=throughputs[workers])
        scaling._write_json(output_dir / "run_manifest.json", manifest)
        return manifest

    monkeypatch.setattr(scaling, "_run_worker_count", fake_run)
    root = tmp_path / "scaling"

    summary = scaling.run_scaling_preflight(spec, output_root=root)

    assert summary["worker_ladder"] == [4, 8, 16, 24, 28]
    assert summary["selected_worker_count"] == 16
    assert summary["evidence_eligible"] is False
    assert (root / "scaling-summary.json").is_file()
    assert (root / "scaling-summary.csv").is_file()
    assert not (root / "decision_summary.json").exists()
    assert [
        path.name for path in sorted(root.glob("workers-*"))
    ] == ["workers-04", "workers-08", "workers-16", "workers-24", "workers-28"]

    with pytest.raises(FileExistsError, match="refuses checkpoint reuse"):
        scaling.run_scaling_preflight(spec, output_root=root)

    recreated = scaling.run_scaling_preflight(
        spec, output_root=root, recreate=True
    )
    assert recreated["selected_worker_count"] == 16
    persisted = json.loads(
        (root / "scaling-summary.json").read_text(encoding="utf-8")
    )
    assert persisted["dataset_count_per_worker"] == 42
