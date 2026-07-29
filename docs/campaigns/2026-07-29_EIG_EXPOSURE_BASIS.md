# EIG-0 — exposure-basis validation spike

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written

**Reviewed commit:** `f19e819d8ccaad9a2f23292ca161c536ee275319` (`f19e819`, worktree
`.claude/worktrees/agent-a345d415f2db46ffc`, branch `main`, clean at start).
**Date:** 2026-07-29. **Instrument:** live TAVI at `127.0.0.1:8642`, deterministic engine,
`noiseless: true`, `isolated: true`, frozen **disabled** background (`catalog_version: 2`).
**Nothing committed from the spike itself; this note is the retained record.**

---

## 1. Declared tolerances (fixed before any run, verbatim)

- **T1 internal parity (regression only, not validation):** direct manifest evaluation at
  `reference_incremental_exposure` vs stored `/1` `expected_counts` — max relative residual ≤ 1e-9.
- **T2 independent basis:** `fit_reference_exposure` derived from the pooled ledger (ΣM, or whatever
  the amplitude basis actually is — determining WHICH is part of the spike) must reproduce the fit's
  `exposure_scale` semantics — rel ≤ 1e-6.
- **T3 unit mapping:** noiseless TAVI scans at two different `number_neutrons` levels; the ratio of
  realized monitor (and of measured expected counts) between the two levels must match the requested
  ratio — rel ≤ 1e-3 (instrument rounding allowance).

---

## 2. What the exposure basis actually is (derived from the ledger, before opening `/1`)

Read from source, then confirmed numerically:

1. `isar/reduce/collate.py::_merge_group` (~333–360) bins every member's energies onto the union
   grid at the auto tolerance and sums **kept** counts and **kept** monitors separately —
   `MergedSpectrum.monitor = ΣM` **per bin**, `mask = (ΣM != 0)`. A point a member did not measure,
   or that its own mask rejected, contributes `0` to both sums. Nothing is per-member normalized.
2. `isar/session/driver.py:3051-3053` builds the master fit's exposure:
   `exposure_scale, _reference_exposure = normalized_exposure(monitor, mask)`.
3. `isar/analyze/context.py:29-44` (`normalized_exposure`) returns
   `scale_i = ΣM_i / E_ref` with **`E_ref = median(ΣM over valid kept bins)`** — and the driver
   **discards** that reference into `_reference_exposure`.
4. `isar/analyze/components.py:204-212` (`CompositeModel.evaluate`) multiplies the model mean by
   `ctx.exposure_for(x)`, i.e. by `scale_i`. So a fitted amplitude means **"counts per `E_ref`
   monitor counts"**, and the exposure the fit saw is **per-bin**, not scalar.

So the contract quantity is:

```
fit_reference_exposure          = median_i( ΣM_i )   over the fit's kept bins   [monitor counts]
reference_incremental_exposure  = fit_reference_exposure / n_members
```

This was computed for every pool **from the member scan JSONs re-parsed independently** (rebinned
with the collation tolerance rule) *before* any `/1` artifact was opened. In every one of the 11
pooled masters analyzed, the independently reconstructed per-bin ΣM matched the persisted
`data/pooled/<digest>.json` `monitor` array exactly (`ledger_monitor_matches_pooled = true`, rtol
1e-12), and the reconstructed grid matched the pooled grid. That is the non-circularity evidence.

**`/1`'s `÷ n_members`** (`driver.py:1663-1664`) is arithmetically identical to evaluating the
composite at a *uniform* exposure of `fit_reference_exposure / n_members` — but only when `ΣM_i` is
constant across the grid, which is the whole question.

---

## 3. Campaign configuration and pool matrix

Sample `Al_phonon_DFT`, `H,K,L = 2.2, 0, 0`, TAVI card (`isar/parsers/cards/tavi.policy.toml`,
`[collation] merge = "merge"`), one workspace per case, then `isar run`. All direct scans
deterministic + noiseless with the fixed seeds below.

