"""Regression self-test for FragSkills, driven by real FragPipe runs.

Point it at one or more finished FragPipe output directories, each containing a
``log_*.txt`` and the report TSVs::

    python scripts/selftest.py --example I:/test_fragskill/dda
    python scripts/selftest.py --example I:/test_fragskill/*/template

It checks the log parser (full and mid-run), the success/failure decision, the
output discovery, the run-type classification, the report summaries, the QC plot
series, and the summary report generator.  The classification precedence and the
quant-matrix reader are additionally tested against synthetic trees, so those
run with no example at all.

Give it one directory per analysis type -- DDA with named experiments, DDA with
none, DIA, TMT, TMT with PTM enrichment -- to cover every output layout.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fragpipe_progress import (  # noqa: E402
    RunProgress, parse_log, STATE_FAILED, STATE_RUNNING, STATE_SUCCEEDED,
)
import parse_reports  # noqa: E402
import quant_matrix as qm  # noqa: E402
import run_type  # noqa: E402

_failures = []
_checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s%s" % (label, (" -- " + detail) if detail else ""))
        _failures.append(label)


def find_log(workdir: str) -> str:
    hits = sorted(glob.glob(os.path.join(workdir, "log_*.txt")))
    if not hits:
        hits = [p for p in [os.path.join(workdir, "fragskills_run.log")]
                if os.path.isfile(p)]
    if not hits:
        raise SystemExit("no FragPipe log found in %s" % workdir)
    return hits[-1]


def test_full_log(log_path: str) -> RunProgress:
    print("\n[1] full log parse: %s" % os.path.basename(log_path))
    p = parse_log(log_path)
    check("run is recognised as succeeded", p.state == STATE_SUCCEEDED, p.state)
    check("total task count was parsed", bool(p.total_tasks), str(p.total_tasks))
    check("every declared task finished",
          p.total_tasks is not None and p.finished_tasks == p.total_tasks,
          "%s of %s" % (p.finished_tasks, p.total_tasks))
    check("plan length matches declared total",
          len(p.plan) == (p.total_tasks or -1),
          "%d vs %s" % (len(p.plan), p.total_tasks))
    check("progress reaches 100%", p.percent == 100.0, str(p.percent))
    check("total runtime captured", p.total_minutes is not None)
    check("task runtimes captured", len(p.task_runtimes) > 0,
          "%d entries" % len(p.task_runtimes))
    check("FragPipe version captured", bool(p.fragpipe_version),
          str(p.fragpipe_version))
    check("input files captured", len(p.input_files) > 0,
          "%d files" % len(p.input_files))
    check("no error recorded on a good run", p.error is None, str(p.error))
    return p


def test_midrun(log_path: str, full: RunProgress) -> None:
    print("\n[2] mid-run parsing (truncated logs)")
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    total_lines = len(lines)
    seen_percent = []
    for frac in (0.25, 0.5, 0.75):
        cut = int(total_lines * frac)
        p = RunProgress()
        for line in lines[:cut]:
            p.feed(line)
        seen_percent.append(p.percent or 0.0)
        check("at %d%% of the log the state is running" % int(frac * 100),
              p.state == STATE_RUNNING, p.state)
        check("at %d%% of the log the total is known" % int(frac * 100),
              p.total_tasks == full.total_tasks,
              "%s vs %s" % (p.total_tasks, full.total_tasks))
        check("at %d%% of the log a current task is reported" % int(frac * 100),
              bool(p.current_task), str(p.current_task))
    check("progress increases monotonically",
          seen_percent == sorted(seen_percent), str(seen_percent))

    print("\n[3] failure detection")
    # A run that stops without ALL JOBS DONE must not be reported as success.
    p = RunProgress()
    for line in lines[: int(total_lines * 0.5)]:
        p.feed(line)
    check("truncated run is not marked succeeded", p.state != STATE_SUCCEEDED,
          p.state)
    # An explicit abort marker must flip the state to failed.
    p2 = RunProgress()
    for line in lines[: int(total_lines * 0.5)]:
        p2.feed(line)
    p2.feed("Process 'MSBooster' finished, exit code: 137\n")
    p2.feed("Process returned non-zero exit code, stopping\n")
    check("abort marker is detected", p2.state == STATE_FAILED, p2.state)
    check("failing task is identified", p2.failed_task == "MSBooster",
          str(p2.failed_task))
    # A non-fatal warning must NOT fail the run.
    p3 = RunProgress()
    for line in lines:
        p3.feed(line)
    p3.feed("WARNING: Process 'X' returned non-zero exit code (1), "
            "but is non-fatal. Continuing.\n")
    check("non-fatal warning does not fail the run",
          p3.state == STATE_SUCCEEDED, p3.state)
    check("non-fatal warning is recorded", len(p3.warnings) == 1)


def test_outputs(workdir: str) -> None:
    print("\n[4] output discovery")
    inv = parse_reports.discover(workdir)
    check("at least one experiment was found", len(inv["experiments"]) > 0)
    check("psm.tsv located", any("psm.tsv" in f for f in inv["report_files"]))
    check("protein.tsv located",
          any("protein.tsv" in f for f in inv["report_files"]))
    check("log located", len(inv["logs"]) > 0)

    print("\n[5] report summaries")
    for exp, files in inv["experiments"].items():
        for name in ("psm.tsv", "peptide.tsv", "ion.tsv", "protein.tsv"):
            path = files.get(name)
            if not path:
                continue
            info = parse_reports.summarize(path)
            check("%s has rows" % name, info["rows"] > 0, str(info["rows"]))
            check("%s has columns" % name, len(info["columns"]) > 0)
        psm, prot = files.get("psm.tsv"), files.get("protein.tsv")
        pep = files.get("peptide.tsv")
        if psm and pep and prot:
            s_psm = parse_reports.summarize(psm)
            s_pep = parse_reports.summarize(pep)
            s_prot = parse_reports.summarize(prot)
            check("PSMs >= peptides", s_psm["psms"] >= s_pep["peptides"],
                  "%s vs %s" % (s_psm["psms"], s_pep["peptides"]))
            check("peptides >= protein groups",
                  s_pep["peptides"] >= s_prot["protein_groups"],
                  "%s vs %s" % (s_pep["peptides"], s_prot["protein_groups"]))
            # protein.tsv may hold MORE groups than psm.tsv names as leads:
            # a group whose evidence is razor/shared peptides is reported even
            # though each of those PSMs names a different lead accession. So
            # containment is the invariant here, not equality.
            psm_ids = {r.get("Protein ID", "")
                       for r in parse_reports.iter_rows(psm)} - {""}
            prot_ids = {r.get("Protein ID", "")
                        for r in parse_reports.iter_rows(prot)} - {""}
            check("every protein ID in psm.tsv is reported in protein.tsv",
                  psm_ids <= prot_ids,
                  "%d missing, e.g. %s" % (len(psm_ids - prot_ids),
                                           sorted(psm_ids - prot_ids)[:3]))
            check("protein groups >= distinct PSM protein IDs",
                  s_prot["protein_groups"] >= s_psm["unique_protein_ids"],
                  "%s vs %s" % (s_prot["protein_groups"],
                                s_psm["unique_protein_ids"]))
        break  # one experiment is enough for a smoke test


def test_run_type(workdir: str) -> None:
    """Invariants the classification must satisfy for any real output folder."""
    print("\n[5b] run-type classification")
    inv = parse_reports.discover(workdir)
    cls = inv["classification"]
    rt = cls["run_type"]
    check("run type is a known value",
          rt in run_type.LABELS, rt)
    check("classification is not 'unknown'", rt != run_type.RUN_UNKNOWN, rt)
    check("a main result was chosen", len(cls["main_files"]) > 0)
    check("every main-result file exists",
          all(os.path.isfile(f["path"]) for f in cls["main_files"]),
          str([f["name"] for f in cls["main_files"]
               if not os.path.isfile(f["path"])]))
    check("the headline file is a 'main' one, not an aside",
          cls["main_files"][0]["role"] == "main")
    print("        -> %s: %s" % (rt, cls["main_files"][0]["name"]))

    if rt in (run_type.RUN_TMT, run_type.RUN_TMT_PTM):
        check("TMT main results live in tmt-report/",
              all(os.path.basename(os.path.dirname(f["path"])) == "tmt-report"
                  for f in cls["main_files"]))
        check("TMT results are all abundance/ratio matrices",
              all(qm.is_matrix(f["path"]) for f in cls["main_files"]))
        ptm = any("site" in f["name"] for f in cls["main_files"])
        check("site-level tables imply the PTM run type",
              ptm == (rt == run_type.RUN_TMT_PTM), "site tables: %s" % ptm)
        if cls["n_experiments"] > 1:
            check("multi-plex leads with a ratio table",
                  cls["main_files"][0]["name"].startswith("ratio_"),
                  cls["main_files"][0]["name"])
        check("combined_*.tsv is called out as not the answer",
              any("combined" in a["what"] for a in cls["avoid"]))
    elif rt == run_type.RUN_DIA:
        check("DIA main results live in dia-quant-output/",
              all(os.path.basename(os.path.dirname(f["path"]))
                  == "dia-quant-output" for f in cls["main_files"]))
        check("the root library reports are called out as not the answer",
              any("library" in a["why"].lower() for a in cls["avoid"]))
    elif rt == run_type.RUN_DDA_GROUPED:
        check("grouped DDA points at combined_* files",
              all(f["name"].startswith("combined_")
                  for f in cls["main_files"]))
        check("more than one experiment was found", cls["n_experiments"] > 1,
              str(cls["n_experiments"]))
    elif rt == run_type.RUN_DDA_SINGLE:
        check("ungrouped DDA points at the per-experiment reports",
              all(f["name"] in parse_reports.REPORT_FILES
                  for f in cls["main_files"]),
              str([f["name"] for f in cls["main_files"]]))

    # Any matrix the classification named must actually read as one.
    for f in cls["main_files"]:
        if not qm.is_matrix(f["path"]):
            continue
        info = qm.summarize_matrix(f["path"])
        check("%s reads as a matrix" % f["name"],
              info["rows"] > 0 and info["sample_count"] > 0,
              "%s rows, %s samples" % (info["rows"], info["sample_count"]))
        check("%s has no sample that is empty throughout" % f["name"],
              not info["empty_samples"], str(info["empty_samples"]))
        check("%s counts one value column per sample" % f["name"],
              len(info["values_per_sample"]) == info["sample_count"])


def _write_tsv(path: str, header, rows=()) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(header) + "\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")


def test_classification_precedence() -> None:
    """The precedence rules, on synthetic trees -- no example data needed.

    Each case exists because the wrong answer is plausible: a TMT run really
    does contain combined_*.tsv, and a DIA run really does contain psm.tsv.
    """
    print("\n[11] classification precedence (synthetic)")
    root = ("Index\tNumberPSM\tGene\tMaxPepProb\tProtein\tProtein ID\t"
            "Entry Name\tProtein Description\tOrganism\t"
            "Indistinguishable Proteins\tReferenceIntensity\tS1\tS2"
            ).split("\t")
    with tempfile.TemporaryDirectory() as tmp:
        # TMT: tmt-report/ present alongside combined_*.tsv and per-exp folders.
        wd = os.path.join(tmp, "tmt")
        _write_tsv(os.path.join(wd, "combined_protein.tsv"),
                   ["Protein", "S1 Intensity", "S2 Intensity"])
        _write_tsv(os.path.join(wd, "expA", "psm.tsv"), ["Spectrum"])
        _write_tsv(os.path.join(wd, "tmt-report", "abundance_protein_MD.tsv"),
                   root, [["P1", "2", "G", "1", "p", "P1", "e", "d", "o", "",
                           "20.0", "20.1", "NA"]])
        cls = parse_reports.discover(wd)["classification"]
        check("tmt-report/ outranks combined_*.tsv",
              cls["run_type"] == run_type.RUN_TMT, cls["run_type"])
        check("the TMT headline is the tmt-report matrix",
              cls["main_files"][0]["name"] == "abundance_protein_MD.tsv")

        # TMT + PTM: site tables switch the type and the headline.
        _write_tsv(os.path.join(wd, "tmt-report",
                                "abundance_single-site_MD.tsv"), root)
        cls = parse_reports.discover(wd)["classification"]
        check("site-level tables select the PTM run type",
              cls["run_type"] == run_type.RUN_TMT_PTM, cls["run_type"])
        check("the PTM headline is the site table",
              cls["main_files"][0]["name"] == "abundance_single-site_MD.tsv")

        # DIA: dia-quant-output/ outranks the root library reports.
        wd = os.path.join(tmp, "dia")
        for n in ("psm.tsv", "peptide.tsv", "ion.tsv", "protein.tsv"):
            _write_tsv(os.path.join(wd, n), ["Peptide"])
        _write_tsv(os.path.join(wd, "dia-quant-output",
                                "abundance_protein_X.tsv"),
                   ["Protein", "Protein ID", "Gene", "D:/r1.mzML"],
                   [["p", "P1", "G", "12.0"]])
        cls = parse_reports.discover(wd)["classification"]
        check("dia-quant-output/ outranks the root reports",
              cls["run_type"] == run_type.RUN_DIA, cls["run_type"])
        check("the DIA sample column is shortened to a run name",
              qm.summarize_matrix(cls["main_files"][0]["path"])["samples"]
              == ["r1"])

        # Grouped vs ungrouped DDA.
        wd = os.path.join(tmp, "dda")
        _write_tsv(os.path.join(wd, "expA", "psm.tsv"), ["Spectrum"])
        _write_tsv(os.path.join(wd, "expB", "psm.tsv"), ["Spectrum"])
        _write_tsv(os.path.join(wd, "combined_protein.tsv"),
                   ["Protein", "expA Intensity", "expB Intensity"])
        cls = parse_reports.discover(wd)["classification"]
        check("combined_*.tsv selects grouped DDA",
              cls["run_type"] == run_type.RUN_DDA_GROUPED, cls["run_type"])

        wd = os.path.join(tmp, "dda1")
        for n in ("psm.tsv", "protein.tsv"):
            _write_tsv(os.path.join(wd, n), ["Protein"])
        cls = parse_reports.discover(wd)["classification"]
        check("root reports alone select ungrouped DDA",
              cls["run_type"] == run_type.RUN_DDA_SINGLE, cls["run_type"])
        check("ungrouped DDA leads with protein.tsv",
              cls["main_files"][0]["name"] == "protein.tsv")

        # combined_site_*.tsv must be discovered, not silently skipped.
        _write_tsv(os.path.join(tmp, "dda", "combined_site_M_15.9949.tsv"),
                   ["Index", "Best Localization Probability",
                    "expA Localization Probability", "expA Intensity"])
        inv = parse_reports.discover(os.path.join(tmp, "dda"))
        check("combined_site_*.tsv is discovered",
              "combined_site_M_15.9949.tsv" in inv["combined_site_files"])
        check("combined_site_*.tsv is offered as a result",
              any(f["name"].startswith("combined_site_")
                  for f in inv["classification"]["main_files"]))
        site = parse_reports.summarize(
            inv["combined_site_files"]["combined_site_M_15.9949.tsv"])
        check("'Best' is not counted as a sample",
              site["samples"] == ["expA"], str(site["samples"]))

        # Nothing at all.
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        cls = parse_reports.discover(empty)["classification"]
        check("an empty workdir classifies as unknown",
              cls["run_type"] == run_type.RUN_UNKNOWN, cls["run_type"])


def test_quant_matrix() -> None:
    """The two conventions that silently corrupt results if confused."""
    print("\n[12] quant-matrix conventions (synthetic)")
    with tempfile.TemporaryDirectory() as tmp:
        tmt = os.path.join(tmp, "tmt-report", "abundance_protein_MD.tsv")
        _write_tsv(tmt,
                   ["Index", "Gene", "ReferenceIntensity", "S1", "S2", "S3"],
                   [["P1", "G1", "20.0", "20.5", "NA", "19.5"],
                    ["P2", "G2", "21.0", "21.5", "22.0", "NA"]])
        info = qm.summarize_matrix(tmt)
        check("TMT matrix is flagged log2", info["log2"] is True)
        check("TMT missing marker is NA", info["missing"] == "NA")
        check("TMT samples are found", info["samples"] == ["S1", "S2", "S3"],
              str(info["samples"]))
        check("NA is counted as missing",
              info["values_per_sample"] == {"S1": 2, "S2": 1, "S3": 1},
              str(info["values_per_sample"]))
        check("no row is complete across all three samples",
              info["complete_rows"] == 0, str(info["complete_rows"]))
        check("ReferenceIntensity is not treated as a sample",
              "ReferenceIntensity" not in info["samples"])

        dia = os.path.join(tmp, "dia-quant-output", "abundance_gene_X_Y.tsv")
        _write_tsv(dia, ["Gene", "D:/data/run one.mzML", "D:/data/r2.raw"],
                   [["G1", "1000", ""], ["G2", "2000", "3000"]])
        info = qm.summarize_matrix(dia)
        check("DIA matrix is flagged linear", info["log2"] is False)
        check("DIA sample paths are shortened",
              info["samples"] == ["run one", "r2"], str(info["samples"]))
        check("an empty cell is counted as missing",
              info["values_per_sample"] == {"run one": 2, "r2": 1},
              str(info["values_per_sample"]))
        check("zero is NOT counted as missing",
              qm.summarize_matrix(_zero_matrix(tmp))["values_per_sample"]
              == {"S1": 1}, "log2 of 1 is a real value")

        # DIA-NN puts two identifier columns AFTER the samples.
        pr = os.path.join(tmp, "dia-quant-output", "report.pr_matrix.tsv")
        _write_tsv(pr, ["Protein.Group", "Precursor.Id", "D:/a.mzML",
                        "D:/b.mzML", "All Mapped Proteins", "All Mapped Genes"],
                   [["PG1", "PEP1", "5", "6", "P1;P2", "G1;G2"]])
        info = qm.summarize_matrix(pr)
        check("trailing identifier columns are not read as samples",
              info["samples"] == ["a", "b"], str(info["samples"]))
        check("DIA-NN matrix is recognised by name",
              info["producer"] == qm.PRODUCER_DIANN, info.get("producer"))

        stats = os.path.join(tmp, "dia-quant-output", "report.stats.tsv")
        _write_tsv(stats, ["File.Name", "Precursors.Identified",
                           "Proteins.Identified"],
                   [["D:/data/r1.mzML", "51734", "5581"]])
        rows = qm.read_stats(stats)
        check("report.stats.tsv is read per run", len(rows) == 1)
        check("stats run name is shortened", rows[0]["Run"] == "r1",
              rows[0].get("Run", ""))


def _zero_matrix(tmp: str) -> str:
    path = os.path.join(tmp, "tmt-report", "ratio_gene_MD.tsv")
    _write_tsv(path, ["Index", "ReferenceIntensity", "S1"],
               [["G1", "20.0", "0.0"]])
    return path


def test_manifest_roundtrip() -> None:
    print("\n[6] manifest generation")
    import make_manifest
    with tempfile.TemporaryDirectory() as tmp:
        for sub in ("ctrl", "treat"):
            os.makedirs(os.path.join(tmp, sub))
            for n in ("a", "b"):
                open(os.path.join(tmp, sub, "sample_%s%s.mzML" % (sub[0], n)),
                     "w").close()
        out = os.path.join(tmp, "files.fp-manifest")
        rc = make_manifest.main(["-i", tmp, "-r", "--experiment-from", "parent",
                                 "-o", out])
        check("make_manifest exits cleanly", rc == 0, str(rc))
        with open(out, "r", encoding="utf-8") as fh:
            rows = [l.rstrip("\n").split("\t") for l in fh if l.strip()]
        check("manifest has one row per file", len(rows) == 4, str(len(rows)))
        check("manifest rows have 4 columns",
              all(len(r) == 4 for r in rows))
        check("experiments derived from parent folders",
              sorted({r[1] for r in rows}) == ["ctrl", "treat"],
              str(sorted({r[1] for r in rows})))
        check("data type defaults to DDA", all(r[3] == "DDA" for r in rows))
        # a file name with two dots must be rejected without --force
        open(os.path.join(tmp, "ctrl", "bad.name.mzML"), "w").close()
        rc = make_manifest.main(["-i", tmp, "-r", "-o", out])
        check("bad file name is rejected", rc == 2, str(rc))


def test_fragpipe_location() -> None:
    """The FragPipe resolver and the confirmation gate."""
    print("\n[7] FragPipe location + confirmation gate")
    import check_fragpipe as cf

    # Numeric version ranking. Lexically, '9.4' sorts above '24.1' -- it must not.
    names = ["FragPipe-9.4", "FragPipe-23.1", "FragPipe-24.0",
             "FragPipe-24.1-build5", "FragPipe-24.1-build36"]
    check("versions rank numerically, not lexically",
          max(names, key=cf._version_key) == "FragPipe-24.1-build36",
          max(names, key=cf._version_key))
    check("build numbers compare numerically",
          cf._version_key("FragPipe-24.1-build36") > cf._version_key("FragPipe-24.1-build5"))

    # A directory that is not an installation must be rejected, not guessed at.
    check("non-installation directory is rejected",
          cf.install_from_root(tempfile.gettempdir()) is None)

    # --fragpipe-dir accepts root / bin / launcher forms.
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "FragPipe-1.2")
        os.makedirs(os.path.join(root, "bin"))
        os.makedirs(os.path.join(root, "tools"))
        os.makedirs(os.path.join(root, "workflows"))
        os.makedirs(os.path.join(root, "lib"))
        launcher = os.path.join(root, "bin",
                                "fragpipe.bat" if os.name == "nt" else "fragpipe")
        open(launcher, "w").close()
        open(os.path.join(root, "lib", "fragpipe-1.2.jar"), "w").close()

        inst = cf.install_from_root(root)
        check("a well-formed install is recognised", inst is not None)
        if inst:
            check("version read from lib/fragpipe-<version>.jar",
                  inst["version"] == "1.2", str(inst["version"]))
            check("tools/ detected", inst["tools_dir"] is not None)
            check("workflows/ detected", inst["workflows_dir"] is not None)

        for label, given in (("root", root), ("bin folder", os.path.join(root, "bin")),
                             ("launcher file", launcher)):
            got, src, _ = cf.locate(explicit_dir=given)
            check("--fragpipe-dir accepts the %s" % label,
                  got is not None and os.path.normcase(got["root"]) == os.path.normcase(root),
                  str(got and got["root"]))
            check("%s counts as explicit" % label, src == "--fragpipe-dir", src)

    # Default pick = newest, and PATH must not short-circuit that.
    _test_newest_is_default(cf)

    # The gate: an auto-detected folder must not be usable without confirmation.
    import fragpipe_run as fr
    check("guessed sources are not treated as explicit",
          not cf.is_explicit("newest of 3 installed")
          and not cf.is_explicit("PATH, and the newest installed")
          and not cf.is_explicit("auto-detected"))
    check("explicit sources are accepted without confirmation",
          set(fr.EXPLICIT_SOURCES) == {"--fragpipe-dir", "--fragpipe",
                                       "$FRAGPIPE_DIR", "$FRAGPIPE_BIN"},
          str(fr.EXPLICIT_SOURCES))
    check("run and check share one EXPLICIT_SOURCES definition",
          fr.EXPLICIT_SOURCES is cf.EXPLICIT_SOURCES)
    check("distinct exit codes for 'confirm' and 'not found'",
          fr.EXIT_NEEDS_CONFIRMATION == 3 and fr.EXIT_NO_FRAGPIPE == 4)


def test_qc_series(workdir: str) -> None:
    """The plot data must match what FragSummarizer would draw."""
    print("\n[8] QC plot series")
    import qc_data

    data = qc_data.collect(workdir)
    check("a log was located", bool(data.get("log")))
    check("task runtimes parsed", len(data.get("runtimes", [])) > 0,
          "%d tasks" % len(data.get("runtimes", [])))
    check("MSFragger is expanded into sub-stages, not left as one bar",
          all(r["task"] != "MSFragger" for r in data.get("runtimes", [])))
    check("Percolator appears at most once",
          [r["task"] for r in data.get("runtimes", [])].count("Percolator") <= 1)
    check("total runtime parsed", data.get("total_minutes") is not None,
          str(data.get("total_minutes")))
    if data.get("runtimes") and data.get("total_minutes"):
        plotted = sum(r["minutes"] for r in data["runtimes"])
        check("plotted tasks sum to no more than the reported total",
              plotted <= data["total_minutes"] + 0.05,
              "%.2f vs %.2f" % (plotted, data["total_minutes"]))

    runs = data.get("runs", {})
    check("per-run series were built", len(runs) > 0, "%d runs" % len(runs))
    for name, series in sorted(runs.items()):
        check("%s: retention converted to minutes" % name[:24],
              "retention" not in series or series["retention"]["max"] < 1000,
              str(series.get("retention", {}).get("max")))
        for key in ("peptide_length", "charge", "missed_cleavages"):
            if key not in series:
                continue
            s = series[key]
            if s["type"] == "histogram":
                check("%s: %s bins are well formed" % (name[:24], key),
                      len(s["bin_edges"]) == len(s["counts"]) + 1)
                check("%s: %s counts every PSM once" % (name[:24], key),
                      sum(s["counts"]) == s["n"])
            else:
                check("%s: %s counts every PSM once" % (name[:24], key),
                      sum(s["counts"]) == s["n"])
        sc = series.get("mz_vs_delta_mass")
        if sc:
            check("%s: delta-mass scatter is inside the 0.5 Da window" % name[:24],
                  all(abs(y) < qc_data.DELTA_MASS_WINDOW for y in sc["y"]))
            check("%s: scatter is downsampled" % name[:24],
                  len(sc["x"]) <= qc_data.SCATTER_POINTS * 2 or sc["downsampled_by"] > 1)
        break  # one run is enough for a smoke test

    overall = data.get("overall", {})
    if overall and runs:
        raw = sum(s.get("psms", 0) for s in runs.values())
        check("overall distributions are deduplicated, not summed",
              overall["charge"]["n"] <= raw,
              "%d vs %d PSMs" % (overall["charge"]["n"], raw))
        check("peptide length uses one bin per integer",
              all(round(b - a, 6) == 1.0 for a, b in
                  zip(overall["peptide_length"]["bin_edges"],
                      overall["peptide_length"]["bin_edges"][1:])))

    mc = data.get("mass_calibration", [])
    if mc:
        check("mass calibration rows carry both levels and both states",
              all(k in mc[0] for k in ("ms1_old_median", "ms1_calibrated_median",
                                       "ms2_old_median", "ms2_calibrated_median")))
        check("run indices resolve to file names",
              any(not r["run"].isdigit() for r in mc),
              str(mc[0]["run"]))

    pf = data.get("percolator_features", {})
    if pf:
        feats = next(iter(pf.values()))
        check("feature weights are sorted by absolute value",
              [abs(f["mean_weight"]) for f in feats]
              == sorted((abs(f["mean_weight"]) for f in feats), reverse=True))
        check("all-zero features are dropped",
              all(any(v != 0 for v in f["splits"]) for f in feats))

    # Sampling must preserve the shape while scaling the counts down.
    sampled = qc_data.collect(workdir, stride=10)
    if runs and sampled.get("runs"):
        name = sorted(runs)[0]
        check("stride sampling reduces the PSM count",
              sampled["runs"][name]["psms"] < runs[name]["psms"])
        check("stride sampling is flagged in the notes",
              any("stride" in n for n in sampled.get("notes", [])))


def test_report(workdir: str) -> None:
    """The HTML report: valid SVG, sane geometry, every page present."""
    print("\n[9] summary report")
    import xml.etree.ElementTree as ET

    import make_report
    import qc_data
    import svg_charts as svg

    data = qc_data.collect(workdir)
    html = make_report.build_html(data, embed_images=False)

    check("report is produced", len(html) > 5000, "%d chars" % len(html))
    check("it is one self-contained document",
          html.startswith("<!DOCTYPE html>") and html.rstrip().endswith("</html>"))
    check("no external resources are referenced",
          "http://" not in html.replace("http://www.w3.org/2000/svg", "")
          and "https://" not in html)

    svgs = re.findall(r"<svg.*?</svg>", html, re.DOTALL)
    check("charts were drawn", len(svgs) > 5, "%d panels" % len(svgs))

    malformed = out_of_canvas = negative = 0
    for chunk in svgs:
        try:
            root = ET.fromstring(chunk)
        except ET.ParseError:
            malformed += 1
            continue
        vb = [float(v) for v in (root.get("viewBox") or "0 0 0 0").split()]
        for el in root.iter():
            if not el.tag.endswith("}rect"):
                continue
            x, y = float(el.get("x")), float(el.get("y"))
            w, h = float(el.get("width")), float(el.get("height"))
            if w < 0 or h < 0:
                negative += 1
            if x < -1 or y < -1 or x + w > vb[2] + 1 or y + h > vb[3] + 1:
                out_of_canvas += 1
    check("every chart is well-formed XML", malformed == 0, "%d bad" % malformed)
    check("no bar has a negative size", negative == 0, "%d bad" % negative)
    check("no bar is drawn outside its canvas", out_of_canvas == 0,
          "%d bad" % out_of_canvas)

    pages = re.findall(r'<h2[^>]*>(.*?)(?:<span|</h2>)', html, re.DOTALL)
    check("the overall statistics page is present",
          any("Overall Statistics" in p for p in pages), str(pages[:4]))
    check("one page per run",
          sum(1 for p in pages if p.endswith(" Statistics")
              and "Overall" not in p) == len(data.get("runs", {})),
          str(len(data.get("runs", {}))))

    # Per-run bars must be used exactly when per-experiment counts say nothing.
    per_run = data.get("ids_per_run") or []
    if len(data.get("ids", [])) < 2 and len(per_run) > 1:
        check("collapsed layouts fall back to per-run ID bars",
              "IDs (per run)" in html)
    elif data.get("ids"):
        check("per-experiment ID bars are labelled as such",
              "IDs (per experiment)" in html)

    # Chart primitives on their own.
    empty = svg.histogram({"counts": [], "bin_edges": []}, "t", "x")
    check("an empty series degrades to a placeholder, not a crash",
          "no-data" in empty)
    hist = svg.histogram({"counts": [1, 2], "bin_edges": [0, 1, 2]}, "t", "x")
    check("a two-bin histogram draws two bars", hist.count("<rect") == 2)
    scale = svg.nice_scale(0, 97, 5)
    check("axis ranges round outward", scale[1] >= 97 and scale[0] <= 0, str(scale))
    check("HTML special characters are escaped",
          svg.esc('a&b<c>"d') == "a&amp;b&lt;c&gt;&quot;d")

    if data.get("runtimes"):
        clock = svg.gantt(data["runtimes"],
                          __import__("datetime").datetime(2026, 1, 1, 12, 0))
        elapsed = svg.gantt(data["runtimes"], None)
        check("the Gantt renders with and without a finish time",
              clock.count("<rect") == elapsed.count("<rect") == len(data["runtimes"]))
        check("a clock axis is used when the finish time is known",
              "12:0" in clock or ":00" in clock)


def test_commands() -> None:
    """The slash commands must stay dispatchers, and must not dangle.

    They point at SKILL.md sections by name rather than restating the rules, so
    the one way they can rot is a heading being renamed underneath them.
    """
    print("\n[10] slash commands and the plugin manifest")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd_dir = os.path.join(repo, "commands")
    if not os.path.isdir(cmd_dir):
        print("  SKIP  no commands/ directory")
        return

    with open(os.path.join(repo, "SKILL.md"), "r", encoding="utf-8") as fh:
        skill = fh.read()
    headings = set(re.findall(r"^## (.+)$", skill, re.M))

    files = sorted(glob.glob(os.path.join(cmd_dir, "fragskills-*.md")))
    check("commands are present", len(files) > 0, "%d found" % len(files))
    for path in files:
        name = os.path.basename(path)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        check("%s has a description" % name,
              bool(re.search(r"^description:\s*\S", text, re.M)))
        check("%s stays a dispatcher, not a second copy of the docs" % name,
              len(text) < 1500, "%d chars" % len(text))
        check("%s loads the skill" % name, "fragskills" in text)
        for section in re.findall(r'"([^"]+)" section', text):
            check("%s points at a real SKILL.md heading" % name,
                  section in headings, "'%s' is gone" % section)
    test_plugin_manifest(repo)


def test_plugin_manifest(repo: str) -> None:
    """The plugin files must keep the install line in the README working.

    Users install with `/plugin install fragskills --marketplace Nesvilab/FragSkills`
    and update as `fragskills@nesvilab`, so neither name may change. A version
    would freeze updates until someone bumps it, so there must be none: each
    commit is then a new version. The plugin lists its commands one by one, so
    a command file missing from the list would never load. `claude plugin
    validate .` checks the schema; this only checks what it cannot know.
    """
    def load(name):
        try:
            with open(os.path.join(repo, ".claude-plugin", name), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError) as exc:
            check("%s parses" % name, False, str(exc))
            return None

    plugin, market = load("plugin.json"), load("marketplace.json")
    if plugin is None or market is None:
        return
    entries = [p for p in market.get("plugins", []) if p.get("name") == "fragskills"]
    check("the plugin is named fragskills", plugin.get("name") == "fragskills",
          str(plugin.get("name")))
    check("the marketplace is named nesvilab and lists the plugin at the root",
          market.get("name") == "nesvilab" and len(entries) == 1
          and entries[0].get("source") in ("./", "."), json.dumps(entries))
    check("no version is pinned, so every commit is an update",
          "version" not in plugin and not any("version" in p for p in entries))
    check("the license is MIT and LICENSE is there", plugin.get("license") == "MIT"
          and os.path.isfile(os.path.join(repo, "LICENSE")))
    listed = plugin.get("commands") or {}
    given = [c.get("source", "") if isinstance(c, dict) else "" for c in listed.values()]
    sources = {os.path.normpath(os.path.join(repo, s)) for s in given}
    check("every listed command file exists, as a ./ path",
          all(s.startswith("./") and "\\" not in s for s in given)
          and all(os.path.isfile(s) for s in sources) and len(sources) == len(listed),
          json.dumps(listed))
    command_files = glob.glob(os.path.join(repo, "commands", "*.md"))
    check("every command file is listed in plugin.json",
          {os.path.normpath(f) for f in command_files} <= sources,
          ", ".join(sorted(os.path.basename(f) for f in command_files
                           if os.path.normpath(f) not in sources)))
    with open(os.path.join(repo, "README.md"), "r", encoding="utf-8") as fh:
        readme = fh.read()
    check("the README installs and updates the plugin by these names",
          "/plugin install fragskills --marketplace Nesvilab/FragSkills" in readme
          and "fragskills@nesvilab" in readme)
    old = re.findall(r"(?<![\w.])/fragskills-(?:new|run|status|report)\b(?!\.md)", readme)
    check("the README names every command as the plugin does",
          all("/fragskills:%s" % name in readme for name in listed) and not old,
          ", ".join(old))


def _make_install(root: str, version: str) -> str:
    """Create a minimal but valid FragPipe install tree."""
    for sub in ("bin", "tools", "workflows", "lib"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    launcher = os.path.join(root, "bin",
                            "fragpipe.bat" if os.name == "nt" else "fragpipe")
    open(launcher, "w").close()
    open(os.path.join(root, "lib", "fragpipe-%s.jar" % version), "w").close()
    return launcher


def _test_newest_is_default(cf) -> None:
    """Detection must default to the newest version, PATH included in the ranking."""
    import shutil as _shutil
    with tempfile.TemporaryDirectory() as tmp:
        versions = ["9.4", "23.1", "24.0", "24.1-build5", "24.1-build36"]
        roots = {}
        for v in versions:
            r = os.path.join(tmp, "FragPipe-%s" % v)
            _make_install(r, v)
            roots[v] = r
        newest = roots["24.1-build36"]

        orig_globs, orig_which = cf.CANDIDATE_ROOT_GLOBS, cf.shutil.which
        orig_env = {k: os.environ.pop(k, None) for k in ("FRAGPIPE_DIR", "FRAGPIPE_BIN")}
        try:
            cf.CANDIDATE_ROOT_GLOBS = [os.path.join(tmp, "FragPipe-*")]

            # (a) nothing on PATH -> newest wins
            cf.shutil.which = lambda *a, **k: None
            inst, source, others = cf.locate()
            check("default pick is the newest install",
                  inst is not None and inst["root"] == newest, str(inst and inst["root"]))
            check("older installs are reported as alternatives",
                  len(others) == len(versions) - 1, str(len(others)))
            check("newest pick is still a guess needing confirmation",
                  not cf.is_explicit(source), source)

            # (b) an OLD fragpipe on PATH must NOT win
            old_launcher = os.path.join(roots["23.1"], "bin",
                                        "fragpipe.bat" if os.name == "nt" else "fragpipe")
            cf.shutil.which = lambda *a, **k: old_launcher
            inst, source, _ = cf.locate()
            check("an older fragpipe on PATH does not outrank the newest",
                  inst is not None and inst["root"] == newest, str(inst and inst["root"]))
            check("the ignored PATH install is reported",
                  inst is not None and inst.get("path_install_ignored") is not None,
                  str(inst and inst.get("path_install_ignored")))

            # (c) when PATH already is the newest, say so and flag nothing
            new_launcher = os.path.join(newest, "bin",
                                        "fragpipe.bat" if os.name == "nt" else "fragpipe")
            cf.shutil.which = lambda *a, **k: new_launcher
            inst, source, _ = cf.locate()
            check("PATH pointing at the newest is used without a warning",
                  inst is not None and inst["root"] == newest
                  and inst.get("path_install_ignored") is None, source)

            # (d) an explicit older choice is never overridden
            inst, source, _ = cf.locate(explicit_dir=roots["23.1"])
            check("an explicitly chosen older version is honoured",
                  inst is not None and inst["root"] == roots["23.1"] and cf.is_explicit(source),
                  "%s / %s" % (inst and inst["root"], source))
        finally:
            cf.CANDIDATE_ROOT_GLOBS, cf.shutil.which = orig_globs, orig_which
            for k, v in orig_env.items():
                if v is not None:
                    os.environ[k] = v


# --- workflow builder ---------------------------------------------------------

def _quiet(fn, *a, **k):
    """Call *fn* with stdout/stderr captured; return (result, captured text)."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        out = fn(*a, **k)
    return out, buf.getvalue()


