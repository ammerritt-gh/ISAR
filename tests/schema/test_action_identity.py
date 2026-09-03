"""Scientific action identity — the frozen digests every consumer must keep reproducing.

Persisted inventory artifacts are keyed by these digests, so the fingerprint is a *file format*, not
an implementation detail: a change to the canonicalization silently orphans every retained action
record and forks candidate identity, so the same scientific question stops deduping against itself.
The literal hex vectors below were computed against the pre-consolidation implementation and pin the
algorithm independently of where it lives; the closing test pins the other half — that
``isar.drive.acquisition.inventory`` re-exports the schema's objects rather than owning a second
hand-maintained copy of the rule.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from isar.drive.acquisition import inventory as drive_identity
from isar.schema import action_identity as schema_identity
from isar.schema.steering import AUXILIARY_TRACK_NODE, CANONICAL_MASTER

_FIXTURE_INVENTORY = (Path(__file__).resolve().parents[1] / "fixtures"
                      / "eig_r1e_o41_master_2p05" / "report" / "inventory"
                      / "f0601fd679fdb540b920ee0810e65afbf68e8e94de333d2cf1c830f24c8581c2.json")

# The cold-start campaign slice: no measured belief evidence yet, so no temperature bin.
_COLD_SLICE_FINGERPRINT = drive_identity.CampaignSliceKey(sample="mn3si").fingerprint


def _payload(**overrides) -> dict:
    payload = {
        "action_class": "quicklook",
        "hkl": [2.15, 0.0, 0.0],
        "energy_lo": -2.0,
        "energy_hi": 7.0,
        "window_family": "core",
        "target_branch_role": "acoustic",
        "rival_branch_role": None,
        "evidence_basis_fingerprint": "a" * 64,
        "hypotheses_fingerprint": None,
        "campaign_slice_fingerprint": "b" * 64,
        "evidence_sink": CANONICAL_MASTER,
    }
    payload.update(overrides)
    return payload


# name -> (payload, frozen sha256). Digests computed against the pre-consolidation implementation.
_FROZEN: dict[str, tuple[dict, str]] = {
    "baseline_quicklook": (
        _payload(),
        "af3fb1439a048b4e3d246fe68ff554fd443bf45237642b64ce5d6089fbbe381e"),
    "target_production": (
        _payload(action_class="production", window_family="target", rival_branch_role="optic",
                 hkl=[2.05, 0.0, 0.0], hypotheses_fingerprint="c" * 64),
        "1f399e5b9b8106cb02866d25dc48f8a914f690e8b0c5d6f48e2a334ae9229b12"),
    "auxiliary_sink": (
        _payload(evidence_sink=AUXILIARY_TRACK_NODE),
        "45100edba13e8ee591eb376c99848e4444babc92852c857133bfa2662256f29f"),
    "cold_campaign_slice": (
        _payload(campaign_slice_fingerprint=_COLD_SLICE_FINGERPRINT),
        "b4a8c2d04a147d92992c9feaf5762098c6231b8e6d4e17260d959cf306ce5908"),
    "negative_zero_hkl": (
        _payload(hkl=[-0.0, 0.0, 0.0]),
        "bf6d6084ca42d1a1c8d84a4a7fbebdf92e07e054eadc0572430825169f55f18b"),
    "positive_zero_hkl": (
        _payload(hkl=[0.0, 0.0, 0.0]),
        "bf6d6084ca42d1a1c8d84a4a7fbebdf92e07e054eadc0572430825169f55f18b"),
    "integer_zero_hkl": (
        _payload(hkl=[0, 0, 0]),
        "bf6d6084ca42d1a1c8d84a4a7fbebdf92e07e054eadc0572430825169f55f18b"),
    "hkl_exactly_two": (
        _payload(hkl=[2.0, 0.0, 0.0]),
        "856530959995d1536809d14cbddb12d75d55f6f1375cb56c6d3ed6af3a23ef5b"),
    "hkl_two_beyond_rounding": (
        _payload(hkl=[2.000000001, 0.0, 0.0]),
        "69d2cfcd93c9b93d3d70e777fe0fea81383abf1b67dc244810c355c02be5455f"),
    "hkl_two_within_rounding": (
        _payload(hkl=[2.0000000001, 0.0, 0.0]),
        "856530959995d1536809d14cbddb12d75d55f6f1375cb56c6d3ed6af3a23ef5b"),
    "window_center_present": (
        _payload(window_center_meV=3.5),
        "af3fb1439a048b4e3d246fe68ff554fd443bf45237642b64ce5d6089fbbe381e"),
    "hkl_as_tuple": (
        _payload(hkl=(2.15, 0.0, 0.0)),
        "af3fb1439a048b4e3d246fe68ff554fd443bf45237642b64ce5d6089fbbe381e"),
    "no_energy_bounds": (
        _payload(energy_lo=None, energy_hi=None),
        "e3436936c1feb9a107d0b1f7df6fed4e986567d159c1418f8d0a397741d4273a"),
    "negative_zero_energy": (
        _payload(energy_lo=-0.0, energy_hi=0),
        "4a89b82e30175f3451a9704b197d77b705d01d9f94711c184c005358e6806779"),
    "non_ascii_branch_role": (
        _payload(target_branch_role="ω-branch"),
        "d6e9900e539af3ad39ff40653a411d2db76fddc8577fff34d29e982af3c85d53"),
}


@pytest.mark.parametrize("name", sorted(_FROZEN))
def test_the_frozen_fingerprint_vectors_still_reproduce(name):
    payload, expected = _FROZEN[name]
    assert schema_identity.scientific_fingerprint_from_mapping(payload) == expected


@pytest.mark.parametrize("name", sorted(_FROZEN))
def test_the_value_type_and_the_mapping_path_agree(name):
    """A ``MeasurementIntent`` rebuilt from a mapping must hash to the mapping's own fingerprint."""
    payload, _ = _FROZEN[name]
    intent = drive_identity.MeasurementIntent(
        action_class=payload["action_class"],
        hkl=drive_identity.canonical_hkl(payload["hkl"]),
        energy_lo=payload["energy_lo"],
        energy_hi=payload["energy_hi"],
        window_family=payload["window_family"],
        target_branch_role=payload["target_branch_role"],
        rival_branch_role=payload["rival_branch_role"],
        evidence_basis_fingerprint=payload["evidence_basis_fingerprint"],
        hypotheses_fingerprint=payload["hypotheses_fingerprint"],
        campaign_slice_fingerprint=payload["campaign_slice_fingerprint"],
        evidence_sink=payload["evidence_sink"],
        window_center_meV=payload.get("window_center_meV"),
    )
    assert (intent.scientific_action_fingerprint
            == schema_identity.scientific_fingerprint_from_mapping(payload))


