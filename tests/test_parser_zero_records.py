"""Recognized, header-only files must be persistent parse failures, not successful zero scans."""
from __future__ import annotations

import pytest

from isar.parsers import ill_in8, spec_s30_raw


def test_in8_header_only_file_reports_zero_of_zero_blocks(monkeypatch):
    monkeypatch.setattr(ill_in8.numor_base, "read_numor", lambda _path: [])
    with pytest.raises(ValueError, match=r"parsed 0 of 0 numor blocks"):
        ill_in8.parse_file("header-only.numor", ill_in8.card_template())


def test_s30_header_only_file_reports_zero_of_zero_blocks(monkeypatch):
    monkeypatch.setattr(spec_s30_raw.spec_base, "read_spec", lambda _path, **_kw: ({}, []))
    with pytest.raises(ValueError, match=r"parsed 0 of 0 SPEC blocks"):
        spec_s30_raw.parse_file("header-only.spec", spec_s30_raw.card_template())
