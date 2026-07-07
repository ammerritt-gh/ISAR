# MVP first autonomous run — 2026-07-07

The first end-to-end autonomous phonon measurement: one natural-language command, no human
numbers in the loop.

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

nanoGPT enforces only `json_object` (not `json_schema`), so schema conformance is prompt +
sampling luck. Probe (3 trials each, temperature 0): `nanogpt/openai/gpt-oss-120b` 3/3,
`nanogpt/deepseek/deepseek-v4-pro:thinking` 3/3, `nanogpt/moonshotai/kimi-k2.5` 2/3 (invents
a wrapper object and enumerates grid points). gpt-oss-120b (the default pin) also threw
transient 503s twice this session; when it is down, pin
`LLMM_SUPERVISOR_MODEL=nanogpt/deepseek/deepseek-v4-pro:thinking`.

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

## Result (run in progress at time of writing)

Escalation ladder works as designed: 21 quicklooks seed the ω(q) model → production scans at
the largest-deficit q → confirm scans reproduce the crossing (peeking guard). Attempt with
the non-consecutive decline counter still converged 8/21 q to <0.1 meV *including the zone
boundary H=3.0*; per-q abandonments (Γ) are honest and audited. Final numbers belong to the
completed run's `campaign_summary.json`.

## Known issues / next work

- **Fit latency dominates wall-clock.** Deterministic-tier scans are near-instant; each
  production/confirm fit runs the full bootstrap error layer (~1–2 min). A 21-point campaign
  needs ~50+ production-tier fits → the campaign is fitting-bound. Next test: speed up the
  per-scan production fit (bootstrap workers are already parallel *within* a scan;
  candidates: replicate count vs tolerance need, profile-likelihood instead of bootstrap for
  the interval, warm-starting from the quicklook fit).
- Γ is structurally unmeasurable for an acoustic branch (ω₀ → 0); a smarter objective parser
  could exclude zone centers up front rather than spending 4 scans discovering it.
- gpt-oss-120b availability is flaky; automatic supervisor-model fallback is TODO.
