"""Lattice-epoch resolver (TDD).

Pins the governing-epoch selection rules: empty list, single epoch, a mid-experiment epoch
change, a retroactive supersede, and scan_ref-vs-ts resolution, plus building a SampleState
(oriented from u/v) out of an epoch record.
"""
import numpy as np
import pytest

from isar.geometry.epochs import resolve_epoch, sample_state_from_epoch
from isar.geometry.tas_geometry import component_q_to_instrument_q


def _epoch(version, *, scan_ref=None, ts=None, supersedes=None,
           lattice=(4.0, 4.0, 4.0, 90.0, 90.0, 90.0), orient=None,
           method="manual", source="test"):
    a, b, c, al, be, ga = lattice
    return {
        "version": version,
        "created": f"2026-01-0{version}T00:00:00",
        "valid_from": {"scan_ref": scan_ref, "ts": ts},
        "supersedes": supersedes,
        "lattice": {"a": a, "b": b, "c": c, "alpha": al, "beta": be, "gamma": ga},
        "orient": orient,
        "method": method,
        "source": source,
    }


def test_empty_list_returns_none():
    assert resolve_epoch([]) is None


def test_single_epoch_governs_everything():
    e = _epoch(1, scan_ref="000000")
    assert resolve_epoch([e], scan_ref="055182") is e
    # A bare query (no locator) still resolves to the only epoch.
    assert resolve_epoch([e]) is e


def test_mid_experiment_change_picks_latest_at_or_before_scan():
    e1 = _epoch(1, scan_ref="000000")
    e2 = _epoch(2, scan_ref="055182")            # phase transition mid-run
    epochs = [e1, e2]
    # Before the change -> the original epoch governs.
    assert resolve_epoch(epochs, scan_ref="055100") is e1
    # Exactly at the boundary and after -> the new epoch governs.
    assert resolve_epoch(epochs, scan_ref="055182") is e2
    assert resolve_epoch(epochs, scan_ref="055300") is e2


def test_retroactive_supersede_skips_superseded_epoch():
    e1 = _epoch(1, scan_ref="000000", lattice=(4.0, 4.0, 4.0, 90, 90, 90))
    # Correction: same validity window, but supersedes v1 with a refined lattice.
    e3 = _epoch(3, scan_ref="000000", supersedes=1,
                lattice=(4.05, 4.05, 4.05, 90, 90, 90))
    got = resolve_epoch([e1, e3], scan_ref="055182")
    assert got is e3
    assert got["lattice"]["a"] == pytest.approx(4.05)


def test_ts_resolution_independent_of_scan_ref():
    e1 = _epoch(1, ts="2026-03-01T00:00:00")
    e2 = _epoch(2, ts="2026-03-05T12:00:00")
    epochs = [e1, e2]
    assert resolve_epoch(epochs, ts="2026-03-03T00:00:00") is e1
    assert resolve_epoch(epochs, ts="2026-03-06T00:00:00") is e2


def test_ts_aware_offsets_order_as_instants():
    # 2026-03-01T06:00:00+05:00 == 2026-03-01T01:00:00Z as instants. Strings do NOT order
    # lexicographically ("06..." > "01..."), so only instant-aware comparison is correct.
    e1 = _epoch(1, ts="2026-03-01T00:00:00+00:00")
    e2 = _epoch(2, ts="2026-03-01T06:00:00+05:00")   # == 01:00:00Z
    epochs = [e1, e2]
    # 00:30Z is after e1 (00:00Z) but before e2 (01:00Z) -> e1 governs.
    assert resolve_epoch(epochs, ts="2026-03-01T00:30:00Z") is e1
    # At/after e2's instant -> e2 governs (lexicographic string order would wrongly pick e1).
    assert resolve_epoch(epochs, ts="2026-03-01T01:00:00Z") is e2
    assert resolve_epoch(epochs, ts="2026-03-01T02:00:00Z") is e2


def test_ts_mixed_aware_and_naive_raises():
    e1 = _epoch(1, ts="2026-03-01T00:00:00")          # naive
    e2 = _epoch(2, ts="2026-03-05T12:00:00+00:00")    # aware
    with pytest.raises(ValueError):
        resolve_epoch([e1, e2], ts="2026-03-06T00:00:00Z")


def test_no_applicable_epoch_returns_none():
    # Only a bounded epoch that starts after the query -> nothing governs yet.
    e = _epoch(2, scan_ref="055182")
    assert resolve_epoch([e], scan_ref="055100") is None


def test_sample_state_from_epoch_unoriented():
    s = sample_state_from_epoch(_epoch(1, lattice=(3.9, 3.9, 6.2, 90, 90, 90)))
    assert s.lattice == (3.9, 3.9, 6.2, 90, 90, 90)
    assert s.oriented is False


def test_sample_state_from_epoch_oriented_from_uv():
    e = _epoch(1, orient={"u": [1, 0, 0], "v": [0, 0, 1]})
    s = sample_state_from_epoch(e)
    assert s.oriented is True
    # The u vector [1,0,0] lies in the horizontal scattering plane, so its Q has ~0
    # out-of-plane component. hkl_to_q returns component-frame Q; convert to the package's
    # instrument frame (+z out of plane) and assert the instrument-z component vanishes.
    q_inst = component_q_to_instrument_q(np.asarray(s.ub().hkl_to_q(1, 0, 0)))
    assert abs(q_inst[2]) < 1e-9  # instrument-frame out-of-plane (z) component
