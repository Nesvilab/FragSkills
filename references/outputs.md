# FragPipe output: layout and column reference

All report files are UTF-8, tab-separated, exactly one header row, no comment lines, no quoting.
`pd.read_csv(path, sep="\t")` reads them directly.

## Which file is the result

A finished run leaves several plausible-looking tables side by side, and the right one depends on
the acquisition. Picking the wrong one is not an error anyone notices: the file opens, the numbers
look reasonable, and they answer a different question. The rule, most specific first:

| If the workdir has | The run was | Main result | Ignore for quant |
|---|---|---|---|
| `tmt-report/` with `*single-site*`/`*multi-site*` | TMT + PTM enrichment | `tmt-report/abundance_single-site_*.tsv`, `ratio_single-site_*.tsv` (and `multi-site`) | `combined_*.tsv`, raw channels in `psm.tsv` |
| `tmt-report/` | TMT / isobaric | `tmt-report/abundance_protein_*.tsv`, `ratio_protein_*.tsv` | `combined_*.tsv`, raw channels in `psm.tsv` |
| `dia-quant-output/` | DIA | `dia-quant-output/abundance_protein_*.tsv` (+ `gene`, `peptide`) | root `psm/peptide/ion/protein.tsv` — those are the **library** |
| `combined_*.tsv` | DDA, experiments named | `combined_protein.tsv`, then `combined_peptide.tsv` / `combined_ion.tsv` | the per-experiment folders (same data, one sample at a time) |
| none of the above | DDA, single/unnamed experiment | root `protein.tsv`, `peptide.tsv`, `ion.tsv`, `psm.tsv` | — |

`parse_reports.py --workdir <dir>` applies exactly this table and prints the verdict, the ranked
files, and what not to use, so run it rather than inferring from a directory listing. The same
verdict is on the inventory as `classification` (and in `fragskills_status.json`).

Two cross-cutting conventions decide whether the numbers come out right:

