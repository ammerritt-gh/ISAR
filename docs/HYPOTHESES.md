# Excitation hypotheses — declaring what you expect to measure

> **Status:** live
> **Authority:** canonical for declaring expected excitations
> **Verified against code revision:** `d483af1` (2026-08-08)

> **Status: Implemented (branch hypotheses).** The branch schema, shipped presets, frozen objective fingerprint, parameter
> priors, branch-MI acquisition, opening moves, deterministic widening, incremental count batches,
> persisted mismatch evidence, and goal > card > shipped resolution are implemented (2026-07-17).
> `level` and `continuum` remain the explicitly declared future plugin surfaces (O10). Targeted-lane
> branch-MI acquisition is unchanged by SCENE-5. Scene-lane (`discover_and_map`) attachment of a
> declared `[hypotheses.*]` block to real-fit beliefs is **open**: the SCENE-5 acceptance battery
> found the mechanism live only against hand-set identities, never against a real fit, because the
> production catalogue declares no identity-conditioned priors (H23, `design/scene.md`'s SCENE-5
> closure amendment).

## What a hypothesis is (and is not)

An **excitation hypothesis** is your physics expectation about what the campaign is measuring —
"the target is an optic phonon with a zone-centre gap around 10 meV, roughly flat" — declared in
`card.policy.toml`, the hand-edited half of the experiment card. It is human intent, so code
never writes it.

A hypothesis **weights** the driver's Bayesian machinery; it never **forces** it:

- it *seeds* per-branch beliefs before any data exists, so a declared optic branch can claim a
  6 meV zone-centre feature instead of the acoustic belief absorbing it;
- it *shapes* where the first scans go, but only through the prices: the planner opens where its
  belief says the branch is both findable and worth finding, so a gapped target's zone-centre
  feature and a gapless target's first resolvable q come out of the scoring rather than a rule;
- it *informs* classification, as a prior over the branch labels — never as a veto.

A hypothesis is **not** a fit model, and it is **not** a catalogue entry. The phenomenon
catalogue (`isar/analyze/catalogue.py`, SCENE-1/H3) is the system's registry of what *can* be
present in any scan and drives the fitter's structure enumeration; an excitation hypothesis is
*your* declaration of which of those phenomena this campaign is chasing and where. The
deterministic fitter fits what the data supports; the hypothesis only tells the *steering* where
to look first and what the features it finds most plausibly are. If the data disagrees with your hypothesis, the system is designed to notice and
say so (see *When you're wrong*, below), not to fit your expectation anyway.

For a tracked branch, a stated energy is an **anchor identity prior**, not an instruction
to pick whichever peak lies nearest 6 meV at every q. Picking a feature independently at every q is
not something this driver measures: `tracked_branch` is the only selector, and a goal asking for the
lowest, highest, or nearest-energy feature at every q is refused at compile time rather than
compiled into a campaign whose prize the planner cannot map. `tracked_branch` jointly assigns the optic,
acoustic rival, new, and missing states over the ordered path. Only modes from a published pooled
master are authoritative track evidence; physical-fit modes remain inspectable diagnostics.
Assignments may be revised when a neighboring pooled master arrives.

**Delivery authority and tracking evidence are separate gates.** A pooled master whose members
disagree on ω₀ (`pool_consistent = false`) still fitted its modes — only their *agreement* is in
doubt — so its candidates are kept as identity and connectivity evidence for that q. What it does not
get is authority: it cannot deliver the point's ω₀, seat convergence, or refresh the dispersion prior,
each of which gates on consistency independently. Where a q holds both a consistent and an
inconsistent master the consistent one is preferred, rather than the inconsistent one excluded. This
split is load-bearing: dropping an inconsistent master's candidate outright empties that q's
observation, breaks the tracked trajectory, and retroactively collapses every confirmed point
downstream of it (AP8, ruling O29). Such a point stays connected but unconverged, and is
re-acquired to resolve the disagreement.

New natural-language and compact-file goals that ask to map a named branch along a multi-point path
default to `tracked_branch`, for acoustic and optic targets alike. Explicit “lowest at every q”,
“highest at every q”, and “nearest at every q” requests retain their local selectors. A frozen step
payload replays with the selector it recorded; the default never rewrites it. Acoustic tracking
anchors at the nearest
predicted-resolvable point to reduced q=0 and walks in both directions; it does not demand a measurable
nonzero acoustic energy at the exact zone centre.

## Declaring hypotheses in `card.policy.toml`

```toml
# The target and any rivals you expect to encounter. Each table is one hypothesis.
[hypotheses.optic]                 # start from the shipped "optic" preset…
zc_energy = { mode = 10.0, sigma = 2.0 }   # …and override: lower-truncated-normal gap (meV)
flatness = "high"                  # weak dispersion expected
confidence = 0.7                   # how strongly the prior weighs against data (0..1)

[hypotheses.acoustic]              # the expected rival; the preset alone is usually enough

# Something we don't ship a preset for — Tier 1: a new locally recognized hypothesis
[hypotheses.my_gapped_mode]
dispersion_type = "branch"
zc_energy = { mode = 4.0, sigma = 1.5 }
slope_zc = { mean = 0.0, sigma = 5.0 }     # meV per r.l.u., at the zone centre
confidence = 0.5
```

Custom branch hypotheses can seed local assignment beliefs and appear in diagnostic branch
evidence. End-to-end connected target tracking and selectable target authority currently
support only the acoustic and optic roles; the Γ/intensity rescue model is currently
acoustic-only.

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

Shipped presets: **`acoustic`** (gapless — zone-centre energy pinned to 0, and a **zero-mean**
slope prior whose sigma is derived from the objective's energy domain (`[0, instrument ceiling]`, the card's `max_energy_transfer_meV`; no per-branch search range exists since 2026-09-05): the shipped mean of
12 meV/r.l.u. was baking in the answer and was removed on 2026-09-02), **`optic`** (gapped;
if you give no `zc_energy` its mode is derived deterministically from the objective's energy
search band), **`null`** (agnostic — near-zero precision everywhere; the escape hatch for
exploratory work and the baseline the mismatch check compares against). Declaring
`[hypotheses.<preset-name>]` starts from that preset and applies your overrides; any other name
is a fully custom Tier-1 hypothesis.

