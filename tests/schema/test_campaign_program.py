"""isar.campaign-program/1: round-trip, freeze semantics, and precise refusals (DECISIONS H1)."""
import json
from pathlib import Path

import pytest

from isar.drive.objective import OBJECTIVE_SCHEMA, ObjectiveSpec
from isar.schema.campaign_program import (
    DISCOVER_STEP_SCHEMA,
    PROGRAM_SCHEMA,
    TARGETED_STEP_SCHEMA,
    CampaignProgram,
    CampaignStep,
    DiscoverAndMapSpec,
    ProgramError,
    _store_canonical_bytes,
    program_fingerprint,
    targeted_program,
)
from isar.schema.question_policy import resolve_policy

FIXTURES = Path(__file__).parents[1] / "drive" / "fixtures"

_DISCOVER = {
    "schema": DISCOVER_STEP_SCHEMA,
    "region": {"kind": "path", "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0],
               "maximum_initial_spacing_rlu": 0.1},
    "energy": {"intrinsic_omega_domain_meV": [0.0, 10.0]},
    "catalogue_profile": "isar.catalogue.inelastic-default/1",
    "deliverables": [{"kind": "phenomenon_map"}],
    "include_unknown": True,
    "resolved_policy": resolve_policy(),
}


def _program(payload=None, **kw):
    return CampaignProgram.from_dict({
        "schema": PROGRAM_SCHEMA,
        "steps": [{"id": "discover-1", "kind": "discover_and_map",
                   "payload": payload if payload is not None else _DISCOVER}],
        **kw})


# --------------------------------------------------------------------------- round trip
def test_discover_program_round_trips_exactly():
    program = _program(source_text="map everything from (2,0,0) to (3,0,0) up to 10 meV")
    d = program.to_dict()
    assert d["schema"] == PROGRAM_SCHEMA
    assert CampaignProgram.from_dict(d).to_dict() == d


def test_targeted_program_carries_the_objective_content_unchanged():
    objective = ObjectiveSpec.along_h(h_start=2.0, h_end=2.2, step=0.1, tolerance_meV=0.5,
                                      budget={"kind": "neutrons", "amount": 1e9})
    program = targeted_program(objective.to_dict(), source_text="map the acoustic branch")
    d = program.to_dict()
    assert d["steps"][0]["payload"] == objective.to_dict()
    assert CampaignProgram.from_dict(d).to_dict() == d
    # The objective content survives the re-versioning: only its schema identity moved.
    assert ObjectiveSpec.from_dict(d["steps"][0]["payload"]).to_dict() == objective.to_dict()


def test_the_objective_payload_schema_is_the_targeted_step_schema():
    assert OBJECTIVE_SCHEMA == TARGETED_STEP_SCHEMA == "isar.campaign-step.targeted/1"


def test_a_source_fingerprint_survives_the_round_trip_and_an_empty_one_is_omitted():
    assert "source" not in _program().to_dict()
    with_source = _program()
    carried = CampaignProgram(steps=with_source.steps, source={"kind": "goal", "sha256": "abc"})
    assert CampaignProgram.from_dict(carried.to_dict()).source == {"kind": "goal",
                                                                   "sha256": "abc"}


# --------------------------------------------------------------------------- freeze semantics
def test_a_program_is_frozen_and_cannot_be_re_pointed():
    program = _program()
    with pytest.raises(Exception):
        program.steps = ()
    with pytest.raises(Exception):
        program.steps[0].payload.catalogue_profile = "something else"


def test_ingested_payloads_are_copied_so_the_caller_cannot_mutate_a_frozen_program():
    payload = {"tolerance_meV": 0.5, "deliverables": []}
    step = CampaignStep(id="targeted-1", kind="targeted", payload=payload)
    payload["tolerance_meV"] = 99.0
    assert step.payload["tolerance_meV"] == 0.5
    # ... and the emitted dict is a copy too, so a consumer editing it cannot reach back in.
    step.to_dict()["payload"]["tolerance_meV"] = 42.0
    assert step.payload["tolerance_meV"] == 0.5


# --------------------------------------------------------------------------- retired contracts
@pytest.mark.parametrize("retired", ["isar.objective/2", "isar.objective-envelope/2",
                                     "isar.objective/1", "isar.objective-envelope/1"])
def test_every_retired_contract_is_refused_by_name(retired):
    with pytest.raises(ProgramError, match="was retired by 'isar.campaign-program/1'"):
        CampaignProgram.from_dict({"schema": retired, "objective": {}, "source": {}})


def test_a_retired_objective_payload_inside_a_program_is_refused_too():
    with pytest.raises(ProgramError, match="isar.objective/2.*was retired"):
        CampaignProgram.from_dict({
            "schema": PROGRAM_SCHEMA,
            "steps": [{"id": "t1", "kind": "targeted",
                       "payload": {"schema": "isar.objective/2", "path": [[2, 0, 0]]}}]})


