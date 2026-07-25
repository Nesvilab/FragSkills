"""Locate the FragPipe installation and show it for confirmation.

**Run this before every FragPipe run.**  Auto-detection can pick the wrong
installation when several versions are present, so the folder must always be
either specified explicitly or shown to the user and confirmed.

    python scripts/check_fragpipe.py
    python scripts/check_fragpipe.py --fragpipe-dir "C:/FragPipe/FragPipe-24.1"
    python scripts/check_fragpipe.py --json

Exit codes: 0 = an installation was found (show it and confirm),
            4 = nothing found (ask the user for the folder).

``fragpipe_run.py`` imports :func:`locate` and refuses to start until the
location has been confirmed.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
from typing import Dict, List, Optional, Tuple

WINDOWS = os.name == "nt"
LAUNCHER = "fragpipe.bat" if WINDOWS else "fragpipe"

EXIT_FOUND, EXIT_NOT_FOUND = 0, 4

#: Resolution sources that represent a deliberate choice by the user. Anything
#: else is a guess and must be confirmed before a run. Imported by
#: fragpipe_run.py so the two cannot drift apart.
EXPLICIT_SOURCES = ("--fragpipe-dir", "--fragpipe", "$FRAGPIPE_DIR", "$FRAGPIPE_BIN")


def is_explicit(source: str) -> bool:
    return source in EXPLICIT_SOURCES

#: Where FragPipe is commonly installed. Matches installation *roots*.
CANDIDATE_ROOT_GLOBS = [
    "C:/FragPipe*",
    "C:/FragPipe/*",
    "C:/Program Files/FragPipe*",
    "C:/Program Files/FragPipe/*",
    "D:/FragPipe*",
    "D:/FragPipe/*",
    "/opt/fragpipe*",
    "/opt/FragPipe*",
    "/usr/local/fragpipe*",
    "~/fragpipe*",
    "~/FragPipe*",
]

_RE_VERSION_JAR = re.compile(r"^fragpipe-(.+)\.jar$", re.IGNORECASE)


def _version_key(text: str) -> Tuple[int, ...]:
    """Numeric sort key. 'FragPipe-24.1-build36' -> (24, 1, 36).

    Plain lexical sorting is wrong: it ranks 'FragPipe-9.4' above
    'FragPipe-24.1' because '9' > '2' as a string.
    """
    return tuple(int(n) for n in re.findall(r"\d+", text)) or (0,)


def _launcher_in(root: str) -> Optional[str]:
    p = os.path.join(root, "bin", LAUNCHER)
    if os.path.isfile(p):
        return p
    # Unix installs sometimes ship only the .bat name, or vice versa.
    for alt in ("fragpipe", "fragpipe.bat"):
        p = os.path.join(root, "bin", alt)
        if os.path.isfile(p):
            return p
    return None


def _detect_version(root: str) -> Optional[str]:
    for jar in glob.glob(os.path.join(root, "lib", "fragpipe-*.jar")):
        m = _RE_VERSION_JAR.match(os.path.basename(jar))
        if m:
            return m.group(1)
    m = re.search(r"FragPipe[-_]?([0-9][^\\/]*)", os.path.basename(root), re.I)
    return m.group(1) if m else None


def _find_diann(root: str) -> Optional[str]:
    name = "DiaNN.exe" if WINDOWS else "diann*"
    for hit in sorted(glob.glob(os.path.join(root, "tools", "diann", "*", "*", name))):
        if os.path.isfile(hit):
            return hit
    return None


def install_from_root(root: str) -> Optional[Dict]:
    """Describe a FragPipe installation, or None if *root* is not one."""
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return None
    launcher = _launcher_in(root)
    if not launcher:
        return None

    tools = os.path.join(root, "tools")
    workflows = os.path.join(root, "workflows")
    python_dir = os.path.join(root, "python")
    wf_files = sorted(glob.glob(os.path.join(workflows, "*.workflow")))

    return {
        "root": root,
        "launcher": launcher,
        "version": _detect_version(root),
        "tools_dir": tools if os.path.isdir(tools) else None,
        "workflows_dir": workflows if os.path.isdir(workflows) else None,
        "workflow_count": len(wf_files),
        "python_dir": python_dir if os.path.isdir(python_dir) else None,
        "diann_bin": _find_diann(root),
        "has_spaces": (" " in root or "\t" in root),
    }


def _root_of(path: str) -> Optional[str]:
    """Accept a root, a bin/ folder, or the launcher file itself."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        # .../bin/fragpipe.bat -> ...
        return os.path.dirname(os.path.dirname(path))
    if os.path.isdir(path):
        if os.path.basename(path).lower() == "bin":
            return os.path.dirname(path)
        return path
    return None


