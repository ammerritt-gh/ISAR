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
    CandidateRow, Footprint, FootprintBand, SceneObservation, Visibility,
    canonical_bytes, fingerprint, footprint_of, observation_fingerprint,
)


def _row(**over) -> CandidateRow:
    base = dict(label="phonon", k=5, n_modes=1, log_evidence=-12.5,
                evidence_method="laplace", posterior_probability=0.7, usable=True,
                identity=None, variant=None, omegas=(4.25,))
    return CandidateRow(**{**base, **over})


def _obs(**over) -> SceneObservation:
    base = dict(scan_ref="exp:scan:det", q=(1.0, 0.0, 0.5), temperature_K=300.0,
                energy_window=(-3.0, 10.0), gauss_fwhm=0.9, lorentz_fwhm=0.2,
                effective_exposure=120.0, background=1.5, best_label="phonon",
                candidates=(_row(), _row(label="bg", n_modes=0, omegas=())),
                evidence_comparison_status="complete", selection_fallback=None,
                arena_fingerprint="a" * 64, evidence_sink=None, fingerprint="")
    obs = SceneObservation(**{**base, **over})
    return SceneObservation(**{**base, **over,
                               "fingerprint": observation_fingerprint(obs)})


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
          omegas=(3.0, None)), CandidateRow),
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


def test_no_window_means_no_coverage_not_a_default_zero():
    fp = footprint_of(_obs(energy_window=None))
    assert fp.bands == ()
    # The detectability inputs still travel: the Bose cut is projection-time, per hypothesis.
    assert (fp.temperature_K, fp.background, fp.gauss_fwhm) == (300.0, 1.5, 0.9)
    assert (fp.scan_ref, fp.q) == ("exp:scan:det", (1.0, 0.0, 0.5))


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
    changed = _obs(candidates=(_row(log_evidence=-11.0), _row(label="bg", n_modes=0, omegas=())))
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
