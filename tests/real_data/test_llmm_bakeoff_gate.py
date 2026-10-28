"""Collection of the live bakeoff must not contact LLMM without explicit opt-in."""
import importlib.util
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("opt_in", [None, "0", "1"])
def test_bakeoff_collection_requires_explicit_llmm_opt_in(monkeypatch, opt_in):
    if opt_in is None:
        monkeypatch.delenv("ISAR_LIVE_LLMM", raising=False)
    else:
        monkeypatch.setenv("ISAR_LIVE_LLMM", opt_in)
    health_calls = []

    def reachable_service(*args, **kwargs):
        health_calls.append((args, kwargs))
        return nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(urllib.request, "urlopen", reachable_service)
    spec = importlib.util.spec_from_file_location(
        "bakeoff_gate_probe", Path(__file__).with_name("test_adjudicate_bakeoff.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(health_calls) == (1 if opt_in == "1" else 0)
    assert {mark.name for mark in module.pytestmark} >= {"live", "slow"}
    skip = next(mark for mark in module.pytestmark if mark.name == "skipif")
    assert bool(skip.args[0]) is (opt_in != "1")
    assert "ISAR_LIVE_LLMM=1" in skip.kwargs["reason"]