def find_all_installs() -> List[Dict]:
    """Every FragPipe installation discoverable in the usual locations."""
    roots = set()
    for pattern in CANDIDATE_ROOT_GLOBS:
        for hit in glob.glob(os.path.expanduser(pattern)):
            if os.path.isdir(hit):
                roots.add(os.path.abspath(hit))

    on_path = shutil.which(LAUNCHER) or shutil.which("fragpipe")
    if on_path:
        r = _root_of(on_path)
        if r:
            roots.add(r)

    installs = [i for i in (install_from_root(r) for r in sorted(roots)) if i]
    installs.sort(key=lambda i: _version_key(i["version"] or os.path.basename(i["root"])),
                  reverse=True)
    return installs


def locate(explicit_dir: Optional[str] = None,
           explicit_bin: Optional[str] = None) -> Tuple[Optional[Dict], str, List[Dict]]:
    """Resolve the installation to use.

    Returns ``(install, source, other_installs)``.  ``install`` is None when
    nothing was found -- the caller must then ask the user for the folder.
    ``source`` records how it was resolved, so the caller can tell an explicit
    choice from a guess.
    """
    # 1. explicit folder
    if explicit_dir:
        root = _root_of(explicit_dir)
        if not root:
            raise SystemExit("--fragpipe-dir does not exist: %s" % explicit_dir)
        inst = install_from_root(root)
        if not inst:
            raise SystemExit(
                "%s is not a FragPipe installation (no bin/%s inside)."
                % (root, LAUNCHER))
        return inst, "--fragpipe-dir", []

    # 2. explicit launcher
    if explicit_bin:
        if not os.path.isfile(os.path.expanduser(explicit_bin)):
            raise SystemExit("--fragpipe does not point to a file: %s" % explicit_bin)
        root = _root_of(explicit_bin)
        inst = install_from_root(root) if root else None
        if not inst:  # a launcher outside a normal layout is still usable
            inst = {"root": root, "launcher": os.path.abspath(explicit_bin),
                    "version": None, "tools_dir": None, "workflows_dir": None,
                    "workflow_count": 0, "python_dir": None, "diann_bin": None,
                    "has_spaces": " " in (explicit_bin or "")}
        return inst, "--fragpipe", []

    # 3. environment
    env_dir = os.environ.get("FRAGPIPE_DIR")
    if env_dir:
        root = _root_of(env_dir)
        inst = install_from_root(root) if root else None
        if inst:
            return inst, "$FRAGPIPE_DIR", []
    env_bin = os.environ.get("FRAGPIPE_BIN")
    if env_bin and os.path.isfile(env_bin):
        root = _root_of(env_bin)
        inst = install_from_root(root) if root else None
        if inst:
            return inst, "$FRAGPIPE_BIN", []

    # 4. Nothing was specified, so this is a guess: take the NEWEST installation
    #    found anywhere -- including whatever is on PATH, which is ranked with
    #    the rest rather than winning automatically. An old fragpipe left on
    #    PATH must not quietly outrank a newer install.
    all_installs = find_all_installs()          # newest first
    if not all_installs:
        return None, "not found", []

    best = dict(all_installs[0])
    others = all_installs[1:]

    on_path = shutil.which(LAUNCHER) or shutil.which("fragpipe")
    path_root = _root_of(on_path) if on_path else None
    known = {os.path.normcase(i["root"]) for i in all_installs}

    if path_root and os.path.normcase(path_root) in known:
        if os.path.normcase(path_root) == os.path.normcase(best["root"]):
            source = "PATH, and the newest installed"
        else:
            source = "newest of %d installed" % len(all_installs)
            best["path_install_ignored"] = path_root
    elif len(all_installs) > 1:
        source = "newest of %d installed" % len(all_installs)
    else:
        source = "auto-detected"
    return best, source, others


