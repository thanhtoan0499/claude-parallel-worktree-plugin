# parallel-worktree-run

Claude Code plugin: spin up N independent, fully running copies of a repo — each its own
git worktree, branch, dev stack, and ports — from a single Claude Code session, and dispatch
background sessions to implement tasks inside them.

Extracted from a working setup in a monorepo (aiquinta-platform); see
[Prerequisites](skills/parallel-worktree-run/SKILL.md#prerequisites--repo-layout-this-plugin-assumes)
in the skill for the repo layout `bin/dev-stack.sh` / `bin/dev-native.sh` expect
(`deploy/docker-compose.yml` with per-service `${..._HOST_PORT}` vars, pnpm + uv workspaces).
If your repo doesn't match, `bin/*.sh` is a reference implementation — adjust the `up`/`down`
command bodies; the worktree/slot/registry orchestration in `bin/parallel-task.sh` doesn't
need to change.

## Install

```
claude plugin install https://github.com/thanhtoan0499/claude-parallel-worktree-plugin
```

or add as a marketplace source and install by name — see Claude Code plugin docs.

## What's in here

- `skills/parallel-worktree-run/SKILL.md` — the skill Claude Code loads to drive the workflow.
- `bin/parallel-task.sh` — orchestrator: `start <task> <native|docker>`, `dispatch`, `list`, `stop`, `rm`.
  Wraps `git worktree add` + slot/port allocation + a JSON registry
  (`.claude/worktrees/.parallel-registry.json` in the target repo).
- `bin/dev-stack.sh` — docker-compose slot runner. Slot *N* offsets every host port by
  `N*100`: gateway `8081+N*100`, frontend `5173+N*100`, postgres `5432+N*100`,
  azurite `10000+N*100`, redis `6379+N*100`. Compose project name `<app>-s${N}`.
- `bin/dev-native.sh` — native (host-run, hot-reload) mode: task *N* → gateway `8500+N`,
  frontend `5500+N`, shared Postgres/Azurite across native tasks.
- `bin/dashboard.py` / `bin/dashboard.html` — local web dashboard (see Dashboard below):
  status + live log per running copy, reading `parallel-task.sh list --json` and each task's
  session transcript. Stdlib-only, no build step.
- `bin/manager_session.py` — the persistent Engineering Manager: one long-lived Claude Code
  session behind a single serialized entry point, so the CTO's chat, every escalation, and every
  daemon wake land in the same conversation instead of a fresh model call each time. State lives
  in `~/.claude/hermes/` (see Manager below).
- `bin/assignments.py` — the assignment ledger (`~/.claude/hermes/assignments.jsonl`): one record
  per outcome the CTO asked for, carrying the manager's plan and progress.
- `bin/escalations.py` / `bin/manager.py` / `bin/manager_daemon.py` — the escalation path: a
  worker appends what it cannot decide to a queue, `manager_daemon.py` classifies each record and
  puts it to the persistent manager session, and only irreversible or genuinely ambiguous calls
  reach you in the dashboard. The daemon also wakes the manager when a dispatched worker finishes
  and on a periodic tick. Every automatic decision is logged where you can audit it.
- `skills/engineering-manager/SKILL.md` — the manager's charter: the eight actions it owns, the
  ledger schema, how it dispatches and sizes work, when it escalates instead of deciding. Doubles
  as the text prepended to the session's first message, so the documented role and the enacted
  role can't drift apart.

All four are added to `PATH` while the plugin is active — the skill invokes them by bare
name (`parallel-task.sh`, `dev-stack.sh`, `dev-native.sh`, `dashboard.py`).

## Usage

From a Claude Code session at your repo root, just ask for two things in parallel — the
skill's trigger phrases include "song song" / "chạy song song" / "in parallel" / "multiple
live previews". Or drive it directly:

```bash
parallel-task.sh start my-task docker   # or: native
parallel-task.sh dispatch my-task "<prompt>" --model opus --effort max   # both flags optional
parallel-task.sh list
parallel-task.sh stop my-task
parallel-task.sh rm my-task             # --force to discard uncommitted changes
```

## Dashboard

Two columns. Left is what the CTO reads: decisions awaiting a human, active assignments with
progress and at-risk flags, and the ADO backlog for reference. Right, full height, is the
Engineering Manager chat — see Manager below. The old single-page operator view (sessions table,
session details, live logs) is still there, collapsed underneath, unchanged in content:

```bash
dashboard.py            # http://127.0.0.1:4400
dashboard.py 5000        # custom port
```

Lists each task's dev-stack status (`dev_status`, from the docker/native port checks) alongside
its dispatched session's live status (`agent_status`: `idle`/`busy`, from `claude agents --json`),
and streams that session's transcript as a readable log — click a task to follow it.

If you edit `bin/dashboard.py`, restart the process. `dashboard.html` is re-read from disk on
every request, but the Python answering `/api/*` is loaded once at start — an old process serving
the new page shows up in the browser as "Unexpected end of JSON input", not an obvious version
mismatch.

## Manager

Run the daemon alongside the dashboard. The dashboard alone only serves pages and takes CTO input
— nothing wakes the manager without the daemon:

```bash
dashboard.py             # http://127.0.0.1:4400
manager_daemon.py        # watches escalations, wakes the manager on worker-finish + tick
```

Skip the daemon and there is no periodic tick and no worker-finished wake. A mechanical
escalation is never decided at all — nothing calls the code that would settle it or deliver an
answer back, so the worker that filed it blocks indefinitely. A tier-3 escalation is classified
and shown in the dashboard's decision panel the moment someone loads the page (`dashboard.py`
does that live, daemon or not) — but nothing pushes a notification, so it waits for a human who
is never told to look.

