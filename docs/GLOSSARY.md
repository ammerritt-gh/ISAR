# ISAR glossary

> **Status:** live
> **Authority:** canonical for term definitions
> **Verified against code revision:** `1310969` (2026-07-30)

Plain-language definitions for the terms in ISAR's reports and figures. Written for a scientist who
knows inelastic scattering and manual fitting but is new to ISAR. (Seeded during the
documentation pass; expanded alongside the reading guide and statistics primer.)

## What ISAR decided — verdicts, and what you should do

ISAR is a triage tool: it makes a reasonable first pass and is explicit about what it could not do.
Every scan gets one verdict. The point of the verdict is to tell you **where to spend your manual time**.

| Verdict | Meaning | What to do |
|---|---|---|
| **resolved** | ISAR is confident in the fit. | Use the number (still your call to confirm). |
| **flagged** (`model_incomplete`) | Usable energy, but the model is imperfect (e.g. a structured residual). | Glance at it; the ω₀ is probably fine. |
| **junk** | A fit was attempted but the result is not trustworthy. | Look manually — ISAR is steering you here, nothing is deleted. |
| **unconstrained** | The data don't pin the parameters down. | Look manually / consider recounting. |
| **held_for_calibration** | Missing the energy zero (or other calibration) needed to fit. | Resolve calibration first, then re-run. |
| **underpowered** | Too few counts to constrain a fit yet. | Recount if the point matters. |
| **no_signal** | No peak detectable above background. | Skip, or recount much longer. |
| **elastic_only** | Only the elastic line (+ background) was resolvable in this scan — a positive but scan-local claim, confidence capped at 0.5. | Treat like a miss for steering; recount or re-window if the point matters. |

"Delivered" in the reports = **resolved + flagged** (ISAR handed you a usable number). Everything else
is ISAR saying *"look here yourself"* — which is a feature, not a failure. **Nothing is ever deleted**
("flag, don't delete"): a declined scan is still in the file with the raw data intact.

## Fitting & lineshape

- **energy transfer** — the energy the probe lost or gained (meV); 0 meV is elastic scattering.
- **phonon / DHO** — the inelastic peak ISAR fits, modelled as a damped harmonic oscillator in the full form of the paper's Eq. 2 (`STATS_PRIMER.md` §8).
- **elastic line** — the peak at zero energy transfer (no energy exchanged).
- **energy zero** — where the elastic line sits on the energy axis; defines 0 meV. ISAR self-calibrates
  it from dedicated elastic/Bragg scans.
- **pipeline zero** — the energy zero taken as the beamline's own file axis, used for an analyzer
  whose elastic line is not significant. It is labelled `pipeline_zero` in the alerts; ISAR never
  assumes a zero of 0.0 on an uncalibrated axis.
- **setting ladder** — how ISAR picks a scan's analyzer-slit setting, and so its resolution curve. A
  slit the card or header declares is kept (a clearly better slit only raises `slit_mismatch`). With
  none, the slit whose curves best fit the elastic lines is used (`slit_inferred`), and
  `slit_ambiguous` says the runner-up is nearly as good.
- **Voigt** — a Gaussian convolved with a Lorentzian; ISAR's instrument-resolution shape.
- **resolution** — the instrumental width the data is smeared by (a Gaussian width and a Lorentzian
  width); ISAR measures it from elastic/Bragg scans. For BL43LXU it is the beamline's measured curve
  for each analyzer and slit, used in place of the Voigt.
- **quasi-elastic** — a broad component sitting near zero energy transfer (real signal, not
  resolution). Since SCENE-1 it is its own catalogue phenomenon — a measurement target in its own
  right (linewidth ⇒ relaxation rate), distinct from *diffuse* scattering (broad in Q, elastic in
  E; scene-level, not yet catalogued).
- **phenomenon catalogue** — the single registry (`isar.phenomenon-catalogue/1`) of what can be
  present in a scan: acoustic, optic, elastic, quasielastic, unknown, bg_only. Each entry declares
  its local models and gates (the physics stays in `analyze/phonon.py`), plus dormant parameter
  priors, transport declarations, and constrained variants consumed when Laplace evidence lands.
  Structure enumeration is catalogue-driven, including the phonon-less nulls `elastic+bg` and `bg`.
