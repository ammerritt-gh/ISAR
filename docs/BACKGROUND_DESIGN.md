# Background Handling — workstream design

**Status: design approved 2026-07-19; implementation not started; sequenced after the
speedup sprint (`docs/SPEEDUP_DESIGN.md`),** whose fast test campaigns are what make this
workstream cheap to iterate. Companion: the audit that motivated it,
`docs/handoffs/HANDOFF_statistics_contract_checkup.md` §3.

Owner intent: real data has gnarly backgrounds — long elastic tails under the inelastic
signal, sloped/non-flat baselines — and today ISAR's only background model is a single
flat constant. ISAR does not need a deep background theory; it needs a modest, honest
family that stops the common real-data shapes from silently biasing phonon amplitudes,
plus the efficiency that comes from *choosing where to count*: learned background
knowledge shared across scans, and a driver that can deliberately buy background
information cheaply.

A `DECISIONS.md` entry is part of this workstream: background becomes an owned design
area (it currently has no decision ID anywhere).

---

## 1. Baseline (verified 2026-07-19, checkup §3)

- **One background model:** `ConstantBackground`, one parameter, attached to every
  enumerated structure (`isar/analyze/components.py:152-162`). Background is in the
  hypothesis space but has no complexity ladder — sloped is impossible, not penalized.
- **The elastic line is a resolution-width Voigt** (`components.py:110-126`) with no tail
  freedom; long elastic wings must today be absorbed by the flat constant or by biasing
  the phonon amplitude — the silent failure this workstream exists to remove.
- **`QuasiElastic`** exists as a card-gated, fixed-width broad component
  (`components.py:128-149`; e.g. `isar/parsers/cards/in8.policy.toml:28-32`) — unchanged
  by this workstream.
- Background amplitude for detectability checks is a median-of-counts heuristic
  (`isar/analyze/structures.py:78-88`); "second-phonon / sloped-BG remain later-step" is a
  code comment (`structures.py:134`), not a design decision.
- No background information crosses scans; no dedicated background estimation exists.
- Synthetic campaigns already parameterize flat background and elastic/phonon height
  ratios (`isar/synth/config.py:92-93`, `generate.py:34-78`) — the validation hooks exist
  but plant no structured backgrounds yet.

**Workstream target:** on synthetic campaigns with planted sloped backgrounds and long
elastic tails, fitted phonon amplitude and ω₀ bias return to the flat-background baseline
levels, with zero regression on existing flat-background campaigns and zero
steering-decision changes where backgrounds are genuinely flat.

---

## 2. Sub-phase A — The background model family

### A.1 New components

1. **`LinearBackground`** — intercept + slope in E. Enumerated as an alternative to
   `ConstantBackground` in the structure list, so AIC pays for the slope: the sloped
   variant must earn its extra parameter exactly the way a second phonon mode earns its
   three (STATS_PRIMER §2). Never both constants-and-slope in one structure.
2. **`ElasticTail`** — a broad component pinned to the elastic center, representing
   non-Voigt elastic wings (mosaic, air scatter, analyzer tails). Shape: single broad
   Lorentzian; amplitude free; width either card-declared (like `quasielastic_fwhm`) or
   free within a prior-bounded range. **Admissibility gates** (physics-gated, like every
   other component): only admissible when an elastic line is actually present in the
   window, and its width must exceed k× the instrumental resolution FWHM (k fixed at
   implementation, ~2–3) so it cannot collapse onto the elastic peak and trade intensity
   with it. Distinct from `QuasiElastic`: the tail is *instrumental/elastic* in origin and
   pinned to E=0 with the elastic line; QE is a physical signal component.

### A.2 Card declarations (human intent)

A `[background]` table in `card.policy.toml` — hand-edited, never written by code (card
ownership hard rule): which family members the instrument admits (`allow_slope`,
`allow_elastic_tail`), optional fixed tail width, optional bounds. Absent table → today's
behavior exactly (flat constant only). This is the same pattern as `quasielastic_fwhm`:
the scientist knows this beamline has sloped air scatter; the card is where that
knowledge lives.

### A.3 Selection discipline (unchanged machinery, wider menu)

Background variants enter the existing structure enumeration and are selected by AIC /
Akaike weights like any other model dimension. Two invariants:

