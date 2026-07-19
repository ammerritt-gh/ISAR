# Campaign diagnostic


## 1. Objective

| field | value |
| --- | --- |
| natural-language goal | MVP zone re-test v16 fold + optic banking + quicklook acoustic seeding 20260709 |
| path (endpoints) | (2, 0, 0) → (3, 0, 0) · 21 grid points |
| tolerance | 0.1 meV (calibrated 68% halfwidth) |
| budget | 2.88e+04 counting_seconds |
| tier | deterministic |

**Pass criteria:** deliver a converged ω₀ (calibrated halfwidth ≤ tolerance) at every reachable grid q, refuse structurally unmeasurable q, and stop with an honest, machine-legible reason.


## 2. Outcome at a glance

| metric | value |
| --- | --- |
| grid q converged | 10 / 21 |
| stop state | unmeetable — 85 scans, 81 proposals |
| budget spent | 10.56 / 2.88e+04 counting_seconds  ⚠ verify-units |
| wall time | 36.9 min total — fitting 36.4 / instrument 20.6 s / overhead 4.55 s |
| center accuracy vs truth | median \|Δω₀\| = 0.0486 meV · coverage68 = 0.5 (36 scored) |
| interval health | 4 certified / 32 unreliable / 0 no-interval |

![Delivered dispersion vs truth](figures/campaign_dispersion_truth.png)

**Delivered dispersion vs truth** — each delivered ω₀ plotted against the dominant Q component, with the analytic truth curve; filled circles are converged q (with calibrated halfwidth error bars), open diamonds are uncertifiable/fast-tier deliveries (centres shown, no honest bar), and × on the bottom rug marks no-signal / infeasible q.

*What good looks like:* filled points sit on the truth line across the reachable path and the open/× markers cluster only where the physics is genuinely hard (near Γ, the zone boundary) — the campaign mapped what it could and flagged what it couldn't.

*Terms:* **calibrated halfwidth** — the σ-calibration-inflated 68% ω₀ interval half-width the stopping rule consumes; **uncertifiable** — a delivered centre whose interval flagged interval_reliable=False — usable number, untrustworthy bar

![Where the scans went](figures/campaign_scan_spend.png)

**Where the scans went** — a stacked bar per grid q counting the scans spent there by kind: quicklook (cheap seeding), production/confirm (full fits), and declined (no usable ω₀).

*What good looks like:* cheap quicklooks spread thin to seed the model and the expensive production fits concentrate on the q that pay off — little beam burned re-confirming a q that will never seat.

*Terms:* **quicklook** — a cheap HESSE-only scan used to seed the ω(q) model, not to deliver; **confirm** — a repeat production fit to tighten an interval already near tolerance

![Budget & time ledgers](figures/campaign_budget_timing.png)

**Budget & time ledgers** — left: the neutron ledger (Σ of per-run cost) and the counting-second ledger against the budget; right: wall-clock seconds broken down by the slowest driver stages.

*What good looks like:* the two ledgers agree with each other and with the stop reason (a statistics-driven stop should show budget barely touched), and the wall split shows where a speed-up would actually pay — usually the fitting stage.

