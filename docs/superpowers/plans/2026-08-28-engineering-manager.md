# Engineering Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the plugin's stateless per-escalation classifier into a persistent Engineering Manager the CTO talks to on the dashboard, which decomposes work into a ledger, routes each worker to a model and effort sized to the task, chases what stalls, and escalates only real decisions.

**Architecture:** One long-lived Claude Code session identified by a saved `session_id`, reached only through `manager_session.ask()` which serializes callers with an `flock`. Four feeds enter it: the CTO's chat, escalations from the existing queue, worker-completion wakes, and a periodic tick. Commitments live in an append-only `assignments.jsonl` ledger rather than in conversation memory, so a lost session costs nothing important.

**Tech Stack:** Python 3 standard library only (`http.server`, `subprocess`, `fcntl`, `json`), bash + `jq`, vanilla JavaScript. No frameworks, no build step, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-28-engineering-manager-design.md`

## Global Constraints

- Python standard library only. Do not add a dependency to anything.
- Tests are plain `assert` functions named `test_*`, collected by a `__main__` block that runs every `test_*` in `globals()` and prints `PASS <name>` then `<n> passed`. No pytest, no unittest, no fixtures. Copy the runner block verbatim from `bin/test_manager.py`.
- Every impure boundary is injected as a parameter with a real default, exactly as `decide(record, ask_model)` already does, so logic is testable without spawning a model or touching the network.
- Catch subprocess failures as `(OSError, subprocess.SubprocessError, json.JSONDecodeError)`. `subprocess.TimeoutExpired` is a `SubprocessError` and **not** an `OSError`; this codebase has shipped that bug four times.
- Append-only JSONL folded by `id`. Reuse `escalations.py`'s `append`, `read_all`, `current_state` — do not write a second implementation of them.
- `bin/dashboard.html` is vanilla JS with no framework. Never assign untrusted data through `innerHTML`; use `textContent` or the existing `el()` helper. Model output, escalation text and chat text are all untrusted.
- Every new POST route lives under the existing `do_POST` `Origin` check and `MAX_BODY_BYTES` cap in `bin/dashboard.py`. Do not add a POST route above that guard.
- Manager model default `claude-opus-5`, effort default `max`, overridable by `PWT_MANAGER_MODEL` and `PWT_MANAGER_EFFORT`. Both flags are passed on every call including resumes.
- Valid effort levels are exactly `low`, `medium`, `high`, `xhigh`, `max`.
- Every failure degrades toward telling the human. Never guess, never silently swallow a message.
- No ADO write-back. Sessions handle their own ADO comments; the dashboard reads ADO only.
- Commit format `<type>: <description>`, one subject line. No `Co-Authored-By` trailer.
- Run the full suite before the final commit of each task: `python3 bin/test_assignments.py && python3 bin/test_manager_session.py && python3 bin/test_manager.py && python3 bin/test_escalations.py && python3 bin/test_dashboard.py && bash bin/test_parallel_task.sh`

## File Structure

| File | Responsibility |
|---|---|
| `bin/assignments.py` | Ledger record shape, `at_risk`, `progress`. Pure; no I/O beyond the shared JSONL helpers. |
| `skills/engineering-manager/SKILL.md` | The charter: eight verbs, routing rubric, hard boundaries. Loaded by Claude Code as a skill and prepended to the manager session's first message. |
| `bin/manager_session.py` | The one serialized door to the persistent session: `ask`, `history`, `busy`, `reset`. |
| `bin/parallel-task.sh` | Adds validated `--model` / `--effort` pass-through on `dispatch`. |
| `bin/manager.py` | Keeps judgement logic and worker delivery; loses the throwaway-process spawner. |
| `bin/manager_daemon.py` | Routes escalations into the session; adds worker-completion and tick wakes. |
| `bin/dashboard.py` | Manager and assignment HTTP routes; drops the ticket-dispatch route. |
| `bin/dashboard.html` | Two-column layout: decisions, assignments, ADO reference on the left; manager chat on the right. |

---

### Task 1: Assignment ledger

**Files:**
- Create: `bin/assignments.py`
- Create: `bin/test_assignments.py`

**Interfaces:**
- Consumes: `escalations.append(path, record)`, `escalations.read_all(path)`, `escalations.current_state(path)`
- Produces: `LEDGER_PATH`, `PRIORITIES`, `STATUSES`, `OPEN_STATUSES`, `new_assignment(title, priority="P1", deadline=None, ado_refs=None) -> dict`, `at_risk(rec, now) -> bool`, `progress(rec) -> float | None`, `open_assignments(path) -> list[dict]`

- [ ] **Step 1: Write the failing test**

Create `bin/test_assignments.py`:

```python
#!/usr/bin/env python3
"""assert-based checks for the assignment ledger. Run: python3 bin/test_assignments.py"""

import os as _os
import tempfile
from datetime import datetime, timezone

from assignments import (
    OPEN_STATUSES,
    at_risk,
    new_assignment,
    open_assignments,
    progress,
)
from escalations import append, current_state

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc).timestamp()
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_assignments.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'assignments'`

- [ ] **Step 3: Write the implementation**

Create `bin/assignments.py`:

```python
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
from datetime import datetime, timezone

from escalations import current_state

LEDGER_PATH = os.path.expanduser("~/.claude/hermes/assignments.jsonl")

PRIORITIES = ("P0", "P1", "P2")
STATUSES = ("assigned", "in_progress", "blocked", "done", "cancelled")
OPEN_STATUSES = ("assigned", "in_progress", "blocked")


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
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() < now


def at_risk(rec: dict, now: float) -> bool:
    """Open, and either its deadline or an unfinished step's ETA has passed."""
    if rec.get("status") in ("done", "cancelled"):
        return False
    if _date_passed(rec.get("deadline"), now):
        return True
    return any(
        _date_passed(step.get("eta"), now)
        for step in (rec.get("plan") or [])
        if step.get("state") != "done"
    )


def progress(rec: dict):
    """Fraction of plan steps done, or None when there is no plan yet.

    None rather than zero, so the UI can draw nothing instead of an empty bar implying no work.
    """
    steps = rec.get("plan") or []
    if not steps:
        return None
    return sum(1 for s in steps if s.get("state") == "done") / len(steps)


def open_assignments(path: str = LEDGER_PATH) -> list[dict]:
    """Every assignment still needing attention."""
    return [a for a in current_state(path) if a.get("status") in OPEN_STATUSES]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_assignments.py`
Expected: PASS for all 16 tests, ending `16 passed`

- [ ] **Step 5: Commit**

```bash
git add bin/assignments.py bin/test_assignments.py
git commit -m "feat: add the assignment ledger with derived at-risk and progress"
```

---

### Task 2: The manager charter

**Files:**
- Create: `skills/engineering-manager/SKILL.md`

**Interfaces:**
- Produces: a file whose full text Task 3 prepends to the manager session's first message. Task 3's `test_charter_has_its_load_bearing_sections` asserts the headings `## The eight actions`, `## Routing work`, and `## Hard boundaries` are present, so those exact heading strings are a contract.

- [ ] **Step 1: Write the charter**

Create `skills/engineering-manager/SKILL.md`:

```markdown
---
name: engineering-manager
description: Act as the Engineering Manager for a team of autonomous coding sessions - take an outcome from the CTO, decompose it, dispatch and size workers, chase what stalls, and escalate only real decisions. Use when asked to manage, assign, plan, or report on parallel worktree work.
---

# Engineering Manager

You are the single point of contact between the CTO and all engineering execution. The CTO states
outcomes. You decompose, dispatch, chase, and report. They should never have to assign work engineer
by engineer, track a ticket themselves, or chase anyone.

You are one long-lived session. Everything you have been told is still in this conversation, and
every commitment you have made is in the ledger. Read the ledger before answering any question about
state — recollection is not evidence.

## The eight actions

**Assign.** The CTO gives you an outcome, a priority, and maybe a deadline. Append a record to the
ledger immediately, before doing anything else, so the commitment survives you. Confirm back in one
line what you recorded.

**Plan.** Decompose the outcome into steps. Each step gets an owner (a worktree task name), any
steps it depends on, and an ETA you are willing to be measured against. Write the plan into the
record's `plan` field. State the plan in the chat in a few lines, not a wall of text.

**Review.** When a plan or an output genuinely needs the CTO's eyes, file an escalation rather than
proceeding. Do not ask for approval on everything — that recreates the babysitting this role exists
to remove. Ask when being wrong is expensive or hard to undo.

**Status.** Answer from the ledger and from live session state, with real numbers. Say which
assignments are done, in progress, blocked, and at risk, and name what each is waiting on.

**Blocker.** When a worker escalates, decide it if the evidence settles it. If it does not, or if it
touches anything irreversible, hand it to the CTO with the evidence already assembled.

**Reprioritize.** Update the record. If reprioritizing strands in-flight work, say so plainly rather
than quietly abandoning it.

**Follow-up.** On a tick, walk the open assignments. Chase steps whose ETA has passed, restart or
re-brief a worker that has stopped making progress, and update each record's `note`. Do not report
"still working" without having checked.

**Report.** If you have not written a report in 24 hours, write one on the next tick: what closed,
what moved, what is at risk, and what needs the CTO. Keep it short enough to read on a phone.

## The ledger

`~/.claude/hermes/assignments.jsonl`, append-only. Write a full record to append an update; the
latest record per `id` wins. Fields: `id`, `ts`, `title`, `priority` (`P0`/`P1`/`P2`), `deadline`,
`ado_refs`, `status` (`assigned`/`in_progress`/`blocked`/`done`/`cancelled`), `plan`, `note`.

A plan step is `{"step", "owner", "depends_on", "eta", "state"}` where `state` is `todo`, `doing`,
or `done`. `at_risk` and `progress` are computed from these — never store them.

## Dispatching work

Provision a copy, then dispatch into it:

    parallel-task.sh start <task-name> native
    parallel-task.sh dispatch <task-name> "<full brief>" --model <model> --effort <level>

The brief must stand alone: the requirement verbatim, the dev URLs `start` printed, the repo's own
rules and commit conventions, and a request to end with files changed, tests run, and the result.
A worker sees only what you write.

`parallel-task.sh list` shows every copy. `stop` pauses one, `rm` removes the worktree and keeps the
branch.

## Routing work

Size each piece of work before dispatching it, and say which tier you chose and why. A mechanical
edit and an ambiguous concurrency change do not deserve the same spend.

| Work | Model | Effort |
|---|---|---|
| Complex: multi-file design, security or concurrency, genuinely ambiguous requirements | `opus` | `max` |
| Medium: integration across a few files, pattern matching, debugging a known failure | `sonnet` | `high` |
| Simple: single file, mechanical change, the brief already contains the code to write | `sonnet` | `medium`, or `low` for pure transcription |

When unsure between two tiers, take the higher one for anything touching security, data, or
migrations, and the lower one for everything else.

## Escalating

File an escalation instead of deciding when the call is irreversible (delete, drop, force-push, a
data migration), when it involves `git push`, a pull request, or `main`, when it touches credentials
or auth, when two readings of the requirement produce two different products, when a worker has
failed to converge after repeated attempts, or when costs look anomalous.

    python3 - <<'PY'
    import sys; sys.path.insert(0, "<plugin bin dir>")
    from escalations import QUEUE_PATH, append, new_record
    append(QUEUE_PATH, new_record(session_id="<worker session id>", kind="scope_question",
        question="<the decision>", options=["<option a>", "<option b>"],
        evidence={"tests": "green", "branch": "feature/x"}))
    PY

Always supply `options`: the dashboard renders one button per option, so a well-formed escalation is
one the CTO can settle with a single click.

## Hard boundaries

- Never edit repository files for ticket work. Dispatch a worker instead. Writing to the ledger and
  the escalation queue is yours to do.
- Never `git push`, never open a pull request, never commit to `main` or `master`.
- Never answer a permission prompt on a human's behalf. If a tool is denied, say so in the chat and
  stop — do not retry around it.
- Never steer a session the CTO opened themselves. Observe and report.
- Never claim a step is done without having seen the evidence: a test result, a diff, a file.
```

- [ ] **Step 2: Verify the file parses as a skill and holds its contract headings**

Run: `python3 -c "t=open('skills/engineering-manager/SKILL.md').read(); [print('OK',h) for h in ('## The eight actions','## Routing work','## Hard boundaries') if h in t] ; assert t.startswith('---')"`
Expected: three `OK` lines and no assertion error

- [ ] **Step 3: Commit**

```bash
git add skills/engineering-manager/SKILL.md
git commit -m "feat: add the Engineering Manager charter"
```

---

### Task 3: The persistent manager session

**Files:**
- Create: `bin/manager_session.py`
- Create: `bin/test_manager_session.py`

**Interfaces:**
- Consumes: `skills/engineering-manager/SKILL.md` from Task 2
- Produces: `MANAGER_MODEL`, `MANAGER_EFFORT`, `STATE_PATH`, `CHAT_PATH`, `LOCK_PATH`, `ManagerBusy`, `ask_argv(session_id, text) -> list[str]`, `ask(text, source, run=subprocess.run, timeout=CALL_TIMEOUT) -> str`, `history(limit=200) -> list[dict]`, `busy() -> bool`, `reset() -> None`, `load_charter() -> str`

Tasks 5-8 call `ask(text, source)` and nothing else.

- [ ] **Step 1: Write the failing test**

Create `bin/test_manager_session.py`:

