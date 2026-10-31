---
name: fragskills
description: Run FragPipe (MSFragger/Philosopher/IonQuant/TMT-Integrator/DIA-NN) proteomics analyses in headless mode, track progress while they run, and interpret the results. Use when asked to search or quantify mass-spectrometry proteomics data, run a FragPipe workflow, monitor a running FragPipe job, or read FragPipe output such as psm.tsv, peptide.tsv, modified_peptide.tsv, ion.tsv, protein.tsv, combined_*.tsv, the tmt-report folder, or the dia-quant-output folder.
---

# FragSkills — running and interpreting FragPipe

FragPipe is a proteomics pipeline that chains MSFragger, MSBooster, Percolator/PeptideProphet,
ProteinProphet, Philosopher, IonQuant, TMT-Integrator, and DIA-NN. This skill drives it headless
and reads what it produces.

**Running the scripts:** every `python scripts/...` below is relative to **this skill's base
directory**, printed when the skill loads. The shell's working directory is the user's project, not
the skill, so always prefix it — `python "<skill base>/scripts/check_fragpipe.py"` — or the call
fails with `can't open file`. Paths you *pass* to the scripts (`--workdir`, `--manifest`, data
folders) are the user's own and are never rewritten.

## Four facts that shape everything

1. **Headless FragPipe always exits 0 — even when it fails.** Never use the exit code to decide
   whether a run worked. Success is the line `====ALL JOBS DONE IN <n> MINUTES====`; failure is
   `Process returned non-zero exit code, stopping`, or a process that ends without ever printing
   `ALL JOBS DONE`. `fragpipe_run.py` implements this and *its own* exit code is trustworthy.
2. **Runs take minutes to many hours.** Always launch with Bash `run_in_background: true` and poll
   the status file. Never block a foreground tool call on a real run.
3. **Where the reports land depends on the experiment names in the manifest, and which one is the
   *answer* depends on the analysis type.** Root of the workdir for a single unnamed experiment;
   `<workdir>/<experiment>/` when experiments are named. TMT, DIA, grouped DDA and single-run DDA
   each have a different main result — see the table under "Reading the output".
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

## New analysis: build the workflow

When the user has raw files and wants an analysis rather than a named workflow, let
`build_workflow.py` pick the shipped workflow from a few questions. It starts from that workflow
unchanged and sets only the run-level settings: FASTA, contaminant removal, TMT channel count. It
supports FragPipe 24.1 and 25.0; with any other version it refuses (exit 6), and the workflow is
picked by hand as in Step 2.

