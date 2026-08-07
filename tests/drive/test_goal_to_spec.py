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
    assert spec.energy_search_meV == (-2.0, 12.0)
    assert spec.target_selector == "tracked_branch" and spec.target_energy_meV is None


def test_acoustic_goal_without_a_range_gets_the_canonical_symmetric_band():
    payload = {"path": {"h_start": 2.0, "h_end": 3.0, "k": 0.0, "l": 0.0}, "step_h": 0.05,
               "tolerance_meV": 0.5, "target_branch": "acoustic"}
    spec = _objective("map the acoustic phonon from (2,0,0) to (3,0,0)", payload)
    assert spec.energy_search_meV == g2s.DEFAULT_ENERGY_SEARCH_ACOUSTIC_MEV == (-9.0, 9.0)


def test_supplied_target_energy_implies_nearest_selector():
    payload = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_energy_meV": 7.5}
    spec = _objective("measure the optic phonon near 7.5 meV", payload)
    assert spec.target_selector == "nearest_energy" and spec.target_energy_meV == 7.5


def test_optic_energy_identity_compiles_as_zone_center_tracked_branch():
    payload = {"q_start": [2, 0, 0], "q_end": [2.5, 0, 0], "step_rlu": 0.05,
               "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic",
               "target_selector": "tracked_branch", "target_zc_energy_meV": 6.0}
    spec = _objective("map the optic phonon around 6 meV", payload)
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
    spec = _objective("at every q, measure the feature nearest 6 meV", payload)
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
    assert next(h for h in shipped.hypotheses if h["name"] == "optic")["zc_energy"]["mean"] == 6.0


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


@pytest.mark.parametrize("payload, message", [
    ({"target_zc_energy_meV": 10.0, "target_zc_energy_sigma_meV": -0.5}, "must be positive"),
    ({"target_zc_energy_sigma_meV": 0.5}, "requires target_zc_energy_meV"),
])
def test_gap_uncertainty_validation_is_explicit(payload, message):
    data = {"q_start": [2, 0, 0], "q_end": [2, 0.5, 0], "step_rlu": 0.05,
            "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "optic", **payload}
    with pytest.raises(g2s.GoalParseError, match=message):
        _objective("optic", data)


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
             "tolerance_meV": 0.5, "target_branch": "acoustic", "budget_hours": 1,
             "energy_search_meV": [-9.0, 9.0]}


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


def test_compiled_card_policy_matches_the_objective_it_was_compiled_from():
    assert [policy.policy_id for policy in _matches(_eig_objective())] == [
        "eig4_slice8_excitation_energy"]


def test_steering_override_without_rebinding_leaves_the_policy_unmatchable():
    # The Test9 failure shape: the policy is present but bound to the pre-override scope, so every
    # scan-window score refuses with qoi_policy_missing.
    drifted = _scope_drifted(_eig_objective())
    assert drifted.scan_window_eig.policy_set is not None
    assert _matches(drifted) == ()


def test_rebinding_after_a_steering_override_restores_an_exact_scope_match():
    rebound = g2s.rebind_scan_window_eig_policy(
        _scope_drifted(_eig_objective()), _CARD_EIG_AUTHORING)
    assert [policy.policy_id for policy in _matches(rebound)] == [
        "eig4_slice8_excitation_energy"]


def test_rebinding_keeps_an_off_objective_without_a_policy_set_unchanged():
    # A frozen objective from before the canonical default existed carries no policy set; with the
    # mode off there is nothing to serve, so rebinding must leave it exactly as it is.
    from dataclasses import replace

    from isar.schema.eig_policy import ScanWindowEIGConfig

    objective = replace(
        g2s.spec_from_dict(_EIG_SPEC, defaults={"tier": "deterministic"}, where="test"),
        scan_window_eig=ScanWindowEIGConfig())
    assert objective.scan_window_eig.policy_set is None
    assert g2s.rebind_scan_window_eig_policy(objective, None) is objective


