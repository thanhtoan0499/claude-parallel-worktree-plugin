# Engineering Manager — design spec

Date: 2026-08-28
Status: approved (design), pre-implementation
Supersedes parts of: `2026-08-25-manager-tier-design.md` (the stateless per-escalation manager)

## Problem

The plugin can dispatch autonomous sessions into isolated worktrees, watch them, and route their
escalations. What it cannot do is be **managed**. The CTO still has to open a terminal session to
talk to whoever is coordinating, assign work ticket by ticket through a per-row button, and chase
each worker themselves.

Two concrete symptoms in today's build:

**There is no one to talk to.** `manager.py` spawns a fresh model for each escalation, feeds it one
record, reads one JSON decision, and throws the process away. That is a classifier, not a manager.
It has no memory of what it decided ten minutes ago, no idea what work is in flight, and no inbox.
The dashboard has no chat.

**The UI embodies the anti-pattern.** The Tickets panel renders one "Giao việc" button per ADO
ticket, which is precisely the CTO assigning work ticket by ticket. The audit feed of decisions made
on the CTO's behalf sits at the bottom of the page inside a collapsed section. There is no panel for
the one thing the CTO actually owns: decisions that need them.

The goal is a single point of contact. The CTO states an outcome; the manager decomposes, dispatches,
chases, and reports; only genuine decisions come back up.

## The eight actions

The CTO's surface is eight verbs. Most of them are not code — a capable session performs them given
the right charter and the right state. Building eight endpoints and eight buttons would rebuild the
bureaucracy this spec exists to remove.

| Verb | What the CTO does | What must be built |
|---|---|---|
| Assign | States an outcome, priority, deadline | Ledger record |
| Plan | Reads back the manager's breakdown | Ledger `plan` field |
| Review | Approves a plan or an output | Nothing — existing tier-3 escalation |
| Status | Asks how it is going | Nothing — ledger renders it |
| Blocker | Receives what needs deciding | Nothing — existing `classify()` tier-3 |
| Reprioritize | Changes priority, scope, or deadline | Ledger record update |
| Follow-up | Nothing; the manager chases | Wake trigger |
| Report | Reads a daily or weekly summary | Wake trigger |

Three things get built: a persistent manager session, an assignment ledger, and wake triggers.
Everything else is charter text.

## Scope

**In:** persistent manager session with one serialized entry point; assignment ledger; daemon wake
on worker completion and on a periodic tick; chat panel and assignment panel in the dashboard;
inline decision buttons; a charter skill.

**Out, deliberately:** a button per verb (chat covers them); ADO write-back (unchanged decision —
sessions handle their own ADO comments); dependency graph or Gantt rendering; token-level streaming;
making `dev-stack.sh` portable to other repo shapes (separate spec).

## Architecture

One Claude Code session, long-lived, identified by a `session_id` kept in a state file. Everything
reaches it through exactly one function.

The mechanism is verified, not assumed. Spikes run 2026-08-28, settled:

- `claude --resume <id> -p "<msg>" --output-format json` preserves conversation context across
  calls and returns **the same `session_id`**, so repeated resumes form one continuous conversation.
- A resumed headless session executes Bash with no permission denial (`permission_denials: []`),
  so the manager can act, not only talk.

Four feeds reach that one entry point, each tagged so the UI can tell them apart:

| Source tag | Sends |
|---|---|
| `cto` | What the CTO typed in the chat panel |
| `daemon:escalation` | An escalation's prompt, built exactly as today by `build_prompt()` |
| `daemon:worker-finished` | "Worker `<task>` finished. Check its work and update the ledger." |
| `daemon:tick` | "Tick. Chase open assignments; report if none in 24h." |

Because all four land in one session, the manager remembers: it knows it dispatched a worker twenty
minutes ago, what it decided for an earlier escalation, and what the CTO told it this morning. The
stateless manager knows none of this.

**Serialization.** Two processes resuming one session would race on the transcript. Every call goes
through `manager_session.ask()`, which holds an `flock` for the duration. The OS provides the queue;
no separate queue is built.

**The tier-3 rail is unchanged.** `classify()` still runs *before* the manager is consulted, and a
tier-3 record still goes straight to the human without a model call. Making the manager persistent
must not widen what it is allowed to decide.

