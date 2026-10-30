"""What an import leaves out, recorded in report/intake_scans.json (C-scan-import, the S3U1 half).

Per scan: the sidecar's state, the analyzers left out and why, fact-shaped lines ISAR does not read, each
analyzer's exposure source. Per batch: the files not read and why. The worked examples E1-E5 of the plan
are asserted here; the reader of this record (S3U2) is tested against hand-built rows equal to them.
"""
import json
import re
import shutil

from artificial import BL43_A03, BL43_A04, BL43_VAR
from intake_fixtures import ENERGY_120, STEM, bl43_workspace, workspace, write_library, write_scan
from isar.intake import scans
from isar.intake.scans import INTAKE_SCANS, read_scans
from isar.parsers import sp8_bl43
from isar.session import ingest

ART_IN8 = BL43_A03.parents[1] / "ILL IN8" / "055177"


def _ingest(ws, *folders):
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, list(folders), workspace_root=ws.root)
    session.save(ws.session_path)
    return report


def _doc(ws) -> dict:
    return json.loads((ws.root / INTAKE_SCANS).read_text(encoding="utf-8"))


def _artificial_copy(folder):
    folder.mkdir(parents=True, exist_ok=True)
    for src in (BL43_A03, BL43_A04, BL43_VAR):
        shutil.copy(src, folder / src.name)
    return folder


def _only_row(ws) -> dict:
    (row,) = read_scans(ws.root).values()
    return row


def test_e1_artificial_copy_with_its_sidecar_names_the_26_analyzers_it_lacks(tmp_path):
    ws = workspace(tmp_path)
    data = _artificial_copy(tmp_path / "data")
    (data / "CsV3Sb5_302K_0p5_0p5_3_sum.pkpos").write_text("peak positions\n", encoding="utf-8", newline="\n")
    _ingest(ws, data)
    row = _only_row(ws)
    gaps = row["import"]
    assert gaps["sidecar"] == {"state": "read", "reason": None}
    listed = {sp8_bl43.detector_id(n) for n in sp8_bl43.read_var(BL43_VAR)["arrays"]["ano"].values()}
    assert len(listed) == 28 and sorted(gaps["left_out"]) == sorted(listed - {"a03", "a04"})
    assert len(gaps["left_out"]) == 26
    assert all(v == {"file": None, "cause": "no file"} for v in gaps["left_out"].values())
    assert gaps["unrecognised_lines"] == [] and gaps["unparsed_lines"] == []
    assert sorted(row["analyzers"]) == ["a03", "a04"]
    for a in row["analyzers"].values():
        assert a["q_source"] == "none" and a["exposure"] == "monitor"
        assert set(a["counts"]) == {"origin", "family", "bins_tested", "bins_off", "max_deviation", "level"}
    assert "not_raw" not in row and row["sidecar"] == BL43_VAR.name
    ((path, pk),) = _doc(ws)["files"].items()
    assert path.endswith(".pkpos") and pk["class"] == "ignored" and pk["set"] is None and pk["seen_at"]


