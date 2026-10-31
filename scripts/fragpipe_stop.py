"""Stop a FragPipe run and every tool it started.

    python scripts/fragpipe_stop.py                             # the FragPipe runs going now
    python scripts/fragpipe_stop.py --workdir D:/run/out        # what would be stopped
    python scripts/fragpipe_stop.py --workdir D:/run/out --yes  # stop it
    python scripts/fragpipe_stop.py --pid 1234 --yes            # a run listed with --pid

FragPipe runs MSFragger, IonQuant, DIA-NN and the other tools as child
processes, so ending FragPipe alone, or the fragpipe_run.py watcher, can leave
them running for hours.  This ends the whole tree: ``taskkill /T /F`` on
Windows; elsewhere SIGTERM, then SIGKILL for whatever is left after a grace
period.

A run is found by its program, never by a PID alone: FragPipe's launcher
(``cmd.exe /c ...\\fragpipe.bat``, ``sh .../bin/fragpipe``) or its JVM
(``java ... FragPipeMain``), with ``--headless`` and this ``--workdir`` among
its own arguments.  A wrapper that only mentions FragPipe on its command line,
or a PID the system has since given to another program, is never touched.
The watcher then records the run as stopped by the user; when no watcher is
left to do it, this script writes the status file itself.  When FragPipe
itself was ended alone, the watcher records the tools it left running
(``tools_left``), and this script stops those.

Exit codes: 0 stopped (or, with no run named, listed), 1 could not stop it, 2
bad arguments, 3 found a run but --yes was not given -- show it to the user
and confirm, 4 nothing is running there.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

STATUS_NAME = "fragskills_status.json"
STOP_NAME = "fragskills_stop.json"      # written before the kill, read by fragpipe_run.py
LOCK_NAME = "fragskills_run.lock"       # held by a fragpipe_run.py watcher while it runs
STOPPED_ERROR = "stopped by the user"
WINDOWS = os.name == "nt"

EXIT_STOPPED, EXIT_NOT_STOPPED, EXIT_BAD_ARGS, EXIT_CONFIRM, EXIT_NOTHING = 0, 1, 2, 3, 4

TERMINAL_STATES = ("succeeded", "failed")
GRACE_SEC = 15          # for the tools to exit after SIGTERM, or for taskkill to finish
WATCHER_SEC = 30        # for fragpipe_run.py to write its final status
START_SEC = 10          # for FragPipe to appear once its watcher has started

LAUNCHERS = ("fragpipe", "fragpipe.bat", "fragpipe.exe")
SHELLS = ("sh", "bash", "dash", "zsh", "ksh")
JAVA = ("java", "java.exe", "javaw", "javaw.exe")
CMD = ("cmd", "cmd.exe")
# MSFragger's scratch files next to each input; a finished run removes them
SCRATCH_EXTS = (".mzBIN_calibrated", ".ms1bin")

# An option's value: quoted (Windows), or up to the next option or the end
# (ps joins argv with spaces, so a path with spaces is not quoted there).
_OPTION = r'%s[ =]+(?:"([^"]+)"|(.+?))(?=\s+--|\s*"|\s*$)'
# What cmd.exe runs: `/c C:\FP\bin\fragpipe.bat ...`, or, when the path has a
# space, `/c ""C:\Program Files\FP\bin\fragpipe.bat" ..."`.  Not /k: that
# console stays open as a prompt after the run, with the same command line.
_CMD_TARGET = re.compile(r'\s/c\s+(?:"+([^"]+)"|([^\s"]+))', re.I)


def _run(cmd: List[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                       timeout=120)
    if r.returncode != 0:
        raise OSError("%s failed: %s" % (cmd[0], (r.stderr or "").strip()[:300]))
    return r.stdout


def _from_proc() -> Optional[List[Dict]]:
    """Linux: every process from /proc, which needs no ps (slim images lack it)."""
    if not os.path.isfile("/proc/self/stat"):
        return None
    procs = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as fh:
                stat = fh.read().decode("utf-8", "replace")
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                argv = fh.read().decode("utf-8", "replace").rstrip("\0")
        except OSError:                     # it ended while being read
            continue
        head, _, rest = stat.rpartition(")")    # the name in (...) may hold spaces
        fields = rest.split()
        if len(fields) < 3 or not fields[1].isdigit() or not fields[2].isdigit():
            continue
        cmd = argv.replace("\0", " ") or "[%s]" % head.partition("(")[2]
        if fields[0] == "Z":                # as ps shows a zombie
            cmd += " <defunct>"
        procs.append({"pid": int(name), "ppid": int(fields[1]), "pgid": int(fields[2]),
                      "cmd": cmd, "created": None})
    return procs


def list_processes() -> List[Dict]:
    """Every process as {pid, ppid, cmd, created, ...}.  Raises OSError when it cannot
    be read.

    *created* is set on Windows only, where a process keeps its parent's PID
    after that parent exits and the PID can go to a newer process; a parent
    must be older than its child.  Elsewhere orphans are reparented, and *pgid*
    (the process group) ties them to the run instead.
    """
    if WINDOWS:
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId, ParentProcessId, CommandLine, Name, SessionId, "
                  "@{n='Created';e={if ($_.CreationDate) {$_.CreationDate.ToFileTimeUtc()}}} | "
                  "ConvertTo-Json -Compress")
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
        rows = json.loads(out) if out.strip() else []
        rows = [rows] if isinstance(rows, dict) else rows
        return [{"pid": r["ProcessId"], "ppid": r["ParentProcessId"],
                 "cmd": r.get("CommandLine") or "", "created": r.get("Created"),
                 "name": r.get("Name") or "", "session": r.get("SessionId")} for r in rows]
    procs = _from_proc()
    if procs is not None:
        return procs
    procs = []
    for line in _run(["ps", "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "pgid=",
                      "-o", "args="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3 and all(x.isdigit() for x in parts[:3]):
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]),
                          "cmd": parts[3] if len(parts) == 4 else "", "created": None})
    return procs


def option_of(cmd: str, name: str) -> Optional[str]:
    m = re.search(_OPTION % re.escape(name), cmd)
    return (m.group(1) or m.group(2)).strip() if m else None


def workdir_of(cmd: str) -> Optional[str]:
    return option_of(cmd, "--workdir")


def _base(token: str) -> str:
    return re.split(r"[\\/]", token)[-1].lower()


def _program(toks: List[str]) -> List[str]:
    """*toks* with argv[0] whole: ps joins argv with spaces, so `/opt/My JDK/bin/java`
    arrives split.  Only a join that is an existing executable counts."""
    if _base(toks[0]) in LAUNCHERS + SHELLS + JAVA + CMD:
        return toks
    for i in range(2, len(toks) + 1):
        if toks[i - 1].startswith("-"):
            break
        path = " ".join(toks[:i])
        if (_base(path) in LAUNCHERS + JAVA and os.path.isfile(path)
                and os.access(path, os.X_OK)):
            return [path] + toks[i:]
    return toks


def is_fragpipe(cmd: str) -> bool:
    """FragPipe's launcher or JVM in headless mode, not a program that names it."""
    toks = [t for t in (t.strip('"') for t in re.findall(r'"[^"]*"|\S+', cmd)) if t]
    if len(toks) < 2 or "--headless" not in cmd.replace('"', " ").split()[1:]:
        return False
    toks = _program(toks)
    prog = _base(toks[0])
    if prog in CMD:                         # how Windows runs fragpipe.bat
        m = _CMD_TARGET.search(cmd)
        target = (m.group(1) or m.group(2)).strip() if m else ""
        return bool(target) and (_base(target) in LAUNCHERS     # cmd /c "fragpipe.bat ..."
                                 or _base(target.split()[0]) in LAUNCHERS)
    if prog in LAUNCHERS:
        return True
    if prog in SHELLS:                      # sh /opt/fragpipe/bin/fragpipe ...
        return len(toks) > 1 and _base(toks[1]) in LAUNCHERS
    if prog in JAVA:                        # org.nesvilab (or, before 23, com.dmtavt)
        return any(t.endswith(".FragPipeMain") or t == "FragPipeMain" for t in toks)
    return False


