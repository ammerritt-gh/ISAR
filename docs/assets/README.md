# Committed validation evidence

> **Status:** live (this index — the artifacts it lists are Historical evidence)
> **Authority:** none — the evidence speaks for itself; `DESIGN.md` §15 owns capability status
> **Provenance:** hand-written; the artifacts beside it are generated, see `MANIFEST.json`

`README.md` and [`../READING_GUIDE.md`](../READING_GUIDE.md) cite validation reports as the reason
to trust ISAR. Those reports are written to `out/`, which is gitignored and disposable under
ruling W1 (retired ledger, git history) — so until this directory existed, a fresh clone could read
the claims and see none of the evidence behind them. This is that evidence, promoted and committed.

## What is here, and what is deliberately not

**Derived evidence only.** Every file is a fit result, a summary table of fitted parameters, or a
rendered figure. **No raw facility data file is committed** — `real_data/` stays gitignored, and
adding a raw file here would be a policy breach, not an improvement. `MANIFEST.json` records a
single digest over the whole `real_data/` tree so a reader who holds that data can confirm they
have the same inputs, without this repository publishing an index of unpublished filenames.

Be precise about what a figure is, though. A scan overlay **plots the measured points it fits** —
counts against energy transfer, with Poisson error bars, labelled with the scan or numor and its
instrument command. That is not incidental, it is the point: `../READING_GUIDE.md` promises you can
judge every call with your own eyes, and you cannot do that without seeing the data. So roughly
eight scans are visible here at plot-reading accuracy. What stays unpublished is the machine-readable
facility files — every scan, every detector, full metadata — which is what `real_data/` holds and
what these figures are not a substitute for. If that trade is ever wrong for a particular dataset,
the fix is to drop the figure from the bundle in `tools/docs/collect_evidence.py`, not to reword
this paragraph.

| Group | What it answers |
|---|---|
| [`phase3_gold/`](phase3_gold/) | The **analysis-core 83-scan gold gate**: does ISAR agree with a human expert across the 83-scan S30 gold set, and does it ever confidently resolve a scan the expert rejected? (Drives the analysis modules directly — the CLI/session equivalence gate at M=200 is a separate, still-open validation, `VAL-2`.) |
| [`in8_phase1/`](in8_phase1/) | Does the same fitting core work on a triple-axis instrument (ILL IN8)? Carries a real S30 IXS positive control, because the contrast is the point |
| [`phase3_step1/`](phase3_step1/) | Does the structure-hypothesis loop reproduce the frozen Phase-1 fitter, and do the error and model-selection layers behave? |
| [`stat2b/`](stat2b/) | The corrected STAT-2b.1 development campaign, pre/post delivery-separation comparison, and 4/8/16/24/28-worker scaling evidence. It proposes a held-out gate but does not authorize one. |

Each group keeps its report's own `FINDINGS.md`, its summary CSV, and the figures a document
actually cites — not everything the run produced. Every figure carries its `.caption.md` sidecar,
so a figure is readable without the report around it.

## Viewable is not reproducible

This bundle makes the evidence **viewable** from a clean checkout. It does **not** make it
reproducible from one: regenerating any group needs the local `real_data/`, which is not
distributed here. Do not read the presence of these files as a claim that `git clone && pytest`
reproduces them.

`MANIFEST.json` records schema version 2, the generating command and input fingerprint for every
group, and the code revision and collection date for the bundle. Every group fingerprint must
match the top-level `test_data_fingerprint` (the digest of `real_data/`; the key keeps its
old name). The documentation tests fail if manifest metadata
drifts from the collector declaration, a committed file stops matching its size or hash, a listed
file goes missing, or an undeclared file appears here.

## Regenerating

From the repo root, with `real_data/` present:

```
python tools/docs/collect_evidence.py
```

The collector clears only its declared `out/<source>` directories, runs each declared generator
synchronously with the active Python interpreter, and checks that `real_data/` is unchanged before
and after every generator. It validates all source artifacts and required PNG caption sidecars,
builds the complete prospective bundle in staging, and only then promotes it with rollback on
failure.

`python tools/docs/collect_evidence.py --check` verifies manifest structure and declared metadata
before checking files, sizes, and hashes; it neither regenerates nor copies anything. The file list
is declared in the collector, not globbed — adding an artifact means naming it there and saying
which citation it serves.

Paths are restricted to portable Windows-safe relative names and must remain beneath `out/` and
`docs/assets/`. Symlinks, junctions/reparse points, directories in place of artifacts, and other
special files are rejected. Sources are revalidated immediately before copying. As with any
filesystem validation followed by a separate open, a malicious local process with concurrent write
access could still race that last check; collection assumes exclusive local control of the checkout.

## When these numbers go stale

They record what a named revision produced on a named date; they are not maintained against later
changes to the fitter. If a change moves any number a document quotes, regenerate the affected
group and update the quoting document in the same commit — that is the rule in
[`../../AGENTS.md`](../../AGENTS.md), and it is why the manifest pins a code revision.