- **elastic_only** — the verdict when the phonon-less `elastic+bg` candidate wins selection with a
  significant elastic line: a positive, scan-local, categorically humble claim (confidence
  hard-capped at 0.5); never region exclusion, never a Bragg identification.
- **constraint transport** — carrying parameters fit in a clean region as *soft* constraints into
  complicated regions (crossings, spurions), assuming slow variation; declared per catalogue entry
  (smoothness class + scale in r.l.u.), one-directional, applied exactly once via the existing
  soft-constraint path. Deviation from the transported expectation is itself a signal (an avoided
  crossing shows as intensities swapping while energies hold) — a typed observation, never
  silently absorbed. Declared at PR-a; consumed at PR-b.
- **residual** — data minus fit. Flat noise is good; leftover structure means the model missed something.
- **cost/dof** — the fit's misfit per degree of freedom; a reduced-χ²-like number (ISAR uses a Poisson
  likelihood, see the statistics primer). Near 1 is a good fit; ≫1 means the model doesn't match.

## Uncertainty & model choice

- **pull** — a difference divided by its error bar (a normalized residual). A pull of 1 = one σ away.
- **bootstrap** — resample the measured counts many times and refit, to get the spread (error bar) of an
  estimate without trusting the fit's curvature.
- **HESSE** — the quick error bar from the fit's curvature (the Hessian). Fast, but optimistic at low
  counts; ISAR uses it as a cross-check, not the authority.
- **Bernstein–von Mises** — the statistical result that the bootstrap and HESSE error bars agree when
  there are plenty of counts; ISAR checks this per scan.
- **credible interval** — a Bayesian posterior interval, reserved for quantities that really are
  posteriors. ISAR computes one for ω₀ and Γ under `--errors bayes` (`bayes_interval`), but it is
  **diagnostics-only as a single-fit interval**. A pooled master's legacy certifying interval can
  include nested posterior particles inside a heterogeneous model-dependent mixture; that mixed
  interval is not itself called credible. Confidence intervals (bootstrap, profile) are never
  called credible. See `STATS_PRIMER.md` §5 (rulings S5/S6).
- **AIC / Akaike weight** — a way to compare candidate models that penalizes extra parameters; the
  weight is relative information-criterion support within the exact scored model set (higher =
  better supported), not a posterior model probability or Bayes factor. Since H4 this is the
  recorded **diagnostics lane**: AIC/AICc/BIC and Akaike weights are computed and persisted per
  candidate but drive nothing — selection is the evidence posterior — except on the named
  fallback lanes (`aic_no_evidence_inputs`, `aic_all_evidence_invalid`,
  `evidence_budget_exhausted`), which are selection-ineligible for acquisition steering.
- **evidence (lnZ) / validity ladder** — the marginal likelihood of one candidate structure under
  its declared priors, produced by a ladder that never trusts a Gaussian volume silently: Laplace
  at the fitted optimum, a warm-started MAP refit when the fit did not see a prior it must
  integrate, `laplace_truncated` for a Γ-floor-only pin, or dynesty nested sampling for other
  boundary-pinned/curvature-failed fits. Every record carries its rung, escalation reasons, and
  (for dynesty) the n_live used; evidence is comparable only within one data-likelihood arena.
  Dominated candidates (evidence ceiling ≥30 nats below a trusted leader) are excluded without
  sampling and recorded as such.
- **laplace_truncated** — the evidence rung for a converged fit whose single pin is one `.Gamma`
  at its floor: rung-1 Laplace with a boundary-mass correction, since the Γ floor is a modelling
  artifact and the boundary is integrable, not an invalidity (H9(b)). E0 window-edge pins,
  unknown pin status, Γ at its ceiling, and two-plus pinned Γs (whose joint truncated mass
  depends on their covariance, which the per-axis product cannot see) all still escalate to
  dynesty. The mass factor is gradient- and offset-aware — exact for the
  local linear-plus-quadratic exponent, so it no longer assumes a half-Gaussian at a railed bound
  nor that θ̂ sits exactly on the floor. The residual gap against dynesty on the synthetic railed-Γ
  probe is within the sampler's own seed scatter and, if real at all, may be attributable to
  HESSE curvature at a railed axis — not to the boundary factor (H10(b)).