def _norm(path: str) -> str:
    p = os.path.normpath(os.path.abspath(path))
    return p.lower() if WINDOWS else p


def _cwd_of(pid: int) -> Optional[str]:
    """A process's working directory: Linux only (/proc), None elsewhere."""
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except (OSError, AttributeError):
        return None


def run_processes(procs: List[Dict], workdir: Optional[str] = None) -> List[Dict]:
    """FragPipe headless processes (the launcher and its JVM), of *workdir* when given.

    A relative --workdir names a folder only from the process's own working
    directory.  Linux shows it; Windows and macOS do not, so there such a run
    never matches a workdir and is stopped with --pid instead.
    """
    hits = []
    for p in procs:
        if not is_fragpipe(p["cmd"]):
            continue
        given = wd = workdir_of(p["cmd"])
        if wd and not os.path.isabs(wd) and _cwd_of(p["pid"]):
            wd = os.path.normpath(os.path.join(_cwd_of(p["pid"]), wd))
        if workdir is not None and not (wd and os.path.isabs(wd) and _same_dir(wd, workdir)):
            continue
        hits.append(dict(p, workdir=wd, given=given))
    return hits


def _same_dir(a: str, b: str) -> bool:
    """The same folder, also when one is named through a symlink (Linux /proc gives
    the physical path)."""
    if _norm(a) == _norm(b):
        return True
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _child_of(child: Dict, parent: Dict) -> bool:
    """Whether *parent* really started *child*, not a newer owner of its parent's PID."""
    a, b = child.get("created"), parent.get("created")
    return a is None or b is None or a >= b


