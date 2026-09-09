#!/usr/bin/env python3
"""assert-based checks for the assignment ledger. Run: python3 bin/test_assignments.py"""

import os as _os
import tempfile
from datetime import UTC, datetime

from assignments import (
    OPEN_STATUSES,
    STALLED_AFTER_SECONDS,
    at_risk,
    new_assignment,
    open_assignments,
    progress,
    stalled,
)
from assignments import append as ledger_append
from escalations import append, current_state

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC).timestamp()
PAST = "2026-08-27"
FUTURE = "2026-08-30"


def test_new_assignment_defaults():
    a = new_assignment("Ship the manager")
    assert a["title"] == "Ship the manager"
    assert a["priority"] == "P1"
    assert a["status"] == "assigned"
    assert a["plan"] == []
    assert a["ado_refs"] == []
    assert len(a["id"]) == 12


def test_new_assignment_rejects_blank_title():
    for bad in ("", "   "):
        try:
            new_assignment(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for title {bad!r}")


def test_new_assignment_rejects_unknown_priority():
    try:
        new_assignment("x", priority="P9")
    except ValueError:
        return
    raise AssertionError("expected ValueError for priority 'P9'")


def test_at_risk_when_deadline_passed():
    a = new_assignment("late", deadline=PAST)
    assert at_risk(a, NOW) is True


def test_at_risk_false_when_deadline_ahead():
    a = new_assignment("fine", deadline=FUTURE)
    assert at_risk(a, NOW) is False


def test_at_risk_on_a_late_step():
    a = new_assignment("stepwise")
    a["plan"] = [{"step": "s", "owner": "w", "depends_on": [], "eta": PAST, "state": "doing"}]
    assert at_risk(a, NOW) is True


def test_at_risk_ignores_a_late_step_already_done():
    a = new_assignment("stepwise")
    a["plan"] = [{"step": "s", "owner": "w", "depends_on": [], "eta": PAST, "state": "done"}]
    assert at_risk(a, NOW) is False


def test_at_risk_false_when_closed():
    for status in ("done", "cancelled"):
        a = new_assignment("closed", deadline=PAST)
        a["status"] = status
        assert at_risk(a, NOW) is False, status


def test_at_risk_false_without_dates():
    assert at_risk(new_assignment("undated"), NOW) is False


def test_deadline_today_is_not_yet_late():
    """A date-only deadline means the end of that day, not midnight."""
    a = new_assignment("today", deadline="2026-08-28")
    assert at_risk(a, NOW) is False


def test_unparseable_date_never_raises_an_alarm():
    a = new_assignment("garbled", deadline="not-a-date")
    assert at_risk(a, NOW) is False


def test_at_risk_ignores_a_malformed_plan_shape():
    """plan is model-authored against a prose schema, not a validated contract — a scalar, a
    dict, or a list of non-dict steps must degrade to "not at risk", never raise."""
    for bad_plan in ("not a list", {"s1": "done"}, ["step one", "step two"]):
        a = new_assignment("malformed")
        a["plan"] = bad_plan
        assert at_risk(a, NOW) is False, bad_plan


def test_progress_none_without_a_plan():
    assert progress(new_assignment("unplanned")) is None


def test_progress_counts_done_steps():
    a = new_assignment("half")
    a["plan"] = [{"state": "done"}, {"state": "doing"}, {"state": "todo"}, {"state": "done"}]
    assert progress(a) == 0.5


def test_progress_full():
    a = new_assignment("all")
    a["plan"] = [{"state": "done"}, {"state": "done"}]
    assert progress(a) == 1.0


def test_progress_ignores_a_malformed_plan_shape():
    for bad_plan in ("not a list", {"s1": "done"}, ["step one", "step two"]):
        a = new_assignment("malformed")
        a["plan"] = bad_plan
        assert progress(a) is None, bad_plan


def test_stalled_when_open_unplanned_and_old():
    a = new_assignment("dropped")
    a["ts"] = NOW - STALLED_AFTER_SECONDS - 1
    assert stalled(a, NOW) is True


def test_stalled_false_when_recent():
    a = new_assignment("just created")
    a["ts"] = NOW - 60
    assert stalled(a, NOW) is False


def test_stalled_false_once_a_plan_exists():
    a = new_assignment("planned")
    a["ts"] = NOW - STALLED_AFTER_SECONDS - 1
    a["plan"] = [{"step": "s", "owner": "w", "depends_on": [], "eta": None, "state": "todo"}]
    assert stalled(a, NOW) is False


def test_stalled_false_when_closed():
    for status in ("done", "cancelled"):
        a = new_assignment("closed")
        a["ts"] = NOW - STALLED_AFTER_SECONDS - 1
        a["status"] = status
        assert stalled(a, NOW) is False, status


def test_stalled_ignores_a_malformed_plan_shape():
    """Same tolerance as at_risk/progress: a plan that isn't a list, or has no dict steps, is
    still "no plan yet" for staleness purposes rather than raising."""
    for bad_plan in ("not a list", {"s1": "done"}, ["step one", "step two"]):
        a = new_assignment("malformed")
        a["ts"] = NOW - STALLED_AFTER_SECONDS - 1
        a["plan"] = bad_plan
        assert stalled(a, NOW) is True, bad_plan


def test_stalled_false_without_a_usable_timestamp():
    a = new_assignment("no ts")
    a["ts"] = "not-a-number"
    assert stalled(a, NOW) is False


def test_stalled_false_when_ts_is_a_bool():
    """bool is an int subclass, so True would otherwise pass isinstance(ts, (int, float)) and
    read as ts=1 — an ancient timestamp that manufactures a false alarm, exactly what
    _date_passed's own docstring forbids for an unparseable value."""
    a = new_assignment("bool ts")
    a["ts"] = True
    assert stalled(a, NOW) is False


def test_ledger_folds_to_the_latest_state():
    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        a = new_assignment("evolving")
        append(path, a)
        append(path, {**a, "status": "in_progress", "note": "started"})
        state = current_state(path)
        assert len(state) == 1
        assert state[0]["status"] == "in_progress"
        assert state[0]["note"] == "started"
    finally:
        _os.unlink(path)


def test_open_assignments_excludes_closed():
    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        live = new_assignment("live")
        gone = new_assignment("gone")
        gone["status"] = "done"
        append(path, live)
        append(path, gone)
        names = [a["title"] for a in open_assignments(path)]
        assert names == ["live"], names
        assert all(s in OPEN_STATUSES for s in ("assigned", "in_progress", "blocked"))
    finally:
        _os.unlink(path)


def test_ledger_append_round_trips_through_current_state():
    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        a = new_assignment("shipped via assignments.append")
        ledger_append(a, path=path)
        state = current_state(path)
        assert len(state) == 1
        assert state[0]["title"] == "shipped via assignments.append"
    finally:
        _os.unlink(path)


def test_ledger_append_is_the_same_code_path_as_escalations_append():
    """Proves delegation rather than a second locking implementation. Does not re-exercise the
    flock itself — that coverage already lives in test_escalations.py."""
    import assignments
    import escalations

    assert assignments._queue_append is escalations.append


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
