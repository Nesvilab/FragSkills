# Summary report and QC plots

FragPipe's QC report (`fragpipe-report.html`) is derived entirely from files the run already wrote:
**`psm.tsv`**, the run's **`log_*.txt`**, the manifest, and the PNGs MSBooster produced. No extra
tool, no re-search, and nothing that has to come from the FragPipe installation.

**Headless runs never generate it.** FragPipe builds the report only from the *Generate summary
report* button on the GUI's Run tab, so after `fragpipe_run.py` it does not exist.

## Which path to use

| Goal | Use |
|---|---|
| The report a FragPipe user expects | `make_report.py` — writes `fragpipe-report.html` |
| A number to quote, a custom figure, one run out of many, a cross-run comparison | `qc_data.py` — the same series as JSON/TSV, then plot it yourself |
| A one-off chart in the report's style | `svg_charts.py` — the drawing primitives on their own |

```bash
python scripts/make_report.py --workdir D:/run/out          # -> fragpipe-report.html
python scripts/qc_data.py --workdir D:/run/out --list       # what is available
python scripts/qc_data.py --workdir D:/run/out --tsv D:/qc  # tidy TSVs
python scripts/qc_data.py --workdir D:/run/out --json qc.json
```

All three are **pure standard-library Python 3.8+**: no FragPipe installation, no Java, no pandas,
no matplotlib. A copied results folder on any machine is enough. A 35 MB `psm.tsv` takes about two
seconds.

### How it relates to FragPipe's own generators

FragPipe has shipped two: `tools/generate_reports_pdf.py` (matplotlib + PyPDF2, through 24.0) and
`tools/FragSummarizer-<version>.jar` (Java + Tablesaw, 24.1 onward). This is a port of both — same
pages, same panels, same derivations. Every chart was compared against the jar's output on a real
DDA run: bar counts, positions, heights and scatter point counts agree to within 0.1 px, apart from
the Gantt bars (2.1 px, because the jar truncates each task duration to whole seconds).

Four deliberate differences:

| | Here | FragPipe's generators |
|---|---|---|
| Peptide counts on DIA/spectral-library ID bars | distinct `Modified Peptide` **or** `Peptide` | distinct `Modified Peptide` only — and that column is **empty for unmodified peptides**, so their bars count only modified forms (7 561 instead of 30 222 on the reference DIA run) |
| Gantt time axis | run start from the log file name + the reported total | the largest `time="HH:MM:SS"` stamp in the log, which comes from Philosopher and can fall outside the run's own window (10 minutes before the run started, on the reference run) |
| MS1 vs MS2 mass-error scale | independent per level | jar: independent; `generate_reports_pdf.py`: shared, which flattens MS1 when MS2 is far off, and is meaningless when the two use different units |
| Percolator weights with no MSBooster plots | shown, page titled *Percolator Features* | dropped entirely — the whole page is conditional on MSBooster plots existing |

Panel titles are also spelled out (*Peptide IDs*, *Delta Mass Distribution*) rather than abbreviated
(*Peptides IDs*, *Delta Mass Dis*).

## What the report contains

Five kinds of page, in this order:

| Page | Panels | Source |
|---|---|---|
| Experiment Design | the manifest as a table | `fragpipe-files.fp-manifest` |
| Workflow Processing Time | Gantt chart of tasks on a clock axis, total badge | `log_*.txt` |
| Overall Statistics | charge, peptide length, PSM/peptide/protein IDs, missed cleavages, MS1 and MS2 mass error | `psm.tsv` + report row counts + `log_*.txt` |
| `<run>` Statistics — one page per raw file | peptide length, missed cleavages, charge, m/z, retention time, hyperscore, delta mass, m/z vs delta mass, RT vs m/z | `psm.tsv` |
| MSBooster & Percolator — one page per raw file | Percolator feature weights, RT calibration curve, delta RT (LOESS), predicted vs observed RT, spectral entropy | `log_*.txt` + `MSBooster/MSBooster_plots/` |

## Where each number comes from

### From `psm.tsv`

Rows are grouped into runs by the **`Spectrum`** column: `b1923_....00312.00312.2` → take the base
name up to the first `.` → `b1923_...`. Not by `Spectrum File`.

