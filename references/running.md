# Running FragPipe headless

## Locating the FragPipe installation

Always settle this before a run — see `check_fragpipe.py`. It resolves an installation in this
order and reports how it got there:

| Order | Source | Counts as explicit? |
|---|---|---|
| 1 | `--fragpipe-dir <folder>` | yes |
| 2 | `--fragpipe <launcher>` | yes |
| 3 | `$FRAGPIPE_DIR` | yes |
| 4 | `$FRAGPIPE_BIN` | yes |
| 5 | **The newest installation found** — everything on `PATH` and in the usual roots, ranked by version | no — a guess |

Only the first four are a deliberate choice; `fragpipe_run.py` refuses to launch on a guess
(exit 3) until the folder has been confirmed or `--accept-detected` is passed.

**Default is the newest version.** `PATH` does not short-circuit this: a `fragpipe` left on `PATH`
from an old install is ranked alongside everything else rather than winning automatically, and when
it loses, the report says so:

```
  resolved  : newest of 3 installed
  NOTE: 'fragpipe.bat' on PATH points at C:\FragPipe\FragPipe-23.1, which is older.
        Using the newest installation instead.
```

To pin an older version, name it explicitly (`--fragpipe-dir`, or `$FRAGPIPE_DIR`) — an explicit
choice is never overridden by a newer install.

`--fragpipe-dir` accepts the installation root, its `bin/` folder, or the launcher file itself.
A folder is only accepted as an installation when it contains `bin/fragpipe.bat` (Windows) or
`bin/fragpipe` (Unix).

A standard installation looks like:

```
C:\FragPipe\FragPipe-24.1-build36\
├── bin/fragpipe.bat        launcher
├── lib/fragpipe-<version>.jar   <- version is read from this file name
├── tools/                  MSFragger, IonQuant, Philosopher, DIA-NN, ...
├── workflows/              ~78 built-in .workflow files
└── python/                 bundled Python for EasyPQP steps
```

Candidate roots searched: `C:\FragPipe*`, `C:\FragPipe\*`, `C:\Program Files\FragPipe*`,
`D:\FragPipe*`, `/opt/fragpipe*`, `/usr/local/fragpipe*`, `~/fragpipe*` (and `FragPipe*` variants).
Ranking is by **numeric** version — `24.1-build36` > `24.1-build5` > `24.0` > `23.1` > `9.4`. Note
that a plain string sort gets this wrong (`'9' > '2'`), so the version is compared as a tuple of the
integers in the folder name. The losers are listed in the report so the user can pick one instead.

```bash
python scripts/check_fragpipe.py                 # detect + report (exit 0 found, 4 not found)
python scripts/check_fragpipe.py --list          # every installation found
python scripts/check_fragpipe.py --json          # includes "needs_confirmation"
python scripts/check_fragpipe.py --fragpipe-dir "C:/FragPipe/FragPipe-24.0"
```

## Command line

Source of truth: `FragPipe-GUI/src/main/java/org/nesvilab/fragpipe/FragPipeMain.java`.

```
fragpipe.bat --headless --workflow <file> --manifest <file> --workdir <dir>   # Windows
fragpipe     --headless --workflow <file> --manifest <file> --workdir <dir>   # Linux
```

| Flag | Required | Meaning |
|---|---|---|
| `--headless` | yes | Run without the GUI. Omitting it while passing the others is a hard error. |
| `--workflow <path>` | yes | Parameter file (`.workflow`). |
| `--manifest <path>` | yes | Input file list (`.fp-manifest`). |
| `--workdir <path>` | yes | Output directory. |
| `--dry-run` | no | Print the plan and stop. Cheap configuration check. |
| `--ram <int>` | no | Max heap in GB. `0` = let FragPipe decide. Overrides `workflow.ram`. |
| `--threads <int>` | no | Thread count. Default = cores − 1. Overrides `workflow.threads`. |
| `--config-tools-folder <dir>` | no | Folder with MSFragger/IonQuant/diaTracer jars. Defaults to the cached config. |
| `--config-diann <file>` | no | Path to the `DiaNN.exe` **binary**, not the installer. |
| `--config-python <dir>` | no | Python directory (needed by EasyPQP spectral-library steps). |
| `-h`, `--help` | no | Usage. Note it exits with code **1**. |

Command-line `--ram` / `--threads` take priority over the values inside the workflow file.

### Exit codes are not a success signal

Headless FragPipe calls `System.exit(0)` on its normal completion path *and* after a task fails
(`Fragpipe.java` `main0()`; failures route through `MessageKillAll`, drain the queue, then exit 0).
Decide success from stdout instead:

| Marker | Meaning |
|---|---|
| `=====ALL JOBS DONE IN <n> MINUTES=====` | success |
| `Process returned non-zero exit code, stopping` | a task failed; the run was aborted |
| `Cancelling <n> remaining tasks` | queue was torn down |
| `WARNING: Process '<x>' returned non-zero exit code (<n>), but is non-fatal. Continuing.` | tolerated, run continues |
| process ended, none of the above seen | died early — bad config, missing tool, OOM kill |