def roots(hits: List[Dict]) -> List[Dict]:
    """The top FragPipe process of each run: its parent is not FragPipe for that workdir."""
    by_pid = {h["pid"]: h for h in hits}
    out = []
    for h in hits:
        parent = by_pid.get(h["ppid"])
        if (parent is None or parent["pid"] == h["pid"] or not _child_of(h, parent)
                or parent.get("workdir") != h.get("workdir")):
            out.append(h)
    return out


def tree(procs: List[Dict], root: int) -> List[int]:
    """*root* and all its descendants."""
    by_pid = {p["pid"]: p for p in procs}
    children: Dict[int, List[int]] = {}
    for p in procs:
        parent = by_pid.get(p["ppid"])
        if parent is not None and p["pid"] != p["ppid"] and _child_of(p, parent):
            children.setdefault(p["ppid"], []).append(p["pid"])
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid not in out:
            out.append(pid)
            todo.extend(children.get(pid, []))
    return out


def snapshot(procs: List[Dict], pids: List[int]) -> Dict[int, tuple]:
    """{pid: (cmd, created)} of the live processes among *pids*, to check later
    that each has ended, and not mistake a newer owner of its PID for it."""
    by_pid = {p["pid"]: p for p in procs}
    return {pid: (by_pid[pid]["cmd"], by_pid[pid].get("created")) for pid in pids
            if pid in by_pid and not by_pid[pid]["cmd"].endswith("<defunct>")}


def _alive(procs: List[Dict], before: Dict[int, tuple]) -> List[int]:
    """PIDs of *before* still running: the same command line, started at the same time."""
    now = {p["pid"]: (p["cmd"], p.get("created")) for p in procs}
    return sorted(pid for pid, key in before.items() if now.get(pid) == key)


def _signal(procs: List[Dict], root: int, sig: int) -> None:
    try:
        if os.getpgid(root) == root:        # its own group, as fragpipe_run.py starts it
            os.killpg(root, sig)
    except OSError:
        pass
    for pid in tree(procs, root):           # and anything that left the group
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def kill(procs: List[Dict], hits: List[Dict]) -> List[int]:
    """End every tree under *hits*; return the PIDs still alive afterwards."""
    targets, before, refused = [], {}, []
    for r in roots(hits):
        pids = tree(procs, r["pid"])
        if os.getpid() in pids:
            print("Not stopping PID %d: this script runs under it." % r["pid"],
                  file=sys.stderr)
            refused.append(r["pid"])
            continue
        targets.append(r["pid"])
        before.update(snapshot(procs, pids))
    for pid in targets:
        if WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=120)
        else:
            _signal(procs, pid, signal.SIGTERM)
    alive: List[int] = []
    deadline = time.time() + GRACE_SEC
    while before:
        alive = _alive(list_processes(), before)
        if not alive or time.time() > deadline:
            break
        time.sleep(0.5)
    if alive and not WINDOWS:
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(1)
        alive = _alive(list_processes(), before)
    return sorted(set(alive) | set(refused))


def _ancestors(procs: List[Dict], pid: int) -> List[int]:
    by_pid = {p["pid"]: p for p in procs}
    out = []
    while pid in by_pid and pid not in out:
        out.append(pid)
        parent = by_pid.get(by_pid[pid]["ppid"])
        if parent is None or not _child_of(by_pid[pid], parent):
            break
        pid = parent["pid"]
    return out


def naming(procs: List[Dict], workdir: str) -> List[Dict]:
    """Processes whose command line names *workdir*: after FragPipe itself has been
    ended alone, its tools (MSFragger's fragger.params is in the workdir) are left
    with nothing that ties them to the run.  An editor or a shell can name it too,
    so these are only shown, never stopped."""
    mine = set(_ancestors(procs, os.getpid()))
    key = re.compile(re.escape(_norm(workdir)) + r'(?=[\\/"\s]|$)')
    out = []
    for p in procs:
        cmd = p["cmd"].lower() if WINDOWS else p["cmd"]
        if (key.search(cmd.replace("/", os.sep)) and p["pid"] not in mine
                and not re.search(r"fragpipe_(run|stop|status)\.py", cmd)):
            out.append(p)
    return out


def hidden_java(procs: List[Dict]) -> List[Dict]:
    """Windows: Java processes in this session whose command line cannot be read,
    which is how a run started as administrator looks from a normal prompt."""
    if not WINDOWS:
        return []
    mine = [p.get("session") for p in procs if p["pid"] == os.getpid()]
    return [p for p in procs if not p["cmd"] and mine and p.get("session") == mine[0]
            and (p.get("name") or "").lower() in ("java.exe", "javaw.exe", "fragpipe.exe")]


