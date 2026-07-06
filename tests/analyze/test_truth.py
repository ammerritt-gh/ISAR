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


def test_report_to_dict_is_json_shaped():
    rep = truth.score_results(_rows_from_deltas([0.1, -0.1], halfwidth=1.0),
                              lambda hkl: 5.0, truth_source="fn:phonon_dft")
    d = truth.report_to_dict(rep)
    assert d["truth_source"] == "fn:phonon_dft"
    assert d["n_scored"] == 2 and len(d["rows"]) == 2
    assert set(d["rows"][0]) >= {"hkl", "omega0", "omega_true", "delta", "halfwidth",
                                 "pull", "in_interval"}
