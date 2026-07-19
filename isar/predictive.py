"""Qt-free posterior-predictive information scoring shared by analysis and steering."""
from __future__ import annotations

import math

import numpy as np


def _discrete_information(latent, observed) -> float:
    latent = np.asarray(latent, dtype=int)
    observed = np.asarray(observed, dtype=int)
    if latent.size == 0:
        return 0.0
    joint, latent_counts, observed_counts = {}, {}, {}
    for left, right in zip(latent.tolist(), observed.tolist()):
        joint[(left, right)] = joint.get((left, right), 0) + 1
        latent_counts[left] = latent_counts.get(left, 0) + 1
        observed_counts[right] = observed_counts.get(right, 0) + 1
    n = float(latent.size)
    return float(sum(
        (count / n) * math.log((count * n) /
                               (latent_counts[left] * observed_counts[right]))
        for (left, right), count in joint.items() if count))


def predictive_score(models: list[dict], *, seed: int, n_simulations: int = 512) -> dict:
    """Seeded information estimate for one incremental scan from full-parameter model draws."""
    draw_models, draw_omega, draw_rates, draw_weights = [], [], [], []
    for model_index, model in enumerate(models):
        rates = np.asarray(model.get("expected_counts") or (), dtype=float)
        omega = np.asarray(model.get("omega0_draws") or (), dtype=float)
        weight = float(model.get("weight") or 0.0)
        if (rates.ndim != 2 or rates.shape[0] == 0 or rates.shape[1] == 0
                or omega.ndim != 1 or omega.size not in (0, rates.shape[0])
                or not np.all(np.isfinite(rates)) or np.any(rates < 0.0)
                or (omega.size and not np.all(np.isfinite(omega)))
                or not math.isfinite(weight) or weight <= 0.0):
            continue
        draw_models.extend([model_index] * rates.shape[0])
        # A no-phonon model has a valid predictive count distribution but no target-ω draw.  Keep it
        # in the joint/model latent state using a dedicated missing-ω state below.
        draw_omega.extend(omega.tolist() if omega.size else [float("nan")] * rates.shape[0])
        draw_rates.extend(rates.tolist())
        draw_weights.extend([weight / rates.shape[0]] * rates.shape[0])
    if not draw_rates or sum(draw_weights) <= 0.0:
        return {"usable": False, "reason": "no valid full-parameter predictive draws"}
    weights = np.asarray(draw_weights, dtype=float)
    weights /= weights.sum()
    rng = np.random.default_rng(int(seed))
    choices = rng.choice(len(draw_rates), size=max(int(n_simulations), 64), p=weights)
    rates = np.maximum(np.asarray(draw_rates, dtype=float)[choices], 0.0)
    counts = rng.poisson(rates)
    energy_index = np.arange(counts.shape[1], dtype=float)
    totals = counts.sum(axis=1)
    centroids = np.divide(counts @ energy_index, totals,
                          out=np.zeros_like(totals, dtype=float), where=totals > 0)
    omega = np.asarray(draw_omega, dtype=float)[choices]
    models_drawn = np.asarray(draw_models, dtype=int)[choices]

    def quantile_bins(values, bins=8):
        edges = np.unique(np.quantile(values, np.linspace(0.0, 1.0, bins + 1)))
        return (np.digitize(values, edges[1:-1]) if edges.size > 2
                else np.zeros(values.size, dtype=int))

    observed = quantile_bins(centroids) * 8 + quantile_bins(totals)
    finite_omega = np.isfinite(omega)
    omega_bins = np.zeros(omega.size, dtype=int)
    if np.any(finite_omega):
        finite_bins = quantile_bins(omega[finite_omega])
        omega_bins[finite_omega] = finite_bins
        omega_bins[~finite_omega] = int(finite_bins.max(initial=0)) + 1
    joint_latent = omega_bins * max(len(models), 1) + models_drawn
    joint_information = _discrete_information(joint_latent, observed)
    omega_information = (_discrete_information(omega_bins[finite_omega], observed[finite_omega])
                         if np.any(finite_omega) else 0.0)
    identity_information = _discrete_information(models_drawn, observed)
    finite_values = omega[finite_omega]
    current_halfwidth = (0.5 * (np.quantile(finite_values, 0.84) -
                                np.quantile(finite_values, 0.16))
                         if finite_values.size else 0.0)
    posterior_widths = []
    for observed_bin in np.unique(observed):
        selected = omega[(observed == observed_bin) & finite_omega]
        if selected.size > 2:
            posterior_widths.append((selected.size, 0.5 * (
                np.quantile(selected, 0.84) - np.quantile(selected, 0.16))))
    expected_width = (sum(n * width for n, width in posterior_widths) /
                      sum(n for n, _ in posterior_widths)) if posterior_widths else current_halfwidth
    return {
        "usable": True, "method": "posterior_predictive_mc",
        "expected_information_nats": float(joint_information),
        "omega_information_nats": float(omega_information),
        "model_branch_information_nats": float(identity_information),
        "expected_omega_halfwidth_reduction_meV": max(
            float(current_halfwidth - expected_width), 0.0),
        "simulation_count": int(len(choices)),
        "score_standard_error_nats": float(
            max(joint_information, 1e-9) / math.sqrt(len(choices))),
        "seed": int(seed), "common_random_seed": int(seed),
    }
