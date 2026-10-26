"""StageTimer / NullTimer — subprocess-side stage timing (mighty-starfish §3)."""
import json

from isar.session.timing import (NullTimer, StageTimer, certification_pass_breakdown,
                                 embedded_run_timings, fit_timing_breakdown)


def test_stage_spans_recorded():
    t = StageTimer(errors_tier="full", M=200)
    with t.stage("ingest_parse", n_records=5):
        pass
    with t.stage("reduce_qc"):
        pass
    d = t.to_dict()
    assert [s["stage"] for s in d["stages"]] == ["ingest_parse", "reduce_qc"]
    assert d["stages"][0]["n_records"] == 5
    assert all(isinstance(s["seconds"], float) and s["seconds"] >= 0.0 for s in d["stages"])


def test_to_dict_shape_has_meta_at_top_level():
    t = StageTimer(errors_tier="fast", M=50, argv=["isar", "run"])
    with t.stage("workspace_load"):
        pass
    d = t.to_dict()
    assert d["errors_tier"] == "fast"
    assert d["M"] == 50
    assert d["argv"] == ["isar", "run"]
    assert isinstance(d["total_seconds"], float) and d["total_seconds"] >= 0.0
    assert isinstance(d["stages"], list)


def test_stage_meta_mutation_rides_into_entry():
    t = StageTimer()
    with t.stage("fit_scans") as meta:
        meta["n_tasks"] = 3
    assert t.to_dict()["stages"][0]["n_tasks"] == 3


def test_write_round_trip(tmp_path):
    t = StageTimer(errors_tier="full")
    with t.stage("report_render"):
        pass
    p = tmp_path / "report" / "run_timings.json"
    t.write(p)
    assert p.exists()
    loaded = json.loads(p.read_text(encoding="utf-8"))
    assert loaded["errors_tier"] == "full"
    assert loaded["stages"][0]["stage"] == "report_render"


def test_null_timer_stage_yields_dict_and_no_ops(tmp_path):
    t = NullTimer(errors_tier="full", M=1)
    with t.stage("anything", extra=1) as meta:
        assert isinstance(meta, dict)
        meta["k"] = "v"  # no-op, but must not crash
    assert t.to_dict() == {}
    # write is best-effort and inert
    t.write(tmp_path / "nope" / "run_timings.json")


def test_fit_timing_breakdown_is_disjoint_and_keeps_methods():
    timings = [{"stages": [
        {"stage": "fit_scans", "per_task": [
            {"seconds": 10.0, "two_stage_seconds": 3.0, "errors_attach_seconds": 4.0,
             "bootstrap_seconds": 2.5, "minos_seconds": 0.5}]},
        {"stage": "fit_masters", "per_master": [
            {"fit_seconds": 20.0, "two_stage_seconds": 7.0, "errors_attach_seconds": 8.0,
             "bootstrap_seconds": 6.0, "profile_seconds": 1.0}]},
    ]}]
    got = fit_timing_breakdown(timings)
    assert got["buckets"] == {
        "scan_error_attachment": 4.0,
        "master_error_attachment": 8.0,
        "two_stage_fitting": 10.0,
        "fit_remainder": 8.0,
    }
    assert got["total_fit_seconds"] == 30.0
    assert got["method_seconds"]["bootstrap_seconds"] == 8.5


def test_fit_timing_breakdown_reports_the_fast_pass_entries():
    """C-timing example 2: stage seconds sum over units, each spectrum (scan or master) gets a row,
    and the two_stage_fitting bucket equals the sum of two_stage_seconds."""
    def entry(total, ladder, stop="complete"):
        return {"total_seconds": total, "n_fits": 100, "stop_reason": stop,
                "stages": {"seed": 0.5, "ladder": ladder, "audits": 1.0}}

    timings = [{"stages": [
        {"stage": "fit_scans", "scan_workers": 2, "per_task": [
            {"scan_id": "E:1:d", "seconds": 4.0, "two_stage_seconds": 3.0,
             "fast_pass": entry(3.0, 1.5)},
            {"scan_id": "E:2:d", "seconds": 9.0, "two_stage_seconds": 8.0,
             "fast_pass": entry(8.0, 6.5, stop="safety_budget")},
            {"scan_id": "E:3:d", "seconds": 1.0, "two_stage_seconds": 0.0, "fast_pass": None}]},
        {"stage": "fit_masters", "per_master": [
            {"master_id": "master:q", "fit_seconds": 5.0, "two_stage_seconds": 4.0,
             "fast_pass": entry(4.0, 2.0)}]},
    ]}]
    got = fit_timing_breakdown(timings)
    assert got["fast_pass_stage_seconds"] == {"seed": 1.5, "ladder": 10.0, "audits": 3.0}
    assert got["buckets"]["two_stage_fitting"] == 15.0
    assert got["per_spectrum_fast_pass"] == [
        {"scan_id": "E:1:d", "total_seconds": 3.0, "stop_reason": "complete", "n_fits": 100},
        {"scan_id": "E:2:d", "total_seconds": 8.0, "stop_reason": "safety_budget", "n_fits": 100},
        {"master_id": "master:q", "total_seconds": 4.0, "stop_reason": "complete", "n_fits": 100}]


