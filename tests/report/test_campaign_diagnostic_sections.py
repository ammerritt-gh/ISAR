"""Unit tests for the deterministic CAMPAIGN_DIAGNOSTIC renderer (Part 2).

These pin the honesty formatter, zone grouping, the pre-S3 reason derivation, the pre-event §3
phrasing, and the §6 defect predicates — the pieces the golden characterization test cannot isolate.
"""
from __future__ import annotations

import json

import pytest

from isar.report import campaign_diagnostic as cd
from isar.report.campaign_diagnostic import (DiagInputs, EventCounts, _event_counts, _fmt_interval,
                                             _per_q, _zones, render_campaign_diagnostic)
from isar.report.findings import FindingsDoc


# --------------------------------------------------------------------- §4 interval honesty
def test_fmt_interval_flagged_is_never_numeric():
    assert _fmt_interval({"interval_reliable": False, "interval_lo": 5.0, "interval_hi": 5.0}) == "(unreliable)"
    # even a degenerate [3.15, 3.15] must not print as a range when flagged
    assert _fmt_interval({"interval_reliable": False, "interval_lo": 3.15, "interval_hi": 3.15}) == "(unreliable)"


def test_fmt_interval_missing_is_dash():
    assert _fmt_interval({}) == "—"
    assert _fmt_interval({"omega0": 5.0}) == "—"       # no halfwidth → no interval


def test_fmt_interval_reliable_is_a_range():
    assert _fmt_interval({"interval_lo": 4.9, "interval_hi": 5.1}).startswith("[")
    # per-q style: omega0 ± calibrated_halfwidth
    s = _fmt_interval({"omega0": 5.0, "calibrated_halfwidth": 0.1, "interval_reliable": True})
    assert s.startswith("[") and "4.9" in s and "5.1" in s


# --------------------------------------------------------------------- §5 zone grouping
def _pq(q, reason, **kw):
    row = {"q": q, "hkl": [2.0 + q, 0.0, 0.0], "reason": reason,
           "converged": reason == "met"}
    row.update(kw)
    return row


def test_zones_merge_contiguous_same_reason():
    rows = [_pq(0.1, "no_signal"), _pq(0.15, "no_signal"), _pq(0.2, "no_signal")]
    zones = _zones(rows)
    assert len(zones) == 1
    assert zones[0][0] == "no_signal" and len(zones[0][1]) == 3


def test_zones_split_on_gap():
    # a converged (met) q between two no_signal runs splits them into TWO zones
    rows = [_pq(0.1, "no_signal"), _pq(0.15, "met"), _pq(0.2, "no_signal")]
    zones = _zones(rows)
    assert len(zones) == 2
    assert all(z[0] == "no_signal" and len(z[1]) == 1 for z in zones)


def test_zones_separate_different_reasons():
    rows = [_pq(0.1, "no_signal"), _pq(0.15, "uncertifiable_interval")]
    zones = _zones(rows)
    assert [z[0] for z in zones] == ["no_signal", "uncertifiable_interval"]
    # None / met reasons never form a zone
    assert _zones([_pq(0.1, None), _pq(0.15, "met")]) == []


