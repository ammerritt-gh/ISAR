"""MIME payload contract tests (Phase E) — Qt-free, so plain (no qtbot / importorskip).

Round-trips must survive; malformed input must degrade to the empty answer ([] / None) and never
raise, because a foreign drag or a truncated payload reaches these decoders at runtime.
"""
from __future__ import annotations

from isar_gui.mime import (
    MIME_GROUP_REF,
    MIME_SCAN_IDS,
    decode_group_ref,
    decode_scan_ids,
    encode_group_ref,
    encode_scan_ids,
)


def test_mime_type_strings_are_distinct():
    assert MIME_SCAN_IDS != MIME_GROUP_REF
    assert MIME_SCAN_IDS.startswith("application/x-isar")


def test_scan_ids_round_trip():
    keys = ["tavi:469", "tavi:470", "in8:12"]
    assert decode_scan_ids(encode_scan_ids(keys)) == keys


def test_scan_ids_empty_round_trip():
    assert decode_scan_ids(encode_scan_ids([])) == []


def test_scan_ids_garbage_returns_empty_without_raising():
    assert decode_scan_ids(b"not json at all") == []
    assert decode_scan_ids(b"") == []
    assert decode_scan_ids(b"\xff\xfe\x00garbage") == []
    # Valid JSON, wrong shape.
    assert decode_scan_ids(b'{"a": 1}') == []
    assert decode_scan_ids(b"42") == []


def test_group_ref_round_trip():
    payload = encode_group_ref("manual", "abc123", "Cold sweep")
    got = decode_group_ref(payload)
    assert got == {"kind": "manual", "group_id": "abc123", "label": "Cold sweep"}


def test_group_ref_auto_kind():
    got = decode_group_ref(encode_group_ref("auto", "auto:T:300", "T = 300 K"))
    assert got["kind"] == "auto"
    assert got["group_id"] == "auto:T:300"


def test_group_ref_garbage_returns_none_without_raising():
    assert decode_group_ref(b"not json") is None
    assert decode_group_ref(b"") is None
    assert decode_group_ref(b"[1, 2, 3]") is None      # list, not a ref dict
    assert decode_group_ref(b'{"kind": "manual"}') is None   # missing group_id
