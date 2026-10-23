"""The count ladder's outputs (design 2026-10-22, §3.1, §3.4): results.csv keeps one summary row per
spectrum with its count state, report/modes.csv holds every fitted mode ordered by energy, both
files are replaced whole, and rung 2 reaches the delivery: it runs when a trigger fires, a slice
that ends early leaves the spectrum provisional ("rung 2 pending"), and a later call resumes it.

Synthetic throughout (hard rule 10): hand-built outcomes, or spectra generated here.
"""
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze import count_ladder
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import convolute_voigt_dho
from isar.drive.isar_io import readers
from isar.schema import card as card_mod
from isar.session import driver, outcome as outcome_mod, scan_delivery
from isar.session.state import ScanEntry, SessionState
from isar.session.workspace import Workspace


def _card():
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting={})
    return card_mod.Card(pol, card_mod.CardState())


def _mode(omega0, err, gamma, amp):
    return {"omega0": omega0, "omega0_err": err, "gamma": gamma, "gamma_err": 0.05,
            "amplitude": amp, "amplitude_intrinsic": amp / 2.0, "hesse_trusted": True,
            "peak_position": math.sqrt(max(omega0 ** 2 - gamma ** 2, 0.0)),
            "peak_position_err": err}


COUNT = {"state": "settled", "reasons": ["rung 2 sampled 3 contending model(s)"],
         "flags": ["structured residual"], "deciding_rung": 2, "ceiling": 6, "count": 3,
         "count_probability": 0.7, "runner_up_count": 2, "runner_up_probability": 0.3,
         "ambiguity": {"count": 3, "probability": 0.7, "runner_up_count": 2,
                       "runner_up_probability": 0.3},
         "count_probabilities": {"2": 0.3, "3": 0.7}}


def _ledger(modes, count=COUNT):
    oc = outcome_mod.FitOutcome(
        detector_id="ana7", verdict="unconstrained", disposition="abstain", confidence=0.0,
        omega0=modes[0]["omega0"], omega0_err=modes[0]["omega0_err"], interval=None,
        modality="unpooled", gamma=0.3, gamma_provisional=False,
        best_label="elastic+stokes3+bg", cost_per_dof=1.1, q=[3.0, 0.2, 0.0], T=50.0,
        sample="S", prior_used=None, calib={}, n_modes=len(modes), modes=modes,
        fit_selection_semantics={"count_ladder": count})
    st = SessionState()
    st.put(ScanEntry(scan_id="E:7:ana7", scan_number=7, scan_type="measurement",
                     status="fitted", detectors={"ana7": oc.to_dict()}))
    return st


def _workspace(tmp_path):
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir(parents=True)
    return ws


def test_the_results_row_summarises_the_count_and_drops_the_secondary_columns(tmp_path):
    ws = _workspace(tmp_path)
    modes = [_mode(4.0, 0.1, 0.3, 50.0), _mode(9.0, 0.2, 0.4, 30.0), _mode(12.0, 0.3, 0.5, 10.0)]
    driver._write_results_csv(ws, _ledger(modes))
    row, = readers.read_results_csv(ws.root)
    assert "secondary_omega0" not in row and "secondary_omega0_err" not in row
    assert row["n_modes"] == 3 and row["count_state"] == "settled"
    assert row["count_probability"] == 0.7 and row["runner_up_count"] == 2
    assert row["runner_up_probability"] == 0.3 and row["count_ambiguous"] is True
    assert row["deciding_rung"] == 2
    assert row["count_state_reason"] == "rung 2 sampled 3 contending model(s)"
    assert row["count_flags"] == "structured residual"


def test_a_zero_mode_winner_reads_back_as_zero_modes(tmp_path):
    ws = _workspace(tmp_path)
    st = _ledger([_mode(4.0, 0.1, 0.3, 50.0)], count={**COUNT, "count": 0})
    st.scans["E:7:ana7"].detectors["ana7"].update(n_modes=0, modes=None, omega0=None)
    driver._write_results_csv(ws, st)
    row, = readers.read_results_csv(ws.root)
    assert row["n_modes"] == 0
    assert readers.read_modes_csv(ws.root) == []


