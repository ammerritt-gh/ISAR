# Campaign Diagnostic — MVP Zone Re-test v16

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

**Workspace:** `out/mvp_zone_test16` · **Date:** 2026-07-09 · **Tier:** deterministic (TAVI synthetic, `phonon_dft` truth)
**Audience:** architect. This report answers: *what did the campaign do, where did it succeed or fail, why, and what should change.*
Every number below is drawn from the workspace's own artifacts (`campaign_summary.json`, `session_log.jsonl`, `results.csv`, `score.json`); §8 lists the provenance per section.

---

## 1. Objective

Natural-language goal (`goal_to_spec`): measure the acoustic phonon along **(2,0,0) → (3,0,0)**, 21 grid points at ΔH = 0.05, each to a **calibrated 68% halfwidth ≤ 0.1 meV**, within a budget of **28 800 counting seconds**. Truth is analytic: E(q) = 6·sin(π·q_red/2) meV, so the path runs from ω = 0 at the zone center (H=2) to ω = 6 meV at the zone boundary (H=3).

**Pass criteria for this re-test:** deliver converged ω₀ at every reachable grid q; correctly refuse structurally unmeasurable q; stop with an honest, human-legible reason.

## 2. Outcome at a glance

| Metric | Value | Assessment |
|---|---|---|
| Grid q converged | **10 / 21** | mid-zone solid; both ends failed (§5) |
| Stop state | `unmeetable` after 85 scans, 82 iterations | honest stop, **mislabeled reasons** (§6.1) |
| Budget spent | 10.6 of 28 800 counting-s (0.04%) | stop was statistics-driven, not budget — *verify accounting* (§6.5) |
| Wall clock | 36.9 min, of which **36.4 min = fitting** (`isar_run`) | campaign is fitting-bound; TAVI I/O is 14.6 s total |
| Accuracy of delivered centers | **median \|Δω₀\| = 0.049 meV** vs truth (36 scored scans) | *within tolerance* — the physics succeeded |
| Interval health | **32 / 36 deliveries have ~zero-width intervals**; coverage68 = 0.50 | the certification machinery failed (§5.3) |

**Headline: the campaign measured the dispersion to spec and then could not certify its own success.** Central values land at half the requested tolerance; the error-bar layer (γ-at-bound collapse → `interval_reliable=False` → excluded from calibrated convergence) is what turned good q-points into "failures."

## 3. What the driver actually did

From `session_log.jsonl` (1 599 events): a propose → validate → run → analyze → decide loop, 82 iterations.

1. **Seeding.** First pass laid quick-look scans across unvisited q ("unvisited q=0 … quick-look to seed the ω(q) model", predicted σ = 1000 meV placeholder prior). Quick-looks use a cheap HESSE-only tier — verdict + rough ω₀, deliberately untrusted for delivery.
2. **Model building.** Each analyzed scan updated the branch model (`model_update` events); the acquisition then targeted the least-constrained q by cost-normalized expected information gain.
3. **Production/confirm fits.** Promising q escalated to the full bootstrap tier. Mid-zone q seated quickly (2–4 production fits each, H=2.20–2.50 all converged with ≤ 4 fits).
4. **Retry logic engaged where designed:** 3 recenter actions ("peak at ΔE=0.009 meV, outside/near window edge; re-centering"), 1 supervisor escalation ("missed the energy window 3× at this q — escalating instead of burning more beam"), 44 `accept_no_converge` records keeping non-delivery numbers for the record.
5. **Stop.** After per-q decline budgets exhausted at 11 q-points, the supervisor declared the objective `unmeetable` and stopped — correctly refusing to spend the remaining 99.96% of budget on q it had concluded could not seat.

The loop mechanics — seed, steer by information gain, escalate tiers, cap retries, stop with a reason — **all functioned**. The failures below are in the physics zones and the certification layer, not the control flow.

## 4. Delivered dispersion vs truth

Converged mid-zone points, calibrated halfwidths in parentheses (meV):

