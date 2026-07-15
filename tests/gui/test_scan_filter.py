"""ScanFilter predicate + query-parser tests (pure — no Qt).

The units here are lightweight ``SimpleNamespace`` stand-ins: :meth:`ScanFilter.matches` reads its
fields by ``getattr``, so a real :class:`~isar_gui.viewmodels.scans.ScanUnit` (which would pull in Qt)
is unnecessary and the whole module stays QApplication-free.
"""
from __future__ import annotations

from types import SimpleNamespace

from isar_gui.scan_filter import ScanFilter, describe, parse_query

Q_STEP = 0.001


def _u(*, num=1, det="ana1", status="ingested", T=300.0, q=(1.5, 0.0, 0.0),
       q_target=None, scan_type="measurement", file_name="scan.dat", verdict=None):
    fit = SimpleNamespace(verdict=verdict) if verdict is not None else None
    return SimpleNamespace(scan_number=num, det=det, status=status, T=T, q=list(q) if q else None,
                           q_target=list(q_target) if q_target else None, scan_type=scan_type,
                           file_name=file_name, fit=fit)


def _match(f, u):
    return f.matches(u, q_step=Q_STEP)


# ---------------------------------------------------------------------------
# parse_query
# ---------------------------------------------------------------------------
def test_bare_number_is_ambiguous_free_text():
    f, err = parse_query("372")
    assert err is None
    assert f.text == "372"
    assert not f.analyzers and f.t_min is None  # NOT forced onto scan_number

def test_analyzer_token():
    f, err = parse_query("ana:9")
    assert err is None
    assert f.analyzers == frozenset({"9"})

def test_analyzer_long_form():
    assert parse_query("analyzer:9")[0].analyzers == frozenset({"9"})

def test_temperature_operators():
    assert parse_query("T>100")[0].t_min == 100
    assert parse_query("T<50")[0].t_max == 50
    assert parse_query("T>=4.2")[0].t_min == 4.2
    assert parse_query("T<=300")[0].t_max == 300
    eq = parse_query("T=300")[0]
    assert eq.t_min == 300 and eq.t_max == 300

def test_status_token_lowercased():
    assert parse_query("status:Held")[0].statuses == frozenset({"held"})

def test_type_and_verdict_and_q():
    assert parse_query("type:measurement")[0].scan_types == frozenset({"measurement"})
    assert parse_query("verdict:accept")[0].verdicts == frozenset({"accept"})
    assert parse_query("q:1.5,0,0")[0].q_target == (1.5, 0.0, 0.0)

def test_combined_tokens_and_combine():
    f, err = parse_query("ana:9 T>100 status:held sample")
    assert err is None
    assert f.analyzers == frozenset({"9"})
    assert f.t_min == 100
    assert f.statuses == frozenset({"held"})
    assert f.text == "sample"

def test_unknown_key_is_an_error_but_filter_still_returned():
    f, err = parse_query("foo:bar ana:9")
    assert err is not None and "foo" in err
    assert f.analyzers == frozenset({"9"})   # good token survives

def test_bad_q_vector_errors():
    f, err = parse_query("q:1,x,0")
    assert err is not None
    assert f.q_target is None

def test_empty_query_is_inactive():
    f, err = parse_query("   ")
    assert err is None
    assert not f.is_active()

def test_parse_never_raises_on_junk():
    for junk in [":::", "T>", "T>abc", "ana:", "===", "q:"]:
        f, _err = parse_query(junk)   # must not raise
        assert isinstance(f, ScanFilter)


# ---------------------------------------------------------------------------
# matches — typed clauses AND-combine, empty = wildcard
# ---------------------------------------------------------------------------
def test_empty_filter_matches_everything():
    assert _match(ScanFilter(), _u()) is True

def test_analyzer_filter():
    f = parse_query("ana:9")[0]
    assert _match(f, _u(det="9")) is True
    assert _match(f, _u(det="3")) is False

