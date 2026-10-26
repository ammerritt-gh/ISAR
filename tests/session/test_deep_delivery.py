"""The deep pass's entry and persistence (plan S2U1, decisions 6, 8, 10): ``isar run --deep-pass``
parses, a deep outcome is carried only under the same settings signature, its record round-trips
through ``FitOutcome`` and results.csv with the columns typed, ``deliver`` refuses rather than
compares the wrong data, and one tiny end-to-end pass (slow) yields a complete record.
"""
import dataclasses
import functools
import json
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import deep_pass, fast_pass
from isar.analyze.fast_pass import FastPassSettings
from isar.cli import main as cli
from isar.drive.isar_io import readers
from isar.session import deep_delivery, driver, scan_delivery
from isar.session import outcome as outcome_mod
from isar.session.deep_delivery import DeepRequest
from isar.session.outcome import FitOutcome, deep_pass_of
from isar.session.state import ScanEntry, SessionState
from isar.session.timing import StageTimer
from isar.session.workspace import Workspace
from isar.synth import bl43_curves, count_bench

REQ = DeepRequest(budget_s=600.0, workers=1, M=10)
DEEP_COLUMNS = ["deep_state", "deep_state_reason", "deep_best_count", "deep_p_best",
                "deep_numerically_marginal"]


def test_the_deep_pass_reads_the_schema_the_fast_pass_writes():
    assert deep_pass.FAST_PASS_SCHEMA == scan_delivery.FAST_PASS_RECORD_SCHEMA


def test_deep_pass_flag_parses_and_defaults_off():
    off = cli.build_parser().parse_args(["run", "d.dat", "--workspace", "w"])
    assert off.deep_pass is False and off.deep_budget_minutes == 60.0 and off.deep_workers is None
    on = cli.build_parser().parse_args(["run", "d.dat", "--workspace", "w", "--deep-pass",
                                        "--deep-budget-minutes", "5", "--deep-workers", "3"])
    assert (on.deep_pass, on.deep_budget_minutes, on.deep_workers) == (True, 5.0, 3)


def test_deep_pass_is_refused_beside_certification_and_analysis_allowance(tmp_path, capsys):
    from isar.cli import run_cmd
    from isar.parsers import spec_s30
    from .test_driver_smoke import _init_workspace
    ws = _init_workspace(tmp_path, spec_s30)
    for extra in (["--analysis-allowance-seconds", "5"], ["--certification-manifest", "m.json"]):
        args = cli.build_parser().parse_args(
            ["run", "d.dat", "--workspace", str(ws.root), "--deep-pass", *extra])
        assert run_cmd.run(args) == 2
        assert "--deep-pass" in capsys.readouterr().err


def _deep_record(request=REQ, **kw) -> dict:
    rec = {"schema": deep_delivery.SCHEMA, "state": "close_call",
           "reason": "close call: preferred count 2 (P 0.71) below 0.80; "
                     + deep_delivery.UNTRUSTED,
           "settings_signature": request.signature(), "trusted": False, "best_count": 2,
           "p_best": 0.71, "count_probabilities": {"1": 0.29, "2": 0.71},
           "close_call": {"statistical": True, "numerical": True}}
    return {**rec, **kw}


def test_is_current_follows_the_settings_signature_and_a_refusal():
    oc = {"deep_pass": _deep_record()}
    assert deep_delivery.is_current(oc, REQ)
    assert not deep_delivery.is_current(oc, dataclasses.replace(REQ, budget_s=60.0))
    assert not deep_delivery.is_current(oc, dataclasses.replace(REQ, M=11))
    assert deep_delivery.is_current(oc, dataclasses.replace(REQ, workers=4))   # scheduling only
    assert not deep_delivery.is_current({"deep_pass": _deep_record(state="refused")}, REQ)
    assert not deep_delivery.is_current({}, REQ) and not deep_delivery.is_current(None, REQ)


def test_an_installed_calibration_makes_a_stored_deep_pass_stale(monkeypatch):
    oc = {"deep_pass": _deep_record()}
    monkeypatch.setattr(deep_pass, "ACCEPTED", dataclasses.replace(deep_pass.ACCEPTED, n_live=800))
    assert not deep_delivery.is_current(oc, REQ)


