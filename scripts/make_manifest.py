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

    # propose a design sheet from the file names, edit it, then build from it
    python scripts/make_manifest.py --input D:/data -r --propose-design \
        --out D:/run/design.tsv
    python scripts/make_manifest.py --design D:/run/design.tsv \
        --out D:/run/files.fp-manifest

A design sheet is tab-separated with a header row naming four columns, in any
order: ``file``, ``condition`` (becomes the experiment), ``replicate`` (the
bioreplicate, an integer or empty) and ``data_type``.  Lines starting with
``#`` are comments.
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
# InputLcmsFile.dirDisallowedChars: headless FragPipe exits before searching
# when a spectral file's folder holds anything else (a space, a non-ASCII letter).
_FOLDER_DISALLOWED = re.compile(r"[^A-Za-z0-9_.\[\]()+\-/\\:]")
#: Files MSFragger and diaTracer write next to the inputs; FragPipe skips them when
#: adding a folder (diaTracer's <run>_diatracer.mzML from 25.0, DiaTracerOutput.isOutput).
_FRAGPIPE_WRITTEN = ("_calibrated.mzml", "_uncalibrated.mzml", "_calibrated.mgf",
                     "_uncalibrated.mgf", "_diatracer.mzml")
#: DIA-Umpire SE writes <run>_Q1/_Q2/_Q3 .mzML and .mgf beside the run it reads; FragPipe
#: moves them into the workdir, but a stopped run, or a workdir that is the data folder,
#: leaves them there, and FragPipe loads them as more runs when the folder is added again.
_UMPIRE_OUTPUT = re.compile(r"(?P<stem>.+)_Q(?P<n>[123])\.(?:mzml|mgf)\Z", re.I)
_UMPIRE_INPUT = (".mzml", ".mzxml", ".raw")
#: Characters Windows' Paths.get refuses in a path (besides ':' after the drive).
_WINDOWS_BAD = re.compile(r'[<>"|?*\x00-\x1f]')

#: Java's String.trim() removes every char <= ' '; its "\\s" is ASCII whitespace only.
JAVA_TRIM = "".join(chr(c) for c in range(0x21))
_JAVA_WHITESPACE = re.compile(r"[ \t\n\x0b\f\r]+")


def java_tokens(line: str) -> List[str]:
    """``line.trim().split("\\s+")`` as Java does it."""
    line = line.strip(JAVA_TRIM)
    return _JAVA_WHITESPACE.split(line) if line else []


def sanitize_experiment(name: str) -> str:
    return _EXPERIMENT_SANITIZE.sub("_", (name or "").strip())


def _walk(folder: str, recursive: bool) -> List[str]:
    """Spectral files in *folder*, the way FragPipe adds a folder: a ``.d`` run
    is taken whole and never entered, MSFragger's calibrated copies are skipped,
    and linked folders are followed (each real folder once)."""
    hits, seen = [], set()
    for root, dirs, files in os.walk(folder, followlinks=True):
        real = os.path.normcase(os.path.realpath(root))
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        deeper = []
        for d in sorted(dirs):
            if d.lower().endswith(".d"):
                hits.append(os.path.join(root, d))
            elif recursive:
                deeper.append(d)
        dirs[:] = deeper
        for f in files:
            low = f.lower()
            if low.endswith(SPECTRA_EXT) and not low.endswith(".d") \
                    and not low.endswith(_FRAGPIPE_WRITTEN) \
                    and not _skip_umpire_output(os.path.join(root, f), files):
                hits.append(os.path.join(root, f))
    return hits


def umpire_source(path: str, names: Optional[Iterable[str]] = None) -> Optional[str]:
    """The run DIA-Umpire made *path* from, when that run is in the same folder (*names*:
    the folder's file names, listed when not given); None for any other file, so a run
    that is only named like one (``plasma_Q1.mzML``) is kept."""
    m = _UMPIRE_OUTPUT.match(os.path.basename(path.rstrip("\\/")))
    if not m:
        return None
    if names is None:
        try:
            names = os.listdir(os.path.dirname(os.path.abspath(path)))
        except OSError:
            return None
    have = {n.lower(): n for n in names}
    return next((have[k] for k in (m.group("stem").lower() + e for e in _UMPIRE_INPUT)
                 if k in have), None)


