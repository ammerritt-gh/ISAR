"""Measurement-arena records (SCENE-4 PR 4.0, work package #26): counts are authoritative, fits
are disposable.

Pins two things: the producer (``isar.session.scan_delivery._process_one``) builds and attaches a
TRUSTED record BEFORE ``fit_one`` is ever called, so it survives a fit crash on the same arena
untouched; and the terminal gate outcomes (excluded / held_for_calibration / undersampled) each
get their own count-untrusted record. The durable jsonl log itself
(``isar.session.measurement_store``) is pinned separately, at the append + last-wins level.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import Voigt
from isar.schema import card as card_mod
from isar.schema.measurement import MEASUREMENT_ARENA_SCHEMA
from isar.session import guidelines as guide_mod
from isar.session import measurement_store, scan_delivery
from isar.session import calibrations


def _elastic_ctx(seed=11):
    x = np.linspace(-10.0, 10.0, 41)
    lam = 20.0 + Voigt(x, 400.0, 1.0, 0.0, 0.0)
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute")


def _card(**state_kw):
    policy = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="absolute", energy_status="absolute",
        measurement_pattern="fscan", alignment_pattern="ascan", temperature_rule={},
        collation={}, fitting={}, dispersion={})
    return card_mod.Card(policy, card_mod.CardState(**state_kw))


@dataclass
class _CalRes:
    """Stands in for :class:`isar.session.calibrations.CalibResolution`: only the fields
    ``_process_one``/``_measurement_record`` actually read."""
    held: bool = False
    held_reason: str | None = None
    resolution_version: str = "v1"

    def summary(self) -> dict:
        return {"resolution_version": self.resolution_version}


def _rec(scan_number=1):
    """A minimal stand-in with only the attributes the producer touches for a gated/undersampled
    or a pre-fit trusted record (never a real ScanRecord — building one is exercised by the
    driver-level tests below instead)."""
    return type("Rec", (), {"scan_number": scan_number, "scan_type": "measurement",
                             "raw_header": None, "normalization": None})()


_WORKING = (None, None, None, None, False, 5.0, None, None, None, [])  # patched in per test


def _stub_context(monkeypatch, ctx):
    working = (ctx, None, None, None, False, 5.0, None, None, None, [])
    monkeypatch.setattr(scan_delivery, "_resolve_prior_and_context", lambda *a, **k: working)


def test_a_fit_one_crash_still_leaves_the_trusted_record_behind(monkeypatch):
    """The hard constraint: the arena's measurement record is built and attached BEFORE
    ``fit_one`` is even called, so a caught exception (the entry becomes ``non_result``) does not
    erase it."""
    ctx = _elastic_ctx()
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    _stub_context(monkeypatch, ctx)

    def boom(*_a, **_kw):
        raise RuntimeError("fitter exploded")
    monkeypatch.setattr(scan_delivery, "fit_one", boom)

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", _card(), None, None, set(),
        "ch", "gh", M=10, seed=1)

    assert entry.status == "non_result"
    assert any("fitter exploded" in r for r in entry.reasons)
    record = entry.measurement_record
    assert record is not None
    assert record["schema"] == MEASUREMENT_ARENA_SCHEMA
    assert record["scan_ref"] == record["scan_id"] == "exp:1:ana7"
    assert record["count_trust"] == "trusted"
    assert record["reasons"] == []
    assert record["q"] == [1.0, 0.0, 0.0]
    assert record["sample"] == "FeSe"
    assert record["conditions"] == [["temperature_K", 50.0]]
    # Built from the SAME ctx a delivered fit would have used — never None for a placeable zero.
    assert record["energy_window"] is not None
    assert record["energy_bands"]


def test_a_successful_fit_carries_the_same_trusted_record(monkeypatch):
    """Not only the crash path: an ordinary delivered fit carries the identical record too, so a
    campaign's measurements.jsonl covers every trusted arena, converged or not."""
    ctx = _elastic_ctx()
    card = _card()
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    _stub_context(monkeypatch, ctx)

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", card,
        guide_mod.resolve(card), None, set(), "ch", "gh", M=10, seed=1)

    assert entry.status == "fitted"
    assert entry.measurement_record is not None
    assert entry.measurement_record["count_trust"] == "trusted"


@pytest.mark.parametrize("gated_status,reason", [
    ("excluded", "excluded (card exclusions_seed / state)"),
    ("held_for_calibration", "no energy zero in calibration coverage"),
])
def test_gate_refusals_are_recorded_attempted_not_trusted(monkeypatch, gated_status, reason):
    """A terminal gate outcome never builds a context, so its record carries no window/bands and
    is never "trusted" — PR 4.0's whole point is that this must not silently earn coverage."""
    from isar.session.state import ScanEntry
    gated = ScanEntry(scan_id="exp:1:ana7", scan_number=1, scan_type="measurement",
                      status=gated_status, reasons=[reason])
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (gated, None))

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", _card(), None, None, set(),
        "ch", "gh", M=10, seed=1)

    assert entry.status == gated_status
    record = entry.measurement_record
    assert record["count_trust"] == gated_status
    assert record["reasons"] == [reason]
    assert record["energy_window"] is None and record["energy_bands"] is None
    assert record["q"] == [1.0, 0.0, 0.0]           # q is known before any gate fires


