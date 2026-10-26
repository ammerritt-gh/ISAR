"""Step-4 gates (the retired unification build order §10.4, git history): the prior builder and guidelines behave as designed."""
import numpy as np
import pytest

from isar.schema.card import CardState
from isar.session import guidelines as G
from isar.session import session_prior as SP
from isar.schema import card as card_mod


# --------------------------------------------------------------------------- prior builder
def _state_with_line(slope=2.0, intercept=1.0, ks=(0.1, 0.2, 0.3, 0.4), sample="S", T=100.0):
    st = CardState()
    for i, k in enumerate(ks):
        SP.add_point(st, scan_id=f"{sample}:{i}", sample=sample, T=T,
                     q=[3.0, k, -0.1], omega0=intercept + slope * k,
                     modality="fold", verdict="resolved")
    return st


def test_add_point_gates_resolved_fold_only():
    st = CardState()
    assert SP.add_point(st, scan_id="a", sample="S", T=100, q=[0, 0.1, 0], omega0=5.0,
                        modality="fork", verdict="resolved") is False   # fork rejected
    assert SP.add_point(st, scan_id="b", sample="S", T=100, q=[0, 0.1, 0], omega0=5.0,
                        modality="fold", verdict="flagged") is False    # not resolved
    assert SP.add_point(st, scan_id="c", sample="S", T=100, q=[0, 0.1, 0], omega0=5.0,
                        modality="fold", verdict="resolved") is True
    assert len(st.dispersion_prior) == 1


def test_add_point_replaces_same_scan():
    st = CardState()
    SP.add_point(st, scan_id="x", sample="S", T=100, q=[0, 0.1, 0], omega0=5.0,
                 modality="fold", verdict="resolved")
    SP.add_point(st, scan_id="x", sample="S", T=100, q=[0, 0.1, 0], omega0=6.0,
                 modality="fold", verdict="resolved")   # re-fit supersedes
    assert len(st.dispersion_prior) == 1
    assert st.dispersion_prior[0]["omega0"] == 6.0


def test_prior_linear_recovery_and_loo():
    st = _state_with_line(slope=2.0, intercept=1.0, ks=(0.1, 0.2, 0.3, 0.4))
    # Predict at k=0.25 (leave nothing out): expect 1 + 2*0.25 = 1.5.
    p = SP.prior_for(st, sample="S", T=100.0, q=[3.0, 0.25, -0.1])
    assert p is not None
    assert p.omega0 == pytest.approx(1.5, abs=1e-6)
    assert p.omega0_sigma == pytest.approx(SP.SIGMA_FLOOR)   # perfect line -> floored sigma
    assert p.amplitude is None                               # detection power abstains (v1)


def test_prior_none_when_empty_or_other_sample():
    st = _state_with_line(sample="S")
    assert SP.prior_for(st, sample="OTHER", T=100.0, q=[3.0, 0.2, 0]) is None
    assert SP.prior_for(CardState(), sample="S", T=100.0, q=[3.0, 0.2, 0]) is None


def test_prior_excludes_target_scan():
    st = _state_with_line(slope=2.0, intercept=1.0, ks=(0.1, 0.2, 0.3, 0.4))
    # Excluding one point still recovers the line from the rest (leave-one-out).
    p = SP.prior_for(st, sample="S", T=100.0, q=[3.0, 0.2, -0.1], exclude_scan_id="S:1")
    assert p.omega0 == pytest.approx(1.4, abs=1e-6)


# --------------------------------------------------------------------------- guidelines
def _card_with_fitting(fitting: dict):
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True),
                   card_mod.DetectorSpec("ana5", 8, "Ana5_3std", False)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting=fitting,
    )
    return card_mod.Card(pol, card_mod.CardState())


