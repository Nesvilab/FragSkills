# FragSkills

A Claude Code skill for running [FragPipe](https://fragpipe.nesvilab.org/) headless and
interpreting its output.

`SKILL.md` is the instruction file Claude reads. This README is for humans.

## What it does

- Finds your FragPipe installation, and **always confirms which one** before running. Several
  versions side by side is normal; picking the wrong one silently changes the results, so a
  detected-but-unconfirmed folder is refused rather than used.
- Launches `fragpipe --headless` with a workflow + manifest + workdir.
- Tracks progress **exactly** (not estimated) by parsing FragPipe's own task plan and
  `Process '<name>' finished` lines, and publishes it to a JSON status file for polling.
- Decides success correctly. Headless FragPipe calls `System.exit(0)` on both the success and the
  failure path, so the exit code is meaningless; the tools key off the stdout markers instead.
- Discovers and summarizes the report TSVs, accounting for FragPipe's per-experiment output layout.
- **Identifies which files are the actual result.** TMT, TMT+PTM, DIA, grouped DDA and single-run
  DDA each leave a different main table, next to several plausible-looking wrong ones. Discovery
  names the run type, ranks the files that answer it, and says explicitly what not to quantify from.
- **Builds the QC summary report that headless runs never produce** — `fragpipe-report.html`, with
  no FragPipe installation, no Java and no third-party packages, from a results folder alone. It is
  a port of FragPipe's own generators (`generate_reports_pdf.py`, then the FragSummarizer jar),
  checked panel by panel against the jar's output. The underlying series are also available as
  JSON/TSV for custom figures.

## Commands

Three slash commands, for the steps worth a typed shortcut:

| Command | Does |
|---|---|
| `/fragskills-run <workdir>` | Launch a headless search in the background |
| `/fragskills-status <workdir>` | Report progress, or why a run stopped |
| `/fragskills-report <workdir>` | Build `fragpipe-report.html` and read the QC out loud |

They are **dispatchers, not documentation** — four lines each, pointing at the matching section of
`SKILL.md`. That keeps one source of truth: edit the skill, and the commands follow. Anything else
is better asked in plain language ("why did the run in D:/run/out fail?", "build me a manifest from
D:/data"); the skill triggers on that and covers the whole workflow.

They live in `commands/` and are **copied** into `~/.claude/commands/` at install time, so re-run
the installer after editing one — unlike the skill itself, which is linked and updates live. Each
install prunes the previous copies first, so a deleted command does not linger. Installing under
`--name` skips them, since they address the skill by its default name.

## Install

Claude Code loads skills from `~/.claude/skills/<name>/`. Run the installer for your platform
from this directory — no administrator rights needed:

```bat
install.bat                     :: Windows  (or just double-click it)
```

```bash
./install.sh                    # Linux / macOS
```

That links the skill into `~/.claude/skills/fragskills`, verifies it, and checks that you have
Python 3.8+. Then start a new Claude Code session — it picks the skill up automatically.

By default the installer creates a **link** (a directory junction on Windows, a symlink on
Unix), so edits in this folder take effect immediately with no reinstall. Junctions and symlinks
both work as a normal user; administrator rights and Developer Mode are not required.

| Option | Effect |
|---|---|
| *(none)* | Install for the current user — `~/.claude/skills/` |
| `--project [DIR]` | Install into `DIR/.claude/skills/` instead (default: current directory) |
| `--copy` | Copy the files rather than linking. Use when this folder lives on a removable drive, a network share, or another machine |
| `--link` | Fail instead of silently falling back to copying |
| `--name NAME` | Install under a different folder name |
| `--force` | Replace an existing installation |
| `--uninstall` | Remove it again |
| `--help` | Full usage |

Re-running the installer is safe: if it is already linked here it says so and does nothing.

If you always use the same FragPipe, name it once and the skill will stop asking which one:

```bat
setx FRAGPIPE_DIR "C:\FragPipe\FragPipe-24.1-build36"
```

```bash
export FRAGPIPE_DIR=/opt/fragpipe
```

Check what it resolves to at any time with:

```bash
python scripts/check_fragpipe.py          # or --list to see every version installed
```

### Uninstall

```bat
install.bat --uninstall
```

```bash
./install.sh --uninstall
```

Removing a link never touches the files it points at, and the installer refuses to delete a
directory that is not a FragSkills install.

## Layout

```
SKILL.md                      entry point Claude reads
install.sh                    installer for Linux / macOS
install.bat                   installer for Windows
commands/                     four-line dispatchers into SKILL.md, not a second copy of it
  fragskills-run.md           launch a headless run
  fragskills-status.md        report progress
  fragskills-report.md        build the QC report and interpret it
references/
  running.md                  CLI flags, manifest/workflow formats, workflow selection
  outputs.md                  output layout rules + column reference for every report
  plots.md                    the summary report: every panel, its source, how to read it
  troubleshooting.md          failure modes and how to diagnose them
scripts/
  check_fragpipe.py           locate the FragPipe install and report it for confirmation
  fragpipe_run.py             launch headless, stream progress, maintain status JSON
  fragpipe_status.py          poll a run (exit 0/1/2/3 = ok/failed/running/not found)
  fragpipe_progress.py        stdout -> structured progress (importable + CLI)
  make_report.py              build fragpipe-report.html from a results folder, in pure Python
  qc_data.py                  the data behind those plots as JSON/TSV, for custom figures
  svg_charts.py               dependency-free SVG chart primitives used by the report
  make_manifest.py            build .fp-manifest files, with FragPipe's naming rules enforced
  parse_reports.py            discover and summarize psm/peptide/ion/protein/combined_* TSVs
  run_type.py                 classify the run and rank the files that hold its result
  quant_matrix.py             read TMT / DIA / DIA-NN quant matrices (log2 vs linear, NA vs empty)
  selftest.py                 regression suite, run against a finished FragPipe output dir
```

Scripts are pure standard-library Python 3.8+ — no pandas or numpy needed. Use pandas for actual
downstream analysis; these tools are for orchestration and orientation.

## Verify

```bash
python scripts/selftest.py --example /path/to/a/finished/fragpipe/output

# one directory per analysis type covers every output layout
python scripts/selftest.py --example ~/runs/dda ~/runs/dda_no_group ~/runs/dia \
                                     ~/runs/tmt ~/runs/tmt_phospho
```

Checks the log parser (full and mid-run), success/failure detection, output discovery, run-type
classification, report summaries, the QC series and the summary report against real completed runs
— each directory needs a `log_*.txt` plus the report TSVs. The classification precedence and the
quant-matrix conventions are also tested against synthetic trees, so those run anywhere. Run it
after any change.

## Status

Validated end to end against five real run layouts: DDA with named experiments, DDA with none, DIA,
TMT, and TMT with phospho enrichment. For each one the skill identifies the run type, points at the
right result files, and reads them — including the column names, `log2`-vs-linear values and
`NA`-vs-empty missing markers, all taken from the output itself rather than from documentation.

The report generator is validated against four layouts — single- and multi-experiment DDA, TMT, and
DIA. Counts (charge, missed cleavages, ID bars, per-run histograms, feature weights) match
FragSummarizer 1.0.2 exactly, and the drawn geometry agrees to within 0.1 px per panel; the
deliberate departures are listed in `references/plots.md`.

Not yet covered: glyco and open/offset searches (`Observed Modifications` is parsed but has no
dedicated summarizer), and DIA-NN's `.speclib`/`.parquet` outputs, which are listed but not read.
