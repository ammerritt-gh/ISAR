"""goal→campaign program: schema validation with a FAKE dispatcher, plus the LLM-free JSON path.

``spec_from_dict`` is the objective *compiler* and is unchanged by SCENE-2 — it is exercised
directly here because that is the layer these cases are about. What changed is the layer above it:
the supervisor and the file reader now speak ``isar.campaign-program/1`` and nothing else.
"""
import copy
import json
from pathlib import Path

import pytest

from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor import goal_to_spec as g2s
from isar.reduce.llm_client import DispatchResult
from isar.schema.campaign_program import PROGRAM_SCHEMA, ProgramError

FIXTURES = Path(__file__).parent / "fixtures"


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


def _targeted(payload):
    """The supervisor reply for a targeted command — the tagged one-step program shape."""
    return {"kind": "targeted", "targeted": payload}


def _objective(goal, payload, **kwargs):
    """Compile a targeted goal through the supervisor and return the step's ObjectiveSpec."""
    program = g2s.goal_to_program(
        goal, dispatcher=_FakeDispatcher(DispatchResult(structured=_targeted(payload))), **kwargs)
    assert [step.kind for step in program.steps] == ["targeted"]
    return ObjectiveSpec.from_dict(program.steps[0].payload)


def test_structured_output_builds_a_one_step_targeted_program():
    disp = _FakeDispatcher(DispatchResult(structured=_targeted(_GOOD)))
    program = g2s.goal_to_program("map the acoustic phonon 2.1->2.4, 0.5 meV, overnight",
                                  dispatcher=disp)
    assert program.to_dict()["schema"] == PROGRAM_SCHEMA
    assert [(s.id, s.kind) for s in program.steps] == [("targeted-1", "targeted")]
    # The command is kept verbatim; the compiled objective is the step payload (P1: frozen config).
    assert program.source_text == "map the acoustic phonon 2.1->2.4, 0.5 meV, overnight"
    spec = ObjectiveSpec.from_dict(program.steps[0].payload)
    assert spec.path == [(2.1, 0.0, 0.0), (2.2, 0.0, 0.0), (2.3, 0.0, 0.0), (2.4, 0.0, 0.0)]
    assert spec.tolerance_meV == 0.5
    assert spec.budget == {"kind": "counting_seconds", "amount": 8 * 3600.0}


def test_budget_neutrons_mapping():
    payload = dict(_GOOD); payload.pop("budget_hours"); payload["budget_neutrons"] = 1e10
    spec = _objective("x", payload)
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
    fenced = "```json\n" + json.dumps(_targeted(_GOOD)) + "\n```"
    disp = _FakeDispatcher(DispatchResult(structured=None, output=fenced))
    program = g2s.goal_to_program("x", dispatcher=disp)
    assert ObjectiveSpec.from_dict(program.steps[0].payload).tolerance_meV == 0.5


def test_one_repair_retry_then_success():
    disp = _FakeDispatcher(DispatchResult(structured=None, output="sorry, no idea"),
                           DispatchResult(structured=_targeted(_GOOD)))
    program = g2s.goal_to_program("x", dispatcher=disp)
    assert ObjectiveSpec.from_dict(program.steps[0].payload).tolerance_meV == 0.5
    assert len(disp.calls) == 2                              # initial + one repair
    assert g2s._REPAIR_SUFFIX in disp.calls[1]["user"]


def test_refuses_honestly_on_persistent_nonconforming():
    disp = _FakeDispatcher(DispatchResult(structured={"kind": "targeted",
                                                      "targeted": {"path": {"h_start": 1}}}),
                           DispatchResult(structured=None, output="still nope"))
    with pytest.raises(g2s.GoalParseError):
        g2s.goal_to_program("x", dispatcher=disp)


def test_a_reply_tagged_targeted_but_carrying_a_discovery_region_is_refused():
    """The tag and the branch must agree, or a survey compiles into a one-point objective."""
    mistagged = {"kind": "targeted",
                 "discover_and_map": {"q_start": [2, 0, 0], "q_end": [3, 0, 0],
                                      "intrinsic_omega_domain_meV": [0, 10]}}
    disp = _FakeDispatcher(DispatchResult(structured=mistagged),
                           DispatchResult(structured=mistagged))
    with pytest.raises(g2s.GoalParseError):
        g2s.goal_to_program("x", dispatcher=disp)


