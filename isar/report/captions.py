"""Figure captions — the de-floating layer.

Every figure ISAR emits gets a `Caption` here: a short title, a plain-language "what you're looking
at", and a "what good looks like" line. The audience is a scientist fluent in the physics (DHO
lineshapes, Voigt resolution, least-squares) and in manual tools (Igor/PAN), but new to ISAR and to
its statistics. The caption is the single source of truth: the figure-save helper draws it under the
PNG and writes a sidecar, and the report writer embeds the same text — so a figure is understandable
on its own (in a slide or an email) and never "floats".

Framing (see the project's triage remit): ISAR is a first-pass triage tool, not full automation. The
"good" line describes *useful triage behaviour* — a reasonable answer where it's confident and a clear
flag where it isn't — not exact agreement with any human fit (which is itself provisional).
"""
from __future__ import annotations

import io
import textwrap
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


@dataclass(frozen=True)
class Caption:
    """One figure's explanation. `jargon` is a tuple of (term, one-line gloss) pairs."""
    title: str
    what: str            # what the axes / marks / colours mean
    good: str            # what a useful (triage-positive) result looks like
    jargon: tuple = ()   # ((term, gloss), ...) — glosses mirror docs/GLOSSARY.md

    def as_text(self) -> str:
        """Compact caption for drawing under the PNG (the helper wraps it to width)."""
        parts = [f"{self.title} — {self.what}", f"What good looks like: {self.good}"]
        if self.jargon:
            parts.append("  ".join(f"{t}: {g}" for t, g in self.jargon))
        return "  ".join(parts)

    def as_markdown(self) -> str:
        """Caption block for a FINDINGS report (image sits above it)."""
        lines = [f"**{self.title}** — {self.what}", "", f"*What good looks like:* {self.good}"]
        if self.jargon:
            terms = "; ".join(f"**{t}** — {g}" for t, g in self.jargon)
            lines += ["", f"*Terms:* {terms}"]
        return "\n".join(lines)


def caption_for(fig_id: str) -> Caption | None:
    return CAPTIONS.get(fig_id)


# ---- de-floating helpers: draw the caption on the PNG + write a sidecar ------------------------

def _sidecar_path(path: Path) -> Path:
    return path.parent / (path.stem + ".caption.md")


def write_sidecar(path, caption: Caption) -> Path:
    """Write `<stem>.caption.md` next to the PNG so the explanation travels with the figure file."""
    path = Path(path)
    sc = _sidecar_path(path)
    sc.write_text(caption.as_markdown() + "\n", encoding="utf-8")
    return sc


_CAPTION_FONTSIZE = 7


def _annotate(fig, caption: Caption) -> None:
    """Draw the wrapped caption in a reserved strip below the axis labels.

    The wrap width scales with the figure width (so a wide multi-panel figure gets a few long lines,
    not many short ones), and the reserved strip is sized from the line count — so the caption never
    overlaps the x-axis labels regardless of figure size.
    """
    w_in, h_in = (float(v) for v in fig.get_size_inches())
    width = int(max(60, min(200, w_in * 11)))
    text = textwrap.fill(caption.as_text(), width=width)
    n_lines = text.count("\n") + 1
    fig.tight_layout()
    line_frac = (n_lines * _CAPTION_FONTSIZE * 1.35) / (h_in * 72.0)
    fig.subplots_adjust(bottom=min(0.6, float(fig.subplotpars.bottom) + line_frac + 0.02))
    fig.text(0.5, 0.006, text, ha="center", va="bottom", fontsize=_CAPTION_FONTSIZE,
             color="0.25", linespacing=1.3)


def _finalize_layout(fig, fig_id: str, draw: bool) -> Caption | None:
    cap = caption_for(fig_id)
    if draw and cap is not None:
        _annotate(fig, cap)
    else:
        fig.tight_layout()
    return cap


