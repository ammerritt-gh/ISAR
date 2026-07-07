# MVP first autonomous run — 2026-07-07

The first end-to-end autonomous phonon measurement: one natural-language command, no human
numbers in the loop. First *completed* run (post-speedup) recorded below under **Result**.

```
isar drive --goal "map the acoustic phonon dispersion from (2,0,0) to (3,0,0) with error
bars on the phonon energy less than 0.1 meV, step sizes along H of 0.05" \
  --tier deterministic --workspace out/mvp_demo --seed 42
```

Chain: LLM supervisor (goal → ObjectiveSpec, 21-point grid) → adaptive loop
(`isar/drive/loop.py`) → TAVI deterministic tier (scan sim) → `isar run` subprocess (fit +
errors) → σ-calibrated stopping rule → `report/campaign_summary.json` + append-only
`session_log.jsonl` audit trail. Design refs: `..\TAVI\docs\CLOSED_LOOP_DESIGN.md`,
`LLM_HARNESS_DESIGN.md`.

## Supervisor model (goal parsing)

The supervisor LLM sits at exactly one place — `goal_to_spec` (goal → ObjectiveSpec at campaign
entry). The run loop (acquire / stop / exceptions) is pure deterministic code and makes **zero**
LLM calls, so a flaky model only ever bites at launch, never mid-map.

Default pin is `nanogpt/zai-org/glm-5.2` (`goal_to_spec.DEFAULT_SUPERVISOR_MODEL`,
env-overridable via `LLMM_SUPERVISOR_MODEL`). It replaced `nanogpt/openai/gpt-oss-120b`, whose
NanoGPT serviceability had collapsed (~23% success, repeated 503s / dropped dispatches at
goal-parse time — cost this session two failed launches before a restart). GLM 5.2 reports >95%
health, supports structured output, and dispatches in ~1.6 s — right for a one-shot
schema-enforced parse (`thinking` is pinned False by the client, so the non-thinking base ref).
`nanogpt/deepseek/deepseek-v4-pro` is the >95%-health fallback (also fine, but ~12 s, reasoning-heavy).
nanoGPT enforces only `json_object` (not `json_schema`), so schema conformance is still prompt +
sampling luck backed by one repair retry.

## Defects found and fixed by the live run

Each of these passed the unit suite and failed only under the real loop — the run is the test.

1. **Workspace never initialized.** The driver called `isar run` on a bare workspace;
   `isar init` needs a recognizable data file, which only exists after the first scan.
   Fix: lazy `isar init` subprocess after the first scan lands (`isar_io/runner.py::init_isar`,
   called from the loop; analysis boundary preserved).
2. **Unicode in an HTTP header.** The supervisor's `notes` ("…(2,0,0)→(3,0,0)") flowed into
   the campaign name and then the `Idempotency-Key` header; http.client encodes headers
   latin-1 → `UnicodeEncodeError`. Fix: ASCII-escape the key in `TaviClient.submit_scan`.
3. **Recenter chased the anti-Stokes peak.** The DHO is two-sided and kT ≫ ħω here, so the
   window-recovery logic happily recentered onto −|ω₀| — producing a window with no
   positive-deltaE coverage, which crashed `structure_ic` (empty Ω_d grid). Fixes: recenter
   reflects to the Stokes side (`abs(peak)`, `supervisor/exceptions.py`); `structure_ic`
   raises a clean error instead of `TypeError`; a fit exception in the batch driver now
   yields a `non_result` row instead of killing the whole `isar run`.
4. **ω(q) model learned nothing from quicklooks.** Cheap-tier fits carry only a HESSE
   `omega0_err` (no bootstrap interval), so every observation had `calibrated_halfwidth=None`
   and was excluded from the acquisition model — every window stayed centered at 0 and
   high-q scans missed forever. Fix: quicklook observations fall back to `omega0 ± omega0_err`
   for the *steering* model only; the stop rule still consumes exclusively production/confirm
   intervals (the P-contract: never `prior_sigma`, never HESSE, for convergence).