- **TMT-Integrator writes log2 values and `NA` for missing.** Do not log-transform again.
- **DIA (FragPipe and DIA-NN) writes linear intensities and leaves missing cells empty.**
  An empty cell is "not quantified", not zero; `combined_*.tsv` uses literal `0` for the same thing.

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
├── combined_site_<AA>_<mass>.tsv                # one per localized modification
├── tmt-report/                                  # TMT-Integrator
├── dia-quant-output/                            # DIA-NN
├── library.tsv irt.tsv                          # spectral library
├── *.pepXML *.pin interact-*.pep.xml            # intermediates
├── combined.prot.xml protein.fas                # protein inference input/output
├── experiment_annotation.tsv                    # file -> sample/condition, as FragPipe saw it
├── msstats.csv msstats_ptm.csv                  # MSstats input
├── reprint.int.tsv reprint.spc.tsv              # REPRINT/CRAPome input
├── fragpipe.workflow fragpipe-files.fp-manifest # exact settings used — keep these
├── fragger.params filter.log sdrf.tsv
└── log_<date>_<time>.txt                        # full console log
```

`experiment_annotation.tsv` is the quickest way to see how FragPipe interpreted the manifest —
which file became which sample, and for TMT which plex and channel. When sample names in a quant
matrix are not what the user expected, that file is the reason.

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

## combined_site_&lt;AA&gt;_&lt;mass&gt;.tsv — site-level localization

IonQuant writes one per localized modification, named for the residue and delta mass:
`combined_site_M_15.9949.tsv` (oxidized Met), `combined_site_C_57.0215.tsv` (carbamidomethyl Cys),
`combined_site_S_79.9663.tsv` (phospho-Ser). Columns:

`Index`, `Gene`, `Protein`, `Protein ID`, `Peptide`, `Best Localization Probability`, then per
sample `<sample> Localization Probability` and `<sample> Intensity`.

`Index` is `<Protein ID>_<residue><position>`, e.g. `A0AVT1_M492`. The lowercase letter inside
`Peptide` marks the modified residue (`GmITVTDPDLIEK`). `Best …` is the maximum across samples, not
a sample — do not read it as one. Intensity `0.0` means the site was not quantified in that sample.
A localization probability below ~0.75 means the site assignment is ambiguous within the peptide.

## tmt-report

Produced by TMT-Integrator when `tmtintegrator.run-tmtintegrator=true`. Two files per level:

| File | Rows are | Values |
|---|---|---|
| `abundance_<level>_<norm>.tsv` | the entity | **log2** normalized intensity |
| `ratio_<level>_<norm>.tsv` | the entity | **log2** ratio to `ReferenceIntensity` |

`<level>` is `gene`, `protein`, `peptide`, `modified-peptide`, and for PTM-enriched runs
`single-site` and `multi-site` instead of `modified-peptide`. `<norm>` is the normalization actually
applied — `MD` (median centering), `GN`, `None` — so file names differ between runs. Never hard-code
them; list the folder first.

Identifier columns, then **one column per sample**:

| Level | Identifier columns |
|---|---|
| protein | `Index`, `NumberPSM`, `Gene`, `MaxPepProb`, `Protein`, `Protein ID`, `Entry Name`, `Protein Description`, `Organism`, `Indistinguishable Proteins`, `ReferenceIntensity` |
| gene | `Index`, `NumberPSM`, `ProteinID`, `MaxPepProb`, `ReferenceIntensity` |
| peptide / single-site / multi-site | `Index`, `Gene`, `ProteinID`, `Peptide`, `SequenceWindow`, `Start`, `End`, `MaxPepProb`, `Spectrum Number`, `Protein`, `Entry Name`, `Protein Description`, `Mapped Genes`, `Mapped Proteins`, `ReferenceIntensity` |
| modified-peptide | as peptide, plus `Assigned Modification` after `Peptide` |

Gotchas that cost time:

- The gene-level table spells it **`ProteinID`**, protein-level **`Protein ID`** (with a space).
  Peptide and site levels use `ProteinID` too. Do not assume one spelling across a merge.
- **Values are already log2.** `ReferenceIntensity` is log2 as well. `NA` is the missing marker.
- `Index` identifies the row: an accession at protein level, `<ProteinID>_<AA><pos>` at single-site
  (`A0A024RBG1_S149`), `<ProteinID>_<start>_<end>_<n>_<n>_<AA><pos>` at multi-site. Lowercase
  residues inside `Peptide` mark the modification (`LGCsPANGNST…`).
- Sample column names come from the `sample` column of `experiment_annotation.tsv`, which derives
  from the per-folder `*annotation.txt` files. When those give no name, FragPipe falls back to
  `<plex>_<channel>` (`01CPTAC…_20171106_127N`) — informative, but it means the annotation file was
  not picked up as intended, which is worth telling the user.
- With more than one plex, only the **ratio** tables are comparable across plexes; abundances share
  no common scale until the reference channel relates them.
- Ratios that look compressed: check `Purity` in `psm.tsv` (co-isolation) before concluding biology.

## dia-quant-output

Produced when `diann.run-dia-nn=true`. Holds two families of files.

**FragPipe's tables** — `abundance_<level>_<tag>.tsv`, levels `protein`, `gene`, `peptide`,
`modified-peptide`; `<tag>` records the quant settings (`MS2quant_Norm_maxlfq`). Identifier columns
carry the full protein annotation (`Protein`, `Protein ID`, `Entry Name`, `Gene`, `Organism`,
`Description`, `Protein Existence`, `Protein Coverage`, `Protein Length`, `Global PG Q-value`,
`Indistinguishable Proteins`), then one column per run. Peptide levels use the Philosopher columns
(`Peptide Sequence`, `Prev AA`, `Next AA`, `Start`, `End`, `Peptide Length`, `Probability`,
`Charges`, `Mapped Genes`, `Mapped Proteins`, …). The gene table is just `Gene` plus the run columns.

**DIA-NN's own output** — the same MaxLFQ quantities in DIA-NN's naming:

| File | Rows are |
|---|---|
| `report.pg_matrix.tsv` | protein groups (`Protein.Group`, `Protein.Ids`, `Protein.Names`, `Genes`, `First.Protein.Description`) |
| `report.gg_matrix.tsv` | gene groups |
| `report.unique_genes_matrix.tsv` | genes, proteotypic peptides only |
| `report.pr_matrix.tsv` | precursors; `All Mapped Proteins`/`All Mapped Genes` sit **after** the sample columns |
| `report.stats.tsv` | one row per run — `Precursors.Identified`, `Proteins.Identified`, mass accuracy, FWHM |
| `report.tsv` | long format, one row per precursor **per run**, 62 columns — the source of all the above, and often 100 MB+ |

The two families agree: on the test data 10,372 of 11,249 protein-level cells match to DIA-NN's
6-significant-figure output precision. They are not row-for-row interchangeable — FragPipe's table
fills ~8% fewer cells and carries a few extra protein rows — so pick one and stay in it. Prefer
`abundance_protein_*.tsv` for its annotation; use `report.pg_matrix.tsv` to match a DIA-NN-based
pipeline exactly.

Gotchas:

- **Sample columns are full paths** to the spectral files
  (`F:\data\…\CPTAC_…_NAT.mzML`), not sample names, in both families. Reduce to basenames before
  plotting or joining. `report.stats.tsv` names runs the same way.
- Values are **linear** intensities; missing is an **empty cell**, not `0` and not `NA`.
- `report.tsv` is per-precursor-per-run: q-values (`Q.Value`, `PG.Q.Value`, `Global.Q.Value`),
  RT and IM predictions, and `Precursor.Normalised`/`PG.MaxLFQ`. Use it when a matrix cannot answer
  the question, and read it in chunks.
- The Philosopher `psm.tsv`/`peptide.tsv`/`ion.tsv`/`protein.tsv` at the workdir root describe the
  **spectral library** built from the DIA data, not the samples. Quote identification counts from
  `report.stats.tsv` unless the question is specifically about the library.