```python
#!/usr/bin/env python3
"""assert-based checks for the persistent manager session. Run: python3 bin/test_manager_session.py"""

import json
import os as _os
import subprocess
import tempfile

import manager_session as ms


class _Recorder:
    """Stand-in for subprocess.run that records argv and replays a scripted reply."""

    def __init__(self, session_id="sess-1", result="ok", raises=None):
        self.session_id = session_id
        self.result = result
        self.raises = raises
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        payload = json.dumps({"session_id": self.session_id, "result": self.result})
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")


def _isolate():
    """Point every state path at a fresh temp dir and return it."""
    d = tempfile.mkdtemp()
    ms.STATE_PATH = _os.path.join(d, "state.json")
    ms.CHAT_PATH = _os.path.join(d, "chat.jsonl")
    ms.LOCK_PATH = _os.path.join(d, "lock")
    return d


def test_argv_bootstraps_without_resume():
    argv = ms.ask_argv(None, "hello")
    assert "--resume" not in argv
    assert argv[-2:] == ["-p", "hello"]
    assert "--model" in argv and "--effort" in argv


def test_argv_resumes_with_a_session_id():
    argv = ms.ask_argv("sess-9", "hello")
    assert argv[argv.index("--resume") + 1] == "sess-9"


def test_effort_and_model_are_sent_on_every_call():
    """--effort applies per invocation, so a resume that omits it silently downgrades."""
    for sid in (None, "sess-9"):
        argv = ms.ask_argv(sid, "x")
        assert argv[argv.index("--model") + 1] == ms.MANAGER_MODEL
        assert argv[argv.index("--effort") + 1] == ms.MANAGER_EFFORT


def test_bootstrap_saves_the_session_id_and_prepends_the_charter():
    _isolate()
    run = _Recorder(session_id="fresh-1")
    reply = ms.ask("first message", "cto", run=run)
    assert reply == "ok"
    assert json.load(open(ms.STATE_PATH))["session_id"] == "fresh-1"
    sent = run.calls[0][-1]
    assert "first message" in sent
    assert "Engineering Manager" in sent, "charter must be prepended to the first message"


def test_second_call_resumes_and_does_not_resend_the_charter():
    _isolate()
    run = _Recorder(session_id="fresh-1")
    ms.ask("first", "cto", run=run)
    ms.ask("second", "cto", run=run)
    second = run.calls[1]
    assert "--resume" in second
    assert second[-1] == "second", "the charter must not be resent on a resume"


def test_both_turns_reach_the_chat_log_with_their_source():
    _isolate()
    ms.ask("wake up", "daemon:tick", run=_Recorder(result="on it"))
    entries = ms.history()
    assert [e["role"] for e in entries] == ["system", "manager"]
    assert all(e["source"] == "daemon:tick" for e in entries)
    assert entries[1]["text"] == "on it"


def test_a_cto_turn_is_logged_as_cto():
    _isolate()
    ms.ask("status?", "cto", run=_Recorder())
    assert ms.history()[0]["role"] == "cto"


def test_a_failing_call_is_recorded_not_swallowed():
    _isolate()
    reply = ms.ask("x", "cto", run=_Recorder(raises=subprocess.TimeoutExpired("claude", 600)))
    assert "failed" in reply.lower()
    entries = ms.history()
    assert entries[-1]["role"] == "manager"
    assert "failed" in entries[-1]["text"].lower()


def test_a_failing_call_releases_the_lock():
    _isolate()
    ms.ask("x", "cto", run=_Recorder(raises=OSError("boom")))
    assert ms.busy() is False


def test_busy_is_false_when_idle():
    _isolate()
    assert ms.busy() is False


def test_reset_drops_the_session_so_the_next_call_bootstraps():
    _isolate()
    run = _Recorder(session_id="a")
    ms.ask("one", "cto", run=run)
    ms.reset()
    assert not _os.path.exists(ms.STATE_PATH)
    run2 = _Recorder(session_id="b")
    ms.ask("two", "cto", run=run2)
    assert "--resume" not in run2.calls[0]
    assert json.load(open(ms.STATE_PATH))["session_id"] == "b"


def test_history_respects_its_limit_and_returns_the_newest():
    _isolate()
    run = _Recorder()
    for i in range(5):
        ms.ask(f"m{i}", "cto", run=run)
    tail = ms.history(limit=3)
    assert len(tail) == 3
    assert tail[-1]["role"] == "manager"


def test_charter_has_its_load_bearing_sections():
    text = ms.load_charter()
    for heading in ("## The eight actions", "## Routing work", "## Hard boundaries"):
        assert heading in text, heading


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_manager_session.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'manager_session'`

- [ ] **Step 3: Write the implementation**

Create `bin/manager_session.py`:

```python
#!/usr/bin/env python3
"""The one serialized door to the persistent Engineering Manager session.

Everything reaching the manager — the CTO's chat, escalations, worker-completion and tick wakes —
goes through ask(). One session means one memory: the manager knows what it dispatched, what it
decided, and what the CTO told it. An flock serializes callers, because two processes resuming the
same session would race on its transcript.
"""

import contextlib
import fcntl
import json
import os
import subprocess
import time

HERMES_DIR = os.path.expanduser("~/.claude/hermes")
STATE_PATH = os.path.join(HERMES_DIR, "manager-session.json")
CHAT_PATH = os.path.join(HERMES_DIR, "manager-chat.jsonl")
LOCK_PATH = os.path.join(HERMES_DIR, "manager.lock")
CHARTER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills",
    "engineering-manager",
    "SKILL.md",
)

MANAGER_MODEL = os.environ.get("PWT_MANAGER_MODEL", "claude-opus-5")
MANAGER_EFFORT = os.environ.get("PWT_MANAGER_EFFORT", "max")

LOCK_TIMEOUT = 30
CALL_TIMEOUT = 600

SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


class ManagerBusy(RuntimeError):
    """Another caller held the lock past the timeout."""


def load_charter(path: str = CHARTER_PATH) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(session_id: str) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"session_id": session_id, "started_at": time.time()}, fh)


def reset() -> None:
    """Forget the session so the next ask() bootstraps a fresh one."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(STATE_PATH)


def append_chat(role: str, source: str, text: str) -> None:
    """One line per turn. `role` says who spoke, `source` says what prompted the exchange."""
    os.makedirs(os.path.dirname(CHAT_PATH), exist_ok=True)
    with open(CHAT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "role": role, "source": source, "text": text}) + "\n")


def history(limit: int = 200) -> list[dict]:
    """The newest `limit` turns, oldest first. A corrupt line is skipped, never fatal."""
    try:
        with open(CHAT_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@contextlib.contextmanager
def _locked(timeout: int = LOCK_TIMEOUT):
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                raise ManagerBusy(f"manager busy: lock held longer than {timeout}s")
            time.sleep(0.2)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def busy() -> bool:
    """True when a call is in flight. Drives the chat's thinking indicator."""
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def ask_argv(session_id, text: str) -> list[str]:
    """Argv for one manager turn.

    Model and effort go on every call, resumes included: --effort applies to the invocation, not to
    the stored session, so omitting it on a resume silently downgrades the manager.
    """
    argv = ["claude", "--model", MANAGER_MODEL, "--effort", MANAGER_EFFORT, "--output-format", "json"]
    if session_id:
        argv += ["--resume", session_id]
    return argv + ["-p", text]


def ask(text: str, source: str, run=subprocess.run, timeout: int = CALL_TIMEOUT) -> str:
    """Send one turn to the manager and return its reply.

    The charter is prepended to the FIRST user message rather than passed as a flag, because
    --resume replays the transcript: a charter inside the transcript is re-read on every later call,
    while a flag passed once at bootstrap is not.
    """
    with _locked():
        session_id = _read_state().get("session_id")
        prompt = text if session_id else f"{load_charter()}\n\n---\n\n{text}"
        append_chat("cto" if source == "cto" else "system", source, text)
        try:
            proc = run(
                ask_argv(session_id, prompt),
                capture_output=True,
                text=True,
                check=True,
                timeout=timeout,
            )
            payload = json.loads(proc.stdout)
        except SUBPROC_ERRORS as e:
            reply = f"manager call failed: {e}"
            append_chat("manager", source, reply)
            return reply
        if not session_id and payload.get("session_id"):
            _write_state(payload["session_id"])
        reply = payload.get("result") or ""
        append_chat("manager", source, reply)
        return reply
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_manager_session.py`
Expected: PASS for all 13 tests, ending `13 passed`

- [ ] **Step 5: Commit**

```bash
git add bin/manager_session.py bin/test_manager_session.py
git commit -m "feat: add the persistent manager session behind one serialized entry point"
```

---

### Task 4: Size each worker — `dispatch --model` / `--effort`

**Files:**
- Modify: `bin/parallel-task.sh` — add `parse_dispatch_args` beside `parse_start_args` (after line 138), rewrite `cmd_dispatch` (lines 327-361)
- Modify: `bin/test_parallel_task.sh`

**Interfaces:**
- Produces: `parse_dispatch_args "$@"` setting globals `DISPATCH_MODEL`, `DISPATCH_EFFORT`, `DISPATCH_PROMPT`; registry entries gain optional `model` and `effort` keys read by Task 9's UI.

Globals rather than a printed tab-separated string, unlike `parse_start_args`: a dispatch prompt is long and multi-line, and a newline inside a tab-delimited return breaks the caller's `read`.

- [ ] **Step 1: Write the failing test**

Insert into `bin/test_parallel_task.sh` between the last `parse_start_args` case (line 40) and the
summary line `[[ $fail -eq 0 ]] && ...` (line 42). The helper is `assert_eq <desc> <expected>
<actual>` — expected first — and the file runs under `set -euo pipefail` while sourcing
`parallel-task.sh`.

