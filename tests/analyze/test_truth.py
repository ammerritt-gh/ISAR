"""σ-calibration scoring against known analytic truth (`isar.analyze.truth`, TODO.md
"σ-calibration"). Sibling of test_gold.py: pins the pure truth-scoring library on synthetic
fixtures — no raw data, no pipeline. The load-bearing properties here are *interval coverage* and
the *inflation factor* (bar optimism), kept separate from *center pull* (a systematic shift).
"""
import numpy as np

from isar.analyze import truth


# --- truth functions ------------------------------------------------------------------------

def test_phonon_dft_anchors():
    # Q=(2.15,0,0) -> reduced q=0.15 -> 6*sin(pi*0.15/2) = 1.4004 meV (TAVI PhononSQW anchor).
    assert abs(truth.phonon_dft(2.15, 0.0, 0.0) - 1.4004) < 1e-3
    # H=3 folds to |q|=1 (zone centers at even integers) -> 6*sin(pi/2) = 6.0 meV.
    assert abs(truth.phonon_dft(3.0, 0.0, 0.0) - 6.0) < 1e-9
    # zone center (even integer) -> 0.
    assert abs(truth.phonon_dft(2.0, 0.0, 0.0)) < 1e-9
    assert "phonon_dft" in truth.TRUTH_FNS


# --- truth CSV matching ---------------------------------------------------------------------

def _write_truth(tmp_path, header, rows, name="truth.csv"):
    p = tmp_path / name
    p.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return p


def test_load_truth_csv_hkl_and_q(tmp_path):
    p = _write_truth(tmp_path, "h,k,l,omega_true",
                     ["2.15,0,0,1.4004", "3,0,0,6.0"])
    rows = truth.load_truth_csv(p)
    assert len(rows) == 2 and rows[0].hkl == (2.15, 0.0, 0.0)
    assert abs(truth.truth_from_csv(rows, (2.15, 0.0, 0.0)) - 1.4004) < 1e-9
    assert abs(truth.truth_from_csv(rows, (3.0, 0.0, 0.0)) - 6.0) < 1e-9
    # no row within tolerance -> None.
    assert truth.truth_from_csv(rows, (2.5, 0.0, 0.0)) is None


def test_load_truth_csv_q_column(tmp_path):
    p = _write_truth(tmp_path, "q,omega_true", ["0.15,1.4004"])
    rows = truth.load_truth_csv(p)
    assert rows[0].q == 0.15 and rows[0].hkl is None
    # (2.15,0,0) folds to |q|=0.15 -> matches the q-row.
    assert abs(truth.truth_from_csv(rows, (2.15, 0.0, 0.0)) - 1.4004) < 1e-9


# --- results.csv loading + disposition reuse ------------------------------------------------

_RESULTS_HEADER = ("scan,detector,sample,T,h,k,l,status,verdict,disposition,confidence,omega0,"
                   "omega0_err,interval_lo,interval_hi,ncount")


def _write_results(tmp_path, rows):
    p = tmp_path / "results.csv"
    p.write_text("\n".join([_RESULTS_HEADER, *rows]) + "\n", encoding="utf-8")
    return p


def test_load_results_maps_disposition_and_skips_held(tmp_path):
    rows = [
        "78,ana7,1,300,3,0,0,ok,resolved,resolved,0.95,6.05,0.02,5.95,6.15,1000",
        "80,ana7,1,300,2.15,0,0,ok,model_incomplete,flagged,0.8,1.45,0.03,1.40,1.50,500",
        "82,ana7,1,300,2.2,0,0,ok,junk,reject,0.2,,,,,100",
        "84,,1,300,,,,held_for_calibration,held_for_calibration,,,,,,",   # held: no hkl -> skipped
    ]
    rr = truth.load_results(_write_results(tmp_path, rows), bucket_by="ncount")
    assert len(rr) == 3                                   # held row (no hkl) dropped
    assert rr[0].disposition == "resolved" and rr[1].disposition == "flagged"
    assert rr[2].disposition == "reject" and rr[2].omega0 is None
    assert rr[0].bucket == "1000"