def test_the_objective_dataclass_refuses_its_own_retired_schema():
    old = ObjectiveSpec.along_h(h_start=2.0, h_end=2.1, step=0.1,
                                tolerance_meV=0.5, budget=None).to_dict()
    old["schema"] = "isar.objective/2"
    with pytest.raises(ValueError, match="was retired by 'isar.campaign-program/1'"):
        ObjectiveSpec.from_dict(old)


# --------------------------------------------------------------------------- validation
def test_a_program_needs_at_least_one_step():
    with pytest.raises(ProgramError, match="at least one step"):
        CampaignProgram.from_dict({"schema": PROGRAM_SCHEMA, "steps": []})


def test_step_ids_must_be_unique():
    step = {"id": "same", "kind": "discover_and_map", "payload": _DISCOVER}
    with pytest.raises(ProgramError, match="duplicate step id"):
        CampaignProgram.from_dict({"schema": PROGRAM_SCHEMA, "steps": [step, dict(step)]})


def test_an_unknown_step_kind_is_rejected_and_names_the_kinds_that_exist():
    with pytest.raises(ProgramError, match="unknown step kind 'refine_everything'"):
        CampaignProgram.from_dict({
            "schema": PROGRAM_SCHEMA,
            "steps": [{"id": "s1", "kind": "refine_everything", "payload": {}}]})


@pytest.mark.parametrize("document, message", [
    ({"steps": []}, "schema must be 'isar.campaign-program/1'"),
    ({"schema": PROGRAM_SCHEMA}, "'steps' must be an ordered list"),
    ({"schema": PROGRAM_SCHEMA, "steps": [], "objective": {}}, "unknown fields"),
])
def test_malformed_programs_say_exactly_what_is_wrong(document, message):
    with pytest.raises(ProgramError, match=message):
        CampaignProgram.from_dict(document)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["region"].pop("q_end"), "needs 'q_end'"),
    (lambda d: d["region"].update(q_start=[2.0, 0.0]), "three reciprocal-lattice coordinates"),
    (lambda d: d["region"].update(q_end=[2.0, 0.0, 0.0]), "identical"),
    (lambda d: d["region"].update(maximum_initial_spacing_rlu=0.0), "positive and finite"),
    (lambda d: d["region"].update(kind="volume"), "is not supported"),
    (lambda d: d["energy"].update(intrinsic_omega_domain_meV=[10.0, 0.0]), "low < high"),
    (lambda d: d.update(catalogue_profile=""), "non-empty string reference"),
    (lambda d: d.update(deliverables=[{"name": "x"}]), "non-empty string 'kind'"),
    (lambda d: d.update(include_unknown="yes"), "must be a boolean"),
    (lambda d: d.update(surprise=1), "unknown fields"),
])
def test_a_malformed_discovery_payload_is_rejected_precisely(mutate, message):
    payload = json.loads(json.dumps(_DISCOVER))
    mutate(payload)
    with pytest.raises(ProgramError, match=message):
        _program(payload)


def test_the_energy_domain_is_intrinsic_and_refuses_a_signed_measurement_window():
    """A negative bound would freeze an anti-Stokes coverage decision that belongs to acquisition.

    Detailed balance ties the anti-Stokes amplitude to the Stokes one through exp(-hbar*omega/kT)
    and gates whether an anti-Stokes peak is admissible at all, at the measured temperature. That
    is a runtime judgement; a program that declared [-10, 10] here would have pre-empted it under
    the guise of an energy range.
    """
    payload = json.loads(json.dumps(_DISCOVER))
    payload["energy"]["intrinsic_omega_domain_meV"] = [-10.0, 10.0]
    with pytest.raises(ProgramError, match="not a signed measurement window"):
        _program(payload)
    # The intrinsic domain of "up to 10 meV" is [0, 10] and is accepted.
    payload["energy"]["intrinsic_omega_domain_meV"] = [0.0, 10.0]
    assert _program(payload).steps[0].payload.intrinsic_omega_domain_meV == (0.0, 10.0)


# --------------------------------------------------------------------------- /2 full freeze
@pytest.mark.parametrize("missing", ["include_unknown", "deliverables", "catalogue_profile",
                                     "resolved_policy"])
def test_a_frozen_step_missing_any_effective_field_refuses_not_defaults(missing):
    """Frozen in FULL (SCENE-4 PR 4.1): a stored /2 document that acquired a future software
    default at read time would be frozen in name only, so the reader defaults nothing."""
    payload = json.loads(json.dumps(_DISCOVER))
    del payload[missing]
    with pytest.raises(ProgramError, match="never acquires read-time defaults"):
        _program(payload)


def test_the_retired_discover_v1_step_is_refused_naming_its_replacement():
    payload = json.loads(json.dumps(_DISCOVER))
    payload["schema"] = "isar.campaign-step.discover_and_map/1"
    with pytest.raises(ProgramError,
                       match="was retired by 'isar.campaign-step.discover_and_map/2'"):
        _program(payload)


