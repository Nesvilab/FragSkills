# FragPipe output: layout and column reference

All report files are UTF-8, tab-separated, exactly one header row, no comment lines, no quoting.
`pd.read_csv(path, sep="\t")` reads them directly.

## Where the files are

This trips people up constantly. The rule comes from `InputLcmsFile.getGroup()`:

1. **If spectral-library generation, DIA-NN, or FragSpecLib is enabled**, every input is collapsed
   into one group and the reports go to the **workdir root**, regardless of experiment names.
2. Otherwise the group name is the manifest's experiment column — or `experiment_bioreplicate`, or
   `exp_<bioreplicate>` when the experiment is blank but a replicate is set — and reports go to
   **`<workdir>/<group>/`**.
3. A single unnamed experiment with no replicate means an empty group name, i.e. the **workdir root**.

`combined_*.tsv`, `tmt-report/`, and `dia-quant-output/` are **always** at the workdir root.

Run `python scripts/parse_reports.py --workdir <dir>` instead of guessing.

```
<workdir>/
├── psm.tsv peptide.tsv ion.tsv protein.tsv      # single/collapsed experiment
├── <experiment>/psm.tsv ...                     # or one folder per experiment
├── combined_peptide.tsv combined_protein.tsv    # multi-experiment matrices
├── tmt-report/                                  # TMT-Integrator
├── dia-quant-output/                            # DIA-NN
├── library.tsv irt.tsv                          # spectral library
├── *.pepXML *.pin interact-*.pep.xml            # intermediates
├── combined.prot.xml protein.fas                # protein inference input/output
├── fragpipe.workflow fragpipe-files.fp-manifest # exact settings used — keep these
├── fragger.params filter.log sdrf.tsv
└── log_<date>_<time>.txt                        # full console log
```

## Reading the reports correctly

- **They are already FDR-filtered** (1% PSM/peptide/protein by default, per `phi-report.filter`).
  Do not re-filter on `Qvalue` unless you have a reason; `Probability`/`Qvalue` are retained for
  inspection, not because filtering is outstanding.
- **Decoys are already removed** unless `phi-report.print-decoys=true`. `Is Decoy` will be uniformly
  `false`, so it is not a QC metric in a default run.
- **Contaminants are kept.** `Is Contaminant=true` rows (trypsin, keratins) should normally be
  excluded from biological conclusions but counted in QC.
- `Protein` is the full header of the group's lead protein; `Protein ID` is the accession; `Gene` is
  often empty for non-UniProt FASTAs.
- `Mapped Proteins` / `Mapped Genes` are semicolon-separated additional members of the group —
  a non-empty `Mapped Proteins` means the peptide is shared.
- `Assigned Modifications` looks like `17M(15.9949)` — position, residue, delta mass; `N-term` and
  `C-term` appear for terminal mods. Empty means unmodified.
- Intensities of `0` mean quantification did not run or found nothing, not "absent".

## psm.tsv — one row per peptide-spectrum match

42 columns. The spectrum-level evidence table; the largest file.

| Column | Notes |
|---|---|
| `Spectrum` | Scan identifier: `<file>.<scan>.<scan>.<charge>` |
| `Spectrum File` | Source `.pepXML`/run — use to split counts per input file |
| `Peptide` | Stripped sequence |
| `Modified Peptide` | Sequence with mods; **empty when unmodified** |
| `Extended Peptide` | Peptide in protein context |
| `Prev AA`, `Next AA` | Flanking residues |
| `Peptide Length`, `Charge`, `Retention` | Retention is in seconds |
| `Observed Mass`, `Calibrated Observed Mass`, `Observed M/Z`, `Calibrated Observed M/Z` | |
| `Calculated Peptide Mass`, `Calculated M/Z` | Theoretical |
| `Delta Mass` | Observed − calculated; the open-search PTM signal |
| `SpectralSim`, `RTScore` | MSBooster deep-learning features |
| `Expectation` | MSFragger E-value |
| `Hyperscore`, `Nextscore` | Primary and runner-up scores |
| `Probability` | PeptideProphet/Percolator posterior |
| `Qvalue` | PSM-level q-value |
| `Number of Enzymatic Termini`, `Number of Missed Cleavages` | Digestion QC |
| `Protein Start`, `Protein End` | Coordinates in the lead protein |
| `Intensity` | MS1 precursor intensity (IonQuant); `0` if not run |
| `Assigned Modifications` | Localized mods |
| `Observed Modifications` | Open/offset-search interpretation |
| `Purity` | Precursor isolation purity — key TMT QC (low purity ⇒ ratio compression) |
| `Is Decoy`, `Is Contaminant`, `Is Unique` | `Is Unique=true` ⇒ peptide maps to one protein group |
| `Protein`, `Protein ID`, `Entry Name`, `Gene`, `Protein Description` | Lead protein |
| `Mapped Genes`, `Mapped Proteins` | Other group members |

TMT/iTRAQ runs add one column per channel (e.g. `126`, `127N`, … or the annotated sample names).

## peptide.tsv — one row per peptide sequence

