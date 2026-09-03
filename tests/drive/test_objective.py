"""ObjectiveSpec — the machine objective the supervisor produces and the loop consumes."""
import pytest

from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.schema.hypotheses import SteeringPolicy, content_fingerprint


def test_dual_clock_defaults_are_tier_specific():
    deterministic = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.2, budget=None)
    assert deterministic.budget is None
    assert deterministic.analysis_limit_seconds == 3600.0

    measurement_dominated = ObjectiveSpec(
        path=[(2.0, 0.0, 0.0)], tolerance_meV=0.2, budget=None, tier="mcstas")
    assert measurement_dominated.budget == {
        "kind": "counting_seconds", "amount": 8.0 * 3600.0}
    assert measurement_dominated.analysis_limit_seconds is None


def test_explicit_analysis_limit_none_survives_round_trip():
    spec = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.2, budget=None,
                         analysis_limit_seconds=None)
    assert ObjectiveSpec.from_dict(spec.to_dict()).analysis_limit_seconds is None


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


def test_persisted_objective_requires_the_targeted_step_schema():
    old = _spec().to_dict()
    old.pop("schema")
    with pytest.raises(ValueError, match="isar.campaign-step.targeted/1"):
        ObjectiveSpec.from_dict(old)


def test_a_retired_objective_schema_is_refused_by_name_not_adapted():
    # H1: isar.objective/2 is unreadable by design. The refusal has to say which contract replaced
    # it, or the only signal an operator gets is "schema mismatch" on a file that used to work.
    old = _spec().to_dict()
    old["schema"] = "isar.objective/2"
    with pytest.raises(ValueError, match="was retired by 'isar.campaign-program/1'"):
        ObjectiveSpec.from_dict(old)


def test_persisted_objective_rejects_malformed_q_with_clear_error():
    malformed = _spec().to_dict()
    malformed["path"] = [[1.0, 0.0]]
    with pytest.raises(ValueError, match="exactly three coordinates"):
        ObjectiveSpec.from_dict(malformed)


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


@pytest.mark.parametrize("start,end,step", [
    (1.0, 2.0, 0.3),       # non-commensurate: legacy round intentionally misses the endpoint
    (2.0, 1.0, 0.3),
    (2.0, 2.0, 0.05),
    (-0.2, 0.5, 0.2),
])
def test_along_h_path_is_equivalent_to_legacy_round_expansion(start, end, step):
    span = end - start
    n = int(round(abs(span) / step))
    sign = 1.0 if span >= 0 else -1.0
    expected = [(round(start + sign * step * i, 9), 0.1, -0.2) for i in range(n + 1)]
    spec = ObjectiveSpec.along_h(h_start=start, h_end=end, step=step, k=0.1, l=-0.2,
                                 tolerance_meV=0.1,
                                 budget={"kind": "counting_seconds", "amount": 100})
    assert spec.path == expected


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


def test_generic_segment_uses_step_as_a_maximum_and_includes_endpoints():
    spec = ObjectiveSpec.segment(q_start=[2, 0, 0], q_end=[2, 0.5, 0], step_rlu=0.05,
                                 tolerance_meV=0.5,
                                 budget={"kind": "counting_seconds", "amount": 8 * 3600},
                                 target_branch="optic", target_label="transverse optic")
    assert len(spec.path) == 11
    assert spec.path[0] == (2.0, 0.0, 0.0)
    assert spec.path[-1] == (2.0, 0.5, 0.0)
    assert max(spec.path[i + 1][1] - spec.path[i][1] for i in range(10)) <= 0.05 + 1e-12
    assert spec.energy_search_meV == (-2.0, 12.0)
    assert spec.target_selector == "tracked_branch"


