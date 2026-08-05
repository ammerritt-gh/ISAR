"""goal→ObjectiveSpec: schema validation with a FAKE dispatcher, plus the LLM-free JSON path."""
import json

import pytest

from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor import goal_to_spec as g2s
from isar.reduce.llm_client import DispatchResult


class _FakeDispatcher:
    """A scripted Dispatcher — returns a queued sequence of DispatchResults, records the calls."""
    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return self._results.pop(0) if self._results else DispatchResult(error="empty")


_GOOD = {"path": {"h_start": 2.1, "h_end": 2.4, "k": 0.0, "l": 0.0},
         "step_h": 0.1, "tolerance_meV": 0.5, "budget_hours": 8.0, "notes": "acoustic branch"}


def test_structured_output_builds_a_spec():
    disp = _FakeDispatcher(DispatchResult(structured=_GOOD))
    spec = g2s.goal_to_spec("map the acoustic phonon 2.1->2.4, 0.5 meV, overnight", dispatcher=disp)
    assert isinstance(spec, ObjectiveSpec)
    assert spec.path == [(2.1, 0.0, 0.0), (2.2, 0.0, 0.0), (2.3, 0.0, 0.0), (2.4, 0.0, 0.0)]
    assert spec.tolerance_meV == 0.5
    assert spec.budget == {"kind": "counting_seconds", "amount": 8 * 3600.0}


def test_budget_neutrons_mapping():
    payload = dict(_GOOD); payload.pop("budget_hours"); payload["budget_neutrons"] = 1e10
    spec = g2s.goal_to_spec("x", dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    assert spec.budget == {"kind": "neutrons", "amount": 1e10}


def test_absent_budget_takes_the_canonical_tier_default():
    # The single source of truth is the constant table; the test reads it rather than restating a
    # number, so a deliberate change to the default cannot be mistaken for a regression here.
    payload = dict(_GOOD); payload.pop("budget_hours")
    for tier, hours in g2s.DEFAULT_BUDGET_HOURS_BY_TIER.items():
        spec = g2s.spec_from_dict(payload, defaults={"tier": tier})
        assert spec.budget == {"kind": "counting_seconds", "amount": hours * 3600.0}
    assert g2s.DEFAULT_BUDGET_HOURS_BY_TIER["deterministic"] == 1.0
    assert g2s.DEFAULT_BUDGET_HOURS_BY_TIER["mcstas"] == 8.0


def test_no_neutron_budget_by_default():
    # None means unlimited: an undeclared neutron budget never becomes a neutron-kind budget.
    assert g2s.DEFAULT_BUDGET_NEUTRONS is None
    payload = dict(_GOOD); payload.pop("budget_hours")
    spec = g2s.spec_from_dict(payload, defaults={"tier": "deterministic"})
    assert spec.budget["kind"] == "counting_seconds"


def test_analysis_limit_is_clamped_to_the_counting_time_budget():
    payload = {**_GOOD, "budget_hours": 0.25}       # 900 s, shorter than the 3600 s analysis default
    spec = g2s.spec_from_dict(payload, defaults={"tier": "deterministic"})
    assert spec.analysis_limit_seconds == 900.0
    # A limit already inside the budget is left exactly as declared.
    inside = g2s.spec_from_dict({**_GOOD, "budget_hours": 2.0, "analysis_limit_seconds": 1200.0},
                                defaults={"tier": "deterministic"})
    assert inside.analysis_limit_seconds == 1200.0


def test_recovers_from_fenced_text_output():
    fenced = "```json\n" + json.dumps(_GOOD) + "\n```"
    disp = _FakeDispatcher(DispatchResult(structured=None, output=fenced))
    spec = g2s.goal_to_spec("x", dispatcher=disp)
    assert spec.tolerance_meV == 0.5


def test_one_repair_retry_then_success():
    disp = _FakeDispatcher(DispatchResult(structured=None, output="sorry, no idea"),
                           DispatchResult(structured=_GOOD))
    spec = g2s.goal_to_spec("x", dispatcher=disp)
    assert spec.tolerance_meV == 0.5
    assert len(disp.calls) == 2                              # initial + one repair
    assert g2s._REPAIR_SUFFIX in disp.calls[1]["user"]


def test_refuses_honestly_on_persistent_nonconforming():
    disp = _FakeDispatcher(DispatchResult(structured={"path": {"h_start": 1}}),   # missing required
                           DispatchResult(structured=None, output="still nope"))
    with pytest.raises(g2s.GoalParseError):
        g2s.goal_to_spec("x", dispatcher=disp)


def test_refuses_on_llmm_unreachable():
    disp = _FakeDispatcher(DispatchResult(error="transport_error: connection refused"),
                           DispatchResult(error="transport_error: connection refused"))
    with pytest.raises(g2s.GoalParseError) as e:
        g2s.goal_to_spec("x", dispatcher=disp)
    assert "transport_error" in str(e.value)


# --------------------------------------------------------------------------- LLM-free path
def test_spec_from_json_goal_schema(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_GOOD), encoding="utf-8")
    spec = g2s.spec_from_json(p, defaults={"tier": "deterministic"})
    assert len(spec.path) == 4 and spec.tolerance_meV == 0.5


