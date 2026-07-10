"""The caption registry de-floats every figure: each figure id maps to a Caption with a title, a
plain-language 'what you're looking at', and a 'what good looks like' line aimed at a scientist who
knows the physics but not ISAR. This test pins the API and the completeness contract — every figure
the three reports produce must have a caption, so a figure can never ship uncaptioned."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from isar.report import captions  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\n"


def _trivial_fig():
    fig, ax = plt.subplots(figsize=(4, 3))
    ax.plot([0, 1, 2], [0, 1, 0])
    return fig

# The figures the documentation pass captions (report library + the three check-script reports).
# A new figure id added to a report must be added here and given a Caption — that is the contract.
_EXPECTED_IDS = (
    # shared reduction-report library (isar/report/figures.py)
    "report/coverage", "report/spectrum", "report/miniscan", "report/merged",
    # gold scoring (docs/phase3_gold_check.py)
    "gold/confusion", "gold/pull_hist", "gold/scatter", "gold/confidence",
    "gold/audit", "gold/resolution_overlay",
    # phase-3 step-1 control (docs/phase3_step1_check.py)
    "step1/spectrum", "step1/dispersion", "step1/bootstrap_hist", "step1/selection_weights",
    "step1/synthetic_fork", "step1/verdict_confidence", "step1/energy_zero",
    # phase-1 anchor control (docs/in8_phase1_check.py)
    "in8/overlay",
    # campaign diagnostic (isar/report/campaign_diagnostic.py)
    "campaign/dispersion_truth", "campaign/scan_spend", "campaign/budget_timing", "campaign/scan",
)


def test_caption_for_known_id_returns_a_caption():
    cap = captions.caption_for("gold/confusion")
    assert isinstance(cap, captions.Caption)
    assert cap.title and cap.what and cap.good


def test_caption_for_unknown_id_returns_none():
    assert captions.caption_for("nope/not-a-figure") is None


def test_every_expected_figure_has_a_caption():
    missing = [fid for fid in _EXPECTED_IDS if captions.caption_for(fid) is None]
    assert not missing, f"figures with no caption: {missing}"


def test_no_caption_field_is_empty():
    bad = [fid for fid, cap in captions.CAPTIONS.items()
           if not (cap.title.strip() and cap.what.strip() and cap.good.strip())]
    assert not bad, f"captions with an empty field: {bad}"


def test_jargon_entries_are_term_gloss_pairs():
    for fid, cap in captions.CAPTIONS.items():
        for pair in cap.jargon:
            assert len(pair) == 2, f"{fid}: jargon entry is not a (term, gloss) pair: {pair!r}"
            term, gloss = pair
            assert term.strip() and gloss.strip(), f"{fid}: empty jargon term/gloss"


def test_as_markdown_and_as_text_contain_the_title():
    cap = captions.caption_for("gold/audit")
    assert cap.title in cap.as_markdown()
    assert cap.title in cap.as_text()
    # the markdown block also surfaces the 'what good looks like' guidance
    assert cap.good in cap.as_markdown()


# ---- captioned save / emit helpers (Slice 2) --------------------------------------------------

def test_save_captioned_writes_png_and_sidecar(tmp_path):
    p = tmp_path / "figures" / "confusion.png"
    p.parent.mkdir(parents=True)
    out = captions.save_captioned(_trivial_fig(), p, "gold/confusion")
    assert out == p and p.exists()
    assert p.read_bytes().startswith(_PNG) and p.stat().st_size > 1000
    sidecar = p.parent / "confusion.caption.md"
    assert sidecar.exists()
    assert captions.caption_for("gold/confusion").good in sidecar.read_text(encoding="utf-8")


def test_save_captioned_draw_false_still_writes_sidecar(tmp_path):
    p = tmp_path / "scatter.png"
    captions.save_captioned(_trivial_fig(), p, "gold/scatter", draw=False)
    assert p.exists() and p.read_bytes().startswith(_PNG)
    assert (p.parent / "scatter.caption.md").exists()


def test_save_captioned_unknown_id_saves_png_but_no_sidecar(tmp_path):
    p = tmp_path / "mystery.png"
    captions.save_captioned(_trivial_fig(), p, "no/such-figure")
    assert p.exists() and p.read_bytes().startswith(_PNG)
    assert not (p.parent / "mystery.caption.md").exists()


def test_captioned_png_bytes_returns_valid_png():
    png = captions.captioned_png_bytes(_trivial_fig(), "gold/confusion")
    assert png.startswith(_PNG) and len(png) > 1000


def test_write_sidecar_contains_title_and_good(tmp_path):
    cap = captions.caption_for("gold/audit")
    sc = captions.write_sidecar(tmp_path / "x.png", cap)
    assert sc.exists()
    txt = sc.read_text(encoding="utf-8")
    assert cap.title in txt and cap.good in txt
