"""Question identity and subject vocabulary (SCENE-4 PR 4.1, slice 2).

Every case here kills a named identity failure: an ID that churns when a scan lands, a region
that mints two questions for one physics, a claim kind collapsing two different questions into
one, a blocker nobody classified, a dependency graph whose assessment order depends on
iteration. Scene payloads are hand-built dicts — the matcher reads the persisted payload shape,
not live objects, so these fixtures ARE the contract.
"""
import pytest

from isar.scene.questions import (
    Blocker, PredicateDeclaration, QuestionAssessment, QuestionExhaustion, QuestionSpec,
    QuestionSubject, match_subject, question_id, resolve_scope, scene_partitions, snap_tile,
    validate_dependency_dag)

P1 = "a" * 64                                    # partition ids in the payload shape
P2 = "b" * 64


def footprint(scan_ref, coord, partition=P1, bands=((0.5, 8.0),)):
    return {"scan_ref": scan_ref, "q": [coord, 0.0, 0.0],
            "bands": [{"lo": lo, "hi": hi, "sides": "stokes"} for lo, hi in bands],
            "temperature_K": 300.0, "background": None, "gauss_fwhm": 1.0,
            "lorentz_fwhm": None, "effective_exposure": 1.0, "path_index": 0,
            "axis_coordinate": coord, "partition_id": partition}


def observation(scan_ref, sample="s1", epoch=1):
    return {"scan_ref": scan_ref, "sample": sample, "lattice_epoch": epoch}


def scene(footprints, clusters=(), observations=()):
    return {"footprints": list(footprints), "clusters": list(clusters),
            "observations": list(observations)}


def subject(**overrides):
    base = dict(kind="phenomenon", partition_id=P1, region_rlu=(1.8, 2.2),
                omega_band_meV=(0.0, 10.0), catalogue_class="acoustic")
    base.update(overrides)
    return QuestionSubject(**base)


PREDICATE = PredicateDeclaration(kind="structural_presence", version=1,
                                 parameters={"min_mass": 0.9})


# ----------------------------------------------------------------------------
# Stable identity
# ----------------------------------------------------------------------------
def test_question_id_is_stable_when_the_scene_grows():
    """The ID is a pure function of family+claim+subject — scene content never enters."""
    s = subject()
    qid = question_id("ConfirmPresence", "structural_presence", s)
    assert qid == question_id("ConfirmPresence", "structural_presence", subject())

    small = scene([footprint("e:1:d", 2.0)])
    grown = scene([footprint("e:1:d", 2.0), footprint("e:2:d", 2.1)])
    assert match_subject(s, small).scan_refs == ("e:1:d",)
    assert match_subject(s, grown).scan_refs == ("e:1:d", "e:2:d")   # assessment input moved
    assert qid == question_id("ConfirmPresence", "structural_presence", s)  # identity did not


def test_reversed_region_endpoints_are_one_question():
    assert (question_id("ConfirmPresence", "structural_presence",
                        subject(region_rlu=(2.2, 1.8)))
            == question_id("ConfirmPresence", "structural_presence",
                           subject(region_rlu=(1.8, 2.2))))


def test_claim_kind_distinguishes_questions_over_one_subject():
    s = subject(kind="parameter", parameter="omega0")
    assert (question_id("EstimateParameter", "parameter_within_tolerance", s)
            != question_id("EstimateParameter", "zc_linear_compatibility", s))


def test_thresholds_and_policy_never_enter_identity():
    """A threshold-only re-freeze keeps every ID: parameters live on the predicate declaration,
    provenance on the spec — neither is hashed into question_id."""
    s = subject()
    loose = QuestionSpec(family="ConfirmPresence", claim_kind="structural_presence", subject=s,
                         predicate=PredicateDeclaration(kind="structural_presence", version=1,
                                                        parameters={"min_mass": 0.5}),
                         policy_provenance={"profile": "isar.question-defaults/1"})
    tight = QuestionSpec(family="ConfirmPresence", claim_kind="structural_presence", subject=s,
                         predicate=PredicateDeclaration(kind="structural_presence", version=1,
                                                        parameters={"min_mass": 0.99}),
                         policy_provenance={"profile": "isar.question-defaults/2"})
    assert loose.question_id == tight.question_id


def test_track_subject_identity_is_endpoint_tiles_not_components():
    """Endpoint order is canonicalized; there is no field that could carry a component ID or a
    scan ref, so scan/label churn cannot reach identity by construction."""
    a = QuestionSubject(kind="track", partition_id=P1, endpoint_a_rlu=(1.0, 1.05),
                        endpoint_b_rlu=(2.0, 2.05), catalogue_class="acoustic")
    b = QuestionSubject(kind="track", partition_id=P1, endpoint_a_rlu=(2.0, 2.05),
                        endpoint_b_rlu=(1.0, 1.05), catalogue_class="acoustic")
    assert question_id("ConnectTrack", "track_connected", a) == \
        question_id("ConnectTrack", "track_connected", b)
    assert "scan_ref" not in a.to_dict() and "component_id" not in a.to_dict()
    with pytest.raises(ValueError, match="both endpoint tiles or neither"):
        QuestionSubject(kind="track", partition_id=P1, endpoint_a_rlu=(1.0, 1.05))


def test_emergent_tiles_snap_to_the_frozen_grid():
    """Two detections in one tile are one subject; the tile quantum is a program-frozen input,
    never a constant hiding in this module."""
    assert snap_tile(2.03, 0.05) == snap_tile(2.04, 0.05) == (2.0, 2.05)
    assert snap_tile(2.07, 0.05) == (2.05, 2.1)
    with pytest.raises(ValueError, match="tile quantum"):
        snap_tile(2.0, 0.0)