def _skip_umpire_output(path: str, names: Optional[Iterable[str]] = None) -> bool:
    source = umpire_source(path, names)
    if source:
        print("[make_manifest] skipped DIA-Umpire's output of %s: %s" % (source, path),
              file=sys.stderr)
    return bool(source)


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
            out += _walk(item, recursive)
        elif any(ch in item for ch in "*?["):
            for hit in glob.glob(item, recursive=recursive):
                if _is_spectra(hit) and not hit.lower().endswith(_FRAGPIPE_WRITTEN) \
                        and not _skip_umpire_output(hit):
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


def check_folders(paths: Iterable[str], base: Optional[str] = None) -> List[str]:
    """FragPipe also refuses odd characters anywhere in the folder path.

    It checks ``toAbsolutePath().getParent()``: a relative path is taken from
    *base* (FragPipe's working folder) and ``..`` is not resolved first.
    """
    problems, seen = [], set()
    for p in paths:
        folder = os.path.dirname(os.path.join(base or os.getcwd(), p.rstrip("\\/")))
        if folder in seen:
            continue
        seen.add(folder)
        bad = sorted(set(_FOLDER_DISALLOWED.findall(folder)))
        if bad:
            problems.append("%s -- folder has characters FragPipe refuses (%s); headless "
                            "FragPipe exits before searching. Allowed: A-Z a-z 0-9 _ . [ ] "
                            "( ) + - / \\ :. Move or rename the folder."
                            % (folder, " ".join(repr(c) for c in bad)))
    return problems


def check_duplicate_names(paths: Iterable[str]) -> List[str]:
    """FragPipe refuses two input files with the same name in different folders
    (``FragpipeRun.checkInputLcmsFiles2``, which compares names case-sensitively)."""
    by_name = {}
    for p in paths:
        by_name.setdefault(os.path.basename(p.rstrip("\\/")), []).append(p)
    return ["%d inputs are named %s (%s); FragPipe refuses two inputs with the same file "
            "name, even in different folders. Rename or remove them."
            % (len(v), os.path.basename(v[0].rstrip("\\/")), ", ".join(sorted(set(v))))
            for v in by_name.values() if len(v) > 1]


def check_same_stem(paths: Iterable[str], base: Optional[str] = None) -> List[str]:
    """Inputs in one folder that differ only in extension (``ctrl_1.raw`` and
    ``ctrl_1.mzML``): MSFragger writes ``<stem>.pepXML`` beside each, so one search
    overwrites the other, and FragPipe's name check (whole names) lets them pass."""
    by_stem = {}
    for p in paths:
        full = os.path.join(base or os.getcwd(), p.rstrip("\\/"))
        stem = os.path.splitext(os.path.basename(full))[0]
        by_stem.setdefault((os.path.normcase(os.path.dirname(full)), os.path.normcase(stem)),
                           []).append(p)
    return ["%s differ only in extension; MSFragger writes one %s.pepXML for them, so one "
            "search overwrites the other. Keep one."
            % (" and ".join(os.path.basename(f.rstrip("\\/")) for f in v),
               os.path.splitext(os.path.basename(v[0].rstrip("\\/")))[0])
            for v in by_stem.values() if len(v) > 1]


def check_paths(paths: Iterable[str], base: Optional[str] = None,
                name_rules: bool = True) -> List[str]:
    """Every name and folder rule FragPipe applies to spectral file paths, and inputs
    whose searches would overwrite each other.

    *name_rules* False leaves out the file and folder character rules, which
    headless FragPipe 24.0 and 24.1-build1..7 do not apply.
    """
    paths = list(paths)
    rules = check_filenames(paths) + check_folders(paths, base) if name_rules else []
    return rules + check_duplicate_names(paths) + check_same_stem(paths, base)


def has_name_rules(version: Optional[str]) -> bool:
    """Does headless FragPipe *version* refuse bad file and folder names?  It does from
    24.1-build8 on (commit fa95dd6c5); 24.0 and 24.1-build1..7 load them.  An unknown
    version, or 24.1 with no build number, is taken to have the check."""
    if not version:
        return True
    return _from_24_1_build(version, 8)


def has_diatracer_filter(version: Optional[str]) -> bool:
    """Does FragPipe *version* refuse diaTracer's ``_diatracer.mzML`` as an input?
    24.1-build61 and 25.0 do (commit befbc48cb); 24.0 and 24.1 up to build60 load it,
    and their diaPASEF workflow notes say to reuse it that way, typed DDA beside the
    ``.d`` typed DIA-Quant.  An unknown version is taken to refuse it."""
    return _from_24_1_build(version, 61)