Call `parse_dispatch_args` directly, never inside `$( )`: it communicates through globals, and a
subshell's assignments never reach the parent. Every rejection case must be guarded by `if`, because
`set -e` would abort the suite on a bare non-zero return.

```bash
# --- parse_dispatch_args --------------------------------------------------

parse_dispatch_args "do the thing"
assert_eq "dispatch: prompt only" "||do the thing" "$DISPATCH_MODEL|$DISPATCH_EFFORT|$DISPATCH_PROMPT"

parse_dispatch_args "do it" --model opus --effort max
assert_eq "dispatch: both flags" "opus|max|do it" "$DISPATCH_MODEL|$DISPATCH_EFFORT|$DISPATCH_PROMPT"

parse_dispatch_args "do it" --model sonnet
assert_eq "dispatch: model only" "sonnet|" "$DISPATCH_MODEL|$DISPATCH_EFFORT"

parse_dispatch_args "do it" --effort low
assert_eq "dispatch: effort only" "|low" "$DISPATCH_MODEL|$DISPATCH_EFFORT"

parse_dispatch_args "$(printf 'line one\nline two')" --effort high
assert_eq "dispatch: multi-line prompt survives" "$(printf 'line one\nline two')" "$DISPATCH_PROMPT"

parse_dispatch_args "flags before the prompt" --effort max
assert_eq "dispatch: flag order does not matter" "max|flags before the prompt" \
  "$DISPATCH_EFFORT|$DISPATCH_PROMPT"

if parse_dispatch_args "do it" --effort turbo 2>/dev/null; then
  echo "FAIL: unknown effort should return non-zero"; fail=1
else
  echo "PASS: unknown effort returns non-zero"
fi

if parse_dispatch_args "do it" --effort 2>/dev/null; then
  echo "FAIL: --effort with no value should return non-zero"; fail=1
else
  echo "PASS: --effort with no value returns non-zero"
fi

if parse_dispatch_args "do it" --model 2>/dev/null; then
  echo "FAIL: --model with no value should return non-zero"; fail=1
else
  echo "PASS: --model with no value returns non-zero"
fi

if parse_dispatch_args --model opus 2>/dev/null; then
  echo "FAIL: dispatch with no prompt should return non-zero"; fail=1
else
  echo "PASS: dispatch with no prompt returns non-zero"
fi
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash bin/test_parallel_task.sh`
Expected: FAIL — `parse_dispatch_args: command not found`, and the suite aborts under `set -e` at the
first unguarded call

- [ ] **Step 3: Add the parser**

Insert into `bin/parallel-task.sh` immediately after `parse_start_args`'s closing brace (line 138):

```bash
# parse_dispatch_args "$@" -> sets DISPATCH_MODEL, DISPATCH_EFFORT, DISPATCH_PROMPT; returns 1 on error.
# Globals rather than a printed tab-separated line: a prompt is multi-line, and a newline inside a
# tab-delimited return would break the caller's read.
parse_dispatch_args() {
  DISPATCH_MODEL=""; DISPATCH_EFFORT=""; DISPATCH_PROMPT=""
  local -a positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --model)
        [[ $# -ge 2 ]] || { echo "error: --model requires a value" >&2; return 1; }
        DISPATCH_MODEL="$2"; shift 2 ;;
      --effort)
        [[ $# -ge 2 ]] || { echo "error: --effort requires a value" >&2; return 1; }
        case "$2" in
          low|medium|high|xhigh|max) DISPATCH_EFFORT="$2" ;;
          *) echo "error: --effort must be low|medium|high|xhigh|max, got '$2'" >&2; return 1 ;;
        esac
        shift 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  [[ ${#positional[@]} -ge 1 ]] || { echo "error: dispatch needs <task-name> <prompt>" >&2; return 1; }
  DISPATCH_PROMPT="${positional[*]}"
}
```

- [ ] **Step 4: Run the parser tests to verify they pass**

Run: `bash bin/test_parallel_task.sh`
Expected: every `dispatch:` case passes along with the pre-existing cases

- [ ] **Step 5: Wire the flags through `cmd_dispatch`**

Replace the head of `cmd_dispatch` (its first four lines, through `local prompt="$*"`) with:

```bash
cmd_dispatch() {
  [[ $# -ge 2 ]] || { echo "error: dispatch needs <task-name> <prompt>" >&2; usage; }
  local task="$1"; shift
  parse_dispatch_args "$@" || usage
  local prompt="$DISPATCH_PROMPT"
```

Replace the launch block with an argv array — a plain `[[ -n x ]] && arr+=(...)` returns 1 when the
test fails and would trip `set -e`, so use `if`:

```bash
  local -a launch=(claude --bg -n "$task")
  if [[ -n "$DISPATCH_MODEL" ]]; then launch+=(--model "$DISPATCH_MODEL"); fi
  if [[ -n "$DISPATCH_EFFORT" ]]; then launch+=(--effort "$DISPATCH_EFFORT"); fi
  launch+=("$prompt")

  local launch_out
  if ! launch_out="$( cd "$wt_path" && "${launch[@]}" 2>&1 )"; then
```

Record the choice in the registry so the dashboard can show what each worker costs — replace the
`reg_merge_entry` call with:

```bash
  reg_merge_entry "$task" "$(jq -n \
    --arg sid "$short_id" --arg fid "$session_id" \
    --arg m "$DISPATCH_MODEL" --arg e "$DISPATCH_EFFORT" \
    '{short_id:$sid, session_id:$fid}
       + (if $m == "" then {} else {model:$m} end)
       + (if $e == "" then {} else {effort:$e} end)')"
  echo ">> $task dispatched: short id $short_id  session $session_id${DISPATCH_MODEL:+  model $DISPATCH_MODEL}${DISPATCH_EFFORT:+  effort $DISPATCH_EFFORT}"
```

Update `usage()` so the dispatch line reads:

```
  dispatch <task-name> <prompt> [--model <model>] [--effort low|medium|high|xhigh|max]
```

- [ ] **Step 6: Verify the whole script still parses and the suite is green**

Run: `bash -n bin/parallel-task.sh && bash bin/test_parallel_task.sh && bin/parallel-task.sh list`
Expected: no syntax error, all tests pass, `list` prints the existing copies unchanged

- [ ] **Step 7: Commit**

```bash
git add bin/parallel-task.sh bin/test_parallel_task.sh
git commit -m "feat: let dispatch pick a worker's model and effort"
```

---

### Task 5: Route escalations through the persistent session

**Files:**
- Modify: `bin/manager.py` — delete `MANAGER_MODEL`, `manager_argv`, `run_manager`
- Modify: `bin/manager_daemon.py` — import and call `manager_session.ask`
- Modify: `bin/test_manager.py` — drop tests for the deleted functions

**Interfaces:**
- Consumes: `manager_session.ask(text, source)` from Task 3
- Produces: `manager_daemon.ask_via_session(record) -> str`, the new default `ask_model` for `process_open`

Only the process that answers changes. `build_prompt`, `parse_decision`, `validate_decision`, `classify`'s tier-3 gate, the low-confidence degrade and the whole `_try_deliver` path stay exactly as they are.

- [ ] **Step 1: Write the failing test**

Add to `bin/test_manager.py`:

