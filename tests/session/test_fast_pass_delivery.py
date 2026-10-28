"""The fast pass wired into the delivery (plan S1U5): ``scan_delivery.fit_one`` runs
``fast_pass.run_fast_pass`` for every errors tier, takes its selection from the engine without
re-ranking, attaches errors to the selected model only, and persists the record.

One real fast pass on a small generated spectrum (one DHO mode beside a weak elastic line), at the
ceiling this file names. The unresolved and no-evidence cases replace the engine's answer with
``dataclasses.replace`` of that real result: what is under test is what the delivery does with such
a state, not when the engine reaches it (``tests/analyze/test_fast_pass.py`` owns that).
Synthetic throughout (hard rule 10).
"""
import copy
import dataclasses
import json
import math

import numpy as np
import pytest

from isar.analyze import fast_pass
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import KB, convolute_voigt_dho
from isar.schema import card as card_mod
from isar.session import scan_delivery
from isar.session.outcome import FitOutcome, _coerce_fit_outcome, fast_pass_of

CEILING = "2"


def _card():
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting={})
    return card_mod.Card(pol, card_mod.CardState())


def _ctx():
    x = np.linspace(-8.0, 16.0, 81)
    a = 500.0 * np.pi / 0.4 * -np.expm1(-5.0 / (KB * 50.0))
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, a, 20.0, 8.0, 50.0, peak="all")
    counts = np.random.default_rng(1).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0, center=0.0,
                       gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _fit(monkeypatch, tier="cheap", **kw):
    monkeypatch.setenv("ISAR_MODE_CEILING", CEILING)
    return scan_delivery.fit_one(_ctx(), _card(), prior=None, M=10, seed=1, errors_tier=tier, **kw)


@pytest.fixture(scope="module")
def real():
    """``(fit, res)``: one cheap-tier delivery and the engine result it was built from."""
    seen = []
    with pytest.MonkeyPatch.context() as mp:
        original = fast_pass.run_fast_pass
        mp.setattr(fast_pass, "run_fast_pass",
                   lambda *a, **k: seen.append(original(*a, **k)) or seen[-1])
        fit = _fit(mp)
    return fit, seen[0]


def _with_engine(monkeypatch, res):
    # A copy: the error tiers attach to the result's fit objects, which the module's result shares.
    monkeypatch.setattr(fast_pass, "run_fast_pass", lambda *a, **k: copy.deepcopy(res))


def test_fitter_revision_invalidates_carry_and_is_recorded(real, monkeypatch):
    from isar.session import guidelines

    g = guidelines.resolve(_card())
    revision = getattr(fast_pass, "IMPLEMENTATION_VERSION", 1)
    original = guidelines.guidelines_signature(g, scan_number=1, q=None, T=50.0)
    with monkeypatch.context() as changed:
        changed.setattr(fast_pass, "IMPLEMENTATION_VERSION", revision + 1, raising=False)
        updated = guidelines.guidelines_signature(g, scan_number=1, q=None, T=50.0)
    assert updated != original, "a fitter repair must invalidate a carried result"
    fit, _result = real
    assert fit["fast_pass"]["implementation_version"] == revision


def test_the_delivery_is_the_engines_answer(real):
    """Winner, count probabilities and candidate posteriors are the engine's own, never re-ranked:
    the candidate rows of each count sum to that count's probability, the winner is the selected
    model, and its row is priced on Laplace evidence."""
    fit, res = real
    fp = fit["fast_pass"]
    assert fp["schema"] == scan_delivery.FAST_PASS_RECORD_SCHEMA == "isar.fast-pass/1"
    assert fit["best_label"] == res.selected.label == fp["selected_label"]
    assert fit["n_modes"] == res.preferred_count
    rows = {r["label"]: r for r in fit["candidates"]}
    by_count = {}
    for r in fit["candidates"]:
        by_count[r["n_modes"]] = by_count.get(r["n_modes"], 0.0) + r["posterior_probability"]
    assert by_count.keys() >= {int(k) for k in fp["count_probabilities"]}
    for k, p in fp["count_probabilities"].items():
        assert by_count[int(k)] == pytest.approx(p, abs=1e-12)
    assert rows[fit["best_label"]]["evidence_method"] in ("laplace", "laplace_truncated")
    sem = fit["fit_selection_semantics"]
    assert sem["selection_method"] == "laplace_evidence_selection"
    assert sem["evidence_arena_fingerprint"] == fp["arena_fingerprint"]
    assert "count_ladder" not in sem
    assert fit["candidates"] and len(rows) == len(fit["candidates"])  # one row per label