def test_an_untagged_compact_discover_payload_is_authoring_input_not_a_frozen_step():
    payload = json.loads(json.dumps(_DISCOVER))
    del payload["schema"]
    with pytest.raises(ProgramError, match="compiled, never read"):
        _program(payload)


def test_a_tampered_policy_block_is_refused_by_the_step():
    payload = json.loads(json.dumps(_DISCOVER))
    del payload["resolved_policy"]["thresholds"]["confirm_presence_min_mass"]
    with pytest.raises(ProgramError, match="missing frozen values"):
        _program(payload)
    unsupported = json.loads(json.dumps(_DISCOVER))
    unsupported["resolved_policy"]["staleness_policy"] = {"mode": "fixed_lag", "lag": 1}
    with pytest.raises(ProgramError, match="is not supported"):
        _program(unsupported)


def test_deliverables_are_frozen_verbatim_including_fields_nothing_reads_yet():
    payload = json.loads(json.dumps(_DISCOVER))
    payload["deliverables"] = [{"kind": "dispersion_map", "branches": ["all"], "future": {"x": 1}}]
    assert _program(payload).to_dict()["steps"][0]["payload"]["deliverables"] == \
        payload["deliverables"]


@pytest.mark.parametrize("deliverables", [None, {}, {"kind": "phenomenon_map"}, "phenomenon_map"])
def test_a_present_but_invalid_deliverables_field_is_refused_not_coerced(deliverables):
    """``or ()`` made a malformed program round-trip as a DIFFERENT valid one.

    A present null or object is a write-it-wrong error, never an omission: coercing it to an empty
    tuple bypassed ``__post_init__`` and silently ran a campaign asked to deliver nothing.
    """
    payload = json.loads(json.dumps(_DISCOVER))
    payload["deliverables"] = deliverables
    with pytest.raises(ProgramError, match="deliverables"):
        _program(payload)


def test_an_explicit_empty_deliverables_list_is_a_valid_declaration():
    payload = json.loads(json.dumps(_DISCOVER))
    payload["deliverables"] = []
    assert _program(payload).steps[0].payload.deliverables == ()


def test_a_present_but_invalid_program_source_is_refused_while_an_absent_one_is_empty():
    assert _program().source == {}
    for bad in (None, "deadbeef"):
        with pytest.raises(ProgramError, match="source must be an object"):
            _program(source=bad)
    assert _program(source={"kind": "goal"}).source == {"kind": "goal"}


# --------------------------------------------------------------------------- golden programs
@pytest.mark.parametrize("name, kind", [
    ("program_discover_challenge.json", "discover_and_map"),
    ("program_targeted_challenge.json", "targeted"),
])
def test_the_golden_compiled_programs_still_load_and_round_trip(name, kind):
    """The frozen LLM-free compilations in tests/drive/fixtures are the contract's worked example.

    They are what ``goal_to_program`` produced for the two challenge commands with the LLM reply
    stubbed (see tests/drive/test_goal_to_spec.py); a schema change that cannot read them back is
    a contract break, not a refactor.
    """
    document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    program = CampaignProgram.from_dict(document)
    assert [step.kind for step in program.steps] == [kind]
    assert program.to_dict() == document
    assert program.source_text


# --------------------------------------------------------------------------- program_fingerprint
def test_program_fingerprint_is_pinned_equal_to_the_scene_store_digest_rule():
    """PR 4.2b C7, fixed PR #33 A-P0-5/B-1: schema's local canonical encoder and scene's
    ``canonical_bytes`` must never be assumed equal — pinned here on a UNICODE payload, the one
    shape that silently forked the two encoders (``ensure_ascii`` True vs False) while every
    ASCII-only test kept passing. A future change to either encoder is caught immediately rather
    than silently re-forking what 'the same program' digests to across the program store and the
    scene/agenda stores."""
    from isar.scene.model import canonical_bytes, digest_bytes

    unicode_discover = {
        **_DISCOVER,
        "deliverables": [{"kind": "phenomenon_map", "note": "ω near Γ"}],
    }
    program = _program(payload=unicode_discover, source_text="map ω near Γ",
                       source={"kind": "goal"})
    payload = program.to_dict()

    store_bytes = canonical_bytes(payload)
    schema_bytes = _store_canonical_bytes(payload)
    assert schema_bytes == store_bytes                 # the two encoders agree BYTE for byte
    assert "\\u03c9" not in schema_bytes.decode("utf-8")  # ensure_ascii=False: ω survives literal
    assert program_fingerprint(program) == digest_bytes(store_bytes)

    # Deterministic and stable under a second, independently-built equal program.
    again = _program(payload=unicode_discover, source_text="map ω near Γ",
                     source={"kind": "goal"})
    assert program_fingerprint(program) == program_fingerprint(again)
    # A changed program moves the fingerprint.
    changed = _program(source_text="a different command entirely")
    assert program_fingerprint(changed) != program_fingerprint(program)
