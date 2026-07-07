# ISAR — what it's for, and how to read everything

This is the front door. It explains, for a scientist who knows inelastic scattering and manual
fitting but has never used ISAR, **what problem ISAR solves, what it deliberately does not do, and
how we know it works** — then points you at the right report or document for each question.

> Status: ISAR is in **development and testing**. The analysis pipeline is assembled behind an
> `isar` command-line tool that runs end-to-end ([UNIFICATION_DESIGN.md](UNIFICATION_DESIGN.md),
> built; user workflow in [USING_ISAR.md](USING_ISAR.md)), and the **autonomous driver**
> (`isar drive`) now closes the loop: from a natural-language goal it proposes scans, drives the
> TAVI virtual instrument, fits each scan, and decides where to remeasure and when to stop — with
> no human numbers in the loop ([MVP_FIRST_RUN.md](MVP_FIRST_RUN.md)). This runs on the TAVI
> *simulator* and on *old* data to exercise the workflow, not yet on a live experiment. The goal
> right now is to make ISAR's behaviour legible — to understand what it does and to show why the
> approach is worth continuing.

## The problem

At an IXS or triple-axis beamtime you take a great many energy scans — Q point after Q point,
temperature after temperature. To know whether a measurement worked, whether to recount, or where the
phonon sits, each scan has to be fit: a damped-harmonic-oscillator (DHO) peak on an elastic line and a
background, convolved with the instrument resolution. Doing that by hand, scan by scan, is slow, and
it happens *after* the beam time you could have used to act on it. By the time you've fit the morning's
data, the morning is gone.

## What ISAR is

**ISAR is an automated triage fitter.** Point it at a run and it fits every scan, reports a phonon
energy with a statistical (counting-noise) uncertainty — see the caveat below on what that does and
does not mean — and, crucially, attaches a **verdict** to each one:

- **resolved** — it's confident; use the number.
- **flagged** — there's a usable number but the model is imperfect; glance at it.
- **declined** (junk / unconstrained / held / underpowered / no_signal) — it couldn't fit this well;
  look manually. **Nothing is deleted** — a declined scan keeps its raw data and is surfaced to you.

Concretely, lines in the run summary read like (real rows from the gold run, `out/phase3_gold/`):

```
scan 168   omega0 = 5.70 meV    verdict: resolved
scan 198   omega0 = 0.88 meV    verdict: flagged (structured residual)
scan 80    —                    verdict: junk — look manually
```

So when you walk in, you have most of the run already fit and a short list of scans flagged for your
eyes. You spend your manual effort where it actually matters.

## What ISAR is *not*

- **Not** a replacement for your own analysis. It's a first pass; you still do the careful work.
- **Not** a final, paper-grade fitter. Its error bars are statistical-only (see the primer).
- **Not** trying to reproduce your hand fits exactly. Hand fits are tuned for a final publication;
  ISAR aims at a consistent, reproducible first pass for detection and triage — different goals. The
  human reference is itself provisional and may be revised, so chasing exact agreement would
  *over-tune* ISAR to one dataset and make it worse on the next experiment. The aim is **good-enough,
  and clear about its failures** — that beats brittle precision.

## Why automate it this way

