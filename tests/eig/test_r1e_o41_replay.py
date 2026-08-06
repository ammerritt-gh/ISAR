"""Slice R1-E regression: one real O41 master must stay eligible, contained and scorable.

The fixture under ``tests/fixtures/eig_r1e_o41_master_2p05`` is a verbatim single-master extract of
the O41_Test4 campaign evidence (session entry, its authenticated predictive and target-projection
artifacts, the retained production scan-design context and action intent, and the campaign card and
goal).  It pins the three facts the R1-E replay established on real state:

* the master passes the merged eligibility policy at a canonical same-Q production action;
* ``target_core/1`` is generated and stays inside the authorized energy domain under both the
  global ω(q) basis and the master's own same-Q basis;
* the legacy-versus-target-core comparison completes, carries nonzero information, and the O41
  selector returns a role.

It also pins the partition degeneracy the replay found: under the ``0.25 meV`` offset alone the
predictive falls inside a single partition cell and both candidates score exactly zero.  That is a
real property of this campaign's 0.5 meV binning, not a defect to be silently normalised away — if
it changes, the change is deliberate and this test should be updated with the reason.
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


def test_real_master_is_eligible_under_the_merged_policy(replayed: dict):
    row = replayed["masters"][0]
    assert row["master_session_key"] == MASTER_KEY
    assert "replay_error" not in row
    assert row["eligibility_replay"]["status"] == "eligible"
    assert row["eligibility_replay"]["scientific_codes"] == []
    assert row["eligibility_replay"]["observability_codes"] == []
    # The retained decision point rejected this same master under the pre-fix policy.
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


def test_legacy_versus_target_core_comparison_is_complete_and_nonzero(replayed: dict):
    row = replayed["masters"][0]
    variants = [item for item in row["score_primary"]["variants"]
                if item["status"] == "complete"]
    assert row["score_primary"]["status"] == "complete"
    assert len(variants) == 1
    variant = variants[0]
    assert variant["reason_codes"] == []
    assert variant["partition"]["bin_width"] == 0.5
    assert list(variant["partition"]["offsets"]) == [0.0, 0.25]
    points = variant["candidate_points"]
    assert len(points) == 2
    assert all(item["executed_point_count"] > 0 for item in points.values())
    assert any(item["information_nats"] > 0.0 for item in points.values())
    assert len(variant["paired_blocks"]) == replayed["campaign_policy"]["block_count"]
    assert variant["delta_information_per_executed_point_standard_error_nats"] >= 0.0

    selector = row["selector"]
    assert selector["available"] is True
    assert selector["authority_status"] == "granted"
    assert selector["physical_role"] in {"legacy", "target_core"}
    # The target core is the strictly smaller, fully contained request the selector requires.
    assert all(selector["structural_conditions"].values())


def test_single_offset_partition_degeneracy_is_pinned(replayed: dict):
    row = replayed["masters"][0]
    stability = row["offset_role_stability"]
    assert set(row["score_offset_probe"]) == {"0.0", "0.25"}
    assert stability["stable"] is True
    assert stability["zero_information_offsets"] == ["0.25"]
    zeroed = row["score_offset_probe"]["0.25"]["score"]["variants"]
    assert [item["status"] for item in zeroed] == ["complete"]
    assert all(point["information_nats"] == 0.0
               for point in zeroed[0]["candidate_points"].values())
