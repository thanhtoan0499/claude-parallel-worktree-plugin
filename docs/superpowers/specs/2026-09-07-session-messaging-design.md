# Manager as a session, board as an artifact — design spec

Date: 2026-09-07
Status: draft (design), pre-implementation

Supersedes the first draft of this file (commit f4ef480), which assumed the ledger stays and no
artifact exists. Both assumptions were wrong; see Decisions below.

## Problem

Two things in this plugin are shaped by a constraint that has since expired.

**The manager is a subprocess, not a session.** Every turn spawns `claude -p --resume <id>`. The
session id is stable — the manager genuinely is one long conversation, as
`2026-08-28-engineering-manager-design.md` specified — but the process is not, and `--resume`
replays the whole transcript each call. Measured 2026-09-07 on the live manager: 37 MB / 65,009
lines of transcript, and 4m30s to answer the single word "pong". Worse, `--resume` only lands on a
session that has **stopped**, so a worker heading down the wrong path cannot be redirected until it
finishes.

**The board is a hand-built web server.** `dashboard.py` (720 lines) + `dashboard.html` (1000+
lines) on port 4400, a process to keep alive, restarted by hand after every edit, reachable only
from localhost. Three of the bugs fixed on 2026-09-07 came from that code existing at all: a single
shared cache lock that froze every endpoint behind one slow `az` call, a duplicate top-level `const`
that made the whole page render blank, and a background process silently reaped.

The 2026-08-25 spec recorded the constraint that produced the first shape:

> **There is no `claude send` subcommand.** No supported way to push a message into a *live*
> session from outside. This is why the manager is spawned fresh per report rather than kept
> running: a long-lived manager would need exactly the mechanism that does not exist.

Correct for the CLI, and still correct. No longer correct for the harness: a session now has
`ListAgents` + `SendMessage`, and `Artifact` + `db`. The premise both shapes rest on has expired.

## Decisions

Taken with the user 2026-09-07, in order, each one changing what came after:

1. **The goal is a native architecture, not a benchmark.** `claude -p --resume` was a workaround for
   a missing mechanism. The mechanism exists now, so use it — even where the numbers barely move.
   Latency is a side effect, not the justification.
2. **The local ledger goes.** `escalations.jsonl` / `assignments.jsonl` were a file queue standing
   in for messaging. Workers talk to the manager directly.
3. **Durable state does not go — it moves.** Dropping local files does not drop the need for a
   record. It becomes the artifact's `db`: server-side, readable by Claude via `read_db`, and not a
   local-file workaround. This is what makes (2) safe rather than lossy.
4. **The board becomes an artifact.** No self-hosted dashboard. The manager publishes a page once
   and keeps its data current.
5. **No chat sidebar.** `claude attach <manager-id>` is a better chat client than anything built
   here — streaming, tool calls, slash commands, interrupts. The dashboard keeps only what a
   terminal cannot do: the board.

## Verified feasibility (spikes run 2026-09-07 — settled, do not re-test)

Run with throwaway `claude --bg` sessions in a scratch dir. Every claim is an observed artefact
written by the session under test, not an inference.

**Messaging**

- A `claude --bg` session has `SendMessage` and `ListAgents`.
- An **idle (`state: done`) session receives a message and wakes to act on it.** Target went idle,
  then wrote `RECEIVED|spike-sender|SPIKE1-PING-7fe2` after a message arrived. This is what makes an
  event-driven manager possible at all.
- **Delivery is asynchronous, 10–35 s observed.** `{"success": true, "msg_id": …}` means **queued,
  not processed**. A first check ~0 s after the send found nothing.
- **A message delivered mid-tool costs the target nothing.** Target was inside a 90-second `sleep`;
  it recorded `RECEIVED|…|Step 2 (sleep 90 running in background)` **and** still finished all three
  steps of its own prompt. `--resume` cannot do this at all.
- A second message to the same session, ~12 minutes idle, also landed.
- **A session can sit in `waiting` (blocked on a permission prompt) while messages to it still
  report success.** `ListAgents` exposes the state; only something that looks will notice.
- **A `cmew`/tmux session did NOT appear in `ListAgents`** ~4 min after creation, while every `--bg`
  and interactive session did. Unresolved. **Do not design on cmew** — `--bg` is proven and is
  already what `parallel-task.sh` uses.

**Artifact**

- A `--bg` session has `Artifact`, `CronCreate`, `CronList`, `ScheduleWakeup`, and the
  `artifact-capabilities` skill.