# --------------------------------------------------------------------- pre-S3 reason derivation
def test_per_q_derives_reasons_when_summary_lacks_them():
    # per_q rows with NO 'reason' field (pre-S3 log)
    summary = {"stop": {"reasons": [], "per_q": [
        {"hkl": [2.2, 0, 0], "q": 0.2, "converged": True, "calibrated_halfwidth": 0.05,
         "omega0": 1.8, "n_production": 4, "n_decline": 0, "n_uncertifiable": 0},
        {"hkl": [3.0, 0, 0], "q": 1.0, "converged": False, "n_production": 3, "n_decline": 0,
         "n_uncertifiable": 0},        # has flagged deliveries in results → uncertifiable
        {"hkl": [2.0, 0, 0], "q": 0.0, "converged": False, "n_production": 0, "n_decline": 4,
         "n_uncertifiable": 0},        # declines only → no_signal
    ]}}
    results = [
        {"h": 3.0, "k": 0.0, "l": 0.0, "scan": 5, "detector": "det", "omega0": 6.1,
         "interval_reliable": False, "disposition": "resolved"},
    ]
    defects = []
    rows = _per_q(summary, results, defects)
    by_q = {r["q"]: r for r in rows}
    assert by_q[0.2]["reason"] == "met" and by_q[0.2]["reason_source"] == "derived (pre-S3)"
    assert by_q[1.0]["reason"] == "uncertifiable_interval"
    assert by_q[0.0]["reason"] == "no_signal"
    assert all(r["reason_source"] == "derived (pre-S3)" for r in rows)


def test_per_q_logged_reason_is_tagged_logged():
    summary = {"stop": {"reasons": [], "per_q": [
        {"hkl": [2.2, 0, 0], "q": 0.2, "converged": True, "reason": "met",
         "calibrated_halfwidth": 0.05, "omega0": 1.8}]}}
    rows = _per_q(summary, [], [])
    assert rows[0]["reason_source"] == "logged"


def test_stop_reason_contradiction_defect_fires():
    # summary narrative claims "no signal" at a q that DID deliver flagged fits → §6 defect
    summary = {"stop": {"reasons": ["q=1.0 declined 3x (no signal)"], "per_q": [
        {"hkl": [3.0, 0, 0], "q": 1.0, "converged": False, "n_production": 3, "n_decline": 3}]}}
    results = [{"h": 3.0, "k": 0.0, "l": 0.0, "scan": 5, "detector": "det", "omega0": 6.1,
                "interval_reliable": False, "disposition": "resolved"}]
    defects = []
    _per_q(summary, results, defects)
    assert any("contradict results.csv" in d for d in defects)


# --------------------------------------------------------------------- §3 escalate_fit phrasing
def _ev(kind, payload):
    return {"ts": 0.0, "kind": kind, "payload": payload}


def _render_driver(events, summary=None):
    di = DiagInputs(workspace=None, summary=summary or {}, events=events)
    ec = _event_counts(events)
    per_q = _per_q(di.summary, di.results, di.defects)
    doc = FindingsDoc("t")
    cd._section_driver(doc, di, per_q, ec)
    return doc.render()


def test_driver_pre_event_phrasing_when_escalate_fit_absent():
    events = [_ev("decide", {"event": "campaign_start", "resumed": {}}),
              _ev("analyze", {"errors_tier": "full"}),
              _ev("analyze", {"errors_tier": "fast"})]
    out = _render_driver(events)
    assert "no escalate_fit records" in out
    assert "inferred from analyze.errors_tier" in out


def test_driver_counts_escalations_when_present():
    events = [_ev("decide", {"event": "campaign_start", "resumed": {}}),
              _ev("escalate_fit", {"phase": "promote"}),
              _ev("escalate_fit", {"phase": "outcome", "outcome": "seated"}),
              _ev("escalate_fit", {"phase": "promote"}),
              _ev("escalate_fit", {"phase": "outcome", "outcome": "failed"})]
    out = _render_driver(events)
    assert "2 full-tier promotions" in out
    assert "seated" in out and "failed" in out


# --------------------------------------------------------------------- §6 verify-units defect
def test_verify_units_defect_fires_on_tiny_spend():
    summary = {"objective": {"tier": "deterministic"},
               "budget": {"kind": "counting_seconds", "amount": 28800.0, "spent": 10.6},
               "stop": {"state": "unmeetable", "reasons": [], "per_q": []}}
    events = [_ev("run", {"hkl": [2.0, 0, 0], "kind": "production", "cost": 5.0}) for _ in range(85)]
    di = DiagInputs(workspace=None, summary=summary, events=events)
    ec = _event_counts(events)
    doc = FindingsDoc("t")
    cd._section_outcome(doc, di, [], ec)
    assert any("verify-units" in d for d in di.defects)
    assert "⚠ verify-units" in doc.render()