def test_negative_zero_is_the_same_scientific_q():
    """O44's zone-centre work makes ``-0.0`` a real hkl component, not a curiosity.

    ``round`` preserves the sign of zero and ``json.dumps(-0.0) == '-0.0'``, so the two spellings
    used to mint different fingerprints and escape dedup.
    """
    assert (schema_identity.canonical_hkl([-0.0, 0.0, 0.0])
            == schema_identity.canonical_hkl([0.0, -0.0, -0.0]))
    assert (schema_identity.scientific_fingerprint_from_mapping(_FROZEN["negative_zero_hkl"][0])
            == schema_identity.scientific_fingerprint_from_mapping(_FROZEN["positive_zero_hkl"][0]))
    assert (schema_identity.scientific_fingerprint_from_mapping(_payload(energy_lo=-0.0))
            == schema_identity.scientific_fingerprint_from_mapping(_payload(energy_lo=0.0)))


def test_an_integer_zero_is_the_same_scientific_q_as_a_float_zero():
    assert (schema_identity.scientific_fingerprint_from_mapping(_FROZEN["integer_zero_hkl"][0])
            == schema_identity.scientific_fingerprint_from_mapping(_FROZEN["positive_zero_hkl"][0]))
    assert (schema_identity.scientific_fingerprint_from_mapping(_payload(energy_hi=7))
            == schema_identity.scientific_fingerprint_from_mapping(_payload(energy_hi=7.0)))


def test_nine_decimals_is_where_two_q_values_become_one():
    """The rounding boundary is the dedup rule: inside it one action, outside it two."""
    fingerprint = schema_identity.scientific_fingerprint_from_mapping
    assert (fingerprint(_FROZEN["hkl_two_within_rounding"][0])
            == fingerprint(_FROZEN["hkl_exactly_two"][0]))
    assert (fingerprint(_FROZEN["hkl_two_beyond_rounding"][0])
            != fingerprint(_FROZEN["hkl_exactly_two"][0]))


def test_the_window_centre_is_validated_but_excluded_from_identity():
    """Folding the centre in would re-mint every fingerprint ever computed (see MeasurementIntent)."""
    fingerprint = schema_identity.scientific_fingerprint_from_mapping
    assert (fingerprint(_FROZEN["window_center_present"][0])
            == fingerprint(_FROZEN["baseline_quicklook"][0]))
    with pytest.raises(ValueError):
        fingerprint(_payload(window_center_meV=float("nan")))


def test_the_evidence_sink_is_part_of_the_scientific_identity():
    fingerprint = schema_identity.scientific_fingerprint_from_mapping
    assert (fingerprint(_FROZEN["auxiliary_sink"][0])
            != fingerprint(_FROZEN["baseline_quicklook"][0]))


@pytest.mark.parametrize("payload", [
    {key: value for key, value in _payload().items() if key != "window_family"},
    _payload(hkl=[float("nan"), 0.0, 0.0]),
    _payload(hkl=[float("inf"), 0.0, 0.0]),
    _payload(hkl=[1.0, 0.0]),
    _payload(evidence_sink="somewhere_else"),
    _payload(energy_lo=float("inf")),
])
def test_a_malformed_payload_is_rejected(payload):
    with pytest.raises(ValueError):
        schema_identity.scientific_fingerprint_from_mapping(payload)


@pytest.mark.parametrize("name", [
    "MeasurementIntent",
    "canonical_hkl",
    "canonical_json",
    "scientific_fingerprint",
    "scientific_fingerprint_from_mapping",
])
def test_the_inventory_names_are_the_schema_objects_themselves(name):
    """Not "they agree" — the same object, so no second implementation can be reintroduced quietly."""
    assert getattr(drive_identity, name) is getattr(schema_identity, name)