def test_a_twins_row_takes_no_weight_and_names_its_terminal_model(real):
    """C-fastpass-record example 6, the candidate-row half: a degenerate twin's row has posterior 0
    and its evidence_validity names the terminal model, which is itself no twin."""
    fit, _res = real
    models = {m["model_id"]: m for m in fit["fast_pass"]["models"]}
    twins = [c for c in fit["fit_selection_semantics"]["candidate_records"]
             if (c["evidence_validity"] or {}).get("fast_pass_status") == "degenerate_twin"]
    assert twins, "the spectrum's higher counts are expected to hold degenerate twins"
    rows = {r["label"]: r for r in fit["candidates"]}
    for c in twins:
        terminal = c["evidence_validity"]["twin_of"]
        assert models[terminal]["twin_of"] is None
        assert c["posterior_probability"] == 0.0 and rows[c["label"]]["posterior_probability"] == 0.0
        assert c["candidate_prior"] == 0.0


def test_the_record_is_json_and_reclassifies_under_its_own_settings(real):
    """The persisted record round-trips through strict JSON and is what reclassify_record reads:
    the same settings give the same state back, and no wall time is in it."""
    fit, res = real
    fp = json.loads(json.dumps(fit["fast_pass"], allow_nan=False))
    again = fast_pass.reclassify_record(fp, res.settings)
    assert again["state"] == fp["state"] and again["flags"] == fp["flags"]
    assert "timing" not in fp and "total_seconds" not in json.dumps(fp)
    timing = scan_delivery._timing_provenance(fit)["fast_pass"]
    assert set(timing) == {"total_seconds", "stages", "families", "n_fits", "seed_count",
                           "stop_reason"}
    assert set(timing["stages"]) == {"seed", "ladder", "audits", "twins", "evidence", "classify"}
    assert timing["stop_reason"] == "complete" and timing["n_fits"] == res.n_fits
    assert sum(timing["stages"].values()) <= timing["total_seconds"] + 1e-9
    assert "rung1_seconds" not in scan_delivery._timing_provenance(fit)


@pytest.mark.parametrize("tier", ["fast", "full"])
def test_the_tier_only_changes_what_is_attached_to_the_winner(real, monkeypatch, tier):
    """Every tier searches with the same fast pass; full attaches the bootstrap interval to the
    selected model alone, fast the delta-method interval."""
    cheap, res = real
    _with_engine(monkeypatch, res)
    fit = _fit(monkeypatch, tier)
    assert fit["best_label"] == cheap["best_label"]
    assert fit["fast_pass"] == cheap["fast_pass"]
    attached = fit["fit_selection_semantics"]["error_attachment_set_fingerprint"]
    if tier == "full":
        assert fit["interval"] is not None and fit["modality"] in ("fold", "fork")
        assert attached != cheap["fit_selection_semantics"]["error_attachment_set_fingerprint"]
    else:
        assert fit["interval"] is None and fit["hesse_interval"] is not None
        assert attached == cheap["fit_selection_semantics"]["error_attachment_set_fingerprint"]


def test_an_unresolved_count_is_never_a_confident_answer(real, monkeypatch):
    """C-fastpass-record example 3, the delivery half: unresolved is unconstrained, and the record
    says the probabilities are conditional."""
    _fit_real, res = real
    _with_engine(monkeypatch, dataclasses.replace(res, state="unresolved",
                                                  probabilities_conditional=True,
                                                  deep_pass_candidate=True))
    fit = _fit(monkeypatch)
    assert fit["verdict"] == "unconstrained"
    assert any("peak count unresolved" in r for r in fit["reasons"])
    assert fit["fast_pass"]["state"] == "unresolved"
    assert fit["fast_pass"]["probabilities_conditional"] is True
    # the comparison is recorded incomplete: the AIC lane with the scene's incomplete fallback
    semantics = fit["fit_selection_semantics"]
    assert semantics is not None
    assert semantics["selection_method"] == "aic_fallback"
    assert semantics["selection_fallback"] == "evidence_budget_exhausted"


