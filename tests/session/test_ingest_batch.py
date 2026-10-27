"""`ingest.ingest_batch` — parse into the ledger WITHOUT fitting (Phase B).

The load-bearing contract is precedence: an unchanged re-ingest never downgrades a fitted/held/
excluded entry, and drifted content re-queues (status back to "ingested") while keeping the stale
detectors payload visible. Built on the synthetic S30 emitter so it needs no gitignored data.
"""
from __future__ import annotations

from pathlib import Path

from isar.parsers import spec_s30
from isar.schema import _json
from isar.session import ingest
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace
from isar.synth_format import run_builder


def _emit_s30(path: Path, n_scans=3, seed0=1000) -> "object":
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN", n_scans=n_scans, seed0=seed0)
    path.write_bytes(spec_s30.emit_file(run, card))
    return card


def _emit_s30_with_alignment(path: Path) -> "object":
    """Two fscan measurements + one ascan (motor scan). The parser classifies scan_type from the
    command (`_classify`: fscan→measurement, ascan→alignment), so the ascan is a CONTEXT scan that
    `router.route` partitions out — it must never become a ledger entry."""
    from isar.synth import config, generate
    from isar.synth_format.run_builder import (_DEFAULT_H_FAN, _DEFAULT_QK, _DEFAULT_QL,
                                               build_scan_from_truths)

    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN", n_scans=2)     # scans 1, 2 are fscan measurements
    cfg = config.default_run_config()
    q_list = [[float(h), _DEFAULT_QK, _DEFAULT_QL] for h in _DEFAULT_H_FAN]
    gts = [generate.make_ground_truth(cfg, H=h, target_peak_counts=100.0, seed=5000 + i)
           for i, h in enumerate(_DEFAULT_H_FAN)]
    run.scans.append(build_scan_from_truths(q_list=q_list, gts=gts, scan_number=3,
                                            command="ascan omega 1 2 3"))
    path.write_bytes(spec_s30.emit_file(run, card))
    return card


def test_fresh_ingest_populates_ledger(tmp_path):
    p1 = tmp_path / "runA.spec_supplementary.dat"
    p2 = tmp_path / "runB.spec_supplementary.dat"
    card = _emit_s30(p1, n_scans=2)
    _emit_s30(p2, n_scans=2)
    session = SessionState()

    report = ingest.ingest_batch(card, session, [p1, p2])

    assert report.dirty is True
    assert report.added and not report.updated
    # Every ledger key is the per-detector form exp:scan:det, status "ingested", no detectors yet.
    for key, entry in session.scans.items():
        assert key.count(":") == 2
        assert entry.status == "ingested"
        assert entry.detectors == {}
        assert "source_file" in entry.provenance
        assert "T" in entry.provenance
        assert "q" in entry.provenance
        assert "ingested_at" in entry.provenance
        # scan-level id (exp:scan) is what appears in the report lists, not the ledger key.
        scan_short = ":".join(key.split(":")[:2])
        assert scan_short in report.added
    # measurement manifest rows carry n_scans/scan_ids.
    meas = [f for f in report.files if f["class"] == "measurement"]
    assert len(meas) == 2
    assert all(f["n_scans"] == 2 and len(f["scan_ids"]) == 2 for f in meas)


def test_identical_reingest_is_noop(tmp_path):
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=2)
    session = SessionState()
    ingest.ingest_batch(card, session, [p])
    before = session.to_dict()

    report2 = ingest.ingest_batch(card, session, [p])

    assert report2.added == [] and report2.updated == []
    assert report2.unchanged  # every scan re-listed as unchanged
    assert report2.dirty is False
    assert session.to_dict() == before  # ledger byte-identical


