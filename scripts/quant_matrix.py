"""Read the quantification matrices: TMT-Integrator, FragPipe DIA, DIA-NN.

All three write the same *shape* -- identifier columns on the left, one column
per sample on the right -- and agree on nothing else.  The level and the
normalisation live in the file name, the column names differ per producer, and
two conventions matter for anyone doing statistics on the numbers:

    producer          file name                        values      missing
    ---------------------------------------------------------------------
    TMT-Integrator    abundance_protein_MD.tsv         log2         "NA"
    FragPipe DIA      abundance_protein_<tag>.tsv      linear       "" (empty)
    DIA-NN            report.pg_matrix.tsv             linear       "" (empty)

Reading a TMT abundance table as if it were linear, or averaging DIA-NN's empty
cells as zero, both give wrong answers quietly.  ``summarize_matrix`` reports
which convention a file follows so the caller does not have to guess.

Everything streams; nothing here needs pandas.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

KIND_ABUNDANCE = "abundance"
KIND_RATIO = "ratio"

PRODUCER_TMT = "tmt-integrator"
PRODUCER_DIA = "fragpipe-dia"
PRODUCER_DIANN = "dia-nn"

#: Levels TMT-Integrator and FragPipe's DIA quant emit, coarse to fine.  The
#: order is the reporting order: gene and protein first, sites last.
LEVELS = ("gene", "protein", "peptide", "modified-peptide",
          "single-site", "multi-site")

_MATRIX_NAME = re.compile(
    r"^(abundance|ratio)_(%s)_(.+)\.tsv$" % "|".join(LEVELS), re.IGNORECASE)

#: DIA-NN's own matrices.  The level is encoded in the file name, but by an
#: abbreviation rather than the word, so it needs a lookup.
DIANN_MATRICES = {
    "report.pg_matrix.tsv": ("protein group", "MaxLFQ protein-group intensity"),
    "report.gg_matrix.tsv": ("gene group", "MaxLFQ gene-group intensity"),
    "report.unique_genes_matrix.tsv": (
        "gene (proteotypic only)", "gene intensity from unique peptides only"),
    "report.pr_matrix.tsv": ("precursor", "per-precursor intensity"),
}
#: Long-format, one row per precursor *per run* -- not a matrix, and large
#: enough (100 MB+) that scanning every cell is not worth it.
DIANN_LONG_REPORT = "report.tsv"
DIANN_STATS = "report.stats.tsv"

#: Every identifier column seen across the three producers.  Used to find where
#: the sample block starts; anything outside this set is taken to be a sample.
META_COLUMNS = frozenset((
    # TMT-Integrator
    "Index", "NumberPSM", "Gene", "MaxPepProb", "Protein", "Protein ID",
    "ProteinID", "Entry Name", "Protein Description", "Organism",
    "Indistinguishable Proteins", "ReferenceIntensity", "Peptide",
    "Assigned Modification", "SequenceWindow", "Start", "End",
    "Spectrum Number", "Mapped Genes", "Mapped Proteins",
    # FragPipe DIA quant
    "Description", "Protein Existence", "Protein Coverage", "Protein Length",
    "Global PG Q-value", "Peptide Sequence", "Prev AA", "Next AA",
    "Peptide Length", "Probability", "Charges", "Modified Peptide",
    "Assigned Modifications",
    # DIA-NN
    "Protein.Group", "Protein.Ids", "Protein.Names", "Genes",
    "First.Protein.Description", "Proteotypic", "Stripped.Sequence",
    "Modified.Sequence", "Precursor.Charge", "Precursor.Id",
    "All Mapped Proteins", "All Mapped Genes",
))

#: Cell values that mean "not quantified here".  TMT-Integrator writes NA,
#: DIA-NN and FragPipe's DIA quant leave the cell empty.  Zero is deliberately
#: NOT in this set: for a log2 table it is a real value.
MISSING_VALUES = frozenset(("", "NA", "N/A", "#N/A", "NaN", "nan", "null"))


# --------------------------------------------------------------------------
# identification, from the file name alone (no I/O)
# --------------------------------------------------------------------------
def describe_matrix(path: str) -> Optional[dict]:
    """What kind of matrix *path* is, judged by its name and parent folder.

    Returns ``None`` for a file that is not a quantification matrix.
    """
    name = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(os.path.abspath(path))).lower()

    if name in DIANN_MATRICES:
        level, meaning = DIANN_MATRICES[name]
        return {"file": path, "name": name, "producer": PRODUCER_DIANN,
                "kind": KIND_ABUNDANCE, "level": level, "tag": "",
                "log2": False, "missing": "(empty cell)", "meaning": meaning}

    m = _MATRIX_NAME.match(name)
    if not m:
        return None
    kind, level, tag = m.group(1).lower(), m.group(2).lower(), m.group(3)
    # tmt-report/ and dia-quant-output/ share this naming but not the numbers.
    if parent == "dia-quant-output":
        producer, log2, missing = PRODUCER_DIA, False, "(empty cell)"
        meaning = "MaxLFQ intensity from DIA-NN quant, %s level" % level
    else:
        producer, log2, missing = PRODUCER_TMT, True, "NA"
        meaning = ("log2 abundance, %s level" % level if kind == KIND_ABUNDANCE
                   else "log2 ratio to the reference channel, %s level" % level)
    return {"file": path, "name": name, "producer": producer, "kind": kind,
            "level": level, "tag": tag, "log2": log2, "missing": missing,
            "meaning": meaning}


def is_matrix(path: str) -> bool:
    return describe_matrix(path) is not None


def level_rank(level: str, ptm: bool = False) -> int:
    """Sort key putting the most useful level first.

    For a PTM run the site tables are the point of the experiment, so they lead;
    otherwise protein and gene do.
    """
    order = (("single-site", "multi-site", "protein", "gene", "peptide",
              "modified-peptide") if ptm else
             ("protein", "gene", "peptide", "modified-peptide",
              "single-site", "multi-site"))
    try:
        return order.index(level)
    except ValueError:
        return len(order)


# --------------------------------------------------------------------------
# columns
# --------------------------------------------------------------------------
def split_columns(header: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split a matrix header into (identifier columns, sample columns).

    Identifiers are the leading run of known column names.  DIA-NN's
    ``report.pr_matrix.tsv`` also puts two identifier columns *after* the
    samples, so the tail is filtered as well rather than assumed to be samples.
    """
    header = list(header)
    i = 0
    while i < len(header) and header[i] in META_COLUMNS:
        i += 1
    if i == 0 and header:
        i = 1  # unknown schema: the first column is an identifier regardless
    ids = header[:i] + [c for c in header[i:] if c in META_COLUMNS]
    samples = [c for c in header[i:] if c not in META_COLUMNS]
    return ids, samples