- **Speed where it counts:** the aim is a first pass fast enough to act on *during* the experiment,
  not after it (the live-speed integration isn't demonstrated yet — see status).
- **Honest triage:** ISAR says where it's unsure. A loud "look here" is more useful than a confident
  wrong answer — and it never silently drops a scan (*flag, don't delete*).
- **Confidence that tracks accuracy:** a miscalibrated error bar can misdirect downstream decisions
  (what to recount, where the dispersion goes), so ISAR is built so its confidence rises and falls with
  how close its fits actually land — while being explicit that its error bars are statistical-only
  (below).

## How we know it works — two tiers

ISAR is validated two ways, deliberately kept separate:

1. **Synthetic ground truth** — generate spectra with *known* parameters, fit them, and check the
   recovered values and the calibration of the error bars. This is the only tier where the true answer
   is actually known, with no human to over-fit to.
2. **Real-data reasonableness** — run the full pipeline on real scans and compare its verdicts against
   a human expert's by-hand fits. This is a *reasonableness and safety* check, **not** a correctness
   proof: the human fits are provisional. The load-bearing property is **safety** — ISAR must never
   confidently "resolve" a scan the expert rejected as a blown-up fit.

The headline real-data check is the **gold report** (`out/phase3_gold/FINDINGS.md`). Its visual audit
puts the data, ISAR's fit, and the expert's fit side by side on representative scans so you can judge
every call with your own eyes.

## When *not* to trust ISAR (limitations)

- **The central value is good; the error bar is statistical-only.** On the gold set ISAR's energy lands
  a median of ~0.16 meV from the human's — well inside the ~1 meV resolution, so the *number* is a good
  ballpark. But the error bar captures counting noise only, not systematics (background shape,
  resolution, energy-zero drift), so it is too tight (the human value lands inside ISAR's 68% interval
  only ~20% of the time). Use the energy as a ballpark; look manually before quoting an uncertainty, and
  don't weight by ISAR's σ. See [STATS_PRIMER.md](STATS_PRIMER.md).
- **`junk` is conservative.** It sometimes parks a scan whose energy was actually fine into your manual
  pile. That's the safe direction (you lose a little automation, not a number), but it means a "junk"
  verdict is worth a look, not a delete.
- **Triple-axis Γ is unreliable without a vanadium scan.** On TAS, ISAR's 1-D resolution can't separate
  the instrumental width from the intrinsic damping; trust ω₀, not Γ. On IXS (resolution measured from
  the elastic line) Γ is meaningful.

## Where to read what

| Question | Where |
|---|---|
| What is ISAR for / why trust it? | this guide |
| How will I run it on my own data (the CLI)? | [USING_ISAR.md](USING_ISAR.md) |
| How does the autonomous loop work (goal → scans → stop)? | [MVP_FIRST_RUN.md](MVP_FIRST_RUN.md), [PHASE_4_NOTES.md](PHASE_4_NOTES.md) |
| What does a term mean (pull, fold/fork, cost/dof, a verdict)? | [GLOSSARY.md](GLOSSARY.md) |
| Why these statistics (vs χ² / HESSE / eyeballing models)? | [STATS_PRIMER.md](STATS_PRIMER.md) |
| What should I *do* with a given verdict? | [INTERPRETING_A_VERDICT.md](INTERPRETING_A_VERDICT.md) |
| One scan, start to finish | [WORKED_EXAMPLE.md](WORKED_EXAMPLE.md) |
| Does it agree with a human on real data? | `out/phase3_gold/FINDINGS.md` (gold report) |
| Does the engine reproduce the trusted fitter + behave? | `out/phase3_step1/FINDINGS.md` |
| Does the same core work on another instrument (triple-axis)? | `out/in8_phase1/FINDINGS.md` |
| The full design rationale | [PHASE_1_DESIGN.md](PHASE_1_DESIGN.md), [PHASE_2_DESIGN.md](PHASE_2_DESIGN.md), [PHASE_3_DESIGN.md](PHASE_3_DESIGN.md) |
| How it all assembles into one tool (current phase) | [UNIFICATION_DESIGN.md](UNIFICATION_DESIGN.md) |
| Why steer measurements this way (Bayesian info-gain; prior art; cheap/sim/real tiers) — research foundation | [MEASUREMENT_STEERING.md](MEASUREMENT_STEERING.md) |
| Build-session handoff briefs (Phase 1/2, Unification, LLMM integration) | archived under [docs/archive/](archive/) |

The `out/**` reports are regenerated by the `docs/*_check.py` scripts (they are gitignored — run a
script to produce its report). Every figure in those reports carries its own caption (drawn on the
image and written as a `<figure>.caption.md` sidecar), so a figure is understandable on its own.