def test_same_scan_in_two_files_dedups(tmp_path):
    p1 = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p1, n_scans=2)
    p2 = tmp_path / "resend" / "run.spec_supplementary.dat"
    p2.parent.mkdir()
    p2.write_bytes(p1.read_bytes())  # same basename -> same experiment_id -> same scan keys
    session = SessionState()

    report = ingest.ingest_batch(card, session, [p1, p2])

    # one ledger entry per (scan, det) despite two source files.
    scan_shorts = {":".join(k.split(":")[:2]) for k in session.scans}
    assert len(scan_shorts) == 2
    assert report.deduped, "a scan sourced from two files must raise a dedup alert"
    for d in report.deduped:
        assert d["kept"] == str(p2.resolve())          # last-wins keeps the later file
        assert d["superseded"] == [str(p1.resolve())]


def test_drifted_content_requeues_but_keeps_detectors(tmp_path):
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1, seed0=1000)
    session = SessionState()
    ingest.ingest_batch(card, session, [p])
    # Simulate a prior fit having populated a detectors payload on one entry.
    key = next(iter(session.scans))
    session.scans[key].status = "fitted"
    session.scans[key].detectors = {"ana1": {"detector_id": "ana1", "omega0": 5.0}}

    # Re-emit the same scan with different counts (growing-file drift) -> new content_hash.
    _emit_s30(p, n_scans=1, seed0=2000)
    report = ingest.ingest_batch(card, session, [p])

    assert report.updated and not report.added
    entry = session.scans[key]
    assert entry.status == "ingested"                            # re-queued for fitting
    assert entry.detectors == {"ana1": {"detector_id": "ana1", "omega0": 5.0}}  # stale fit kept
    assert report.dirty is True


def test_unchanged_reingest_over_fitted_leaves_status(tmp_path):
    """The precedence guarantee: re-ingesting an unchanged file never downgrades a fitted entry."""
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    session = SessionState()
    ingest.ingest_batch(card, session, [p])
    key = next(iter(session.scans))
    # A run turned it into a fitted entry (same content_hash the driver would compute — ingest hashes
    # the reduced+QC'd record, exactly as run_batch does).
    session.scans[key].status = "fitted"
    session.scans[key].detectors = {"ana1": {"omega0": 5.0}}
    frozen = session.scans[key].content_hash

    report = ingest.ingest_batch(card, session, [p])

    entry = session.scans[key]
    assert entry.status == "fitted"                 # untouched
    assert entry.content_hash == frozen
    assert entry.detectors == {"ana1": {"omega0": 5.0}}
    assert ":".join(key.split(":")[:2]) in report.unchanged


def test_foreign_and_unrecognized_make_manifest_rows_not_ledger(tmp_path):
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    (tmp_path / "notes.txt").write_text("not data", encoding="utf-8")   # -> unrecognized
    session = SessionState()

    report = ingest.ingest_batch(card, session, [tmp_path])

    classes = {f["class"] for f in report.files}
    assert "measurement" in classes and "unrecognized" in classes
    # unrecognized file has a manifest row but produced no ledger entry.
    unrec = [f for f in report.files if f["class"] == "unrecognized"]
    assert unrec and unrec[0]["n_scans"] == 0 and unrec[0]["scan_ids"] == []
    for rp, row in session.files.items():
        if row.get("format") == "unrecognized":
            assert "scan_ids" not in row       # only measurements carry scan_ids
    # ledger keys all belong to the measurement file's experiment.
    assert all(k.startswith("run.spec_supplementary.dat:") for k in session.scans)


def test_in_file_context_scan_is_not_ledgered(tmp_path):
    """D1: an in-file alignment (ascan) scan is context, not a result — no ledger entry, counted as
    n_context on the file's manifest row."""
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30_with_alignment(p)
    session = SessionState()

    report = ingest.ingest_batch(card, session, [p])

    scan_shorts = {":".join(k.split(":")[:2]) for k in session.scans}
    assert scan_shorts == {"run.spec_supplementary.dat:1", "run.spec_supplementary.dat:2"}
    assert not any(k.split(":")[1] == "3" for k in session.scans)   # scan 3 (ascan) never ledgered
    meas = [f for f in report.files if f["class"] == "measurement"][0]
    assert meas["n_scans"] == 2 and len(meas["scan_ids"]) == 2
    assert meas["n_context"] == 1                                    # the ascan counted, not ledgered
    assert session.files[str(p.resolve())]["n_context"] == 1


