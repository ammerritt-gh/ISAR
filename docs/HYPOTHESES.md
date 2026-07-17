# Excitation hypotheses — declaring what you expect to measure

> **Status: Partial.** The branch schema, shipped presets, frozen objective fingerprint, parameter
> priors, branch-MI acquisition, opening moves, deterministic widening, incremental count batches,
> and persisted mismatch evidence are implemented (2026-07-17). Card tables parse and validate;
> resolving goal overrides against card declarations is the remaining user-facing gap.

## What a hypothesis is (and is not)

An **excitation hypothesis** is your physics expectation about what the campaign is measuring —
"the target is an optic phonon with a zone-centre gap around 10 meV, roughly flat" — declared in
`card.policy.toml`, the hand-edited half of the experiment card. It is human intent, so code
never writes it.

A hypothesis **weights** the driver's Bayesian machinery; it never **forces** it:

- it *seeds* per-branch beliefs before any data exists, so a declared optic branch can claim a
  6 meV zone-centre feature instead of the acoustic belief absorbing it;
- it *shapes* the opening moves (an optic target starts at the zone centre at positive energy —
  scanning the Bragg peak itself tells you nothing; an acoustic target starts at the smallest q
  where its predicted energy is resolvable);
- it *informs* classification, as a prior over the branch labels — never as a veto.

A hypothesis is **not** a fit model. The deterministic fitter fits what the data supports; the
hypothesis only tells the *steering* where to look first and what the features it finds most
plausibly are. If the data disagrees with your hypothesis, the system is designed to notice and
say so (see *When you're wrong*, below), not to fit your expectation anyway.

## Declaring hypotheses in `card.policy.toml`

```toml
# The target and any rivals you expect to encounter. Each table is one hypothesis.
[hypotheses.optic]                 # start from the shipped "optic" preset…
zc_energy = { mode = 10.0, sigma = 2.0 }   # …and override: zone-centre gap, half-normal (meV)
flatness = "high"                  # weak dispersion expected
confidence = 0.7                   # how strongly the prior weighs against data (0..1)

[hypotheses.acoustic]              # the expected rival; the preset alone is usually enough

# Something we don't ship a preset for — Tier 1: a new character is data, not code
[hypotheses.my_gapped_mode]
dispersion_type = "branch"
zc_energy = { mode = 4.0, sigma = 1.5 }
slope_zc = { mean = 0.0, sigma = 5.0 }     # meV per r.l.u., at the zone centre
confidence = 0.5
```

Field reference (all optional unless noted; unset fields inherit the preset or an
uninformative default):

| Field | Meaning |
|---|---|
| `preset` | Optional shipped base (`"acoustic"`, `"optic"`, or `"null"`). A table named for a shipped preset inherits it automatically. |
| `dispersion_type` | `"branch"` (an E(q) curve) / `"level"` (flat in q, e.g. crystal-field) / `"continuum"`. **First slice implements `branch` only**; the others are declared interface (O10) and are rejected with a clear error until their plugins exist. |
| `zc_energy` | Zone-centre energy prior: `{ mode, sigma }` (or `{ mean, sigma }`), truncated at zero — it can never imply a negative energy. The acoustic preset has mean 0. |
| `slope_zc` | Prior on the dispersion slope at the zone centre (mean, sigma). |
| `flatness` | `"low"` / `"high"` — shorthand that scales the slope-prior sigma. |
| `confidence` | 0..1; scales the prior's precision. Treat it as "how much data would it take to talk me out of this." The prior's influence decays automatically as real data accumulates regardless. |

Shipped presets: **`acoustic`** (gapless — zone-centre energy pinned to 0), **`optic`** (gapped;
if you give no `zc_energy` its mode is derived deterministically from the objective's energy
search band), **`null`** (agnostic — near-zero precision everywhere; the escape hatch for
exploratory work and the baseline the mismatch check compares against). Declaring
`[hypotheses.<preset-name>]` starts from that preset and applies your overrides; any other name
is a fully custom Tier-1 hypothesis.

## What the driver does with a hypothesis

- **Opening book.** The first scans probe the hypothesis's most discriminating feature — for a
  gapped target, the zone centre at the claimed gap ± 2σ (the *sanity-check opening move*, O11).
  A grossly wrong prior is caught in the first scans, not at the end of the night.
- **Belief seeding.** Each declared hypothesis gets a branch belief carrying the hypothesis as a
  prior on the regression parameters (gap, slope) — *never* as fake data points (O8). With zero
  measurements the belief predicts from the prior with honest width; with data, the data wins.
- **Scan value.** The acquisition ranks scans by information about the target's energy *plus* a
  λ-weighted term for reducing branch-label uncertainty (O6). That is what buys the cheap
  "establish what the zone-centre feature is" scan, widens a window over a rival where branches
  approach, and stops spending on a rival once it is confidently labeled not-the-target. Rivals
  are only ever measured to a looser *differentiation tolerance* — enough to tell them apart
  from the target, never publication error bars.
- **Search bands.** Energy windows derive from the hypothesis's predicted energy and widen on a
  deterministic schedule when the expected signal is absent (O11).

## When you're wrong (this is expected)

The hypothesis may be wrong; the design treats that as a first-class outcome, not an edge case:

- the sanity-check opening move tests the most falsifiable claim first;
- if the expected signal is absent, search bands widen deterministically rather than the driver
  re-scanning the same empty window;
- a posterior-predictive check compares the prior's prediction against a data-only prediction;
  disagreement is flagged in machine-owned `card.state.json` and stated plainly in the report;
- a feature matching *none* of your hypotheses is banked as an agnostic branch (the assignment
  posterior's `new` class) with wide priors and reported: "a third feature near 8 meV matches
  none of your hypotheses." After the beamtime, look at it, name it, write a Tier-1 preset for
  it — the next campaign steers with it.

## Extending the system (the three tiers, O10)

1. **New character → TOML, no code.** Anything branch-shaped — a gapped magnon, a soft mode — is
   a new parameter preset, declarable inline as above.
2. **New machinery → registry plugin.** A `continuum` is not a curve; a crystal-field `level`
   needs the slope prior disabled; a new classification feature (intensity-vs-Q, polarization)
   must be supplied by the fitter. Each `dispersion_type` is a registry entry implementing a
   small interface (prior contribution, likelihood features, steering defaults) — an addition,
   never a rebuild.
3. **The truly unknown → the agnostic track.** Needs no declaration at all; see above.

## Reproducibility

The resolved hypothesis set (parameters + preset versions) is content-hashed into
`card.state.json` at campaign start. Steering is deterministic given the workspace and the
policy file; presets are part of that chain, so a preset edit is a visible fingerprint change,
never a silent behavior change.

The frozen objective stores `hypotheses`, `target_hypothesis`, `hypothesis_source`, `steering`, and
`hypothesis_fingerprint`. Machine state stores the snapshot under `hypothesis_snapshot`, the
prior-vs-data-only check under `hypothesis_mismatch`, drive regression ingredients under
`branch_belief_models`, and viewer/report rows under `branch_belief`. Each viewer row carries
`label_probabilities`, `assignment_entropy`, and `p_is_target`; there is no ambiguous
`target_probability` field.

---

*Why it is designed this way: `MEASUREMENT_STEERING.md` §4.5 and DECISIONS O6–O11.*
