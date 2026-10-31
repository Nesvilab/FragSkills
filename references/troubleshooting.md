# FragPipe troubleshooting

## Finding the real error in a long log

A failing run leaves a 1000+ line log. The failure is almost never the last line — FragPipe still
prints citations and a runtime table afterwards. Work backwards from the marker:

```bash
# where it broke and what ran before it
python scripts/fragpipe_status.py --workdir <dir> -v

# the abort marker, then read upward from there
grep -n "Process returned non-zero exit code\|finished, exit code: [^0]\|Cancelling" <log>
```

The task named in the *last* `Process '<name>' finished, exit code: <non-zero>` is the one that
failed; its own stderr sits in the ~50 lines immediately above that line.

## Run ended with no `ALL JOBS DONE` and no failure marker

FragPipe died before the pipeline started. Usual causes, in order of likelihood:

- A validation error printed to stderr in the first few lines (missing workflow/manifest, bad path).
- Tools not configured: pass `--config-tools-folder`, and `--config-diann` / `--config-python` for
  DIA and spectral-library workflows. Headless mode otherwise relies on cached GUI configuration
  that may not exist on a server.
- The JVM was OOM-killed by the OS. Lower `--ram`.

`--dry-run` reproduces configuration problems in seconds without doing any work — use it first.

## `FragPipe cannot be run from a path that contains spaces or tabs`

Move the FragPipe installation to a path with no spaces. This is checked at startup and is fatal.

## Input files rejected

- **Multiple dots in the file name** (`sample.rep1.mzML`): rename to `sample_rep1.mzML`.
- **Disallowed characters**: file names may only use `A-Za-z0-9_.[]()+-`.
- Experiment names silently have every character outside `A-Za-z0-9_` replaced by `_`, which can
  merge two experiments you meant to keep apart (`ctrl-1` and `ctrl_1` both become `ctrl_1`).

`scripts/make_manifest.py` checks these before you burn hours on a run.

## MSFragger fails or runs out of memory

MSFragger is the memory-hungry step; a JVM OOM appears as a non-zero exit from the `MSFragger` task.

- Lower `--ram` if it exceeds physical memory (the JVM must fit alongside the OS).
- Raise `msfragger.misc.slice-db` to split a large FASTA into slices.
- Open searches and large variable-modification sets multiply memory and time — check the workflow
  matches the intent before assuming a bug.

## No `psm.tsv` / `protein.tsv` at all

- `phi-report.run-report=false` in the workflow — nothing downstream of filtering is written.
- The run failed before `PhilosopherReport`. Check the task list in the status output.
- They exist, but under `<workdir>/<experiment>/` rather than the root. Run
  `parse_reports.py --workdir <dir>`; see `outputs.md` for the layout rule.

## No `combined_*.tsv`

Expected unless there is more than one experiment **and** IonQuant runs (or
`phi-report.pep-level-summary` / `prot-level-summary` is enabled). A single experiment never
produces them.

## Very few identifications

- **FASTA has no decoys.** FragPipe expects decoys already in the database, tagged per
  `database.decoy-tag` (default `rev_`). Without them FDR filtering removes nearly everything.
- Wrong enzyme, precursor tolerance, or fixed modifications for the sample — compare the workflow to
  how the sample was actually prepared (e.g. TMT workflows fix a TMT mass on K and peptide N-term;
  running TMT data through an LFQ workflow, or the reverse, collapses the yield).
- Wrong data type in the manifest: DIA files marked `DDA` are searched as DDA and give poor results.
  This fails silently because unrecognised values default to `DDA`.

## TMT-specific

- The annotation file must be named `*annotation.txt` and sit in the **same folder as the spectral
  files**, exactly one per folder. Headless mode has no other way to find it.
- Compressed ratios: check `Purity` in `psm.tsv`. Low precursor purity means co-isolation.

## DIA-specific

- `--config-diann` must point at the `DiaNN.exe` binary itself, not the installer.
- Spectral-library steps need Python; pass `--config-python`.
- With library generation enabled, all experiments collapse into the workdir root — per-experiment
  folders will not appear, and that is correct behaviour, not a failure.

## Stopping a run

Use `fragpipe_stop.py` (SKILL.md, "Stopping a run"). It finds the run by its workdir, also when it
was started outside `fragpipe_run.py`, and ends FragPipe and every tool it started. Ctrl-C on
`fragpipe_run.py` in a terminal also ends the whole tree.

Do not end one process of a run on its own. After `fragpipe.bat` (Windows) or the
`fragpipe_run.py` watcher is ended, FragPipe keeps running, and `fragpipe_stop.py --workdir <dir>
--yes` still stops it. After the FragPipe JVM itself is ended (on Linux and macOS the `fragpipe`
launcher becomes that JVM), MSFragger, IonQuant, DIA-NN and the rest keep running. When a
`fragpipe_run.py` watcher was still going, it records them as `tools_left` in the status file,
refuses a new run in that workdir on the same computer while they run (from another computer it
warns), and `fragpipe_stop.py --workdir <dir> --yes` stops them. Otherwise nothing ties them to the
run: `fragpipe_stop.py` finds no run, and lists the
processes whose command line names the workdir, such as MSFragger's `java -jar ...
<workdir>/fragger.params`. End the ones the user confirms by hand: Task Manager, Details tab, End
process tree, or `kill` on Linux and macOS.

The status file records the computer a run started on (`host`). For a workdir on a shared drive,
`fragpipe_stop.py` on another computer cannot see the run's processes; it says so and names the
computer to stop it from.

## Re-running into a used workdir

FragPipe cleans its Philosopher workspace at the start (`WorkspaceCleanInit`), but stale
`psm.tsv`/`protein.tsv` from a previous run are **not** guaranteed to be removed. When re-running
after a failure, use a fresh workdir, or you may end up reading the previous run's results and
believing the new one succeeded. `fragskills_status.json` records `started_at` — compare it against
the report files' mtimes if in doubt.
