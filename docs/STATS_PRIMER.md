# The statistics ISAR uses — for someone who fits by hand

You don't need to care about the statistics to read ISAR's reports, but if you've spent years fitting
in Igor/PAN/Matlab, a few of ISAR's choices differ from the defaults there. This explains *why*, in
the language of experimental uncertainty (precision vs accuracy), and — most importantly — what ISAR's
error bars do and **don't** mean.

## 1. The fit cost: Poisson likelihood (Baker–Cousins), not χ²

Standard least-squares minimizes χ² = Σ (data − model)² / σ². For counts you have to choose σ², and the
usual choices bias the answer at low counts:

- **Neyman χ²** (σ² = data) systematically *under*-weights high points and pulls fitted amplitudes and
  widths **down**.
- **Pearson χ²** (σ² = model) pulls them the other way.

This isn't a subtlety you can ignore for damping and intensity ratios: a phonon's Γ and its
Stokes/anti-Stokes ratio are exactly the quantities these biases distort. ISAR instead minimizes the
**Poisson likelihood** (Baker–Cousins −2 ln L), which is the right cost for counting data and is
unbiased as counts get small. Near a well-counted peak all three agree; the difference shows up in the
tails and at low signal — which is where triage decisions get made.

`cost/dof` in the reports is this Baker–Cousins cost per degree of freedom — read it like a reduced χ²:
near 1 is a good fit, ≫1 means the model doesn't match the data.

## 2. The error bars: bootstrap (+ profile), not the curvature

The fast error bar most tools report (the Hessian / "HESSE": invert the curvature of the cost at the
minimum) assumes the cost is a clean parabola — i.e. the likelihood is Gaussian. At high counts it is,
and HESSE is fine. At low counts the likelihood is skewed and HESSE is **optimistic** — it quotes a
tighter error than the data support.

ISAR's authoritative error bar is a **parametric bootstrap**: take the best-fit model, draw many new
Poisson datasets from it, refit each, and read off the spread of the parameter. It also computes a
**profile-likelihood (MINOS)** interval, which can be asymmetric. ISAR then checks that the bootstrap
and the curvature error agree — the **Bernstein–von Mises** condition, which holds when there are
plenty of counts. When they agree (the "asymptotic" regime), the quick number is trustworthy; when they
diverge, ISAR flags it. The step-1 report shows this check per scan.

## 3. Choosing the model: AIC, not eyeballing or raw χ²

"Is there an elastic line here, or just a phonon and background?" Adding parameters always lowers χ², so
you can't pick a model by χ² alone. ISAR uses **AIC** (= cost + 2k: the fit cost plus a penalty of two
per free parameter) and the derived **Akaike weights** (each model's share of the evidence), with a
nested likelihood-ratio test for nested models. This picks the simplest model the data actually support,
rather than the most flexible one — and it's reproducible, where eyeballing is not. (The candidates here
are small and explicit, e.g. phonon+background vs elastic+phonon+background.) When two genuinely
different structures fit equally well — say a one-phonon vs a two-phonon description — ISAR reports a
**fork** (both answers) instead of averaging them into a single wrong number.

*(A Bayesian posterior path — and a frequentist↔Bayesian agreement check — is built
(`isar/analyze/errors/bayes.py`, dynesty nested sampling; attach via `errors.attach(..., with_bayes=True)`)
but not yet wired into the driver-facing fit path. ISAR's intervals in that path today are the
frequentist bootstrap/profile ones above.)*

## 4. The honest caveat: ISAR's error bars are statistical-only

This is the most important thing to know before you use an ISAR uncertainty.

ISAR's bootstrap interval captures **counting statistics only**. It does **not** include systematic
uncertainty — the choice of background shape, the resolution function, energy-zero drift between scans.
On well-counted data the counting error is tiny, so the interval is tight; and when we compare against
a human's value, the human's value falls inside ISAR's 68% interval far less than 68% of the time (on
the gold set, around 20%). **That is expected from a statistical-only interval, and it means the error
bars are too small to use as final uncertainties.**

What *is* reliable is the **central value**. On the gold set ISAR's energy lands a median of ~0.16 meV
from the human's — well inside the ~1 meV instrument resolution. So:

> **Practical rule:** treat ISAR's energy as a good ballpark and its confidence as triage guidance.
> Do **not** weight by ISAR's σ or quote it as a final uncertainty — measure that yourself once you've
> looked at the scan. A systematics-aware error model is future work.

This is why the reports separate two things cleanly: **parameter recovery** (good — the number is in the
right place) from **interval calibration** (a known limitation — the *width* is statistical-only).

## 5. Why two validation tiers

- **Synthetic ground truth** is where we can actually test calibration: we know the true parameters, so
  we can ask whether the recovered values are unbiased and whether the intervals cover the truth at
  their stated rate. No human, no over-fitting.
- **Real-data comparison** to a human's by-hand fits tests *reasonableness and safety* on messy data —
  does ISAR make sensible calls, and does it decline the scans a human rejected? The human reference is
  provisional, so this is a sanity check, not a source of truth. Agreeing *exactly* is not the goal.

See [READING_GUIDE.md](READING_GUIDE.md) for the map and [GLOSSARY.md](GLOSSARY.md) for term-by-term
definitions.
