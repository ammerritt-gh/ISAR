# ISAR — what it's for, and how to read everything

> **Status:** live
> **Authority:** canonical for the general documentation map — the single map; every maintained
> document is reachable from here (enforced by `tests/docs/test_doc_drift.py`)
> **Verified against code revision:** `d0ea1df` (2026-07-25)

This is the front door. It explains, for a scientist who knows inelastic scattering and manual
fitting but has never used ISAR, **what problem ISAR solves, what it deliberately does not do, and
how we know it works** — then points you at the right report or document for each question.

> Status: ISAR is in **development and testing**. The analysis pipeline is assembled behind an
> `isar` command-line tool that runs end-to-end (user workflow in
> [USING_ISAR.md](USING_ISAR.md); the workspace/session/CLI contract in
> [WORKSPACE_AND_CLI.md](WORKSPACE_AND_CLI.md)), and the **autonomous driver** (`isar drive`)
> closes the loop: from a natural-language goal it proposes scans, drives the TAVI virtual
> instrument, fits each scan, and decides where to remeasure and when to stop — with no human
> numbers in the loop ([ARCHITECTURE.md §8](ARCHITECTURE.md)). This runs on TAVI's *deterministic*
> simulation rung and on *old* data to exercise the workflow — realistic clocks and the McStas
> rung are Planned, and it has never run on a live experiment. The goal right now is to make
> ISAR's behaviour legible — to understand what it does and to show why the approach is worth
> continuing.
>
> Per-capability status (Implemented / Partial / Planned) is maintained in one place:
> [ARCHITECTURE.md §15](ARCHITECTURE.md).

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

Concretely, lines in the run summary read like (real rows from the gold run,
[assets/phase3_gold/](assets/phase3_gold/)):

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

The headline real-data check is the **gold report**,
[assets/phase3_gold/FINDINGS.md](assets/phase3_gold/FINDINGS.md) — committed, so you can read it
without running anything. Its visual audit puts the data, ISAR's fit, and the expert's fit side by
side on representative scans so you can judge every call with your own eyes.

## When *not* to trust ISAR (limitations)

- **The central value is good; the error bar is statistical-only.** On the gold set ISAR's energy lands
  a median of ~0.16 meV from the human's — well inside the ~1 meV resolution, so the *number* is a good
  ballpark. But the error bar captures counting noise only, not systematics (background shape,
  resolution, energy-zero drift), so it is too tight (the human value lands inside ISAR's 68% interval
  only 18% of the time). Use the energy as a ballpark; look manually before quoting an uncertainty, and
  don't weight by ISAR's σ. See [STATS_PRIMER.md](STATS_PRIMER.md).
- **`junk` is conservative.** It sometimes parks a scan whose energy was actually fine into your manual
  pile. That's the safe direction (you lose a little automation, not a number), but it means a "junk"
  verdict is worth a look, not a delete.
- **Triple-axis Γ is unreliable without a vanadium scan.** On TAS, ISAR's 1-D resolution can't separate
  the instrumental width from the intrinsic damping; trust ω₀, not Γ. On IXS (resolution measured from
  the elastic line) Γ is meaningful.

## Where to read what

This is the sole general map of ISAR's documentation. The **Status** column says whether you may
rely on what you are about to open: *live* is current and authoritative, maintained with the
code; *draft* is being designed and may change under you; *terminal* means the document's job has
ended — it is awaiting absorption into a living document and then deletion, and must never be
read as current behaviour. (A documentation rebuild is in progress; the frontier is in
[HANDOFF-doc-rebuild.md](HANDOFF-doc-rebuild.md).)

