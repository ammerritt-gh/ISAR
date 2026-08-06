"""The two fail-closed evidence joins in the O41 authority path must say so in the record.

Both helpers deliberately fall back to legacy when the evidence will not join — that behaviour is
correct and unchanged here. What was missing is the log entry: in the durable record, "no offsets
were published" and "the offsets failed to join" looked identical, which makes an evidence-plumbing
regression indistinguishable from a campaign that simply had nothing to join.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from isar.drive.acquisition import authority
from isar.eig.o41_authority import PRIMARY_PARAMETERS, PRIMARY_POLICY_ID, PRIMARY_VARIANT_ID


def test_a_failed_partition_offset_join_is_logged_and_still_fails_closed(caplog):
    # A negative offset is rejected by O41PartitionOffsetEvidence — the shape of any offset set the
    # selector must not trust.
    score_set = SimpleNamespace(candidate_scores=[
        {"candidate_id": "legacy", "partition_information_nats": [0.10]},
        {"candidate_id": "target", "partition_information_nats": [0.20]}])
    with caplog.at_level(logging.WARNING, logger=authority.__name__):
        result = authority._partition_offset_evidence(
            score_set, {"offsets": [-1.0]}, {"legacy"}, {"target"})
    assert result is None                                   # control flow unchanged: fail closed
    assert "did not join" in caplog.text


def _primary_comparison():
    variant = SimpleNamespace(policy_id=PRIMARY_POLICY_ID, variant_id=PRIMARY_VARIANT_ID,
                              status="complete",
                              compiled_partition={"qoi_id": "excitation.energy", "qoi_unit": "meV",
                                                  **PRIMARY_PARAMETERS},
                              score_set={"candidate_scores": []})
    return SimpleNamespace(variants=[variant])


def test_an_unreadable_primary_score_set_is_logged_and_marked_mismatch(caplog, monkeypatch):
    def explode(_payload):
        raise KeyError("candidate_scores")

    monkeypatch.setattr(authority, "_score_set_from_mapping", explode)
    inventory = SimpleNamespace(shadow_scoring=SimpleNamespace(variants=[]))
    realized = SimpleNamespace(realized_request_fingerprint="f" * 64, source_candidate_id="c1")
    with caplog.at_level(logging.WARNING, logger=authority.__name__):
        evidence = authority._primary_score_evidence(
            inventory, _primary_comparison(), realized, realized)
    assert evidence.status == "unavailable"                 # control flow unchanged
    assert evidence.evidence_join_status == "mismatch"
    assert "could not be read" in caplog.text


@pytest.mark.parametrize("helper", ["_partition_offset_evidence", "_primary_score_evidence"])
def test_the_helpers_still_exist_under_the_names_the_selector_calls(helper):
    assert callable(getattr(authority, helper))