def short_sample(name: str) -> str:
    """Display name for a sample column.

    FragPipe's DIA matrices and DIA-NN name each column by the **full path** of
    the spectral file, which makes a header unreadable and unusable as a label.
    TMT columns are already short and pass through untouched.
    """
    if "\\" not in name and "/" not in name:
        return name
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    for ext in (".mzML", ".mzXML", ".raw", ".d", ".wiff", ".dia"):
        if base.lower().endswith(ext.lower()):
            return base[: -len(ext)]
    return base


# --------------------------------------------------------------------------
# summarising
# --------------------------------------------------------------------------
def summarize_matrix(path: str) -> dict:
    """Row/sample counts and per-sample completeness for one matrix.

    One streaming pass, one row at a time, so a 10 MB table costs nothing.
    """
    info = describe_matrix(path) or {"file": path,
                                     "name": os.path.basename(path)}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        header_line = fh.readline()
        if not header_line:
            return dict(info, rows=0, samples=[], sample_count=0)
        header = header_line.rstrip("\r\n").split("\t")
        ids, samples = split_columns(header)
        idx = [header.index(s) for s in samples]
        present = [0] * len(samples)
        rows = complete = 0
        lo = hi = None
        for line in fh:
            if not line.strip():
                continue
            rows += 1
            parts = line.rstrip("\r\n").split("\t")
            n_here = 0
            for j, col in enumerate(idx):
                cell = parts[col].strip() if col < len(parts) else ""
                if cell in MISSING_VALUES:
                    continue
                present[j] += 1
                n_here += 1
                try:
                    v = float(cell)
                except ValueError:
                    continue
                if lo is None or v < lo:
                    lo = v
                if hi is None or v > hi:
                    hi = v
            if samples and n_here == len(samples):
                complete += 1

    per_sample = {short_sample(s): present[j] for j, s in enumerate(samples)}
    quantified = sum(1 for c in present if c)
    return dict(
        info,
        rows=rows,
        id_columns=ids,
        samples=[short_sample(s) for s in samples],
        sample_columns=samples,
        sample_count=len(samples),
        values_per_sample=per_sample,
        complete_rows=complete,
        complete_pct=round(100.0 * complete / rows, 1) if rows else None,
        empty_samples=[short_sample(s) for j, s in enumerate(samples)
                       if not present[j]],
        quantified_samples=quantified,
        value_min=lo,
        value_max=hi,
    )


def read_stats(path: str) -> List[dict]:
    """``report.stats.tsv`` -- DIA-NN's per-run identification and QC summary.

    Small (one row per run), so it is read whole and returned as dicts with the
    run name shortened the same way sample columns are.
    """
    out: List[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        header_line = fh.readline()
        if not header_line:
            return out
        header = header_line.rstrip("\r\n").split("\t")
        for line in fh:
            if not line.strip():
                continue
            parts = line.rstrip("\r\n").split("\t")
            parts += [""] * (len(header) - len(parts))
            row = dict(zip(header, parts[: len(header)]))
            if row.get("File.Name"):
                row["Run"] = short_sample(row["File.Name"])
            out.append(row)
    return out


def stats_overview(rows: Sequence[dict]) -> List[dict]:
    """The three numbers worth showing from ``report.stats.tsv``, per run."""
    keep = ("Run", "Precursors.Identified", "Proteins.Identified",
            "Median.Mass.Acc.MS2", "Average.Missed.Tryptic.Cleavages")
    return [{k: r.get(k, "") for k in keep if k in r or k == "Run"}
            for r in rows]


def render_matrix(info: dict) -> str:
    """One-line-per-fact rendering of a single matrix summary."""
    L = ["%s" % info.get("file", "")]
    if info.get("meaning"):
        L.append("  %-22s %s" % ("contains", info["meaning"]))
    if info.get("tag"):
        L.append("  %-22s %s" % ("normalisation tag", info["tag"]))
    L.append("  %-22s %s" % ("values",
                             "log2" if info.get("log2") else "linear"))
    L.append("  %-22s %s" % ("missing marker", info.get("missing", "?")))
    L.append("  %-22s %s" % ("rows", info.get("rows")))
    L.append("  %-22s %s" % ("samples", info.get("sample_count")))
    if info.get("complete_pct") is not None:
        L.append("  %-22s %s%% (%s rows quantified in every sample)"
                 % ("completeness", info["complete_pct"],
                    info.get("complete_rows")))
    if info.get("value_min") is not None:
        L.append("  %-22s %.4g .. %.4g"
                 % ("value range", info["value_min"], info["value_max"]))
    if info.get("empty_samples"):
        L.append("  %-22s %s" % ("EMPTY samples",
                                 ", ".join(info["empty_samples"])))
    return "\n".join(L)