## Components

### `bin/manager_session.py` (new)

State lives beside the existing queue in `~/.claude/hermes/`:

- `manager-session.json` — `{"session_id": "...", "started_at": <float>}`
- `manager-chat.jsonl` — append-only conversation, what the UI renders
- `manager.lock` — the `flock` file

Public surface:

- `ask(text, source, timeout=600) -> str` — acquire the lock; bootstrap if no session exists; run
  `claude --resume <id> -p <text> --output-format json --model <model>`; append the outgoing turn
  and the reply to the chat log; release; return the reply text.
- `history(limit=200) -> list[dict]` — chat entries for the UI.
- `busy() -> bool` — whether the lock is currently held; drives the "thinking" indicator.
- `reset() -> None` — delete `manager-session.json` so the next `ask()` bootstraps a fresh session.

**Bootstrap.** With no saved session, the first call runs `claude -p "<charter>\n\n<text>"` and saves
the `session_id` from the JSON reply. The charter is prepended to the first *user* message rather
than passed as a system-prompt flag, because `--resume` replays the transcript: a charter inside the
transcript is re-read on every future call, while a flag passed once at bootstrap is not.

**Chat log entry shape:**

```json
{"ts": 1756339200.0, "role": "cto|manager", "source": "cto|daemon:escalation|daemon:worker-finished|daemon:tick", "text": "..."}
```

`role` says who spoke; `source` says what prompted the exchange. An entry with `role: "manager"` and
a `source` other than `cto` is the manager speaking unprompted — the UI tints those differently so
proactive work is visible rather than silent.

**Model.** `claude-opus-5` at `--effort max`, overridable by `PWT_MANAGER_MODEL` and
`PWT_MANAGER_EFFORT`. The old `claude-fable-5` was sized for classifying one record against a
rubric; this session now plans, decomposes, sizes and routes every piece of work, and writes
reports. It is one session for the whole team, so it is the wrong place to economize — a bad
routing decision here costs more than the manager's own tokens ever will.
`manager.py`'s `MANAGER_MODEL` constant moves here and takes the new default. Both flags are passed
on every call, including resumes, because `--effort` applies to the invocation rather than to the
stored session.

**Failure handling.** A timeout or non-zero exit appends an entry with `role: "manager"` and text
naming the failure, so a lost message is visible in the chat rather than silently dropped. The lock
is acquired with a 30-second timeout; a caller that cannot get it raises, and the HTTP layer turns
that into `503 {"error": "manager busy"}`.

### `bin/assignments.py` (new)

`~/.claude/hermes/assignments.jsonl`, append-only, folded by `id` — the same idiom as
`escalations.py`, reusing its `append` / `read_all` / `current_state` shape rather than inventing a
second one.

One record is one thing the CTO asked for:

| Field | Written by | Value |
|---|---|---|
| `id` | system | `uuid4().hex[:12]` |
| `ts` | system | epoch float |
| `title` | CTO | The outcome, in the CTO's own words |
| `priority` | CTO | `"P0"`, `"P1"`, or `"P2"` |
| `deadline` | CTO | ISO-8601 date string, or `null` |
| `ado_refs` | manager | `[{"id": "8148", "url": "https://..."}]` — the canonical shape already used across the dashboard |
| `status` | manager | `"assigned"`, `"in_progress"`, `"blocked"`, `"done"`, `"cancelled"` |
| `plan` | manager | List of steps (below); `[]` until the manager has planned |
| `note` | manager | Latest one-line update |

A plan step:

```json
{"step": "Add the ledger module", "owner": "ledger-module", "depends_on": [], "eta": "2026-08-29", "state": "todo|doing|done"}
```

`owner` is a task name in the worktree registry. That field is the join between the manager's
notebook and the running worktrees.

**There is no `planned` status and no mandatory review gate.** Not every plan needs the CTO's eyes,
and forcing approval on all of them recreates the per-ticket babysitting this design removes. When
the manager judges that a plan does need review, it files a normal escalation and the existing
tier-3 path renders it.

Two derived values, both pure functions, so they are testable without I/O and can never go stale:

