"""Stage E gates: guideline fit-window masking in `build_context`, two-stage multimode fitting in
`fit_one`, the additive results.csv columns / report.json modes, and the drive-reader coercion.

These are deliberately light and synthetic (no real data files): a namespace-stub record for the
mask path, and the analyze two-peak spectrum pattern for the multimode path.
"""
import types
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.errors.bayes import BayesResult
from isar.report import run_report
from isar.schema import card as card_mod
from isar.schema.statistics import validate_shadow_model_selection
from isar.schema.steering import ParamConstraints
from isar.session import driver, scan_delivery
from isar.session import guidelines as G
from isar.session.state import ScanEntry, SessionState, canonical_hash
from isar.session.workspace import Workspace
from isar.analyze.lineshapes import convolute_voigt_dho
from isar.drive.isar_io import readers


# --------------------------------------------------------------------------- fixtures
def _card_with_fitting(fitting: dict):
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting=fitting)
    return card_mod.Card(pol, card_mod.CardState())


def _fake_record(x, counts):
    """A stub ScanRecord carrying exactly what `ScanContext.from_scan` + `build_context` read.
    counting_mode='time' so only the guideline region mask is exercised (no monitor cut)."""
    x = np.asarray(x, dtype=float)
    ax = types.SimpleNamespace(role="scanned", values=x)
    sp = types.SimpleNamespace(intensity=np.asarray(counts, dtype=float),
                               mask=np.ones_like(x, dtype=bool))
    return types.SimpleNamespace(
        scan_axes=[ax], spectra={"ana7": sp},
        temperature=types.SimpleNamespace(setpoint=np.full_like(x, 100.0)),
        energy_calibration=types.SimpleNamespace(status="needs_zero"),
        normalization=types.SimpleNamespace(counting_mode="time", preset=0.0,
                                            monitor=np.ones_like(x)))


_CALRES = types.SimpleNamespace(center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0)


# --------------------------------------------------------------------------- build_context masking
def test_build_context_applies_fit_range_and_excludes_masks_only():
    x = np.linspace(0.0, 20.0, 41)                        # 0,0.5,...,20
    counts = np.full_like(x, 100.0)
    rec = _fake_record(x, counts)
    card = _card_with_fitting({
        "expectations": [{"scans": [7], "fit_range": [4.0, 16.0],
                          "exclude_regions": [[9.0, 11.0]]}]})
    g = G.resolve(card)

    ctx, n_used, reasons = scan_delivery.build_context(
        rec, "ana7", card, _CALRES, None, g, scan_number=7, q=[3.0, 0.2, 0.0], T=100.0)

    assert ctx is not None
    # kept = [4,16] minus (9,11): 25 points in-range, minus 3 excluded (9.5,10,10.5) -> 22
    kept_expected = int(((x >= 4.0) & (x <= 16.0) & ~((x >= 9.0) & (x <= 11.0))).sum())
    assert n_used == kept_expected
    assert int(np.asarray(ctx.mask).sum()) == kept_expected
    # P5: intensity arrays are untouched (full length, unchanged values).
    assert ctx.counts.shape == x.shape
    assert np.array_equal(ctx.counts, counts)
    assert np.array_equal(rec.spectra["ana7"].intensity, counts)
    # provenance reasons name both mask kinds.
    joined = " ".join(reasons)
    assert "fit_range" in joined and "exclude_region" in joined


def test_build_context_min_fit_points_reguard_trips_when_overmasked():
    x = np.linspace(0.0, 20.0, 41)
    rec = _fake_record(x, np.full_like(x, 100.0))
    # A fit_range keeping < min_fit_points (default 6): [9.8, 10.2] -> only x=10.0.
    card = _card_with_fitting({"expectations": [{"scans": [7], "fit_range": [9.8, 10.2]}]})
    g = G.resolve(card)
    ctx, n_used, reasons = scan_delivery.build_context(
        rec, "ana7", card, _CALRES, None, g, scan_number=7, q=None, T=None)
    assert ctx is None                                    # undersampled after masking
    assert n_used < g.min_fit_points
    assert any("fit_range" in r for r in reasons)


def test_build_context_no_guideline_is_noop_mask():
    x = np.linspace(0.0, 20.0, 41)
    rec = _fake_record(x, np.full_like(x, 100.0))
    g = G.resolve(_card_with_fitting({}))
    ctx, n_used, reasons = scan_delivery.build_context(
        rec, "ana7", card=_card_with_fitting({}), calres=_CALRES, prior=None, g=g,
        scan_number=7, q=None, T=None)
    assert n_used == x.size and reasons == []


