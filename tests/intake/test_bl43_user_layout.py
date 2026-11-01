"""A BL43 beamtime folder as users delivered it (2026-11-01): `RT` for the temperature in the file names,
and the data, the resolution curves and the gpi files in sibling folders under one top folder, with macOS
metadata files beside them. Everything is generated at test time; nothing reads `real_data/`."""
from __future__ import annotations

from pathlib import Path

import isar
from intake_fixtures import workspace, write_gpi, write_library, write_scan
from isar.intake.gpi import read_gpi_check
from isar.intake.settings import current_settings
from isar.parsers import sp8_bl43
from isar.session import ingest

STEM = "Smp_RT_0p5_0p5_3"
Q = {"a03": 23.22, "a04": 24.68, "a05": 26.14}          # invented |Q| in 1/nm, written to the data and the gpi file
HKL = {"a03": [0.4, 0.4, 2.8], "a04": [0.45, 0.45, 2.9], "a05": [0.5, 0.5, 3.0]}


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parent.parent == Path(__file__).resolve().parents[2]


def _users_layout(top: Path) -> None:
    """``top/<data>``, ``top/resolution_function/rf`` and ``top/gpi/ici_gpi_qpos`` (with the beamline's other
    per-set files), plus ``._*`` and ``.DS_Store`` beside them."""
    data = top / "test_RT_0p5_0p5_3"
    write_scan(data, stem=STEM, q_nm=Q)
    (data / f"{STEM}.pkpos").write_text("peaks\n", encoding="utf-8", newline="\n")
    (data / f"._{STEM}_a03").write_bytes(b"\x00\x05\x16\x07 AppleDouble")
    write_library(top / "resolution_function" / "rf")
    (top / "resolution_function" / "rf" / "._sarf_t_s2_40_40_a03").write_bytes(b"\x00\x05\x16\x07")
    write_gpi(top / "gpi", STEM, q_nm=Q, hkl=HKL, sarf_name="sarf_t_s2_40_40")
    gpi = (top / "gpi" / "gpi_files").rename(top / "gpi" / "ici_gpi_qpos")
    for name in (f"gpi.hkl_pos.{STEM}", f"ici.{STEM}", f"yaml.{STEM}", ".DS_Store", f"._yaml.{STEM}"):
        (gpi / name).write_text("not read\n", encoding="utf-8", newline="\n")


def test_rt_in_the_file_name_is_300_k_from_the_file_name(tmp_path):
    write_scan(tmp_path, stem=STEM)
    plan = ingest.discover([tmp_path], sp8_bl43.card_template())
    (rec,) = ingest.parse_all(plan, sp8_bl43.card_template())
    assert rec.facts["T_K"] == {"value": 300.0, "source": "filename"}


def test_a_file_s_own_temperature_beats_rt_without_a_conflict(tmp_path):
    write_scan(tmp_path, stem=STEM)
    for f in tmp_path.glob(f"{STEM}_a*"):                    # the trailing `#T=` line the beamline writes
        f.write_text(f.read_text(encoding="utf-8") + "#T=  295.000 K\n", encoding="utf-8", newline="\n")
    (rec,) = ingest.parse_all(ingest.discover([tmp_path], sp8_bl43.card_template()), sp8_bl43.card_template())
    assert rec.facts["T_K"]["value"] == 295.0 and "conflicts" not in rec.facts["T_K"]
    assert not [f for f in rec.scan_flags if f.type == "fact_conflict"]


def test_importing_the_top_folder_reads_the_scan_and_finds_both_sidecar_folders(tmp_path):
    top = tmp_path / "test_RT_0p5_0p5_3"
    _users_layout(top)
    ws = workspace(tmp_path, sample="Smp")
    card, session = ws.load()

    plan = ingest.discover([top], card)
    assert [f.name for f in plan.measurement_files] == [f"{STEM}_a03", f"{STEM}_a04", f"{STEM}_a05"]   # no ._ copy

    ingest.ingest_batch(card, session, [top], workspace_root=ws.root)
    ws.save(card, session)
    saved = current_settings(ws.root)
    assert saved.resolution_folder.path == str((top / "resolution_function" / "rf").resolve())
    assert saved.gpi_folder.path == str((top / "gpi" / "ici_gpi_qpos").resolve())
    assert (saved.resolution_folder.source, saved.gpi_folder.source) == ("discovered", "discovered")
    (check,) = read_gpi_check(ws.root).scans.values()
    assert check.accepted, check.problem
    (rec,) = ingest.parse_all(ingest.discover([top], card), card)
    assert rec.facts["T_K"]["value"] == 300.0


def test_importing_only_the_data_folder_finds_no_sibling_folder(tmp_path):
    top = tmp_path / "test_RT_0p5_0p5_3"
    _users_layout(top)
    ws = workspace(tmp_path, sample="Smp")
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, [top / "test_RT_0p5_0p5_3"], workspace_root=ws.root)
    saved = current_settings(ws.root)
    assert saved.resolution_folder is None and saved.gpi_folder is None      # nothing above the data is searched
    assert "in or below" in report.curves["problem"]
