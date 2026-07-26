"""Discover and summarize FragPipe output.

Two entry points:

``discover(workdir)``      -- an inventory dict (also embedded in the status file)
``summarize(path)``        -- per-file statistics without loading everything

CLI::

    python scripts/parse_reports.py --workdir I:/test_fragskills/dda
    python scripts/parse_reports.py --workdir I:/test_fragskills/dda --summary
    python scripts/parse_reports.py --file .../psm.tsv --columns
    python scripts/parse_reports.py --file .../psm.tsv --head 5
    python scripts/parse_reports.py --workdir ... --summary --json

Everything streams line by line, so a 35 MB psm.tsv is cheap and there is no
pandas/numpy requirement.  For real analysis, load the TSVs with pandas --
they are plain tab-separated with a single header row and no comment lines.

Output layout FragPipe uses (see references/outputs.md):
  * one unnamed experiment, or spectral-library/DIA-NN enabled -> reports sit in
    the workdir root;
  * named experiments -> reports sit in ``<workdir>/<experiment>/``
    (``<experiment>_<bioreplicate>`` when a bioreplicate is set).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Dict, Iterator, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quant_matrix as qm  # noqa: E402
import run_type  # noqa: E402

#: Per-experiment report files produced by Philosopher / IonQuant.
REPORT_FILES = ("psm.tsv", "peptide.tsv", "modified_peptide.tsv", "ion.tsv",
                "protein.tsv")
#: Multi-experiment summaries, always written to the workdir root.
COMBINED_FILES = ("combined_peptide.tsv", "combined_modified_peptide.tsv",
                  "combined_ion.tsv", "combined_protein.tsv")
#: Site-level matrices IonQuant writes one per localised modification, named
#: after the residue and delta mass: ``combined_site_M_15.9949.tsv``.
COMBINED_SITE_GLOB = "combined_site_*.tsv"
#: Sub-directories holding quantification output for specific workflows.
TMT_DIR = "tmt-report"
DIA_QUANT_DIR = "dia-quant-output"
#: Other artefacts worth pointing at.
AUX_FILES = ("fragpipe.workflow", "fragpipe-files.fp-manifest", "fragger.params",
             "fragger_dia.params", "filter.log", "sdrf.tsv", "library.tsv",
             "combined.prot.xml", "protein.fas", "msbooster_params.txt",
             "experiment_annotation.tsv", "tmt-integrator-conf.yml",
             "msstats.csv", "msstats_ptm.csv", "reprint.int.tsv",
             "reprint.spc.tsv", "fragpipe-report.html",
             "fragskills-qc-report.html")

#: Files big enough that counting their rows is not free.  ``report.tsv`` runs
#: to hundreds of MB on a real DIA study, and its row count (precursors x runs)
#: is not a number anyone acts on, so the summary reports its size instead.
_HUGE_MB = 64

_TRUE = {"true", "1", "yes"}


# --------------------------------------------------------------------------
# low-level TSV helpers
# --------------------------------------------------------------------------
def read_header(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        first = fh.readline()
    return first.rstrip("\r\n").split("\t") if first else []


def iter_rows(path: str) -> Iterator[Dict[str, str]]:
    """Stream a FragPipe TSV as dicts. Ragged rows are padded, not dropped."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        header_line = fh.readline()
        if not header_line:
            return
        header = header_line.rstrip("\r\n").split("\t")
        width = len(header)
        for line in fh:
            if not line.strip():
                continue
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < width:
                parts += [""] * (width - len(parts))
            yield dict(zip(header, parts[:width]))


def count_rows(path: str) -> int:
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.readline()
        for line in fh:
            if line.strip():
                n += 1
    return n


def _is_true(value: str) -> bool:
    return (value or "").strip().lower() in _TRUE