def test_refuses_on_llmm_unreachable():
    disp = _FakeDispatcher(DispatchResult(error="transport_error: connection refused"),
                           DispatchResult(error="transport_error: connection refused"))
    with pytest.raises(g2s.GoalParseError) as e:
        g2s.goal_to_program("x", dispatcher=disp)
    assert "transport_error" in str(e.value)


# --------------------------------------------------------------------------- LLM-free path
def _program_file(tmp_path, payload, *, kind="targeted", step_id="targeted-1", **extra):
    p = tmp_path / "program.json"
    p.write_text(json.dumps({"schema": PROGRAM_SCHEMA, **extra,
                             "steps": [{"id": step_id, "kind": kind, "payload": payload}]}),
                 encoding="utf-8")
    return p


def test_program_from_json_carries_compact_authoring_input(tmp_path):
    program = g2s.program_from_json(_program_file(tmp_path, _GOOD))
    spec = g2s.spec_from_dict(program.steps[0].payload, defaults={"tier": "deterministic"})
    assert len(spec.path) == 4 and spec.tolerance_meV == 0.5


def test_program_from_json_carries_a_frozen_objective_payload(tmp_path):
    full = ObjectiveSpec.along_h(h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.3,
                                 budget={"kind": "neutrons", "amount": 1e9}).to_dict()
    program = g2s.program_from_json(_program_file(tmp_path, full))
    spec = g2s.spec_from_dict(program.steps[0].payload)
    assert spec.tolerance_meV == 0.3 and spec.budget["kind"] == "neutrons"


def test_program_from_json_rejects_a_bare_objective_spec_file(tmp_path):
    # The old --spec file shape is not a program; it is refused rather than wrapped implicitly.
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(_GOOD), encoding="utf-8")
    with pytest.raises(ProgramError, match="schema must be 'isar.campaign-program/1'"):
        g2s.program_from_json(p)


def test_program_from_json_reads_a_frozen_discovery_step(tmp_path):
    payload = json.loads((FIXTURES / "program_discover_challenge.json").read_text(
        encoding="utf-8"))["steps"][0]["payload"]
    program = g2s.program_from_json(_program_file(tmp_path, payload, kind="discover_and_map",
                                                  step_id="discover-1"))
    assert program.steps[0].payload.intrinsic_omega_domain_meV == (0.0, 10.0)


def test_transverse_optic_k_segment_compiles_to_eleven_points():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8,
               "target_branch": "optic", "target_label": "transverse optic"}
    spec = _objective("map the transverse optic phonon from (2,0,0) along k, out to k=0.5",
                      payload)
    assert len(spec.path) == 11 and spec.path[-1] == (2.0, 0.5, 0.0)
    assert spec.target_branch == "optic" and spec.target_label == "transverse optic"
    assert spec.energy_search_meV == (0.0, g2s.DEFAULT_INSTRUMENT_ENERGY_CEILING_MEV)
    assert spec.target_selector == "tracked_branch"


def test_the_energy_domain_is_the_instruments_not_the_branchs():
    """Owner ruling 2026-09-05: no per-branch search ranges. Every branch plans in [0, ceiling];
    the ceiling is the card's kinematic maximum when given, else the shipped default (and said)."""
    payload = {"path": {"h_start": 2.0, "h_end": 3.0, "k": 0.0, "l": 0.0}, "step_h": 0.05,
               "tolerance_meV": 0.5, "target_branch": "acoustic"}
    spec = _objective("map the acoustic phonon from (2,0,0) to (3,0,0)", payload)
    assert spec.energy_search_meV == (0.0, g2s.DEFAULT_INSTRUMENT_ENERGY_CEILING_MEV)
    assert spec.target_search_source == "instrument_default"
    program = g2s.goal_to_program(
        "map the acoustic phonon from (2,0,0) to (3,0,0)",
        dispatcher=_FakeDispatcher(DispatchResult(structured={"kind": "targeted", "targeted": payload})),
        energy_ceiling_meV=35.0)
    compiled = program.steps[0].payload
    assert compiled["energy_search_meV"] == [0.0, 35.0]
    assert compiled["target_search_source"] == "instrument"
    # The acoustic slope prior is a BOUND derived from the domain, so it grows with the ceiling
    # instead of freezing a demo-sample number.
    acoustic = next(h for h in compiled["hypotheses"] if h["name"] == "acoustic")
    assert acoustic["slope_zc"]["sigma"] == pytest.approx(35.0 / 0.5)


