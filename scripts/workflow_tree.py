"""The question tree behind build_workflow.py.

``workflow_tree.json`` lists the questions and, for every workflow FragPipe
ships, the answers that select it -- a *route*.  This module walks it::

    tree = load_tree()
    res = resolve(tree, {"analysis": "standard", "acquisition": "DDA"})
    res.open        # questions to ask now, each with the answers still possible
    res.rejected    # answers no shipped workflow accepts, with the nearest one
    res.route       # the route, once nothing is open

Questions are visited in file order.  One is *active* when every question
named in its ``when`` is settled to one of the listed values.  An active
question offers only the answers some route still accepts; the others are
reported as unavailable, naming the nearest shipped workflow.  A question with
a single possible answer is settled as "implied" and never asked, unless it is
marked ``always_ask``: those describe the samples or the instrument (PTM
enrichment, the TMT reference, the DIA instrument), so the user confirms the one
answer, or sees that theirs is unavailable and which workflow is nearest.

Earlier questions win: an answer that conflicts with the ones before it is
rejected, and its question stays open until the user answers it again, so
changing an early answer never silently overrides a later one.  An answer to a
question that is not active yet is kept (``pending``) until the questions it
depends on are answered; it is dropped only once they make it irrelevant.
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

TREE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "workflow_tree.json")

SOURCE_ANSWER = "answer"
SOURCE_IMPLIED = "implied"


def load_tree(path: str = TREE_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        tree = json.load(fh)
    tree["_questions"] = {q["id"]: q for q in tree["questions"]}
    tree["_settings"] = {s["id"]: s for s in tree["settings"]}
    return tree


# --- FragPipe versions --------------------------------------------------------

def supported_version(tree: dict, version: Optional[str]) -> Optional[str]:
    """Map '24.1-build61' to '24.1', or None when the builder does not support it."""
    m = re.match(r"^\s*(\d+)\.(\d+)", version or "")
    if not m:
        return None
    major_minor = "%s.%s" % (m.group(1), m.group(2))
    return major_minor if major_minor in tree["versions"] else None


def rename_key(tree: dict, version: str, key: str) -> str:
    """Translate a 24.1 key name into the name *version* uses."""
    return tree["versions"][version]["renames"].get(key, key)


# --- routes -------------------------------------------------------------------

def _accepts(route: dict, qid: str, value: str) -> bool:
    want = route["answers"].get(qid)
    if want is None:
        return True
    if isinstance(want, list):
        return value in want
    return value == want


def compatible(route: dict, settled: Dict[str, str]) -> bool:
    return all(_accepts(route, q, v) for q, v in settled.items())


def is_active(question: dict, settled: Dict[str, str]) -> bool:
    return all(dep in settled and settled[dep] in allowed
               for dep, allowed in question.get("when", {}).items())


def nearest(tree: dict, wanted: Dict[str, str], testing: Optional[str] = None,
            later: Optional[Dict[str, str]] = None) -> dict:
    """The shipped workflow that agrees with the most of *wanted*.

    Questions marked ``keep_in_nearest`` (the kind of analysis, acquisition, DIA
    format, TMT plex) describe the data rather than the settings, so the
    suggestion never crosses them -- except *testing*, the question being
    answered, whose nearest workflow necessarily differs in it.  *later* holds
    answers given for questions not reached yet: they count too, and are kept
    when some workflow keeps them all.  Ties go to the route listed first, which
    puts the general-purpose workflows ahead of their variants (LFQ with acetyl
    suggests LFQ-MBR, not TMT10-acetyl).
    """
    def kept_by(answers: Dict[str, str]) -> List[dict]:
        keeps = [q for q in answers if q != testing
                 and tree["_questions"].get(q, {}).get("keep_in_nearest")]
        return [r for r in tree["routes"] if all(_accepts(r, q, answers[q]) for q in keeps)]

    if later:
        wanted = dict({q: v for q, v in later.items() if q not in wanted}, **wanted)
    candidates = (kept_by(wanted) if later else []) or kept_by(
        {q: v for q, v in wanted.items() if q not in (later or {})}) or tree["routes"]
    best, best_score = None, -1
    for route in candidates:
        score = sum(1 for q, v in wanted.items()
                    if q in route["answers"] and _accepts(route, q, v))
        if score > best_score:
            best, best_score = route, score
    differs = {}
    for q, v in wanted.items():
        if not _accepts(best, q, v):
            want = best["answers"].get(q)
            differs[q] = want if not isinstance(want, list) else "/".join(want)
    return {"workflow": best["workflow"], "differs": differs}


class Resolution:
    """What a set of answers settles, leaves open, or rejects."""

    def __init__(self) -> None:
        self.settled: Dict[str, str] = {}     # question id -> value
        self.sources: Dict[str, str] = {}     # question id -> answer | implied
        self.open: List[dict] = []
        self.rejected: List[dict] = []
        self.pending: Dict[str, str] = {}     # answered, but not asked yet
        self.dropped: List[dict] = []         # answered, but never asked with these answers
        self.route: Optional[dict] = None

    @property
    def used_answers(self) -> Dict[str, str]:
        return {q: v for q, v in self.settled.items()
                if self.sources[q] == SOURCE_ANSWER}

    @property
    def kept_answers(self) -> Dict[str, str]:
        """Answers to keep in the plan: used, rejected (until replaced) and pending."""
        kept = dict(self.pending)
        kept.update((r["id"], r["value"]) for r in self.rejected)
        kept.update(self.used_answers)
        return kept


def _label(question: dict, value: str) -> str:
    for a in question["answers"]:
        if a["value"] == value:
            return a["label"]
    return value


def _open_question(tree: dict, q: dict, settled: Dict[str, str], values: List[str],
                   possible: List[str]) -> dict:
    qid = q["id"]
    return {
        "id": qid,
        "kind": "choice",
        "text": q["text"],
        "answers": [{"value": v, "label": _label(q, v)} for v in possible],
        "unavailable": [
            dict({"value": v, "label": _label(q, v)},
                 nearest=nearest(tree, dict(settled, **{qid: v}), qid))
            for v in values if v not in possible],
    }


def resolve(tree: dict, answers: Dict[str, str]) -> Resolution:
    res = Resolution()
    order = {q["id"]: i for i, q in enumerate(tree["questions"])}
    undecided = set()       # open, rejected, or waiting on one that is
    for q in tree["questions"]:
        qid = q["id"]
        if not is_active(q, res.settled):
            if any(dep in undecided for dep in q.get("when", {})):
                undecided.add(qid)
                if qid in answers:
                    res.pending[qid] = answers[qid]
            elif qid in answers:
                res.dropped.append({"id": qid, "value": answers[qid],
                                    "reason": "not asked with the answers given"})
            continue
        alive = [r for r in tree["routes"] if compatible(r, res.settled)]
        values = [a["value"] for a in q["answers"]]
        possible = [v for v in values if any(_accepts(r, qid, v) for r in alive)]

        if qid in answers:
            value = answers[qid]
            if value in possible:
                res.settled[qid], res.sources[qid] = value, SOURCE_ANSWER
                continue
            wanted = dict(res.settled, **{qid: value})
            later = {k: v for k, v in answers.items()
                     if order.get(k, -1) > order[qid] and k not in wanted}
            res.rejected.append({
                "id": qid, "value": value,
                "reason": ("not one of: %s" % ", ".join(values)
                           if value not in values else
                           "no shipped workflow combines it with the answers before it"),
                "nearest": nearest(tree, wanted, qid, later) if value in values else None,
            })
            # asked again, even with one possible answer: the user's answer was refused
            undecided.add(qid)
            res.open.append(_open_question(tree, q, res.settled, values, possible))
            continue
        if len(possible) == 1 and not q.get("always_ask"):
            res.settled[qid], res.sources[qid] = possible[0], SOURCE_IMPLIED
            continue
        undecided.add(qid)
        res.open.append(_open_question(tree, q, res.settled, values, possible))

    if not res.open:
        routes = [r for r in tree["routes"] if compatible(r, res.settled)]
        if len(routes) != 1:
            raise ValueError("workflow_tree.json is ambiguous: answers %s match %s"
                             % (res.settled, [r["workflow"] for r in routes]))
        res.route = routes[0]
    return res


def describe(tree: dict, qid: str, value: str) -> str:
    """Human label for an answer, for the confirmation table."""
    q = tree["_questions"].get(qid) or tree["_settings"].get(qid)
    if not q or "answers" not in q:
        return value
    return _label(q, value)