| case | member scans (`scan_command1` @ `number_neutrons`, seed) | job ids |
|---|---|---|
| `c0_single` | `deltaE 0.5 4.0 0.25` @ 1e9 (101) | j-0006 |
| `c1_same_grid_same_exposure` | `0.5 4.0 0.25` @ 1e9 (201); `0.5 4.0 0.25` @ 1e9 (202) | j-0007, j-0008 |
| `c2_same_grid_unequal_exposure` | `0.5 4.0 0.25` @ 1e9 (301); `0.5 4.0 0.25` @ **4e9** (302) | j-0009, j-0010 |
| `c3_partial_overlap_equal_exposure` | `0.5 3.0 0.25` @ 1e9 (401); `2.0 4.5 0.25` @ 1e9 (402) | j-0014, j-0015 |
| `c4_partial_overlap_unequal_exposure` | `0.5 3.0 0.25` @ 1e9 (501); `2.0 4.5 0.25` @ **4e9** (502) | j-0016, j-0017 |
| `c5_interleaved_steps` | `0.5 4.0 step 0.25` @ 1e9 (601); `0.5 4.0 step 0.50` @ 1e9 (602) | j-0018, j-0019 |
| `c6_masked_member` | `0.5 4.0 0.25` @ 1e9 (701); same @ 1e9 (702) with points 0–4 marked invalid | j-0020, j-0021 |
| `c7_live_campaign` | `run_campaign`, `ObjectiveSpec.along_h(2.1→2.4, step 0.1, tol 0.5 meV, budget 1e13 n, tier deterministic)`, **seed 11**, `max_iters` 80, background frozen disabled | driver-generated (scans 22–32) |