def test_a_declared_energy_search_range_is_refused_by_name():
    spec = {"path": {"h_start": 2.0, "h_end": 3.0, "k": 0.0, "l": 0.0}, "step_h": 0.05,
            "tolerance_meV": 0.5, "target_branch": "acoustic", "energy_search_meV": [-9.0, 9.0]}
    with pytest.raises(g2s.GoalParseError, match="energy_search_meV is retired"):
        g2s.spec_from_dict(spec, where="file")


def test_supplied_target_energy_is_a_zone_centre_anchor_on_the_declared_branch():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_energy_meV": 7.5}
    spec = _objective("measure the optic phonon near 7.5 meV", payload)
    assert spec.target_selector == "tracked_branch"
    optic = next(h for h in spec.hypotheses if h["name"] == "optic")
    assert optic["zc_energy"]["mean"] == 7.5


def test_optic_energy_identity_compiles_as_zone_center_tracked_branch():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "tracked_branch", "target_zc_energy_meV": 6.0}
    spec = _objective("map the optic phonon around 6 meV", payload)
    optic = next(h for h in spec.hypotheses if h["name"] == "optic")
    assert spec.target_selector == "tracked_branch"
    assert optic["zc_energy"]["mean"] == 6.0


def test_zone_center_energy_implies_tracked_branch_when_selector_is_omitted():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_zc_energy_meV": 6.0}
    spec = g2s.spec_from_dict(payload)
    assert spec.target_selector == "tracked_branch"


def test_tracked_branch_promotes_target_energy_alias_without_global_selector():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "tracked_branch", "target_energy_meV": 6.0}
    spec = g2s.spec_from_dict(payload)
    optic = next(h for h in spec.hypotheses if h["name"] == "optic")
    assert optic["zc_energy"]["mean"] == 6.0


def test_a_per_q_energy_selector_is_refused_and_names_the_branch_route():
    """The planner maps a declared branch, so a spec asking for the feature nearest an energy at
    every q is refused at compile time instead of becoming a campaign that never terminates."""
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "nearest_energy", "target_energy_meV": 6.0}
    with pytest.raises(g2s.GoalParseError, match="target_branch"):
        g2s.spec_from_dict(payload)
    # The supervisor cannot emit one either: the retired names are out of the schema enum.
    assert not g2s._conforms(payload, g2s.LLM_GOAL_SCHEMA)
    assert "nearest_energy" not in g2s.SYSTEM and "lowest_energy" not in g2s.SYSTEM


def test_file_schema_accepts_operator_steering_but_llm_schema_does_not():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8,
               "steering": {"quicklook_ncount": 50_000_000}}
    assert g2s._conforms(payload, g2s.FILE_SPEC_SCHEMA)
    assert not g2s._conforms(payload, g2s.LLM_GOAL_SCHEMA)
    spec = g2s.spec_from_dict(payload)
    assert spec.steering["quicklook_ncount"] == 50_000_000
    assert spec.steering["dynamic_ncount"] is True


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

    card_spec = _objective("optic", base, card_hypotheses=card)
    optic = next(h for h in card_spec.hypotheses if h["name"] == "optic")
    assert optic["zc_energy"] == {"mean": 8.0, "sigma": 1.0}
    assert card_spec.hypothesis_source == "card_policy"

    goal_payload = {**base, "target_zc_energy_meV": 10.0,
                    "target_zc_energy_sigma_meV": 0.5}
    goal_spec = _objective("optic at 10 +- 0.5 meV", goal_payload, card_hypotheses=card)
    optic = next(h for h in goal_spec.hypotheses if h["name"] == "optic")
    assert optic["zc_energy"] == {"mean": 10.0, "sigma": 0.5}
    assert goal_spec.hypothesis_source == "goal_override"

    shipped = _objective("optic", base)
    assert shipped.hypothesis_source == "shipped_preset"
    # The shipped optic prior is the middle of the instrument's domain [0, ceiling], not a sample
    # number: with the default ceiling of 20 meV that is 10 +- 5 (owner ruling 2026-09-05).
    ceiling = g2s.DEFAULT_INSTRUMENT_ENERGY_CEILING_MEV
    optic = next(h for h in shipped.hypotheses if h["name"] == "optic")["zc_energy"]
    assert optic == {"mean": ceiling / 2.0, "sigma": ceiling / 4.0}


