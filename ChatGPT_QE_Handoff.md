**This design fits the job you described.** One broad central component, with a fitted intensity and width, tied to the elastic zero and convolved with the measured resolution, is a sensible phenomenological addition. It gives the fitter somewhere to put that extra central intensity without assigning it to a phonon.

I would keep the proposed scope. **The main correction is to the success criterion; the remaining changes are small clarifications.**

## 1. The a06 acceptance test currently allows the existing failure

This is the most important issue.

The design identifies the problematic fit as:

\\[ E_0=1.18\\ \\mathrm{meV},\\qquad \\Gamma=3.31\\ \\mathrm{meV}. \\]

But the acceptance criterion only requires no phonon below half a resolution FWHM—approximately **0.565 meV**. The existing problematic oscillator already clears that threshold.

Likewise, requiring the selected model to “have an elastic line” becomes a weak check when an elastic component is included in every starting model. It could remain at negligible intensity while the overdamped phonon continues to describe the central scattering.

**Change the a06 check to examine whether the intended redistribution actually happens:**

> Compare the central-region decomposition before and after the change. The elastic and QE components should account for the broader central intensity previously assigned to the spurious central phonon. Report whether an overdamped DHO still carries essentially the same central contribution.

A before/after plot and component parameter table are enough for this inspection. You do not need a new automatic classifier or a universal rule against overdamped phonons. If that oscillator remains, the result should explain whether QE failed to take the central intensity or whether a separate inelastic contribution is still supported.

I would also add **one mixed synthetic case** to the three already listed:

> Elastic line + QE + one genuine low-energy phonon above one resolution FWHM: retain the phonon and recover the central contribution without adding another phonon.

The separate QE-only and phonon-only cases are useful, but they do not check the competition when both are present.

## 2. Keep the zero-height simplification, but soften its guarantee

This statement is too strong:

> “When a spectrum does not need the QE, its fitted height goes to zero.”

With noisy data, an unnecessary component can acquire a small positive fitted amplitude. It may therefore survive the duplicate rule and retain its parameter penalty. That is compatible with your proposed always-present policy—and with the synthetic criterion already allowing a QE “too weak to matter.”

I would replace the guarantee with:

> When the duplicate rule removes the QE, the reduced model is priced without QE parameters or their prior penalty. Otherwise, a weak fitted QE may remain.

**This does not require doubling the initial model list.** It just describes the chosen search honestly.

There is one implementation detail to make explicit: the duplicate rule must be able to **construct and price a reduced model on demand**, even though QE-free and elastic-free models were not initial candidates. Removing both components should work too. The replacement retains the same phonon count, drops the corresponding parameters, and must not be counted alongside its pruned parent as another independent result.

That is the small integration detail most likely to get lost behind “the existing duplicate rule handles it.”

## 3. Keep the accepted width floor, but describe it as a modeling choice

The flat region followed by an exponential tail is a reasonable, proper prior once normalized. I see no reason to reopen the accepted penalty or introduce an upper cap.

However, these claims about the lower bound are incorrect:

> “Below [¼ resolution FWHM] the QE is identical to the elastic line.”

> “It rejects nothing physical.”

The convolved Lorentzian becomes identical to the elastic resolution profile **in the limit of zero intrinsic width**. There is no universal quarter-FWHM threshold below which the profiles become identical or the broadening becomes unmeasurable.

For this deliberately limited phenomenological model, the accepted floor can stay. Describe it as:

> The quarter-resolution floor is a pragmatic limit on separating two strongly overlapping central components. Narrower central broadening is absorbed into the unresolved central contribution in this model.

That preserves the scope while avoiding a false physical justification.

## 4. Define exactly what “QE height” means

The design correctly distinguishes fitted amplitude from the reported finite-window spectral weight. It should add one explicit definition of the fitted amplitude.

For example:

> The fitted QE height is the intrinsic intensity at zero energy, before resolution convolution.

Alternatively, use ISAR’s existing observed-height convention if that is easier to maintain. The important part is to choose one convention and use it consistently in the fast pass, deep pass, amplitude prior, and saved model.

Also specify that **the intrinsic QE is not renormalized to the numerical convolution grid**. Its detailed-balanced form has no finite all-energy area, so normalizing it over whatever padding happens to be used would make its amplitude convention depend on a numerical implementation choice.

Your proposed reporting convention—integrating the intrinsic contribution over the measured energy window—is otherwise fine for this purpose.

## 5. Use the known 40×40 resolution for the validation run

The general slit-inference repair can remain outside this job. For the a06 comparison, however, use the known **40×40 SARF**, through the existing configuration or override.

Similar FWHMs do not establish that resolution tails are identical, and the central shoulders are precisely what this change is trying to describe. This is a setup correction for the comparison, not a reason to expand the feature.

## What I would leave as designed

The central decisions are appropriate:

- One QE component with two fitted parameters.
- A shared, calibrated zero.
- Joint fitting with elastic intensity, phonons, and the optional sample background.
- Full measured-resolution convolution.
- The selected detailed-balance factor, fixed by temperature.
- QE available at zero phonon count and excluded from the reported phonon count.
- Consistent treatment in both evidence routes.
- BL43LXU-only scope and the already accepted timing measurement.

**I would proceed after tightening the a06 criterion and clarifying the pruning and amplitude conventions.** The useful outcome is straightforward: the elastic-plus-QE contribution takes the central excess, genuine resolved phonons remain available, and the fitter stops needing a near-zero oscillator merely to explain a slightly broader central line.