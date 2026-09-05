"""Cross-repo parity: the grid the adapter predicts is the grid the instrument executes.

ISAR scores a scan design over the points it believes the instrument will visit.  If that belief is
wrong the error is silent — a scored grid and an executed grid are equally plausible in isolation, and
nothing downstream ever compares them.  Before the adapter seam, ISAR canonicalized energies at six
decimals while TAVI executes ``np.round(np.linspace(...), 3)``; a resolution-derived step such as
0.0925 meV made the two disagree on almost every point.

The golden vectors below were hand-derived from TAVI's source semantics
(``tavi/utilities.py:91-118``, ``parse_scan_steps``) — deliberately **not** by importing TAVI, so the
drive layer keeps no runtime dependency on the instrument server and a TAVI-side change to those
semantics fails here loudly instead of being absorbed. The one live test round-trips a real scan.
"""
from __future__ import annotations

import math
import os
import random

import pytest

from isar.drive.acquisition.grid_adapters import (
    DEFAULT_GRID_ADAPTER,
    GRID_ADAPTER_SCHEMA,
    TAVI_GRID_ADAPTER,
    TaviUniformGridAdapter,
)
from isar.drive.acquisition.scan_grid import resolve_point_count, resolve_uniform_grid


# (name, (e_lo, e_hi, e_step), expected scan command, expected executed energies)
#
# Derivation for every row, applied by hand from TAVI's source:
#   start, end, step = float() of the %g-rendered command tokens
#   n = int(np.floor((end - start) / step + 0.5)) + 1
#   last = start + step * (n - 1)
#   values = np.round(np.linspace(start, last, n), 3)     # numpy round-half-to-EVEN
GOLDEN_EXECUTED_GRIDS = [
    (
        # Resolution-derived non-round step (0.25 × 0.37 meV FWHM). TAVI's 3-decimal rounding is
        # lossy here and ISAR's old 6-decimal canonicalization was not: 0.5 + 0.0925 = 0.5925 was
        # scored at 0.5925 and executed at 0.592 (round-half-to-even).
        "resolution_derived_step_0_0925",
        (0.5, 3.0, 0.0925),
        "deltaE 0.5 3 0.0925",
        [0.5, 0.592, 0.685, 0.778, 0.87, 0.962, 1.055, 1.148, 1.24, 1.332, 1.425, 1.518,
         1.61, 1.702, 1.795, 1.888, 1.98, 2.072, 2.165, 2.258, 2.35, 2.442, 2.535, 2.628,
         2.72, 2.812, 2.905, 2.998],
    ),
    (
        # Point-cap-coarsened step: 9 meV across the 100-point cap. The %g wire rendering truncates
        # 0.0909090909… to 0.0909091, and the count must still come out at exactly 100.
        "point_cap_coarsened_step",
        (-2.0, 7.0, 9.0 / 99.0),
        "deltaE -2 7 0.0909091",
        [-2.0, -1.909, -1.818, -1.727, -1.636, -1.545, -1.455, -1.364, -1.273, -1.182,
         -1.091, -1.0, -0.909, -0.818, -0.727, -0.636, -0.545, -0.455, -0.364, -0.273,
         -0.182, -0.091, 0.0, 0.091, 0.182, 0.273, 0.364, 0.455, 0.545, 0.636, 0.727,
         0.818, 0.909, 1.0, 1.091, 1.182, 1.273, 1.364, 1.455, 1.545, 1.636, 1.727, 1.818,
         1.909, 2.0, 2.091, 2.182, 2.273, 2.364, 2.455, 2.545, 2.636, 2.727, 2.818, 2.909,
         3.0, 3.091, 3.182, 3.273, 3.364, 3.455, 3.545, 3.636, 3.727, 3.818, 3.909, 4.0,
         4.091, 4.182, 4.273, 4.364, 4.455, 4.545, 4.636, 4.727, 4.818, 4.909, 5.0, 5.091,
         5.182, 5.273, 5.364, 5.455, 5.545, 5.636, 5.727, 5.818, 5.909, 6.0, 6.091, 6.182,
         6.273, 6.364, 6.455, 6.545, 6.636, 6.727, 6.818, 6.909, 7.0],
    ),
    (
        # A span crossing zero: the anti-Stokes side is where a sign error would show up, and the
        # point at the origin comes out of linspace as -0.0.
        "negative_and_positive_endpoints",
        (-2.0, 6.6, 0.2),
        "deltaE -2 6.6 0.2",
        [-2.0, -1.8, -1.6, -1.4, -1.2, -1.0, -0.8, -0.6, -0.4, -0.2, 0.0, 0.2, 0.4, 0.6,
         0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.4, 2.6, 2.8, 3.0, 3.2, 3.4, 3.6, 3.8,
         4.0, 4.2, 4.4, 4.6, 4.8, 5.0, 5.2, 5.4, 5.6, 5.8, 6.0, 6.2, 6.4, 6.6],
    ),
    (
        # Nearest-integer rounding UP: 1.0/0.4 = 2.5 → 4 points, whose last point (1.2) lands
        # ABOVE the declared upper bound. The instrument really does go there.
        "final_point_above_declared_bound",
        (0.0, 1.0, 0.4),
        "deltaE 0 1 0.4",
        [0.0, 0.4, 0.8, 1.2],
    ),
    (
        # Nearest-integer rounding DOWN: 1.0/0.3 = 3.33 → 4 points ending at 0.9, below the bound.
        "final_point_below_declared_bound",
        (0.0, 1.0, 0.3),
        "deltaE 0 1 0.3",
        [0.0, 0.3, 0.6, 0.9],
    ),
    (
        # Bounds that only reach the instrument through their %g rendering: a designed 3.3999…95
        # is submitted (and therefore executed) as 3.4.
        "wire_rendering_round_trip",
        (3.3999999999999995, 6.6000000000000005, 0.2000000000000001),
        "deltaE 3.4 6.6 0.2",
        [3.4, 3.6, 3.8, 4.0, 4.2, 4.4, 4.6, 4.8, 5.0, 5.2, 5.4, 5.6, 5.8, 6.0, 6.2, 6.4, 6.6],
    ),
    (
        # Banker's rounding: 0.0625 and 0.3125 are exact binary halves at the third decimal.
        # numpy rounds them to the EVEN neighbour (0.062, 0.312); a half-up implementation would
        # produce 0.063 and 0.313 and disagree with the instrument on two of nine points.
        "bankers_rounding_ties",
        (0.0, 0.5, 0.0625),
        "deltaE 0 0.5 0.0625",
        [0.0, 0.062, 0.125, 0.188, 0.25, 0.312, 0.375, 0.438, 0.5],
    ),
]