def test_guidelines_expectation_by_qrange():
    card = _card_with_fitting({
        "expectations": [{"q": {"component": "k", "min": 0.1, "max": 0.35},
                          "omega0": 5.2, "omega0_sigma": 0.5}]})
    g = G.resolve(card)
    p = g.expectation_for(scan_number=200, q=[3.0, 0.25, -0.1], T=100.0)
    assert p is not None and p.omega0 == 5.2 and p.omega0_sigma == 0.5
    # Outside the K range -> no expectation.
    assert g.expectation_for(scan_number=200, q=[3.0, 0.5, -0.1], T=100.0) is None


def test_guidelines_override_beats_expectation_and_excludes():
    card = _card_with_fitting({
        "expectations": [{"scans": [231], "omega0": 5.2}],
        "overrides": [{"scan": 231, "omega0": 4.0, "omega0_sigma": 0.3},
                      {"scan": 999, "exclude": True, "reason": "blown up"}]})
    g = G.resolve(card)
    p = g.expectation_for(scan_number=231, q=None, T=None)
    assert p.omega0 == 4.0 and p.omega0_sigma == 0.3      # override wins
    assert g.excluded(999) == "blown up"
    assert g.excluded(231) is None


def test_structure_filter_narrows_never_empties():
    from isar.analyze.structures import Hypothesis
    from isar.analyze.components import PhononDHO, ConstantBackground, Elastic
    hyps = [Hypothesis("stokes+bg", (PhononDHO, ConstantBackground)),
            Hypothesis("elastic+stokes+bg", (PhononDHO, Elastic, ConstantBackground))]
    card = _card_with_fitting({
        "expectations": [{"scans": [5], "omega0": 5.0, "structures": ["elastic+stokes+bg"]}]})
    g = G.resolve(card)
    kept, warn = g.filter_structures(hyps, scan_number=5, q=None, T=None)
    assert [h.label for h in kept] == ["elastic+stokes+bg"] and warn is None
    # A whitelist naming no admissible label keeps all + warns.
    card2 = _card_with_fitting({
        "expectations": [{"scans": [5], "omega0": 5.0, "structures": ["nonexistent"]}]})
    kept2, warn2 = G.resolve(card2).filter_structures(hyps, scan_number=5, q=None, T=None)
    assert len(kept2) == 2 and warn2 is not None


def test_fit_detectors_precedence():
    card = _card_with_fitting({})
    g = G.resolve(card)
    assert G.fit_detectors(g, card) == ["ana7"]                       # card-enabled only
    assert G.fit_detectors(g, card, cli_detectors=["ana5"]) == ["ana5"]  # CLI overrides
    card2 = _card_with_fitting({"detectors": ["ana5", "ana7"]})
    assert G.fit_detectors(G.resolve(card2), card2) == ["ana5", "ana7"]  # guideline overrides


def test_omega_window_off_when_zero():
    assert G.resolve(_card_with_fitting({"omega_window": [0.0, 0.0]})).omega_window is None
    assert G.resolve(_card_with_fitting({"omega_window": [1.0, 20.0]})).omega_window == (1.0, 20.0)


def test_guidelines_signature_changes_with_exclusion():
    base = _card_with_fitting({})
    excl = _card_with_fitting({"overrides": [{"scan": 231, "exclude": True}]})
    sig_base = G.guidelines_signature(G.resolve(base), scan_number=231, q=None, T=None)
    sig_excl = G.guidelines_signature(G.resolve(excl), scan_number=231, q=None, T=None)
    assert sig_base != sig_excl
    # A scan the edit doesn't touch keeps its signature.
    other_base = G.guidelines_signature(G.resolve(base), scan_number=100, q=None, T=None)
    other_excl = G.guidelines_signature(G.resolve(excl), scan_number=100, q=None, T=None)
    assert other_base == other_excl


