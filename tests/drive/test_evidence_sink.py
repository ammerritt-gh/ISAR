"""The evidence-sink contract on the acquisition side (PR2.6).

The sink travels: intent → proposal meta → submission payload → ``card.state.json`` provenance →
reduction and the session. Each hop is one place it can silently be dropped, and a dropped sink is not
a visible failure — it is an auxiliary probe quietly pooled into the master that certifies the target.
The tests here pin the hops the driver owns; :mod:`tests.session.test_auxiliary_evidence` pins what the
session does once the sink arrives.

The other half is the realized-outcome taxonomy, which exists for one distinction: a probe that looked
where a rival was predicted and found nothing has *answered* its question, and a probe whose analysis
could not run has not. Collapsing the clean null into the failure would make a successful null look
like a broken scan, and the driver would fire the same action again for the same reason.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition.inventory import MeasurementIntent
from isar.schema.steering import (AUXILIARY_TRACK_NODE, CANONICAL_MASTER, EVIDENCE_SINKS,
                                  NON_CANONICAL_SINKS, UNKNOWN_SINK, evidence_sink_for,
                                  realized_outcome_for)


def _intent(**kw):
    base = dict(action_class="broaden", hkl=(2.9, 0.0, 0.0), energy_lo=-2.0, energy_hi=7.0,
                window_family="target")
    base.update(kw)
    return MeasurementIntent(**base)


# -- the sink is part of the action's identity ------------------------------------------------------

def test_the_default_sink_is_canonical():
    assert _intent().evidence_sink == CANONICAL_MASTER


def test_the_same_question_routed_to_two_sinks_is_two_actions():
    """They must not dedupe onto each other: the same window answered against different evidence
    updates a different posterior, so collapsing them would silently discard one."""
    canonical = _intent()
    auxiliary = _intent(evidence_sink=AUXILIARY_TRACK_NODE)
    assert (canonical.scientific_action_fingerprint
            != auxiliary.scientific_action_fingerprint)


def test_an_unknown_sink_is_refused_at_construction():
    """Fail where the mistake is, not three hops later where a typo would read as canonical and be
    pooled into the master."""
    with pytest.raises(ValueError, match="evidence_sink"):
        _intent(evidence_sink="auxilliary_track_node")   # plausible misspelling


def test_every_declared_sink_constructs():
    for sink in EVIDENCE_SINKS:
        assert _intent(evidence_sink=sink).evidence_sink == sink


# -- resolving a sink back from persisted provenance ------------------------------------------------

def test_a_scan_with_no_tavi_job_is_canonical():
    """Hand-loaded data has no provenance record and must read as ordinary evidence."""
    assert evidence_sink_for({}, []) == CANONICAL_MASTER
    assert evidence_sink_for(None, None) == CANONICAL_MASTER


def test_the_sink_is_resolved_from_the_matching_job():
    provenance = [{"job_id": "7", "evidence_sink": AUXILIARY_TRACK_NODE},
                  {"job_id": "8", "evidence_sink": CANONICAL_MASTER}]
    assert evidence_sink_for({"tavi": {"job_id": "7"}}, provenance) == AUXILIARY_TRACK_NODE
    assert evidence_sink_for({"tavi": {"job_id": "8"}}, provenance) == CANONICAL_MASTER


def test_a_job_with_no_provenance_entry_is_canonical():
    assert evidence_sink_for({"tavi": {"job_id": "9"}}, [{"job_id": "7"}]) == CANONICAL_MASTER


def test_a_pre_existing_provenance_record_reads_as_canonical():
    """Every record written before this field existed. They are ordinary evidence and must keep
    pooling exactly as they did."""
    assert evidence_sink_for({"tavi": {"job_id": "7"}},
                             [{"job_id": "7", "acquisition_id": "a"}]) == CANONICAL_MASTER


def test_a_sink_from_a_newer_writer_fails_closed(caplog):
    """An older reader must not grant unfamiliar sinks the auxiliary treatment it cannot implement —
    and ``canonical_master`` is not the safe default for that, it is the *most* privileged sink there
    is. Falling open to it pools a scan whose routing this reader does not understand into the ΣN pool
    that certifies the target. An explicitly unrecognised sink quarantines instead."""
    with caplog.at_level("WARNING"):
        sink = evidence_sink_for({"tavi": {"job_id": "7"}},
                                 [{"job_id": "7", "evidence_sink": "from_the_future"}])
    assert sink == UNKNOWN_SINK
    assert sink not in EVIDENCE_SINKS, "an unknown sink must not become a recognised one"
    assert "from_the_future" in caplog.text, "a quarantined scan is never a silent no-op"


def test_an_unknown_sink_is_excluded_from_the_pooled_master():
    """The consumer half of the same rule: quarantine only means something if collation honours it.
    The scan is still parsed, fit and reported — nothing is deleted — but its counts never reach the
    pool that certifies the target."""
    assert UNKNOWN_SINK != CANONICAL_MASTER
    assert UNKNOWN_SINK not in NON_CANONICAL_SINKS, (
        "quarantine is not a declared routing sink; it is the absence of a known one")


def test_non_canonical_sinks_exclude_the_canonical_one():
    assert CANONICAL_MASTER not in NON_CANONICAL_SINKS
    assert set(NON_CANONICAL_SINKS) | {CANONICAL_MASTER} == set(EVIDENCE_SINKS)


# -- non-detection is a result, not a failure -------------------------------------------------------

def test_a_clean_null_is_evidence_and_not_an_unusable_analysis():
    assert realized_outcome_for("no_signal", has_candidate=False) == "candidate_not_detected"


def test_a_resolved_fit_with_no_candidate_is_also_a_clean_null():
    assert realized_outcome_for("resolved", has_candidate=False) == "candidate_not_detected"


def test_a_detection_is_named_as_one():
    assert realized_outcome_for("resolved", has_candidate=True) == "candidate_detected"
    assert realized_outcome_for("flagged", has_candidate=True) == "candidate_detected"


def test_a_failed_analysis_is_not_dressed_up_as_a_null():
    """The converse trap: reporting "we looked and found nothing" when the fit never ran would let a
    broken scan update the posterior as though it were a measurement."""
    for disposition in ("held", "junk", "reject", "abstain", "declined", ""):
        assert realized_outcome_for(disposition, has_candidate=False) == "analysis_unusable"
