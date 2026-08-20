"""Step-2 gates (the retired unification build order §10.2, git history): ingest discovers/classifies files, parses via the
card-pinned plugin, dedups (last-wins) and orders by scan number, and detects sequence gaps.

Built on the synthetic emitters so it needs no gitignored data (CI-safe)."""
from types import SimpleNamespace

from isar.parsers import spec_s30
from isar.session import ingest
from isar.synth_format import run_builder


def _emit_s30(tmp_path, name, n_scans, seed0=1000):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN", n_scans=n_scans, seed0=seed0)
    p = tmp_path / f"{name}.spec_supplementary.dat"
    p.write_bytes(spec_s30.emit_file(run, card))
    return p, card


def test_discover_classifies_and_parses(tmp_path):
    p, card = _emit_s30(tmp_path, "run", n_scans=3)
    plan = ingest.discover([tmp_path], card)
    assert p in plan.measurement_files
    assert plan.unrecognized == [] and plan.foreign == []
    recs = ingest.parse_all(plan, card)
    assert [r.scan_number for r in recs] == [1, 2, 3]   # ordered by scan number


def test_skip_suffixes_and_workspace_files(tmp_path):
    p, card = _emit_s30(tmp_path, "run", n_scans=1)
    (tmp_path / "notes.log").write_text("junk", encoding="utf-8")
    (tmp_path / "old.spec.UNUSED").write_text("#F whatever\n", encoding="utf-8")
    (tmp_path / "card.state.json").write_text("{}", encoding="utf-8")
    plan = ingest.discover([tmp_path], card)
    names = {f.name for f in plan.measurement_files}
    assert names == {p.name}                       # only the real supplementary file
    assert plan.unrecognized == []                 # skipped-by-suffix, not opened+unrecognized


def test_dedup_keeps_last_occurrence(tmp_path):
    """Two copies of the same run (growing-file resend) dedup to the unique scan set."""
    p1, card = _emit_s30(tmp_path, "run", n_scans=3)
    # A second path with the same experiment_id + scan numbers.
    p2 = tmp_path / "resend" / "run.spec_supplementary.dat"
    p2.parent.mkdir()
    p2.write_bytes(p1.read_bytes())
    plan = ingest.discover([p1, p2], card)
    assert len(plan.measurement_files) == 2
    recs = ingest.parse_all(plan, card)
    assert [r.scan_number for r in recs] == [1, 2, 3]   # deduped by (experiment_id, scan_number)


def test_file_gaps_detects_holes():
    recs = [SimpleNamespace(experiment_id="E", scan_number=n) for n in (10, 11, 15, 16, 20)]
    gaps = ingest.file_gaps(recs)
    assert ("E", 11, 15) in gaps
    assert ("E", 16, 20) in gaps
    assert len(gaps) == 2


def test_companion_glob_routes_to_calibration(tmp_path):
    """A file matching the calibration companion_glob is a calibration source, never a measurement
    (even if detect() would call it the same instrument) — the R2 guard."""
    p, card = _emit_s30(tmp_path, "run", n_scans=1)
    # Point the card's calibration at a companion glob and drop a matching file in.
    card.policy.calibration = {"method": "bragg_companion", "companion_glob": "companion_*.spec"}
    comp = tmp_path / "companion_bragg.spec"
    comp.write_text("#F companion\n#C herix\n", encoding="utf-8")
    plan = ingest.discover([tmp_path], card)
    assert comp in plan.companion_files
    assert comp not in plan.measurement_files