- **constraint origin** — where an effective soft constraint on a fit came from; selection
  eligibility requires every effective constraint be soft with a prespecified origin.
  `user_declared` is the scientist's pre-fit soft declaration, prespecified by construction, and
  enters lnZ exactly once (H4(c), H9(a)); eyeballed seeding carries no origin class at all.
  `catalogue_variant` is a catalogue `ConstrainedVariant`'s hard pin (predicate + pins) — it
  competes as its own additional candidate rather than folding into the base structure, and is
  deliberately never prespecified-soft, since a hard-conditioned fit is not an unconditioned
  data-likelihood estimate (H9(c)). For steering it disqualifies the scan only by WINNING: a
  non-winning offered variant contributes neither eligibility nor reasons to the selection
  aggregate and adds no EIG science codes (H10(a)); any other constraint origin keeps the
  all-soft-prespecified eligibility rule.
- **evidence identity / identity axis** — identity is open-vocabulary, catalogue-driven, and lives
  in (Q,E) space as cross-scan objects (`label@entry`) alongside the structure axis: a scan's fit
  contributes per-candidate evidence, splitting structure × identity twins only where rival
  entries declare distinguishing priors (the no-dilution rule). The persisted per-label lnZ is the
  scene store's aggregation input for SCENE-3; branch assignment keeps driving delivery until
  SCENE-6, with `identity_ba_disagreement` recorded, never acted on (H9(d)).
- **unresolved candidate / escalation budget** — the escalation phase of one candidate set is
  bounded in wall clock. A candidate skipped when the budget runs out is recorded `unresolved`
  with its evidence ceiling: like a dominated candidate it holds no lnZ and enters no posterior,
  but unlike one it was never *ruled out*. If its ceiling still reaches the best trusted evidence
  in the set, the set cannot name a winner on evidence and selection degrades to the recorded AIC
  lane (`evidence_budget_exhausted`). A skipped candidate whose own ceiling sits below the leader
  is ruled out by that bound and costs the set nothing. The budget is a backstop against a
  pathological set, not a speed control — the expensive class (a superset of the winner) has a
  ceiling at or above the leader's by construction, which is exactly why dominance pruning cannot
  touch it.
- **posterior model probability** — P(s) = π_s·Z_s / Σ π·Z over admissible structures, uniform π;
  the weight that drives selection, pooling, and the EIG/master consumers since H4. Persisted per
  candidate in `isar.fit_selection_semantics/2` alongside the AIC diagnostics; `None` on the
  recorded AIC fallback lanes rather than invented.
- **count ladder, rung 1** — the cheap rung that decides the peak count of every spectrum: Laplace evidence for
  each count from 0 to the mode ceiling, with and without the elastic line, several starts per model. It never
  samples. (Not the same as the Q rung below.)
- **count ladder, rung 2** — nested sampling of the *contending* counts, run only when a trigger fires: a split
  that competes with its parent, starts that disagree, two modes merged, a mode amplitude at zero, structure left
  in the residual, a count at the ceiling, or a Laplace evidence that is invalid.
- **contending model** — a model rung 2 samples: within 10 nats of the best rung-1 evidence, or the count above
  the best, or one with no usable rung-1 evidence.
- **count state** — the one state of a spectrum's peak count: `fitting`, `provisional` (rung 2 still pending),
  `settled`, `search limited` (the ceiling or the budget stopped the search), `unresolved` (a contender's evidence
  could not be computed; a failed calculation is never evidence against a model), or `held` (not fitted, because
  its calibration is missing). Its reasons are recorded beside it. See `USING_ISAR.md`, "Reading the peak count".
- **ambiguous count** — the runner-up count has probability at least 0.20. Both counts and both probabilities are
  reported, the verdict is `unconstrained`, and the count stays out of the dispersion prior.
- **degenerate limit / twin** — a fit railed at a lower limit is the degenerate case of a model next to it: a
  mode's amplitude or the elastic height at zero, a mode's energy at zero, a mode's width at its floor (the
  resolution-limited delta-function mode), or the background at zero (the background-free model). That neighbour
  is its twin. The railed model is not a second vote for its twin.
- **resolvable (benchmark)** — a synthetic truth whose extra mode a reference fit finds in at least 90% of Poisson
  draws, and whose weakest-removed version is split in at most 5%. The benchmark's measurable sense of "a human could
  fit it".
- **count flag** — a quality warning on a fit, kept apart from its state: `structured residual`, or `central
  residual, quasi-elastic not tested`. The second never states that the quasi-elastic component is absent.
