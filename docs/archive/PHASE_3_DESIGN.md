# ISAR Phase 3 — Analysis on messy real data (Resolve — the back half of the R)

> **Lifecycle:** Archived
> **Provenance:** hand-written
> **Verified against code revision:** `d0ea1df` (2026-07-25)
> **Superseded by:** `DESIGN.md` §7. Kept for the design reasoning it records, which `DESIGN.md` summarizes but does not reproduce. Not maintained; never a normative source.

**Status:** Draft for discussion. The major framing forks were worked through in a design session and
are settled (the error *spine*, physics-gated structure enumeration, modality-branched reporting, the
growing-point-set contract); a handful of genuine engineering decisions remain open and are marked as
such in §15 (the TAS 4-D convolution engine, the Bayesian sampler, bootstrap `M`, the energy-zero fit
method). This is **not** yet at handoff readiness the way Phase 2 was — it is the design-session
artifact to cut into before a `HANDOFF_PHASE_3.md` is written.

**Relationship to the master design:** This is the concrete realization of `DESIGN.md` §7.5 (Analyze —
DHO ⊗ resolution), §8 (the physics encoding: peak form, raw-energy validity, resolution dimensionality),
§12 (confidence & triage scoring), and the *solving* of the calibration problems §7.4 names but Phase 2
deliberately left as fields-and-flags. It is the **Resolve** half of the floating R: normalized, QC'd
scans → fitted phonons with trustworthy, decision-grade uncertainty and a human-readable verdict.

**Relationship to Phase 1:** Phase 1 built the DHO ⊗ resolution fitter (`isar/synth/fit.py`), the three
swappable estimators (`estimators.py`, Baker–Cousins gated), and — crucially — a Monte-Carlo scoring
harness (`score.py`) that validates *against known truth*: draw M Poisson realizations, refit, look at
bias and the **pull distribution**. Phase 3's central move is to **promote that harness from
MC-against-truth to per-scan MC-against-fit** (parametric bootstrap), because on real data there is no
truth and only one realization. The MINOS/HESSE comparison (`plot_minos_hesse`) and the "untrustworthy
σ" detector (`TRUST_SIGMA_FACTOR`, the Γ→0 corner where HESSE collapses) are reused directly — they were
the synthetic-world prototypes of the per-scan error machinery built here.

**Relationship to Phase 2:** Phase 3 consumes the normalized `ScanRecord` JSON — **honoring the QC mask,
never applying it** (a masked point stays in the file; the fitter skips it, reversibly, per P5/0.4) —
plus the experiment card (`policy.toml` + `state.json`) and the shape-only QC verdicts. Phase 2
deliberately produced no dispersion prior and solved no calibration; Phase 3 adds the analysis-stage
objects that were missing — the running dispersion prior, the fitted energy-zero series, the resolution
model in use — and writes its fit results and confidence back as session state.

---

## 0. Read this first — five corrections to the framing

The brief that kicked off Phase 3 listed four decisions as co-equal (initial conditions, how many peaks,
when to discard, how to estimate errors). The list is right; the framing of it is not, in five ways that
shape the whole design. They go up front.

**0.1 Error estimation is the spine, not one of four ribs.** The IC and peak-count decisions only feed
the fit; the *uncertainty* is what the human acts on, what §12 confidence is built from, and — the part
that matters most — it is the **entire input to Phase 4** ("put beam where the error is too big"). A
wrong error bar doesn't just mislabel one scan; it misdirects every future-measurement decision built on
top of it. So the other three decisions are treated here as *upstream of* a trustworthy error, and the
design budget is spent accordingly: the error layer (§5) is the largest and most load-bearing section.

**0.2 Peak structure is physics-gated enumeration, not a free peak count.** The naive implementation —
"try 1, 2, 3 peaks, pick the lowest-cost / best-IC" — throws away the fact that `DESIGN.md` §8 already
established the peak-form problem as **over-constrained**. Detailed balance decides whether an
anti-Stokes peak is even *admissible* before one is fitted: at 10 K a 5 meV phonon puts the anti-Stokes
at ~0.3 % of Stokes (I_AS/I_S = exp(−ℏω/k_BT) ≈ exp(−5.8)), so **a second peak on the gain side at low
T is the elastic line, not a phonon.** The structure search enumerates only the hypotheses physics
permits for this (ω, T, zero-status); it does not brute-force a peak count. And the model-selection
criterion is **AIC/BIC or a nested F-test — never raw χ²** (the abandoned `fit_phonons` used raw χ², which
systematically fits noise as a phantom phonon; this is a named anti-pattern, not a free choice).

**0.3 The three upstream decisions collapse into one loop.** IC, peak count, and part of the discard call
are not three separate stages — they are one loop: *enumerate physically-admissible structure hypotheses
(§2) → build a structure-aware deterministic IC for each (§3) → fit each → model-select / average across
them with a modality test (§4) → estimate within- and across-model uncertainty (§5).* Designing them as
one loop keeps the physics gating in charge of which hypotheses ever enter, and makes "how many peaks" a
*consequence* of model selection over admissible structures rather than a knob.

