# SVP round 1 — TAVI-as-backend deterministic re-certification (2026-07-10)

> **Lifecycle:** Historical evidence
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** the current contract named in [`campaigns/README.md`](README.md). A dated record of what happened, never a statement of current behaviour (DECISIONS D3).

First campaign after the S3 uniform `interval_reliable` rule (`bdbeb69`), the R4 logging events
(`a3bf4c6`), and the campaign diagnostic (`0d1756f`); also the first live exercise of the
poison-bug fixes (ISAR `c1aa62c`, TAVI `0928171c`) and the `scan_command2` fix (`2d66b57`).
Scope per owner: deterministic engine only — **no McStas, no budget/clock work this round**
(DECISIONS T2 realistic clocks and O2 budget semantics remain open; see §5).

## Invocation

    isar drive --spec objective_v16.json --tier deterministic --workspace out/svp_recert1 --seed 42
    isar score --workspace out/svp_recert1 --truth-fn phonon_dft --bucket-by h --json out/svp_recert1/score.json

TAVI at 127.0.0.1:8642 (deterministic engine, PUMA); supervisor not needed (`--spec` is the
LLM-free path). Wall clock 29.5 min; 81 scans (57 production, 24 quicklook), 77 proposals.
Workspace promoted to `C:\Users\AMM\Documents\ISAR_baselines\svp_recert1`.

## Finding 0 — first launch died at 0/21, and the taxonomy caught it

A fresh TAVI launch carries GUI-default `scan_command2 = "deltaE 2 10 0.25"`. The driver only set
`scan_command1`, so TAVI saw a two-command scan (deltaE × deltaE, 1650 points) and rejected every
validation. The per-q reason enum labeled all 21 q `infeasible` ("all 3 proposals
validation-rejected") — a correct, legible diagnosis on its first live outing. Fix `2d66b57`:
the driver owns the scan definition and now clears `scan_command2` explicitly. This run had zero
`Both commands scan` blockers.

## Scorecard (vs v16, pre-S3 — `docs/campaigns/2026-07-09_MVP16_DIAGNOSTIC.md`)

| metric | v16 (pre-S3) | this run | note |
|---|---|---|---|
| converged | 10/21 | **3/21** | v16's count included fictional-precision seatings |
| stop state | unmeetable | unmeetable | |
| median \|Δω₀\| (delivered) | 0.049 meV | **0.046 meV** | centers unaffected by the convergence change |
| zero-halfwidth deliveries | 32/36, many **seated convergence** | 38/39, **none seated** | the S3 gate held live |
| coverage68 | 0.50 (all deliveries) | 1.0 on the certified set (n=1 scored) | honest but tiny-n |
| confident-wrong (gross pull) | 2 zones | 0 scored rows with \|pull\| > 3 | but see Finding 3 |
| per-q reasons | free text, partly false | met 3 · uncertifiable_interval 7 · no_signal 6 · fast_tier_only 3 · signal_unmodeled 2 | all logged, none derived |

Converged q: 0.45 (hw 0.0020 — see Finding 3), 0.70 (hw 0.078, truth ∈ interval),
0.75 (hw 0.089, truth ∈ interval). Tolerance 0.1 meV.

## Findings

**1. The S3 gate held under live fire.** All 38 flagged (`interval_reliable=False`) deliveries
were excluded from convergence; the only reliable bootstrap row in the whole run (scan 39) is the
only coverage-scored row. v16's mid-zone fictional precision cannot recur through that path.

**2. Escalation is now visible, and it names the bottleneck.** 17 `escalate_fit` promotions
(reason `untrusted_hesse_no_reliable_interval`), outcomes **16 failed / 1 seated**: the full-tier
bootstrap fires and then fails to certify because γ rails to its bound. The MVP16 audit's
unanswerable question ("did escalation fire and fail, or never fire?") is answered per-scan in
the log. Certification machinery — γ-at-bound calibration v2, then tas4d — is confirmed as the
blocking layer; the driver, steering, retry, and stop logic all behaved.

**3. New defect: a mis-certified interval through the trusted-HESSE carve-out.** q=0.45
converged via scan 31 (`interval_reliable=False`, `hesse_trusted=True`): certified HESSE interval
[3.8248, 3.8276], truth 3.8967 — outside by 0.070 meV ≈ 35 halfwidths. The center is within
tolerance, so the delivery is not gross-wrong, but the *certified width* is overconfident by an
order of magnitude. γ = 0.0134 passed the at-bound heuristic, so the trust gate
(`covariance_ok ∧ ¬gamma_at_bound ∧ counts`) admitted it. The pull-based confident-wrong check
missed it because flagged rows are pull-excluded and this row's *flag* is on the bootstrap, not
the HESSE path. Action: fold into γ-at-bound calibration v2 — the HESSE trust gate needs the
same calibration scrutiny as the bootstrap flag (TODO updated).

**4. Recenter elastic guard fired in production.** 3 `recenter_refused_elastic` events near Γ —
on a real instrument that is beam not spent pointing at the elastic line. 1 ordinary recenter
proceeded. 0 `proposal_clamped` events (the 594-point pathology cannot recur regardless: emitted
count now guarded, and TAVI rejects over-limit before its feasibility expansion).

**5. Still open (deliberately).** Budget/clock: `budget.spent` remains TAVI's *compute* eta
(`_charge_budget` sums `eta.estimated_seconds`; the deterministic engine reports milliseconds —
the MVP16 "10.6 of 28 800" is this semantics gap, not a units bug). T2 realistic clocks + O2
ledger renaming/documentation remain the SVP's next rung, along with McStas. 13 validations
rejected `infeasible_points` (5/9/2 points at window edges) — genuine instrument geometry, fine.

## Verdict

An honest 3/21 that names why each of the other 18 q cannot deliver beats v16's 10/21 with
fictional precision. Centers hold at ~0.046 meV. The one regression-grade discovery (Finding 3)
is exactly the kind the run was designed to surface, and it sharpens the next work item:
γ-at-bound calibration v2 must cover the HESSE trust gate, not only the bootstrap flag.
