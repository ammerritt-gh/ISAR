# EIG-4 completion — governance restoration, tuning, and closure cleanup

> **Lifecycle:** Active plan
> **Provenance:** hand-written
> **Verified against code revision:** `170388c` (2026-08-05)
> **Owner:** @ammerritt-gh
> **Exit criteria:** AGENTS.md rules 6–8 are in force again; a campaign that has completed its
> first pass across Q selects EIG-designed scans for 80%+ of the production requests that follow;
> the four STAT-2b protected-hash mismatches are closed; this plan is then removed and tombstoned
> according to policy.

## Why this exists

The EIG-4 repair series (R1–R3) is mechanically finished. R3's cold-start SVP met its agreed
minimal gate on 2026-08-05, and met it twice: once on the original resolution widths and again
after the section-width correction, both reaching stop state `met` with all 20 measurable grid q
converged and **3 `target_core` grants from a cold start** where the gate required one — against
warm-seeded O41 Test 10's single grant. What remains is not repair. It is the governance
restoration the suspension owes, the tuning that makes the capability worth having, and a closure
cleanup.

The framing that matters for everything below: **a grant that fires rarely is worth nothing.**
Saving 66% of the neutrons on 1% of requests rounds to zero saved neutrons. EIG-4 is not finished
when grants are *possible*; it is finished when they are *routine*.

### What landed on 2026-08-05

| Commit | Change |
|---|---|
| `f8cbd0f` | Design measure marginalises over bootstrap-grade models and renormalises |
| `c3d1c02` | Predictive escalation refits only the promoted master; readiness no longer needs a homogeneous basis |
| `fd8f5e1` | Per-offset evidence persisted on the authority decision |
| `bcdf482` | Canonical scan-window EIG policy supplied when a card declares none |
| `c3a2b6a` | Workspace picker reaches a folder that does not exist yet |
| `b816d76` | Authoritative selection recorded as demonstrated |
| `5b90b9c` | Windows sized on the resolution **section**, not its projection |
| `a7591f9` | Fisher tier skips an unrankable q instead of failing the generator |
| `006dc92` | Counts tier keeps its baseline pick; generator faults log a traceback |
| `170388c` | Scan point ceiling 100 → 220 to match section-width sampling |

Two measurements worth carrying forward. Escalation went from **273.7 s to 29.3 s** because it was
refitting every master rather than the one it owed. And of 21 Test 10 masters, **only 2 were
EIG-ready** until `f8cbd0f`, because a homogeneity requirement rejected masters over Akaike weights
of order 1e-11; it is now 21 of 21.

## Part 1 — Restore AGENTS.md rules 6–8

Rules 6, 7 and 8 (protected-drift closure, no automatic source-closure approval, and the whole
driver being selection-critical) were suspended on 2026-08-04 for R1–R3. Rule 5, STAT-2b history
immutability, stayed in force throughout and is unaffected.

The work is to delete the temporary-suspension block from `AGENTS.md` and confirm the three rules
describe current practice. That is a small edit. The judgement is in the sequencing.

**Read this before choosing the order.** Rule 6 requires protected drift to be closed before a
branch merges. Four protected-hash mismatches are currently open (Part 3). Restoring rule 6 while
they are open means the tuning slice — which necessarily edits `isar/session/driver.py`, the
protected-*semantic* file — runs under full ceremony *and* on top of pre-existing drift. That is
the most expensive possible ordering.

Three options, and this is the owner's call:

1. **Restore now, tune under ceremony.** Honest about governance, most expensive to execute.
2. **Close Part 3 first, then restore, then tune.** Removes the drift so ceremony is survivable.
   Contradicts the "cleanup last" ordering but is the cheapest correct path.
3. **Tune first, restore at the end.** Cheapest to execute, but extends a suspension that was
   scoped to R1–R3 to cover work that is not repair — which is how a temporary suspension becomes
   permanent.

The plan as written follows the owner's stated order (1, then 2, then 3). If the tuning slice turns
out to be painful under ceremony, reconsider rather than quietly widen the suspension.

