"""Lattice-length validation for the reciprocal-basis builder."""
import math

import pytest

from isar.geometry.sample_mount import reciprocal_basis_tas


def test_zero_length_raises():
    with pytest.raises(ValueError):
        reciprocal_basis_tas(0.0, 4.0, 4.0, 90.0, 90.0, 90.0)


def test_infinite_length_raises():
    with pytest.raises(ValueError):
        reciprocal_basis_tas(math.inf, 4.0, 4.0, 90.0, 90.0, 90.0)


def test_valid_lengths_ok():
    basis = reciprocal_basis_tas(4.0, 4.0, 4.0, 90.0, 90.0, 90.0)
    assert basis.shape == (3, 3)
