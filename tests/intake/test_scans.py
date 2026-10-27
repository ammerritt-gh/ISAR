"""The import record report/intake_scans.json (C-scan-record): file facts per scan, merged by key."""
import json

from intake_fixtures import bl43_workspace, rows_for, write_scan
from isar.intake import scans
from isar.intake.scans import INTAKE_SCANS, read_scans, record_import
from isar.parsers import sp8_bl43
from isar.session import ingest


def _ingest(ws, data):
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    session.save(ws.session_path)
    return report


def test_applies_to_bl43_only():
    card = sp8_bl43.card_template()
    assert scans.applies(card)
    card.policy.plugin = "spec_s30"
    assert not scans.applies(card)


def test_missing_record_reads_empty(tmp_path):
    assert read_scans(tmp_path) == {}


def test_row_holds_file_facts_analyzers_and_flags(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",), slit=(60, 60))
    (row,) = rows_for(ws, data)
    exp, num = row["scan"].split(":")
    assert (row["experiment_id"], str(row["scan_number"])) == (exp, num) and exp == "Smp"
    assert row["set"] == "Smp_100K_0p5_0p5_3_A"
    assert row["facts"]["T_K"] == {"value": 100.0, "source": "filename"}
    assert row["facts"]["slit"]["value"] == "60x60" and row["facts"]["slit"]["source"] == "header"
    assert sorted(row["analyzers"]) == ["a03", "a04", "a05"]
    assert row["analyzers"]["a03"]["q_source"] == "none" and row["analyzers"]["a03"]["q"] is None
    assert row["files"]["a03"] == "Smp_100K_0p5_0p5_3_A_a03"
    assert any(f["type"] == "missing_sidecar" for f in row["flags"])


def test_ingest_writes_it_and_an_unchanged_reingest_leaves_it_byte_identical(tmp_path):
    ws, data = bl43_workspace(tmp_path)
    _ingest(ws, data)
    path = ws.root / INTAKE_SCANS
    first = path.read_bytes()
    assert len(read_scans(ws.root)) == 2 and json.loads(first)["schema"] == scans.SCHEMA
    _ingest(ws, data)
    assert path.read_bytes() == first
    assert record_import(ws.root, rows_for(ws, data)) is False   # nothing changed (new imported_at only)


def test_new_counts_replace_that_row_only_and_a_scan_absent_from_the_batch_keeps_its_row(tmp_path):
    ws, data = bl43_workspace(tmp_path)
    _ingest(ws, data)
    before = read_scans(ws.root)
    key_a = next(k for k, r in before.items() if r["set"].endswith("_A"))
    key_b = next(k for k in before if k != key_a)
    write_scan(data, stem="Smp_100K_0p5_0p5_3_A", seed=99)                  # A regenerated, different counts
    only_a = [r for r in rows_for(ws, data) if r["scan"] == key_a]
    assert record_import(ws.root, only_a) is True
    after = read_scans(ws.root)
    assert after[key_a]["data_revision"] != before[key_a]["data_revision"]
    assert after[key_a]["imported_at"] != before[key_a]["imported_at"]
    assert after[key_b] == before[key_b]


def test_unknown_keys_and_imported_at_are_kept(tmp_path):
    record_import(tmp_path, [{"scan": "spec1:42", "data_revision": "aa", "imported_at": "t0"}])
    path = tmp_path / INTAKE_SCANS
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["files"] = {"x": 1}                                   # S3's top-level section
    doc["scans"]["spec1:42"]["import"] = {"gaps": []}          # a row key this writer does not make
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    assert record_import(tmp_path, [{"scan": "spec1:42", "data_revision": "aa", "imported_at": "t1"}]) is False
    assert record_import(tmp_path, [{"scan": "spec1:42", "data_revision": "bb", "imported_at": "t2"},
                                    {"scan": "spec1:43", "data_revision": "cc", "imported_at": "t2"}]) is True
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["files"] == {"x": 1}
    assert doc["scans"]["spec1:42"] == {"scan": "spec1:42", "data_revision": "bb", "imported_at": "t2",
                                        "import": {"gaps": []}}
    assert sorted(doc["scans"]) == ["spec1:42", "spec1:43"]


def test_non_bl43_ingest_writes_no_record(tmp_path):
    ws, data = bl43_workspace(tmp_path)
    card, session = ws.load()
    card.policy.plugin = "spec_s30"
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    assert not (ws.root / INTAKE_SCANS).exists()