## Part 2 — The tuning slice

**This is the part that makes EIG-4 worth having, and it owns the 80% target.**

Success is not a grant count. It is: after a campaign's first pass across Q, **80%+ of subsequent
production requests are EIG-designed**. The operating model is legacy as the survey instrument for
one pass, EIG as the follow-up instrument thereafter.

### Where the rate stands

| Campaign | Iterations | Scored decisions | Grants | Rate |
|---|---|---|---|---|
| O41 Test 10 (warm seed) | 45 | 15 | 1 | ~7% |
| R3 cold start (vanadium widths) | 59 | 17 | 3 | 18% |
| R3 cold start (section widths, cap 220) | 103 | 12 | 3 | **25%** |

All three cold-start grants narrow 105→17, 103→17 and 108→17 points — a 6× reduction, about 84% of
the neutrons, against the vanadium run's 3× (49→17). The legacy survey needs ~105 points at section
sampling while the core stays at 17, so the *saving per grant* rose along with the rate.

**The binding constraint has moved, and this is the single most important fact for this slice.**
On the vanadium run the blockers split evenly (20 `o41_applicability_unavailable`, 20
`o36_ineligible`). On the repaired run they are 22 and **69**: `o36_ineligible` now accounts for
two thirds of all decisions and is the reason the rate is 25% rather than far higher. Applicability
is no longer the problem; eligibility is.

### Attribution comes from the histogram, not from guesswork

`isar diagnose --workspace X --eig` and the `eig_decision` session-log events carry a `blocker`, a
`blocker_detail` list, and — since `fd8f5e1` — the per-offset margins behind every decision. Read
the histogram before touching a threshold. The blocker names the knob:

- **`o41_offset_disagreement`** → partition fragility. The two 0.5 meV offsets resolve opposite
  roles. This is the failure the per-offset agreement rule exists to catch, and the fix is the
  partition, not the margin floor.
- **`o41_margin_below_floor`** → the `O41_MARGIN_SE_MULTIPLE = 3.0` floor is the binding constraint.
  Only then is re-tuning that constant the honest response.
- **`o41_margin_not_decisive`** → predictive sharpness. The answer is better bases, not looser
  gates.
- **`o41_applicability_unavailable`** → masters never reached an escalated basis. After `c3d1c02`
  and `f8cbd0f` this should be rare; if it is not, that is a regression, not a tuning target.
- **`o36_ineligible`** → **now the dominant blocker (69 of 103 decisions) and therefore the first
  thing to work on.** On Test 10, 8 of 10 such classifications were honest and 2 were
  indeterminate, because `eligibility_summary` drops diagnostics carrying `target_modality`. At 20
  cases a 20% blind spot was tolerable; at 69 it is not — **persist that diagnostic before drawing
  any conclusion from this code**, or two thirds of the histogram remains unreadable. Only once the
  honest and indeterminate cases can be told apart is it worth asking whether the O36 eligibility
  gate itself is too strict for a cold campaign.

### Carry into this slice

- **The `eligibility_summary` diagnostics drop**, above. It is attribution machinery, so it belongs
  here rather than in R2b.
- **A slope-dependent section width.** `5b90b9c` switched window sizing from the marginal
  ("vanadium") energy FWHM to the section (`bragg.dE`) — 1.1577 vs 0.4616 meV at (2,0,0,0), a
  factor of 2.51 that is exactly `sqrt(M[E,E] * inv(M)[E,E])`. A constant-Q scan measures a
  section, so this is right in kind. The exact width of a *dispersive* mode is a slope-dependent
  section, focusing or defocusing with the sign of the dispersion against the ellipsoid tilt, and
  lies between the two. TAVI exposes `projections.q_par_E` / `q_perp_E` for this.
- **Budget calibration.** The vanadium-era campaign reached `met` in 59 iterations partly *because*
  it undersampled by ~2.5× in both window width and step. Correct sampling costs more beam per q,
  and the 3600-count deterministic budget was calibrated against the cheaper, wrong sampling. This
  is arithmetic, not a bug — but a campaign that now ends on budget rather than `met` should be
  read that way before anything is "fixed".
