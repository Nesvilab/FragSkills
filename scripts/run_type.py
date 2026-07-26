"""Decide what kind of run produced a work directory, and which files answer it.

A finished FragPipe run leaves several plausible-looking result tables side by
side, and the right one depends on the acquisition and on whether experiments
were named.  A TMT run writes ``combined_protein.tsv`` **and** ``tmt-report/``;
only the latter carries the reporter-ion quantities.  A DIA run writes
``psm.tsv`` and ``protein.tsv`` at the root, but those describe the *library*,
not the samples.  Picking the wrong one is not an error anyone notices --- the
file opens, the numbers look fine, and they answer a different question.

So the precedence is fixed, most specific first:

    tmt-report/          -> TMT (site-level tables present => PTM run)
    dia-quant-output/    -> DIA
    combined_*.tsv       -> DDA with named experiments
    root reports         -> DDA, single/unnamed experiment

``classify`` takes the inventory dict from ``parse_reports.discover`` and never
mutates it.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quant_matrix as qm  # noqa: E402

RUN_TMT_PTM = "tmt-ptm"
RUN_TMT = "tmt"
RUN_DIA = "dia"
RUN_DDA_GROUPED = "dda-grouped"
RUN_DDA_SINGLE = "dda-single"
RUN_UNKNOWN = "unknown"

LABELS = {
    RUN_TMT_PTM: "TMT / isobaric labelling, site-level (PTM enrichment)",
    RUN_TMT: "TMT / isobaric labelling",
    RUN_DIA: "DIA (DIA-NN quantification)",
    RUN_DDA_GROUPED: "DDA label-free, experiments named",
    RUN_DDA_SINGLE: "DDA label-free, single or unnamed experiment",
    RUN_UNKNOWN: "unrecognised - no result tables found",
}

#: Order to report the plain per-experiment reports in: the summarised levels
#: first, the raw spectrum table last.
_ROOT_ORDER = ("protein.tsv", "peptide.tsv", "modified_peptide.tsv",
               "ion.tsv", "psm.tsv")
_ROOT_NOTES = {
    "protein.tsv": "protein groups, with coverage and per-group quant",
    "peptide.tsv": "one row per peptide sequence",
    "modified_peptide.tsv": "one row per modified peptide form (PTM counting)",
    "ion.tsv": "one row per precursor ion - the IonQuant unit",
    "psm.tsv": "one row per PSM - spectrum-level evidence, largest file",
}
_COMBINED_ORDER = ("combined_protein.tsv", "combined_peptide.tsv",
                   "combined_modified_peptide.tsv", "combined_ion.tsv")
_COMBINED_NOTES = {
    "combined_protein.tsv": "protein x sample matrix - the usual starting point",
    "combined_peptide.tsv": "peptide x sample matrix",
    "combined_modified_peptide.tsv": "modified-peptide x sample matrix (PTM)",
    "combined_ion.tsv": "precursor-ion x sample matrix, most granular",
}
#: DIA-NN's own outputs, after FragPipe's abundance_* tables.
_DIANN_ORDER = ("report.pg_matrix.tsv", "report.gg_matrix.tsv",
                "report.unique_genes_matrix.tsv", "report.pr_matrix.tsv")


def _entry(path: str, note: str, role: str = "main") -> dict:
    return {"name": os.path.basename(path), "path": path, "note": note,
            "role": role}


# --------------------------------------------------------------------------
# per-type file selection
# --------------------------------------------------------------------------
def _tmt_main(tmt_files: Dict[str, str], ptm: bool,
              multi_plex: bool) -> List[dict]:
    """Rank tmt-report/ by level, abundance and ratio paired at each level."""
    described = []
    for name, path in tmt_files.items():
        d = qm.describe_matrix(path)
        if d:
            described.append((d, path))
    # With one plex the abundances are directly comparable and are the simpler
    # table; with several, only the reference-channel ratios are, so those lead.
    preferred = qm.KIND_RATIO if multi_plex else qm.KIND_ABUNDANCE
    described.sort(key=lambda dp: (qm.level_rank(dp[0]["level"], ptm),
                                   0 if dp[0]["kind"] == preferred else 1))
    out = []
    for d, path in described:
        if d["kind"] == qm.KIND_ABUNDANCE:
            note = "log2 abundance, %s level" % d["level"]
        else:
            note = ("log2 ratio vs the reference channel, %s level%s"
                    % (d["level"],
                       " - use this to compare across plexes" if multi_plex
                       else ""))
        out.append(_entry(path, note))
    return out


def _dia_main(dia_files: Dict[str, str]) -> List[dict]:
    """FragPipe's abundance_* tables first, then DIA-NN's own matrices."""
    described = []
    for name, path in dia_files.items():
        d = qm.describe_matrix(path)
        if d and d["producer"] == qm.PRODUCER_DIA:
            described.append((d, path))
    described.sort(key=lambda dp: (qm.level_rank(dp[0]["level"]),
                                   dp[0]["kind"]))
    out = [_entry(p, "MaxLFQ intensity, %s level" % d["level"])
           for d, p in described]

    for name in _DIANN_ORDER:
        if name in dia_files:
            level, meaning = qm.DIANN_MATRICES[name]
            out.append(_entry(dia_files[name],
                              "DIA-NN's own %s matrix - %s" % (level, meaning),
                              role="also"))
    if qm.DIANN_STATS in dia_files:
        out.append(_entry(dia_files[qm.DIANN_STATS],
                          "per-run QC: precursors and proteins identified",
                          role="also"))
    if qm.DIANN_LONG_REPORT in dia_files:
        out.append(_entry(dia_files[qm.DIANN_LONG_REPORT],
                          "long format, one row per precursor per run - the "
                          "source of every matrix above (large)", role="also"))
    return out


def _ordered(files: Dict[str, str], order, notes) -> List[dict]:
    out = [_entry(files[n], notes.get(n, "")) for n in order if n in files]
    out += [_entry(p, "") for n, p in sorted(files.items()) if n not in order]
    return out


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
def classify(inv: dict) -> dict:
    """Which analysis this was, and which files hold its result."""
    tmt = dict(inv.get("tmt_report") or {})
    dia = dict(inv.get("dia_quant") or {})
    combined = dict(inv.get("combined_files") or {})
    site = dict(inv.get("combined_site_files") or {})
    experiments = dict(inv.get("experiments") or {})
    n_exp = len(experiments)

    avoid: List[dict] = []
    notes: List[str] = []

    if tmt:
        ptm = any(lv in n for n in tmt for lv in ("single-site", "multi-site"))
        run_type = RUN_TMT_PTM if ptm else RUN_TMT
        main = _tmt_main(tmt, ptm, multi_plex=n_exp > 1)
        main_dir = os.path.dirname(next(iter(tmt.values()))) if tmt else None
        reason = ("tmt-report/ is present with %d files%s"
                  % (len(tmt), " including site-level tables" if ptm else ""))
        if combined:
            avoid.append({
                "what": "combined_*.tsv",
                "why": "MS1 and spectral-count quant, not reporter ions. Useful "
                       "for identification counts; not the TMT quantities."})
        avoid.append({
            "what": "psm.tsv channel columns",
            "why": "raw, unnormalised reporter intensities. TMT-Integrator "
                   "normalises and rolls these up; use its output instead."})
        if n_exp > 1:
            notes.append("%d plexes. Values are comparable across plexes only "
                         "through the reference channel, so prefer ratio_* "
                         "unless a single plex is being analysed." % n_exp)

    elif dia:
        run_type = RUN_DIA
        main = _dia_main(dia)
        main_dir = os.path.dirname(next(iter(dia.values())))
        reason = "dia-quant-output/ is present with %d files" % len(dia)
        if inv.get("report_files"):
            avoid.append({
                "what": "psm.tsv / peptide.tsv / ion.tsv / protein.tsv at the "
                        "workdir root",
                "why": "these describe the spectral LIBRARY built from the DIA "
                       "data, not the samples. Quantification is DIA-NN's."})
        notes.append("FragPipe's abundance_* tables and DIA-NN's report.*_matrix "
                     "tables carry the same MaxLFQ quantities; the abundance_* "
                     "ones add protein annotation and fill slightly fewer cells.")

    elif combined or site:
        run_type = RUN_DDA_GROUPED
        main = _ordered(combined, _COMBINED_ORDER, _COMBINED_NOTES)
        for name in sorted(site):
            main.append(_entry(site[name],
                               "per-site localisation and intensity (%s)"
                               % _site_label(name)))
        main_dir = inv.get("workdir")
        reason = ("%d experiments with combined_*.tsv cross-experiment matrices"
                  % n_exp)
        avoid.append({
            "what": "the per-experiment psm.tsv / protein.tsv folders",
            "why": "correct, but one experiment at a time. The combined_* "
                   "matrices already align every experiment on one row set."})
        notes.append("For label-free differential analysis use the "
                     "'<sample> MaxLFQ Intensity' columns where present, and "
                     "treat 0 as missing rather than as a measured zero.")

    elif experiments:
        run_type = RUN_DDA_SINGLE
        only = experiments.get("") or next(iter(experiments.values()))
        main = _ordered(only, _ROOT_ORDER, _ROOT_NOTES)
        main_dir = os.path.dirname(next(iter(only.values()))) if only else None
        reason = ("a single set of reports and no combined_*.tsv - the manifest "
                  "gave no experiment names, so every input was pooled into one "
                  "group")
        if n_exp > 1:
            notes.append(
                "%d experiment folders but no combined_*.tsv. Enable IonQuant, "
                "or phi-report.pep-level-summary / prot-level-summary, to get "
                "cross-experiment matrices." % n_exp)
        else:
            notes.append("All input runs were pooled, so these tables do not "
                         "separate the samples. Name experiments in the "
                         "manifest to get per-sample columns.")

    else:
        return {"run_type": RUN_UNKNOWN, "label": LABELS[RUN_UNKNOWN],
                "ptm": False, "main_dir": None, "main_files": [],
                "avoid": [], "notes": [], "n_experiments": 0,
                "reason": "no psm.tsv, combined_*.tsv, tmt-report/ or "
                          "dia-quant-output/ found"}

    return {
        "run_type": run_type,
        "label": LABELS[run_type],
        "ptm": run_type == RUN_TMT_PTM,
        "n_experiments": n_exp,
        "main_dir": main_dir,
        "main_files": main,
        "avoid": avoid,
        "notes": notes,
        "reason": reason,
    }


def _site_label(name: str) -> str:
    """``combined_site_M_15.9949.tsv`` -> ``M +15.9949``."""
    stem = name[len("combined_site_"):-len(".tsv")] if name.startswith(
        "combined_site_") else name
    parts = stem.split("_", 1)
    if len(parts) == 2:
        return "%s +%s" % (parts[0], parts[1])
    return stem


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render(cls: dict) -> str:
    """The block that tells the reader which file to open first."""
    L = ["Run type    : %s" % cls["label"],
         "Detected by : %s" % cls["reason"]]
    if cls["run_type"] == RUN_UNKNOWN:
        return "\n".join(L)

    main = [f for f in cls["main_files"] if f["role"] == "main"]
    also = [f for f in cls["main_files"] if f["role"] != "main"]
    if main:
        L.append("")
        L.append("MAIN RESULT -> %s" % (cls["main_dir"] or ""))
        width = max(len(f["name"]) for f in main)
        for i, f in enumerate(main):
            L.append("  %s %-*s  %s" % ("*" if i == 0 else " ", width,
                                        f["name"], f["note"]))
        L.append("  (* start here)")
    if also:
        L.append("")
        L.append("Also in that folder:")
        width = max(len(f["name"]) for f in also)
        for f in also:
            L.append("    %-*s  %s" % (width, f["name"], f["note"]))
    for a in cls["avoid"]:
        L.append("")
        L.append("NOT the main result: %s" % a["what"])
        L.append("    %s" % a["why"])
    for n in cls["notes"]:
        L.append("")
        L.append("NOTE: %s" % n)
    return "\n".join(L)
