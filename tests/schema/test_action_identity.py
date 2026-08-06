"""Scientific action identity — the fingerprint two hand-maintained implementations must agree on.

``isar.schema.action_identity`` exists so a Qt-free evidence module can authenticate a persisted
measurement intent without importing the acquisition package; ``isar.drive.acquisition.inventory``
owns the live value type. They compute the same hash by hand, and a silent divergence between them
forks candidate identity — the same scientific question dedupes against itself in one module and not
the other. Nothing asserted that until this file.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition import inventory as drive_identity
from isar.schema import action_identity as schema_identity
from isar.schema.steering import CANONICAL_MASTER


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


_CORPUS = [
    _payload(),
    _payload(hkl=(2.15, 0.0, 0.0)),                       # tuple, not list
    _payload(hkl=[-0.0, 0.0, 0.0]),                       # reduced-zone negative zero
    _payload(hkl=[0, 0, 0]),                              # ints, not floats
    _payload(energy_lo=-0.0, energy_hi=0),                # negative-zero / int bounds
    _payload(energy_lo=None, energy_hi=None),
    _payload(action_class="rival_probe", rival_branch_role="optic"),
    _payload(target_branch_role="ω-branch"),              # non-ASCII role text
    _payload(window_center_meV=3.5),                      # validated, excluded from identity
]


@pytest.mark.parametrize("payload", _CORPUS)
def test_the_two_fingerprint_implementations_agree(payload):
    assert (schema_identity.scientific_fingerprint_from_mapping(payload)
            == drive_identity.scientific_fingerprint_from_mapping(payload))


@pytest.mark.parametrize("payload", [
    {key: value for key, value in _payload().items() if key != "window_family"},
    _payload(hkl=[float("nan"), 0.0, 0.0]),
    _payload(hkl=[float("inf"), 0.0, 0.0]),
    _payload(hkl=[1.0, 0.0]),
    _payload(evidence_sink="somewhere_else"),
    _payload(energy_lo=float("inf")),
])
def test_the_two_implementations_reject_the_same_payloads(payload):
    with pytest.raises(ValueError):
        schema_identity.scientific_fingerprint_from_mapping(payload)
    with pytest.raises(ValueError):
        drive_identity.scientific_fingerprint_from_mapping(payload)


@pytest.mark.parametrize("identity", [schema_identity, drive_identity])
def test_negative_zero_is_the_same_scientific_q(identity):
    """O44's zone-centre work makes ``-0.0`` a real hkl component, not a curiosity.

    ``round`` preserves the sign of zero and ``json.dumps(-0.0) == '-0.0'``, so the two spellings
    used to mint different fingerprints and escape dedup.
    """
    assert (identity.canonical_hkl([-0.0, 0.0, 0.0])
            == identity.canonical_hkl([0.0, -0.0, -0.0]))
    assert (identity.scientific_fingerprint_from_mapping(_payload(hkl=[-0.0, 0.0, 0.0]))
            == identity.scientific_fingerprint_from_mapping(_payload(hkl=[0.0, 0.0, 0.0])))
    assert (identity.scientific_fingerprint_from_mapping(_payload(energy_lo=-0.0))
            == identity.scientific_fingerprint_from_mapping(_payload(energy_lo=0.0)))


@pytest.mark.parametrize("identity", [schema_identity, drive_identity])
def test_an_integer_zero_is_the_same_scientific_q_as_a_float_zero(identity):
    assert (identity.scientific_fingerprint_from_mapping(_payload(hkl=[0, 0, 0]))
            == identity.scientific_fingerprint_from_mapping(_payload(hkl=[0.0, 0.0, 0.0])))
    assert (identity.scientific_fingerprint_from_mapping(_payload(energy_hi=7))
            == identity.scientific_fingerprint_from_mapping(_payload(energy_hi=7.0)))