def test_spec_from_json_full_objective(tmp_path):
    full = ObjectiveSpec.along_h(h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.3,
                                 budget={"kind": "neutrons", "amount": 1e9}).to_dict()
    p = tmp_path / "obj.json"
    p.write_text(json.dumps(full), encoding="utf-8")
    spec = g2s.spec_from_json(p)
    assert spec.tolerance_meV == 0.3 and spec.budget["kind"] == "neutrons"


def test_spec_from_json_rejects_garbage(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"nonsense": 1}), encoding="utf-8")
    with pytest.raises(g2s.GoalParseError):
        g2s.spec_from_json(p)


def test_transverse_optic_k_segment_compiles_to_eleven_points():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8,
               "target_branch": "optic", "target_label": "transverse optic"}
    spec = g2s.goal_to_spec(
        "map the transverse optic phonon from (2,0,0) along k, out to k=0.5",
        dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    assert len(spec.path) == 11 and spec.path[-1] == (2.0, 0.5, 0.0)
    assert spec.target_branch == "optic" and spec.target_label == "transverse optic"
    assert spec.energy_search_meV == (-2.0, 12.0)
    assert spec.target_selector == "tracked_branch" and spec.target_energy_meV is None


def test_acoustic_goal_without_a_range_gets_the_canonical_symmetric_band():
    payload = {"path": {"h_start": 2.0, "h_end": 3.0, "k": 0.0, "l": 0.0}, "step_h": 0.05,
               "tolerance_meV": 0.5, "target_branch": "acoustic"}
    spec = g2s.goal_to_spec(
        "map the acoustic phonon from (2,0,0) to (3,0,0)",
        dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    assert spec.energy_search_meV == g2s.DEFAULT_ENERGY_SEARCH_ACOUSTIC_MEV == (-9.0, 9.0)


def test_supplied_target_energy_implies_nearest_selector():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_energy_meV": 7.5}
    spec = g2s.goal_to_spec("measure the optic phonon near 7.5 meV",
                            dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    assert spec.target_selector == "nearest_energy" and spec.target_energy_meV == 7.5


def test_optic_energy_identity_compiles_as_zone_center_tracked_branch():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "tracked_branch", "target_zc_energy_meV": 6.0}
    spec = g2s.goal_to_spec(
        "map the optic phonon around 6 meV",
        dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    optic = next(h for h in spec.hypotheses if h["name"] == "optic")
    assert spec.target_selector == "tracked_branch"
    assert spec.target_energy_meV is None
    assert optic["zc_energy"]["mean"] == 6.0


def test_zone_center_energy_implies_tracked_branch_when_selector_is_omitted():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_zc_energy_meV": 6.0}
    spec = g2s.spec_from_dict(payload)
    assert spec.target_selector == "tracked_branch"
    assert spec.target_energy_meV is None


def test_tracked_branch_promotes_target_energy_alias_without_global_selector():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "tracked_branch", "target_energy_meV": 6.0}
    spec = g2s.spec_from_dict(payload)
    optic = next(h for h in spec.hypotheses if h["name"] == "optic")
    assert spec.target_energy_meV is None
    assert optic["zc_energy"]["mean"] == 6.0


def test_explicit_nearest_energy_throughout_remains_global_nearest():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "nearest_energy", "target_energy_meV": 6.0}
    spec = g2s.goal_to_spec(
        "at every q, measure the feature nearest 6 meV",
        dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    assert spec.target_selector == "nearest_energy"
    assert spec.target_energy_meV == 6.0


def test_file_schema_accepts_operator_steering_but_llm_schema_does_not():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8,
               "steering": {"quicklook_ncount": 50_000_000}}
    assert g2s._conforms(payload, g2s.FILE_SPEC_SCHEMA)
    assert not g2s._conforms(payload, g2s.LLM_GOAL_SCHEMA)
    spec = g2s.spec_from_dict(payload)
    assert spec.steering["quicklook_ncount"] == 50_000_000
    assert spec.steering["dynamic_ncount"] is True
    assert spec.steering["maximum_bridge_steps"] == 1


def test_file_schema_accepts_explicitly_disabled_analysis_limit():
    payload = {**_GOOD, "analysis_limit_seconds": None}
    spec = g2s.spec_from_dict(payload, defaults={"tier": "deterministic"})
    assert spec.analysis_limit_seconds is None


def test_file_schema_still_rejects_invalid_analysis_limit_type():
    payload = {**_GOOD, "analysis_limit_seconds": "none"}
    assert not g2s._conforms(payload, g2s.FILE_SPEC_SCHEMA)
    with pytest.raises(g2s.GoalParseError):
        g2s.spec_from_dict(payload, defaults={"tier": "deterministic"})


def test_hypothesis_resolution_precedence_goal_then_card_then_shipped():
    base = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
            "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic"}
    card = {"optic": {"zc_energy": {"mean": 8.0, "sigma": 1.0}, "confidence": 0.7},
            "acoustic": {}}

    card_spec = g2s.goal_to_spec(
        "optic", dispatcher=_FakeDispatcher(DispatchResult(structured=base)),
        card_hypotheses=card)
    optic = next(h for h in card_spec.hypotheses if h["name"] == "optic")
    assert optic["zc_energy"] == {"mean": 8.0, "sigma": 1.0}
    assert card_spec.hypothesis_source == "card_policy"

    goal_payload = {**base, "target_zc_energy_meV": 10.0,
                    "target_zc_energy_sigma_meV": 0.5}
    goal_spec = g2s.goal_to_spec(
        "optic at 10 +- 0.5 meV",
        dispatcher=_FakeDispatcher(DispatchResult(structured=goal_payload)),
        card_hypotheses=card)
    optic = next(h for h in goal_spec.hypotheses if h["name"] == "optic")
    assert optic["zc_energy"] == {"mean": 10.0, "sigma": 0.5}
    assert goal_spec.hypothesis_source == "goal_override"

    shipped = g2s.goal_to_spec(
        "optic", dispatcher=_FakeDispatcher(DispatchResult(structured=base)))
    assert shipped.hypothesis_source == "shipped_preset"
    assert next(h for h in shipped.hypotheses if h["name"] == "optic")["zc_energy"]["mean"] == 6.0