# --------------------------------------------------------------------------- fit-window masking (Stage D)
def test_parse_fit_window_fields_from_toml():
    card = _card_with_fitting({
        "expectations": [{
            "q": {"component": "k", "min": 0.1, "max": 0.35},
            "fit_range": [2.0, 8.0],
            "exclude_regions": [[3.0, 3.5], [6.0, 6.2]],
            "peaks": [{"omega0": 4.05, "sigma": 0.5}, {"omega0": 6.5, "sigma": 0.4}],
        }]})
    g = G.resolve(card)
    fr, excl = g.mask_regions_for(scan_number=200, q=[3.0, 0.25, -0.1], T=100.0)
    assert fr == (2.0, 8.0)
    assert excl == ((3.0, 3.5), (6.0, 6.2))
    peaks = g.peaks_for(scan_number=200, q=[3.0, 0.25, -0.1], T=100.0)
    assert peaks == ({"omega0": 4.05, "sigma": 0.5}, {"omega0": 6.5, "sigma": 0.4})
    # Outside the K range -> no mask, no peaks.
    assert g.mask_regions_for(scan_number=200, q=[3.0, 0.9, -0.1], T=100.0) == (None, ())
    assert g.peaks_for(scan_number=200, q=[3.0, 0.9, -0.1], T=100.0) == ()


def test_scalar_omega0_normalizes_to_one_peak():
    card = _card_with_fitting({
        "expectations": [{"scans": [7], "omega0": 5.2, "omega0_sigma": 0.6}]})
    g = G.resolve(card)
    peaks = g.peaks_for(scan_number=7, q=None, T=None)
    assert peaks == ({"omega0": 5.2, "sigma": 0.6},)
    # expectation_for (primary-only back-compat) is unchanged.
    p = g.expectation_for(scan_number=7, q=None, T=None)
    assert p.omega0 == 5.2 and p.omega0_sigma == 0.6
    # No explicit peaks and no scalar omega0 -> empty.
    assert G.Expectation().peak_hints() == ()


def test_override_beats_expectation_for_mask_and_peaks():
    card = _card_with_fitting({
        "expectations": [{"scans": [42], "fit_range": [1.0, 9.0],
                          "exclude_regions": [[2.0, 2.5]],
                          "peaks": [{"omega0": 5.0, "sigma": 0.5}]}],
        "overrides": [{"scan": 42, "fit_range": [3.0, 7.0],
                       "exclude_regions": [[4.0, 4.2]],
                       "peaks": [{"omega0": 4.0, "sigma": 0.3},
                                 {"omega0": 6.0, "sigma": 0.3}]}]})
    g = G.resolve(card)
    fr, excl = g.mask_regions_for(scan_number=42, q=None, T=None)
    assert fr == (3.0, 7.0) and excl == ((4.0, 4.2),)          # override wins
    peaks = g.peaks_for(scan_number=42, q=None, T=None)
    assert peaks == ({"omega0": 4.0, "sigma": 0.3}, {"omega0": 6.0, "sigma": 0.3})


def test_malformed_fit_window_fields_warn_and_ignore():
    card = _card_with_fitting({
        "expectations": [{
            "scans": [3],
            "fit_range": [8.0, 2.0],                            # lo >= hi -> ignored
            "exclude_regions": [[3.0, 2.0], [5.0, 6.0]],        # first bad -> dropped
            "peaks": [{"sigma": 0.5},                           # no omega0 -> dropped
                      {"omega0": 4.0, "sigma": -1.0},           # sigma <= 0 -> dropped
                      {"omega0": 6.0}],                         # ok, default sigma
        }]})
    with pytest.warns(UserWarning):
        g = G.resolve(card)
    fr, excl = g.mask_regions_for(scan_number=3, q=None, T=None)
    assert fr is None
    assert excl == ((5.0, 6.0),)
    assert g.peaks_for(scan_number=3, q=None, T=None) == ({"omega0": 6.0, "sigma": 0.5},)