# --- coverage / inflation on synthetic pulls ------------------------------------------------

def _rows_from_deltas(deltas, halfwidth, *, omega_true=5.0, bucket=None):
    """Delivered ResultRows with omega0 = truth+delta and a fixed-halfwidth interval."""
    out = []
    for i, d in enumerate(deltas):
        o = omega_true + d
        out.append(truth.ResultRow(
            scan=i, detector="ana7", hkl=(3.0, 0.0, 0.0), verdict="resolved",
            disposition="resolved", omega0=o,
            interval_lo=o - halfwidth, interval_hi=o + halfwidth, bucket=bucket))
    return out


def test_too_tight_intervals_undercover_and_inflate():
    # deltas ~ N(0,1) but halfwidth 0.5 -> intervals too tight: coverage < 0.68, inflation > 1.
    rng = np.random.default_rng(0)
    deltas = rng.standard_normal(4000)
    rows = _rows_from_deltas(deltas, halfwidth=0.5)
    rep = truth.score_results(rows, lambda hkl: 5.0)
    assert rep.n_scored == 4000
    assert rep.coverage68 < 0.68
    assert rep.inflation > 1.0
    assert abs(rep.inflation - (0.9945 / 0.5)) < 0.1     # 68th pct of |z| / halfwidth


def test_well_calibrated_intervals_hit_target():
    # deltas ~ N(0,1), halfwidth 1.0 (== 1 sigma) -> coverage ≈ 0.68, inflation ≈ 1.
    rng = np.random.default_rng(1)
    deltas = rng.standard_normal(4000)
    rows = _rows_from_deltas(deltas, halfwidth=1.0)
    rep = truth.score_results(rows, lambda hkl: 5.0)
    assert abs(rep.coverage68 - 0.68) < 0.03
    assert abs(rep.inflation - 1.0) < 0.05
    assert abs(rep.center_pull_mean) < 0.05              # symmetric -> no systematic shift


def test_center_pull_is_separated_from_bar_optimism():
    # a constant +0.3 shift on top of tight bars: nonzero signed center pull AND under-coverage.
    rng = np.random.default_rng(2)
    deltas = 0.3 + rng.standard_normal(2000)
    rep = truth.score_results(_rows_from_deltas(deltas, halfwidth=1.0), lambda hkl: 5.0)
    assert abs(rep.center_pull_mean - 0.3) < 0.08        # systematic, inflation cannot fix
    assert rep.pull_mean is not None and rep.pull_mean > 0


def test_unreliable_interval_rows_excluded_from_coverage_and_inflation():
    """FIX 2: a row whose interval is flagged unreliable (γ-at-bound degenerate bootstrap) must
    not poison coverage or the inflation factor — its absurdly tight halfwidth is ignored — but
    its center pull (Δ) is still scored. A well-calibrated reliable row alongside it survives."""
    good = truth.ResultRow(scan=1, detector="a", hkl=(3, 0, 0), verdict="resolved",
                           disposition="resolved", omega0=6.0, interval_lo=5.0, interval_hi=7.0)
    poison = truth.ResultRow(scan=2, detector="a", hkl=(3, 0, 0), verdict="resolved",
                             disposition="resolved", omega0=6.5,
                             interval_lo=6.4993, interval_hi=6.5007,   # halfwidth ~7e-4
                             interval_reliable=False)
    rep = truth.score_results([good, poison], lambda hkl: 6.0)
    assert rep.n_scored == 2                                   # both scored for center pull
    assert rep.n_zero_halfwidth == 1                           # the poison row: no usable interval
    # coverage over the one reliable row (truth 6.0 ∈ [5,7]) → 1.0, unpolluted by the tight bar.
    assert rep.coverage68 == 1.0
    # inflation from the reliable row only (|Δ|=0/hw → 0), not the 60σ poison pull.
    assert rep.inflation is not None and rep.inflation < 1.0
    # the poison row's Δ (+0.5) still contributes to the signed center pull.
    assert rep.center_pull_mean is not None and rep.center_pull_mean > 0.0


