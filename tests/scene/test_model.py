"""Pins the scene layer's shared vocabulary (isar/scene/model.py).

Three things are load-bearing here and nothing else in the layer re-checks them: the DTOs
are immutable and survive the JSON boundary unchanged (tuples would silently become lists),
the signed→intrinsic window fold is exactly the geometry SCENE_DESIGN specifies, and the
fingerprint depends on the observation's content rather than on the order its JSON payload
happened to be built in — while candidate order IS content, and is pinned as such.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from isar.scene.model import (
    COUNT_TRUST_STATES, TOPOLOGY_ROLES, CandidateRow, ComponentDescriptor, ConditionSet, Footprint,
    FootprintBand, ModeInterval, SceneObservation, Visibility, canonical_bytes, covers, fingerprint,
    footprint_of, observation_fingerprint, visibility_of,
)


def _mode_interval(**over) -> ModeInterval:
    base = dict(low=4.1, center=4.25, high=4.4, nominal_level=0.68,
                interval_kind="parametric_bootstrap_percentile",
                inference_paradigm="frequentist_sampling_distribution",
                source_quantity="freq_interval",
                authority={"authority_scopes": {"user_diagnostic": "active"},
                          "producer_authority_scopes": {"single_fit_delivery": "candidate"}},
                conditioning_fingerprint="c" * 64)
    return ModeInterval(**{**base, **over})


def _row(**over) -> CandidateRow:
    base = dict(label="phonon", k=5, n_modes=1, log_evidence=-12.5,
                evidence_method="laplace", posterior_probability=0.7, usable=True,
                identity=None, variant=None, omegas=(4.25,), mode_intervals=(None,),
                components=(ComponentDescriptor("mode", 0),))
    return CandidateRow(**{**base, **over})


def _obs(**over) -> SceneObservation:
    base = dict(scan_ref="exp:scan:det", q=(1.0, 0.0, 0.5), temperature_K=300.0,
                energy_window=(-3.0, 10.0), gauss_fwhm=0.9, lorentz_fwhm=0.2,
                effective_exposure=120.0, background=1.5, best_label="phonon",
                candidates=(_row(), _row(label="bg", n_modes=0, omegas=(), mode_intervals=(),
                                        components=())),
                evidence_comparison_status="complete", selection_fallback=None,
                arena_fingerprint="a" * 64, evidence_sink=None,
                sample="LiHoF4", conditions=(("temperature_K", 300.0),),
                count_trust="trusted", lattice_epoch=None, fingerprint="")
    fields = {**base, **over}
    # A hole-free axis unless a test says otherwise: the bands ARE the coverage, and the window
    # is only their envelope.
    fields.setdefault("energy_bands",
                      None if fields["energy_window"] is None else (fields["energy_window"],))
    obs = SceneObservation(**fields)
    return SceneObservation(**{**fields, "fingerprint": observation_fingerprint(obs)})


# ----------------------------------------------------------------------------
# Immutability and the JSON boundary
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("dto,field,value", [
    (_row(), "label", "other"),
    (_obs(), "scan_ref", "other"),
    (FootprintBand(0.0, 3.0, "both"), "hi", 4.0),
    (footprint_of(_obs()), "scan_ref", "other"),
])
def test_dtos_are_frozen(dto, field, value):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(dto, field, value)


@pytest.mark.parametrize("dto,cls", [
    (_row(), CandidateRow),
    (_row(k=None, log_evidence=None, evidence_method=None, posterior_probability=None,
          usable=False, identity="acoustic", variant="near_degenerate", n_modes=2,
          omegas=(3.0, None), mode_intervals=(_mode_interval(), None)), CandidateRow),
    # Conditioning provenance (H9) survives the boundary verbatim: the constraint list is
    # persisted mappings, not a re-encoding, and "recorded but unconstrained" (``()``) must stay
    # distinguishable from "no semantics record" (``None``).
    (_row(constraint_origin="transported_prior",
          effective_constraints=({"constraint_kind": "hard", "parameter": "phonon.Gamma",
                                  "origin": "transported_prior", "value": 0.5},)), CandidateRow),
    (_row(constraint_origin="none", effective_constraints=()), CandidateRow),
    (_obs(), SceneObservation),
    (_obs(q=None, temperature_K=None, energy_window=None, best_label=None,
          candidates=()), SceneObservation),
    (FootprintBand(0.0, 3.0, "both"), FootprintBand),
    (footprint_of(_obs()), Footprint),
    (footprint_of(_obs(energy_window=None)), Footprint),
])
def test_round_trip_through_json(dto, cls):
    assert cls.from_dict(dto.to_dict()) == dto
    # ... and through real JSON, where every tuple would come back as a list.
    assert cls.from_dict(json.loads(json.dumps(dto.to_dict()))) == dto


def test_visibility_members_are_their_wire_strings():
    assert [v.value for v in Visibility] == [
        "observed", "searched_no_signal", "censored_outside_footprint",
        "blocked_unresolved", "structural_endpoint",
    ]
    assert json.loads(json.dumps({"v": Visibility.OBSERVED}))["v"] == "observed"


# ----------------------------------------------------------------------------
# Footprint geometry: signed window folded onto intrinsic |omega|
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("window,expected", [
    ((-10.0, 10.0), [(0.0, 10.0, "both")]),                       # symmetric: one folded band
    ((2.0, 10.0), [(2.0, 10.0, "stokes")]),                       # energy-loss only
    ((-10.0, -2.0), [(2.0, 10.0, "anti_stokes")]),                # energy-gain only, folded positive
    ((-3.0, 10.0), [(0.0, 3.0, "both"), (3.0, 10.0, "stokes")]),  # asymmetric: overlap + remainder
    ((-3.0, 0.0), [(0.0, 3.0, "anti_stokes")]),                   # touching zero is not spanning it
    ((0.0, 10.0), [(0.0, 10.0, "stokes")]),
    ((5.0, 5.0), []),                                             # degenerate window covers nothing
    ((0.0, 0.0), []),
])
def test_footprint_bands(window, expected):
    fp = footprint_of(_obs(energy_window=window))
    assert [(b.lo, b.hi, b.sides) for b in fp.bands] == expected


def test_a_mask_hole_is_a_hole_in_the_footprint_not_covered_ground():
    """Review finding 4: coverage is the KEPT runs, never the outer envelope.

    An excluded region between 4 and 7 meV was not measured, so a mode hypothesized there is
    censored — no absence charge — while the runs on either side answer as before. Reading the
    (0, 10) envelope instead would price a phantom miss against a region no counting reached.
    """
    fp = footprint_of(_obs(energy_window=(0.0, 10.0), energy_bands=((0.0, 4.0), (7.0, 10.0))))
    assert [(b.lo, b.hi, b.sides) for b in fp.bands] == [(0.0, 4.0, "stokes"),
                                                         (7.0, 10.0, "stokes")]
    assert covers(fp, 3.0) and covers(fp, 8.0)
    assert not covers(fp, 5.5)
    assert visibility_of(5.5, fp, False) == Visibility.CENSORED_OUTSIDE_FOOTPRINT
    assert visibility_of(3.0, fp, False) == Visibility.SEARCHED_NO_SIGNAL


def test_bands_from_both_sides_of_zero_still_fold_onto_one_intrinsic_axis():
    """Two kept runs straddling zero cover ``|ω|`` from both sides where they overlap."""
    fp = footprint_of(_obs(energy_window=(-6.0, 8.0), energy_bands=((-6.0, -2.0), (1.0, 8.0))))
    # The gain run folds onto |ω| ∈ [2, 6], the loss run covers [1, 8]: only their overlap is
    # measured from both sides.
    assert [(b.lo, b.hi, b.sides) for b in fp.bands] == [(1.0, 2.0, "stokes"), (2.0, 6.0, "both"),
                                                         (6.0, 8.0, "stokes")]
    # |ω| < 1 was looked at from NEITHER side: the gain run stops at −2 and the loss run starts at 1.
    assert not covers(fp, 0.5)


@pytest.mark.parametrize("mode_intervals", [(), (None, None), (_mode_interval(), None)])
def test_a_mis_sized_mode_intervals_is_refused(mode_intervals):
    """PR 4.2a review, Major finding 1 (corrected): ``omegas=(4.25,)`` pinned by ``_row`` — any
    ``mode_intervals`` length but 1 disagrees with ``omegas`` and is refused. ``n_modes`` plays
    no part in the check (see the follow-up correction below)."""
    with pytest.raises(ValueError, match="len\\(omegas\\)=1.*len\\(mode_intervals\\)"):
        _row(mode_intervals=mode_intervals)


def test_a_result_less_candidate_disagrees_with_n_modes_but_still_constructs():
    """Follow-up correction to Major finding 1: a result-less candidate legitimately persists
    ``n_modes=1`` with ``omegas=()`` (``_mode_omegas`` docstring / ``_candidate_rows``'s
    default when ``r is None``) — ``n_modes`` disagreeing with ``len(omegas)`` is NOT a
    slot-misalignment, only ``omegas``/``mode_intervals`` disagreeing is."""
    row = _row(n_modes=1, omegas=(), mode_intervals=(), components=())
    assert row.n_modes == 1 and row.omegas == () and row.mode_intervals == ()


def test_a_row_without_component_descriptors_is_refused_like_a_legacy_row():
    """Review finding 9: the descriptors are the only statement of what a candidate is made of,
    so a payload lacking them must fail rather than read as a structureless candidate."""
    payload = _row().to_dict()
    assert payload["components"] == [{"kind": "mode", "slot": 0}]
    payload.pop("components")
    with pytest.raises(KeyError):
        CandidateRow.from_dict(payload)


def test_an_observation_without_energy_bands_is_refused():
    payload = _obs().to_dict()
    payload.pop("energy_bands")
    with pytest.raises(KeyError):
        SceneObservation.from_dict(payload)


@pytest.mark.parametrize("kind,slot", [("mode", None), ("elastic", 0), ("bragg", None)])
def test_component_descriptors_refuse_a_malformed_declaration(kind, slot):
    with pytest.raises(ValueError):
        ComponentDescriptor(kind, slot)


def test_no_window_means_no_coverage_not_a_default_zero():
    fp = footprint_of(_obs(energy_window=None))
    assert fp.bands == ()
    # The detectability inputs still travel, and ALL of them: the Bose cut is projection-time,
    # per hypothesis, and it needs both resolution components (a Voigt's Lorentzian wings decide
    # whether a weak neighbour was resolvable) and how deep the look was. A footprint carrying
    # only gauss_fwhm would let a projection call a region "searched" on the wrong resolution.
    assert (fp.temperature_K, fp.background, fp.gauss_fwhm) == (300.0, 1.5, 0.9)
    assert (fp.lorentz_fwhm, fp.effective_exposure) == (0.2, 120.0)
    assert (fp.scan_ref, fp.q) == ("exp:scan:det", (1.0, 0.0, 0.5))


def test_footprint_detectability_fields_survive_the_json_boundary():
    fp = footprint_of(_obs())
    assert Footprint.from_dict(json.loads(json.dumps(fp.to_dict()))) == fp
    # Absent is a real state, not a zero: an observation with no recorded exposure must not
    # come back claiming one.
    bare = footprint_of(_obs(lorentz_fwhm=None, effective_exposure=None))
    assert (bare.lorentz_fwhm, bare.effective_exposure) == (None, None)
    assert Footprint.from_dict(json.loads(json.dumps(bare.to_dict()))) == bare


# ----------------------------------------------------------------------------
# Canonical bytes and fingerprints
# ----------------------------------------------------------------------------
def test_canonical_bytes_ignore_key_order():
    a = {"b": 1, "a": [1, 2], "c": None}
    b = {"c": None, "a": [1, 2], "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert canonical_bytes(a) == b'{"a":[1,2],"b":1,"c":null}'
    assert fingerprint(a) == fingerprint(b) == fingerprint(a)


def test_observation_fingerprint_tracks_content_not_field_order():
    obs = _obs()
    assert obs.fingerprint == observation_fingerprint(obs)
    # Whatever the field held while building cannot feed back into the digest.
    assert observation_fingerprint(
        SceneObservation(**{**obs.to_dict(),
                            "q": obs.q, "energy_window": obs.energy_window,
                            "candidates": obs.candidates,
                            "fingerprint": "not-a-digest"})) == obs.fingerprint
    # A changed candidate row changes it; a reordered payload does not.
    changed = _obs(candidates=(_row(log_evidence=-11.0),
                               _row(label="bg", n_modes=0, omegas=(), mode_intervals=())))
    assert changed.fingerprint != obs.fingerprint
    reordered = dict(reversed(list(obs.to_dict().items())))
    reordered.pop("fingerprint")
    assert fingerprint(reordered) == obs.fingerprint
    # Candidate order, however, IS content at this layer: a builder must fix a deterministic
    # order, not rely on the digest to forgive one.
    assert _obs(candidates=tuple(reversed(obs.candidates))).fingerprint != obs.fingerprint


def test_from_dict_refuses_a_payload_with_no_fingerprint():
    payload = _obs().to_dict()
    payload.pop("fingerprint")
    with pytest.raises(KeyError):
        SceneObservation.from_dict(payload)


# ----------------------------------------------------------------------------
# PR #30 review response (disposition 14): validation and canonicalization on EVERY
# construction path, not only ``from_dict``.
# ----------------------------------------------------------------------------
def test_direct_construction_canonicalizes_conditions():
    """A caller building a ``SceneObservation`` directly (every test fixture, the workspace
    adapter) must fingerprint the same canonical form a JSON round-trip would — sorted by name,
    float-valued — or two byte-identical observations built two different ways would digest
    differently. Deliberately passed already-unsorted, mixed-type: ``__post_init__`` canonicalizes
    on construction, not only inside ``from_dict``."""
    obs = _obs(conditions=(("field_T", "3"), ("temperature_K", 300)))
    assert obs.conditions == (("field_T", 3.0), ("temperature_K", 300.0))


@pytest.mark.parametrize("count_trust", COUNT_TRUST_STATES)
def test_every_vocabulary_count_trust_state_is_legal(count_trust):
    _obs(count_trust=count_trust)  # must not raise


def test_an_unknown_count_trust_is_refused_loudly():
    with pytest.raises(ValueError, match="count_trust"):
        _obs(count_trust="quarantined")


@pytest.mark.parametrize("topology_role", (*TOPOLOGY_ROLES, None))
def test_every_vocabulary_topology_role_is_legal(topology_role):
    _obs(topology_role=topology_role)  # must not raise


def test_an_unknown_topology_role_is_refused_loudly():
    with pytest.raises(ValueError, match="topology_role"):
        _obs(topology_role="off_map")


# ----------------------------------------------------------------------------
# ConditionSet (#28, generalized per PR #30 review response disposition 2)
# ----------------------------------------------------------------------------
def test_every_other_declared_dimension_enters_the_key_exact_value():
    """Same binned temperature, different declared field: the partition key must differ, or a
    same-T-different-field arena pair would silently share a partition (#28's open failure)."""
    a = ConditionSet.of((("temperature_K", 300.0), ("field_T", 1.0)), t_round=5.0)
    b = ConditionSet.of((("temperature_K", 300.0), ("field_T", 2.0)), t_round=5.0)
    assert a.key != b.key
    assert a.key == (("temperature_K", 300.0), ("field_T", 1.0))


def test_extra_dimensions_are_exact_not_binned():
    """The extra dimension gets no tolerance of its own: 1.0 and 1.0000001 are different keys,
    unlike temperature which the card's t_round pools."""
    a = ConditionSet.of((("temperature_K", 300.0), ("field_T", 1.0)), t_round=5.0)
    b = ConditionSet.of((("temperature_K", 300.0), ("field_T", 1.0000001)), t_round=5.0)
    assert a.key != b.key


def test_no_extra_dimensions_reproduces_the_temperature_only_key():
    cs = ConditionSet.of((("temperature_K", 300.0),), t_round=5.0)
    assert cs.key == (("temperature_K", 300.0),)


def test_a_t_round_below_the_microkelvin_floor_fails_closed_like_none():
    """Disposition 15: the fixed 6-decimal rounding ``of`` applies cannot represent a bin narrower
    than 1e-6 without silently COLLAPSING bins the caller declared distinct — the same
    fail-closed-to-no-coupling arm a missing binning policy already takes, not a finer bin."""
    resolved = ConditionSet.of((("temperature_K", 300.0),), t_round=1e-6)
    assert resolved.resolved is True
    for tiny in (1e-7, 0.0, -1.0):
        unresolved = ConditionSet.of((("temperature_K", 300.0),), t_round=tiny)
        assert unresolved.resolved is False and unresolved.temperature_K_binned is None
    unresolved_none = ConditionSet.of((("temperature_K", 300.0),), t_round=None)
    assert unresolved_none.resolved is False


def test_an_unresolved_set_still_carries_its_other_dimensions():
    """The #28 fail-closed arm is about the TEMPERATURE leg only — a declared extra dimension is
    exact-match regardless, so it must survive an unresolved binning exactly as it would a
    resolved one."""
    cs = ConditionSet.of((("temperature_K", 300.0), ("field_T", 1.0)), t_round=None)
    assert cs.resolved is False
    assert cs.other == (("field_T", 1.0),)