@pytest.mark.parametrize("retired", ["isar.objective-envelope/1", "isar.objective-envelope/2"])
def test_the_retired_objective_envelope_is_unreadable_by_design(tmp_path, retired):
    # H1: the old envelope is not deprecated, it is gone. The refusal names the contract that
    # replaced it so an operator knows what to write instead - there is no adapter to reach for.
    path = tmp_path / "old-envelope.json"
    path.write_text(json.dumps({"schema": retired, "objective": {}, "source": {}}),
                    encoding="utf-8")
    with pytest.raises(ProgramError, match="was retired by 'isar.campaign-program/1'"):
        g2s.program_from_json(path)
    assert not hasattr(g2s, "envelope_from_json") and not hasattr(g2s, "make_envelope")


def test_gap_uncertainty_validation_is_explicit():
    data = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
            "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
            "target_zc_energy_meV": 10.0, "target_zc_energy_sigma_meV": -0.5}
    with pytest.raises(g2s.GoalParseError, match="must be positive"):
        _objective("optic", data)


def test_a_sigma_without_an_energy_is_dropped_not_refused(caplog):
    """A sigma with no energy to qualify carries no operator intent -- the sentence never said it,
    the model's structured output filled it in (a local 35B model did so on four of four compiles
    of the demo sentence, 2026-09-03). Refusing it turned "enter the sentence, hit go" into no scan
    at all; it is dropped with a warning and the branch keeps its shipped prior."""
    data = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
            "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
            "target_zc_energy_sigma_meV": 0.5}
    with caplog.at_level("WARNING"):
        spec = _objective("optic", data)
    assert any("ignoring target_zc_energy_sigma_meV" in r.getMessage() for r in caplog.records)
    optic = next(h for h in spec.hypotheses if h.get("name") == "optic")
    assert optic["zc_energy"]["sigma"] != 0.5


def test_zero_valued_target_filler_is_dropped_at_the_llm_boundary(caplog):
    """The local 35B model pads every optional numeric key with 0.0 when the sentence gives it
    nothing (six of six compiles, 2026-09-08): a zone-centre anchor at 0 +- 0 meV at 0 K. Read
    literally that is refused twice over (sigma, then temperature) and the sentence never said any
    of it. It is dropped with one warning; the branch keeps its shipped prior and no temperature
    is declared. A negative sigma is still refused: that is an operator typo, not filler."""
    data = {"q_start": [2, 0, 0], "q_end": [3, 0, 0], "step_rlu": 0.05, "tolerance_meV": 0.1,
            "target_branch": "acoustic", "target_label": "acoustic phonon branch",
            "target_selector": "tracked_branch", "target_energy_meV": 0.0,
            "target_temperature_K": 0.0, "target_zc_energy_meV": 0.0,
            "target_zc_energy_sigma_meV": 0.0}
    with caplog.at_level("WARNING"):
        spec = _objective("map the acoustic phonon branch from (2,0,0) to (3,0,0)", data)
    assert any("dropping zero-valued supervisor filler" in r.getMessage() for r in caplog.records)
    assert spec.target_temperature_K is None
    assert spec.hypothesis_source == "shipped_preset"
    assert spec.meta["raw"].keys().isdisjoint(g2s._ZERO_IS_FILLER)

    with pytest.raises(g2s.GoalParseError, match="must be positive.*supervisor reply"):
        _objective("optic", {**data, "target_branch": "optic", "target_zc_energy_meV": 8.0,
                             "target_zc_energy_sigma_meV": -0.5})


def test_goal_layer_rejects_degenerate_segment():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8}
    with pytest.raises(g2s.GoalParseError, match="degenerate"):
        _objective("single accidental segment", payload)


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


# --------------------------------------------------------------- scan-window EIG policy rebinding
_CARD_EIG_AUTHORING = {"policies": [{
    "policy_id": "eig4_slice8_excitation_energy",
    "policy_kind": "isar.scalar_absolute_partitions/o36.5",
    "source_qoi": {"id": "excitation.energy", "unit": "meV"},
    "scope": {"bind_to_current_campaign_configuration": True,
              "annotation": {"campaign_name": "regression",
                             "experiment_description": "policy scope rebinding",
                             "date": "2026-08-05"}},
    "provenance": {"kind": "explicit_user_analysis/1", "source_id": "O41",
                   "rationale": "regression fixture"},
    "variants": [{"variant_id": "primary_0p5_mev",
                  "parameters": {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]}}],
}]}