```python
def at_risk(rec, now):
    """Open, and either its deadline or an unfinished step's ETA has passed."""
    if rec.get("status") in ("done", "cancelled"):
        return False
    if _date_passed(rec.get("deadline"), now):
        return True
    return any(
        _date_passed(s.get("eta"), now)
        for s in (rec.get("plan") or [])
        if s.get("state") != "done"
    )


def progress(rec):
    """Fraction of plan steps done, or None when there is no plan yet."""
    steps = rec.get("plan") or []
    if not steps:
        return None
    return sum(1 for s in steps if s.get("state") == "done") / len(steps)
```

`progress()` returns `None` rather than zero when unplanned, and the UI draws no bar in that case.
The number is always "steps done over steps planned" — never an invented percentage.

### `bin/parallel-task.sh` (modified) — sizing each worker

The manager does not dispatch every worker on the same model. It sizes the work first and routes
accordingly, because a mechanical single-file edit and an ambiguous concurrency change do not
deserve the same spend.

`cmd_dispatch` gains two optional pass-through flags, forwarded to `claude --bg`:

```bash
parallel-task.sh dispatch <task> "<prompt>" [--model <model>] [--effort <level>]
```

Both are validated by a pure `parse_dispatch_args` function, in the same testable shape as the
existing `parse_start_args`: `--effort` must be one of `low`, `medium`, `high`, `xhigh`, `max`, and
a flag given without a value is an error rather than a silently swallowed next argument. Omit both
and behaviour is exactly as today. The chosen values are written into the registry entry so the
dashboard can show what each worker is costing.

The routing rubric itself is charter, not code — the manager applies judgement per task:

| Work | Model | Effort |
|---|---|---|
| Complex: multi-file design, security or concurrency, genuinely ambiguous requirements | `opus` | `max` |
| Medium: integration across a few files, pattern matching, debugging a known failure | `sonnet` | `high` |
| Simple: single file, mechanical change, the brief already contains the code to write | `sonnet` | `medium`, or `low` for pure transcription |

Code enforces only the value ranges; which tier a task belongs to is the manager's call, and the
charter requires it to state the tier and its reason when it dispatches, so a wrong routing decision
is visible in the chat rather than buried.

### `bin/manager_daemon.py` (modified)

The existing pass over the escalation queue is unchanged in shape. Two things change and two are
added.

**Changed:** `run_manager` is replaced by a call to `manager_session.ask(build_prompt(record),
source="daemon:escalation")`. The prompt building, `parse_decision`, `validate_decision`, the
low-confidence degrade, and the whole `_try_deliver` path stay exactly as they are — only the
process that answers changes from a fresh throwaway to the persistent session.

**Changed:** `manager.py` loses `manager_argv` and `run_manager`; `resume_argv` and `deliver_answer`
stay, because delivering an answer into a *worker* session is still a separate concern.

**Added — worker completion.** Each pass reads `claude agents --json --all` and compares the status
of every session named in the worktree registry against `~/.claude/hermes/manager-seen-sessions.json`.
A session that moves to `idle`, or disappears from the list entirely, fires one
`daemon:worker-finished` wake and its new status is recorded, so the same transition never fires
twice. A session absent from the file on first sight is recorded without firing — a daemon restart
must not re-announce work that finished days ago.

**Added — periodic tick.** Every `PWT_MANAGER_TICK_SECONDS` (default `1800`), if there is at least
one assignment whose status is not `done` or `cancelled`, or at least one running worker, fire one
`daemon:tick` wake. With nothing open, no tick is sent — an idle dashboard must not burn tokens.

What the manager does on a tick is charter, not code: chase stalled steps, update the ledger, and
write a report if it has not written one in 24 hours.

### `skills/engineering-manager/SKILL.md` (new)

The charter. One file serving two purposes: a plugin skill Claude Code loads, and the text prepended
to the manager session's first message. A single source, so the documented role and the enacted role
cannot drift.

Contents: the eight verbs and what each obliges the manager to do; the ledger's location and schema;
how to dispatch work (`parallel-task.sh start` then `dispatch`, never editing repo files directly);
the model and effort routing rubric above, with the requirement to state the chosen tier and its
reason when dispatching; when to file an escalation instead of deciding; and the hard boundaries
carried over from the
existing orchestrator role — never `git push`, never open a PR, never touch `main`, never edit
repository files for ticket work, never answer a permission prompt on a human's behalf.

