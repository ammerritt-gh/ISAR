"""The inter-spectra foundation (S1U1): every connection importable with its final field names, the settings
table, the severity table, the import roots, the Qt-free read side, and the BL43-like fixture.

Field lists are the plan's connection blocks; a later unit that renames one is a correction, not an edit.
"""
from __future__ import annotations

import dataclasses
import importlib
import math
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import isar
from inter_fixtures import build_inter_workspace, scan_keys, spectrum_keys
from isar.analyze.coupled.settings import RETIRED_SETTINGS, SETTING_DEFS, InterSettings
from isar.analyze.coupled.types import FLAG_CODES, SEVERITIES, SEVERITY_OF

REPO_ROOT = Path(__file__).resolve().parents[1]

MODULES = (
    "isar.session.inter_spectra.nodes", "isar.session.inter_spectra.layout", "isar.session.inter_spectra.graph",
    "isar.session.inter_spectra.rings", "isar.session.inter_spectra.schedule", "isar.session.inter_spectra.preview",
    "isar.session.inter_spectra.q_provider", "isar.session.inter_spectra.records",
    "isar.session.inter_spectra.authority", "isar.session.inter_spectra.runner",
    "isar.analyze.coupled.types", "isar.analyze.coupled.refit", "isar.analyze.coupled.identity",
    "isar.intake.inter_spectra", "isar.intake.inter_values", "isar.workspace_api.inter_spectra",
)