def test_interval_reliable_defaults_true_when_column_absent(tmp_path):
    """Back-compat: a results.csv written before the column existed loads as reliable."""
    p = tmp_path / "results.csv"
    p.write_text("scan,detector,h,k,l,verdict,omega0,interval_lo,interval_hi\n"
                 "1,a,3,0,0,resolved,6.0,5.5,6.5\n", encoding="utf-8")
    rows = truth.load_results(p)
    assert len(rows) == 1 and rows[0].interval_reliable is True


def test_zero_halfwidth_rows_skipped_from_inflation():
    rows = [
        truth.ResultRow(scan=1, detector="a", hkl=(3, 0, 0), verdict="resolved",
                        disposition="resolved", omega0=6.1, interval_lo=6.0, interval_hi=6.2),
        truth.ResultRow(scan=2, detector="a", hkl=(3, 0, 0), verdict="resolved",
                        disposition="resolved", omega0=6.0, interval_lo=6.0, interval_hi=6.0),
    ]
    rep = truth.score_results(rows, lambda hkl: 6.0)
    assert rep.n_scored == 2 and rep.n_zero_halfwidth == 1
    assert rep.inflation is not None                     # computed from the one nonzero-hw row


def test_non_delivered_and_no_truth_rows_counted_not_scored():
    rows = [
        truth.ResultRow(scan=1, detector="a", hkl=(3, 0, 0), verdict="junk",
                        disposition="reject", omega0=None, interval_lo=None, interval_hi=None),
        truth.ResultRow(scan=2, detector="a", hkl=(9, 0, 0), verdict="resolved",
                        disposition="resolved", omega0=6.0, interval_lo=5.9, interval_hi=6.1),
    ]
    rep = truth.score_results(rows, lambda hkl: (5.0 if hkl == (3, 0, 0) else None))
    assert rep.n_scored == 0
    assert rep.n_not_delivered == 1 and rep.n_delivered_no_truth == 1


def test_bucketing_by_column():
    lo = _rows_from_deltas(np.linspace(-0.4, 0.4, 20), halfwidth=1.0, bucket="low")
    hi = _rows_from_deltas(np.linspace(-0.9, 0.9, 20), halfwidth=0.5, bucket="high")
    rep = truth.score_results(lo + hi, lambda hkl: 5.0, bucket_by="ncount")
    labels = {b.label: b for b in rep.buckets}
    assert set(labels) == {"low", "high"}
    assert labels["low"].coverage68 == 1.0               # all within halfwidth 1.0
    assert labels["high"].coverage68 < 1.0               # tight bars miss the tails


## --- bayes interval source (posterior credible-interval coverage) -------------------------

def _row(scan, o, *, boot=None, bayes=None, bayes_ok=None, hkl=(3.0, 0.0, 0.0)):
    """A delivered ResultRow with an optional bootstrap (lo,hi) and/or bayes (lo,hi) interval."""
    blo, bhi = (boot if boot else (None, None))
    ylo, yhi = (bayes if bayes else (None, None))
    return truth.ResultRow(
        scan=scan, detector="a", hkl=hkl, verdict="resolved", disposition="resolved", omega0=o,
        interval_lo=blo, interval_hi=bhi, bayes_lo=ylo, bayes_hi=yhi, bayes_ok=bayes_ok)


def test_interval_source_scores_bootstrap_vs_bayes_independently():
    # Truth 6.0. Bootstrap bar [5.5,6.5] COVERS it; the (tighter) bayes bar [6.4,6.6] does NOT.
    rows = [_row(1, 6.5, boot=(5.5, 6.5), bayes=(6.4, 6.6), bayes_ok=True)]
    boot = truth.score_results(rows, lambda hkl: 6.0)                       # default = bootstrap
    bays = truth.score_results(rows, lambda hkl: 6.0, interval_source="bayes")
    assert boot.n_scored == bays.n_scored == 1
    assert boot.coverage68 == 1.0 and bays.coverage68 == 0.0               # numbers differ by source
    # bootstrap halfwidth 0.5 (|Δ|=0.5 → ratio 1.0); bayes halfwidth 0.1 (|Δ|=0.5 → ratio 5.0).
    assert bays.inflation > boot.inflation