def test_workflow_tree() -> None:
    """workflow_tree.json is consistent: every route resolves to itself."""
    import itertools
    import workflow_tree as wt
    print("\n[13] workflow builder: question tree")
    tree = wt.load_tree()
    order = [q["id"] for q in tree["questions"]]
    check("question ids are unique", len(order) == len(set(order)))
    late = ["%s<-%s" % (q["id"], dep) for i, q in enumerate(tree["questions"])
            for dep in q.get("when", {}) if dep not in order[:i]]
    check("a question depends only on earlier questions", not late, ", ".join(late))
    names = [r["workflow"] for r in tree["routes"]]
    check("one route per workflow", len(names) == len(set(names)))
    bad_values = []
    for r in tree["routes"]:
        for q, want in r["answers"].items():
            values = {a["value"] for a in tree["_questions"].get(q, {"answers": []})["answers"]}
            for v in (want if isinstance(want, list) else [want]):
                if v not in values:
                    bad_values.append("%s:%s=%s" % (r["workflow"], q, v))
        if r["data"] not in tree["data_type_sets"]:
            bad_values.append("%s: data set %s" % (r["workflow"], r["data"]))
    check("routes use only known questions, answers and data sets", not bad_values,
          ", ".join(bad_values[:5]))

    wrong = []
    for r in tree["routes"]:
        keys = list(r["answers"])
        lists = [v if isinstance(v, list) else [v] for v in r["answers"].values()]
        for combo in itertools.product(*lists):
            try:
                res = wt.resolve(tree, dict(zip(keys, combo)))
            except ValueError as exc:
                wrong.append(str(exc))
                continue
            if res.open or res.rejected or res.route is not r:
                wrong.append("%s %s" % (r["workflow"], dict(zip(keys, combo))))
            loose = [q for q in res.settled if q not in r["answers"]]
            if loose:
                wrong.append("%s leaves %s unconstrained" % (r["workflow"], loose))
    check("every route's answers select exactly that route", not wrong, "; ".join(wrong[:3]))

    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DIA", "search": "open"})
    check("an unshipped combination is rejected", [r["id"] for r in res.rejected] == ["search"],
          str(res.rejected))
    check("the nearest shipped workflow keeps the acquisition (DIA, not the DDA 'Open')",
          res.rejected and (res.rejected[0]["nearest"] or {}).get("workflow")
          == "DIA_SpecLib_Quant", str(res.rejected))
    near = wt.nearest(tree, {"analysis": "glyco", "acquisition": "DDA+"}, "acquisition")
    check("... and the kind of analysis (glyco with DDA+ suggests a glyco workflow)",
          near["workflow"].startswith("glyco"), str(near))
    later = wt.resolve(tree, {"analysis": "glyco", "acquisition": "DDA", "glyco_type": "O",
                              "quant": "TMT", "tmt_plex": "16"})
    check("... counting answers to later questions (O-glyco with TMTpro-16 keeps O-glyco, "
          "not N-glyco TMT)", bool(later.rejected) and (later.rejected[0]["nearest"] or {})
          .get("workflow", "").startswith("glyco-O"), str(later.rejected))
    check("a rejected answer stays asked and stays in the plan",
          "search" in [q["id"] for q in res.open]
          and res.kept_answers.get("search") == "open", str([q["id"] for q in res.open]))
    search = next((q for q in res.open if q["id"] == "search"), None)
    check("an open question lists only answers that still lead somewhere",
          search is not None and {a["value"] for a in search["answers"]}
          == {"closed", "nonspecific_hla"}, str(search and search["answers"]))
    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DDA", "search": "closed",
                            "quant": "TMT", "tmt_plex": "35", "tmt_reference": "virtual",
                            "ptm": "phospho"})
    check("a rejected answer is asked again even when one answer is left",
          [q["id"] for q in res.open] == ["ptm"] and res.route is None
          and res.rejected[0]["nearest"]["workflow"] == "TMT35", str(res.rejected))
    res = wt.resolve(tree, {"analysis": "standard", "ptm": "phospho"})
    check("an answer to a question not asked yet is kept, not dropped",
          res.pending == {"ptm": "phospho"} and not res.dropped
          and res.kept_answers.get("ptm") == "phospho", "%s %s" % (res.pending, res.dropped))
    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DDA", "search": "open",
                            "quant": "none", "open_variant": "full", "ptm": "phospho"})
    check("... and dropped, with a reason, once the answers make it irrelevant",
          [d["id"] for d in res.dropped] == ["ptm"] and "ptm" not in res.kept_answers,
          str(res.dropped))
    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DDA+"})
    ptm = next((q for q in res.open if q["id"] == "ptm"), {})
    check("a question narrowed to one answer by the routes is implied, not asked",
          all(res.sources.get(q) == wt.SOURCE_IMPLIED for q in ("search", "quant")),
          str(res.sources))
    check("... but the PTM enrichment is asked, its other answers listed with the nearest "
          "workflow", [q["id"] for q in res.open] == ["ptm"]
          and [a["value"] for a in ptm.get("answers", [])] == ["none"]
          and sorted(u["value"] for u in ptm.get("unavailable", []))
          == ["acetyl", "phospho", "ubiquitin"]
          and all(u["nearest"]["workflow"] for u in ptm["unavailable"]), str(ptm)[:400])
    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DDA+", "ptm": "none"})
    check("... and confirming it settles the route",
          not res.open and res.route["workflow"] == "WWA", str(res.open)[:300])
    asked = []
    for answers in ({"analysis": "standard", "acquisition": "DDA", "search": "closed",
                     "quant": "TMT", "tmt_plex": "16", "tmt_ms_level": "MS3"},
                    {"analysis": "glyco", "acquisition": "DIA", "glyco_type": "O"}):
        asked += [(q["id"], len(q["answers"])) for q in wt.resolve(tree, answers).open
                  if q["id"] in ("tmt_reference", "dia_format")]
    check("the TMT reference and the DIA instrument are asked with one answer too",
          ("tmt_reference", 1) in asked and ("dia_format", 1) in asked, str(asked))
    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DDA", "search": "closed",
                            "quant": "TMT", "tmt_plex": "16", "tmt_reference": "bridge"})
    check("the nearest workflow keeps the TMT reagent (TMTpro-16 is never sent to TMT10)",
          res.rejected and res.rejected[0]["nearest"]["workflow"] == "TMT16",
          str(res.rejected))
    res = wt.resolve(tree, {"analysis": "standard", "acquisition": "DIA",
                            "dia_format": "thermo_sciex", "search": "closed",
                            "quant": "LFQ", "ptm": "none"})
    check("a single possible answer is implied, not asked",
          res.sources.get("dia_library") == wt.SOURCE_IMPLIED
          and res.route and res.route["workflow"] == "DIA_SpecLib_Quant",
          str(res.sources))
    check("24.1 and 25.0 are supported, 24.0 is not",
          wt.supported_version(tree, "24.1-build61") == "24.1"
          and wt.supported_version(tree, "25.0-build3") == "25.0"
          and wt.supported_version(tree, "24.0") is None)
    renames = tree["versions"]["25.0"]["renames"]
    check("the 25.0 rename table is one-to-one", len(set(renames.values())) == len(renames))


