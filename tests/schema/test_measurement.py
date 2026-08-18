"""MeasurementArena validation (SCENE-4 PR 4.0 review response, W2): the record must not be able
to encode nonsense — an unknown count_trust, a duplicate condition name, a non-finite geometry
value, or an inverted band — since nothing downstream re-checks it once it is on disk."""
from __future__ import annotations

import json

import pytest

from isar.schema.measurement import (CAMPAIGN_PROVENANCE_FIELDS, COUNT_TRUST_STATES,
                                     TOPOLOGY_ROLES, MeasurementArena, campaign_provenance)
from isar.session import measurement_store


def _record(**over) -> dict:
    base = dict(scan_ref="e:1:d", scan_id="e:1:d", scan_number=1, detector="d", sample="s",
                conditions=(("temperature_K", 200.0),), q=(1.0, 0.0, 0.0), lattice_epoch=None,
                energy_window=(-5.0, 5.0), energy_bands=((-5.0, 5.0),), effective_exposure=1000.0,
                exposure_provenance=None, count_trust="trusted", reasons=(),
                written_at="2026-01-01T00:00:00+00:00", content_hash="ch")
    base.update(over)
    return base


def test_attempted_joins_the_shared_vocabulary():
    assert COUNT_TRUST_STATES == (
        "trusted", "excluded", "held_for_calibration", "undersampled", "attempted")
    MeasurementArena(**_record(count_trust="attempted"))  # does not raise


@pytest.mark.parametrize("factory", [
    lambda: MeasurementArena(**_record(count_trust="bogus")),
    lambda: MeasurementArena.from_dict({**_record(count_trust="bogus"),
                                        "schema": "isar.measurement-arena/1"}),
])
def test_an_unknown_count_trust_is_rejected(factory):
    with pytest.raises(ValueError, match="count_trust"):
        factory()


@pytest.mark.parametrize("factory", [
    lambda: MeasurementArena(**_record(conditions=(("temperature_K", 200.0),
                                                    ("temperature_K", 210.0)))),
    lambda: MeasurementArena.from_dict({**_record(), "schema": "isar.measurement-arena/1",
                                        "conditions": [["temperature_K", 200.0],
                                                       ["temperature_K", 210.0]]}),
])
def test_duplicate_condition_names_are_rejected(factory):
    with pytest.raises(ValueError, match="duplicate condition"):
        factory()


def test_non_finite_q_is_rejected():
    with pytest.raises(ValueError, match="non-finite"):
        MeasurementArena(**_record(q=(float("nan"), 0.0, 0.0)))


def test_non_finite_exposure_is_rejected():
    with pytest.raises(ValueError, match="not finite"):
        MeasurementArena(**_record(effective_exposure=float("inf")))


def test_non_finite_condition_value_is_rejected():
    with pytest.raises(ValueError, match="non-finite"):
        MeasurementArena(**_record(conditions=(("temperature_K", float("nan")),)))


def test_non_finite_energy_window_is_rejected():
    with pytest.raises(ValueError, match="non-finite"):
        MeasurementArena(**_record(energy_window=(float("nan"), 5.0)))


def test_an_inverted_energy_window_is_rejected():
    with pytest.raises(ValueError, match="energy_window"):
        MeasurementArena(**_record(energy_window=(5.0, 1.0)))


@pytest.mark.parametrize("bands", [
    ((float("nan"), 5.0),),
    ((5.0, 1.0),),          # lo > hi
])
def test_malformed_energy_bands_are_rejected(bands):
    with pytest.raises(ValueError, match="energy_bands"):
        MeasurementArena(**_record(energy_bands=bands))


def test_content_hash_is_required_on_construction_and_from_dict():
    with pytest.raises(TypeError):
        MeasurementArena(**{k: v for k, v in _record().items() if k != "content_hash"})
    with pytest.raises(KeyError):
        MeasurementArena.from_dict(
            {k: v for k, v in {**_record(), "schema": "isar.measurement-arena/1"}.items()
             if k != "content_hash"})


def test_program_fields_default_none_and_round_trip():
    rec = MeasurementArena(**_record(program_fingerprint="pf", program_step="s1"))
    assert (rec.program_fingerprint, rec.program_step) == ("pf", "s1")
    back = MeasurementArena.from_dict(rec.to_dict())
    assert (back.program_fingerprint, back.program_step) == ("pf", "s1")
    assert MeasurementArena(**_record()).program_fingerprint is None