`c7` terminated `met` ("all 4 measurable grid q converged: a certified ω₀ interval … ≤ 0.5 meV at
each point") and produced 4 pooled masters with `n_members` 4, 2, 2, 3.

Notes on the partial-support cases:

- **TAVI refuses to execute a partially infeasible scan.** `POST /scan` returns
  `HTTP 400 infeasible_points` with `"partial": true` whenever any requested point fails the
  scattering triangle (verified live at `H=3.3`, `deltaE -6…4`). An *instrument-side* partial
  execution therefore cannot reach ISAR from TAVI: `executed_feasible_mask` is all-true for every
  accepted scan. `c6` represents partial support the way ISAR's own parser does — a member with
  `valid_mask_1=false` / `counts=null` on a contiguous block, which is the identical representation
  produced by point QC rejection (`isar/parsers/tavi.py:362-380`).
- `c3`/`c4` (window overlap) and `c5` (differing step on the same window) are the driver-reachable
  heterogeneity shapes; `c5` is precisely what EIG's own varied windows will create.

---

## 4. Results

### 4.1 T3 — the exposure unit

Same window `deltaE 0.5 4.0 0.25`, same seed 9101, noiseless, three requested levels:

| levels compared | requested ratio | max rel error, realized monitor | max rel error, expected counts |
|---|---|---|---|
| 1e9 → 4e9 | 4.0 | **0.0** | **0.0** |
| 1e9 → 0.25e9 | 0.25 | **0.0** | **0.0** |

Realized `ScanRecord.normalization.monitor` is *identically* the requested `number_neutrons` at
every point (one unique value per scan: 1e9, 4e9, 2.5e8). Noiseless expected counts scale exactly
linearly. **T3 PASSES at residual 0.0** (tolerance 1e-3).

### 4.2 T1 / T2 per pool

`ΣM` lists the distinct per-bin summed-monitor values; `support` the distinct per-bin member counts.
T1/T2 are the max over all retained models and all draws in the `/1` artifact.

| case / master | n_members | kept bins | ΣM per bin (distinct) | support | `fit_reference_exposure` | `reference_incremental_exposure` | **T1** max rel / max abs | **T2** max rel / max abs |
|---|---|---|---|---|---|---|---|---|
| c0_single | 1 | 15 | {1e9} | {1} | 1e9 | 1e9 | **0.0** / 0.0 | **0.0** / 0.0 |
| c1 same grid, same exposure | 2 | 15 | {2e9} | {2} | 2e9 | 1e9 | **0.0** / 0.0 | **0.0** / 0.0 |
| c2 same grid, unequal exposure | 2 | 15 | {5e9} | {2} | 5e9 | 2.5e9 | **0.0** / 0.0 | **0.0** / 0.0 |
| c3 partial overlap, equal exposure | 2 | 17 | {1e9, 2e9} | {1,2} | 1e9 | 5e8 | **0.500** / 318.16 counts | **0.0** / 0.0 |
| c4 partial overlap, unequal exposure | 2 | 17 | {1e9, 4e9, 5e9} | {1,2} | 4e9 | 2e9 | **3.000** / 1091.57 counts | **0.0** / 0.0 |
| c5 interleaved steps | 2 | 15 | {1e9, 2e9} | {1,2} | 2e9 | 1e9 | **1.000** / 329.83 counts | **0.0** / 0.0 |
| c6 masked member | 2 | 15 | {1e9, 2e9} | {1,2} | 2e9 | 1e9 | **1.000** / 212.59 counts | **0.0** / 0.0 |
| c7 master `(42,0,0)` | 4 | 97 | {1e8, 1.1e8, 2e8, 2.1e8} | {1,2,3} | 1e8 | 2.5e7 | **0.524** / 24.32 counts | **0.0** / 0.0 |
| c7 master `(44,0,0)` | 2 | 32 | {1.1e8} | {2} | 1.1e8 | 5.5e7 | **0.0** / 0.0 | **0.0** / 0.0 |
| c7 master `(46,0,0)` | 2 | 63 | {1e7, 1e8, 1.1e8} | {1,2} | 1e8 | 5e7 | **9.000** / 22.06 counts | **0.0** / 0.0 |
| c7 master `(48,0,0)` | 3 | 98 | {1e7, 1e8, 1.1e8, 2e8} | {1,2} | 1e8 | 3.33e7 | **9.000** / 11.51 counts | **0.0** / 0.0 |

- **T2 passes everywhere at exactly 0.0** (tolerance 1e-6). The ledger-derived
  `fit_reference_exposure` together with the ledger's own per-bin `ΣM_i` reproduces every stored
  `/1` expected-count matrix bit-for-bit, for every model and every draw, in all 11 masters. The
  exposure basis is *identified*: `E_ref = median(ΣM)` in **monitor counts**, applied per bin as
  `ΣM_i / E_ref`.
- **T1 passes only where ΣM is constant across the grid** (c0, c1, c2 and one of four c7 masters).
  Everywhere else it fails by **50 % to 900 %** — nine orders of magnitude outside 1e-9.

### 4.3 Per-bin heterogeneity characterization

The scalar path's per-bin ratio (`scalar prediction / stored /1`) is exactly `E_ref / ΣM_i` — a step
function of the bin's member support and per-member exposure, with **no draw and no model
dependence** (identical T1 for every retained model in every case):

- **c3** (grid 0.5…4.5 step 0.25): ratio `1.0` on the 12 single-member bins, `0.5` on the 5 overlap
  bins (2.0…3.0).
- **c4**: `4.0` on the six low-window-only bins (ΣM 1e9), `0.8` on the five overlap bins (ΣM 5e9),
  `1.0` on the six high-window-only bins (ΣM 4e9) — three distinct exposure regimes.
- **c5**: `1.0, 2.0, 1.0, 2.0, …` alternating bin by bin — the sharpest shape, and the one EIG's own
  candidate families will produce.
- **c6**: `2.0` on the five bins the masked member did not supply, `1.0` elsewhere.
- **c7 `(48,0,0)`**: four regimes spanning ΣM 1e7 → 2e8, a **20×** dynamic range within one master.

**No single scalar can repair this.** The floor on the max relative residual achievable by *any*
scalar is `√(ΣM_max/ΣM_min) − 1` (attained by the geometric-mean scalar):

| master | ΣM span | best single scalar | floor on max rel residual |
|---|---|---|---|
| c3 | 2.0× | 1.414e9 | **0.414** |
| c4 | 5.0× | 2.236e9 | **1.236** |
| c5 | 2.0× | 1.414e9 | **0.414** |
| c6 | 2.0× | 1.414e9 | **0.414** |
| c7 `(42,0,0)` | 2.1× | 1.449e8 | **0.449** |
| c7 `(46,0,0)` | 11.0× | 3.317e7 | **2.317** |
| c7 `(48,0,0)` | 20.0× | 4.472e7 | **3.472** |

Only a **per-energy exposure vector** reproduces the fitted grid on a heterogeneous pool.

### 4.4 The current driver already produces heterogeneous pools — this is not an EIG-only problem

This is the finding that most changes the plan's assumptions. `c7` is a plain campaign on today's
code, where every realization is the single hard-coded `legacy_uniform` scan:

**3 of 4 pooled masters (75 %) are exposure-heterogeneous**, with ΣM spans of 2.1×, 11× and 20× and
T1 residuals of 52 %, 900 % and 900 %.

The causes are ordinary current behaviour, not EIG: the driver changes the window between iterations
at the same Q (target-support adjustment, missed-window recovery, differing search vs. target
windows) and changes the per-point count request (dynamic exposure — ΣM values of 1e7 and 1e8 in the
same master come from two different `number_neutrons` presets). Master `(42,0,0)` has
`n_members = 4` but **no bin covered by more than 3 members**.

So an eligibility gate that routes heterogeneous masters to legacy would, on this campaign, disable
authoritative EIG on three quarters of the pooled masters **before EIG contributes any heterogeneity
of its own**. ADR-007's conditional-go branch, taken literally, ships a feature that is off almost
everywhere.

### 4.5 A second, independent defect: the median reference is not stable

`E_ref = median(ΣM)` is a rank statistic over a multimodal distribution once the pool is
heterogeneous. In **c5** (8 bins at ΣM 2e9, 7 at 1e9) removing any single bin moves the reference
between `2.0e9` and `1.5e9` — a 25 % jump in the declared amplitude basis
(`median_reference_stable = false`). c3/c4/c6/c7 happened to be stable only because their mode split
was lopsided.

This is harmless for *prediction* — the forward prediction is exactly invariant to the choice of
reference, because a different `E_ref′` rescales the fitted amplitudes by exactly the reciprocal
factor and `μ(x)·E/E_ref` is unchanged — **provided the published number is the one the fit actually
used**. It is *not* harmless if `/2` recomputes the reference from the pooled spectrum at
publication time on a different kept set, or if anything fingerprints or compares
`fit_reference_exposure` across master revisions.

### 4.6 Scope note — where an exposure basis exists at all

The pooled merge reads `ref.normalization.monitor` only: there is **no time fallback** on the pooled
path, unlike the per-scan `select_exposure` (`context.py:46-71`, monitor then time). When no usable
monitor exists, `normalized_exposure` returns `(None, None)`, the master fits on the legacy uniform
path, amplitudes mean "counts per point", and **there is no exposure basis at all** — the genuine
`missing_exposure_basis` case, not a bug. `/1` is produced only for pooled masters
(`predictive_n_members` is passed at `driver.py:3087` and nowhere else), confirmed by c0 where the
per-scan row carries no `predictive_ref`.

---

## 5. Exposure-unit conclusion

**TAVI `number_neutrons` *is* the predictive exposure unit; no adapter is needed today, but the
conversion seam should still be declared.**

- Realized per-point monitor ≡ requested `number_neutrons`, exactly (T3, rel error 0.0). The
  proportionality constant is **1.000** and the unit is **monitor counts**.
- `fit_reference_exposure` is in the same unit: for a homogeneous pool of `n` members each requested
  at `N` neutrons/point, `fit_reference_exposure = n·N` (c1: 2×1e9 = 2e9; c7 `(44,0,0)`: 2×5.5e7 =
  1.1e8).
- `reference_incremental_exposure = fit_reference_exposure / n_members` is the **mean** per-member
  exposure, not any member's request. On equal-exposure pools it coincides with the request (c1:
  1e9); on c2 (1e9 + 4e9) it is **2.5e9**, a value no member ever requested. A `/2` consumer must
  not read it as "the exposure of one scan".
