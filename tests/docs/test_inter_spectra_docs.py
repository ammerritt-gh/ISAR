"""The inter-spectra design doc agrees with the code it describes.

Prevents: the settings table drifting from ``InterSettings`` (a renamed or added setting the reader never
sees), a calibration mark that says one thing while the code's ``calibrated`` flag says the other, a setting's
help sentence that the dialog shows but the doc omits, and a moved or deleted module that the code-location
table still names. Prose is not judged here; ``test_doc_drift.py`` covers the document policy.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from isar.analyze.coupled.settings import SETTING_DEFS, InterSettings

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "design" / "inter_spectra.md"

CALIBRATED = "calibrated on synthetic tracks, not on real data"
UNCALIBRATED = "uncalibrated"


def _rows(heading: str) -> list[list[str]]:
    """The table rows under one ``## `` heading, each as its stripped cells (backticks kept)."""
    text = DOC.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert m, f"{DOC.name} has no section {heading!r}"
    return [[c.strip() for c in line.strip().strip("|").split("|")]
            for line in m.group(1).splitlines() if line.startswith("| `")]


def _name(row: list[str]) -> str:
    return row[0].strip("`")


def test_settings_table_names_every_field_with_its_calibration_mark():
    rows = {_name(r): r for r in _rows("5. Settings")}
    fields = {f.name for f in dataclasses.fields(InterSettings)} - {"version"}
    assert set(rows) == fields, (
        f"settings table and InterSettings disagree: doc only {sorted(set(rows) - fields)}, "
        f"code only {sorted(fields - set(rows))}")
    for d in SETTING_DEFS:
        mark = rows[d.name][4]          # cells: name, group, default, bounds, mark, help
        want = CALIBRATED if d.calibrated else UNCALIBRATED
        assert mark == want, f"{d.name}: the doc says {mark!r}, the code's calibrated flag gives {want!r}"


def test_every_setting_help_sentence_is_in_the_table():
    rows = {_name(r): r for r in _rows("5. Settings")}
    for d in SETTING_DEFS:
        assert d.name in rows, f"setting {d.name!r} is missing from the settings table"
        assert rows[d.name][5] == d.help, f"setting {d.name!r}: the table's help is not the code's help sentence"


def test_code_locations_exist():
    paths = [_name(r) for r in _rows("7. Code locations")]
    assert paths, "the code-location table is empty"
    missing = [p for p in paths if not (REPO / p).exists()]
    assert not missing, f"the code-location table names paths that do not exist: {missing}"