## HTTP surface

Existing routes are unchanged: `GET /api/tasks`, `GET /api/tasks/<name>/log`,
`GET /api/escalations`, `GET /api/ado-tickets`, `POST /api/escalations/<id>/answer`. The server is
already `ThreadingHTTPServer`, so a slow manager call cannot block other requests. The existing
`Origin` CSRF guard and body-size cap apply to every new POST below without modification.

**New:**

- `GET /api/manager/chat` → `{"entries": [...], "busy": true|false}`
- `POST /api/manager/chat` `{"text": "..."}` → appends the CTO turn to the chat log, starts a
  background thread running `ask(text, "cto")`, and returns `202 {"ok": true}` immediately. The UI
  polls the GET route for the reply. No HTTP request is ever held open for a model call.
- `POST /api/manager/reset` → `{"ok": true}`
- `GET /api/assignments` → each record plus derived `at_risk` and `progress`
- `POST /api/assignments` `{"title", "priority", "deadline", "ado_refs"}` → appends the record and
  wakes the manager with the assignment, in a background thread as above

**Removed:** `POST /api/tickets/dispatch`, along with `_build_dispatch_argv`, `_run_dispatch`,
`_build_dispatch_prompt`, `_ticket_task_slug`, `_parse_ticket_ids` and their tests. The route
provisioned a worktree straight from a ticket, which is the CTO assigning work to an engineer. Its
replacement is `POST /api/assignments`: the CTO states the outcome, and the manager decides whether
it needs one worktree, three, or none, provisioning them by running `parallel-task.sh` itself. A
dead HTTP route kept "just in case" is worse than a deleted one; the behavior is not lost, it moves
to the party that should own it.

## User interface

Two columns. The left is what the CTO reads, the right is how the CTO speaks.

| Position | Panel | Serves |
|---|---|---|
| Left, top | **Needs your decision** — tier-3 escalations, each with buttons rendered from the record's `options` | Review, Blocker |
| Left, middle | **Active assignments** — title, priority, deadline, progress bar, plan steps with owners, at-risk flag | Status, Plan |
| Left, bottom | **ADO backlog** — reference only, each row with an assign action | Context |
| Right, full height, sticky | **Manager chat** — history, composer, thinking indicator | Assign, Reprioritize, Status, Report |
| Collapsed, unchanged | Sessions table, session details, live logs, stat internals | Operations |

Four counters across the top: assigned, at risk, awaiting you, done this week. The current counters
(sessions, working now, idle or done, blocked) count sessions, which is the engineer's tier, not the
CTO's; they move into the collapsed operations section. The page title changes from "Runtime health
& logs" — which describes the collapsed section rather than the page — to "Engineering Manager",
with the existing header lockup ("Agent Control Center") retained above it.

**Decision buttons are nearly free.** Escalation records already carry `options`, `validate_decision`
already requires the answer to be one of them, `POST /api/escalations/<id>/answer` already exists,
and `manager_daemon` already delivers an `answered` record back into the blocked worker. Only the
buttons are missing: each renders one `options` entry and POSTs it with `decided_by: "cto"`.

**The audit feed moves into the chat.** "Decided for you" stops being its own panel at the foot of a
collapsed section — where it was invisible — and becomes tinted lines in the chat, because a decision
made on the CTO's behalf is the manager talking to them. One panel fewer, and the record is where it
will actually be read.

**The assign action on an ADO row** creates an assignment and notifies the manager. It no longer
provisions anything directly.

`dashboard.html` keeps its existing conventions without exception: vanilla JS, no framework, no
`innerHTML` with untrusted data — every value goes through `textContent` or the `el()` helper. Chat
text is untrusted (it contains model output and worker-authored escalation text) and is bound the
same way.

## Errors and degradation

Every failure degrades toward "tell the human", never toward guessing.

- Manager call times out or exits non-zero: a failure line appears in the chat; the escalation
  record follows the existing `needs_human` path.
- Lock unavailable within 30 seconds: `503 {"error": "manager busy"}`; the UI shows it and keeps the
  composed text so nothing typed is lost.