| Panel | Column | Derivation |
|---|---|---|
| Peptide length | `Peptide Length` | histogram |
| Charge | `Charge` | count per integer value |
| Missed cleavages | `Number of Missed Cleavages` | count per integer value |
| M/Z | `Calculated M/Z` | histogram |
| Retention time | `Retention` | **divide by 60** — the column is seconds, every plot is minutes |
| Hyperscore | `Hyperscore` | histogram |
| Delta mass | `Delta Mass` | histogram, unfiltered |
| M/Z vs delta mass | `Calculated M/Z`, `Delta Mass` | scatter, keeping **`abs(Delta Mass) < 0.5` Da** |
| RT vs M/Z | `Retention`/60, `Calculated M/Z` | scatter, no filter |

Two rules that change the numbers if you skip them:

- **The overall distributions are deduplicated**; the per-run pages are not. Charge, peptide length
  and missed cleavages on the Overall Statistics page count each
  `(Spectrum, Modified Peptide, Peptide Length, Charge, Number of Missed Cleavages)` **once**, so a
  spectrum reported at several ranks does not inflate the shape.
- **Scatter panels are downsampled** to ~5000 points by taking every *k*-th row. Do the same: a
  200 000-point SVG is unreadable and slow, and the shape does not change.

Binning follows one rule, and it matters for the discrete columns: if every value is a whole number
and the range spans less than 200, use **one bin per integer** (peptide length 7…50 → 44 bins).
Otherwise use `n/20` bins clamped to `[10, 50]`. Fractional bins over integer data produce a comb
of empty bars.

### Identification counts

The PSM / Peptides / Proteins bars are **row counts of the report files**, one bar per experiment:
`psm.tsv`, `peptide.tsv`, `protein.tsv` in each experiment folder (or the workdir root — see
`outputs.md` for the layout rule). They are not recomputed from `psm.tsv`, so peptide and protein
bars reflect Philosopher's filtered lists. The panel title says **(per experiment)**.

DIA and spectral-library runs collapse everything into the workdir root, which leaves a single bar
carrying no comparison. Those fall back to splitting `psm.tsv` by raw file — PSMs per run, distinct
peptide forms, distinct proteins — labelled by the manifest's experiment name. The panel title then
says **(per run)**, and the peptide and protein numbers are psm-derived rather than Philosopher's.
`qc_data.py` exposes both as `ids` and `ids_per_run`.

### From `log_*.txt`

The newest `log_*.txt` in the workdir is the one used (sorted by name; the name carries the
timestamp). Three sections are parsed.

**Task runtimes → the Gantt chart.** The block after the line `Task Runtimes:`, one
`<task>: <minutes> minutes` per line. Parse at the **last** colon — `Percolator: Convert to pepxml:
0.10 minutes` has two. The chart is not a plain copy of that block:

- `MSFragger` is replaced *in place* by its sub-stages, taken from the MSFragger banners
  (`FIRST SEARCH DONE IN ... MIN`, `MASS CALIBRATION AND PARAMETER OPTIMIZATION DONE IN ...`,
  `MASS CALIBRATION DONE IN ...`, `MAIN SEARCH DONE IN ...`).
- All `Percolator*` entries are summed into one bar.
- `WorkspaceClean`, `WorkspaceCleanInit` and zero-length tasks are dropped.

The badge total is the sum of the bars, so it is slightly *below* the `ALL JOBS DONE IN <n> MINUTES`
figure. Bars are laid out backwards from the run's finish time, so the x axis is clock time, not
elapsed time — tasks are assumed contiguous, which is true for a normal sequential run.

**Mass calibration → the MS1/MS2 error bars.** MSFragger prints a table inside the
`MASS CALIBRATION AND PARAMETER OPTIMIZATION` (or plain `MASS CALIBRATION`) banner:

```
 Run |  MS1   (Old)  |  MS1   (New)  |  MS2   (Old)  |  MS2   (New)
 001 |   1.52   1.14 |  -0.04   0.85 |   0.73   1.70 |  -0.13   1.68
```

Each cell is `median MAD`. The plot shows the **medians**, before vs after calibration. Rows are
those starting with a digit, between the banner and `Finding the optimal parameters:`. When
calibration runs more than once (multiple search stages) the parts are numbered and kept separate.
Units: MS1 is ppm; MS2 follows `fragment_mass_units` (`0` = Da, `1`/`2` = ppm) from the same log.