def test_properties_io() -> None:
    import build_workflow as bw
    print("\n[14] workflow builder: .workflow files")
    for value in ["G:\\dev\\db.fas", "a=b:c#d!e", " leading", "tab\there", "\u00e9t\u00e9", ""]:
        check("escape/unescape round-trips %a" % value,
              bw.unescape(bw.escape(value)) == value, bw.escape(value))
    emoji = "C:\\db\\human_\U0001F9EC.fas"
    check("a character outside the BMP is written as a surrogate pair, as Java reads it",
          "\\uD83E\\uDDEC" in bw.escape(emoji) and bw.unescape(bw.escape(emoji)) == emoji,
          bw.escape(emoji))
    check("Windows paths escape like Java Properties",
          bw.escape("G:\\dev\\x.fas") == "G\\:\\\\dev\\\\x.fas", bw.escape("G:\\dev\\x.fas"))
    base = ("# Workflow: T\r\n\r\na.key=1\r\nmulti=one, \\\r\n    two\r\n"
            "z.key=keep \\: me\r\n")
    out = bw.render_workflow(base, {"multi": "three", "new.key": "C:\\x"}, ["# head"])
    lines = out.split("\r\n")
    check("CRLF is kept", "\r\n" in out and "\n" not in out.replace("\r\n", ""))
    check("the header goes on top", lines[0] == "# head", lines[0])
    check("a continued value is replaced as one entry",
          "multi=three" in lines and not any(l.strip() == "two" for l in lines), out)
    check("untouched lines keep their exact text", "z.key=keep \\: me" in lines)
    check("a key the base lacks is appended", lines[-2] == "new.key=C\\:\\\\x", lines[-2])


