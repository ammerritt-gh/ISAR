from types import SimpleNamespace

import numpy as np

from isar.session import driver
from isar.session.driver import (_bounded_parameter_draws, _persist_master_predictive,
                                 _predictive_ensemble, _predictive_score)


def test_full_parameter_draw_basis_prefers_nested_then_bootstrap_then_laplace():
    nested = SimpleNamespace(
        within_model={"bayes_ok": True, "bayes_parameter_names": ["phonon.Omega_d", "bg.BG"],
                      "bayes_parameter_samples": [[2.0, 1.0], [2.1, 1.2]],
                      "parameter_names": ["wrong"], "parameter_samples": [[9.0]]},
        covariance_ok=True, parameter_covariance=np.eye(2),
        params={"phonon.Omega_d": 2.0, "bg.BG": 1.0})
    names, samples, basis = _bounded_parameter_draws(nested, seed=7)
    assert names == ("phonon.Omega_d", "bg.BG")
    assert samples.shape == (2, 2) and basis == "nested_sampling"

    nested.within_model = {"parameter_names": ["phonon.Omega_d", "bg.BG"],
                           "parameter_samples": [[2.0, 1.0], [2.1, 1.2]]}
    assert _bounded_parameter_draws(nested, seed=7)[2] == "parametric_bootstrap"

    nested.within_model = {}
    names, samples, basis = _bounded_parameter_draws(nested, seed=7, maximum=8)
    assert names == ("phonon.Omega_d", "bg.BG")
    assert samples.shape == (8, 2) and basis == "laplace_hesse"


def test_predictive_information_is_deterministic_and_model_discriminating():
    models = [
        {"weight": 0.5, "omega0_draws": [2.0, 2.1],
         "expected_counts": [[30.0, 1.0, 1.0], [28.0, 2.0, 1.0]]},
        {"weight": 0.5, "omega0_draws": [6.0, 6.1],
         "expected_counts": [[1.0, 1.0, 30.0], [1.0, 2.0, 28.0]]},
    ]
    first = _predictive_score(models, seed=123, n_simulations=256)
    second = _predictive_score(models, seed=123, n_simulations=256)
    assert first == second
    assert first["usable"] is True
    assert first["model_branch_information_nats"] > 0.0
    assert first["expected_information_nats"] >= first["model_branch_information_nats"]


def test_no_phonon_model_keeps_expected_counts_for_identity_information():
    models = [
        {"weight": 0.5, "omega0_draws": [],
         "expected_counts": [[30.0, 1.0, 1.0], [28.0, 2.0, 1.0]]},
        {"weight": 0.5, "omega0_draws": [6.0, 6.1],
         "expected_counts": [[1.0, 1.0, 30.0], [1.0, 2.0, 28.0]]},
    ]
    score = _predictive_score(models, seed=123, n_simulations=256)
    assert score["usable"] is True
    assert score["model_branch_information_nats"] > 0.0
    assert score["expected_omega_halfwidth_reduction_meV"] >= 0.0


def test_predictive_ensemble_keeps_null_model_without_omega(monkeypatch):
    class NullComposite:
        @staticmethod
        def phonon_indices():
            return []

        @staticmethod
        def evaluate(x, sample, _ctx):
            return np.full(len(x), sample[0], dtype=float)

    monkeypatch.setattr(
        driver, "_bounded_parameter_draws",
        lambda result, **kwargs: (("bg.BG",), np.asarray([[2.0], [3.0]]), "laplace_hesse"))
    monkeypatch.setattr(
        driver, "_model_manifest_of",
        lambda result, by_hyp: {"schema": "isar.model_manifest/1", "components": []})
    result = SimpleNamespace(omega0=None)
    hypothesis = SimpleNamespace(build=lambda: NullComposite())
    score = SimpleNamespace(label="null", weight=1.0)
    ctx = SimpleNamespace(x_used=np.asarray([-1.0, 0.0, 1.0]))

    ensemble = _predictive_ensemble(
        result, [score], {"null": result}, {"null": hypothesis}, ctx, seed=7, n_members=2)

    assert ensemble is not None
    assert ensemble["models"][0]["omega0_draws"] == []
    assert ensemble["models"][0]["expected_counts"] == [[1.0, 1.0, 1.0], [1.5, 1.5, 1.5]]
    assert ensemble["diagnostics"]["usable"] is True


def test_predictive_artifact_is_content_addressed_and_retained(tmp_path):
    payload = {"schema": "isar.master_predictive/1", "seed": 7, "models": [],
               "master_content_hash": "abc", "diagnostics": {"usable": False}}
    data = tmp_path / "data"
    first = _persist_master_predictive(data, payload)
    second = _persist_master_predictive(data, payload)
    assert first == second
    assert (tmp_path / first).exists()

    changed = _persist_master_predictive(data, {**payload, "master_content_hash": "def"})
    assert changed != first
    assert (tmp_path / first).exists() and (tmp_path / changed).exists()