- First publish ~10.7 s; **republish to the same path ~4.6 s and keeps the same URL.**
- This account's capability roster includes `db`, which supports `onSnapshot` — so a page can update
  live without republishing.

## Architecture

```
today                                    proposed
─────                                    ────────
person → :4400 dashboard (Python)        person → claude attach <manager>   (chat)
  → daemon poll 5s                       person → artifact URL              (board)
    → claude -p --resume  (4m30s)
      → manager (cold, replays 37MB)     systemd timer → ensures a "manager" session exists
        → claude --resume (worker must
           be stopped)                   manager (live --bg session, mostly asleep)
worker → *.jsonl → daemon poll             woken by: worker SendMessage · your attach · cron
                                           writes: artifact db
                                         worker → write_db, then SendMessage
                                         page   → onSnapshot(db), live, no polling
```

The manager stops being a subprocess the daemon invokes and becomes a peer that things message. The
page stops being served and becomes published.

### Refresh cadence is deliberately uneven

| Data | Changes | Mechanism |
|---|---|---|
| Escalations, blocked sessions | seconds — and it is the urgent one | worker messages the manager; manager writes db; page updates via `onSnapshot` |
| ADO tickets | hours | cron sweep, 15–30 min |
| Worker liveness | minutes | cron sweep, 10–15 min — catches what nothing pushes |

Making all three realtime would be two-thirds waste. The urgent thing is immediate; the slow things
are scheduled.

## Data model

One artifact, published **once** with `capabilities: {db: {}}`, republished only when the page's
own design changes.

**`sessions/<task-name>`**
```
task, session_id, short_id, state, branch, worktree,
started_at, last_seen, ado_refs[], pr{number, state, url}
```
`state` from `ListAgents`: `running` · `idle` · `waiting` · `done`. `waiting` is new and load-bearing
— nothing today distinguishes a worker blocked on a permission prompt from a working one.

**`escalations/<id>`**
```
ts, session, kind, kind_raw, severity, question, options[],
status, answer, answered_at
```
`kind` is normalised against the twelve-name vocabulary closed on 2026-09-07; `kind_raw` preserves
what the worker actually wrote so drift stays visible. `severity` derives from `kind` + tier.

**`tickets/<ado-id>`**
```
id, title, state, sprint, type, url, pr{number, state}, changed
```
"Not started", "in flight" and "done this sprint" are filters over `state` + `sprint`, not three
collections.

**`meta/status`**
```
last_ado_sweep, last_session_scan, manager_session_id, manager_started_at
```
Required, because the cadences differ. The page must be able to say "ADO as of 12 minutes ago"
rather than implying every number is live. This doubles as the failure indicator — see Errors.

### Who writes what

| Writes | Who | When |
|---|---|---|
| `sessions/` | manager | on a cron sweep, and on any message that reveals a state change |
| `escalations/` | **worker**, then manager updates | worker writes the record, *then* messages |
| `escalations/<id>.answer` | **the page** | when you click an option |
| `tickets/` | manager | cron, 15–30 min |
| `meta/status` | manager | after each sweep |

Two of these rows are not obvious and are explained under Errors: the worker writing before
messaging, and the page writing the answer.

## Manager lifecycle

**The manager does not poll.** A `--bg` session finishes its turn and goes `done`; a polling loop
would be a model turn per tick. It wakes on events instead:

| Wake source | For |
|---|---|
| `SendMessage` from a worker | escalation, work finished |
| Your `claude attach` | assigning work, asking for status |
| Cron | ADO sweep; liveness sweep |

Most of the time it is asleep and costs nothing. That, not the 4m30s, is the real win.

**Something outside must guarantee it exists.** A dead manager cannot be woken — its cron died with
it and messages go nowhere. The smallest form is a systemd timer running roughly:

```bash
claude agents --json --all | jq -e '.[]|select(.name=="manager" and .state!="done")' \
  || claude --bg -n manager -- "$(cat CHARTER.md)"
```

Five lines against `manager_daemon.py`'s 330 — but not zero, and this spec does not pretend
otherwise.

**Restart is no longer amnesia.** Today the manager's memory *is* the 37 MB transcript; losing the
session loses everything. Here the durable state is the db, so a fresh manager reads it back and
reconstitutes: which workers are running, which escalations are open, which tickets are live. The
session becomes working context, not the system of record. This is the second reason the db stays,
independent of auditability.

## Errors and degradation

| Failure | Symptom | Caught by |
|---|---|---|
| Message queued, never processed | `success: true`, then silence | every message with an expected outcome writes a db record; the cron sweep chases overdue ones |
| Worker stuck in `waiting` | message "succeeds", worker frozen | cron reads `ListAgents`, escalates on `waiting` |
| Manager dead | nothing wakes | systemd timer |
| db unwritable | board stops moving | `meta/status` ages — see below |

