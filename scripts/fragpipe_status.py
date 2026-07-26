"""Report the progress of a FragPipe run.

Reads ``<workdir>/fragskills_status.json`` when the run was started by
``fragpipe_run.py``.  Falls back to re-parsing the newest FragPipe log in the
work directory, so it also works for runs launched some other way (the GUI, a
cluster script, ``fragpipe --headless`` invoked directly).

    python scripts/fragpipe_status.py --workdir D:/run/out
    python scripts/fragpipe_status.py --workdir D:/run/out --json
    python scripts/fragpipe_status.py --log D:/run/out/log_2026-06-09_10-46-28.txt

Exit codes: 0 = succeeded, 1 = failed, 2 = still running, 3 = nothing found.
That makes it usable directly as a wait-loop predicate.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fragpipe_progress import (  # noqa: E402
    parse_log, STATE_FAILED, STATE_RUNNING, STATE_SUCCEEDED,
)

STATUS_NAME = "fragskills_status.json"
EXIT_SUCCEEDED, EXIT_FAILED, EXIT_RUNNING, EXIT_NOT_FOUND = 0, 1, 2, 3


def newest_log(workdir: str) -> Optional[str]:
    """Newest FragPipe console log in *workdir*, preferring our own tee."""
    candidates = []
    ours = os.path.join(workdir, "fragskills_run.log")
    if os.path.isfile(ours):
        candidates.append(ours)
    candidates.extend(glob.glob(os.path.join(workdir, "log_*.txt")))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def load_status_file(workdir: str, explicit: Optional[str] = None) -> Optional[dict]:
    path = explicit or os.path.join(workdir, STATUS_NAME)
    if not os.path.isfile(path):
        return None
    for _ in range(3):  # tolerate reading mid-write
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            time.sleep(0.2)
    return None


def status_from_log(log_path: str) -> dict:
    prog = parse_log(log_path)
    snap = prog.snapshot()
    snap["summary"] = prog.describe()
    snap["console_log"] = log_path
    snap["source"] = "log"
    # A log that stops mid-run means either "still going" or "died"; mtime tells
    # us which, but only the caller knows the tolerance, so surface the age.
    snap["log_age_sec"] = round(time.time() - os.path.getmtime(log_path), 1)
    return snap


def render(snap: dict, verbose: bool) -> str:
    lines = []
    state = snap.get("state", "unknown")
    lines.append("state       : %s" % state.upper())
    lines.append("summary     : %s" % snap.get("summary", ""))
    if snap.get("workdir"):
        lines.append("workdir     : %s" % snap["workdir"])
    tot, fin = snap.get("total_tasks"), snap.get("finished_tasks")
    if tot:
        pct = snap.get("percent") or 0.0
        bar_w = 32
        filled = int(bar_w * pct / 100.0)
        lines.append("progress    : [%s%s] %5.1f%%  (%s/%s tasks)"
                     % ("#" * filled, "-" * (bar_w - filled), pct, fin, tot))
    if snap.get("current_task"):
        lines.append("current     : %s" % snap["current_task"])
    if snap.get("elapsed_sec") is not None:
        lines.append("elapsed     : %s" % _hms(snap["elapsed_sec"]))
    if snap.get("total_minutes"):
        lines.append("total time  : %.1f min" % snap["total_minutes"])
    if snap.get("log_age_sec") is not None:
        lines.append("log age     : %s (time since last output)"
                     % _hms(snap["log_age_sec"]))
    if snap.get("fragpipe_version"):
        lines.append("fragpipe    : %s" % snap["fragpipe_version"])
    if snap.get("console_log"):
        lines.append("console log : %s" % snap["console_log"])
    for w in snap.get("warnings") or []:
        lines.append("warning     : %s" % w)
    if snap.get("error"):
        lines.append("error       : %s" % snap["error"])

    if snap.get("milestones"):
        lines.append("")
        lines.append("Milestones:")
        for m in snap["milestones"][-8:]:
            lines.append("  - %s" % m)

    outputs = snap.get("outputs")
    if outputs and not outputs.get("error"):
        found = [(k, outputs[k]) for k in
                 ("experiments", "report_files", "combined_files",
                  "combined_site_files", "tmt_report", "dia_quant")
                 if outputs.get(k)]
        if found:
            lines.append("")
            lines.append("Outputs:")
            for key, val in found:
                lines.append("  %-20s %s" % (key + ":", _brief(val)))
        cls = outputs.get("classification")
        if cls and cls.get("main_files"):
            main = [f for f in cls["main_files"] if f.get("role") == "main"]
            lines.append("  %-20s %s" % ("run type:", cls.get("label", "")))
            if main:
                lines.append("  %-20s %s" % ("start with:", main[0]["name"]))
                lines.append("  %-20s %s" % ("", cls.get("main_dir") or ""))
        for note in outputs.get("notes") or []:
            lines.append("")
            lines.append("NOTE: %s" % note)

    if verbose:
        if snap.get("task_runtimes_min"):
            lines.append("")
            lines.append("Task runtimes (min):")
            for k, v in sorted(snap["task_runtimes_min"].items(),
                               key=lambda kv: -kv[1]):
                lines.append("  %-34s %6.2f" % (k, v))
        if snap.get("recent"):
            lines.append("")
            lines.append("Recent output:")
            for line in snap["recent"][-15:]:
                lines.append("  | %s" % line)
    elif state == STATE_FAILED and snap.get("recent"):
        lines.append("")
        lines.append("Last output before failure:")
        for line in snap["recent"][-12:]:
            lines.append("  | %s" % line)
    return "\n".join(lines)


def _brief(val) -> str:
    if isinstance(val, dict):
        return "%d entr%s" % (len(val), "y" if len(val) == 1 else "ies")
    if isinstance(val, list):
        return "%d item%s" % (len(val), "" if len(val) == 1 else "s")
    return str(val)


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%dh %02dm %02ds" % (h, m, s)
    if m:
        return "%dm %02ds" % (m, s)
    return "%ds" % s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report FragPipe run progress.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--workdir", help="FragPipe output directory")
    src.add_argument("--log", help="parse a specific log file instead")
    ap.add_argument("--status-file", help="explicit status JSON path")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="include task runtimes and recent output")
    args = ap.parse_args(argv)

    if args.log:
        if not os.path.isfile(args.log):
            print("log not found: %s" % args.log, file=sys.stderr)
            return EXIT_NOT_FOUND
        snap = status_from_log(args.log)
    else:
        workdir = os.path.abspath(args.workdir)
        snap = load_status_file(workdir, args.status_file)
        if snap is not None:
            snap.setdefault("source", "status-file")
        else:
            log = newest_log(workdir)
            if log is None:
                print("No status file and no FragPipe log under %s - the run has "
                      "not produced output yet." % workdir, file=sys.stderr)
                return EXIT_NOT_FOUND
            snap = status_from_log(log)

    if args.json:
        json.dump(snap, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render(snap, args.verbose))

    state = snap.get("state")
    if state == STATE_SUCCEEDED:
        return EXIT_SUCCEEDED
    if state == STATE_FAILED:
        return EXIT_FAILED
    return EXIT_RUNNING


if __name__ == "__main__":
    sys.exit(main())
