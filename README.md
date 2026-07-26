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
references/
  running.md                  CLI flags, manifest/workflow formats, workflow selection
  outputs.md                  output layout rules + column reference for every report
  troubleshooting.md          failure modes and how to diagnose them
scripts/
  check_fragpipe.py           locate the FragPipe install and report it for confirmation
  fragpipe_run.py             launch headless, stream progress, maintain status JSON
  fragpipe_status.py          poll a run (exit 0/1/2/3 = ok/failed/running/not found)
  fragpipe_progress.py        stdout -> structured progress (importable + CLI)
  make_manifest.py            build .fp-manifest files, with FragPipe's naming rules enforced
  parse_reports.py            discover and summarize psm/peptide/ion/protein/combined_* TSVs
  selftest.py                 regression suite, run against a finished FragPipe output dir
```

Scripts are pure standard-library Python 3.8+ — no pandas or numpy needed. Use pandas for actual
downstream analysis; these tools are for orchestration and orientation.

## Verify

```bash
python scripts/selftest.py --example /path/to/a/finished/fragpipe/output
```

Checks the log parser (full and mid-run), success/failure detection, output discovery, report
summaries, and manifest generation against a real completed run — it needs a directory containing
a `log_*.txt` plus the report TSVs. Run it after any change.

## Status

Implemented and validated against a DDA run (LFQ + spectral-library generation): running,
progress tracking, and the core `psm.tsv` / `peptide.tsv` / `ion.tsv` / `protein.tsv` /
`combined_*.tsv` reports.

`tmt-report/` and `dia-quant-output/` are discovered, listed, and documented structurally, but
their column-level semantics are described from the FragPipe documentation rather than verified
against real output. Feed a TMT and a DIA example through `selftest.py` and extend
`parse_reports.py` with dedicated summarizers to close that gap.