_EIG_SPEC = {"path": {"h_start": 2.0, "h_end": 2.2, "k": 0.0, "l": 0.0}, "step_h": 0.1,
             "tolerance_meV": 0.5, "target_branch": "acoustic", "budget_hours": 1}


def _eig_objective():
    return g2s.spec_from_dict(_EIG_SPEC, defaults={"tier": "deterministic"}, where="test",
                              card_eig_policy_set=_CARD_EIG_AUTHORING)


def _scope_drifted(objective, mode="authoritative"):
    """Reproduce the CLI's steering override, which recomputes the hypothesis fingerprint."""
    from dataclasses import replace

    from isar.schema.hypotheses import content_fingerprint

    steering = {**dict(objective.steering), "scan_window_eig_mode": mode}
    return replace(objective, steering=steering,
                   hypothesis_fingerprint=content_fingerprint(objective.hypotheses, steering))


def _matches(objective):
    from isar.drive.objective import current_objective_comparison_scope
    from isar.schema.eig_policy import matching_qoi_policies

    scope = current_objective_comparison_scope(objective)
    return matching_qoi_policies(
        objective.scan_window_eig.policy_set, qoi_id="excitation.energy", qoi_unit="meV",
        scope_kind="campaign_configuration", scope_fingerprint=scope["fingerprint"])


# ------------------------------------------------------- discovery compilation (golden programs)
_DISCOVER_COMMAND = ("Scan the region from (2,0,0) to (3,0,0) up to 10meV, map out all the "
                     "phenomena of interest.")
_DISCOVER_REPLY = {"kind": "discover_and_map", "discover_and_map": {
    "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0],
    "maximum_initial_spacing_rlu": 0.1, "intrinsic_omega_domain_meV": [0.0, 10.0],
    "notes": "map all phenomena of interest"}}

# The 0.05 meV here is DELIBERATELY not the compiler default (0.1 meV since 2026-09-03): the
# command states the bar explicitly, so this golden pins that an operator-declared tolerance is
# carried through untouched. Make it equal the default and the golden stops discriminating.
_TARGETED_COMMAND = "Map the acoustic phonon from (2,0,0) to (3,0,0) to +-0.05 meV, overnight."
_TARGETED_REPLY = {"kind": "targeted", "targeted": {
    "path": {"h_start": 2.0, "h_end": 3.0, "k": 0.0, "l": 0.0}, "step_h": 0.05,
    "tolerance_meV": 0.05, "budget_hours": 8, "target_branch": "acoustic",
    "notes": "acoustic branch (2,0,0)->(3,0,0)"}}


@pytest.mark.parametrize("command, reply, golden", [
    (_DISCOVER_COMMAND, _DISCOVER_REPLY, "program_discover_challenge.json"),
    (_TARGETED_COMMAND, _TARGETED_REPLY, "program_targeted_challenge.json"),
])
def test_a_frozen_supervisor_reply_compiles_to_the_golden_program(command, reply, golden):
    """LLM-free goldens: the supervisor's reply is stubbed, so this pins the *compiler*.

    The whole point of P1 is that the LLM's answer becomes config before anything deterministic
    runs. These two files are that config for the two challenge commands; a change to them is a
    deliberate re-baseline, never incidental.
    """
    program = g2s.goal_to_program(
        command, dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))
    assert program.to_dict() == json.loads(
        (FIXTURES / golden).read_text(encoding="utf-8"))


def test_a_discovery_command_compiles_to_a_frozen_discover_step_not_an_objective():
    program = g2s.goal_to_program(
        _DISCOVER_COMMAND, dispatcher=_FakeDispatcher(DispatchResult(structured=_DISCOVER_REPLY)))
    assert [(s.id, s.kind) for s in program.steps] == [("discover-1", "discover_and_map")]
    spec = program.steps[0].payload
    assert spec.q_start == (2.0, 0.0, 0.0) and spec.q_end == (3.0, 0.0, 0.0)
    # "up to 10 meV" is an intrinsic domain [0, 10] — never a signed [-10, 10] measurement window.
    assert spec.intrinsic_omega_domain_meV == (0.0, 10.0)
    assert spec.include_unknown is True
    assert spec.catalogue_profile == g2s.DEFAULT_CATALOGUE_PROFILE
    assert program.source_text == _DISCOVER_COMMAND


