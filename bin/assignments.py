#!/usr/bin/env python3
"""Assignment ledger: one append-only JSONL record per outcome the CTO asked for.

The ledger is the manager's notebook, not a second ticket system — it holds only what ADO and the
worktree registry do not: the CTO's priority and deadline, the manager's own breakdown, and which
worker owns each step. Storage reuses the escalation queue's append/fold helpers rather than
repeating them.
"""

import os
import time
import uuid
from datetime import UTC, datetime

from escalations import append as _queue_append
from escalations import current_state

LEDGER_PATH = os.path.expanduser("~/.claude/hermes/assignments.jsonl")

PRIORITIES = ("P0", "P1", "P2")
STATUSES = ("assigned", "in_progress", "blocked", "done", "cancelled")
OPEN_STATUSES = ("assigned", "in_progress", "blocked")

# How long an assignment can sit with no plan before it counts as stalled rather than merely
# new. Long enough that the manager mid-turn on something else, or normal call latency, is not
# flagged; short enough that a record the manager was never actually told about (this is how
# Finding 1 was found live: the ledger write succeeded but the notify call failed) surfaces the
# same day instead of sitting invisible indefinitely.
STALLED_AFTER_SECONDS = 60 * 60  # 1 hour


def new_assignment(title: str, priority: str = "P1", deadline=None, ado_refs=None) -> dict:
    """A fresh assignment. `plan` stays empty until the manager has decomposed it."""
    if not title or not title.strip():
        raise ValueError("title is required")
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")
    return {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "title": title.strip(),
        "priority": priority,
        "deadline": deadline,
        "ado_refs": list(ado_refs or []),
        "status": "assigned",
        "plan": [],
        "note": "",
    }


def append(record: dict, path: str = LEDGER_PATH) -> None:
    """Append one record to the ledger, stamped with the moment of the write.

    Delegates to escalations.append so there is exactly one locked writer for both queues —
    appending by shell redirection instead would skip that lock and risk a torn line, which every
    reader then silently drops.

    `ts` is the CREATION time and every later revision copies it verbatim (that is what makes
    stalled() able to ask how long an assignment has gone unplanned). So before this stamp, the
    ledger recorded no time at all for anything that happened AFTER an assignment was created:
    every revision of a record carried the identical `ts`, and the difference between the first
    record and the closing one — how long the work actually took — was exactly zero. `updated_at`
    is not derived from anything and cannot be recomputed later; only the writer knows when it
    wrote. It is set here rather than by each caller so the one mandated write path cannot be
    used without it.
    """
    _queue_append(path, {**record, "updated_at": time.time()})


def _date_passed(value, now: float) -> bool:
    """True when an ISO-8601 date or datetime is strictly in the past.

    A date with no time means the END of that day: a deadline of today is not late at 09:00.
    An unparseable value never raises an alarm — a typo in a date must not manufacture urgency.
    """
    if not value:
        return False
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    if len(text) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() < now


def at_risk(rec: dict, now: float) -> bool:
    """Open, and either its deadline or an unfinished step's ETA has passed.

    `plan` is model-authored against a prose schema, not a validated contract — a plan that is not
    a list, or a step that is not a dict, is ignored rather than raising.
    """
    if rec.get("status") in ("done", "cancelled"):
        return False
    if _date_passed(rec.get("deadline"), now):
        return True
    plan = rec.get("plan")
    if not isinstance(plan, list):
        return False
    return any(
        _date_passed(step.get("eta"), now) for step in plan if isinstance(step, dict) and step.get("state") != "done"
    )


def progress(rec: dict):
    """Fraction of plan steps done, or None when there is no plan yet.

    None rather than zero, so the UI can draw nothing instead of an empty bar implying no work.
    Same tolerance as at_risk: a plan that is not a list, or a step that is not a dict, is ignored.
    """
    plan = rec.get("plan")
    if not isinstance(plan, list):
        return None
    steps = [s for s in plan if isinstance(s, dict)]
    if not steps:
        return None
    return sum(1 for s in steps if s.get("state") == "done") / len(steps)


def stalled(rec: dict, now: float) -> bool:
    """Open, has no plan steps, and has sat that way longer than STALLED_AFTER_SECONDS.

    A distinct condition from at_risk, not a variant of it: at_risk fires on a deadline or a
    step ETA that has passed, which presupposes the work was planned and is now late. stalled
    fires on the absence of a plan altogether — nobody has broken the assignment into steps, so
    there is no date for it to be late against. One is being worked and is late; the other was
    never started. Reuses progress()'s notion of "no plan yet" rather than re-deriving it, so a
    malformed `plan` (not a list, or a list with no dict steps) degrades the same way in both.
    """
    if rec.get("status") in ("done", "cancelled"):
        return False
    if progress(rec) is not None:
        return False
    ts = rec.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    return (now - ts) > STALLED_AFTER_SECONDS


def open_assignments(path: str = LEDGER_PATH) -> list[dict]:
    """Every assignment still needing attention."""
    return [a for a in current_state(path) if a.get("status") in OPEN_STATUSES]