def fragpipe_pids(procs: List[Dict], launcher: int) -> Dict[int, Optional[int]]:
    """Windows: {pid: created} of FragPipe's own processes, the launcher
    (cmd.exe running fragpipe.bat) and the JVM it starts, taken once FragPipe
    prints.  The tools are the JVM's children, never the launcher's, so they are
    told apart however early they start."""
    by_pid = {p["pid"]: p for p in procs}
    top = by_pid.get(launcher)
    if top is None:
        return {}
    out = {launcher: top.get("created")}
    for p in procs:
        if p["ppid"] == launcher and p["pid"] != launcher and _child_of(p, top) \
                and (p.get("name") or "").lower() != "conhost.exe":
            out[p["pid"]] = p.get("created")
    return out


def tools_left(procs: List[Dict], launcher: int,
               anchors: Dict[int, Optional[int]]) -> List[Dict]:
    """Processes a run started that outlived FragPipe, as fragpipe_run.py finds
    them once FragPipe has exited.  POSIX: the launcher's process group, which
    fragpipe_run.py gave it and every tool inherits.  Windows: the children of
    FragPipe's processes in *anchors* (a dead parent's PID stays their parent
    PID), started after them, and those children's own descendants."""
    if not WINDOWS:
        left = [p for p in procs if p.get("pgid") == launcher and p["pid"] != os.getpid()]
    else:
        by_pid = {p["pid"]: p for p in procs}

        def orphan(p: Dict) -> bool:
            if p["ppid"] not in anchors or p["pid"] in anchors \
                    or not _child_of(p, {"created": anchors[p["ppid"]]}):
                return False
            owner = by_pid.get(p["ppid"])     # the PID may have gone to a newer process
            return owner is None or owner.get("created") == anchors[p["ppid"]] \
                or not _child_of(p, owner)
        keep = {pid for p in procs if orphan(p) for pid in tree(procs, p["pid"])}
        # each console tool has its own conhost.exe, which ends with the tool
        left = [p for p in procs if p["pid"] in keep
                and (p.get("name") or "").lower() != "conhost.exe"]
    return [{"pid": p["pid"], "cmd": p["cmd"], "created": p.get("created")} for p in left
            if not p["cmd"].endswith("<defunct>")]


def tools_still_running(procs: List[Dict], status: Optional[dict]) -> List[Dict]:
    """The status's tools_left that are still running: the same PID, command line
    and start time, so a PID the system has since reused never counts.  An entry
    whose command line cannot be read here is marked *hidden*."""
    status = status or {}
    if status.get("host") not in (None, socket.gethostname()):
        return []                           # another computer's PIDs mean nothing here
    want = {t["pid"]: (t.get("cmd", ""), t.get("created")) for t in status.get("tools_left") or []
            if isinstance(t, dict) and isinstance(t.get("pid"), int)}
    out = []
    for p in procs:
        cmd, created = want.get(p["pid"], (None, None))
        if cmd is None:
            continue
        if (p["cmd"], p.get("created")) == (cmd, created):
            out.append(p)
        elif not p["cmd"] and created is not None and p.get("created") == created:
            # run as administrator, its command line is empty from a normal prompt;
            # on Windows its PID and start time alone still name it
            out.append(dict(p, cmd=cmd, hidden=True))
    return out


def local_watcher(procs: List[Dict], workdir: str, status: Optional[dict]) -> Optional[Dict]:
    """This computer's fragpipe_run.py for *workdir*: the status's watcher_pid, or a
    process running fragpipe_run.py with that --workdir.  Its command line is
    empty when it runs as administrator and this script does not."""
    status = status or {}
    wp = status.get("watcher_pid") if status.get("host") in (None, socket.gethostname()) \
        else None                           # another computer's PID means nothing here
    for p in procs:
        if p["pid"] == wp and ("fragpipe_run" in p["cmd"] or not p["cmd"]
                               and "python" in (p.get("name") or "").lower()):
            return p
    for p in procs:
        wd = workdir_of(p["cmd"]) if re.search(r"fragpipe_run(\.py)?\b", p["cmd"]) else None
        if wd and (_same_dir(wd, workdir) if os.path.isabs(wd) else _tail_of(workdir, wd)):
            return p
    return None


# --- the watcher's lock -------------------------------------------------------

def lock_workdir(workdir: str):
    """Take <workdir>/fragskills_run.lock, which fragpipe_run.py holds for its whole
    life, so two runs started together cannot both pass its check.  Returns the open
    file (keep it to hold the lock; the system releases it when the process ends,
    however it ends), or None when another process holds it.  Raises OSError when
    the folder cannot be locked at all, as on NFS without lockd or Lustre mounted
    noflock."""
    fh = open(os.path.join(workdir, LOCK_NAME), "a+")
    try:
        if WINDOWS:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        fh.close()
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN) \
                or WINDOWS and exc.errno == errno.EDEADLOCK:
            return None
        raise
    return fh