- Point-neutron cost (points × requested per-point exposure) is therefore directly commensurate:
  for candidate exposure `E` neutrons/point the evaluator's ratio is `E / fit_reference_exposure`,
  both sides in monitor counts. **No request-to-predictive adapter is required for TAVI.** A real
  instrument whose recorded monitor is not the requested preset would need one, so `/2` should carry
  `unit`, `request_unit` and `request_to_exposure_factor` (`monitor_counts`, `number_neutrons`,
  `1.0`) even though the map is currently the identity.

---

## 6. Partial-support ledger signature (what an eligibility gate must key on)

Everything a gate needs is already available at `/2` production time: `MergedSpectrum.monitor`
(ΣM per bin), `MergedSpectrum.mask`, `members`, and the fit's kept mask
(`mask & (exposure_scale > 0)`, `driver.py:3053-3054`).

**Exact predicate:**

```
exposure_homogeneous  ⟺  max(ΣM_kept) / min(ΣM_kept) - 1 <= 1e-12,
                          with ΣM_kept non-empty, finite and > 0
```

Record all three fields, because the first is the cause and the others the symptom/magnitude:

- `full_member_support` — `support_count_i == n_members` for every kept bin `i`;
- `exposure_homogeneous` — as above;
- `exposure_ratio_span` — `max(ΣM_kept)/min(ΣM_kept)`.

