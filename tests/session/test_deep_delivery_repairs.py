"""Deep delivery qualifies its own winner at the count ceiling."""

import pytest

from isar.analyze import deep_pass
from isar.session import deep_delivery


@pytest.mark.parametrize("best_count, limited", [(2, True), (1, False)])
def test_a_deep_winner_at_the_count_ceiling_is_search_limited(monkeypatch, best_count, limited):
    entry = deep_pass.ModelEntry("chosen", best_count, 2, -1.0, None, None)
    model = deep_pass.ModelEvidence("chosen", best_count, "reference", -1.0, 0.1)
    res = deep_pass.DeepPassResult(
        models=[model], complete=True, search_limited=None, count_probabilities={best_count: 1.0},
        comparison_scope="full", best_count=best_count,
        close_call={"p_best": 1.0, "statistical": False, "numerical": False}, wall_s=0.0)
    monkeypatch.setattr(deep_pass, "plain_hypotheses", lambda *args: {})
    monkeypatch.setattr(deep_pass, "models_from_record", lambda *args: [entry])
    monkeypatch.setattr(deep_pass, "deep_pass", lambda *args, **kwargs: res)
    monkeypatch.setattr(deep_delivery, "_chosen", lambda *args: None)
    fit = {"fast_pass": {"settings": {"mode_ceiling": 2}, "search_limit_reason": ""}}
    record, _ = deep_delivery.deliver(fit, None, deep_delivery.DeepRequest())
    assert record["state"] == ("search_limited" if limited else "resolved")
    assert record["complete"] is not limited
    assert record["comparison_scope"] == ("partial (search limited)" if limited else "full")
    assert ("ceiling" in (record["search_limited"] or "")) is limited
