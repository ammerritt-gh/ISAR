# HANDOFF — SCENE tuning calibration

> **Status:** live
> **Owner:** Adrian
> **Purpose:** the measured baseline for tuning discovery-lane placement and cost, from the
> first end-to-end `discover_and_map` campaign against TAVI. Delete when SCENE-5.5 (priced
> envelope) lands and a re-run replaces these numbers.
> **Exit criteria:** §6's four items are closed, or superseded by a later calibration run.

The run: `isar drive --goal "scan the region from (2,0,0) to (3,0,0) up to 10 meV, map out all
the phenomena of interest"`, deterministic tier, TAVI virtual PUMA, sample `Al_phonon_DFT`,
T = 200 K, `--budget 1800s --analysis-limit 30m`, 2026-09-01, ISAR `97ee835`.
Ended `over_budget — 9 deliveries, beam release (budget_exhausted)`, exit 0.
Workspace kept at `test_workspaces/2026-09-01-llmm-role/ws-discovery` (gitignored).

**Headline: 6 h 17 min of wall clock produced one resolved dispersion point.** Nothing crashed and
nothing is wrong with the physics — the fit that did resolve was excellent. The cost is structural,
and it is in two places that this document names.

---

## 1. Where the time went

| phase | seconds | % wall | instrumented? | counted by the budget? |
|---|---:|---:|---|---|
| TAVI acquisition (11 scans) | **3.0** | 0.01% | yes (`wait_scan`) | no (deterministic tier is ~instant) |
| `isar run` fitting (11 invocations) | **1,969** | 8.7% | yes (`timing:isar_run`) | **yes** — this is the analysis clock |
| `scene_build` (10 builds) | 27 | 0.1% | yes (`timing:scene_build`) | no |
| **planner / scene-EIG scoring** | **20,625** | **91.2%** | **no** | **no** |
| everything else | ~12 | 0.05% | yes | — |
| **total wall** | **22,636** | 100% | | |

The campaign stopped because the *analysis* clock hit 1800 s. It had by then spent **5 h 44 min in
a phase that emits no timing record and is invisible to every budget.**

That phase is the gap between `timing:scene_build` and the next `propose`, i.e. `plan_next_action`
building and scoring the candidate designs. It grows sharply with scene size:

| iteration | gap (s) | designs scored |
|---:|---:|---:|
| 4 | 131 | 24 |
| 5 | 2,436 | 39 |
| 6 | 4,344 | 17 |
| 7 | 4,426 | 16 |
| 8 | 4,458 | 15 |
| 9 | 4,830 | 14 |

Note the shape: the cost keeps climbing *while the candidate count falls*. It is not driven by
the number of designs — it tracks the scene posterior, which by the end enumerated **4,096
assignments over 9 observations in one cluster** (`n_possible` 3,110,400). Cost per decision is a
function of scene size, not agenda size.

**Tuning target 1 — instrument `plan_next_action` and put it on a clock.** Right now the only
budget that can stop a discovery campaign measures the 9% of work that is not the bottleneck. A
`--budget`/`--analysis-limit` pair that cannot see 91% of the run cannot steer it. This is
also the honest prerequisite for SCENE-5.5's priced envelope: pricing per-neutron is moot when
neutrons are 0.01% of the cost and scoring is 91%.

---

## 2. Where the scans went

Placement in **q** is good. Scene-EIG spread 11 scans across the declared region without
clumping or revisiting wastefully:

`h = 2.046, 2.138, 2.138*, 2.415, 2.508, 2.554, 2.600, 2.703, 2.800, 2.840, 2.960`
(*the −E corroboration revisit)

Placement in **energy** is the problem. Every scan took the *full declared ω-domain*:

| job | h | ω_true (meV) | window (meV) | step | pts within ±1 meV of the mode |
|---|---:|---:|---|---:|---:|
| j-0005 | 2.600 | 4.854 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0006 | 2.800 | 5.706 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0007 | 2.554 | 4.572 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0008 | 2.046 | 0.434 | 0.0 .. 10.0 | 0.50 | 3/21 (14%) |
| j-0009 | 2.703 | 5.358 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0010 | 2.960 | 5.926 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0011 | 2.138 | 1.290 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0012 | 2.138 | 1.290 | **−6.0 .. 9.0** | 0.50 | 4/31 (13%) |
| j-0013 | 2.840 | 5.794 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0014 | 2.508 | 4.289 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |
| j-0015 | 2.415 | 3.636 | 0.0 .. 10.0 | 0.50 | 4/21 (19%) |

ω_true is the deterministic truth function, linearly interpolated from
`calibration/phonon_dft_deterministic.json` (`truth_fn: phonon_dft`, h-grid 2.03…2.9, ω 0.28…5.93 meV).

So **81–87% of every scan's points sat far from the only mode in the region**, and the window
never narrowed as the dispersion became known — not even at iteration 9, with 9 observations and a
posterior confident to six nines.

This is structural, not a bug: scan-window EIG is the **targeted** lane's realization layer. A
`discover_and_map` step carries no `scan_window_eig_mode` at all (`DiscoverAndMapSpec` has no such
field, pinned by `tests/drive/test_discovery_world.py:67`), and the CLI refuses the flag by name
for discovery under H21(a). Scene-EIG chooses *where*; nothing chooses *how wide*.

