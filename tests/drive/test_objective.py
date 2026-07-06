"""ObjectiveSpec — the machine objective the supervisor produces and the loop consumes."""
import pytest

from isar.drive.objective import ObjectiveSpec


def _spec():
    return ObjectiveSpec(
        path=[(1.0, 0.0, 0.0), (1.5, 0.0, 0.0), (2.0, 0.0, 0.0)],
        tolerance_meV=0.1,
        budget={"kind": "counting_seconds", "amount": 28800.0},
        tier="deterministic",
        name="acoustic A->B",
        meta={"goal": "map the acoustic phonon"},
    )


def test_round_trip_dict():
    spec = _spec()
    d = spec.to_dict()
    back = ObjectiveSpec.from_dict(d)
    assert back.to_dict() == d
    assert back.path == spec.path
    assert back.tolerance_meV == spec.tolerance_meV
    assert back.budget == spec.budget
    assert back.tier == spec.tier
    assert back.name == spec.name
    assert back.meta == spec.meta


def test_path_normalized_to_float_triples():
    spec = ObjectiveSpec(path=[(1, 0, 0), [2, 0, 0]], tolerance_meV=0.1,
                         budget={"kind": "neutrons", "amount": 1e9})
    assert spec.path == [(1.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    assert all(isinstance(c, float) for p in spec.path for c in p)


def test_along_h_builds_the_expected_grid():
    spec = ObjectiveSpec.along_h(h_start=1.0, h_end=2.0, step=0.5, k=0.0, l=0.0,
                                 tolerance_meV=0.1, budget={"kind": "counting_seconds", "amount": 100})
    assert spec.path == [(1.0, 0.0, 0.0), (1.5, 0.0, 0.0), (2.0, 0.0, 0.0)]


def test_along_h_descending():
    spec = ObjectiveSpec.along_h(h_start=2.0, h_end=1.0, step=0.5, tolerance_meV=0.1,
                                 budget={"kind": "counting_seconds", "amount": 100})
    assert spec.path[0] == (2.0, 0.0, 0.0)
    assert spec.path[-1] == (1.0, 0.0, 0.0)


def test_rejects_bad_tier():
    with pytest.raises(ValueError):
        ObjectiveSpec(path=[(0, 0, 0)], tolerance_meV=0.1,
                      budget={"kind": "neutrons", "amount": 1}, tier="quantum")


def test_rejects_malformed_budget():
    with pytest.raises(ValueError):
        ObjectiveSpec(path=[(0, 0, 0)], tolerance_meV=0.1, budget={"amount": 1})


def test_along_h_rejects_nonpositive_step():
    with pytest.raises(ValueError):
        ObjectiveSpec.along_h(h_start=0.0, h_end=1.0, step=0.0, tolerance_meV=0.1,
                              budget={"kind": "neutrons", "amount": 1})