```python
def test_ask_via_session_sends_the_built_prompt_tagged_as_an_escalation():
    import manager_daemon
    from escalations import new_record

    sent = {}

    def fake_ask(text, source):
        sent["text"] = text
        sent["source"] = source
        return '{"answer": "retry", "reason": "transient", "confidence": "high"}'

    rec = new_record("s1", "red_tests", "Retry or reassign?", options=["retry", "reassign"])
    raw = manager_daemon.ask_via_session(rec, ask=fake_ask)
    assert "Retry or reassign?" in sent["text"]
    assert sent["source"] == "daemon:escalation"
    assert "retry" in raw


def test_manager_no_longer_spawns_a_throwaway_process():
    import manager

    for gone in ("manager_argv", "run_manager", "MANAGER_MODEL"):
        assert not hasattr(manager, gone), f"{gone} should have moved or been removed"
    assert hasattr(manager, "resume_argv"), "delivering into a worker session is still manager.py's job"
    assert hasattr(manager, "deliver_answer")
```

Delete `test_prompt_contains_question_and_options`'s siblings only if they reference the removed names — read the file and remove exactly the tests that import `MANAGER_MODEL`, `manager_argv`, or `run_manager`, and no others.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_manager.py`
Expected: FAIL with `AttributeError: module 'manager_daemon' has no attribute 'ask_via_session'`

- [ ] **Step 3: Make the change**

In `bin/manager.py`, delete the `MANAGER_MODEL` constant, `manager_argv`, and `run_manager`, and drop the now-unused `subprocess` import only if nothing else uses it (`deliver_answer` still does — keep it). Keep `build_prompt`, `parse_decision`, `validate_decision`, `resume_argv`, `deliver_answer`, `_route`, `decide` untouched.

In `bin/manager_daemon.py`, change the import line and add the adapter:

```python
import manager_session
from escalations import QUEUE_PATH, append, classify, current_state, record_answer
from manager import build_prompt, decide, deliver_answer


def ask_via_session(record: dict, ask=manager_session.ask) -> str:
    """Put one escalation to the persistent manager and hand back its raw reply.

    Same contract as the throwaway process it replaces — a JSON decision as text — but the manager
    now answers with the rest of its work in context.
    """
    return ask(build_prompt(record), "daemon:escalation")
```

and in `main()` replace `run_manager` with `ask_via_session`:

```python
            for outcome in process_open(path, ask_via_session, deliver_answer):
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_manager.py && python3 test_escalations.py`
Expected: both suites pass

- [ ] **Step 5: Commit**

```bash
git add bin/manager.py bin/manager_daemon.py bin/test_manager.py
git commit -m "feat: answer escalations from the persistent session instead of a throwaway process"
```

---

### Task 6: Wake triggers — worker completion and the tick

**Files:**
- Modify: `bin/manager_daemon.py`
- Modify: `bin/test_manager.py`

**Interfaces:**
- Consumes: `assignments.open_assignments` (Task 1), `manager_session.ask` (Task 3)
- Produces: `SEEN_PATH`, `TICK_SECONDS`, `list_agents(run=subprocess.run) -> list[dict]`, `finished_sessions(agents, seen) -> tuple[list[dict], dict]`, `should_tick(last_tick, now, open_count, running_count) -> bool`

- [ ] **Step 1: Write the failing test**

Add to `bin/test_manager.py`:

```python
def test_finished_sessions_fires_once_on_the_transition_to_idle():
    import manager_daemon as md

    agents = [{"sessionId": "a", "name": "task-a", "status": "idle"}]
    fired, seen = md.finished_sessions(agents, {"a": "busy"})
    assert [f["sessionId"] for f in fired] == ["a"]
    assert seen["a"] == "idle"

    fired_again, _ = md.finished_sessions(agents, seen)
    assert fired_again == [], "the same status must not fire twice"


def test_finished_sessions_records_an_unseen_session_without_firing():
    """A daemon restart must not re-announce work that finished days ago."""
    import manager_daemon as md

    fired, seen = md.finished_sessions([{"sessionId": "a", "name": "t", "status": "idle"}], {})
    assert fired == []
    assert seen == {"a": "idle"}


def test_finished_sessions_ignores_a_still_busy_worker():
    import manager_daemon as md

    fired, _ = md.finished_sessions([{"sessionId": "a", "name": "t", "status": "busy"}], {"a": "busy"})
    assert fired == []


def test_list_agents_degrades_to_empty_on_a_subprocess_failure():
    import subprocess

    import manager_daemon as md

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("claude", 30)

    assert md.list_agents(run=boom) == []


def test_should_tick_stays_quiet_with_nothing_open():
    import manager_daemon as md

    assert md.should_tick(0, md.TICK_SECONDS + 1, open_count=0, running_count=0) is False


def test_should_tick_fires_when_work_is_open_and_the_interval_has_passed():
    import manager_daemon as md

    assert md.should_tick(0, md.TICK_SECONDS + 1, open_count=1, running_count=0) is True
    assert md.should_tick(0, md.TICK_SECONDS + 1, open_count=0, running_count=2) is True


def test_should_tick_waits_out_the_interval():
    import manager_daemon as md

    assert md.should_tick(0, md.TICK_SECONDS - 1, open_count=3, running_count=1) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_manager.py`
Expected: FAIL with `AttributeError: module 'manager_daemon' has no attribute 'finished_sessions'`

- [ ] **Step 3: Write the implementation**

Add to `bin/manager_daemon.py`:

```python
import json
import os
import subprocess

from assignments import open_assignments

SEEN_PATH = os.path.expanduser("~/.claude/hermes/manager-seen-sessions.json")
TICK_SECONDS = int(os.environ.get("PWT_MANAGER_TICK_SECONDS", "1800"))
DONE_STATUSES = ("idle", "done", "finished", "stopped")

SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


def list_agents(run=subprocess.run) -> list[dict]:
    """Every session Claude Code knows about, or an empty list if the call fails.

    Degrading to empty rather than raising keeps a transient CLI failure from aborting the
    escalation pass, which is the more important half of a daemon cycle.
    """
    try:
        proc = run(
            ["claude", "agents", "--json", "--all"],
            capture_output=True, text=True, check=True, timeout=30,
        )
        agents = json.loads(proc.stdout)
    except SUBPROC_ERRORS:
        return []
    return agents if isinstance(agents, list) else []


def finished_sessions(agents: list[dict], seen: dict) -> tuple[list[dict], dict]:
    """Sessions that just stopped working, and the status map to persist.

    A session absent from `seen` is recorded silently: on a daemon restart, work that finished days
    ago must not be re-announced.
    """
    fired, updated = [], dict(seen)
    for agent in agents:
        sid = agent.get("sessionId")
        if not sid:
            continue
        status = agent.get("status")
        previous = seen.get(sid)
        if previous is not None and previous != status and status in DONE_STATUSES:
            fired.append(agent)
        updated[sid] = status
    return fired, updated


def _read_seen() -> dict:
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_seen(seen: dict) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)


def should_tick(last_tick: float, now: float, open_count: int, running_count: int) -> bool:
    """Tick only when the interval has passed AND there is something to chase.

    An idle dashboard must not burn tokens on a manager with nothing to manage.
    """
    if now - last_tick < TICK_SECONDS:
        return False
    return open_count > 0 or running_count > 0
```

Extend `main()`'s loop body, after the existing `process_open` call:

```python
        try:
            agents = list_agents()
            fired, seen = finished_sessions(agents, _read_seen())
            _write_seen(seen)
            for agent in fired:
                name = agent.get("name") or agent.get("sessionId")
                manager_session.ask(
                    f"Worker '{name}' (session {agent.get('sessionId')}) finished. "
                    "Check its work, update the ledger, and dispatch what comes next.",
                    "daemon:worker-finished",
                )

            running = sum(1 for a in agents if a.get("status") == "busy")
            if should_tick(last_tick, time.time(), len(open_assignments()), running):
                last_tick = time.time()
                manager_session.ask(
                    "Tick. Walk the open assignments: chase anything past its ETA, update each "
                    "note, and write a report if you have not written one in 24 hours.",
                    "daemon:tick",
                )
        except Exception as e:  # a bad wake pass must not kill the daemon
            print(f"  wake pass failed: {e}", file=sys.stderr)
```