@pytest.mark.parametrize(
    "name,window,command,expected",
    GOLDEN_EXECUTED_GRIDS,
    ids=[row[0] for row in GOLDEN_EXECUTED_GRIDS],
)
def test_adapter_reproduces_tavis_executed_grid(name, window, command, expected):
    e_lo, e_hi, e_step = window
    adapter = TAVI_GRID_ADAPTER

    assert adapter.format_scan_command(e_lo, e_hi, e_step) == command
    assert adapter.point_count(e_lo, e_hi, e_step) == len(expected)
    energies = adapter.executed_energies(e_lo, e_step, len(expected))
    assert list(energies) == expected


def test_bankers_ties_are_not_half_up():
    # Guards the specific reimplementation mistake: rounding half away from zero.
    energies = TAVI_GRID_ADAPTER.executed_energies(0.0, 0.0625, 9)
    half_up = tuple(math.floor(0.0625 * index * 1000 + 0.5) / 1000 for index in range(9))
    assert energies != half_up
    assert energies[1] == 0.062 and half_up[1] == 0.063


def test_adapter_declares_a_versioned_identity():
    identity = TAVI_GRID_ADAPTER.identity()
    assert identity == {
        "adapter_schema": GRID_ADAPTER_SCHEMA,
        "adapter_id": "tavi_delta_e",
        "adapter_version": 1,
    }
    assert DEFAULT_GRID_ADAPTER is TAVI_GRID_ADAPTER
    # A different adapter is a different measurement contract, and the identity must say so.
    other = TaviUniformGridAdapter(adapter_id="other", adapter_version=1)
    assert other.identity() != TAVI_GRID_ADAPTER.identity()


def test_returned_grid_canonicalization_accepts_only_serialization_noise():
    assert TAVI_GRID_ADAPTER.canonicalize_returned_energies(
        [0.30000000000000004, -0.0, 1.25]) == (0.3, 0.0, 1.25)
    with pytest.raises(ValueError, match="outside adapter precision"):
        TAVI_GRID_ADAPTER.canonicalize_returned_energies([0.3004])


def test_resolved_grid_point_count_is_the_adapters_and_respects_the_cap():
    # The resolver's count is the instrument's answer for the command ISAR would submit — including
    # after the cap coarsens the step, where the %g rendering of span/(cap-1) must not smuggle in an
    # extra point.
    for e_lo, e_hi, e_step, cap in (
            (-2.0, 7.0, 0.0925, 100), (-2.0, 7.0, 0.01, 100), (0.5, 3.0, 0.0925, 40),
            (-7.0, 6.2, 0.2, 100), (-2.0, 6.6, 0.001, 12), (3.4, 6.6, 0.2, 100)):
        grid = resolve_uniform_grid(e_lo, e_hi, e_step, max_points=cap)
        assert grid.n_points <= cap
        assert grid.n_points == DEFAULT_GRID_ADAPTER.point_count(
            grid.e_lo, grid.e_hi, grid.e_step)
        assert len(grid.energies) == grid.n_points