def test_undersampled_is_recorded_attempted_not_trusted(monkeypatch):
    from isar.session.state import ScanEntry
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    undersampled = ScanEntry(scan_id="exp:1:ana7", scan_number=1, scan_type="measurement",
                             status="non_result", reasons=["undersampled: 2 < 5 kept points"])
    monkeypatch.setattr(scan_delivery, "_resolve_prior_and_context",
                        lambda *a, **k: undersampled)

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", _card(), None, None, set(),
        "ch", "gh", M=10, seed=1)

    assert entry.status == "non_result"
    assert entry.measurement_record["count_trust"] == "undersampled"


def test_a_non_finite_energy_axis_is_a_terminal_non_result_not_a_batch_abort(monkeypatch):
    """Review finding 9: ``_energy_bands`` rejects a non-finite value anywhere in ``ctx.x`` (even
    a masked-out one). That ValueError must not escape ``_process_one`` and abort the whole batch
    — it becomes a terminal ``non_result`` carrying its (energy-field-less) measurement record."""
    ctx = _elastic_ctx()
    broken_x = ctx.x.copy()
    broken_x[0] = float("nan")
    ctx = replace(ctx, x=broken_x)
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    _stub_context(monkeypatch, ctx)

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", _card(), None, None, set(),
        "ch", "gh", M=10, seed=1)

    assert entry.status == "non_result"
    assert any("energy window error" in r for r in entry.reasons)
    record = entry.measurement_record
    assert record is not None
    assert record["count_trust"] == "trusted"
    assert record["energy_window"] is None and record["energy_bands"] is None
    assert any("energy window error" in r for r in record["reasons"])


def _driven_rec(job_id="j1"):
    """A scan whose raw header names the TAVI job the driver submitted — the join
    ``_acquisition_provenance_for`` uses to find the request's provenance record."""
    return type("Rec", (), {"scan_number": 1, "scan_type": "measurement",
                            "raw_header": {"tavi": {"job_id": job_id}},
                            "normalization": None})()


_PROVENANCE = {"job_id": "j1", "acquisition_id": "acq:1",
               "topology_role": "canonical_path", "program_fingerprint": "pf",
               "program_step": "step-1", "path_fingerprint": "pathfp"}


def test_the_request_provenance_block_is_stamped_verbatim_on_the_record(monkeypatch):
    """SCENE-4 PR 4.3 (D2): the driver owns the campaign facts, the delivery pipeline owns the
    record — the four fields travel on the persisted request artifact and are copied, never
    re-derived here."""
    ctx = _elastic_ctx()
    card = _card(acquisition_provenance=[dict(_PROVENANCE)])
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    _stub_context(monkeypatch, ctx)

    entry = scan_delivery._process_one(
        _driven_rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", card,
        guide_mod.resolve(card), None, set(), "ch", "gh", M=10, seed=1)

    record = entry.measurement_record
    assert record["topology_role"] == "canonical_path"
    assert record["program_fingerprint"] == "pf"
    assert record["program_step"] == "step-1"
    assert record["path_fingerprint"] == "pathfp"


def test_a_scan_with_no_request_provenance_stays_all_none(monkeypatch):
    """The manual/legacy lane: no acquisition record for this scan, so nothing invents a campaign
    identity for it (the scene layer's geometric fallback governs those arenas)."""
    ctx = _elastic_ctx()
    card = _card()
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    _stub_context(monkeypatch, ctx)

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", card,
        guide_mod.resolve(card), None, set(), "ch", "gh", M=10, seed=1)

    record = entry.measurement_record
    assert [record[k] for k in ("topology_role", "program_fingerprint", "program_step",
                                "path_fingerprint")] == [None, None, None, None]


def test_a_gated_arena_still_carries_its_request_provenance(monkeypatch):
    """A terminal gate outcome is still an attempt AGAINST a program step — the provenance is what
    lets the campaign see which step it was spent on."""
    from isar.session.state import ScanEntry
    gated = ScanEntry(scan_id="exp:1:ana7", scan_number=1, scan_type="measurement",
                      status="excluded", reasons=["excluded"])
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (gated, None))

    entry = scan_delivery._process_one(
        _driven_rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7",
        _card(acquisition_provenance=[dict(_PROVENANCE)]), None, None, set(),
        "ch", "gh", M=10, seed=1)

    assert entry.measurement_record["program_step"] == "step-1"


def test_lattice_epoch_is_the_current_one_at_write_time(monkeypatch):
    ctx = _elastic_ctx()
    monkeypatch.setattr(scan_delivery, "_gate_scan", lambda *a, **k: (None, _CalRes()))
    _stub_context(monkeypatch, ctx)
    card = _card(lattice_epochs=[{"version": 1}, {"version": 3}, {"version": 2}])

    entry = scan_delivery._process_one(
        _rec(), "ana7", [1.0, 0.0, 0.0], 50.0, "FeSe", "exp:1:ana7", card,
        guide_mod.resolve(card), None, set(), "ch", "gh", M=10, seed=1)

    assert entry.measurement_record["lattice_epoch"] == 3


