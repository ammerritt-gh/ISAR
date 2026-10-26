"""The fast pass's outputs: results.csv keeps one summary row per spectrum with its count state,
report/modes.csv holds every fitted mode ordered by energy, and both files are replaced whole.

Synthetic throughout (hard rule 10): hand-built outcomes, or spectra generated here.
"""
import math

import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import KB, convolute_voigt_dho
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


def _record(n, *, state="resolved", probs=None, confident=None):
    """A C-fastpass-record with ``n`` peaks, one per mode; the writer reads only these keys."""
    status = {"omega0": "constrained", "Gamma": "constrained", "height": "constrained",
              "area": "constrained"}
    peaks = [{"index": i, "omega0": 4.0 + 5.0 * i, "delta": False, "regime": "underdamped",
              "status": status, "height": 100.0, "sigma_height": 5.0, "area_stokes": 50.0,
              "sigma_area": 3.0, "area_status": "usable", "corr_omega0_Gamma": 0.1,
              "deletion_impact": 500.0, "manual_fit": False,
              "confident": True if confident is None else confident[i]} for i in range(n)]
    return {"schema": "isar.fast-pass/1", "state": state, "preferred_count": n,
            "count_probabilities": probs or {str(n - 1): 0.3, str(n): 0.7},
            "probabilities_conditional": False, "close_call_alternatives": [],
            "numerically_marginal": False, "search_limit_reason": "",
            "deep_pass_candidate": state != "resolved", "models": [], "peaks": peaks, "flags": []}


def _ledger(modes, record=None):
    oc = outcome_mod.FitOutcome(
        detector_id="ana7", verdict="unconstrained", disposition="abstain", confidence=0.0,
        omega0=modes[0]["omega0"], omega0_err=modes[0]["omega0_err"], interval=None,
        modality="unpooled", gamma=0.3, gamma_provisional=False,
        best_label="elastic+stokes3+bg", cost_per_dof=1.1, q=[3.0, 0.2, 0.0], T=50.0,
        sample="S", prior_used=None, calib={}, n_modes=len(modes), modes=modes,
        fast_pass=_record(len(modes)) if record is None else record)
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
    assert row["n_modes"] == 3 and row["count_state"] == "resolved"
    assert row["count_probability"] == 0.7 and row["count_probabilities"] == {"2": 0.3, "3": 0.7}
    assert row["count_state_reason"] == "counts 2 (0.30), 3 (0.70)"
    assert row["n_confident"] == 3 and row["n_tentative"] == 0 and row["flags"] is None
    assert row["probabilities_conditional"] is False and row["deep_pass_candidate"] is False
    assert "deciding_rung" not in row and "runner_up_count" not in row


def test_a_zero_mode_winner_reads_back_as_zero_modes(tmp_path):
    ws = _workspace(tmp_path)
    st = _ledger([_mode(4.0, 0.1, 0.3, 50.0)], record=_record(0, probs={"0": 1.0}))
    st.scans["E:7:ana7"].detectors["ana7"].update(n_modes=0, modes=None, omega0=None)
    driver._write_results_csv(ws, st)
    row, = readers.read_results_csv(ws.root)
    assert row["n_modes"] == 0
    assert readers.read_modes_csv(ws.root) == []


def test_modes_csv_has_one_row_per_mode_ordered_by_energy_and_missing_stays_empty(tmp_path):
    ws = _workspace(tmp_path)
    modes = [_mode(9.0, 0.2, 0.4, 30.0), _mode(4.0, float("nan"), 0.3, 50.0),
             _mode(12.0, 0.3, 0.5, 10.0)]
    st = _ledger(modes, record=_record(3, confident=[True, False, True]))
    st.scans["E:7:ana7"].provenance = {"analyzer_facts": {"Q_nm": {"value": 3.1, "source": "trailer"}}}
    driver._write_results_csv(ws, st)
    rows = readers.read_modes_csv(ws.root)
    assert [r["mode_index"] for r in rows] == [0, 1, 2]
    assert [r["omega0"] for r in rows] == [4.0, 9.0, 12.0]
    assert rows[0]["omega0_err"] is None and rows[0]["uncertainty_kind"] is None
    assert rows[1]["omega0_err"] == 0.2 and rows[1]["uncertainty_kind"] == "hesse"
    assert rows[1]["peak_position"] == pytest.approx(math.sqrt(81.0 - 0.16))
    assert all(r["scan_id"] == "E:7:ana7" and r["detector"] == "ana7" for r in rows)
    assert all(r["count_state"] == "resolved" and r["best_model"] == "elastic+stokes3+bg"
               for r in rows)
    # each mode joins the fast-pass peak of its index: the middle peak is the tentative one
    assert [r["confident"] for r in rows] == [True, False, True]
    assert all(r["Q_nm"] == pytest.approx(3.1) and r["area_status"] == "usable" for r in rows)
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


# --------------------------------------------------------------------------- the card's ceiling
def _ctx(lam, x, seed):
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _weak_elastic_ctx():
    """One peak and a weak elastic line."""
    x = np.linspace(-8.0, 16.0, 81)
    a = 500.0 * np.pi / 0.4 * -np.expm1(-5.0 / (KB * 50.0))   # pre-Eq.-2 Lorentzian 500, as intrinsic A
    return _ctx(convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, a, 20.0, 8.0, 50.0,
                                    peak="all"), x, 1)


def test_the_cards_ceiling_bounds_the_fast_pass_not_the_environment_default(monkeypatch):
    """The card's ``[fitting] max_modes`` is the fast pass's ceiling: a card above the environment
    default still reaches its counts, and every candidate row carries its own hypothesis."""
    monkeypatch.setenv("ISAR_MODE_CEILING", "1")
    card = _card()
    card.policy.fitting["max_modes"] = 2
    fit = scan_delivery.fit_one(_weak_elastic_ctx(), card, prior=None, M=10, seed=1,
                                errors_tier="cheap")
    assert fit["fast_pass"]["settings"]["mode_ceiling"] == 2
    assert max(int(r["n_modes"]) for r in fit["candidates"]) == 2
    assert fit["best_label"] in {r["label"] for r in fit["candidates"]}


# --------------------------------------------------------------------------- scans with no fit
def _unfitted_row(root, status, reasons):
    ws = _workspace(root)
    st = SessionState()
    st.put(ScanEntry(scan_id="E:8:ana7", scan_number=8, scan_type="measurement", status=status,
                     reasons=reasons))
    driver._write_results_csv(ws, st)
    row, = readers.read_results_csv(ws.root)
    return row


def test_a_scan_held_for_calibration_reads_held_with_its_reason(tmp_path):
    row = _unfitted_row(tmp_path, "held_for_calibration", ["ana7: no energy zero"])
    assert row["count_state"] == "held" and row["count_state_reason"] == "ana7: no energy zero"


def test_a_scan_with_no_fit_reads_a_stated_state_not_a_blank(tmp_path):
    """An excluded scan is not fitted; an ingested scan is still waiting for its fit."""
    excluded = _unfitted_row(tmp_path / "x", "excluded", ["excluded (card exclusions_seed / state)"])
    assert excluded["count_state"] == "not fitted"
    assert excluded["count_state_reason"] == "excluded (card exclusions_seed / state)"
    ingested = _unfitted_row(tmp_path / "y", "ingested", [])
    assert ingested["count_state"] == "fitting" and ingested["count_state_reason"] == "ingested"
