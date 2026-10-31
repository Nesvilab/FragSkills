# FragSkills

[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-D97757)](#install)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-3776AB)](#install)

FragSkills is a Claude Code plugin that runs [FragPipe](https://fragpipe.nesvilab.org/) headless,
from raw files to results, and reads the output. From a few questions about the experiment, it
picks the workflow, launches FragPipe, tracks the run, and points at the files to use.

Three things make headless FragPipe easy to get wrong: there are 77 shipped workflows to choose
from, FragPipe exits 0 whether the run succeeded or failed, and each run type writes files that
look like the result but are not (in a DIA run, the root `protein.tsv` describes the spectral
library, not the quantification). FragSkills handles each of them before, during, and after the
run.

## What it does

- **Picks the workflow from a few questions**, such as how the spectra were acquired, how the
  samples are quantified, and whether they were enriched for a PTM. The answers select one of the
  shipped workflows, used unchanged apart from the FASTA, contaminant removal, and TMT channel
  count. The builder needs FragPipe 24.1 or 25.0; with other versions, pick the workflow by hand
  and launch it with `/fragskills:run`.
- **Confirms which FragPipe to use**, since several versions are often installed side by side.
- **Checks the inputs before launch**: a misspelled data type that FragPipe would read as DDA, a
  manifest row it would drop, a file name it would refuse.
- **Tracks progress exactly**, as finished tasks out of FragPipe's own task plan, and tells success
  from failure by FragPipe's output rather than its exit code.
- **Stops a run cleanly.** `/fragskills:stop` ends FragPipe and every tool it started, after you
  confirm, so no MSFragger or DIA-NN process is left running.
- **Names the real result.** It identifies the run type and says which files to quantify from and
  which to leave alone (validated on five real layouts; see [Status](#status)).
- **Builds the QC report headless runs never produce** (`fragskills-qc-report.html`), with counts
  that match FragSummarizer 1.0.2. It reads any finished results folder and needs no FragPipe or
  Java.

Nothing is launched before you confirm a table of every answer and every changed parameter. For a
TMTpro 18-plex phospho experiment, the builder picks the shipped TMT16-phospho workflow, and the
table lists the three changes:

```text
Parameters changed from TMT16-phospho.workflow (every other parameter is FragPipe's)
  parameter                         source                          value
  database.db-path                  answer: fasta                   D:\db\human_decoys.fasta
  phi-report.remove-contaminants    answer: remove_contaminants     true  (was false)
  tmtintegrator.channel_num         answer: tmt_plex                TMT-18  (was TMT-16)
```

[Install](#install) it, then type `/fragskills:new <data folder>` or ask in plain language;
[Use](#use) lists the commands.

## Install

You need Claude Code, Python 3.8+, FragPipe and Git. Windows comes without Git: run
`winget install --id Git.Git -e --source winget`, then open a new terminal. In Claude Code, type:

```
/plugin install fragskills --marketplace Nesvilab/FragSkills
```

Or, in a terminal: `claude plugin install fragskills --marketplace Nesvilab/FragSkills`. Start a
new Claude Code session to use it. If your Claude Code rejects `--marketplace`, run `claude update`,
or install in two steps: `claude plugin marketplace add Nesvilab/FragSkills`, then
`claude plugin install fragskills@nesvilab`.

**Updates.** Turn on auto-update once: `/plugin`, Marketplaces tab, `nesvilab`, Enable
auto-update. Claude Code then downloads each change in the background while you work, and loads
it the next time you start Claude Code (or at once with `/reload-plugins`). To update by hand:
`claude plugin update fragskills@nesvilab`, then start a new session.

**Used `install.bat` or `install.sh` before?** Run it again with `--uninstall` from that folder
first, or Claude Code shows the skill twice.

**From a clone** (to edit the skill, or on a machine without network access): in this folder run
`install.bat` (Windows) or `./install.sh` (Linux / macOS). It links the folder into
`~/.claude/skills/fragskills`, so edits take effect at once, and checks for Python. Options:
`--project [DIR]` (the commands then load only when Claude Code starts in DIR), `--copy` (for a
removable or network drive), `--name NAME`, `--force`, `--uninstall`, `--help`.

To stop the skill asking which FragPipe to use, set `FRAGPIPE_DIR`. Windows:
`setx FRAGPIPE_DIR "C:\FragPipe\FragPipe-24.1-build36"`. Linux / macOS:
`export FRAGPIPE_DIR=/opt/fragpipe`. To see the installs the skill finds, ask Claude "which
FragPipe installs can you find?".

## Use

Ask in plain language ("analyse the TMT data in D:/data", "why did the run in D:/run/out fail?"),
or type a command:

| Command | Does |
|---|---|
| `/fragskills:new <data folder> [workdir]` | Ask the questions, build and confirm the workflow, launch |
| `/fragskills:run <workdir> [workflow] [manifest]` | Launch a headless run in the background |
| `/fragskills:status <workdir>` | Report progress, or why a run stopped |
| `/fragskills:report <workdir>` | Build the QC report and explain it |
| `/fragskills:stop [workdir]` | Stop a run and every tool it started, after you confirm |

**A new analysis** (`/fragskills:new`) asks you to confirm the FragPipe install, answer the
questions and check the proposed design (the channel sheet for TMT). It launches only after you
confirm the table of every answer and changed parameter. In `<workdir>/fragskills-repro/`, edit
only the design and channel sheets: `fragpipe_run.py` refuses a built workflow or manifest that
changed after you confirmed it. To change other parameters, start from your own workflow with
`/fragskills:run`.

**Any finished FragPipe results folder** can be read, with no FragPipe or Java. Ask Claude, or
run the scripts yourself from a clone (the plugin keeps its copy under
`~/.claude/plugins/cache/nesvilab/fragskills/<version>/`):

```bash
python scripts/parse_reports.py --workdir D:/run/out              # run type and main result
python scripts/parse_reports.py --workdir D:/run/out --summary    # + counts, completeness
python scripts/make_report.py   --workdir D:/run/out              # fragskills-qc-report.html
python scripts/qc_data.py       --workdir D:/run/out --tsv D:/qc  # the data behind the plots
```

Quantify TMT from `tmt-report/` and DIA from `dia-quant-output/`, not from `combined_*.tsv` or
the root `psm.tsv`/`protein.tsv`. TMT values are log2 with `NA` for missing; DIA values are linear
with empty cells. `references/outputs.md` has every column.

## Layout

```
SKILL.md        the instructions Claude reads
.claude-plugin/ the plugin manifest and the nesvilab marketplace that lists it
commands/       the five slash commands, each pointing at a section of SKILL.md
references/     running FragPipe, output formats, QC plots, troubleshooting
scripts/        standard-library Python 3.8+, no pandas or numpy
  check_fragpipe.py     find the FragPipe install
  build_workflow.py     questions -> workflow, manifest and confirmation table
  workflow_tree.json    the questions, one route per shipped workflow
  make_manifest.py      manifests and design sheets, with FragPipe's rules checked
  fragpipe_run.py       launch headless and track progress
  fragpipe_status.py    poll a run
  fragpipe_stop.py      stop a run and the tools it started
  parse_reports.py      classify a run and summarize its reports
  make_report.py        the QC report
  qc_data.py            the data behind the QC plots
  selftest.py           the test suite
  ...                   shared modules
```

## Test

```bash
python scripts/selftest.py                                     # synthetic checks and the builder
python scripts/selftest.py --example D:/runs/dda D:/runs/tmt   # + finished runs
```

`--example` takes finished output folders (each with its `log_*.txt`), one per analysis type. The
builder test also rebuilds every shipped workflow from the FragPipe installs it finds
(`--fragpipe-dir` to choose). Run it after any change, and `claude plugin validate .` after
changing `.claude-plugin/` (the only expected warnings are the missing version and icon: leaving
out the version makes every commit an update).

## Status

- Run reading is validated on five real layouts: DDA with and without experiments, DIA, TMT, and
  TMT with phospho enrichment.
- QC report counts match FragSummarizer 1.0.2; `references/plots.md` lists the deliberate
  differences.
- The builder rebuilds all 77 shipped workflows key for key on 24.1-build61 and on FragPipe
  develop (25.0-build3).
- Not yet covered: workflows beyond the shipped ones, glyco and open-search summaries, and DIA-NN
  `.speclib`/`.parquet` outputs (listed, not read).

## License

MIT. See `LICENSE`.
