"""Part 2B — the G7 sandbox preview fit (isar.sandbox_api).

The fit CORE (``fit_context``) is exercised in-process with a fake reduced record + a stub
``CalibResolution``, exactly as ``tests/session/test_driver_multimode.py`` exercises the engine — no
file parsing, no workspace. Held/error states are checked without the engine. The full front-end
``preview_fit`` (re-parse → reduce → calibrate) is smoke-tested on real data (gitignored), so it is
only structurally checked here.
"""
from __future__ import annotations

import types
import threading

import numpy as np
import pytest

from isar import sandbox_api as sandbox


def _card():
    from isar.schema import card as card_mod
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting={})
    return card_mod.Card(pol, card_mod.CardState())


def _record(x, counts):
    x = np.asarray(x, dtype=float)
    ax = types.SimpleNamespace(role="scanned", values=x)
    sp = types.SimpleNamespace(intensity=np.asarray(counts, dtype=float),
                               mask=np.ones_like(x, dtype=bool), q=[3.0, 0.2, 0.0])
    return types.SimpleNamespace(
        experiment_id="E", scan_number=7, scan_axes=[ax], spectra={"ana7": sp},
        temperature=types.SimpleNamespace(setpoint=np.full_like(x, 50.0)),
        energy_calibration=types.SimpleNamespace(status="needs_zero"),
        normalization=types.SimpleNamespace(counting_mode="time", preset=0.0,
                                            monitor=np.ones_like(x)))


def _calres(center=0.0):
    return types.SimpleNamespace(center=center, gauss_fwhm=1.0, lorentz_fwhm=0.0, held=False,
                                 held_reason=None)


# --------------------------------------------------------------------------- held/error
# (Qt-free is asserted in tests/test_qt_free_core.py — an in-process check here is unreliable because
# sibling GUI tests in the same session have already imported PySide6.)
def _guidelines(card):
    from isar.session import guidelines as G
    return G.resolve(card)


def test_held_when_calibration_unresolved():
    card = _card()
    r = sandbox.fit_context(_record([0, 1, 2], [1, 2, 1]), "ana7", card, _calres(center=None),
                            _guidelines(card), unit_key="E:7:ana7", scan_number=7, q=[0, 0, 0],
                            T=50.0)
    assert r.status == "held" and not r.ok


def test_bad_unit_key_is_error():
    r = sandbox.preview_fit("nonexistent", "not-a-key")
    assert r.status == "error"


def test_preview_fit_writes_nothing_on_a_non_workspace(tmp_path):
    # Hard Rule 1: the sandbox NEVER writes. Even against a real directory that isn't a workspace it
    # returns error/held and leaves the directory untouched (no card.*, no results, no files).
    before = set(p.name for p in tmp_path.iterdir())
    r = sandbox.preview_fit(str(tmp_path), "E:7:ana7")
    assert r.status in ("error", "held")
    assert set(p.name for p in tmp_path.iterdir()) == before   # nothing written


def test_vm_serializes_and_coalesces_requests(qtbot, monkeypatch):
    # A/B/C while A is active runs A then C: B is coalesced, concurrency never exceeds one, and
    # only C's (latest) result is emitted.
    from isar_gui.viewmodels.sandbox import SandboxViewModel
    from isar_gui.viewmodels import sandbox as vm_module
    release = threading.Event()
    lock = threading.Lock()
    calls = []
    active = maximum = 0

    def fake_preview(_root, key, center_override=None):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            calls.append(key)
        if key == "E:1:d":
            assert release.wait(timeout=5)
        with lock:
            active -= 1
        return sandbox.SandboxResult(unit_key=key, status="held", reason="test")

    monkeypatch.setattr(vm_module.sandbox_api, "preview_fit", fake_preview)
    vm = SandboxViewModel(workspace_root="workspace")
    busy = []
    vm.busy_changed.connect(busy.append)
    got = []
    vm.result_ready.connect(got.append)
    vm.request_fit("E:1:d")
    vm.request_fit("E:2:d")
    vm.request_fit("E:3:d")
    release.set()
    qtbot.waitUntil(lambda: bool(got) and busy[-1] is False, timeout=5000)
    assert calls == ["E:1:d", "E:3:d"]
    assert maximum == 1
    assert [result.unit_key for result in got] == ["E:3:d"]