*Terms:* **counting seconds** — instrument counting time (the deterministic tier's virtual clock); verify its units before trusting a budget-limited stop


## 3. What the driver did

1. **Campaign start.** Objective logged; calibration seeded.
2. **Workspace init.** Auto-init'd after the first scan (1 event).
3. **Proposals & runs.** 81 proposals → 85 scans submitted (61 production, 24 quicklook).
4. **Validation.** 99 TAVI validations, 14 rejected as infeasible.
5. **Analyze.** 85 ISAR analyses (24 cheap, 38 fast, 23 full).
6. **Tier escalation.** no escalate_fit records (pre-event log; tier changes inferred from analyze.errors_tier only — 23 full-tier analyses).
7. **Recenter.** 4 window recenters, 0 refused onto the elastic line.
8. **Model updates.** 81 updates; converged 0 → 10 of 21 grid q over the run.
9. **Certification.** no certify pass (campaign did not stop 'met').
10. **Stop.** state `unmeetable` — 21 grid q evaluated; reasons: converged 10/21 grid q; the rest cannot deliver the statistics:; q=0 (hkl=[2.0, 0.0, 0.0]) declined 4× (no signal).


## 4. Delivered dispersion vs truth

| q | hkl | ω₀ (meV) | halfwidth | source | reason | n_prod | collate-only | ω_true | Δ | in interval |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | (2, 0, 0) | — | — | — | unmeasured | 0 | 0 | — | — | — |
| 0.05 | (2.05, 0, 0) | — | — | — | unmeasured | 2 | 1 | — | — | — |
| 0.1 | (2.1, 0, 0) | — | — | — | unmeasured | 2 | 1 | — | — | — |
| 0.15 | (2.15, 0, 0) | — | — | — | unmeasured | 2 | 1 | — | — | — |
| 0.2 | (2.2, 0, 0) | 1.875 | 0.0001727 | single_production | met | 4 | 1 | 1.854 | 0.0209 | no |
| 0.25 | (2.25, 0, 0) | 2.375 | 0.0004926 | single_production | met | 2 | 1 | 2.296 | 0.0789 | no |
| 0.3 | (2.3, 0, 0) | 2.75 | 0.0004426 | single_production | met | 2 | 1 | 2.724 | 0.0259 | no |
| 0.35 | (2.35, 0, 0) | 3.149 | 0.001239 | single_production | met | 2 | 1 | 3.135 | 0.0145 | no |
| 0.4 | (2.4, 0, 0) | 3.55 | 0.0007947 | single_production | met | 2 | 1 | 3.527 | 0.0233 | no |
| 0.45 | (2.45, 0, 0) | 3.826 | 0.002023 | single_production | met | 1 | 1 | 3.897 | -0.0705 | no |
| 0.5 | (2.5, 0, 0) | 4.25 | 0.005258 | single_production | met | 2 | 1 | 4.243 | 0.00785 | no |
| 0.55 | (2.55, 0, 0) | — | — | — | unmeasured | 5 | 1 | — | — | — |
| 0.6 | (2.6, 0, 0) | 4.834 | 0.04457 | single_production | met | 4 | 1 | 4.854 | -0.0203 | yes |
| 0.65 | (2.65, 0, 0) | 5.054 | 0.09357 | single_production | met | 2 | 1 | 5.116 | -0.0617 | yes |
| 0.7 | (2.7, 0, 0) | — | — | — | unmeasured | 5 | 1 | — | — | — |
| 0.75 | (2.75, 0, 0) | 5.507 | 0.0964 | single_production | met | 2 | 1 | 5.543 | -0.0365 | yes |
| 0.8 | (2.8, 0, 0) | 5.602 | (unreliable) | single_production | interval_above_tolerance | 8 | 1 | 5.706 | -0.104 | no |
| 0.85 | (2.85, 0, 0) | — | — | — | unmeasured | 3 | 1 | — | — | — |
| 0.9 | (2.9, 0, 0) | — | — | — | unmeasured | 2 | 0 | — | — | — |
| 0.95 | (2.95, 0, 0) | — | — | — | unmeasured | 3 | 0 | — | — | — |
| 1 | (3, 0, 0) | — | — | — | unmeasured | 6 | 0 | — | — | — |


## 5. Failure dossier

### 5.1 q=0..0.15 (4 points): unmeasured

| q | scan | det | disposition | verdict | ω₀ | interval |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 469 | det | unmodeled | signal_unmodeled | — | (unreliable) |
| 0 | 470 | det | unmodeled | signal_unmodeled | — | (unreliable) |
| 0 | 471 | det | unmodeled | signal_unmodeled | — | (unreliable) |
| 0 | 472 | det | unmodeled | signal_unmodeled | — | (unreliable) |
| 0.1 | 495 | det | unmodeled | signal_unmodeled | — | (unreliable) |

*(+8 collate-only branch_continuation rows at these q — see results.csv; not listed individually.)*

![adjacent converged q=0.2 (contrast)](figures/dossier/scan_476_det_contrast.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

**Diagnosis.** No pooled master has been published at 4 q. Individual scan fits cannot stand in for it; acquire/collate at least one feasible member.

**Fix →** TODO: acquire the first feasible master member

### 5.2 q=0.55..0.55 (1 point): unmeasured

| q | scan | det | disposition | verdict | ω₀ | interval |
| --- | --- | --- | --- | --- | --- | --- |
| 0.55 | 479 | det | resolved | resolved | 4.5 | (unreliable) |
| 0.55 | 516 | det | resolved | resolved | 4.54 | (unreliable) |

*(+4 collate-only branch_continuation rows at these q — see results.csv; not listed individually.)*

![scan 479 evidence](figures/dossier/scan_479_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![scan 516 evidence](figures/dossier/scan_516_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![adjacent converged q=0.5 (contrast)](figures/dossier/scan_500_det_contrast.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

**Diagnosis.** No pooled master has been published at 1 q. Individual scan fits cannot stand in for it; acquire/collate at least one feasible member.

**Fix →** TODO: acquire the first feasible master member

### 5.3 q=0.7..0.7 (1 point): unmeasured

| q | scan | det | disposition | verdict | ω₀ | interval |
| --- | --- | --- | --- | --- | --- | --- |
| 0.7 | 485 | det | resolved | resolved | 5.42 | (unreliable) |
| 0.7 | 522 | det | resolved | resolved | 5.43 | (unreliable) |

*(+4 collate-only branch_continuation rows at these q — see results.csv; not listed individually.)*

![scan 485 evidence](figures/dossier/scan_485_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![scan 522 evidence](figures/dossier/scan_522_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![adjacent converged q=0.65 (contrast)](figures/dossier/scan_486_det_contrast.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

**Diagnosis.** No pooled master has been published at 1 q. Individual scan fits cannot stand in for it; acquire/collate at least one feasible member.

**Fix →** TODO: acquire the first feasible master member

### 5.4 q=0.8..0.8 (1 point): interval_above_tolerance

| q | scan | det | disposition | verdict | ω₀ | interval |
| --- | --- | --- | --- | --- | --- | --- |
| 0.8 | 483 | det | resolved | resolved | 5.72 | (unreliable) |
| 0.8 | 514 | det | resolved | resolved | 5.7 | (unreliable) |
| 0.8 | 533 | det | resolved | resolved | 5.6 | [5.505, 5.675] |

*(+6 collate-only branch_continuation rows at these q — see results.csv; not listed individually.)*

![scan 483 evidence](figures/dossier/scan_483_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![scan 514 evidence](figures/dossier/scan_514_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![scan 533 evidence](figures/dossier/scan_533_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![adjacent converged q=0.75 (contrast)](figures/dossier/scan_506_det_contrast.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

**Diagnosis.** The model-robust pooled ω interval remains above tolerance at 1 q. Continue with the best master-predictive acquisition.

**Fix →** TODO: continue by master-predictive information ranking

### 5.5 q=0.85..1 (4 points): unmeasured

| q | scan | det | disposition | verdict | ω₀ | interval |
| --- | --- | --- | --- | --- | --- | --- |
| 0.85 | 482 | det | resolved | resolved | 5.86 | (unreliable) |
| 0.95 | 480 | det | resolved | resolved | 6.36 | (unreliable) |
| 1 | 477 | det | resolved | resolved | 6.32 | (unreliable) |
| 1 | 526 | det | resolved | resolved | 6.1 | (unreliable) |

*(+14 collate-only branch_continuation rows at these q — see results.csv; not listed individually.)*

![scan 482 evidence](figures/dossier/scan_482_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![scan 480 evidence](figures/dossier/scan_480_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

![scan 477 evidence](figures/dossier/scan_477_det.png)

**One scan, reconstructed** — raw counts (±√N) for one scan/detector with the fit REBUILT from the stored parameters (an NNLS amplitude solve, not a refit) drawn as total + components, and a pull-residual strip beneath; the title carries the disposition/verdict and the honest interval string.

*What good looks like:* the reconstructed curve overlays the data and the pull is flat noise — the delivered ω₀ is supported by the counts; where a scan was declined you can see why in the residual.

*Terms:* **pull** — a difference divided by its error bar (a normalized residual); pull 1 = one σ away; **residual** — data minus fit; flat noise is good, leftover structure means the model missed something; **reconstructed** — the curve is rebuilt from the reported ω₀/Γ/resolution, not fitted again — presentation only

**Diagnosis.** No pooled master has been published at 4 q. Individual scan fits cannot stand in for it; acquire/collate at least one feasible member.

**Fix →** TODO: acquire the first feasible master member


## 6. Defects uncovered

> ⚠ **Caveat —** The items below are machinery (logging / accounting / presentation) defects surfaced mechanically — not physics failures.

1. budget ledger reports counting-seconds spent < 1% of the amount while 85 run events exist (10.56 / 2.88e+04 s) — flag verify-units until the deterministic-tier virtual clock is confirmed
2. decide events carry no 'action' field (pre-Part-1 log) — actions were fingerprinted from the legacy 'event' field


## 7. Verdict & recommended actions

Stop state **unmeetable**; 10/21 grid q converged. Priority actions, most impactful first:

- Resolve `unmeasured` zones → TODO: acquire the first feasible master member
- Resolve `interval_above_tolerance` zones → TODO: continue by master-predictive information ranking
- budget ledger reports counting-seconds spent < 1% of the amount while 85 run events exist (10.56 / 2.88e+04 s) → TODO: logging/accounting fix
- decide events carry no 'action' field (pre-Part-1 log) → TODO: logging/accounting fix


## 8. Provenance

| section | source |
| --- | --- |
| §1 objective, §2 budget/stop, §4 per-q status | report/campaign_summary.json |
| §2 wall split, §3 driver narrative, event counts | report/session_log.jsonl |
| §2 interval health, §4/§5 per-scan verdicts & params | report/results.csv |
| §2 accuracy, §4 truth columns | score.json |
| physics model / dispersion prior | card.state.json |
| §5 dossier figures — results.csv params + data/*.json re-parse, reconstruct_curve | 14 rendered under report/figures/dossier/ |
| buried per-scan set — every non-pooled fitted row, same reconstruct_curve path | 36 rendered under report/figures/scans/ |
| final-certification manifests and derived checkpoints | report/certification/ — 0 files, 0.00 MiB |
| ordinary analysis manifests and derived checkpoints | report/analysis/ — 0 files, 0.00 MiB |

No number in this report was produced by refitting or re-scoring; every value is a re-presentation of what the campaign already recorded.

Certification and ordinary-analysis revisions are retained indefinitely. They are derived artifacts; an operator may manually prune an old revision only after confirming that no published result references it. ISAR performs no automatic deletion.
