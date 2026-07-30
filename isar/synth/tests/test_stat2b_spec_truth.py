"""Fast contract tests for STAT-2b campaign specifications and planted truth."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from isar.synth.stat2b.spec import (
    CAMPAIGN_SCHEMA,
    CRITICAL_STRATA,
    CampaignSpec,
    ScenarioSpec,
    load_campaign_spec,
)
from isar.synth.stat2b.truth import generate_dataset


def _scenario(
    scenario_id="one",
    *,
    truth_kind="one_mode",
    energies=(4.0,),
    target_rank="single",
    critical_strata=("weak_signal",),
    oracle_required=False,
    **changes,
):
    values = dict(
        id=scenario_id,
        truth_kind=truth_kind,
        target_present=bool(energies),
        target_rank=target_rank,
        peak_counts=30,
        x_min=-10.0,
        x_max=10.0,
        x_step=0.25,
        gauss_fwhm=0.961,
        lorentz_fwhm=0.596,
        T=100.0,
        gamma=0.5,
        energies=energies,
        critical_strata=critical_strata,
        oracle_required=oracle_required,
    )
    values.update(changes)
    return ScenarioSpec(**values)


def _scenarios():
    return (
        _scenario(),
        _scenario(
            "near", energies=(1.2,), critical_strata=("near_elastic",),
        ),
        _scenario(
            "two", truth_kind="two_mode", energies=(3.0, 4.5), target_rank="lower",
            critical_strata=("partially_resolved_two_mode",), oracle_required=True,
        ),
        _scenario(
            "null", truth_kind="target_absent", energies=(), target_rank="none",
            critical_strata=("target_absent",), oracle_required=True,
        ),
        _scenario(
            "slope", truth_kind="misspecified_background", energies=(4.0,),
            background_slope_fraction=0.5, critical_strata=("misspecified_background",),
            oracle_required=True,
        ),
    )


def _spec(scenarios=None):
    return CampaignSpec(
        schema=CAMPAIGN_SCHEMA,
        campaign_id="test-stat2b",
        base_seed=20260730,
        phase_seed_namespaces={"development": "dev-sealed", "heldout": "held-sealed"},
        development_realizations=20,
        heldout_realizations=0,
        delivery_replicates=200,
        certification_tolerance_mev=0.1,
        cell_weighting="equal",
        critical_strata=tuple(CRITICAL_STRATA),
        scenarios=_scenarios() if scenarios is None else tuple(scenarios),
    )


def _toml(scenarios):
    lines = [
        f'schema = "{CAMPAIGN_SCHEMA}"',
        'campaign_id = "test-stat2b"',
        "base_seed = 20260730",
        "development_realizations = 20",
        "heldout_realizations = 0",
        "delivery_replicates = 200",
        "certification_tolerance_mev = 0.1",
        'cell_weighting = "equal"',
        'critical_strata = ["near_elastic", "weak_signal", '
        '"partially_resolved_two_mode", "target_absent", "misspecified_background"]',
        "",
        "[phase_seed_namespaces]",
        'development = "dev-sealed"',
        'heldout = "held-sealed"',
    ]
    for scenario in scenarios:
        payload = scenario.canonical_payload()
        lines.extend(
            [
                "",
                "[[scenarios]]",
                f'id = "{payload["id"]}"',
                f'truth_kind = "{payload["truth_kind"]}"',
                f'target_present = {str(payload["target_present"]).lower()}',
                f'target_rank = "{payload["target_rank"]}"',
                f'peak_counts = {payload["peak_counts"]}',
                f'x_min = {payload["x_min"]}',
                f'x_max = {payload["x_max"]}',
                f'x_step = {payload["x_step"]}',
                f'gauss_fwhm = {payload["gauss_fwhm"]}',
                f'lorentz_fwhm = {payload["lorentz_fwhm"]}',
                f'T = {payload["T"]}',
                f'gamma = {payload["gamma"]}',
                "energies = [" + ", ".join(str(v) for v in payload["energies"]) + "]",
                "critical_strata = ["
                + ", ".join(f'"{v}"' for v in payload["critical_strata"]) + "]",
                f'oracle_required = {str(payload["oracle_required"]).lower()}',
            ]
        )
        for name in (
            "background_slope_fraction",
        ):
            if payload[name]:
                lines.append(f"{name} = {payload[name]}")
    return "\n".join(lines) + "\n"


def test_strict_load_has_order_independent_canonical_fingerprint(tmp_path):
    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    first.write_text(_toml(_scenarios()), encoding="utf-8")
    second.write_text(_toml(tuple(reversed(_scenarios()))), encoding="utf-8")
    loaded = load_campaign_spec(first)
    reordered = load_campaign_spec(second)
    assert loaded.fingerprint == reordered.fingerprint
    assert [scenario.id for scenario in loaded.scenarios] == sorted(s.id for s in _scenarios())

    bad = tmp_path / "bad.toml"
    bad.write_text(first.read_text(encoding="utf-8") + "surprise = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        load_campaign_spec(bad)


def test_scenario_rejects_background_slope_that_can_make_truth_negative():
    with pytest.raises(ValueError, match="background_slope_fraction"):
        replace(_scenario("slope"), background_slope_fraction=1.01)


def test_seed_streams_are_phase_separated_and_order_independent():
    spec = _spec()
    reverse = _spec(reversed(spec.scenarios))
    a = generate_dataset(spec, "one", 7, phase="development")
    b = generate_dataset(reverse, "one", 7, phase="development")
    heldout = generate_dataset(spec, "one", 7, phase="heldout")
    assert a.dataset_id == b.dataset_id
    assert a.seed == b.seed
    assert np.array_equal(a.fit_context.counts, b.fit_context.counts)
    assert not np.array_equal(a.fit_context.counts, a.heldout_counts)
    assert heldout.dataset_id != a.dataset_id
    assert heldout.seed != a.seed


def test_truth_manifest_is_firewalled_from_fit_context():
    dataset = generate_dataset(_spec(), "two", 0)
    assert dataset.truth_manifest["target_mode_index"] == 0
    assert dataset.truth_manifest["target_rank"] == "lower"
    assert not hasattr(dataset.fit_context, "truth_manifest")
    assert not hasattr(dataset.fit_context, "target_rank")
    assert np.array_equal(dataset.fit_context.counts, dataset.fit_context.counts_used)
    assert dataset.truth_manifest["truth_fingerprint"]


@pytest.mark.parametrize(
    ("name", "updates", "assertion"),
    [
        (
            "null_elastic_qe",
            dict(
                truth_kind="target_absent", energies=(), target_rank="none",
                target_present=False, elastic_height_ratio=2.0, qe_height_ratio=1.0,
                critical_strata=("target_absent",), oracle_required=True,
            ),
            lambda d: not d.truth_manifest["modes"] and d.lambda_counts.max() > 30,
        ),
        (
            "stokes_only",
            dict(phonon_sides="stokes"),
            lambda d: d.truth_manifest["modes"][0]["phonon_sides"] == "stokes",
        ),
        (
            "continuum",
            dict(
                truth_kind="continuum", energies=(), target_rank="none",
                target_present=False, continuum_height_ratio=2.0,
                critical_strata=("target_absent",), oracle_required=True,
            ),
            lambda d: not d.truth_manifest["modes"] and d.lambda_counts.max() > 30,
        ),
        (
            "perturbed_exposure",
            dict(
                calibration_offset=0.2, resolution_scale=1.2,
                exposure_pattern="linear", exposure_min_scale=0.5, exposure_max_scale=1.5,
            ),
            lambda d: d.truth_manifest["true_center"] != d.fit_context.center
            and d.fit_context.exposure_scale is not None,
        ),
    ],
)
def test_representative_truth_regimes(name, updates, assertion):
    scenario = replace(_scenario(name), **updates)
    scenarios = list(_scenarios())
    scenarios.append(scenario)
    spec = _spec(scenarios)
    dataset = generate_dataset(spec, name, 2)
    assert np.all(np.isfinite(dataset.lambda_counts))
    assert np.all(dataset.lambda_counts >= 0)
    assert assertion(dataset)


@pytest.mark.parametrize(
    ("elastic_ratio", "qe_ratio"),
    [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)],
)
def test_four_target_absent_component_combinations(elastic_ratio, qe_ratio):
    null = _scenario(
        "null_combo", truth_kind="target_absent", energies=(), target_rank="none",
        critical_strata=("target_absent",), oracle_required=True,
        elastic_height_ratio=elastic_ratio, qe_height_ratio=qe_ratio,
    )
    scenarios = list(_scenarios())
    scenarios[3] = null
    dataset = generate_dataset(_spec(scenarios), null, 1)
    assert dataset.truth_manifest["target_present"] is False
    assert dataset.truth_manifest["elastic_height_ratio"] == elastic_ratio
    assert dataset.truth_manifest["qe_height_ratio"] == qe_ratio
