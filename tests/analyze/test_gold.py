"""Step-6 gold-standard scoring (§10, §14): score Phase-3 verdicts against the curated FeSe_1
human fits. These unit tests pin the pure scoring library `isar.analyze.gold` on fixtures — no raw
data, no pipeline. The real-data gate lives in docs/phase3_gold_check.py.

The load-bearing property is the *safety* one: a scan the human REJECTed must never come back as an
ISAR `resolved` (a confident accept). The recovery property is `which ω`: on the agreed-accept set,
ISAR's ω must be statistically consistent with the gold ω.
"""
import types

from isar.analyze import gold


# --- load_gold ------------------------------------------------------------------------------

_GOLD_HEADER = ("Label,Scan,Sample,T(K),K(rlu),Slit(mm),phonon_energy,center,gauss_fwhm,"
                "lorentz_fwhm,damping,phonon_amplitude,elastic_amplitude,BG,phonon_energy_err,"
                "center_err,gauss_fwhm_err,lorentz_fwhm_err,damping_err,phonon_amplitude_err,"
                "elastic_amplitude_err,BG_err,Chi2,Quality")


def _write_gold(tmp_path, rows):
    p = tmp_path / "gold.csv"
    p.write_text("\n".join([_GOLD_HEADER, *rows]) + "\n", encoding="utf-8")
    return p


def test_load_gold_parses_fields_and_quality(tmp_path):
    rows = [
        # scan 84, sample 1, T=300, K=0.25, omega0=7.47, center=-0.534, damping=0.0186, OK
        "T300K_K0.25_s1,84,1,300,0.2500,18,7.470200,-0.534127,0.958,0.590,0.018569,1.13,1.29,"
        "0.0,0.023330,0.032,0.0,0.0,0.040,1.45,1.97,0.43,2.4137,OK",
        # scan 82, sample 1, REJECT (railed)
        "T300K_K0.2_s1,82,1,300,0.2000,18,14.000000,-0.319751,0.958,0.590,2.000000,216.8,9.2,"
        "0.0,9.245386,0.242,0.0,0.0,16.5,341.0,3.4,2.0,18.4919,REJECT",
    ]
    g = gold.load_gold(_write_gold(tmp_path, rows))
    assert len(g) == 2
    r0 = g[0]
    assert r0.scan == 84 and r0.sample == 1
    assert r0.K == 0.25 and r0.T == 300.0
    assert abs(r0.omega0 - 7.470200) < 1e-9          # phonon_energy -> omega0
    assert abs(r0.gamma - 0.018569) < 1e-9           # damping -> gamma
    assert abs(r0.center - (-0.534127)) < 1e-9
    assert abs(r0.omega0_err - 0.023330) < 1e-9
    assert abs(r0.chi2 - 2.4137) < 1e-6
    assert r0.quality == "OK"
    assert g[1].scan == 82 and g[1].quality == "REJECT"


# --- select_analyzer ------------------------------------------------------------------------

def _rec(ks):
    """A duck-typed record: spectra[id].q = [H, K, L]; select_analyzer reads only K (q[1])."""
    spectra = {name: types.SimpleNamespace(q=[4.0, k, 0.0]) for name, k in ks.items()}
    return types.SimpleNamespace(spectra=spectra)


def _gold_row(K=0.05, scan=76, quality="OK", omega0=2.0, omega0_err=0.05, analyzer=None):
    return gold.GoldRow(scan=scan, sample=1, T=300.0, K=K, omega0=omega0, omega0_err=omega0_err,
                        gamma=0.1, gamma_err=0.02, center=0.0, chi2=1.0, quality=quality,
                        slit=18.0, analyzer=analyzer, label=f"row{scan}")


def test_select_analyzer_matches_by_reported_k():
    rec = _rec({"ana5": 0.0450, "ana6": 0.0475, "ana7": 0.0500, "ana8": 0.0526})
    ch = gold.select_analyzer(rec, _gold_row(K=0.05))
    assert ch.detector_id == "ana7"          # nominal K=0.05 -> the at-nominal analyzer
    assert ch.source == "matched"
    assert ch.k_residual < 1e-6 and ch.flagged is False


def test_select_analyzer_override_wins():
    rec = _rec({"ana5": 0.0450, "ana7": 0.0500})
    ch = gold.select_analyzer(rec, _gold_row(K=0.05), override="ana5")
    assert ch.detector_id == "ana5" and ch.source == "override"
    # override carries the (large) residual honestly, so a wrong override is visible
    assert ch.k_residual > 0.0


def test_select_analyzer_flags_far_match():
    rec = _rec({"ana5": 0.0450, "ana7": 0.0500, "ana9": 0.0553})
    ch = gold.select_analyzer(rec, _gold_row(K=0.50))    # no analyzer anywhere near 0.5
    assert ch.flagged is True


# --- verdict_disposition --------------------------------------------------------------------

def test_verdict_disposition_mapping():
    # "score the number, flag the fit": resolved = clean number; model_incomplete = delivered but
    # flagged (real peak, imperfect model); junk/unconstrained = no usable number -> reject.
    assert gold.verdict_disposition("resolved") == "resolved"
    assert gold.verdict_disposition("model_incomplete") == "flagged"
    for v in ("junk", "unconstrained"):
        assert gold.verdict_disposition(v) == "reject"
    for v in ("held_for_calibration", "underpowered", "no_signal"):
        assert gold.verdict_disposition(v) == "abstain"
    assert gold.delivered("resolved") and gold.delivered("flagged")
    assert not gold.delivered("reject") and not gold.delivered("abstain")


# --- score_row ------------------------------------------------------------------------------

