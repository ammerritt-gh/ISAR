import json
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.schema import card as card_mod
from isar.session import driver, evidence_update, master_delivery, scan_delivery
from isar.session.promotions import PromotionKey, PromotionStore
from isar.session.state import ScanEntry, SessionState
from isar.analyze.lineshapes import convolute_voigt_dho


def _result(**overrides):
    values = dict(
        analysis_usable=True, covariance_ok=True, parameter_covariance=[[1.0]],
        params={"phonon.omega0": 5.0, "phonon.Gamma": 1.0},
        param_errors={"phonon.omega0": 0.1, "phonon.Gamma": 0.1})
    values.update(overrides)
    return SimpleNamespace(**values)


def _prelim(best="best", runner_weight=0.05):
    return SimpleNamespace(
        best=best,
        scores=[SimpleNamespace(label="best", weight=1.0 - runner_weight),
                SimpleNamespace(label="runner", weight=runner_weight)])


def _ctx():
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(
        x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
        peak="stokes+astokes") + 8.0
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _card():
    policy = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan", temperature_rule={},
        collation={}, fitting={})
    return card_mod.Card(policy, card_mod.CardState())


def test_auto_escalation_reasons_are_stable_and_ordered(monkeypatch):
    monkeypatch.setattr(scan_delivery, "_hesse_interval_and_trust", lambda best, ctx: (None, False))
    fit = _result(
        params={"phonon.omega0": 0.01, "phonon.Gamma": 0.002},
        param_errors={"phonon.omega0": 0.1, "phonon.Gamma": 0.1})
    assert scan_delivery._auto_escalation_reasons(
        _prelim(runner_weight=0.10), {"best": fit}, _ctx()) == [
            "untrusted_hesse",
            "omega0_within_2sigma_of_bound",
            "gamma_within_2sigma_of_bound",
            "runner_up_model_weight_ge_0.10",
        ]


def test_auto_escalates_missing_best_and_covariance(monkeypatch):
    monkeypatch.setattr(scan_delivery, "_hesse_interval_and_trust", lambda best, ctx: (None, False))
    assert scan_delivery._auto_escalation_reasons(_prelim(best=None), {}, _ctx()) == ["no_usable_best"]
    reasons = scan_delivery._auto_escalation_reasons(
        _prelim(runner_weight=0.0),
        {"best": _result(covariance_ok=False, parameter_covariance=None)}, _ctx())
    assert reasons[:2] == ["no_usable_covariance", "untrusted_hesse"]


# Slow-marked (owner rule 2026-08-08): real fits with evidence escalations — 108 s measured.
@pytest.mark.slow
def test_auto_attaches_to_same_point_estimates_without_reenumeration(monkeypatch):
    enumeration_calls = []
    attach_calls = []
    original = scan_delivery.fast_pass.run_fast_pass

    def enumerate_once(*args, **kwargs):
        enumeration_calls.append(original(*args, **kwargs))
        return enumeration_calls[-1]

    def attach(items, **kwargs):
        attach_calls.append([id(item[0]) for item in items])

    monkeypatch.setattr(scan_delivery.fast_pass, "run_fast_pass", enumerate_once)
    monkeypatch.setattr(scan_delivery, "_auto_escalation_reasons", lambda *args: ["untrusted_hesse"])
    monkeypatch.setattr(scan_delivery.errors, "attach_batch", attach)
    fit = scan_delivery.fit_one(_ctx(), _card(), prior=None, M=20, seed=1, errors_tier="auto",
                         fit_role="master")

    assert len(enumeration_calls) == 1
    # the promotion attaches to the fast pass's own selected fit, not to a refit
    assert attach_calls == [[id(enumeration_calls[0].selected.fit)]]
    assert fit["errors_tier_requested"] == "auto"
    assert fit["errors_tier_effective"] == "full"
    assert fit["fit_role"] == "master"
    assert fit["promotion_state"] == "completed"
    assert fit["bootstrap_M_requested"] == 20


def test_promotion_store_resume_and_superseded_guard(tmp_path):
    key = PromotionKey("master:q", "rev-a", "spectrum", "point", "full", 200, 9)
    store = PromotionStore(tmp_path)
    pending = store.begin(key, escalation_reasons=["untrusted_hesse"])
    assert pending["status"] == "pending"
    assert store.pending() == [pending]

    store.complete(key, {"scan_id": "master:q", "status": "fitted"})
    superseded = store.resolve_publication(key, current_revision="rev-b")
    assert superseded["status"] == "superseded"
    assert superseded["result"]["scan_id"] == "master:q"
    assert json.loads(store.path_for(key).read_text(encoding="utf-8")) == superseded