def test_signature_includes_fit_window_and_changes_on_edit():
    base = _card_with_fitting({})
    sig_base = G.guidelines_signature(G.resolve(base), scan_number=50, q=None, T=None)
    assert "fit_range" in sig_base and "exclude_regions" in sig_base and "peaks" in sig_base

    for field_edit in (
        {"expectations": [{"scans": [50], "fit_range": [1.0, 9.0]}]},
        {"expectations": [{"scans": [50], "exclude_regions": [[3.0, 3.5]]}]},
        {"expectations": [{"scans": [50], "peaks": [{"omega0": 4.0, "sigma": 0.5}]}]},
    ):
        edited = _card_with_fitting(field_edit)
        sig_edit = G.guidelines_signature(G.resolve(edited), scan_number=50, q=None, T=None)
        assert sig_edit != sig_base
        # A scan the edit doesn't touch keeps its signature.
        assert (G.guidelines_signature(G.resolve(edited), scan_number=999, q=None, T=None)
                == G.guidelines_signature(G.resolve(base), scan_number=999, q=None, T=None))


# --------------------------------------------------------------------------- gold reproduction gate
def test_prior_reproduces_predict_omega_shape():
    """prior_for must match the gold check's predict_omega on a K-dispersion at one T (same rule:
    linear-in-K within same T, pooled otherwise, sigma floored at 0.3)."""
    # Mimic predict_omega on synthetic OK rows: omega0 = 1.0 + 3.0*K + small scatter.
    rng = np.random.default_rng(0)
    ks = [0.1, 0.15, 0.2, 0.25, 0.3]
    st = CardState()
    truth = {}
    for i, k in enumerate(ks):
        w = 1.0 + 3.0 * k + float(rng.normal(0, 0.05))
        truth[k] = w
        SP.add_point(st, scan_id=f"S:{i}", sample="S", T=100.0, q=[3.0, k, -0.1],
                     omega0=w, modality="fold", verdict="resolved")

    # Reference predict_omega (from docs/phase3_gold_check.py), K-only.
    def predict(target_k, exclude_i):
        pool = [(k, truth[k]) for j, k in enumerate(ks) if j != exclude_i]
        K = np.array([k for k, _ in pool]); O = np.array([o for _, o in pool])
        coef = np.polyfit(K, O, 1)
        return float(np.polyval(coef, target_k)), max(0.3, float(np.std(O - np.polyval(coef, K))))

    for i, k in enumerate(ks):
        pred, sig = predict(k, i)
        p = SP.prior_for(st, sample="S", T=100.0, q=[3.0, k, -0.1], exclude_scan_id=f"S:{i}")
        assert p.omega0 == pytest.approx(pred, abs=1e-6)
        assert p.omega0_sigma == pytest.approx(sig, abs=1e-6)

# --------------------------------------------------------------------------- mode ceiling (S1U1)
def test_mode_ceiling_precedence_card_then_env_then_six(monkeypatch):
    monkeypatch.delenv("ISAR_MODE_CEILING", raising=False)
    assert G.mode_ceiling(G.resolve(_card_with_fitting({}))) == 6          # the default
    monkeypatch.setenv("ISAR_MODE_CEILING", "3")
    assert G.mode_ceiling(G.resolve(_card_with_fitting({}))) == 3          # environment
    card = _card_with_fitting({"max_modes": 4})
    assert G.mode_ceiling(G.resolve(card)) == 4                            # card beats environment
    assert G.mode_ceiling(card) == 4                                       # a Card works too
    assert G.mode_ceiling(G.Guidelines()) == 3                             # so does a bare policy


def test_mode_ceiling_ignores_a_malformed_value_loudly(monkeypatch):
    monkeypatch.delenv("ISAR_MODE_CEILING", raising=False)
    with pytest.warns(UserWarning, match="max_modes"):
        g = G.resolve(_card_with_fitting({"max_modes": 0}))
    assert G.mode_ceiling(g) == 6
    monkeypatch.setenv("ISAR_MODE_CEILING", "many")
    with pytest.warns(UserWarning, match="ISAR_MODE_CEILING"):
        assert G.mode_ceiling(G.Guidelines()) == 6