def test_vm_workspace_rebind_invalidates_old_result_and_runs_new_request(qtbot, monkeypatch):
    from isar_gui.viewmodels.sandbox import SandboxViewModel
    from isar_gui.viewmodels import sandbox as vm_module
    release_old = threading.Event()
    calls = []

    def fake_preview(root, key, center_override=None):
        calls.append((root, key))
        if root == "old-workspace":
            assert release_old.wait(timeout=5)
        return sandbox.SandboxResult(unit_key=key, status="held", reason=root)

    monkeypatch.setattr(vm_module.sandbox_api, "preview_fit", fake_preview)
    vm = SandboxViewModel(workspace_root="old-workspace")
    got = []
    busy = []
    vm.result_ready.connect(got.append)
    vm.busy_changed.connect(busy.append)

    vm.request_fit("old:1:d")
    vm.request_fit("old:2:d")             # old pending request must be cleared by the rebind
    vm.set_workspace_root("new-workspace")
    vm.request_fit("new:3:d")             # waits behind active old:1, then runs
    release_old.set()

    qtbot.waitUntil(lambda: bool(got) and busy[-1] is False, timeout=5000)
    assert calls == [("old-workspace", "old:1:d"), ("new-workspace", "new:3:d")]
    assert [(result.unit_key, result.reason) for result in got] == [
        ("new:3:d", "new-workspace")]


def test_fit_inputs_uses_production_session_prior_fallback(monkeypatch):
    from isar.session import session_prior

    card = _card()
    card.policy.dispersion = {"zone_period": 2.0}
    card.policy.collation = {"t_round": 4.0}
    expected = object()
    called = {}

    def fake_prior_for(state, **kwargs):
        called["state"] = state
        called.update(kwargs)
        return expected

    monkeypatch.setattr(session_prior, "prior_for", fake_prior_for)
    guidelines = types.SimpleNamespace(expectation_for=lambda *args, **kwargs: None)
    prior, labels, peaks = sandbox._fit_inputs(
        guidelines, _record([0, 1], [1, 1]), card, 7, [3.0, 0.2, 0.0], 50.0)

    assert prior is expected and labels is None and peaks is None
    assert called == {"state": card.state, "sample": "E", "T": 50.0,
                      "q": [3.0, 0.2, 0.0], "period": 2.0, "t_round": 4.0}


# --------------------------------------------------------------------------- zero and identity safety
def test_measurement_is_never_self_calibrated_even_with_a_strong_peak():
    x = np.linspace(-6.0, 12.0, 91)
    counts = 500.0 * np.exp(-0.5 * ((x - 6.0) / 0.6) ** 2) + 10.0
    r = sandbox.fit_context(_record(x, counts), "ana7", _card(), _calres(center=None),
                            _guidelines(_card()), unit_key="E:7:ana7", scan_number=7,
                            q=[0, 0, 0], T=50.0)
    assert r.status == "held"
    assert r.center is None
    assert not hasattr(sandbox, "_self_calibrate")


def test_unit_identity_is_right_anchored_and_record_match_is_exact():
    assert sandbox._split_unit("proposal:cycle:7:ana7") == ("proposal:cycle", 7, "ana7")
    records = [types.SimpleNamespace(experiment_id="other", scan_number=7),
               types.SimpleNamespace(experiment_id="proposal:cycle", scan_number=7)]
    assert sandbox._find_record(records, "proposal:cycle", 7) is records[1]
    assert sandbox._find_record(records, "missing", 7) is None


def test_missing_temperature_is_held_with_raw_data():
    r = sandbox.fit_context(_record([0, 1, 2], [1, 2, 1]), "ana7", _card(), _calres(),
                            _guidelines(_card()), unit_key="E:7:ana7", scan_number=7,
                            q=[0, 0, 0], T=float("nan"))
    assert r.status == "held" and "temperature" in r.reason
    assert r.x is not None
    assert not r.needs_manual_zero


def test_manual_zero_honours_the_users_placement_bypassing_the_threshold():
    # The user placed E₀ at 1.2 (raw). The manual path trusts it EVEN when the auto significance
    # gate would refuse — a threshold must not overrule what the operator can see. Center stays near
    # the placement (local refinement is bounded), zero_source records it was placed by hand.
    x = np.linspace(-6.0, 6.0, 61)
    flat = np.full_like(x, 10.0)                    # auto self-cal would refuse this (ok=False)
    calres = sandbox._manual_calres(_record(x, flat), "ana7", _calres(center=None), 1.2)
    assert calres.center is not None
    assert abs(calres.center - 1.2) < 1.5          # honoured the mark (refinement is bounded to ±1.5)
    assert calres.zero_source == "manual (preview)"