def _fnum(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def discover(workdir: str) -> dict:
    """Inventory the outputs of a finished (or partial) FragPipe run."""
    workdir = os.path.abspath(workdir)
    inv = {
        "workdir": workdir,
        "experiments": {},
        "report_files": {},
        "combined_files": {},
        "combined_site_files": {},
        "tmt_report": {},
        "dia_quant": {},
        "aux_files": {},
        "logs": [],
        "notes": [],
    }
    if not os.path.isdir(workdir):
        inv["notes"].append("workdir does not exist")
        inv["classification"] = run_type.classify(inv)
        return inv

    # Root-level reports = the single unnamed experiment (or speclib/DIA-NN run).
    root_reports = {name: os.path.join(workdir, name)
                    for name in REPORT_FILES
                    if os.path.isfile(os.path.join(workdir, name))}
    if root_reports:
        inv["experiments"][""] = root_reports
        inv["report_files"].update(root_reports)

    # Per-experiment subfolders.
    for entry in sorted(os.listdir(workdir)):
        sub = os.path.join(workdir, entry)
        if not os.path.isdir(sub) or entry in (TMT_DIR, DIA_QUANT_DIR):
            continue
        found = {name: os.path.join(sub, name)
                 for name in REPORT_FILES
                 if os.path.isfile(os.path.join(sub, name))}
        if found:
            inv["experiments"][entry] = found
            for k, v in found.items():
                inv["report_files"]["%s/%s" % (entry, k)] = v

    for name in COMBINED_FILES:
        p = os.path.join(workdir, name)
        if os.path.isfile(p):
            inv["combined_files"][name] = p

    for p in sorted(glob.glob(os.path.join(workdir, COMBINED_SITE_GLOB))):
        inv["combined_site_files"][os.path.basename(p)] = p

    tmt_dir = os.path.join(workdir, TMT_DIR)
    if os.path.isdir(tmt_dir):
        inv["tmt_report"] = {f: os.path.join(tmt_dir, f)
                             for f in sorted(os.listdir(tmt_dir))
                             if os.path.isfile(os.path.join(tmt_dir, f))}

    dia_dir = os.path.join(workdir, DIA_QUANT_DIR)
    if os.path.isdir(dia_dir):
        inv["dia_quant"] = {f: os.path.join(dia_dir, f)
                            for f in sorted(os.listdir(dia_dir))
                            if os.path.isfile(os.path.join(dia_dir, f))}

    for name in AUX_FILES:
        p = os.path.join(workdir, name)
        if os.path.isfile(p):
            inv["aux_files"][name] = p

    inv["logs"] = sorted(
        os.path.join(workdir, f) for f in os.listdir(workdir)
        if (f.startswith("log_") and f.endswith(".txt"))
        or f == "fragskills_run.log")

    if not inv["experiments"] and not inv["tmt_report"] and not inv["dia_quant"]:
        inv["notes"].append(
            "No psm.tsv/protein.tsv found. Either the run has not reached the "
            "report stage, or 'Generate report' (phi-report.run-report) was off.")
    # Derived, not discovered: which of the above is *the* result. Kept on the
    # inventory so the status file and every caller see the same verdict.
    inv["classification"] = run_type.classify(inv)
    return inv


# --------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------
def summarize(path: str) -> dict:
    """Statistics for a single report file, chosen by its base name."""
    base = os.path.basename(path).lower()
    if qm.is_matrix(path):
        return dict(qm.summarize_matrix(path), size_bytes=os.path.getsize(path),
                    columns=read_header(path))
    info = {"file": path, "rows": 0, "size_bytes": os.path.getsize(path),
            "columns": read_header(path)}
    if base == "psm.tsv":
        info.update(_summarize_psm(path))
    elif base in ("peptide.tsv", "modified_peptide.tsv"):
        info.update(_summarize_peptide(path))
    elif base == "ion.tsv":
        info.update(_summarize_ion(path))
    elif base == "protein.tsv":
        info.update(_summarize_protein(path))
    elif base.startswith("combined_"):
        info.update(_summarize_combined(path))
    else:
        info["rows"] = count_rows(path)
    return info


def _uniq(container: set) -> int:
    container.discard("")
    return len(container)


def _summarize_psm(path: str) -> dict:
    peptides, mod_peptides, proteins, genes, spectra_files = set(), set(), set(), set(), set()
    charges: Dict[str, int] = {}
    rows = decoys = contaminants = unique_psms = 0
    modified = 0
    intensities = 0
    for r in iter_rows(path):
        rows += 1
        peptides.add(r.get("Peptide", ""))
        mod_peptides.add(r.get("Modified Peptide", "") or r.get("Peptide", ""))
        proteins.add(r.get("Protein ID", "") or r.get("Protein", ""))
        genes.add(r.get("Gene", ""))
        sf = r.get("Spectrum File", "")
        if sf:
            spectra_files.add(sf)
        c = r.get("Charge", "")
        if c:
            charges[c] = charges.get(c, 0) + 1
        if _is_true(r.get("Is Decoy", "")):
            decoys += 1
        if _is_true(r.get("Is Contaminant", "")):
            contaminants += 1
        if _is_true(r.get("Is Unique", "")):
            unique_psms += 1
        if (r.get("Assigned Modifications", "") or "").strip():
            modified += 1
        iv = _fnum(r.get("Intensity", ""))
        if iv:
            intensities += 1
    return {
        "rows": rows,
        "psms": rows,
        "unique_peptides": _uniq(peptides),
        "unique_modified_peptides": _uniq(mod_peptides),
        "unique_protein_ids": _uniq(proteins),
        "unique_genes": _uniq(genes),
        "spectrum_files": sorted(spectra_files),
        "charge_distribution": dict(sorted(charges.items())),
        "decoy_psms": decoys,
        "contaminant_psms": contaminants,
        "unique_psms": unique_psms,
        "psms_with_modifications": modified,
        "psms_with_nonzero_intensity": intensities,
    }


def _summarize_peptide(path: str) -> dict:
    proteins, genes = set(), set()
    rows = decoys = contaminants = 0
    spectral_counts = 0
    for r in iter_rows(path):
        rows += 1
        proteins.add(r.get("Protein ID", "") or r.get("Protein", ""))
        genes.add(r.get("Gene", ""))
        if _is_true(r.get("Is Decoy", "")):
            decoys += 1
        if _is_true(r.get("Is Contaminant", "")):
            contaminants += 1
        sc = _fnum(r.get("Spectral Count", ""))
        if sc:
            spectral_counts += int(sc)
    return {"rows": rows, "peptides": rows,
            "unique_protein_ids": _uniq(proteins), "unique_genes": _uniq(genes),
            "decoy_peptides": decoys, "contaminant_peptides": contaminants,
            "total_spectral_count": spectral_counts}


def _summarize_ion(path: str) -> dict:
    peptides, proteins, genes = set(), set(), set()
    charges: Dict[str, int] = {}
    rows = decoys = contaminants = 0
    for r in iter_rows(path):
        rows += 1
        peptides.add(r.get("Peptide Sequence", ""))
        proteins.add(r.get("Protein ID", "") or r.get("Protein", ""))
        genes.add(r.get("Gene", ""))
        c = r.get("Charge", "")
        if c:
            charges[c] = charges.get(c, 0) + 1
        if _is_true(r.get("Is Decoy", "")):
            decoys += 1
        if _is_true(r.get("Is Contaminant", "")):
            contaminants += 1
    return {"rows": rows, "ions": rows,
            "unique_peptide_sequences": _uniq(peptides),
            "unique_protein_ids": _uniq(proteins), "unique_genes": _uniq(genes),
            "charge_distribution": dict(sorted(charges.items())),
            "decoy_ions": decoys, "contaminant_ions": contaminants}


def _summarize_protein(path: str) -> dict:
    genes, organisms = set(), set()
    rows = decoys = contaminants = 0
    coverage_sum = coverage_n = 0.0
    with_unique = 0
    total_unique_peptides = 0
    for r in iter_rows(path):
        rows += 1
        genes.add(r.get("Gene", ""))
        org = r.get("Organism", "")
        if org:
            organisms.add(org)
        if _is_true(r.get("Is Decoy", "")):
            decoys += 1
        if _is_true(r.get("Is Contaminant", "")):
            contaminants += 1
        cv = _fnum(r.get("Coverage", ""))
        if cv is not None:
            coverage_sum += cv
            coverage_n += 1
        up = _fnum(r.get("Unique Peptides", ""))
        if up:
            with_unique += 1
            total_unique_peptides += int(up)
    return {"rows": rows, "protein_groups": rows,
            "unique_genes": _uniq(genes), "organisms": sorted(organisms)[:10],
            "decoy_proteins": decoys, "contaminant_proteins": contaminants,
            "mean_coverage_pct": round(coverage_sum / coverage_n, 2) if coverage_n else None,
            "proteins_with_unique_peptides": with_unique,
            "total_unique_peptides": total_unique_peptides}


#: Suffixes IonQuant/Philosopher append to per-sample quant columns.  Matched
#: longest first, so ``x MaxLFQ Intensity`` yields the sample ``x`` and not
#: ``x MaxLFQ``.
_QUANT_SUFFIXES = tuple(sorted((
    " Intensity", " MaxLFQ Intensity", " Spectral Count",
    " Unique Spectral Count", " Total Spectral Count",
    " Razor Spectral Count", " Unique Intensity",
    " Total Intensity", " Razor Intensity",
    " MaxLFQ Unique Intensity", " MaxLFQ Total Intensity",
    " MaxLFQ Razor Intensity", " Match Type",
    # ion-level columns
    " Apex Retention Time", " Apex Scan Number", " Retention Time Start",
    " Retention Time End", " Retention Time FWHM", " Traced Scans",
    " Localization",
    # combined_site_*.tsv
    " Localization Probability",
), key=len, reverse=True))


#: These look exactly like per-sample columns but hold an across-experiment
#: aggregate: ``Combined Spectral Count`` is the total, ``Best Localization
#: Probability`` (combined_site_*.tsv) the maximum. Neither prefix is a sample.
_NOT_A_SAMPLE = frozenset(("Combined", "Best"))


def _summarize_combined(path: str) -> dict:
    header = read_header(path)
    samples, measures = set(), set()
    for col in header:
        for suf in _QUANT_SUFFIXES:
            if col.endswith(suf) and len(col) > len(suf):
                name = col[: -len(suf)]
                if name not in _NOT_A_SAMPLE:
                    samples.add(name)
                    measures.add(suf.strip())
                break
    return {"rows": count_rows(path),
            "samples": sorted(samples),
            "sample_count": len(samples),
            "quant_measures": sorted(measures)}


def _summarize_quant_dir(files: Dict[str, str]) -> dict:
    """Summarize one of the quant folders, matrix-aware and size-aware."""
    out: Dict[str, dict] = {}
    for name, p in sorted(files.items()):
        if not name.lower().endswith((".tsv", ".txt")):
            continue
        if qm.is_matrix(p):
            out[name] = qm.summarize_matrix(p)
        elif os.path.getsize(p) > _HUGE_MB * 1024 * 1024:
            out[name] = {"file": p, "rows": None,
                         "size_mb": round(os.path.getsize(p) / 1048576.0, 1),
                         "columns": read_header(p),
                         "note": "not scanned - over %d MB" % _HUGE_MB}
        else:
            out[name] = {"file": p, "rows": count_rows(p),
                         "columns": read_header(p)}
    return out


def summarize_workdir(workdir: str) -> dict:
    inv = discover(workdir)
    out = {"workdir": inv["workdir"], "notes": inv["notes"],
           "classification": inv["classification"],
           "experiments": {}, "combined": {}, "tmt_report": {},
           "dia_quant": {}, "dia_run_stats": []}
    for exp, files in inv["experiments"].items():
        out["experiments"][exp or "(root)"] = {
            name: summarize(p) for name, p in sorted(files.items())}
    for name, p in sorted(inv["combined_files"].items()):
        out["combined"][name] = summarize(p)
    for name, p in sorted(inv["combined_site_files"].items()):
        out["combined"][name] = summarize(p)
    out["tmt_report"] = _summarize_quant_dir(inv["tmt_report"])
    out["dia_quant"] = _summarize_quant_dir(inv["dia_quant"])

    stats = inv["dia_quant"].get(qm.DIANN_STATS)
    if stats:
        out["dia_run_stats"] = qm.stats_overview(qm.read_stats(stats))
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_inventory(inv: dict) -> str:
    L = ["FragPipe output: %s" % inv["workdir"], ""]
    if not os.path.isdir(inv["workdir"]):
        L.append("ERROR: this directory does not exist.")
        return "\n".join(L)
    L.append(run_type.render(inv["classification"]))
    L.append("")
    L.append("-" * 70)
    L.append("")
    if inv["experiments"]:
        L.append("Experiments (%d):" % len(inv["experiments"]))
        for exp, files in inv["experiments"].items():
            L.append("  %-24s %s" % (exp or "(root / single experiment)",
                                     ", ".join(sorted(files))))
    if inv["combined_files"] or inv["combined_site_files"]:
        L.append("")
        L.append("Combined (multi-experiment) reports:")
        for n, p in sorted(inv["combined_files"].items()):
            L.append("  %s" % p)
        for n, p in sorted(inv["combined_site_files"].items()):
            L.append("  %s" % p)
    if inv["tmt_report"]:
        L.append("")
        L.append("TMT report (%d files): %s" % (len(inv["tmt_report"]),
                 os.path.dirname(next(iter(inv["tmt_report"].values())))))
        for n in sorted(inv["tmt_report"])[:20]:
            L.append("  %s" % n)
    if inv["dia_quant"]:
        L.append("")
        L.append("DIA quant output (%d files): %s" % (len(inv["dia_quant"]),
                 os.path.dirname(next(iter(inv["dia_quant"].values())))))
        for n in sorted(inv["dia_quant"])[:20]:
            L.append("  %s" % n)
    if inv["aux_files"]:
        L.append("")
        L.append("Other: %s" % ", ".join(sorted(inv["aux_files"])))
    if inv["logs"]:
        L.append("Logs : %s" % ", ".join(os.path.basename(x) for x in inv["logs"]))
    for note in inv["notes"]:
        L.append("")
        L.append("NOTE: %s" % note)
    return "\n".join(L)


_HIDE = {"file", "columns", "size_bytes", "spectrum_files", "organisms", "samples"}


def render_summary(summary: dict) -> str:
    L = ["FragPipe results: %s" % summary["workdir"], ""]
    if summary.get("classification"):
        L.append(run_type.render(summary["classification"]))
    for exp, files in summary["experiments"].items():
        L.append("")
        L.append("=== experiment: %s ===" % exp)
        for name, info in files.items():
            L.append("  %s  (%s rows)" % (name, info.get("rows")))
            for k, v in info.items():
                if k in _HIDE or k == "rows" or v in (None, 0, {}, []):
                    continue
                L.append("      %-28s %s" % (k, v))
            if info.get("spectrum_files"):
                L.append("      %-28s %d" % ("spectrum_files",
                                             len(info["spectrum_files"])))
    for name, info in summary["combined"].items():
        L.append("")
        L.append("=== %s ===" % name)
        L.append("  rows: %s, samples: %s" % (info.get("rows"),
                                              info.get("sample_count")))
        if info.get("samples"):
            L.append("  sample names: %s" % ", ".join(info["samples"]))
        if info.get("quant_measures"):
            L.append("  measures: %s" % ", ".join(info["quant_measures"]))
    for section, label in (("tmt_report", "TMT report"),
                           ("dia_quant", "DIA quant output")):
        if summary.get(section):
            L.append("")
            L.append("=== %s ===" % label)
            width = max(len(n) for n in summary[section])
            for name, info in summary[section].items():
                if info.get("sample_count"):
                    L.append("  %-*s %7s rows x %2d samples   %s values, "
                             "%s%% complete"
                             % (width, name, info["rows"], info["sample_count"],
                                "log2" if info.get("log2") else "linear",
                                info.get("complete_pct")))
                    if info.get("empty_samples"):
                        L.append("  %-*s   EMPTY in every row: %s"
                                 % (width, "", ", ".join(info["empty_samples"])))
                elif info.get("rows") is None:
                    L.append("  %-*s %s" % (width, name,
                                            info.get("note", "not scanned")))
                else:
                    L.append("  %-*s %7s rows, %d columns"
                             % (width, name, info["rows"],
                                len(info.get("columns") or [])))

    if summary.get("dia_run_stats"):
        L.append("")
        L.append("=== per-run identifications (report.stats.tsv) ===")
        for r in summary["dia_run_stats"]:
            L.append("  %-40s %8s precursors, %6s protein groups"
                     % (r.get("Run", "?"), r.get("Precursors.Identified", "?"),
                        r.get("Proteins.Identified", "?")))
    for note in summary.get("notes") or []:
        L.append("")
        L.append("NOTE: %s" % note)
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Discover and summarize FragPipe output.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--workdir", help="FragPipe output directory")
    src.add_argument("--file", help="a single report TSV")
    ap.add_argument("--summary", action="store_true",
                    help="compute per-file statistics (reads the files)")
    ap.add_argument("--columns", action="store_true", help="list column names")
    ap.add_argument("--head", type=int, metavar="N",
                    help="print the first N data rows as key: value blocks")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if args.file:
        path = os.path.abspath(args.file)
        if not os.path.isfile(path):
            print("not a file: %s" % path, file=sys.stderr)
            return 2
        if args.columns:
            cols = read_header(path)
            if args.json:
                json.dump(cols, sys.stdout, indent=2)
                sys.stdout.write("\n")
            else:
                print("%d columns in %s" % (len(cols), os.path.basename(path)))
                for i, c in enumerate(cols):
                    print("  [%2d] %s" % (i, c))
            return 0
        if args.head:
            rows = []
            for i, r in enumerate(iter_rows(path)):
                if i >= args.head:
                    break
                rows.append(r)
            if args.json:
                json.dump(rows, sys.stdout, indent=2)
                sys.stdout.write("\n")
            else:
                for i, r in enumerate(rows):
                    print("--- row %d ---" % (i + 1))
                    for k, v in r.items():
                        print("  %-30s %s" % (k, v))
            return 0
        info = summarize(path)
        if args.json:
            json.dump(info, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print("%s" % path)
            for k, v in info.items():
                if k == "columns":
                    print("  %-28s %d" % ("column_count", len(v)))
                    continue
                print("  %-28s %s" % (k, v))
        return 0

    workdir = os.path.abspath(args.workdir)
    if args.summary:
        result = summarize_workdir(workdir)
        print(json.dumps(result, indent=2) if args.json else render_summary(result))
    else:
        inv = discover(workdir)
        print(json.dumps(inv, indent=2) if args.json else render_inventory(inv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