Initialise `last_tick = time.time()` before the loop, so a daemon restart does not fire a tick immediately.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_manager.py`
Expected: all tests pass, including the seven new ones

- [ ] **Step 5: Commit**

```bash
git add bin/manager_daemon.py bin/test_manager.py
git commit -m "feat: wake the manager when a worker finishes and on a periodic tick"
```

---

### Task 7: Manager HTTP routes

**Files:**
- Modify: `bin/dashboard.py` — add GET/POST routes inside the existing handlers
- Modify: `bin/test_dashboard.py`

**Interfaces:**
- Consumes: `manager_session.ask`, `history`, `busy`, `reset`, `ManagerBusy` (Task 3)
- Produces: `GET /api/manager/chat` → `{"entries": [...], "busy": bool}`; `POST /api/manager/chat` `{"text"}` → `202 {"ok": true}`; `POST /api/manager/reset` → `{"ok": true}`; `start_manager_turn(text, source)` spawning the background thread

- [ ] **Step 1: Write the failing test**

Add to `bin/test_dashboard.py`:

```python
def test_start_manager_turn_returns_before_the_model_does():
    """A model call must never be held inside an HTTP request."""
    import threading
    import time as _time

    import dashboard

    released = threading.Event()

    def slow_ask(text, source):
        _time.sleep(0.3)
        released.set()
        return "done"

    began = _time.monotonic()
    thread = dashboard.start_manager_turn("hello", "cto", ask=slow_ask)
    assert _time.monotonic() - began < 0.2, "start_manager_turn blocked on the model"
    thread.join(timeout=5)
    assert released.is_set()


def test_manager_chat_payload_shape():
    import dashboard

    payload = dashboard.manager_chat_payload(
        history=lambda limit=200: [{"ts": 1.0, "role": "cto", "source": "cto", "text": "hi"}],
        busy=lambda: True,
    )
    assert payload["busy"] is True
    assert payload["entries"][0]["role"] == "cto"