def test_held_result_carries_raw_data_for_manual_placement():
    # A held-for-no-zero result must NOT be blank: it carries the raw spectrum + a starting guess so
    # the dock can show the data and let the user drag E₀ onto the elastic.
    x = np.linspace(-6.0, 6.0, 61)
    counts = 400.0 * np.exp(-0.5 * ((x - 0.5) / 0.6) ** 2) + 10.0
    r = sandbox.fit_context(_record(x, counts), "ana7", _card(), _calres(center=None),
                            _guidelines(_card()), unit_key="E:7:ana7", scan_number=7, q=[0, 0, 0],
                            T=50.0)
    assert r.status == "held"
    assert r.needs_manual_zero
    assert r.x is not None and len(r.x) == len(counts)   # raw data carried
    assert r.zero_guess is not None and abs(r.zero_guess - 0.5) < 1.0   # guess near the elastic


def test_fit_no_result_preserves_calibrated_marker_without_manual_zero_prompt(
        qtbot, monkeypatch):
    from isar.session import driver
    from isar_gui.viewmodels.sandbox import SandboxViewModel
    from isar_gui.views.sandbox_pane import SandboxPane

    ctx = types.SimpleNamespace()
    monkeypatch.setattr(driver, "build_context", lambda *args, **kwargs: (ctx, 3, []))
    monkeypatch.setattr(driver, "fit_one", lambda *args, **kwargs: {
        "omega0": None, "gamma": None})
    result = sandbox.fit_context(
        _record([0, 1, 2], [1, 2, 1]), "ana7", _card(), _calres(center=0.35),
        _guidelines(_card()), unit_key="E:7:ana7", scan_number=7, q=[0, 0, 0], T=50.0)

    assert result.status == "held" and not result.needs_manual_zero
    assert result.zero_guess == pytest.approx(0.35)
    dock = SandboxPane(SandboxViewModel(), None)
    qtbot.addWidget(dock)
    dock._on_result(result)
    assert "drag" not in dock._status.text().lower()


@pytest.mark.parametrize(
    ("label", "modes", "amplitudes", "prefixes"),
    [
        ("stokes+bg", [{"omega0": 5.0, "gamma": 0.4, "amplitude": 120.0}],
         {"phonon.A": 120.0, "bg.BG": 8.0}, ["phonon", "bg"]),
        ("stokes+qe+bg", [{"omega0": 5.0, "gamma": 0.4, "amplitude": 120.0}],
         {"phonon.A": 120.0, "qe.QE": 30.0, "bg.BG": 8.0}, ["phonon", "qe", "bg"]),
        ("elastic+stokes2+bg",
         [{"omega0": 4.0, "gamma": 0.3, "amplitude": 90.0},
          {"omega0": 9.0, "gamma": 0.7, "amplitude": 50.0}],
         {"phonon.A": 90.0, "phonon2.A": 50.0, "elastic.E": 20.0, "bg.BG": 8.0},
         ["phonon", "phonon2", "elastic", "bg"]),
    ],
)
def test_winning_curve_preserves_exact_selected_structure(label, modes, amplitudes, prefixes):
    ctx = types.SimpleNamespace(
        center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, quasielastic_fwhm=2.5,
        T=50.0, energy_status="needs_zero", window=(-8.0, 15.0), prior_omega=None,
        counts_used=np.ones(20))
    x = np.linspace(-8.0, 15.0, 101)
    curve = sandbox._winning_curve(
        {"best_label": label, "modes": modes, "amplitudes": amplitudes}, ctx, x)
    assert [name for name, _values in curve.components] == prefixes
    assert np.allclose(curve.total, sum(values for _name, values in curve.components))


# --------------------------------------------------------------------------- the real fit core
@pytest.mark.slow
def test_fit_core_fits_a_single_peak():
    from isar.synth._ff import convolute_voigt_dho
    x = np.linspace(-6.0, 24.0, 121)
    peak = convolute_voigt_dho(x, 6.0, 0.0, 1.0, 0.0, 0.4, 600.0, 0.0, 0.0, 50.0,
                               peak="stokes+astokes")
    counts = peak + 10.0
    card = _card()
    r = sandbox.fit_context(_record(x, counts), "ana7", card, _calres(center=0.0),
                            _guidelines(card), unit_key="E:7:ana7", scan_number=7,
                            q=[3.0, 0.2, 0.0], T=50.0,
                            peaks=({"omega0": 6.0, "sigma": 0.5},), m=20, seed=1)
    assert r.status == "fitted", r.reason
    assert abs(r.omega0 - 6.0) < 1.5                      # recovered the phonon
    assert r.x_fit is not None and r.total is not None    # a preview curve was reconstructed
    assert len(r.x) == len(counts)                        # data carried for the overlay