def _csv_row(tmp_path, deep):
    oc = FitOutcome(detector_id="ana6", verdict="unconstrained", disposition="abstain",
                    confidence=0.0, omega0=None, omega0_err=None, interval=None,
                    modality="unpooled", gamma=None, gamma_provisional=False, best_label=None,
                    cost_per_dof=None, q=None, T=None, sample="S", prior_used=None, calib={},
                    deep_pass=deep)
    st = SessionState()
    st.put(ScanEntry(scan_id="E:6:ana6", scan_number=6, scan_type="measurement", status="fitted",
                     detectors={"ana6": oc.to_dict()}))
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    outcome_mod._write_results_csv(ws, st)
    row, = readers.read_results_csv(ws.root)
    return row


def test_a_deep_record_round_trips_into_five_typed_columns_appended_last(tmp_path):
    assert outcome_mod._CSV_COLUMNS[-6:] == ["deep_pass_candidate"] + DEEP_COLUMNS
    row = _csv_row(tmp_path, _deep_record())
    assert row["deep_state"] == "close_call" and "untrusted" in row["deep_state_reason"]
    assert row["deep_best_count"] == 2 and isinstance(row["deep_best_count"], int)
    assert row["deep_p_best"] == pytest.approx(0.71) and isinstance(row["deep_p_best"], float)
    assert row["deep_numerically_marginal"] is True
    assert deep_pass_of({"deep_pass": _deep_record()})["best_count"] == 2 and deep_pass_of(None) == {}
    # no deep pass: the columns are empty, never an error
    plain = _csv_row(tmp_path / "plain", None)
    assert all(plain[c] is None for c in DEEP_COLUMNS)


@functools.lru_cache(maxsize=1)
def _spectrum():
    """One ceiling-1 synthetic spectrum on the committed a06 resolution curve and its fast pass."""
    truth = count_bench.CaseTruth(
        stream=0, index=0, kind="single", modes=(count_bench.ModeTruth(5.0, 0.4, 500.0),),
        elastic_height=300.0, background=8.0, T=50.0, exposure=1.0,
        gauss_fwhm=bl43_curves.curve().fwhm, lorentz_fwhm=0.0, ceiling=1)
    ctx = count_bench.context_of(count_bench.draw_observation(truth, np.random.default_rng(3)))
    res = fast_pass.run_fast_pass(ctx, settings=FastPassSettings(mode_ceiling=1))
    return ctx, {"fast_pass": scan_delivery._fast_pass_record(res)}


def test_deliver_refuses_rather_than_compares_other_data():
    ctx, fit = _spectrum()
    other = {"fast_pass": {**fit["fast_pass"], "arena_fingerprint": "other-data"}}
    record, timing = deep_delivery.deliver(other, ctx, REQ)
    assert record["state"] == "refused" and "data changed" in record["reason"] and timing is None
    assert not deep_delivery.is_current({"deep_pass": record}, REQ)    # a refusal is retried
    record, _ = deep_delivery.deliver({}, ctx, REQ)
    assert record["state"] == "refused" and "no fast-pass record" in record["reason"]
    record, _ = deep_delivery.deliver(fit, ctx, REQ, refuse="hard-fix escalation replaced the fit")
    assert record["state"] == "refused" and "hard-fix" in record["reason"]


def test_the_chosen_fit_is_the_heaviest_model_of_the_best_count_whatever_its_stage():
    # the lighter reference variant must not be published over the exploratory one the weights favour
    ref = deep_pass.ModelEvidence("ref", 2, "reference", log_z=-10.0)
    dom = deep_pass.ModelEvidence("dom", 2, "exploratory", log_z=-2.0)
    other = deep_pass.ModelEvidence("one", 1, "reference", log_z=-1.0)
    res = SimpleNamespace(models=[ref, dom, other], best_count=2)
    assert deep_delivery._chosen_model(res).label == "dom"