| Question | Where | Status |
|---|---|---|
| What is ISAR for / why trust it? | this guide | live |
| What should ISAR become (the owner's master objectives)? | [DESIGN_GOALS.md](DESIGN_GOALS.md) | live (owner-authored) |
| The product overview and quickstart | [../README.md](../README.md) | live |
| Contributor rules, hard rules, and area routing | [../AGENTS.md](../AGENTS.md) | live |
| The backlog | [../TODO.md](../TODO.md) | live |
| How will I run it on my own data (the CLI)? | [USING_ISAR.md](USING_ISAR.md) | live |
| How does the autonomous loop work (goal → scans → stop)? | [ARCHITECTURE.md §8](ARCHITECTURE.md) | live |
| What is legal at an ambiguous frontier / how does it stop? | [design/steering.md](design/steering.md) | live |
| Where did a campaign spend its time? | `isar profile` ([USING_ISAR.md](USING_ISAR.md)) | live |
| What does a term mean (pull, fold/fork, cost/dof, a verdict)? | [GLOSSARY.md](GLOSSARY.md) | live |
| Why these statistics (vs χ² / HESSE / eyeballing models)? — the statistics design doc | [STATS_PRIMER.md](STATS_PRIMER.md) | live |
| What should I *do* with a given verdict? | [INTERPRETING_A_VERDICT.md](INTERPRETING_A_VERDICT.md) | live |
| One scan, start to finish | [WORKED_EXAMPLE.md](WORKED_EXAMPLE.md) | live |
| How do I declare the excitations I expect? | [HYPOTHESES.md](HYPOTHESES.md) | live |
| How does LLM flag adjudication work, and what does it never touch? | [ADJUDICATION.md](ADJUDICATION.md) | live |
| How it all assembles into one tool (workspace, session, CLI) | [WORKSPACE_AND_CLI.md](WORKSPACE_AND_CLI.md) | live |
| What the campaign diagnostic owes you | [design/reporting.md](design/reporting.md) | live |
| How the GUI is built and bounded | [design/gui.md](design/gui.md) | live |
| What the data/card schema looks like across facilities | [SCHEMA_LANDSCAPE.md](SCHEMA_LANDSCAPE.md) | live |
| The scene layer (catalogue, questions, discovery) | [design/scene.md](design/scene.md) | live |
| The SCENE-4 execution ledger (PR 4.4 pending) | [plans/SCENE4_EXECUTION_PLAN.md](plans/SCENE4_EXECUTION_PLAN.md) | live |
| The statistics audit still in flight (STAT-3–6) | [plans/stats-audit.md](plans/stats-audit.md) | live |
| Why steer measurements this way (Bayesian info-gain; prior art) — research foundation | [design/steering.md](design/steering.md) | live |
| What is being built next (speedup, backgrounds, steering sub-phases A–C) | [plans/speedup.md](plans/speedup.md), [plans/background.md](plans/background.md), [design/steering.md §10](design/steering.md) | live |
| Same-action scan-window EIG — contract and status | [design/eig.md](design/eig.md) | live |
| The documentation rebuild in progress (frontier + operator closeout list) | [HANDOFF-doc-rebuild.md](HANDOFF-doc-rebuild.md) | live |
| Does it agree with a human on real data? | [assets/phase3_gold/FINDINGS.md](assets/phase3_gold/FINDINGS.md) (gold report) | evidence bundle |
| Does the engine reproduce the trusted fitter + behave? | [assets/phase3_step1/FINDINGS.md](assets/phase3_step1/FINDINGS.md) | evidence bundle |
| Does the same core work on another instrument (triple-axis)? | [assets/in8_phase1/FINDINGS.md](assets/in8_phase1/FINDINGS.md) | evidence bundle |
| What the committed evidence bundle is and is not | [assets/README.md](assets/README.md) | live |
| Why was a decision made this way? | [DECISIONS.md](DECISIONS.md) — being absorbed into the owning design docs | terminal |
| The frozen campaign evidence bundles (data, retained forever) | [campaigns/README.md](campaigns/README.md) | live |
| The 2026-07-06 IN8 calibration record | [../calibration/README.md](../calibration/README.md) | terminal (fate on the closeout list) |
| The pre-EIG statistical roadmap | [ISAR_PRE_EIG3_STATISTICAL_CONSISTENCY_HANDOFF.md](ISAR_PRE_EIG3_STATISTICAL_CONSISTENCY_HANDOFF.md) | terminal |
| The full design rationale | [archive/](archive/) — indexed in [archive/README.md](archive/README.md): [PHASE_1](archive/PHASE_1_DESIGN.md), [PHASE_2](archive/PHASE_2_DESIGN.md), [PHASE_3](archive/PHASE_3_DESIGN.md) | terminal (being absorbed) |
| Why the workspace/session/CLI was designed this way | [UNIFICATION_DESIGN.md](UNIFICATION_DESIGN.md) — frozen: ~40 docstrings cite its §N | terminal (deleted after the docstring repair) |

Those three reports are **committed** under [assets/](assets/) — a clean checkout can read them and
look at their figures. What a clean checkout cannot do is *regenerate* them: the `docs/*_check.py`
scripts that produce them read the local, gitignored `test_data/`, and they write to the disposable
`out/` tree. [assets/README.md](assets/README.md) says exactly what was promoted, from which code
revision, and by which command. Every figure carries its own caption (drawn on the image and written
as a `<figure>.caption.md` sidecar), so a figure is understandable on its own.
