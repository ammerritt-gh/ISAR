"""Stage 6 (B2): the driver side of the opt-in master structure-skip.

``_fit_masters`` reads the ``[fitting].reuse_master_structure`` policy and, when on, threads an
invalidated group's PREVIOUS fitted ``best_label`` down to the fit as a ``structure_hint`` (both the
serial and pooled paths). ``_fit_master_group`` stamps ``provenance["structure_skipped"]`` on any
entry the hint actually shortcut. With the flag off (default) no hint is threaded and nothing
changes — the guard the whole feature rides on.
"""
from types import SimpleNamespace

from isar.session import driver
from isar.session.state import ScanEntry, SessionState


class _MS:
    def __init__(self, members):
        self.members = members
        self.n_members = len(members)


class _Grp:
    def __init__(self, key, merged):
        self.key = key
        self.key_fields = {"sample": "tavi", "q": [2.1, 0.0, 0.0], "T": 200.0}
        self.merged = merged


class _Col:
    def __init__(self, merge, groups):
        self.merge = merge
        self.groups = groups


def _card(reuse):
    # _fit_masters only reads card.policy.fitting; the stubbed _fit_master_group ignores the rest.
    return SimpleNamespace(policy=SimpleNamespace(fitting={"reuse_master_structure": reuse}))


def _capturing_stub(monkeypatch, *, best_label=None):
    """Replace _fit_master_group with a fake that records the structure_hint it was handed and
    writes a fitted master carrying ``best_label`` (so a later run can read it back as a hint)."""
    seen = []

    def fake(ms, grp, member_rec, member_det, card, runcal, g, master_id, scan_number, chash,
             *, M, seed, errors_tier="full", boot_workers=1, boot_executor=None,
             boot_adaptive=False, structure_hint=None, bayes_opts=None):
        seen.append((master_id, structure_hint))
        e = ScanEntry(scan_id=master_id, scan_number=int(scan_number), scan_type="measurement",
                      content_hash=chash, status="fitted")
        e.detectors[member_det or "merged"] = {"pooled": True, "n_members": ms.n_members,
                                               "omega0": 0.95, "best_label": best_label}
        return e

    monkeypatch.setattr(driver, "_fit_master_group", fake)
    return seen


def _grp1(members):
    return _Col("merge", [_Grp(("tavi", "42", 200.0), _MS(list(members)))])


def test_flag_off_threads_no_hint(monkeypatch):
    # Prime a fitted master with a best_label, then refit (new member) with the flag OFF: the hint
    # must stay None (the off-path guarantee).
    seen = _capturing_stub(monkeypatch, best_label="elastic+stokes+bg")
    s = SessionState()
    ms = _MS([("tavi", 1, "det")])
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), ms)])
    assert driver._fit_masters(col, [], _card(False), None, None, s, M=10, seed_base=6000) == 1
    ms.members.append(("tavi", 2, "det")); ms.n_members = 2      # invalidate → refit
    seen.clear()
    assert driver._fit_masters(col, [], _card(False), None, None, s, M=10, seed_base=6000) == 1
    assert seen == [("master:tavi_42_200.0", None)]             # no hint threaded


def test_flag_on_threads_previous_best_label(monkeypatch):
    seen = _capturing_stub(monkeypatch, best_label="elastic+stokes+bg")
    s = SessionState()
    ms = _MS([("tavi", 1, "det")])
    col = _Col("merge", [_Grp(("tavi", "42", 200.0), ms)])
    assert driver._fit_masters(col, [], _card(True), None, None, s, M=10, seed_base=6000) == 1
    ms.members.append(("tavi", 2, "det")); ms.n_members = 2
    seen.clear()
    assert driver._fit_masters(col, [], _card(True), None, None, s, M=10, seed_base=6000) == 1
    assert seen == [("master:tavi_42_200.0", "elastic+stokes+bg")]   # previous winner threaded


