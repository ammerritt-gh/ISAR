"""BL43LXU scan identity (design R10): (experiment, scan number(s), analyzer), on files written at test time.

The experiment is the SPEC file name when a header or the file name carries it, else the card's label,
else the file stem. A scan the beamline already summed is passed over at ingest when its individual
scans are in the batch, else it is read and flagged `pre_combined_input`.
"""
from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np
import pytest

from isar.parsers import sp8_bl43
from isar.session import ingest
from isar.session.state import SessionState
from test_bl43_counts import STEM, lattice, parse, write_member, write_var

HASH_BASE = 1_000_000_000


def write_ba122(folder: Path, spec: str, scans: str, analyzer: int = 6, t_k: str = "26") -> Path:
    """One Ba122 export file: energy, intensity, error with N = (y/e)^2 recovering exactly."""
    path = folder / f"{spec}_T{t_k}K__scan_{scans}_A{analyzer}_Q_4_0p075_0_inel.dat"
    counts = (np.arange(8) + 1.0) ** 2
    rows = [f"{e:.4f} {n:.6E} {n ** 0.5:.6E}" for e, n in zip(lattice(0, 8), counts)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


def ingest_folder(folder: Path):
    card, session = sp8_bl43.card_template(), SessionState()
    report = ingest.ingest_batch(card, session, [folder])
    records = ingest.parse_all(ingest.discover([folder], card), card)
    return report, session, {(r.experiment_id, r.scan_number): r for r in records}


def pre_combined(rec) -> list:
    return [f for f in rec.scan_flags if f.type == "pre_combined_input"]


# --- experiment ---------------------------------------------------------------------------------
def test_spec_file_name_is_the_experiment_so_restarting_scan_numbers_do_not_collide(tmp_path):
    write_ba122(tmp_path, "spec1", "58")
    write_ba122(tmp_path, "spec2", "58")                       # spec2 restarts its numbering
    report, session, records = ingest_folder(tmp_path)
    assert report.refused == [] and sorted(report.added) == ["spec1:58", "spec2:58"]
    assert sorted(session.scans) == ["spec1:58:a06", "spec2:58:a06"]
    assert sorted(records) == [("spec1", 58), ("spec2", 58)]
    assert records[("spec1", 58)].facts["specfile"] == {"value": "spec1", "source": "filename"}


def test_experiment_is_header_specfile_then_card_label_then_the_stem(tmp_path):
    path = write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6)
    assert parse(tmp_path).experiment_id == STEM                               # the CsV3Sb5 layout names nothing
    assert parse(tmp_path, sample={"name": "CsV3Sb5"}).experiment_id == "CsV3Sb5"
    assert parse(tmp_path, sample={"experiment_id": "run7", "name": "CsV3Sb5"}).experiment_id == "run7"
    path.write_text("# specfile spec9\n" + path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    rec = parse(tmp_path, sample={"name": "CsV3Sb5"})
    assert rec.experiment_id == "spec9" and rec.facts["specfile"] == {"value": "spec9", "source": "header"}


# --- scan number --------------------------------------------------------------------------------
def test_scan_number_is_the_one_listed_else_a_hash_of_the_set_key(tmp_path):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6)
    assert parse(tmp_path).scan_number == HASH_BASE + zlib.crc32(STEM.encode()) % HASH_BASE
    path = tmp_path / f"{STEM}_a03"
    text = path.read_text(encoding="utf-8")
    path.write_text("# scan 12\n" + text, encoding="utf-8", newline="\n")
    rec = parse(tmp_path)
    assert rec.scan_number == 12 and not pre_combined(rec)
    path.write_text("# scan 12 13\n" + text, encoding="utf-8", newline="\n")
    rec = parse(tmp_path)
    assert rec.scan_number == HASH_BASE + zlib.crc32(STEM.encode()) % HASH_BASE
    assert rec.facts["scan_numbers"] == {"value": [12, 13], "source": "header"}   # the list stays a fact


# --- summed sets --------------------------------------------------------------------------------
def test_summed_set_beside_its_individual_scans_is_passed_over_and_reported(tmp_path):
    for scans in ("58", "59", "58_59"):
        write_ba122(tmp_path, "spec1", scans)
    report, session, records = ingest_folder(tmp_path)
    assert sorted(report.added) == ["spec1:58", "spec1:59"] and report.refused == []
    assert sorted(records) == [("spec1", 58), ("spec1", 59)]
    (passed,) = report.passed_over
    assert passed["set"] == "spec1_T26K__scan_58_59" and passed["scans"] == [58, 59]
    assert "spec1_T26K__scan_58_59" in report.to_jsonable()["passed_over"][0]["set"]
    assert all(p.exists() for p in tmp_path.iterdir())                         # nothing moved or deleted


def test_summed_set_is_not_passed_over_for_another_spec_files_scans(tmp_path):
    write_ba122(tmp_path, "spec1", "58_59")
    write_ba122(tmp_path, "spec2", "58")
    write_ba122(tmp_path, "spec2", "59")                                       # same numbers, other experiment
    report, _, records = ingest_folder(tmp_path)
    assert report.passed_over == [] and len(report.added) == 3
    (summed,) = [r for r in records.values() if r.experiment_id == "spec1"]
    assert len(pre_combined(summed)) == 1


def test_summed_set_beside_only_part_of_its_scans_is_read_and_flagged_pre_combined(tmp_path):
    write_ba122(tmp_path, "spec1", "58")
    write_ba122(tmp_path, "spec1", "58_59")
    report, session, records = ingest_folder(tmp_path)
    assert report.passed_over == [] and report.refused == [] and len(report.added) == 2
    summed = next(r for k, r in records.items() if k[1] >= HASH_BASE)
    assert summed.facts["scan_numbers"]["value"] == [58, 59] and summed.experiment_id == "spec1"
    (flag,) = pre_combined(summed)
    assert flag.severity == "warning" and "58" in flag.reason
    assert not pre_combined(records[("spec1", 58)])                            # a single scan is not flagged


@pytest.mark.parametrize("nscans, flagged", [(2, True), (1, False)])
def test_set_whose_var_reports_merged_scans_is_flagged_pre_combined(tmp_path, nscans, flagged):
    write_member(tmp_path, "a03", lattice(0, 10), np.ones(10), 1e6)
    var = write_var(tmp_path, [3])
    var.write_text(var.read_text(encoding="utf-8").replace("nscans =     2", f"nscans =     {nscans}"),
                   encoding="utf-8", newline="\n")
    assert bool(pre_combined(parse(tmp_path))) is flagged