def save_captioned(fig, path, fig_id: str, *, dpi: int = 110, draw: bool = True) -> Path:
    """Save `fig` to `path` with its caption drawn underneath, and write the sidecar `.md`.

    Replaces the `tight_layout(); savefig(); close()` tail in the check scripts. An unknown `fig_id`
    still saves a valid PNG (no caption / no sidecar); use `draw=False` for already-text-heavy panels
    to keep the sidecar without drawing over the figure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cap = _finalize_layout(fig, fig_id, draw)
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    if cap is not None:
        write_sidecar(path, cap)
    return path


def captioned_png_bytes(fig, fig_id: str, *, dpi: int = 100, draw: bool = True) -> bytes:
    """Return PNG bytes with the caption drawn underneath (the report-library / in-memory path)."""
    _finalize_layout(fig, fig_id, draw)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    plt.close(fig)
    return buf.getvalue()


# Shared glosses reused across captions (kept in sync with docs/GLOSSARY.md).
_J_PULL = ("pull", "a difference divided by its error bar (a normalized residual); pull 1 = one σ away")
_J_RESID = ("residual", "data minus fit; flat noise is good, leftover structure means the model missed something")
_J_RESOLVED = ("resolved", "ISAR is confident — use the number")
_J_FLAGGED = ("flagged", "usable number but imperfect model — eyeball it")
_J_REJECT = ("reject", "ISAR couldn't fit it — look manually (nothing is deleted)")
_J_ABSTAIN = ("abstain", "too little to attempt — recount or skip")
_J_BOOT = ("bootstrap", "resample the data many times and refit to get the spread of an estimate")


CAPTIONS: dict[str, Caption] = {
    # ---- shared reduction-report library (isar/report/figures.py) -------------------------------
    "report/coverage": Caption(
        title="Q coverage",
        what="each tick is a Q point you actually measured along the dominant scan direction; a red "
             "span is a gap with no data there.",
        good="ticks span the range with few or no red gaps — you sampled Q without holes to fill.",
        jargon=(("rlu", "reciprocal lattice units — Q in units of the crystal's own lattice"),)),
    "report/spectrum": Caption(
        title="Raw spectrum (one analyzer)",
        what="counts vs energy transfer for a single analyzer: blue = kept points (±√N), red × = "
             "masked (shown, not deleted), orange rings = cosmic-ray-cleaned.",
        good="a clean peak, with points masked only where you'd expect (spurions, dropouts) and "
             "nothing silently thrown away.",
        jargon=(("energy transfer", "energy the probe lost/gained; 0 = elastic"),
                ("masked", "excluded from the fit but still kept in the file"))),
    "report/miniscan": Caption(
        title="Per-pass drill-down",
        what="each bar is one acquisition pass at a single point; red bars were rejected as cosmic "
             "rays, the dashed line is the mean of the kept passes.",
        good="one or two spike passes removed and the rest tightly clustered — the cut took out a "
             "cosmic ray, not real signal."),
    "report/merged": Caption(
        title="Merged spectrum (combine-before-divide)",
        what="summed raw counts vs energy for repeat scans at the same (Q, T).",
        good="the merged peak is smoother than any single scan (more counts) with no step artifacts "
             "from stitching.",
        jargon=(("combine-before-divide",
                 "sum raw counts, then normalize once — never average already-normalized rates"),)),

    # ---- gold scoring (docs/phase3_gold_check.py) ----------------------------------------------
    "gold/confusion": Caption(
        title="What the human did vs what ISAR did",
        what="rows are the human's call (accept / reject), columns are ISAR's (resolved / flagged / "
             "reject / abstain); each cell counts scans.",
        good="the human's rejects never land in ISAR's 'resolved' column (ISAR declines the same bad "
             "scans), and most human-accepts land in resolved or flagged (ISAR hands you a usable "
             "number).",
        jargon=(_J_RESOLVED, _J_FLAGGED, _J_REJECT, _J_ABSTAIN)),
    "gold/pull_hist": Caption(
        title="How close ISAR's energy is to the human's",
        what="for scans where both gave a number, the spread of (ISAR ω₀ − human ω₀) divided by the "
             "combined error bar; 0 is exact agreement, the line marks mean ± sd.",
        good="a hump near 0. A small offset is fine for triage — the human fit is itself provisional, "
             "and these error bars are statistical-only (see the report's limitations).",
        jargon=(_J_PULL,)),
    "gold/scatter": Caption(
        title="ISAR energy vs human energy",
        what="one point per scan: ISAR's ω₀ (y) against the human's ω₀ (x); the dashed line is exact "
             "agreement, colour is ISAR's verdict, × marks scans the human rejected.",
        good="points hug the diagonal across the whole range — ISAR lands in the same ballpark the "
             "human did, with the verdict colours showing where it's confident vs flagged.",
        jargon=(_J_RESOLVED, _J_FLAGGED)),
    "gold/confidence": Caption(
        title="Is ISAR confident when it's right?",
        what="x = how far ISAR's energy is from the human's (in error bars); y = ISAR's confidence. "
             "Blue = human accepted; red × = human rejected but ISAR called it resolved (the danger "
             "corner).",
        good="confidence drops as the gap grows and the red danger corner is empty — when ISAR is "
             "confident it's usually close, and it is not confidently wrong.",
        jargon=(("confidence", "ISAR's self-rated trust in a fit, 0–1"), _J_PULL)),
    "gold/audit": Caption(
        title="See it with your own eyes",
        what="representative scans side by side: the data, ISAR's fit, the human's (provisional) fit "
             "overlaid, and the residual underneath; each panel is labelled with ISAR's verdict and "
             "both energies.",
        good="for delivered scans the two fits track the same peak; for a flagged or over-flagged "
             "scan you can see why ISAR hesitated; for the human-rejected scan you can see the "
             "blown-up fit ISAR also declined — so you can judge every call yourself.",
        jargon=(_J_RESID, _J_FLAGGED, _J_REJECT)),
    "gold/resolution_overlay": Caption(
        title="ISAR's resolution vs a hand-measured one",
        what="the instrument resolution ISAR measured itself from the elastic/Bragg scans, against a "
             "hand-measured value, for each slit setting.",
        good="the two are in the same ballpark. Exact match is not required or expected — ISAR "
             "calibrates its own resolution each run; this is a transparency check, not a target.",
        jargon=(("resolution", "the instrumental width the data is smeared by (Voigt: a Gaussian and "
                 "a Lorentzian width)"),
                ("slit", "the analyzer slit setting that sets the resolution"))),

    # ---- phase-3 step-1 control (docs/phase3_step1_check.py) ------------------------------------
    "step1/spectrum": Caption(
        title="One scan, taken apart",
        what="the data with the winning fit drawn as separate pieces — phonon, elastic line, "
             "background — and a pull-residual strip beneath.",
        good="the fit overlays the data, the phonon and elastic pieces are clearly separated, and the "
             "residual is flat noise.",
        jargon=(("phonon (DHO)", "the inelastic peak, a damped harmonic oscillator"),
                ("elastic line", "the peak at zero energy transfer"), _J_PULL)),
    "step1/dispersion": Caption(
        title="Energy vs momentum, against the known answer",
        what="recovered phonon energy ω₀ as a function of momentum K, with bootstrap error bars, "
             "overlaid on the frozen Phase-1 baseline.",
        good="ISAR's points sit on the baseline and trace a sensible dispersion — the automated "
             "pipeline reproduces the trusted fitter.",
        jargon=(("dispersion", "how phonon energy changes with momentum K"), _J_BOOT)),
    "step1/bootstrap_hist": Caption(
        title="Cheap error bar vs honest error bar",
        what="per scan, the spread of ω₀ from bootstrap resampling (histogram + 68% band) with the "
             "quick HESSE ±σ band drawn on top.",
        good="the two bands overlap — on well-counted data the fast error bar and the resampled one "
             "agree, which is the regime where the quick number is trustworthy.",
        jargon=(_J_BOOT,
                ("HESSE", "error bar from the fit's curvature — fast, but optimistic at low counts"),
                ("Bernstein–von Mises", "the result that the two agree when data is plentiful"))),
    "step1/selection_weights": Caption(
        title="Which model the data prefers",
        what="for each scan, the Akaike weight on each candidate model (e.g. elastic+phonon+background "
             "vs phonon+background); the bars sum to 1.",
        good="one model carries almost all the weight — the data clearly prefers a structure and ISAR "
             "isn't guessing between equally-good models.",
        jargon=(("Akaike weight", "relative evidence for a model from AIC; higher = better supported"),
                ("n/c", "that model did not converge"))),
    "step1/synthetic_fork": Caption(
        title="When the answer is ambiguous, say so",
        what="a synthetic case where two structures fit equally well: the pooled ω₀ density is "
             "bimodal, with each mode and its weight marked.",
        good="ISAR reports BOTH peaks (a 'fork') instead of averaging them into a single wrong number "
             "— it tells you the data is ambiguous.",
        jargon=(("fork", "two or more equally-supported answers, reported separately"),
                ("fold", "a single agreed answer"))),
    "step1/verdict_confidence": Caption(
        title="The triage verdict in action",
        what="left: per-scan confidence with the 'resolved' threshold drawn; right: the gallery of "
             "non-resolved buckets exercised on synthetic cases.",
        good="real control scans sit above the threshold (confidently resolved) and every "
             "non-resolved bucket is reachable — the triage taxonomy works end to end.",
        jargon=(("confidence", "ISAR's self-rated trust in a fit, 0–1"),
                ("buckets", "the reasons ISAR declines (held / underpowered / no_signal / junk / "
                 "unconstrained) — each a different 'look here' signal"))),
    "step1/energy_zero": Caption(
        title="Finding the energy zero, and checking it",
        what="the fitted energy zero per scan over time, with leave-one-out interpolated values drawn "
             "on top.",
        good="the zero drifts smoothly and the interpolation tracks it — ISAR can recover a scan's "
             "zero from its neighbours when that scan's own elastic line is unreliable.",
        jargon=(("energy zero", "where elastic scattering sits on the energy axis (defines 0 meV)"),
                ("leave-one-out", "predict each point from the others to test the interpolation"))),

    # ---- phase-1 anchor control (docs/in8_phase1_check.py) -------------------------------------
    "in8/overlay": Caption(
        title="Same core, a different instrument (triple-axis)",
        what="a real IN8 triple-axis scan with ISAR's 1-D Voigt-convolved fit and the residual below.",
        good="ISAR recovers a sensible phonon energy on real triple-axis data (not just IXS), and the "
             "residual shows the broad low-energy tail is real quasi-elastic signal, not a fit "
             "failure.",
        jargon=(("TAS", "triple-axis spectrometer (neutron)"),
                ("Voigt", "a Gaussian⊗Lorentzian resolution shape"),
                ("quasi-elastic", "a broad component near zero energy transfer"))),
    "in8_gold/anchors": Caption(
        title="ISAR's fits on real triple-axis anchors",
        what="each panel is one IN8 scan: data with error bars, ISAR's best fit broken into its "
             "components (phonon / quasi-elastic / background), and the pull residual below.",
        good="the phonon component sits at the right energy and the broad quasi-elastic component "
             "absorbs the low-energy magnetic tail, leaving a flat pull — the energy is trustworthy even "
             "where the model is imperfect.",
        jargon=(_J_PULL, ("quasi-elastic", "a broad component near zero energy transfer — real "
                          "magnetic/diffuse scattering on this sample, not resolution"))),
    "in8_gold/scatter": Caption(
        title="ISAR energy vs gold energy (IN8)",
        what="one point per scored scan: ISAR's ω₀ (y) against the gold ω₀ (x); dashed is exact "
             "agreement, × marks the rejected anchor.",
        good="points hug the diagonal — ISAR recovers the same energy on a second instrument. NB only "
             "055182 is vetted truth; the other gold values are provisional placeholders.",
        jargon=()),

    # ---- campaign diagnostic (isar/report/campaign_diagnostic.py) ------------------------------
    "campaign/dispersion_truth": Caption(
        title="Delivered dispersion vs truth",
        what="each delivered ω₀ plotted against the dominant Q component, with the analytic truth "
             "curve; filled circles are converged q (with calibrated halfwidth error bars), open "
             "diamonds are uncertifiable/fast-tier deliveries (centres shown, no honest bar), and × "
             "on the bottom rug marks no-signal / infeasible q.",
        good="filled points sit on the truth line across the reachable path and the open/× markers "
             "cluster only where the physics is genuinely hard (near Γ, the zone boundary) — the "
             "campaign mapped what it could and flagged what it couldn't.",
        jargon=(("calibrated halfwidth", "the σ-calibration-inflated 68% ω₀ interval half-width the "
                 "stopping rule consumes"),
                ("uncertifiable", "a delivered centre whose interval flagged interval_reliable=False "
                 "— usable number, untrustworthy bar"))),
    "campaign/scan_spend": Caption(
        title="Where the scans went",
        what="a stacked bar per grid q counting the scans spent there by kind: quicklook (cheap "
             "seeding), production/confirm (full fits), and declined (no usable ω₀).",
        good="cheap quicklooks spread thin to seed the model and the expensive production fits "
             "concentrate on the q that pay off — little beam burned re-confirming a q that will "
             "never seat.",
        jargon=(("quicklook", "a cheap HESSE-only scan used to seed the ω(q) model, not to deliver"),
                ("confirm", "a repeat production fit to tighten an interval already near tolerance"))),
    "campaign/budget_timing": Caption(
        title="Budget & time ledgers",
        what="left: the neutron ledger (Σ of per-run cost) and the counting-second ledger against the "
             "budget; right: wall-clock seconds broken down by the slowest driver stages.",
        good="the two ledgers agree with each other and with the stop reason (a statistics-driven "
             "stop should show budget barely touched), and the wall split shows where a speed-up "
             "would actually pay — usually the fitting stage.",
        jargon=(("counting seconds", "instrument counting time (the deterministic tier's virtual "
                 "clock); verify its units before trusting a budget-limited stop"),)),
    "campaign/scan": Caption(
        title="One scan, reconstructed",
        what="raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters "
             "(an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual "
             "strip beneath; the title carries the disposition/verdict and the honest interval string.",
        good="the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ "
             "is supported by the counts; where a scan was declined you can see why in the residual.",
        jargon=(_J_PULL, _J_RESID,
                ("reconstructed", "the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted "
                 "again — presentation only"))),

    # ---- statistics primer gallery (docs/stats/make_figures.py) --------------------------------
    "stats/three_questions": Caption(
        title="Every results row answers one of three questions",
        what="a static map of the pipeline: MINIMIZE (which parameters, via Baker–Cousins), SELECT "
             "(which model, via Akaike weights), CERTIFY (what uncertainty, via a four-rung interval "
             "ladder — Hessian → profile → bootstrap → Bayesian); the columns list the actual "
             "results.csv columns each stage produces. The Bayesian rung is drawn dashed because it is "
             "Planned, not wired in. No data — generated by docs/stats/make_figures.py, seed 20260710.",
        good="you can trace any number in a results row back to the question it answers and to the "
             "rung that produced its error bar — nothing floats unattributed.",
        jargon=(("Baker–Cousins", "the Poisson maximum-likelihood cost ISAR minimizes instead of a χ²"),
                ("ladder", "intervals ordered cheapest-to-most-honest; agreement across rungs is the "
                 "trust signal (hesse_trusted, interval_reliable)"))),
    "stats/cost_bias": Caption(
        title="Why not the χ² you already use",
        what="(a) for a constant rate the three cost functions have exact closed-form minimizers — "
             "Neyman χ² → the harmonic mean, modified-Pearson χ² → the RMS, Baker–Cousins → the "
             "arithmetic mean — so the two χ²'s provably straddle the likelihood, drawn on one seeded "
             "Poisson draw. (b) the measured consequence: mean fitted/true phonon amplitude vs counts "
             "(N=500 scans per level, ±SE), showing the χ² normalizations bias low/high at triage "
             "counts while Baker–Cousins stays near 1. Generated by make_figures.py, seed 20260710.",
        good="the closed-form marks land on the numeric minima (a), and by ~100 counts all three "
             "converge to 1 while at 5 counts the χ² choices are visibly biased in opposite "
             "directions (b) — the reason ISAR minimizes the likelihood, not a χ².",
        jargon=(("Baker–Cousins", "the Poisson −2lnL; its constant-rate minimizer is the arithmetic "
                 "mean (unbiased)"),
                ("modified-Pearson χ²", "Σ(y−f)²/f; minimizer is the RMS, biased high"),
                ("Neyman χ²", "Σ(y−f)²/max(y,1); minimizer is the harmonic mean, biased low"))),
    "stats/coverage_table": Caption(
        title="Measured 68% coverage of the interval ladder",
        what="two row groups — ω₀ (Hessian / profile / bootstrap) and Γ (Hessian / bootstrap; ω₀ has "
             "the extra profile rung, Γ does not) — against count level (15/30/100), each cell the "
             "observed fraction of synthetic scans whose 68% interval covered the true value ± its "
             "binomial SE. Γ rows EXCLUDE fits whose Γ railed to its bound (those are the wall figure's "
             "story, not the ladder's; the excluded count per cell is noted below the table and in the "
             "JSON). Cells are shaded by how far they sit below nominal (white ≤1 SE, amber 1–2, red >2 "
             "— no green, because under-coverage is the finding). A footnote line adds one "
             "skewed-regime cell (Γ_true=0.10 meV at 15 counts, near the resolution/bound) with its "
             "at-bound count and the median bootstrap Γ-interval asymmetry (hi−med)/(med−lo) — the "
             "regime where a symmetric HESSE bar is most exposed. Values are copied verbatim from the "
             "committed coverage_summary.json; N=200 synthetic scans per cell, seed 20260710.",
        good="the ω₀ rungs agree with each other near 0.68 (HESSE does not degrade on a location "
             "parameter); the Γ rows and the skewed-cell footnote show whether the symmetric Hessian "
             "band under-covers the bootstrap once the Γ posterior is right-skewed — measured on known "
             "truth, an honest result either way. But this is an ensemble average: matching coverage "
             "here does not rescue any single measurement (see the per-scan width-ratio figure) — the "
             "per-scan hesse_trusted / interval_reliable flags are what protect one scan.",
        jargon=(_J_BOOT,
                ("Hessian", "the curvature (HESSE) error bar — fast, but optimistic when the cost is "
                 "not parabolic (Γ near its bound), honest when it is (ω₀)"),
                ("at-bound", "Γ railed to its floor → the interval degenerates; excluded from the Γ "
                 "coverage number and counted separately"))),
    "stats/coverage_curve": Caption(
        title="Coverage vs counts — ω₀ and Γ side by side",
        what="the same measured coverage as the table in two panels: (a) ω₀ for the three rungs, (b) Γ "
             "for the two rungs (at-bound fits excluded), both with binomial-SE bars, a dashed nominal "
             "0.68 line, and a grey ±1 binomial-SE band at N=200. Panel (b) also carries an extra "
             "skewed-regime cell as star markers (Γ_true=0.10 meV at 15 counts, near the "
             "resolution/bound), where the Γ posterior is right-skewed. On ω₀ the three rungs track "
             "each other (HESSE is honest on a location parameter); the Γ panel is where a skewed, "
             "bound-adjacent parameter can separate the Hessian rung from the bootstrap. Same "
             "coverage_summary.json as the table — one dataset, two views. Seed 20260710.",
        good="the ω₀ rungs sit together near 0.68 and any joint dip across all three at high counts "
             "reads as a systematic (not interval width); at the skewed-Γ star markers the symmetric "
             "Hessian band under-covers the bootstrap if the Γ posterior is skewed enough — exactly "
             "the regime the hesse_trusted flag encodes (an honest null if the separation is small). "
             "Remember these are ensemble averages: the per-scan width-ratio figure shows a single "
             "scan can still disagree even when the average calibrates.",
        jargon=(_J_BOOT,
                ("Bernstein–von Mises", "the theorem that the rungs agree once counts are plentiful"),
                ("Hessian", "the curvature error bar; honest on ω₀, optimistic on a skewed Γ"))),
    "stats/coverage_scatter": Caption(
        title="Per-scan interval-width ratio — one scan is not the ensemble",
        what="box plots of the per-scan width(HESSE)/width(bootstrap) ratio by count level: (a) ω₀ over "
             "all converged scans, (b) Γ over at-bound-excluded scans (with the skewed Γ_true=0.10 cell "
             "appended). The grey band is the [0.80, 1.25] agreement window (log-symmetric: a HESSE bar "
             "within ±25% of the bootstrap), the dashed line is exact agreement (ratio 1.0), and each "
             "box is annotated with the fraction of scans falling OUTSIDE the band and its N. Built from "
             "the committed coverage_perscan.json (median/IQR/frac-outside also in "
             "coverage_summary.json → diagnostics.width_ratio); seed 20260710.",
        good="the boxes straddle 1.0 with most scans inside the band — but the spread and the "
             "frac-outside are the point: coverage is an ensemble average, and a single scan can still "
             "draw a HESSE bar well off its bootstrap. That per-scan disagreement is exactly what the "
             "hesse_trusted / interval_reliable flags catch, scan by scan.",
        jargon=(_J_BOOT,
                ("width ratio", "one scan's HESSE interval width ÷ its bootstrap width; 1.0 = the fast "
                 "bar matches the honest one, far from 1.0 = trust the bootstrap on THIS scan"),
                ("IQR", "inter-quartile range (25th–75th percentile) — the box height; how much the "
                 "ratio varies scan to scan"))),
    "stats/wall": Caption(
        title="When the ladder breaks — the Γ wall",
        what="(a) a schematic contrasting an interior cost minimum (curvature defined, HESSE "
             "meaningful) with a minimum pinned at the Γ≥0 bound (no parabola to invert; the bootstrap "
             "refits pile onto the wall); (b) a healthy bootstrap ω₀ histogram from an archived scan "
             "with a genuine interior Γ minimum (M=200, interval_reliable=True); (c) an archived scan "
             "whose Γ railed to its floor, re-fit standalone from the archived calibration — the "
             "bootstrap collapses to a spike, drawn on an x-span as wide as (b). Each panel is labelled "
             "with the scan number it used; the script asserts the collapsed width <0.01 meV and the "
             "healthy width sane. Generated by make_figures.py against workspace mvp_zone_test16, seed "
             "20260710.",
        good="ISAR recognizes the collapse — Γ at the bound → curvature undefined → it flags it "
             "(interval_reliable=False, gamma_provisional) and falls back to the HESSE band rather than "
             "quoting a 0.0007 meV error bar as if it were real.",
        jargon=(_J_BOOT,
                ("Γ at the bound", "the damping railed to its floor; ω₀=√(Ω_d²+Γ²) is then locally "
                 "1:1 in Ω_d and the interval degenerates"),
                ("interval_reliable", "False when the derived ω₀ interval came from a boundary-pinned "
                 "fit — usable centre, untrustworthy bar"))),
    "stats/akaike_example": Caption(
        title="Which model, decided by evidence not by raw cost",
        what="archived scan 507: the one-mode (stokes+bg) fit drawn dashed and the two-mode "
             "(stokes2+bg) fit solid with its components dotted, over the data (±√N); the annotation "
             "carries the build-time AIC / ΔAIC / Akaike-weight table from score_models, and the lower "
             "strip shows the pull residuals for both models — the coherent one-mode residual near "
             "7 meV that the second mode flattens. Generated by make_figures.py against workspace "
             "mvp_zone_test16, seed 20260710.",
        good="the Akaike weight concentrates on the two-mode model and its pull residual is flat where "
             "the one-mode residual carried leftover structure — the second mode earned its parameters "
             "against the AIC tax, rather than being read off raw cost (which always prefers more "
             "peaks).",
        jargon=(_J_PULL, _J_RESID,
                ("Akaike weight", "relative evidence from AIC = cost + 2k; the parameter tax is why a "
                 "flexible model does not win automatically"),
                ("ΔAIC", "AIC gap to the best model; ≳10 is decisive"))),
    "stats/branch_posterior": Caption(
        title="The earned Bayesian pieces — a branch posterior",
        what="acoustic-vs-optic branch-assignment posterior for two archived scans: an unambiguous one "
             "(505, entropy H≈0.003) and an ambiguous one (477, H≈0.646 of the ln 2≈0.693 maximum), "
             "with the assigned branch in colour and the weights printed on the bars; the entropy "
             "number itself is the deliverable (near 0 → bankable into the prior, near ln 2 → report "
             "the ambiguity). These are archived numbers rendered from results.csv, as the caption "
             "states — not recomputed. Generated by make_figures.py, seed 20260710.",
        good="the certain scan puts almost all the weight on one branch (low entropy, safe to bank) "
             "and the ambiguous scan sits near a coin flip (high entropy, reported as ambiguous) — the "
             "posterior tells you which is which instead of silently guessing.",
        jargon=(("credible", "used here because this genuinely is a posterior; ISAR's ω₀ intervals are "
                 "frequentist and are never called credible"),
                ("assignment entropy", "Shannon entropy of the branch posterior in nats; 0 = certain, "
                 "ln 2 ≈ 0.693 = a coin flip"))),
}