- **Core width policy.** `max(2*FWHM, 3*sigma)` is a *half*-width, so a core spans four FWHM, and
  the prediction sigma (0.002–0.07 meV in a converging campaign) never binds. Whether four FWHM is
  the right core is a physics question nobody has yet asked deliberately.

### Do not do here

Do not re-tune `O41_MARGIN_SE_MULTIPLE` or the per-offset agreement rule *before* reading the
histogram. Those gates were installed deliberately in `2ea22c8` after the R1-E replay granted
authority on margins of ~1.7e-5 nats/point that flipped role between partition offsets. Loosening
them without attribution re-opens exactly that failure.

## Part 3 — STAT-2b protected-hash cleanup

Four protected-hash mismatches are open, surfacing as six failing tests in
`tests/stats/test_stat2b_source_closure.py`. They have been the only red in the suite all through
the R-series.

| File | Category | Origin |
|---|---|---|
| `isar/drive/objective.py` | protected_adjacent | pre-existing (budget constants) |
| `isar/drive/session_log.py` | protected_adjacent | pre-existing |
| `isar/session/driver.py` | **protected_semantic** | introduced by slice 10 |
| `isar/session/worker.py` | protected_adjacent | introduced by slice 10 |

Close them through the candidate/active closure cycle governed by S9 — authenticate the aggregate's
raw bytes, reproduce its semantic fingerprint, and write a candidate against its source and graph
identities. Promotion needs an explicit owner decision and a separate minimal commit that changes
no protected production source (rule 7).

Two things not to get wrong. **Rule 5 is not suspended and never was**: do not refresh the completed
held-out gate or the aggregate to match current production. And `driver.py` is protected-*semantic*,
not merely adjacent, so it needs selection or evidence-contract review with proportionate proof
rather than the bounded source review the other three take.

## Known open items, not owned by this plan

- **Sub-floor model pruning.** `model_weight_floor` is 0.005 and `pruned_model_weight` is 0.0 while
  models carrying ~1e-11 are retained. Had pruning worked, the predictive artifact would have been
  homogeneous and the blocker `f8cbd0f` fixed would never have existed. Worth a look on its own.
- **R2b — `decide_eig_window()` consolidation.** ~210 duplicated lines across `legacy_window`,
  `alternative_window` and `_family_window` in `isar/drive/acquisition/physical_windows.py`, called
  from `scan_design.py`. Deferred behind the tuning slice deliberately: tuning will be reading and
  adjusting that window policy constantly, and will map which of those lines actually vary between
  call sites. Sweep the unrealistic-fixture pattern at the same time.
- **Open-or-create workspace lifecycle.** Pointing the GUI at an empty folder cannot start a
  campaign there, because `save_as` requires a `ready` workspace and a new one has no content yet.
  Closing that needs a decision about what New and Open mean, not a patch.
- **Legacy survey window span.** A survey scan spanning the whole declared search band at
  resolution sampling is expensive by construction. Explicitly declined 2026-08-05 on the grounds
  that EIG-designed scans will displace it; recorded here so the decision is not silently revisited.

## Verification

Per AGENTS.md: targeted tests while iterating, one parallel not-slow run and one parallel full run
at each slice boundary, full serial before merging to `main`. Six failures in
`tests/stats/test_stat2b_source_closure.py` are expected until Part 3 lands; **any seventh failure
is real**.

A tuning change is verified by a campaign, not by unit tests. Re-run the cold start and compare
grant rate and the blocker histogram against the R3 baseline recorded above:

```
isar drive --spec <goal>.json --workspace <ws> \
  --scan-window-eig-mode authoritative --tier deterministic \
  --analysis-limit 1h --seed 0 --control-token r3stop
isar diagnose --workspace <ws> --eig
```

TAVI must be reachable; its API base is `http://127.0.0.1:8642/api/v1` (the bare port answers 404
on every path, which is not a health signal — use `/api/v1/health`).