# (module, class, field names in order), from the plan's connection blocks
CONTRACT = (
    ("isar.session.inter_spectra.nodes", "SpectrumNode",
     ["key", "scan", "detector", "label", "series", "T", "eligible", "reason"]),
    ("isar.session.inter_spectra.nodes", "NodeSet", ["nodes", "series", "slots", "repeats"]),
    ("isar.session.inter_spectra.layout", "Grid", ["rows"]),
    ("isar.session.inter_spectra.q_provider", "QPoint", ["hkl", "source", "cart", "epoch"]),
    ("isar.session.inter_spectra.q_provider", "QCoordinates", ["points", "reasons", "lattices"]),
    ("isar.session.inter_spectra.graph", "Link", ["kind", "a", "b", "step", "dq", "metric"]),
    ("isar.session.inter_spectra.graph", "Term", ["kind", "members", "spacings", "t_frac", "line", "metric"]),
    ("isar.session.inter_spectra.graph", "LinkGraph", ["nodes", "metric", "links", "triplets", "unlinked", "notes"]),
    ("isar.session.inter_spectra.rings", "Ring", ["index", "members", "locked", "conditioners"]),
    ("isar.session.inter_spectra.rings", "Seam", ["ring", "link", "fronts"]),
    ("isar.session.inter_spectra.rings", "AnchoredPlan",
     ["anchors", "ignored_anchors", "ring_of", "fronts", "rings", "seams", "not_reached"]),
    ("isar.session.inter_spectra.schedule", "SimultaneousPlan",
     ["free", "held", "terms", "terms_of", "groups", "not_coupled"]),
    ("isar.session.inter_spectra.preview", "Preview",
     ["text", "can_start", "blocked", "notes", "n_spectra", "n_refit", "n_rings", "n_not_reached", "n_terms",
      "n_groups"]),
    ("isar.session.inter_spectra.records", "Cond", ["unit", "fit", "inputs"]),
    ("isar.session.inter_spectra.records", "Accept", ["decision", "reason", "detail", "intake_seq"]),
    ("isar.session.inter_spectra.records", "Flag", ["score", "flagged", "reasons"]),
    ("isar.session.inter_spectra.records", "RefitLine",
     ["batch", "fit", "unit", "at", "stage", "index", "status", "reason", "seed", "inputs", "conditioned_on",
      "flag", "why_changed", "accept", "payload"]),
    ("isar.session.inter_spectra.records", "StageLine",
     ["batch", "at", "stage", "index", "round", "progress", "seams", "not_reached"]),
    ("isar.session.inter_spectra.records", "BatchInfo",
     ["batch", "number", "at", "batch_kind", "scope", "anchors", "rings", "seams", "settings", "preview", "fixed",
      "unreached", "terms", "metric", "core", "intake_seq", "epochs", "end"]),
    ("isar.session.inter_spectra.records", "InterLog", ["batches", "refits", "stages", "damaged"]),
    ("isar.session.inter_spectra.authority", "Stale", ["code", "unit"]),
    ("isar.session.inter_spectra.authority", "Resolution",
     ["unit", "fit", "source", "chosen", "locked", "lock_fit", "stale", "conditions", "notes"]),
    ("isar.session.inter_spectra.authority", "Effective", ["unit", "outcome", "resolution", "label", "line"]),
    ("isar.session.inter_spectra.runner", "BatchSpec", ["batch_id", "kind", "direction", "anchors", "members"]),
    ("isar.session.inter_spectra.runner", "BatchItem", ["item_id", "unit_key", "pass_kind", "cores", "spec"]),
    ("isar.session.inter_spectra.runner", "InterResult",
     ["item_id", "unit_key", "pass_kind", "status", "reason", "payload", "cores", "ack"]),
    ("isar.analyze.coupled.types", "HeldMode",
     ["branch", "omega0", "gamma", "height", "guard", "guard_source", "status", "regime", "pinned"]),
    ("isar.analyze.coupled.types", "Dormant", ["branch", "last_seen_fit", "last_seen_unit"]),
    ("isar.analyze.coupled.types", "Held", ["unit", "fit", "inputs", "window", "modes", "dormant", "T"]),
    ("isar.analyze.coupled.types", "TermInput", ["term", "held"]),
    ("isar.analyze.coupled.types", "Snapshot",
     ["snapshot_id", "stage_id", "unit", "fit_id", "context", "own", "own_record", "terms", "settings",
      "batch_kind", "fast", "aliases"]),
    ("isar.analyze.coupled.types", "Assignment",
     ["unit", "snapshot_id", "count", "slots", "origins", "factors", "discrete", "triggers", "candidates",
      "coverage", "search_limited", "changed", "reasons", "selected_model", "mask_only"]),
    ("isar.analyze.coupled.types", "PricingJob", ["unit", "snapshot_id", "count", "slots", "key"]),
    ("isar.analyze.coupled.types", "Priced", ["key", "count", "slots", "ln_z", "status", "mode", "reason", "starts",
                                              "seconds", "basins", "loss"]),
    ("isar.analyze.coupled.types", "RefitResult",
     ["unit", "snapshot_id", "status", "reason", "assignment", "fit", "modes", "D", "J", "P", "U", "unary",
      "energies", "R", "R_signed", "F", "delta_fast", "flag", "why_changed", "overlay", "overlay_unavailable",
      "covariance", "errors", "normalisers", "starts", "timing", "priced", "cache_hits"]),
    ("isar.analyze.coupled.identity", "ModeObs",
     ["ref", "omega0", "gamma", "height", "sigma_guard", "sigma_provenance", "branch"]),
    ("isar.analyze.coupled.identity", "Match", ["branch", "local", "term_ids"]),
    ("isar.analyze.coupled.identity", "IdentityResult",
     ["ids", "status", "alternatives", "absent", "loops", "drift", "seams", "flags"]),
    ("isar.intake.inter_spectra", "Pick", ["fit", "seq"]),
    ("isar.intake.inter_spectra", "LockState", ["locked", "fit", "seq"]),
    ("isar.intake.inter_spectra", "InterUser", ["anchors", "picks", "locks", "settings", "max_seq", "damaged"]),
    ("isar.workspace_api.inter_spectra", "FlagReason", ["code", "severity", "detail"]),
    ("isar.workspace_api.inter_spectra", "FlagInfo",
     ["severity", "score", "cost_of_coupling", "link_mismatch", "reasons", "flagged"]),
    ("isar.workspace_api.inter_spectra", "SpectrumInter",
     ["key", "fit_id", "in_use", "source", "label", "flag", "locked", "locked_fit_id", "lock_stale", "stale",
      "stale_text"]),
    ("isar.workspace_api.inter_spectra", "BatchSummary",
     ["batch_id", "label", "kind", "direction", "status", "n_total", "n_done", "n_failed", "n_not_reached",
      "anchors"]),
    ("isar.workspace_api.inter_spectra", "InterOverview", ["revision", "by_key", "anchors", "settings", "batches"]),
    ("isar.workspace_api.inter_spectra", "FitEntry",
     ["fit_id", "label", "batch_id", "batch_label", "stage_label", "changed", "authoritative", "picked", "locked",
      "stale", "stale_text", "flag", "why_changed", "conditional_errors", "errors_available"]),
    ("isar.workspace_api.inter_spectra", "BatchRequest", ["kind", "direction", "anchors", "selection"]),
    ("isar.workspace_api.inter_spectra", "BatchPreview", ["can_start", "reason", "members", "line", "notes"]),
    ("isar.analyze.coupled.settings", "SettingDef",
     ["name", "group", "kind", "default", "calibrated", "unit", "minimum", "maximum", "choices", "help",
      "strict_minimum"]),
)

