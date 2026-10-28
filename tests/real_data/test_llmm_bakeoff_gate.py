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
    assert health_calls == []
    assert {mark.name for mark in module.pytestmark} >= {"live", "slow"}
    skip = next(mark for mark in module.pytestmark if mark.name == "skipif")
    assert bool(skip.args[0]) is (opt_in != "1")
    assert "ISAR_LIVE_LLMM=1" in skip.kwargs["reason"]


@pytest.mark.parametrize("connection_error", [False, True])
def test_opted_in_bakeoff_fails_when_llmm_is_unavailable(monkeypatch, tmp_path, caplog, connection_error):
    monkeypatch.setenv("ISAR_LIVE_LLMM", "1")

    def unavailable_service(*args, **kwargs):
        if connection_error:
            raise OSError("synthetic LLMM connection failure")
        return nullcontext(SimpleNamespace(status=503))

    monkeypatch.setattr(urllib.request, "urlopen", unavailable_service)
    spec = importlib.util.spec_from_file_location(
        "bakeoff_unavailable_probe", Path(__file__).with_name("test_adjudicate_bakeoff.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    skip = next(mark for mark in module.pytestmark if mark.name == "skipif")
    assert not skip.args[0], "an opted-in live check must fail rather than skip when LLMM is down"
    monkeypatch.setattr(module, "_scenario", lambda *args: pytest.fail("built scans before checking LLMM"))
    with pytest.raises(AssertionError, match="LLMM not reachable"):
        module.test_local_vs_nanogpt_bakeoff(tmp_path)
    if connection_error:
        assert "synthetic LLMM connection failure" in caplog.text
