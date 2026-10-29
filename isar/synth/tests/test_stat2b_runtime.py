from pathlib import Path

import pytest

from isar.synth.stat2b import campaign


def _pin_runtime_environment(monkeypatch, *, physical_cores="16"):
    for name in campaign.THREAD_ENVIRONMENT_VARIABLES:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("ISAR_PHYSICAL_CORES", physical_cores)


def test_runtime_provenance_binds_workers_threads_blas_cores_and_affinity(monkeypatch):
    _pin_runtime_environment(monkeypatch)
    monkeypatch.setattr(campaign.os, "cpu_count", lambda: 32)
    monkeypatch.setattr(
        campaign,
        "_process_affinity",
        lambda: {
            "kind": "test",
            "logical_processor_count": 32,
            "logical_processors": list(range(32)),
        },
    )

    provenance = campaign._runtime_provenance(28)

    assert provenance["dataset_worker_count"] == 28
    assert provenance["physical_core_count"] == 16
    assert provenance["logical_processor_count"] >= 28
    assert provenance["thread_environment"] == {
        name: "1" for name in campaign.THREAD_ENVIRONMENT_VARIABLES
    }
    assert provenance["blas_implementation"]["name"]
    assert provenance["process_affinity"]["logical_processor_count"] >= 28
    campaign._validate_parallel_runtime(provenance, 28)


def test_runtime_fingerprint_changes_with_dataset_worker_count(monkeypatch):
    _pin_runtime_environment(monkeypatch)

    four = campaign._runtime_provenance(4)
    eight = campaign._runtime_provenance(8)

    assert campaign._runtime_fingerprint(four) != campaign._runtime_fingerprint(eight)


def test_parallel_runtime_refuses_unpinned_threads_or_missing_physical_cores(
    monkeypatch,
):
    _pin_runtime_environment(monkeypatch)
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "8")
    bad_threads = campaign._runtime_provenance(4)
    with pytest.raises(RuntimeError, match="OPENBLAS_NUM_THREADS"):
        campaign._validate_parallel_runtime(bad_threads, 4)

    _pin_runtime_environment(monkeypatch)
    monkeypatch.delenv("ISAR_PHYSICAL_CORES")
    missing_physical = campaign._runtime_provenance(4)
    with pytest.raises(RuntimeError, match="ISAR_PHYSICAL_CORES"):
        campaign._validate_parallel_runtime(missing_physical, 4)


@pytest.mark.parametrize(
    "launcher_name",
    ("run-stat2b-campaign.bat", "run-stat2b-scaling.bat"),
)
def test_windows_launchers_pin_numerical_threads_before_python(launcher_name):
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / "tools" / launcher_name).read_text(encoding="utf-8")
    python_offset = text.index("python -m isar.synth.stat2b")

    for name in campaign.THREAD_ENVIRONMENT_VARIABLES:
        assignment = f'set "{name}=1"'
        assert assignment in text
        assert text.index(assignment) < python_offset
    assert text.index("ISAR_PHYSICAL_CORES") < python_offset


def test_campaign_launcher_persists_traceback_log():
    repo_root = Path(__file__).resolve().parents[3]
    text = (repo_root / "tools" / "run-stat2b-campaign.bat").read_text(encoding="utf-8")

    assert 'set "STAT2B_LOG=' in text
    assert '>> "%STAT2B_LOG%" 2>&1' in text
    assert "Durable console log: %STAT2B_LOG%" in text
    assert "STAT-2b campaign failed. Check:" in text