# the settings table, in display order: (name, group, default), the plan's table
SETTING_TABLE = (
    ("q_slope_meV_per_rlu", "front", 10.6), ("q_slope_meV_per_invA", "front", 8.0),
    ("t_tolerance_meV", "front", 1.0), ("flag_threshold", "front", 54.0),
    ("shape_family", "advanced", "welsch"), ("step_exponent", "advanced", 0.25),
    ("sweep_tol_meV", "advanced", 0.01), ("sweep_cap", "advanced", 20),
    ("q_curvature_scale_meV", "hidden", 1.0),
    ("reference_T_K", "hidden", 100.0), ("t_spacing_form", "hidden", "widen"), ("penalty_cap", "hidden", 12.0),
    ("birth_edge", "hidden", 2.0), ("birth_mid", "hidden", 12.0), ("cost_rel_tol", "hidden", 1e-5),
    ("collinearity_tol", "hidden", 0.01), ("composition_limit", "hidden", 4), ("confidence", "hidden", 0.80),
    ("close_call_nats", "hidden", 2.0), ("evidence_hysteresis_nats", "hidden", 0.1),
    ("linewidth_ratio", "hidden", 2.0), ("full_search", "hidden", False),
)


def _fields(module: str, name: str) -> list[str]:
    return [f.name for f in dataclasses.fields(getattr(importlib.import_module(module), name))]


def test_the_import_root_is_this_checkout():
    assert Path(isar.__file__).resolve().is_relative_to(REPO_ROOT)


@pytest.mark.parametrize("module", MODULES)
def test_every_connection_module_imports(module):
    importlib.import_module(module)


@pytest.mark.parametrize("module,name,expected", CONTRACT, ids=[f"{m.rsplit('.', 1)[1]}.{n}" for m, n, _ in CONTRACT])
def test_field_names_equal_the_plan(module, name, expected):
    got = _fields(module, name)
    assert got == expected, (f"{name} fields differ from the plan: missing {sorted(set(expected) - set(got))}, "
                             f"extra {sorted(set(got) - set(expected))}")


def test_inter_settings_has_one_field_per_table_row():
    got = [f.name for f in dataclasses.fields(InterSettings)]
    expected = ["version"] + [d.name for d in SETTING_DEFS]
    assert got == expected, (f"InterSettings fields differ from SETTING_DEFS: missing "
                             f"{sorted(set(expected) - set(got))}, extra {sorted(set(got) - set(expected))}")


def test_the_settings_table_is_the_plan_table():
    assert tuple((d.name, d.group, d.default) for d in SETTING_DEFS) == SETTING_TABLE


# plan revision 4: calibrated on synthetic tracks (expert continuation 2); every other row uncalibrated
CALIBRATED_ROWS = {"t_tolerance_meV", "step_exponent", "reference_T_K", "penalty_cap", "flag_threshold"}


@pytest.mark.parametrize("d", SETTING_DEFS, ids=lambda d: d.name)
def test_every_row_carries_the_plan_mark_and_is_described(d):
    assert d.calibrated is (d.name in CALIBRATED_ROWS)
    assert d.help.strip()
    assert d.default is not None


def test_from_values_worked_examples():
    assert InterSettings.from_values({}) == InterSettings()
    for bad, name in (({"sweep_cap": "x"}, "sweep_cap"), ({"bogus": 1}, "bogus"),
                      ({"confidence": 0.3}, "confidence"), ({"t_tolerance_meV": 0}, "t_tolerance_meV")):
        with pytest.raises(ValueError, match=name):
            InterSettings.from_values(bad)


def test_the_q_tolerance_is_a_slope_times_the_spacing_in_its_own_metric():
    s = InterSettings()
    assert s.q_tolerance("hkl", 0.19, "w") == pytest.approx(10.6 * 0.19)
    assert s.q_tolerance("lattice", 0.25, "w") == pytest.approx(2.0)
    for bad in (None, "kelvin"):
        with pytest.raises(ValueError, match="x: a q link has no metric"):
            s.q_tolerance(bad, 0.1, "x")
    assert s.widened(2.0, 0.04) == pytest.approx(math.sqrt(4 + 6 * 0.04))
    assert replace(s, penalty_cap=24.0).widened(2.0, 0.04) == pytest.approx(math.sqrt(4 + 12 * 0.04))
    # the divide arm's curvature is C/d, so its width uses C/d (correction 1 of S2)
    assert s.widened(1.0, 0.5, d=0.25) == pytest.approx(math.sqrt(1 + 0.5 * 12 / 0.25 * 0.5))
    assert s.t_base(100.0) == pytest.approx((1.0, 0.0))
    assert replace(s, t_spacing_form="divide").t_base(50.0) == pytest.approx((1.0, 0.5))
    link = SimpleNamespace(kind="T", step=100.0, metric=None, a="x", b="y")
    assert s.link_tolerance(link) == pytest.approx(1.0)
    assert s.link_tolerance(SimpleNamespace(kind="q", step=0.1, metric="hkl", a="a", b="b")) == pytest.approx(1.06)


