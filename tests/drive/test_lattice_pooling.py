"""A repeat scan must land on the grid it is pooling with.

The EIG planner's model is that a repeat adds counts to the pooled master ("detectability is a
property of the pooled master, not of any individual scan").  The reducer can only honour that for
members whose executed energies coincide, and before the lattice snap nothing enforced it: on the
2026-09-03 full-path campaign, h = 2.90 collected 65 members across ~60 windows differing by
hundredths of a meV at one step, which ``_merge_group`` binned into 1419 mutually exclusive bins.
``n_members`` read 65 while ``effective_exposure`` -- the peak-local MEDIAN of the summed monitor --
stayed at one scan's 1e7.

These two tests are the criterion, end to end: the realization layer emits a shared lattice, and a
master built from it reports the SUM of its members' exposure.
"""
import numpy as np
import pytest

from isar.analyze.context import effective_exposure
from isar.drive.acquisition.grid_adapters import TAVI_GRID_ADAPTER
from isar.drive.acquisition.scan_grid import resolve_point_count, snap_to_lattice
from isar.reduce import collate

# Three designs for the same grid point, as the planner emits them: one step, bounds a fraction of a
# step apart.  These are real windows from the h = 2.90 pool.
DESIGNS = [(2.24, 8.52, 0.19), (2.25, 8.53, 0.19), (2.801, 8.891, 0.19)]
MONITOR = 1e7


def _executed(design):
    e_lo, e_hi, e_step = snap_to_lattice(*design)
    n_points, step = resolve_point_count(e_lo, e_hi, e_step)
    return TAVI_GRID_ADAPTER.executed_energies(e_lo, step, n_points)


def _member(energies, scan_number):
    """One collatable spectrum ref on ``energies``, flat counts at a flat monitor."""
    values = np.asarray(energies, dtype=float)
    ones = np.ones_like(values)
    return collate.SpectrumRef(
        experiment_id="LAT", scan_number=scan_number, detector_id="det", scan_type="measurement",
        q=[2.9, 0.0, 0.0], q_key=(58, 0, 0), temperature=300.0, t_key=300.0,
        spectrum=type("Sp", (), {"intensity": 4.0 * ones, "mask": ones.astype(bool)})(),
        energy=values,
        normalization=type("Norm", (), {"monitor": MONITOR * ones})())


def test_designs_offset_by_a_fraction_of_a_step_emit_one_shared_lattice():
    grids = [_executed(design) for design in DESIGNS]
    # Every executed point of every design is a point of the common lattice: same step, and bounds
    # that are whole multiples of it.  Without the snap these three share almost no point at all.
    assert {round(g[1] - g[0], 6) for g in grids} == {0.19}
    for grid in grids:
        for energy in grid:
            assert abs(energy / 0.19 - round(energy / 0.19)) < 1e-6, energy
    overlap = set(grids[0]) & set(grids[1]) & set(grids[2])
    assert len(overlap) >= min(len(g) for g in grids) - 2


def test_a_master_of_snapped_repeats_reports_the_summed_exposure():
    members = [_member(_executed(design), i + 1) for i, design in enumerate(DESIGNS)]
    merged = collate._merge_group(members, consistency_bootstrap_samples=8)

    assert merged.n_members == len(DESIGNS)
    # Pooling is real, not nominal: the shared band carries every member's monitor, so the
    # peak-local exposure the planner reads is the SUM, not one member's.
    exposure = effective_exposure(merged.energy, merged.monitor, merged.mask,
                                  target_center=5.0, resolution_fwhm=0.8)
    assert exposure["source"] == "target_window"
    assert exposure["value"] == pytest.approx(len(DESIGNS) * MONITOR)


def test_without_the_snap_the_same_designs_pool_into_nothing():
    """The defect this slice fixes, pinned so it cannot come back unnoticed."""
    raw = []
    for i, (e_lo, e_hi, e_step) in enumerate(DESIGNS):
        n_points, step = resolve_point_count(e_lo, e_hi, e_step)
        raw.append(_member(TAVI_GRID_ADAPTER.executed_energies(e_lo, step, n_points), i + 1))
    merged = collate._merge_group(raw, consistency_bootstrap_samples=8)

    assert merged.n_members == len(DESIGNS)
    exposure = effective_exposure(merged.energy, merged.monitor, merged.mask,
                                  target_center=5.0, resolution_fwhm=0.8)
    # Three members, one member's worth of exposure: the pool the planner was topping up.
    assert exposure["value"] == pytest.approx(MONITOR)