def test_manager_chat_payload_degrades_when_history_fails():
    import dashboard

    def boom(limit=200):
        raise OSError("no such file")

    payload = dashboard.manager_chat_payload(history=boom, busy=lambda: False)
    assert payload["entries"] == []
    assert payload["busy"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: FAIL with `AttributeError: module 'dashboard' has no attribute 'start_manager_turn'`

- [ ] **Step 3: Write the implementation**

Add near the other helpers in `bin/dashboard.py`:

```python
import threading

import manager_session


def start_manager_turn(text: str, source: str, ask=None) -> threading.Thread:
    """Run one manager turn off the request thread.

    A manager call takes tens of seconds and may take ten minutes; holding an HTTP request open for
    it would stall the browser and time out the fetch. The turn is already recorded in the chat log
    by ask(), so the UI learns the answer by polling GET /api/manager/chat.
    """
    ask = ask or manager_session.ask
    thread = threading.Thread(target=ask, args=(text, source), daemon=True)
    thread.start()
    return thread


def manager_chat_payload(history=None, busy=None) -> dict:
    """Chat entries plus whether a turn is in flight. Degrades to empty, never raises."""
    history = history or manager_session.history
    busy = busy or manager_session.busy
    try:
        entries = history()
    except (OSError, ValueError):
        entries = []
    try:
        in_flight = busy()
    except OSError:
        in_flight = False
    return {"entries": entries, "busy": in_flight}
```

In `do_GET`, beside the other route checks:

```python
        if parsed.path == "/api/manager/chat":
            self._json(manager_chat_payload())
            return
```

In `do_POST`, below the existing `Origin` guard and alongside the other POST routes:

```python
        if parsed.path == "/api/manager/chat":
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._json({"error": str(e)}, status=400)
                return
            text = (body.get("text") or "").strip()
            if not text:
                self._json({"error": "text is required"}, status=400)
                return
            try:
                start_manager_turn(text, "cto")
            except manager_session.ManagerBusy as e:
                self._json({"error": str(e)}, status=503)
                return
            self._json({"ok": True}, status=202)
            return

        if parsed.path == "/api/manager/reset":
            manager_session.reset()
            self._json({"ok": True})
            return
```

The existing POST body reading (length check against `MAX_BODY_BYTES`, JSON parse, dict check) is duplicated across the current routes; extract it once as `_read_json_body()` on the handler, raising `ValueError` with the existing messages, and use it from every POST route including the pre-existing ones.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_dashboard.py`
Expected: all tests pass, including the three new ones

- [ ] **Step 5: Verify the routes live**

Run each in turn, with the dashboard started on a spare port:

```bash
python3 bin/dashboard.py 4455 . & sleep 2
curl -s localhost:4455/api/manager/chat | head -c 200
curl -s -X POST localhost:4455/api/manager/chat -H 'Content-Type: application/json' -d '{"text":""}' -w ' [%{http_code}]\n'
kill %1
```

Expected: the GET returns `{"entries": [...], "busy": false}`; the empty-text POST returns `400`.

- [ ] **Step 6: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: serve the manager chat over HTTP without holding a request on the model"
```

---

### Task 8: Assignment routes, and retiring ticket dispatch

**Files:**
- Modify: `bin/dashboard.py` — add assignment routes; delete `POST /api/tickets/dispatch` and `_build_dispatch_argv`, `_run_dispatch`, `_build_dispatch_prompt`, `_ticket_task_slug`, `_parse_ticket_ids`
- Modify: `bin/test_dashboard.py` — delete the tests for the removed helpers

**Interfaces:**
- Consumes: `assignments.new_assignment`, `at_risk`, `progress`, `LEDGER_PATH`; `escalations.append`, `current_state`; `start_manager_turn` (Task 7)
- Produces: `GET /api/assignments` → records each with `at_risk` and `progress`; `POST /api/assignments` `{"title","priority","deadline","ado_refs"}` → `202`; `get_assignments(now=None) -> list[dict]`

The route provisioned a worktree straight from a ticket, which is the CTO assigning work engineer by engineer — the thing this design removes. The behaviour is not lost: the manager provisions by running `parallel-task.sh` itself, deciding how many copies the outcome actually needs.

- [ ] **Step 1: Write the failing test**

Add to `bin/test_dashboard.py`:

```python
def test_get_assignments_decorates_with_at_risk_and_progress():
    import os as _os
    import tempfile

    import dashboard
    from assignments import new_assignment
    from escalations import append

    fd, path = tempfile.mkstemp()
    _os.close(fd)
    try:
        a = new_assignment("late one", deadline="2020-01-01")
        a["plan"] = [{"state": "done"}, {"state": "todo"}]
        append(path, a)
        rows = dashboard.get_assignments(path=path)
        assert rows[0]["at_risk"] is True
        assert rows[0]["progress"] == 0.5
    finally:
        _os.unlink(path)


def test_ticket_dispatch_helpers_are_gone():
    import dashboard

    for gone in (
        "_build_dispatch_argv",
        "_run_dispatch",
        "_build_dispatch_prompt",
        "_ticket_task_slug",
        "_parse_ticket_ids",
    ):
        assert not hasattr(dashboard, gone), f"{gone} should have been removed with the route"
```

Then delete every existing test in `bin/test_dashboard.py` that references one of those five names, and nothing else.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: FAIL with `AttributeError: module 'dashboard' has no attribute 'get_assignments'`

- [ ] **Step 3: Write the implementation**

Add to `bin/dashboard.py`:

```python
import time

import assignments as ledger


def get_assignments(path: str = None, now: float = None) -> list[dict]:
    """Every assignment, each decorated with the two derived values the UI needs."""
    path = path or ledger.LEDGER_PATH
    now = now if now is not None else time.time()
    rows = []
    for rec in current_state(path):
        rows.append({**rec, "at_risk": ledger.at_risk(rec, now), "progress": ledger.progress(rec)})
    return rows
```

In `do_GET`:

```python
        if parsed.path == "/api/assignments":
            try:
                self._json(get_assignments())
            except (OSError, ValueError) as e:
                self._json({"error": str(e)}, status=500)
            return
```

In `do_POST`:

```python
        if parsed.path == "/api/assignments":
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._json({"error": str(e)}, status=400)
                return
            try:
                record = ledger.new_assignment(
                    body.get("title") or "",
                    priority=body.get("priority") or "P1",
                    deadline=body.get("deadline"),
                    ado_refs=body.get("ado_refs") or [],
                )
            except ValueError as e:
                self._json({"error": str(e)}, status=400)
                return
            append(ledger.LEDGER_PATH, record)
            refs = ", ".join(f"AB#{r.get('id')}" for r in record["ado_refs"]) or "none"
            start_manager_turn(
                f"New assignment {record['id']}: {record['title']}\n"
                f"Priority {record['priority']}, deadline {record['deadline'] or 'none'}, "
                f"ADO refs: {refs}.\n"
                "It is already in the ledger. Plan it, size each step, dispatch, and confirm in one line.",
                "cto",
            )
            self._json({"ok": True, "record": record}, status=202)
            return
```

Delete the `/api/tickets/dispatch` route block and the five helper functions listed in **Files**.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_dashboard.py`
Expected: all tests pass; the deleted helpers' tests are gone and the two new ones pass

- [ ] **Step 5: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: assign outcomes to the manager, replacing per-ticket worktree dispatch"
```

---

### Task 9: Dashboard left column — decisions, assignments, counters

**Files:**
- Modify: `bin/dashboard.html`

**Interfaces:**
- Consumes: `GET /api/assignments` (Task 8), `GET /api/escalations` and `POST /api/escalations/<id>/answer` (both pre-existing)
- Produces: DOM ids `#decisions`, `#assignments`, `#counters` and the two-column shell `#manager-grid`, which Task 10 mounts the chat into

Read `bin/dashboard.html` in full before editing. Follow its existing conventions exactly: the `el()` helper, `textContent` for every value, the current polling idiom, and the existing CSS variable names. Do not introduce a framework, a build step, or `innerHTML` with data.

- [ ] **Step 1: Restructure the page shell**

Change the page heading from "Runtime health & logs" to "Engineering Manager", keeping the existing "Agent Control Center" header lockup above it. Wrap the CTO-facing panels in a two-column grid and leave the operator `<details>` section below it, untouched:

```html
<div id="counters" class="counters"></div>
<div id="manager-grid" class="manager-grid">
  <div class="manager-col-main">
    <section id="decisions" class="panel"></section>
    <section id="assignments" class="panel"></section>
    <section id="ado" class="panel"></section>
  </div>
  <aside id="manager-chat" class="panel manager-col-side"></aside>
</div>
```

```css
.manager-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 420px); gap: 16px; align-items: start; }
.manager-col-side { position: sticky; top: 16px; max-height: calc(100dvh - 32px); display: flex; flex-direction: column; }
@media (max-width: 1100px) { .manager-grid { grid-template-columns: minmax(0, 1fr); } .manager-col-side { position: static; max-height: none; } }
```

Move the existing Tickets panel into `#ado`, retitle it "ADO backlog" with the subtitle "Reference only — assign an outcome, not a ticket", and keep its rows and links exactly as they are.

Delete the "Decided for you" panel and its render function. Those decisions are no longer lost by
removing it: a tier-2 answer now flows through `manager_session.ask(..., "daemon:escalation")`, so
both the escalation and the manager's answer land in the chat log and render in Task 10's panel as
tinted proactive turns. The panel's failing was its position — buried at the foot of a collapsed
section — and the chat is where it will actually be read. The manager's answer appears there as its
raw decision JSON; that is legible enough for MVP and is noted in the README rather than parsed.

- [ ] **Step 2: Render the four counters**

```js
function renderCounters(assignments, escalations) {
  const open = assignments.filter(a => !["done", "cancelled"].includes(a.status));
  const weekAgo = Date.now() / 1000 - 7 * 86400;
  const cards = [
    ["Đang giao", open.length],
    ["Trễ hạn", open.filter(a => a.at_risk).length],
    ["Chờ bạn", escalations.filter(e => e.status === "needs_human").length],
    ["Xong tuần này", assignments.filter(a => a.status === "done" && (a.ts || 0) >= weekAgo).length],
  ];
  const box = document.getElementById("counters");
  box.replaceChildren(...cards.map(([label, n]) =>
    el("div", { class: "counter" }, [
      el("div", { class: "counter-n" }, String(n)),
      el("div", { class: "counter-label" }, label),
    ])));
}
```

- [ ] **Step 3: Render decisions with one button per option**

The record already carries `options`, `validate_decision` already requires the answer to be one of them, and `POST /api/escalations/<id>/answer` already exists — only the buttons are missing.

```js
function renderDecisions(escalations) {
  const pending = escalations.filter(e => e.status === "needs_human");
  const panel = document.getElementById("decisions");
  const head = el("h2", {}, "Cần bạn quyết");
  if (!pending.length) {
    panel.replaceChildren(head, el("p", { class: "muted" }, "Không có gì chờ bạn."));
    return;
  }
  panel.replaceChildren(head, ...pending.map(rec =>
    el("div", { class: "decision" }, [
      el("div", { class: "decision-q" }, rec.question || ""),
      el("div", { class: "decision-why muted" }, rec.reason || ""),
      el("div", { class: "decision-actions" },
        (rec.options || []).map(opt =>
          el("button", { class: "btn", onclick: () => answerEscalation(rec.id, opt) }, opt))),
    ])));
}

async function answerEscalation(id, answer) {
  const res = await fetch(`/api/escalations/${encodeURIComponent(id)}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ answer, decided_by: "cto" }),
  });
  if (!res.ok) { alert(`Không gửi được: ${res.status}`); return; }
  pollEscalations();
}
```

If `el()` does not support an `onclick` key, attach the listener with `addEventListener` after construction rather than changing `el()`.

- [ ] **Step 4: Render assignments**

`progress` is `null` when there is no plan yet. Draw no bar in that case — never substitute zero, which would read as "started and got nowhere".

```js
function renderAssignments(rows) {
  const open = rows.filter(a => !["done", "cancelled"].includes(a.status));
  const panel = document.getElementById("assignments");
  const head = el("h2", {}, "Việc đang giao");
  if (!open.length) {
    panel.replaceChildren(head, el("p", { class: "muted" }, "Chưa có việc nào được giao."));
    return;
  }
  panel.replaceChildren(head, ...open.map(a => {
    const parts = [
      el("div", { class: "assign-top" }, [
        el("span", { class: `pill p-${a.priority}` }, a.priority),
        el("span", { class: "assign-title" }, a.title),
        el("span", { class: `pill s-${a.status}` }, a.status),
        ...(a.at_risk ? [el("span", { class: "pill at-risk" }, "trễ hạn")] : []),
      ]),
    ];
    if (a.progress !== null && a.progress !== undefined) {
      const bar = el("div", { class: "bar" }, [el("div", { class: "bar-fill" })]);
      bar.firstChild.style.width = `${Math.round(a.progress * 100)}%`;
      parts.push(bar, el("div", { class: "muted" },
        `${(a.plan || []).filter(s => s.state === "done").length}/${(a.plan || []).length} bước`));
    }
    (a.plan || []).forEach(s => parts.push(
      el("div", { class: `step step-${s.state}` }, [
        el("span", { class: "step-name" }, s.step || ""),
        el("span", { class: "muted" }, [s.owner, s.eta].filter(Boolean).join(" · ")),
      ])));
    if (a.note) parts.push(el("div", { class: "assign-note muted" }, a.note));
    return el("div", { class: "assign" }, parts);
  }));
}
```

- [ ] **Step 5: Wire the polling**

Add `pollAssignments()` fetching `/api/assignments` into `lastAssignments`, and call `renderCounters(lastAssignments, lastEscalations)` from both it and `pollEscalations`. Follow the existing polling interval and failure handling: on failure keep the last-known data and render the same visible error treatment the other panels already use — a silently stale panel is the failure mode this dashboard has already been bitten by.

- [ ] **Step 6: Verify in a browser**

```bash
python3 bin/dashboard.py 4455 . & sleep 2
curl -s localhost:4455/ | grep -c 'manager-grid'
```

Then open `http://127.0.0.1:4455/`, confirm: the page title reads "Engineering Manager"; four counters render; "Cần bạn quyết" shows its empty line; "Việc đang giao" shows its empty line; the ADO backlog still lists tickets with working links; the operator `<details>` section still opens and its contents are unchanged. Kill the server.