def test_design_sheet() -> None:
    import make_manifest as mm
    print("\n[15] workflow builder: design sheet + manifest check")
    rows = mm.propose_design(["/d/ctrl_rep1.mzML", "/d/ctrl_rep2.mzML", "/d/HeLa_R3.raw",
                              "/d/b1923_293T_QE3_122212.mzML", "/d/sample10.mzML"])
    got = [(r["condition"], r["replicate"]) for r in rows]
    check("replicates are read from a trailing number", got[:3] ==
          [("ctrl", "1"), ("ctrl", "2"), ("HeLa", "3")], str(got))
    check("a long run counter is not taken for a replicate",
          got[3] == ("b1923_293T_QE3_122212", "1"), str(got[3]))
    check("a number glued to the name is a replicate", got[4] == ("sample", "10"), str(got[4]))
    with tempfile.TemporaryDirectory() as tmp:
        f1 = os.path.join(tmp, "a.mzML")
        open(f1, "w").close()
        sheet = os.path.join(tmp, "design.tsv")
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write("# comment\nfile\tcondition\treplicate\tdata_type\n")
            fh.write("%s\tct rl\t1\tdia-quant\n" % f1)
        rows, problems = mm.read_design(sheet)
        check("a valid sheet parses", not problems and rows[0]["data_type"] == "DIA-Quant"
              and rows[0]["condition"] == "ct_rl", "%s %s" % (problems, rows))
        _, problems = mm.read_design(sheet, ["DDA"])
        check("a data type the workflow does not allow is refused", len(problems) == 1,
              str(problems))
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n")
            fh.write("%s\tc\tone\tDIA_Quant\n%s\tc\t1\tDDA\n" % (f1, os.path.join(tmp, "x.raw")))
        _, problems = mm.read_design(sheet)
        check("bad replicate, unknown data type and a missing file are all reported",
              len(problems) == 3, str(problems))
        f2 = os.path.join(tmp, "sub", "a.mzML")
        os.makedirs(os.path.dirname(f2))
        open(f2, "w").close()
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n")
            fh.write("%s\tc\t1\tDDA\n%s\t\t\tDDA\n" % (f1, f2))
        _, problems = mm.read_design(sheet)
        check("a design refuses an unnamed file beside named ones, and a repeated file name",
              len(problems) == 2 and "neither condition" in problems[0]
              and "same file name" in problems[1], str(problems))
        with open(sheet, "wb") as fh:
            fh.write(b"file\tcondition\treplicate\tdata_type\n%s\tKontrolle_\xe4\t1\tDDA\n"
                     % f1.encode())
        check("a design sheet that is not UTF-8 is a problem, not a crash",
              "not UTF-8" in " ".join(mm.read_design(sheet)[1]))

        man = os.path.join(tmp, "m.fp-manifest")
        fs = [os.path.join(tmp, "f%d.mzML" % i) for i in range(4)]
        for f in fs:                # a listed file that is missing stops FragPipe
            open(f, "w").close()
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\t\t\tDDA\n%s\te\t\t\n%s\te\tx\tdia_quant\n%s\t\t\tdia-quant\n"
                     % tuple(fs))
        problems = mm.check_manifest(man)
        check("manifest check flags an empty type, a bad replicate and an unknown type",
              len(problems) == 3 and "guesses" in problems[0], str(problems))
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("# a comment FragPipe skips\n%s\te\t1 \tDDA\n%s\te\t1_0\tDDA\n"
                     "%s\te\t+2\tDDA\n" % tuple(fs[:3]))
        problems = mm.check_manifest(man)
        check("manifest check skips # comments and reads bioreplicates like Integer.parseInt",
              len(problems) == 2 and all("bioreplicate" in p for p in problems), str(problems))
        with open(man, "w", encoding="utf-8-sig") as fh:
            fh.write("%s\te\t1\tDDA\n" % fs[0])
        check("a byte-order mark is reported (FragPipe drops the first row)",
              "byte-order mark" in " ".join(mm.check_manifest(man)))
        spaced = os.path.join(tmp, "My Data", "f.mzML")
        os.makedirs(os.path.dirname(spaced))
        open(spaced, "w").close()
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\te\t1\tDDA\n%s\te\t1\tDDA\n%s\te\t2\tDDA\n" % (spaced, f1, f2))
        problems = mm.check_manifest(man)
        check("a folder FragPipe refuses and a repeated file name are reported",
              len(problems) == 2 and "My Data" in problems[0] and "a.mzML" in problems[1],
              str(problems))
        problems = mm.check_manifest(man, name_rules=False)
        check("... and for FragPipe 24.0, whose headless mode has no name rules, only the "
              "repeated name", len(problems) == 1 and "a.mzML" in problems[0], str(problems))

        # Java's rules, not Python's: trim keeps a non-breaking space, parseInt takes
        # any Unicode digit, a bare // line is a comment
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("//\n%s\te\t１\tDDA\n%s\te\t1\tDIA \n%s\te\t \tDDA\n"
                     " \n" % tuple(fs[:3]))
        problems = mm.check_manifest(man)
        check("NBSP cells and an NBSP-only line are reported; a fullwidth replicate and a "
              "// line are not", len(problems) == 5 and "unknown data type" in problems[0]
              and "bioreplicate" in problems[1] and "no such file" in problems[2]
              and "no data type" in problems[3] and "file name" in problems[4], str(problems))
        check("folders are checked as FragPipe sees them: '..' unresolved, relative to "
              "FragPipe's folder",
              mm.check_folders([os.path.join(tmp, "My Data", "..", "a.mzML")])
              and mm.check_folders(["data/a.mzML"], base=os.path.join(tmp, "My Data"))
              and not mm.check_folders(["data/a.mzML"], base=tmp))
        upper = os.path.join(tmp, "up", "A.mzML")
        os.makedirs(os.path.dirname(upper))
        open(upper, "w").close()
        check("names differing only in case are not duplicates (FragPipe compares exactly)",
              not mm.check_duplicate_names([f1, upper]))
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n%s\tc\t²\tDDA\n%s\t\t\tDDA\n"
                     % (f1, upper))
        rows, problems = mm.read_design(sheet, groups_collapse=True)
        check("a replicate like '²' is a problem, not a crash; an unnamed file is fine "
              "where FragPipe puts every file in one group",
              len(problems) == 1 and "whole number" in problems[0], str(problems))
        check("a design sheet that is gone is a problem, not a crash",
              "cannot read" in " ".join(mm.read_design(os.path.join(tmp, "gone.tsv"))[1]))

        # adding a folder: a .d run is one input, never entered; MSFragger's and
        # diaTracer's copies skipped
        run = os.path.join(tmp, "bruker", "run1.d")
        os.makedirs(run)
        for name in ("analysis.tdf", "run1.mgf"):
            open(os.path.join(run, name), "w").close()
        for name in ("s1.mzML", "s1_uncalibrated.mzML", "s1_calibrated.MGF",
                     "run1_diatracer.mzML"):
            open(os.path.join(tmp, "bruker", name), "w").close()
        got = [os.path.basename(p) for p in mm.collect_files([os.path.join(tmp, "bruker")],
                                                             recursive=True)]
        check("a folder gives its .d runs whole and skips MSFragger's calibrated copies and "
              "diaTracer's output", got == ["run1.d", "s1.mzML"], str(got))
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\te\t1\tDIA\n" % os.path.join(tmp, "bruker", "run1_diatracer.mzML"))
        check("diaTracer's output in a manifest is reported (FragPipe stops on it)",
              "diaTracer's output" in " ".join(mm.check_manifest(man)))
        check("... but not for 24.0 and 24.1 up to build60, which load it",
              not mm.check_manifest(man, diatracer_filter=False)
              and [mm.has_diatracer_filter(v) for v in ("24.0", "24.1-build60", "24.1-build61",
                                                         "25.0-build3", None)]
              == [False, False, True, True, True])
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\te\t1\tDDA\n" % os.path.join(tmp, "gone.mzML"))
        problems = mm.check_manifest(man)
        check("a manifest row for a file that is not there is reported (FragPipe stops on it)",
              len(problems) == 1 and "no such file" in problems[0], str(problems))

        # DIA-Umpire writes <run>_Q1/_Q2/_Q3 beside its input, which FragPipe would load
        ump = os.path.join(tmp, "umpire")
        os.makedirs(ump)
        for name in ("s2.mzML", "s2_Q1.mzML", "s2_Q2.mzML", "s2_Q3.mgf", "s2_Q3.mzML",
                     "plasma_Q1.mzML"):
            open(os.path.join(ump, name), "w").close()
        got = [os.path.basename(p) for p in mm.collect_files([ump], recursive=True)]
        check("a folder skips DIA-Umpire's output of a run beside it, but keeps a run only "
              "named like it", got == ["plasma_Q1.mzML", "s2.mzML"], str(got))
        found = []
        for rows in ((("s2.mzML", "DIA"), ("s2_Q1.mzML", "DDA")), (("s2_Q2.mzML", "DIA"),),
                     (("s2_Q3.mgf", "DDA"), ("s2_Q3.mzML", "DDA")), (("plasma_Q1.mzML", "DIA"),)):
            with open(man, "w", encoding="utf-8") as fh:
                fh.write("".join("%s\te\t%d\t%s\n" % (os.path.join(ump, n), i + 1, t)
                                 for i, (n, t) in enumerate(rows)))
            found.append(mm.check_manifest(man))
        check("a manifest listing DIA-Umpire's output with its run, typed DIA, or as both "
              ".mgf and .mzML is reported; plasma_Q1 is not",
              [len(p) for p in found] == [1, 1, 1, 0] and "also lists" in found[0][0]
              and "typed DIA" in found[1][0] and "differ only in extension" in found[2][0],
              str(found))
        for name in ("ctrl_1.raw", "ctrl_1.mzML"):
            open(os.path.join(ump, name), "w").close()
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("".join("%s\te\t1\tDDA\n" % os.path.join(ump, n)
                             for n in ("ctrl_1.raw", "ctrl_1.mzML")))
        problems = mm.check_manifest(man, name_rules=False)
        check("two inputs in one folder that differ only in extension are reported (one "
              ".pepXML for both), name rules or not", len(problems) == 1
              and "ctrl_1.pepXML" in problems[0]
              and not mm.check_same_stem([os.path.join(tmp, "a", "x.raw"),
                                          os.path.join(tmp, "b", "x.mzML")]), str(problems))
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\te\t99999999999\tDDA\n" % fs[0])
        problems = mm.check_manifest(man)
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n%s\tc\t99999999999\tDDA\n" % f1)
        check("a bioreplicate too large for a Java int is reported, in a manifest and a sheet",
              len(problems) == 1 and "bioreplicate" in problems[0]
              and len(mm.read_design(sheet)[1]) == 1, str(problems))
        dup = os.path.join(tmp, "dup")
        for sub in ("x", "y"):
            os.makedirs(os.path.join(dup, sub))
            open(os.path.join(dup, sub, "a.mzML"), "w").close()
        rc, out = _quiet(mm.main, ["--input", dup, "--recursive", "--out",
                                   os.path.join(tmp, "dup.fp-manifest")])
        check("make_manifest.py refuses two inputs of the same file name",
              rc == 2 and "same file name" in out
              and not os.path.exists(os.path.join(tmp, "dup.fp-manifest")), out[-300:])
        if os.name == "nt":
            import subprocess
            loop = os.path.join(tmp, "bruker", "loop")
            if subprocess.run(["cmd", "/c", "mklink", "/J", loop, os.path.join(tmp, "bruker")],
                              capture_output=True).returncode == 0:
                try:
                    got = [os.path.basename(p) for p in
                           mm.collect_files([os.path.join(tmp, "bruker")], recursive=True)]
                finally:
                    os.rmdir(loop)              # the junction, not its target
                check("a folder linked back to its parent is walked once",
                      got == ["run1.d", "s1.mzML"], str(got))
            with open(man, "w", encoding="utf-8") as fh:
                fh.write("%s \te\t1\tDDA\n" % fs[0])
            problems = mm.check_manifest(man, name_rules=False)
            check("a path Java cannot read on Windows (a trailing space) is reported, name "
                  "rules or not", len(problems) == 1 and "drops the row" in problems[0],
                  str(problems))
        top = os.path.join(tmp, "top")
        os.makedirs(top)
        try:
            os.symlink(os.path.join(tmp, "bruker"), os.path.join(top, "linked"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            print("  SKIP  a linked folder is followed, as FragPipe does (no symlinks here)")
        else:
            got = [os.path.basename(p) for p in mm.collect_files([top], recursive=True)]
            check("a linked folder is followed, as FragPipe does",
                  got == ["run1.d", "s1.mzML"], str(got))

        # Java's rules: a line of control characters is an empty path (the folder FragPipe
        # runs in), and Integer.parseInt reads UTF-16 chars, so a math-bold digit is no digit
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\te\t1\tDDA\n%s\te\t\U0001d7cf\tDDA\n\x1a" % (fs[0], fs[1]))
        problems = mm.check_manifest(man)
        check("a Ctrl-Z line and a replicate outside the BMP are reported",
              len(problems) == 2 and "bioreplicate" in problems[0]
              and "control characters" in problems[1], str(problems))
        check("a manifest that cannot be read is a problem, not a crash",
              "cannot read" in " ".join(mm.check_manifest(tmp)))
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n%s\tc\t1\tDIA\n%s\t\t\tDIA\n"
                     % (f1, upper))
        out_m = os.path.join(tmp, "collapse.fp-manifest")
        rc1, _ = _quiet(mm.main, ["--design", sheet, "--out", out_m])
        rc2, _ = _quiet(mm.main, ["--design", sheet, "--out", out_m, "--groups-collapse"])
        check("make_manifest.py --design takes a blank condition beside named ones only "
              "with --groups-collapse", rc1 == 2 and rc2 == 0, "%s %s" % (rc1, rc2))


def _fake_install(root: str, version: str, workflows: dict) -> dict:
    _make_install(root, version)
    for name, text in workflows.items():
        with open(os.path.join(root, "workflows", name + ".workflow"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(text)
    import check_fragpipe as cf
    return cf.install_from_root(root)


_LFQ_TEXT = ("# Workflow: LFQ-MBR\r\n\r\ndatabase.decoy-tag=rev_\r\n"
             "phi-report.remove-contaminants=false\r\n"
             "quantitation.run-label-free-quant=true\r\n"
             "tmtintegrator.run-tmtintegrator=false\r\n")
_DIA_TEXT = ("# Workflow: DIA_SpecLib_Quant\r\n\r\ndiann.run-diann=true\r\n"
             "phi-report.remove-contaminants=false\r\ntmtintegrator.run-tmtintegrator=false\r\n")
_TMT_TEXT = ("# Workflow: TMT10-bridge\r\n\r\nphi-report.remove-contaminants=false\r\n"
             "tmtintegrator.add_Ref=-1\r\ntmtintegrator.channel_num=TMT-10\r\n"
             "tmtintegrator.ref_tag=pool\r\ntmtintegrator.run-tmtintegrator=true\r\n")


def test_builder_flow() -> None:
    import build_workflow as bw
    import fragskills_plan as fp
    print("\n[16] workflow builder: questions, build, confirm, lock")
    with tempfile.TemporaryDirectory() as tmp:
        install = _fake_install(os.path.join(tmp, "FragPipe-24.1-build61"), "24.1-build61",
                                {"LFQ-MBR": _LFQ_TEXT, "DIA_SpecLib_Quant": _DIA_TEXT})
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        for n in ("ctrl_1", "ctrl_2", "treat_1"):
            open(os.path.join(data, n + ".mzML"), "w").close()
        fasta = os.path.join(tmp, "db.fas")
        with open(fasta, "w") as fh:
            fh.write(">rev_x\nAAA\n")
        wd = os.path.join(tmp, "out")
        base = ["--workdir", wd]
        rc, _ = _quiet(bw.main, base + ["--data", data])
        check("no FragPipe folder means a bad call, never a guess", rc == bw.EXIT_CONFIG_ERROR,
              str(rc))
        rc, _ = _quiet(bw.main, base + ["--fragpipe-dir", install["root"], "--data", data])
        check("open questions exit 5", rc == bw.EXIT_QUESTIONS, str(rc))
        rc, out = _quiet(bw.main, base + ["--json", "--answer", "analysis=standard",
                                          "--answer", "acquisition=DDA", "--answer",
                                          "search=closed", "--answer", "quant=LFQ",
                                          "--answer", "ptm=acetyl"])
        payload = json.loads(out) if out.strip().startswith("{") else {}
        check("an unshipped answer is refused (exit 6) with the nearest workflow",
              rc == bw.EXIT_REFUSED and any(
                  r["id"] == "ptm" and r["nearest"]["workflow"] == "LFQ-MBR"
                  for r in payload.get("rejected", [])), out[:300])
        check("... and stays in the plan until the user answers it again",
              fp.load_plan(fp.plan_path(wd))["answers"].get("ptm") == "acetyl")
        rc, out = _quiet(bw.main, base + ["--json", "--answer", "ptm=none",
                                          "--answer", "fasta=" + fasta,
                                          "--answer", "remove_contaminants=yes"])
        payload = json.loads(out)
        design = next((q for q in payload["open"] if q["id"] == "design"), None)
        check("the design is proposed from the file names",
              design is not None and [(r["condition"], r["replicate"]) for r in design["rows"]]
              == [("ctrl", "1"), ("ctrl", "2"), ("treat", "1")], str(design))
        with open(design["path"], "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(design["path"], "w", encoding="utf-8") as fh:
            fh.write(text.replace("\tctrl\t", "\tcontrol\t").replace("\ttreat\t1\t",
                                                                    "\ttreat\tB\t"))
        rc, out = _quiet(bw.main, base + ["--json"])
        again = next(q for q in json.loads(out)["open"] if q["id"] == "design")
        check("an edited proposal survives a call without --design, its mistakes shown",
              [r["condition"] for r in again["rows"]] == ["control", "control", "treat"]
              and any("'B'" in p for p in again["problems"]), str(again)[:300])
        open(os.path.join(data, "treat_2.mzML"), "w").close()
        rc, out = _quiet(bw.main, base + ["--json"])
        again = next(q for q in json.loads(out)["open"] if q["id"] == "design")
        check("a new raw file is merged into the edited proposal, the old one kept as .bak",
              [r["condition"] for r in again["rows"]] == ["control", "control", "treat",
                                                          "treat"]
              and os.path.isfile(design["path"] + ".bak"), str(again)[:300])
        with open(design["path"], "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(design["path"], "w", encoding="utf-8") as fh:
            fh.write(text.replace("\ttreat\tB\t", "\ttreat\t1\t"))
        rc, out = _quiet(bw.main, base + ["--design", design["path"]])
        check("a complete plan builds (exit 0)", rc == bw.EXIT_OK, out[-300:])
        plan = fp.load_plan(fp.plan_path(wd))
        wf, mf = plan["files"]["workflow"], plan["files"]["manifest"]
        changed = {c["key"]: c["value"] for c in plan["changes"]}
        check("only run-level settings change",
              changed == {"database.db-path": fasta,
                          "phi-report.remove-contaminants": "true"}, str(changed))
        check("the table names the base workflow and each change's source",
              "LFQ-MBR, shipped by FragPipe" in out and "answer: remove_contaminants" in out)
        check("an unconfirmed plan cannot launch",
              fp.check_lock(wf, mf)[0] == fp.LOCK_REFUSED, fp.check_lock(wf, mf)[1])
        rc, _ = _quiet(bw.main, base + ["--confirm"])
        check("--confirm succeeds", rc == bw.EXIT_OK, str(rc))
        check("a confirmed plan may launch", fp.check_lock(wf, mf)[0] == fp.LOCK_OK,
              fp.check_lock(wf, mf)[1])
        check("... with its manifest spelt otherwise", os.name != "nt"
              or fp.check_lock(wf, mf.upper())[0] == fp.LOCK_OK, fp.check_lock(wf, mf)[1])
        check("a hand-written workflow is never locked",
              fp.check_lock(os.path.join(install["root"], "workflows", "LFQ-MBR.workflow"),
                            mf)[0] == fp.LOCK_NONE)
        for path, label in ((mf, "manifest"), (wf, "workflow")):
            with open(path, "rb") as fh:
                original = fh.read()
            with open(path, "wb") as fh:
                fh.write(original.replace(b"label-free-quant=true", b"label-free-quant=false")
                         if path == wf else original + b"\n")
            check("a %s changed after confirmation is refused" % label,
                  fp.check_lock(wf, mf)[0] == fp.LOCK_REFUSED)
            if path == wf:
                with open(path, "wb") as fh:
                    fh.write(b"\xef\xbb\xbf" + original.replace(b"label-free-quant=true",
                                                               b"label-free-quant=false"))
                check("... also when the editor added a byte-order mark",
                      fp.check_lock(wf, mf)[0] == fp.LOCK_REFUSED, fp.check_lock(wf, mf)[1])
            with open(path, "wb") as fh:
                fh.write(original)
        check("put back, it may launch again", fp.check_lock(wf, mf)[0] == fp.LOCK_OK)
        check("it launches only on the FragPipe it was built from",
              fp.check_lock(wf, mf, "24.0")[0] == fp.LOCK_REFUSED
              and fp.check_lock(wf, mf, "24.1-build61")[0] == fp.LOCK_OK)
        check("... and never on a FragPipe whose version is unknown",
              fp.check_lock(wf, mf, None)[0] == fp.LOCK_REFUSED)
        moved = os.path.join(tmp, "copy.fp-manifest")
        with open(mf, "rb") as fh, open(moved, "wb") as out_fh:
            out_fh.write(fh.read())
        check("a copy of the confirmed manifest at another path is refused",
              fp.check_lock(wf, moved)[0] == fp.LOCK_REFUSED)
        with open(wf, "rb") as fh:
            original = fh.read()
        with open(wf, "wb") as fh:
            fh.write(original.decode("utf-8").encode("utf-16"))
        check("a workflow re-saved as UTF-16, which hides the marker, is still locked",
              fp.check_lock(wf, mf)[0] == fp.LOCK_REFUSED)
        with open(wf, "wb") as fh:
            fh.write(original)
        copy16 = os.path.join(os.path.dirname(wf), "edited.workflow")
        with open(copy16, "wb") as fh:
            fh.write(original.decode("utf-8").encode("utf-16"))
        status = fp.check_lock(copy16, mf)
        check("... and so is a UTF-16 copy beside the plan, and the workflow spelt otherwise",
              status[0] == fp.LOCK_REFUSED and "is not the workflow it confirmed" in status[1]
              and fp.is_builder_made(wf.upper() if os.name == "nt" else wf), str(status))
        os.remove(copy16)

        # the lock refuses a damaged plan with a reason, and an unknown version always
        with open(fp.plan_path(wd), "rb") as fh:
            saved_plan = fh.read()
        damaged = json.loads(saved_plan)
        damaged["confirmed"]["annotations"] = {"rel_annotation.txt": "00"}
        with open(fp.plan_path(wd), "w", encoding="utf-8") as fh:
            json.dump(damaged, fh)
        here = os.getcwd()
        os.chdir(tmp)
        try:
            open("rel_annotation.txt", "w").close()
            status = fp.check_lock(wf, mf)
        finally:
            os.chdir(here)
        check("a relative annotation path in the plan is read where it lies, not a traceback",
              status[0] == fp.LOCK_REFUSED and "changed after it was confirmed" in status[1],
              str(status))
        damaged = json.loads(saved_plan)
        damaged["confirmed"]["fragpipe_version"] = damaged["fragpipe"]["version"] = None
        with open(fp.plan_path(wd), "w", encoding="utf-8") as fh:
            json.dump(damaged, fh)
        check("a FragPipe of unknown version is refused even by a plan that knows none",
              fp.check_lock(wf, mf, None)[0] == fp.LOCK_REFUSED)
        with open(fp.plan_path(wd), "wb") as fh:
            fh.write(saved_plan)
        real_sha = fp.sha256_file

        def locked(path):
            if fp._same_path(path, mf):
                raise PermissionError(13, "Permission denied", path)
            return real_sha(path)
        fp.sha256_file = locked
        try:
            status = fp.check_lock(wf, mf)
        finally:
            fp.sha256_file = real_sha
        check("a confirmed file another program holds is refused, naming that file",
              status[0] == fp.LOCK_REFUSED and "cannot read %s" % mf in status[1], str(status))
        real_replace, err = os.replace, None

        def refuse(src, dst):
            raise PermissionError(13, "Access is denied", src, None, dst)
        os.replace = refuse
        try:
            fp.save_plan(os.path.join(tmp, "p.json"), {"schema": 1})
        except OSError as exc:
            err = exc
        finally:
            os.replace = real_replace
        check("a plan that cannot replace the old one leaves no .tmp behind",
              err is not None and not os.path.exists(os.path.join(tmp, "p.json.tmp")))

        import fragpipe_run as fr
        check("headless name rules from 24.1-build8 on (and for 24.1 or an unknown version)",
              [fr.has_name_rules(v) for v in ("24.0", "24.1-build7", "24.1-build8", "24.1",
                                              "25.0-build3", None)]
              == [False, False, True, True, True, True])
        hand16 = os.path.join(tmp, "hand16.workflow")
        with open(hand16, "wb") as fh:
            fh.write(_LFQ_TEXT.encode("utf-16"))
        rc, out = _quiet(fr.main, ["--workflow", hand16, "--manifest", mf, "--workdir",
                                   os.path.join(tmp, "o7"), "--fragpipe-dir", install["root"]])
        check("fragpipe_run.py refuses a workflow FragPipe would silently ignore (UTF-16)",
              rc == fr.EXIT_CONFIG_ERROR and "not UTF-8" in out, out[-300:])
        with open(hand16, "wb") as fh:
            fh.write(_LFQ_TEXT.encode("utf-16-le"))
        rc, out = _quiet(fr.main, ["--workflow", hand16, "--manifest", mf, "--workdir",
                                   os.path.join(tmp, "o7"), "--fragpipe-dir", install["root"]])
        check("... also UTF-16 with no byte-order mark, which decodes as UTF-8 with NULs",
              rc == fr.EXIT_CONFIG_ERROR and "not UTF-8" in out, out[-300:])
        with open(hand16, "w", encoding="utf-8", newline="") as fh:
            fh.write(_LFQ_TEXT + "database.db-path=D:\\db\\uniprot.fasta\r\n")
        rc, out = _quiet(fr.main, ["--workflow", hand16, "--manifest", mf, "--workdir",
                                   os.path.join(tmp, "o7"), "--fragpipe-dir", install["root"]])
        check("fragpipe_run.py refuses a workflow with a \\u Java cannot read (exit 2)",
              rc == fr.EXIT_CONFIG_ERROR and "4 hex digits" in out, out[-300:])
        check("... read as Java reads it: comments skipped, \\\\ escaped, lines continued",
              [fr.malformed_unicode_escapes(t) for t in (
                  "a=1\nb=D:\\db\\uniprot.fasta\n", "# D:\\db\\uniprot\n! \\uZZ\n"
                  "b=D:\\\\db\\\\uniprot\nc=\\u00e9\n", "a=x\\\n  \\uZZ\n")]
              == [[2], [], [1]], str([fr.malformed_unicode_escapes("a=x\\\n  \\uZZ\n")]))
        import io
        for label, fn, argv in (
                ("fragpipe_run.py", fr.main, ["--workflow", os.path.join(tmp, "none.workflow"),
                                              "--manifest", mf, "--workdir", wd,
                                              "--fragpipe-dir", install["root"]]),
                ("build_workflow.py", bw.main, base + ["--show"])):
            console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
            real_stdout, real_stderr = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = console, io.StringIO()
            try:
                fn(argv)
            finally:
                sys.stdout, sys.stderr = real_stdout, real_stderr
            check("%s prints what the console cannot show as escapes, never a crash" % label,
                  console.errors == "backslashreplace", console.errors)
        old = _fake_install(os.path.join(tmp, "FragPipe-24.0"), "24.0", {"LFQ-MBR": _LFQ_TEXT})
        rc, out = _quiet(fr.main, ["--workflow", wf, "--manifest", mf, "--workdir", wd,
                                   "--fragpipe-dir", old["root"]])
        check("fragpipe_run.py stops a refused plan with exit 5, before launching",
              rc == fr.EXIT_PLAN_NOT_CONFIRMED and "STOP" in out, out[-300:])
        rc, out = _quiet(bw.main, base + ["--confirm", "--fragpipe-dir", old["root"]])
        check("--confirm and --show use the plan's FragPipe, never another one",
              rc == bw.EXIT_CONFIG_ERROR and "built from" in out
              and _quiet(bw.main, base + ["--show", "--fragpipe-dir", install["root"]])[0]
              == bw.EXIT_OK, out[-300:])
        check("... named by its root, its bin folder or its launcher",
              _quiet(bw.main, base + ["--show", "--fragpipe-dir",
                                      os.path.join(install["root"], "bin")])[0] == bw.EXIT_OK
              and _quiet(bw.main, base + ["--show", "--fragpipe-dir",
                                          install["launcher"]])[0] == bw.EXIT_OK)
        import io
        import types
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="backslashreplace")
        real_stdout, sys.stdout = sys.stdout, console
        try:
            bw._emit(types.SimpleNamespace(json=False), {}, "workdir: C:\\out_\u4e2d")
        finally:
            sys.stdout = real_stdout
        console.flush()
        shown = console.buffer.getvalue().decode("cp1252")
        check("a table the console cannot show says so and points to --json",
              shown.startswith("NOTE") and "--json" in shown, shown)
        bad = os.path.join(tmp, "bad.fp-manifest")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("%s\tctrl\t1\tDIA_Quant\n" % os.path.join(data, "ctrl_1.mzML"))
        rc, out = _quiet(fr.main, ["--workflow", os.path.join(install["root"], "workflows",
                                                              "LFQ-MBR.workflow"),
                                   "--manifest", bad, "--workdir", os.path.join(tmp, "o4"),
                                   "--fragpipe-dir", install["root"]])
        check("fragpipe_run.py refuses a manifest data type FragPipe does not know (exit 2)",
              rc == fr.EXIT_CONFIG_ERROR and "DIA_Quant" in out, out[-300:])
        rc, _ = _quiet(bw.main, base + ["--confirm", "--answer", "remove_contaminants=no"])
        check("--confirm with a new answer is refused; the answer is never confirmed unseen",
              rc == bw.EXIT_CONFIG_ERROR
              and fp.load_plan(fp.plan_path(wd))["answers"]["remove_contaminants"] == "yes")
        rc, _ = _quiet(bw.main, base)
        check("a rebuild clears the confirmation",
              rc == bw.EXIT_OK and fp.check_lock(wf, mf)[0] == fp.LOCK_REFUSED, str(rc))
        _quiet(bw.main, base + ["--answer", "remove_contaminants=no"])
        rc, out = _quiet(bw.main, base + ["--show"])
        answers_part = out.split("Parameters changed")[0]
        check("the table shows every run-level answer, even one equal to FragPipe's",
              re.search(r"^\s+remove_contaminants\s", answers_part, re.M)
              and re.search(r"^\s+fasta\s", answers_part, re.M)
              and "phi-report.remove-contaminants" not in out, out[:600])
        with open(fp.plan_path(wd), "w", encoding="utf-8") as fh:
            fh.write('{"state": "confirmed",}')
        check("an unreadable plan refuses the launch with a reason, not a traceback",
              fp.check_lock(wf, mf)[0] == fp.LOCK_REFUSED
              and _quiet(bw.main, base + ["--show"])[0] == bw.EXIT_CONFIG_ERROR)
        rc, _ = _quiet(bw.main, ["--workdir", os.path.join(tmp, "o2"), "--fragpipe-dir",
                                 old["root"], "--data", data])
        check("an unsupported FragPipe version is refused (exit 6)", rc == bw.EXIT_REFUSED,
              str(rc))

        # changing the acquisition after the design was accepted proposes it again
        wd3 = os.path.join(tmp, "o3")
        argv = ["--workdir", wd3, "--fragpipe-dir", install["root"], "--data", data]
        for a in ("analysis=standard", "acquisition=DDA", "search=closed", "quant=LFQ",
                  "ptm=none", "fasta=" + fasta, "remove_contaminants=no"):
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet3 = os.path.join(fp.repro_dir(wd3), fp.DESIGN_NAME)
        rc, _ = _quiet(bw.main, ["--workdir", wd3, "--design", sheet3])
        rc, out = _quiet(bw.main, ["--workdir", wd3, "--json", "--answer", "acquisition=DIA",
                                   "--answer", "dia_format=thermo_sciex"])
        again = next((q for q in json.loads(out).get("open", []) if q["id"] == "design"), {})
        check("an accepted design is proposed again when the workflow's data types change",
              rc == bw.EXIT_QUESTIONS and again.get("rows")
              and all(r["data_type"] == "DIA" for r in again["rows"])
              and again["rows"][0]["condition"] == "ctrl", out[-400:])

        # ... also when it was accepted on a call that left the FASTA open
        wd4 = os.path.join(tmp, "o5")
        argv = ["--workdir", wd4, "--fragpipe-dir", install["root"], "--data", data]
        for a in ("analysis=standard", "acquisition=DDA", "search=closed", "quant=LFQ",
                  "ptm=none", "remove_contaminants=no"):
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet4 = os.path.join(fp.repro_dir(wd4), fp.DESIGN_NAME)
        rc, _ = _quiet(bw.main, ["--workdir", wd4, "--design", sheet4])
        rc2, out = _quiet(bw.main, ["--workdir", wd4, "--json", "--answer", "acquisition=DIA",
                                    "--answer", "dia_format=thermo_sciex",
                                    "--answer", "fasta=" + fasta])
        again = next((q for q in json.loads(out).get("open", []) if q["id"] == "design"), {})
        check("... also when it was accepted while the FASTA was still open",
              rc == bw.EXIT_QUESTIONS and rc2 == bw.EXIT_QUESTIONS and again.get("rows")
              and all(r["data_type"] == "DIA" for r in again["rows"]), out[-400:])

        # a design passed with the change is the user's new sheet, not the old one
        mine = os.path.join(tmp, "dia_design.tsv")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n")
            for n, t in (("ctrl_1", "DIA"), ("ctrl_2", "DIA"), ("treat_1", "DIA-Quant")):
                fh.write("%s\t%s\t%s\t%s\n" % (os.path.join(data, n + ".mzML"),
                                               n.split("_")[0], n.split("_")[1], t))
        rc, out = _quiet(bw.main, ["--workdir", wd3, "--design", mine, "--answer",
                                   "fasta=" + fasta])
        plan3 = fp.load_plan(fp.plan_path(wd3))
        check("a design passed in the same call as a workflow change is used as given",
              rc == bw.EXIT_OK and "DIA-Quant" in {i["data_type"] for i in plan3["inputs"]},
              out[-300:])

        # an accepted design follows --data; rows taken out of a proposal stay out
        wd5 = os.path.join(tmp, "o6")
        data2 = os.path.join(tmp, "data2")
        os.makedirs(data2)
        for n in ("wt_1", "wt_2", "ko_1", "blank_1"):
            open(os.path.join(data2, n + ".mzML"), "w").close()
        argv = ["--workdir", wd5, "--fragpipe-dir", install["root"], "--data", data]
        for a in ("analysis=standard", "acquisition=DDA", "search=closed", "quant=LFQ",
                  "ptm=none", "fasta=" + fasta, "remove_contaminants=no"):
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet5 = os.path.join(fp.repro_dir(wd5), fp.DESIGN_NAME)
        rc, _ = _quiet(bw.main, ["--workdir", wd5, "--design", sheet5])
        rc2, out = _quiet(bw.main, ["--workdir", wd5, "--json", "--data", data2])
        again = next((q for q in json.loads(out).get("open", []) if q["id"] == "design"), {})
        check("an accepted design is proposed again from new --data folders",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_QUESTIONS and len(again.get("rows", [])) == 4
              and all(r["file"].startswith(data2) for r in again["rows"]), out[-400:])
        with open(sheet5, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines(True)
        with open(sheet5, "w", encoding="utf-8") as fh:
            fh.write("".join(l for l in lines if "blank_1" not in l))
        open(os.path.join(data2, "ko_2.mzML"), "w").close()
        rc, out = _quiet(bw.main, ["--workdir", wd5, "--json"])
        again = next((q for q in json.loads(out).get("open", []) if q["id"] == "design"), {})
        names = sorted(os.path.basename(r["file"]) for r in again.get("rows", []))
        check("a row the user took out stays out when another file appears",
              names == ["ko_1.mzML", "ko_2.mzML", "wt_1.mzML", "wt_2.mzML"]
              and any("stay out" in n for n in again.get("notes", [])), str(names))
        rc, _ = _quiet(bw.main, ["--workdir", wd5, "--design", sheet5])
        os.remove(sheet5)
        rc2, out = _quiet(bw.main, ["--workdir", wd5, "--json"])
        check("an accepted sheet that is gone is proposed again, not a traceback",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_QUESTIONS and "is gone" in out, out[-300:])

        # what each sheet was made for: the workflow's data types, the folders, the files
        lfq = ("analysis=standard", "acquisition=DDA", "search=closed", "quant=LFQ",
               "ptm=none", "fasta=" + fasta, "remove_contaminants=no")
        dia = ("acquisition=DIA", "dia_format=thermo_sciex")

        def start(name, folders, *extra):
            w = os.path.join(tmp, name)
            argv = ["--workdir", w, "--fragpipe-dir", install["root"], "--data"] + folders
            for a in lfq + extra:
                argv += ["--answer", a]
            _quiet(bw.main, argv)
            return w, os.path.join(fp.repro_dir(w), fp.DESIGN_NAME)

        def proposed(out):
            q = next((q for q in json.loads(out).get("open", []) if q["id"] == "design"), {})
            return q, sorted(os.path.basename(r["file"]) for r in q.get("rows", []))

        def drop_row(sheet, name):
            with open(sheet, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines(True)
            with open(sheet, "w", encoding="utf-8") as fh:
                fh.write("".join(l for l in lines if name not in l))

        w, s = start("r1", [data])
        with open(s, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(s, "w", encoding="utf-8") as fh:      # the user's library run
            fh.write(text.replace("\tDDA\n", "\tGPF-DIA\n", 1))
        _quiet(bw.main, ["--workdir", w, "--answer", "acquisition=DIA"])
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer",
                                   "dia_format=thermo_sciex", "--design", s])
        again, _ = proposed(out)
        types = sorted(r["data_type"] for r in again.get("rows", []))
        check("a DDA proposal accepted on the call that settles a DIA workflow is proposed "
              "again, a data type the user typed kept", rc == bw.EXIT_QUESTIONS
              and types == ["DIA", "DIA", "DIA", "GPF-DIA"]
              and any("data_type was set to DIA for 3 file(s)" in n
                      for n in again.get("notes", [])), out[-400:])

        w, s = start("r1b", [data], *dia)
        mine_dda = os.path.join(tmp, "all_dda.tsv")
        with open(mine_dda, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n" + "".join(
                "%s\t%s\t%s\tDDA\n" % (os.path.join(data, n + ".mzML"), n[:-2], n[-1])
                for n in ("ctrl_1", "ctrl_2", "treat_1", "treat_2")))
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--design", mine_dda])
        again, _ = proposed(out)
        check("a sheet with no DIA run is not built for a workflow that quantifies DIA",
              rc == bw.EXIT_QUESTIONS
              and any("no file is typed DIA" in p for p in again.get("problems", [])),
              out[-400:])

        w, s = start("r3b", [data])
        with open(s, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(s, "w", encoding="utf-8") as fh:      # rows as the text table shows them
            fh.write(re.sub(r"^.*[\\/](treat_\d\.mzML)\ttreat\t", r"\1\tdrug\t", text,
                            flags=re.M))
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer", dia[0], "--answer",
                                   dia[1]])
        again, names = proposed(out)
        check("rows rewritten with bare file names are still those files' rows",
              len(names) == 4 and sorted(r["condition"] for r in again["rows"])
              == ["ctrl", "ctrl", "drug", "drug"]
              and not any("stay out" in n for n in again.get("notes", [])), out[-400:])

        w, s = start("r3", [data])
        os.remove(s)
        rc, out = _quiet(bw.main, ["--workdir", w, "--json"])
        again, names = proposed(out)
        check("a deleted proposal is proposed again with every file",
              rc == bw.EXIT_QUESTIONS and len(names) == 4
              and not any("stay out" in n for n in again.get("notes", [])), out[-400:])

        w, s = start("r4", [data])
        drop_row(s, "treat_2")
        rc, _ = _quiet(bw.main, ["--workdir", w, "--design", s])
        same = (data.upper() if os.name == "nt" else data) + os.sep
        rc2, out = _quiet(bw.main, ["--workdir", w, "--data", same])
        check("the same data folder spelt otherwise keeps the accepted design",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_OK, out[-300:])
        more = os.path.join(tmp, "more")
        os.makedirs(more)
        open(os.path.join(more, "extra_1.mzML"), "w").close()
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", data, more])
        again, names = proposed(out)
        check("an accepted design proposed again for more folders keeps a row taken out out",
              rc == bw.EXIT_QUESTIONS and names == ["ctrl_1.mzML", "ctrl_2.mzML",
                                                    "extra_1.mzML", "treat_1.mzML"]
              and any("stay out: treat_2.mzML" in n for n in again.get("notes", [])),
              out[-400:])

        w, s = start("r5", [data])
        rc, _ = _quiet(bw.main, ["--workdir", w, "--design", s])
        mine5 = os.path.join(tmp, "r5_design.tsv")
        with open(s, "rb") as fh, open(mine5, "wb") as out_fh:
            out_fh.write(fh.read())
        rc2, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", data2,
                                    "--design", mine5])
        again, _ = proposed(out)
        check("a sheet passed with new --data, naming files outside them, is not built",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_QUESTIONS
              and any("not under the --data folders" in p for p in again.get("problems", [])),
              out[-400:])

        w, s = start("r6", [data])
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer", dia[0], "--answer",
                                   dia[1]])
        again, _ = proposed(out)
        check("a proposal whose data types change says so",
              rc == bw.EXIT_QUESTIONS and all(r["data_type"] == "DIA" for r in again["rows"])
              and any("data_type was set to DIA" in n for n in again.get("notes", [])),
              out[-400:])

        data3 = os.path.join(tmp, "data3")
        os.makedirs(data3)
        for n in ("a_1", "a_2"):
            open(os.path.join(data3, n + ".mzML"), "w").close()
        w, s = start("r7", [data3], *dia)
        with open(s, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(s, "w", encoding="utf-8") as fh:
            fh.write(text.replace("\tDIA\n", "\tGPF_DIA\n", 1))
        open(os.path.join(data3, "a_3.mzML"), "w").close()
        rc, out = _quiet(bw.main, ["--workdir", w, "--json"])
        again, names = proposed(out)
        check("a mistyped data type survives a re-proposal, its problem still shown",
              len(names) == 3 and any("GPF_DIA" in p for p in again.get("problems", [])),
              out[-400:])

        w, s = start("r8", [data3])
        rc, _ = _quiet(bw.main, ["--workdir", w, "--design", s])
        open(os.path.join(data3, "a_4.mzML"), "w").close()
        rc2, out = _quiet(bw.main, ["--workdir", w, "--json"])
        again, names = proposed(out)
        check("an accepted design is proposed again when a raw file appears",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_QUESTIONS and "a_4.mzML" in names
              and any("added to or removed" in n for n in again.get("notes", [])), out[-400:])

        w, s = start("r9", [data3])
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", data2])
        again, _ = proposed(out)
        check("a proposal for other --data folders says the folders changed",
              any("data folders changed" in n for n in again.get("notes", []))
              and not any("added to and" in n for n in again.get("notes", [])), out[-400:])

        import stat
        w, s = start("r11", [data3])
        os.chmod(s, stat.S_IREAD)
        try:
            rc, out = _quiet(bw.main, ["--workdir", w, "--answer", dia[0], "--answer", dia[1],
                                       "--answer", "tmt_plex=10"])
        finally:
            os.chmod(s, stat.S_IREAD | stat.S_IWRITE)
        kept = fp.load_plan(fp.plan_path(w))["answers"]
        check("a proposal that cannot be rewritten is exit 2, the call's answers kept",
              rc == bw.EXIT_CONFIG_ERROR and "cannot write" in out
              and kept.get("dia_format") == "thermo_sciex", out[-400:])
        check("... and an answer the others made irrelevant is reported DROPPED",
              "DROPPED tmt_plex" in out, out[-400:])

        # an accepted sheet survives a call that stops on the way
        data4 = os.path.join(tmp, "data4")
        os.makedirs(data4)
        for n in ("b_1", "b_2"):
            open(os.path.join(data4, n + ".mzML"), "w").close()
        w, s = start("r13", [data4])
        mine13 = os.path.join(tmp, "r13_design.tsv")
        with open(s, "r", encoding="utf-8") as fh, open(mine13, "w", encoding="utf-8") as o:
            o.write(fh.read().replace("\tb\t", "\tMYb\t"))
        rc, _ = _quiet(bw.main, ["--workdir", w, "--design", mine13])
        os.rename(data4, data4 + "_away")
        rc2, _ = _quiet(bw.main, ["--workdir", w])
        os.rename(data4 + "_away", data4)
        rc3, _ = _quiet(bw.main, ["--workdir", w])
        check("a data folder that is briefly away keeps the accepted design",
              (rc, rc2, rc3) == (bw.EXIT_OK, bw.EXIT_QUESTIONS, bw.EXIT_OK)
              and fp.load_plan(fp.plan_path(w))["design"] == mine13, str((rc, rc2, rc3)))
        os.chmod(s, stat.S_IREAD)
        try:
            rc, out = _quiet(bw.main, ["--workdir", w, "--answer", dia[0], "--answer", dia[1]])
        finally:
            os.chmod(s, stat.S_IREAD | stat.S_IWRITE)
        check("a re-proposal that cannot be written keeps the accepted sheet bound",
              rc == bw.EXIT_CONFIG_ERROR
              and fp.load_plan(fp.plan_path(w))["design"] == mine13, out[-300:])
        rc, out = _quiet(bw.main, ["--workdir", w, "--json"])
        again, _ = proposed(out)
        check("... and its edits reach the new proposal once it can be written",
              rc == bw.EXIT_QUESTIONS and [r["condition"] for r in again.get("rows", [])]
              == ["MYb", "MYb"], out[-300:])

        # a design given without --data settles: its own rows are the files
        w = os.path.join(tmp, "r12")
        argv = ["--workdir", w, "--fragpipe-dir", install["root"]]
        for a in lfq:
            argv += ["--answer", a]
        rc, _ = _quiet(bw.main, argv + ["--design", mine13])
        rc2, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer", dia[0], "--answer",
                                    dia[1]])
        again, names = proposed(out)
        rc3, out3 = _quiet(bw.main, ["--workdir", w, "--json"])
        rc4, out4 = _quiet(bw.main, ["--workdir", w, "--design", again.get("path", "")])
        check("a design given without --data is proposed again from its rows and accepted",
              (rc, rc2, rc3, rc4) == (bw.EXIT_OK, bw.EXIT_QUESTIONS, bw.EXIT_QUESTIONS,
                                      bw.EXIT_OK) and names == ["b_1.mzML", "b_2.mzML"]
              and proposed(out3)[1] == names, "%s %s" % ((rc, rc2, rc3, rc4), out4[-300:]))

        # an own sheet given without --data names its files: once --data is given, the
        # folders' other files stay out
        mine14 = os.path.join(tmp, "r14_design.tsv")
        with open(mine13, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines(True)
        with open(mine14, "w", encoding="utf-8") as fh:
            fh.write("".join(line for line in lines if "b_2.mzML" not in line))
        w = os.path.join(tmp, "r14")
        argv = ["--workdir", w, "--fragpipe-dir", install["root"]]
        for a in lfq:
            argv += ["--answer", a]
        rc, _ = _quiet(bw.main, argv + ["--design", mine14])
        rc2, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", data4])
        again, names = proposed(out)
        check("an own sheet given without --data keeps out the folder files it does not list "
              "once --data is given", (rc, rc2) == (bw.EXIT_OK, bw.EXIT_QUESTIONS)
              and names == ["b_1.mzML"]
              and [r["condition"] for r in again.get("rows", [])] == ["MYb"]
              and any("not in the design sheet accepted earlier" in n and "b_2.mzML" in n
                      for n in again.get("notes", [])), out[-400:])
        rc3, out = _quiet(bw.main, ["--workdir", w, "--json"])
        check("... and on the calls after it", rc3 == bw.EXIT_QUESTIONS
              and proposed(out)[1] == ["b_1.mzML"], out[-300:])

        # FragPipe's file and folder name rules follow the build: 24.1-build7 loads any name
        old7 = _fake_install(os.path.join(tmp, "FragPipe-24.1-build7"), "24.1-build7",
                             {"LFQ-MBR": _LFQ_TEXT})
        dots = os.path.join(tmp, "data dots")
        os.makedirs(dots)
        open(os.path.join(dots, "s.rep1.mzML"), "w").close()
        found = []
        for inst in (old7, install):
            w = os.path.join(tmp, "r15_" + inst["version"])
            argv = ["--workdir", w, "--fragpipe-dir", inst["root"], "--json", "--data", dots]
            for a in lfq:
                argv += ["--answer", a]
            rc, out = _quiet(bw.main, argv)
            found.append(proposed(out)[0].get("problems", []))
        check("the builder applies FragPipe's name rules only from 24.1-build8 on",
              not found[0] and any("multiple dots" in p for p in found[1])
              and any("data dots" in p for p in found[1]), str(found))

        def lfq_call(w, *extra):
            argv = ["--workdir", w, "--fragpipe-dir", install["root"]]
            for a in lfq:
                argv += ["--answer", a]
            return _quiet(bw.main, argv + list(extra))

        def edit(sheet, *pairs):
            with open(sheet, "r", encoding="utf-8") as fh:
                text = fh.read()
            for old_text, new_text in pairs:
                text = re.sub(old_text, new_text, text, flags=re.M)
            with open(sheet, "w", encoding="utf-8") as fh:
                fh.write(text)

        # a data folder inside the workdir holds the user's files, not FragPipe's
        w = os.path.join(tmp, "r16")
        inner = os.path.join(w, "raw")
        os.makedirs(inner)
        for n in ("c_1", "c_2"):
            open(os.path.join(inner, n + ".mzML"), "w").close()
        rc, out = lfq_call(w, "--json", "--data", inner)
        check("a data folder inside the workdir is proposed, not 'no spectral files'",
              rc == bw.EXIT_QUESTIONS and proposed(out)[1] == ["c_1.mzML", "c_2.mzML"],
              out[-300:])

        # one of two data folders briefly away: the call stops and keeps the design
        d_a, d_b = os.path.join(tmp, "dA"), os.path.join(tmp, "dB")
        for d, n in ((d_a, "x_1"), (d_b, "y_1")):
            os.makedirs(d)
            open(os.path.join(d, n + ".mzML"), "w").close()
        w17, s = start("r17", [d_a, d_b])
        edit(s, (r"\tx\t", "\tWT\t"))
        rc, _ = _quiet(bw.main, ["--workdir", w17, "--design", s])
        os.rename(d_b, d_b + "_away")
        try:
            rc2, out2 = _quiet(bw.main, ["--workdir", w17, "--json"])
        finally:
            os.rename(d_b + "_away", d_b)
        rc3, out3 = _quiet(bw.main, ["--workdir", w17])
        check("one of two data folders briefly away keeps the accepted design and its edits",
              (rc, rc2, rc3) == (bw.EXIT_OK, bw.EXIT_QUESTIONS, bw.EXIT_OK)
              and "No such folder" in out2 and fp.load_plan(fp.plan_path(w17))["design"] == s
              and "WT" in {i["experiment"] for i in fp.load_plan(fp.plan_path(w17))["inputs"]},
              out2[-300:] + out3[-300:])

        # a copied workdir: its plan is the other folder's until it is rebuilt here
        import shutil
        w17c = w17 + "_copy"
        shutil.copytree(w17, w17c)
        rc, out = _quiet(bw.main, ["--workdir", w17c, "--show"])
        rc2, out2 = _quiet(bw.main, ["--workdir", w17c])
        plan_c = fp.load_plan(fp.plan_path(w17c))
        check("a copied workdir's plan is refused by --show, and a rebuild there uses its own "
              "copy of the design", rc == bw.EXIT_CONFIG_ERROR and "was built in" in out
              and rc2 == bw.EXIT_OK and fp._same_path(plan_c["workdir"], w17c)
              and bw._inside(plan_c["design"], fp.repro_dir(w17c))
              and bw._inside(plan_c["files"]["workflow"], fp.repro_dir(w17c)),
              out[-300:] + out2[-300:])
        rc, out = _quiet(bw.main, ["--workdir", os.path.join(tmp, "nowhere"), "--show"])
        check("--show where there is no plan says what --workdir is",
              rc == bw.EXIT_CONFIG_ERROR and "no plan at" in out, out[-300:])
        broken = fp.load_plan(fp.plan_path(w17c))
        del broken["changes"]
        fp.save_plan(fp.plan_path(w17c), broken)
        rc, out = _quiet(bw.main, ["--workdir", w17c, "--show"])
        check("a plan edited by hand into one --show cannot read is exit 2, not a traceback",
              rc == bw.EXIT_CONFIG_ERROR and "damaged" in out and "changes" in out, out[-300:])
        broken["state"] = fp.STATE_QUESTIONS
        fp.save_plan(fp.plan_path(w17c), broken)
        status = fp.check_lock(os.path.join(fp.repro_dir(w17c), fp.WORKFLOW_NAME),
                               os.path.join(fp.repro_dir(w17c), fp.MANIFEST_NAME))
        check("a plan with open questions again says so at launch, and how to list them",
              status[0] == fp.LOCK_REFUSED and "open questions again" in status[1]
              and "--workdir" in status[1], str(status))

        # a merge never reads a sheet that is not UTF-8 (the user's names would be lost)
        w, s = start("r18", [data4])
        with open(s, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(s, "w", encoding="cp1252") as fh:
            fh.write(text.replace("\tb\t", "\tKäse\t"))
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer", dia[0], "--answer",
                                   dia[1]])
        with open(s, "rb") as fh:
            still = fh.read()
        check("a sheet that is not UTF-8 is not merged into a new proposal",
              rc == bw.EXIT_QUESTIONS and "not UTF-8" in out and b"K\xe4se" in still,
              out[-300:])

        # an own sheet given without --data names its files, also when the workflow
        # changed before --data was given
        w = os.path.join(tmp, "r19")
        rc, _ = lfq_call(w, "--design", mine14)
        rc2, _ = _quiet(bw.main, ["--workdir", w, "--answer", dia[0], "--answer", dia[1]])
        rc3, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", data4])
        again, names = proposed(out)
        check("... also when the workflow changed before --data was given",
              (rc, rc2, rc3) == (bw.EXIT_OK, bw.EXIT_QUESTIONS, bw.EXIT_QUESTIONS)
              and names == ["b_1.mzML"]
              and any("not in the design sheet made earlier" in n and "b_2.mzML" in n
                      for n in again.get("notes", [])), out[-400:])

        # an own sheet that does not fit the workflow is not bound to it
        data5 = os.path.join(tmp, "data5")
        os.makedirs(data5)
        for n in ("a_1", "a_2", "lib_1"):
            open(os.path.join(data5, n + ".mzML"), "w").close()
        mixed = os.path.join(tmp, "r20_design.tsv")
        with open(mixed, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n" + "".join(
                "%s\t%s\t%s\t%s\n" % (os.path.join(data5, n + ".mzML"), c, r, t)
                for n, c, r, t in (("a_1", "A", 1, "DIA"), ("a_2", "A", 2, "DIA"),
                                   ("lib_1", "L", 1, "DDA"))))
        w = os.path.join(tmp, "r20")
        rc, _ = lfq_call(w, "--data", data5, "--design", mixed)
        rc2, out = _quiet(bw.main, ["--workdir", w, "--answer", dia[0], "--answer", dia[1]])
        types = {os.path.basename(i["file"]): i["data_type"]
                 for i in (fp.load_plan(fp.plan_path(w)).get("inputs") or [])}
        check("an own sheet refused by a DDA workflow is accepted as written once the "
              "workflow is DIA", (rc, rc2) == (bw.EXIT_QUESTIONS, bw.EXIT_OK)
              and types == {"a_1.mzML": "DIA", "a_2.mzML": "DIA", "lib_1.mzML": "DDA"},
              out[-300:])

        # a data type the user typed in a proposal is theirs across workflow changes
        w, s = start("r21", [data5], *dia)
        edit(s, (r"(lib_1\.mzML\t[^\t]*\t[^\t]*\t)DIA$", r"\1DDA"))
        _quiet(bw.main, ["--workdir", w, "--answer", "acquisition=DDA"])
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer", dia[0], "--answer",
                                   dia[1]])
        again, _ = proposed(out)
        types = {os.path.basename(r["file"]): r["data_type"] for r in again.get("rows", [])}
        check("a data type the user typed survives DIA -> DDA -> DIA",
              types == {"a_1.mzML": "DIA", "a_2.mzML": "DIA", "lib_1.mzML": "DDA"}, out[-400:])
        w, s = start("r22", [data5], *dia)
        edit(s, (r"(lib_1\.mzML\t[^\t]*\t[^\t]*\t)DIA$", r"\1DIA Lib"))
        rc, out = _quiet(bw.main, ["--workdir", w, "--json", "--answer", "acquisition=DDA"])
        again, _ = proposed(out)
        check("... and a type the builder cannot read is named when it is reset",
              any("data_type was set" in n and "lib_1.mzML" in n
                  for n in again.get("notes", [])), out[-400:])

        # a moved data folder: the rows follow their files, and so does a row taken out
        old_dir, new_dir = os.path.join(tmp, "r23_old"), os.path.join(tmp, "r23_new")
        os.makedirs(old_dir)
        for n in ("wt_1", "wt_2", "blank_1"):
            open(os.path.join(old_dir, n + ".mzML"), "w").close()
        w, s = start("r23", [old_dir])
        edit(s, (r"\twt\t", "\twildtype\t"))
        drop_row(s, "blank_1")
        rc, _ = _quiet(bw.main, ["--workdir", w, "--design", s])
        os.rename(old_dir, new_dir)
        rc2, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", new_dir])
        again, names = proposed(out)
        check("a moved data folder keeps a row taken out out, as the rows keep their edits",
              (rc, rc2) == (bw.EXIT_OK, bw.EXIT_QUESTIONS)
              and names == ["wt_1.mzML", "wt_2.mzML"]
              and [r["condition"] for r in again["rows"]] == ["wildtype", "wildtype"]
              and any("stay out: blank_1.mzML" in n for n in again.get("notes", [])),
              out[-400:])

        # rows of an own sheet for files outside the --data given later are named
        b1, b2 = os.path.join(tmp, "batch1"), os.path.join(tmp, "batch2")
        own = os.path.join(tmp, "r24_design.tsv")
        with open(own, "w", encoding="utf-8") as fh:
            fh.write("file\tcondition\treplicate\tdata_type\n")
            for d, n, c, r in ((b1, "a_1", "A", 1), (b1, "a_2", "A", 2), (b2, "c_1", "C", 1)):
                os.makedirs(d, exist_ok=True)
                open(os.path.join(d, n + ".mzML"), "w").close()
                fh.write("%s\t%s\t%s\tDDA\n" % (os.path.join(d, n + ".mzML"), c, r))
        w = os.path.join(tmp, "r24")
        rc, _ = lfq_call(w, "--design", own)
        rc2, out = _quiet(bw.main, ["--workdir", w, "--json", "--data", b1])
        again, names = proposed(out)
        check("rows of an own sheet left out by a later --data are named",
              (rc, rc2) == (bw.EXIT_OK, bw.EXIT_QUESTIONS) and names == ["a_1.mzML", "a_2.mzML"]
              and any("left out: c_1.mzML (C)" in n for n in again.get("notes", [])),
              out[-400:])

        # a FragPipe build whose workflow quantifies otherwise than its route is refused
        drift = _fake_install(os.path.join(tmp, "FragPipe-drift"), "24.1-build61",
                              {"LFQ-MBR": _LFQ_TEXT.replace("label-free-quant=true",
                                                            "label-free-quant=false")})
        argv = ["--workdir", os.path.join(tmp, "r25"), "--fragpipe-dir", drift["root"],
                "--data", data]
        for a in lfq:
            argv += ["--answer", a]
        rc, out = _quiet(bw.main, argv)
        check("a shipped workflow that does not quantify as its route says is refused (exit 6)",
              rc == bw.EXIT_REFUSED and "quantifies nothing" in out, out[-300:])

        # an empty or unreadable data folder beside others is away too: the design stays
        w = os.path.join(tmp, "r26")
        rc, _ = lfq_call(w, "--data", d_a, d_b)
        s = os.path.join(fp.repro_dir(w), fp.DESIGN_NAME)
        edit(s, (r"\ty\t", "\tDrugX\t"))
        rc, _ = _quiet(bw.main, ["--workdir", w, "--design", s])
        moved_away = os.path.join(tmp, "y_1_away.mzML")
        os.rename(os.path.join(d_b, "y_1.mzML"), moved_away)
        try:
            rc2, out2 = _quiet(bw.main, ["--workdir", w, "--json"])
        finally:
            os.rename(moved_away, os.path.join(d_b, "y_1.mzML"))
        real_listdir = os.listdir

        def unreadable(path="."):
            if fp._same_path(path, d_b):
                raise PermissionError(13, "Permission denied", path)
            return real_listdir(path)
        os.listdir = unreadable
        try:
            rc3, out3 = _quiet(bw.main, ["--workdir", w, "--json"])
        finally:
            os.listdir = real_listdir
        rc4, _ = _quiet(bw.main, ["--workdir", w])
        check("a data folder that is empty or unreadable beside another keeps the accepted design",
              (rc, rc2, rc3, rc4) == (bw.EXIT_OK, bw.EXIT_QUESTIONS, bw.EXIT_QUESTIONS, bw.EXIT_OK)
              and "No spectral files under %s" % json.dumps(d_b)[1:-1] in out2
              and "Cannot read" in out3
              and fp.load_plan(fp.plan_path(w))["design"] == s, out2[-300:] + out3[-300:])

        # inputs that differ only in extension write one .pepXML: refused in the proposal
        stems = os.path.join(tmp, "stems")
        os.makedirs(stems)
        for n in ("k_1.raw", "k_1.mzML", "k_2.mzML"):
            open(os.path.join(stems, n), "w").close()
        rc, out = lfq_call(os.path.join(tmp, "r27"), "--json", "--data", stems)
        check("a proposal with ctrl_1.raw beside ctrl_1.mzML says one search would overwrite "
              "the other", rc == bw.EXIT_QUESTIONS
              and any("differ only in extension" in p for p in proposed(out)[0]["problems"]),
              out[-300:])

        # a row for a file the walk does not collect (.mzBIN) survives a re-proposal
        w, s = start("r28", [data5])
        with open(os.path.join(data5, "lib_2.mzBIN"), "w") as fh:
            fh.write("x")
        with open(s, "a", encoding="utf-8") as fh:
            fh.write("%s\tlibrary\t2\tDDA\n" % os.path.join(data5, "lib_2.mzBIN"))
        open(os.path.join(data5, "a_3.mzML"), "w").close()
        try:
            rc, out = _quiet(bw.main, ["--workdir", w, "--json"])
        finally:
            os.remove(os.path.join(data5, "a_3.mzML"))
        again, names = proposed(out)
        check("a .mzBIN row the user added survives a re-proposal",
              rc == bw.EXIT_QUESTIONS and "lib_2.mzBIN" in names and "a_3.mzML" in names
              and not any("left out" in n for n in again.get("notes", [])), out[-400:])

        # without --data the rows are the files: a deleted row is no 'removed file'
        w = os.path.join(tmp, "r29")
        lfq_call(w, "--design", own)
        _quiet(bw.main, ["--workdir", w, "--answer", dia[0], "--answer", dia[1]])
        drop_row(os.path.join(fp.repro_dir(w), fp.DESIGN_NAME), "a_2.mzML")
        rc, out = _quiet(bw.main, ["--workdir", w, "--json"])
        check("a row taken out of a proposal made without --data is not reported as a file "
              "removed from the data folders", rc == bw.EXIT_QUESTIONS
              and not any("data folders" in n for n in proposed(out)[0].get("notes", [])),
              out[-400:])

        # a plan edited by hand into one the builder cannot read is exit 2, never a traceback
        for label, field, value, argv in (
                ("answers null, on a build", "answers", None, []),
                ("fragpipe a string, on a build", "fragpipe", "C:/FragPipe", []),
                ("an input missing its data type, on --show", "inputs", [{"file": "x"}],
                 ["--show"])):
            w = os.path.join(tmp, "r30_" + field)
            shutil.copytree(w17, w)
            plan_w = fp.load_plan(fp.plan_path(w))
            plan_w["workdir"] = w
            plan_w[field] = value
            fp.save_plan(fp.plan_path(w), plan_w)
            rc, out = _quiet(bw.main, ["--workdir", w] + argv)
            check("a damaged plan (%s) is exit 2 naming the field" % label,
                  rc == bw.EXIT_CONFIG_ERROR and "damaged" in out and field in out, out[-300:])