# --------------------------------------------------------------------- render-status accounting
def test_render_scan_figure_categorizes_skip_vs_fail(tmp_path, monkeypatch):
    import types
    # (a) genuinely non-reconstructable (a param is absent) → "skip", never "fail"
    skip_row = {"scan": 1, "detector": "det", "omega0": 5.0, "gamma": 0.1, "zero": 0.0,
                "gauss_fwhm": 0.7}  # lorentz_fwhm missing
    assert cd._render_scan_figure({}, skip_row, tmp_path / "a.png") == ("skip", None)

    # (b) params present + record present, but the render RAISES → "fail" with scan/det in the message
    good_row = {"scan": 42, "detector": "det", "omega0": 5.0, "gamma": 0.1, "zero": 0.0,
                "gauss_fwhm": 0.7, "lorentz_fwhm": 0.3}
    fake_rec = types.SimpleNamespace(spectra={"det": object()})
    monkeypatch.setattr(cd, "scan_fit_figure",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    status, msg = cd._render_scan_figure({42: fake_rec}, good_row, tmp_path / "b.png")
    assert status == "fail" and msg.startswith("42/det:") and "boom" in msg


def test_render_failed_becomes_a_defect(tmp_path, monkeypatch):
    # A render failure must surface as a §6 defect (reconstruction bug), never a soft §8 footnote.
    import types
    ws = tmp_path / "ws"
    report = ws / "report"
    report.mkdir(parents=True)
    (report / "campaign_summary.json").write_text(json.dumps(
        {"objective": {"tier": "deterministic", "path": [[2.0, 0, 0]], "budget": {}},
         "stop": {"state": "unmeetable", "reasons": [], "per_q": []},
         "budget": {"kind": "neutrons", "amount": 1.0, "spent": 0.0}}), encoding="utf-8")
    (report / "session_log.jsonl").write_text("", encoding="utf-8")
    (report / "results.csv").write_text(
        "scan,detector,h,k,l,disposition,verdict,omega0,gamma,zero,gauss_fwhm,lorentz_fwhm,T,"
        "interval_reliable\n"
        "7,det,2.0,0.0,0.0,resolved,resolved,5.0,0.1,0.0,0.7,0.3,200,True\n", encoding="utf-8")
    monkeypatch.setattr(cd, "_load_records",
                        lambda ws: {7: types.SimpleNamespace(spectra={"det": object()})})
    monkeypatch.setattr(cd, "scan_fit_figure",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("nan/inf")))
    out = cd.render_campaign_diagnostic(ws)
    text = out.read_text(encoding="utf-8")
    assert "per-scan figure render failed" in text and "reconstruction bug" in text
    assert "buried per-scan set" in text          # truthful §8 provenance clause


# --------------------------------------------------------------------- crashed-campaign path
def test_crashed_campaign_renders_without_summary(tmp_path):
    ws = tmp_path / "ws"
    report = ws / "report"
    report.mkdir(parents=True)
    # a session log + results.csv but NO campaign_summary.json
    with (report / "session_log.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 0.0, "kind": "decide", "payload": {"event": "campaign_start"}}) + "\n")
        fh.write(json.dumps({"ts": 1.0, "kind": "run",
                             "payload": {"hkl": [2.0, 0, 0], "kind": "production", "cost": 5.0}}) + "\n")
    (report / "results.csv").write_text(
        "scan,detector,h,k,l,disposition,verdict,omega0,interval_reliable\n"
        "1,det,2.0,0.0,0.0,resolved,resolved,3.0,True\n", encoding="utf-8")
    out = render_campaign_diagnostic(ws)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "campaign_summary.json is absent" in text
    assert "## 8. Provenance" in text