- **statistical semantics** — additive producer metadata declaring what estimator, constraints,
  model denominator, interval/particle measure, probability-like score, and authority scope a
  quantity actually has. Missing metadata leaves a legacy quantity valid but semantically
  unavailable; readers never fabricate producer provenance. Candidate comparison identity is split
  into data-likelihood, model-parameterization, constraint, and combined-context fingerprints;
  predictive/projection semantics are self-authenticated when present.
- **shadow selection lane** — the diagnostics-only `isar.model_selection/2-shadow` comparison
  introduced by STAT-2a. It exactly reuses a complete unconstrained delivery fit bundle when
  possible; otherwise it fits the complete standard candidate family without delivery constraints.
  Schema `/2` binds each candidate to the declared data-likelihood context and distinguishes exact,
  partial, fresh, skipped, and unavailable execution while retaining `/1` read compatibility.
  Its weights and changed-selection flag cannot affect verdicts, intervals, certification,
  stopping, acquisition, or cache identity. The evidence-backed successor it anticipated landed
  as production selection under H4; the shadow lane stays AIC-labeled by design (it measures
  criteria, D6).
- **authority scope** — one named use for which a statistical quantity is active, diagnostic,
  shadow-only, inactive, or ineligible. Certification, stopping, EIG, acquisition selection, and
  user diagnostics are separate scopes. For intervals, a producer's capability is distinct from
  the realized authority of the value that survives reliability, triage, and tracking gates.
- **model interval** — the current pooled-master ω quantile from retained model-weighted
  particles (evidence posteriors since H4; the record's `model_weight_kind` names the lane). It
  is conditional on target-present models and may mix nested, bootstrap, and HESSE/Laplace
  particles. It remains active for legacy certification and stopping, but is ineligible for EIG
  or acquisition.
- **fork / fold** — a *fork* is two or more equally-supported answers reported separately (the data are
  ambiguous); a *fold* is a single agreed answer.

## Running ISAR — cards, workspaces, and the prior

- **experiment card** — the one place all per-experiment decisions live, in two halves: a
  human-edited `card.policy.toml` (detectors, column names, collation rules, your fitting
  guidelines — ISAR never writes it) and a machine-written `card.state.json` (accumulated state:
  calibration versions, the dispersion prior, exclusions).
- **workspace** — the per-experiment directory `isar init` creates: the card, the session ledger,
  and the `report/` output. One experiment = one workspace.
- **session ledger** — ISAR's bookkeeping (`session.json`): which files it has seen and what
  happened to each scan. Deleting it is safe (a re-run rebuilds it); the card state is not.
- **idempotent run** — re-running `isar run` on the same data does no redundant work: every file
  is re-read, but only scans that are new, changed, previously *held*, or touched by a card edit
  (a new guideline or exclusion) are (re)fitted.
- **held** — a scan ISAR is not ready to fit (most often `held_for_calibration`: no energy zero
  measured yet). Held scans are re-examined automatically on every later run; ISAR never guesses
  the missing piece.
- **fact source** — where a scan's or spectrum's value came from: `header` (a `# key value` line in the
  file, or its trailing `#Q=`/`#T=` line when no leading header gives it), `trailer` (a trailing line
  that disagrees with the leading header, kept as a conflict), `sidecar` (a beamline file beside the
  data, such as `.var`), `filename`, `card` (your `[facts]`), `inferred` (ISAR derived it) or `missing`.
  The first found wins in that order; a disagreement is flagged `fact_conflict`.
- **Q rung** — how a spectrum's momentum transfer Q was obtained, best first: rung 1, the analyzer's
  own HKL from the file; rung 2, computed from the sample angles and orientation (planned); rung 3,
  computed from the arm-centre HKL and labelled approximate (planned); rung 4, no Q (`q = None`). The
  spectrum is still identified by its scan and analyzer, with or without Q.
- **dispersion prior** — the running ω(Q) expectation ISAR builds from its own resolved fits
  (same sample and temperature), used to seed initial conditions and sanity-check new fits. Only
  clean, single-answer (*fold*) results enter it; ambiguous (*fork*) results never do.
- **expectation / guideline** — what *you* tell ISAR in the card's `[fitting]` section: expected
  phonon energies (per Q range or per scan), restricted model choices, known-bad scans. Guidelines
  narrow what the automation tries; they can never force a physically inadmissible model.
- **provenance** — every reported number carries where its inputs came from: which resolution
  version, which energy-zero source, whether the seed was your expectation or the auto prior and
  how many neighbors informed it.

## Steering vocabulary (the autonomous driver)

- **branch** — one dispersion curve ω(Q) (e.g. the transverse acoustic phonon). A campaign
  targets one branch; others it encounters are **rivals**.
- **branch assignment / assignment entropy** — the normalized, versioned assignment-score
  distribution over branch labels ({acoustic, optic, new, …}), and its Shannon entropy. It is
  useful probability-like state, not a calibrated Bayesian posterior; high entropy means the
  scorer distributes weight across several declared assignment states. Shown as a color channel
  in the GUI.
- **branch belief** — the driver's per-branch dispersion estimate: a weighted local regression
  over that branch's confirmed points, with honest uncertainty growth away from data.
- **excitation hypothesis** *(branch hypotheses Implemented; `level`/`continuum` Partial —
  `docs/HYPOTHESES.md`)* — your declared physics
  expectation for the target (and rivals) in `card.policy.toml`: character (acoustic / optic /
  custom), zone-centre energy with uncertainty, flatness, confidence. It weights the driver's
  seeding, classification, and scan choice; it never forces a fit.
- **confidence** — a heuristic triage score produced by `isar.confidence/1`, not a calibrated
  probability that a result is correct.
- **differentiation tolerance** — the looser precision to which a *rival* branch is
  measured: enough to tell it apart from the target, never the target's tolerance. Rivals get
  identification, not publication error bars.
- **sanity-check opening move** — the campaign's first scans probe the declared
  hypothesis's most discriminating feature (a gapped target: the zone centre at the claimed
  gap), so a grossly wrong expectation is caught immediately.