def test_modes_csv_has_one_row_per_mode_ordered_by_energy_and_missing_stays_empty(tmp_path):
    ws = _workspace(tmp_path)
    modes = [_mode(9.0, 0.2, 0.4, 30.0), _mode(4.0, float("nan"), 0.3, 50.0),
             _mode(12.0, 0.3, 0.5, 10.0)]
    driver._write_results_csv(ws, _ledger(modes))
    rows = readers.read_modes_csv(ws.root)
    assert [r["mode_index"] for r in rows] == [0, 1, 2]
    assert [r["omega0"] for r in rows] == [4.0, 9.0, 12.0]
    assert rows[0]["omega0_err"] is None and rows[0]["uncertainty_kind"] is None
    assert rows[1]["omega0_err"] == 0.2 and rows[1]["uncertainty_kind"] == "hesse"
    assert rows[1]["peak_position"] == pytest.approx(math.sqrt(81.0 - 0.16))
    assert all(r["scan_id"] == "E:7:ana7" and r["detector"] == "ana7" for r in rows)
    assert all(r["count_state"] == "settled" and r["best_model"] == "elastic+stokes3+bg"
               for r in rows)
    assert isinstance(rows[2]["amplitude"], float) and isinstance(rows[2]["gamma_err"], float)


def test_results_and_modes_are_each_replaced_whole(tmp_path, monkeypatch):
    """Each file is written in full to a temporary name and swapped in, so no reader ever sees a
    torn file."""
    ws = _workspace(tmp_path)
    swapped = []
    original = outcome_mod.replace_retrying

    def spy(temporary, path):
        swapped.append((path.name, temporary.read_text(encoding="utf-8").count("\n")))
        return original(temporary, path)

    monkeypatch.setattr(outcome_mod, "replace_retrying", spy)
    modes = [_mode(4.0, 0.1, 0.3, 50.0), _mode(9.0, 0.2, 0.4, 30.0)]
    driver._write_results_csv(ws, _ledger(modes))
    assert swapped == [("results.csv", 2), ("modes.csv", 3)]


# --------------------------------------------------------------------------- rung 2 in delivery
def _ctx(lam, x, seed):
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _weak_elastic_ctx():
    """One peak and a weak elastic line: both elastic variants contend at ceiling 1, so rung 2 has
    two models to sample (a few seconds at the suite's n_live)."""
    x = np.linspace(-8.0, 16.0, 81)
    return _ctx(convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 20.0, 8.0, 50.0,
                                    peak="all"), x, 1)


@pytest.fixture
def forced_trigger(monkeypatch):
    """Rung 1's triggers made to fire, at ceiling 1: what is under test is that delivery then runs
    rung 2, not when a trigger fires (tests/analyze/test_count_ladder.py owns that). Returns the
    labels sampled, in order."""
    monkeypatch.setenv("ISAR_MODE_CEILING", "1")
    real = count_ladder.triggers
    monkeypatch.setattr(count_ladder, "triggers", lambda record, ctx, contenders=None: (
        real(record, ctx, contenders) or [count_ladder.Trigger("forced", "test")]))
    sampled = []
    real_sample = count_ladder.sample_model
    monkeypatch.setattr(count_ladder, "sample_model", lambda model, ctx, **kw: (
        sampled.append(model.label) or real_sample(model, ctx, **kw)))
    return sampled


