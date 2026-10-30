"""Build a FragPipe workflow from a few questions about the data.

Instead of picking one of ~77 shipped workflows, answer the questions this
script asks.  It starts from the shipped workflow those answers select, changes
only the run-level settings (FASTA, contaminant removal, TMT channel count),
writes everything into ``<workdir>/fragskills-repro/``, and prints a table of
every answer and change with its source.  Nothing launches until the user has
seen that table and it is confirmed::

    # 1. ask; repeat with --answer until nothing is open (exit 5 = questions open)
    python scripts/build_workflow.py --workdir D:/run/out \
        --fragpipe-dir "C:/FragPipe/FragPipe-24.1-build61" --data D:/data
    python scripts/build_workflow.py --workdir D:/run/out \
        --answer analysis=standard --answer acquisition=DDA --json

    # 2. the design: edit the proposed sheet it wrote, then accept it
    python scripts/build_workflow.py --workdir D:/run/out \
        --design D:/run/out/fragskills-repro/fragskills_design.tsv

    # 3. show the table to the user; when they confirm
    python scripts/build_workflow.py --workdir D:/run/out --confirm

    # 4. launch as usual -- fragpipe_run.py checks the confirmation
    python scripts/fragpipe_run.py \
        --workflow D:/run/out/fragskills-repro/fragskills.workflow \
        --manifest D:/run/out/fragskills-repro/fragskills.fp-manifest \
        --workdir D:/run/out --fragpipe-dir "C:/FragPipe/FragPipe-24.1-build61"

Answers are kept in ``fragskills_plan.json``, so each call adds to the last;
``--data`` replaces the folders given before.  A call that changes the plan
clears an earlier confirmation; one refused for bad arguments or an unsupported
FragPipe changes nothing.  The questions and the workflow each answer set
selects live in ``workflow_tree.json``.

Exit codes:

    0  built (prints the confirmation table) or confirmed
    2  bad arguments or inputs
    5  questions are open -- ask them, then re-run with --answer
    6  refused: an answer no shipped workflow combines with the others (it stays
       open, listed under "rejected" with the nearest shipped workflow), or an
       unsupported FragPipe
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fragskills_plan as fp  # noqa: E402
import make_manifest as mm  # noqa: E402
import workflow_tree as wt  # noqa: E402
from check_fragpipe import _root_of, install_from_root  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_QUESTIONS = 5
EXIT_REFUSED = 6

DB_KEY = "database.db-path"
SUPPORTED_TEXT = "24.1 and 25.0"


# --- .workflow files (Java .properties) ---------------------------------------

def _logical_lines(text: str):
    """Yield ``(first_line, last_line, logical_text)``, joining continuations."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start, s = i, lines[i]
        while s.endswith("\\") and (len(s) - len(s.rstrip("\\"))) % 2 == 1 \
                and i + 1 < len(lines):
            i += 1
            s = s[:-1] + lines[i].lstrip()
        yield start, i, s
        i += 1


def _split_entry(logical: str) -> Optional[Tuple[str, str]]:
    t = logical.strip()
    if not t or t[0] in "#!" or "=" not in t:
        return None
    key, raw = t.split("=", 1)
    return key.strip(), raw.strip()


def unescape(raw: str) -> str:
    out, i = [], 0
    while i < len(raw):
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt == "u" and re.match(r"[0-9a-fA-F]{4}", raw[i + 2:i + 6]):
                out.append(chr(int(raw[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    # 🧬 is one character written as a UTF-16 surrogate pair, as in Java
    return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def escape(value: str) -> str:
    """Escape a value the way java.util.Properties.store() does."""
    out = []
    for i, ch in enumerate(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch in "\t\n\r\f":
            out.append({"\t": "\\t", "\n": "\\n", "\r": "\\r", "\f": "\\f"}[ch])
        elif ch in "=:#!":
            out.append("\\" + ch)
        elif ch == " " and i == 0:
            out.append("\\ ")
        elif ord(ch) > 0xffff:          # Java's \uXXXX takes 4 digits: a surrogate pair
            v = ord(ch) - 0x10000
            out.append("\\u%04X\\u%04X" % (0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff)))
        elif ord(ch) < 0x20 or ord(ch) > 0x7e:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def read_properties(path: str) -> "OrderedDict[str, str]":
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    props = OrderedDict()
    for _, _, logical in _logical_lines(text):
        kv = _split_entry(logical)
        if kv:
            props[kv[0]] = unescape(kv[1])
    return props


def render_workflow(base_text: str, changes: Dict[str, str], header: List[str]) -> str:
    """The base workflow with *changes* applied and *header* comments on top.

    Every line not touched keeps its exact text, so a diff against the shipped
    file shows only what the builder changed.
    """
    newline = "\r\n" if "\r\n" in base_text else "\n"
    lines = base_text.splitlines()
    replace, remaining = {}, dict(changes)
    for start, end, logical in _logical_lines(base_text):
        kv = _split_entry(logical)
        if kv and kv[0] in remaining:
            replace[start] = (end, "%s=%s" % (kv[0], escape(remaining.pop(kv[0]))))
    out, i = list(header), 0
    while i < len(lines):
        if i in replace:
            end, text = replace[i]
            out.append(text)
            i = end + 1
            continue
        out.append(lines[i])
        i += 1
    for key, value in remaining.items():          # keys the base never set
        out.append("%s=%s" % (key, escape(value)))
    return newline.join(out) + newline


# --- input files ---------------------------------------------------------------

def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)),
                                   os.path.normcase(os.path.abspath(folder))]) \
            == os.path.normcase(os.path.abspath(folder))
    except ValueError:      # different drives
        return False


def collect_data(data_dirs: List[str], workdir: str) -> List[str]:
    """Spectral files under the data folders, minus anything FragPipe wrote into the
    workdir; a data folder that is itself inside the workdir is the user's, and kept."""
    files = set()
    for d in data_dirs:
        inner = _inside(d, workdir)
        files.update(f for f in mm.collect_files([d], recursive=True)
                     if (not _inside(f, fp.repro_dir(workdir)) if inner
                         else not _inside(f, workdir)))
    return sorted(files)


def collect_or_ask(data_dirs: List[str], workdir: str) -> Tuple[List[str], Optional[dict]]:
    """``(files, None)``, or ``([], the data question to ask again)`` when a --data
    folder is gone, unreadable or empty (an unplugged drive, a share asleep, a mount point
    left behind): the call then stops before any sheet is proposed again, and the accepted
    one stays."""
    away = [d for d in data_dirs if not os.path.exists(d)]
    if away:
        return [], {"id": "data", "kind": "path",
                    "text": "No such folder: %s. Reconnect it, or re-run with --data naming "
                            "the folders to use." % ", ".join(away)}
    found = set()
    for d in data_dirs:
        try:
            if os.path.isdir(d):
                os.listdir(d)       # the walk below takes an unreadable folder as empty
        except OSError as exc:
            return [], {"id": "data", "kind": "path",
                        "text": "Cannot read %s (%s). Reconnect it or fix its permissions, or "
                                "re-run with --data naming the folders to use."
                                % (d, exc.strerror or exc)}
        got = collect_data([d], workdir)
        if not got:
            return [], {"id": "data", "kind": "path",
                        "text": "No spectral files under %s. Reconnect it, or re-run with "
                                "--data naming the folders to use." % d,
                        "problem": "looked for %s" % ", ".join(mm.SPECTRA_EXT)}
        found.update(got)
    return sorted(found), None


def _utf8(path: str) -> bool:
    """Is *path* UTF-8 text?  A sheet merged into a new proposal must be: read loosely,
    the user's accented names would come back as U+FFFD."""
    try:
        with open(path, "rb") as fh:
            fh.read().decode("utf-8")
    except UnicodeDecodeError:
        return False
    except OSError:
        pass
    return True


def _bad_names(found: List[str]) -> List[str]:
    """Files whose names cannot be written as UTF-8 text (bytes of another encoding)."""
    bad = []
    for f in found:
        try:
            f.encode("utf-8")
        except UnicodeError:
            bad.append("%r: the file name is not valid text (it cannot be written as "
                       "UTF-8); rename the file" % f)
    return bad


def group_by_folder(files: List[str]) -> Tuple["OrderedDict[str, dict]", List[str]]:
    """One TMT/iTRAQ plex per folder, named after it -- FragPipe's own rule."""
    plexes, problems, by_lower = OrderedDict(), [], {}
    for f in files:
        folder = os.path.dirname(os.path.abspath(f.rstrip("\\/")))
        name = mm.sanitize_experiment(os.path.basename(folder))
        # case-insensitively: Plex1 and plex1 would share an output folder on Windows
        name = by_lower.setdefault(name.lower(), name)
        entry = plexes.setdefault(name, {"folder": folder, "files": []})
        if os.path.normcase(entry["folder"]) != os.path.normcase(folder):
            problems.append("two folders would both be plex '%s': %s and %s. Rename one."
                            % (name, entry["folder"], folder))
            continue
        entry["files"].append(f)
    return plexes, problems


# --- TMT / iTRAQ annotations ---------------------------------------------------

CHANNEL_COLUMNS = ("plex", "channel", "sample", "condition", "replicate")
annotation_files = fp.annotation_files


#: QuantLabelAnnotation.unifyAnnotationSampleName drops these from every sample name
#: before FragPipe compares it (IonQuant and TMT-Integrator use the name as written).
_SAMPLE_DROPPED = re.compile(r"[^0-9A-Za-z_-]")
#: FragPipe's charset detection gives up on a file holding any of these.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: Line breaks as Java's readLine sees them (not U+2028, U+0085, a form feed, ...).
_LINES = re.compile(r"\r\n|\r|\n")


