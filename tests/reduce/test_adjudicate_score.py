"""Scoring an adjudication against the injected synthetic truth (scan.truth["quirks"]).

These are pure functions — no network. They map each injected quirk to the decision it warrants,
then measure recall, the destructive-error count (the metric that disqualifies a model: masking or
dropping genuine data), and schema adherence.
"""
from isar.reduce import adjudicate, adjudicate_score
from isar.schema import scan


def _adj(rows, narrative="n", model="m"):
    decisions = [scan.Decision(target=t, decision=dec, shape=sh, reason=r, confidence=c)
                 for (t, dec, sh, r, c) in rows]
    return scan.Adjudication(decisions, narrative, model)


def test_expected_decisions_maps_quirk_types():
    quirks = [
        {"type": "spike", "detector": "ana1", "index": 10, "magnitude": 8000},
        {"type": "stale_head", "detector": "ana2", "indices": [0, 1, 2], "level": 600},
        {"type": "dead_detector", "detector": "ana4", "level": 30},
        {"type": "empty", "detector": "ana5", "level": 20},
        {"type": "sensor_divergence", "sensor": 298.0, "setpoint": 2.0},
        {"type": "truncated", "kept": 18, "original": 40},
    ]
    exp = adjudicate_score.expected_decisions(quirks)
    assert exp["point:ana1:10"] == {"mask"}
    assert exp["point:ana2:0"] == {"mask"} and exp["point:ana2:2"] == {"mask"}
    assert "drop_detector" in exp["spectrum:ana4"]
    assert "drop_detector" in exp["spectrum:ana5"]
    assert "hold_for_human" in exp["scan"]


def test_score_counts_matches_and_destructive_errors():
    quirks = [{"type": "spike", "detector": "ana1", "index": 10, "magnitude": 8000},
              {"type": "dead_detector", "detector": "ana4", "level": 30}]
    adj = _adj([
        ("point:ana1:10", "mask", "not_applicable", "cosmic", 0.99),         # matches
        ("spectrum:ana4", "drop_detector", "not_applicable", "dead", 0.95),  # matches
        ("spectrum:ana3", "drop_detector", "not_applicable", "misjudged", 0.9),  # destructive
    ])
    sc = adjudicate_score.score_adjudication(adj, quirks)
    assert sc.expected == 2
    assert sc.matched == 2
    assert sc.destructive == 1
    assert sc.schema_ok is True


def test_keep_on_a_genuine_flag_is_not_destructive_but_misses_recall():
    quirks = [{"type": "spike", "detector": "ana1", "index": 10, "magnitude": 8000}]
    adj = _adj([("point:ana1:10", "keep", "not_applicable", "looked fine", 0.5)])
    sc = adjudicate_score.score_adjudication(adj, quirks)
    assert sc.expected == 1
    assert sc.matched == 0        # wrong call → not matched
    assert sc.destructive == 0    # but a 'keep' never destroys data


def test_hold_for_human_failure_marks_schema_not_ok():
    fail = adjudicate._hold_for_human(type("R", (), {"model_id": "m", "error": "x"})())
    sc = adjudicate_score.score_adjudication(fail, [])
    assert sc.schema_ok is False


def test_aggregate_sums_scorecards():
    a = adjudicate_score.ScoreCard(expected=2, matched=2, destructive=0, schema_ok=True)
    b = adjudicate_score.ScoreCard(expected=3, matched=1, destructive=1, schema_ok=False)
    agg = adjudicate_score.aggregate([a, b])
    assert agg["expected"] == 5
    assert agg["matched"] == 3
    assert agg["destructive"] == 1
    assert agg["recall"] == 3 / 5
    assert agg["schema_adherence"] == 0.5
    assert agg["n_scans"] == 2