def test_a_delivery_whose_trigger_fires_runs_rung_2_through_fit_one(forced_trigger):
    """The suite guard: when a trigger fires, fit_one samples every contending model and the
    spectrum's state comes from rung 2. At the ceiling the state is search limited, and the rung-1
    result stays shown."""
    fit = scan_delivery.fit_one(_weak_elastic_ctx(), _card(), prior=None, M=10, seed=1,
                                errors_tier="cheap")
    count = fit["fit_selection_semantics"]["count_ladder"]
    assert len(count["contending"]) >= 2 and forced_trigger == count["contending"]
    assert set(count["rung2"]) == set(count["contending"])
    assert all(m["method"] == "nested" and m["log_evidence"] is not None
               for m in count["rung2"].values())
    assert count["state"] == "search limited" and count["deciding_rung"] == 1
    assert any("mode ceiling" in r for r in count["reasons"])
    assert fit["rung2_seconds"] > 0.0


@pytest.mark.slow
def test_a_settled_rung_2_is_what_delivery_shows(forced_trigger, monkeypatch):
    """Below the ceiling a finished rung 2 settles the count and replaces rung 1's selection: the
    shown model is rung 2's best, priced on its nested-sampling evidence. Slow: three nested runs
    of up to eight parameters."""
    monkeypatch.setenv("ISAR_MODE_CEILING", "2")
    x = np.linspace(-8.0, 16.0, 61)
    ctx = _ctx(convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 400.0, 0.0, 6.0, 50.0,
                                   peak="stokes+astokes"), x, 3)
    fit = scan_delivery.fit_one(ctx, _card(), prior=None, M=10, seed=1, errors_tier="cheap")
    count = fit["fit_selection_semantics"]["count_ladder"]
    assert count["state"] == "settled" and count["deciding_rung"] == 2
    assert fit["best_label"] in count["contending"] and fit["n_modes"] == 1
    rows = {r["label"]: r for r in fit["candidates"]}
    assert rows[fit["best_label"]]["evidence_method"] == "dynesty"
    assert {label for label, r in rows.items() if (r["posterior_probability"] or 0.0) > 0.0} \
        <= set(count["contending"])
    assert count["count_probability"] == pytest.approx(
        count["rung2"][fit["best_label"]]["probability"])


def test_a_slice_that_ends_leaves_rung_2_pending_and_a_later_call_resumes_it(forced_trigger,
                                                                            monkeypatch):
    """Delivery order: a deadline already past delivers the rung-1 result as provisional with
    "rung 2 pending" and samples nothing; a slice that ends after one model stores it, and the next
    call resumes from what was stored, never sampling a model twice."""
    ctx, card = _weak_elastic_ctx(), _card()
    first = scan_delivery.fit_one(ctx, card, prior=None, M=10, seed=1, errors_tier="cheap",
                                  rung2_deadline=time.time())
    count = first["fit_selection_semantics"]["count_ladder"]
    assert count["state"] == "provisional" and count["deciding_rung"] == 1
    assert count["reasons"][0].startswith(scan_delivery.RUNG2_PENDING)
    assert forced_trigger == [] and first["rung2_partial"] == {}
    contending = count["contending"]
    assert len(contending) >= 2

    # The slice ends once the first model is sampled: the clock jumps past the deadline.
    recording = count_ladder.sample_model               # the fixture's recording wrapper

    def then_out_of_time(model, ctx_, **kw):
        out = recording(model, ctx_, **kw)
        monkeypatch.setattr(scan_delivery, "time", type("Clock", (), {
            "time": staticmethod(lambda: float("inf")), "monotonic": time.monotonic,
            "perf_counter": time.perf_counter}))
        return out

    monkeypatch.setattr(count_ladder, "sample_model", then_out_of_time)
    second = scan_delivery.fit_one(ctx, card, prior=None, M=10, seed=1, errors_tier="cheap",
                                   rung2_deadline=time.time() + 3600.0)
    monkeypatch.setattr(scan_delivery, "time", time)
    monkeypatch.setattr(count_ladder, "sample_model", recording)
    partial = second["rung2_partial"]
    assert list(partial) == contending[:1]
    assert second["fit_selection_semantics"]["count_ladder"]["state"] == "provisional"
    forced_trigger.clear()
    last = scan_delivery.fit_one(ctx, card, prior=None, M=10, seed=1, errors_tier="cheap",
                                 rung2_resume=partial)
    assert forced_trigger == contending[1:]
    assert set(last["rung2_partial"]) == set(contending)
    assert last["rung2_partial"][contending[0]] == partial[contending[0]]
    assert last["fit_selection_semantics"]["count_ladder"]["state"] != "provisional"