def test_flag_on_no_previous_entry_threads_no_hint(monkeypatch):
    # Cold group (no prior ledger entry) with the flag on → nothing to reuse → hint None.
    seen = _capturing_stub(monkeypatch, best_label="stokes+bg")
    s = SessionState()
    assert driver._fit_masters(_grp1([("tavi", 1, "det")]), [], _card(True), None, None, s,
                               M=10, seed_base=6000) == 1
    assert seen == [("master:tavi_42_200.0", None)]


def test_flag_on_previous_not_fitted_threads_no_hint(monkeypatch):
    # A previous entry that was NOT fitted (held/non_result) is not a usable hint source.
    seen = _capturing_stub(monkeypatch, best_label="stokes+bg")
    s = SessionState()
    s.put(ScanEntry(scan_id="master:tavi_42_200.0", scan_number=driver._MASTER_SCAN_BASE,
                    scan_type="measurement", content_hash="stale", status="non_result"))
    assert driver._fit_masters(_grp1([("tavi", 1, "det")]), [], _card(True), None, None, s,
                               M=10, seed_base=6000) == 1
    assert seen == [("master:tavi_42_200.0", None)]


def test_provenance_stamped_when_fit_reports_skip(monkeypatch):
    # _fit_master_group stamps provenance["structure_skipped"] iff fit_one reports the shortcut.
    # Stub the heavy internals so only the stamping logic is under test.
    def fake_resolve(rec, det, card, runcal):
        return SimpleNamespace(center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, summary=lambda: {})

    def make_fit(skipped):
        def fake_fit_one(ctx, card, **k):
            assert k.get("structure_hint") == "stokes+bg"       # hint reached the fit
            return {"verdict": "resolved", "confidence": 0.9, "reasons": [], "omega0": 5.0,
                    "omega0_err": 0.1, "interval": (4.9, 5.1), "modality": "fold", "gamma": 0.4,
                    "best_label": "stokes+bg", "cost_per_dof": 1.0, "n_modes": 1, "modes": None,
                    "interval_reliable": True, "omega0_profile": None, "hesse_interval": None,
                    "hesse_trusted": False, "fit_kind": "unconstrained", "two_stage_seconds": 0.1,
                    "errors_attach_seconds": 0.0, "structure_skipped": skipped}
        return fake_fit_one

    import numpy as np
    monkeypatch.setattr(driver.calibrations, "resolve", fake_resolve)
    monkeypatch.setattr(driver, "_assign_and_triage", lambda *a, **k: None)
    card = SimpleNamespace(policy=SimpleNamespace(
        instrument="S30", energy_status="needs_zero", quasielastic_fwhm=None, dispersion={},
        collation={}))
    counts = np.full(40, 30.0)
    ms = SimpleNamespace(energy=np.linspace(-8.0, 16.0, 40), intensity=counts,
                         variance=counts.copy(), monitor=np.ones(40),
                         mask=np.ones(40, dtype=bool), n_members=3,
                         consistency_statistic=None, consistency_pvalue=None,
                         consistency_alpha=0.01, pool_consistent=True,
                         consistency_seed=12345,
                         members=[("tavi", 1, "det"), ("tavi", 2, "det"), ("tavi", 3, "det")])
    grp = _Grp(("tavi", "42", 200.0), ms)
    g = SimpleNamespace(min_fit_points=6, monitor_frac=0.5)

    monkeypatch.setattr(driver, "fit_one", make_fit(True))
    e = driver._fit_master_group(ms, grp, object(), "det", card, None, g, "master:x", 9_000_000,
                                 "ch", M=10, seed=1, structure_hint="stokes+bg")
    assert e.status == "fitted"
    assert e.provenance.get("structure_skipped") is True

    # And NOT stamped when the fit fell through to the full enumeration.
    monkeypatch.setattr(driver, "fit_one", make_fit(False))
    e2 = driver._fit_master_group(ms, grp, object(), "det", card, None, g, "master:x", 9_000_000,
                                  "ch", M=10, seed=1, structure_hint="stokes+bg")
    assert "structure_skipped" not in e2.provenance