- [ ] **Step 7: Commit**

```bash
git add bin/dashboard.html
git commit -m "feat: rebuild the dashboard around decisions and assignments"
```

---

### Task 10: Dashboard right column — the manager chat

**Files:**
- Modify: `bin/dashboard.html`

**Interfaces:**
- Consumes: `GET /api/manager/chat`, `POST /api/manager/chat`, `POST /api/manager/reset` (Task 7); mounts into `#manager-chat` from Task 9

- [ ] **Step 1: Build the panel**

```js
function renderChat(payload) {
  const panel = document.getElementById("manager-chat");
  if (document.activeElement === panel.querySelector(".chat-input")) {
    renderChatLog(payload);   // never rebuild the composer under the cursor
    return;
  }
  const input = el("textarea", { class: "chat-input", rows: "3", placeholder: "Giao việc, đổi ưu tiên, hỏi tiến độ…" });
  const send = el("button", { class: "btn btn-primary" }, "Gửi");
  send.addEventListener("click", () => sendChat(input));
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendChat(input); }
  });
  const reset = el("button", { class: "btn btn-quiet" }, "Phiên mới");
  reset.addEventListener("click", async () => {
    if (!confirm("Bắt đầu phiên manager mới? Lịch sử chat sẽ không được nạp lại.")) return;
    await fetch("/api/manager/reset", { method: "POST" });
  });
  panel.replaceChildren(
    el("div", { class: "chat-head" }, [el("h2", {}, "Manager"), reset]),
    el("div", { class: "chat-log", id: "chat-log" }),
    el("div", { class: "chat-compose" }, [input, send]),
  );
  renderChatLog(payload);
}

function renderChatLog(payload) {
  const log = document.getElementById("chat-log");
  if (!log) return;
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const rows = (payload.entries || []).map(e =>
    el("div", { class: `msg msg-${e.role} ${e.source !== "cto" ? "msg-auto" : ""}` }, [
      ...(e.source !== "cto" ? [el("div", { class: "msg-why" }, sourceLabel(e.source))] : []),
      el("div", { class: "msg-text" }, e.text || ""),
    ]));
  if (payload.busy) rows.push(el("div", { class: "msg msg-thinking" }, "Manager đang xử lý…"));
  log.replaceChildren(...rows);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function sourceLabel(source) {
  return {
    "daemon:escalation": "escalation từ worker",
    "daemon:worker-finished": "worker vừa xong",
    "daemon:tick": "tự rà soát định kỳ",
  }[source] || source;
}

async function sendChat(input) {
  const text = input.value.trim();
  if (!text) return;
  input.disabled = true;
  const res = await fetch("/api/manager/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  input.disabled = false;
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(res.status === 503 ? "Manager đang bận, thử lại sau." : `Lỗi ${res.status}: ${body.error || ""}`);
    return;   // the typed text stays in the box so nothing is lost
  }
  input.value = "";
  pollChat();
}

async function pollChat() {
  try {
    const res = await fetch("/api/manager/chat");
    renderChat(await res.json());
  } catch (e) {
    /* keep the last render; the next tick retries */
  }
}
```

Three behaviours that matter and are easy to lose: the composer is never rebuilt while it holds focus (typing would be destroyed on the next poll tick, the same class of bug already fixed for the dispatch form); a failed send keeps the typed text; and the log only auto-scrolls when the reader is already at the bottom.

`msg-auto` is styled distinctly from a reply to the CTO — that tint is how proactive work stays visible rather than silent. `msg-text` uses `white-space: pre-wrap` so the manager's line breaks survive, and its value is set with `textContent` because model output is untrusted.

- [ ] **Step 2: Poll it**

Start `pollChat()` on load and on the existing fast interval alongside the other pollers.

- [ ] **Step 3: Verify end to end against a real manager**

```bash
python3 bin/dashboard.py 4455 . & sleep 2
```

Open `http://127.0.0.1:4455/`, type `Trả lời ngắn: bạn là ai và bạn quản lý gì?` and send. Confirm: the CTO turn appears immediately; "Manager đang xử lý…" shows while it runs; the reply appears within a minute; `~/.claude/hermes/manager-session.json` now holds a `session_id`; a second message resumes the same id rather than creating another. Then confirm typing survives a poll tick by typing without sending and waiting ten seconds. Kill the server.

- [ ] **Step 4: Commit**

```bash
git add bin/dashboard.html
git commit -m "feat: chat with the manager from the dashboard"
```

---

### Task 11: Documentation and full regression

**Files:**
- Modify: `README.md`
- Modify: `skills/parallel-worktree-run/SKILL.md`

- [ ] **Step 1: Update the README**

Replace the `bin/escalations.py / manager.py / manager_daemon.py` bullet in "What's in here" with an accurate description of the persistent manager, the ledger, and `manager_session.py`. Add a "Manager" section covering: starting `dashboard.py` and `manager_daemon.py` together, what the chat does, where the ledger and session state live (`~/.claude/hermes/`), the `PWT_MANAGER_MODEL` / `PWT_MANAGER_EFFORT` / `PWT_MANAGER_TICK_SECONDS` variables, and the routing rubric table. Update the Dashboard section to describe the two-column layout. Document `dispatch --model` / `--effort` in the usage block.

- [ ] **Step 2: Update the worktree skill's cross-reference**

In `skills/parallel-worktree-run/SKILL.md`, update Step 6 so it describes the persistent manager rather than the short-lived Fable one, and point at `skills/engineering-manager/SKILL.md`. Update the `dispatch` line in Quick reference to show the two new flags. Change nothing else in that file.

- [ ] **Step 3: Run every suite**

```bash
cd bin && python3 test_assignments.py && python3 test_manager_session.py && python3 test_manager.py && python3 test_escalations.py && python3 test_dashboard.py && cd .. && bash bin/test_parallel_task.sh
```

Expected: every suite prints its `<n> passed` line with no failures.

- [ ] **Step 4: Confirm the live stack still starts**

```bash
python3 bin/dashboard.py 4455 . & sleep 2
curl -s -o /dev/null -w 'dashboard %{http_code}\n' localhost:4455/
for p in /api/tasks /api/escalations /api/assignments /api/manager/chat /api/ado-tickets; do
  curl -s -o /dev/null -w "$p %{http_code}\n" "localhost:4455$p"
done
kill %1
bash -n bin/parallel-task.sh && bin/parallel-task.sh list
```

Expected: `200` for the page and for all five routes; `parallel-task.sh list` prints the existing copies.

- [ ] **Step 5: Commit**

```bash
git add README.md skills/parallel-worktree-run/SKILL.md
git commit -m "docs: document the Engineering Manager, the ledger, and worker sizing"
```