def test_a_discovery_reply_without_a_spacing_takes_the_compiler_default():
    reply = copy.deepcopy(_DISCOVER_REPLY)
    del reply["discover_and_map"]["maximum_initial_spacing_rlu"]
    program = g2s.goal_to_program(
        _DISCOVER_COMMAND, dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))
    assert (program.steps[0].payload.maximum_initial_spacing_rlu
            == g2s.DEFAULT_DISCOVER_SPACING_RLU)


def test_a_discovery_reply_with_an_explicit_zero_spacing_is_refused_not_defaulted():
    """A default fills an OMISSION, never a wrong value: 0 r.l.u. is a declaration, and quietly
    rewriting it to 0.05 would run a survey the supervisor did not ask for."""
    reply = copy.deepcopy(_DISCOVER_REPLY)
    reply["discover_and_map"]["maximum_initial_spacing_rlu"] = 0
    with pytest.raises(ProgramError, match="must be positive and finite"):
        g2s.goal_to_program(_DISCOVER_COMMAND,
                            dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))


def test_a_discovery_reply_with_an_explicit_empty_catalogue_profile_is_refused():
    reply = copy.deepcopy(_DISCOVER_REPLY)
    reply["discover_and_map"]["catalogue_profile"] = ""
    with pytest.raises(ProgramError, match="non-empty string reference"):
        g2s.goal_to_program(_DISCOVER_COMMAND,
                            dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))


def test_an_explicitly_empty_deliverables_list_stays_empty():
    """"Deliver nothing" is a legal, if unusual, declaration — not the default phenomenon map."""
    reply = copy.deepcopy(_DISCOVER_REPLY)
    reply["discover_and_map"]["deliverables"] = []
    program = g2s.goal_to_program(
        _DISCOVER_COMMAND, dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))
    assert program.steps[0].payload.deliverables == ()


@pytest.mark.parametrize("absent", [True, False])
def test_absent_or_null_discovery_fields_still_take_the_compiler_defaults(absent):
    """Only absent-or-null means "omitted". Driven through ``_discover_spec`` directly because the
    supervisor's own reply schema rejects an explicit null before the compiler ever sees it."""
    reply = copy.deepcopy(_DISCOVER_REPLY["discover_and_map"])
    for name in ("maximum_initial_spacing_rlu", "catalogue_profile", "deliverables"):
        if absent:
            reply.pop(name, None)
        else:
            reply[name] = None
    spec = g2s._discover_spec(reply)
    assert spec.maximum_initial_spacing_rlu == g2s.DEFAULT_DISCOVER_SPACING_RLU
    assert spec.catalogue_profile == g2s.DEFAULT_CATALOGUE_PROFILE
    assert spec.deliverables == tuple(g2s.DEFAULT_DELIVERABLES)


def test_a_discovery_reply_with_a_signed_energy_window_is_refused():
    reply = copy.deepcopy(_DISCOVER_REPLY)
    reply["discover_and_map"]["intrinsic_omega_domain_meV"] = [-10.0, 10.0]
    with pytest.raises(ProgramError, match="not a signed measurement window"):
        g2s.goal_to_program(_DISCOVER_COMMAND,
                            dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))


def test_an_invalid_include_unknown_is_refused_not_coerced():
    """PR #31 finding L: bool() laundered "false" into True and 0 into False, so an invalid
    explicit declaration never reached the schema's boolean check. Defaults apply only when
    the field is absent or null."""
    import pytest as _pytest
    from isar.drive.supervisor.goal_to_spec import _discover_spec
    from isar.schema.campaign_program import ProgramError
    data = {"q_start": [2, 0, 0], "q_end": [3, 0, 0],
            "intrinsic_omega_domain_meV": [0, 10]}
    with _pytest.raises(ProgramError, match="must be a boolean"):
        _discover_spec({**data, "include_unknown": "false"})
    assert _discover_spec({**data, "include_unknown": None}).include_unknown is True
    assert _discover_spec({**data, "include_unknown": False}).include_unknown is False