Observed: eligible group (c0, c1, c2, c7 `(44,0,0)`) → homogeneous `true`, full support `true`,
span `1.0`. Ineligible group (c3, c4, c5, c6, c7 `(42,0,0)`, `(46,0,0)`, `(48,0,0)`) → homogeneous
`false`, full support `false`, span `2.0 / 5.0 / 2.0 / 2.0 / 2.1 / 11.0 / 20.0`. The predicate
separates the matrix with no false positives or negatives.

**`n_members` alone is NOT a usable gate** — c1/c2 (`n_members = 2`) are eligible while c3–c6
(`n_members = 2`) are not, and c0 (`n_members = 1`) is eligible. Neither is "members share a
window": c5's members share the window and differ only in step. Neither is "one member is
partially masked": c6 and c3 have identical ledger signatures despite different causes — which is
the right outcome, since the contract depends on the ΣM profile and not on why it varies.

---

## 7. Verdict

### **CONDITIONAL GO** — the scalar survives, the contract's *validation clause* and ADR-007's *ineligibility action* do not

Precisely:

1. **The scalar `fit_reference_exposure` is a sound forward-prediction basis on every pool tested,
   heterogeneous ones included.** Fitted amplitudes are a rate — counts per `E_ref` monitor counts —
   and combine-before-divide with per-bin exposure in the model mean is the statistically correct
   treatment, so `μ(x)·E/fit_reference_exposure` for a **new uniform-exposure candidate scan** (the
   only thing the EIG evaluator ever needs) is correct arithmetic regardless of the pool's support
   profile. Nothing measured here refutes that.
2. **What fails is the contract's self-consistency clause**, which asks the scalar to reproduce the
   stored fitted-grid matrix whose exposure profile was *not* uniform. It fails on 7 of 11 masters
   by 50 %–900 %, and no scalar can do better than 41 %–347 % on those pools (§4.3). That clause
   must be restated, not the basis.
3. **ADR-007's conditional-go action — "authoritative EIG stays disabled for heterogeneous masters"
   — should not be adopted as written.** §4.4 shows 75 % of the masters in an ordinary present-day
   campaign are heterogeneous, so that rule ships a feature that is almost always off, and it does
   so on the strength of a verification artefact rather than a statistical defect. Recommend
   instead:
   - `/2` publishes the scalar **and** the per-energy exposure vector
     (`exposure_basis.per_energy_monitor`, aligned to `energy_meV`), so the fitted-grid parity
     clause is checkable on every master. Cost: one float array already in memory.
   - `scalar_exposure_eligible` (the §6 predicate) is published as a **declaration and diagnostic**,
     and gates only the *scalar fitted-grid parity assertion* — not candidate evaluation.
   - If the plan owner still wants a hard trust gate on heterogeneity, gate on
     `exposure_ratio_span` above a declared threshold rather than on exact homogeneity, and record
     the expected disable rate (75 % at span > 1.0; 50 % at span > 3; 25 % at span > 15 on this
     campaign) before choosing it.
4. **Mandatory regardless of (3):** `fit_reference_exposure` must be the value the fit itself used,
   persisted, never recomputed (§4.5). Today it is thrown away at `driver.py:3051`.

If the plan owner rejects (3) and keeps ADR-007's literal ineligibility action, the honest label for
this spike is **NO-GO on the scalar as the operative contract** — because under that rule the
scalar contract cannot serve the masters EIG is meant to steer, and `/2` should carry a per-energy
exposure vector from the start. The two readings differ only in whether the per-energy vector is
published now or later; both require publishing it eventually, so publishing it in PR-1 is the
low-regret choice.

**Owner disposition (2026-07-29, ADR-017):** recommendation §7.3 above was adopted as written — `/2`
publishes both the scalar and the per-energy exposure vector, `scalar_exposure_eligible` /
`exposure_homogeneous` are declared as diagnostics, and there is **no heterogeneity gate on
candidate evaluation**. See §8 for the resulting contract-text repairs and ADR-017 in
`docs/DECISIONS.md` for the full adopted text.

---

## 8. Required `/2` contract-text repairs in `docs/EIG_STEERING_DESIGN.md`

Section **"Future predictive artifact"** (lines 24–49 at spike time). Every item is a factual
correction; all were applied to `EIG_STEERING_DESIGN.md` on 2026-07-29 per ADR-017.

