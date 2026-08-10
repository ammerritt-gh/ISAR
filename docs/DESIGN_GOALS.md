This is a USER-owned document outlinining what ISAR should be able to *do*. Some of these are "in progress" or "wishlist" items some are mostly done, some need refinement; this is the *master objectives* list that shows what ISAR is aiming for. It should NEVER be AI-edited; if you find a discrepency, bring it up with the USER, do NOT edit it yourself.

# Translation Catalogues


# Data Reduction


# Data Analysis


# Sample Knowledge

## Sample lattice
The sample lattice is the fundamental aspect that maps from real space (atoms regularly spaced apart to form a crystal) to reciprocal lattice space (Brillouin zones). We map lattice paramters and the space group as the fundament parameters to form the metric for QE space: how are Bragg peaks spaced, what distances are HKL in, what angles and geometry do we work with, or what Bragg peaks are allowed versus forbidden. This is the "map" that allows us to understand QE space. We align on Bragg peaks and bound phenomena within Brillouin zones sized by space group and lattice parameters.

This means that understanding the lattice parameters and space group are critical to understanding the observed phenomena. An incorrect lattice parameter will scale the fundamental map incorrectly and put q targets off. An incorrect space group will lead to confusion as Bragg peaks go missing and acoustic phonons have the incorrect periodicity.

The sample lattice must be a first-class parameter tied to every scan, but it is *mutable*. If we have the wrong lattice constants and do a scan, those scans happened at some actual absolute machine setup, they just didn't map into QE space the way we thought they did. We stamp every scan with a "sample lattice parameter" ID, traced back to a recorded lattice parameter set. Then, we can always take any scan, trace what the lattice parameters used was, and transform it as necessary to reuse it in a different mapping; we apply the recorded change, always with clear provenance for tracebacks or modifications, and proceed.

Consider the case where we scan over H across a Bragg peak at (2,0,0) and measure the center at (2.1,0,0). We adjust the lattice paramter *a* by 5% up, rescan, and now the Bragg peak is found exactly at (2,0,0). Scan 1 is perfectly compatible with scan 2, after we note the modified lattice parameters; we can apply the change in lattice parameters to back out that the peak at (2.1,0,0) was at the (2,0,0) Bragg peak.

Users (or ISAR) may adjust commentary and metaparameters, e.g. we change temperature and need to adjust lattice parameters to compensate for the lattice expanding/shrinking, thus we branch into two different families of lattices that won't be remixed later.

## UB matrix
If the lattice parameters tell us what the sample *itself* is doing, the UB matrix tells us how our sample is orientated relative to the instrument configuration; in effect, we are fixing the relative orientation of the sample relative to the *instrument.* The *U* portion is the crystal orientation, and *B* is the reciprocal-lattice basis matrix.

The simplest way to think of the UB matrix is that if we can take two (non-collinear) points, these define a plane; two Bragg peaks then define a plane for us to orient the sample with. With a known lattice, we can get the full 3D orientation in the correct right-handed coordinate system. Thus, to define the UB matrix, we take at least two (possibly more) Bragg points, compare them against sample angles, and adjust to find the UB matrix. This math is known and straightforward.

The key idea in this section is that like the lattice paramters, the UB matrix can be stamped; and in fact, this is what will be recorded, because the full matrix *includes* the sample lattice parameters. Now, if a scan was done in some orientation with some UB matrix, we can convert any point of that scan into a different UB matrix-frame. We still explicitly convert into "orientation" and "sample lattice parameters," because e.g. the latter changes with temperature routinely (orientation CAN change with temperature if e.g. the mounting flexes as it cools! We cannot assume that orientation is always fixed).

## Space group
Finally, space group. The space group does not enter into the UB matrix at all. Instead, it mainly tells us which peaks are allowed or forbidden. Then, because Brillouin zones are centered by Bragg peaks, this also changes the size and shape of Brillouin zones; a simple cubic and a fcc lattice may have the same lattice parameters and both have a peak at (2,0,0), but because the (1,0,0) peak is forbidden in fcc the Brillouin zone is doubled in size. Space groups also place constraints on the allowed lattice parameters, e.g. simple cubic requires *a*=*b*=*c* and alpha=beta=gamma=90 degrees.

