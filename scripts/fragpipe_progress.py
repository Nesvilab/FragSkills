"""Parse FragPipe headless stdout into structured progress.

FragPipe prints a machine-friendly plan block up front::

    29 commands to execute:
    CheckCentroid
    <command line>
    WorkspaceCleanInit [Work dir: ...]
    <command line>
    ...
    ~~~~~~~~~~~~~~~~~~~~~~

then, during execution, announces each task the same way (name line followed by
the command line) and emits ``Process '<name>' finished, exit code: <n>`` when it
completes.  Progress is therefore ``finished / total`` where ``total`` comes from
the plan header.

The run ends with either::

    ====...====ALL JOBS DONE IN 10.1 MINUTES====...====      (success)
    Process returned non-zero exit code, stopping             (failure)

IMPORTANT: headless FragPipe calls ``System.exit(0)`` on both paths, so the
process exit code must NOT be used to decide success.  Use the markers above.

This module is pure and dependency-free so it can be reused by the runner, the
status poller, and by post-hoc analysis of a saved ``log_*.txt``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

__all__ = ["RunProgress", "parse_log", "STATE_PENDING", "STATE_RUNNING",
           "STATE_SUCCEEDED", "STATE_FAILED"]

STATE_PENDING = "pending"      # process started, plan not printed yet
STATE_RUNNING = "running"      # plan known, tasks executing
STATE_SUCCEEDED = "succeeded"  # saw ALL JOBS DONE
STATE_FAILED = "failed"        # saw a fatal non-zero exit / cancellation

# --- markers -----------------------------------------------------------------
RE_PLAN_HEADER = re.compile(r"^(\d+) commands to execute:\s*$")
RE_TILDES = re.compile(r"^~{4,}\s*$")
RE_WORKDIR_SUFFIX = re.compile(r"\s*\[Work dir: .*\]\s*$")
RE_FINISHED = re.compile(r"^Process '(.+)' finished, exit code: (-?\d+)\s*$")
RE_FATAL = re.compile(r"^Process returned non-zero exit code, stopping\s*$")
RE_NONFATAL = re.compile(
    r"^WARNING: Process '(.+)' returned non-zero exit code \((-?\d+)\), "
    r"but is non-fatal\. Continuing\.\s*$")
RE_CANCELLING = re.compile(r"^Cancelling (\d+) remaining tasks\s*$")
RE_ALL_DONE = re.compile(r"^=+ALL JOBS DONE IN ([\d.]+) MINUTES=+\s*$")
RE_RUNTIMES_HEADER = re.compile(r"^Task Runtimes:\s*$")
RE_RUNTIME_ROW = re.compile(r"^  (.+): ([\d.]+) minutes\s*$")
RE_VERSION = re.compile(r"^FragPipe version (.+?)\s*$")
RE_TOOL_VERSION = re.compile(r"^([A-Za-z0-9_\-+ ]+) version (.+?)\s*$")
RE_LCMS_FILE = re.compile(r"^  - (.+?)\t(\S+)\s*$")
# MSFragger / IonQuant style banners: ****FIRST SEARCH DONE IN 0.830 MIN****
RE_BANNER = re.compile(r"^\*{3,}(.+?)\*{3,}\s*$")
# Lines that are never a task-start announcement.
RE_NOT_A_TASK = re.compile(r"^(\s|#|>)|=|\t")

# Tool banners printed in the version block; used to skip false "task" matches.
_VERSION_BLOCK_END = "commands to execute:"


def _strip_workdir(line: str) -> str:
    return RE_WORKDIR_SUFFIX.sub("", line).strip()


class RunProgress:
    """Incremental state machine. Feed it one stdout line at a time.

    ``feed()`` returns an event dict when something worth reporting happened
    (``None`` otherwise), so a caller can print sparse progress updates instead
    of echoing every line of a very chatty log.
    """

    #: how many recent log lines to retain for the status snapshot
    RECENT_MAX = 25

    def __init__(self, recent_max: Optional[int] = None) -> None:
        self.state: str = STATE_PENDING
        self.plan: List[str] = []
        self.total_tasks: Optional[int] = None
        self.finished_tasks: int = 0
        self.current_task: Optional[str] = None
        self.task_counts: Dict[str, int] = {}
        self.failed_task: Optional[str] = None
        self.error: Optional[str] = None
        self.warnings: List[str] = []
        self.milestones: List[str] = []
        self.task_runtimes: Dict[str, float] = {}
        self.total_minutes: Optional[float] = None
        self.fragpipe_version: Optional[str] = None
        self.tool_versions: Dict[str, str] = {}
        self.input_files: List[Dict[str, str]] = []
        self.recent: List[str] = []
        self.line_count: int = 0

        self._recent_max = recent_max if recent_max is not None else self.RECENT_MAX
        self._plan_lines: List[str] = []
        self._in_plan_block = False
        self._plan_done = False
        self._exec_started = False
        self._plan_set = set()
        self._in_runtimes = False
        self._in_lcms_block = False

    # -- ingestion ------------------------------------------------------------
    def feed(self, raw_line: str) -> Optional[dict]:
        line = raw_line.rstrip("\r\n")
        self.line_count += 1
        self._remember(line)

        # Terminal states still consume lines (task runtimes come after
        # ALL JOBS DONE is printed in some layouts), but nothing can un-fail.
        event = (self._feed_plan(line)
                 or self._feed_execution(line)
                 or self._feed_epilogue(line)
                 or self._feed_metadata(line))
        return event

    def _remember(self, line: str) -> None:
        if line.strip():
            self.recent.append(line)
            if len(self.recent) > self._recent_max:
                del self.recent[0:len(self.recent) - self._recent_max]

    # -- phase 1: the plan block ---------------------------------------------
    def _feed_plan(self, line: str) -> Optional[dict]:
        if not self._plan_done:
            m = RE_PLAN_HEADER.match(line)
            if m:
                self.total_tasks = int(m.group(1))
                self._in_plan_block = True
                self._plan_lines = []
                return None
            if self._in_plan_block:
                if RE_TILDES.match(line):
                    self._finish_plan_block()
                    return {"type": "plan",
                            "total_tasks": self.total_tasks,
                            "plan": list(self.plan)}
                self._plan_lines.append(line)
                return None
        return None

    def _finish_plan_block(self) -> None:
        # Alternating name / command-line pairs.
        names = [_strip_workdir(l) for l in self._plan_lines[0::2]]
        names = [n for n in names if n]
        if self.total_tasks:
            names = names[: self.total_tasks]
        self.plan = names
        self._plan_set = set(names)
        self._in_plan_block = False
        self._plan_done = True
        self._plan_lines = []
        # The plan is printed only once FragPipe has validated its inputs and is
        # about to execute, so this is the point where "running" becomes true.
        if self.state == STATE_PENDING:
            self.state = STATE_RUNNING
        if names:
            self.current_task = names[0]

    # -- phase 2: execution ---------------------------------------------------
    def _feed_execution(self, line: str) -> Optional[dict]:
        if not self._plan_done:
            return None

        m = RE_FINISHED.match(line)
        if m:
            name, code = m.group(1), int(m.group(2))
            self._exec_started = True
            if self.state == STATE_PENDING:
                self.state = STATE_RUNNING
            self.finished_tasks += 1
            self.task_counts[name] = self.task_counts.get(name, 0) + 1
            if code != 0:
                self.failed_task = name
            # Best guess at what runs next.
            self.current_task = (self.plan[self.finished_tasks]
                                 if self.finished_tasks < len(self.plan) else None)
            return {"type": "task_finished", "task": name, "exit_code": code,
                    "finished": self.finished_tasks, "total": self.total_tasks,
                    "percent": self.percent}

        m = RE_NONFATAL.match(line)
        if m:
            msg = "non-fatal non-zero exit from '%s' (code %s)" % (m.group(1), m.group(2))
            self.warnings.append(msg)
            return {"type": "warning", "message": msg}

        if RE_FATAL.match(line):
            self.state = STATE_FAILED
            self.error = ("FragPipe stopped: a task returned a non-zero exit code"
                          + (" (%s)" % self.failed_task if self.failed_task else ""))
            return {"type": "failed", "task": self.failed_task, "message": self.error}

        m = RE_CANCELLING.match(line)
        if m:
            if self.state != STATE_SUCCEEDED:
                self.state = STATE_FAILED
                if not self.error:
                    self.error = "FragPipe cancelled %s remaining task(s)" % m.group(1)
            return {"type": "cancelled", "remaining": int(m.group(1))}

        m = RE_BANNER.match(line)
        if m and self._exec_started:
            text = m.group(1).strip()
            if text and text not in self.milestones:
                self.milestones.append(text)
                return {"type": "milestone", "message": text}
            return None

        # Task-start announcement: a bare plan name, optionally with a work dir.
        if not RE_NOT_A_TASK.search(line):
            candidate = _strip_workdir(line)
            if candidate and candidate in self._plan_set:
                self._exec_started = True
                if self.state == STATE_PENDING:
                    self.state = STATE_RUNNING
                if candidate != self.current_task:
                    self.current_task = candidate
                    return {"type": "task_started", "task": candidate,
                            "finished": self.finished_tasks,
                            "total": self.total_tasks, "percent": self.percent}
        return None

    # -- phase 3: epilogue ----------------------------------------------------
    def _feed_epilogue(self, line: str) -> Optional[dict]:
        m = RE_ALL_DONE.match(line)
        if m:
            self.total_minutes = float(m.group(1))
            self.current_task = None
            if self.state != STATE_FAILED:
                self.state = STATE_SUCCEEDED
                if self.total_tasks:
                    self.finished_tasks = max(self.finished_tasks, self.total_tasks)
            return {"type": "done", "state": self.state,
                    "total_minutes": self.total_minutes}

        if RE_RUNTIMES_HEADER.match(line):
            self._in_runtimes = True
            return None
        if self._in_runtimes:
            m = RE_RUNTIME_ROW.match(line)
            if m:
                self.task_runtimes[m.group(1)] = float(m.group(2))
                return None
            if line.strip():
                self._in_runtimes = False
        return None

    # -- metadata (versions, input files) ------------------------------------
    def _feed_metadata(self, line: str) -> Optional[dict]:
        if self._plan_done:
            return None
        m = RE_VERSION.match(line)
        if m:
            self.fragpipe_version = m.group(1)
            return None
        m = RE_TOOL_VERSION.match(line)
        if m:
            self.tool_versions[m.group(1).strip()] = m.group(2)
            return None
        if line.startswith("LCMS files:"):
            self._in_lcms_block = True
            return None
        if self._in_lcms_block:
            m = RE_LCMS_FILE.match(line)
            if m:
                self.input_files.append({"path": m.group(1), "data_type": m.group(2)})
            elif _VERSION_BLOCK_END in line:
                self._in_lcms_block = False
        return None

    # -- output ---------------------------------------------------------------
    @property
    def percent(self) -> Optional[float]:
        if not self.total_tasks:
            return None
        return round(100.0 * self.finished_tasks / self.total_tasks, 1)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "total_tasks": self.total_tasks,
            "finished_tasks": self.finished_tasks,
            "percent": self.percent,
            "current_task": self.current_task,
            "plan": list(self.plan),
            "task_counts": dict(self.task_counts),
            "task_runtimes_min": dict(self.task_runtimes),
            "total_minutes": self.total_minutes,
            "failed_task": self.failed_task,
            "error": self.error,
            "warnings": list(self.warnings),
            "milestones": list(self.milestones),
            "fragpipe_version": self.fragpipe_version,
            "tool_versions": dict(self.tool_versions),
            "input_files": list(self.input_files),
            "recent": list(self.recent),
            "log_lines": self.line_count,
        }

    def describe(self) -> str:
        """One-line human summary."""
        if self.state == STATE_SUCCEEDED:
            mins = ("%.1f min" % self.total_minutes) if self.total_minutes else "?"
            return "SUCCEEDED - %s task(s) in %s" % (self.finished_tasks, mins)
        if self.state == STATE_FAILED:
            return "FAILED - %s" % (self.error or "unknown error")
        if self.state == STATE_RUNNING and self.total_tasks:
            return "RUNNING - %s/%s (%.1f%%) current: %s" % (
                self.finished_tasks, self.total_tasks, self.percent or 0.0,
                self.current_task or "?")
        return "STARTING - FragPipe is validating inputs; no task plan yet"


def parse_log(path: str, encoding: str = "utf-8") -> RunProgress:
    """Parse a saved ``log_*.txt`` (or any captured stdout) in full."""
    prog = RunProgress()
    with open(path, "r", encoding=encoding, errors="replace") as fh:
        for line in fh:
            prog.feed(line)
    return prog


if __name__ == "__main__":
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(description="Parse a FragPipe log into progress JSON.")
    ap.add_argument("log", help="path to a FragPipe log_*.txt or captured stdout")
    ap.add_argument("--json", action="store_true", help="emit the full snapshot as JSON")
    args = ap.parse_args()

    p = parse_log(args.log)
    if args.json:
        json.dump(p.snapshot(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(p.describe())
        if p.task_runtimes:
            print("\nTask runtimes (min):")
            for k, v in sorted(p.task_runtimes.items(), key=lambda kv: -kv[1]):
                print("  %-32s %6.2f" % (k, v))
    sys.exit(0 if p.state != STATE_FAILED else 1)