**Tuning target 2 — give the discovery lane a window policy.** The cheapest version is not full
EIG: once a cluster's `n_modes` posterior is decided and ω is localized, clip the domain to the
predicted band plus a margin. That is the difference between 4/21 and (say) 15/21 useful points
per scan — a ~4× improvement in information per neutron with no new scoring machinery.

---

## 3. What the campaign actually learned

| verdict | scans |
|---|---:|
| `branch_continuum` (no ω₀) | 10 |
| `resolved` | **1** |

The one resolved scan is j-0012 — the organic on-path −E corroboration revisit (SCENE-5, H23):

- fitted ω₀ = **1.3007 ± 0.0251 meV**
- truth at h = 2.138 = **1.290 meV**
- error **0.011 meV ≈ 0.4σ**

Two things follow. The fitter is not the weak link: when a scan is placed and windowed such that
the mode is resolvable, ISAR nails it. And **the single adaptive act in the whole campaign — the
−E revisit — is the one that produced the only usable number.** That is the strongest evidence in
this run that the SCENE-5 machinery is worth having; it is also the clearest signal that there is
far too little of it per unit cost.

Final scene posterior: 1 cluster, 9 observations, `n_modes = 2` at P ≈ 0.999999, label
`stokes2+bg`.

**Caveat that must travel with those six nines:** the cluster reports `truncated: true` and
`truncation_unbounded: true`, with `log_discarded_mass_bound: null` — 4,096 of 3,110,400
assignments enumerated and *no bound on the discarded mass*. The probabilities are conditional on
the enumerated subset. Linkage was clean (`linkage_truncated: false`, zero dropped prior mass), so
this is enumeration breadth, not linkage. Per the standing rule that only clean single-answer folds
may enter the dispersion prior, an unbounded-truncation posterior should not be treated as a fold
without a second look.

**Tuning target 3 — either bound the discarded mass or stop reporting six nines.** A posterior
whose normalization is unbounded should not present as certainty; this is the number most likely to
mislead a future reader of a campaign report.

---

## 4. Scene-EIG scoring behaviour (for calibration)

The MI arc is healthy and worth preserving as the reference shape:

| decision | designs | winner MI (nats) | families in play |
|---:|---:|---:|---|
| 1 | 19 | 0.138 | ConfirmPresence, DiscoverRegion, VerifyCompleteness |
| 2 | 21 | 0.171 | + EstimateParameter |
| 3 | 25 | 0.128 | + ConnectTrack |
| 4 | 24 | 0.146 | " |
| 5 | **39** | 0.151 | ConnectTrack, DiscoverRegion, EstimateParameter |
| 6 | 17 | 3.061 | ConfirmPresence, DiscoverRegion, VerifyCompleteness |
| 7 | 16 | 5.145 | " |
| 8 | 15 | 5.790 | " |
| 9 | 14 | 7.136 | " |
| 10 | 13 | **7.753** | " |

Candidate pool peaks at 39 then collapses to 13 as questions close. Information per placement jumps
~50× at decision 6, once enough arenas exist to constrain identity. The scoring *discriminates* —
the first decision's 19 designs spread smoothly 0.138 → 0.087 nats with no ties, which is the
contrast with the targeted lane, where the 0.5 meV QoI partition against a ~0.018 meV prediction
sigma makes comparisons frequent cost-decided ties.

Nothing here needs fixing. It is the baseline to regress against after tuning.

---

## 5. Known-inert and out of scope for this run

- **Declared hypotheses do not move real placements.** The production catalogue declares no
  identity-conditioned priors, so no real fit's identity is ever non-None-matched — the permanent
  xfail tripwire `tests/drive/test_fcc_battery.py::test_acoustic_priority_declared_hypothesis_moves_real_placements`.
  Do not read this run's placements as evidence for or against hypothesis priority.
- **Cost / per-neutron pricing is Planned** (SCENE-5.5, H21(a)). Section 1 argues the pricing
  target should be re-examined before it is built.
- **Discovery will not cold-start.** It refuses a bare workspace by name — it needs
  `card.policy.toml` for sample identity, condition setpoint, binning policy and lattice epoch to
  mint the cold-start partition. This run was seeded with `isar init` on a probe scan from an
  earlier targeted campaign. The targeted lane auto-inits; discovery does not. Deliberate, but it
  means "discovery from nothing" is not reachable through `isar drive` alone.
- **Cold-start `WorkspaceNotFound` in the targeted lane** (separate finding, not this run):
  `isar/drive/acquisition/eig_shadow.py:220` opens the workspace during scoring on the first
  iteration, before the first scan auto-inits it. It degrades safely to legacy control but
  surfaces as a stack trace, so scan-window EIG cannot score on the opening iterations of any
  cold-start targeted campaign. Undecided whether defect or accepted noise.

---

## 6. Suggested order of attack

1. **Instrument `plan_next_action`** (§1). Without it, every later tuning claim is unmeasurable.
   One timing record per scoring pass, and put it on the analysis clock.
2. **Window policy for the discovery lane** (§2). Highest ratio of information gained to work
   done; needs no new scoring machinery.
3. **Bound or de-advertise the truncated posterior** (§3).
4. Re-run this exact goal and diff against §1–§4. The campaign is cheap in neutrons (3 s) and
   expensive only in scoring, so a tuning loop is fast once (1) makes it visible.