def test_one_bad_file_isolated_good_files_ingested(tmp_path, monkeypatch, caplog):
    """Per-file isolation: a file whose parse raises must not abort the batch. The good file's scans
    land in the ledger; the bad file gets a manifest row carrying an ``error`` (class still
    "measurement"), and the batch continues."""
    good = tmp_path / "good.spec_supplementary.dat"
    bad = tmp_path / "bad.spec_supplementary.dat"
    card = _emit_s30(good, n_scans=2)
    _emit_s30(bad, n_scans=2)                         # valid bytes; we force the PARSE to raise below

    real_parse = spec_s30.parse_file
    bad_rp = str(bad.resolve())

    def flaky_parse(path, card):
        if str(Path(path).resolve()) == bad_rp:
            raise ValueError("simulated corrupt block")
        return real_parse(path, card)

    monkeypatch.setattr(spec_s30, "parse_file", flaky_parse)
    session = SessionState()

    with caplog.at_level("WARNING"):
        report = ingest.ingest_batch(card, session, [good, bad])

    # good file's scans are in the ledger
    good_shorts = {":".join(k.split(":")[:2]) for k in session.scans}
    assert good_shorts == {"good.spec_supplementary.dat:1", "good.spec_supplementary.dat:2"}
    # bad file has a manifest row with an error, still classified measurement
    bad_row = [f for f in report.files if f["path"] == bad_rp]
    assert len(bad_row) == 1
    assert bad_row[0]["class"] == "measurement"
    assert "ValueError: simulated corrupt block" in bad_row[0]["error"]
    assert session.files[bad_rp]["error"] == "ValueError: simulated corrupt block"
    assert report.dirty is True
    assert "failed to parse" in caplog.text


def test_bad_file_error_cleared_when_it_later_parses(tmp_path, monkeypatch):
    """A file that errored once must lose its stale ``error`` key once it parses clean."""
    data = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(data, n_scans=1)
    rp = str(data.resolve())
    session = SessionState()

    real_parse = spec_s30.parse_file
    monkeypatch.setattr(spec_s30, "parse_file",
                        lambda p, c: (_ for _ in ()).throw(ValueError("boom")))
    ingest.ingest_batch(card, session, [data])
    assert session.files[rp].get("error")

    monkeypatch.setattr(spec_s30, "parse_file", real_parse)   # now it parses
    report = ingest.ingest_batch(card, session, [data])
    assert "error" not in session.files[rp]
    assert not any(f.get("error") for f in report.files)


def test_writes_one_unit_file_per_ledger_entry(tmp_path):
    """With ``data_dir``, ingest writes one ``data/ingest/`` file per ledger entry (9 analyzers ×
    scans), and every unit file carries the S30 target #Q as ``q_target`` provenance."""
    from isar.session import unit_data

    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=2)
    data_dir = tmp_path / "ws" / "data"
    session = SessionState()

    ingest.ingest_batch(card, session, [p], data_dir=data_dir)

    unit_files = sorted((data_dir / "ingest").glob("*.json"))
    assert len(unit_files) == len(session.scans) == 18          # 2 scans × 9 analyzers
    # a spot-checked entry's provenance carries q_target (S30 target #Q), distinct from its own q
    entry = next(iter(session.scans.values()))
    assert entry.provenance["q_target"] and len(entry.provenance["q_target"]) == 3
    assert entry.provenance["q_target"] != entry.provenance["q"]
    assert "command" in entry.provenance and "n_points" in entry.provenance
    d = unit_data.read_unit_data(unit_files[0])
    assert d["schema"] == "isar.unit_data/1"