def test_a_provisional_master_never_seeds_the_prior():
    """Hard rule 5's gate reads the count outcome a delivery writes (fit_selection_semantics)."""
    from isar.session import evidence_update

    entry = ScanEntry("master:q", 1, "measurement", "fitted",
                      provenance={"errors_tier_effective": "full"})
    provisional = {"fit_selection_semantics": {"count_ladder": {"state": "provisional",
                                                                "ambiguity": None}}}
    assert not evidence_update._master_prior_admissible(entry, provisional)


# --------------------------------------------------------------------------- the two passes
def test_run_batch_delivers_rung_1_first_then_spends_the_slice_on_pending(monkeypatch, tmp_path):
    calls = []
    pending = {"value": True}

    def fake_pass(workspace, data_paths, **kw):
        calls.append(kw)
        return driver.RunSummary(context_lines=[f"pass {len(calls)}"])

    def fake_load():
        oc = {"fit_selection_semantics": {"count_ladder": {
            "state": "provisional" if pending["value"] else "settled"}}}
        st = SessionState()
        st.put(ScanEntry("E:1:d", 1, "measurement", "fitted", detectors={"d": oc}))
        return None, st

    ws = _workspace(tmp_path)
    monkeypatch.setattr(driver, "_run_batch_pass", fake_pass)
    monkeypatch.setattr(ws, "load", fake_load)
    before = time.time()
    summary = driver.run_batch(ws, [], rung2_slice_seconds=30.0)
    first, second = calls
    assert first["retask_pending"] is False and first["rung2_deadline"] <= time.time()
    assert first["rung2_deadline"] >= before
    assert second["retask_pending"] is True and second["force"] is False
    assert 29.0 <= second["rung2_deadline"] - before <= 31.0 + (time.time() - before)
    assert summary.context_lines == ["pass 1", "pass 2"]

    calls.clear()
    driver.run_batch(ws, [], rung2_slice_seconds=0.0)        # no slice: rung 2 waits for a later run
    assert len(calls) == 1
    calls.clear()
    pending["value"] = False
    driver.run_batch(ws, [])                                # nothing pending: one pass
    assert len(calls) == 1
    calls.clear()
    pending["value"] = True
    monkeypatch.setenv(driver.RUNG2_SLICE_ENV, "")
    driver.run_batch(ws, [])                                # no slice set: rung 2 to completion
    assert calls[1]["rung2_deadline"] is None


def test_the_rung_2_slice_refuses_a_malformed_value(monkeypatch):
    monkeypatch.setenv(driver.RUNG2_SLICE_ENV, "soon")
    with pytest.raises(ValueError):
        driver.resolve_rung2_slice()
    with pytest.raises(ValueError):
        driver.resolve_rung2_slice(-1.0)
    monkeypatch.setenv(driver.RUNG2_SLICE_ENV, "12.5")
    assert driver.resolve_rung2_slice() == 12.5


# --------------------------------------------------------------------------- the per-fit count record
def _tried(label, count, lnz=None, *, converged=True, resolved_to=None):
    """A rung-1 model as the count record reads it: only the fields it uses."""
    return SimpleNamespace(label=label, count=count, log_evidence=lnz, converged=converged,
                           resolved_to=resolved_to,
                           evidence_method="laplace" if lnz is not None else None)