- **AIC always sees the simple side:** the flat-constant variant is never pruned from the
  enumeration, whatever the card allows — otherwise selection silently biases complex
  (same rule as the speedup design's enumeration-pruning guard).
- **Over-flexibility is bounded by the gates, not by hope:** the admissibility gates above
  plus AIC's parameter penalty are the defense for "over-flexible backgrounds eat real
  signal" (the archived Phase-3 rationale for flat-only). If synthetic validation shows
  the tail still eats signal at low counts, the response is tightening the gates or the
  tail-width prior — not abandoning the family.

---

## 3. Sub-phase B — Cross-scan background learning

Background level per unit exposure at nearby q on the same instrument/configuration is
strongly correlated; at low counts the background level is *the* limiting nuisance. So:

- **The background prior** is a provenance-tagged record per (instrument, configuration,
  detector) — background rate normalized per unit monitor exposure per energy bin, with
  the same normalization-provenance discipline the steering design imposes on intensities
  (MEASUREMENT_STEERING §4.4: a raw fitted amplitude is not portable; a normalized one
  with provenance is). Slowly varying in q: nearest-neighbor pooling with a generous
  distance-decaying weight, not a global constant.
- **Consumption:** a soft Gaussian prior on the background parameters of a new fit,
  entering the optimizer's objective only — **the data-cost / posterior-cost split holds**
  exactly as MEASUREMENT_STEERING §4.4 prescribes: AIC, goodness-of-fit, and
  falsification are recomputed from the pure Poisson data cost *at the fitted parameters*,
  never from the prior-augmented objective (the same mechanism the dispersion prior
  already uses), and the
  fit is tagged (`fit_kind` gains a background-prior marker or reuses `soft_prior`
  provenance). Prior width decays with accumulated evidence but is floored so the prior
  nudges and never pins.
- **Admission gate** (mirrors fold-only prior discipline): only healthy fits feed the
  background prior — converged, `interval_reliable`, not flagged, and the background
  parameters not at bounds. A fit constrained by the background prior itself contributes
  only its *data-driven residual update*, with reduced weight, so the prior cannot
  self-reinforce (the §4.5 circularity trap, applied to backgrounds).
- **Storage:** machine-owned `card.state.json`, versioned alongside the calibration and
  dispersion-prior series. Nothing hand-edited.

---

## 4. Sub-phase C — Steering integration (buy background cheaply)

Because the driver chooses the energy window, background knowledge has a *price*, and it
is low:

- **Off-peak anchor bins.** When the background prior at a candidate q is loose
  (floor-width or absent), the proposed window includes a few deliberately off-peak bins —
  on the quiet side, outside the predicted signal and elastic regions — whose job is to
  pin the background level. When the prior is tight, the anchors are dropped and the
  window reverts to signal-tight. This composes with the existing adaptive window logic
  (`isar/drive/acquisition/acquire.py:274-327`) as a small additive term, not a rewrite.
- **EIG treats background as a nuisance to marginalize, not a target.** The
  goal-oriented/marginal-EIG design (MEASUREMENT_STEERING §4.1 item 1) already prescribes
  integrating nuisances out; this sub-phase widens the nuisance space to the background
  family, so scan value is information about ω₀ *after* background uncertainty, and a scan
  that mostly pins background is valued exactly as much as that pinning helps ω₀ — no
  separate background-scan mode, no hardcoded rule.
- **Provenance:** proposals record when and why anchor bins were added (loose-prior
  reason), so campaigns remain replayable and the behavior is visible in reports.

---

## 5. Validation

Synthetic-first, using the fast campaigns the speedup sprint delivers:

1. **Planted-background ensembles.** Extend `isar/synth` generation with sloped
   backgrounds and long elastic tails (parameters alongside the existing
   `bg_to_phonon_height_ratio` / `elastic_to_phonon_height_ratio`). Acceptance: amplitude
   and ω₀ bias vs truth at 15–100 peak counts comparable to the flat-background baseline;
   the checkup's silent-bias failure (tail counts absorbed into phonon amplitude)
   demonstrably removed.
2. **No-regression:** existing flat-background campaigns produce identical selections and
   decisions (flat constant still wins where it should; AIC evidence recorded).
3. **Learning curve:** across a multi-q campaign, the background prior demonstrably
   tightens and low-count fits stabilize (interval width and verdict quality at the
   low-count tail improve vs a no-learning control run).
4. **Steering economy:** campaigns with anchor bins reach tolerance in fewer total counts
   than signal-tight-only controls on planted-background truth; anchors demonstrably
   disappear once priors tighten.
5. **Identifiability test:** on cards where both are admissible, planted-truth ensembles
   with only-tail, only-QE, and both must show `ElasticTail` and `QuasiElastic` do not
   trade intensity at low counts (and that the width gate holds); if they do, the response
   is tightening the gates or requiring card-fixed tail width on those instruments.
6. Coverage spot-check: rerun the STATS_PRIMER coverage machinery on a planted-background
   cell to confirm interval calibration is not degraded by the wider model family.

---

## 6. Guardrails

- Card ownership split: `[background]` policy is human-owned; the learned prior is
  machine-owned state. Code never writes policy.
- Fit-vs-decide: background priors enter the optimizer objective only; AIC/GoF/
  falsification always read the pure Poisson data cost.
- Priors are parameter-precision, never pseudo-data; the background prior cannot feed on
  fits it constrained without down-weighting (anti-circularity).
- D3 variance convention, Baker–Cousins cost, derived anti-Stokes: untouched.
- All schema/report additions additive (frozen Stage II↔III contract).
- QC still masks, never deletes; nothing here discards data.

## 7. Out of scope (explicitly deferred)

- **Multiphonon continuum** and **paramagnetic/Q-T-lawed quasielastic** shapes — they
  belong behind the HYPOTHESES Tier-2 registry seam as coarse *named* hypotheses so the
  falsification layer can identify them rather than fit-a-phonon-anyway; deferred to the
  steering-physics workstream.
- Power-law backgrounds, spurion modeling, dedicated background *scans* (separate
  measurements rather than in-window anchor bins).
- The systematics-aware error model (the checkup's cross-cutting caveat) — this
  workstream shrinks the largest named systematic but does not build the general model.

## 8. Sequencing within the workstream

A → B → C, each with its validation step before the next: the model family must exist
before there is anything to learn; the prior must exist before steering has anything to
consult. A is independently shippable (real cards can declare backgrounds immediately);
B and C each end with a planted-background campaign comparison committed under
`docs/campaigns/`.

---

*Approved by Adrian 2026-07-19 (scope: sloped + elastic-tail family, cross-scan learning,
steering integration in; multiphonon/paramagnetic out). Motivated by checkup concern 3.*