def test_exclude_regions_toml_roundtrips_to_build_context():
    """A card TOML string with exclude_regions resolves and masks the right points."""
    import tomllib
    toml = (
        '[fitting]\n'
        '[[fitting.expectations]]\n'
        'scans = [7]\n'
        'exclude_regions = [[9.0, 11.0]]\n')
    fitting = tomllib.loads(toml)["fitting"]
    card = _card_with_fitting(fitting)
    g = G.resolve(card)
    x = np.linspace(0.0, 20.0, 41)
    rec = _fake_record(x, np.full_like(x, 100.0))
    ctx, n_used, reasons = scan_delivery.build_context(
        rec, "ana7", card, _CALRES, None, g, scan_number=7, q=None, T=None)
    assert n_used == int((~((x >= 9.0) & (x <= 11.0))).sum())
    assert any("exclude_region" in r for r in reasons)


# --------------------------------------------------------------------------- guidelines-hash refit
def test_exclude_regions_edit_changes_guidelines_hash():
    """The correction channel: editing exclude_regions changes the scan's guidelines_hash (refit),
    while an untouched scan keeps its hash."""
    base = G.resolve(_card_with_fitting({}))
    edit = G.resolve(_card_with_fitting(
        {"expectations": [{"scans": [7], "exclude_regions": [[9.0, 11.0]]}]}))
    q, T = [3.0, 0.2, 0.0], 100.0
    h_base = canonical_hash(G.guidelines_signature(base, scan_number=7, q=q, T=T))
    h_edit = canonical_hash(G.guidelines_signature(edit, scan_number=7, q=q, T=T))
    assert h_base != h_edit                               # scan 7 refits
    # scan 999 (not named) is untouched.
    assert (canonical_hash(G.guidelines_signature(base, scan_number=999, q=q, T=T))
            == canonical_hash(G.guidelines_signature(edit, scan_number=999, q=q, T=T)))


# --------------------------------------------------------------------------- multimode fit_one
def _two_peak_ctx(o1=4.0, a1=500.0, o2=10.0, a2=350.0, g=0.35, bg=8.0, n=121):
    x = np.linspace(-6.0, 24.0, n)
    p1 = convolute_voigt_dho(x, o1, 0.0, 1.0, 0.0, g, a1, 0.0, 0.0, 50.0, peak="stokes+astokes")
    p2 = convolute_voigt_dho(x, o2, 0.0, 1.0, 0.0, g, a2, 0.0, 0.0, 50.0, peak="stokes+astokes")
    counts = p1 + p2 + bg
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