def test_objective_envelope_v1_is_rejected_after_prerelease_reset(tmp_path):
    path = tmp_path / "old-envelope.json"
    path.write_text(json.dumps({"schema": "isar.objective-envelope/1",
                                "objective": {}, "source": {}}), encoding="utf-8")
    with pytest.raises(g2s.GoalParseError, match="objective-envelope/2"):
        g2s.envelope_from_json(path)


@pytest.mark.parametrize("payload, message", [
    ({"target_zc_energy_meV": 10.0, "target_zc_energy_sigma_meV": -0.5}, "must be positive"),
    ({"target_zc_energy_sigma_meV": 0.5}, "requires target_zc_energy_meV"),
])
def test_gap_uncertainty_validation_is_explicit(payload, message):
    data = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
            "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic", **payload}
    with pytest.raises(g2s.GoalParseError, match=message):
        g2s.goal_to_spec("optic", dispatcher=_FakeDispatcher(DispatchResult(structured=data)))


def test_goal_layer_rejects_degenerate_segment():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8}
    with pytest.raises(g2s.GoalParseError, match="degenerate"):
        g2s.goal_to_spec("single accidental segment",
                         dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))


def test_schema_checker_enforces_array_shape_and_branch_enum():
    payload = {"q_start": [2, 0], "q_end": [2, 0.5, 0], "tolerance_meV": 0.5,
               "budget_hours": 8, "target_branch": "banana"}
    assert not g2s._conforms(payload, g2s.GOAL_SCHEMA)


def test_schema_checker_requires_real_booleans_and_rejects_unknown_types():
    assert g2s._conforms(True, {"type": "boolean"})
    assert not g2s._conforms("false", {"type": "boolean"})
    assert not g2s._conforms(1, {"type": "boolean"})
    assert g2s._conforms("anything", {})
    assert not g2s._conforms("anything", {"type": "mystery"})
