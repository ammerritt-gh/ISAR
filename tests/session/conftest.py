"""Explicit numerical stubs for session orchestration tests only."""
import pytest

from isar.session import scan_delivery


@pytest.fixture
def fixed_fit(monkeypatch):
    """Only orchestration tests opt in: real ingest/calibration/ledger, fixed fit output.

    No assertion in these tests depends on the optimizer. Keep the real fitting and
    spawned workers in test_parallel_masters_match_serial_end_to_end.
    """
    def fit_one(ctx, card, **kwargs):
        return dict(verdict="signal_unmodeled", confidence="low", omega0=None,
                    omega0_err=None, interval=None, modality="fold", gamma=None,
                    best_label=None, cost_per_dof=None, reasons=[], n_modes=0,
                    modes=[], fast_pass=None, interval_reliable=False,
                    fast_pass_timing={"total_seconds": 0.125, "stop_reason": "complete"})

    monkeypatch.setattr(scan_delivery, "fit_one", fit_one)