- `claude agents --json --all` fails during a pass: worker-completion detection is skipped for that
  pass and the escalation pass still runs. Subprocess errors are caught as
  `(OSError, subprocess.SubprocessError, json.JSONDecodeError)` — the shared tuple already used in
  `dashboard.py`, which covers `TimeoutExpired`, a class of bug this codebase has now hit four times.
- Ledger file missing or holding a corrupt line: treated as the escalation queue already treats it —
  unreadable lines are skipped, not fatal.
- Manager session lost or corrupt: the next `ask()` bootstraps a new one. Nothing important is lost,
  because commitments live in the ledger rather than in conversation memory.

## Testing

The existing idiom continues: plain `assert` functions named `test_*`, collected and run by a
`__main__` block, no test framework. New files `bin/test_manager_session.py` and
`bin/test_assignments.py`; `bin/test_dashboard.py` and `bin/test_manager.py` are extended.

Every impure boundary is injected so the logic is testable without spawning a model, exactly as
`decide(record, ask_model)` already does.

Required coverage:

- `at_risk` — past deadline; past step ETA; past ETA on an already-done step (must be false); done
  and cancelled records (must be false); no deadline and no ETAs.
- `progress` — no plan returns `None`; partial plan; fully done plan.
- Ledger fold — a record appended twice yields only the later state; first-seen order is preserved.
- `ask()` — bootstrap path saves the session id; the resume path reuses it; both turns reach the
  chat log with the right `source`; a raising subprocess writes a failure entry and releases the
  lock.
- Worker-completion detection — busy to idle fires once; the same status twice fires nothing; a
  session unseen on first sight is recorded silently; a failing `claude agents` call does not abort
  the pass.
- Tick gating — no open assignments and no running workers produces no tick.
- `parse_dispatch_args` — both flags parsed; each alone; neither; an unknown effort level rejected;
  a flag given without a value rejected rather than consuming the next argument.
- Routes — `POST /api/manager/chat` returns 202 without waiting on the model; `POST /api/assignments`
  rejects a missing title and an out-of-range priority; the `Origin` guard covers both.

## Known ceilings

Stated rather than hidden, each with its upgrade path.

**Conversation cost grows with length.** `--resume` replays the whole transcript on every call, so
cost rises as the conversation lengthens. Mitigated by the reset control and by commitments living
in the ledger. Upgrade path: have the charter instruct the manager to summarize and reset itself
past a threshold.

**One manager, serialized.** The `flock` means a long manager turn delays the next message. Correct
for one CTO and a handful of workers. Upgrade path: a real inbox with a single consumer, if waiting
ever becomes noticeable.

**Wake triggers are polled, not pushed.** Worker completion is noticed within one daemon pass
(5 seconds); the tick is coarse by design. Sufficient for work measured in minutes.

**Permission prompts.** The spike showed Bash running without denial, but a headless session cannot
answer a prompt if one appears. The manager's charter requires it to report a denial into the chat
rather than retry, so the failure is visible and the human can act.

## Files

| File | Change |
|---|---|
| `bin/manager_session.py` | New — the single serialized entry point |
| `bin/assignments.py` | New — ledger, `at_risk`, `progress` |
| `bin/manager_daemon.py` | Route escalations to the session; add worker-completion and tick wakes |
| `bin/manager.py` | Drop `manager_argv` / `run_manager` and move `MANAGER_MODEL` to `manager_session.py` with the new default; keep `resume_argv` / `deliver_answer` and all judgement logic |
| `bin/parallel-task.sh` | `dispatch` gains validated `--model` / `--effort` pass-through, recorded in the registry |
| `bin/dashboard.py` | Add manager and assignment routes; remove `/api/tickets/dispatch` and its helpers |
| `bin/dashboard.html` | Two-column layout, chat panel, assignments panel, decision buttons, new counters |
| `skills/engineering-manager/SKILL.md` | New — the charter, and the session's bootstrap text |
| `bin/test_manager_session.py`, `bin/test_assignments.py` | New |
| `bin/test_parallel_task.sh` | Extended with `parse_dispatch_args` cases |
| `bin/test_dashboard.py`, `bin/test_manager.py` | Extended; tests for removed helpers deleted |
| `README.md` | Document the manager session, the ledger, and the new dashboard |