Space groups can also be degenerate, or users may use different orientations; for example, a tetragonal system can be rotated 45 degrees and scaled to make it equivalent to a orthorhombic system. We should be able to clearly match QE space between these two systems, changing orientation and space group as required for the need at hand; for example, if the material undergoes a tetragonal to orthorhombic transition, we can measure the lattice parameter splitting of the two twinned lattices, which is easier in orthorhombic than tetragonal, thus we switch before we actually undergo the transition.

Like the UB matrix, we stamp the space group for scans, thus we can always swap between different QE space mappings.

## Alignment and lattice corrections
Sample alignment is, bluntly, adjusting the UB matrix and the sample parameters to make the measurements match the ideal map. Our claim: we expect Bragg peaks at points X Y Z and not at A B C. We measure those Bragg peaks, find them at X' Y' Z' and adjust the UB matrix. We check A, if we observe a peak we have to update our space group, or not.

What we need to do is apply it to ISAR. Sample orientation and parameters are not items to be fixed by the user and left, but something that ISAR factors into its hypotheses as the *ground truth mapping of QE space.* This means that if ISAR measure a phonon at (2+/-h,0, 0) and gets different answers, its should be updating that there is a misalignment problem and that *it needs to correct this.* It can make the decision to check the Bragg peak, notice a misalignment, correct the UB matrix, reorient the older scans and then continue measuring.

As well, sample alignment is oftentimes a slightly tedious job for initializing an experiment. We ideally have some idea about the orientation of our sample when we place it in the mount, we try to get it roughly aligned with the beam, and we measure to fix the UB matrix. There are techniques to accomplish these, and we can transmit them to ISAR; an ideal case is we simply mount the sample and give some weak priors, then let ISAR take it from there to completely align the sample.

## Sample quality
Samples are oftentimes not ideal. Even a single crystal carries a fundamental mosaic, a spread as the atoms move slightly over long distances from their perfect orientation; this crystal mosaic broadens scans, as it's the crystal itself spread out. But there can be bigger issues; crystals can have very broad mosaics which heavily smears out signal, or can be made up of many smaller crystals that each have their own separate scattering. Thus, we have to consider how the sample *itself* can modify our expected scattering away from ideal.

Consider the case of two samples that are coaligned: checked and mounted to try and place their orientations together. If they are off (they are), we get two separate signals; two Bragg peaks where we expected one, slightly shifted, or two branches. Most often, this comes as a smearing of signal when it's broad (phonons) and split peaks when the signal is sharp (Bragg peaks). We can include this: we can record that we are measuring multiple crystals and fit each one separately, trying to deconvolve their smeared signal. There is a gradient that starts quantized (one perfect crystal, to two crystals, three, etc...) that smooths out as many crystals are coaligned (at 1000 crystals, their composite mosaic becomes like one crystal, nicely replicating a single crystal with broad mosaic). These are things that ISAR can handle.

## Sublattices
Crystal lattices aren't necessarily the only lattice of importance. Neutrons are sensitive to magnetic signal, and a magnetic lattice can coexist with the structural crystal lattice (I am not an expert on magnetic scattering, so this is something I will need to research and come back to).

Another good example is orthorhombic samples; the two possible orientations can coexist, and we measure two Bragg peaks naturally from the two different orientations (this is called twinning). So similarly to the two coaligned samples, we need to be able to disentangle two different orientations.

# Phenomena Hypotheses


# Automated Scans

## Pause/Failure States
When running an automated scan, the only reason to ever stop the campaign is from user choice, meeting a campaign goal, being overbudget or machine failure. If a user wants to intervene or sets a stopping point, this is fine. If we reach our goal (measure this branch to +/- 0.1 meV) we can stop this campaign and consider it complete. If a user sets a budget and we go over that budget (e.g. scan for 4 hours and alert me when finished), then we stick to that budget. If there is some machine failure, we don't keep scanning with garbage data (e.g. if the IXS beam is turned off, we don't keep scanning and getting nothing, we wait for the go-ahead when it's back up). This means that bad fits, low intensity, unknown phenomena, unexpected results, anything that isn't a real "stoppage" does NOT stop an automated campaign. ISAR should never have a stoppage that ends a campaign early outside of these scenarios. A bad fit is not a reason to stop, but it should inform new scans; this is the core of EIG. 

We have had campaigns stopped in the past for e.g. bad fits in previous iterations, so we need a review and cleanup pass to *remove all these extra stopping points.* We must maintain explicit, clearly delineated stopping points that are only activated under controlled, documented circumstances.