5. **One hopeless q aborted the whole map.** Γ (H=2.0, acoustic ω₀=0, degenerate with the
   elastic line) declined 4× → the old stop rule declared the entire campaign unmeetable
   after 4 scans. Fix: a stuck q (≥3 *consecutive* declines) is abandoned per-q — the
   acquisition engine skips it, the campaign maps the rest, terminal decision only when every
   q is converged-or-stuck. Declines reset on a resolved fit (window-hunting misses during
   seeding must not burn the budget — first version of this fix cost 13 workable q's).

## Result — first completed run, post-speedup (`out/mvp_stage5`, seed 42)

The loop ran to a clean terminal state: **`unmeetable`, 27 min wall-clock**, no hang / crash /
`BrokenProcessPool`. `campaign_summary.json` + `session_log.jsonl` written. The prior runs never
reached this — they were fit-latency-bound (see below); the fast-error tier fixed that.

**Speedup delivered.** Analyze-tier counts over the run: **cheap 59 / fast 22 / full 16**. The
`fast` HESSE path carried 22 production analyses without a bootstrap; only 16 full bootstraps ran
(the low-q escalations). No certify pass fired (correct — nothing reached `met`). Earlier worry
that the deterministic engine's Γ→0 (sharp peaks, Γ at bound) would suppress the fast tier did
**not** materialize: 22 production fits converged on trusted HESSE intervals.

**Interior accuracy is excellent.** 11/21 q converged (contiguous lower branch H=2.05→2.70);
converged-q **median |Δω₀| = 0.020 meV**, 10 of 11 within 0.05 meV of the analytic truth
`6·|sin(π(H−2)/2)|`. Coverage against that truth is ~0 as expected — the residual is systematic
1-D-resolution model mismatch, not statistics (a *result*, not a failure). Terminal `unmeetable`
because the zone-center H=2.0 floored above tolerance and the upper half was declined (below).

### Two real problems the run exposed — both at high and low q

These are exactly the two ends of the branch, and exactly where real experiments are hardest.
The peaks here are clean (deterministic tier), so both are recoverable with work — flagged for a
future round, not fixed here.

1. **Low-q: one confident-and-wrong q (near-zone-center).** H=2.05 reported `converged` with a
   0.257 ± 0.064 meV interval, but truth is 0.471 meV — the interval misses it by 0.213 meV
   (3.3σ). At the zone center the acoustic ω is barely above the elastic line (truth ω only
   0.47 meV), the fit latched a low artifact, and the certify gate trusted it. One q, but it is
   the "certified + wrong" failure the criteria explicitly hunt. The gate needs to *flag* rather
   than converge this close to the elastic line.
2. **High-q: real signal discarded as `no_signal` (upper-branch false negative).** The stop
   message reads "H=2.5→3.0 declined (no signal)", but the per-scan `omega0` shows the signal is
   there and often *right*: H=2.6 fit ω₀=4.87 (truth 4.85), H=2.75 → 5.58 (5.54), H=2.8 → 5.72
   (5.71) — all classified `no_signal` and abandoned, mixed with a few genuine wrong-peak latches
   (H=2.5 → 7.61, H=2.95 → 8.10, H=3.0 → 7.73). At higher energy transfer (4–6 meV) the quicklook
   excursion test (`verdict.EXCURSION_K`) calls recoverable phonon signal `no_signal`, so the
   driver never invests production scans where a single fit already lands within 0.02 meV. This is
   an **acquisition/detection-gate limitation** (window centering + excursion threshold at high E),
   distinct from the *genuine* zone-center degeneracy at H=2.0.

Full P1–P2 / C1–C4 / H1–H3 / F-failure scorecard: F2 hit once (H=2.05); F1/F3/F4/F5 clear;
P1/P2/C1/C4/H1/H2/H3 met; C3 vacuous (no certify pass). The speedup works and the driver maps a
branch autonomously with sub-0.02 meV interior accuracy and terminates honestly — but it is
**not** a clean pass.

## Known issues / next work

- **[fixed] Fit latency no longer dominates.** Prior runs ran the full bootstrap on every
  production/confirm fit (~1–2 min each) → fitting-bound, never completed. The `fast` HESSE tier
  (converge without bootstrap when Γ off-bound + PD covariance + counts ≥ 200) + the shared-pool
  workorder + adaptive M brought a 21-point campaign to 27 min. Certify-on-`met` re-backs the
  reported numbers with a bootstrap only when the campaign actually meets tolerance.
- **[next] Near-zone-center certify gate is too trusting.** H=2.05 above. Where truth ω approaches
  the elastic line, a low-energy artifact can pass the trust gate. Want the gate to flag (not
  converge) inside a zone-center exclusion band, or a degeneracy check against the elastic line.
- **[next] High-E quicklook detection is a false-negative.** `EXCURSION_K` / window centering
  discards recoverable 4–6 meV signal as `no_signal`, costing the entire upper branch. Biggest
  single win: real signal is present and single fits already recover ω₀ — the gate, not the
  physics, abandons it.
- **Confirm churn at marginal q** (peeking guard re-confirming a q whose half-width sits right at
  tolerance) remains a fit-budget sink at marginal q; cheaper now that most fits are `fast`, but a
  confirm cap is still worth it.
- Γ (H=2.0) is structurally unmeasurable for an acoustic branch (ω₀ → 0); a smarter objective
  parser could exclude zone centers up front rather than discovering it over 8 pooled scans.
- Both trouble zones (high-q detection, low-q zone-center degeneracy) mirror the hard cases in
  real TAS experiments. Clean deterministic peaks make them tractable here; they are the
  standing future-work targets, and were the expected weak spots going in.