def _from_24_1_build(version: Optional[str], build: int) -> bool:
    """Is *version* 24.1-build<*build*> or later (24.1 without a build, or unknown: yes)?"""
    if not version:
        return True
    key = tuple(int(n) for n in re.findall(r"\d+", version))
    if key[:2] != (24, 1):
        return key[:2] > (24, 1)
    return len(key) < 3 or key[2] >= build


DESIGN_COLUMNS = ("file", "condition", "replicate", "data_type")

#: A trailing replicate number: "ctrl_rep2", "HeLa_R1", "sample03".  The digits
#: must not continue a longer number, so a date or run counter such as
#: "..._122212" is not mistaken for replicate 212.
_REPLICATE_SUFFIX = re.compile(
    r"^(?P<cond>.*?)[_\-. ]*(?:replicate|rep|bio|br|r)?[_\-]?(?<!\d)(?P<rep>\d{1,3})$",
    re.IGNORECASE)


def canonical_data_type(value: str) -> Optional[str]:
    """'dia-quant' -> 'DIA-Quant'; None when FragPipe would not recognise it."""
    return _DATA_TYPE_LOOKUP.get((value or "").strip().lower())


def propose_design(paths: List[str], data_type: str = "DDA") -> List[dict]:
    """Guess condition and replicate from each file name, for the user to edit.

    A trailing number becomes the replicate and the rest of the name the
    condition; a name without one is its own condition, numbered in order.
    """
    rows, counters = [], {}
    for p in paths:
        stem = _stem(p)
        m = _REPLICATE_SUFFIX.match(stem)
        cond = m.group("cond").strip("_-. ") if m else ""
        if cond:
            rep = str(int(m.group("rep")))
        else:
            cond, rep = stem, None
        cond = sanitize_experiment(cond)
        if rep is None:
            counters[cond] = counters.get(cond, 0) + 1
            rep = str(counters[cond])
        rows.append({"file": p, "condition": cond, "replicate": rep,
                     "data_type": data_type})
    return rows


def write_design(path: str, rows: List[dict], comment: str = "") -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in comment.splitlines():
            fh.write("# %s\n" % line)
        fh.write("\t".join(DESIGN_COLUMNS) + "\n")
        for r in rows:
            fh.write("\t".join(str(r.get(c, "")) for c in DESIGN_COLUMNS) + "\n")


def read_design(path: str, allowed_types: Iterable[str] = VALID_DATA_TYPES,
                groups_collapse: bool = False) -> Tuple[List[dict], List[str]]:
    """Parse and validate a design sheet.  Returns ``(rows, problems)``.

    *groups_collapse*: the workflow runs DIA-NN, FragDIA or the spectral library
    builder, so FragPipe puts every file in one group and an unnamed experiment
    beside named ones is fine.
    """
    allowed = list(allowed_types)
    rows, problems, header = [], [], None
    seen = set()
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except UnicodeDecodeError:
        return [], ["%s is not UTF-8 text. Save it as tab-separated UTF-8." % path]
    except OSError as exc:
        return [], ["cannot read the design sheet %s (%s)" % (path, exc.strerror or exc)]
    for n, line in enumerate(lines, 1):
        line = line.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if header is None:
            header = [c.lower() for c in cells]
            missing = [c for c in DESIGN_COLUMNS if c not in header]
            if missing:
                problems.append("%s: header is missing column(s) %s (need %s)"
                                % (path, ", ".join(missing), ", ".join(DESIGN_COLUMNS)))
                return [], problems
            continue
        cells += [""] * (len(header) - len(cells))
        rec = dict(zip(header, cells))
        where = "%s line %d" % (os.path.basename(path), n)
        f = os.path.abspath(os.path.expanduser(rec["file"])) if rec["file"] else ""
        if not f:
            problems.append("%s: empty file" % where)
            continue
        if not os.path.exists(f):
            problems.append("%s: no such file %s" % (where, f))
        if os.path.normcase(f) in seen:
            problems.append("%s: %s is listed twice" % (where, f))
        seen.add(os.path.normcase(f))
        dt = canonical_data_type(rec["data_type"])
        if dt is None:
            problems.append("%s: data type '%s' is not one of %s"
                            % (where, rec["data_type"], ", ".join(VALID_DATA_TYPES)))
        elif dt not in allowed:
            problems.append("%s: data type %s does not fit this workflow (allowed: %s)"
                            % (where, dt, ", ".join(allowed)))
        rep = rec["replicate"]
        # isdigit() would pass '²', which int() refuses; FragPipe reads a Java int
        whole = rep.isdecimal() and len(rep) <= 10 and int(rep) < 2 ** 31
        if rep and not whole:
            problems.append("%s: replicate '%s' is not a whole number" % (where, rep))
        rows.append({"file": f, "condition": sanitize_experiment(rec["condition"]),
                     "replicate": str(int(rep)) if whole else "",
                     "data_type": dt or rec["data_type"]})
    if header is None:
        problems.append("%s: empty design sheet" % path)
    elif not rows:
        problems.append("%s: no files listed" % path)
    blank = [r for r in rows if not r["condition"] and not r["replicate"]]
    if blank and len(blank) < len(rows) and not groups_collapse:
        problems.append("%s: %d file(s) have neither condition nor replicate (%s) while the "
                        "others do; FragPipe refuses an unnamed experiment next to named "
                        "ones (unless the workflow runs DIA-NN, FragDIA or builds a spectral "
                        "library). Give every file a condition, or none."
                        % (os.path.basename(path), len(blank),
                           ", ".join(os.path.basename(r["file"]) for r in blank[:5])))
    problems += check_duplicate_names(r["file"] for r in rows)
    return rows, problems