23 columns. `Peptide`, `Prev AA`, `Next AA`, `Peptide Length`, `Protein Start`, `Protein End`,
`Class`, `Charges`, `Probability`, `Qvalue`, `Spectral Count`, `Intensity`,
`Assigned Modifications`, `Observed Modifications`, `Is Decoy`, `Is Contaminant`, `Protein`,
`Protein ID`, `Entry Name`, `Gene`, `Protein Description`, `Mapped Genes`, `Mapped Proteins`.

`Charges` is a comma-separated list of observed charge states. `Spectral Count` is the number of
PSMs collapsing into this row — the peptide-level quant proxy.

## modified_peptide.tsv — one row per modified peptide form

Same shape as `peptide.tsv`, but keyed on the modified sequence, so the same stripped sequence
appears once per PTM form. Present only when the workflow reports modified peptides; a run with no
variable modifications will not produce it. Use it, not `peptide.tsv`, for PTM-level counting.

## ion.tsv — one row per peptide ion (sequence + mods + charge)

27 columns. `Peptide Sequence`, `Modified Sequence`, `Prev AA`, `Next AA`, `Peptide Length`,
`Protein Start`, `Protein End`, `Class`, `M/Z`, `Charge`, `Observed Mass`, `Probability`, `Qvalue`,
`Expectation`, `Spectral Count`, `Intensity`, `Assigned Modifications`, `Observed Modifications`,
`Is Decoy`, `Is Contaminant`, `Protein`, `Protein ID`, `Entry Name`, `Gene`, `Protein Description`,
`Mapped Genes`, `Mapped Proteins`.

This is the precursor-level unit IonQuant quantifies. One peptide yields several ion rows when it is
seen at multiple charges or in multiple PTM forms.

## protein.tsv — one row per protein group

26 columns.

| Column | Notes |
|---|---|
| `Protein`, `Protein ID`, `Entry Name`, `Gene` | Lead protein of the group |
| `Length`, `Organism`, `Protein Description`, `Protein Existence` | From the FASTA header |
| `Is Decoy`, `Is Contaminant` | |
| `Coverage` | Sequence coverage, percent |
| `Protein Probability`, `Top Peptide Probability`, `Protein Qvalue` | Inference confidence |
| `Total Peptides`, `Unique Peptides`, `Razor Peptides` | Evidence counts |
| `Total / Unique / Razor Spectral Count` | Spectral-counting quant |
| `Total / Unique / Razor Intensity` | MS1 quant; `0` when IonQuant did not run |
| `Razor Assigned Modifications`, `Razor Observed Modifications` | |
| `Indistinguishable Proteins` | Semicolon-separated; other accessions with identical evidence |

*Unique* = peptides belonging only to this group. *Razor* = shared peptides assigned to the group
with the most evidence. Prefer `Unique` for conservative claims, `Razor` for coverage. Report
protein counts as "protein groups", not "proteins" — `Indistinguishable Proteins` shows why.

## combined_*.tsv — cross-experiment matrices

`combined_peptide.tsv`, `combined_modified_peptide.tsv`, `combined_ion.tsv`,
`combined_protein.tsv`, always at the workdir root. Written when there is more than one experiment
**and** either IonQuant runs, or `phi-report.pep-level-summary` / `prot-level-summary` is on. If
they are missing with multiple experiments, that setting is the reason.

Layout is identifier columns followed by per-sample quant columns named
`<Experiment> <Measure>`, e.g. `ctrl Intensity`, `ctrl MaxLFQ Intensity`, `treat Spectral Count`.
`parse_reports.py --summary` extracts the sample names and measures from the header.

**`MaxLFQ Intensity` is the column to use for label-free differential analysis** — it is the
normalized, ratio-based estimate. Plain `Intensity` is the raw sum. Missing values are `0`; treat
them as missing, not as measured zero.

## tmt-report

Produced by TMT-Integrator when `tmtintegrator.run-tmtintegrator=true`. Contains abundance and ratio
matrices at several levels, typically `abundance_<level>_<norm>.tsv` and `ratio_<level>_<norm>.tsv`
for levels gene / protein / peptide / single-site / multi-site.

Rows are the biological entity, columns are the annotated sample names taken from the
`*annotation.txt` file that must sit next to the spectral files (one per folder — FragPipe locates
it by that naming convention, including in headless mode). Prefer the gene- or protein-level
abundance table for downstream statistics; use the ratio tables when a reference/bridge channel was
used. Check `Purity` in `psm.tsv` when ratios look compressed.

Run `parse_reports.py --workdir <dir>` to list exactly which files this run produced, then
`--file <path> --columns` on the one you need.

## dia-quant-output

Produced by DIA-NN when `diann.run-dia-nn=true`. Contains DIA-NN's own outputs — the main precursor
report, plus `pg_matrix` / `pr_matrix` style matrices and a `.stats` file. Quantities come from
DIA-NN, not Philosopher, so column names follow DIA-NN conventions (`Protein.Group`,
`Genes`, `Precursor.Id`, `PG.MaxLFQ`, …) rather than the Philosopher ones above.

For DIA runs the Philosopher `psm.tsv`/`protein.tsv` at the workdir root describe the **library**
built from the DIA data, not the final quantification. Report quantification from
`dia-quant-output/`, and identification counts from there too unless the question is specifically
about the library.

Discover the actual file names with `parse_reports.py` before referring to them.
