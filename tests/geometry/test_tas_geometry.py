"""sense_sample validation in the TAS angle solvers."""
import numpy as np
import pytest

from isar.geometry.tas_geometry import (
    stt_from_q_norm,
    solve_sample_angles,
    solve_instrument_angles,
)


def test_stt_from_q_norm_rejects_bad_sense():
    with pytest.raises(ValueError):
        stt_from_q_norm(1.5, 2.6634, 2.6634, sense_sample=0)


def test_solve_sample_angles_rejects_bad_sense():
    q = np.array([1.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        solve_sample_angles(q, 2.6634, 2.6634, sense_sample=0)


def test_solve_instrument_angles_rejects_bad_sense():
    q = np.array([1.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        solve_instrument_angles(q, 2.6634, 2.6634, sense_sample=0)


def test_valid_sense_values_still_work():
    assert stt_from_q_norm(1.5, 2.6634, 2.6634, sense_sample=-1) < 0
    assert stt_from_q_norm(1.5, 2.6634, 2.6634, sense_sample=1) > 0
