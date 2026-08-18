"""The cold-start declared partition (SCENE-4 PR 4.3, C3 / D4).

One scenario per acceptance item in the plan's C3 bullet: a resolved declaration fires anchored
openers instead of going inert, blank knowledge still surveys, an unresolvable declared condition
refuses by name, the first real measurement lands on the identical key, the persisted record
replays it, and two identical calls stay byte-identical.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from isar.drive.steering.cold_start import (
    COLD_START_CONDITION_UNRESOLVED, ColdStartConditionUnresolved, DeclaredPartition,
    declare_partition)
from isar.drive.steering.planner import NO_ELIGIBLE_ACTION, SELECTED, plan_next_action
from isar.schema.campaign_program import CampaignProgram, CampaignStep, DiscoverAndMapSpec
from isar.schema.measurement import MeasurementArena
from isar.schema.question_policy import resolve_policy
from isar.scene.build import _partition_key
from isar.scene.model import (canonical_bytes, digest_bytes, partition_id,
                              path_geometry_fingerprint)
from isar.scene.store import SCENE_STORE
from isar.workspace_api.scene import _synthetic_observation

T_ROUND = 5.0

#: The path ``discover_step()`` declares. Every arena a 4.3 campaign delivers carries its
#: step's path (``_campaign_provenance_block``, and ``MeasurementArena`` REFUSES a
#: ``canonical_path`` arena without one), so a fixture observation that omits it is not a
#: workspace this build can produce.
STEP_PATH = path_geometry_fingerprint((2.0, 0.0, 0.0), (3.0, 0.0, 0.0))


# ----------------------------------------------------------------------------
# Builders (deliberately local: this file must stay readable on its own)
# ----------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _SceneRevision:
    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "_SceneRevision":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))


def scene(sample_knowledge=None, t_round=T_ROUND, measurements=()):
    """A workspace with no measurement by default: declarations only. ``measurements`` are
    ``(observation, footprint)`` pairs from :func:`measured`, for the warm-workspace cases."""
    return {"observations": [o for o, _ in measurements],
            "footprints": [f for _, f in measurements if f is not None],
            "clusters": [], "non_scoring": [],
            "sample_knowledge": sample_knowledge or {}, "condition_t_round": t_round}


def discover_step(step_id="discover-1", q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0),
                  spacing=1.0):
    spec = DiscoverAndMapSpec(
        q_start=q_start, q_end=q_end, maximum_initial_spacing_rlu=spacing,
        intrinsic_omega_domain_meV=(0.0, 10.0),
        catalogue_profile="isar.catalogue.default/1",
        resolved_policy=resolve_policy(user_ask={"min_effective_exposure": 1.0}))
    return CampaignStep(id=step_id, kind="discover_and_map", payload=spec)


def declared(step, *, sample="s1", temperature=300.0, epoch=1, t_round=T_ROUND):
    conditions = () if temperature is None else (("temperature_K", temperature),)
    return declare_partition(step, sample=sample, conditions=conditions, lattice_epoch=epoch,
                             condition_t_round=t_round)


def measured(scan_ref, partition_id, *, program_step, epoch=1, coord=2.0, count_trust="trusted",
             path_fingerprint=STEP_PATH, topology_role="canonical_path"):
    """One observation + its footprint, the two-record pair the agenda joins by ``scan_ref``.

    The retirement key is ``(program_step, path_fingerprint)``: a TRUSTED observation carrying the
    declaring step's id AND its path is what retires the declaration (H20(d), amended 2026-08-18).
    Both default to the canonical step's, because that is what a delivered arena always carries —
    an observation with a step id but no path is not a workspace this build can write, and a
    fixture that omitted it made the step-id half look sufficient when it is not.

    An untrusted observation gets NO footprint, exactly as ``isar.scene.build`` writes it — the
    footprint set is built from the trusted observations alone.
    """
    observation = {"scan_ref": scan_ref, "candidates": [], "count_trust": count_trust,
                   "sample": "s1", "lattice_epoch": epoch, "conditions": [], "best_label": None,
                   "program_step": program_step, "path_fingerprint": path_fingerprint,
                   "topology_role": topology_role}
    if count_trust != "trusted":
        return (observation, None)
    return (observation,
            {"scan_ref": scan_ref, "axis_coordinate": coord, "partition_id": partition_id,
             "effective_exposure": 5.0,
             "bands": [{"lo": 0.0, "hi": 10.0, "sides": "stokes"}]})


def derive(step, declared_partition=None, **scene_kw):
    """``derive_agenda`` against one frozen step — the fan-out rules below are ITS rules, and
    driving them through the whole planner would only add stores between the input and the
    verdict."""
    from isar.scene.agenda import derive_agenda
    return derive_agenda(scene(**scene_kw), step.payload.to_dict(), step_id=step.id,
                         program_fingerprint="prog-1",
                         declared_partition=declared_partition)


def write_scene(report_dir: Path, payload: dict) -> None:
    SCENE_STORE.write_revision(report_dir, _SceneRevision.of(payload))


def decision_lines(report_dir: Path) -> list[dict]:
    path = report_dir / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def agenda_payload(report_dir: Path) -> dict:
    from isar.scene.agenda import AGENDA_STORE
    return AGENDA_STORE.read_current(report_dir)[1]


def inventory_evaluations(report_dir: Path, ref: dict) -> list[dict]:
    blob = json.loads(Path(report_dir, *ref["path"].split("/")[1:]).read_text(encoding="utf-8"))
    return blob["evaluations"]


# ----------------------------------------------------------------------------
# (a) A resolved declaration fires the anchored openers instead of going inert
# ----------------------------------------------------------------------------
def test_declared_partition_opens_anchored_specs_and_seeds_the_first_node(tmp_path):
    step = discover_step()                                    # region 2.0..3.0, spacing 1.0
    program = CampaignProgram(steps=(step,))
    write_scene(tmp_path, scene(sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1}))
    partition = declared(step)

    result = plan_next_action(tmp_path, program, "discover-1", declared_partition=partition)
    assert result.outcome == SELECTED                          # not the unfanned INAPPLICABLE lane

    payload = agenda_payload(tmp_path)
    notes = {name for name, _ in payload["derivation_notes"]}
    assert "anchored_openers_inert" not in notes               # the declared epoch reached the gate
    assert "predetermined_openers_inert" not in notes
    anchored = [s for s in payload["specs"] if s["family"] == "ConfirmPresence"]
    assert [s["subject"]["anchor"] for s in anchored] == [2.0, 3.0]
    assert {s["subject"]["partition_id"] for s in anchored} == {partition.partition_id}

    # The region is no wider than the spacing, so the opening book takes its seed path: the
    # nearest declared in-region anchor to the midpoint (2.0, ties to the smaller), NEVER the
    # bare midpoint 2.5 an epoch-inert partition would have fallen back to.
    survey = [e for e in inventory_evaluations(tmp_path, result.inventory_ref)
              if e["generator"] == "opening_book_survey"]
    assert [e["intent"]["hkl"] for e in survey] == [[2.0, 0.0, 0.0]]

    # And no generator answered the program-scoped refusal.
    reasons = {o.inapplicable_reason for r in result.run_records for o in r.generator_outcomes}
    assert "program_scoped_subject_unfanned" not in reasons


def test_without_a_declaration_cold_start_is_unchanged(tmp_path):
    """The 4.2b arm, pinned beside its new sibling: no declaration, no fan-out, no fault."""
    step = discover_step()
    program = CampaignProgram(steps=(step,))
    write_scene(tmp_path, scene(sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1}))

    result = plan_next_action(tmp_path, program, "discover-1")
    assert result.outcome == NO_ELIGIBLE_ACTION
    reasons = {o.inapplicable_reason for r in result.run_records for o in r.generator_outcomes}
    assert "program_scoped_subject_unfanned" in reasons
    assert "declared_partition" not in decision_lines(tmp_path)[-1]["decision_basis"]


# ----------------------------------------------------------------------------
# (b) Blank knowledge degrades to the uniform survey, never to an error
# ----------------------------------------------------------------------------
def test_blank_knowledge_still_proposes_a_uniform_survey(tmp_path):
    step = discover_step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.25)
    program = CampaignProgram(steps=(step,))
    write_scene(tmp_path, scene(sample_knowledge={}))          # no zone period, no epoch
    partition = declared(step, epoch=None)

    result = plan_next_action(tmp_path, program, "discover-1", declared_partition=partition)
    assert result.outcome == SELECTED

    notes = {name for name, _ in agenda_payload(tmp_path)["derivation_notes"]}
    assert "predetermined_openers_inert" in notes              # inert, and it SAYS so
    survey = [e for e in inventory_evaluations(tmp_path, result.inventory_ref)
              if e["generator"] == "opening_book_survey"]
    # Three evenly spaced interior nodes across a 1.0 region at 0.25 spacing — the plain
    # geometric fill, in geometric order because no anchor weight exists to reorder it.
    assert [e["intent"]["hkl"][0] for e in survey] == [2.25, 2.5, 2.75]


# ----------------------------------------------------------------------------
# (c) An unresolvable declared condition refuses by name, minting nothing
# ----------------------------------------------------------------------------
def test_unresolved_declared_condition_refuses(tmp_path):
    step = discover_step()
    with pytest.raises(ColdStartConditionUnresolved) as excinfo:
        declare_partition(step, sample="s1", conditions=(("temperature_K", 300.0),),
                          lattice_epoch=1, condition_t_round=None)
    assert COLD_START_CONDITION_UNRESOLVED in str(excinfo.value)
    assert ColdStartConditionUnresolved.name == COLD_START_CONDITION_UNRESOLVED

    # A declaration with NO condition at all is resolvable and still mints (the coherent
    # shared-state arm), so the refusal is about unbinnable declarations, not about silence.
    assert declare_partition(step, sample="s1", lattice_epoch=1).partition_id


def test_replay_of_an_unresolvable_record_refuses_too():
    """The replay path is checked by the SAME rule, not trusted: a hand-edited or corrupted
    record whose t_round has gone null would otherwise mint a scan_ref-salted orphan key
    silently, on the one path nobody is watching."""
    good = declared(discover_step()).to_dict()
    assert DeclaredPartition.from_dict(good).partition_id       # the round trip itself is fine
    with pytest.raises(ColdStartConditionUnresolved):
        DeclaredPartition.from_dict({**good, "condition_t_round": None})


# ----------------------------------------------------------------------------
# (d) The first real measurement lands on the identical key
# ----------------------------------------------------------------------------
def test_first_measurement_lands_on_the_declared_key():
    step = discover_step()
    partition = declared(step)

    record = MeasurementArena(
        scan_ref="e:1:d", scan_id="e:1:d", scan_number=1, detector="d", sample="s1",
        conditions=(("temperature_K", 300.0),), q=(2.0, 0.0, 0.0), lattice_epoch=1,
        energy_window=(0.0, 10.0), energy_bands=((0.0, 10.0),), effective_exposure=5.0,
        exposure_provenance=None, count_trust="trusted", reasons=(),
        written_at="2026-08-18T00:00:00+00:00", content_hash="c0",
        program_fingerprint="pf", program_step=step.id, topology_role="canonical_path",
        path_fingerprint=partition.path_fingerprint)

    # Through the measured-provenance path: record -> scene adapter -> builder's partition key.
    measured = _partition_key(_synthetic_observation(record), T_ROUND)
    assert measured == partition.key
    assert partition_id(measured) == partition.partition_id

    # A DIFFERENT declared path is a different partition — the key is not accidentally coarse.
    other = declared(discover_step(q_start=(2.0, 0.0, 0.0), q_end=(4.0, 0.0, 0.0)))
    assert other.partition_id != partition.partition_id


def test_the_two_producers_of_a_partition_key_agree_from_one_card(tmp_path):
    """The declaration and the delivered arena are built by DIFFERENT layers from DIFFERENT
    inputs; this pins them against each other, from ONE card.

    ``test_first_measurement_lands_on_the_declared_key`` above hands the same literals to both
    sides, so it proves ``partition_key`` is deterministic and nothing more. It passed throughout
    the window in which the runner minted ``sample`` from the CAMPAIGN CONFIG — whose default is a
    synthetic name the CLI never overrides and no card is obliged to match — and ``conditions``
    from a ``[temperature_rule] setpoint`` key two of the four card templates do not carry. The
    declared partition could not join its own measurement on any real workspace, and no test could
    tell, because every test wrote both sides by hand.
    """
    from isar.drive.program_runner import _declared_conditions, _declared_sample
    from isar.schema.card import Card
    from isar.session.driver import _sample_of
    from synthetic_ws import DISCOVERY_POLICY_TOML, write_policy, write_state

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    write_policy(root, DISCOVERY_POLICY_TOML)
    write_state(root, lattice_epochs=[{"version": 1}])
    card = Card.load(root / "card.policy.toml", root / "card.state.json")
    step = discover_step()

    partition = declare_partition(
        step, sample=_declared_sample(card, step), conditions=_declared_conditions(card, step),
        lattice_epoch=1,
        condition_t_round=float(dict(card.policy.collation or {}).get("t_round")))

    # The DELIVERY side, through the real producer. The record's experiment_id is deliberately NOT
    # the card's sample name: `_sample_of` prefers the card, and the declaration has to land on
    # that same branch rather than on some third source of its own.
    class _Rec:
        experiment_id = "experiment-id-that-is-not-the-sample-name"

    arena_sample = _sample_of(_Rec(), card)
    assert arena_sample != _Rec.experiment_id, "the card must win, or this test proves nothing"

    # `_measurement_record` writes the MEASURED temperature, and the card's binning is what
    # absorbs the drift between a setpoint and the regulation around it — so a readback 2 K off
    # the declared 300 K setpoint must still land in the declared bin.
    for readback in (300.0, 302.0):
        record = MeasurementArena(
            scan_ref="e:1:d", scan_id="e:1:d", scan_number=1, detector="d", sample=arena_sample,
            conditions=(("temperature_K", readback),), q=(2.0, 0.0, 0.0), lattice_epoch=1,
            energy_window=(0.0, 10.0), energy_bands=((0.0, 10.0),), effective_exposure=5.0,
            exposure_provenance=None, count_trust="trusted", reasons=(),
            written_at="2026-08-18T00:00:00+00:00", content_hash="c0",
            program_fingerprint="pf", program_step=step.id, topology_role="canonical_path",
            path_fingerprint=partition.path_fingerprint)
        assert _partition_key(_synthetic_observation(record), T_ROUND) == partition.key


def test_a_card_that_cannot_name_its_sample_refuses_rather_than_guessing(tmp_path):
    """Cold start cannot use ``_sample_of``'s ``record.experiment_id`` fallback — no record exists
    yet — so a card without a ``[sample]`` name has no knowable sample identity to declare."""
    from isar.drive.program_runner import WorkspaceUndeclared, _declared_sample
    from isar.schema.card import Card
    from synthetic_ws import write_policy, write_state

    root = tmp_path / "ws"
    root.mkdir(parents=True)
    write_policy(root)                      # the base card: no [sample] table
    write_state(root, lattice_epochs=[{"version": 1}])
    card = Card.load(root / "card.policy.toml", root / "card.state.json")
    with pytest.raises(WorkspaceUndeclared, match=r"no \[sample\] name"):
        _declared_sample(card, discover_step())


def test_a_setpoint_field_only_card_refuses_and_a_temperatureless_one_declares_nothing(tmp_path):
    """The middle card shape is the one that mattered: s30 and in8 name only WHERE the setpoint
    lives, which says a temperature dimension exists without saying what value the first scan will
    land on. Declaring no condition there would put the declaration in the undeclared-couples-only-
    with-undeclared lane while every arena lands in the binned one."""
    from isar.drive.program_runner import WorkspaceUndeclared, _declared_conditions
    from isar.schema.card import Card
    from synthetic_ws import _POLICY_TOML, write_policy, write_state

    def card_with(setpoint_line: str, name: str):
        root = tmp_path / name
        root.mkdir(parents=True)
        write_policy(root, _POLICY_TOML.replace("setpoint = 300.0", setpoint_line))
        write_state(root, lattice_epochs=[{"version": 1}])
        return Card.load(root / "card.policy.toml", root / "card.state.json")

    step = discover_step()
    with pytest.raises(WorkspaceUndeclared, match="no literal `setpoint`"):
        _declared_conditions(card_with('setpoint_field = "T_Control"', "field_only"), step)
    # No temperature dimension declared at all: `()` is exactly what a T-less arena carries.
    assert _declared_conditions(card_with("", "no_rule"), step) == ()


# ----------------------------------------------------------------------------
# (e) Replay reproduces the key from the persisted record, not from the card
# ----------------------------------------------------------------------------
def test_decision_record_replays_the_declared_partition(tmp_path):
    step = discover_step()
    program = CampaignProgram(steps=(step,))
    write_scene(tmp_path, scene(sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1}))
    partition = declared(step)

    plan_next_action(tmp_path, program, "discover-1", declared_partition=partition)
    embedded = decision_lines(tmp_path)[-1]["decision_basis"]["declared_partition"]
    assert embedded == partition.to_dict()                     # full components, not just the id

    replayed = DeclaredPartition.from_dict(embedded)
    assert replayed.key == partition.key                       # recomputed, never copied
    assert replayed.partition_id == partition.partition_id


# ----------------------------------------------------------------------------
# (f) Two identical calls append byte-identical decision lines
# ----------------------------------------------------------------------------
def test_double_run_with_a_declaration_is_byte_identical(tmp_path):
    step = discover_step()
    program = CampaignProgram(steps=(step,))
    write_scene(tmp_path, scene(sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1}))
    partition = declared(step)

    first = plan_next_action(tmp_path, program, "discover-1", declared_partition=partition)
    second = plan_next_action(tmp_path, program, "discover-1", declared_partition=partition)
    assert first.agenda_digest == second.agenda_digest
    assert first.selection_fingerprint == second.selection_fingerprint

    lines = (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0] == lines[1]
    assert json.loads(lines[0])["decision_basis"]["declared_partition"]["partition_id"] \
        == partition.partition_id


# ----------------------------------------------------------------------------
# (g) Observation wins the epoch map: the seed is a setdefault, never an override
# ----------------------------------------------------------------------------
def test_an_observed_epoch_overrides_the_declared_one():
    """The declaration is still live (its own step has measured nothing), but an observation
    already ATTRIBUTES the declared partition to lattice epoch 2 while the declaration claims 1.
    The seed must lose: the anchored openers go inert on the observed frame, exactly as they
    would for any other epoch-foreign partition."""
    step = discover_step()
    partition = declared(step, epoch=1)
    other_step = measured("e:1:d", partition.partition_id, program_step="discover-0", epoch=2)

    revision = derive(step, (partition.partition_id, 1, step.id),
                      sample_knowledge={"zone_period": 1.0, "lattice_epoch": 1},
                      measurements=[other_step])

    notes = {name for name, _ in revision.payload["derivation_notes"]}
    assert "anchored_openers_inert" in notes
    assert not [s for s in revision.payload["specs"]
                if s["family"] == "ConfirmPresence" and s["subject"]["anchor"] is not None]


# ----------------------------------------------------------------------------
# (h) The warm-workspace retirement rule (owner ruling 2026-08-18)
# ----------------------------------------------------------------------------
def test_the_declaration_is_live_until_its_own_step_measures_then_retires():
    step = discover_step()                                     # the DECLARING step
    partition = declared(step)
    frame = (partition.partition_id, 1, step.id)
    knowledge = {"zone_period": 1.0, "lattice_epoch": 1}
    foreign = measured("e:1:d", "P-foreign", program_step="discover-0")

    # Another step has measured, in another partition. That says nothing about where THIS
    # step's first scan will land, so the declaration still joins the fan-out — as a UNION with
    # the observed partition, not in place of it — and its anchors fire.
    live = derive(step, frame, sample_knowledge=knowledge, measurements=[foreign])
    anchored = [s for s in live.payload["specs"]
                if s["family"] == "ConfirmPresence" and s["subject"]["anchor"] is not None]
    assert {s["subject"]["partition_id"] for s in anchored} == {partition.partition_id,
                                                               "P-foreign"}
    assert sorted({s["subject"]["anchor"] for s in anchored}) == [2.0, 3.0]
    assert {s["subject"]["partition_id"] for s in live.payload["specs"]
            if s["family"] == "DiscoverRegion"} == {partition.partition_id, "P-foreign"}

    # Now the declaring step has produced ONE observation — and it landed somewhere else
    # entirely. Cold start is over: reality owns the fan-out, and the declared partition is
    # gone from it rather than lingering as a phantom nothing will ever measure.
    own = measured("e:2:d", "P-foreign", program_step=step.id, coord=2.5)
    retired = derive(step, frame, sample_knowledge=knowledge, measurements=[foreign, own])
    assert {s["subject"]["partition_id"] for s in retired.payload["specs"]
            if s["subject"]["partition_id"] is not None} == {"P-foreign"}

    # And the retirement is the ONLY difference: dropping the declaration entirely now derives
    # the identical agenda.
    assert retired.payload == derive(step, None, sample_knowledge=knowledge,
                                     measurements=[foreign, own]).payload


# ----------------------------------------------------------------------------
# (i) A BROKEN first delivery does not retire the declaration (verifier A1)
# ----------------------------------------------------------------------------
def test_an_untrusted_own_step_observation_does_not_retire_the_declaration():
    """A broken scan is not "taking a measurement" in the initialization sense of the ruling.

    It carries no footprint and says nothing about where this step's scans land, so retiring on
    it would leave the campaign with neither declared nor observed knowledge — the uniform survey,
    at exactly the moment the declaration is the only knowledge there is. The declaration stays
    live until a TRUSTED own-step observation lands.
    """
    step = discover_step()
    partition = declared(step)
    frame = (partition.partition_id, 1, step.id)
    knowledge = {"zone_period": 1.0, "lattice_epoch": 1}
    broken = measured("e:1:d", partition.partition_id, program_step=step.id,
                      count_trust="excluded")

    live = derive(step, frame, sample_knowledge=knowledge, measurements=[broken])
    anchored = [s for s in live.payload["specs"]
                if s["family"] == "ConfirmPresence" and s["subject"]["anchor"] is not None]
    # The anchors fire on the DECLARED partition — not on the program-scoped ``None`` lane a
    # retired declaration would have left behind.
    assert {s["subject"]["partition_id"] for s in anchored} == {partition.partition_id}
    assert sorted({s["subject"]["anchor"] for s in anchored}) == [2.0, 3.0]

    # And the very next delivery, trusted, retires it.
    good = measured("e:2:d", "P-real", program_step=step.id, coord=2.5)
    retired = derive(step, frame, sample_knowledge=knowledge, measurements=[broken, good])
    assert {s["subject"]["partition_id"] for s in retired.payload["specs"]
            if s["subject"]["partition_id"] is not None} == {"P-real"}


# ----------------------------------------------------------------------------
# (j) The retirement identity is (step, PATH) — review finding, amends H20(d)
# ----------------------------------------------------------------------------
def test_a_reused_step_id_on_another_path_does_not_retire_the_declaration():
    """``discover-1`` is the id nearly every authored program uses.

    An older program's trusted observation carrying that same id — but measuring a DIFFERENT
    trajectory — used to retire this program's declaration before its first scan, blanking the
    declared knowledge at exactly the moment it was the only knowledge there was. The path is what
    tells the two apart, and it survives a consultation recompile, so it can never retire a
    declaration for an administrative reason.
    """
    step = discover_step()
    partition = declared(step)
    frame = (partition.partition_id, 1, step.id, partition.path_fingerprint)
    knowledge = {"zone_period": 1.0, "lattice_epoch": 1}
    other_path = path_geometry_fingerprint((7.0, 0.0, 0.0), (8.0, 0.0, 0.0))
    namesake = measured("e:1:d", "P-old", program_step=step.id, path_fingerprint=other_path)

    live = derive(step, frame, sample_knowledge=knowledge, measurements=[namesake])
    assert partition.partition_id in {s["subject"]["partition_id"]
                                      for s in live.payload["specs"]}, (
        "an unrelated program reusing the step id retired this declaration")

    # The SAME id on THIS path is the real thing, and it does retire.
    own = measured("e:2:d", "P-real", program_step=step.id, coord=2.5)
    retired = derive(step, frame, sample_knowledge=knowledge, measurements=[namesake, own])
    assert partition.partition_id not in {s["subject"]["partition_id"]
                                          for s in retired.payload["specs"]}


# ----------------------------------------------------------------------------
# (k) A step only opens questions it can WRITE INTO — review finding
# ----------------------------------------------------------------------------
def test_a_partition_on_another_path_is_never_fanned_into():
    """Every arena this step delivers is stamped with THIS step's path, so a question scoped to a
    partition on a different path can never be closed by anything the step measures: it would be
    selected, delivered into the current partition, left open, and re-selected — an unsatisfiable
    loop that spends budget without moving a question.

    A partition with NO declared path stays in: that is the legacy / manual / pooled-master lane,
    and excluding it would blank the agenda of every workspace predating campaign provenance.
    """
    step = discover_step()
    knowledge = {"zone_period": 1.0, "lattice_epoch": 1}
    other_path = path_geometry_fingerprint((7.0, 0.0, 0.0), (8.0, 0.0, 0.0))
    elsewhere = measured("e:1:d", "P-otherpath", program_step="discover-9",
                         path_fingerprint=other_path)
    legacy = measured("e:2:d", "P-legacy", program_step=None, coord=2.5,
                      path_fingerprint=None, topology_role=None)
    mine = measured("e:3:d", "P-mine", program_step=step.id, coord=2.2)

    revision = derive(step, None, sample_knowledge=knowledge,
                      measurements=[elsewhere, legacy, mine])
    scoped = {s["subject"]["partition_id"] for s in revision.payload["specs"]
              if s["subject"]["partition_id"] is not None}
    assert "P-otherpath" not in scoped, "fanned into a partition this step cannot write into"
    assert {"P-mine", "P-legacy"} <= scoped