def test_a_stored_spec_whose_id_does_not_rederive_refuses():
    spec = QuestionSpec(family="ConfirmPresence", claim_kind="structural_presence",
                        subject=subject(), predicate=PREDICATE)
    d = spec.to_dict()
    d["question_id"] = "0" * 64
    with pytest.raises(ValueError, match="does not re-derive"):
        QuestionSpec.from_dict(d)
    assert QuestionSpec.from_dict(spec.to_dict()) == spec


# ----------------------------------------------------------------------------
# Partition scoping
# ----------------------------------------------------------------------------
def test_matching_is_partition_exact():
    """A subject in partition A sees nothing from partition B, however close the q."""
    payload = scene([footprint("e:1:d", 2.0, partition=P1),
                     footprint("e:2:d", 2.0, partition=P2)],
                    clusters=[{"scan_refs": ["e:1:d"]}, {"scan_refs": ["e:2:d"]}])
    match = match_subject(subject(), payload)
    assert match.scan_refs == ("e:1:d",)
    assert match.cluster_indices == (0,)


def test_a_program_scoped_subject_matches_nothing_directly():
    """Fan-out first: the derivation resolves scope to partitions and emits children; the
    matcher refusing program-scoped subjects is what makes silent pooling impossible."""
    program_scoped = QuestionSubject(kind="campaign", scope_selector={"sample": "s1"})
    payload = scene([footprint("e:1:d", 2.0)], observations=[observation("e:1:d")])
    assert match_subject(program_scoped, payload).scan_refs == ()
    assert resolve_scope(program_scoped, payload) == (P1,)


def test_scope_resolution_separates_two_temperatures():
    """One program region over two condition partitions ⇒ two children, never one pooled
    assessment; a selector no partition satisfies resolves to nothing (fail closed upstream)."""
    payload = scene(
        [footprint("e:1:d", 2.0, partition=P1), footprint("e:2:d", 2.0, partition=P2)],
        observations=[observation("e:1:d"), observation("e:2:d")])
    program_scoped = QuestionSubject(kind="campaign", scope_selector={"sample": "s1"})
    assert resolve_scope(program_scoped, payload) == (P1, P2)
    assert scene_partitions(payload) == (P1, P2)
    foreign = QuestionSubject(kind="campaign", scope_selector={"sample": "s9"})
    assert resolve_scope(foreign, payload) == ()


def test_a_subject_is_partition_specific_or_program_scoped_never_both():
    with pytest.raises(ValueError, match="never both"):
        QuestionSubject(kind="campaign", partition_id=P1, scope_selector={"sample": "s1"})


# ----------------------------------------------------------------------------
# Assessments, blockers, exhaustion
# ----------------------------------------------------------------------------
def test_snapshot_statuses_exclude_history_transitions():
    for status in ("overtaken", "dissolved", "reopened"):
        with pytest.raises(ValueError, match="history"):
            QuestionAssessment(question_id="q", status=status)
    open_assessment = QuestionAssessment(question_id="q", status="open")
    assert open_assessment.durable_blockers() == ()


def test_an_answered_assessment_carries_its_answer():
    with pytest.raises(ValueError, match="must carry its answer"):
        QuestionAssessment(question_id="q", status="answered")


def test_blockers_are_typed_and_unclassified_names_refuse():
    assessment = QuestionAssessment(
        question_id="q", status="open",
        blockers=(Blocker("bose_dead", "T=4K"), Blocker("quarantined_scan"),
                  Blocker("within_candidate_uncertainty_unavailable")))
    assert [b.name for b in assessment.durable_blockers()] == ["bose_dead"]
    with pytest.raises(ValueError, match="unclassified blocker"):
        Blocker("something_new")


def test_exhaustion_is_evidence_not_a_boolean():
    proof = QuestionExhaustion(
        question_id="q", basis_agenda_digest="c" * 64, scene_digest="d" * 64,
        generator_registry_fingerprint="r1",
        required_generator_ids=("presence_probe",),
        generator_status={"presence_probe": "exhausted"})
    assert proof.exhausted() is True
    partial = QuestionExhaustion(
        question_id="q", basis_agenda_digest="c" * 64, scene_digest="d" * 64,
        generator_registry_fingerprint="r1",
        required_generator_ids=("presence_probe", "identity_discriminator"),
        generator_status={"presence_probe": "exhausted"})
    assert partial.exhausted() is False
    # No required generators is NOT exhaustion: nothing was proved about anything.
    empty = QuestionExhaustion(question_id="q", basis_agenda_digest="c" * 64,
                               scene_digest="d" * 64, generator_registry_fingerprint="r1")
    assert empty.exhausted() is False


# ----------------------------------------------------------------------------
# Dependency DAG
# ----------------------------------------------------------------------------
def spec_with_deps(claim, deps):
    return QuestionSpec(family="ConfirmPresence", claim_kind=claim, subject=subject(),
                        predicate=PREDICATE, dependencies=deps)


def test_a_self_dependency_refuses():
    a = spec_with_deps("claim_a", ())
    with pytest.raises(ValueError, match="dependency cycle"):
        validate_dependency_dag([spec_with_deps("claim_a", (a.question_id,))])


def test_a_dependency_cycle_refuses():
    a = spec_with_deps("claim_a", ())
    b = spec_with_deps("claim_b", (a.question_id,))
    a_cyclic = spec_with_deps("claim_a", (b.question_id,))
    with pytest.raises(ValueError, match="dependency cycle"):
        validate_dependency_dag([a_cyclic, b])
    validate_dependency_dag([a, b])              # acyclic: fine


def test_a_dependency_on_an_absent_question_is_legal():
    validate_dependency_dag([spec_with_deps("claim_a", ("0" * 64,))])
