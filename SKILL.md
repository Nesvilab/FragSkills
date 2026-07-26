---
name: fragskills
description: Run FragPipe (MSFragger/Philosopher/IonQuant/TMT-Integrator/DIA-NN) proteomics analyses in headless mode, track progress while they run, and interpret the results. Use when asked to search or quantify mass-spectrometry proteomics data, run a FragPipe workflow, monitor a running FragPipe job, or read FragPipe output such as psm.tsv, peptide.tsv, modified_peptide.tsv, ion.tsv, protein.tsv, combined_*.tsv, the tmt-report folder, or the dia-quant-output folder.
---

# FragSkills — running and interpreting FragPipe

FragPipe is a proteomics pipeline that chains MSFragger, MSBooster, Percolator/PeptideProphet,
ProteinProphet, Philosopher, IonQuant, TMT-Integrator, and DIA-NN. This skill drives it headless
and reads what it produces.

## Three facts that shape everything

1. **Headless FragPipe always exits 0 — even when it fails.** Never use the exit code to decide
   whether a run worked. Success is the line `====ALL JOBS DONE IN <n> MINUTES====`; failure is
   `Process returned non-zero exit code, stopping`, or a process that ends without ever printing
   `ALL JOBS DONE`. `fragpipe_run.py` implements this and *its own* exit code is trustworthy.
2. **Runs take minutes to many hours.** Always launch with Bash `run_in_background: true` and poll
   the status file. Never block a foreground tool call on a real run.
3. **Where the reports land depends on the experiment names in the manifest.** Root of the workdir
   for a single unnamed experiment; `<workdir>/<experiment>/` when experiments are named. See
   `references/outputs.md`.
4. **Which FragPipe installation runs is never assumed.** Several versions are commonly installed
   at once. Detection defaults to the newest, but the folder is always confirmed with the user
   before a run — see Step 1 below.

## Step 1 (ALWAYS): settle which FragPipe installation to use

**Never start a run before the FragPipe folder has been shown to the user and confirmed.** Users
routinely have several versions side by side, and silently picking the wrong one changes the
results. Run this first, every time:

```bash
python scripts/check_fragpipe.py
```

Then, based on its exit code:

- **Exit 0 — found.** It defaults to the **newest** version installed. Show the user the `folder:`
  line (and the "Other installations found" list if present) and ask them to confirm or choose a
  different one. Pass what they confirm as `--fragpipe-dir`.
- **Exit 4 — not found.** Ask the user for the folder where FragPipe is installed — the one
  containing `bin/`, `tools/`, and `workflows/` (e.g. `C:\FragPipe\FragPipe-24.1-build36` or
  `/opt/fragpipe`). Do not guess, and do not offer to install FragPipe. Then pass it as
  `--fragpipe-dir`.

`fragpipe_run.py` enforces this: it prints the folder it resolved and **refuses to launch (exit 3)**
when the location was auto-detected rather than specified. Once the user has confirmed, either pass
`--fragpipe-dir <folder>` or add `--accept-detected`.

If the user sets `FRAGPIPE_DIR` (or `FRAGPIPE_BIN`) themselves, that counts as explicit and runs
without further confirmation — but still show them which folder it resolved to.

`check_fragpipe.py` also reports the version, tools/, workflows/ (with a count), the bundled Python,
and the DIA-NN binary — use it to find built-in workflows and to fill `--tools-folder`, `--diann`,
or `--python-dir` when a workflow needs them.

## Step 2: run the job

FragPipe headless needs a **workflow** (parameters), a **manifest** (input files), and a **workdir**
(output).

```bash
# a. Manifest: 4 tab-separated columns — path, experiment, bioreplicate, data type
python scripts/make_manifest.py --input D:/data --recursive \
    --experiment-from parent --data-type DDA --out D:/run/files.fp-manifest

# b. Workflow: copy a built-in from <FragPipe>/workflows/ and set the FASTA in it.
#    database.db-path must point at a FASTA that ALREADY CONTAINS DECOYS, and the
#    file is Java .properties — read references/running.md before editing one.

# c. Launch in the BACKGROUND, with the confirmed folder
python scripts/fragpipe_run.py \
    --workflow     D:/run/fragpipe.workflow \
    --manifest     D:/run/files.fp-manifest \
    --workdir      D:/run/out \
    --fragpipe-dir "C:/FragPipe/FragPipe-24.1-build36" \
    --ram 64 --threads 16
```

