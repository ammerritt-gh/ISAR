"""StageTimer / NullTimer — subprocess-side stage timing (mighty-starfish §3)."""
import json

from isar.session.timing import NullTimer, StageTimer


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