def test_malformed_embedded_timing_is_logged_and_skipped(caplog):
    events = [{"kind": "analyze", "payload": {"timings": "broken"}}]
    assert embedded_run_timings(events) == []
    assert "not an object" in caplog.text


def test_certification_pass_breakdown_reports_parallel_critical_path(tmp_path):
    root = tmp_path / "report" / "certification"
    revision = root / "rev-a"
    units = revision / "units"
    requests = root / "requests"
    units.mkdir(parents=True)
    requests.mkdir()
    (requests / "req-a.json").write_text(json.dumps({
        "per_q": [{"hkl": [2.0, 0.0, 0.0]}, {"hkl": [2.1, 0.0, 0.0]},
                  {"hkl": [2.2, 0.0, 0.0]}]}), encoding="utf-8")
    (revision / "manifest.json").write_text(json.dumps({
        "fingerprint": "rev-a", "request_fingerprint": "req-a",
        "worklist": [
            {"unit_key": "u1", "hkl": [2.1, 0.0, 0.0]},
            {"unit_key": "u2", "hkl": [2.2, 0.0, 0.0]},
        ]}), encoding="utf-8")
    (revision / "runtime.json").write_text(
        json.dumps({"activity_id": "activity-a"}), encoding="utf-8")
    (revision / "progress.json").write_text(json.dumps({
        "activity_id": "activity-a", "newly_fitted": 1, "reused_checkpoint": 1,
        "reused_workspace": 0}), encoding="utf-8")
    for key, seconds, bootstrap in (("u1", 8.0, 5.0), ("u2", 13.0, 9.0)):
        (units / f"{key}.json").write_text(json.dumps({
            "unit_key": key,
            "entry": {"scan_id": f"master:{key}", "provenance": {
                "fit_seconds": seconds, "bootstrap_seconds": bootstrap,
                "minos_seconds": 1.0, "profile_seconds": 2.0,
            }}}), encoding="utf-8")
    events = [
        {"ts": 10.0, "kind": "activity_start", "payload": {
            "activity_id": "activity-a", "stage": "final_certification"}},
        {"ts": 24.0, "kind": "activity_end", "payload": {
            "activity_id": "activity-a", "stage": "final_certification"}},
    ]

    got = certification_pass_breakdown(tmp_path, events)

    assert len(got) == 1
    assert got[0]["requested_points"] == 3
    assert got[0]["selected_points"] == 2
    assert got[0]["carried_points"] == 1
    assert got[0]["newly_fitted"] == 1
    assert got[0]["reused_checkpoint"] == 1
    assert got[0]["wall_seconds"] == 14.0
    assert got[0]["unit_work_seconds"] == 21.0
    assert got[0]["critical_unit_seconds"] == 13.0
    assert got[0]["slowest_hkl"] == [2.2, 0.0, 0.0]
    assert got[0]["critical_method_seconds"]["bootstrap_seconds"] == 9.0


def test_certification_pass_breakdown_warns_on_malformed_unit(tmp_path, caplog):
    revision = tmp_path / "report" / "certification" / "rev-b"
    (revision / "units").mkdir(parents=True)
    (revision / "manifest.json").write_text(
        json.dumps({"fingerprint": "rev-b", "worklist": []}), encoding="utf-8")
    (revision / "units" / "bad.json").write_text("not json", encoding="utf-8")

    got = certification_pass_breakdown(tmp_path, [])

    assert len(got) == 1
    assert got[0]["critical_unit_seconds"] is None
    assert "could not read timing data" in caplog.text
