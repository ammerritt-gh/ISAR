"""FindingsDoc is the shared skeleton every generated report uses, so the three reports read the same
way: plain-language sections, figures embedded WITH their caption, caveats made visually prominent
(not buried), and a PASS/FAIL gate. These tests pin the building blocks and that a figure block pulls
its caption from the registry (a figure can't be embedded uncaptioned)."""
from isar.report import captions, findings


def test_title_section_para_render_in_order():
    d = findings.FindingsDoc("My report")
    d.section("What this checks")
    d.para("Plain words a physicist understands.")
    d.section("Method")
    out = d.render()
    assert out.startswith("# My report")
    assert "## What this checks" in out
    assert "Plain words a physicist understands." in out
    assert out.index("## What this checks") < out.index("## Method")


def test_bullet_and_table():
    d = findings.FindingsDoc("t")
    d.bullet("first point")
    d.table(["a", "b"], [(1, 2), (3, 4)])
    out = d.render()
    assert "- first point" in out
    assert "| a | b |" in out and "| 1 | 2 |" in out


def test_caveat_is_visually_distinct_and_keeps_the_text():
    d = findings.FindingsDoc("t")
    d.caveat("These error bars are statistical-only — don't weight by them.")
    out = d.render()
    assert "These error bars are statistical-only" in out
    assert "Caveat" in out and out.count(">") >= 1  # rendered as a blockquote


def test_figure_block_embeds_image_and_its_caption():
    d = findings.FindingsDoc("t")
    d.figure_block("gold/confusion", "figures/confusion.png")
    out = d.render()
    cap = captions.caption_for("gold/confusion")
    assert "figures/confusion.png" in out
    assert cap.title in out and cap.good in out


def test_figure_block_unknown_id_still_embeds_image():
    d = findings.FindingsDoc("t")
    d.figure_block("no/such-figure", "figures/x.png", alt="x")
    out = d.render()
    assert "figures/x.png" in out


def test_gate_reports_each_item_and_overall_fail():
    d = findings.FindingsDoc("t")
    d.gate([("safety", True, "0 violations"), ("coverage", False, "0.21 vs 0.68")])
    out = d.render()
    assert "safety" in out and "coverage" in out
    assert "PASS" in out and "FAIL" in out
    assert "**FAIL**" in out  # one item failed -> overall FAIL


def test_gate_overall_pass_when_all_ok():
    d = findings.FindingsDoc("t")
    d.gate([("safety", True, "0"), ("recovery", True, "0.16 meV")])
    assert "**PASS**" in d.render()