**0.4 "Discard" is never a delete — it is classification into a named non-result bucket.** Continuity with
P5 and Phase 2's 0.4: the analysis stage never destroys data and almost never emits a bare "unusable." A
scan that doesn't yield a fit is classified — *held-for-calibration*, *unconstrained*, *underpowered*,
*no-signal*, or (rarely) *junk* — each with a machine-readable reason that lands in the report (§6). The
important sub-point: at low counts a real phonon looks like "no peak," so **"no signal" must be expressed
as a detection-power statement relative to the dispersion prior** ("this scan lacked the statistical power
to see the phonon the prior expects here"), not as "there is no phonon." That distinction is the hook into
Phase 4.

**0.5 Frequentist and Bayesian errors answer different questions — the comparison is the product.** The
parametric bootstrap (the frequentist path, the one the *Errors-in-Fitting* demonstration already
embodies) gives the **sampling distribution of the estimator**. Bayesian posterior sampling gives the
**posterior given the data and a prior**, and lets the dispersion prior act as a literal prior on ω. They
*agree* in the well-counted regime (Bernstein–von Mises) and *diverge* exactly where the scan is sparse or
the prior is doing the work — and that divergence is itself the most honest "trust this number?" signal we
can compute. So the error layer is built like the estimators and backends before it: **pluggable, run
side-by-side, with the agreement between paradigms as a first-class reported diagnostic.** This is the
third instance of that pattern in the repo, and it is deliberate.

---

## 1. Purpose & what "messy" actually means

Phase 3 closes the pipeline: it takes the clean-but-unfitted normalized scans from Phase 2 and produces,
fast enough to steer a beamtime, a fitted phonon (ω₀, Γ, amplitudes) **with a decision-grade uncertainty
and an explicit verdict** — a number, a fork, or a named non-result — plus the confidence and provenance
the human and the future planner need.

**"Messy" is the whole problem, and it is messy in ways the Phase 1 generator never produced.** The
generator defines a tidy world (one phonon, flat background, known truth). Real scans bring:

| Messiness | Where it bites | Phase 3 response |
|---|---|---|
| Structured / correlated background (tail of a strong neighbouring mode) | flat-BG assumption fails, ω biases | background model is a structure-hypothesis component (§2), not a constant |
| Two unresolved modes / a shoulder | 1-vs-2-peak ambiguity | the modality fork (§4) — emit both, don't average to the gap |
| Spurions, accidental Bragg, λ/2 leakage (TAS especially) | a sharp non-phonon feature | physics gate + residual diagnosis (§2, §12); flagged not fitted-as-phonon |
| Apparent Γ varying with dispersion slope (TAS focusing) | Γ is instrumental, not intrinsic | 4-D resolution convolution (§8); ω trustworthy before Γ |
| Missing / drifting energy zero (HERIX) | peak assignment couples to calibration | solve the zero (§7); missing → flagged non-result, never E₀=0 |
| Low counts, weak peak | the error bar itself is unreliable | the two-paradigm error layer + agreement diagnostic (§5) |

The thread tying these together: **the dominant uncertainty on a messy scan is usually structural ("is it
one peak or two? is that the elastic line or a phonon?"), not the within-model σ.** A design that reports
a tight σ on a wrong structure is worse than useless, because it is confidently wrong, and for a
dispersion curve a confidently-wrong number propagates into the prior and distorts every neighbour. §4 and
§5 exist to make that failure impossible — the reported uncertainty must include the structural ambiguity.

---

## 2. The structure-hypothesis loop (physics-gated)

The deterministic core of Phase 3. For each scan (with its fixed context — resolution version, energy
zero, T, the prior's expectation at this Q), enumerate the **physically admissible** peak-structure
hypotheses, fit each, and let model selection (§4) decide among them.

**Admissibility is gated by §8 physics before any fit runs:**

- **Elastic line.** Admissible iff the scan's zero-status permits it and the window includes E≈0. On a
  HERIX scan with no fitted zero, "is the feature at zero" is *unanswerable*, which couples structure to
  calibration (§7) — the loop must not assume a clean calibrate-then-fit ordering for no-elastic high-K
  scans.
- **Stokes phonon.** Always admissible if the prior (or the scan shape) suggests a feature on the loss
  side.
- **Anti-Stokes phonon.** Admissible **only** when k_BT approaches ℏω — the Bose factor the DHO already
  ties the anti-Stokes amplitude to is evaluated *before* an anti-Stokes component is added. At low T this
  gate removes the Stokes+anti-Stokes hypothesis from the table, so a gain-side feature is forced to be
  fitted as the elastic line. This is the single most important guard against the classic
  misassignment.
- **Second phonon / shoulder.** Admissible when the prior at this Q suggests two branches, or when a
  residual-driven pass (§12, LLM, off critical path) identifies an unfitted shoulder. *Not* enumerated by
  default — added only on evidence, so the loop doesn't manufacture phantom peaks.
- **Background.** Constant by default; a sloped or structured background becomes a hypothesis component
  only when the residual demands it (a strong neighbouring mode's tail). Over-flexible backgrounds eat
  real signal, so this is conservative.

The output of the gate is a small set of admissible structures (typically 1–3): e.g. `{elastic+Stokes}`,
`{elastic+Stokes+Stokes2}`, or at high T `{Stokes+antiStokes}`. Each is handed to §3 for a structure-aware
IC and then fit. **Peak count is never an independent knob** — it is whatever the admissible-structure set
contains, decided among by §4.

---

## 3. Initial conditions

The grid + NNLS `initial_guess` already in `fit.py` is the right backbone and is kept: scan Ω_d
candidates, at each fix Γ₀ and solve the linear amplitudes (A, E, BG) by non-negative least squares (the
model is exactly linear in them), pick the lowest-estimator-cost candidate. It is robust where a raw
argmax is noisy, and it already carries the elastic line in the NNLS basis.

**The one change Phase 3 forces: ICs are a function of the structure hypothesis (§2), not a free guess.**
A single-phonon hypothesis uses the existing 1-D Ω_d grid; an elastic+Stokes hypothesis adds the elastic
column to the NNLS basis (already supported); a two-phonon hypothesis needs a 2-D Ω_d grid or a
residual-seeded second centre; an anti-Stokes hypothesis seeds the gain-side amplitude from the Bose
factor at the trial ω. This is why §0.3 collapses IC into the structure loop: the IC generator is
parameterized by the hypothesis.

**Prior-first, LLM-second**, per `DESIGN.md` §8: when the running dispersion prior has a confident ω(Q)
at this point, it seeds Ω_d directly (ω(Q) is smooth; neighbours are a strong prior) and the grid only
refines locally. The LLM IC proposal is a *fallback* for the sparse-prior / hard-shape case, and the
residual-diagnosis LLM pass runs **after** deterministic attempts, off the critical path. Early in a
beamtime the prior is sparse, so the deterministic grid+NNLS does most of the work — which is the right
default, since it is reproducible and model-free.

**Empirical note (IN8 Phase-1 check, 2026-06-29 — `docs/in8_phase1_check.py`).** The grid+NNLS IC seeds
ω₀ correctly with no prior on clean data (055182 → 4.47 meV). It surfaced two soft spots the structure
loop must handle: (a) when the elastic line is **off-window** (IN8 FX=2, EN≥1, elastic at 0), the elastic
NNLS column has no in-window leverage and becomes **degenerate** — the fit hands it an arbitrary huge
amplitude that contributes ~0, so it is not actually modeling anything; and (b) at the *correct* (Cooper–
Nathans) resolution a sharp elastic can at most spike the single nearest point, so the elastic+Stokes
hypothesis's NNLS basis must include a **broad quasi-elastic / background** column (§8), not just the
resolution-width elastic. The single-DHO IC also could not represent the two-feature scan (055181), as
expected — the structure enumeration (§2) is exactly the fix.

---

## 4. Model selection & modality-branched reporting

Given the fits of the admissible structures (§2), decide what to report. The resolution of the design
fork ("fold across-model uncertainty into the bar, or keep it separate?") is **neither fixed policy — the
data's shape decides, deterministically.**

**Step 1 — select / weight, never by raw χ².** Score each admissible structure by AIC/BIC (or a nested
F-test for strictly nested structures). Compute Akaike weights `wᵢ = exp(−½ΔAICᵢ) / Σⱼ exp(−½ΔAICⱼ)` (the
Bayesian path uses evidence/marginal-likelihood weights, §5). Structures with negligible weight drop out;
the survivors are the ones the data genuinely supports.

**Step 2 — build the pooled across-model distribution of ω** (and Γ): the weight-mixed combination of each
surviving structure's within-model ω distribution (the bootstrap sample or posterior from §5, not just a
point ± σ). This mixture is the honest representation of "what does this scan say ω is, accounting for not
knowing the structure."

**Step 3 — the modality test disposes** (deterministic, so it stays on the deterministic side of P1):

- **Unimodal pooled distribution** (the surviving structures agree on ω within their intervals): **fold
  in.** Report one ω with an error that already includes the structural spread. This is most scans. The
  error is honestly inflated relative to any single-model σ — which is the point.
- **Multimodal pooled distribution** (the structures genuinely disagree — the 1-vs-2-peak fork, or
  elastic-vs-phonon at an uncalibrated zero): **do not collapse it.** The mean of a bimodal distribution
  is the least likely value and corresponds to no physical peak; averaging here would emit an actively
  wrong number sitting in the gap. Instead **report the modes with their weights and raise a
  disambiguation flag** — "ω is either 5, or 5 and 8; re-measure to separate." The "error" here is not a σ;
  it is a discrete fork.

The fork is a first-class report shape, not an error condition. It propagates: §9 collation must accept a
Q-point that has not yet resolved to a single ω (it holds both candidates, does not feed an averaged ghost
into the dispersion prior), and the prior treats a forked point as *unresolved*, not as data. This is the
behaviour signed off in the design session: forcing conformance to a model is good, and emitting a fork
when no single model fits is better than emitting a confident average of two.

---

## 5. Error estimation — two paradigms, agreement as the diagnostic

The spine (§0.1). The reported uncertainty has two layers — **within-model** (this section) and
**across-model** (§4, folded or forked) — and within-model is estimated by **two independent paradigms run
side-by-side**, because their agreement is the trust signal.

### 5.1 Retire the Jacobian as the reported error
The *Errors-in-Fitting* demonstration is the justification, and it is exactly a parametric-bootstrap study:
generate from a model under Poisson noise, fit, and compare the **covariance/Jacobian error** (`curve_fit`
`pcov`, equivalently HESSE — the parabolic approximation to the cost surface) against the **actual spread of
the fitted value around truth**. They diverge in precisely the beamtime-triage regime: low counts, weak
peak, nonzero/structured background, and parameters against a boundary. Near Γ→0 the Hessian goes
near-singular and σ blows up or collapses (the corner `TRUST_SIGMA_FACTOR` patches). The Jacobian
covariance is symmetric by construction when the truth is skewed. **Conclusion: HESSE is kept only as a
fast convergence check; it is never the reported error.**

### 5.2 Frequentist path — parametric bootstrap (Tier 2, the authoritative number)
The bridge from the demonstration to production. Fit the scan; take the best-fit DHO ⊗ resolution model as
the generating model; draw `M` Poisson realizations of the counts; refit each under **Baker–Cousins** (the
unbiased estimator — Neyman/Pearson would bias the bootstrap centre, per `estimators.py`); the scatter of
the refit parameters **is** the error. No truth required, only the fit. Properties that matter:

- It **reproduces the very experiment that showed HESSE is wrong**, per scan.
- It yields **asymmetric, boundary-aware intervals** for free (percentile interval; BCa optional if
  coverage matters).
- It **exposes bimodality** — the two-phonon ambiguity appears as a double-peaked bootstrap distribution,
  feeding §4's modality test.
- It **turns every real scan into its own mini score-cell**, so the `TRUST_SIGMA_FACTOR` outlier logic and
  the pull-style diagnostics written for synthetic data become computable per real scan.

It is embarrassingly parallel and has **no silent-failure mode** — a degenerate fit shows up as a wide or
multimodal bootstrap, not as a wrong-but-confident number. Cost is `M`≈100–200 refits/scan; affordability
is settled (§11 — points arrive ~1 min apart, so there is ample time to refit the whole growing set per
point; bootstrap is fully within budget and runs async off the inter-scan critical path).

### 5.3 Frequentist fast cousin — MINOS (Tier 1, inline)
iminuit already provides profile-likelihood (MINOS) errors: cheap (one likelihood scan per parameter),
asymmetric, boundary-aware, non-parabolic. MINOS is the **asymptotic limit the bootstrap converges to** in
the well-counted regime, so it is the fast inline default that goes in every report, with the bootstrap
reserved for the authoritative number and for flagged/ambiguous scans. **Where MINOS and the bootstrap
disagree, the scan is out of the asymptotic regime** — a free, built-in "too sparse to trust the fast
error" detector, computed by running both.

**Empirical note (IN8 Phase-1 check).** On real IN8 TAS data MINOS was produced for *only* the scans that
fully converged (6 of 17); the rest yielded no usable covariance/profile at all. So on sparse TAS data
**convergence itself is the first discriminator**, upstream of the MINOS↔bootstrap agreement test — the
bootstrap (§5.2), which has no silent-failure mode, is what must carry these scans. One concrete gap to
close here: the Phase-1 `fit()` reports MINOS on the fitted Ω_d/Γ but **not on the derived ω₀** (ω₀ carries
a HESSE-propagated error only); §5 must propagate the profile/bootstrap interval to ω₀, the reported
quantity.

### 5.4 Bayesian path — posterior sampling
Answers the different, complementary question, and brings three things the frequentist side structurally
cannot:

- **The dispersion prior becomes a literal prior on ω**, not a soft IC seed. On an ill-constrained scan the
  posterior is regularized toward the neighbours instead of running to a boundary — the right triage
  behaviour, *and* a liability that must be made visible (§5.5).
- **Nuisances are marginalized by integration** (background, elastic amplitude), so the ω interval honestly
  absorbs background uncertainty rather than profiling it out.
- **Boundaries are handled by prior support** (Γ≥0, A≥0) — the cleanest fix to the Γ→0 corner that
  `TRUST_SIGMA_FACTOR` currently patches; the posterior simply piles mass appropriately, no Hessian
  pathology.

**The cost is a silent-failure mode the bootstrap does not have:** a poorly-mixing chain returns a
confident, wrong interval with no exception. So **convergence diagnostics are load-bearing, not polish** —
R̂ / ESS for MCMC, or nested sampling (e.g. dynesty, which returns the evidence as a bonus for §4's weights)
with its own stopping criterion. A failed diagnostic must route the scan to "Bayesian interval
untrustworthy → fall back to frequentist + flag," consistent with `DESIGN.md` §9's "make degradation
visible." With a 5-parameter cheap model, NUTS (numpyro) or nested sampling converges fast; the discipline
is only that the failure is *caught*.

**Build note (step 7, 2026-07-01 — `isar/analyze/errors/bayes.py`).** Implemented as **dynesty static
nested sampling** (D9 resolved, §15): gradient-free, so it calls the *identical* Baker–Cousins likelihood
the bootstrap/MINOS use — no JAX reimplementation, no model divergence to contaminate §5.5 — and returns the
evidence as a bonus. The dispersion prior is folded into the log-likelihood as `Normal(ω₀, σ)` because
ω₀=√(Ω_d²+Γ²) is non-separable in the sampled (Ω_d,Γ). The silent-failure gate is keyed on **ESS**
(nlive-robust) rather than `logzerr` — the latter scales √(H/nlive), so it measures the live-point *budget*
and evidence precision (the §4 follow-on), not interval validity. A failed gate *or* any sampler exception
sets `bayes_ok=False` → frequentist fallback + flag.

### 5.5 Agreement is the product
In the well-counted regime the two paradigms agree (Bernstein–von Mises) and either is reported. **Where
they diverge, the divergence localizes why,** and that is a reported diagnostic and a §12 confidence input:

- Frequentist interval ≫ Bayesian interval → the prior is doing most of the work → "this ω is mostly
  dispersion-prior, little data" → a re-measure flag, and a circularity caution (§6).
- Bootstrap multimodal but MINOS tight → structural ambiguity the fast error hides → route to §4's fork.
- Both wide and disagreeing → genuinely uninformative scan → *unconstrained* (§6).

The error layer is therefore pluggable and side-by-side, exactly like estimators and backends. The
deliverable per scan is not "ω ± σ" but a small object: `{within_model: {freq_interval, bayes_interval,
agreement_metric}, across_model: {folded_interval | fork}, paradigm_divergence_flags}`.

**Build note (step 7).** `agree.py` now carries the full **three-method** error-extraction cross-check on ω₀
— the HESSE / bootstrap / posterior half-widths as data, plus the `prior_driven` / `fork_suspected` /
`unconstrained` flags — and stays backward-compatible with the frequentist-only call. `attach(with_bayes=,
prior=)` writes `bayes_interval` / `log_evidence` / `bayes_ok` into `within_model`. Consumption of the new
flags by `confidence.py`/`verdict.py`, and of `log_evidence` by `select.py` as evidence weights (§4), are the
step-7 follow-ons (§14).

---

## 6. Discard — non-result classification (never a delete)

Per §0.4 / P5. A scan that yields no fitted number is classified into a named bucket with a reason and
surfaced in the report; nothing is destroyed.

| Bucket | Trigger | Nature |
|---|---|---|
| **held-for-calibration** | no resolution version for this analyzer yet; or HERIX scan with no fitted energy zero | a **sequencing** hold (`DESIGN.md` §4 bootstrap gates), not a quality judgment — revisited when calibration arrives |
| **unconstrained** | no admissible structure converges; or both paradigms wide and disagreeing; or bootstrap multimodal with no prior to break the tie | held, not deleted |
| **underpowered** | the prior expects a phonon here, but the scan lacked the counts to detect it at the achieved statistics | a *detection-power* statement, **not** "no phonon" — and the direct Phase 4 hook ("expected ω here, needs ~N more counts") |
| **no-signal** | no excursion above background **and** the prior does not expect one here | genuine absence; still flagged, not deleted |
| **junk** | catastrophic GoF for the best admissible structure **and** a residual with no interpretable structure (noise everywhere) | rarest bucket; the only one resembling "unusable" |

**The underpowered/no-signal split is the subtle one** and reuses `score.py`'s `data_threshold` logic on a
single real scan: predict from the prior where and how strong the phonon should be, then ask whether this
scan had the statistical power to see it. If not → *underpowered*, not *no phonon*.

**The circularity guard (the §0.4 / Q3 caution).** Using the prior to drive a detection-power test risks a
loop: prior says "expect a peak here" → scan flagged underpowered → never contributes to the prior → the
prior never improves. The guard: an *underpowered* verdict is provisional and prior-conditional, it is
flagged as such, and it never **subtracts** confidence from the prior (it cannot mark a region "confirmed
empty"). Early in a beamtime the operator is present to break such loops by hand (a stated operating
assumption — Phase 3 is not "fully automated"), so this is a guard, not a hard requirement, but it is
designed in so Phases 4/5 inherit it.

**Empirical note (IN8 Phase-1 check).** Two of these buckets got concrete evidence. The repeat pairs make
the *underpowered* split real: the strong np=25 Q=(2,0,4) pair (055289/055294) reproduces ω₀ to ~0.08 meV,
while the sparse np=12 and weak-feature pairs scatter 1–2 meV — exactly the low-power instability the
detection-power test must catch. And the unmodeled broad quasi-elastic tail (§8) is precisely the
*structured residual* that must **not** be classed as *junk*: GoF is poor (cost/dof ≫ 1) yet the phonon is
real and ω₀ is recovered — so the junk trigger's "residual with no interpretable structure" clause is
load-bearing, and a high cost/dof from a known-missing component (quasi-elastic) should route to a
"model-incomplete" flag, not to junk.

---

## 7. Calibration solving (the part Phase 2 deferred here)

Phase 2 carries the fields and the three-state `raw_abscissa_kind` + `status` but solves nothing. Phase 3
solves the cases the analysis actually needs:

- **HERIX energy zero (`needs_zero`).** Fit the elastic line of calibration/elastic scans to get E₀, build
  the **time-keyed** zero series (scan number ≈ time), and **interpolate to the scan's timestamp** — because
  mono drift is *temporal*, the same T measured hours apart needs different zeros. This corrects the
  abandoned codebase's two named anti-patterns at once: keying the zero by **temperature** instead of time,
  and **silently defaulting to E₀ = 0** on a missing zero. In ISAR a **missing zero is a flagged
  non-result** (`held-for-calibration`, §6), never E₀ = 0 — defaulting to zero silently asserts the IN8
  fixed-kf case on a HERIX instrument and emits a hidden offset into ω.
- **IN8 / HB-3 (`absolute`).** Fixed-kf geometry → the raw EN column is the energy transfer, centre fixed
  at 0; no zero to fit. The loop takes the absolute axis directly.
- **ESRF ID28 (`needs_conversion_and_zero`).** The raw abscissa is monochromator *temperature*, needing a
  T→E conversion *and* a zero. **Deferred within Phase 3** (ID28 is not a near-term instrument); the field
  and status exist so the loop refuses to fit it rather than mis-fitting it.

---

## 8. Resolution convolution by instrument

"One DHO + resolution convolution" is too flat (`DESIGN.md` §8): the DHO core is shared, the **convolution
operator is per-card**, and the two instrument classes differ in *dimensionality*, not just value.

- **IXS (S30, ID28, BL43) — 1-D Voigt.** Resolution is tight in Q and near-1-D in energy, so the
  Voigt-convolved DHO already in `_ff.py` / `fit.py` is legitimate. This is the validated path and the
  Phase 3 default.
- **TAS (IN8, HB-3) — 4-D tilted ellipsoid.** Resolution is a tilted 4-D ellipsoid in (Q,E), and the
  **apparent** linewidth depends on the dispersion slope cutting through it (focusing/defocusing): a steep
  mode in a defocused configuration broadens *instrumentally*, so a naive 1-D fit reads that as intrinsic
  Γ. Intrinsic Γ on IN8 requires convolving the DHO against the Cooper–Nathans/Popovici matrix and the
  *local dispersion* (ResLib/Tobyfit-style). **Consequence for triage: on TAS, ω₀ is trustworthy before Γ
  is** — the loop can report a calibrated ω from a 1-D fit while marking Γ as *resolution-limited / not
  yet deconvolved* until the 4-D path is in. Whether to wrap an existing engine or implement the
  Cooper–Nathans projection ourselves is an open decision (§15-D5).

**Empirical validation (IN8 Phase-1 check, 2026-06-29 — `docs/in8_phase1_check.py`, `out/in8_phase1/`).**
The 1-D Voigt core was run against the real Fe₂Mo₃O₈ IN8 numors and **recovers ω₀** (clean anchor 055182 =
4.47 meV ≈ hand-vetted 4.2; converged on 6/17 `bs qh` scans). The S30 IXS positive control (same core,
resolution *measured* from the in-window elastic line) recovers a full ω₀(K) dispersion with **meaningful Γ**
— confirming the IXS↔TAS difference is the convolution *dimensionality*, not the core. Two concrete results
for this section:

- **Cooper–Nathans is now computed from the numor header** (DM/DA/KFIX/ALF/BET/ETA), ported from ResCal
  `rc_cnmat` into the check (`cn_resolution_matrix` / `cn_energy_fwhm`, Cooper & Nathans 1967). It gives the
  elastic energy FWHM **≈0.58 meV** for this config (≈Q-independent at the elastic line; ~0.70 at the
  phonon), corroborated by the cot(θ_M)·collimation estimate. **This scalar CN energy resolution is the
  precursor to the 4-D engine (D5) and the first candidate for `isar/synth/resolution`** — it already
  removes the "guess the resolution" step for TAS.
- **The broad low-E tail in the data is NOT resolution — it is real quasi-elastic / diffuse scattering.** A
  width sweep shows the fit's cost/dof only bottoms out near ~1.5 meV, far broader than any plausible CN
  resolution (0.58), so a sharp elastic line cannot and should not absorb it (at the CN width the elastic
  can only spike the single nearest point). Fe₂Mo₃O₈ is magnetic ⇒ likely magnetic quasi-elastic. **The TAS
  card's structure basis (§2/§3) therefore needs a quasi-elastic/background component**, distinct from the
  elastic Voigt and the flat BG. This is a fit-quality / Γ issue only; ω₀ is recovered throughout — the
  concrete embodiment of "ω trustworthy before Γ on TAS."

---

## 9. Confidence, triage scoring & collation

**Computed confidence, not LLM self-report** (`DESIGN.md` §12). Replace any `high/medium/low` self-rating
with a number built from the quantities §4–§6 already produce: agreement of ω across the admissible
structures (§4), the parameter intervals themselves (§5), the cross-paradigm agreement (§5.5), distance
from the running dispersion prior, and **provenance (P6)** — a fit on a sparse prior + provisional
resolution version is inherently lower-confidence than the same fit late in the run, and the confidence
must say so. This computed confidence is what routes a scan to human review and what Phase 4 will act on.

**Collation feeds the prior, and must accept forks.** The deterministic groupby (§7.6 of the master,
`Scan_Combining.py` reference) updates the running dispersion prior — but a **forked** Q-point (§4) is
*unresolved*, not data: it holds both candidates and feeds neither averaged ghost into the prior. The
prior tracks per-(sample, T) ω(Q) with confidence, and a low-confidence or forked point cannot masquerade
as a tight constraint on its neighbours.

---

## 10. Validation

**FeSeS_1 is necessary but not sufficient.** The gold standard (79/83 human-validated fits, curated
`fit_results.csv`, `DESIGN.md` §11) is the primary per-scan scoring target — every Phase 3 verdict (which
ω, which fork, which discard bucket) is scored against it, exactly as the harness scores synthetic recovery
against truth. But FeSeS_1 is **one sample on one instrument (S30 IXS)**, and validating the analysis layer
only there would bake in HERIX-flavoured messiness. The peak-count, discard, and error logic must also be
exercised on **at least one TAS (IN8) dataset with trusted fits**, because TAS is messy in a structurally
different way (4-D focusing makes apparent Γ vary, spurions are common, backgrounds are structured). Until
that second-instrument scoring exists, the "generalizes across instruments" claim of `DESIGN.md` §1 is
untested *for the analysis layer specifically* — Phase 2 proved it for parsing, Phase 3 must prove it for
fitting. This is the analysis-layer analogue of Phase 2's "synthetic round-trip + real-file smoke test"
pair: synthetic recovery (truth known) for coverage, two real instruments for fidelity.

The synthetic harness also gains a new self-test: **bootstrap calibration.** On synthetic cells where truth
*is* known, the per-scan parametric-bootstrap interval can be checked for coverage (does the 68 % interval
contain truth 68 % of the time?) against the MC-against-truth pull width. If per-scan bootstrap coverage
matches the cohort pull width, the promotion of §0.1 is validated; if not, that mismatch is itself a
finding.

**Build note (step 7, `tests/analyze/test_bayes_coverage.py`).** The bootstrap-calibration self-test is
promoted to a three-method comparison (HESSE / bootstrap / posterior). A concrete, honest finding: on the
clean synthetic generator **HESSE does not degrade on ω₀** — ω₀ is a *location* parameter, robustly recovered
even at low counts (covariance valid and ≈0.6 coverage down to target=6), and Γ here sits far from its floor —
so all three roads *coincide* here. The Jacobian's documented failure (§5.1, `TRUST_SIGMA_FACTOR`) is the
Γ→0 / parameter-at-boundary / structural-ambiguity corner of *real* messy data, which this generator does not
manufacture; the gate therefore makes no synthetic HESSE-breakdown claim. What it *does* prove is the honest
Step-7 product: **bootstrap ≈ posterior, both calibrated on ω₀**, across a well-counted and a sparse regime —
the two roads that replace the fragile Jacobian mutually confirming each other exactly where a single error
estimate is least trustworthy. Consistent with §8's "ω₀ trustworthy before Γ".

---

## 11. The growing-point-set contract (forward design for Phases 4/5)

"Refit everything after every point" (the settled Phase 4/5 operating mode) is a bigger statement than
"bootstrap is affordable." It means the natural object is not a finished scan but a **growing point set**,
and it imposes a contract on the fit/error layer *now*, even though Phase 3 only ever calls it on finished
scans:

- The fit + error estimate is a **pure function of `(points_so_far, model, prior, context)`** — no
  whole-scan-at-once assumptions, re-invocable after each new point.
- **Warm-start** each refit from the previous point's result; the grid+NNLS cold start (§3) runs once, then
  subsequent points perturb. Re-entrant, cheap per point.
- Built this way, Phases 4/5 inherit the loop for free instead of needing a rewrite. The cost of honouring
  the contract in Phase 3 is near-zero; the cost of retrofitting it later is a rewrite.

**Planted flag for Phase 4 (peeking / optional-stopping bias).** Using the shrinking per-point error as a
*stopping rule* biases the result: stopping the instant σ_ω drops below target preferentially stops on
downward fluctuations (classic sequential-analysis "peeking"), so stopped-early ω's carry a small
systematic pull. The shrinking interval is a fine *monitor*; as a *trigger* it needs a conservative margin
or an alpha-spending-style correction. Acknowledged as a Phase 4 concern, recorded here so the Phase 3
error object carries what Phase 4 will need to correct it.

---

## 12. LLM role in Phase 3

Unchanged from `DESIGN.md` §9, and strictly off the path of any fitted number (P1):

- **IC / candidate-structure proposal** — only when the dispersion prior is sparse or the shape is hard;
  seeded by the prior first (§3). Batched, T2/T3 tier.
- **Residual diagnosis on a poor fit** — the one place an LLM beats rules: look at the actual fit+residual
  figure (vision tier) and propose a *structural* fix ("unfitted shoulder at +6 meV → add a Stokes
  component, seed ω there"), which re-enters §2 as a new admissible hypothesis. Runs **after** deterministic
  attempts, **async, off the critical path** — the speed value proposition forbids an LLM in the per-scan
  inline loop.
- **Triage narrative** — turn the computed confidence and the verdict (number / fork / bucket) into one or
  two human sentences per scan for the report.

The model never decides the peak count, never picks the ω, never sets the error, and never decides a
discard — those are deterministic (§2–§6). It proposes structures and explains verdicts; deterministic code
disposes.

---

## 13. Module layout (proposed)

```
isar/
  analyze/
    structures.py     # §2 physics-gated admissible-structure enumeration (detailed balance, zero-status)
    ic.py             # §3 structure-aware IC (grid+NNLS backbone, per-hypothesis basis, prior seed)
    select.py         # §4 AIC/BIC/F-test, Akaike weights, pooled across-model distribution + modality test
    errors/
      bootstrap.py    # §5.2 parametric bootstrap (Baker-Cousins, percentile/BCa), per-scan score-cell
      profile.py      # §5.3 MINOS wrapper (reuses fit.py iminuit path)
      bayes.py        # §5.4 posterior sampling (NUTS / nested) + convergence diagnostics + fallback
      agree.py        # §5.5 cross-paradigm agreement metric, divergence flags
    verdict.py        # §6 non-result classification (buckets + reasons + detection-power test)
    confidence.py     # §9 computed confidence from §4/§5/§6 + provenance
  calibrate/
    energy_zero.py    # §7 HERIX elastic-line fit, time-keyed series, interpolate-to-timestamp
  resolve/
    voigt1d.py        # §8 IXS path (reuses _ff.py); the validated default
    tas4d.py          # §8 TAS 4-D ellipsoid convolution (engine TBD, §15-D5)
  collate/
    prior.py          # §9 running dispersion prior; accepts forked/low-confidence points as unresolved
  report/
    analysis.py       # extends Phase 2 report: verdict, fork, confidence, paradigm-agreement, provenance
tests/
  gold/               # §10 FeSeS_1 per-scan scoring against fit_results.csv
  gold_tas/           # §10 the second-instrument (IN8) scoring set — the generalization guard
  bootstrap_cov/      # §10 synthetic bootstrap-coverage self-test
```

Run (cmd):
```
python -m isar.analyze.run <card> <scan_or_run>   :: fit one scan/run → verdict + confidence + report
python -m isar.tests.gold                          :: score against FeSeS_1 (the gate)
python -m isar.tests.gold_tas                       :: score against the IN8 trusted set (generalization)
```

---

## 14. Build order

1. **Structure loop skeleton** (`structures.py`, `ic.py`) over the *existing* 1-D Voigt fitter — admissible
   enumeration + structure-aware IC, single-phonon and elastic+Stokes first. Measurable immediately against
   a few FeSeS_1 scans.
2. **Model selection + modality** (`select.py`) — AIC/BIC, Akaike weights, pooled distribution, the
   unimodal-fold / multimodal-fork decision. The fork is the new report shape; wire it through.
3. **Frequentist errors** (`bootstrap.py`, `profile.py`, `agree.py`) — promote the `score.py` MC machinery
   to per-scan bootstrap; MINOS inline; the agreement metric. **Validate bootstrap coverage on synthetic
   truth here** (§10) before trusting it on real data.
4. **Verdict + confidence** (`verdict.py`, `confidence.py`) — the non-result buckets, the detection-power
   test with its circularity guard, the computed confidence.
5. **Energy-zero calibration** (`energy_zero.py`) — time-keyed series, interpolate-to-timestamp,
   missing→held. Unblocks the no-elastic high-K HERIX scans and breaks the structure↔calibration coupling.
6. **Gold-standard scoring** (`tests/gold`) — score every verdict against FeSeS_1. This is the gate; nothing
   after step 2 should be called done without it.
7. **Bayesian path** (`bayes.py`) — posterior sampling + diagnostics + fallback; the second paradigm and the
   full agreement diagnostic. Heavier; comes after the frequentist path is scored. *Done:* dynesty static
   nested sampling (D9), ESS-gated convergence with frequentist fallback, the dispersion prior as a literal
   Normal prior on ω₀; `agree.py` extended to the three-method (HESSE / bootstrap / posterior) error-extraction
   cross-check with `prior_driven` / `fork_suspected` / `unconstrained` flags; `attach(..., with_bayes=, prior=)`
   writes `bayes_interval` / `log_evidence` / `bayes_ok`; synthetic three-method coverage gate
   (`tests/analyze/test_bayes_coverage.py`). *Follow-ons (out of step 7):* teach `confidence.py`/`verdict.py` to
   consume the new flags, and wire `log_evidence` into `select.py` as an evidence-weight alternative to the
   Akaike weights (§4).
8. **Second-instrument generalization** (`gold_tas`) + **TAS 4-D resolution** (`tas4d.py`) — prove the
   analysis layer on IN8; report ω before Γ until the 4-D path lands. *Started:* the IN8 Phase-1 check
   already validates ω₀ recovery on real IN8 and provides a **scalar Cooper–Nathans energy resolution from
   the header** (port of `rc_cnmat`); promote it to `isar/synth/resolution` and add a quasi-elastic/
   background structure for the magnetic QE tail (§8). The remaining gap is the focusing-dependent Γ (the
   full 4-D ellipsoid + local dispersion).
9. **Growing-point-set re-entrancy pass** — confirm the fit/error layer honours the §11 contract (warm-start,
   pure function of points-so-far), so Phase 4 inherits it.
10. **LLM residual-diagnosis path** (`analyze` ↔ LLMM) — async, off critical path, only after the
    deterministic loop is scored and trustworthy.

Steps 1–6 deliver a fully deterministic, scored, single-paradigm Phase 3 on the validated IXS path — a
shippable triage tool. 7–8 add the second paradigm and the second instrument. 9–10 set up Phase 4 and add
the one LLM capability that earns its place.

**Build note (step 8, 2026-07-01).** The second-instrument generalization (`gold_tas`) is built and green
(the 4-D `tas4d.py` remains deferred, D5). The scalar Cooper–Nathans energy resolution is promoted from the
Phase-1 check to `isar/synth/resolution.py` (`cn_energy_fwhm`, ≈0.58 meV on 055182, fallback on a closed
triangle). A **fixed-width, amplitude-only** `QuasiElastic` component (`isar/analyze/components.py` — a Voigt
whose *Lorentzian* part is the broad QE width, so it slots into the NNLS basis as one linear column, no new
nonlinear parameter) is admitted for TAS via a card-declared `quasielastic_fwhm`
(`in8.policy.toml` → `CardPolicy`/`ScanContext` → `structures.quasielastic_admissible`); `enumerate_structures`
then adds `stokes+qe+bg` / `elastic+stokes+qe+bg` and AIC/BIC decides per scan (the deterministic realization
of "added only on evidence"). The full Phase-3 pipeline is scored against a thin hand-vetted IN8 anchor set
(`test_data/ILL IN8/IN8_fit_results.csv`; only 055182 ≈ 4.2 meV and the aborted 055183 are vetted truth) by
`docs/phase3_gold_in8_check.py` — **prior-free**, `center=0`, CN resolution. Result: recovers ω₀ on 055182
(|Δ| = 0.31 meV) and declines the 3-point 055183 (safety 0); the QE component demonstrably earns its parameter
(055194 cost/dof 8.4→2.1; 055182/055186 have *no* usable single-DHO fit → usable with QE). Honest scope: a
generalization **smoke test** (~5 anchors), not a statistical validation — the 83-scan IXS gold stays the
gate, and the verdict layer **over-flags to `junk` on TAS** (the known §6 refinement — route a high cost from
a known-missing component to `model_incomplete`, not `junk` — is the follow-on, not step 8). *Remaining (D5):*
the focusing-dependent Γ (the full 4-D ellipsoid + local dispersion); ω₀ before Γ until then.

---

## 15. Decisions

**Resolved (in the design session):**

- **D1 — error estimation is the spine; HESSE is retired as the reported error (§0.1, §5.1).** The Jacobian
  covariance is a fast convergence check only; reported errors come from profile/bootstrap/posterior.
- **D2 — peak structure is physics-gated enumeration, not a free peak count; selection by AIC/BIC/F-test,
  never raw χ² (§0.2, §2, §4).**
- **D3 — modality-branched reporting (§4).** Build the pooled across-model distribution; unimodal → fold the
  structural spread into one error bar; multimodal → emit a fork (modes + weights + disambiguation flag),
  never an average sitting in the gap. The fork is a first-class report/collation/prior shape.
- **D4 — two error paradigms run side-by-side; their agreement is a reported diagnostic (§0.5, §5).**
  Frequentist (parametric bootstrap, with MINOS as the inline cousin) and Bayesian (posterior with the
  dispersion prior as a literal prior). Divergence localizes *why* and feeds confidence.
- **D6 — build growing-point-set-native (§11).** Fit/error layer is a re-entrant pure function of
  points-so-far; Phase 3 calls it on finished scans, Phases 4/5 inherit it.
- **D7 — discard is classification into named non-result buckets, never a delete (§6);** underpowered vs
  no-signal split via a prior-driven detection-power test with a circularity guard.
- **D8 — missing HERIX energy zero is a flagged non-result, never E₀ = 0; zero series keyed by time, not
  temperature (§7).** Directly corrects two abandoned-codebase anti-patterns.

**Open (need a call before / during build):**

- **D5 — TAS 4-D convolution engine:** wrap an existing engine (ResLib / Tobyfit) or implement the
  Cooper–Nathans/Popovici projection ourselves? Decides how soon IN8 *Γ* (not just ω) is trustworthy.
  Inherited from `DESIGN.md` §13-Q3. *Partial progress:* the **scalar CN energy resolution is already
  implemented** (port of ResCal `rc_cnmat` in the IN8 Phase-1 check, §8) and gives the right elastic FWHM
  (~0.58 meV), so the "ourselves" path is demonstrably feasible; the open part is specifically the 4-D
  ellipsoid + local-dispersion focusing that makes Γ (not ω) trustworthy.
- **D9 — Bayesian sampler: RESOLVED → dynesty (static nested sampling).** Built in step 7
  (`isar/analyze/errors/bayes.py`). The forward model (`convolute_voigt_dho`, numpy FFT + interpolation) is
  **not differentiable**, so NUTS would need a JAX reimplementation that risks diverging from the frozen numpy
  model and contaminating the §5.5 agreement diagnostic; a gradient-free sampler calls the **exact** Baker–
  Cousins log-likelihood the bootstrap/MINOS use, keeping the statistical model provably identical across
  paradigms. Nested sampling also returns the marginal-likelihood **evidence** (`log_evidence`, for the §4
  evidence-weight follow-on) and has a self-contained `dlogz` stopping criterion. Two refinements found in
  build: (a) interval quality is gated on **ESS** (nlive-robust), not `logzerr` (which scales as √(H/nlive) —
  it governs *evidence* precision, the §4 follow-on, not interval validity); (b) the ω₀ prior is folded into
  the log-likelihood because ω₀=√(Ω_d²+Γ²) is a non-separable function of the sampled parameters. A failed
  convergence gate sets `bayes_ok=False` → frequentist fallback + flag. `nlive` (default 400) is a D10-adjacent
  empirical pin.
- **D10 — bootstrap `M` and interval type:** `M`≈100–200 expected affordable (§11), percentile vs BCa.
  Pin empirically against the coverage self-test (§10, step 3).
- **D11 — prior representation for IC seeding across temperatures:** ω(Q) shifts with T near transitions, so
  a naive cross-T prior can mislead. How much prior is shared across T? Inherited from `DESIGN.md` §13-Q6.

---

## 16. Explicit non-goals (anti-creep)

- **No closing of the measurement loop** — Phase 3 *produces* the confidence and the underpowered/forked
  verdicts a planner would consume, but proposes no next measurement. That is Phase 4 (§11 sets up the
  inputs; see `../campaigns/PHASE_4_NOTES.md`).
- **No automated data collection or instrument control** — Phase 5.
- **No LLM in the path of a fitted number, an ω, an error, a peak count, or a discard** — the LLM proposes
  structures and narrates verdicts, off the critical path (§12, P1).
- **No physical deletion of any data point, ever** — masks honored, non-results classified, all reversible
  (§0.4, P5).
- **No ID28 T→E conversion built** — the `needs_conversion_and_zero` status makes the loop refuse it
  cleanly; solving it waits on ID28 being a near-term instrument (§7).
- **No final, paper-grade fits** — Phase 3 is triage; the system of record is a later deterministic re-run
  from frozen config (`DESIGN.md` §1 non-goals).
- **No stopping-rule / peeking correction** — flagged for Phase 4 (§11), not built here.