Pick a workflow by matching the user's experiment: `LFQ-MBR` for label-free DDA, `TMT10`/`TMT16`
for isobaric, `DIA_SpecLib_Quant` for DIA, `Open` for open/PTM discovery, `Basic-Search` for plain
identification. Confirm the choice with the user when it is not obvious — a wrong workflow wastes
hours. `--dry-run` makes FragPipe print its plan and stop; use it to validate configuration cheaply.

`fragpipe_run.py` exit codes: **0** succeeded, **1** failed, **2** bad arguments or missing inputs,
**3** FragPipe folder not confirmed, **4** no FragPipe installation found.

## Reporting progress while it runs

`fragpipe_run.py` keeps `<workdir>/fragskills_status.json` current and mirrors all output to
`<workdir>/fragskills_run.log`. Poll it:

```bash
python scripts/fragpipe_status.py --workdir D:/run/out          # human readable
python scripts/fragpipe_status.py --workdir D:/run/out --json   # machine readable
python scripts/fragpipe_status.py --workdir D:/run/out -v       # + runtimes, recent output
```

Exit codes make it a usable predicate: **0 succeeded, 1 failed, 2 still running, 3 nothing yet.**

Progress is `finished / total`, where FragPipe declares the total up front as
`N commands to execute:` and prints `Process '<name>' finished, exit code: 0` per task. This is
exact, not estimated. It also works on a run someone else started — pass `--workdir` and it falls
back to parsing the newest `log_*.txt`.

**How to report to the user:** poll on a sensible cadence (a long search step can run 30+ minutes
with no task boundary), and relay the percentage, the current task, and elapsed time. `MSFragger`,
`MSBooster`, `IonQuant`, `DIA-NN`, and `SpecLibGen` are the slow stages; a run sitting on one of
them is normal, not stuck. Sub-stage milestones (`FIRST SEARCH DONE IN 0.83 MIN`, etc.) appear in
the `milestones` field. Do not claim a run finished until the state is `succeeded`.

## Reading the output

```bash
python scripts/parse_reports.py --workdir D:/run/out              # what exists, and where
python scripts/parse_reports.py --workdir D:/run/out --summary    # counts and QC per file
python scripts/parse_reports.py --file .../psm.tsv --columns      # schema
python scripts/parse_reports.py --file .../psm.tsv --head 3       # sample rows
```

Always run discovery first rather than assuming paths — the layout depends on the manifest and on
which modules ran.

The core reports, all tab-separated with one header row:

| File | One row per | Use it for |
|---|---|---|
| `psm.tsv` | peptide-spectrum match | spectrum-level evidence, PTM localization, per-file counts |
| `peptide.tsv` | peptide sequence | peptide-level ID lists |
| `modified_peptide.tsv` | modified peptide form | PTM-level ID lists |
| `ion.tsv` | peptide ion (sequence + mods + charge) | precursor-level quant input |
| `protein.tsv` | protein group | protein IDs, coverage, per-group quant |
| `combined_*.tsv` | as above, across experiments | cross-sample comparison and quant matrices |

For column-by-column meaning, the per-experiment vs. root layout rule, and how to filter decoys and
contaminants, read `references/outputs.md`. For TMT (`tmt-report/`) and DIA-NN
(`dia-quant-output/`), read `references/outputs.md#tmt-report` and `#dia-quant-output`.

Reports are already FDR-filtered (1% by default) and, unless `phi-report.print-decoys=true`, decoys
are already removed — do not re-filter by q-value without checking, and do not report decoy counts
as if they were a QC metric when the column is uniformly `false`. Contaminants *are* retained by
default (`Is Contaminant`) and should usually be excluded from biological conclusions.

For real analysis (joins, quant matrices, statistics) load the TSVs with pandas:
`pd.read_csv(path, sep="\t")`. `parse_reports.py` is for orientation and QC, not for doing the
science.

## When something goes wrong

`references/troubleshooting.md` covers the common failures: file names FragPipe rejects, missing
FASTA decoys, out-of-memory in MSFragger, spaces in the install path, and how to find the real
error in a 1500-line log.

## Reference files

- `references/running.md` — locating FragPipe, CLI flags, manifest and workflow formats, workflow
  selection, batch jobs
- `references/outputs.md` — output layout rules and full column reference for every report
- `references/troubleshooting.md` — failure modes and diagnosis