| q_red | ω₀ delivered | ω truth | Δ | halfwidth |
|---|---|---|---|---|
| 0.20 | 1.875 | 1.854 | +0.021 | (0.0002)* |
| 0.25 | 2.375 | 2.334 | +0.041 | (0.0005)* |
| 0.30 | 2.750 | 2.716 | +0.034 | (0.0004)* |
| 0.35 | 3.149 | 3.090 | +0.059 | (0.0012)* |
| 0.40 | 3.550 | 3.527 | +0.023 | (0.0008)* |
| 0.45 | 3.826 | 3.896 | −0.070 | (0.0020)* |
| 0.50 | 4.250 | 4.243 | +0.007 | (0.0053)* |
| 0.60 | 4.834 | 4.854 | −0.020 | 0.045 |
| 0.65 | 5.054 | 5.126 | −0.072 | 0.094 |
| 0.75 | 5.507 | 5.596 | −0.089 | 0.096 |

\* Halfwidths ≪ 1 μeV are the γ-at-bound pathology (§5.3), not real precision. **Every delivered center is within 0.09 meV of truth** — but only the q ≥ 0.60 intervals are plausibly honest.

## 5. Failure dossier

### 5.1 Near-Γ (H = 2.00–2.15): genuine physics wall, partially misplayed

Truth here is ω = 0–0.47 meV — under or inside the elastic line. The four H=2.0 scans all returned `signal_unmodeled` with enormous cost/dof (2 054–7 335) and descriptor peaks the model can't account for (e.g. E = 2.20 meV at H=2.0 — fold/optic leakage, not the acoustic target). **Declining is the right call** — an acoustic ω₀→0 is structurally unmeasurable — but two things went wrong around it:

- **The driver spent 13 scans discovering it** (4 + 3×3 declines across H=2.00–2.15). The TODO's *objective-aware Γ exclusion* would remove these q at parse time.
- **The recenter logic chased the elastic line:** "peak observed at ΔE = 0.009 meV … re-centering on the Stokes side (0.009 meV)" — it re-aimed the scan window at what is almost certainly the elastic line at ~0 meV. Harmless here (the q was ultimately declined) but a live-instrument defect: it would burn beam pointing at E = 0.

### 5.2 Zone boundary (H = 2.85–3.00): signal present, stop reason wrong

The stop report says these q "declined 3× (**no signal**)." The per-scan record contradicts this: at H=3.0, scans 477 and 526 are **`resolved` at ω₀ = 6.32 and 6.10 meV** (truth: 6.0) — the signal is plainly there. What actually happened: every fit at these q either stayed on the untrusted fast-error tier or hit **Γ-at-bound → `interval_reliable=False`** ("resolution-limited spike, degenerate bootstrap" — scans 527, 528, 530), so no *calibrated* interval ever existed, `calibrated_halfwidth = None`, the q could not converge, and three non-deliveries were miscounted as "no signal" declines. This is the known open boundary zone — steep-slope resolution asymmetry the 1-D width cannot model — awaiting the `tas4d` convolution. The measurement layer did its job; the certification layer could not, and the stop message then *misreported the cause*.

### 5.3 The zero-width-interval epidemic (mid-zone, H = 2.20–2.55)

32 of 36 scored deliveries carry ~zero halfwidths; convergences at q = 0.20–0.50 rest on intervals of 0.2–5 μeV — absurd for a 0.73 meV FWHM instrument. Mechanism (documented in TODO): a fit whose Γ rails to its floor is a resolution-limited δ-spike whose bootstrap ω₀ interval collapses. The honest-flag route (`interval_reliable`) exists and *fires* — but at these q the flagged fits were still the ones that seated convergence (contrast §5.2 where the same flag blocked it). **The convergence rule treats the flag inconsistently:** fatal at the boundary, ignored mid-zone. Result: coverage68 = 0.50 against a nominal 0.68, and "converged" q whose claimed precision is fictional even though their *centers* are excellent.

### 5.4 H = 2.80: confirm churn