def _isar(verdict="resolved", confidence=0.85, omega0=7.5, interval=(7.3, 7.5, 7.7),
          omega0_err=None, modality="fold"):
    return gold.IsarResult(verdict=verdict, confidence=confidence, omega0=omega0,
                           omega0_err=omega0_err, interval=interval, modality=modality)


def test_score_row_resolved_ok_agrees():
    s = gold.score_row(_gold_row(quality="OK", omega0=7.4, omega0_err=0.3),
                       _isar(verdict="resolved", omega0=7.5, interval=(7.3, 7.5, 7.7)))
    assert s.gold_disposition == "accept" and s.isar_disposition == "resolved"
    assert s.agree is True
    assert s.omega0_in_interval is True          # gold 7.4 in [7.3, 7.7]
    assert s.omega0_pull is not None


def test_score_row_model_incomplete_ok_is_flagged_agreement():
    # "score the number, flag the fit": model_incomplete delivers a usable omega -> agreement with OK.
    s = gold.score_row(_gold_row(quality="OK", omega0=7.4, omega0_err=0.3),
                       _isar(verdict="model_incomplete", omega0=7.45, interval=(7.3, 7.45, 7.6)))
    assert s.isar_disposition == "flagged"
    assert s.agree is True                        # delivered a usable number where the human did
    assert s.safety is False


def test_score_row_resolved_reject_is_safety_violation():
    s = gold.score_row(_gold_row(quality="REJECT", omega0=14.0, omega0_err=9.0),
                       _isar(verdict="resolved", omega0=7.5))
    assert s.gold_disposition == "reject" and s.isar_disposition == "resolved"
    assert s.agree is False                       # a clean resolve on a human-REJECT is the worst case
    assert s.safety is True                       # the hard safety violation


def test_score_row_model_incomplete_reject_is_soft_not_safety():
    s = gold.score_row(_gold_row(quality="REJECT", omega0=14.0, omega0_err=9.0),
                       _isar(verdict="model_incomplete", omega0=5.4))
    assert s.isar_disposition == "flagged"
    assert s.agree is False                       # delivered where the human rejected (disagreement)
    assert s.safety is False                      # but flagged, not a clean resolve -> not the hard violation


def test_score_row_junk_reject_agrees():
    s = gold.score_row(_gold_row(quality="REJECT"),
                       _isar(verdict="junk", omega0=None, interval=None))
    assert s.isar_disposition == "reject" and s.agree is True


def test_score_row_abstain_excluded_from_agreement():
    s = gold.score_row(_gold_row(quality="OK"),
                       _isar(verdict="held_for_calibration", omega0=None, interval=None))
    assert s.isar_disposition == "abstain"
    assert s.agree is None                        # neither agree nor disagree


def test_score_row_pull_arithmetic():
    # sigma_isar = (7.7-7.3)/2 = 0.2; sigma_gold = 0.3; pull = (7.5-7.0)/sqrt(0.04+0.09)
    s = gold.score_row(_gold_row(quality="OK", omega0=7.0, omega0_err=0.3),
                       _isar(omega0=7.5, interval=(7.3, 7.5, 7.7)))
    assert abs(s.omega0_abs_resid - 0.5) < 1e-9
    assert abs(s.omega0_pull - (0.5 / (0.13 ** 0.5))) < 1e-6


def test_score_row_interval_coverage_flag():
    inside = gold.score_row(_gold_row(omega0=7.4), _isar(interval=(7.3, 7.5, 7.7)))
    outside = gold.score_row(_gold_row(omega0=7.0), _isar(interval=(7.3, 7.5, 7.7)))
    assert inside.omega0_in_interval is True
    assert outside.omega0_in_interval is False


# --- confusion + aggregate ------------------------------------------------------------------

def _score(quality, verdict, *, omega0=7.5, gold_o=7.4, gerr=0.3, interval=(7.3, 7.5, 7.7),
           conf=0.85, scan=1):
    return gold.score_row(_gold_row(scan=scan, quality=quality, omega0=gold_o, omega0_err=gerr),
                          _isar(verdict=verdict, confidence=conf, omega0=omega0, interval=interval))


def test_confusion_counts():
    scores = [
        _score("OK", "resolved"),                 # accept x resolved
        _score("OK", "model_incomplete"),         # accept x flagged
        _score("OK", "held_for_calibration"),     # accept x abstain
        _score("REJECT", "junk"),                 # reject x reject
        _score("REJECT", "resolved"),             # reject x resolved (safety violation)
    ]
    c = gold.confusion(scores)
    assert c.count("accept", "resolved") == 1
    assert c.count("accept", "flagged") == 1
    assert c.count("accept", "abstain") == 1
    assert c.count("reject", "reject") == 1
    assert c.count("reject", "resolved") == 1


def test_aggregate_metrics():
    scores = [
        _score("OK", "resolved", omega0=7.45, gold_o=7.4, scan=1, conf=0.9),         # delivered, in-interval
        _score("OK", "model_incomplete", omega0=7.50, gold_o=7.4, scan=2, conf=0.8),  # delivered (flagged)
        _score("REJECT", "resolved", omega0=7.5, gold_o=14.0, gerr=9.0, scan=3, conf=0.7),  # safety
        _score("REJECT", "junk", scan=4, conf=0.4),                                   # agree (reject)
    ]
    rep = gold.aggregate(scores)
    assert rep.n == 4
    assert rep.safety_violations == [3]           # gold-REJECT that ISAR cleanly resolved
    assert rep.n_delivered == 3                   # scans 1,2 (on OK) + 3 (on REJECT)
    # recovery set = delivered & gold-OK (scans 1,2), both omegas inside their intervals
    assert rep.coverage68 == 1.0
    assert rep.conf_agree > rep.conf_disagree     # confident on delivered-correct, less on delivered-wrong
    assert rep.median_abs_resid is not None
