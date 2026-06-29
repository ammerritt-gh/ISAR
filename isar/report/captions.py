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
}