def test_deliver_passes_the_fast_pass_search_limit_to_the_deep_pass(monkeypatch):
    ctx, fit = _spectrum()
    limited = {"fast_pass": {**fit["fast_pass"], "search_limit_reason": "safety_budget"}}
    seen = {}
    real = deep_pass.deep_pass

    def spy(entries, **kw):
        seen.update(kw)
        return real(entries, **{**kw, "budget_s": 0.0})     # nothing sampled: only the wiring is tested

    monkeypatch.setattr(deep_pass, "deep_pass", spy)
    record, _ = deep_delivery.deliver(limited, ctx, REQ)
    assert seen["fast_limit"] == "safety_budget"
    assert record["state"] == "search_limited" and not record["complete"]
    assert "fast pass search limited: safety_budget" in record["search_limited"]


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_deliver_end_to_end_at_tiny_settings(monkeypatch):
    """Staged coverage of every model of a ceiling-1 record at n_live 32/16: a persisted record
    with every run, the chosen model's errors, and a timing entry for each run."""
    monkeypatch.setattr(deep_pass, "ACCEPTED", dataclasses.replace(deep_pass.ACCEPTED, n_live=32))
    monkeypatch.setattr(deep_pass, "EXPLORATORY",
                        dataclasses.replace(deep_pass.EXPLORATORY, n_live=16))
    ctx, fit = _spectrum()
    # The ceiling-1 fast pass really is search limited (its best count is the ceiling), and the
    # deep pass rightly inherits that as partial; this test is about staged coverage, so lift it.
    assert fit["fast_pass"]["search_limit_reason"] == "ceiling"
    fit = {"fast_pass": {**fit["fast_pass"], "search_limit_reason": ""}}
    record, timing = deep_delivery.deliver(fit, ctx, REQ)
    record = scan_delivery._finite_json(record)
    json.dumps(record, allow_nan=False)
    assert record["schema"] == deep_delivery.SCHEMA and record["state"] != "refused", record["reason"]
    assert record["complete"] and record["comparison_scope"] == "full"
    assert record["trusted"] is False and "untrusted" in record["reason"]
    assert {m["label"] for m in record["models"]} == {m["label"] for m in fit["fast_pass"]["models"]}
    assert abs(sum(record["count_probabilities"].values()) - 1.0) < 1e-9
    assert record["chosen"]["errors"] is not None, record["chosen"]
    n_runs = sum(len(m["runs"]) for m in record["models"])
    assert n_runs == sum(len(m["runs"]) for m in timing["models"]) >= len(record["models"])
    run = record["models"][0]["runs"][0]
    assert run["n_like_calls"] > 0 and run["information_h"] is not None and run["seed"] is not None
    assert deep_delivery.is_current({"deep_pass": record}, REQ)


@pytest.mark.slow
def test_a_deep_run_is_carried_under_the_same_settings_and_redone_under_others(tmp_path, monkeypatch):
    """Through the real driver, with ``deliver`` stubbed: the record reaches the ledger, results.csv
    and the timer; an identical request carries it; a changed budget refits and delivers again."""
    from isar.parsers import ill_in8
    from .test_driver_smoke import IN8_MAIN_NUMOR, _find, _init_workspace, write_in8_pair
    calls = []

    def stub(fit, ctx, request, *, executor=None, refuse=None):
        calls.append(request.budget_s)
        return _deep_record(request), {"total_seconds": 1.0, "models": []}

    monkeypatch.setattr(deep_delivery, "deliver", stub)
    data = write_in8_pair(tmp_path / "in8")
    ws = _init_workspace(tmp_path, ill_in8)
    timer = StageTimer()
    s1 = driver.run_batch(ws, [data], M=10, deep_pass=REQ, timer=timer)
    assert calls == [600.0] and s1.n_fitted_this_run >= 1
    _card, session = ws.load()
    entry = _find(session, IN8_MAIN_NUMOR)
    assert entry.detectors["det0"]["deep_pass"]["best_count"] == 2
    row = next(r for r in readers.read_results_csv(ws.root) if r["scan"] == IN8_MAIN_NUMOR)
    assert row["deep_state"] == "close_call"
    fit = next(s for s in timer.to_dict()["stages"] if s["stage"] == "fit_scans")
    assert fit["deep_workers"] == 1 and fit["deep_budget_s"] == 600.0
    assert any(t.get("deep_pass") for t in fit["per_task"])

    s2 = driver.run_batch(ws, [data], M=10, deep_pass=REQ)
    assert calls == [600.0] and s2.n_fitted_this_run == 0                    # carried
    s3 = driver.run_batch(ws, [data], M=10, deep_pass=dataclasses.replace(REQ, budget_s=60.0))
    assert calls == [600.0, 60.0] and s3.n_fitted_this_run >= 1              # stale: refit + deliver
    s4 = driver.run_batch(ws, [data], M=10)                                  # no deep request
    assert calls == [600.0, 60.0] and s4.n_fitted_this_run == 0