def test_the_retired_settings_are_gone_from_the_table():
    assert RETIRED_SETTINGS == {"q_tolerance_meV", "q_change_scale_meV"}
    assert not RETIRED_SETTINGS & {d.name for d in SETTING_DEFS}


def test_severity_table_covers_every_flag_code():
    assert set(SEVERITY_OF) == set(FLAG_CODES)
    assert set(SEVERITY_OF.values()) <= set(SEVERITIES)


def _modules_after(imports: str) -> set[str]:
    """The module names a fresh interpreter, run from the checkout, has loaded after ``imports``."""
    code = f"import sys; {imports}; print('\\n'.join(sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return set(out.stdout.split())


def _loaded_after(imports: str) -> str:
    """Import in a fresh interpreter run from the checkout, and report which heavy modules were loaded."""
    code = f"import sys; {imports}; print(sorted({{'numpy', 'scipy', 'PySide6'}} & set(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_the_core_store_and_records_load_no_numerics_and_no_qt():
    """The pure modules stay light: they import no numpy, scipy or Qt. The numerical core (refit, identity) and the
    GUI-side writers (inter_values, which reuse isar.intake.values) are not pure and are not listed."""
    loaded = _loaded_after("import isar.session.inter_spectra.authority, isar.session.inter_spectra.runner, isar.session.inter_spectra.graph, "
                           "isar.session.inter_spectra.rings, isar.session.inter_spectra.schedule, "
                           "isar.session.inter_spectra.preview, isar.analyze.coupled.settings, "
                           "isar.analyze.coupled.types, isar.intake.inter_spectra")
    assert loaded == "[]", loaded


def test_the_read_facade_adds_no_scipy_qt_or_coupled_numerics():
    """Measured against the facade's own dependencies. Importing ``isar.workspace_api`` runs the facade, so a base
    that imports the package would hide every import the facade makes. numpy is in the base, so it cannot show."""
    base = _modules_after("import isar.intake.inter_spectra, isar.intake.inputs, isar.intake.scans, isar.intake.store, "
                          "isar.schema.card, isar.session.state, isar.session.workspace")
    assert not [m for m in base if m.startswith(("isar.workspace_api", "isar.session.inter_spectra"))]
    added = _modules_after("import isar.workspace_api.inter_spectra") - base
    heavy = sorted(m for m in added if m.split(".")[0] in ("scipy", "PySide6")
                   or (m.startswith("isar.analyze.coupled.") and m != "isar.analyze.coupled.settings"))
    assert heavy == [], heavy


def test_the_bl43_like_workspace_has_six_scans_of_28_spectra_each_with_a_fast_solo(tmp_path):
    ws = build_inter_workspace(tmp_path)
    keys = spectrum_keys(ws)
    assert len(scan_keys(ws)) == 2 * 3
    assert len(keys) == 2 * 3 * 28
    from isar.session.workspace import Workspace
    _, session = Workspace.open(ws).load()
    for key in keys:
        det = key.rsplit(":", 1)[1]
        assert session.scans[key].detectors[det]["fast_pass"]["state"] == "resolved", key
    assert (ws / "report" / "intake_scans.json").exists()


def test_a_stamped_fast_solo_carries_the_fit_inputs_hash_of_its_spectrum(tmp_path):
    from isar.intake import inputs, scans as intake_scans, store
    from isar.session import currentness
    from isar.session.workspace import Workspace
    ws = build_inter_workspace(tmp_path)
    card, session = Workspace.open(ws).load()
    rows = intake_scans.read_scans(ws)
    read = store.read_all(ws, store.ENTRIES)
    index = inputs.index_entries(read.entries, lines=read.lines)
    key = spectrum_keys(ws)[-1]
    scan, det = key.rsplit(":", 1)
    fresh = inputs.inputs_hash(inputs.unit_inputs(rows[scan], inputs.scan_inputs(rows[scan], index), det,
                                                  card.state.tabulated_calibration.get(scan)))
    assert (session.scans[key].provenance or {}).get("fit_inputs", {}).get("hash") == fresh
    assert "fast" in currentness.current_passes(ws, [key])[key]


def test_each_stamped_fast_solo_carries_its_own_temperature(tmp_path):
    from isar.session.workspace import Workspace
    ws = build_inter_workspace(tmp_path)
    _, session = Workspace.open(ws).load()
    keys = spectrum_keys(ws)
    for key in keys:
        det = key.rsplit(":", 1)[1]
        assert session.scans[key].detectors[det]["T"] == session.scans[key].provenance["T"], key
    assert {session.scans[k].detectors[k.rsplit(":", 1)[1]]["T"] for k in keys} == {100.0, 150.0, 200.0}
