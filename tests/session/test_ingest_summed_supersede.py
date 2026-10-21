"""A summed BL43LXU set is superseded in the ledger once its individual scans are there (design R10).

`_collect` already passes a summed set over when its scans are in the same batch. These tests pin the
other orders: the summed set ingested first (its ledger entries must then be marked `excluded`, never
deleted), the individual scans in another folder, and the summed set arriving second. Files are written
at test time in the Ba122 layout (the only one whose file name lists scan numbers); nothing reads
`real_data/`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from isar.parsers import sp8_bl43
from isar.session import driver, ingest
from isar.session.state import SessionState
from isar.session.workspace import Workspace

SUMMED = "spec1_T26K__scan_58_59"


def write_ba122(folder: Path, scans: str, spec: str = "spec1", analyzer: int = 6) -> Path:
    """One Ba122 export file: energy, intensity, error, with N = (y/e)^2 recovering exactly."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{spec}_T26K__scan_{scans}_A{analyzer}_Q_4_0p075_0_inel.dat"
    counts = (np.arange(8) + 1.0) ** 2
    rows = [f"{e:.4f} {n:.6E} {n ** 0.5:.6E}" for e, n in zip(np.arange(8) * 0.2, counts)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


def ingest_into(session: SessionState, *folders: Path):
    return ingest.ingest_batch(sp8_bl43.card_template(), session, list(folders))


def summed_entries(session: SessionState) -> list:
    return [e for e in session.scans.values() if e.provenance.get("file_set") == SUMMED]


def individual_entries(session: SessionState) -> list:
    return [e for e in session.scans.values() if e.provenance.get("file_set") != SUMMED]


def snapshot(session: SessionState) -> str:
    return json.dumps(session.to_dict(), sort_keys=True)


def assert_superseded(session: SessionState) -> None:
    """The summed set's entries stay in the ledger, excluded, with a reason naming it and its scans."""
    (entry,) = summed_entries(session)
    assert entry.status == "excluded" and entry.content_hash and entry.provenance["source_file"]
    (reason,) = entry.reasons
    assert SUMMED in reason and "58" in reason and "59" in reason and "no count is used twice" in reason
    assert entry.provenance["scan_facts"]["scan_numbers"]["value"] == [58, 59]     # provenance kept
    assert individual_entries(session) and all(e.status == "ingested" for e in individual_entries(session))


# --- ingest ---------------------------------------------------------------------------------------
def test_summed_set_alone_is_read_and_flagged_pre_combined(tmp_path):
    write_ba122(tmp_path, "58_59")
    session = SessionState()
    report = ingest_into(session, tmp_path)
    assert report.passed_over == [] and len(report.added) == 1
    (entry,) = summed_entries(session)
    assert entry.status == "ingested" and entry.reasons == []
    assert "pre_combined_input" in {f["type"] for f in entry.provenance["scan_flags"]}


def test_later_batch_with_the_individual_scans_excludes_the_summed_entries(tmp_path):
    write_ba122(tmp_path, "58_59")
    session = SessionState()
    ingest_into(session, tmp_path)
    write_ba122(tmp_path, "58")
    write_ba122(tmp_path, "59")
    report = ingest_into(session, tmp_path)
    assert report.dirty and [p["scans"] for p in report.passed_over] == [[58, 59]]
    assert_superseded(session)
    assert len(session.scans) == 3                                  # nothing deleted, nothing renamed
    assert all(p.exists() for p in tmp_path.iterdir())
    before = snapshot(session)
    again = ingest_into(session, tmp_path)
    assert not again.dirty and snapshot(session) == before          # a re-ingest is a no-op


def test_individual_scans_in_another_folder_still_exclude_the_summed_entries(tmp_path):
    write_ba122(tmp_path / "early", "58_59")
    session = SessionState()
    ingest_into(session, tmp_path / "early")
    write_ba122(tmp_path / "late", "58")
    write_ba122(tmp_path / "late", "59")
    report = ingest_into(session, tmp_path / "late")                # the summed files are not in this batch
    assert report.dirty
    assert_superseded(session)
    assert not ingest_into(session, tmp_path / "late").dirty


def test_summed_set_arriving_after_its_individual_scans_is_not_read(tmp_path):
    write_ba122(tmp_path / "early", "58")
    write_ba122(tmp_path / "early", "59")
    session = SessionState()
    ingest_into(session, tmp_path / "early")
    write_ba122(tmp_path / "late", "58_59")
    report = ingest_into(session, tmp_path / "late")
    assert [p["scans"] for p in report.passed_over] == [[58, 59]]
    assert summed_entries(session) == [] and report.added == []     # passed over: never entered
    assert len(session.scans) == 2


def test_summed_set_with_only_some_of_its_scans_present_stays_active(tmp_path):
    write_ba122(tmp_path / "early", "58_59")
    session = SessionState()
    ingest_into(session, tmp_path / "early")
    write_ba122(tmp_path / "late", "58")                            # scan 59 never arrives
    report = ingest_into(session, tmp_path / "late")
    assert report.passed_over == []
    (entry,) = summed_entries(session)
    assert entry.status == "ingested" and entry.reasons == []


def test_other_spec_files_scans_do_not_supersede(tmp_path):
    write_ba122(tmp_path / "early", "58_59")
    session = SessionState()
    ingest_into(session, tmp_path / "early")
    write_ba122(tmp_path / "late", "58", spec="spec2")
    write_ba122(tmp_path / "late", "59", spec="spec2")              # same numbers, another experiment
    ingest_into(session, tmp_path / "late")
    assert [e.status for e in summed_entries(session)] == ["ingested"]


# --- run ------------------------------------------------------------------------------------------
def bl43_workspace(tmp_path: Path) -> Workspace:
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(
        (Path(sp8_bl43.__file__).parent / "cards" / "bl43lxu.policy.toml").read_text(encoding="utf-8"),
        encoding="utf-8")
    return ws


def results_rows(ws: Workspace, scan_id_prefix: str) -> list[dict]:
    import csv
    with (ws.report_dir / "results.csv").open(encoding="utf-8", newline="") as stream:
        return [r for r in csv.DictReader(stream) if r["scan_id"].startswith(scan_id_prefix)]




def queued_scan_ids(monkeypatch) -> list:
    """Spy on the fit queue: every scan_id `run_batch` hands to the fitter lands in the list."""
    seen: list = []
    real = driver.scan_delivery._run_tasks

    def spy(tasks, *args, **kwargs):
        seen.extend(t[5] for t in tasks)
        return real(tasks, *args, **kwargs)

    monkeypatch.setattr(driver.scan_delivery, "_run_tasks", spy)
    return seen


def summed_dicts(ws: Workspace) -> list:
    """The summed set's ledger rows as stored (held individual scans are re-queued by every run, so
    only the summed rows can be compared across runs)."""
    return [e.__dict__ for e in summed_entries(ws.load()[1])]


def test_run_excludes_a_summed_set_and_never_fits_or_tabulates_it(tmp_path, monkeypatch):
    data = tmp_path / "data"
    write_ba122(data, "58_59")
    ws = bl43_workspace(tmp_path)
    driver.run_batch(ws, [data], M=10)
    card, session = ws.load()
    (entry,) = summed_entries(session)
    assert entry.status == "held_for_calibration"               # BL43LXU is held for want of an energy zero
    # Stand in for an earlier fit, as the resolution slice will leave one: fitted, with an outcome.
    entry.status, entry.reasons = "fitted", []
    entry.detectors = {"a06": {"q": [4.0, 0.075, 0.0], "verdict": "resolved", "disposition": "accepted"}}
    ws.save(card, session)

    write_ba122(data, "58")
    write_ba122(data, "59")
    queued = queued_scan_ids(monkeypatch)
    summary = driver.run_batch(ws, [data], M=10)
    _card, session = ws.load()
    (entry,) = summed_entries(session)
    assert entry.status == "excluded" and entry.detectors == {} and SUMMED in entry.reasons[0]
    assert entry.scan_id not in queued and sorted(queued) == sorted(e.scan_id for e in individual_entries(session))
    assert all(e.status == "held_for_calibration" for e in individual_entries(session))
    (row,) = results_rows(ws, entry.scan_id)                    # surfaced as an exclusion, with no fit in it
    assert row["status"] == "excluded" and not row["verdict"] and SUMMED in row["reasons"]
    assert summary.counts["by_disposition"] == {} and summary.counts["by_status"]["excluded"] == 1
    from isar.report import run_report
    assert all(scan != entry.scan_number for scan, _d, _s, _o in run_report._outcomes(session))
    before = summed_dicts(ws)
    driver.run_batch(ws, [data], M=10)
    assert summed_dicts(ws) == before                           # a second run leaves the sum alone


def test_run_with_the_individual_scans_in_another_folder_excludes_the_earlier_sum(tmp_path):
    write_ba122(tmp_path / "early", "58_59")
    ws = bl43_workspace(tmp_path)
    driver.run_batch(ws, [tmp_path / "early"], M=10)
    write_ba122(tmp_path / "late", "58")
    write_ba122(tmp_path / "late", "59")
    driver.run_batch(ws, [tmp_path / "late"], M=10)             # the summed files are not in this run
    _card, session = ws.load()
    assert_superseded_status(session)


def test_run_does_not_read_a_summed_set_arriving_after_its_individual_scans(tmp_path, monkeypatch):
    write_ba122(tmp_path / "early", "58")
    write_ba122(tmp_path / "early", "59")
    ws = bl43_workspace(tmp_path)
    driver.run_batch(ws, [tmp_path / "early"], M=10)
    write_ba122(tmp_path / "late", "58_59")
    queued = queued_scan_ids(monkeypatch)
    driver.run_batch(ws, [tmp_path / "late"], M=10)
    _card, session = ws.load()
    assert summed_entries(session) == [] and queued == [] and len(session.scans) == 2


def test_run_keeps_a_summed_set_active_while_only_some_of_its_scans_are_present(tmp_path):
    write_ba122(tmp_path / "early", "58_59")
    ws = bl43_workspace(tmp_path)
    driver.run_batch(ws, [tmp_path / "early"], M=10)
    write_ba122(tmp_path / "late", "58")
    driver.run_batch(ws, [tmp_path / "late"], M=10)
    _card, session = ws.load()
    (entry,) = summed_entries(session)
    assert entry.status == "held_for_calibration" and SUMMED not in " ".join(entry.reasons)


def assert_superseded_status(session: SessionState) -> None:
    (entry,) = summed_entries(session)
    assert entry.status == "excluded" and SUMMED in entry.reasons[0]
    assert {e.status for e in individual_entries(session)} == {"held_for_calibration"}