def watcher_holds(workdir: str) -> bool:
    """Whether a fragpipe_run.py watcher is alive for *workdir*, whatever has
    happened to the FragPipe processes it started."""
    if not os.path.isfile(os.path.join(workdir, LOCK_NAME)):
        return False
    try:
        fh = lock_workdir(workdir)
    except OSError:
        return False
    if fh is None:
        return True
    fh.close()
    return False


# --- the status file ----------------------------------------------------------

def load_status(workdir: str) -> Optional[dict]:
    try:
        with open(os.path.join(workdir, STATUS_NAME), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _elapsed(started_at: Optional[str]) -> Optional[float]:
    try:
        return round((datetime.now(timezone.utc)
                      - datetime.fromisoformat(started_at)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def _same_run(a: Optional[dict], b: Optional[dict]) -> bool:
    return bool(a and b) and all(a.get(k) == b.get(k) for k in ("watcher_pid", "started_at"))


def finish_status(workdir: str, before: Optional[dict], watcher: bool,
                  root: int, held: bool = False) -> str:
    """Record the stop.  *before* is the run's status as read before the kill (None
    when the folder held none of this run's); *held* means a watcher holds the
    workdir's lock.  Returns what happened:

      "watcher"  the watcher recorded the stop
      "ended"    the run had ended on its own first, and its status says how
      "new run"  another run has started here since; its status is left alone
      "pending"  the watcher has not finished yet; it will record the stop
      "written"  this script wrote the status
    """
    path = os.path.join(workdir, STATUS_NAME)
    deadline = time.time() + (WATCHER_SEC if watcher or held else 0)

    def verdict(status: Optional[dict]) -> Optional[str]:
        if before is None or status is None:
            return None
        if not _same_run(status, before):
            return "new run"
        if status.get("stopped"):
            return "watcher"
        if "child_exit_code" in status:         # the watcher's last write, made before
            return "ended"                      # it could see the stop request
        return None

    while True:
        status = load_status(workdir)
        if verdict(status):
            return verdict(status)
        if held and not watcher_holds(workdir):     # the watcher has exited
            if before is None:
                return "watcher"                # it wrote its status, wherever --status put it
            status = load_status(workdir)       # its last write may have just landed
            if verdict(status):
                return verdict(status)
            break                               # it exited without one: write it here
        if time.time() > deadline:
            break
        time.sleep(0.5)
    if held and watcher_holds(workdir):
        return "pending"                        # never write over a live watcher's status
    if before is not None and status is not None:
        status["elapsed_sec"] = _elapsed(status.get("started_at"))
    else:                                   # none of this run's, or an earlier run's
        status = {"schema": 1, "workdir": workdir, "pid": root, "source": "fragpipe_stop.py"}
    if status.get("state") == "failed" and status.get("error"):
        status.update(stopped=True, updated_at=_now())  # it had already failed: keep why
    else:
        status.update(state="failed", error=STOPPED_ERROR, stopped=True, updated_at=_now(),
                      summary="FAILED - %s" % STOPPED_ERROR)
    try:
        _write_json(path, status)
    except OSError as exc:
        print("warning: could not write %s: %s" % (path, exc), file=sys.stderr)
    return "written"


def leftovers(cmd: str, workdir: str, since: float) -> List[str]:
    """MSFragger scratch files next to the run's inputs, written since *since*."""
    manifest = option_of(cmd, "--manifest")
    found = []
    try:
        with open(manifest or "", "r", encoding="utf-8", errors="replace") as fh:
            rows = [line.split("\t")[0].strip() for line in fh if line.strip()]
    except OSError:
        return found
    for row in rows:
        stem = os.path.splitext(os.path.join(workdir, row))[0]
        for ext in SCRATCH_EXTS:
            try:
                if os.path.getmtime(stem + ext) >= since:
                    found.append(stem + ext)
            except OSError:
                pass
    return found


def _started(status: Optional[dict]) -> float:
    try:
        return datetime.fromisoformat(status["started_at"]).timestamp()
    except (TypeError, KeyError, ValueError):
        return 0.0


def _tail_of(workdir: str, rel: str) -> bool:
    """Whether the relative path *rel* could name *workdir*: it ends with it."""
    parts = [x for x in os.path.normpath(rel).split(os.sep) if x not in ("", ".", "..")]
    tail = os.sep.join(parts)
    w = _norm(workdir)
    tail = tail.lower() if WINDOWS else tail
    return bool(tail) and (w == tail or w.endswith(os.sep + tail))


def describe(where: str, status: Optional[dict], procs: List[Dict],
             hits: List[Dict], held: bool = False) -> str:
    n = len({pid for r in roots(hits) for pid in tree(procs, r["pid"])})
    lines = ["FragPipe run %s" % where,
             "  processes : %d (FragPipe and the tools it started; PID %s)"
             % (n, ", ".join(str(r["pid"]) for r in roots(hits)))]
    if status:
        lines.append("  status    : %s" % (status.get("summary") or status.get("state")))
        for key, label in (("current_task", "current"), ("started_at", "started"),
                           ("fragpipe_dir", "fragpipe")):
            if status.get(key):
                lines.append("  %-9s : %s" % (label, status[key]))
    elif held:
        lines.append("  status    : its watcher (fragpipe_run.py) writes the status outside "
                     "this folder (--status)")
    else:
        lines.append("  status    : no status file from this run (started outside "
                     "fragpipe_run.py)")
    return "\n".join(lines)


def _stop_tools(procs: List[Dict], workdir: str, status: dict, tools: List[Dict],
                yes: bool) -> int:
    """FragPipe has ended, but tools it started, which its watcher recorded, run on."""
    print("FragPipe has ended in %s (%s), but tools it started are still running:"
          % (workdir, status.get("summary") or status.get("state")))
    for p in tools:
        print("  PID %-7d %s" % (p["pid"], p["cmd"][:150]))
    if not yes:
        print("\nNot stopped. Show this to the user, and re-run with --yes once they confirm.")
        return EXIT_CONFIRM
    try:
        alive = kill(procs, [dict(p, workdir=None) for p in tools])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Could not stop them: %s" % exc, file=sys.stderr)
        return EXIT_NOT_STOPPED
    if alive:
        print("Could not stop PID %s. Stop them in Task Manager (Windows) or with kill -9 "
              "(Linux/macOS)." % ", ".join(map(str, alive)), file=sys.stderr)
        if any(p.get("hidden") for p in tools):
            print("They run as administrator: only a prompt opened with Run as administrator "
                  "can stop them.", file=sys.stderr)
        return EXIT_NOT_STOPPED
    # the watcher's error says they are still running, and how to stop them
    err = re.sub(r"; \d+ tools? it started (?:is|are) still running .*\Z", "",
                 status.get("error") or "", flags=re.S)
    err = "%s; the tools it left were %s" % (err or "FragPipe was ended", STOPPED_ERROR)
    status.update(stopped=True, tools_left=[], error=err, summary="FAILED - %s" % err,
                  updated_at=_now())
    try:
        _write_json(os.path.join(workdir, STATUS_NAME), status)
    except OSError as exc:
        print("warning: could not update the status file: %s" % exc, file=sys.stderr)
    print("\nStopped the tools FragPipe left running in %s." % workdir)
    return EXIT_STOPPED


def _stop_by_pid(procs: List[Dict], run: Dict, yes: bool) -> int:
    """A run whose workdir cannot be resolved: stop its tree, with no status file."""
    print(describe("with PID %d (--workdir %s)" % (run["pid"], run["workdir"] or "not given"),
                   None, procs, [run]))
    if not yes:
        print("\nNot stopped. Show this to the user, and re-run with --yes once they confirm.")
        return EXIT_CONFIRM
    try:
        alive = kill(procs, [run])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Could not stop the run: %s" % exc, file=sys.stderr)
        return EXIT_NOT_STOPPED
    if alive:
        print("Could not stop PID %s. Stop them in Task Manager (Windows) or with kill -9 "
              "(Linux/macOS)." % ", ".join(map(str, alive)), file=sys.stderr)
        return EXIT_NOT_STOPPED
    print("\nStopped the FragPipe run with PID %d. Its workdir cannot be found from here, "
          "so no status file was written." % run["pid"])
    return EXIT_STOPPED


def _hidden_note(procs: List[Dict]) -> None:
    hidden = hidden_java(procs)
    if hidden:
        print("Note: the command line of Java process %s cannot be read from here. A FragPipe "
              "run started as administrator looks like this; only a prompt opened with Run as "
              "administrator can see or stop it." % ", ".join(str(p["pid"]) for p in hidden))


def _nothing_found(procs: List[Dict], workdir: str, status: Optional[dict],
                   held: Optional[bool] = None) -> int:
    """No FragPipe process works in *workdir* on this computer.  Say so, unless the
    run is elsewhere or out of sight, or FragPipe was ended on its own and left its
    tools running."""
    state = status.get("state") if status else None
    going = bool(state) and state not in TERMINAL_STATES
    host = (status or {}).get("host")
    held = watcher_holds(workdir) if held is None else held
    watcher = local_watcher(procs, workdir, status) if held else None
    left = naming(procs, workdir)
    shown = "\n".join("  PID %-7d %s" % (p["pid"], p["cmd"][:150]) for p in left)
    named = ("These processes name the folder on their command line:\n%s\nThis script cannot "
             "tie them to the run, so it does not stop them. Show them to the user; once they "
             "confirm, end them in Task Manager (Details tab, End process tree) or with kill."
             % shown)
    remote = bool(host) and host != socket.gethostname() and watcher is None
    recorded = [t for t in (status or {}).get("tools_left") or [] if isinstance(t, dict)]
    if remote and (going or recorded):
        if going:
            print("FragPipe is not running in %s on this computer (%s); its status file says "
                  "it runs on %s. Stop it there: fragpipe_stop.py --workdir %s"
                  % (workdir, socket.gethostname(), host, workdir))
        else:
            print("FragPipe has ended in %s, but its status file says tools it started may "
                  "still run on %s (PID %s); this computer (%s) cannot see them. Check and "
                  "stop them there: fragpipe_stop.py --workdir %s"
                  % (workdir, host, ", ".join(str(t.get("pid")) for t in recorded),
                     socket.gethostname(), workdir))
        if left:
            print(named)
        return EXIT_NOT_STOPPED
    if held:
        if watcher is not None and not watcher["cmd"]:
            print("A fragpipe_run.py watcher holds %s, but the run's processes cannot be seen "
                  "from this prompt: it was started as administrator." % workdir)
            _hidden_note(procs)
        elif watcher is None and hidden_java(procs):
            print("A fragpipe_run.py watcher holds %s, but the run's processes cannot be seen "
                  "from this prompt: either it was started as administrator (see the note "
                  "below), or it runs on another computer that shares this folder, where it "
                  "must be stopped." % workdir)
            _hidden_note(procs)
        elif watcher is not None:
            print("The watcher of the run in %s (fragpipe_run.py, PID %d) is still going, but "
                  "no FragPipe process for this folder can be seen: FragPipe may be starting or "
                  "just ending. Run this again in a few seconds." % (workdir, watcher["pid"]))
        else:
            print("A fragpipe_run.py watcher holds %s, but not on this computer: the run is on "
                  "another computer that shares this folder. Stop it there." % workdir)
        if left:
            print(named)
        return EXIT_NOT_STOPPED
    if going and left:
        print("FragPipe is not running in %s, but its status file says %s. If FragPipe was "
              "ended on its own, the tools it started may still be running." % (workdir, state))
        print(named)
        return EXIT_NOT_STOPPED
    print("Nothing is running in %s%s." % (
        workdir, " (its status file says %s)" % state if state else ""))
    if left:
        print("One of these may be a FragPipe tool that was left running. %s" % named)
    _hidden_note(procs)
    return EXIT_NOTHING


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):     # a workdir in any script must not end it
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Stop a FragPipe run and every tool it started.")
    which = ap.add_mutually_exclusive_group()
    which.add_argument("--workdir", help="the run's output folder; omit to list the runs "
                                         "going now")
    which.add_argument("--pid", type=int,
                       help="the run's PID as the listing shows it; for a run whose "
                            "--workdir is relative")
    ap.add_argument("--yes", action="store_true",
                    help="stop it. Only once the user has confirmed.")
    args = ap.parse_args(argv)
    if args.yes and not args.workdir and args.pid is None:
        print("--yes needs --workdir or --pid: name the run to stop.", file=sys.stderr)
        return EXIT_BAD_ARGS
    if args.workdir and not os.path.isdir(args.workdir):
        print("No such folder: %s" % args.workdir, file=sys.stderr)
        return EXIT_BAD_ARGS

    try:
        procs = list_processes()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Cannot list the running processes: %s" % exc, file=sys.stderr)
        return EXIT_NOT_STOPPED

    if args.pid is not None:
        run = [r for r in roots(run_processes(procs)) if r["pid"] == args.pid]
        if not run:
            print("PID %d is not a FragPipe run that is going now. Run this script with no "
                  "options to list them." % args.pid)
            return EXIT_NOTHING
        wd = run[0]["workdir"]
        if not (wd and os.path.isabs(wd) and os.path.isdir(wd)):
            return _stop_by_pid(procs, run[0], args.yes)
        args.workdir = wd                   # the usual way, with its status file

    if not args.workdir:
        runs = roots(run_processes(procs))
        if not runs:
            print("No FragPipe run is going on this computer.")
            _hidden_note(procs)
            return EXIT_NOTHING
        print("FragPipe runs going now:")
        for r in runs:
            wd = r["workdir"]
            if not wd or not os.path.isabs(wd):
                print("  %s  (PID %d; a relative workdir: stop it with --pid %d)"
                      % (wd or "<no --workdir>", r["pid"], r["pid"]))
            else:
                print("  %s  (PID %d)" % (wd, r["pid"]))
        print("To stop one, re-run with --workdir <workdir> --yes, once the user confirms.")
        return EXIT_STOPPED

    workdir = os.path.abspath(args.workdir)
    hits = run_processes(procs, workdir)
    status = load_status(workdir)
    held = watcher_holds(workdir)
    if not hits and not tools_still_running(procs, status) and held:
        watcher = local_watcher(procs, workdir, status)
        if watcher is not None and watcher["cmd"]:
            # its watcher is here and going: FragPipe may not have started yet
            print("FragPipe is not running in %s yet, but its watcher is; waiting up to %d s..."
                  % (workdir, START_SEC))
            deadline = time.time() + START_SEC
            while not hits and held and time.time() < deadline \
                    and not ("child_exit_code" in (status or {})    # it has ended, not an
                             and status.get("watcher_pid") == watcher["pid"]   # earlier run
                             and status.get("host") in (None, socket.gethostname())):
                time.sleep(0.5)
                try:
                    procs = list_processes()
                except (OSError, ValueError, subprocess.SubprocessError):
                    break
                hits = run_processes(procs, workdir)
                status = load_status(workdir)
                held = watcher_holds(workdir)
    if not hits:
        tools = tools_still_running(procs, status)
        if tools:
            return _stop_tools(procs, workdir, status, tools, args.yes)
        maybe = [r for r in roots(run_processes(procs)) if r["given"]
                 and not os.path.isabs(r["given"]) and _tail_of(workdir, r["given"])]
        if maybe:
            print("No run names %s as its --workdir, but %s a relative one that may be it:"
                  % (workdir, "this FragPipe run has" if len(maybe) == 1
                     else "these FragPipe runs have"))
            for r in maybe:
                print("  %s  (PID %d)" % (r["workdir"], r["pid"]))
            print("Not stopped. If the user confirms it is this run, re-run with --pid <PID> "
                  "--yes.")
            return EXIT_NOT_STOPPED
        return _nothing_found(procs, workdir, status, held)
    pids = {h["pid"] for h in hits}
    here = bool(status) and status.get("host") in (None, socket.gethostname())
    live = here and any(p["pid"] == status.get("watcher_pid") and "fragpipe_run" in p["cmd"]
                        for p in procs)
    # this run's status names its launcher, or its watcher is alive here or is the
    # launcher's parent (before FragPipe prints anything); an earlier run's does neither
    owned = status if here and (
        status.get("pid") in pids
        or status.get("state") not in TERMINAL_STATES
        and (live or any(r["ppid"] == status.get("watcher_pid") for r in roots(hits)))) else None
    print(describe("in %s" % workdir, owned, procs, hits, held))
    if not args.yes:
        print("\nNot stopped. Show this to the user, and re-run with --yes once they confirm.")
        return EXIT_CONFIRM

    watcher = bool(owned) and live
    marker = os.path.join(workdir, STOP_NAME)
    try:
        _write_json(marker, {"requested_at": _now(), "pids": sorted(pids)})
    except OSError as exc:
        print("warning: could not write %s (%s); the run will read as failed, not "
              "stopped" % (marker, exc), file=sys.stderr)
    try:
        alive = kill(procs, hits)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Could not stop the run: %s" % exc, file=sys.stderr)
        alive = [-1]
    if alive:
        try:
            os.remove(marker)       # it keeps running, so it must not read as stopped later
        except OSError:
            pass
        if alive != [-1]:
            print("Could not stop PID %s. Stop them in Task Manager (Windows) or with "
                  "kill -9 (Linux/macOS)." % ", ".join(map(str, alive)), file=sys.stderr)
        return EXIT_NOT_STOPPED
    root = roots(hits)[0]
    done = finish_status(workdir, owned, watcher, root["pid"], held)
    if done not in ("watcher", "pending"):
        try:
            os.remove(marker)       # no watcher is left to read it, or it has read already
        except OSError:
            pass
    final = load_status(workdir) or {}
    rc = EXIT_STOPPED
    print("\nStopped the FragPipe run in %s." % workdir)
    if done == "ended":
        print("It had already ended on its own: %s"
              % (final.get("summary") or final.get("state")))
    elif done == "new run":
        print("Another run has started in this folder since; its status file was left alone.")
    elif done == "pending":
        print("Its watcher (fragpipe_run.py) has not finished yet; it records the stop when "
              "it does.")
        try:
            left = naming(list_processes(), workdir)
        except (OSError, ValueError, subprocess.SubprocessError):
            left = []
        if left:                            # what may still hold the watcher's output open
            print("These processes still name the folder on their command line:\n%s\nShow "
                  "them to the user; once they confirm, end them in Task Manager (Details tab, "
                  "End process tree) or with kill."
                  % "\n".join("  PID %-7d %s" % (p["pid"], p["cmd"][:150]) for p in left))
            rc = EXIT_NOT_STOPPED
    files = leftovers(root["cmd"], workdir, _started(owned))
    if files:
        print("\nMSFragger's scratch files, which a finished run removes, are left next to the "
              "inputs:\n%s" % "\n".join("  " + f for f in files))
    return rc


if __name__ == "__main__":
    sys.exit(main())