def test_the_campaign_provenance_block_round_trips_through_the_jsonl(tmp_path):
    """SCENE-4 PR 4.3 (D2/D3): all four provenance fields survive writer -> reader verbatim."""
    rec = MeasurementArena(**_record(program_fingerprint="pf", program_step="s1",
                                     topology_role="canonical_path", path_fingerprint="pathfp"))
    measurement_store.append_measurement(tmp_path, rec.to_dict())
    line = (tmp_path / measurement_store.FILENAME).read_text(encoding="utf-8").strip()
    back = MeasurementArena.from_dict(json.loads(line))
    assert back == rec
    assert (back.topology_role, back.path_fingerprint) == ("canonical_path", "pathfp")


def test_an_old_record_without_the_provenance_fields_reads_none():
    """H1: no compat shim — an absent field is simply absent, never a defaulted campaign identity."""
    old = {**_record(), "schema": "isar.measurement-arena/1"}
    assert "topology_role" not in old and "path_fingerprint" not in old
    back = MeasurementArena.from_dict(old)
    assert (back.topology_role, back.path_fingerprint,
            back.program_fingerprint, back.program_step) == (None, None, None, None)


@pytest.mark.parametrize("factory", [
    lambda: MeasurementArena(**_record(topology_role="off_map")),
    lambda: MeasurementArena.from_dict({**_record(topology_role="off_map"),
                                        "schema": "isar.measurement-arena/1"}),
])
def test_a_present_but_unknown_topology_role_refuses_loudly(factory):
    with pytest.raises(ValueError, match="topology_role"):
        factory()


@pytest.mark.parametrize("role", TOPOLOGY_ROLES)
def test_every_vocabulary_topology_role_is_legal(role):
    # "canonical_path" must name the path it is on (below); every other role need not.
    assert MeasurementArena(**_record(topology_role=role,
                                      path_fingerprint="pathfp")).topology_role == role


@pytest.mark.parametrize("factory", [
    lambda: MeasurementArena(**_record(topology_role="canonical_path")),
    lambda: MeasurementArena.from_dict({**_record(topology_role="canonical_path"),
                                        "schema": "isar.measurement-arena/1"}),
])
def test_a_canonical_path_arena_without_a_path_fingerprint_refuses_loudly(factory):
    """H1: partition_key would silently drop such an arena into the provenance-less lane, pooling
    it with arenas from every other path at the same sample/conditions/epoch."""
    with pytest.raises(ValueError, match="canonical_path.*path_fingerprint"):
        factory()


def test_a_context_probe_without_a_path_fingerprint_stays_legal():
    """An off-path diagnostic declares no path — nothing to name."""
    rec = MeasurementArena(**_record(topology_role="context_probe"))
    assert (rec.topology_role, rec.path_fingerprint) == ("context_probe", None)


def test_the_scene_layer_reads_the_same_topology_vocabulary():
    """One source of truth: the scene DTO re-exports the record schema's tuple, so a producer and
    a consumer can never diverge on the legal strings."""
    from isar.scene import model as scene_model
    assert scene_model.TOPOLOGY_ROLES is TOPOLOGY_ROLES


def test_campaign_provenance_reads_a_request_payload_and_tolerates_none():
    payload = {"job_id": "j1", "topology_role": "context_probe", "program_fingerprint": "pf",
               "program_step": "s1", "path_fingerprint": "pathfp"}
    assert campaign_provenance(payload) == {
        "topology_role": "context_probe", "program_fingerprint": "pf",
        "program_step": "s1", "path_fingerprint": "pathfp"}
    assert campaign_provenance(None) == dict.fromkeys(CAMPAIGN_PROVENANCE_FIELDS)


def test_the_writer_rejects_a_non_finite_payload_outright(tmp_path):
    """W2: append_measurement encodes strict JSON (allow_nan=False), mirroring
    isar.scene.model.canonical_bytes — a non-finite value must not silently persist as the
    non-standard NaN/Infinity token a strict reader later refuses."""
    with pytest.raises(ValueError):
        measurement_store.append_measurement(tmp_path, {"x": float("nan")})


def test_the_writer_round_trips_a_real_record(tmp_path):
    rec = MeasurementArena(**_record())
    measurement_store.append_measurement(tmp_path, rec.to_dict())
    line = (tmp_path / measurement_store.FILENAME).read_text(encoding="utf-8").strip()
    assert MeasurementArena.from_dict(json.loads(line)) == rec