def test_pending_promotion_resumes_from_a_new_store_instance(tmp_path):
    key = PromotionKey("master:q", "rev-a", "spectrum", "point", "full", 200, 9)
    PromotionStore(tmp_path).begin(key, escalation_reasons=["untrusted_hesse"])

    resumed = PromotionStore(tmp_path)
    assert [record["promotion_id"] for record in resumed.pending()] == [key.digest]
    resumed.complete(key, {"scan_id": "master:q", "content_hash": "rev-a"})
    published = resumed.resolve_publication(key, current_revision="rev-a")
    assert published["status"] == "published"
    assert resumed.pending() == []


def test_reconcile_keeps_current_pending_and_supersedes_stale(tmp_path):
    current = PromotionKey("master:q", "rev-b", "spectrum-b", "point-b", "full", 200, 9)
    stale = PromotionKey("master:q", "rev-a", "spectrum-a", "point-a", "full", 200, 9)
    store = PromotionStore(tmp_path)
    store.begin(stale, escalation_reasons=["untrusted_hesse"])
    store.begin(current, escalation_reasons=["untrusted_hesse"])

    session = SessionState(scans={
        "master:q": ScanEntry("master:q", 1, "measurement", "fitted",
                              content_hash="rev-b")})
    unresolved = master_delivery.unresolved_current_promotions(tmp_path, session)
    assert [record["promotion_id"] for record in unresolved] == [current.digest]
    assert store.load(stale)["status"] == "superseded"
    assert store.load(current)["status"] == "pending"


def test_published_retry_supersedes_other_pending_attempt_for_same_revision(tmp_path):
    abandoned = PromotionKey("master:q", "rev-a", "spectrum", "point-old", "full", 200, 8)
    retry = PromotionKey("master:q", "rev-a", "spectrum", "point-new", "full", 200, 9)
    store = PromotionStore(tmp_path)
    store.begin(abandoned, escalation_reasons=["untrusted_hesse"])
    store.begin(retry, escalation_reasons=["untrusted_hesse"])
    store.complete(retry, {"scan_id": "master:q", "content_hash": "rev-a"})
    assert store.resolve_publication(retry, current_revision="rev-a")["status"] == "published"

    changed = store.supersede_other_attempts(retry)
    assert [record["promotion_id"] for record in changed] == [abandoned.digest]
    assert store.load(abandoned)["superseded_by"] == retry.digest


def test_current_promotion_reconciliation_fails_closed_on_unreadable_record(tmp_path, caplog):
    store = PromotionStore(tmp_path)
    store.root.mkdir(parents=True)
    (store.root / "broken.json").write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreadable promotion record"):
        store.reconcile({"master:q": "rev-a"})
    assert "could not read promotion record" in caplog.text


def _settled(**outcome):
    """An outcome whose peak count the count ladder settled, unambiguously."""
    return {**outcome, "fit_selection_semantics": {"count_ladder": {"state": "settled",
                                                                    "ambiguity": None}}}


def test_prior_admission_requires_full_or_trusted_hesse():
    def entry(tier):
        return ScanEntry("master:q", 1, "measurement", "fitted",
                         provenance={"errors_tier_effective": tier})

    assert evidence_update._master_prior_admissible(entry("full"), _settled(hesse_trusted=False))
    assert evidence_update._master_prior_admissible(entry("bayes"), _settled(hesse_trusted=False))
    assert evidence_update._master_prior_admissible(entry("fast"), _settled(hesse_trusted=True))
    assert not evidence_update._master_prior_admissible(entry("fast"), _settled(hesse_trusted=False))
    assert not evidence_update._master_prior_admissible(entry("cheap"), _settled(hesse_trusted=True))
    assert evidence_update._master_prior_admissible(
        ScanEntry("legacy", 1, "measurement", "fitted"), _settled(hesse_trusted=False))


@pytest.mark.parametrize("count", [
    {"state": "provisional", "ambiguity": None},
    {"state": "search limited", "ambiguity": None},
    {"state": "unresolved", "ambiguity": None},
    {"state": "settled", "ambiguity": {"count": 2, "probability": 0.6, "runner_up_count": 3,
                                       "runner_up_probability": 0.4}},
    None,
])
def test_only_a_settled_unambiguous_count_enters_the_prior(count):
    """Hard rule 5's fold gate, for the peak count: a provisional result (rung 2 still to run), a
    search-limited, unresolved or ambiguous one, and one with no count outcome at all, never seed
    the dispersion prior, however good its errors."""
    entry = ScanEntry("master:q", 1, "measurement", "fitted",
                      provenance={"errors_tier_effective": "full"})
    outcome = {"hesse_trusted": True, "fit_selection_semantics": {"count_ladder": count}}
    assert not evidence_update._master_prior_admissible(entry, outcome)
    assert evidence_update._master_prior_admissible(entry, _settled(hesse_trusted=True))