The same gap catches a decision you already made: answering a card (`POST
/api/escalations/<id>/answer`) only records the answer, it does not deliver it — delivery is the
daemon's job too. The card leaves the decision panel looking resolved while the worker stays
blocked. Nothing is lost, though — the daemon delivers every answered-but-undelivered record as
soon as it next starts.

The chat, right column of the dashboard, talks to `bin/manager_session.py`: one long-lived Claude
Code session, not a fresh model call per message. State an outcome ("ship the ledger fix by
Friday, P1") and the manager plans it, sizes each step, and dispatches workers itself via
`parallel-task.sh`, remembering across the whole conversation what it dispatched and what it was
told. Ask it for status and it reads the ledger and live session state back to you. Its charter —
the eight actions, the ledger schema, the escalation rubric — is
`skills/engineering-manager/SKILL.md`; the same file is prepended to the session's first message,
so the documented role and the enacted role can't drift apart.

One known rough edge: the manager's answer to an escalation currently appears in the chat as its
raw decision JSON (`{"answer": ..., "reason": ..., "confidence": ...}`), not a formatted sentence
— expected for now, not a bug.

State lives in `~/.claude/hermes/`:
- `manager-session.json` — the session id the manager resumes on every call
- `manager-chat.jsonl` — the conversation the dashboard renders
- `manager.lock` — serializes the CTO's chat, escalations, and daemon wakes onto the one session
- `escalations.jsonl` — the queue a worker appends to and `manager_daemon.py` watches; the file
  behind the daemon warning above
- `manager-seen-sessions.json` — last-known status per worker session, so a worker-finished wake
  fires once per transition, not on every daemon pass
- `assignments.jsonl` — the ledger: one record per outcome, with the manager's plan and progress

| Variable | Default | Controls |
|---|---|---|
| `PWT_MANAGER_MODEL` | `claude-opus-5` | Model the manager session itself runs on |
| `PWT_MANAGER_EFFORT` | `max` | Effort level for the manager session |
| `PWT_MANAGER_TICK_SECONDS` | `1800` | Minimum gap between `daemon:tick` wakes (only fires while an assignment is open or a worker is running) |
| `PWT_REGISTRY` | `.claude/worktrees/.parallel-registry.json` resolved against the daemon's own cwd | Worktree registry the daemon reads to scope worker-finished wakes |

`PWT_REGISTRY` matters whenever `manager_daemon.py` doesn't start with the repo root as its
working directory — cron, systemd, any launcher with its own cwd. The daemon resolves the registry
path relative to itself, not the repo, so a wrong cwd doesn't error — it just silently degrades to
zero worker-finished wakes. Run the daemon from the repo root, or set `PWT_REGISTRY` to the
absolute registry path.

The manager's `claude` subprocess also runs with the repo root as its working directory, and that
is where Claude Code resolves its permission settings from — a manager started from the plugin's
own directory (or any other cwd) can silently lose the tool access the target repo grants.
`dashboard.py` resolves this from the repo it's serving (an explicit argv path, else its own cwd);
`manager_daemon.py` takes no repo argument and serves no repo, so it always falls back to its own
cwd. `PWT_REPO_ROOT` overrides both and wins over either's own resolution.

The manager sizes each worker instead of dispatching everything on one model:

| Work | Model | Effort |
|---|---|---|
| Complex — multi-file design, security or concurrency, genuinely ambiguous requirements | `opus` | `max` |
| Medium — integration across a few files, pattern matching, debugging a known failure | `sonnet` | `high` |
| Simple — single file, mechanical change, the brief already contains the code to write | `sonnet` | `medium`, or `low` for pure transcription |

Which tier a task belongs to is the manager's judgment, not code — `dispatch --model` /
`--effort` (Usage, above) only validates the values (`--effort` must be one of `low`, `medium`,
`high`, `xhigh`, `max`).

## Troubleshooting

See the [Troubleshooting section](skills/parallel-worktree-run/SKILL.md#troubleshooting) in
the skill — covers a `start` failing partway (auto-rolled-back since v1.0.0), port collisions
from an unoffset compose service, and how to verify a copy's isolation actually held (not
just that its frontend returns 200).