def test_status_prefix_covers_held_family():
    f = parse_query("status:held")[0]
    assert _match(f, _u(status="held_for_calibration")) is True
    assert _match(f, _u(status="held_for_sample")) is True
    assert _match(f, _u(status="fitted")) is False

def test_status_parsed_alias_matches_ingested():
    f = parse_query("status:parsed")[0]
    assert _match(f, _u(status="ingested")) is True

def test_temperature_range():
    f = parse_query("T>100 T<400")[0]
    assert _match(f, _u(T=300)) is True
    assert _match(f, _u(T=50)) is False
    assert _match(f, _u(T=500)) is False

def test_temperature_equal_with_slack():
    f = parse_query("T=300")[0]
    assert _match(f, _u(T=300.0)) is True

def test_temperature_missing_fails_a_t_filter():
    f = parse_query("T>100")[0]
    assert _match(f, _u(T=None)) is False

def test_verdict_via_fit():
    f = parse_query("verdict:accept")[0]
    assert _match(f, _u(verdict="accept")) is True
    assert _match(f, _u(verdict="reject")) is False
    assert _match(f, _u(verdict=None)) is False   # no fit -> no verdict

def test_q_binning_tolerance():
    f = parse_query("q:1.5,0,0")[0]
    assert _match(f, _u(q=(1.5001, 0.0, 0.0))) is True   # within one q_step bin
    assert _match(f, _u(q=(1.6, 0.0, 0.0))) is False

def test_typed_clauses_and_combine():
    f = parse_query("ana:9 T>100")[0]
    assert _match(f, _u(det="9", T=300)) is True
    assert _match(f, _u(det="9", T=50)) is False
    assert _match(f, _u(det="3", T=300)) is False

def test_non_numeric_t_is_tolerated():
    f = parse_query("T>100")[0]
    assert _match(f, _u(T="warm")) is False   # unparseable T never crashes, just fails the clause


# ---------------------------------------------------------------------------
# matches — ambiguous free text ORs across fields
# ---------------------------------------------------------------------------
def test_free_text_matches_scan_number():
    f = parse_query("372")[0]
    assert _match(f, _u(num=372)) is True
    assert _match(f, _u(num=100)) is False

def test_free_text_matches_temperature():
    f = parse_query("300")[0]
    assert _match(f, _u(num=1, T=300.0)) is True

def test_free_text_matches_file_name():
    f = parse_query("cef")[0]
    assert _match(f, _u(file_name="CeF3_cold.dat")) is True

def test_free_text_multiple_tokens_and_combine():
    f = parse_query("cold 300")[0]
    assert _match(f, _u(T=300.0, file_name="cold.dat")) is True
    assert _match(f, _u(T=300.0, file_name="warm.dat")) is False

def test_free_text_and_typed_combine():
    f = parse_query("ana:9 cold")[0]
    assert _match(f, _u(det="9", file_name="cold.dat")) is True
    assert _match(f, _u(det="3", file_name="cold.dat")) is False


# ---------------------------------------------------------------------------
# persistence + describe
# ---------------------------------------------------------------------------
def test_to_from_dict_round_trip():
    f = parse_query("ana:9 T>100 status:held q:1.5,0,0 cold")[0]
    d = f.to_dict()
    back = ScanFilter.from_dict(d)
    assert back == f

def test_from_dict_tolerates_garbage():
    assert ScanFilter.from_dict(None) == ScanFilter()
    assert ScanFilter.from_dict({"t_min": "nope"}).t_min is None

def test_to_dict_only_emits_active_fields():
    assert ScanFilter().to_dict() == {}
    assert set(parse_query("ana:9")[0].to_dict()) == {"analyzers"}

def test_describe_mentions_active_clauses():
    s = describe(parse_query("ana:9 T>100 status:held")[0])
    assert "analyzer 9" in s and "T ≥ 100" in s and "held" in s

def test_describe_empty_is_blank():
    assert describe(ScanFilter()) == ""
