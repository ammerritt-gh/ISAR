# ISAR glossary

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

"Delivered" in the reports = **resolved + flagged** (ISAR handed you a usable number). Everything else
is ISAR saying *"look here yourself"* — which is a feature, not a failure. **Nothing is ever deleted**
("flag, don't delete"): a declined scan is still in the file with the raw data intact.

## Fitting & lineshape

- **energy transfer** — the energy the probe lost or gained (meV); 0 meV is elastic scattering.
- **phonon / DHO** — the inelastic peak ISAR fits, modelled as a damped harmonic oscillator.
- **elastic line** — the peak at zero energy transfer (no energy exchanged).
- **energy zero** — where the elastic line sits on the energy axis; defines 0 meV. ISAR self-calibrates
  it from dedicated elastic/Bragg scans.
- **Voigt** — a Gaussian convolved with a Lorentzian; ISAR's instrument-resolution shape.
- **resolution** — the instrumental width the data is smeared by (a Gaussian width and a Lorentzian
  width); ISAR measures it from elastic/Bragg scans.
- **quasi-elastic** — a broad component sitting near zero energy transfer (real signal, not resolution).
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
  **diagnostics-only**: the bootstrap interval is what certifies. Confidence intervals (bootstrap,
  profile) are never called credible. See DECISIONS S5.
- **AIC / Akaike weight** — a way to compare candidate models that penalizes extra parameters; the
  weight is the relative evidence for each model (higher = better supported).
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
- **branch assignment / assignment entropy** — the Bayesian posterior assigning each fitted
  feature to a branch label ({acoustic, optic, new, …}), and the Shannon entropy of that
  posterior — high entropy means "the labels are genuinely ambiguous here." Shown as a color
  channel in the GUI.
- **branch belief** — the driver's per-branch dispersion estimate: a weighted local regression
  over that branch's confirmed points, with honest uncertainty growth away from data.
- **excitation hypothesis** *(branch hypotheses Implemented; `level`/`continuum` Partial —
  `docs/HYPOTHESES.md`)* — your declared physics
  expectation for the target (and rivals) in `card.policy.toml`: character (acoustic / optic /
  custom), zone-centre energy with uncertainty, flatness, confidence. It weights the driver's
  seeding, classification, and scan choice; it never forces a fit.
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
