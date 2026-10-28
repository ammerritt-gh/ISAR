"""Deep delivery qualifies its own winner and retains the error-method timings."""
from types import SimpleNamespace

import pytest

from isar.analyze import deep_pass
from isar.session import deep_delivery, predictive_artifact


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
    monkeypatch.setattr(deep_delivery, "_chosen", lambda *args: {"errors": {"method_seconds": {"bootstrap": 1.0}}})
    fit = {"fast_pass": {"settings": {"mode_ceiling": 2}, "search_limit_reason": ""}}
    record, timing = deep_delivery.deliver(fit, None, deep_delivery.DeepRequest())
    assert record["state"] == ("search_limited" if limited else "resolved")
    assert record["complete"] is not limited
    assert record["comparison_scope"] == ("partial (search limited)" if limited else "full")
    assert ("ceiling" in (record["search_limited"] or "")) is limited
    assert timing["errors_method_seconds"] == {"bootstrap": 1.0}
    assert "method_seconds" not in record["chosen"]["errors"]


def test_chosen_errors_preserve_the_method_seconds(monkeypatch):
    timings = {"bootstrap": 1.25, "minos": 2.5, "profile": 3.75}
    result = SimpleNamespace(params={"h": 1.0}, within_model={"method_seconds": timings})
    entry = SimpleNamespace(problem=SimpleNamespace(hyp=object(), ctx=object()))
    model = SimpleNamespace(label="chosen", status="reference", runs=[])
    record = {"models": [{"label": "chosen", "params": {"h": 1.0}, "ln_z": -1.0}]}
    monkeypatch.setattr(deep_delivery.loop, "fit_hypothesis", lambda *args, **kwargs: result)
    monkeypatch.setattr(predictive_artifact, "model_manifest_of", lambda *args: {})
    monkeypatch.setattr(deep_delivery.errors, "attach", lambda *args, **kwargs: None)
    chosen = deep_delivery._chosen(model, entry, record, deep_delivery.DeepRequest(), None)
    assert chosen["errors"]["method_seconds"] == timings