def test_with_nothing_priced_there_is_no_winner_and_a_valid_record(real, monkeypatch):
    """No model with evidence: no winner is declared, the selection is the recorded AIC lane (a
    vocabulary the selection schema accepts, so its record is written), and the candidates stay."""
    _fit_real, res = real
    models = tuple(dataclasses.replace(m, evidence_status="no_evidence", ln_z=None, probability=0.0,
                                       reason="Hessian not positive definite")
                   for m in res.models)
    _with_engine(monkeypatch, dataclasses.replace(
        res, models=models, selected=None, preferred_count=None, count_probabilities={},
        peaks=(), state="unresolved", probabilities_conditional=True))
    fit = _fit(monkeypatch)
    assert fit["best_label"] is None and fit["verdict"] == "unconstrained"
    sem = fit["fit_selection_semantics"]
    assert sem is not None and sem["selection_method"] == "aic_fallback"
    assert sem["selection_fallback"] == "aic_all_evidence_invalid"
    assert fit["candidates"] and fit["fast_pass"]["selected_model_id"] is None


def test_the_card_whitelist_narrows_the_search_or_warns(monkeypatch):
    """Guideline structure labels reach the engine as its whitelist; labels that name no catalogue
    structure keep every structure and say so."""
    seen = []
    original = fast_pass.run_fast_pass
    monkeypatch.setattr(fast_pass, "run_fast_pass",
                        lambda *a, **k: seen.append(k["structures"]) or original(*a, **k))
    monkeypatch.setenv("ISAR_MODE_CEILING", "1")
    fit = scan_delivery.fit_one(_ctx(), _card(), prior=None, M=10, seed=1, errors_tier="cheap",
                                structure_labels=["elastic+stokes+bg", "elastic+bg"])
    assert seen[-1] == ("elastic+stokes+bg", "elastic+bg")
    # their background-free and delta twins are derived from them; nothing without an elastic line
    assert fit["candidates"] and all(r["label"].startswith("elastic") for r in fit["candidates"])
    route = fit["fit_selection_semantics"]["candidate_generation_route"]
    assert route["route"] == "fast_pass" and route["guideline_changed_model_set"] is True
    fit = scan_delivery.fit_one(_ctx(), _card(), prior=None, M=10, seed=1, errors_tier="cheap",
                                structure_labels=["nothing+like+this"])
    assert seen[-1] is None
    assert any("match no admissible" in r for r in fit["reasons"])


def test_the_outcome_keeps_the_record(real):
    """FitOutcome.fast_pass survives the ledger's dict round trip; fast_pass_of never raises."""
    fit, _res = real
    oc = FitOutcome(detector_id="d", verdict=fit["verdict"], disposition="resolved",
                    confidence=0.5, omega0=fit["omega0"], omega0_err=None, interval=None,
                    modality="unpooled", gamma=None, gamma_provisional=False,
                    best_label=fit["best_label"], cost_per_dof=1.0, q=[0, 0, 0], T=50.0,
                    sample="S", prior_used=None, calib={}, fast_pass=fit["fast_pass"])
    back = _coerce_fit_outcome(json.loads(json.dumps(oc.to_dict())))
    assert back.fast_pass == fit["fast_pass"]
    assert fast_pass_of(oc.to_dict())["state"] == fit["fast_pass"]["state"]
    assert fast_pass_of({}) == {} and fast_pass_of({"fast_pass": None}) == {}
    assert not math.isnan(fit["fast_pass"]["ln_like_saturated"])


def test_fit_one_reports_the_fast_pass_steps_then_errors(real, monkeypatch):
    """C-progress through fit_one: seed first, errors last, and the same fit as without a callback."""
    events = []
    fit = _fit(monkeypatch, progress=events.append)
    assert events[0]["phase"] == "seed" and events[-1]["phase"] == "errors"
    assert all(e["pass"] == "fast" for e in events)
    assert fit["best_label"] == real[0]["best_label"] and fit["n_modes"] == real[0]["n_modes"]
    assert fit["fast_pass"]["count_probabilities"] == real[0]["fast_pass"]["count_probabilities"]