def write_manifest(path: str, rows: Iterable[Tuple[str, str, str, str]]) -> None:
    out = os.path.abspath(path)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    # FragPipe reads this with a Java reader; keep it plain ASCII-safe UTF-8
    # with Unix newlines, which it handles on every platform.  A line starting
    # with // is a comment to FragPipe, so a POSIX //path is written as /path.
    def _path(p: str) -> str:
        return "/" + p.lstrip("/") if os.sep == "/" and p.startswith("//") else p
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join("%s\t%s\t%s\t%s\n" % ((_path(r[0]),) + tuple(r[1:])) for r in rows))


_JAVA_INT = re.compile(r"[+-]?\d+\Z")       # Integer.parseInt takes a Unicode digit
_JAVA_BLANK = re.compile(r"[ \t\n\x0b\f\r]*\Z")     # StringUtils.isNotBlank: ASCII \s


def _java_parses_int(cell: str) -> bool:
    """Would Java's Integer.parseInt accept *cell* as it is (no trimming)?  It reads
    UTF-16 chars, so a digit outside the Basic Multilingual Plane is not one."""
    return len(cell) <= 11 and bool(_JAVA_INT.match(cell)) \
        and all(ord(c) < 0x10000 for c in cell) and -2 ** 31 <= int(cell) < 2 ** 31


def _windows_path_problem(cell: str) -> Optional[str]:
    """Why ``Paths.get`` would refuse *cell* on Windows, or None."""
    rest = cell[2:] if re.match(r"[A-Za-z]:", cell) else cell
    bad = _WINDOWS_BAD.search(rest) or re.search(":", rest)
    if bad:
        return "holds %r" % bad.group(0)
    if any(seg.endswith(" ") for seg in re.split(r"[\\/]", rest)):
        return "has a folder or file name ending in a space"
    return None