- **zone-centre energy prior** — a Gaussian prior truncated below zero, so it cannot predict a
  negative excitation energy. The zero-mean acoustic case is therefore a half-normal; a gapped
  optic prior with a positive mode is a general lower-truncated normal, not a half-normal.
- **repair / drop** — supervisor actions with deliberately different consequences: `repair`
  modifies a rejected proposal and retries it; `drop` skips that point rather than resubmitting it.

## Scene vocabulary (the cross-scan layer)

- **scene** — ISAR's reading of a whole stretch of q at once, rather than one scan at a time. It takes
  each scan's finished candidate comparison as given and asks what set of readings hangs together
  across neighbouring scans. See `docs/design/scene.md`.
- **scene factor** — one declared term of the scene's score, or one declared non-scoring job. A factor
  says up front what it reads, what it is tuned to, and how large its term can get; nothing may
  contribute an undeclared bonus. Its jobs are: **couples** (which scans belong in one cluster),
  **score** (a term in the belief), **propose** (offer an alternative reading), **classify** (label a
  component without moving belief), **refine_visibility** (reinterpret why something was not seen),
  **couples_arena** (cluster on a whole neighbouring scan's coverage rather than on one of its
  components), and **degradations** (say why the factor had nothing declared to work with).
- **visibility state** — why a hypothesized excitation is, or is not, present in a neighbouring scan.
  **observed**: something was seen there. **searched_no_signal**: that scan looked at that energy and
  found nothing — the *only* absence that counts against a claim, and only after the detectability
  gate. **censored_outside_footprint**: that scan never looked there, so it is no evidence either
  way. **blocked_unresolved**: something made the region uninformative — the elastic line, an
  anti-Stokes band too Bose-suppressed to have shown anything, a look too shallow to compare, or a
  continuation nothing declared says where to expect — look again with better data, and the
  report's `reason` says which. **structural_endpoint**: see below.
- **structural endpoint** — an absence the *declared physics* explains, so nothing is missing. A
  gapless branch reaches zero energy at a zone centre and disappears under the elastic response; a
  scan there sees nothing not because the branch is absent but because it has run out of energy to
  have. It is granted to a READING, not to a mode: the scene must be looking at a linked track that
  descends toward a declared fold through a run of elastic-bearing scans. A lone low-energy feature
  beside a zone centre gets no such explanation, and neither does a flat track. Distinct from
  **blocked_unresolved** in what you should do: blocked means count longer,
  structural endpoint means no amount of counting will produce a peak. Never penalized.
