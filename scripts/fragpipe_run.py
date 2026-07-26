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
    2  bad arguments or missing input files
    3  the FragPipe folder was guessed -- show it to the user and confirm
    4  no FragPipe installation found -- ask the user for the folder
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fragpipe_progress import (  # noqa: E402
    RunProgress, STATE_FAILED, STATE_SUCCEEDED,
)
from check_fragpipe import EXPLICIT_SOURCES, is_explicit, locate, render  # noqa: E402

STATUS_NAME = "fragskills_status.json"
CONSOLE_LOG_NAME = "fragskills_run.log"

WINDOWS = os.name == "nt"

#: Exit codes. 0 = FragPipe succeeded, 1 = it failed.
EXIT_CONFIG_ERROR = 2       # bad arguments / missing input files
EXIT_NEEDS_CONFIRMATION = 3  # FragPipe folder was guessed and not confirmed
EXIT_NO_FRAGPIPE = 4         # no installation found -- ask the user


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


def preflight(args, fragpipe_bin: str) -> List[str]:
    """Return a list of fatal problems (empty means good to go)."""
    problems = []
    for label, path in (("--workflow", args.workflow), ("--manifest", args.manifest)):
        if not os.path.isfile(path):
            problems.append("%s file not found: %s" % (label, path))
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

    problems = preflight(args, fragpipe_bin)
    if problems:
        for p in problems:
            print("[fragskills] ERROR: %s" % p, file=sys.stderr)
        return EXIT_CONFIG_ERROR

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    status_path = args.status or os.path.join(workdir, STATUS_NAME)
    console_log = os.path.join(workdir, CONSOLE_LOG_NAME)

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

    interrupted = False
    try:
        with open(console_log, "w", encoding="utf-8", errors="replace") as logfh:
            for line in proc.stdout:  # type: ignore[union-attr]
                logfh.write(line)
                if not args.quiet:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                event = prog.feed(line)
                if event:
                    _report(event, prog, quiet=args.quiet)
                    status.write(payload({"pid": proc.pid}), force=True)
                else:
                    status.write(payload({"pid": proc.pid}))
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

    if interrupted:
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

    extra = {"pid": proc.pid, "child_exit_code": child_rc}
    if prog.state == STATE_SUCCEEDED and not args.dry_run:
        extra["outputs"] = collect_outputs(workdir)
    status.write(payload(extra), force=True)

    print("\n[fragskills] %s" % prog.describe(), flush=True)
    print("[fragskills] status: %s" % status_path, flush=True)
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