def _record_of(ladder, models):
    """``scan_delivery._count_summary`` over hand-built rung-1 models, with "two" selected."""
    sel = SimpleNamespace(best="two", scores=[SimpleNamespace(
        label="two", posterior_probability=1.0, weight=1.0, log_evidence=-8.0,
        evidence_method="dynesty")])
    fits = {"two": SimpleNamespace(label="two", n_modes=2, modes=None)}
    return scan_delivery._count_summary(ladder, sel, fits,
                                        SimpleNamespace(ceiling=4, models=models))


def test_every_count_rung_1_tried_is_listed_with_its_evidence_state():
    """A settled rung 2 prices only the contending counts, but every count rung 1 tried stays in the
    record, each with its evidence or the reason it has none."""
    models = [_tried("zero", 0, -20.0), _tried("one_res", 1, -11.0, resolved_to="elastic"),
              _tried("two", 2, -9.0), _tried("three", 3, converged=False)]
    ladder = count_ladder.CountLadderResult(
        state=count_ladder.SETTLED, reasons=["rung 2 sampled 1 contending model(s)"],
        contending=["two"], shown="rung2", best_label="two", count_probabilities={2: 1.0},
        rung2={"two": count_ladder.Rung2Model(
            label="two", count=2, log_evidence=-8.0, log_evidence_err=0.1, method="nested",
            reason=None, result=None, result_source="rung1", seconds=1.0)})
    rows = _record_of(ladder, models)["candidate_counts"]
    assert [r["count"] for r in rows] == [0, 1, 2, 3]
    assert [(r["count"], r["label"], r["probability"], r["log_evidence"], r["evidence_state"])
            for r in rows] == [
        (0, "zero", None, -20.0, "not sampled at rung 2"),
        (1, "one_res", None, -11.0, "resolved to elastic"),
        (2, "two", 1.0, -8.0, "rung 2 nested sampling"),
        (3, "three", None, None, "unconverged")]


def test_a_count_whose_rung_2_sampling_failed_says_so_and_is_not_dropped():
    models = [_tried("two", 2, -9.0), _tried("three", 3, -14.0)]
    ladder = count_ladder.CountLadderResult(
        state=count_ladder.UNRESOLVED, reasons=["evidence for three could not be computed (boom)"],
        contending=["two", "three"], shown="rung1", best_label="two", count_probabilities={2: 1.0},
        rung2={"two": count_ladder.Rung2Model(
                   label="two", count=2, log_evidence=-8.0, log_evidence_err=0.1, method="nested",
                   reason=None, result=None, result_source="rung1", seconds=1.0),
               "three": count_ladder.Rung2Model(
                   label="three", count=3, log_evidence=None, log_evidence_err=None,
                   method="failed", reason="ValueError: boom", result=None,
                   result_source="rung1", seconds=0.1)})
    rows = _record_of(ladder, models)["candidate_counts"]
    assert [r["count"] for r in rows] == [2, 3]
    assert [(r["count"], r["probability"], r["evidence_state"]) for r in rows] == [
        (2, 1.0, "rung 2 nested sampling"), (3, None, "sampling failed: ValueError: boom")]


def test_a_count_priced_by_rung_1_alone_keeps_its_probability_and_says_rung_1():
    """The opposite direction: no rung 2 ran, every count is priced by rung 1 and each row says so."""
    models = [_tried("one", 1, -11.0), _tried("two", 2, -9.0)]
    ladder = count_ladder.CountLadderResult(
        state=count_ladder.SETTLED, reasons=["no escalation trigger fired; rung 1 decides"],
        shown="rung1", best_label="two", count_probabilities={1: 0.25, 2: 0.75})
    rows = _record_of(ladder, models)["candidate_counts"]
    assert [(r["count"], r["probability"], r["log_evidence"], r["evidence_state"])
            for r in rows] == [(1, 0.25, -11.0, "rung 1 lnZ"), (2, 0.75, -9.0, "rung 1 lnZ")]