def check_manifest(path: str, base: Optional[str] = None, name_rules: bool = True,
                   diatracer_filter: bool = True) -> List[str]:
    """Rows FragPipe would misread, drop or stop on, often without saying so.

    Mirrors ``TabWorkflow.manifestLoad`` and ``InputLcmsFile`` (24.1): lines
    starting with ``#`` or ``//`` are comments; a line is trimmed (Java's trim,
    which keeps a non-breaking space) and split on tabs; a bioreplicate goes to
    ``Integer.parseInt`` untrimmed and a failure drops the row; an unrecognised
    data type becomes DDA, and an empty one is guessed from the file name (a
    ``.d`` folder becomes DIA).  The file is read as UTF-8, so a byte-order mark
    spoils the first path.  Folder and file names FragPipe refuses (when
    *name_rules*), repeated file names, missing files, diaTracer's output (when
    *diatracer_filter*) and DIA-Umpire's leftover output are reported too; a
    relative path is taken from *base*, the folder FragPipe runs in.
    """
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except UnicodeDecodeError:
        return ["%s is not UTF-8 text; FragPipe reads manifests as UTF-8. Save it as UTF-8."
                % path]
    except OSError as exc:
        return ["cannot read %s (%s)" % (path, exc.strerror or exc)]
    problems, paths, rows = [], [], []
    if text.startswith("\ufeff"):
        problems.append("%s starts with a byte-order mark, so FragPipe cannot read the first "
                        "row's path and drops it. Save the file as UTF-8 without BOM." % path)
        text = text[1:]
    for n, line in enumerate(re.split(r"\r\n|\r|\n", text), 1):
        if _JAVA_BLANK.match(line):
            continue
        if not line.strip(JAVA_TRIM):
            problems.append("manifest line %d holds only control characters (such as an "
                            "end-of-file Ctrl-Z); FragPipe loads it as an empty path, which "
                            "is the folder it runs in. Remove the line." % n)
            continue
        if line.startswith("#"):
            continue
        if line.startswith("//"):
            first = line.split("\t")[0].strip(JAVA_TRIM)
            if _is_spectra(first):              # a POSIX //path, not a comment line
                problems.append("manifest line %d: FragPipe skips lines starting with // as "
                                "comments, so %s would not be analysed. Write the path "
                                "another way." % (n, first))
            continue
        cells = line.strip(JAVA_TRIM).split("\t")
        while cells and cells[-1] == "":                   # Java's split() drops these
            cells.pop()
        if not cells:
            continue
        name = os.path.basename(cells[0].rstrip("\\/")) or "(no path)"
        bad_path = _windows_path_problem(cells[0]) if os.name == "nt" else None
        if bad_path:
            problems.append("manifest line %d (%s): the path %s, which Java cannot read on "
                            "Windows, so FragPipe drops the row with only a logged error"
                            % (n, name, bad_path))
        else:                                   # a dropped row meets no name rule
            paths.append(cells[0])
            full = os.path.join(base or os.getcwd(), cells[0])
            if not os.path.exists(full):
                problems.append("manifest line %d (%s): no such file or folder %s; headless "
                                "FragPipe stops on it" % (n, name, full))
        if diatracer_filter and name.lower().endswith("_diatracer.mzml"):
            problems.append("manifest line %d (%s): this is diaTracer's output for the .d "
                            "beside it, not a run, and this FragPipe stops on it. List the .d "
                            "with data type DIA instead." % (n, name))
        rep = cells[2] if len(cells) > 2 else ""
        dtype = cells[3].strip(JAVA_TRIM) if len(cells) > 3 else ""
        if not bad_path:
            rows.append((n, name, os.path.normcase(os.path.abspath(full)), dtype))
        if rep.strip(JAVA_TRIM) and not _java_parses_int(rep):
            problems.append("manifest line %d (%s): bioreplicate '%s' is not a plain "
                            "integer (no spaces), so FragPipe drops the row"
                            % (n, name, rep))
        if not dtype:
            problems.append("manifest line %d (%s): no data type, so FragPipe guesses "
                            "one from the file name; set it explicitly" % (n, name))
        elif dtype.lower() not in _DATA_TYPE_LOOKUP:        # no Python strip: Java's keeps NBSP
            problems.append("manifest line %d (%s): unknown data type '%s', which "
                            "FragPipe silently treats as DDA (valid: %s)"
                            % (n, name, dtype, ", ".join(VALID_DATA_TYPES)))
    return problems + _umpire_rows(rows) + check_paths(paths, base, name_rules)


