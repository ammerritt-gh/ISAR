"""The planner's energy domain is the instrument's, never the phonon's (owner ruling 2026-09-05).

Above: the card's kinematic ceiling. Below: zero for an acoustic target, three elastic-resolution
FWHMs for an optic target. An acoustic branch rising past 9 meV, or an optic branch above 12 meV,
must be as measurable as the 6 meV demo phonon.
"""
from __future__ import annotations

import pytest

from isar.drive.loop import OPTIC_ELASTIC_EXCLUSION_FWHM, PlannerRefusal, _planner_energy_domain
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot

_PATH = ((2.0, 0.0, 0.0), (2.5, 0.0, 0.0), (3.0, 0.0, 0.0))


def _objective(branch: str, ceiling: float) -> ObjectiveSpec:
    frozen = shipped_hypothesis_snapshot(branch, (0.0, ceiling))
    return ObjectiveSpec.along_h(h_start=2.0, h_end=3.0, step=0.5, tolerance_meV=0.1,
                                 budget={"kind": "neutrons", "amount": 1e11},
                                 target_branch=branch, energy_search_meV=(0.0, ceiling), **frozen)


def _resolution(elastic_fwhm: float):
    def cb(h, k, l, deltaE):
        return {"fwhm_dE": elastic_fwhm if deltaE == 0.0 else 0.8}
    return cb


def test_an_acoustic_target_plans_from_zero_to_the_instrument_ceiling():
    assert _planner_energy_domain(_objective("acoustic", 35.0), _PATH, _resolution(0.3)) == (0.0, 35.0)


def test_an_optic_target_starts_three_elastic_widths_above_zero():
    lo, hi = _planner_energy_domain(_objective("optic", 20.0), _PATH, _resolution(0.3))
    assert lo == pytest.approx(OPTIC_ELASTIC_EXCLUSION_FWHM * 0.3) and hi == 20.0
    # The hull window that scanned the elastic line on 2026-09-05 started at -2 meV; no window can
    # now start below the exclusion, so the elastic line is outside the domain by construction.
    assert lo > 0.0


def test_an_empty_domain_is_refused_not_planned():
    with pytest.raises(PlannerRefusal, match="empty"):
        _planner_energy_domain(_objective("optic", 0.5), _PATH, _resolution(0.3))
