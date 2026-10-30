"""The workflow builder's plan file, and the lock that guards a launch.

``build_workflow.py`` keeps everything it builds in ``<workdir>/fragskills-repro/``:

    fragskills.workflow       the shipped workflow plus the run-level settings
    fragskills.fp-manifest    the input files with their data types
    fragskills_plan.json      answers, base workflow, every change and its reason
    fragskills_design.tsv     the proposed / edited design sheet
    fragskills_channels.tsv   TMT/iTRAQ only: the channel annotation sheet
    annotations/              copies of the TMT annotations written next to the raw files

The workflow it writes starts with :data:`MARKER`.  ``fragpipe_run.py`` calls
:func:`check_lock` before every launch: a workflow carrying the marker runs only
when the plan beside it is confirmed and nothing it covers has changed since.
A workflow without the marker, and with no plan beside it, is hand-written and
is never affected.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

REPRO_DIR = "fragskills-repro"
PLAN_NAME = "fragskills_plan.json"
WORKFLOW_NAME = "fragskills.workflow"
MANIFEST_NAME = "fragskills.fp-manifest"
DESIGN_NAME = "fragskills_design.tsv"
CHANNELS_NAME = "fragskills_channels.tsv"
ANNOTATIONS_DIR = "annotations"

MARKER = "# fragskills-plan: " + PLAN_NAME

STATE_QUESTIONS = "questions"
STATE_BUILT = "built"
STATE_CONFIRMED = "confirmed"

LOCK_NONE = "not-builder-made"
LOCK_OK = "confirmed"
LOCK_REFUSED = "refused"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        if exc.filename is None:        # a failed read names no file; say which one
            raise OSError(exc.errno, exc.strerror, path) from exc
        raise
    return h.hexdigest()


def repro_dir(workdir: str) -> str:
    return os.path.join(os.path.abspath(workdir), REPRO_DIR)


def plan_path(workdir: str) -> str:
    return os.path.join(repro_dir(workdir), PLAN_NAME)


class PlanError(ValueError):
    """fragskills_plan.json exists but cannot be read."""


def load_plan(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            plan = json.load(fh)
    except OSError as exc:
        raise PlanError("%s cannot be opened (%s). Close any program that holds it (a sync "
                        "client, an editor), then try again." % (path, exc.strerror or exc))
    except ValueError as exc:
        raise PlanError("%s cannot be read (%s). Re-run build_workflow.py with a new "
                        "--workdir, or delete the file to start the plan again."
                        % (path, exc))
    if not isinstance(plan, dict):
        raise PlanError("%s is not a FragSkills plan. Delete it to start the plan again."
                        % path)
    return plan


def save_plan(path: str, plan: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plan["updated_at"] = now_iso()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:                    # no half-saved plan left beside the real one
            os.remove(tmp)
        except OSError:
            pass
        raise


def is_builder_made(workflow_path: str) -> bool:
    """True when the workflow starts with the builder's marker comment.

    Errs on the side of the lock: a byte-order mark, a marker line someone
    changed but did not remove, or any file beside a plan (a copy, another
    spelling of the name, or an edit saved as UTF-16, which hides the marker)
    all count.
    """
    if os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(workflow_path)),
                                   PLAN_NAME)):
        return True
    try:
        with open(workflow_path, "r", encoding="utf-8-sig", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                if "fragskills-plan:" in line:
                    return True
    except OSError:
        return False
    return False


def annotation_files(folder: str) -> list:
    """Entries headless FragPipe would take as this folder's TMT annotation.

    It lists names ending in ``annotation.txt`` next to a plex's raw files, a
    folder of that name included, and needs exactly one (FragPipe-GUI
    ``TmtiPanel.java``, ``Files.list``).
    """
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names if n.endswith("annotation.txt")]


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _same_file(a: str, b: str) -> bool:
    """The same file, however it is named (case, a Windows 8.3 short name)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _same_path(a, b)


_ANY_VERSION = object()


def check_lock(workflow_path: str, manifest_path: str,
               fragpipe_version=_ANY_VERSION) -> Tuple[str, str]:
    """May this workflow and manifest be launched (by FragPipe *fragpipe_version*)?

    Returns ``(status, message)`` with status :data:`LOCK_NONE` (a hand-written
    workflow; nothing to check), :data:`LOCK_OK`, or :data:`LOCK_REFUSED`.
    A *fragpipe_version* of None is a FragPipe whose version is unknown, which a
    builder-made workflow refuses; leave it out to skip the version check.
    """
    if not is_builder_made(workflow_path):
        return LOCK_NONE, "hand-written workflow"

    plan_file = os.path.join(os.path.dirname(os.path.abspath(workflow_path)), PLAN_NAME)
    try:
        plan = load_plan(plan_file)
    except PlanError as exc:
        return LOCK_REFUSED, str(exc)
    try:
        return _check_plan(plan, plan_file, workflow_path, manifest_path, fragpipe_version)
    except OSError as exc:
        return LOCK_REFUSED, ("cannot read %s (%s), so the lock cannot check it."
                              % (exc.filename or plan_file, exc.strerror or exc))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return LOCK_REFUSED, ("%s is damaged (%s: %s). Re-run build_workflow.py with a new "
                              "--workdir." % (plan_file, type(exc).__name__, exc))


