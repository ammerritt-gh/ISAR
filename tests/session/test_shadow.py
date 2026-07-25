import csv
import json

from isar.drive.session_log import log_event
from isar.session.shadow import canonical_decisions, compare_workspaces


def _workspace(root, *, proposal_ncount=100, prior_omega=2.0, gamma="0.2",
               stop_state="met", terminal_cause=None, stop_reasons=None):
    report = root / "report"
    report.mkdir(parents=True)
    log = report / "session_log.jsonl"
    log_event(log, "timing", {"stage": "fit", "seconds": 99.0}, ts=1.0)
    log_event(log, "propose", {
        "hkl": [2.0, 0.0, 0.0], "e_lo": -2.0, "e_hi": 7.0,
        "ncount": proposal_ncount, "reason": "free prose", "timings": {"x": 1}}, ts=2.0)
    stop_payload = {"state": stop_state, "timings": {"seconds": 5}}
    if terminal_cause is not None:
        stop_payload["terminal_cause"] = terminal_cause
    if stop_reasons is not None:
        stop_payload["reasons"] = list(stop_reasons)
    log_event(log, "stop", stop_payload, ts=3.0)
    (root / "card.state.json").write_text(json.dumps({
        "dispersion_prior": [{"scan_id": "master:q", "omega0": prior_omega}]}),
        encoding="utf-8")
    with open(report / "results.csv", "w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[
            "scan_id", "detector", "pooled", "omega0", "interval_lo", "interval_hi", "gamma"])
        writer.writeheader()
        writer.writerow({"scan_id": "master:q", "detector": "det", "pooled": "True",
                         "omega0": "2.0", "interval_lo": "1.9", "interval_hi": "2.1",
                         "gamma": gamma})
    return root


def test_canonical_decisions_ignore_timing_events_and_fields():
    events = [
        {"ts": 1, "kind": "timing", "payload": {"seconds": 4}},
        {"ts": 2, "kind": "propose", "payload": {"hkl": [1, 0, 0], "seconds": 3}},
    ]
    assert canonical_decisions(events) == [
        {"kind": "propose", "payload": {"hkl": [1, 0, 0]}}]


def test_canonical_stop_retains_terminal_cause_and_reasons_but_strips_timing():
    events = [{"ts": 2, "kind": "stop", "payload": {
        "state": "incomplete", "terminal_cause": "action_inventory_exhausted",
        "reasons": ["declare identity_search_meV"], "seconds": 3,
        "timings": {"fit_seconds": 2.0},
        "per_q": [{
            "hkl": [2.9, 0.0, 0.0], "state": "blocked", "seconds": 1.5,
            "timings": {"fit_seconds": 1.0}}],
    }}]

    assert canonical_decisions(events) == [{"kind": "stop", "payload": {
        "state": "incomplete", "terminal_cause": "action_inventory_exhausted",
        "reasons": ["declare identity_search_meV"],
        "per_q": [{"hkl": [2.9, 0.0, 0.0], "state": "blocked"}],
    }}]


def test_canonical_stop_leaves_optional_absent_terminal_cause_absent():
    projected = canonical_decisions([
        {"kind": "stop", "payload": {"state": "met", "reasons": ["complete"]}},
    ])

    assert projected == [{"kind": "stop", "payload": {
        "state": "met", "reasons": ["complete"]}}]
    assert "terminal_cause" not in projected[0]["payload"]


def test_compare_workspaces_ignores_timestamps_but_detects_contract_sections(tmp_path):
    left = _workspace(tmp_path / "left")
    right = _workspace(tmp_path / "right")
    assert compare_workspaces(left, right).identical

    changed = _workspace(tmp_path / "changed", proposal_ncount=200, prior_omega=2.2)
    comparison = compare_workspaces(left, changed)
    assert not comparison.identical
    assert comparison.differences == ("decisions", "dispersion_prior")

    changed_output = _workspace(tmp_path / "changed-output", gamma="0.4")
    comparison = compare_workspaces(left, changed_output)
    assert comparison.differences == ("certified_results",)


def test_compare_workspaces_detects_only_decisions_when_terminal_cause_changes(tmp_path):
    left = _workspace(
        tmp_path / "left", stop_state="incomplete", terminal_cause="controller_fault",
        stop_reasons=("campaign stopped",))
    right = _workspace(
        tmp_path / "right", stop_state="incomplete",
        terminal_cause="action_inventory_exhausted", stop_reasons=("campaign stopped",))

    comparison = compare_workspaces(left, right)

    assert not comparison.identical
    assert comparison.differences == ("decisions",)


def test_compare_workspaces_detects_only_decisions_when_stop_reasons_change(tmp_path):
    left = _workspace(
        tmp_path / "left", stop_state="incomplete",
        terminal_cause="action_inventory_exhausted",
        stop_reasons=("declare identity_search_meV",))
    right = _workspace(
        tmp_path / "right", stop_state="incomplete",
        terminal_cause="action_inventory_exhausted",
        stop_reasons=("no eligible action remains",))

    comparison = compare_workspaces(left, right)

    assert not comparison.identical
    assert comparison.differences == ("decisions",)


def test_operator_control_tokens_are_volatile_only_for_operator_stops(tmp_path):
    left = _workspace(
        tmp_path / "left", stop_state="operator_stopped",
        stop_reasons=("operator stop request honored at iteration 2", "token first.token"))
    right = _workspace(
        tmp_path / "right", stop_state="operator_stopped",
        stop_reasons=("operator stop request honored at iteration 2", "token second-token"))

    comparison = compare_workspaces(left, right)

    assert comparison.identical
    assert comparison.left["decisions"][-1]["payload"]["reasons"] == [
        "operator stop request honored at iteration 2"]
    assert canonical_decisions([{
        "kind": "stop", "payload": {"state": "incomplete", "reasons": ["token first-token"]},
    }]) != canonical_decisions([{
        "kind": "stop", "payload": {"state": "incomplete", "reasons": ["token second-token"]},
    }])


def test_analysis_spent_is_volatile_but_limit_and_semantic_text_are_not(tmp_path):
    left = _workspace(
        tmp_path / "left", stop_state="incomplete",
        stop_reasons=("analysis limit exhausted before final certification (12.3/60.0s)",))
    same_contract = _workspace(
        tmp_path / "same-contract", stop_state="incomplete",
        stop_reasons=("analysis limit exhausted before final certification (59.9/60.0s)",))
    different_text = _workspace(
        tmp_path / "different-text", stop_state="incomplete",
        stop_reasons=("analysis limit exhausted (59.9/60.0s)",))

    identical = compare_workspaces(left, same_contract)
    semantic_difference = compare_workspaces(left, different_text)

    assert identical.identical
    assert identical.left["decisions"][-1]["payload"]["reasons"] == [
        "analysis limit exhausted before final certification (<spent>/60.0s)"]
    assert semantic_difference.differences == ("decisions",)