def test_e2_a_set_without_its_sidecar_says_so_and_a_later_sidecar_closes_it(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",))
    report = _ingest(ws, data)
    assert report.added
    gaps = _only_row(ws)["import"]
    assert gaps["sidecar"]["state"] == "missing" and gaps["sidecar"]["reason"]
    assert gaps["left_out"] == {}                                       # no .var: no expected list
    write_scan(data, stem=f"{STEM}_A", energy=ENERGY_120, centroids={"a03": 0.1})   # the .var arrives
    report = _ingest(ws, data)                                          # the scan's folder, imported again
    assert report.updated and not report.added
    row = _only_row(ws)
    assert row["import"]["sidecar"] == {"state": "read", "reason": None} and row["import"]["left_out"] == {}
    assert row["sidecar"] == f"{STEM}_A.var"


def test_e3_a_renamed_temperature_trailer_is_listed_with_its_line(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",))
    path = data / f"{STEM}_A_a03"
    path.write_text(path.read_text(encoding="utf-8") + "#Temp=  416.000 K\n", encoding="utf-8", newline="\n")
    _ingest(ws, data)
    assert _only_row(ws)["import"]["unrecognised_lines"] == [
        {"analyzer": "a03", "position": "trailer", "key": "Temp", "line": "#Temp=  416.000 K"}]


def test_e4_an_unrelated_batch_leaves_earlier_rows_and_file_entries_byte_identical(tmp_path):
    ws, data_a = bl43_workspace(tmp_path, stems=("A",))
    (data_a / "notes.txt").write_text("beam was down\n", encoding="utf-8", newline="\n")
    _ingest(ws, data_a)
    before = _doc(ws)
    (key_a,) = before["scans"]
    data_b = tmp_path / "other"
    write_scan(data_b, stem=f"{STEM}_B", seed=7, energy=ENERGY_120)
    _ingest(ws, data_b)
    after = _doc(ws)
    assert sorted(after["scans"]) != [key_a] and after["scans"][key_a] == before["scans"][key_a]
    assert after["files"] == before["files"]
    (entry,) = before["files"].values()
    assert entry["class"] == "unrecognised"


def test_e5_a_folder_with_no_bl43_set_still_writes_the_record_and_lists_its_files(tmp_path):
    ws = workspace(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / "notes.txt").write_text("beam was down\n", encoding="utf-8", newline="\n")
    shutil.copy(ART_IN8, data / ART_IN8.name)
    assert not (ws.root / INTAKE_SCANS).exists()
    _ingest(ws, data)
    doc = _doc(ws)
    assert doc["scans"] == {}
    classes = {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: e["class"] for p, e in doc["files"].items()}
    assert classes == {"notes.txt": "unrecognised", ART_IN8.name: "foreign"}
    first = (ws.root / INTAKE_SCANS).read_bytes()
    _ingest(ws, data)                                   # nothing changed: byte-identical (seen_at kept)
    assert (ws.root / INTAKE_SCANS).read_bytes() == first


def test_a_file_that_is_read_later_loses_its_entry(tmp_path):
    ws = workspace(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    lone = data / f"{STEM}.var"                         # a sidecar alone is not data
    lone.write_text("silicon_order = 4\n", encoding="utf-8", newline="\n")
    _ingest(ws, data)
    assert [e["class"] for e in _doc(ws)["files"].values()] == ["unrecognised"]
    write_scan(data, stem=STEM, energy=ENERGY_120, centroids={"a03": 0.1})
    _ingest(ws, data)
    assert _doc(ws).get("files", {}) == {} and len(read_scans(ws.root)) == 1


def test_a_set_that_cannot_be_parsed_lists_its_files_as_failed(tmp_path):
    ws = workspace(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    bad = data / "Bad_100K_0p5_0p5_3_a03"
    bad.write_text("#      meV      secs   npts          norm_i             y_i            ye_i\n",
                   encoding="utf-8", newline="\n")                       # a label and no data rows
    _ingest(ws, data)
    ((path, entry),) = _doc(ws)["files"].items()
    assert path.endswith("Bad_100K_0p5_0p5_3_a03")
    assert entry["class"] == "failed" and entry["set"] == "Bad_100K_0p5_0p5_3"
    assert "no analyzer could be read" in entry["reason"] and "holds no data rows" in entry["reason"]


def test_curve_files_inside_the_data_folder_are_never_listed(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",))
    write_library(data / "rf")                                           # 9 curve files beside the scans
    card, _ = ws.load()
    assert len(ingest.discover([data], card).ignored_files) == 9          # (the ignore list does catch them)
    report = _ingest(ws, data)
    assert report.curves and not report.curves.get("error")
    assert not [p for p in _doc(ws).get("files", {}) if "rf" in p.replace("\\", "/").split("/")]


def test_an_explicit_reimport_reads_every_file_again_after_the_reader_is_fixed(tmp_path, monkeypatch):
    ws, data = bl43_workspace(tmp_path, stems=("A",))
    path = data / f"{STEM}_A_a03"
    path.write_text(path.read_text(encoding="utf-8") + "#Temp=  416.000 K\n", encoding="utf-8", newline="\n")
    assert _ingest(ws, data).added
    assert len(_only_row(ws)["import"]["unrecognised_lines"]) == 1
    # the shipped reader is corrected to know `#Temp=` as the temperature; the files are the same bytes
    monkeypatch.setattr(sp8_bl43, "_TRAILER", re.compile(r"#(Q|T|Temp)=\s*(\S*)"))
    monkeypatch.setitem(sp8_bl43._TRAILER_KEYS, "Temp", "T_K")
    report = _ingest(ws, data)
    assert report.updated and not report.unchanged
    row = _only_row(ws)
    assert row["import"]["unrecognised_lines"] == []
    assert row["flags"] == [f for f in row["flags"] if f["type"] != "unrecognised_line"]


def test_the_import_block_is_absent_for_a_record_that_carries_no_reader_keys(tmp_path):
    from intake_fixtures import ANALYZERS
    from isar.session.state import content_hash
    write_scan(tmp_path / "d", energy=ENERGY_120)
    card = sp8_bl43.card_template()
    rec = sp8_bl43.parse_set(ingest.discover([tmp_path / "d"], card).file_sets[0], card)[0]
    rec.raw_header = {k: v for k, v in rec.raw_header.items() if k in ("files", "sidecar")}
    row = scans.scan_row(rec, content_hash(rec))
    assert "import" not in row and sorted(row["analyzers"]) == list(ANALYZERS)
    assert all(a["exposure"] == "monitor" for a in row["analyzers"].values())


# --- dropped files import like a folder (S1U1) --------------------------------------------------
STEM_NAME = "CsV3Sb5_302K_0p5_0p5_3_sum"


def _no_stamp(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "imported_at"}


def _files_only(ws, data):
    return _ingest(ws, data / BL43_A03.name, data / BL43_A04.name)


def test_files_only_and_folder_imports_give_the_same_row_and_ledger(tmp_path):
    w1, w2 = workspace(tmp_path / "one"), workspace(tmp_path / "two")
    data = _artificial_copy(tmp_path / "data")
    r1 = _ingest(w1, data)
    r2 = _files_only(w2, data)
    row1, row2 = _only_row(w1), _only_row(w2)
    assert _no_stamp(row1) == _no_stamp(row2)
    assert row2["sidecar"] == BL43_VAR.name and row2["import"]["sidecar"]["state"] == "read"
    assert not [f for f in row2["flags"] if f["type"] == "missing_sidecar"]
    c1, _ = w1.load()
    c2, s2 = w2.load()
    h1 = {k: e.content_hash for k, e in w1.load()[1].scans.items()}
    assert h1 and h1 == {k: e.content_hash for k, e in s2.scans.items()}
    assert r2.companions == r1.companions
    assert r2.companions["sidecar"] == {"read": [STEM_NAME], "missing": [], "unreadable": []}
    g = r2.companions["gpi"]
    assert g == {"folder": None, "found": [], "missing": [STEM_NAME], "not_used": [], "error": None}
    c = r2.companions["curves"]
    assert c["folder"] is None and c["problem"].startswith("no resolution folder is saved") and c["n_curves"] == 0


def _mark_fitted(ws):
    card, session = ws.load()
    for e in session.scans.values():
        e.status = "fitted"
    session.save(ws.session_path)


def _statuses(ws):
    return {k: e.status for k, e in ws.load()[1].scans.items()}


def test_importing_again_repairs_a_row_written_without_the_var_when_the_ledger_is_current(tmp_path):
    ws = workspace(tmp_path / "w")
    data = _artificial_copy(tmp_path / "data")
    _ingest(ws, data)
    good = _only_row(ws)
    _mark_fitted(ws)
    bare = _artificial_copy(tmp_path / "bare")                     # the stale state: a row from a no-.var parse
    (bare / BL43_VAR.name).unlink()
    other = workspace(tmp_path / "other")
    _ingest(other, bare)
    stale = _only_row(other)
    assert stale["data_revision"] != good["data_revision"]
    scans.record_import(ws.root, [stale])
    assert _only_row(ws)["data_revision"] == stale["data_revision"]

    report = _files_only(ws, data)

    assert _no_stamp(_only_row(ws)) == _no_stamp(good)
    assert report.unchanged == [good["scan"]] and report.updated == [] and report.added == []
    assert set(_statuses(ws).values()) == {"fitted"}


def test_importing_again_after_the_var_arrives_supersedes_the_fits_into_the_history(tmp_path):
    ws = workspace(tmp_path / "w")
    data = _artificial_copy(tmp_path / "data")
    (data / BL43_VAR.name).unlink()
    _files_only(ws, data)
    assert _only_row(ws)["import"]["sidecar"]["state"] == "missing"
    _mark_fitted(ws)
    shutil.copy(BL43_VAR, data / BL43_VAR.name)

    report = _files_only(ws, data)

    assert report.updated and not report.unchanged
    assert _only_row(ws)["import"]["sidecar"]["state"] == "read"
    assert set(_statuses(ws).values()) == {"ingested"}
    lines = (ws.root / "report" / "fit_history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(_statuses(ws))


def test_a_set_with_no_var_and_a_zero_byte_var_are_named_in_the_companions(tmp_path):
    bare = _artificial_copy(tmp_path / "bare")
    (bare / BL43_VAR.name).unlink()
    report = _ingest(workspace(tmp_path / "a"), bare)
    assert report.companions["sidecar"] == {"read": [], "missing": [STEM_NAME], "unreadable": []}
    (bare / BL43_VAR.name).write_bytes(b"")
    ws = workspace(tmp_path / "b")
    report = _ingest(ws, bare)
    assert report.companions["sidecar"]["unreadable"] == [STEM_NAME]
    assert len([f for f in _only_row(ws)["flags"] if f["type"] == "missing_sidecar"]) == 1


def test_the_gpi_lists_cover_only_this_import_and_a_non_bl43_card_has_no_companions(tmp_path):
    from isar.intake import settings as intake_settings
    ws = workspace(tmp_path / "w")
    a = _artificial_copy(tmp_path / "a")
    gdir = tmp_path / "gpi"
    gdir.mkdir()
    (gdir / f"gpi.{STEM_NAME}").write_text("not a gpi file\n", encoding="utf-8")
    assert intake_settings.save_gpi_folder(ws.root, gdir).ok
    first = _ingest(ws, a)
    assert first.companions["gpi"]["found"] == [STEM_NAME] and first.companions["gpi"]["folder"] == str(gdir)
    b = tmp_path / "b"
    b.mkdir()
    for src in (BL43_A03, BL43_A04, BL43_VAR):
        shutil.copy(src, b / src.name.replace(STEM_NAME, "CsV3Sb5_303K_0p5_0p5_3_sum"))
    second = _ingest(ws, b)
    g = second.companions["gpi"]
    assert g["found"] == [] and g["missing"] == ["CsV3Sb5_303K_0p5_0p5_3_sum"]
    assert ingest.IngestReport().companions is None and ingest.IngestReport().to_jsonable()["companions"] is None