# ---------------------------------------------------------------------------
# The durable jsonl log itself
# ---------------------------------------------------------------------------
def test_append_is_fsynced_and_last_record_per_ref_wins(tmp_path):
    report_dir = tmp_path / "report"
    first = {"schema": MEASUREMENT_ARENA_SCHEMA, "scan_ref": "e:1:d", "count_trust": "excluded"}
    second = {"schema": MEASUREMENT_ARENA_SCHEMA, "scan_ref": "e:1:d", "count_trust": "trusted"}
    measurement_store.append_measurement(report_dir, first)
    measurement_store.append_measurement(report_dir, second)

    path = report_dir / measurement_store.FILENAME
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    latest = {}
    for record in lines:
        latest[record["scan_ref"]] = record          # last write wins
    assert latest["e:1:d"]["count_trust"] == "trusted"


def test_append_creates_report_dir(tmp_path):
    report_dir = tmp_path / "nested" / "report"
    measurement_store.append_measurement(
        report_dir, {"schema": MEASUREMENT_ARENA_SCHEMA, "scan_ref": "e:1:d"})
    assert (report_dir / measurement_store.FILENAME).is_file()


# ---------------------------------------------------------------------------
# End to end: the driver seam (isar.session.driver's on_complete callback)
# ---------------------------------------------------------------------------
def _s30_held_batch(tmp_path):
    """The cheapest driver delivery in the suite (test_scene_composition.py's fixture): three
    scans with no companion .spec, so every one holds for calibration — never fitted, always
    gated, and it runs in about a second."""
    from pathlib import Path

    from isar.parsers import spec_s30
    from isar.session.workspace import Workspace
    from isar.synth_format import run_builder

    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(Path(spec_s30._CARD_PATH).read_text(encoding="utf-8"),
                              encoding="utf-8")
    run = run_builder.build_s30_run("SYN", n_scans=3, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(spec_s30.emit_file(run, spec_s30.card_template()))
    return ws, data


def test_measurements_jsonl_is_written_by_a_real_delivery(tmp_path):
    from isar.session import driver
    ws, data = _s30_held_batch(tmp_path)
    driver.run_batch(ws, [data], M=10, detectors="ana5")

    path = ws.report_dir / measurement_store.FILENAME
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    # Two lines per scan (W4): the pre-dispatch "attempted" stub, then the gate's terminal
    # outcome — last-line-wins per scan_ref is exercised in test_append_is_fsynced_...; here the
    # real driver seam just needs both lines to have landed.
    assert len(lines) == 6
    assert {r["count_trust"] for r in lines} == {"attempted", "held_for_calibration"}
    assert {r["schema"] for r in lines} == {MEASUREMENT_ARENA_SCHEMA}
    latest = {}
    for r in lines:
        latest[r["scan_ref"]] = r
    assert len(latest) == 3
    assert {r["count_trust"] for r in latest.values()} == {"held_for_calibration"}


def test_a_hard_worker_kill_still_leaves_the_pre_dispatch_stub(tmp_path, monkeypatch):
    """W4: the parent writes one ``count_trust="attempted"`` stub per fresh task BEFORE any
    worker starts — so even a worker that never returns (simulated here by ``_run_tasks`` raising
    before ``on_complete`` ever runs for any task) leaves proof the scan was attempted."""
    from isar.session import driver
    ws, data = _s30_held_batch(tmp_path)

    def killed(*_a, **_kw):
        raise RuntimeError("worker killed before returning")
    monkeypatch.setattr(scan_delivery, "_run_tasks", killed)

    with pytest.raises(RuntimeError, match="worker killed"):
        driver.run_batch(ws, [data], M=10, detectors="ana5")

    path = ws.report_dir / measurement_store.FILENAME
    assert path.is_file()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert {r["count_trust"] for r in lines} == {"attempted"}
    assert all(r["content_hash"] for r in lines)          # stamped, not left blank


def test_measurements_jsonl_survives_a_later_stage_failing(tmp_path, monkeypatch):
    """The hard constraint at the driver seam: the append happens in ``on_complete``, strictly
    before ``_apply_and_finish`` (which writes results.csv) even gets a chance to run — so a crash
    in that later stage must not erase the records already appended."""
    from isar.session import driver
    ws, data = _s30_held_batch(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("apply_and_finish exploded")
    monkeypatch.setattr(driver, "_apply_and_finish", boom)

    with pytest.raises(RuntimeError, match="apply_and_finish exploded"):
        driver.run_batch(ws, [data], M=10, detectors="ana5")

    path = ws.report_dir / measurement_store.FILENAME
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    # Stub + enriched line per scan (W4) — see test_measurements_jsonl_is_written_by_a_real_delivery.
    assert len(lines) == 6
    assert not (ws.report_dir / "results.csv").exists()