1. **Step 1 first**, then ask for a workdir (the run's output folder) if the user gave none.
2. **Ask the open questions.**

   ```bash
   python scripts/build_workflow.py --workdir D:/run/out \
       --fragpipe-dir "<confirmed folder>" --data D:/data --json
   ```

   Exit 5 means questions are open. Each item of `open` has an `id`, a `text` and a `kind`:
   - `choice`: offer its `answers` (show `label`, send back `value`) with AskUserQuestion, up to 4
     questions per dialog. A question with more than 4 answers goes in a numbered list in your
     message instead. Name the `unavailable` answers in the question text with their nearest
     shipped workflow and how it differs (`nearest.differs`); never offer them. Mark the
     `default`. The PTM enrichment, the TMT reference and the DIA instrument are asked even when
     only one answer is shipped with the others: ask the user in your message to confirm that
     answer, naming the unavailable ones, and send back what they say.
   - `path`: free text. The FASTA goes back as `--answer fasta=<path>` and must already contain
     decoys; the data folder goes back as `--data <folder>` (it replaces the folders given before).
   - `table`: the design or channel sheet, step 3.

   Never answer for the user, even when the file names make the answer look obvious; say what they
   suggest and let the user choose. Re-run with `--answer id=value` per answer (repeatable). The
   plan in the workdir remembers answers and `--fragpipe-dir`, so pass only the new ones, and
   repeat until nothing is open. An answer no shipped workflow combines with the earlier ones exits
   6 and comes back under `rejected`, with the nearest shipped workflow and how it differs; the
   question stays in `open`. Tell the user and ask it again. An answer listed under `dropped` was
   made irrelevant by the others (say so); one given early is kept until its question comes up.
3. **The design.** `design` is `fragskills_design.tsv` (file, condition, replicate, data_type),
   proposed from the file names. Show it as a table, apply the user's edits to the file, and re-run
   with `--design <path>`. Rows with the same condition and replicate are fractions of one sample.
   A sample sheet the user already has, in the same columns, is passed the same way, with or
   without `--data` (without it, the sheet's rows are the files; files it does not list stay out
   when `--data` is given later). TMT and iTRAQ
   get `channels` instead: `fragskills_channels.tsv` (plex, channel, sample, condition,
   replicate), one plex per data folder, accepted with `--channels <path>`. Sample names take no
   spaces (a non-breaking space included) and must be unique across all plexes; `NA` (exactly that, any case) marks an unused
   channel, and `N/A` or `n.a.` is refused. FragPipe compares names with only A-Z a-z 0-9 `_` `-`
   kept, so `WT.1` and `WT1` clash; the reports use the names as written, so names made only of
   those characters are safest. A bridge workflow needs exactly one sample per plex whose name, as
   written, contains its reference tag (`pool_1`, `pool_2`). A plex folder that already holds
   the user's own `*annotation.txt` is used as is: its rows in the sheet come from that file,
   read again on every build, and a problem in them is fixed in that file (the message names
   it). An accepted sheet is proposed again when it stops fitting: a workflow with other data
   types, other `--data` folders or files in them, another channel count or other plexes. A call
   that stops before the new proposal is written (any data folder that is away, or a sheet saved
   in another encoding than UTF-8, which is never merged) keeps the accepted sheet. Relay every
   `PROBLEM` and `NOTE` the builder prints with a sheet: a note says when it merged rows into a
   new proposal or reset their data types, which rows the user took out stay out, which rows were
   left out because their files are not under `--data`, and where it kept the previous version
   (`.bak`, `.bak2`, ...).
4. **Confirm.** Exit 0 prints the confirmation table: FragPipe folder, base workflow, every answer
   and every changed parameter with its source, the input files and the files written. Show it to
   the user as printed and ask them to confirm. Only after they say yes:

   ```bash
   python scripts/build_workflow.py --workdir D:/run/out --confirm
   ```

   This records checksums and, for TMT, writes `<plex>_annotation.txt` into each plex's raw-file
   folder, the only place headless FragPipe looks. Nothing goes into the data folders before this.
5. **Launch** as in Step 2c, with
   `--workflow D:/run/out/fragskills-repro/fragskills.workflow` and
   `--manifest D:/run/out/fragskills-repro/fragskills.fp-manifest`. `fragpipe_run.py` refuses a
   builder-made workflow (exit 5) until its plan is confirmed, and again when the workflow, the
   manifest or a TMT annotation changes afterwards, or when another FragPipe version (or one whose
   version it cannot tell) launches it. Any workflow file beside the plan counts as builder-made,
   copies included. Do not edit those files: re-run the builder, show the table and confirm
   again. A builder call that changes the plan clears the confirmation; `--confirm` takes no other
   inputs and uses the FragPipe the plan was built from. A workdir copied or moved elsewhere is
   rebuilt there (run the builder with the new `--workdir`, show the table, confirm) before
   `--confirm` or `--show` accept it. If the table starts with a `NOTE` that
   the console cannot show some characters, re-run with `--json` for the exact paths.

`build_workflow.py` exit codes: **0** built or confirmed, **2** bad arguments or inputs (the message
says which), **5** questions open, **6** refused: an answer no shipped workflow combines with the
others, an unsupported FragPipe version, or a shipped workflow that this FragPipe build ships
otherwise than the builder expects (pick the workflow by hand then). `--show` reprints the
table. Everything it writes is in
`<workdir>/fragskills-repro/`, and `fragskills_plan.json` there records each answer and change with
its reason.

## Step 2: run the job

FragPipe headless needs a **workflow** (parameters), a **manifest** (input files), and a **workdir**
(output).

```bash
# a. Manifest: 4 tab-separated columns — path, experiment, bioreplicate, data type
python scripts/make_manifest.py --input D:/data --recursive \
    --experiment-from parent --data-type DDA --out D:/run/files.fp-manifest

# b. Workflow: build it from questions ("New analysis: build the workflow"), or copy a
#    built-in from <FragPipe>/workflows/ and set the FASTA in it.
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

By hand, pick a workflow by matching the user's experiment: `LFQ-MBR` for label-free DDA, `TMT10`/`TMT16`
for isobaric, `DIA_SpecLib_Quant` for DIA, `Open` for open/PTM discovery, `Basic-Search` for plain
identification. Confirm the choice with the user when it is not obvious — a wrong workflow wastes
hours. `--dry-run` makes FragPipe print its plan and stop; use it to validate configuration cheaply.

`fragpipe_run.py` exit codes: **0** succeeded, **1** failed, **2** bad arguments, missing inputs, a
workflow that is not UTF-8 or holds a `\u` Java cannot read (an unescaped `D:\db\uniprot.fasta`),
or a manifest row FragPipe would misread or refuse (a file that is not there, unknown or empty
data type, non-integer bioreplicate, a file or folder name that 24.1-build8 and later refuse, a
path Windows cannot open, diaTracer's `_diatracer.mzML` output listed as an input on
24.1-build61 or later, DIA-Umpire's `_Q1`/`_Q2`/`_Q3` output listed with its run or typed DIA,
two inputs with the same file name, or two in one folder that differ only in extension, such as
`ctrl_1.raw` and `ctrl_1.mzML`, whose searches would overwrite each other), a FragPipe run
already going in that workdir, or tools the last run there left running (stop them with
`fragpipe_stop.py`), **3** FragPipe folder not confirmed, **4** no FragPipe installation found, **5** a builder-made
workflow whose plan is not confirmed or has changed since.

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

## Stopping a run

Stop a run only when the user asks, and only after they have seen which run it is and confirmed:
the hours it has run are lost, and FragPipe cannot resume, so running the same command again
starts from the beginning.

```bash
python scripts/fragpipe_stop.py                              # list the FragPipe runs going now
python scripts/fragpipe_stop.py --workdir D:/run/out         # show what would be stopped (exit 3)
python scripts/fragpipe_stop.py --workdir D:/run/out --yes   # stop it, after the user confirms
python scripts/fragpipe_stop.py --pid 1234 --yes             # a run listed with --pid
```

Without a workdir it lists every FragPipe headless run on the computer with its workdir; ask which
one when there are several. It stops FragPipe and every tool it started (MSFragger, IonQuant, DIA-NN
and the rest) and checks that they are gone. It picks the run by its program, FragPipe's launcher or
JVM with `--headless` and this `--workdir` among its own arguments, so it works on runs started
outside this skill too, and it never touches a shell that only mentions FragPipe or a PID the system
has given to another program. A run started with a relative `--workdir` is listed with its PID; stop
it with `--pid` (on Linux, `--workdir` usually finds it too). When `--workdir` exits 1 and names
such a run, it may be the one: show it to the user, and stop it by PID once they confirm. When it
exits 4 but the user says a run is going, list every run with no options before saying there is
nothing to stop.

After a stop, the status file reads `failed` with `stopped by the user`, and `fragpipe_run.py` exits
1; when no watcher is left, the script writes the status file itself. Do not stop a run by ending
`fragpipe_run.py`, its background task, or one process in Task Manager: that can leave FragPipe and
its tools running.

When FragPipe itself was ended on its own (in Task Manager, or by the system running out of memory),
its tools can keep running. A run started with `fragpipe_run.py` records them: the watcher exits 1
with `tools_left` in the status file and names their PIDs, a new run in that workdir on the same
computer is refused until they are gone (from another computer it only warns), and
`fragpipe_stop.py --workdir <dir>` shows them (exit 3) and stops them with `--yes` once the user
confirms; the status file then says they were stopped by the user. Otherwise nothing ties them to
the run: the script lists the
processes whose command line names the workdir (exit 1 while the status file says the run is still
going). Show them to the user, and have them end the ones they confirm in Task Manager (End process
tree) or with `kill`.

Exit 1 can also mean the run cannot be stopped from here. When the status file says it runs on
another computer, or the workdir is shared with one, stop it there. A run started from an
Administrator prompt cannot be seen from a normal one; when the script says so, tell the user to
run it from a prompt opened with Run as administrator. When it says FragPipe may be starting or
just ending, run it again in a few seconds.

A stop during the MSFragger search can leave its scratch files, `<name>.mzBIN_calibrated` and
`<name>.ms1bin`, next to the input files (a finished run removes them); the script lists the
ones it finds. Tell the user, and delete them only if they ask.

Exit codes: **0** stopped (or listed), **1** could not stop it, or what is left needs the user (a
run with a relative workdir that may be the one, or tools FragPipe left running; the message names
the PIDs), **2** bad arguments or no such folder, **3** found a run but `--yes` was not given (show
it to the user and confirm), **4** nothing is running there.

## Reading the output

```bash
python scripts/parse_reports.py --workdir D:/run/out              # what exists, and where
python scripts/parse_reports.py --workdir D:/run/out --summary    # counts and QC per file
python scripts/parse_reports.py --file .../psm.tsv --columns      # schema
python scripts/parse_reports.py --file .../psm.tsv --head 3       # sample rows
```

Always run discovery first rather than assuming paths — the layout depends on the manifest and on
which modules ran.

**Which file is the result depends on the analysis type, and the wrong one fails silently** — it
opens, the numbers look reasonable, and they answer a different question. `parse_reports.py` leads
its output with the verdict (`Run type`, `MAIN RESULT ->`, and what *not* to use); follow it. The
rule it applies, most specific first:

| Analysis | Main result | Do not quantify from |
|---|---|---|
| **TMT + PTM enrichment** (`tmt-report/` has `single-site`/`multi-site`) | `tmt-report/abundance_single-site_*.tsv` and `ratio_single-site_*.tsv`, plus `multi-site` | `combined_*.tsv`; raw channel columns in `psm.tsv` |
| **TMT** (`tmt-report/` exists) | `tmt-report/abundance_protein_*.tsv` and `ratio_protein_*.tsv` (also gene/peptide) | `combined_*.tsv`; raw channel columns in `psm.tsv` |
| **DIA** (`dia-quant-output/` exists) | `dia-quant-output/abundance_protein_*.tsv`, plus gene/peptide and DIA-NN's `report.*_matrix.tsv` | root `psm/peptide/ion/protein.tsv` — those are the **spectral library** |
| **DDA, experiments named** (`combined_*.tsv` exist) | `combined_protein.tsv`, then `combined_peptide.tsv` / `combined_modified_peptide.tsv` / `combined_ion.tsv` / `combined_site_*.tsv` | the per-experiment folders — same data, one sample at a time |
| **DDA, single or unnamed experiment** | root `protein.tsv`, `peptide.tsv`, `ion.tsv`, `psm.tsv` | — |

Two conventions decide whether the numbers come out right: **TMT-Integrator writes log2 with `NA`
for missing** (do not log-transform again), while **DIA writes linear intensities with missing cells
left empty** (an empty cell is not a zero). With more than one TMT plex, only the `ratio_*` tables
are comparable across plexes. DIA sample columns are **full file paths**, not sample names.

The per-experiment reports, all tab-separated with one header row:

| File | One row per | Use it for |
|---|---|---|
| `psm.tsv` | peptide-spectrum match | spectrum-level evidence, PTM localization, per-file counts |
| `peptide.tsv` | peptide sequence | peptide-level ID lists |
| `modified_peptide.tsv` | modified peptide form | PTM-level ID lists |
| `ion.tsv` | peptide ion (sequence + mods + charge) | precursor-level quant input |
| `protein.tsv` | protein group | protein IDs, coverage, per-group quant |
| `combined_*.tsv` | as above, across experiments | cross-sample comparison and quant matrices |
| `combined_site_<AA>_<mass>.tsv` | modification site | per-site localization probability and intensity |

For column-by-column meaning, the per-experiment vs. root layout rule, and how to filter decoys and
contaminants, read `references/outputs.md`. For TMT (`tmt-report/`) and DIA-NN
(`dia-quant-output/`), read `references/outputs.md#tmt-report` and `#dia-quant-output` — both list
the real columns per level, including the `ProteinID` / `Protein ID` spelling trap in TMT.

Reports are already FDR-filtered (1% by default) and, unless `phi-report.print-decoys=true`, decoys
are already removed — do not re-filter by q-value without checking, and do not report decoy counts
as if they were a QC metric when the column is uniformly `false`. Contaminants *are* retained by
default (`Is Contaminant`) and should usually be excluded from biological conclusions.

For real analysis (joins, quant matrices, statistics) load the TSVs with pandas:
`pd.read_csv(path, sep="\t")`. `parse_reports.py` is for orientation and QC, not for doing the
science.

## Summary report and QC plots

FragPipe generates its QC report only from a GUI button, so **a headless run never produces one**.
This skill builds it directly:

```bash
python scripts/make_report.py --workdir D:/run/out     # -> <workdir>/fragskills-qc-report.html
```

The name is deliberately not `fragpipe-report.html` — that belongs to FragPipe's own summary report
module. Never write to that path; a workdir can hold both files.

Seconds to run, and it needs **nothing but the run's own output** — no FragPipe installation, no
Java, no pandas or matplotlib. It works on a copied results folder or a machine that has never had
FragPipe on it. The page is self-contained (inline SVG, MSBooster PNGs embedded), so it can be
emailed or archived alone: experiment design, runtime Gantt chart, overall and per-run PSM
distributions, mass calibration before/after, Percolator feature weights, MSBooster plots. Offer it
after any successful run. All it requires in the workdir is a `log_*.txt` and `psm.tsv`.

This is a port of FragPipe's own report generators (`generate_reports_pdf.py` through 24.0, the
FragSummarizer jar after), verified panel by panel against the jar's output. Where it deliberately
differs, `references/plots.md` says so.

