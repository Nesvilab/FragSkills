"""Launch FragPipe in headless mode and publish live progress to a status file.

The FragPipe folder must always be settled before a run: pass --fragpipe-dir, or
let it be detected and confirm the folder it prints.  An unconfirmed guess is
refused (exit 3), because picking the wrong one of several installed versions
silently changes the result.

Typical use from Claude Code -- start it in the background, then poll::

    python scripts/check_fragpipe.py          # show the folder, confirm it first

    python scripts/fragpipe_run.py \
        --workflow     D:/run/fragpipe.workflow \
        --manifest     D:/run/files.fp-manifest \
        --workdir      D:/run/out \
        --fragpipe-dir "C:/FragPipe/FragPipe-24.1-build36"

    python scripts/fragpipe_status.py --workdir D:/run/out

Why a status file: real runs take minutes to many hours, far longer than a
foreground tool call can wait.  This script streams the child's stdout, keeps
``<workdir>/fragskills_status.json`` current, and mirrors everything to
``<workdir>/fragskills_run.log``.

Exit code of THIS script is meaningful, unlike FragPipe's own (always 0):

    0  FragPipe succeeded
    1  FragPipe failed
    2  bad arguments, missing input files, or a manifest FragPipe would misread
    3  the FragPipe folder was guessed -- show it to the user and confirm
    4  no FragPipe installation found -- ask the user for the folder
    5  a workflow from build_workflow.py whose plan is not confirmed, or has
       changed since -- show the user the table and confirm it
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fragpipe_progress import (  # noqa: E402
    RunProgress, STATE_FAILED, STATE_SUCCEEDED,
)
from check_fragpipe import (  # noqa: E402
    EXPLICIT_SOURCES, is_explicit, locate, render,
)
from fragpipe_stop import (  # noqa: E402
    LOCK_NAME, STOP_NAME, STOPPED_ERROR, fragpipe_pids, is_fragpipe, list_processes,
    lock_workdir, naming, roots, run_processes, tools_left, tools_still_running,
)
from fragskills_plan import LOCK_REFUSED, check_lock, is_builder_made  # noqa: E402
from make_manifest import check_manifest, has_diatracer_filter, has_name_rules  # noqa: E402

STATUS_NAME = "fragskills_status.json"
CONSOLE_LOG_NAME = "fragskills_run.log"

WINDOWS = os.name == "nt"

#: Exit codes. 0 = FragPipe succeeded, 1 = it failed.
EXIT_CONFIG_ERROR = 2       # bad arguments / missing input files
EXIT_NEEDS_CONFIRMATION = 3  # FragPipe folder was guessed and not confirmed
EXIT_NO_FRAGPIPE = 4         # no installation found -- ask the user
EXIT_PLAN_NOT_CONFIRMED = 5  # builder-made workflow, plan not confirmed


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_installation(args) -> Tuple[Optional[dict], str, list]:
    """Find the FragPipe installation, honouring an explicit choice."""
    return locate(args.fragpipe_dir, args.fragpipe)


def build_command(args, fragpipe_bin: str) -> List[str]:
    cmd = [fragpipe_bin, "--headless",
           "--workflow", os.path.abspath(args.workflow),
           "--manifest", os.path.abspath(args.manifest),
           "--workdir", os.path.abspath(args.workdir)]
    if args.ram is not None:
        cmd += ["--ram", str(args.ram)]
    if args.threads is not None:
        cmd += ["--threads", str(args.threads)]
    if args.tools_folder:
        cmd += ["--config-tools-folder", os.path.abspath(args.tools_folder)]
    if args.diann:
        cmd += ["--config-diann", os.path.abspath(args.diann)]
    if args.python_dir:
        cmd += ["--config-python", os.path.abspath(args.python_dir)]
    if args.dry_run:
        cmd += ["--dry-run"]
    return cmd


def malformed_unicode_escapes(text: str) -> List[int]:
    """Lines where ``java.util.Properties.load`` throws "Malformed \\uxxxx encoding": a
    backslash-u not followed by 4 hex digits.  Read as Java reads a .properties file:
    comment lines (# or !) skipped, a line ending in an odd number of backslashes
    continued on the next, ``\\\\`` an escaped backslash."""
    lines, bad, i = re.split(r"\r\n|\r|\n", text), [], 0
    while i < len(lines):
        start, logical = i, lines[i].lstrip(" \t\f")
        if logical and logical[0] not in "#!":
            while (len(logical) - len(logical.rstrip("\\"))) % 2 and i + 1 < len(lines):
                i += 1
                logical = logical[:-1] + lines[i].lstrip(" \t\f")
            j = 0
            while j < len(logical):
                if logical[j] != "\\":
                    j += 1
                elif logical[j + 1:j + 2] == "u" and not _HEX4.match(logical, j + 2):
                    bad.append(start + 1)
                    break
                else:
                    j += 2
        i += 1
    return bad


_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")


def preflight(args, fragpipe_bin: str, version: Optional[str] = None) -> List[str]:
    """Return a list of fatal problems (empty means good to go)."""
    problems = []
    for label, path in (("--workflow", args.workflow), ("--manifest", args.manifest)):
        if not os.path.isfile(path):
            problems.append("%s file not found: %s" % (label, path))
    if os.path.isfile(args.workflow):
        # PropsFile.load reads strict UTF-8 and only logs a failure, so FragPipe would
        # run without the settings after the first bad byte; UTF-16 with no byte-order
        # mark decodes as UTF-8 but spells every key with NULs between its letters
        text = ""
        try:
            with open(args.workflow, "rb") as fh:
                text = fh.read().decode("utf-8")
            bad = "\x00" in text
        except UnicodeDecodeError:
            bad = True
        except OSError as exc:
            problems.append("cannot read %s (%s)" % (args.workflow, exc.strerror or exc))
            bad = False
        if bad:
            problems.append(
                "%s is not UTF-8 text (UTF-16 or another encoding). FragPipe reads a workflow "
                "as UTF-8 and silently drops the settings it cannot read. %s" % (
                    args.workflow,
                    "It is the workflow builder's: re-run build_workflow.py, show the table "
                    "and confirm, instead of saving it again." if is_builder_made(args.workflow)
                    else "Save it as UTF-8."))
        else:
            for n in malformed_unicode_escapes(text):
                problems.append(
                    "%s line %d: a \\u not followed by 4 hex digits (such as an unescaped "
                    "D:\\db\\uniprot.fasta) makes Java stop reading the workflow, so FragPipe "
                    "cannot load it. Write each backslash in a value as \\\\ (see "
                    "references/running.md)." % (args.workflow, n))
    if os.path.isfile(args.manifest):
        # FragPipe runs in the workdir, so relative rows resolve there
        problems.extend(check_manifest(args.manifest, os.path.abspath(args.workdir),
                                       name_rules=has_name_rules(version),
                                       diatracer_filter=has_diatracer_filter(version)))
    if " " in fragpipe_bin or "\t" in fragpipe_bin:
        problems.append(
            "FragPipe refuses to run from a path containing spaces or tabs: %s"
            % fragpipe_bin)
    return problems


class StatusWriter:
    """Atomically maintains the status JSON, throttled to avoid churn."""

    def __init__(self, path: str, min_interval: float = 2.0) -> None:
        self.path = path
        self.min_interval = min_interval
        self._last = 0.0

    def write(self, payload: dict, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last) < self.min_interval:
            return
        self._last = now
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:  # never let status writing kill the run
            print("[fragskills] warning: could not write status file: %s" % exc,
                  file=sys.stderr, flush=True)


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_status(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def runs_in(workdir: str, status_path: str) -> Tuple[List[dict], List[dict]]:
    """FragPipe runs already writing to *workdir*, and the tools an earlier run there
    left running, as its status file records them; none when processes cannot be
    listed."""
    try:
        procs = list_processes()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("[fragskills] warning: cannot list processes (%s); not checking for another "
              "run in %s" % (exc, workdir), file=sys.stderr)
        return [], []
    last = _read_status(status_path) or {}
    tools = tools_still_running(procs, last)
    host = last.get("host")
    if host not in (None, socket.gethostname()) and last.get("tools_left") \
            and not last.get("stopped"):    # this computer cannot see them, nor refuse for them
        print("[fragskills] warning: the last run in %s, on %s, left tools running (PID %s); "
              "they may still be writing there. Check on %s with fragpipe_stop.py --workdir %s"
              % (workdir, host, ", ".join(str(t.get("pid")) for t in last["tools_left"]
                                          if isinstance(t, dict)), host, workdir),
              file=sys.stderr)
    known = {p["pid"] for p in tools}
    left = [p for p in naming(procs, workdir) if not is_fragpipe(p["cmd"])
            and p["pid"] not in known]
    if left:        # e.g. MSFragger, left running when its FragPipe was ended on its own
        print("[fragskills] warning: these processes name %s on their command line; if one is "
              "a tool an earlier FragPipe run left running, it may still be writing there:\n%s"
              % (workdir, "\n".join("    PID %-7d %s" % (p["pid"], p["cmd"][:150])
                                     for p in left)), file=sys.stderr)
    return roots(run_processes(procs, workdir)), tools


def collect_outputs(workdir: str) -> dict:
    """Light-weight inventory so the status file points at the real results."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from parse_reports import discover  # noqa: WPS433
        return discover(workdir)
    except Exception as exc:
        return {"error": "output discovery failed: %s" % exc}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run FragPipe headless with live progress reporting.")
    ap.add_argument("--workflow", required=True, help="path to a .workflow file")
    ap.add_argument("--manifest", required=True, help="path to a .fp-manifest file")
    ap.add_argument("--workdir", required=True, help="output directory")
    ap.add_argument("--fragpipe-dir",
                    help="FragPipe installation folder (the one containing bin/, "
                         "tools/, workflows/). Preferred over --fragpipe.")
    ap.add_argument("--fragpipe", help="path to fragpipe.bat / fragpipe")
    ap.add_argument("--accept-detected", action="store_true",
                    help="proceed with an auto-detected FragPipe folder. Only use "
                         "this after showing the folder to the user and having "
                         "them confirm it.")
    ap.add_argument("--ram", type=int, help="max RAM in GB (0 = let FragPipe decide)")
    ap.add_argument("--threads", type=int, help="thread count (default: cores - 1)")
    ap.add_argument("--tools-folder", help="--config-tools-folder passthrough")
    ap.add_argument("--diann", help="--config-diann passthrough (DiaNN.exe)")
    ap.add_argument("--python-dir", help="--config-python passthrough")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask FragPipe to plan the run without executing it")
    ap.add_argument("--status", help="status JSON path (default: <workdir>/%s)"
                                     % STATUS_NAME)
    ap.add_argument("--quiet", action="store_true",
                    help="only print progress events, not raw FragPipe output")
    ap.add_argument("--print-command", action="store_true",
                    help="print the resolved command line and exit")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # FragPipe's output (an en dash in a
        try:                                    # citation) must not end the watch on a
            stream.reconfigure(errors="backslashreplace")   # console that cannot show it
        except (AttributeError, ValueError):
            pass

    # --- locate FragPipe, and never run against an unconfirmed guess ---------
    try:
        install, source, others = resolve_installation(args)
    except SystemExit as exc:   # an explicit path that is not a FragPipe install
        print("[fragskills] ERROR: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG_ERROR
    print(render(install, source, others))
    print("")

    if install is None:
        print("[fragskills] ERROR: no FragPipe installation found. Ask the user for "
              "the folder, then pass --fragpipe-dir <folder>.", file=sys.stderr)
        return EXIT_NO_FRAGPIPE

    if not is_explicit(source) and not args.accept_detected:
        print("[fragskills] STOP: the FragPipe folder above was detected, not "
              "specified.\n"
              "           Show it to the user and have them confirm it, then "
              "re-run with\n"
              "             --fragpipe-dir \"%s\"\n"
              "           (or --accept-detected once confirmed)."
              % install["root"], file=sys.stderr)
        return EXIT_NEEDS_CONFIRMATION

    fragpipe_bin = install["launcher"]
    cmd = build_command(args, fragpipe_bin)

    if args.print_command:
        print(subprocess.list2cmdline(cmd))
        return 0

    problems = preflight(args, fragpipe_bin, install.get("version"))
    if problems:
        for p in problems:
            print("[fragskills] ERROR: %s" % p, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    lock, why = check_lock(args.workflow, args.manifest, install.get("version"))
    if lock == LOCK_REFUSED:
        print("[fragskills] STOP: %s" % why, file=sys.stderr)
        return EXIT_PLAN_NOT_CONFIRMED

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    # two runs in one folder overwrite each other's results; the lock stays held
    # until main() returns and drops it
    try:
        lock = lock_workdir(workdir)
        held = lock is None
    except OSError as exc:
        lock, held = None, False
        print("[fragskills] warning: cannot lock %s (%s); checking only for running FragPipe "
              "processes" % (workdir, exc), file=sys.stderr)
    if held:
        print("[fragskills] ERROR: another fragpipe_run.py is already running in %s (it holds "
              "%s). Wait for it to finish, stop it with fragpipe_stop.py, or choose another "
              "--workdir." % (workdir, LOCK_NAME), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    status_path = args.status or os.path.join(workdir, STATUS_NAME)
    busy, tools = runs_in(workdir, status_path)
    if busy:
        print("[fragskills] ERROR: FragPipe is already running in %s (PID %s). Wait for it "
              "to finish, stop it with fragpipe_stop.py, or choose another --workdir."
              % (workdir, ", ".join(str(r["pid"]) for r in busy)), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    if tools:
        print("[fragskills] ERROR: tools the last FragPipe run in %s started are still running "
              "(PID %s), and may still be writing there. Stop them with fragpipe_stop.py "
              "--workdir %s --yes once the user confirms, or choose another --workdir."
              % (workdir, ", ".join(str(p["pid"]) for p in tools), workdir), file=sys.stderr)
        return EXIT_CONFIG_ERROR
    console_log = os.path.join(workdir, CONSOLE_LOG_NAME)
    stop_path = os.path.join(workdir, STOP_NAME)     # fragpipe_stop.py writes it
    try:
        os.remove(stop_path)                         # left by an earlier run
    except OSError:
        pass

    prog = RunProgress()
    status = StatusWriter(status_path)
    started_at = time.time()

    def payload(extra: Optional[dict] = None) -> dict:
        base = {
            "schema": 1,
            "state": prog.state,
            "workdir": workdir,
            "command": cmd,
            "fragpipe_bin": fragpipe_bin,
            "fragpipe_dir": install.get("root"),
            "fragpipe_version": install.get("version"),
            "fragpipe_source": source,
            "started_at": datetime.fromtimestamp(started_at).astimezone()
                                  .isoformat(timespec="seconds"),
            "updated_at": _now_iso(),
            "elapsed_sec": round(time.time() - started_at, 1),
            "console_log": console_log,
            "status_file": status_path,
            "watcher_pid": os.getpid(),
            "host": socket.gethostname(),
            "summary": prog.describe(),
        }
        base.update(prog.snapshot())
        if extra:
            base.update(extra)
        return base

    status.write(payload({"state": "starting"}), force=True)
    print("[fragskills] %s" % subprocess.list2cmdline(cmd), flush=True)
    print("[fragskills] status file: %s" % status_path, flush=True)

    popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        bufsize=1, universal_newlines=True,
                        encoding="utf-8", errors="replace", cwd=workdir)
    if WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except OSError as exc:
        prog.state = STATE_FAILED
        prog.error = "could not start FragPipe: %s" % exc
        status.write(payload(), force=True)
        print("[fragskills] ERROR: %s" % prog.error, file=sys.stderr)
        return EXIT_CONFIG_ERROR
    run = {"pid": proc.pid}
    status.write(payload(run), force=True)    # fragpipe_stop.py needs it

    # Windows: FragPipe's own processes, so the tools its JVM starts can be found
    # if that JVM is ended alone (elsewhere the process group ties them together)
    anchors: dict = {}
    look_at = 0.0
    interrupted = False
    try:
        with open(console_log, "w", encoding="utf-8", errors="replace") as logfh:
            for line in proc.stdout:  # type: ignore[union-attr]
                logfh.write(line)
                if not args.quiet:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                if WINDOWS and len(anchors) < 2 and time.time() >= look_at:
                    look_at = time.time() + 10      # the JVM is what prints
                    try:
                        anchors = fragpipe_pids(list_processes(), proc.pid)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    if len(anchors) > 1:
                        run["fragpipe_pids"] = sorted(anchors)
                        status.write(payload(run), force=True)
                event = prog.feed(line)
                if event:
                    _report(event, prog, quiet=args.quiet)
                    status.write(payload(run), force=True)
                else:
                    status.write(payload(run))
                logfh.flush()
    except KeyboardInterrupt:
        interrupted = True
        print("\n[fragskills] interrupted - terminating FragPipe and children",
              file=sys.stderr, flush=True)
        kill_tree(proc)
    finally:
        try:
            proc.wait(timeout=120)
        except Exception:
            kill_tree(proc)

    child_rc = proc.returncode
    stopped = os.path.isfile(stop_path) and prog.state != STATE_SUCCEEDED
    try:
        os.remove(stop_path)
    except OSError:
        pass
    left: List[dict] = []
    if not (stopped or interrupted or prog.state == STATE_SUCCEEDED):
        try:            # FragPipe was ended alone (Task Manager, the OOM killer)?
            left = tools_left(list_processes(), proc.pid, anchors)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    if stopped:
        prog.state = STATE_FAILED
        prog.error = STOPPED_ERROR
    elif interrupted:
        prog.state = STATE_FAILED
        prog.error = "run was interrupted by the user"
    elif prog.state == STATE_SUCCEEDED:
        pass
    elif prog.state == STATE_FAILED:
        pass
    else:
        # No terminal marker seen. FragPipe died early (bad workflow, missing
        # tools, OOM kill). Its exit code is 0 on the normal path, so a missing
        # "ALL JOBS DONE" is the real signal.
        prog.state = STATE_FAILED
        prog.error = ("FragPipe exited (code %s) without printing 'ALL JOBS DONE'. "
                      "Check the tail of %s for the cause."
                      % (child_rc, console_log))

    if left:
        them = "it" if len(left) == 1 else "them"
        prog.error = ("%s; %d tool%s it started %s still running (PID %s): show %s with "
                      "fragpipe_stop.py --workdir %s, and stop %s with --yes once the user "
                      "confirms"
                      % (prog.error, len(left), "" if len(left) == 1 else "s",
                         "is" if len(left) == 1 else "are",
                         ", ".join(str(p["pid"]) for p in left), them, workdir, them))
    extra = dict(run, child_exit_code=child_rc)
    if left:
        extra["tools_left"] = left
    if stopped:
        extra["stopped"] = True
    if prog.state == STATE_SUCCEEDED and not args.dry_run:
        extra["outputs"] = collect_outputs(workdir)
    status.write(payload(extra), force=True)
    own = os.path.join(workdir, STATUS_NAME)
    if left and os.path.normcase(os.path.abspath(status_path)) != os.path.normcase(own):
        StatusWriter(own).write(payload(extra), force=True)     # where fragpipe_stop.py looks

    print("\n[fragskills] %s" % prog.describe(), flush=True)
    print("[fragskills] status: %s" % status_path, flush=True)
    if stopped:
        return 1
    if prog.state == STATE_FAILED:
        print("[fragskills] last lines of output:", file=sys.stderr)
        for line in prog.recent[-15:]:
            print("    %s" % line, file=sys.stderr)
        return 1
    return 0


def _report(event: dict, prog: RunProgress, quiet: bool) -> None:
    """Print a compact progress line for notable events."""
    etype = event.get("type")
    if etype == "plan":
        print("[fragskills] plan: %d task(s)" % (event.get("total_tasks") or 0),
              flush=True)
    elif etype == "task_started":
        pct = event.get("percent")
        print("[fragskills] [%5.1f%%] start  %s"
              % (pct if pct is not None else 0.0, event["task"]), flush=True)
    elif etype == "task_finished":
        pct = event.get("percent")
        mark = "ok " if event.get("exit_code") == 0 else "FAIL"
        print("[fragskills] [%5.1f%%] %s %s (%s/%s)"
              % (pct if pct is not None else 0.0, mark, event["task"],
                 event.get("finished"), event.get("total")), flush=True)
    elif etype == "milestone" and quiet:
        print("[fragskills]         %s" % event["message"], flush=True)
    elif etype == "warning":
        print("[fragskills] WARNING: %s" % event["message"], flush=True)
    elif etype in ("failed", "cancelled"):
        print("[fragskills] %s" % (event.get("message") or etype), flush=True)
    elif etype == "done":
        print("[fragskills] ALL JOBS DONE in %s min" % event.get("total_minutes"),
              flush=True)


if __name__ == "__main__":
    sys.exit(main())