def test_the_declared_branch_is_the_only_target_the_planner_can_map():
    """The planner's prize is defined per declared branch, so a per-q feature selector names a
    prize it can never satisfy — and a campaign carrying one runs until its budget dies."""
    common = dict(path=[(2, 0, 0)], tolerance_meV=0.5,
                  budget={"kind": "neutrons", "amount": 1}, target_branch="optic")
    assert ObjectiveSpec(**common).target_selector == "tracked_branch"
    tracked = ObjectiveSpec(**common, target_selector="tracked_branch")
    assert tracked.target_selector == "tracked_branch"
    acoustic = ObjectiveSpec(path=[(2.0, 0.0, 0.0)], tolerance_meV=0.5,
                             budget={"kind": "neutrons", "amount": 1e6},
                             target_branch="acoustic", target_selector="tracked_branch")
    assert acoustic.target_selector == "tracked_branch"
    for retired in ("nearest_energy", "lowest_energy", "highest_energy", "brightest"):
        with pytest.raises(ValueError, match="target_branch"):
            ObjectiveSpec(**common, target_selector=retired)


def test_explicit_degenerate_segment_remains_a_valid_single_point():
    spec = ObjectiveSpec.segment(q_start=[2, 0, 0], q_end=[2, 0, 0], step_rlu=0.05,
                                 tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1})
    assert spec.path == [(2.0, 0.0, 0.0)]


def test_old_style_objective_has_no_implicit_null_hypothesis():
    spec = _spec()
    assert spec.hypotheses == []
    assert spec.target_hypothesis is None
    assert spec.steering == {}


def test_frozen_hypothesis_snapshot_round_trips_and_detects_tampering():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    spec = ObjectiveSpec(path=[(2, 0, 0)], tolerance_meV=0.5,
                         budget={"kind": "neutrons", "amount": 1},
                         target_branch="optic", **frozen)
    assert ObjectiveSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()
    tampered = spec.to_dict()
    tampered["hypotheses"][0]["confidence"] = 0.01
    with pytest.raises(ValueError, match="fingerprint"):
        ObjectiveSpec.from_dict(tampered)


def test_new_snapshots_enable_dynamic_counting_but_legacy_fingerprint_stays_exact():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    assert frozen["steering"]["dynamic_ncount"] is True

    old_policy = SteeringPolicy().to_dict()
    for key in ("dynamic_ncount", "max_ncount_per_scan", "ncount_growth_cap",
                "tolerance_margin", "interval_floor_window",
                "interval_floor_min_improvement", "interval_floor_min_exposure_ratio",
                ):
        old_policy.pop(key)
    old_fingerprint = content_fingerprint(frozen["hypotheses"], old_policy)
    legacy = ObjectiveSpec(
        path=[(2, 0, 0)], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1}, target_branch="optic",
        hypotheses=frozen["hypotheses"], target_hypothesis="optic",
        hypothesis_source="shipped_preset", hypothesis_fingerprint=old_fingerprint,
        steering=old_policy)
    assert legacy.hypothesis_fingerprint == old_fingerprint
    assert legacy.steering == old_policy


def test_pre_bridge_snapshot_with_dynamic_counting_keeps_exact_fingerprint():
    frozen = shipped_hypothesis_snapshot("optic", (-2.0, 12.0))
    old_policy = dict(frozen["steering"])
    old_fingerprint = content_fingerprint(frozen["hypotheses"], old_policy)
    legacy = ObjectiveSpec(
        path=[(2, 0, 0)], tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1}, target_branch="optic",
        hypotheses=frozen["hypotheses"], target_hypothesis="optic",
        hypothesis_source="shipped_preset", hypothesis_fingerprint=old_fingerprint,
        steering=old_policy)
    assert legacy.hypothesis_fingerprint == old_fingerprint
    assert legacy.steering == old_policy
    assert ObjectiveSpec.from_dict(legacy.to_dict()).hypothesis_fingerprint == old_fingerprint


@pytest.mark.parametrize("names, message", [
    (["optic", "optic"], "duplicate"), (["optic", ""], "non-empty"),
])
def test_frozen_hypothesis_names_must_be_nonempty_and_unique(names, message):
    hypotheses = [{"name": name} for name in names]
    with pytest.raises(ValueError, match=message):
        ObjectiveSpec(path=[(2, 0, 0)], tolerance_meV=0.5,
                      budget={"kind": "neutrons", "amount": 1}, target_branch="optic",
                      hypotheses=hypotheses, target_hypothesis="optic")