def test_unchanged_reingest_writes_no_unit_files(tmp_path):
    """Idempotency: a re-ingest with every unit file already present writes nothing and stays clean
    (dirty False → caller skips save; no workspace tick)."""
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    data_dir = tmp_path / "ws" / "data"
    session = SessionState()
    ingest.ingest_batch(card, session, [p], data_dir=data_dir)
    ingest_dir = data_dir / "ingest"
    mtimes = {f: f.stat().st_mtime_ns for f in ingest_dir.glob("*.json")}

    report = ingest.ingest_batch(card, session, [p], data_dir=data_dir)

    assert report.dirty is False and not report.added and not report.updated
    assert {f: f.stat().st_mtime_ns for f in ingest_dir.glob("*.json")} == mtimes  # untouched


def test_backfill_missing_unit_file_on_unchanged_without_ticking_dirty(tmp_path):
    """A pre-Phase-2 workspace (ledger present, unit files missing) is repaired by a re-ingest: the
    missing file is rewritten even though the entry is ``unchanged`` — and dirty stays False."""
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    data_dir = tmp_path / "ws" / "data"
    session = SessionState()
    ingest.ingest_batch(card, session, [p], data_dir=data_dir)
    victim = sorted((data_dir / "ingest").glob("*.json"))[0]
    victim.unlink()

    report = ingest.ingest_batch(card, session, [p], data_dir=data_dir)

    assert victim.exists()                      # backfilled
    assert report.dirty is False                # ledger untouched — backfill is not a mutation
    assert report.unchanged and not report.added and not report.updated


def test_unchanged_reingest_repairs_corrupt_and_legacy_only_unit_files(tmp_path):
    """A no-op ingest repairs durable-viewer artifacts without changing the scientific ledger."""
    from isar.session import unit_data

    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    data_dir = tmp_path / "ws" / "data"
    session = SessionState()
    ingest.ingest_batch(card, session, [p], data_dir=data_dir)
    entry = next(iter(session.scans.values()))
    exp, scan, det = entry.scan_id.rsplit(":", 2)
    canonical = unit_data.unit_data_path(data_dir, exp, int(scan), det)
    legacy = unit_data.legacy_unit_data_path(data_dir, exp, int(scan), det)
    original = canonical.read_text(encoding="utf-8")

    # A collision/mismatch or torn canonical file must be replaced on an unchanged re-ingest.
    canonical.write_text("{ torn", encoding="utf-8")
    report = ingest.ingest_batch(card, session, [p], data_dir=data_dir)
    assert report.dirty is False and report.unchanged
    assert unit_data.canonical_unit_data_is_valid(data_dir, exp, int(scan), det)

    # A valid old filename remains readable, but a no-op re-ingest promotes it back to canonical.
    canonical.unlink()
    legacy.write_text(original, encoding="utf-8")
    report = ingest.ingest_batch(card, session, [p], data_dir=data_dir)
    assert report.dirty is False and report.unchanged
    assert canonical.exists() and legacy.exists()       # old scientific artifacts are never deleted


def test_no_data_dir_writes_no_unit_files_and_still_stamps_provenance(tmp_path):
    """``data_dir`` is optional: omitting it writes no unit files but still enriches provenance."""
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    session = SessionState()

    ingest.ingest_batch(card, session, [p])       # no data_dir

    assert not (tmp_path / "data").exists()
    entry = next(iter(session.scans.values()))
    assert "q_target" in entry.provenance and "energy_status" in entry.provenance