def test_tmt_annotations() -> None:
    import build_workflow as bw
    import fragskills_plan as fp
    print("\n[17] workflow builder: TMT annotations in the raw-file folders")
    with tempfile.TemporaryDirectory() as tmp:
        install = _fake_install(os.path.join(tmp, "FragPipe-24.1-build61"), "24.1-build61",
                                {"TMT10-bridge": _TMT_TEXT, "LFQ-MBR": _LFQ_TEXT})
        data = os.path.join(tmp, "data")
        for plex in ("p1", "p2"):
            os.makedirs(os.path.join(data, plex))
            open(os.path.join(data, plex, plex + "_F1.mzML"), "w").close()
        fasta = os.path.join(tmp, "db.fas")
        open(fasta, "w").close()
        wd = os.path.join(tmp, "out")
        answers = ["analysis=standard", "acquisition=DDA", "search=closed", "quant=TMT",
                   "tmt_plex=11", "tmt_ms_level=MS2", "tmt_reference=bridge", "ptm=none",
                   "fasta=" + fasta, "remove_contaminants=no"]
        argv = ["--workdir", wd, "--fragpipe-dir", install["root"], "--data", data, "--json"]
        for a in answers:
            argv += ["--answer", a]
        rc, out = _quiet(bw.main, argv)
        payload = json.loads(out)
        sheet = payload["open"][0].get("path")
        check("the channel sheet lists every 11-plex channel of each plex folder",
              payload["open"][0]["id"] == "channels" and len(payload["open"][0]["rows"]) == 22,
              str(payload["open"][0].get("rows", ""))[:200])
        base = ["--workdir", wd, "--channels", sheet]
        rc, out = _quiet(bw.main, base)
        check("the bridge workflow needs a 'pool' sample in every plex",
              rc == bw.EXIT_QUESTIONS and out.count("contains 'pool'") == 2, out[-400:])
        with open(sheet, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(sheet, "w", encoding="utf-8") as fh:
            fh.write(text.replace("\tp1_131C\t", "\tpool_1\t").replace("\tp2_131C\t", "\tpool_2\t"))
        rc, out = _quiet(bw.main, base)
        plan = fp.load_plan(fp.plan_path(wd))
        check("the TMT plan builds", rc == bw.EXIT_OK, out[-300:])
        check("the table shows each plex's sample names", "126=p1_126" in out
              and "131C=pool_2" in out, out[-600:])
        check("plex size changes only tmtintegrator.channel_num",
              [c["key"] for c in plan["changes"]] == ["database.db-path",
                                                       "tmtintegrator.channel_num"]
              and plan["changes"][1]["value"] == "TMT-11", str(plan["changes"]))
        target = os.path.join(data, "p1", "p1_annotation.txt")
        check("nothing is written into the raw folders before --confirm",
              not os.path.exists(target))
        copy = os.path.join(fp.repro_dir(wd), fp.ANNOTATIONS_DIR, "p1_annotation.txt")
        with open(copy, "a", encoding="utf-8") as fh:
            fh.write("127N SWAPPED\n")
        rc, _ = _quiet(bw.main, ["--workdir", wd, "--confirm"])
        check("--confirm refuses an annotation copy changed after the table was shown",
              rc == bw.EXIT_CONFIG_ERROR and not os.path.exists(target), str(rc))
        _quiet(bw.main, base)
        os.remove(copy)
        rc, out = _quiet(bw.main, ["--workdir", wd, "--confirm"])
        check("--confirm refuses an annotation copy that is gone",
              rc == bw.EXIT_CONFIG_ERROR and "is missing" in out, out[-300:])
        _quiet(bw.main, base)
        real_replace = os.replace

        def refuse(src, dst):
            raise PermissionError(13, "Access is denied", src, None, dst)
        os.replace = refuse
        try:
            rc, out = _quiet(bw.main, ["--workdir", wd, "--confirm"])
        finally:
            os.replace = real_replace
        check("a plan that cannot be saved confirms nothing: no annotation written, the "
              "error names the plan", rc == bw.EXIT_CONFIG_ERROR and not os.path.exists(target)
              and fp.load_plan(fp.plan_path(wd))["state"] == fp.STATE_BUILT
              and "fragskills_plan.json (" in out
              and not os.path.exists(fp.plan_path(wd) + ".tmp"), out[-300:])
        rc, _ = _quiet(bw.main, ["--workdir", wd, "--confirm"])
        check("--confirm writes the annotation next to the raw files",
              rc == bw.EXIT_OK and os.path.isfile(target), str(rc))
        with open(target, "r", encoding="utf-8") as fh:
            first = fh.readline().strip()
        check("annotation lines are '<channel> <sample>'", first == "126 p1_126", first)
        rc, _ = _quiet(bw.main, base)
        rc2, _ = _quiet(bw.main, ["--workdir", wd, "--confirm"])
        check("a rebuild recognises the annotation it wrote itself",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_OK, "%s %s" % (rc, rc2))
        plan = fp.load_plan(fp.plan_path(wd))
        with open(target, "rb") as fh:
            written = fh.read()
        with open(target, "wb") as fh:
            fh.write(written.replace(b"126 p1_126", b"126 p1_127N"))
        check("a TMT annotation edited after --confirm blocks the launch",
              fp.check_lock(plan["files"]["workflow"], plan["files"]["manifest"])[0]
              == fp.LOCK_REFUSED)
        with open(target, "wb") as fh:
            fh.write(written)

        # a build of another workflow in between does not make the builder's own
        # annotations look like the user's
        rc, out = _quiet(bw.main, ["--workdir", wd, "--json", "--answer", "quant=LFQ"])
        design = next((q for q in json.loads(out).get("open", []) if q["id"] == "design"), {})
        rc, _ = _quiet(bw.main, ["--workdir", wd, "--design", design.get("path", "")])
        back = ["--workdir", wd]
        for a in answers:
            back += ["--answer", a]
        rc2, out = _quiet(bw.main, back)
        plan = fp.load_plan(fp.plan_path(wd))
        check("after an LFQ build, the TMT rebuild still owns the annotations it wrote",
              rc == bw.EXIT_OK and rc2 == bw.EXIT_OK
              and [a["status"] for a in plan["annotations"]] == ["to write", "to write"],
              out[-300:])
        rc, _ = _quiet(bw.main, ["--workdir", wd, "--confirm"])
        extra = os.path.join(data, "p2", "mine_annotation.txt")
        open(extra, "w").close()
        check("a second annotation in a plex folder blocks the launch",
              fp.check_lock(plan["files"]["workflow"], plan["files"]["manifest"])[0]
              == fp.LOCK_REFUSED)
        rc, _ = _quiet(bw.main, base)
        check("... and the build", rc == bw.EXIT_CONFIG_ERROR, str(rc))
        os.remove(extra)
        rc, out = _quiet(bw.main, ["--workdir", wd, "--json", "--answer", "tmt_plex=10"])
        chans = next((q for q in json.loads(out).get("open", []) if q["id"] == "channels"), {})
        check("an accepted channel sheet is proposed again for a new channel count, edits kept",
              rc == bw.EXIT_QUESTIONS and len(chans.get("rows", [])) == 20
              and any("channel count or the plexes changed" in n for n in chans.get("notes", []))
              and os.path.isfile(sheet + ".bak"), out[-400:])

        # an existing annotation in another order is the user's and is used as is
        q1 = os.path.join(tmp, "data2", "q1")
        os.makedirs(q1)
        open(os.path.join(q1, "q1_F1.mzML"), "w").close()
        import workflow_tree as wt
        ch11 = wt.load_tree()["channels"]["TMT-11"]
        mine = os.path.join(q1, "mine_annotation.txt")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("".join("%s %s\n" % (c, "pool_q1" if c == "131C" else "q1s_" + c)
                             for c in reversed(ch11)))
        wd2 = os.path.join(tmp, "out2")
        argv = ["--workdir", wd2, "--fragpipe-dir", install["root"], "--data",
                os.path.dirname(q1)]
        for a in answers:
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet2 = os.path.join(fp.repro_dir(wd2), fp.CHANNELS_NAME)
        rc, out = _quiet(bw.main, ["--workdir", wd2, "--channels", sheet2])
        plan2 = fp.load_plan(fp.plan_path(wd2))
        check("an existing annotation listed in another order is used as is",
              rc == bw.EXIT_OK and plan2["annotations"][0]["status"] == "existing", out[-300:])
        # read again before any sheet is accepted, too
        wd2b = os.path.join(tmp, "out2b")
        _quiet(bw.main, ["--workdir", wd2b] + argv[2:])
        with open(mine, "r", encoding="utf-8") as fh:
            before = fh.read()
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write(before.replace("126 q1s_126\n", "126 q1s_126y\n"))
        rc, out = _quiet(bw.main, ["--workdir", wd2b, "--json"])
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write(before)
        chans = next((q for q in json.loads(out).get("open", []) if q["id"] == "channels"), {})
        check("an existing annotation edited before any sheet was accepted is read again too",
              rc == bw.EXIT_QUESTIONS and any(r["sample"] == "q1s_126y"
                                              for r in chans.get("rows", []))
              and any("FragPipe uses the annotation file" in n for n in chans.get("notes", [])),
              out[-400:])
        with open(mine, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write(text.replace("126 q1s_126\n", "126 q1s_126x\n"))
        rc, out = _quiet(bw.main, ["--workdir", wd2, "--json"])
        chans = next((q for q in json.loads(out).get("open", []) if q["id"] == "channels"), {})
        check("an existing annotation edited later is read again: the sheet takes its samples",
              rc == bw.EXIT_QUESTIONS and any(r["sample"] == "q1s_126x"
                                              for r in chans.get("rows", []))
              and any("FragPipe uses the annotation file" in n for n in chans.get("notes", [])),
              out[-400:])
        rc, out = _quiet(bw.main, ["--workdir", wd2, "--channels", sheet2])
        check("... and the sheet as proposed is accepted", rc == bw.EXIT_OK, out[-400:])
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write(text.replace("126 q1s_126\n", "126 q1s_126x \n"))
        rc, out = _quiet(bw.main, ["--workdir", wd2, "--channels", sheet2])
        check("a sample in an existing annotation that ends in a non-breaking space is "
              "refused, the file named (IonQuant keeps the space in the name)",
              rc == bw.EXIT_CONFIG_ERROR and "Unicode space (U+00A0)" in out
              and "mine_annotation.txt" in out, out[-400:])
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write(text)
        _quiet(bw.main, ["--workdir", wd2, "--channels", sheet2])
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("channel sample\n" + text)
        rc, out = _quiet(bw.main, ["--workdir", wd2, "--channels", sheet2])
        check("an existing annotation with a line FragPipe would count is refused",
              rc == bw.EXIT_CONFIG_ERROR and "counts every line" in out, out[-300:])

        # sample names: unique across plexes, NA may repeat
        rows = [{"plex": p, "channel": c, "sample": "%s_%s" % (p, c), "condition": "",
                 "replicate": ""} for p in ("a", "b") for c in ch11]
        rows[10]["sample"] = rows[21]["sample"] = "pool"
        sheet3 = os.path.join(tmp, "ch.tsv")
        bw.write_channels(sheet3, rows, "")
        _, probs = bw.read_channels(sheet3, ["a", "b"], ch11, "pool")
        check("the same sample name in two plexes is refused, as FragPipe does",
              any("unique across all plexes" in p for p in probs), str(probs))
        rows[10]["sample"], rows[21]["sample"] = "pool_a", "pool_b"
        rows[1]["sample"] = rows[2]["sample"] = rows[12]["sample"] = "NA"
        bw.write_channels(sheet3, rows, "")
        _, probs = bw.read_channels(sheet3, ["a", "b"], ch11, "pool")
        check("NA marks unused channels and may repeat", not probs, str(probs))
        sheet5 = os.path.join(tmp, "ch5.tsv")

        def probs_for(changes, from_files=None):
            changed = [dict(r) for r in rows]
            for i, col, value in changes:
                changed[i][col] = value
            bw.write_channels(sheet5, changed, "")
            return bw.read_channels(sheet5, ["a", "b"], ch11, "pool", None, from_files)[1]
        probs = probs_for([(3, "sample", "pool_a2")])
        check("two samples naming the bridge tag in one plex are refused",
              len(probs) == 1 and "plex a" in probs[0] and "found 2" in probs[0], str(probs))
        probs = probs_for([(3, "sample", "WT\x7f1"), (4, "sample", "WT\x0c2"),
                           (15, "sample", "WT\x1f3")])
        check("DEL, a form feed and a unit separator in a sample name are refused",
              len(probs) == 3 and all("control character" in p for p in probs), str(probs))
        probs = probs_for([(0, "replicate", "x"), (3, "sample", "N/A"), (21, "sample", "b_131C")],
                          {"a": "a_annotation.txt", "b": "b_annotation.txt"})
        check("in plexes whose samples come from their own files, a sample problem names the "
              "file, a sheet-only one does not, and a missing bridge sample says to edit the file",
              len(probs) == 3
              and any("replicate 'x'" in p and "(from" not in p for p in probs)
              and any("'N/A'" in p and "(from a_annotation.txt)" in p for p in probs)
              and any("plex b" in p and "found 0" in p and "b_annotation.txt" in p
                      and "edit that file" in p for p in probs), str(probs))
        rows[1]["sample"], rows[12]["sample"] = "WT.1", "WT1"
        bw.write_channels(sheet3, rows, "")
        notes = []
        _, probs = bw.read_channels(sheet3, ["a", "b"], ch11, "pool", notes)
        check("names FragPipe shortens to the same name are refused, and shortened names noted",
              len(probs) == 1 and "'WT1' is used 2 times" in probs[0]
              and any("WT.1 as WT1" in n for n in notes), "%s %s" % (probs, notes))
        rows[1]["sample"], rows[2]["sample"], rows[12]["sample"] = "对照", "N/A", "n/a"
        bw.write_channels(sheet3, rows, "")
        _, probs = bw.read_channels(sheet3, ["a", "b"], ch11, "pool")
        check("a name FragPipe empties is refused, and so is N/A: IonQuant takes only NA as "
              "unused", len(probs) == 3 and "none of the characters" in probs[0]
              and all("write NA" in p for p in probs[1:]), str(probs))
        rows[1]["sample"], rows[2]["sample"], rows[12]["sample"] = "WT\x011", "NA", "na"
        rows[21]["sample"] = "po.ol_b"
        bw.write_channels(sheet3, rows, "")
        _, probs = bw.read_channels(sheet3, ["a", "b"], ch11, "pool")
        check("a control character in a sample name is refused, and the bridge tag is "
              "looked for in the name as written (po.ol_b is no 'pool')",
              len(probs) == 2 and "control character" in probs[0]
              and "plex b" in probs[1] and "'pool'" in probs[1], str(probs))
        ann = os.path.join(tmp, "x_annotation.txt")
        with open(ann, "w", encoding="utf-8") as fh:
            fh.write("".join("%s s_%s\n" % (c, c) for c in ch11))
        probs = bw.check_annotation(ann, ch11)[1]
        check("a non-breaking space does not separate channel and sample, as in FragPipe",
              len(probs) == 11 and "FragPipe stops" in probs[0], str(probs[:2]))
        with open(ann, "w", encoding="utf-8-sig") as fh:
            fh.write("".join("%s s_%s\n" % (c, c) for c in ch11))
        probs = bw.check_annotation(ann, ch11)[1]
        check("an annotation's byte-order mark is reported", len(probs) == 1
              and "byte-order mark" in probs[0], str(probs))
        with open(ann, "wb") as fh:
            fh.write(b"".join(b"%s Kontrolle_\xe4_%s\n" % (c.encode(), c.encode()) for c in ch11))
        probs = bw.check_annotation(ann, ch11)[1]
        check("a non-UTF-8 annotation gets one problem, not a false channel count",
              len(probs) == 1 and "IonQuant" in probs[0], str(probs))
        with open(ann, "w", encoding="utf-8") as fh:
            fh.write("".join("%s s_%s\n" % (c, c) for c in ch11) + "\x1a")
        probs = bw.check_annotation(ann, ch11)[1]
        check("an annotation ending in Ctrl-Z is refused (FragPipe cannot detect its encoding)",
              len(probs) == 1 and "control character" in probs[0], str(probs))
        with open(ann, "w", encoding="utf-8") as fh:
            fh.write("".join("%s %s\n" % (c, "NA " if c in ch11[1:3] else "s_" + c)
                             for c in ch11))
        probs = bw.check_annotation(ann, ch11)[1]
        check("NA followed by a non-breaking space in an annotation is refused (IonQuant "
              "takes it as a sample, and two in a plex stop it)", len(probs) == 2
              and all("Unicode space (U+00A0)" in p for p in probs), str(probs))
        with open(ann, "w", encoding="utf-8") as fh:
            fh.write("".join("%s s_%s\n" % (c, c) for c in ch11).replace("s_126\n",
                                                                          "s_12 6\n"))
        probs = bw.check_annotation(ann, ch11)[1]
        check("a line separator (U+2028) in an annotation's sample is refused, not read as a "
              "line break", len(probs) == 1 and "U+2028" in probs[0], str(probs))
        plexes, probs = bw.group_by_folder([os.path.join(tmp, "b1", "Plex1", "x.mzML"),
                                            os.path.join(tmp, "b2", "plex1", "y.mzML")])
        check("plex folders named alike but for case are refused", len(probs) == 1,
              str(probs))

        # an accepted channel sheet is for its folders, even when another batch has
        # plex folders of the same names
        for batch in ("batchA", "batchB"):
            for plex in ("p1", "p2"):
                os.makedirs(os.path.join(tmp, batch, plex))
                open(os.path.join(tmp, batch, plex, "%s_%s.mzML" % (batch, plex)), "w").close()
        wd3 = os.path.join(tmp, "out3")
        argv = ["--workdir", wd3, "--fragpipe-dir", install["root"], "--data",
                os.path.join(tmp, "batchA")]
        for a in answers:
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet4 = os.path.join(fp.repro_dir(wd3), fp.CHANNELS_NAME)
        with open(sheet4, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(sheet4, "w", encoding="utf-8") as fh:
            fh.write(text.replace("\tp1_131C\t", "\tpool_1\t").replace("\tp2_131C\t", "\tpool_2\t")
                     .replace("\tp1_126\t", "\tp1.126\t"))
        rc, out1 = _quiet(bw.main, ["--workdir", wd3, "--channels", sheet4])
        check("the table of a built TMT plan notes the sample names FragPipe compares shortened",
              rc == bw.EXIT_OK and "NOTE: FragPipe compares sample names" in out1
              and "p1.126 as p1126" in out1, out1[-400:])
        rc2, out = _quiet(bw.main, ["--workdir", wd3, "--json", "--data",
                                    os.path.join(tmp, "batchB")])
        chans = next((q for q in json.loads(out).get("open", []) if q["id"] == "channels"), {})
        check("an accepted channel sheet is proposed again for another batch with the same "
              "plex folder names", rc == bw.EXIT_OK and rc2 == bw.EXIT_QUESTIONS
              and any("data folders changed" in n for n in chans.get("notes", [])), out[-400:])
        wd4 = os.path.join(tmp, "out4")
        argv = ["--workdir", wd4, "--fragpipe-dir", install["root"], "--data",
                os.path.join(tmp, "batchA")]
        for a in answers:
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet6 = os.path.join(fp.repro_dir(wd4), fp.CHANNELS_NAME)
        with open(sheet6, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(sheet6, "w", encoding="utf-8") as fh:
            fh.write(text.replace("\tp1_131C\t", "\tpool_1\t").replace("\tp2_131C\t", "\tpool_2\t"))
        rc, out = _quiet(bw.main, ["--workdir", wd4, "--json", "--data",
                                   os.path.join(tmp, "batchB"), "--channels", sheet6])
        chans = next((q for q in json.loads(out).get("open", []) if q["id"] == "channels"), {})
        check("a channel sheet proposed for one batch is not accepted on the call that moves "
              "to another", rc == bw.EXIT_QUESTIONS
              and any("data folders changed" in n for n in chans.get("notes", [])), out[-400:])

        # one of two data folders briefly away: the call stops, the accepted sheet stays
        drives = [os.path.join(tmp, "driveA"), os.path.join(tmp, "driveB")]
        for d, plex in zip(drives, ("q1", "q2")):
            os.makedirs(os.path.join(d, plex))
            open(os.path.join(d, plex, plex + "_F1.mzML"), "w").close()
        wd5 = os.path.join(tmp, "out5")
        argv = ["--workdir", wd5, "--fragpipe-dir", install["root"], "--data"] + drives
        for a in answers:
            argv += ["--answer", a]
        _quiet(bw.main, argv)
        sheet7 = os.path.join(fp.repro_dir(wd5), fp.CHANNELS_NAME)
        with open(sheet7, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(sheet7, "w", encoding="utf-8") as fh:
            fh.write(text.replace("\tq1_131C\t", "\tpool_1\t").replace("\tq2_131C\t", "\tpool_2\t")
                     .replace("\tq2_126\t", "\tKO_7\t"))
        rc, _ = _quiet(bw.main, ["--workdir", wd5, "--channels", sheet7])
        os.rename(drives[1], drives[1] + "_away")
        try:
            rc2, out2 = _quiet(bw.main, ["--workdir", wd5, "--json"])
        finally:
            os.rename(drives[1] + "_away", drives[1])
        rc3, out3 = _quiet(bw.main, ["--workdir", wd5])
        check("one of two data folders briefly away keeps the accepted channel sheet and its "
              "edits", (rc, rc2, rc3) == (bw.EXIT_OK, bw.EXIT_QUESTIONS, bw.EXIT_OK)
              and "No such folder" in out2 and "126=KO_7" in out3
              and fp.load_plan(fp.plan_path(wd5))["channels"] == sheet7,
              out2[-300:] + out3[-300:])

        # a channel sheet that is not UTF-8 is never merged into a new proposal
        with open(sheet7, "r", encoding="utf-8") as fh:
            text = fh.read()
        with open(sheet7, "w", encoding="cp1252") as fh:
            fh.write(text.replace("\tKO_7\t", "\tKäse_7\t"))
        rc, out = _quiet(bw.main, ["--workdir", wd5, "--json", "--answer", "tmt_plex=10"])
        with open(sheet7, "rb") as fh:
            still = fh.read()
        check("a channel sheet that is not UTF-8 is not merged into a new proposal, and stays "
              "accepted, with no note saying it was proposed again",
              rc == bw.EXIT_QUESTIONS and "not UTF-8" in out and "proposed again" not in out
              and "was updated" not in out and b"K\xe4se_7" in still
              and fp.load_plan(fp.plan_path(wd5))["channels"] == sheet7, out[-300:])

        # a channel sheet of the user's own is bound only by a build that accepts it
        ch10 = wt.load_tree()["channels"]["TMT-10"]
        mine = os.path.join(tmp, "my_channels.tsv")
        with open(mine, "w", encoding="utf-8") as fh:
            fh.write("plex\tchannel\tsample\tcondition\treplicate\n" + "".join(
                "%s\t%s\t%s\t\t\n" % (p, c, "pool_%s" % p[-1] if c == ch10[-1] else
                                      "%s_s%s" % (p, c)) for p in ("q1", "q2") for c in ch10))
        wd7 = os.path.join(tmp, "out7")
        argv = ["--workdir", wd7, "--fragpipe-dir", install["root"], "--data"] + drives
        for a in answers:
            argv += ["--answer", a]
        rc, _ = _quiet(bw.main, argv + ["--channels", mine])
        bound = fp.load_plan(fp.plan_path(wd7)).get("channels_for")
        rc2, out2 = _quiet(bw.main, ["--workdir", wd7, "--answer", "tmt_plex=10"])
        check("an own channel sheet refused for TMT-11 is read as it stands for TMT-10, not "
              "proposed again", (rc, rc2) == (bw.EXIT_QUESTIONS, bw.EXIT_OK) and not bound
              and fp.load_plan(fp.plan_path(wd7))["channels"] == mine, out2[-300:])
        target = os.path.join(drives[0], "q1", "q1_annotation.txt")
        os.makedirs(target)
        try:
            rc, out = _quiet(bw.main, ["--workdir", wd7, "--confirm"])
        finally:
            os.rmdir(target)
        check("--confirm with a folder at an annotation's path names it, writing nothing",
              rc == bw.EXIT_CONFIG_ERROR and "is not a file" in out
              and not os.path.exists(os.path.join(drives[1], "q2", "q2_annotation.txt")),
              out[-300:])
        broken = fp.load_plan(fp.plan_path(wd7))
        broken["annotations"] = [{"plex": "q1"}]
        fp.save_plan(fp.plan_path(wd7), broken)
        rc, out = _quiet(bw.main, ["--workdir", wd7, "--show"])
        check("a hand-damaged TMT plan is exit 2 on --show, naming the annotations",
              rc == bw.EXIT_CONFIG_ERROR and "annotations" in out, out[-300:])

        # FragPipe counts any entry whose name ends in annotation.txt, a folder too
        odd = os.path.join(tmp, "batchA", "p1", "old_annotation.txt")
        os.makedirs(odd)
        argv = ["--workdir", os.path.join(tmp, "out6"), "--fragpipe-dir", install["root"],
                "--data", os.path.join(tmp, "batchA")]
        for a in answers:
            argv += ["--answer", a]
        rc, out = _quiet(bw.main, argv)
        check("a folder named like an annotation in a plex folder is refused",
              rc == bw.EXIT_CONFIG_ERROR and "not a file" in out
              and odd in fp.annotation_files(os.path.dirname(odd)), out[-300:])
        os.rmdir(odd)


_FAKE_FRAGPIPE = """import os, subprocess, sys, time
workdir = sys.argv[sys.argv.index("--workdir") + 1]
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)",
                          os.path.join(workdir, "fragger.params")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
with open(os.path.join(workdir, "fake_child.pid"), "w") as fh:
    fh.write(str(child.pid))
print("2 commands to execute:", flush=True)
time.sleep(600)
"""


_STANDIN_WATCHER = """import sys, time
sys.path.insert(0, sys.argv[1])
import fragpipe_stop
held = fragpipe_stop.lock_workdir(sys.argv[sys.argv.index("--workdir") + 1])
time.sleep(120)
"""


def test_stop() -> None:
    """fragpipe_stop.py must end the whole run, and nothing else.

    A fake FragPipe launcher, a .bat or a shell script like the real one, starts a
    child that sleeps, as FragPipe starts MSFragger; fragpipe_run.py watches it.
    fragpipe_stop.py must refuse without --yes, then end both processes and leave the
    run recorded as stopped. A status file whose PID now belongs to another program
    must not get that program killed.
    """
    print("\n[20] stopping a run")
    import shutil
    import socket
    import subprocess
    import time
    import fragpipe_stop as fs

    win = 'C:\\WINDOWS\\system32\\cmd.exe /c ""C:\\FP\\bin\\fragpipe.bat" --headless ' \
          '--workflow C:\\w.workflow --workdir "C:\\my run\\out" --ram 8"'
    check("the workdir is read from a quoted Windows command line",
          fs.workdir_of(win) == "C:\\my run\\out", str(fs.workdir_of(win)))
    check("and from an unquoted one, as the last option",
          fs.workdir_of('java -cp x org.nesvilab.fragpipe.FragPipeMain --headless '
                        '--workdir C:\\out') == "C:\\out")
    check("and from ps output, where a path with spaces is not quoted",
          fs.workdir_of("/opt/fp/jre/bin/java FragPipeMain --headless --workdir "
                        "/data/my run/out --threads 8") == "/data/my run/out")
    here = os.path.abspath("run_out")
    procs = [{"pid": 5, "ppid": 1, "cmd": 'bash -c "fragpipe --headless --workdir %s"' % here},
             {"pid": 10, "ppid": 5, "cmd": 'fragpipe --headless --workdir "%s"' % here},
             {"pid": 11, "ppid": 10, "cmd": "java FragPipeMain --headless --workdir %s" % here},
             {"pid": 12, "ppid": 11, "cmd": "java -jar MSFragger.jar params"},
             {"pid": 20, "ppid": 1, "cmd": "python fragpipe_run.py --workdir %s" % here},
             {"pid": 30, "ppid": 1, "cmd": "fragpipe --headless --workdir %s2" % here},
             {"pid": 40, "ppid": 1, "cmd": "fragpipe --headless --workdir run_out"}]
    hits = fs.run_processes(procs, here)
    check("only FragPipe headless processes of that workdir are picked: not the shell that "
          "runs it, not the watcher, not a workdir that only starts the same, not a relative "
          "workdir", [h["pid"] for h in hits] == [10, 11], str(hits))
    check("the run's tree holds the tools it started",
          [r["pid"] for r in fs.roots(hits)] == [10]
          and sorted(fs.tree(procs, 10)) == [10, 11, 12])
    check("a run with a relative workdir is still listed",
          40 in [r["pid"] for r in fs.roots(fs.run_processes(procs))])
    real = ("C:\\WINDOWS\\system32\\cmd.exe /c C:\\FP\\bin\\fragpipe.bat --headless "
            "--workflow C:\\w.workflow --workdir C:\\out")
    spaced = ('C:\\WINDOWS\\system32\\cmd.exe /c ""C:\\Program Files\\FP\\bin\\fragpipe.bat" '
              '--headless --workdir "C:\\out dir""')
    for cmd in (real, spaced, win,
                'cmd /c "C:\\FP\\bin\\fragpipe.bat --headless --workdir C:\\out"',
                "java -Xmx8G -cp lib/* org.nesvilab.fragpipe.FragPipeMain --headless",
                "java -cp x com.dmtavt.fragpipe.FragPipeMain --headless",
                "/bin/sh /opt/fragpipe/bin/fragpipe --headless --workdir /out",
                "/opt/fragpipe/bin/fragpipe --headless --workdir /out"):
        check("FragPipe is recognised: %s" % cmd[:70], fs.is_fragpipe(cmd))
    for cmd in ("C:\\WINDOWS\\system32\\cmd.exe /c C:\\FP\\bin\\fragpipe.bat",
                "C:\\WINDOWS\\system32\\cmd.exe /K C:\\FP\\bin\\fragpipe.bat --headless "
                "--workdir C:\\out",
                'powershell -Command "& C:\\FP\\bin\\fragpipe.bat --headless --workdir C:\\out"',
                'bash -c "fragpipe --headless --workdir /out"',
                'cmd.exe /c "python fragpipe_run.py --fragpipe C:\\FP\\bin\\fragpipe.bat '
                '--headless"',
                "python fragpipe_stop.py --workdir C:\\out --headless",
                "java -jar MSFragger.jar --headless"):
        check("a program that only names FragPipe, its GUI, or the console `start` leaves "
              "open is not taken for it: %s" % cmd[:70], not fs.is_fragpipe(cmd))
    launch = "cmd.exe /c C:\\FP\\bin\\fragpipe.bat --headless --workdir C:\\x"
    stale = [{"pid": 70, "ppid": 1, "cmd": launch, "created": 200},
             {"pid": 71, "ppid": 70, "cmd": "explorer.exe", "created": 100},
             {"pid": 72, "ppid": 70, "cmd": "java FragPipeMain --headless --workdir C:\\x",
              "created": 300}]
    check("a process older than the PID it names as its parent is not in the run (Windows "
          "hands a dead parent's PID to new processes)", sorted(fs.tree(stale, 70)) == [70, 72])
    nested = [{"pid": 80, "ppid": 1, "cmd": "fragpipe --headless --workdir /a"},
              {"pid": 81, "ppid": 80, "cmd": "java FragPipeMain --headless --workdir /b"}]
    check("a run under another run's process is still listed as its own",
          sorted(r["pid"] for r in fs.roots(fs.run_processes(nested))) == [80, 81])
    jdk = tempfile.mkdtemp()
    java = os.path.join(jdk, "My JDK", "bin", "java")
    os.makedirs(os.path.dirname(java))
    with open(java, "w") as fh:
        fh.write("")
    os.chmod(java, 0o755)
    check("a JVM whose path has a space, as ps shows it unquoted, is recognised",
          fs.is_fragpipe(java + " -cp x org.nesvilab.fragpipe.FragPipeMain --headless "
                                "--workdir /out")
          and not fs.is_fragpipe(java.replace("My JDK", "No JDK") + " -cp x "
                                 "org.nesvilab.fragpipe.FragPipeMain --headless"))
    check("a relative workdir may name a folder only by its end",
          fs._tail_of(os.path.join(jdk, "proj", "out"), "out")
          and fs._tail_of(os.path.join(jdk, "proj", "out"), os.path.join(".", "proj", "out"))
          and not fs._tail_of(os.path.join(jdk, "proj", "out"), "ut"))
    run = {"state": "running", "watcher_pid": 5, "started_at": "2026-10-30T10:00:00+00:00",
           "pid": 9}
    fs._write_json(os.path.join(jdk, fs.STATUS_NAME),
                   dict(run, state="failed", error="MSFragger crashed", child_exit_code=1))
    check("a run that ended on its own before the stop is not recorded as stopped",
          fs.finish_status(jdk, run, False, 9) == "ended"
          and not (fs.load_status(jdk) or {}).get("stopped"))
    fs._write_json(os.path.join(jdk, fs.STATUS_NAME),
                   dict(run, state="failed", error="MSFragger crashed"))
    done, status = fs.finish_status(jdk, run, False, 9), fs.load_status(jdk) or {}
    check("one that had already failed, with no watcher left, keeps its own error",
          done == "written" and status.get("stopped") is True
          and status.get("error") == "MSFragger crashed", str(status))
    os.remove(os.path.join(jdk, fs.STATUS_NAME))
    held = fs.lock_workdir(jdk)
    saved, fs.WATCHER_SEC = fs.WATCHER_SEC, 1
    try:
        check("a folder whose lock is held is seen as having a live watcher",
              held is not None and fs.watcher_holds(jdk))
        check("and the stop leaves that watcher's status to it",
              fs.finish_status(jdk, None, False, 9, held=True) == "pending"
              and fs.load_status(jdk) is None)
        held.close()
        check("until it has exited", not fs.watcher_holds(jdk)
              and fs.finish_status(jdk, None, False, 9, held=True) == "watcher")
        fs._write_json(os.path.join(jdk, fs.STATUS_NAME), run)
        held = fs.lock_workdir(jdk)
        check("a live watcher's status is never written over, however long it takes",
              fs.finish_status(jdk, run, True, 9, held=True) == "pending"
              and not (fs.load_status(jdk) or {}).get("stopped"))
        held.close()
        done, status = fs.finish_status(jdk, run, True, 9, held=True), fs.load_status(jdk) or {}
        check("and once it has exited without recording the stop, the script records it",
              done == "written" and status.get("stopped") is True
              and status.get("error") == fs.STOPPED_ERROR, "%s %s" % (done, status))
        os.remove(os.path.join(jdk, fs.STATUS_NAME))
    finally:
        fs.WATCHER_SEC = saved
    import errno
    if os.name == "nt":
        import msvcrt as locker
        name = "locking"
    else:
        import fcntl as locker
        name = "flock"
    real = getattr(locker, name)
    try:
        def no_locks(*a):
            raise OSError(errno.ENOLCK, "No locks available")
        setattr(locker, name, no_locks)
        try:
            fs.lock_workdir(jdk)
            unsupported = False
        except OSError:
            unsupported = True
        check("a filesystem that cannot lock (NFS without lockd) is not taken for a held lock",
              unsupported and not fs.watcher_holds(jdk))
    finally:
        setattr(locker, name, real)
    tool = {"pid": 90, "ppid": 1, "cmd": "java -jar MSFragger.jar %s"
            % os.path.join(jdk, "out", "fragger.params")}
    procs = [tool,
             {"pid": 91, "ppid": 1, "cmd": "notepad %s" % os.path.join(jdk, "out2", "x.txt")},
             {"pid": 92, "ppid": 1, "cmd": "python fragpipe_stop.py --workdir %s"
              % os.path.join(jdk, "out")}]
    out_dir = os.path.join(jdk, "out")
    os.makedirs(out_dir)
    check("the processes that name a workdir are found, not those of a folder that starts "
          "the same, nor this skill's own scripts",
          [x["pid"] for x in fs.naming(procs, out_dir)] == [90])
    rc, out = _quiet(fs._nothing_found, procs, out_dir, {"state": "running"})
    check("when FragPipe is gone but its status says running, the tools left are named "
          "(exit 1)", rc == fs.EXIT_NOT_STOPPED and "PID 90" in out, out)
    rc, out = _quiet(fs._nothing_found, procs, out_dir, {"state": "succeeded"})
    check("after a finished run they are only noted (exit 4)",
          rc == fs.EXIT_NOTHING and "PID 90" in out, out)
    rc, out = _quiet(fs._nothing_found, procs, out_dir,
                     {"state": "running", "host": "another-computer"})
    check("a run whose status says it runs on another computer is not called idle (exit 1)",
          rc == fs.EXIT_NOT_STOPPED and "another-computer" in out
          and "Nothing is running" not in out, out)
    rc, out = _quiet(fs._nothing_found, procs, out_dir, None, True)
    check("nor one whose watcher holds the folder from another computer (exit 1)",
          rc == fs.EXIT_NOT_STOPPED and "another computer" in out
          and "Nothing is running" not in out, out)
    mine = procs + [{"pid": 95, "ppid": 1, "cmd": "python fragpipe_run.py --workdir %s"
                     % out_dir}]
    rc, out = _quiet(fs._nothing_found, mine, out_dir, None, True)
    check("nor one whose watcher is here while FragPipe starts or ends (exit 1)",
          rc == fs.EXIT_NOT_STOPPED and "PID 95" in out and "again" in out, out)
    me = {"pid": os.getpid(), "ppid": 1, "cmd": "python", "name": "python.exe", "session": 1}
    elevated = {"pid": 96, "ppid": 1, "cmd": "", "name": "java.exe", "session": 1}
    rc, out = _quiet(fs._nothing_found, mine + [me, elevated], out_dir, None, True)
    check("an unrelated Java run as administrator does not make a visible watcher's run "
          "read as started as administrator", rc == fs.EXIT_NOT_STOPPED and "again" in out
          and "started as administrator" not in out, out)
    rc, out = _quiet(fs._nothing_found, procs, out_dir,
                     {"state": "failed", "host": "another-computer",
                      "tools_left": [{"pid": 97, "cmd": "java -jar MSFragger.jar"}]})
    check("tools another computer's run left are not called nothing (exit 1)",
          rc == fs.EXIT_NOT_STOPPED and "another-computer" in out and "97" in out
          and "Nothing is running" not in out, out)
    was_windows = fs.WINDOWS
    try:
        fs.WINDOWS = True
        launched = [{"pid": 100, "ppid": 1, "cmd": "cmd.exe /c fragpipe.bat", "created": 10},
                   {"pid": 101, "ppid": 100, "cmd": "java FragPipeMain", "created": 11,
                    "name": "java.exe"},
                   {"pid": 106, "ppid": 100, "cmd": "conhost.exe", "created": 11,
                    "name": "conhost.exe"},
                   {"pid": 102, "ppid": 101, "cmd": "java -jar MSFragger.jar", "created": 12}]
        anchors = fs.fragpipe_pids(launched, 100)
        check("Windows: FragPipe's own processes are its launcher and the JVM, not the tools "
              "that JVM starts", anchors == {100: 10, 101: 11}, str(anchors))
        after = [{"pid": 102, "ppid": 101, "cmd": "java -jar MSFragger.jar", "created": 12},
                 {"pid": 103, "ppid": 102, "cmd": "a tool's own child", "created": 13},
                 {"pid": 104, "ppid": 101, "cmd": "older than the JVM", "created": 5},
                 {"pid": 101, "ppid": 1, "cmd": "a newer owner of the JVM's PID", "created": 30},
                 {"pid": 105, "ppid": 101, "cmd": "that owner's child", "created": 31},
                 {"pid": 107, "ppid": 102, "cmd": "conhost.exe 0x4", "created": 12,
                  "name": "conhost.exe"}]
        check("Windows: once the JVM has died, the tools it left are found, not a newer "
              "owner of its PID or that owner's children, nor a tool's conhost.exe",
              sorted(x["pid"] for x in fs.tools_left(after, 100, anchors)) == [102, 103],
              str(fs.tools_left(after, 100, anchors)))
        fs.WINDOWS = False
        group = [{"pid": 200, "ppid": 1, "pgid": 150, "cmd": "java -jar MSFragger.jar"},
                 {"pid": 201, "ppid": 1, "pgid": 151, "cmd": "another group"},
                 {"pid": 202, "ppid": 200, "pgid": 150, "cmd": "done <defunct>"}]
        check("POSIX: the tools left are the run's process group",
              [x["pid"] for x in fs.tools_left(group, 150, {})] == [200])
    finally:
        fs.WINDOWS = was_windows
    tool = {"pid": 200, "cmd": "java -jar MSFragger.jar", "created": None}
    here_host = socket.gethostname()
    check("the tools a status records are still running only with the same command line, "
          "on this computer",
          [x["pid"] for x in fs.tools_still_running(group, {"tools_left": [tool],
                                                            "host": here_host})] == [200]
          and not fs.tools_still_running(group, {"tools_left": [dict(tool, cmd="other")]})
          and not fs.tools_still_running(group, {"tools_left": [tool], "host": "elsewhere"}))
    elevated = [{"pid": 300, "ppid": 1, "cmd": "", "created": 77}]
    recorded = {"tools_left": [{"pid": 300, "cmd": "java -jar MSFragger.jar", "created": 77}]}
    seen = fs.tools_still_running(elevated, recorded)
    check("a tool run as administrator, whose command line cannot be read, still counts by "
          "its start time", [x["pid"] for x in seen] == [300] and seen[0].get("hidden")
          and not fs.tools_still_running([dict(elevated[0], created=78)], recorded))
    me = {"pid": os.getpid(), "ppid": 1, "cmd": "python", "name": "python.exe", "session": 1}
    hidden = [me, {"pid": 93, "ppid": 1, "cmd": "", "name": "java.exe", "session": 1},
              {"pid": 94, "ppid": 1, "cmd": "", "name": "java.exe", "session": 0}]
    check("a Java process in this session whose command line cannot be read is noted (Windows)",
          [x["pid"] for x in fs.hidden_java(hidden)] == ([93] if os.name == "nt" else []))
    link = os.path.join(jdk, "link")
    try:
        os.symlink(out_dir, link)
    except (OSError, NotImplementedError):
        link = None
    if link:
        check("a workdir named through a symlink is the same folder",
              fs._same_dir(out_dir, link) and not fs._same_dir(out_dir, jdk))
    shutil.rmtree(jdk, ignore_errors=True)

    tmp = tempfile.mkdtemp()
    watcher = other = bare = rel = alone = watched = standin = None
    try:
        root = os.path.join(tmp, "FragPipe-24.1-build61")
        launcher = _make_install(root, "24.1-build61")
        if " " in launcher:                # FragPipe refuses such a path
            print("  SKIP  live stop test: a space in %s" % launcher)
            return
        fake = os.path.join(tmp, "fake_fragpipe.py")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_FRAGPIPE)
        if os.name == "nt":
            with open(launcher, "w", newline="\r\n") as fh:
                fh.write('@echo off\n"%s" -I "%s" %%*\n' % (sys.executable, fake))
        else:
            with open(launcher, "w") as fh:
                fh.write('#!/bin/sh\n"%s" -I "%s" "$@"\n' % (sys.executable, fake))
            os.chmod(launcher, 0o755)
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        open(os.path.join(data, "a.mzML"), "w").close()
        man = os.path.join(tmp, "files.fp-manifest")
        with open(man, "w", encoding="utf-8") as fh:
            fh.write("%s\texp\t1\tDDA\n" % os.path.join(data, "a.mzML"))
        wf = os.path.join(tmp, "fragpipe.workflow")
        with open(wf, "w", encoding="utf-8") as fh:
            fh.write("database.decoy-tag=rev_\n")
        wd = os.path.join(tmp, "out")
        with open(os.path.join(tmp, "watcher.log"), "w") as log:
            watcher = subprocess.Popen(
                [sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "fragpipe_run.py"),
                 "--workflow", wf, "--manifest", man, "--workdir", wd,
                 "--fragpipe-dir", root, "--quiet"], stdout=log, stderr=subprocess.STDOUT)
        child_pid_file = os.path.join(wd, "fake_child.pid")
        deadline = time.time() + 60
        while time.time() < deadline and not (
                os.path.isfile(child_pid_file) and (fs.load_status(wd) or {}).get("pid")):
            time.sleep(0.3)
        check("the fake run starts", os.path.isfile(child_pid_file))
        if not os.path.isfile(child_pid_file):
            return
        with open(child_pid_file) as fh:
            child = int(fh.read())
        procs = fs.list_processes()
        before = fs.snapshot(procs, [pid for r in fs.roots(fs.run_processes(procs, wd))
                                     for pid in fs.tree(procs, r["pid"])])
        check("the run is found with the tool it started",
              child in before and len(before) >= 2, str(before))

        import fragpipe_run as fr
        rc, out = _quiet(fr.main, ["--workflow", wf, "--manifest", man, "--workdir", wd,
                                   "--fragpipe-dir", root, "--quiet"])
        check("fragpipe_run.py refuses a second run in the same workdir (exit 2)",
              rc == fr.EXIT_CONFIG_ERROR and "already running" in out
              and watcher.poll() is None, out[-300:])
        rc, out = _quiet(fs.main, [])
        check("with no workdir, the running runs are listed",
              rc == fs.EXIT_STOPPED and os.path.normcase(wd) in os.path.normcase(out), out)
        rc, out = _quiet(fs.main, ["--workdir", wd])
        check("without --yes nothing is stopped (exit 3)",
              rc == fs.EXIT_CONFIRM and watcher.poll() is None
              and fs._alive(fs.list_processes(), before) == sorted(before), out)
        rc, out = _quiet(fs.main, ["--workdir", wd, "--yes"])
        check("--yes stops it (exit 0)", rc == fs.EXIT_STOPPED, out)
        try:
            watcher_rc = watcher.wait(timeout=60)
        except subprocess.TimeoutExpired:
            watcher_rc = None
        check("FragPipe and the tool it started are gone",
              fs._alive(fs.list_processes(), before) == [])
        status = fs.load_status(wd) or {}
        check("the watcher exits 1 and records the run as stopped by the user",
              watcher_rc == 1 and status.get("state") == "failed"
              and status.get("stopped") is True and status.get("error") == fs.STOPPED_ERROR,
              "rc=%s %s" % (watcher_rc, {k: status.get(k) for k in ("state", "error",
                                                                     "stopped")}))
        check("the stop request is removed", not os.path.exists(os.path.join(wd, fs.STOP_NAME)))
        rc, out = _quiet(fs.main, ["--workdir", wd, "--yes"])
        check("stopping it again finds nothing (exit 4)", rc == fs.EXIT_NOTHING, out)
        rc, out = _quiet(fs.main, ["--workdir", os.path.join(tmp, "none"), "--yes"])
        check("a workdir that does not exist is refused (exit 2)", rc == fs.EXIT_BAD_ARGS, out)
        odd = os.path.join(tmp, "\u6570\u636e out")
        os.makedirs(odd)
        r = subprocess.run([sys.executable, "-I", fs.__file__, "--workdir", odd],
                           capture_output=True, timeout=120)
        check("a non-ASCII workdir does not crash the script on a console that cannot show it",
              r.returncode == fs.EXIT_NOTHING and b"Traceback" not in r.stderr,
              (r.stdout + r.stderr).decode("utf-8", "replace")[-300:])

        # started outside fragpipe_run.py, over a status file an earlier run left
        wd3 = os.path.join(tmp, "out3")
        os.makedirs(wd3)
        fs._write_json(os.path.join(wd3, fs.STATUS_NAME),
                       {"state": "succeeded", "pid": 1, "summary": "SUCCEEDED - an earlier run"})
        scratch = os.path.join(data, "a.mzBIN_calibrated")
        open(scratch, "w").close()
        bare = subprocess.Popen([launcher, "--headless", "--workflow", wf, "--manifest", man,
                                 "--workdir", wd3], stdout=subprocess.DEVNULL,
                                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                   if os.name == "nt" else {"start_new_session": True}))
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.isfile(os.path.join(wd3, "fake_child.pid")):
            time.sleep(0.3)
        rc, out = _quiet(fr.main, ["--workflow", wf, "--manifest", man, "--workdir", wd3,
                                   "--fragpipe-dir", root, "--quiet"])
        check("and refuses one where FragPipe runs without a watcher, naming its PID",
              rc == fr.EXIT_CONFIG_ERROR and "PID %d" % bare.pid in out, out[-300:])
        rc, out = _quiet(fs.main, ["--workdir", wd3])
        check("an earlier run's status is not shown as this run's",
              rc == fs.EXIT_CONFIRM and "an earlier run" not in out
              and "no status file from this run" in out, out)
        rc, out = _quiet(fs.main, ["--workdir", wd3, "--yes"])
        status = fs.load_status(wd3) or {}
        check("with no watcher, the script records the stop itself",
              rc == fs.EXIT_STOPPED and status.get("state") == "failed"
              and status.get("stopped") is True and status.get("source") == "fragpipe_stop.py"
              and status.get("pid") == bare.pid
              and not os.path.exists(os.path.join(wd3, fs.STOP_NAME)), out + str(status))
        check("MSFragger's scratch files next to the inputs are listed", scratch in out, out)
        try:
            bare.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        check("and the run is gone", bare.poll() is not None
              and not fs.run_processes(fs.list_processes(), wd3))

        # a status file left from a crash, whose PID the system now uses elsewhere
        other = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(600)"])
        wd2 = os.path.join(tmp, "out2")
        os.makedirs(wd2)
        fs._write_json(os.path.join(wd2, fs.STATUS_NAME),
                       {"state": "running", "pid": other.pid, "watcher_pid": other.pid})
        rc, out = _quiet(fs.main, ["--workdir", wd2, "--yes"])
        check("a PID that is not FragPipe for this workdir is never killed",
              rc == fs.EXIT_NOTHING and other.poll() is None, out)
        rc, out = _quiet(fs.main, ["--pid", str(other.pid), "--yes"])
        check("nor one named with --pid (exit 4)",
              rc == fs.EXIT_NOTHING and other.poll() is None, out)

        # FragPipe itself ended alone: its tool is left with nothing tying it to the run
        wd5 = os.path.join(tmp, "out5")
        os.makedirs(wd5)
        fs._write_json(os.path.join(wd5, fs.STATUS_NAME), {"state": "running"})
        alone = subprocess.Popen([launcher, "--headless", "--workflow", wf, "--manifest", man,
                                  "--workdir", wd5], stdout=subprocess.DEVNULL,
                                 **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                    if os.name == "nt" else {"start_new_session": True}))
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.isfile(os.path.join(wd5, "fake_child.pid")):
            time.sleep(0.3)
        with open(os.path.join(wd5, "fake_child.pid")) as fh:
            orphan = int(fh.read())
        jvm = [x["pid"] for x in fs.list_processes()
               if x["ppid"] == alone.pid and "fake_fragpipe" in x["cmd"]]
        for pid in jvm:                     # ours: the fake FragPipe this test started
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, 9)
        try:
            alone.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        rc, out = _quiet(fs.main, ["--workdir", wd5, "--yes"])
        orphan_alive = orphan in fs.snapshot(fs.list_processes(), [orphan])
        check("when FragPipe was ended alone, the tool it left is named, not stopped (exit 1)",
              len(jvm) == 1 and rc == fs.EXIT_NOT_STOPPED and "PID %-7d" % orphan in out
              and orphan_alive, out)
        if orphan_alive and "fragger.params" in (fs.snapshot(fs.list_processes(), [orphan])
                                                 .get(orphan, ("",))[0]):
            if os.name == "nt":             # ours too: the fake tool started above
                subprocess.run(["taskkill", "/PID", str(orphan), "/F"], capture_output=True)
            else:
                os.kill(orphan, 9)

        # FragPipe's JVM ended alone under its watcher, which records the tool it left
        wd6 = os.path.join(tmp, "out6")
        with open(os.path.join(tmp, "watcher6.log"), "w") as log:
            watched = subprocess.Popen(
                [sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "fragpipe_run.py"),
                 "--workflow", wf, "--manifest", man, "--workdir", wd6,
                 "--fragpipe-dir", root, "--quiet"], stdout=log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline and not (
                os.path.isfile(os.path.join(wd6, "fake_child.pid"))
                and (fs.load_status(wd6) or {}).get("pid")
                and (os.name != "nt" or (fs.load_status(wd6) or {}).get("fragpipe_pids"))):
            time.sleep(0.3)
        status = fs.load_status(wd6) or {}
        with open(os.path.join(wd6, "fake_child.pid")) as fh:
            orphan = int(fh.read())
        jvm = [x["pid"] for x in fs.list_processes()
               if x["ppid"] == status.get("pid") and "fake_fragpipe" in x["cmd"]]
        for pid in jvm:                     # ours: the fake FragPipe this test started
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, 9)
        try:
            watched_rc = watched.wait(timeout=60)
        except subprocess.TimeoutExpired:
            watched_rc = None
        status = fs.load_status(wd6) or {}
        check("when FragPipe's JVM is ended alone, its watcher exits 1 and records the tool "
              "it left", len(jvm) == 1 and watched_rc == 1
              and [t.get("pid") for t in status.get("tools_left") or []] == [orphan]
              and "still running" in (status.get("error") or ""),
              "rc=%s %s" % (watched_rc, {k: status.get(k) for k in ("error", "tools_left")}))
        rc, out = _quiet(fr.main, ["--workflow", wf, "--manifest", man, "--workdir", wd6,
                                   "--fragpipe-dir", root, "--quiet"])
        check("a new run there is refused while that tool runs (exit 2)",
              rc == fr.EXIT_CONFIG_ERROR and "PID %d" % orphan in out, out[-300:])
        rc, out = _quiet(fs.main, ["--workdir", wd6])
        check("the stop shows that tool (exit 3)",
              rc == fs.EXIT_CONFIRM and "PID %-7d" % orphan in out
              and orphan in fs.snapshot(fs.list_processes(), [orphan]), out)
        rc, out = _quiet(fs.main, ["--workdir", wd6, "--yes"])
        status = fs.load_status(wd6) or {}
        check("and with --yes stops it (exit 0), and the status no longer says it runs",
              rc == fs.EXIT_STOPPED and orphan not in fs.snapshot(fs.list_processes(), [orphan])
              and status.get("stopped") is True and status.get("tools_left") == []
              and "still running" not in (status.get("error") or "")
              + (status.get("summary") or "")
              and fs.STOPPED_ERROR in (status.get("summary") or ""),
              out + str({k: status.get(k) for k in ("stopped", "tools_left", "summary")}))
        fs._write_json(os.path.join(wd6, fs.STATUS_NAME),
                       dict(status, host="another-computer", stopped=False,
                            tools_left=[{"pid": orphan, "cmd": "java", "created": None}]))
        rc, out = _quiet(fr.runs_in, wd6, os.path.join(wd6, fs.STATUS_NAME))
        check("a relaunch is warned of tools another computer's run left there",
              rc == ([], []) and "another-computer" in out, out)

        # a watcher holds the folder before FragPipe starts, over an earlier run's status
        wd7 = os.path.join(tmp, "out7")
        os.makedirs(wd7)
        fs._write_json(os.path.join(wd7, fs.STATUS_NAME),
                       {"state": "succeeded", "child_exit_code": 0, "watcher_pid": 1, "pid": 1,
                        "host": socket.gethostname()})
        standin_py = os.path.join(tmp, "standin", "fragpipe_run.py")
        os.makedirs(os.path.dirname(standin_py))
        with open(standin_py, "w", encoding="utf-8") as fh:
            fh.write(_STANDIN_WATCHER)
        standin = subprocess.Popen([sys.executable, "-I", standin_py,
                                    os.path.dirname(os.path.abspath(fs.__file__)),
                                    "--workdir", wd7])
        deadline = time.time() + 30
        while time.time() < deadline and not fs.watcher_holds(wd7):
            time.sleep(0.2)
        calls = []
        real_list, saved_start = fs.list_processes, fs.START_SEC
        try:
            fs.list_processes = lambda: calls.append(1) or real_list()
            fs.START_SEC = 1
            rc, out = _quiet(fs.main, ["--workdir", wd7])
        finally:
            fs.list_processes, fs.START_SEC = real_list, saved_start
        check("a stop while the watcher starts waits for FragPipe, even over an earlier "
              "run's final status (exit 1 when none comes)",
              len(calls) >= 2 and rc == fs.EXIT_NOT_STOPPED and "again" in out,
              "%d listings: %s" % (len(calls), out))
        standin.kill()                      # ours: the stand-in started above
        standin.wait(timeout=30)

        # started with a relative --workdir, which only Linux can resolve
        wd4 = os.path.join(tmp, "out4")
        os.makedirs(wd4)
        rel = subprocess.Popen([launcher, "--headless", "--workflow", wf, "--manifest", man,
                                "--workdir", "out4"], cwd=tmp, stdout=subprocess.DEVNULL,
                               **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                                  if os.name == "nt" else {"start_new_session": True}))
        deadline = time.time() + 60
        while time.time() < deadline and not os.path.isfile(os.path.join(wd4, "fake_child.pid")):
            time.sleep(0.3)
        rc, out = _quiet(fs.main, [])
        check("a run with a relative workdir is listed with its PID",
              rc == fs.EXIT_STOPPED and str(rel.pid) in out, out)
        rc, out = _quiet(fs.main, ["--workdir", wd4])
        if os.path.isdir("/proc/self"):
            check("on Linux its workdir is resolved from its own folder (exit 3)",
                  rc == fs.EXIT_CONFIRM, out)
        else:
            check("elsewhere its folder is not called idle: it is named as a run that may be "
                  "the one (exit 1)", rc == fs.EXIT_NOT_STOPPED and "--pid" in out
                  and str(rel.pid) in out, out)
        rc, out = _quiet(fs.main, ["--pid", str(rel.pid)])
        check("--pid without --yes stops nothing (exit 3)",
              rc == fs.EXIT_CONFIRM and rel.poll() is None, out)
        rc, out = _quiet(fs.main, ["--pid", str(rel.pid), "--yes"])
        try:
            rel.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        check("--pid --yes stops it and the tool it started",
              rc == fs.EXIT_STOPPED and rel.poll() is not None
              and not [h for h in fs.run_processes(fs.list_processes())
                       if h["pid"] == rel.pid], out)
    finally:
        started = [p.pid for p in (watcher, bare, rel, alone, watched) if p is not None]
        try:
            procs = fs.list_processes()
            left = [h for h in fs.run_processes(procs)
                    if any(h["pid"] in fs.tree(procs, pid) for pid in started)
                    or h["workdir"] and os.path.normcase(os.path.dirname(h["workdir"]))
                    == os.path.normcase(tmp)]
            # and the fake tools a failed check may have left: they sleep, naming tmp
            left += [dict(x, workdir=None) for x in procs if "time.sleep(600)" in x["cmd"]
                     and os.path.normcase(tmp) in os.path.normcase(x["cmd"])]
            if left:
                fs.kill(procs, left)
        except Exception:
            pass
        for proc in (watcher, other, bare, rel, alone, watched, standin):
            if proc is not None and proc.poll() is None:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def test_rebuild_shipped(workflows_dir: str, version: str) -> None:
    """Every shipped workflow comes back key for key from its own answers."""
    import build_workflow as bw
    import workflow_tree as wt
    print("\n[18] workflow builder: rebuild every shipped workflow (%s, %s)"
          % (version, workflows_dir))
    tree = wt.load_tree()
    names = sorted(f[:-len(".workflow")] for f in os.listdir(workflows_dir)
                   if f.endswith(".workflow"))
    routes = {r["workflow"]: r for r in tree["routes"]}
    check("every shipped workflow has a route", not set(names) - set(routes),
          "no route: %s" % sorted(set(names) - set(routes)))
    if set(routes) - set(names):
        print("  NOTE  not in this build, so the builder refuses them here: %s"
              % ", ".join(sorted(set(routes) - set(names))))
    routed = [n for n in names if n in routes]
    drifted = []
    for n in routed:
        why = bw.quant_drift(routes[n], bw.read_properties(os.path.join(workflows_dir,
                                                                         n + ".workflow")))
        if why:
            drifted.append("%s %s" % (n, why))
    check("every routed workflow quantifies as its route's quant answer says", not drifted,
          "; ".join(drifted[:4]))
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        data = os.path.join(tmp, "data", "plex1")
        os.makedirs(data)
        open(os.path.join(data, "a.mzML"), "w").close()
        fasta = os.path.join(tmp, "db.fas")
        open(fasta, "w").close()
        install = {"version": version, "workflows_dir": workflows_dir, "root": tmp}
        for name in names:
            if name not in routes:
                continue
            route, path = routes[name], os.path.join(workflows_dir, name + ".workflow")
            base = bw.read_properties(path)
            answers = {}
            for q, want in route["answers"].items():
                if isinstance(want, list):
                    plex = base.get("tmtintegrator.channel_num", "").replace("TMT-", "")
                    want = plex if q == "tmt_plex" and plex in want else want[0]
                answers[q] = want
            answers["fasta"] = fasta
            answers["remove_contaminants"] = \
                "yes" if base.get("phi-report.remove-contaminants") == "true" else "no"
            plan = {"answers": answers, "data": [os.path.dirname(data)]}
            wd = os.path.join(tmp, "w_" + name)
            res = bw.evaluate(tree, install, plan, wd)
            for q in res["open"]:
                if q["id"] in ("design", "channels"):
                    plan[q["id"]] = q["path"]
                    if q["id"] == "channels" and base.get("tmtintegrator.add_Ref") == "-1":
                        tag = base.get("tmtintegrator.ref_tag", "")
                        with open(q["path"], "r", encoding="utf-8") as fh:
                            text = fh.read()
                        with open(q["path"], "w", encoding="utf-8") as fh:
                            fh.write(text.replace("\tplex1_126\t", "\t%s_1\t" % tag, 1))
            if plan.get("design") or plan.get("channels"):
                res = bw.evaluate(tree, install, plan, wd)
            if res["state"] != "built":
                failures.append("%s: %s %s" % (name, [q["id"] for q in res["open"]],
                                               res["problems"]))
                continue
            if [c["key"] for c in res["changes"]] != ["database.db-path"]:
                failures.append("%s changes %s" % (name, [c["key"] for c in res["changes"]]))
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
                text = bw.render_workflow(fh.read(), {c["key"]: c["value"]
                                                      for c in res["changes"]}, ["# x"])
            rebuilt = os.path.join(tmp, name + ".rebuilt")
            with open(rebuilt, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            got = bw.read_properties(rebuilt)
            want = dict(base)
            want["database.db-path"] = fasta
            if dict(got) != want:
                diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
                failures.append("%s differs in %s" % (name, diff[:4]))
    check("all %d routed shipped workflows rebuild key for key" % len(routed), not failures,
          "; ".join(failures[:4]))


def test_renames(dir_241: str, dir_250: str) -> None:
    """The 24.1 -> 25.0 rename table accounts for every key difference."""
    import build_workflow as bw
    import workflow_tree as wt
    print("\n[19] workflow builder: 24.1 -> 25.0 key renames")
    tree = wt.load_tree()
    common = sorted(set(os.listdir(dir_241)) & set(os.listdir(dir_250)))
    common = [f for f in common if f.endswith(".workflow")]
    bad = []
    for f in common:
        old = bw.read_properties(os.path.join(dir_241, f))
        new = bw.read_properties(os.path.join(dir_250, f))
        mapped = {wt.rename_key(tree, "25.0", k): v for k, v in old.items()}
        for d in (mapped, new):
            d.pop("workflow.saved-with-ver", None)
        if mapped != dict(new):
            keys = sorted(k for k in set(mapped) | set(new) if mapped.get(k) != new.get(k))
            bad.append("%s: %s" % (f, keys[:4]))
    check("renamed 24.1 workflows equal the 25.0 ones (%d compared)" % len(common),
          bool(common) and not bad, "; ".join(bad[:3]))


def _builder_installs(explicit: list) -> list:
    """FragPipe installs the rebuild test reads (it only reads their workflows/)."""
    import check_fragpipe as cf
    import workflow_tree as wt
    tree = wt.load_tree()
    found = [cf.install_from_root(d) for d in explicit] if explicit else cf.find_all_installs()
    return [i for i in found if i and i.get("workflows_dir")
            and wt.supported_version(tree, i.get("version"))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FragSkills self-test.")
    ap.add_argument("--example", nargs="*", default=[], metavar="DIR",
                    help="one or more finished FragPipe output directories; "
                         "give one per analysis type to cover every layout")
    ap.add_argument("--fragpipe-dir", action="append", default=[],
                    help="FragPipe install(s) whose workflows/ the builder rebuild test "
                         "reads (default: every supported install found)")
    args = ap.parse_args(argv)
    try:                        # a failing check's detail must print on any console
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

    workdirs = [os.path.abspath(e) for e in args.example]
    missing = [w for w in workdirs if not os.path.isdir(w)]
    if missing:
        print("not a directory: %s" % ", ".join(missing), file=sys.stderr)
        return 2

    for workdir in workdirs:
        if len(workdirs) > 1:
            print("\n%s\n=== example: %s\n%s" % ("=" * 70, workdir, "=" * 70))
        log_path = find_log(workdir)
        full = test_full_log(log_path)
        test_midrun(log_path, full)
        test_outputs(workdir)
        test_run_type(workdir)
        test_qc_series(workdir)
        test_report(workdir)

    if len(workdirs) > 1:
        print("\n%s\n=== layout-independent checks\n%s" % ("=" * 70, "=" * 70))
    test_manifest_roundtrip()
    test_fragpipe_location()
    test_commands()
    test_classification_precedence()
    test_quant_matrix()
    test_workflow_tree()
    test_properties_io()
    test_design_sheet()
    test_builder_flow()
    test_tmt_annotations()
    test_stop()

    installs = _builder_installs(args.fragpipe_dir)
    if not installs:
        print("\n[18] SKIP  rebuild test: no FragPipe 24.1/25.0 install found "
              "(pass --fragpipe-dir)")
    for inst in installs:
        test_rebuild_shipped(inst["workflows_dir"], inst["version"])
    dev = os.environ.get("FRAGSKILLS_FP25_WORKFLOWS")
    if dev and os.path.isdir(dev):
        test_rebuild_shipped(dev, "25.0-develop")
        v241 = [i for i in installs if (i.get("version") or "").startswith("24.1")]
        if v241:
            test_renames(v241[0]["workflows_dir"], dev)
        else:
            print("\n[19] SKIP  rename check: needs a FragPipe 24.1 install beside "
                  "FRAGSKILLS_FP25_WORKFLOWS (pass --fragpipe-dir)")
    elif dev:
        print("\n[19] SKIP  25.0 checks: FRAGSKILLS_FP25_WORKFLOWS=%s is not a folder" % dev)
    else:
        print("\n[19] SKIP  25.0 checks: set FRAGSKILLS_FP25_WORKFLOWS to FragPipe "
              "develop's workflows/ folder")

    print("\n%s" % ("-" * 60))
    if _failures:
        print("FAILED: %d of %d checks" % (len(_failures), _checks))
        for f in _failures:
            print("  - %s" % f)
        return 1
    print("OK: all %d checks passed" % _checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