`scripts/fragpipe_run.py` encodes all of this and exits 0/1 correctly.

### Progress markers

```
29 commands to execute:          <- total task count, printed once, up front
CheckCentroid                    <- task name (plan block)
<command line>                   <- and its command; strictly alternating
...
~~~~~~~~~~~~~~~~~~~~~~           <- end of plan block
...
CheckCentroid                    <- same format again when the task actually starts
<command line>
Done in 5.6 s.
Process 'CheckCentroid' finished, exit code: 0    <- one per completed task
```

So progress = (count of `Process '…' finished` lines) / (declared total). Task names repeat
legitimately (three input files → three `MSFragger move pepxml` tasks). A task name may carry a
` [Work dir: <dir>]` suffix. After the run, a `Task Runtimes:` block lists per-task minutes.

MSFragger and friends print their own banners between task boundaries, which is what to show a user
during a long step:

```
***************************FIRST SEARCH DONE IN 0.830 MIN**************************
************MASS CALIBRATION AND PARAMETER OPTIMIZATION DONE IN 1.677 MIN*********
***************************MAIN SEARCH DONE IN 0.397 MIN***************************
```

FragPipe also writes its own `log_<date>_<time>.txt` into the workdir incrementally during the run,
so it can be tailed even for a job this skill did not launch.

## The manifest (`.fp-manifest`)

Headerless, tab-separated, four columns, one row per input file:

```
F:\data\PXD001468\b1923_293T_proteinID_03A_QE3_122212.mzML			DDA
F:\data\PXD001468\b1924_293T_proteinID_04A_QE3_122212.mzML			DDA
```

| # | Column | Notes |
|---|---|---|
| 1 | absolute path | `.mzML`, `.mzXML`, `.raw` (Thermo), `.d` (Bruker, a directory), `.mgf` |
| 2 | experiment | may be empty; **controls the output folder layout** |
| 3 | bioreplicate | integer or empty |
| 4 | data type | `DDA`, `DDA+`, `DIA`, `GPF-DIA`, `DIA-Quant`, `DIA-Lib` |

Unrecognised data types silently become `DDA` (`InputLcmsFile.java`), so a typo fails quietly —
generate manifests with `scripts/make_manifest.py` rather than by hand.

Constraints FragPipe enforces:

- Experiment names: every character outside `A-Za-z0-9_` is replaced with `_`.
- Spectral **file names** may only contain `A-Za-z0-9_.[]()+-`, and may not contain more than one
  dot (`sample.rep1.mzML` is rejected — `REASON_MULTIPLE_DOTS`).
- Their **folders** may only contain `A-Za-z0-9_.[]()+-/\:` (no spaces, no non-ASCII letters).
  Headless 24.1-build8 and later exit 1 on either rule; headless 24.0 and 24.1-build1 to build7
  do not check them.
- On Windows, in every build, a path Java cannot open (`<>"|?*`, a control character, a `:` after
  the drive, a folder or file name ending in a space) drops its row with only a log line.
- Two inputs with the same file name, even in different folders, are refused.
- Lines starting with `#` or `//` are comments, and a bioreplicate that is not a plain integer
  drops its row with only a warning. A line holding only control characters (a trailing Ctrl-Z)
  is loaded as an empty path, the working folder. `fragpipe_run.py` checks all of this before
  launching (exit 2).
- A listed file that is not there stops headless FragPipe; `fragpipe_run.py` names it first.
- The workflow must be UTF-8: FragPipe only logs a failure to read one saved as UTF-16 and runs
  with none of its settings, so `fragpipe_run.py` refuses it (exit 2). It also refuses a `\u` not
  followed by four hex digits, which stops Java reading the file (see the backslash rule below).
- A folder added as input is walked the way FragPipe does it: a `.d` run is one input, MSFragger's
  `_calibrated`/`_uncalibrated` copies are skipped, linked folders are followed. `.mzBIN` files
  (24.1 only) are not collected by `make_manifest.py`; list them in a design sheet.
- diaTracer writes `<run>_diatracer.mzML` beside each `.d`. List the `.d` (data type `DIA`), not
  that file: 24.1-build61 and 25.0 refuse it in a manifest and skip it when a folder is added, and
  24.1 stops on a DIA input that is not a `.d` when diaTracer runs. 24.0 and 24.1 up to build60
  load it, and their diaPASEF workflow notes reuse it typed DDA beside the `.d` typed DIA-Quant.
  `make_manifest.py` skips it on a walk.
- DIA-Umpire writes `<run>_Q1`, `_Q2` and `_Q3` (`.mzML` and `.mgf`) beside its input before
  moving them into the workdir, and FragPipe loads leftover copies like any run. `make_manifest.py`
  skips them on a walk when their run is in the same folder; `fragpipe_run.py` refuses one listed
  with its run or typed DIA.
- Two inputs in one folder that differ only in extension (`ctrl_1.raw` and `ctrl_1.mzML`, or a
  `_Q1.mgf` and `_Q1.mzML`) pass FragPipe's name check, but MSFragger writes one `.pepXML` for
  both, so one search overwrites the other. The builder and `fragpipe_run.py` refuse them; keep
  one.
