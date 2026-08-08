"""Slice R1-E regression, re-baselined under ``isar.fit_selection_semantics/2``.

The fixture under ``tests/fixtures/eig_r1e_o41_master_2p05`` is a verbatim single-master extract of
the O41_Test4 campaign evidence (session entry, its authenticated predictive and target-projection
artifacts, the retained production scan-design context and action intent, and the campaign card and
goal).  Its embedded selection records are ``/1`` — the retired AIC contract — and per H1 there is
no compatibility reader: under the ``/2`` evidence contract the replay's eligibility check reads
them as the observability gap ``selection_record_invalid`` and the master fails closed.

What this file now pins:

* the refusal is STRUCTURED — ``unassessable`` with observability codes, never a scientific
  verdict, never a crash, and never a silently-granted grandfather pass;
* ``target_core/1`` geometry is still generated and stays inside the authorized energy domain
  under both bases (geometry does not depend on the selection record);
* an unassessable master is never scored — no comparison, no selector role.

Historical facts established by the original R1-E replay (the master passed the merged policy
under /1; the 0.25 meV single-offset partition degeneracy of this campaign's 0.5 meV binning)
remain true of that campaign and live in this docstring and git history.  Exercising them again
would need a current-contract re-extract from a fresh campaign; fabricating /2 evidence fields
inside verbatim hash-authenticated campaign evidence was rejected (H1: evidence is what it is).
The live "clean current-contract master is eligible" pin is tests/eig/test_eligibility.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.eig_replay import replay_o41_test4 as replay_tool


FIXTURE = Path("tests/fixtures/eig_r1e_o41_master_2p05")
MASTER_KEY = "master:tavi_(41, 0, 0)_200.0"


@pytest.fixture(scope="module")
def replayed() -> dict:
    report = replay_tool.replay(FIXTURE.resolve())
    assert len(report["masters"]) == 1
    return report


def test_old_contract_master_fails_closed_with_a_structured_refusal(replayed: dict):
    row = replayed["masters"][0]
    assert row["master_session_key"] == MASTER_KEY
    assert "replay_error" not in row
    replay = row["eligibility_replay"]
    assert replay["status"] == "unassessable"
    # An unreadable record is an observability gap, not a scientific verdict.
    assert replay["scientific_codes"] == []
    assert "selection_record_invalid" in replay["observability_codes"]
    # The retained decision point (historical evidence, /1-era) is untouched by the flip.
    persisted = row["persisted_decision_point"]
    assert persisted["eligibility_status"] == "ineligible"
    assert "selection_comparison_incomplete" in persisted["scientific_codes"]


def test_target_core_geometry_is_generated_and_contained_on_both_bases(replayed: dict):
    row = replayed["masters"][0]
    domain = row["global_basis"]["authorized_domain"]
    assert row["same_q_basis"]["usable"] is True
    assert row["same_q_basis"]["source"] == "model_interval"
    for basis in ("target_core_global_basis", "target_core_same_q_basis"):
        core = row[basis]
        assert core["present"] is True, (basis, core.get("refusal"))
        assert core["contained_in_authorized_domain"] is True
        assert domain[0] <= core["e_lo"] < core["e_hi"] <= domain[1]
        assert core["n_points"] >= 2
    # The same-Q basis re-centres the core on the master's own ω₀, not the dispersion extrapolation.
    assert row["target_core_same_q_basis"]["centre_meV"] == pytest.approx(
        row["same_q_basis"]["mean_meV"], abs=1e-6)


def test_unassessable_master_is_never_scored(replayed: dict):
    """Fail-closed means fail-closed: no comparison score, no selector role, no offset probe.

    The pre-/2 versions of this file pinned the completed comparison (nonzero information,
    granted selector authority) and the 0.25 meV single-offset partition degeneracy.  Those runs
    required an assessable master; with the fixture's /1 records refused at the trust boundary,
    their absence IS the regression pin — a scored-but-ineligible master here would mean the
    eligibility gate leaks.
    """
    row = replayed["masters"][0]
    for key in ("score_primary", "selector", "offset_role_stability", "score_offset_probe"):
        assert key not in row, f"{key} present — an unassessable master was scored"