When the user wants a *number*, a custom figure, one run out of many, or a cross-run comparison,
skip the report and take the series directly:

```bash
python scripts/qc_data.py --workdir D:/run/out --list        # inventory
python scripts/qc_data.py --workdir D:/run/out --tsv D:/qc   # tidy TSVs, or --json
```

Two derivations are easy to get wrong and silently change the numbers: `Retention` in `psm.tsv` is
**seconds** while every plot shows minutes, and the overall distributions **deduplicate** on
`(Spectrum, Modified Peptide, Peptide Length, Charge, Number of Missed Cleavages)`. `qc_data.py`
handles both; do the same when working from `psm.tsv` directly.

`references/plots.md` documents every panel, its source and derivation, what a healthy one looks
like, and the log formats behind the runtime, mass-calibration and Percolator sections.

## When something goes wrong

`references/troubleshooting.md` covers the common failures: file names FragPipe rejects, missing
FASTA decoys, out-of-memory in MSFragger, spaces in the install path, and how to find the real
error in a 1500-line log.

## Reference files

- `references/running.md` — locating FragPipe, CLI flags, manifest and workflow formats, workflow
  selection, batch jobs
- `references/outputs.md` — output layout rules and full column reference for every report
- `references/plots.md` — the summary report: every panel, where its numbers come from, how to read
  it, and how to rebuild any of it as a custom figure
- `references/troubleshooting.md` — failure modes and diagnosis