- The group name becomes `experiment`, or `experiment_bioreplicate`, or `exp_<bioreplicate>` when
  the experiment is blank but a replicate is set.

## The workflow file (`.workflow`)

A Java `.properties` file: `key=value`, `#` comments, and **backslash-escaped values**. On Windows a
path must be written with escaped separators and colon:

```properties
database.db-path=G\:\\dev\\2026-01-29-decoys-reviewed-contam-UP000005640.fas
```

Editing one by hand is error-prone; prefer copying a built-in from `<FragPipe>/workflows/` and
changing only what is needed. When you must write a path programmatically, escape `\` → `\\` and
`:` → `\:` (Python: `path.replace("\\", "\\\\").replace(":", "\\:")`).

Keys worth knowing:

| Key | Purpose |
|---|---|
| `database.db-path` | FASTA. Must already contain decoys (see `database.decoy-tag`). |
| `database.decoy-tag` | Decoy prefix, default `rev_`. |
| `workflow.ram` / `workflow.threads` | Overridden by `--ram` / `--threads`. |
| `msfragger.search_enzyme_name_1`, `msfragger.precursor_mass_*`, `msfragger.fragment_mass_*` | Search space. |
| `percolator.run-percolator` / `peptide-prophet.run-peptide-prophet` | PSM validation engine (mutually exclusive). |
| `protein-prophet.run-protein-prophet` | Protein inference. |
| `phi-report.run-report` | **Off means no psm.tsv/protein.tsv at all.** |
| `phi-report.filter` | e.g. `--sequential --picked --prot 0.01`. |
| `phi-report.print-decoys` | Default `false` — decoys are absent from the reports. |
| `phi-report.pep-level-summary` / `prot-level-summary` | Produce `combined_peptide.tsv` / `combined_protein.tsv`. |
| `ionquant.run-ionquant` | LFQ/MBR quantification; also writes the `combined_*` matrices. |
| `tmtintegrator.run-tmtintegrator` | Produces `tmt-report/`. |
| `dia.run-dia-quant` | "Quantify DIA": produces `dia-quant-output/`. The engine is `diann.run-diann` (DIA-NN) or `fragdia.run-fragdia` (FragDIA, the three glyco DIA workflows). `diann.run-diann=true` alone does nothing — `LFQ-MBR` has it too. FragPipe 24.0 and 24.1 builds before 47 use `diann.run-dia-nn` instead. |
| `speclibgen.run-speclibgen` | Builds `library.tsv`; **collapses all experiments to the workdir root**. |

## Choosing a built-in workflow

`<FragPipe>/workflows/` ships 77 of them in 24.1-build61 and 25.0. `scripts/build_workflow.py` picks one
from a few questions about the data and changes only the run-level settings (FASTA, contaminant
removal, TMT channel count); SKILL.md "New analysis: build the workflow" walks through it. Pick by
hand when the user names a workflow or brings their own. Common starting points:

| Goal | Workflow |
|---|---|
| Plain identification | `Basic-Search` |
| Label-free quant with match-between-runs | `LFQ-MBR` |
| LFQ phospho / ubiquitin | `LFQ-phospho`, `LFQ-ubiquitin` |
| TMT | `TMT10`, `TMT16`, `TMT18-Astral`, `TMT10-MS3`, `TMT10-phospho` |
| iTRAQ | `iTRAQ4` |
| SILAC | `SILAC3` |
| DIA (library-free → DIA-NN quant) | `DIA_SpecLib_Quant`, `..._diaPASEF` |
| DIA-Umpire pseudo-DDA | `DIA_DIA-Umpire_SpecLib_Quant` |
| Open / PTM discovery | `Open`, `Open-quickscan`, `Mass-Offset-CommonPTMs` |
| Glyco | `glyco-N-HCD`, `glyco-O-Pair`, … |
| Immunopeptidomics | `Nonspecific-HLA*` |

## Batch jobs

FragPipe also accepts `.job` files (used by the GUI's Batch tab) — a tab-separated table with a
header comment line:

```
# Job name (optional)	Workflow file path	Manifest file path	Output directory	Tools folder path (optional)	Fasta path (optional)	RAM (optional)	Threads (optional)
run1	D:\wf.workflow	D:\files.fp-manifest	D:\out	C:\FragPipe\tools	D:\db.fas	93	11
```

For headless automation, prefer invoking `fragpipe_run.py` once per job — it gives per-job status
files and correct success detection, which the batch format does not.

## Environment notes

- FragPipe refuses to run from a path containing spaces or tabs (`FragpipeLoader.java`).
- Thermo `.raw` and Bruker `.d` need extra runtime components; `.mzML` is the portable choice.
- Bruker/timsTOF on Windows requires the Visual C++ 2017 redistributable.
- Set `--ram` well below physical memory; MSFragger is the memory-hungry step and a JVM OOM shows up
  as a non-zero exit from the `MSFragger` task.