def test_one_mode_ceiling_rule_for_production_and_the_benchmark(monkeypatch):
    """The environment rule lives once, in the analyze layer: the benchmark and the card reader read
    it alike (a 0 from the environment is refused by both), and analyze imports nothing from
    session to get it. The benchmark's explicit argument may still be 0."""
    import ast
    import inspect

    from isar.analyze import phonon
    from isar.synth import count_bench as cb

    monkeypatch.setenv("ISAR_MODE_CEILING", "0")
    with pytest.warns(UserWarning, match="ISAR_MODE_CEILING"):
        assert cb.mode_ceiling() == G.mode_ceiling(G.Guidelines()) == 6
    assert cb.mode_ceiling(0) == 0
    monkeypatch.setenv("ISAR_MODE_CEILING", "5")
    assert cb.mode_ceiling() == G.mode_ceiling(G.Guidelines()) == phonon.default_mode_ceiling() == 5
    imported = {node.module for node in ast.walk(ast.parse(inspect.getsource(phonon)))
                if isinstance(node, ast.ImportFrom)}
    assert not any(m and m.startswith("isar.session") for m in imported)


def test_guidelines_signature_changes_with_ceiling_and_recipe_version(monkeypatch):
    from isar.analyze import prior
    monkeypatch.delenv("ISAR_MODE_CEILING", raising=False)
    sig = lambda card: G.guidelines_signature(G.resolve(card), scan_number=1, q=None, T=None)
    base = sig(_card_with_fitting({}))
    assert sig(_card_with_fitting({})) == base                              # stable
    assert sig(_card_with_fitting({"max_modes": 4})) != base                # card ceiling
    monkeypatch.setenv("ISAR_MODE_CEILING", "3")
    assert sig(_card_with_fitting({})) != base                              # environment ceiling
    monkeypatch.delenv("ISAR_MODE_CEILING")
    monkeypatch.setattr(prior, "PRIOR_RECIPE_VERSION", prior.PRIOR_RECIPE_VERSION + "+next")
    assert sig(_card_with_fitting({})) != base                              # recipe version


def test_the_card_tables_configure_the_fast_pass():
    from isar.analyze.prior import PriorSettings
    card = _card_with_fitting({
        "fast_pass": {"seed_stop_gain": 4.0, "confident_anchor": [[300.0, 25.0], [600.0, 100.0]]},
        "prior": {"amplitude_margin": 3.0}})
    settings = G.fast_pass_settings(card)
    assert settings.seed_stop_gain == 4.0
    assert settings.confident_anchor == ((300.0, 25.0), (600.0, 100.0))
    assert settings.prior == PriorSettings(amplitude_margin=3.0)
    assert G.fast_pass_settings(_card_with_fitting({})).prior is None


def test_bad_fast_pass_entries_warn_and_keep_the_defaults():
    from isar.analyze.fast_pass import FastPassSettings
    card = _card_with_fitting({
        "fast_pass": {"mode_ceiling": 2, "no_such_key": 1, "confident_anchor": 70},
        "prior": {"amplitude_margin": -1.0}})
    with pytest.warns(UserWarning) as caught:
        settings = G.fast_pass_settings(card)
    text = " ".join(str(w.message) for w in caught)
    assert "mode_ceiling" in text and "no_such_key" in text and "confident_anchor" in text
    assert "[fitting.prior]" in text
    assert settings.confident_anchor == FastPassSettings().confident_anchor
    assert settings.prior is None and settings.mode_ceiling == G.mode_ceiling(G.resolve(card))


def test_a_fast_pass_edit_changes_the_signature_of_a_scan():
    base = G.guidelines_signature(G.resolve(_card_with_fitting({})), scan_number=1, q=None, T=None)
    edit = G.guidelines_signature(G.resolve(_card_with_fitting({"fast_pass": {"seed_stop_gain": 4.0}})),
                                  scan_number=1, q=None, T=None)
    assert edit != base
