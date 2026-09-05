"""AP8 replay: a mid-campaign pool-inconsistency must not wipe confirmed identity or stop the run.

AP8 (acoustic map 2.0→3.0, 21 points, unlimited budget) had confirmed the *entire* acoustic path by
revision 21. Then a precision scan at index 13 (H=2.65) pooled a second member that disagreed with the
first (consistency χ²=13.3, p=0.0059 < α=0.01), flagging the pooled master ``pool_consistent=False``
even though its pooled ω₀ was within tolerance. The tracker then dropped that q's candidate, which broke
the Viterbi trajectory and retroactively wiped the confirmed identities of indices 13-20 — collapsing
the frontier to 13 and stopping ``branch_ambiguity; frontier_blocked at path index/indices 14..20`` with
beam budget unused.

The fix keeps a ``pool_consistent=False`` master's candidate in the identity tracker (the acoustic mode
is still fitted; only its ω₀ agreement is in doubt), so the trajectory stays connected. Convergence and
the dispersion prior gate on ``pool_consistent`` independently, so the inconsistent point cannot certify
or seed the prior — it is re-acquired instead.

The synthetic reproductions live in ``tests/session/test_branch_tracking.py``
(``test_pool_inconsistent_master_keeps_identity_connected``), ``tests/drive/test_stopping.py``
(``test_pool_inconsistent_master_never_certifies_even_when_tight``), and
``tests/drive/test_tracked_frontier.py`` (``test_pool_inconsistent_point_is_re_acquired_not_abandoned``).
This module ties the fix to the real frozen workspace when it is present (it is gitignored, like the
AP6/AP7 fixtures), so it is skipped in a clean checkout.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_WORKSPACE = Path(__file__).resolve().parents[2] / "test_workspaces" / "AP8_Failed"

pytestmark = pytest.mark.skipif(
    not (_WORKSPACE / "session.json").exists(),
    reason="gitignored test_workspaces/AP8_Failed/ frozen workspace not present")


def _replay_track():
    from isar.schema.card import Card
    from isar.session import evidence_update
    from isar.session.state import SessionState

    session = SessionState.load(str(_WORKSPACE / "session.json"))
    card = Card.load(str(_WORKSPACE / "card.policy.toml"), str(_WORKSPACE / "card.state.json"))
    t_round = (getattr(card.policy, "collation", None) or {}).get("t_round") or 5.0
    track = evidence_update._apply_tracked_branch(session, card, t_round=float(t_round))
    return track, card


def test_frozen_ap8_no_longer_retracts_the_confirmed_tail():
    """The recorded failure wiped indices 13-20 to ``blocked``/``missing``. After the fix the tracker,
    re-run over AP8's own frozen session, keeps every one of them decisively identified."""
    track, _card = _replay_track()
    states = [a["track_state"] for a in track["assignments"]]

    assert "ambiguous" not in states[13:], states
    # Indices 13-20 (H=2.65..3.00) — the tail AP8 abandoned — all stay confirmed with a selected mode.
    for index in range(13, 21):
        assert states[index] == "confirmed", (index, states)
        assert track["assignments"][index]["selected_candidate_index"] is not None, index


def test_frozen_ap8_index_13_stays_unconverged_so_the_campaign_continues():
    """Q1 precondition on real data: index 13's frozen master is ``pool_consistent=False`` with no
    certifiable half-width, so it cannot count as converged — the campaign stays short of ``met`` and
    re-acquires index 13 rather than terminating."""
    from isar.drive.isar_io.readers import read_results_csv

    rows = read_results_csv(_WORKSPACE)
    pooled_2_65 = [
        r for r in rows
        if str(r.get("pooled")).lower() in ("true", "1", "yes")
        and abs(float(r.get("h")) - 2.65) < 1e-6]
    assert pooled_2_65, "frozen AP8 must carry a pooled master at H=2.65"
    master = pooled_2_65[-1]
    assert str(master.get("pool_consistent")).lower() in ("false", "0", "no")
    assert master.get("halfwidth") in (None, "", "None")