def _check_plan(plan, plan_file, workflow_path, manifest_path, fragpipe_version):
    rebuild = ("Re-run build_workflow.py for this workdir, show the user the table, "
               "and confirm with --confirm.")
    if plan is None:
        return LOCK_REFUSED, ("%s was made by the workflow builder, but %s is not beside "
                              "it. %s" % (workflow_path, PLAN_NAME, rebuild))
    confirmed = plan.get("confirmed")
    if plan.get("state") == STATE_QUESTIONS:
        return LOCK_REFUSED, ("the plan in %s has open questions again (a later builder call "
                              "changed it). Run build_workflow.py --workdir \"%s\" to list "
                              "them, answer them, show the user the new table, then run "
                              "--confirm." % (plan_file,
                                              os.path.dirname(os.path.dirname(plan_file))))
    if plan.get("state") != STATE_CONFIRMED or not confirmed:
        return LOCK_REFUSED, ("the plan in %s has not been confirmed by the user. Show "
                              "them the confirmation table (build_workflow.py --show), "
                              "then run build_workflow.py --confirm." % plan_file)
    expected_workflow = (plan.get("files") or {}).get("workflow")
    if expected_workflow and not _same_file(workflow_path, expected_workflow) and \
            sha256_file(workflow_path) != confirmed.get("workflow_sha256"):
        return LOCK_REFUSED, ("%s sits beside the plan in %s but is not the workflow it "
                              "confirmed (%s). Launch that one, or keep other workflows "
                              "outside %s." % (workflow_path, os.path.dirname(plan_file),
                                               expected_workflow, REPRO_DIR))
    if sha256_file(workflow_path) != confirmed.get("workflow_sha256"):
        return LOCK_REFUSED, ("%s changed after it was confirmed. %s" % (workflow_path, rebuild))
    built_with = confirmed.get("fragpipe_version") or (plan.get("fragpipe") or {}).get("version")
    if fragpipe_version is not _ANY_VERSION and (fragpipe_version is None
                                                 or fragpipe_version != built_with):
        return LOCK_REFUSED, ("this workflow was built from FragPipe %s and confirmed for it, "
                              "not for %s. Launch it with --fragpipe-dir %s, or re-run "
                              "build_workflow.py with --fragpipe-dir for this one."
                              % (built_with or "of unknown version",
                                 "FragPipe %s" % fragpipe_version
                                 if fragpipe_version else "a FragPipe of unknown version",
                                 (plan.get("fragpipe") or {}).get("dir")))
    expected_manifest = os.path.join(os.path.dirname(os.path.abspath(workflow_path)),
                                     MANIFEST_NAME)
    if not _same_file(manifest_path, expected_manifest):
        return LOCK_REFUSED, ("this workflow was confirmed together with %s, not %s."
                              % (expected_manifest, manifest_path))
    if not os.path.isfile(manifest_path) or \
            sha256_file(manifest_path) != confirmed.get("manifest_sha256"):
        return LOCK_REFUSED, ("%s changed after it was confirmed. %s" % (manifest_path, rebuild))
    for path, digest in sorted((confirmed.get("annotations") or {}).items()):
        if not os.path.isfile(path) or os.path.basename(path) not in \
                os.listdir(os.path.dirname(os.path.abspath(path))):  # FragPipe: exact name
            return LOCK_REFUSED, ("the TMT annotation %s is gone or renamed. %s"
                                  % (path, rebuild))
        if sha256_file(path) != digest:
            return LOCK_REFUSED, ("the TMT annotation %s changed after it was confirmed. %s"
                                  % (path, rebuild))
        extra = [f for f in annotation_files(os.path.dirname(os.path.abspath(path)))
                 if not _same_path(f, path)]
        if extra:
            return LOCK_REFUSED, ("%s also holds %s; FragPipe needs exactly one annotation "
                                  "per plex folder. Move the extra one away."
                                  % (os.path.dirname(path),
                                     ", ".join(os.path.basename(f) for f in extra)))
    return LOCK_OK, "plan confirmed at %s" % confirmed.get("at", "?")
