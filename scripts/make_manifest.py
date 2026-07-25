"""Build a FragPipe ``.fp-manifest`` file.

The manifest is a headerless, tab-separated file with exactly four columns::

    <absolute path>\t<experiment>\t<bioreplicate>\t<data type>

``experiment`` and ``bioreplicate`` may be empty; ``data type`` is one of
DDA, DDA+, DIA, GPF-DIA, DIA-Quant, DIA-Lib (case-insensitive on input --
anything unrecognised is silently treated as DDA by FragPipe).

The experiment name controls the output layout, so it is worth getting right:
FragPipe writes per-experiment reports into ``<workdir>/<experiment>/``.  See
references/outputs.md.

Examples::

    # one experiment per file, named from the file stem
    python scripts/make_manifest.py --input D:/data --experiment-from stem \
        --out D:/run/files.fp-manifest

    # one experiment per containing folder, DIA data
    python scripts/make_manifest.py --input D:/data --recursive \
        --experiment-from parent --data-type DIA --out D:/run/files.fp-manifest

    # everything in a single unnamed experiment (reports land at workdir root)
    python scripts/make_manifest.py --input D:/data --out D:/run/files.fp-manifest
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from typing import Iterable, List, Optional, Tuple

#: Extensions FragPipe accepts as spectral input.
SPECTRA_EXT = (".mzml", ".mzxml", ".raw", ".d", ".mgf")

VALID_DATA_TYPES = ("DDA", "DDA+", "DIA", "GPF-DIA", "DIA-Quant", "DIA-Lib")
_DATA_TYPE_LOOKUP = {t.lower(): t for t in VALID_DATA_TYPES}

#: FragPipe replaces every character outside [A-Za-z0-9_] in experiment names.
_EXPERIMENT_SANITIZE = re.compile(r"[^A-Za-z0-9_]")
#: Characters FragPipe rejects in the *file name* itself.
_FILENAME_ALLOWED = re.compile(r"^[A-Za-z0-9_.\[\]()+\-]+$")


def sanitize_experiment(name: str) -> str:
    return _EXPERIMENT_SANITIZE.sub("_", (name or "").strip())


def collect_files(inputs: Iterable[str], recursive: bool) -> List[str]:
    """Expand directories, globs, and plain paths into a sorted file list."""
    out: List[str] = []
    for item in inputs:
        item = os.path.expanduser(item)
        if os.path.isdir(item):
            # Bruker .d is a directory, not a file -- treat it as a leaf.
            if item.lower().rstrip("\\/").endswith(".d"):
                out.append(item)
                continue
            pattern = "**/*" if recursive else "*"
            for hit in glob.glob(os.path.join(item, pattern), recursive=recursive):
                if _is_spectra(hit):
                    out.append(hit)
        elif any(ch in item for ch in "*?["):
            for hit in glob.glob(item, recursive=recursive):
                if _is_spectra(hit):
                    out.append(hit)
        elif os.path.exists(item):
            out.append(item)
        else:
            print("[make_manifest] warning: no such path, skipped: %s" % item,
                  file=sys.stderr)
    # de-duplicate, keep deterministic order
    seen, unique = set(), []
    for p in sorted(os.path.abspath(p) for p in out):
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _is_spectra(path: str) -> bool:
    low = path.lower().rstrip("\\/")
    if low.endswith(".d"):
        return os.path.isdir(path)
    return os.path.isfile(path) and low.endswith(SPECTRA_EXT)


def _stem(path: str) -> str:
    base = os.path.basename(path.rstrip("\\/"))
    return os.path.splitext(base)[0]


def derive_experiment(path: str, mode: str, fixed: Optional[str],
                      regex: Optional[str]) -> str:
    if mode == "none":
        return ""
    if mode == "fixed":
        return sanitize_experiment(fixed or "")
    if mode == "stem":
        return sanitize_experiment(_stem(path))
    if mode == "parent":
        return sanitize_experiment(os.path.basename(os.path.dirname(path.rstrip("\\/"))))
    if mode == "regex":
        m = re.search(regex or "", _stem(path))
        if not m:
            return ""
        return sanitize_experiment(m.group(1) if m.groups() else m.group(0))
    raise ValueError("unknown --experiment-from mode: %s" % mode)


def check_filenames(paths: Iterable[str]) -> List[str]:
    """FragPipe rejects odd characters and extra dots in spectral file names."""
    problems = []
    for p in paths:
        base = os.path.basename(p.rstrip("\\/"))
        stem, ext = os.path.splitext(base)
        if "." in stem:
            problems.append("%s -- file name has multiple dots; FragPipe will "
                            "refuse it. Rename to remove the extra dots." % base)
        if not _FILENAME_ALLOWED.match(base):
            problems.append("%s -- file name has characters outside "
                            "A-Z a-z 0-9 _ . [ ] ( ) + -" % base)
    return problems


def build_rows(paths: List[str], experiment_mode: str, fixed: Optional[str],
               regex: Optional[str], replicate: Optional[str],
               data_type: Optional[str]) -> List[Tuple[str, str, str, str]]:
    rows = []
    for p in paths:
        exp = derive_experiment(p, experiment_mode, fixed, regex)
        rep = ""
        if replicate == "index":
            rep = ""  # filled in below, per experiment
        elif replicate:
            rep = str(replicate)
        dt = _DATA_TYPE_LOOKUP.get((data_type or "DDA").lower(), "DDA")
        rows.append((p, exp, rep, dt))

    if replicate == "index":
        counters = {}
        numbered = []
        for p, exp, _, dt in rows:
            counters[exp] = counters.get(exp, 0) + 1
            numbered.append((p, exp, str(counters[exp]), dt))
        rows = numbered
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Create a FragPipe .fp-manifest file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--input", "-i", nargs="+", required=True,
                    help="files, directories, or globs of spectral data")
    ap.add_argument("--out", "-o", required=True, help="manifest path to write")
    ap.add_argument("--recursive", "-r", action="store_true",
                    help="descend into subdirectories")
    ap.add_argument("--experiment-from", default="none",
                    choices=["none", "stem", "parent", "fixed", "regex"],
                    help="how to name the experiment for each file "
                         "(default: none = single unnamed experiment)")
    ap.add_argument("--experiment", help="name used with --experiment-from fixed")
    ap.add_argument("--experiment-regex",
                    help="pattern used with --experiment-from regex; group 1 "
                         "(or the whole match) becomes the experiment name")
    ap.add_argument("--replicate",
                    help="bioreplicate value: a literal number for all files, "
                         "or 'index' to number files within each experiment")
    ap.add_argument("--data-type", default="DDA",
                    help="one of %s (default DDA)" % ", ".join(VALID_DATA_TYPES))
    ap.add_argument("--force", action="store_true",
                    help="write even if file-name validation finds problems")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the manifest instead of writing it")
    args = ap.parse_args(argv)

    if args.data_type.lower() not in _DATA_TYPE_LOOKUP:
        print("[make_manifest] ERROR: --data-type must be one of %s"
              % ", ".join(VALID_DATA_TYPES), file=sys.stderr)
        return 2
    if args.experiment_from == "fixed" and not args.experiment:
        print("[make_manifest] ERROR: --experiment-from fixed requires --experiment",
              file=sys.stderr)
        return 2
    if args.experiment_from == "regex" and not args.experiment_regex:
        print("[make_manifest] ERROR: --experiment-from regex requires "
              "--experiment-regex", file=sys.stderr)
        return 2

    paths = collect_files(args.input, args.recursive)
    if not paths:
        print("[make_manifest] ERROR: no spectral files found (looked for %s)"
              % ", ".join(SPECTRA_EXT), file=sys.stderr)
        return 2

    problems = check_filenames(paths)
    if problems:
        for p in problems:
            print("[make_manifest] WARNING: %s" % p, file=sys.stderr)
        if not args.force:
            print("[make_manifest] refusing to write; fix the names or pass "
                  "--force", file=sys.stderr)
            return 2

    rows = build_rows(paths, args.experiment_from, args.experiment,
                      args.experiment_regex, args.replicate, args.data_type)
    text = "".join("%s\t%s\t%s\t%s\n" % r for r in rows)

    if args.dry_run:
        sys.stdout.write(text)
    else:
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        # FragPipe reads this with a Java reader; keep it plain ASCII-safe UTF-8
        # with Unix newlines, which it handles on every platform.
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("[make_manifest] wrote %d file(s) to %s" % (len(rows), out))

    exps = sorted({r[1] for r in rows})
    shown = [e or "(unnamed)" for e in exps]
    print("[make_manifest] %d experiment(s): %s" % (len(exps), ", ".join(shown)))
    if exps == [""]:
        print("[make_manifest] note: with a single unnamed experiment, reports "
              "are written to the workdir root, not a subfolder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