def test_rebinding_refuses_when_a_hand_authored_policy_cannot_be_reconstructed():
    # A policy that is not the canonical default cannot be recompiled from truth once its card
    # authoring is gone; substituting the default would silently measure a different partition.
    authoring = copy.deepcopy(_CARD_EIG_AUTHORING)
    authoring["policies"][0]["variants"][0]["parameters"]["bin_width"] = 1.0
    objective = g2s.spec_from_dict(_EIG_SPEC, defaults={"tier": "deterministic"}, where="test",
                                   card_eig_policy_set=authoring)
    # Drifted to shadow, not authoritative: this deliberately non-O41 policy could not survive the
    # authoritative profile check at construction, and the rebinding rule under test is mode-free.
    with pytest.raises(g2s.GoalParseError, match="card authoring source is unavailable"):
        g2s.rebind_scan_window_eig_policy(_scope_drifted(objective, mode="shadow"), None)


def test_a_policy_differing_only_in_attribution_is_not_the_canonical_default():
    # The annotation and provenance are the scientist's account of why this partition is measured.
    # A policy matching the default's parameters but carrying its own attribution must not be
    # silently rebound to the default's, which would rewrite that account.
    authoring = copy.deepcopy(_CARD_EIG_AUTHORING)
    authoring["policies"][0]["provenance"]["rationale"] = "a specific reviewed decision"
    objective = g2s.spec_from_dict(_EIG_SPEC, defaults={"tier": "deterministic"}, where="test",
                                   card_eig_policy_set=authoring)
    with pytest.raises(g2s.GoalParseError, match="card authoring source is unavailable"):
        g2s.rebind_scan_window_eig_policy(_scope_drifted(objective), None)


def test_rebinding_the_canonical_policy_needs_no_card_authoring():
    # An ordinary campaign never declares the table, so its policy IS the canonical default and
    # can be recompiled against the post-override scope without a card to read it from.
    ordinary = g2s.spec_from_dict(_EIG_SPEC, defaults={"tier": "deterministic"}, where="test")
    rebound = g2s.rebind_scan_window_eig_policy(_scope_drifted(ordinary), None)
    assert [policy.policy_id for policy in _matches(rebound)] == [
        "eig4_slice8_excitation_energy"]


def test_an_ordinary_campaign_can_switch_scan_window_eig_on():
    # The whole point: an ordinary campaign that never authored a policy table must be able to
    # turn authoritative EIG on, instead of refusing with
    # scan_window_eig_authoritative_profile_invalid and demanding a purpose-built O41 campaign.
    from isar.eig.o41_authority import validate_o41_authoritative_profile

    objective = g2s.spec_from_dict(_EIG_SPEC, defaults={"tier": "deterministic"}, where="test")
    assert objective.scan_window_eig.policy_set is not None      # carried, unused while off
    assert (objective.steering or {}).get("scan_window_eig_mode", "off") == "off"

    authoritative = g2s.rebind_scan_window_eig_policy(_scope_drifted(objective), None)
    validate_o41_authoritative_profile(authoritative.scan_window_eig)
    assert [policy.policy_id for policy in _matches(authoritative)] == [
        "eig4_slice8_excitation_energy"]


# ------------------------------------------------------- discovery compilation (golden programs)
_DISCOVER_COMMAND = ("Scan the region from (2,0,0) to (3,0,0) up to 10meV, map out all the "
                     "phenomena of interest.")
_DISCOVER_REPLY = {"kind": "discover_and_map", "discover_and_map": {
    "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0],
    "maximum_initial_spacing_rlu": 0.1, "intrinsic_omega_domain_meV": [0.0, 10.0],
    "notes": "map all phenomena of interest"}}

_TARGETED_COMMAND = "Map the acoustic phonon from (2,0,0) to (3,0,0) to +-0.5 meV, overnight."
_TARGETED_REPLY = {"kind": "targeted", "targeted": {
    "path": {"h_start": 2.0, "h_end": 3.0, "k": 0.0, "l": 0.0}, "step_h": 0.05,
    "tolerance_meV": 0.5, "budget_hours": 8, "target_branch": "acoustic",
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


def test_a_discovery_reply_with_a_signed_energy_window_is_refused():
    reply = copy.deepcopy(_DISCOVER_REPLY)
    reply["discover_and_map"]["intrinsic_omega_domain_meV"] = [-10.0, 10.0]
    with pytest.raises(ProgramError, match="not a signed measurement window"):
        g2s.goal_to_program(_DISCOVER_COMMAND,
                            dispatcher=_FakeDispatcher(DispatchResult(structured=reply)))
