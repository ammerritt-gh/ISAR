"""Reciprocal-space forward/inverse guards for degenerate cells."""
import pytest

from isar.geometry.reciprocal_space import (
    update_Q_from_HKL_direct,
    update_HKL_from_Q_direct,
)


def test_forward_rejects_degenerate_cell():
    # alpha=beta=gamma=170 deg is a geometrically impossible cell: the volume radicand goes
    # negative (V undefined). The forward function must raise, matching the inverse function.
    with pytest.raises(ValueError):
        update_Q_from_HKL_direct(1, 0, 0, 4.0, 4.0, 4.0, 170.0, 170.0, 170.0)


def test_inverse_rejects_degenerate_cell():
    with pytest.raises(ValueError):
        update_HKL_from_Q_direct(1.0, 0.0, 0.0, 4.0, 4.0, 4.0, 170.0, 170.0, 170.0)


def test_forward_valid_cell_ok():
    qx, qy, qz = update_Q_from_HKL_direct(1, 0, 0, 4.0, 4.0, 4.0, 90.0, 90.0, 90.0)
    assert qx == pytest.approx(2 * 3.141592653589793 / 4.0)