def test_bayes_failed_or_absent_excluded_from_coverage_but_center_pull_kept():
    rows = [
        _row(1, 6.5, bayes=(6.0, 7.0), bayes_ok=True),     # good posterior: truth 6.0 ∈ [6,7]
        _row(2, 6.9, bayes=(6.85, 6.95), bayes_ok=False),  # failed posterior: excluded from cov/infl
        _row(3, 7.0, bayes=(6.95, 7.05), bayes_ok=None),   # absent posterior: excluded from cov/infl
    ]
    rep = truth.score_results(rows, lambda hkl: 6.0, interval_source="bayes")
    assert rep.n_scored == 3                                # all three scored for center pull
    assert rep.n_zero_halfwidth == 2                        # rows 2 & 3 have no usable bayes interval
    assert rep.coverage68 == 1.0                            # coverage over the one good posterior row
    # center pull sees all three Δ (+0.5, +0.9, +1.0) → mean 0.8, not just the good row.
    assert abs(rep.center_pull_mean - 0.8) < 1e-9


def test_bayes_source_ignores_bootstrap_reliability_flag():
    # A row with an UNreliable bootstrap bar but a fine posterior is fully scored under bayes.
    row = truth.ResultRow(scan=1, detector="a", hkl=(3, 0, 0), verdict="resolved",
                          disposition="resolved", omega0=6.0,
                          interval_lo=5.999, interval_hi=6.001, interval_reliable=False,
                          bayes_lo=5.5, bayes_hi=6.5, bayes_ok=True)
    rep = truth.score_results([row], lambda hkl: 6.0, interval_source="bayes")
    assert rep.n_scored == 1 and rep.n_zero_halfwidth == 0
    assert rep.coverage68 == 1.0


def test_default_interval_source_matches_explicit_bootstrap():
    rng = np.random.default_rng(7)
    deltas = rng.standard_normal(500)
    rows = _rows_from_deltas(deltas, halfwidth=0.7)
    a = truth.report_to_dict(truth.score_results(rows, lambda hkl: 5.0))
    b = truth.report_to_dict(truth.score_results(rows, lambda hkl: 5.0, interval_source="bootstrap"))
    assert a == b                                           # omitting the source == "bootstrap"


def test_load_results_parses_bayes_columns(tmp_path):
    p = tmp_path / "results.csv"
    p.write_text(
        "scan,detector,h,k,l,verdict,omega0,interval_lo,interval_hi,bayes_lo,bayes_hi,bayes_ok\n"
        "1,a,3,0,0,resolved,6.0,5.5,6.5,5.8,6.2,True\n"      # bool arrives as the string "True"
        "2,a,3,0,0,resolved,6.1,5.6,6.6,,,False\n",           # failed posterior, no bounds
        encoding="utf-8")
    rows = truth.load_results(p)
    assert rows[0].bayes_lo == 5.8 and rows[0].bayes_hi == 6.2 and rows[0].bayes_ok is True
    assert rows[1].bayes_lo is None and rows[1].bayes_ok is False
    # absent column → None (no posterior written), not a silently-trusted True.
    p2 = tmp_path / "old.csv"
    p2.write_text("scan,detector,h,k,l,verdict,omega0,interval_lo,interval_hi\n"
                  "1,a,3,0,0,resolved,6.0,5.5,6.5\n", encoding="utf-8")
    assert truth.load_results(p2)[0].bayes_ok is None


def test_report_to_dict_is_json_shaped():
    rep = truth.score_results(_rows_from_deltas([0.1, -0.1], halfwidth=1.0),
                              lambda hkl: 5.0, truth_source="fn:phonon_dft")
    d = truth.report_to_dict(rep)
    assert d["truth_source"] == "fn:phonon_dft"
    assert d["n_scored"] == 2 and len(d["rows"]) == 2
    assert set(d["rows"][0]) >= {"hkl", "omega0", "omega_true", "delta", "halfwidth",
                                 "pull", "in_interval"}