The table names its rows `001`, `002`, … and nothing else — **that index is not the manifest
order**, so a label has to come from the log. Two forms appear: DIA searches number the files
explicitly (`002. sample.mzML 9.0 s`), DDA searches list them in processing order before the first
search (`sample.mzML: Scans = 42672; ...`). `qc_data.py` reads both and falls back to the bare
index when neither is present; the report itself always shows `Part 1: 001`.

**Percolator SVM weights → the feature-weight bars.** After each per-run Percolator invocation:

```
Learned normalized SVM weights for the 3 cross-validation splits:
 Split1	 Split2	 Split3	FeatureName
-0.1920	-0.2367	-0.2257	abs_ppm
```

The plot shows the **mean of the three splits**, sorted by absolute value, green for positive and
red for negative. Features named `*m0*` and rows that are zero in all three splits are dropped.
Blocks are matched to runs in order, against the
`Reading tab-delimited input from datafile <run>_edited.pin` lines.

### From `MSBooster/MSBooster_plots/`

PNGs written by MSBooster, embedded as base64. Only four kinds are included:

| Folder / file token | Label in the report |
|---|---|
| `RT_calibration_curves/` | RT Calibration Curve |
| `*delta_RT_loess*` | Delta RT (LOESS) |
| `*pred_RT_real_units*` | Predicted vs. Observed RT |
| `*unweighted_spectral_entropy*` | Unweighted Spectral Entropy |

The run name is the file name up to `_edited`. `RT_calibration_curves/` holds both `<run>.png` and
`<run>_calibrated.png`; both are shown, the second labelled *(calibrated)*. (FragPipe's own report
keys on the raw file name, so the calibrated curve lands under a run that does not exist and never
appears.) Which files exist depends on the MSBooster version and on which features were enabled —
`pred_RT_real_units` is absent in recent versions, where `delta_RT_loess_real` plays that role.

## Reading the plots

What to look for, in the order the pages appear:

- **Runtime Gantt** — one task dominating is normal (MSFragger, MSBooster, SpecLibGen, DIA-NN). A
  long `PhilosopherFilter` or `IonQuant` relative to the search suggests too many experiments or a
  huge FASTA.
- **Charge** — tryptic DDA should peak at 2+, then 3+. A 1+-heavy distribution means the data are
  not what the workflow assumes; a 4+-heavy one suggests missed cleavages or a non-tryptic enzyme.
- **Peptide length** — a smooth mode around 9–14. A hard cut at the edges means
  `digest_min_length` / `digest_max_length` is clipping real peptides.
- **Missed cleavages** — mostly 0, with 1 clearly smaller. Comparable counts at 0 and 1 mean an
  incomplete digest; a spike at 2 with `allowed_missed_cleavage` set to 1 cannot happen and points
  at the wrong enzyme setting.
- **ID bars** — compare runs against each other, not against an absolute. One run at half the PSMs
  of its neighbours is a sample or LC problem, not a search problem.
- **MS1/MS2 mass error** — the point of the panel is the *change*: `Old` medians can be several ppm
  off, `Calibrated` should sit near 0 (|median| well under 1 ppm for Orbitrap MS1). A calibrated
  median that stays offset means calibration failed for that run — check the search tolerances.
- **Delta mass** — closed search: one sharp spike at 0 (median ≈ 0.000 Da). Open search: the spike
  at 0 plus discrete peaks at real modification masses. A broad smear with no structure means the
  precursor tolerance is too wide for the data.
- **M/Z vs delta mass** — a horizontal band at 0. A tilt with m/z is uncorrected calibration; the
  ±0.5 Da window makes isotope-error stripes at ±1 Da invisible by design.
- **RT vs M/Z** — even coverage across the gradient. Empty early or late regions mean the gradient
  is not being used; a dense vertical stripe is a co-eluting contaminant.
- **Percolator features** — `delta_RT_loess` and the spectral-entropy/hyperscore features carrying
  the largest weights is the healthy MSBooster pattern. If the RT and spectral features are near
  zero, MSBooster contributed nothing and rescoring gained little.
- **MSBooster RT calibration** — the observed-vs-predicted cloud should be a tight monotone curve.
  A fat cloud means the RT model does not fit the gradient, which weakens rescoring.

## Building your own figures

`qc_data.py` is the extraction layer; do the plotting yourself. `--json` gives one document with
every series, `--tsv DIR` writes long-format tables that load straight into pandas or R:

| File | Columns |
|---|---|
| `runtimes.tsv` | `task`, `minutes` |
| `ids.tsv` | `experiment`, `psms`, `peptides`, `proteins` |
| `mass_calibration.tsv` | `part`, `run_index`, `run`, `level` (MS1/MS2), `state` (old/calibrated), `median`, `mad` |
| `percolator_features.tsv` | `run`, `feature`, `mean_weight` |
| `distributions.tsv` | `scope` (`overall` or run), `series`, `bin_start`, `bin_end`, `count` |
| `scatter.tsv` | `run`, `series`, `x`, `y` |
| `msbooster_plots.tsv` | `run`, `label`, `path` |

`--json` additionally carries `ids_per_run`, `started_at` / `finished_at`, `search` (the tolerances)
and `run_order`.

In JSON, each series is one of three shapes:

```jsonc
{"type": "category",  "categories": [2,3,4], "counts": [41301,26478,5594], "n": 73952}
{"type": "histogram", "bin_edges": [7.0,8.0,...], "counts": [...], "n": ..., "min":, "max":, "mean":, "median":}
{"type": "scatter",   "x": [...], "y": [...], "n_total": 18306, "downsampled_by": 3}
```

Pre-binned counts are what the report draws, so plot them with `plt.bar`/`plt.stairs`, not `plt.hist`:

```python
import json, matplotlib.pyplot as plt
d = json.load(open("qc.json"))

h = d["runs"]["b1923_293T_proteinID_03A_QE3_122212"]["peptide_length"]
plt.stairs(h["counts"], h["bin_edges"], fill=True)

mc = d["mass_calibration"]                      # before/after, per run
plt.bar([r["run"] for r in mc], [r["ms1_old_median"] for r in mc])
plt.bar([r["run"] for r in mc], [r["ms1_calibrated_median"] for r in mc])
```

For anything not in the report — a PTM-site histogram, quant CVs, a peptide-overlap Venn — go
straight to `psm.tsv` / `combined_*.tsv` with pandas (`pd.read_csv(path, sep="\t")`) and follow the
column reference in `outputs.md`. Remember that the reports are already FDR-filtered and that
contaminants are still present.

Use the report's own colours if you want the figures to sit beside it: `#696969` for distribution
bars, `#6B8EDB` for the Gantt bars, `#808080` (before) and `#1a1a1a` (calibrated) for the mass-error
pairs, `#2ecc71`/`#e74c3c` for positive/negative feature weights. `svg_charts.py` exposes them as
constants and will draw any of these panels on its own:

```python
import qc_data, svg_charts as svg
data = qc_data.collect("D:/run/out")
open("charge.svg", "w").write(
    svg.category_bar(data["overall"]["charge"], "Charge", "Charge States"))
```

## When it does not work

| Symptom | Cause |
|---|---|
| Exit 1, "nothing to report on" | `--workdir` is not the FragPipe output directory, or the run never reached the report stage. A `log_*.txt` and `psm.tsv` are the minimum. |
| Report generated, per-run pages missing | No `psm.tsv` — the run failed early, or `phi-report.run-report` was off. Everything log-derived still renders. |
| No Experiment Design table | `fragpipe-files.fp-manifest` is missing from the workdir. Keep it: it also carries the experiment/bioreplicate mapping. |
| No Gantt chart | No `Task Runtimes` block, i.e. the run did not finish. |
| Gantt on an elapsed axis instead of clock times | The log was renamed, so the start time could not be read from `log_<date>_<time>.txt`. Bars and durations are unaffected. |
| No mass-error or Percolator panels | The run used PeptideProphet rather than Percolator, or MSFragger's calibration was disabled (`calibrate_mass = 0`). Nothing is wrong; those sections are simply absent from the log. |
| No MSBooster page | MSBooster was off, or `MSBooster/MSBooster_plots/` was cleaned up by "delete temp files". |
| Very large `psm.tsv` is slow, or memory runs out | `--stride 10` keeps every tenth PSM: the distributions keep their shape, the counts scale by 1/10. |
| Report is tens of MB | MSBooster PNGs are embedded. `--link-images` references them instead, at the cost of self-containment. |
| Counts differ from a report FragPipe generated | Expected in the four cases listed at the top; otherwise check deduplication on the overall panels and seconds→minutes on retention. |