8 production fits pooled without converging — halfwidth floored at 0.123 meV, just above the 0.1 tolerance. This is the TODO's *confirm churn* case (a q whose achievable halfwidth sits at tolerance re-confirms forever). The 3-decline cap eventually caught it; a tolerance-vs-floor test would catch it after 2 fits instead of 11.

### 5.5 Proposal feasibility churn

14 of 99 validations rejected with "infeasible_points: 10 point(s) unreachable," each repaired by resubmitting feasible subsets. The proposer never learned which points were unreachable — the same blocker recurred throughout the campaign. Wasted iterations only, but at instrument scale it's wasted minutes per batch.

## 6. Defects uncovered (machinery, not physics)

1. **Stop reasons are canned and sometimes false.** "no signal" is emitted for q where resolved fits at the right energy exist (§5.2). The stop message must distinguish: *no signal* / *signal, interval uncertifiable (γ-at-bound)* / *signal, interval floored above tolerance* / *fits never left the fast tier*. All four states are already in `results.csv`; the stop logic just doesn't consult them.
2. **`interval_reliable` is inconsistently enforced** in convergence (§5.3) and **never shown** in `report.md` — which prints `[3.15, 3.15]` with conf 1.00, exactly the confident-nonsense presentation the flag exists to prevent.
3. **Triage table drowns its audience:** 54/106 rows are `branch_continuation` — internal collate-only records rendered as if they were user-facing results, with "junk/reject" rows that still display fitted ω₀ values.
4. **Elastic-line recentering near Γ** (§5.1) — window re-aiming needs a floor: never recenter onto \|ΔE\| ≲ resolution FWHM.
5. **Budget accounting suspicious:** `spent = 10.56` of 28 800 counting-seconds after 85 scans at ncount 1e7. Possibly correct for the deterministic tier's virtual clock — verify units before a budget-limited campaign trusts it.
6. **Escalation gap:** at H=2.85–3.0 the fast-tier reasons say "untrusted → escalate to full," yet several such q show no full-tier certified fit. Either escalation didn't fire or its results didn't seat; the log can't distinguish — an explicit `escalate_fit` event is needed.

## 7. Verdict & recommended actions

**The v16 loop is architecturally sound and scientifically ~half-certified.** Control flow, steering, retry caps, and the honest stop all work. Central values meet the objective everywhere a number was delivered. The blocking layer is **interval certification**: γ-at-bound collapse corrupts the mid-zone claims and blocks the boundary deliveries, and the stop report then narrates the wrong story.

Priority order (mapped to existing TODO items):

1. **Unify the `interval_reliable` convergence rule** (new, small): a flagged interval never seats convergence *and* never counts as a "no signal" decline — it's a third state, "uncertifiable," with its own stop-reason text. Fixes §5.2, §5.3, §6.1 together.
2. **γ-at-bound interval calibration v2 / calibration-curve re-run** (TODO: σ-calibration) — the underlying statistical fix.
3. **Objective-aware Γ exclusion** (TODO, exists) — removes the 13-scan near-Γ burn.
4. **`tas4d` convolution** (TODO, exists) — the real unlock for the boundary zone.
5. **Confirm-churn cap by floor detection** (TODO: production-fit latency, lever 1).
6. Report-layer fixes: surface `interval_reliable`, collapse collate-only rows, correct stop-reason taxonomy (§6.1–6.3).

## 8. Provenance — how this report was assembled

| Section | Source |
|---|---|
| Objective, budget, per-q status, stop reasons | `report/campaign_summary.json` |
| Narrative, decisions, recenters, escalation, timings | `report/session_log.jsonl` (1 599 events) |
| Per-scan verdicts, fit kinds, `interval_reliable`, reason strings | `report/results.csv` |
| Truth comparison, coverage, pulls | `score.json` (`fn:phonon_dft`) |

No numbers in this report were produced by refitting; everything is a re-presentation of what the campaign already recorded. The gap this report fills is *assembly*: the workspace contained the entire story and no artifact told it.