**Freshness display is the alarm.** The page always shows "ADO as of N minutes ago". N growing *is*
the failure signal; no separate alerting is needed, and none is designed.

### Two holes found while writing this, with their fixes

**A dead manager would swallow escalations.** If `SendMessage` were the only path, a worker
messaging a dead manager gets `success: true` and the escalation is gone — strictly *less* durable
than today, where the file is always written.

Fix: the worker **writes db first, then messages**. The message is a wake signal; the db row is the
record. A manager that was down finds the row when it comes back. Background sessions have the
`Artifact` tool (spiked), so this needs no new infrastructure.

**Clicking an answer does not wake the manager.** The page writes `escalations/<id>.answer`, but a
db write is not a wake, and a page cannot call `SendMessage`. The answer sits until something looks.

Fix: a short cron dedicated to it — a `read_db` for pending answers every 2–3 minutes, far cheaper
than the ADO sweep. In practice this pairs with simply telling the manager over `attach` when you
need it acted on now.

### Degrade, don't fail

- **db unreachable** — `claude.use("db")` resolves `null`. The page must render an explicit "cannot
  reach data" state, never a blank board. (The capability contract requires branching on `null`.)
- **No manager** — the page still reads db and shows the last known state with its timestamp. Stale
  data with an honest clock beats an empty screen.
- **Messaging broken entirely** — `claude -p --resume` remains as the fallback path. Slow, proven,
  still correct.

### Not fixable, only avoided

Message ordering is not guaranteed. Two messages sent close together may be handled in either order,
so anything order-dependent must be a single message.

## Migration

Five steps. Each is useful on its own, and each keeps the old path alive until the new one is proven
**by use, not by a green test**.

1. **Artifact as a read-only mirror.** A scheduled `--bg` session reads today's ledger + ADO and
   writes db; publish the page. The dashboard keeps running. Nothing is removed, both boards can be
   compared, and the db schema gets settled before anything depends on it.
2. **Workers dual-write.** Escalations go to db *and* the ledger. Redundant on purpose: it is the
   only way to compare the two on real data rather than on tests.
3. **Manager becomes a live session.** The one genuinely risky step — `ask_result()` stops returning
   `(ok, text)` and every caller that reads a reply synchronously is rewritten around the db. The old
   daemon stays as the fallback.
4. **Retire the dashboard.** Only after the artifact has been the thing actually opened daily for at
   least a week — not when it merely works.
5. **Retire the daemon and the ledger.** Last.

Every step is reversible by turning the new path off; none needs a revert.

**This deletes most of the 2026-09-07 dashboard work** — sprint/state filters, status and PR chips,
the collapsible done group, the chat model/effort picker. That is accepted: steps 1–3 take weeks and
the dashboard is what gets used meanwhile. The data-gathering logic in `dashboard.py` (two-identity
ADO querying, ticket→PR association, `kind` normalisation) is not lost — it becomes the manager's
job. Only the HTTP and HTML layers go.

## Testing

Session behaviour cannot run in CI: it needs real sessions, real messages, real publishes. Keep the
transport behind a port so decision logic stays testable with a fake sender, matching how `ask` is
already injected in `test_dashboard.py`. One manual end-to-end run per migration step, before that
step's old path is switched off.

## Known ceilings

**Reply latency replaces reply blocking.** 10–35 s to deliver is not instant. It does not grow with
transcript length, which the current 4m30s does.

**Ordering is not guaranteed.** See above.

**`cmew` is unproven.** Not visible to `ListAgents` in the one test run; needs its own spike before
any design depends on it.

**A supervisor outside Claude is still required.** Small, but real, and the one piece that does not
become native.

**Live-session cost is unmeasured.** Whether a resident manager is cheaper than replaying 37 MB
depends on how an idle session is billed. Measure during step 3, not before.

## Files

- `bin/manager_session.py` — rewritten around queued delivery; `--resume` kept as fallback
- `bin/manager_daemon.py` — retired at step 5, replaced by the systemd timer
- `bin/dashboard.py`, `bin/dashboard.html` — retired at step 4
- `skills/engineering-manager/SKILL.md` — escalation protocol: write db, then message
- `docs/superpowers/specs/2026-08-25-manager-tier-design.md` — annotate: the "no mechanism exists"
  finding is superseded for the harness, still true for the CLI
- ledger modules — retired at step 5
