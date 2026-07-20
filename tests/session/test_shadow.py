import csv
import json

from isar.drive.session_log import log_event
from isar.session.shadow import canonical_decisions, compare_workspaces


def _workspace(root, *, proposal_ncount=100, prior_omega=2.0, gamma="0.2"):
    report = root / "report"
    report.mkdir(parents=True)
    log = report / "session_log.jsonl"
    log_event(log, "timing", {"stage": "fit", "seconds": 99.0}, ts=1.0)
    log_event(log, "propose", {
        "hkl": [2.0, 0.0, 0.0], "e_lo": -2.0, "e_hi": 7.0,
        "ncount": proposal_ncount, "reason": "free prose", "timings": {"x": 1}}, ts=2.0)
    log_event(log, "stop", {"state": "met", "timings": {"seconds": 5}}, ts=3.0)
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
