# Interpreting an ISAR verdict — what should I do with this scan?

> **Lifecycle:** Current
> **Authority:** canonical for what a verdict means and what to do with it
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)

Every scan ISAR processes gets exactly one **verdict**. The verdict is not a grade; it's a routing
decision that tells you **where to spend your manual time**. Nothing is ever deleted — a declined scan
keeps its raw data and is surfaced to you with its reason.

## The two questions a verdict answers

1. **Did ISAR hand me a usable number?** ("delivered" = `resolved` + `flagged`.)
2. **If not, why — and what should I do about it?**

## The verdicts

### Delivered — ISAR gave you an energy

- **`resolved`** — a clean, confident fit. *Use the number* (you can still confirm it). These are the
  scans you don't have to touch first.
- **`flagged`** (`model_incomplete`) — there's a usable ω₀, but the model is imperfect: the fit left a
  *structured* residual (often a broad quasi-elastic component the elastic+DHO+background model doesn't
  capture). The energy is probably fine; *glance at it* before relying on it. This is not a rejection —
  it's "good number, imperfect model."

### Declined — ISAR is steering you to look

- **`junk`** — a fit was attempted but the result isn't trustworthy: catastrophic goodness-of-fit, or a
  large *structureless* residual. *Look manually.* Note ISAR is **conservative** here: it sometimes
  lands a correct energy in `junk` because its automatic fit (grid + non-negative least squares) is
  worse than a hand fit would be. So a `junk` scan is worth a look, not a delete.
- **`unconstrained`** — the data don't pin the parameters down (e.g. two structures fit equally well
  with nothing to choose between them, or there's no real peak to constrain). *Look manually / consider
  recounting.*
- **`held_for_calibration`** — ISAR is missing something it needs first, almost always the **energy
  zero** (no usable elastic line and no nearby calibration scan to borrow one from). *Resolve the
  calibration, then re-run* — it never silently fits with the zero at 0.
- **`underpowered`** — too few counts to constrain a fit yet. This verdict is **provisional**: it is a
  statement about detection power given the current counts, and it never marks a region as
  confirmed-empty. *Recount if the point matters.*
- **`no_signal`** — no peak detectable above background. *Skip, or recount much longer.*

## The confidence score

Delivered scans also carry a **confidence** in [0, 1] — a geometric mean of sub-scores (structure
agreement, how tight the interval is, whether the bootstrap and curvature error bars agree, how far the
fit sits from the dispersion prior, and provenance). Use it as relative triage guidance: higher means
ISAR is more sure. On the gold set, confidence is meaningfully higher when ISAR's energy is close to the
human's than when it's far — i.e. confidence tracks accuracy. (It is *not* a calibrated probability;
don't read 0.8 as "80% chance correct.")

## Fork vs fold

When ISAR pools the candidate models it reports the shape of the answer:

- **fold** — a single agreed energy (the normal case).
- **fork** — two or more equally-supported energies, reported *separately*. A fork is information: the
  data are genuinely ambiguous, and ISAR is refusing to average two real possibilities into one wrong
  number. *Look at both.*

## A scan verdict does not stop a campaign

During an autonomous campaign, individual scan fits are provenance and troubleshooting aids. They do
not vote on the final ω, branch identity, convergence, or whether to abandon a q-point. ISAR combines
all accepted raw counts at the same `(q,T)` first and assesses that pooled **master**. A string of
`junk`, `underpowered`, `no_signal`, or ambiguous individual fits therefore cannot terminate the point
while the master may still improve with more information.

The master browser is the decision view: it shows the combined counts and fit, model-weighted ω
interval, member consistency, branch/track state, linewidth state, members, revision, and expected next
information. Campaign states are `met`, `incomplete`, or independently proven `infeasible`. “Poor fit”
and “low gain” mean continue until a configured global limit, not “unmeetable.”

Final certification does not reopen the individual verdicts. It freezes one job per requested master,
reuses an exact full/Bayes master when available, and otherwise checkpoints that master as soon as it
finishes. Until every requested master has a durable unit and the hash-bearing publication marker is
written, the GUI says `checkpointed` rather than implying that a partial generation is final.

## The one promise

**Flag, don't delete.** Whatever the verdict, the raw data stays and the reason is recorded. ISAR's job
is to sort your scans into "trust it," "glance at it," and "look here" — never to throw your data away.

See [GLOSSARY.md](GLOSSARY.md) for definitions and [READING_GUIDE.md](READING_GUIDE.md) for the big
picture.
