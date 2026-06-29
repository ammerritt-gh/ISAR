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
- **AIC / Akaike weight** — a way to compare candidate models that penalizes extra parameters; the
  weight is the relative evidence for each model (higher = better supported).
- **fork / fold** — a *fork* is two or more equally-supported answers reported separately (the data are
  ambiguous); a *fold* is a single agreed answer.

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