def fragpipe_sample(name: str) -> str:
    """A sample name as FragPipe keeps it: trimmed, with only A-Z a-z 0-9 _ - left."""
    return _SAMPLE_DROPPED.sub("", name.strip(mm.JAVA_TRIM))


def read_annotation(path: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """``(channel, sample)`` pairs, read the way FragPipe reads them, and problems.

    ``TmtiPanel.parseTmtAnnotationFile``: every non-blank line, trimmed, must be
    exactly two tokens split on ASCII whitespace (a non-breaking space is not
    one) -- anything else stops the run.  IonQuant reads the file as UTF-8.
    """
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8")
    except UnicodeDecodeError:
        return [], ["%s is not UTF-8 text, and IonQuant refuses an annotation that is not; "
                    "save it as UTF-8" % path]
    except OSError as exc:
        return [], ["cannot read %s (%s)" % (path, exc.strerror or exc)]
    if _CONTROL.search(text):
        return [], ["%s holds a control character (such as an end-of-file Ctrl-Z), so "
                    "FragPipe cannot detect its encoding and stops; remove it" % path]
    pairs, problems = [], []
    if text.startswith("﻿"):
        problems.append("%s starts with a byte-order mark, which FragPipe reads as part of "
                        "the first channel name; save it as UTF-8 without BOM" % path)
        text = text[1:]
    for n, line in enumerate(_LINES.split(text), 1):
        parts = mm.java_tokens(line)
        if not parts:
            continue
        if len(parts) != 2:
            problems.append("%s line %d: '%s' is not '<channel> <sample>'; FragPipe stops "
                            "on it" % (path, n, line.strip()))
            continue
        space = re.search(r"\s", parts[1])     # not ASCII: java_tokens split on those
        if space:
            # IonQuant keeps it in the name: 'NA' followed by one is a sample to it
            problems.append("%s line %d: sample %r holds a Unicode space (U+%04X), which "
                            "IonQuant keeps in the name; remove it"
                            % (path, n, parts[1], ord(space.group(0))))
        pairs.append((parts[0], parts[1]))
    return pairs, problems


def check_annotation(path: str, channels: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """An existing annotation as FragPipe will use it: one line per channel, no more."""
    pairs, problems = read_annotation(path)
    if problems and not pairs:          # unreadable: the channel count would mislead
        return {}, problems
    got = [c for c, _ in pairs]
    unknown = [c for c in got if c not in channels]
    missing = [c for c in channels if c not in got]
    twice = sorted({c for c in got if got.count(c) > 1})
    if unknown or missing or twice:
        problems.append("%s must list each of the %d channels once%s%s%s; FragPipe counts "
                        "every line" % (path, len(channels),
                                        "; not channels: " + ", ".join(unknown) if unknown else "",
                                        "; missing: " + ", ".join(missing) if missing else "",
                                        "; repeated: " + ", ".join(twice) if twice else ""))
    return dict(pairs), problems


def annotation_text(rows: List[dict]) -> str:
    return "".join("%s %s\n" % (r["channel"], r["sample"]) for r in rows)


def propose_channels(plexes, channels: List[str], existing: Dict[str, str]) -> List[dict]:
    rows = []
    for plex in plexes:
        known = dict(read_annotation(existing[plex])[0]) if plex in existing else {}
        for ch in channels:
            rows.append({"plex": plex, "channel": ch,
                         "sample": known.get(ch, "%s_%s" % (plex, ch)),
                         "condition": "", "replicate": ""})
    return rows


def read_sheet(path: str) -> List[dict]:
    """Rows of a tab-separated sheet, with lower-case header keys.  Never raises."""
    rows, header = [], None
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            lines = _LINES.split(fh.read())
    except OSError:
        return rows
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        rows.append(dict(zip(header, cells + [""] * (len(header) - len(cells)))))
    return rows


def keep_previous(path: str) -> str:
    """Copy *path* to a new ``<path>.bak``, ``.bak2``, ... before the builder rewrites
    it; an earlier backup is never overwritten, and the same content is kept once."""
    with open(path, "rb") as fh:
        data = fh.read()
    n = 1
    while True:
        backup = path + (".bak" if n == 1 else ".bak%d" % n)
        if not os.path.exists(backup):
            with open(backup, "wb") as fh:
                fh.write(data)
            return backup
        with open(backup, "rb") as fh:
            if fh.read() == data:
                return backup
        n += 1


def write_channels(path: str, rows: List[dict], comment: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in comment.splitlines():
            fh.write("# %s\n" % line)
        fh.write("\t".join(CHANNEL_COLUMNS) + "\n")
        for r in rows:
            fh.write("\t".join(r.get(c, "") for c in CHANNEL_COLUMNS) + "\n")


def read_channels(path: str, plexes, channels: List[str], ref_tag: Optional[str],
                  notes: Optional[List[str]] = None, from_files: Optional[Dict[str, str]] = None
                  ) -> Tuple["OrderedDict[str, List[dict]]", List[str]]:
    """Parse the channel sheet; one list of rows per plex, in channel order.

    Sample names are checked the way FragPipe and IonQuant use them
    (:func:`fragpipe_sample`); names FragPipe compares shortened are listed in
    *notes*.  A problem in a plex whose samples come from its own annotation file
    (*from_files*) names that file.
    """
    by_plex, problems, header = OrderedDict((p, []) for p in plexes), [], None
    from_files = from_files or {}

    def own(plexes_with) -> str:
        """Where the user fixes a plex whose samples come from its own file."""
        files = ["%s (%s)" % (p, from_files[p]) for p in OrderedDict.fromkeys(plexes_with)
                 if p in from_files]
        return ("; the samples of plex %s come from its annotation file: edit that file"
                % ", ".join(files)) if files else ""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            lines = _LINES.split(fh.read())
    except UnicodeDecodeError:
        return by_plex, ["%s is not UTF-8 text. Save it as tab-separated UTF-8." % path]
    except OSError as exc:
        return by_plex, ["cannot read the channel sheet %s (%s)" % (path, exc.strerror or exc)]
    for n, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("\t")]
        if header is None:
            header = [c.lower() for c in cells]
            missing = [c for c in CHANNEL_COLUMNS if c not in header]
            if missing:
                return by_plex, ["%s: header is missing column(s) %s"
                                 % (path, ", ".join(missing))]
            continue
        cells += [""] * (len(header) - len(cells))
        rec = {c: rec_v for c, rec_v in zip(header, cells) if c in CHANNEL_COLUMNS}
        where = swhere = "%s line %d" % (os.path.basename(path), n)
        if rec["plex"] in from_files:           # the sample, not the rest, is the file's
            swhere += " (from %s)" % from_files[rec["plex"]]
        if rec["plex"] not in by_plex:
            problems.append("%s: plex '%s' is not one of the data folders (%s)"
                            % (where, rec["plex"], ", ".join(by_plex)))
            continue
        if rec["channel"] not in channels:
            problems.append("%s: channel '%s' is not a %d-plex channel"
                            % (where, rec["channel"], len(channels)))
            continue
        sample = rec["sample"]
        if _CONTROL.search(sample):
            problems.append("%s: sample name %r holds a control character, which stops "
                            "FragPipe (it cannot detect the annotation's encoding)"
                            % (swhere, sample))
        elif not sample or re.search(r"\s", sample):
            problems.append("%s: sample name '%s' must be non-empty with no spaces"
                            % (swhere, sample))
        elif not fragpipe_sample(sample):
            problems.append("%s: sample name '%s' has none of the characters FragPipe keeps "
                            "(A-Z a-z 0-9 _ -), so FragPipe stops on an empty sample name. "
                            "Use NA for an unused channel." % (swhere, sample))
        elif fragpipe_sample(sample).lower() == "na" and sample.lower() != "na":
            # FragPipe reads it as NA, IonQuant as a sample (and stops on two in a plex)
            problems.append("%s: write NA, not '%s', for an unused channel" % (swhere, sample))
        rep = rec.get("replicate", "")
        if rep and not rep.isdecimal():
            problems.append("%s: replicate '%s' is not a whole number" % (where, rep))
        by_plex[rec["plex"]].append(rec)
    for plex, rows in by_plex.items():
        got = [r["channel"] for r in rows]
        if sorted(got) != sorted(channels):
            missing = [c for c in channels if c not in got]
            twice = sorted({c for c in got if got.count(c) > 1})
            problems.append("plex %s: every channel must appear once%s%s"
                            % (plex, "; missing " + ", ".join(missing) if missing else "",
                               "; repeated " + ", ".join(twice) if twice else ""))
        if ref_tag:
            # on the name as written: IonQuant names the channel columns with it, and
            # TMT-Integrator looks for the tag there (it then holds in FragPipe's kept name)
            refs = [r["sample"] for r in rows if ref_tag in r["sample"]]
            if len(refs) != 1:
                problems.append("plex %s: the bridge workflow needs exactly one sample whose "
                                "name contains '%s' (TMT-Integrator's reference channel); "
                                "found %d%s" % (plex, ref_tag, len(refs), own([plex])))
        order = {c: i for i, c in enumerate(channels)}
        rows.sort(key=lambda r: order.get(r["channel"], 999))
    # TMT-Integrator needs every sample name unique across all plexes, compared as
    # FragPipe keeps them (CmdTmtIntegrator.configure); NA, as written, marks an unused
    # channel and may repeat
    seen_in, typed, shortened = OrderedDict(), {}, []
    for plex, rows in by_plex.items():
        for r in rows:
            kept = fragpipe_sample(r["sample"])
            if not kept or r["sample"].lower() == "na":
                continue
            if kept != r["sample"]:
                shortened.append("%s as %s" % (r["sample"], kept))
            seen_in.setdefault(kept, []).append(plex)
            typed.setdefault(kept, []).append(r["sample"])
    for sample, plexes_with in seen_in.items():
        if len(plexes_with) > 1:
            names = sorted(set(typed[sample]))
            problems.append("sample name '%s' is used %d times (%s%s); FragPipe needs sample "
                            "names unique across all plexes. Use NA for an unused channel%s."
                            % (sample, len(plexes_with), ", ".join(plexes_with),
                               "" if names == [sample] else
                               "; typed as %s" % ", ".join(names), own(plexes_with)))
    if shortened and notes is not None:
        notes.append("FragPipe compares sample names with only A-Z a-z 0-9 _ - kept, so it "
                     "counts %s%s; names made only of those characters are safest."
                     % (", ".join(shortened[:8]), ", ..." if len(shortened) > 8 else ""))
    return by_plex, problems


# --- evaluation ----------------------------------------------------------------

def _setting_question(setting: dict, problem: Optional[str] = None) -> dict:
    q = {"id": setting["id"], "kind": setting["kind"], "text": setting["text"]}
    if setting["kind"] == "choice":
        q["answers"] = [{"value": a["value"], "label": a["label"]} for a in setting["answers"]]
        q["default"] = setting.get("default")
    if problem:
        q["problem"] = problem
    return q


def _setting_value(setting: dict, answer: str) -> Optional[str]:
    for a in setting.get("answers", []):
        if a["value"] == answer:
            return a["set"]
    return None


def evaluate(tree: dict, install: dict, plan: dict, workdir: str) -> dict:
    """Everything the current answers settle; writes nothing."""
    result = {"state": fp.STATE_QUESTIONS, "open": [], "rejected": [], "problems": []}
    version = install.get("version")
    supported = wt.supported_version(tree, version)
    if supported is None:
        result["state"] = "refused"
        result["problems"].append(
            "FragPipe %s is not supported by the workflow builder (supported: %s). "
            "fragpipe_run.py, fragpipe_status.py and make_report.py still work with it."
            % (version or "of unknown version", SUPPORTED_TEXT))
        return result
    result["supported_as"] = supported

    answers = plan.get("answers", {})
    res = wt.resolve(tree, {k: v for k, v in answers.items() if k in tree["_questions"]})
    result["rejected"] = res.rejected
    result["resolved"] = [{"id": q, "value": v, "label": wt.describe(tree, q, v),
                           "source": res.sources[q]} for q, v in res.settled.items()]
    result["kept_answers"] = res.kept_answers
    result["dropped"] = res.dropped
    result["open"] = list(res.open)
    if res.open:
        return result

    route = res.route
    base_path = os.path.join(install.get("workflows_dir") or "", route["workflow"] + ".workflow")
    if not os.path.isfile(base_path):
        result["state"] = "refused"
        result["problems"].append("this FragPipe has no %s.workflow in %s"
                                  % (route["workflow"], install.get("workflows_dir")))
        return result
    base = read_properties(base_path)
    drift = quant_drift(route, base)
    if drift:
        result["state"] = "refused"
        result["problems"].append("%s %s; this FragPipe build differs from the ones "
                                  "workflow_tree.json was checked against. Pick the workflow "
                                  "by hand." % (base_path, drift))
        return result
    data_set = dict(tree["data_type_sets"][route["data"]], name=route["data"])
    result.update(route=route["workflow"], base_path=base_path, data_types=data_set)

    # run-level settings
    settings = tree["_settings"]
    values: Dict[str, Tuple[str, str]] = {}          # 24.1 key -> (value, source)
    fasta = answers.get("fasta")
    if not fasta:
        result["open"].append(_setting_question(settings["fasta"]))
    elif not os.path.isfile(fasta):
        result["open"].append(_setting_question(settings["fasta"], "no such file: %s" % fasta))
    else:
        values[DB_KEY] = (os.path.abspath(fasta), "answer: fasta")
    rc = settings["remove_contaminants"]
    rc_value = _setting_value(rc, answers.get("remove_contaminants", ""))
    if rc_value is None:
        result["open"].append(_setting_question(rc))
    else:
        values[rc["key"]] = (rc_value, "answer: remove_contaminants")
    if DB_KEY in values:
        result["resolved"].append({"id": "fasta", "value": values[DB_KEY][0],
                                   "label": values[DB_KEY][0], "source": wt.SOURCE_ANSWER})
    if rc_value is not None:
        result["resolved"].append({
            "id": "remove_contaminants", "value": answers["remove_contaminants"],
            "label": wt.describe(tree, "remove_contaminants", answers["remove_contaminants"]),
            "source": wt.SOURCE_ANSWER})
    for d in tree["derived"]:
        if d["from"] in res.settled:
            values[d["key"]] = (d["format"].format(res.settled[d["from"]]),
                                "%s: %s" % (res.sources[d["from"]], d["from"]))

    changes = []
    for key24, (value, source) in values.items():
        key = wt.rename_key(tree, supported, key24)
        if base.get(key) == value:
            continue
        why = next((s["why"] for s in tree["settings"] if s["key"] == key24), None) or \
            next((d["why"] for d in tree["derived"] if d["key"] == key24), "")
        changes.append({"key": key, "value": value, "base": base.get(key),
                        "source": source, "why": why})
    missing = [c["key"] for c in changes if c["key"] != DB_KEY and c["base"] is None]
    if missing:
        result["state"] = "refused"
        result["problems"].append("%s has no %s, which the builder would set; this FragPipe "
                                  "build differs from the ones workflow_tree.json was checked "
                                  "against. Pick the workflow by hand." % (base_path,
                                                                           ", ".join(missing)))
        return result
    result["changes"] = changes
    final = dict(base)
    final.update((c["key"], c["value"]) for c in changes)

    # design: the input files, their data types and conditions
    data_dirs = plan.get("data") or []
    name_rules = mm.has_name_rules(version)     # 24.0 and 24.1-build1..7 load any name
    isobaric = final.get("tmtintegrator.run-tmtintegrator") == "true"
    if isobaric:
        _design_isobaric(tree, plan, workdir, final, data_set, data_dirs, result, name_rules)
    else:
        _design_plain(tree, plan, workdir, data_set, data_dirs, final, result, name_rules)
    if not result["open"] and not result["problems"]:
        result["state"] = fp.STATE_BUILT
    return result


def groups_collapse(final: Dict[str, str]) -> bool:
    """``InputLcmsFile.getGroup``: every file is one group when DIA-NN, FragDIA or
    the spectral library builder runs, so experiment names do not matter."""
    engine = any(final.get(k) == "true" for k in ("diann.run-diann", "diann.run-dia-nn",
                                                   "fragdia.run-fragdia"))
    # FragSpecLib's switch was fragspeclib.run-fragspeclib in 24.1 builds 36 to 39
    return any(final.get(k) == "true" for k in ("speclibgen.run-speclibgen",
                                                "fragspeclib.run-fragspeclib")) or \
        (engine and final.get("dia.run-dia-quant") != "false")


#: What each quant answer needs the base workflow to run (see quant_kind).
_QUANT_NEEDS = {"none": "none", "LFQ": "ms1", "SILAC": "ms1", "TMT": "isobaric",
                "iTRAQ": "isobaric"}
_QUANT_RUNS = {"none": "quantifies nothing", "ms1": "runs IonQuant or DIA-NN quantification",
               "isobaric": "runs TMT-Integrator"}


def quant_kind(props: Dict[str, str]) -> str:
    """How a workflow quantifies: 'isobaric' (TMT-Integrator), 'ms1' (IonQuant MS1, also
    with SILAC labels, or DIA-NN/FragDIA), or 'none'."""
    if props.get("tmtintegrator.run-tmtintegrator") == "true":
        return "isobaric"
    if props.get("quantitation.run-label-free-quant") == "true" or quantifies_dia(props):
        return "ms1"
    return "none"


def quant_drift(route: dict, base: Dict[str, str]) -> Optional[str]:
    """Why *base* does not quantify the way *route* says (a FragPipe build whose shipped
    workflow differs from the ones workflow_tree.json was checked against), or None.
    In 24.1 builds 1 to 39 the glyco DDA workflows run no label-free quant."""
    want = _QUANT_NEEDS.get(route["answers"].get("quant"))
    if want is None or quant_kind(base) == want:
        return None
    return "%s, but the builder picks it for quant=%s" % (_QUANT_RUNS[quant_kind(base)],
                                                         route["answers"]["quant"])


def quantifies_dia(final: Dict[str, str]) -> bool:
    """DIA-NN or FragDIA quantifies; it skips every group with no DIA or DIA-Quant run."""
    engine = any(final.get(k) == "true" for k in ("diann.run-diann", "diann.run-dia-nn",
                                                   "fragdia.run-fragdia"))
    return engine and final.get("dia.run-dia-quant") != "false"


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def same_folders(a, b) -> bool:
    """The same data folders, however they were spelled (case, trailing slash)."""
    return sorted({_key(d) for d in a or []}) == sorted({_key(d) for d in b or []})


def _check_rows(rows: List[dict], data_dirs: List[str], final: Dict[str, str]) -> List[str]:
    """Rows that cannot be what the user meant: outside the --data folders, or no run
    for the DIA quantification to quantify."""
    problems = []
    outside = [r["file"] for r in rows if data_dirs
               and not any(_inside(r["file"], d) for d in data_dirs)]
    if outside:
        problems.append("%d file(s) of the design sheet are not under the --data folders (%s): "
                        "%s. Give the folders that hold them with --data, or fix the sheet."
                        % (len(outside), ", ".join(data_dirs),
                           ", ".join(os.path.basename(f.rstrip("\\/")) for f in outside[:5])))
    if rows and quantifies_dia(final) and not any(r["data_type"] in ("DIA", "DIA-Quant")
                                                  for r in rows):
        problems.append("this workflow quantifies DIA runs, but no file is typed DIA or "
                        "DIA-Quant, so nothing would be quantified. Check data_type.")
    return problems


def _write_sheet(write, path: str, notes: List[str], result: dict) -> bool:
    """Back up *path* and rewrite it; a sheet held open elsewhere is a problem, not a crash."""
    try:
        if os.path.isfile(path):
            notes.append("The previous sheet is kept as %s." % keep_previous(path))
        write()
        return True
    except OSError as exc:
        result["problems"].append("cannot write %s (%s); close it in any program that has "
                                  "it open, then re-run" % (path, exc.strerror or exc))
        return False
    except UnicodeError as exc:         # a cell that is not text; the names are checked first
        result["problems"].append("cannot write %s (%s)" % (path, exc))
        return False


def _design_plain(tree, plan, workdir, data_set, data_dirs, final, result,
                  name_rules: bool = True) -> None:
    """The design sheet: the accepted one while it still fits, else a proposal.

    The plan records what each sheet was made for -- ``design_for`` (accepted:
    data set, data folders, the files in them) and ``design_proposal`` (the
    proposal: the same) -- so a change of workflow, folders or files is noticed,
    and rows the user removed are told apart from files that appeared.
    """
    proposal = os.path.join(fp.repro_dir(workdir), fp.DESIGN_NAME)
    updates = result.setdefault("plan_updates", {})
    collapse = groups_collapse(final)
    design, accepted_for, notes = plan.get("design"), plan.get("design_for") or {}, []
    found, ask = collect_or_ask(data_dirs, workdir) if data_dirs else ([], None)
    if ask:
        result["open"].append(ask)
        return
    if _bad_names(found):
        result["problems"] += _bad_names(found)
        return
    folder_files = sorted(_key(f) for f in found)
    previous = None             # an accepted design that no longer fits; merged below
    if design and not os.path.isfile(design):
        notes.append("The design sheet accepted earlier, %s, is gone, so the design was "
                     "proposed again." % design)
        design = None
    elif design and accepted_for.get("data_set") not in (None, data_set["name"]):
        notes.append("The workflow changed to one for %s data, so the design accepted "
                     "earlier was proposed again." % data_set["default"])
        previous, design = design, None
    elif design and "data" in accepted_for and not same_folders(accepted_for["data"],
                                                                 data_dirs):
        notes.append("%s, so the design accepted earlier was proposed again from them; rows "
                     "for the same files keep their edits."
                     % ("The data folders changed" if accepted_for["data"] else
                        "Data folders were given for a design accepted without them"))
        previous, design = design, None
    elif design and data_dirs and accepted_for.get("files") is not None and \
            set(accepted_for["files"]) != set(folder_files):
        notes.append("Files were added to or removed from the data folders since the design "
                     "was accepted, so it was proposed again; rows for the same files keep "
                     "their edits.")
        previous, design = design, None
    # unbound only once the new proposal is written: a call that stops on the way
    # keeps the accepted sheet, its edits and the rows it left out
    unbind = bool(plan.get("design")) and not design
    if design:
        rows, problems = mm.read_design(design, data_set["allowed"], collapse)
        problems = problems or _check_rows(rows, data_dirs, final)
        if problems:
            # not bound to this workflow: the next build reads it again as it stands
            result["open"].append({"id": "design", "kind": "table", "path": design,
                                   "text": "Fix the design sheet, then re-run with --design.",
                                   "problems": problems})
            return
        updates["design_for"] = dict(accepted_for, data_set=data_set["name"], data=data_dirs,
                                     files=accepted_for.get("files", folder_files))
        result["problems"] += mm.check_paths((r["file"] for r in rows), name_rules=name_rules)
        result["inputs"] = [{"file": r["file"], "experiment": r["condition"],
                             "replicate": r["replicate"], "data_type": r["data_type"]}
                            for r in rows]
        return

    # the rows the user may have edited: the design that stopped fitting, or the proposal
    made = plan.get("design_proposal") or {}
    if previous:
        source, source_set = previous, accepted_for.get("data_set")
        source_files, source_dirs = accepted_for.get("files"), accepted_for.get("data")
    else:
        source = proposal if os.path.isfile(proposal) else None
        source_set, source_files, source_dirs = \
            made.get("data_set"), made.get("files"), made.get("data")
    if source and not _utf8(source):
        # read loosely, the user's accented names would be merged as U+FFFD
        result["open"].append({"id": "design", "kind": "table", "path": source,
                               "text": "Save the design sheet as UTF-8, then re-run.",
                               "problems": ["%s is not UTF-8 text. Save it as tab-separated "
                                            "UTF-8." % source]})
        return
    # the builder's own proposal records the type it wrote for each file, and which
    # types the user gave
    from_builder = bool(source) and fp._same_file(source, proposal)
    written = made.get("types") if from_builder else None
    user_types = set(made.get("user_types") or []) if from_builder else set()
    old_rows = read_sheet(source) if source else []
    old = OrderedDict((_key(r["file"]), r) for r in old_rows if r.get("file"))
    # without --data the files are the sheet's own (a design given without folders); with
    # it, a row for a file the walk does not collect (a .mzBIN run) stays while it is there
    walked = {_key(f) for f in found}
    files = found + [r["file"] for k, r in old.items()
                     if k not in walked and os.path.exists(r["file"])
                     and any(_inside(r["file"], d) for d in data_dirs)] \
        if data_dirs else [r["file"] for r in old.values()]
    if not files:
        result["open"].append({"id": "data", "kind": "path",
                               "text": "Folder(s) holding the raw files. Re-run with "
                                       "--data <folder>, or give a design sheet with "
                                       "--design <sheet>.", "notes": notes})
        return

    now = {_key(f) for f in files}
    real = {_key(f): f for f in files}
    # a row rewritten with the bare file name the table shows is still that file, and a
    # moved folder's files are the same files: what the sheet was made for follows them
    by_name, moved = {}, {}
    for k, f in real.items():
        by_name.setdefault(os.path.normcase(os.path.basename(f.rstrip("\\/"))), []).append(k)
    for k in [k for k in old if k not in now]:
        hits = by_name.get(os.path.normcase(os.path.basename(old[k]["file"].rstrip("\\/"))), [])
        if len(hits) == 1 and hits[0] not in old:
            old[hits[0]] = old.pop(k)
            moved[k] = hits[0]
    for k in source_files or []:
        hits = by_name.get(os.path.basename(k), [])
        if k not in now and k not in moved and len(hits) == 1:
            moved[k] = hits[0]
    if source_files is not None:
        source_files = sorted({moved.get(k, k) for k in source_files})
    written = {moved.get(k, k): t for k, t in written.items()} if written is not None else None
    user_types = {moved.get(k, k) for k in user_types}
    if previous or source is None or source_set != data_set["name"] \
            or source_files is None or set(source_files) != now or moved:
        # never drop an edit: rows for the same file keep condition and replicate (and
        # data_type, unless the workflow's data types changed); files taken out of the
        # sheet stay out -- known only from a sheet whose rows still name the files
        taken_out = unlisted = set()
        if source_files is not None and set(old) & set(source_files):
            taken_out = (set(source_files) - set(old)) & now
        if (previous or made) and not source_dirs and data_dirs and set(old) & now:
            # a sheet made without --data lists the files it is about: the other
            # files in the folders given now were left out of it
            unlisted = now - set(old)
        left_out = taken_out | unlisted
        keep_types = source_set in (None, data_set["name"])
        old_default = (tree["data_type_sets"].get(source_set) or {}).get("default")
        rows = mm.propose_design([f for f in files if _key(f) not in left_out],
                                 data_set["default"])
        retyped, by_user = [], set()
        for r in rows:
            k = _key(r["file"])
            o = old.get(k)
            if o:
                r.update(condition=o.get("condition", ""), replicate=o.get("replicate", ""))
                typed = o.get("data_type", "")
                canonical = mm.canonical_data_type(typed)
                if written is not None:
                    user = k in user_types or (bool(typed) and
                                               (canonical or typed) != written.get(k))
                else:   # a sheet of the user's own (often a copy of a proposal), or a
                    # proposal recorded before the types were: the old default is not theirs
                    user = bool(canonical) and canonical != old_default
                if keep_types and typed:
                    # kept even when wrong, so the sheet still reports it
                    r["data_type"] = canonical or typed
                elif user and canonical in data_set["allowed"]:
                    r["data_type"] = canonical      # typed by the user, and still allowed
                elif typed and (canonical or typed) != r["data_type"]:
                    retyped.append(os.path.basename(r["file"].rstrip("\\/")))
                    user = False
                if user:
                    by_user.add(k)
        if retyped:
            notes.append("The workflow now takes %s data, so data_type was set to %s for %d "
                         "file(s) (%s). Check data_type."
                         % (data_set["default"], data_set["default"], len(retyped),
                            ", ".join(retyped[:5]) + (", ..." if len(retyped) > 5 else "")))
        if source and not previous:
            if source_dirs is not None and not same_folders(source_dirs, data_dirs):
                notes.append("The data folders changed, so the design was proposed again from "
                             "them; rows for the same files keep their edits.")
            elif data_dirs:     # without folders the rows are the files: nothing to count
                before = set(source_files) if source_files is not None else set(old)
                if before != now:
                    notes.append("%d file(s) added to and %d removed from the data folders "
                                 "since the last proposal; the other rows keep their edits."
                                 % (len(now - before), len(before - now)))
        def named(keys) -> str:
            names = sorted(os.path.basename(real[k].rstrip("\\/")) for k in keys)
            return ", ".join(names[:5]) + (", ..." if len(names) > 5 else "")
        if taken_out:
            notes.append("%d file(s) taken out of the sheet earlier stay out: %s."
                         % (len(taken_out), named(taken_out)))
        if unlisted:
            notes.append("%d file(s) under the --data folders are not in the design sheet "
                         "%s earlier, so they stay out: %s. Add a row for each one to "
                         "include it." % (len(unlisted), "accepted" if previous else "made",
                                          named(unlisted)))
        gone = [k for k in old if k not in now] if data_dirs and (previous or not source_dirs) \
            else []
        # the rows are built from the folders' files: say which of the user's rows went
        outside = [k for k in gone if not any(_inside(old[k]["file"], d) for d in data_dirs)]
        for keys, why in ((outside, "not under the --data folders, so they were left out: %s. "
                                    "Give their folders with --data to keep them."),
                          ([k for k in gone if k not in outside],
                           "no longer in the --data folders, so they were left out: %s.")):
            if keys:
                names = sorted("%s (%s)" % (os.path.basename(old[k]["file"].rstrip("\\/")),
                                            old[k].get("condition", "")) for k in keys)
                notes.append(("%d row(s) of the earlier design sheet are for files " + why)
                             % (len(keys), ", ".join(names[:5])
                                + (", ..." if len(names) > 5 else "")))
        if not _write_sheet(lambda: mm.write_design(
                proposal, rows, "Proposed from the file names. condition becomes the "
                "experiment, replicate the bioreplicate.\nData types allowed by this "
                "workflow: %s" % ", ".join(data_set["allowed"])), proposal, notes, result):
            return
        updates["design_proposal"] = {"data_set": data_set["name"], "data": data_dirs,
                                      "files": sorted(now),
                                      "types": {_key(r["file"]): r["data_type"] for r in rows},
                                      "user_types": sorted(by_user)}
    if unbind:
        updates.update(design=None, design_for=None)
    rows, problems = mm.read_design(proposal, data_set["allowed"], collapse)
    paths = [r["file"] for r in rows]
    result["open"].append({
        "id": "design", "kind": "table", "path": proposal,
        "text": "Show this design to the user and apply their edits to the file. "
                "Files with the same condition and replicate are merged as fractions. "
                "Then re-run with --design %s" % proposal,
        "columns": list(mm.DESIGN_COLUMNS), "rows": rows,
        "problems": problems
        + (mm.check_filenames(paths) + mm.check_folders(paths) if name_rules else [])
        + mm.check_same_stem(paths)
        + (_check_rows(rows, [], final) if not problems else []),
        "notes": notes})


def _design_isobaric(tree, plan, workdir, final, data_set, data_dirs, result,
                     name_rules: bool = True) -> None:
    label = final.get("tmtintegrator.channel_num", "")
    channels = tree["channels"].get(label)
    if not channels:
        result["problems"].append("no channel list for %s in workflow_tree.json" % label)
        return
    if not data_dirs:
        result["open"].append({"id": "data", "kind": "path",
                               "text": "Folder(s) holding the raw files, one folder per plex "
                                       "(FragPipe finds a plex's annotation only in the "
                                       "folder of its files). Re-run with --data <folder>."})
        return
    files, ask = collect_or_ask(data_dirs, workdir)
    if ask:
        result["open"].append(ask)
        return
    if _bad_names(files):
        result["problems"] += _bad_names(files)
        return
    plexes, problems = group_by_folder(files)
    result["problems"] += problems + mm.check_paths(files, name_rules=name_rules)
    # a channel sheet of the user's own is bound to the channel count, plexes and folders
    # by the first build that accepts it; until then it is read as it stands
    updates = result.setdefault("plan_updates", {})
    made_for = {"label": label, "plexes": list(plexes), "data": data_dirs}
    accepted_for = plan.get("channels_for") or {}
    # an annotation this builder wrote earlier (recorded at every --confirm, kept
    # across builds of other workflows), still unedited, is not the user's
    written = {os.path.normcase(p): d for p, d in (plan.get("own_annotations") or {}).items()}
    own = {}
    existing, from_file = {}, {}
    for plex, entry in plexes.items():
        entries = annotation_files(entry["folder"])
        odd = [f for f in entries if not os.path.isfile(f)]
        if odd:
            # FragPipe lists every name ending in annotation.txt, a folder too
            result["problems"] += ["%s holds %s, which is not a file but which FragPipe "
                                   "counts as an annotation; rename it or move it away."
                                   % (entry["folder"], os.path.basename(f)) for f in odd]
            continue
        found = []
        for f in entries:
            digest = written.get(os.path.normcase(f))
            if digest and fp.sha256_file(f) == digest:
                own[os.path.normcase(f)] = digest
            else:
                found.append(f)
        mine = [f for f in entries if os.path.normcase(f) in own]
        if len(found) + len(mine) > 1:
            result["problems"].append(
                "%s holds %d *annotation.txt files (%s); FragPipe needs exactly one per "
                "plex folder. Move the extra ones away." % (
                    entry["folder"], len(found) + len(mine),
                    ", ".join(os.path.basename(f) for f in found + mine)))
        elif found:
            existing[plex] = found[0]
            from_file[plex], bad = check_annotation(found[0], channels)
            result["problems"] += [p + ". Fix that file, or move it away to let the builder "
                                   "write one." for p in bad]
        else:
            # a name FragPipe does not match (it is case-sensitive) that Windows would
            # still treat as the file the builder is about to write
            target = "%s_annotation.txt" % plex
            clash = [n for n in os.listdir(entry["folder"])
                     if n.lower() == target.lower() and n != target]
            if clash:
                result["problems"].append(
                    "%s holds %s, which FragPipe does not read as an annotation but which "
                    "would be overwritten by %s. Rename it to %s, or move it away."
                    % (entry["folder"], clash[0], target, target))
    if result["problems"]:
        return

    add_ref = final.get("tmtintegrator.add_Ref", "")
    ref_tag = final.get("tmtintegrator.ref_tag", "") if add_ref == "-1" else ""

    def differs(path: str) -> List[str]:
        """Plexes whose own annotation file says other samples than the sheet *path*
        (FragPipe uses the file); read each build, so a fixed file is seen.  A sheet that
        is not UTF-8 is left to read_channels, which says so."""
        if not _utf8(path):
            return []
        rows = read_sheet(path)
        return [plex for plex, samples in from_file.items()
                if {r.get("channel"): r.get("sample", "") for r in rows
                    if r.get("plex") == plex} != {c: s.strip() for c, s in samples.items()}]

    def from_file_note(stale: List[str], what: str) -> str:
        return ("FragPipe uses the annotation file of plex %s, which differs from %s, so the "
                "sheet's samples for it were taken from that file again."
                % (", ".join("%s (%s)" % (p, existing[p]) for p in stale), what))

    moved = ("The data folders changed, so the channel sheet was proposed again; rows for "
             "plexes of the same name keep their edits. Check that they fit the new runs.")
    # the channel sheet: the accepted one while it fits this channel count, these plexes
    # and folders (``channels_for``), else a proposal (made for ``channels_proposal``)
    sheet, notes, previous = plan.get("channels"), [], None
    stale = differs(sheet) if sheet and os.path.isfile(sheet) else []
    if sheet and not os.path.isfile(sheet):
        notes.append("The channel sheet accepted earlier, %s, is gone, so the channels were "
                     "proposed again." % sheet)
        sheet = None
    elif sheet and (accepted_for.get("label") not in (None, label) or (
            accepted_for.get("plexes") is not None
            and set(accepted_for["plexes"]) != set(plexes))):
        notes.append("The channel count or the plexes changed since the channel sheet was "
                     "accepted, so it was proposed again; rows for the same plex and channel "
                     "keep their edits.")
        previous, sheet = sheet, None
    elif sheet and "data" in accepted_for and not same_folders(accepted_for["data"], data_dirs):
        notes.append(moved)
        previous, sheet = sheet, None
    elif stale:
        notes.append(from_file_note(stale, "the channel sheet accepted earlier"))
        previous, sheet = sheet, None
    unbind = bool(plan.get("channels")) and not sheet     # once the proposal is written
    if not sheet:
        sheet = os.path.join(fp.repro_dir(workdir), fp.CHANNELS_NAME)
        made = plan.get("channels_proposal") or {}
        have = os.path.isfile(sheet)
        why = None
        if have and not previous:
            stale = differs(sheet)
            if made.get("label") != label or set(made.get("plexes") or []) != set(plexes):
                why = ("The plexes or the channel count changed, so the sheet was updated; "
                       "rows for the same plex and channel keep their edits." if made else
                       "The channel sheet was proposed again; rows for the same plex and "
                       "channel keep their edits.")
            elif not same_folders(made.get("data"), data_dirs):
                why = moved
            elif stale:
                why = from_file_note(stale, "the sheet")
            if why:
                notes.append(why)
        if previous or not have or why:
            rows = propose_channels(plexes, channels, existing)
            source = previous or (sheet if have else None)
            if source and not _utf8(source):
                # read loosely, the user's sample names would be merged as U+FFFD; the
                # accepted sheet stays bound, and nothing was proposed (no notes saying so)
                result["open"].append({"id": "channels", "kind": "table", "path": source,
                                       "text": "Save the channel sheet as UTF-8, then re-run.",
                                       "problems": ["%s is not UTF-8 text. Save it as "
                                                    "tab-separated UTF-8." % source]})
                return
            if source:
                # never drop an edit; a plex with its own annotation keeps that file's samples
                old = {(r.get("plex"), r.get("channel")): r for r in read_sheet(source)}
                for r in rows:
                    o = old.get((r["plex"], r["channel"]))
                    if o:
                        r.update((c, o.get(c, "")) for c in ("sample", "condition", "replicate")
                                 if c != "sample" or r["plex"] not in existing)
            if not _write_sheet(lambda: write_channels(
                    sheet, rows, "One row per channel of each plex (%s). sample is the name "
                    "the reports use; NA marks an unused channel. condition and replicate feed "
                    "the FragPipe-Analyst hand-off.%s"
                    % (label, "\nIn each plex, give the bridge channel a sample name "
                       "containing '%s', different in every plex (%s_1, %s_2, ...)."
                       % (ref_tag, ref_tag, ref_tag) if ref_tag else "")), sheet, notes, result):
                return
            updates["channels_proposal"] = dict(made_for)
        if unbind:
            updates.update(channels=None, channels_for=None)
        by_plex, problems = read_channels(sheet, plexes, channels, ref_tag or None, notes,
                                          existing)
        result["open"].append({
            "id": "channels", "kind": "table", "path": sheet,
            "text": "Show this channel annotation to the user, one plex per data folder, "
                    "and apply their edits to the file. Then re-run with --channels %s"
                    % sheet,
            "columns": list(CHANNEL_COLUMNS),
            "rows": [r for plex_rows in by_plex.values() for r in plex_rows],
            "problems": problems, "notes": notes})
        return
    by_plex, problems = read_channels(sheet, plexes, channels, ref_tag or None, notes, existing)
    if problems:
        # not bound: the next build reads it again as it stands
        result["open"].append({"id": "channels", "kind": "table", "path": sheet,
                               "text": "Fix the channel sheet, then re-run with --channels.",
                               "problems": problems, "notes": notes})
        return
    updates["channels_for"] = dict(made_for)
    result["notes"] = notes

    result["inputs"] = [{"file": f, "experiment": plex, "replicate": "",
                         "data_type": data_set["default"]}
                        for plex, entry in plexes.items() for f in entry["files"]]
    result["annotations"] = []
    for plex, entry in plexes.items():
        text = annotation_text(by_plex[plex])
        if plex in existing:
            result["annotations"].append({"plex": plex, "folder": entry["folder"],
                                          "path": existing[plex], "status": "existing",
                                          "sha256": fp.sha256_file(existing[plex]),
                                          "text": text})
        else:
            target = os.path.join(entry["folder"], "%s_annotation.txt" % plex)
            result["annotations"].append({
                "plex": plex, "folder": entry["folder"], "status": "to write", "text": text,
                "path": target, "own_sha256": own.get(os.path.normcase(target))})
    result["channel_rows"] = {p: rows for p, rows in by_plex.items()}


# --- writing ---------------------------------------------------------------------

def write_outputs(tree: dict, install: dict, plan: dict, result: dict, workdir: str) -> None:
    repro = fp.repro_dir(workdir)
    os.makedirs(repro, exist_ok=True)
    wf_path = os.path.join(repro, fp.WORKFLOW_NAME)
    mf_path = os.path.join(repro, fp.MANIFEST_NAME)

    with open(result["base_path"], "r", encoding="utf-8", errors="replace", newline="") as fh:
        base_text = fh.read()
    header = [fp.MARKER,
              "# Built by FragSkills build_workflow.py from %s.workflow (FragPipe %s)."
              % (result["route"], install.get("version")),
              "# Change it through the builder: any edit makes fragpipe_run.py refuse it."]
    text = render_workflow(base_text, {c["key"]: c["value"] for c in result["changes"]}, header)
    with open(wf_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    mm.write_manifest(mf_path, [(i["file"], i["experiment"], i["replicate"], i["data_type"])
                                for i in result["inputs"]])

    annotations = []
    for a in result.get("annotations", []):
        copy = os.path.join(repro, fp.ANNOTATIONS_DIR, "%s_annotation.txt" % a["plex"])
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        if a["status"] == "existing":
            shutil.copyfile(a["path"], copy)        # exactly what FragPipe will read
        else:
            with open(copy, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(a["text"])
        annotations.append({k: a[k] for k in ("plex", "folder", "path", "status",
                                              "own_sha256") if a.get(k)})
        annotations[-1]["copy"] = copy
        annotations[-1]["sha256"] = a.get("sha256") or fp.sha256_file(copy)

    plan.update({
        "state": fp.STATE_BUILT,
        "route": {"workflow": result["route"], "base": result["base_path"],
                  "base_sha256": fp.sha256_file(result["base_path"]),
                  "label": "shipped by FragPipe"},
        "resolved": result["resolved"],
        "changes": result["changes"],
        "inputs": result["inputs"],
        "annotations": annotations,
        "channel_rows": result.get("channel_rows"),
        "notes": result.get("notes") or [],
        "files": {"workflow": wf_path, "manifest": mf_path,
                  "workflow_sha256": fp.sha256_file(wf_path),
                  "manifest_sha256": fp.sha256_file(mf_path)},
        "confirmed": None,
    })


def confirm(plan: dict, plan_file: str) -> List[str]:
    """Write the TMT annotations and save what the user confirmed -- all or nothing."""
    if plan.get("state") not in (fp.STATE_BUILT, fp.STATE_CONFIRMED):
        return ["nothing to confirm: the plan still has open questions"]
    files = plan["files"]
    for label in ("workflow", "manifest"):
        path = files[label]
        if not os.path.isfile(path) or fp.sha256_file(path) != files[label + "_sha256"]:
            return ["%s changed after the table was shown; re-run the builder and show the "
                    "user the new table" % path]
    written = {}
    annotations = plan.get("annotations") or []
    # check every annotation before writing any, so a refusal leaves nothing half done
    for a in annotations:
        path = a["path"]
        others = [f for f in annotation_files(a["folder"])
                  if os.path.normcase(f) != os.path.normcase(path)]
        if others:
            return ["%s also holds %s; FragPipe needs exactly one annotation per plex "
                    "folder. Move the extra one away, then re-run the builder."
                    % (a["folder"], ", ".join(os.path.basename(f) for f in others))]
        if os.path.lexists(path) and not os.path.isfile(path):
            return ["%s is not a file, but FragPipe counts it as an annotation; rename it or "
                    "move it away, then re-run the builder" % path]
        if a["status"] in ("existing", "written"):
            if not os.path.isfile(path) or fp.sha256_file(path) != a["sha256"]:
                return ["%s changed after the table was shown; re-run the builder" % path]
            continue
        if os.path.isfile(path) and not (a.get("own_sha256")
                                         and fp.sha256_file(path) == a["own_sha256"]):
            return ["%s appeared after the table was shown; re-run the builder" % path]
        if not os.path.isfile(a["copy"]):
            return ["%s is missing; re-run the builder" % a["copy"]]
        if fp.sha256_file(a["copy"]) != a["sha256"]:
            return ["%s changed after the table was shown; re-run the builder" % a["copy"]]
    done = []                                   # (annotation, what it replaced)
    saved = json.loads(json.dumps(plan))
    try:
        for a in annotations:
            if a["status"] in ("existing", "written"):
                continue
            before = None
            if os.path.isfile(a["path"]):       # the builder's own earlier copy
                with open(a["path"], "rb") as fh:
                    before = fh.read()
            with open(a["copy"], "rb") as fh:
                data = fh.read()
            try:
                with open(a["path"], "wb") as fh:
                    done.append((a, before))
                    fh.write(data)
            except OSError as exc:              # a full disk fails at close, unnamed
                if exc.filename is None:
                    raise OSError(exc.errno, exc.strerror, a["path"]) from exc
                raise
        own = dict(plan.get("own_annotations") or {})
        for a in annotations:
            if a["status"] == "to write":
                a["status"] = "written"
            if a["status"] == "written":
                own[a["path"]] = a["sha256"]
            written[a["path"]] = a["sha256"]
        plan["own_annotations"] = own
        plan["state"] = fp.STATE_CONFIRMED
        plan["confirmed"] = {"at": fp.now_iso(), "workflow_sha256": files["workflow_sha256"],
                             "manifest_sha256": files["manifest_sha256"],
                             "annotations": written,
                             "fragpipe_version": (plan.get("fragpipe") or {}).get("version")}
        fp.save_plan(plan_file, plan)           # inside: a plan that cannot be saved
    except OSError as exc:                      # leaves no annotation it does not record
        for a, before in done:                  # put the raw folders back as they were
            try:
                if before is None:
                    os.remove(a["path"])
                else:
                    with open(a["path"], "wb") as fh:
                        fh.write(before)
            except OSError:
                pass
        plan.clear()
        plan.update(saved)
        failed = exc.filename2 or exc.filename or plan_file  # os.replace names its target 2nd
        return ["could not write %s (%s); nothing was confirmed" % (failed, exc.strerror or exc)]
    return []


# --- output -------------------------------------------------------------------

def _rows(table: List[Tuple[str, ...]], widths: Tuple[Optional[int], ...]) -> List[str]:
    """Align columns; a width of None fits the column to its content, uncut."""
    widths = tuple(w if w is not None else max(len(str(r[i])) for r in table)
                   for i, w in enumerate(widths))
    out = []
    for row in table:
        cells = []
        for cell, w in zip(row, widths):
            cell = str(cell)
            cells.append(cell if len(cell) <= w else cell[:w - 3] + "...")
        out.append("  " + "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip())
    return out


def render_table(tree: dict, install: dict, plan: dict, workdir: str) -> str:
    L = ["FragSkills workflow plan", ""]
    L.append("  FragPipe  : %s  (%s)" % (install.get("version"), install.get("root")))
    L.append("  workdir   : %s" % os.path.abspath(workdir))
    route = plan["route"]
    n_changed = len(plan["changes"])
    L.append("  workflow  : %s, %s; used unchanged apart from the %d setting%s below"
             % (route["workflow"], route["label"], n_changed, "" if n_changed == 1 else "s"))
    L += ["", "Answers"]
    L += _rows([("question", "answer", "source")] +
               [(r["id"], r["label"], r["source"]) for r in plan["resolved"]], (20, 62, 8))
    L += ["", "Parameters changed from %s.workflow (every other parameter is FragPipe's)"
          % route["workflow"]]
    if plan["changes"]:
        table = [("parameter", "source", "value")]
        for c in plan["changes"]:
            value = c["value"] if c["base"] is None else "%s  (was %s)" % (c["value"], c["base"])
            table.append((c["key"], c["source"], value))
        L += _rows([(k, s, v) if i else ("parameter", "source", "value")
                    for i, (k, s, v) in enumerate(table)], (32, 30, None))
    else:
        L.append("  (none)")

    inputs = plan["inputs"]
    exps = OrderedDict()
    for i in inputs:
        exps[i["experiment"] or "(unnamed)"] = exps.get(i["experiment"] or "(unnamed)", 0) + 1
    types = sorted({i["data_type"] for i in inputs})
    L += ["", "Input files: %d in %d experiment%s, data type %s"
          % (len(inputs), len(exps), "" if len(exps) == 1 else "s", ", ".join(types))]
    L.append("  " + ", ".join("%s (%d)" % kv for kv in list(exps.items())[:12])
             + (", ..." if len(exps) > 12 else ""))
    if plan.get("annotations"):
        L += ["", "TMT annotations (FragPipe reads them from the raw-file folders)"]
        for a in plan["annotations"]:
            what = {"existing": "existing file, used as is",
                    "to write": "written at --confirm",
                    "written": "written"}[a["status"]]
            L.append("  %-12s %s  [%s]" % (a["plex"], a["path"], what))
            rows = (plan.get("channel_rows") or {}).get(a["plex"]) or []
            if rows:
                L.append("  %-12s %s" % ("", "  ".join("%s=%s" % (r["channel"], r["sample"])
                                                       for r in rows)))
    for n in plan.get("notes") or []:
        L.append("  NOTE: %s" % n)

    L += ["", "Files", "  %s" % plan["files"]["workflow"], "  %s" % plan["files"]["manifest"],
          "  %s" % fp.plan_path(workdir)]
    L.append("")
    if plan.get("state") == fp.STATE_CONFIRMED:
        L.append("CONFIRMED at %s. Launch with:" % plan["confirmed"]["at"])
        L.append("  python scripts/fragpipe_run.py --workflow \"%s\" --manifest \"%s\" "
                 "--workdir \"%s\" --fragpipe-dir \"%s\""
                 % (plan["files"]["workflow"], plan["files"]["manifest"],
                    os.path.abspath(workdir), install.get("root")))
    else:
        L.append("NOT CONFIRMED. Show this table to the user. When they confirm, run:")
        L.append("  python scripts/build_workflow.py --workdir \"%s\" --confirm"
                 % os.path.abspath(workdir))
    return "\n".join(L)


def render_rejected(rejected: List[dict]) -> List[str]:
    L = []
    for r in rejected:
        near = r.get("nearest")
        L.append("REJECTED %s=%s: %s%s" % (
            r["id"], r["value"], r["reason"],
            "; nearest shipped workflow: %s (differs in %s)"
            % (near["workflow"], ", ".join("%s=%s" % kv for kv in near["differs"].items()))
            if near else ""))
    return L


def render_dropped(dropped: List[dict]) -> List[str]:
    return ["DROPPED %s=%s: %s" % (d["id"], d["value"], d["reason"]) for d in dropped]


def render_questions(result: dict) -> str:
    L = render_rejected(result.get("rejected", [])) + render_dropped(result.get("dropped", []))
    if L:
        L.append("")
    L.append("Open questions (answer with --answer ID=VALUE):")
    for q in result["open"]:
        L.append("")
        L.append("  [%s] %s" % (q["id"], q["text"]))
        for a in q.get("answers", []):
            L.append("      %-16s %s%s" % (a["value"], a["label"],
                                          "  (default)" if q.get("default") == a["value"] else ""))
        for u in q.get("unavailable", []):
            differs = u["nearest"].get("differs") or {}
            L.append("      (%s not shipped with these answers; nearest: %s%s)"
                     % (u["value"], u["nearest"]["workflow"],
                        ", with %s" % ", ".join("%s=%s" % kv for kv in sorted(differs.items()))
                        if differs else ""))
        if q.get("path"):
            L.append("      file: %s" % q["path"])
        for p in q.get("problems", []) + ([q["problem"]] if q.get("problem") else []):
            L.append("      PROBLEM: %s" % p)
        for n in q.get("notes", []):
            L.append("      NOTE: %s" % n)
        if q.get("rows"):
            cols = q["columns"]
            shown = [tuple(os.path.basename(r[c].rstrip("\\/")) if c == "file" else r.get(c, "")
                           for c in cols) for r in q["rows"][:40]]
            L += ["      " + line for line in
                  _rows([tuple(cols)] + shown,
                        tuple(44 if c in ("file", "sample") else 14 for c in cols))]
            if len(q["rows"]) > 40:
                L.append("        ... %d more rows in the file" % (len(q["rows"]) - 40))
    return "\n".join(L)


# --- command line ---------------------------------------------------------------

def _parse_answers(items: List[str], tree: dict) -> Tuple[Dict[str, str], List[str]]:
    known = set(tree["_questions"]) | set(tree["_settings"])
    out, problems = {}, []
    for item in items or []:
        if "=" not in item:
            problems.append("--answer needs ID=VALUE, got '%s'" % item)
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in known:
            problems.append("unknown question '%s' (known: %s)" % (k, ", ".join(sorted(known))))
            continue
        q = tree["_questions"].get(k) or tree["_settings"][k]
        values = [a["value"] for a in q.get("answers", [])]
        if values and v not in values:
            problems.append("'%s' is not an answer to %s (answers: %s)"
                            % (v, k, ", ".join(values)))
            continue
        out[k] = v
    return out, problems


def _cannot_write(exc: OSError, default: str) -> None:
    print("[build_workflow] ERROR: could not write %s (%s); close any program that holds it "
          "(a sync client, an editor), then re-run"
          % (exc.filename2 or exc.filename or default, exc.strerror or exc), file=sys.stderr)


def _install(path: str) -> Optional[dict]:
    root = _root_of(path)
    return install_from_root(root) if root else None


def _rebase(plan: dict, workdir: str) -> None:
    """A plan copied or moved with its workdir: the sheets it accepted from the old
    fragskills-repro/ are the copies in this one."""
    old, new = fp.repro_dir(plan["workdir"]), fp.repro_dir(workdir)
    for key in ("design", "channels"):
        path = plan.get(key)
        if path and _inside(path, old):
            plan[key] = os.path.join(new, os.path.relpath(path, old))
    plan["workdir"] = workdir


#: Plan entries every call reads, when present, and their types.
_PLAN_TYPES = (("workdir", str), ("answers", dict), ("fragpipe", dict), ("data", list),
               ("design", str), ("channels", str), ("design_for", dict),
               ("channels_for", dict), ("design_proposal", dict),
               ("channels_proposal", dict), ("own_annotations", dict))
#: The keys --confirm and --show read in each item of a built plan's lists.
_ITEM_KEYS = (("changes", ("key", "value", "base", "source")),
              ("inputs", ("file", "experiment", "replicate", "data_type")),
              ("resolved", ("id", "label", "source")),
              ("annotations", ("plex", "folder", "path", "status", "sha256")))


def _damaged(plan: dict, built: bool = True) -> List[str]:
    """What a plan edited by hand lacks that the builder reads (*built*: also what
    --confirm and --show read); empty when it is whole."""
    bad = [k for k, kind in _PLAN_TYPES if plan.get(k) is not None
           and not isinstance(plan[k], kind)]
    if plan.get("answers") is None:
        bad.append("answers")
    if not built:
        return bad
    need = [("route", dict), ("files", dict), ("changes", list), ("resolved", list),
            ("inputs", list)]
    if plan.get("state") == fp.STATE_CONFIRMED:
        need.append(("confirmed", dict))
    bad += [k for k, kind in need if not isinstance(plan.get(k), kind)]
    files = plan.get("files") if isinstance(plan.get("files"), dict) else {}
    bad += ["files.%s" % k for k in ("workflow", "manifest", "workflow_sha256",
                                     "manifest_sha256") if not isinstance(files.get(k), str)]
    if isinstance(plan.get("route"), dict) and not isinstance(plan["route"].get("workflow"),
                                                              str):
        bad.append("route.workflow")
    for key, keys in _ITEM_KEYS:
        items = plan.get(key)
        if key in bad or (key == "annotations" and items is None):
            continue
        # an annotation still to write is written from its copy in fragskills-repro/
        if not isinstance(items, list) or not all(
                isinstance(i, dict) and all(k in i for k in keys)
                and (key != "annotations" or i["status"] != "to write" or i.get("copy"))
                for i in items):
            bad.append(key)
    return bad


def _emit(args, payload: dict, text: str) -> None:
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    try:
        text.encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        print("NOTE: this console cannot show some characters below, so they print as "
              "\\u escapes and those paths cannot be copied from here. Run with --json for "
              "the exact paths.")
    print(text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build a FragPipe workflow from questions about the data.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--workdir", required=True, help="the run's output directory")
    ap.add_argument("--fragpipe-dir",
                    help="the FragPipe folder confirmed in Step 1 (remembered in the plan)")
    ap.add_argument("--data", nargs="+",
                    help="folder(s) holding the raw files; replaces those given before")
    ap.add_argument("--answer", action="append", metavar="ID=VALUE",
                    help="answer one question; repeat as needed")
    ap.add_argument("--design", help="accept this design sheet (file, condition, "
                                     "replicate, data_type)")
    ap.add_argument("--channels", help="accept this TMT/iTRAQ channel sheet")
    ap.add_argument("--confirm", action="store_true",
                    help="record that the user confirmed the table, and write the TMT "
                         "annotations")
    ap.add_argument("--show", action="store_true",
                    help="print the table of a built or confirmed plan")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # a path outside the console's code page
        try:                                    # must not crash after a confirm
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    tree = wt.load_tree()
    workdir = os.path.abspath(args.workdir)
    plan_file = fp.plan_path(workdir)
    try:
        loaded = fp.load_plan(plan_file)
    except fp.PlanError as exc:
        print("[build_workflow] ERROR: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR
    plan = loaded or {"schema": 1, "created_at": fp.now_iso(), "workdir": workdir,
                      "answers": {}}
    if (args.confirm or args.show) and (args.answer or args.design or args.channels
                                        or args.data):
        print("[build_workflow] ERROR: --confirm and --show take no --answer, --design, "
              "--channels or --data: run those first, show the user the new table, then "
              "confirm it on its own.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if (args.confirm or args.show) and loaded is None:
        print("[build_workflow] ERROR: no plan at %s. --workdir is the run's output folder, "
              "the one that holds %s/." % (plan_file, fp.REPRO_DIR), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    damaged = _damaged(plan, built=False)
    if damaged:
        print("[build_workflow] ERROR: %s is damaged (%s wrong). Re-run build_workflow.py "
              "with a new --workdir, or delete the file to start the plan again."
              % (plan_file, ", ".join(damaged)), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    built_in = plan.get("workdir")
    if built_in and not fp._same_file(built_in, workdir):
        if args.confirm or args.show:
            # its table and files are those of the other workdir
            print("[build_workflow] ERROR: this plan was built in %s, not in %s. Re-run "
                  "build_workflow.py --workdir \"%s\" without --confirm or --show to rebuild "
                  "it here, show the user the new table, then confirm it."
                  % (built_in, workdir, workdir), file=sys.stderr)
            return EXIT_CONFIG_ERROR
        _rebase(plan, workdir)
    built_on = (plan.get("fragpipe") or {}).get("dir")
    fragpipe_dir = args.fragpipe_dir or built_on
    if not fragpipe_dir:
        print("[build_workflow] ERROR: pass --fragpipe-dir with the FragPipe folder the "
              "user confirmed (check_fragpipe.py, Step 1).", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    install = _install(fragpipe_dir)    # a root, its bin folder or the launcher
    if (args.confirm or args.show) and args.fragpipe_dir and built_on and install and \
            not fp._same_file(install["root"], built_on):
        print("[build_workflow] ERROR: this plan was built from %s, not from %s; --confirm "
              "and --show use the FragPipe it was built from. To use the other one, rebuild "
              "with --fragpipe-dir and show the user the new table."
              % (built_on, install["root"]), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if install is None:
        print("[build_workflow] ERROR: %s is not a FragPipe installation." % fragpipe_dir,
              file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.confirm or args.show:
        if plan.get("state") not in (fp.STATE_BUILT, fp.STATE_CONFIRMED):
            print("[build_workflow] ERROR: no built plan in %s yet; run without --confirm "
                  "or --show to see the open questions." % plan_file, file=sys.stderr)
            return EXIT_CONFIG_ERROR
        damaged = _damaged(plan)
        if damaged:
            print("[build_workflow] ERROR: %s is damaged (%s missing or wrong). Re-run "
                  "build_workflow.py --workdir \"%s\" without --confirm or --show to rebuild "
                  "it, then show the table and confirm." % (plan_file, ", ".join(damaged),
                                                             workdir), file=sys.stderr)
            return EXIT_CONFIG_ERROR
        if args.confirm:
            problems = confirm(plan, plan_file)
            if problems:
                for p in problems:
                    print("[build_workflow] ERROR: %s" % p, file=sys.stderr)
                return EXIT_CONFIG_ERROR
        _emit(args, {"state": plan["state"], "plan": plan_file, "route": plan["route"],
                     "changes": plan["changes"], "files": plan["files"],
                     "table": render_table(tree, install, plan, workdir)},
              render_table(tree, install, plan, workdir))
        return EXIT_OK

    new_answers, problems = _parse_answers(args.answer, tree)
    for label, path in (("--design", args.design), ("--channels", args.channels)):
        if path and not os.path.isfile(path):
            problems.append("%s: no such file %s" % (label, path))
    if args.data:
        for d in args.data:
            if not os.path.exists(d):
                problems.append("--data: no such folder %s" % d)
    if problems:
        for p in problems:
            print("[build_workflow] ERROR: %s" % p, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    plan["fragpipe"] = {"dir": install["root"], "version": install.get("version")}
    if args.data:
        plan["data"] = sorted({os.path.abspath(d) for d in args.data})
    repro = fp.repro_dir(workdir)
    if args.design:
        # the builder's proposal is accepted for what it was proposed for; another
        # sheet for the folders given so far, and the first build settles its workflow
        plan["design"] = os.path.abspath(args.design)
        made = plan.get("design_proposal")
        if made and fp._same_path(plan["design"], os.path.join(repro, fp.DESIGN_NAME)):
            plan["design_for"] = dict(made)
        else:
            plan["design_for"] = {"data_set": None, "data": plan.get("data") or []}
    if args.channels:
        plan["channels"] = os.path.abspath(args.channels)
        made = plan.get("channels_proposal")
        plan["channels_for"] = dict(made) if made and fp._same_path(
            plan["channels"], os.path.join(repro, fp.CHANNELS_NAME)) else None
    plan["answers"].update(new_answers)
    if plan["answers"].get("fasta"):
        plan["answers"]["fasta"] = os.path.abspath(os.path.expanduser(plan["answers"]["fasta"]))

    result = evaluate(tree, install, plan, workdir)

    def saved() -> bool:
        try:
            fp.save_plan(plan_file, plan)
            return True
        except OSError as exc:
            _cannot_write(exc, plan_file)
            return False
    # keep the answers that still count (used, rejected until replaced, not asked yet);
    # those the other answers made irrelevant are reported as dropped
    settings = {k: v for k, v in plan["answers"].items() if k in tree["_settings"]}
    if "kept_answers" in result:
        plan["answers"] = dict(result["kept_answers"], **settings)
    plan["fragpipe"]["supported_as"] = result.get("supported_as")
    plan.update(result.get("plan_updates") or {})    # what each sheet was made for

    if result["state"] == "refused":
        for p in result["problems"]:
            print("[build_workflow] REFUSED: %s" % p, file=sys.stderr)
        return EXIT_REFUSED
    if result["rejected"]:
        plan["state"], plan["confirmed"] = fp.STATE_QUESTIONS, None
        if not saved():
            return EXIT_CONFIG_ERROR
        _emit(args, {"state": "rejected", "plan": plan_file, "rejected": result["rejected"],
                     "open": result["open"], "dropped": result.get("dropped", []),
                     "resolved": result.get("resolved", [])},
              render_questions(result))
        return EXIT_REFUSED
    if result["problems"]:
        for line in render_dropped(result.get("dropped", [])):
            print("[build_workflow] %s" % line, file=sys.stderr)
        for p in result["problems"]:
            print("[build_workflow] ERROR: %s" % p, file=sys.stderr)
        plan["state"], plan["confirmed"] = fp.STATE_QUESTIONS, None
        if not saved():
            return EXIT_CONFIG_ERROR
        return EXIT_CONFIG_ERROR
    if result["open"]:
        plan["state"], plan["confirmed"] = fp.STATE_QUESTIONS, None
        if not saved():
            return EXIT_CONFIG_ERROR
        _emit(args, {"state": "questions", "plan": plan_file, "open": result["open"],
                     "rejected": result["rejected"], "dropped": result.get("dropped", []),
                     "resolved": result.get("resolved", [])},
              render_questions(result))
        return EXIT_QUESTIONS

    try:
        write_outputs(tree, install, plan, result, workdir)
    except OSError as exc:
        _cannot_write(exc, fp.repro_dir(workdir))
        return EXIT_CONFIG_ERROR
    if not saved():
        return EXIT_CONFIG_ERROR
    table = render_table(tree, install, plan, workdir)
    if result.get("dropped"):
        table = "\n".join(render_dropped(result["dropped"])) + "\n\n" + table
    _emit(args, {"state": plan["state"], "plan": plan_file, "route": plan["route"],
                 "dropped": result.get("dropped", []), "changes": plan["changes"],
                 "files": plan["files"], "table": table}, table)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
