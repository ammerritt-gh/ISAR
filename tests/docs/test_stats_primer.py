"""Regression tests for docs/STATS_PRIMER.md and its generated figure gallery.

The primer's whole trust model is "every number here is generated, not hand-typed" (see its
opening section). These tests check the mechanical half of that claim: every cited figure exists,
every cited figure has a caption (sidecar file + registry entry), the committed coverage JSON has
the metadata the primer's text assumes, and the coverage numbers quoted in prose actually match
the committed JSON. They do not re-derive the physics — that is the job of the generation script
and its own assertions (see docs/stats/make_figures.py).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from isar.report import captions

DOCS_DIR = __import__("pathlib").Path(__file__).resolve().parents[2] / "docs"
PRIMER_PATH = DOCS_DIR / "STATS_PRIMER.md"
COVERAGE_JSON_PATH = DOCS_DIR / "stats" / "figures" / "coverage_summary.json"
MAKE_FIGURES_PATH = DOCS_DIR / "stats" / "make_figures.py"

_FIG_REF_RE = re.compile(r"stats/figures/[A-Za-z0-9_.]+")
_IMAGE_EXTS = (".png", ".svg", ".jpg", ".jpeg")


def _primer_text() -> str:
    return PRIMER_PATH.read_text(encoding="utf-8")


def _figure_refs() -> list[str]:
    """All `stats/figures/<name>` paths cited via markdown image syntax in the primer."""
    return _FIG_REF_RE.findall(_primer_text())


# ---- 1. every cited figure path resolves ----------------------------------------------------

def test_every_cited_figure_path_exists():
    refs = _figure_refs()
    assert refs, "expected at least one stats/figures/* reference in the primer"
    missing = [r for r in refs if not (DOCS_DIR / r).exists()]
    assert not missing, f"primer cites figures that don't exist under docs/: {missing}"


# ---- 2. every cited image figure has a caption sidecar + registry entry ---------------------

def _cited_image_refs() -> list[str]:
    return sorted(set(r for r in _figure_refs() if r.lower().endswith(_IMAGE_EXTS)))


def test_every_cited_image_figure_has_a_caption_sidecar():
    for ref in _cited_image_refs():
        path = DOCS_DIR / ref
        sidecar = path.parent / (path.stem + ".caption.md")
        assert sidecar.exists(), f"{ref} has no .caption.md sidecar at {sidecar}"


def test_every_cited_image_figure_has_a_registry_caption():
    for ref in _cited_image_refs():
        stem = __import__("pathlib").Path(ref).stem
        # Usually the fig_id mirrors the filename stem (e.g. coverage_curve.png -> stats/coverage_curve),
        # but docs/stats/make_figures.py's figure_wall() saves the file as "wall_figure.png" while
        # registering it under the caption id "stats/wall" (see its _save_png(..., "wall_figure",
        # "stats/wall", ...) call) — so fall back to the "_figure"-suffix-stripped id too.
        candidates = [f"stats/{stem}"]
        if stem.endswith("_figure"):
            candidates.append(f"stats/{stem[: -len('_figure')]}")
        cap = next((captions.caption_for(c) for c in candidates if captions.caption_for(c)), None)
        assert cap is not None, (
            f"no isar.report.captions registry entry for any of {candidates!r}"
        )
        assert isinstance(cap, captions.Caption)
        assert cap.title and cap.what and cap.good


# ---- 3. coverage_summary.json meta ----------------------------------------------------------

def _load_coverage_summary() -> dict:
    assert COVERAGE_JSON_PATH.exists(), f"missing {COVERAGE_JSON_PATH}"
    return json.loads(COVERAGE_JSON_PATH.read_text(encoding="utf-8"))


def test_coverage_summary_json_parses_and_has_expected_meta():
    summary = _load_coverage_summary()
    meta = summary["meta"]
    assert meta["seed"] == 20260710
    assert meta["fast"] is False
    assert meta["N_per_level"] == 200


# ---- 4. quoted coverage numbers in primer section 3.1 match the JSON ------------------------

def test_omega0_coverage_numbers_in_primer_match_json():
    summary = _load_coverage_summary()
    primer = _primer_text()
    cells = summary["cells"]
    checked = 0
    for level, cell in cells.items():
        for method, stats in cell["omega0"].items():
            cov = stats["coverage"]
            assert cov is not None
            formatted = f"{cov:.2f}"
            assert formatted in primer, (
                f"omega0 coverage {formatted} (level={level}, method={method}) "
                f"not found quoted in STATS_PRIMER.md"
            )
            checked += 1
    assert checked > 0


def test_gamma_skewed_incl_bound_coverage_numbers_in_primer_match_json():
    summary = _load_coverage_summary()
    primer = _primer_text()
    skewed = summary["gamma_skewed"]
    incl = skewed["gamma_incl_bound"]

    hessian_formatted = f"{incl['hessian']['coverage']:.2f}"
    bootstrap_formatted = f"{incl['bootstrap']['coverage']:.2f}"

    assert hessian_formatted in primer, (
        f"gamma_skewed gamma_incl_bound hessian coverage {hessian_formatted} "
        "not found quoted in STATS_PRIMER.md"
    )
    assert bootstrap_formatted in primer, (
        f"gamma_skewed gamma_incl_bound bootstrap coverage {bootstrap_formatted} "
        "not found quoted in STATS_PRIMER.md"
    )


def test_gamma_skewed_width_ratio_median_in_primer_matches_json():
    summary = _load_coverage_summary()
    primer = _primer_text()
    median = summary["gamma_skewed"]["diagnostics"]["width_ratio"]["median"]
    formatted = f"{median:.2f}"
    assert formatted in primer, (
        f"gamma_skewed diagnostics.width_ratio.median {formatted} "
        "not found quoted in STATS_PRIMER.md"
    )


# ---- 5. three_questions smoke test -----------------------------------------------------------

# make_figures.py hardcodes FIGDIR = Path(__file__).resolve().parent / "figures" (no env var or
# CLI override), and this test suite is test-only (no changes to make_figures.py allowed), so we
# cannot point a fresh regeneration at a scratch directory. Instead this is a regression check that
# the already-committed artifacts are present — a full regeneration/determinism check lives in the
# slow test below, which runs against the real hardcoded FIGDIR and restores it afterward.
def test_three_questions_committed_artifacts_exist():
    svg_path = DOCS_DIR / "stats" / "figures" / "three_questions.svg"
    caption_path = DOCS_DIR / "stats" / "figures" / "three_questions.caption.md"
    assert svg_path.exists()
    assert caption_path.exists()


def test_three_questions_metadata_and_compatibility_alias():
    caption_path = DOCS_DIR / "stats" / "figures" / "three_questions.caption.md"
    json_path = DOCS_DIR / "stats" / "figures" / "three_questions.json"
    caption = caption_path.read_text(encoding="utf-8")
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert caption.count("> **Lifecycle:** Current") == 1
    assert caption.count(
        "> **Provenance:** generated by `python docs/stats/make_figures.py --only three_questions`"
    ) == 1
    assert "preceding bootstrap rung" in caption
    assert payload["planned_rung"] == payload["noncertifying_rung"] == "Bayesian credible"


# ---- 6. determinism (slow, mutates and restores the real committed file) --------------------

@pytest.mark.slow
def test_three_questions_regeneration_is_deterministic():
    """Run `make_figures.py --only three_questions` twice and confirm deterministic output.

    FIGDIR is hardcoded next to the script (no override), so this necessarily writes to the real
    committed docs/stats/figures/three_questions.{svg,json}. Per the primer's own documented
    determinism contract ("Determinism is pinned to the numeric JSON dumps ... PNG bytes may
    differ across matplotlib versions; the numbers may not" — see the "Reproducing everything"
    section of STATS_PRIMER.md), this compares the numeric JSON dump rather than the SVG bytes:
    matplotlib's SVG backend embeds non-deterministic internal ids (e.g. clip-path ids derived
    from object identity) even when the plotted numbers are bit-identical, so an SVG-byte
    comparison would be a false positive for non-determinism. The original files are read first
    and restored in a finally block regardless of outcome, so the working tree is left
    byte-identical.
    """
    svg_path = DOCS_DIR / "stats" / "figures" / "three_questions.svg"
    json_path = DOCS_DIR / "stats" / "figures" / "three_questions.json"
    original_svg = svg_path.read_bytes() if svg_path.exists() else None
    original_json = json_path.read_bytes() if json_path.exists() else None
    try:
        cmd = [sys.executable, str(MAKE_FIGURES_PATH), "--only", "three_questions"]
        subprocess.run(cmd, check=True, cwd=str(DOCS_DIR.parent), capture_output=True)
        first = json.loads(json_path.read_text(encoding="utf-8"))
        subprocess.run(cmd, check=True, cwd=str(DOCS_DIR.parent), capture_output=True)
        second = json.loads(json_path.read_text(encoding="utf-8"))
        assert first == second, "three_questions.json is not deterministic across two runs"
    finally:
        if original_svg is not None:
            svg_path.write_bytes(original_svg)
        if original_json is not None:
            json_path.write_bytes(original_json)