## What the driver does with a hypothesis

- **The hypothesis map is the prior the planner scores against.** Every declared hypothesis becomes
  a branch belief; the EIG planner reads that belief's predictive at every grid point and ranks
  every `(Q, window, exposure)` by expected progress toward the prize per neutron
  (`design/eig.md`). There is no opening book, no connected frontier and no per-q ceiling: a wide
  window happens because the belief is wide, and the first scan is chosen from the prior alone.
- **Predictive.** The belief predicts `(mean, sigma)` for its branch at any q, with the class prior
  entering as parameter precision and weakening with distance from the fold, truncated to the
  declared energy domain. A cold branch far from the fold therefore predicts "somewhere in the
  band" rather than a confident wrong answer, and the planner's windows widen accordingly.
- **Assignment.** Every fitted peak gets a soft distribution over the declared branches plus a flat
  "something new" escape. A peak whose branch is decided (probability ≥ 0.85 and identity entropy
  ≤ 0.2 nats) anchors that branch's belief; an undecided one is evidence, not an anchor.
- **Evidence is not delivery.** Every current pooled master marks its q as measured and retains all
  fitted candidates, including a wrong-branch or near-equal-posterior result. Only a confirmed target
  fold publishes target `omega0`, enters the target prior, or certifies. The viewer shows ambiguous
  candidates as open markers without certifying whiskers or a connecting line.
- **Belief seeding.** Each declared hypothesis gets a branch belief carrying the hypothesis as a
  prior on the regression parameters (gap, slope) — *never* as fake data points (O8). With zero
  measurements the belief predicts from the prior with honest width; with data, the data wins.
- **Scan value.** The planner simulates the counts each candidate scan would produce under the
  current beliefs and the measured intensity map, and scores the expected number of grid points it
  would bring inside the bar, per neutron. Because the prize is one mutually exclusive posterior
  over "which peak here is the target", a scan that cannot tell two branches apart is worth less
  than one that can — which is what buys exposure where the branches approach, and buys rival
  measurements only as far as excluding the rival needs. Nothing is legislated per phenomenon.
- **Search bands.** The declared energy domain is a hard bound: the belief predictive is truncated
  to it, and no window is generated outside it. Windows themselves come from the belief moments and
  from each surviving lump of the reweighted scenario set — never from a widening schedule.

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

1. **New locally recognized branch hypothesis → TOML, no code.** A custom branch-shaped
   hypothesis — a gapped magnon, a soft mode — is a new parameter preset, declarable inline as
   above, and can participate in local assignment and diagnostic belief construction. Promoting
   it to a selectable, connected campaign target still requires extending the current
   acoustic/optic tracking machinery.
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

Resolution precedence is deterministic: an energy/gap stated in the natural-language goal wins;
otherwise a matching `[hypotheses.<target>]` card declaration wins; otherwise the shipped preset is
frozen. `hypothesis_source` records which source won. A frozen objective is the payload of an
`isar.campaign-step.targeted/1` step inside an `isar.campaign-program/1` campaign program; the
retired `isar.objective` and `isar.objective-envelope` documents are refused by design, with no
compatibility reader (rulings H1, H2).

## Viewer labels versus acquisition capability

Branch names in the viewer are presentation derived from persisted identity, never hard-coded fit
outcomes. A `confirmed` track with a known physical assignment is labelled `Acoustic`, `Optic`, and so
on; provisional or ambiguous identities receive stable `Branch N` labels. The active target is styled
solid and context/rival branches dashed. Viewer metadata tolerates no target role and then styles all
branches equally.

For an acoustic target the exact zone centre carries no prize at all: ω(q→0) → 0 is degenerate with
the elastic line, so that grid point is exempt from precision coverage on the geometry alone, from
the first iteration (`GridQ.precision_coverage_eligible`). It never publishes target `omega0`, seeds
the dispersion prior, or enters the precision denominator. An optic target has a finite energy there
and is measured like any other q.

This does not expand the current objective engine: arbitrary named targets continue to use the existing
target semantics, while target-free “map every visible dispersion” acquisition remains Planned. The
viewer tolerance is the compatibility door for that future mode, not an assertion that it can already
be requested.

---

*Why it is designed this way: `design/steering.md` §4.5 and rulings O6–O11.*