def _umpire_rows(rows: List[Tuple[int, str, str, str]]) -> List[str]:
    """Rows that are DIA-Umpire's leftover output of a run beside them (FragPipe loads
    them with no check): listed with that run, or typed DIA.  A Q file listed as both
    .mgf and .mzML is caught by :func:`check_same_stem`."""
    problems, listed = [], {full for _, _, full, _ in rows}
    for n, name, full, dtype in rows:
        source = umpire_source(full)
        if not source:
            continue
        if os.path.normcase(os.path.join(os.path.dirname(full), source)) in listed:
            problems.append("manifest line %d (%s): DIA-Umpire's output of %s, which the "
                            "manifest also lists; FragPipe loads it as one more run, and when "
                            "DIA-Umpire runs again it rewrites the file and moves it into the "
                            "workdir. Remove the row." % (n, name, source))
        elif (canonical_data_type(dtype) or "") in ("DIA", "GPF-DIA", "DIA-Lib"):
            problems.append("manifest line %d (%s): DIA-Umpire's output of %s, typed %s, so "
                            "FragPipe would run DIA-Umpire on it again or search it as DIA. "
                            "Type it DDA, or list %s instead." % (n, name, source, dtype,
                                                                  source))
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
    ap.add_argument("--input", "-i", nargs="+",
                    help="files, directories, or globs of spectral data")
    ap.add_argument("--out", "-o", required=True, help="manifest path to write")
    ap.add_argument("--propose-design", action="store_true",
                    help="write a design sheet guessed from the file names to "
                         "--out, for the user to edit, instead of a manifest")
    ap.add_argument("--design",
                    help="build the manifest from this design sheet instead of "
                         "--input and --experiment-from")
    ap.add_argument("--groups-collapse", action="store_true",
                    help="with --design: the workflow runs DIA-NN or FragDIA quantification "
                         "or builds a spectral library, so FragPipe puts every file in one "
                         "group and a blank condition next to named ones is allowed")
    ap.add_argument("--recursive", "-r", action="store_true",
                    help="descend into subdirectories (as FragPipe does, a .d run is taken "
                         "whole and MSFragger's _calibrated/_uncalibrated copies are skipped)")
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
                    help="write even if the file name, folder name or repeated-name checks "
                         "find problems")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the manifest instead of writing it")
    args = ap.parse_args(argv)

    if args.data_type.lower() not in _DATA_TYPE_LOOKUP:
        print("[make_manifest] ERROR: --data-type must be one of %s"
              % ", ".join(VALID_DATA_TYPES), file=sys.stderr)
        return 2
    if args.design:
        return _manifest_from_design(args)
    if not args.input:
        print("[make_manifest] ERROR: give --input, or --design with a design sheet",
              file=sys.stderr)
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

    problems = check_paths(paths)
    if problems:
        for p in problems:
            print("[make_manifest] WARNING: %s" % p, file=sys.stderr)
        if not args.force:
            print("[make_manifest] refusing to write; fix the names or pass "
                  "--force", file=sys.stderr)
            return 2

    if args.propose_design:
        rows = propose_design(paths, canonical_data_type(args.data_type))
        if args.dry_run:
            for r in rows:
                print("\t".join(str(r[c]) for c in DESIGN_COLUMNS))
            return 0
        write_design(args.out, rows,
                     "Proposed from the file names; check every row, then build "
                     "the manifest with --design.")
        print("[make_manifest] proposed a design for %d file(s): %s"
              % (len(rows), os.path.abspath(args.out)))
        return 0

    rows = build_rows(paths, args.experiment_from, args.experiment,
                      args.experiment_regex, args.replicate, args.data_type)
    return _write_rows(args, rows)


def _manifest_from_design(args) -> int:
    design, problems = read_design(args.design, groups_collapse=args.groups_collapse)
    if problems:
        for p in problems:
            print("[make_manifest] ERROR: %s" % p, file=sys.stderr)
        if not args.groups_collapse and any("neither condition nor replicate" in p
                                            for p in problems):
            print("[make_manifest] If the workflow runs DIA-NN or FragDIA quantification or "
                  "builds a spectral library, pass --groups-collapse.", file=sys.stderr)
        return 2
    names = check_paths(r["file"] for r in design)
    if names:
        for p in names:
            print("[make_manifest] WARNING: %s" % p, file=sys.stderr)
        if not args.force:
            print("[make_manifest] refusing to write; fix the names or pass "
                  "--force", file=sys.stderr)
            return 2
    rows = [(r["file"], r["condition"], r["replicate"], r["data_type"]) for r in design]
    return _write_rows(args, rows)


def _write_rows(args, rows) -> int:
    if args.dry_run:
        sys.stdout.write("".join("%s\t%s\t%s\t%s\n" % tuple(r) for r in rows))
    else:
        write_manifest(args.out, rows)
        print("[make_manifest] wrote %d file(s) to %s"
              % (len(rows), os.path.abspath(args.out)))

    exps = sorted({r[1] for r in rows})
    shown = [e or "(unnamed)" for e in exps]
    print("[make_manifest] %d experiment(s): %s" % (len(exps), ", ".join(shown)))
    if exps == [""]:
        print("[make_manifest] note: with a single unnamed experiment, reports "
              "are written to the workdir root, not a subfolder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