- **measurement-arena record** — one line per measured arena in `report/measurements.jsonl`
  (`isar.measurement-arena/1`), written independently of whether any fit succeeded: counts are
  authoritative, fits are disposable interpretations on top. A pre-dispatch stub survives even a
  killed worker, so "we measured there" can never be erased by an analysis failure. What the
  scene may credit as coverage is decided by its **count_trust**.
- **count_trust** — the measurement record's terminal state: **trusted** (usable counts — the
  only state that earns a footprint and coverage credit), **excluded** / **held_for_calibration**
  / **undersampled** (gate outcomes: attempted, recorded, no coverage), and **attempted** (the
  pre-dispatch stub — the delivery never reported back). Everything non-trusted is recorded as
  attempted but claims nothing.
- **transported conditioning** — the non-scoring reason an arena gets when its winning candidate
  comparison ran under priors *derived from other arenas' data* (branch rescue and its kin).
  Summing its evidence as independent would count the source arena's neutrons twice, so the
  arena is experiment-map-only until a real transport contract exists (#21, interim Variant A).
  Priors the scientist *declared* are not transport — declared information is counted exactly
  once in the local evidence and scores normally.
- **scene partition** — the boundary no scene coupling may cross: `(sample identity, condition
  set, lattice-epoch frame token)`, with the campaign-path component joining when path
  provenance lands. Different samples, different binned conditions (temperature now; any
  declared dimension exactly), or different lattice interpretations are different physical
  scenes — no cluster edges, no linkage, no presence charges, no endpoint readings across the
  boundary, and knowledge factors go inert on partitions whose frame is not the declared one.
- **sample knowledge** — what the scientist DECLARED about the sample, read off the experiment card:
  the reduced-zone period, the Bragg reflections along the scan path, the steepest dispersion slope
  any branch may have (meV per rlu — the only thing that says how far a track could have moved
  between two q, which the energy resolution does not), and the lattice epoch in force.
  Never inferred from the data. Declaring nothing is allowed; the capabilities that need it then go
  inert and say so in the revision's `sample_knowledge.degradations`.
- **Bragg (at the scene)** — whether an elastic feature sits where a reflection was declared is a
  *scene-level* classification (`bragg_compatible` / `off_lattice` / `unclassified` when no
  reflections were declared / `unplaceable_q` when they were but this scan has no usable q), not a fit
  result: a single scan's elastic fit is blind to where in reciprocal space it sits. Crucially, the zone period
  does not confer a reflection — FCC (300) has integer indices, sits on a fold, and is extinct. The
  classification moves no belief, because elastic intensity is ubiquitous: off-lattice elastic is not
  improbable, only not-Bragg.
- **scene revision / scene pointer** — one scored scene, stored as a content-addressed file under
  `<workspace>/report/scene/<digest>.json`, plus `current.json` (`isar.scene-pointer/1`) naming the
  revision the workspace is currently showing and the one it succeeded. Lineage and the write time
  live in the pointer and never inside the digested revision, so rebuilding an unchanged workspace
  reproduces the same digest and "did anything change?" is answerable by comparing hashes. Nothing
  is ever deleted; a missing scene reads as "no scene", while a damaged one (a pointer naming a
  missing file, or content that no longer matches its digest) is an error rather than an empty
  answer.
- **hypothesis map** — the renderable projection of a stored revision: what the scene expects at
  each scan it scored, and where it claims nothing. Three axes are kept apart on purpose — mode
  markers and coverage gaps are in intrinsic |ω| (unsigned mode energy), while a fitted curve is on
  the signed energy axis the scan was measured on, and folding one onto the other is the plot's
  job. Where the scene's leading reading is not the model the scan actually delivered, no curve is
  drawn and the reason is recorded: drawing it would put one hypothesis's intensity under another
  hypothesis's name.
- **footprint (coverage)** — where a scan actually looked, in intrinsic |ω|: the *cells* of the
  bins it kept, merged into runs, folded onto the unsigned axis (a window spanning zero covers part
  of |ω| from both the Stokes and anti-Stokes sides). Built from the kept bins rather than from the
  window's end points, so a QC-masked or excluded stretch in the middle is a hole in coverage, not
  a searched region — absence inside a hole is never evidence.
- **detectability gate** — the checks an absence must pass before it counts against a claim. The
  neighbouring scan must have been able to *see* the excitation, not merely to have looked at that
  energy: an anti-Stokes-only band must not be too Bose-suppressed to have shown anything, the
  neighbour must have counted comparably deep (within a factor of ~3 of the exposure that saw the
  mode), and — where that neighbour holds other peaks of its own — where the track should have
  reappeared must be knowable from a declared dispersion slope, since a neighbour full of other
  features is no unambiguous non-detection (a covered neighbour holding nothing still counts).
  Anything unknown fails closed — the absence is demoted to **blocked_unresolved** with the
  reason recorded, never charged.
- **path topology / adjacency** — scans are ordered along the path they were measured on, derived
  from their q alone; two scans are neighbours only if they are *consecutive* on that path.
  Adjacency is never a radius in q: a radius makes every scan inside it a neighbour of every other,
  so adding an intermediate scan would leave the old skip-neighbour judgement standing beside the
  two new ones and price one absence twice. A q set that is not a path at all fails closed — no
  ordering, no cross-scan coupling, and the reason recorded.
- **linkage / linkage prior** — which component at one scan is the *same track* as which component
  at its neighbour. Two readings of the same scans can differ only in their linkage; that is the
  branch-crossing ambiguity. An assignment that admits several linkage readings must not thereby
  outweigh one that admits a single reading, so each reading carries a `ln(1/n)` prior over the
  readings *enumerated* for it — a hypothesis is never boosted by being enumerated. If the cap
  drops readings, the share of prior mass dropped is recorded and the cluster is reported
  unresolved rather than narrowed.
- **component descriptor** — what a fitted candidate is made of, written down at fit time: an
  ordered list of `mode` / `elastic` / `quasielastic` entries (with the mode's slot into that
  candidate's energies). The scene reads component meaning from these and from nothing else — a
  display label is a name, never a schema to be parsed back into physics.
- **unknown-physical / spurious / no_signal** — the three distinct outcomes for a feature that does
  not cohere with the scene. *Unknown-physical* is real scattering the declared model does not
  describe. *Spurious* is an instrument artifact — it coheres in the instrument's own coordinates
  (analyzer and monochromator angles, incident energy) rather than in (Q, ω), which is how a spurion
  is told from physics. *No_signal* is nothing there. Collapsing the three into "rejected" would throw
  away the one discriminant that matters.

## Validation vocabulary

- **gold / expert fit** — a human's by-hand fit of the same data (in their usual tool). ISAR is compared
  against it as a **reasonableness check**, not a source of truth — the human fit is provisional and may
  be revised. Agreeing exactly is **not** the goal.
- **synthetic ground truth** — data ISAR generates with known parameters, then fits. This is the one
  tier where the true answer is actually known, with no human to over-fit to.
- **safety (violation)** — a safety violation would be ISAR confidently *resolving* a scan a human
  rejected as a blown-up fit. ISAR must decline those; zero violations is the load-bearing property.
- **rlu** — reciprocal lattice units; momentum Q in units of the crystal's own reciprocal lattice.
- **TAS / IXS** — triple-axis spectrometer (neutron) / inelastic X-ray scattering; ISAR handles both.

## Ruling IDs

- **Ruling IDs** (`O27`, `H1`, `S10`, `G16`, `P1`, `W1`, `ADR-019`, "the 22 grilled decisions",
  …) — stable historical names for design rulings, safe to cite from docs and code forever. Each
  ruling's *content* lives in the design doc that owns its system (recorded there as dated rules
  or amendments); the append-only `DECISIONS.md` ledger that originally defined the lettered
  series was retired 2026-08-20 (doc rebuild) and every version of it is in git history
  (`git log --all -- docs/DECISIONS.md`). A few IDs also survive as code vocabulary — e.g.
  **H1**, the SCENE program's
  constitution (`design/scene.md`); **S10**, evidence freezes only at owner-named milestones;
  **O44**, the acoustic zone-centre exclusion (`ARCHITECTURE.md` §15).

  **Retired 2026-09-02, kept here so a citation in git history resolves:** **O11** (decline
  widening), **O15** (the connected frontier), **O27** (same-q production ceilings), **O28** (the
  targeted action-inventory exhaustion proof), **O36 / O41 / O42 / O43 / H22** (the same-action
  scan-window comparison, its restricted authority profile and its default-on flip). The targeted
  lane's placement is the EIG planner and none of these mechanisms exists any more
  (`design/eig.md` §8, `design/steering.md` "Placement is the EIG planner").