1. **The reproduction clause was false as written.**
   > `fit_reference_exposure` is the amplitude basis of the pooled fitted model. … Evaluating the
   > original grid at `reference_incremental_exposure = fit_reference_exposure / n_members` must
   > reproduce `/1` `expected_counts` within the fixture tolerance.

   True only for exposure-homogeneous pools (4 of 11 measured). Replaced with: the fitted grid is
   reproduced by the **per-energy** exposure vector `ΣM_i / fit_reference_exposure`; the scalar
   `reference_incremental_exposure` reproduces it **iff** `exposure_homogeneous` (§6). The `/1`
   reference itself must go when `/1` emission is deleted (ADR-006) — restate the clause against the
   `/2` producer's own stored matrices, not `/1`.

2. **The `exposure_basis` field list was incomplete.** Added: `unit` (`monitor_counts`),
   `request_unit` (`number_neutrons`) and `request_to_exposure_factor` (`1.0`, the seam ADR-007
   asked for), `per_energy_monitor` (ΣM aligned to `energy_meV`), `scalar_exposure_eligible`,
   `exposure_homogeneous`, `full_member_support`, `exposure_ratio_span`, `kept_point_count`.
   Without `per_energy_monitor` the eligibility claim is asserted rather than auditable.

3. **"deliberately scalar-exposure-only" did not name the ineligible outcome.** ADR-017 rejected a
   new refusal code for it: `scalar_exposure_eligible: false` is a declaration/diagnostic only, and
   does **not** gate candidate evaluation — there is no `exposure_basis_not_scalar` code.
   `missing_exposure_basis` stays reserved for the genuine no-monitor case (§4.6). The sentence
   "Per-energy exposure vectors for partially supported pooled grids are not specified in this
   phase" was deleted, since item 2 was adopted.

4. **`fit_reference_exposure` must be persisted from the fit, never recomputed.** Fixed its
   definition as the value `normalized_exposure` returned for that exact fit (`context.py:29-44`),
   i.e. `median(ΣM)` over the fit's kept mask. §4.5 shows a recomputation on a different kept set can
   differ by 25 %. This is a PR-1 plumbing change: `driver.py:3051` drops it into
   `_reference_exposure`, and `ScanContext` carries no reference field at all — either add
   `exposure_reference` to `ScanContext` or thread it into
   `fit_one(..., predictive_n_members=...)` alongside `n_members`.

5. **`reference_incremental_exposure` is now documented as a mean, not a per-scan exposure**
   (§5; c2 = 2.5e9, requested by no member). Its only contractual role is the `/1`-parity identity;
   once `/1` is gone it is a diagnostic and is labelled one.

6. **The no-basis case is stated explicitly** (§4.6): a master fitted on the legacy uniform path has
   no exposure basis and returns `missing_exposure_basis`; the pooled merge has no time fallback.

7. **Plan-level (not `EIG_STEERING_DESIGN.md`):** ADR-007's conditional-go action and the
   ADR-014/EIG-4 trust-gate wording carry the §7.3 revision via ADR-017, and the fixture
   `tests/fixtures/eig_predictive_v2.json` must gain at least one heterogeneous-support pool (c5's
   alternating profile is the cheapest adversarial shape) — the fixture set at spike time could not
   detect a scalar/vector confusion at all.

---

## 9. Reproduction

The instrumentation (`scratch/make_pools.py`, `scratch/run_campaign_pools.py`, `scratch/t3_units.py`,
`scratch/analyze_pools.py`, and their JSON/log outputs) was scratch-only spike tooling written in a
temporary worktree (`.claude/worktrees/agent-a345d415f2db46ffc`). It was never merged and no longer
exists on any live branch. The numeric evidence recorded in §4 above — tables, ratios, and per-bin
characterizations — is the retained record of that run; it is not independently re-runnable from this
repository.

**Truth firewall (as run):** no TAVI planted source parameter was read anywhere in the derivation.
The scripts touched only `GET /schema`'s `background.catalog_version` (to stamp a *disabled* profile,
exactly as `tests/drive/test_live_tavi.py` does) and the scan-payload fields the ISAR parser consumes
(energies, counts, `number_neutrons`, `valid_mask_1`). `result.analytic_model` — which carries the
planted dispersion identity, file path and hash — was never read. Measured counts were compared only
against predictions derived from ISAR's own fitted manifests.
