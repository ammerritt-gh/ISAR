"""goal→ObjectiveSpec: schema validation with a FAKE dispatcher, plus the LLM-free JSON path."""
import json

import pytest

from isar.drive.objective import ObjectiveSpec
from isar.drive.supervisor import goal_to_spec as g2s
from isar.reduce.llm_client import DispatchResult


class _FakeDispatcher:
    """A scripted Dispatcher — returns a queued sequence of DispatchResults, records the calls."""
    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    def dispatch(self, **kwargs):
        self.calls.append(kwargs)
        return self._results.pop(0) if self._results else DispatchResult(error="empty")


_GOOD = {"path": {"h_start": 2.1, "h_end": 2.4, "k": 0.0, "l": 0.0},
         "step_h": 0.1, "tolerance_meV": 0.5, "budget_hours": 8.0, "notes": "acoustic branch"}


def test_structured_output_builds_a_spec():
    disp = _FakeDispatcher(DispatchResult(structured=_GOOD))
    spec = g2s.goal_to_spec("map the acoustic phonon 2.1->2.4, 0.5 meV, overnight", dispatcher=disp)
    assert isinstance(spec, ObjectiveSpec)
    assert spec.path == [(2.1, 0.0, 0.0), (2.2, 0.0, 0.0), (2.3, 0.0, 0.0), (2.4, 0.0, 0.0)]
    assert spec.tolerance_meV == 0.5
    assert spec.budget == {"kind": "counting_seconds", "amount": 8 * 3600.0}


def test_budget_neutrons_mapping():
    payload = dict(_GOOD); payload.pop("budget_hours"); payload["budget_neutrons"] = 1e10
    spec = g2s.goal_to_spec("x", dispatcher=_FakeDispatcher(DispatchResult(structured=payload)))
    assert spec.budget == {"kind": "neutrons", "amount": 1e10}


def test_recovers_from_fenced_text_output():
    fenced = "```json\n" + json.dumps(_GOOD) + "\n```"
    disp = _FakeDispatcher(DispatchResult(structured=None, output=fenced))
    spec = g2s.goal_to_spec("x", dispatcher=disp)
    assert spec.tolerance_meV == 0.5


def test_one_repair_retry_then_success():
    disp = _FakeDispatcher(DispatchResult(structured=None, output="sorry, no idea"),
                           DispatchResult(structured=_GOOD))
    spec = g2s.goal_to_spec("x", dispatcher=disp)
    assert spec.tolerance_meV == 0.5
    assert len(disp.calls) == 2                              # initial + one repair
    assert g2s._REPAIR_SUFFIX in disp.calls[1]["user"]


def test_refuses_honestly_on_persistent_nonconforming():
    disp = _FakeDispatcher(DispatchResult(structured={"path": {"h_start": 1}}),   # missing required
                           DispatchResult(structured=None, output="still nope"))
    with pytest.raises(g2s.GoalParseError):
        g2s.goal_to_spec("x", dispatcher=disp)


def test_refuses_on_llmm_unreachable():
    disp = _FakeDispatcher(DispatchResult(error="transport_error: connection refused"),
                           DispatchResult(error="transport_error: connection refused"))
    with pytest.raises(g2s.GoalParseError) as e:
        g2s.goal_to_spec("x", dispatcher=disp)
    assert "transport_error" in str(e.value)


# --------------------------------------------------------------------------- LLM-free path
def test_spec_from_json_goal_schema(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_GOOD), encoding="utf-8")
    spec = g2s.spec_from_json(p, defaults={"tier": "deterministic"})
    assert len(spec.path) == 4 and spec.tolerance_meV == 0.5


def test_spec_from_json_full_objective(tmp_path):
    full = ObjectiveSpec.along_h(h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.3,
                                 budget={"kind": "neutrons", "amount": 1e9}).to_dict()
    p = tmp_path / "obj.json"
    p.write_text(json.dumps(full), encoding="utf-8")
    spec = g2s.spec_from_json(p)
    assert spec.tolerance_meV == 0.3 and spec.budget["kind"] == "neutrons"


def test_spec_from_json_rejects_garbage(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"nonsense": 1}), encoding="utf-8")
    with pytest.raises(g2s.GoalParseError):
        g2s.spec_from_json(p)
