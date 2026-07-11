"""SampleState.ub() cache invalidation on lattice mutation."""
import math

import numpy as np
import pytest

from isar.geometry.sample_state import SampleState
from isar.geometry.plane import set_orientation_from_plane


def test_ub_cache_rebuilds_after_lattice_mutation():
    s = SampleState(4.0, 4.0, 4.0, 90.0, 90.0, 90.0)
    q_before = np.asarray(s.ub().hkl_to_q(1, 0, 0))
    s.a = 5.0  # mutate a lattice field after the cache was built
    q_after = np.asarray(s.ub().hkl_to_q(1, 0, 0))
    # |Q(100)| = 2*pi/a must track the new lattice, not the stale cached B.
    assert np.linalg.norm(q_before) == pytest.approx(2 * math.pi / 4.0)
    assert np.linalg.norm(q_after) == pytest.approx(2 * math.pi / 5.0)


def test_oriented_sample_preserves_U_across_lattice_mutation():
    s = SampleState(4.0, 4.0, 6.0, 90.0, 90.0, 90.0)
    set_orientation_from_plane(s, [1, 0, 0], [0, 0, 1])
    assert s.oriented is True
    u_before = s.ub().U.copy()
    assert not np.allclose(u_before, np.eye(3))  # a non-trivial mount rotation was set

    s.a = 4.2  # change the lattice; U (mount rotation) is lattice-independent
    assert s.oriented is True
    u_after = s.ub().U
    assert np.allclose(u_before, u_after)
    # B still reflects the new lattice: |Q(100)| = 2*pi/4.2 (rotation-invariant magnitude).
    q = np.asarray(s.ub().hkl_to_q(1, 0, 0))
    assert np.linalg.norm(q) == pytest.approx(2 * math.pi / 4.2)
