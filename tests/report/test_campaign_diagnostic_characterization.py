"""Characterization test: pin the renderer against a frozen copy of the MVP-16 campaign workspace.

Same inputs → same report (modulo timestamps / epochs / absolute paths, normalized away). The golden
is committed at ``tests/data/golden/CAMPAIGN_DIAGNOSTIC.mvp16.md``; regenerate it deliberately (see the
module-level ``_write_golden`` recipe) when the renderer's output legitimately changes.

Skips cleanly when the frozen baseline is not present on this machine.
"""
from __future__ import annotations

import difflib
import re
import shutil
from pathlib import Path

import pytest

from isar.report.campaign_diagnostic import render_campaign_diagnostic

_BASELINE = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")
_GOLDEN = Path(__file__).resolve().parents[1] / "data" / "golden" / "CAMPAIGN_DIAGNOSTIC.mvp16.md"

_EXPECTED_HEADINGS = [
    "## 1. Objective",
    "## 2. Outcome at a glance",
    "## 3. What the driver did",
    "## 4. Delivered dispersion vs truth",
    "## 5. Failure dossier",
    "## 6. Defects uncovered",
    "## 7. Verdict & recommended actions",
    "## 8. Provenance",
]


def normalize(text: str) -> str:
    """Erase run-to-run noise so the golden compares on content, not on timestamps/paths."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # ISO-8601 timestamps
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?",
                  "<TS>", text)
    # epoch seconds (this decade)
    text = re.sub(r"\b17\d{8}(\.\d+)?\b", "<EPOCH>", text)
    # absolute paths (Windows or POSIX)
    text = re.sub(r"[A-Za-z]:\\[^\s|)]+", "<PATH>", text)
    text = re.sub(r"(?<![\w])/(?:[\w.-]+/)+[\w.-]+", "<PATH>", text)
    return text


@pytest.fixture(scope="session")
def rendered(tmp_path_factory):
    if not _BASELINE.exists():
        pytest.skip(f"frozen baseline not present: {_BASELINE}")
    ws = tmp_path_factory.mktemp("mvp16") / "ws"
    shutil.copytree(_BASELINE, ws)
    out = render_campaign_diagnostic(ws)
    return ws, out, out.read_text(encoding="utf-8")


def test_section_headings_in_order(rendered):
    _ws, _out, text = rendered
    headings = [ln.rstrip() for ln in text.splitlines() if ln.startswith("## ")]
    assert headings == _EXPECTED_HEADINGS


def test_key_content_present(rendered):
    _ws, _out, text = rendered
    assert "10 / 21" in text                       # converged count (acceptance criterion 1)
    assert "unmeetable" in text
    assert "verify-units" in text                  # budget ledger caveat (acceptance criterion 5)
    # center accuracy vs truth — score.json is present in this baseline (| is table-escaped to \|)
    assert "Δω₀" in text and "coverage68" in text


def test_always_on_figures_and_sidecars_exist(rendered):
    ws, _out, _text = rendered
    figs = ws / "report" / "figures"
    for name in ("campaign_dispersion_truth", "campaign_scan_spend", "campaign_budget_timing"):
        assert (figs / f"{name}.png").exists(), name
        assert (figs / f"{name}.caption.md").exists(), name


def test_buried_scan_figures_rendered(rendered):
    ws, _out, _text = rendered
    scans = list((ws / "report" / "figures" / "scans").glob("*.png"))
    assert scans, "expected a non-empty buried per-scan figure set"


def test_matches_golden(rendered):
    _ws, _out, text = rendered
    if not _GOLDEN.exists():
        pytest.skip(f"golden not committed yet: {_GOLDEN}")
    got = normalize(text)
    want = normalize(_GOLDEN.read_text(encoding="utf-8"))
    if got != want:
        diff = "\n".join(difflib.unified_diff(want.splitlines(), got.splitlines(),
                                              fromfile="golden", tofile="rendered", lineterm=""))
        raise AssertionError("CAMPAIGN_DIAGNOSTIC drifted from golden:\n" + diff)
