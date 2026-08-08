# Excitation hypotheses — declaring what you expect to measure

> **Lifecycle:** Current
> **Authority:** canonical for declaring expected excitations
> **Provenance:** hand-written
> **Verified against code revision:** `d483af1` (2026-08-08)

> **Status: Implemented (branch hypotheses).** The branch schema, shipped presets, frozen objective fingerprint, parameter
> priors, branch-MI acquisition, opening moves, deterministic widening, incremental count batches,
> persisted mismatch evidence, and goal > card > shipped resolution are implemented (2026-07-17).
> `level` and `continuum` remain the explicitly declared future plugin surfaces (O10).

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

A hypothesis is **not** a fit model, and it is **not** a catalogue entry. The phenomenon
catalogue (`isar/analyze/catalogue.py`, SCENE-1/H3) is the system's registry of what *can* be
present in any scan and drives the fitter's structure enumeration; an excitation hypothesis is
*your* declaration of which of those phenomena this campaign is chasing and where. The
deterministic fitter fits what the data supports; the hypothesis only tells the *steering* where
to look first and what the features it finds most plausibly are. If the data disagrees with your hypothesis, the system is designed to notice and
say so (see *When you're wrong*, below), not to fit your expectation anyway.

For a tracked branch, a stated energy is an **anchor identity prior**, not an instruction
to pick whichever peak lies nearest 6 meV at every q. The global-nearest behavior remains available
only through the explicit `nearest_energy` selector. `tracked_branch` jointly assigns the optic,
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
downstream of it (AP8, DECISIONS O29). Such a point stays connected but unconverged, and is
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
- **Connected frontier.** Once the target branch's resolvable anchor is identified, acquisition moves
  one adjacent q at a time in both directions. An ambiguous point permits one provisional adjacent bridge, then may be revisited
  with evidence from both sides. An unresolved frontier cannot seed the dispersion prior, certify a
  farther point, or be crossed by a plotted branch line.
- **Evidence is not delivery.** Every current pooled master marks its q as measured and retains all
  fitted candidates, including a wrong-branch or near-equal-posterior result. Only a confirmed target
  fold publishes target `omega0`, enters the target prior, or certifies. The viewer shows ambiguous
  candidates as open markers without certifying whiskers or a connecting line.
- **Bounded deadlock.** The per-q production ceiling changes strategy rather than declaring absence:
  acquisition moves to an untried differentiation probe. Demonstrated exposure saturation plus an
  exhausted bridge/revisit allowance ends automatically as honest `incomplete` branch ambiguity.
- **Belief seeding.** Each declared hypothesis gets a branch belief carrying the hypothesis as a
  prior on the regression parameters (gap, slope) — *never* as fake data points (O8). With zero
  measurements the belief predicts from the prior with honest width; with data, the data wins.
- **Scan value.** The acquisition simulates incremental Poisson counts from every retained plausible
  master model and ranks mutual information about the joint target-energy/model/branch state. Expected
  ω-width reduction and identity-entropy reduction are reported separately rather than mixed with an
  arbitrary unit-conversion weight. That is what buys the cheap
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

Scan-window distinction policy is frozen separately under the objective's top-level
`scan_window_eig` block. Its authenticated policy-set and numerical-scoring fingerprints are
deliberately excluded from `hypothesis_fingerprint` and from the stable current-objective comparison
scope: changing a diagnostic partition or Monte Carlo count must not claim the excitation hypothesis
changed, and a policy may bind to the current campaign without recursively hashing itself. The
`scan_window_eig_mode` intent remains in the ordinary steering snapshot; card-only scope-binding
markers and authoring records never survive into a frozen objective.

The block is optional on read: a step payload that omits it materializes the authenticated
no-policy/default-scoring value, while new serialization always writes it. That default does not
merge current card intent into a frozen record — frozen payloads, programs, and resumes remain
self-contained, and explicit null or authoring markers are invalid.

Resolution precedence is deterministic: an energy/gap stated in the natural-language goal wins;
otherwise a matching `[hypotheses.<target>]` card declaration wins; otherwise the shipped preset is
frozen. `hypothesis_source` records which source won. A frozen objective is the payload of an
`isar.campaign-step.targeted/1` step inside an `isar.campaign-program/1` campaign program; the
retired `isar.objective` and `isar.objective-envelope` documents are refused by design, with no
compatibility reader (DECISIONS H1, H2).

## Viewer labels versus acquisition capability

Branch names in the viewer are presentation derived from persisted identity, never hard-coded fit
outcomes. A `confirmed` track with a known physical assignment is labelled `Acoustic`, `Optic`, and so
on; provisional or ambiguous identities receive stable `Branch N` labels. The active target is styled
solid and context/rival branches dashed. Viewer metadata tolerates no target role and then styles all
branches equally.

For an acoustic target, exact Γ may be persisted as `inferred_unresolved` when at least three
connected, authoritative low-q assignments support a smooth positive-slope extrapolation compatible
with zero. This is structural endpoint evidence, not a fitted mode: it never publishes target
`omega0`, seeds the dispersion prior, or enters the precision denominator. A lone Γ feature—such as
an unrelated finite-energy mode—cannot establish this status.

This does not expand the current objective engine: arbitrary named targets continue to use the existing
target semantics, while target-free “map every visible dispersion” acquisition remains Planned. The
viewer tolerance is the compatibility door for that future mode, not an assertion that it can already
be requested.

---

*Why it is designed this way: `MEASUREMENT_STEERING.md` §4.5 and DECISIONS O6–O11.*
