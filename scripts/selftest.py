"""Regression self-test for FragSkill, driven by a real FragPipe run.

Point it at a finished FragPipe output directory containing a ``log_*.txt`` and
the report TSVs::

    python scripts/selftest.py --example I:/test_fragskill/dda

It checks the log parser (full and mid-run), the success/failure decision, the
output discovery, and the report summaries.  Run it after changing any script,
especially when adding TMT or DIA support.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fragpipe_progress import (  # noqa: E402
    RunProgress, parse_log, STATE_FAILED, STATE_RUNNING, STATE_SUCCEEDED,
)
import parse_reports  # noqa: E402

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
        hits = [p for p in [os.path.join(workdir, "fragskill_run.log")]
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
            check("protein.tsv row count equals psm.tsv distinct protein IDs",
                  s_prot["protein_groups"] == s_psm["unique_protein_ids"],
                  "%s vs %s" % (s_prot["protein_groups"],
                                s_psm["unique_protein_ids"]))
        break  # one experiment is enough for a smoke test


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FragSkill self-test.")
    ap.add_argument("--example", required=True,
                    help="a finished FragPipe output directory")
    args = ap.parse_args(argv)

    workdir = os.path.abspath(args.example)
    if not os.path.isdir(workdir):
        print("not a directory: %s" % workdir, file=sys.stderr)
        return 2

    log_path = find_log(workdir)
    full = test_full_log(log_path)
    test_midrun(log_path, full)
    test_outputs(workdir)
    test_manifest_roundtrip()
    test_fragpipe_location()

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