def test_ingest_then_run_preserves_manifest_and_requeues(tmp_path):
    """Driver interplay: after `isar ingest`, a later `run_batch` re-queues the ingested entries and
    its manifest write preserves ingest's n_scans/scan_ids (setdefault(...).update merge)."""
    from isar.session import driver

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=2)
    # Stand up a real workspace: copy the pinned card template so Workspace.load resolves it.
    (ws_dir / "card.policy.toml").write_text(
        Path(spec_s30._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    (ws_dir / "card.state.json").write_text("{}", encoding="utf-8")
    SessionState().save(ws_dir / "session.json")

    ws = Workspace.open(ws_dir)
    _card, session = ws.load()
    ingest.ingest_batch(_card, session, [p])
    session.save(ws.session_path)
    rp = str(p.resolve())
    assert session.files[rp]["scan_ids"]              # ingest stamped scan_ids

    driver.run_batch(ws, [p], formats=(), M=8, errors_tier="cheap")
    _card2, session2 = ws.load()
    assert all(e.status != "ingested" for e in session2.scans.values())  # run consumed them
    assert session2.files[rp].get("scan_ids"), "run must preserve ingest's scan_ids (merge, not replace)"

    # The run MERGES provenance onto the ingested entry: ingest-only keys survive AND the run's core
    # per-unit keys are present (so the GUI's q display / #Q labels work in the fit path too).
    e2 = next(iter(session2.scans.values()))
    assert "ingested_at" in e2.provenance                    # ingest-only key survived the run
    assert "resolution_version" in e2.provenance             # run-only key present
    assert e2.provenance.get("q") is not None and e2.provenance.get("T") is not None
    assert e2.provenance.get("q_target") and len(e2.provenance["q_target"]) == 3

    # Re-ingest AFTER the run: the fitted/held entries must read as "unchanged" — this pins driver-vs-
    # ingest content_hash parity end-to-end (both hash the reduced+QC'd record). A hash mismatch would
    # re-queue the whole workspace on every parse-preview.
    report3 = ingest.ingest_batch(_card2, session2, [p])
    assert report3.added == [] and report3.updated == []
    assert report3.unchanged and report3.dirty is False
    assert all(e.status != "ingested" for e in session2.scans.values())  # nothing downgraded


def test_run_only_workspace_stamps_core_provenance_and_unit_files(tmp_path):
    """A run WITHOUT a prior `isar ingest` still stamps the ingest-parity per-unit provenance
    (q/T/q_target/source_file/…) and writes the per-unit data files — so the GUI's per-unit q
    display, scan-group #Q labels, and counts plot all work in the plain fit path."""
    from isar.session import driver

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    p = tmp_path / "run.spec_supplementary.dat"
    card = _emit_s30(p, n_scans=1)
    (ws_dir / "card.policy.toml").write_text(
        Path(spec_s30._CARD_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    (ws_dir / "card.state.json").write_text("{}", encoding="utf-8")
    SessionState().save(ws_dir / "session.json")
    ws = Workspace.open(ws_dir)

    driver.run_batch(ws, [p], formats=(), M=8, errors_tier="cheap")

    _card, session = ws.load()
    assert session.scans                                # entries were created
    for e in session.scans.values():
        assert e.provenance.get("q") is not None        # per-unit analyzer q
        assert e.provenance.get("T") is not None
        assert e.provenance.get("q_target") and len(e.provenance["q_target"]) == 3   # target #Q
        assert e.provenance.get("source_file")          # absolute source path (ingest-consistent)
        assert e.provenance.get("energy_status")        # 3-state calibration, verbatim
    # per-unit q is each analyzer's OWN q — most differ from the scan's single target #Q (one
    # analyzer may legitimately sit at the target), proving q is not just copied from #Q.
    provs = list(session.scans.values())
    assert any(e.provenance["q"] != e.provenance["q_target"] for e in provs)
    # one unit data file per ledger entry, matching `isar ingest`
    unit_files = sorted((ws_dir / "data" / "ingest").glob("*.json"))
    assert len(unit_files) == len(session.scans)


# --- BL43 with a workspace: the import record and the fit history (C-scan-record, C-fit-history) ---
def _bl43_ingest(ws, *folders):
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, list(folders), workspace_root=ws.root)
    session.save(ws.session_path)
    return report


def _mark_fitted(ws, match=lambda e: True):
    """Stand-in for a run: the matching entries fitted, each tied to the inputs it used."""
    _card, session = ws.load()
    for e in session.scans.values():
        if match(e):
            e.status, e.detectors = "fitted", {e.scan_id.rsplit(":", 1)[1]: {"omega0": 4.0}}
            e.provenance = {**e.provenance, "fit_inputs": {"run": "r1", "hash": "7c", "fallbacks": []}}
    session.save(ws.session_path)
    return {k: _json.to_jsonable(e) for k, e in session.scans.items()}


def test_bl43_reimport_keeps_every_replaced_fit_in_the_history(tmp_path):
    from intake_fixtures import bl43_workspace, write_scan
    from isar.intake.fit_inputs import FIT_HISTORY, read_history

    ws, data = bl43_workspace(tmp_path)
    _bl43_ingest(ws, data)
    before = _mark_fitted(ws, lambda e: e.provenance.get("file_set", "").endswith("_A"))   # B stays a placeholder
    assert not _bl43_ingest(ws, data).dirty and not (ws.root / FIT_HISTORY).exists()   # unchanged: nothing
    write_scan(data, stem="Smp_100K_0p5_0p5_3_A", seed=99)
    write_scan(data, stem="Smp_100K_0p5_0p5_3_B", seed=98)
    report = _bl43_ingest(ws, data)
    assert len(report.updated) == 2
    hist = read_history(ws.root)
    assert sorted(h["unit"] for h in hist) == sorted(k for k, e in before.items() if e["status"] == "fitted")
    for h in hist:                                    # in full, as the ledger held it before the import
        assert h["by"] == "reimport" and h["run"] is None and h["entry"] == before[h["unit"]]
        assert h["fit_inputs"] == {"run": "r1", "hash": "7c", "fallbacks": []}
    _card, session = ws.load()
    assert all(session.scans[h["unit"]].status == "ingested" for h in hist)   # the ledger moved on after


def test_bl43_summed_set_superseded_by_its_scans_keeps_its_fit(tmp_path):
    import numpy as np

    from intake_fixtures import workspace
    from isar.intake.fit_inputs import read_history

    def ba122(folder, scans):
        folder.mkdir(parents=True, exist_ok=True)
        counts = (np.arange(8) + 1.0) ** 2
        rows = [f"{e:.4f} {n:.6E} {n ** 0.5:.6E}" for e, n in zip(np.arange(8) * 0.2, counts)]
        (folder / f"spec1_T26K__scan_{scans}_A6_Q_4_0p075_0_inel.dat").write_text(
            "\n".join(rows) + "\n", encoding="utf-8", newline="\n")

    ws = workspace(tmp_path)
    data = tmp_path / "data"
    ba122(data, "58_59")
    _bl43_ingest(ws, data)
    before = _mark_fitted(ws)
    ba122(data, "58")
    ba122(data, "59")
    _bl43_ingest(ws, data)
    (h,) = read_history(ws.root)
    assert h["by"] == "reimport" and h["entry"] == before[h["unit"]]
    assert ws.load()[1].scans[h["unit"]].status == "excluded"


def test_bl43_ingest_never_stops_for_damaged_intake_lines(tmp_path):
    from intake_fixtures import bl43_workspace
    from isar.intake import store
    from isar.intake.scans import read_scans
    from isar.intake.values import save_value

    ws, data = bl43_workspace(tmp_path)
    for name in (store.SETTINGS, store.ENTRIES):
        path = ws.root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'[1, 2]\n{"id": "e000000000002", "seq": 2, "kind": "mystery"}\n{"kind": "torn')
    report = _bl43_ingest(ws, data)
    assert report.curves is not None and "error" not in report.curves
    assert report.curves["problem"] is None and report.curves["source"] == "card"
    assert len(read_scans(ws.root)) == 2 and len(report.added) == 2
    assert save_value(ws.root, "temperature", 100.0, list(read_scans(ws.root))).ok   # a save appends past them