@pytest.mark.slow
def test_fit_one_two_peak_returns_two_modes_and_secondary_csv(tmp_path):
    ctx = _two_peak_ctx()
    card = _card_with_fitting({})
    peaks = ({"omega0": 4.0, "sigma": 0.5}, {"omega0": 10.0, "sigma": 0.5})
    fit = scan_delivery.fit_one(ctx, card, prior=None, M=20, seed=1, peaks=peaks)

    assert fit["n_modes"] == 2
    assert fit["modes"] is not None and len(fit["modes"]) == 2
    oms = sorted(m["omega0"] for m in fit["modes"])
    assert abs(oms[0] - 4.0) < 1.0 and abs(oms[1] - 10.0) < 1.0

    # Build an outcome + ledger and write results.csv: secondary columns populate.
    outcome = driver.FitOutcome(
        detector_id="ana7", verdict=fit["verdict"], disposition="resolved",
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=False, best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None, calib={},
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"])
    st = SessionState()
    st.put(ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement",
                     status="fitted", detectors={"ana7": outcome.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    path = driver._write_results_csv(ws, st)

    rows = readers.read_results_csv(ws.root)
    assert len(rows) == 1
    row = rows[0]
    assert row["n_modes"] == 2                            # int-coerced
    assert isinstance(row["secondary_omega0"], float)     # float-coerced, populated
    # secondary is the non-primary mode's omega0 (primary matches scalar omega0).
    assert row["secondary_omega0"] != row["omega0"]
    assert abs(row["secondary_omega0"] - (oms[1] if row["omega0"] == oms[0] else oms[0])) < 1e-6


@pytest.mark.slow
def test_fit_one_single_peak_leaves_secondary_blank(tmp_path):
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    fit = scan_delivery.fit_one(ctx, _card_with_fitting({}), prior=None, M=20, seed=1)
    assert fit["n_modes"] == 1
    outcome = driver.FitOutcome(
        detector_id="ana7", verdict=fit["verdict"], disposition="resolved",
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=False, best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None, calib={},
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"])
    st = SessionState()
    st.put(ScanEntry(scan_id="E:8:ana7", scan_number=8, scan_type="measurement",
                     status="fitted", detectors={"ana7": outcome.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    driver._write_results_csv(ws, st)
    row = readers.read_results_csv(ws.root)[0]
    assert row["n_modes"] == 1
    assert row["secondary_omega0"] is None and row["secondary_omega0_err"] is None


# --------------------------------------------------------------------------- fast (HESSE) tier
def test_hesse_trust_gate_unit():
    # Pure gate logic (no fitting): Γ off-bound + PD σ + counts ≥ floor → trusted; each failure mode
    # flips it; a non-finite/non-positive σ yields no interval at all.
    ctx_hi = types.SimpleNamespace(counts=np.full(50, 100.0), mask=np.ones(50, dtype=bool))  # 5000
    good = types.SimpleNamespace(omega0=5.0, omega0_err_hesse=0.02, covariance_ok=True,
                                 gamma_at_bound=False)
    iv, trusted = scan_delivery._hesse_interval_and_trust(good, ctx_hi)
    assert iv == (pytest.approx(4.98), pytest.approx(5.02)) and trusted is True

    gb = types.SimpleNamespace(omega0=5.0, omega0_err_hesse=0.02, covariance_ok=True,
                               gamma_at_bound=True)                       # Γ at bound → untrusted
    iv2, t2 = scan_delivery._hesse_interval_and_trust(gb, ctx_hi)
    assert iv2 is not None and t2 is False

    ctx_lo = types.SimpleNamespace(counts=np.full(50, 1.0), mask=np.ones(50, dtype=bool))     # 50
    assert scan_delivery._hesse_interval_and_trust(good, ctx_lo)[1] is False      # low counts → untrusted

    bad = types.SimpleNamespace(omega0=5.0, omega0_err_hesse=float("nan"), covariance_ok=False,
                                gamma_at_bound=False)
    assert scan_delivery._hesse_interval_and_trust(bad, ctx_hi) == (None, False)  # non-PD → no interval


def test_fast_tier_reports_trusted_hesse_interval_and_no_bootstrap(tmp_path):
    # A well-resolved single peak with plenty of counts: the fast tier reports a trusted HESSE
    # interval and runs NO bootstrap/profile (interval + omega0_profile stay None).
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    fit = scan_delivery.fit_one(ctx, _card_with_fitting({}), prior=None, M=20, seed=1, errors_tier="fast")

    assert fit["hesse_interval"] is not None
    lo, hi = fit["hesse_interval"]
    assert lo < fit["omega0"] < hi
    assert fit["hesse_trusted"] is True
    assert fit["interval"] is None and fit["omega0_profile"] is None     # no bootstrap/profile ran
    assert fit["interval_reliable"] is False
    shadow = validate_shadow_model_selection(fit["shadow_model_selection"])
    assert shadow["selection_status"] in {"complete", "degenerate_single_candidate"}
    assert shadow["shadow_execution_kind"] == "exact"
    assert shadow["reused_model_identities"] == shadow["attempted_model_identities"]
    assert shadow["freshly_fitted_model_identities"] == []
    assert shadow["standard_selected_label"] == fit["best_label"]
    assert {
        item["label"]: item["weight"] for item in shadow["standard_model_weights"]
    } == pytest.approx({
        item["label"]: item["weight"] for item in shadow["legacy_model_weights"]
    })

    outcome = driver.FitOutcome(
        detector_id="ana7", verdict=fit["verdict"], disposition="resolved",
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=False, best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None, calib={},
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        hesse_interval=fit["hesse_interval"], hesse_trusted=fit["hesse_trusted"],
        shadow_model_selection=fit["shadow_model_selection"])
    st = SessionState()
    st.put(ScanEntry(scan_id="E:9:ana7", scan_number=9, scan_type="measurement",
                     status="fitted", detectors={"ana7": outcome.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    driver._write_results_csv(ws, st)
    row = readers.read_results_csv(ws.root)[0]
    assert isinstance(row["hesse_lo"], float) and isinstance(row["hesse_hi"], float)
    assert row["hesse_trusted"] is True
    assert row["shadow_model_selection"]["standard_selected_label"] == fit["best_label"]


@pytest.mark.parametrize(
    "constraints",
    [
        ParamConstraints(
            soft={"phonon.Omega_d": (4.0, 0.2)},
            origins={"phonon.Omega_d": "prior_predictive"},
        ),
        ParamConstraints(
            hard={"phonon.Gamma": 0.3},
            origins={"phonon.Gamma": "prior_predictive"},
        ),
    ],
)
def test_shadow_selection_is_invariant_to_delivery_constraints(constraints):
    ctx = _single_peak_ctx_for_csv()
    card = _card_with_fitting({})
    baseline = scan_delivery.fit_one(
        ctx, card, prior=None, M=20, seed=1, errors_tier="fast"
    )
    constrained = scan_delivery.fit_one(
        replace(ctx, constraints=constraints),
        card,
        prior=None,
        M=20,
        seed=1,
        errors_tier="fast",
    )

    baseline_shadow = validate_shadow_model_selection(
        baseline["shadow_model_selection"]
    )
    constrained_shadow = validate_shadow_model_selection(
        constrained["shadow_model_selection"]
    )
    assert constrained_shadow["standard_selection_fingerprint"] == (
        baseline_shadow["standard_selection_fingerprint"]
    )
    assert {
        item["label"]: item["weight"]
        for item in constrained_shadow["standard_model_weights"]
    } == pytest.approx({
        item["label"]: item["weight"]
        for item in baseline_shadow["standard_model_weights"]
    })
    assert constrained_shadow["delivery_effective_constraint_origins"] == [
        "prior_predictive"
    ]
    assert baseline_shadow["shadow_execution_kind"] == "exact"
    assert constrained_shadow["shadow_execution_kind"] == "fresh"
    assert constrained_shadow["reused_model_identities"] == []
    assert constrained_shadow["freshly_fitted_model_identities"] == (
        constrained_shadow["attempted_model_identities"]
    )


def test_accepted_delivery_hint_records_partial_shadow_execution():
    base = _single_peak_ctx_for_csv()
    ctx = replace(
        base,
        counts=np.random.default_rng(3).poisson(
            np.clip(base.counts, 0.0, None)
        ).astype(float),
    )
    card = _card_with_fitting({})
    hinted = scan_delivery.fit_one(
        ctx,
        card,
        prior=None,
        M=20,
        seed=1,
        errors_tier="fast",
        structure_hint="stokes+bg",
    )

    assert hinted["structure_skipped"] is True
    shadow = validate_shadow_model_selection(hinted["shadow_model_selection"])
    assert shadow["shadow_execution_kind"] == "partial"
    assert shadow["reused_model_identities"]
    assert shadow["freshly_fitted_model_identities"]
    assert {
        item["model_identity"] for item in shadow["reused_model_identities"]
    } | {
        item["model_identity"]
        for item in shadow["freshly_fitted_model_identities"]
    } == {
        item["model_identity"] for item in shadow["attempted_model_identities"]
    }


def test_soft_deadline_shadow_policy_skips_extra_candidate_fits(monkeypatch):
    ctx = replace(
        _single_peak_ctx_for_csv(),
        constraints=ParamConstraints(
            soft={"phonon.Omega_d": (4.0, 0.2)},
            origins={"phonon.Omega_d": "prior_predictive"},
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("deadline-bounded work launched shadow optimizers")

    monkeypatch.setattr(scan_delivery.loop, "fit_shadow_candidates", forbidden)
    fit = scan_delivery.fit_one(
        ctx,
        _card_with_fitting({}),
        prior=None,
        M=20,
        seed=1,
        errors_tier="fast",
        shadow_enabled=False,
    )

    shadow = validate_shadow_model_selection(fit["shadow_model_selection"])
    assert shadow["selection_status"] == "unavailable"
    assert shadow["unavailable_reason_code"] == "shadow_skipped_soft_deadline"
    assert shadow["runtime_increment_seconds"] == 0.0
    assert shadow["shadow_execution_kind"] == "skipped"


def test_shadow_comparator_failure_cannot_change_delivery(monkeypatch):
    ctx = _single_peak_ctx_for_csv()
    card = _card_with_fitting({})
    baseline = scan_delivery.fit_one(
        ctx,
        card,
        prior=None,
        M=20,
        seed=1,
        errors_tier="fast",
        shadow_enabled=False,
    )

    def injected_failure(*_args, **_kwargs):
        raise RuntimeError("injected shadow-only failure")

    monkeypatch.setattr(
        scan_delivery.shadow_selection, "compare_shadow_selection", injected_failure
    )
    observed = scan_delivery.fit_one(
        ctx, card, prior=None, M=20, seed=1, errors_tier="fast"
    )

    for key in (
        "best_label",
        "candidates",
        "hesse_interval",
        "verdict",
        "confidence",
        "omega0",
        "gamma",
    ):
        assert observed[key] == baseline[key]
    shadow = validate_shadow_model_selection(observed["shadow_model_selection"])
    assert shadow["selection_status"] == "unavailable"
    assert shadow["unavailable_reason_code"] == "shadow_comparison_failed"
    assert shadow["shadow_execution_kind"] == "unavailable"


def test_shadow_unavailable_projection_has_a_nonraising_minimal_fallback(monkeypatch):
    def injected_failure(*_args, **_kwargs):
        raise RuntimeError("injected unavailable-record validator failure")

    monkeypatch.setattr(
        scan_delivery.shadow_selection, "unavailable_shadow_selection", injected_failure
    )
    record = scan_delivery._safe_shadow_unavailable(
        "shadow_context_construction_failed",
        "context_construction",
        point={"runtime_increment_seconds": 0.01},
        legacy_fingerprint=None,
        legacy_selected_label="stokes+bg",
        legacy_weights=[],
    )

    assert record["selection_status"] == "unavailable"
    assert record["selection_changed"] is None
    assert record["standard_model_weights"] == []
    assert record["shadow_execution_kind"] == "unavailable"
    assert record["reused_model_identities"] == []
    assert record["freshly_fitted_model_identities"] == []
    assert (
        record["unavailable_reason_code"]
        == "shadow_unavailable_record_construction_failed"
    )


# --------------------------------------------------------------------------- results.csv bayes columns
def _canned_bayes_for_csv():
    return BayesResult(omega0_interval=(4.9, 5.0, 5.1), gamma_interval=(0.30, 0.40, 0.50),
                       omega0_samples=np.array([4.9, 5.0, 5.1]), gamma_samples=np.array([0.4]),
                       log_evidence=-85.0, log_evidence_err=0.5, ess=500.0, bimodal=False,
                       converged=True, n_live=250, seed=777, prior_used=False, fail_reason=None)


def _single_peak_ctx_for_csv():
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _outcome_from_fit(fit, scan_number, det="ana7"):
    return driver.FitOutcome(
        detector_id=det, verdict=fit["verdict"], disposition="resolved",
        confidence=fit["confidence"], omega0=fit["omega0"], omega0_err=fit["omega0_err"],
        interval=fit["interval"], modality=fit["modality"], gamma=fit["gamma"],
        gamma_provisional=False, best_label=fit["best_label"], cost_per_dof=fit["cost_per_dof"],
        q=[3.0, 0.2, 0.0], T=50.0, sample="S", prior_used=None, calib={},
        reasons=fit["reasons"], n_modes=fit["n_modes"], modes=fit["modes"],
        hesse_interval=fit.get("hesse_interval"), hesse_trusted=fit.get("hesse_trusted", False),
        bayes_interval=fit.get("bayes_interval"), bayes_ok=fit.get("bayes_ok"),
        bayes_ess=fit.get("bayes_ess"), bayes_fail_reason=fit.get("bayes_fail_reason"),
        log_evidence=fit.get("log_evidence"), log_evidence_err=fit.get("log_evidence_err"),
        bayes_freq_ratio=fit.get("bayes_freq_ratio"), hesse_freq_ratio=fit.get("hesse_freq_ratio"),
        bayes_prior_driven=fit.get("bayes_prior_driven", False),
        bayes_fork_suspected=fit.get("bayes_fork_suspected", False),
        bayes_unconstrained=fit.get("bayes_unconstrained", False))


@pytest.mark.slow
def test_bayes_tier_populates_csv_bayes_columns(tmp_path, monkeypatch):
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: _canned_bayes_for_csv())
    fit = scan_delivery.fit_one(_single_peak_ctx_for_csv(), _card_with_fitting({}), prior=None, M=20,
                         seed=1, errors_tier="bayes",
                         bayes_opts={"n_live": 250, "dlogz": 0.5, "max_iter": 30_000})
    outcome = _outcome_from_fit(fit, 10)
    st = SessionState()
    st.put(ScanEntry(scan_id="E:10:ana7", scan_number=10, scan_type="measurement",
                     status="fitted", detectors={"ana7": outcome.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    driver._write_results_csv(ws, st)
    row = readers.read_results_csv(ws.root)[0]
    assert row["bayes_lo"] == pytest.approx(4.9)
    assert row["bayes_med"] == pytest.approx(5.0)
    assert row["bayes_hi"] == pytest.approx(5.1)
    assert str(row["bayes_ok"]).lower() == "true"
    assert row["bayes_ess"] == pytest.approx(500.0)
    assert row["log_evidence"] == pytest.approx(-85.0)
    assert row["log_evidence_err"] == pytest.approx(0.5)


@pytest.mark.slow
def test_full_tier_leaves_csv_bayes_columns_empty(tmp_path):
    fit = scan_delivery.fit_one(_single_peak_ctx_for_csv(), _card_with_fitting({}), prior=None, M=20,
                         seed=1, errors_tier="full")
    outcome = _outcome_from_fit(fit, 11)
    st = SessionState()
    st.put(ScanEntry(scan_id="E:11:ana7", scan_number=11, scan_type="measurement",
                     status="fitted", detectors={"ana7": outcome.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    driver._write_results_csv(ws, st)
    row = readers.read_results_csv(ws.root)[0]
    for key in ("bayes_lo", "bayes_med", "bayes_hi", "bayes_ok", "bayes_ess",
               "bayes_fail_reason", "bayes_freq_ratio", "hesse_freq_ratio",
               "log_evidence", "log_evidence_err"):
        assert key in row
        assert row[key] is None, key


# --------------------------------------------------------------------------- report.json modes
def _report_card():
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[], energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan", temperature_rule={}, collation={})
    return card_mod.Card(pol, card_mod.CardState())


def test_report_json_carries_modes_for_multimode(tmp_path):
    st = SessionState()
    st.put(ScanEntry(
        scan_id="E:7:ana7", scan_number=7, scan_type="measurement", status="fitted",
        detectors={"ana7": {
            "detector_id": "ana7", "verdict": "resolved", "disposition": "resolved",
            "confidence": 0.8, "omega0": 4.0, "omega0_err": 0.05,
            "interval": [3.9, 4.0, 4.1], "modality": "fold", "gamma": 0.4,
            "gamma_provisional": False, "best_label": "stokes2+bg", "cost_per_dof": 1.1,
            "q": [3.0, 0.2, 0.0], "T": 50.0, "sample": "S", "prior_used": None, "calib": {},
            "reasons": [], "n_modes": 2,
            "modes": [{"omega0": 4.0, "omega0_err": 0.05, "gamma": 0.4, "amplitude": 500.0},
                      {"omega0": 10.0, "omega0_err": 0.07, "gamma": 0.35, "amplitude": 350.0}]}}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    run_report.write_run_report(st, _report_card(), ws, formats=("markdown",))
    data = readers.read_report_json(ws.root)
    scan = data["scans"][0]
    assert scan["n_modes"] == 2
    assert [m["omega0"] for m in scan["modes"]] == [4.0, 10.0]
    assert scan["modes"][0]["mode"] == 0 and scan["modes"][1]["mode"] == 1


def test_report_json_single_mode_has_no_modes_key(tmp_path):
    st = SessionState()
    st.put(ScanEntry(
        scan_id="E:8:ana7", scan_number=8, scan_type="measurement", status="fitted",
        detectors={"ana7": {
            "detector_id": "ana7", "verdict": "resolved", "disposition": "resolved",
            "confidence": 0.8, "omega0": 5.0, "omega0_err": 0.05, "interval": None,
            "modality": "fold", "gamma": 0.4, "gamma_provisional": False,
            "best_label": "stokes+bg", "cost_per_dof": 1.1, "q": [3.0, 0.2, 0.0], "T": 50.0,
            "sample": "S", "prior_used": None, "calib": {}, "reasons": [], "n_modes": 1}}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    run_report.write_run_report(st, _report_card(), ws, formats=("markdown",))
    scan = readers.read_report_json(ws.root)["scans"][0]
    assert "modes" not in scan