def not_found_help() -> str:
    """Explain where we looked, using this platform's conventions."""
    if WINDOWS:
        searched = r"C:\FragPipe*, C:\Program Files\FragPipe*, D:\FragPipe*"
        example = r"C:\FragPipe\FragPipe-24.1-build36"
        setter = r'set FRAGPIPE_DIR=C:\FragPipe\FragPipe-24.1-build36'
    else:
        searched = "/opt/fragpipe*, /usr/local/fragpipe*, ~/fragpipe*"
        example = "/opt/fragpipe"
        setter = "export FRAGPIPE_DIR=/opt/fragpipe"
    return """\
No FragPipe installation was found.

Looked in: $FRAGPIPE_DIR, $FRAGPIPE_BIN, PATH, and the usual install locations
(%s).

Please provide the folder where FragPipe is installed -- the one containing
bin/, tools/, and workflows/, for example:
    %s

Then pass it as --fragpipe-dir <folder>, or set it once with:
    %s""" % (searched, example, setter)


def render(install: Optional[Dict], source: str, others: List[Dict]) -> str:
    if install is None:
        return not_found_help()

    L = ["FragPipe installation", ""]
    L.append("  folder    : %s" % install["root"])
    L.append("  launcher  : %s" % install["launcher"])
    L.append("  version   : %s" % (install["version"] or "unknown"))
    L.append("  tools     : %s" % (install["tools_dir"] or "MISSING"))
    L.append("  workflows : %s" % (
        "%s (%d workflows)" % (install["workflows_dir"], install["workflow_count"])
        if install["workflows_dir"] else "MISSING"))
    L.append("  python    : %s" % (install["python_dir"] or "not bundled"))
    L.append("  DIA-NN    : %s" % (install["diann_bin"] or "not found"))
    L.append("  resolved  : %s" % source)

    warnings = []
    if install["has_spaces"]:
        warnings.append("The path contains a space. FragPipe refuses to run from "
                        "such a path -- move the installation.")
    if not install["tools_dir"]:
        warnings.append("No tools/ folder: MSFragger and IonQuant will not be found.")
    if not install["workflows_dir"]:
        warnings.append("No workflows/ folder: no built-in workflows to start from.")
    for w in warnings:
        L.append("")
        L.append("  WARNING: %s" % w)

    if install.get("path_install_ignored"):
        L.append("")
        L.append("  NOTE: '%s' on PATH points at %s, which is older."
                 % (LAUNCHER, install["path_install_ignored"]))
        L.append("        Using the newest installation instead.")

    if others:
        L.append("")
        L.append("Other installations found (%d, not used):" % len(others))
        for o in others:
            L.append("  %-52s %s" % (o["root"], o["version"] or ""))

    L.append("")
    if is_explicit(source):
        L.append("This location was specified explicitly.")
    else:
        L.append("This is the NEWEST installation found, chosen by default.")
        L.append("Confirm it is the FragPipe you want before running"
                 + (", or pick one of the others above." if others else "."))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Locate the FragPipe installation and show it for confirmation.")
    ap.add_argument("--fragpipe-dir", help="FragPipe installation folder")
    ap.add_argument("--fragpipe", help="path to the launcher instead of the folder")
    ap.add_argument("--list", action="store_true",
                    help="list every installation found")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    if args.list:
        installs = find_all_installs()
        if args.json:
            json.dump(installs, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            if not installs:
                print(not_found_help())
            for i in installs:
                print("%-52s %s" % (i["root"], i["version"] or ""))
        return EXIT_FOUND if installs else EXIT_NOT_FOUND

    install, source, others = locate(args.fragpipe_dir, args.fragpipe)
    if args.json:
        json.dump({"install": install, "source": source, "others": others,
                   "found": install is not None,
                   "needs_confirmation": install is not None and not is_explicit(source)},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render(install, source, others))
    return EXIT_FOUND if install else EXIT_NOT_FOUND


if __name__ == "__main__":
    sys.exit(main())