def test_the_budget_ledger_and_the_resolver_count_degenerate_windows_identically():
    # Two counting rules would let an unbudgeted request and the grid it resolves to disagree about
    # how many points the instrument executes. Both go through resolve_point_count, including for a
    # non-positive step and an inverted window.
    for e_lo, e_hi, e_step in ((-2.0, 7.0, 0.2), (5.0, 5.0, 0.2), (7.0, -2.0, 0.2),
                               (-2.0, 7.0, 0.0), (-2.0, 7.0, -1.0)):
        count, step = resolve_point_count(e_lo, e_hi, e_step)
        assert step > 0.0
        assert count >= 1
        if count <= 1000:
            assert resolve_uniform_grid(
                e_lo, e_hi, e_step, max_points=1000).n_points == count


# ---- live round trip -----------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ISAR_LIVE_TAVI"),
                    reason="set ISAR_LIVE_TAVI (and run TAVI at 127.0.0.1:8642) to enable")
def test_live_adapter_prediction_equals_the_executed_scan_values():
    """The whole point of the golden fixtures, checked against the real instrument once."""
    from isar.drive.instrument.tavi_client import TaviClient

    e_lo, e_hi, e_step = 0.5, 3.0, 0.0925
    command = DEFAULT_GRID_ADAPTER.format_scan_command(e_lo, e_hi, e_step)
    predicted = DEFAULT_GRID_ADAPTER.executed_energies(
        e_lo, e_step, DEFAULT_GRID_ADAPTER.point_count(e_lo, e_hi, e_step))

    client = TaviClient()
    schema = client.get_schema()
    if "deterministic" not in schema.get("engines", []):
        pytest.skip("TAVI does not advertise the deterministic engine")

    submit = client.submit_scan({
        "parameters": {"scan_command1": command, "number_neutrons": 1000},
        "engine": "deterministic", "seed": 1, "isolated": True,
    })
    snapshot = client.wait_scan(submit["job_id"], timeout=180.0, poll_wait=5.0)
    if snapshot.get("state") != "done":
        pytest.skip(f"scan terminated {snapshot.get('state')!r}: {snapshot.get('error')}")

    executed = (client.get_scan_data(submit["job_id"])["result"] or {}).get("scan_values_1")
    assert executed is not None, "TAVI returned no scan_values_1"
    assert [float(value) for value in executed] == list(predicted)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ISAR_LIVE_TAVI"),
                    reason="set ISAR_LIVE_TAVI (and run TAVI at 127.0.0.1:8642) to enable")
def test_live_tavi_refuses_a_single_point_request():
    """The external fact ``degenerate_window`` encodes, checked against the real instrument.

    The refusal in ``alternative_window`` exists because TAVI will not run a one-point scan. If that
    ever stops being true the refusal is over-strict and this says so, loudly, rather than leaving a
    family suppressed for a reason that expired.
    """
    from isar.drive.instrument.tavi_client import TaviClient

    client = TaviClient()
    # deltaE 6.9 7 0.25 — exactly what broad_recovery/1 resolves to on a (6.9, 7.0) domain.
    validation = client.validate({"parameters": {"scan_command1": "deltaE 6.9 7 0.25",
                                                 "number_neutrons": 1000}})
    assert validation.get("requested_points") == 1
    assert validation.get("would_queue") is False
    assert any("1 scan point" in str(blocker) for blocker in (validation.get("blockers") or ()))


@pytest.mark.parametrize("point_cap", [2, 3, 5, 100, 200, 500, 5_000, 100_000])
def test_coarsening_to_the_point_cap_never_exceeds_it(point_cap):
    """The cap must hold for the cap it is handed, not the cap it usually gets.

    The coarsened step has to be derived from the span the adapter counts over -- the wire span,
    after the ``%g`` round trip -- not the raw designed span. Dividing the raw span yielded a step
    fractionally too small whenever the rendering moved a bound, and the adapter then answered
    ``point_cap + 1``: an over-limit scan request that every fingerprint downstream would have
    recorded as legal.
    """
    rng = random.Random(4242)
    for _ in range(4000):
        e_lo = rng.uniform(-200.0, 200.0)
        e_hi = e_lo + rng.uniform(1e-4, 400.0)
        # A step far below the cap's implied resolution, so coarsening always engages.
        e_step = rng.uniform(1e-9, 1e-4)
        grid = resolve_uniform_grid(e_lo, e_hi, e_step, max_points=point_cap)
        assert grid.n_points <= point_cap, (e_lo, e_hi, e_step, point_cap, grid.n_points)
        # The count stays the instrument's answer rather than a clamp (ADR-018).
        assert grid.n_points == DEFAULT_GRID_ADAPTER.point_count(
            grid.e_lo, max(grid.e_hi, grid.e_lo), grid.e_step)
