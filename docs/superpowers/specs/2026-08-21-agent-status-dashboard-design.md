# Agent status dashboard — design spec

Date: 2026-08-21
Status: approved (design), pre-implementation

## Problem

`parallel-task.sh` provisions N independent worktree+dev-stack copies. Today the implementing
work inside each one is dispatched as a background `Agent` tool call from the root Claude Code
session (SKILL.md Step 3). That subagent is non-interactive (one-shot, can't be talked to mid-run)
and its lifecycle is tied to the root session. The only visibility into it is
`parallel-task.sh list` (infra-level: branch, mode, ports, up/down) and a task-notification when it
finishes.

The user wants: each task to run as its own independent, addressable top-level Claude Code
session (not a subagent) — and a way to see all running tasks' live status + output in one place
to manage them, without needing a "manager" session to babysit each one via task-notifications.

## Non-goals (V1)

Explicitly out of scope for this iteration — cut per YAGNI, not forgotten:

- Decision-evidence / risk / verification-checklist pages
- Diff review UI
- An approval workflow ("Approve recommendation")
- Health-check pipeline / "safe actions" (restart, etc.)
- An "attach" button in the dashboard UI — `claude attach <id>` from a terminal already covers
  this once the session is addressable; no need to embed a terminal in the browser for V1.
- Viewing a task's log after its worktree is removed (`parallel-task.sh rm`) — the transcript file
  is keyed by `sessionId`, independent of the worktree, so it technically survives `rm`, but V1's
  dashboard only shows tasks still in the registry.

## Feasibility spikes (already run, 2026-08-21)

Two things were verified live against the real `claude` CLI before locking this design in:

1. **Background dispatch**: `claude --bg -n <name> "<prompt>"`, run from inside the target
   worktree directory, starts an independent top-level session and returns immediately. It prints
   `backgrounded · <short-id> · <name>` to stdout. **`--bg` ignores `--session-id`** — it assigns
   its own id, so a UUID cannot be pre-generated and handed in; the short-id must be parsed from
   this launch output.
2. **Status + transcript location**: `claude agents --json --cwd <path>` returns an array
   including, per session: `id` (short), `sessionId` (full UUID), `name`, `cwd`, `kind`
   (`interactive`/`background`), `status` (`idle`/`busy`), `state` (present at least for
   `done`/`blocked`). The session's full transcript is a JSONL file at
   `~/.claude/projects/<cwd-with-slashes-as-dashes>/<sessionId>.jsonl` — confirmed by locating and
   reading one after a real `--bg` launch.

   `claude logs <id>` was also checked and rejected as a log source: it prints the raw ANSI
   terminal-replay stream (cursor positioning, color escapes), not renderable as a plain-text log
   without a terminal emulator. The transcript JSONL is the right source instead.

   The transcript file has more line-type noise than a subagent's transcript (`last-prompt`,
   `custom-title`, `agent-name`, `mode`, `permission-mode`, `atis-latch`, `file-history-snapshot`,
   `attachment` records for hook output, in addition to the real `user`/`assistant` conversation
   turns). A renderer that only acts on lines where `type` is `"user"` or `"assistant"` AND a
   `message.content` array is present naturally skips all of this — no special-casing needed.

## Architecture

```
root Claude session              parallel-task.sh                  dashboard.py (stdlib)      browser
  |  Step 2: start <task> <mode>    | git worktree add, dev-stack up |                            |
  |                                 | (unchanged from today)         |                            |
  |  Step 3: build the task prompt  |                                |                            |
  |  (verbatim requirements, dev    |                                |                            |
  |   URLs, EnterWorktree note,     |                                |                            |
  |   gates — same content as       |                                |                            |
  |   today's Agent-call prompt)    |                                |                            |
  |    dispatch <task> "<prompt>" ->| cd "$wt_path" &&               |                            |
  |                                 |   claude --bg -n "$task" "$p"  |                            |
  |                                 | parse `backgrounded · <id>`    |                            |
  |                                 | claude agents --json           |                            |
  |                                 |   --cwd "$wt_path"             |                            |
  |                                 | -> sessionId                   |                            |
  |                                 | registry[$task] += {short_id,  |                            |
  |                                 |   session_id}                  |                            |
  |                                 |                                 |<- GET /api/tasks ---------|
  |                                 |<-------------------------------- list --json (shells out)   |
  |                                 |                                 -> task table JSON --------->|
  |                                 |                                 |<- GET /api/tasks/<n>/log -|
  |                                 |                                 reads + parses that task's   |
  |                                 |                                 <sessionId>.jsonl            |
  |                                 |                                 -> rendered log lines ------>|
```

## Components

### 1. Registry — 2 new fields per task entry

Alongside the existing `branch`, `path`, `mode`, `num`, `ports`: add `short_id` (from the
`backgrounded · <id> · <name>` line) and `session_id` (full UUID, resolved via `claude agents
--json --cwd <path>` matched by `name` right after dispatch — matching by name rather than
assuming `short_id` is a literal prefix of `session_id`, since that prefix relationship was only
observed once and isn't documented behavior to rely on).

### 2. `parallel-task.sh dispatch <task-name> <prompt>`

New subcommand, runs after `start` (which is unchanged — still does worktree creation + dev-stack
provisioning exactly as today). Replaces SKILL.md Step 3's `Agent` tool call:

```bash
cd "$wt_path" && claude --bg -n "$task" "$prompt"
```

parses the `backgrounded · <short-id> · <name>` line from stdout, then runs `claude agents --json
--cwd "$wt_path"` to resolve `session_id`, and writes both into the registry entry for `$task`.

SKILL.md's Step 3 responsibilities are otherwise unchanged: the orchestrating Claude still builds
the actual prompt text (task requirements verbatim, dev URLs from Step 2, the mandatory
`EnterWorktree` instruction, repo gates, the `--frontend-only` prohibition, the end-of-task summary
ask) — it just hands that prompt to `dispatch` instead of calling `Agent` itself. Launching several
tasks concurrently is still possible (multiple `dispatch` calls), but no longer requires "send them
in one message" — each is a separate, already-backgrounded shell command.

### 3. `parallel-task.sh list --json`

New flag on the existing `cmd_list`. Emits the registry's per-task fields as JSON, plus for any
task with a `session_id`: the live `status`/`state` from `claude agents --json` (looked up by
`session_id`, not re-deriving liveness from docker/port checks for the Claude-session part — the
existing `docker_slot_busy`/`port_busy` checks stay, unchanged, for the dev-stack's own up/down
state, which `claude agents` knows nothing about).

### 4. `bin/dashboard.py`

New script, Python stdlib only (`http.server.ThreadingHTTPServer`, `json`, `subprocess`), added to
`bin/` alongside `dev-stack.sh`/`dev-native.sh` (same PATH convention). Invoked as
`dashboard.py [port]`.

- `GET /api/tasks` → runs `parallel-task.sh list --json`, returns its stdout verbatim.
- `GET /api/tasks/<name>/log` → looks up that task's `session_id` from the registry, computes the
  transcript path (`~/.claude/projects/<slugified-worktree-path>/<session_id>.jsonl`), and if it
  exists: reads it, parses each line, and renders only lines where `type` is `"user"` or
  `"assistant"` and `message.content` is present:
  - content item `type == "text"` → the text, as one log line
  - content item `type == "tool_use"` → `→ <ToolName>(<compact args>)`
  - content item `type == "tool_result"` → 1-line truncated result summary
  All other line shapes (`last-prompt`, `custom-title`, `attachment`, etc.) are skipped by this
  same type check — no separate filter list to maintain.
  Returns the last ~200 rendered lines as JSON. Re-parses the full file on every poll (files are
  small / short-lived — no byte-offset diffing in V1). If no `session_id` is registered yet, or
  the transcript file doesn't exist: returns `{"status": "not-available"}` instead of an error.
- `GET /` → serves the static `dashboard.html`.

### 5. `dashboard.html`

One static page, vanilla JS (no build step, no new frontend dependency). Polls `/api/tasks` every
~2s for the task table (name/mode/branch/ports/status), and `/api/tasks/<selected>/log` every ~2s
for the log pane of whichever task is selected.

## Error handling

- `dispatch` on an unknown task-name → error to stderr, matching existing `stop`/`rm` behavior.
- `dispatch` where the `claude --bg` launch's stdout doesn't contain a `backgrounded · <id>` line
  (e.g. it errored instead) → surface that stdout/stderr directly and exit non-zero; do not write
  a partial registry entry.
- Dashboard: a task with no `session_id` yet (dispatch not run, or failed before registry write) →
  log pane shows "no session dispatched yet", not a 500.
- Malformed/partial JSONL line (mid-write race) → skip that line, don't crash the parse.

## Testing

- `bin/`: this plugin's existing scripts have no test suite (bash, wraps external tools) — matches
  that precedent; no new test harness for `dashboard.py` beyond a manual smoke check (dispatch a
  task, confirm `/api/tasks` and `/api/tasks/<name>/log` return sane data while it's running).
- If `dashboard.py`'s JSONL-rendering logic grows non-trivial branches, add a small
  `test_dashboard.py` with `assert`-based cases per Ponytail's "non-trivial logic leaves one
  runnable check" rule — not a full pytest suite.

## Open items for implementation

- Exact default port for `dashboard.py` (pick something outside the documented docker/native
  port ranges: not `8081+N*100`, `5173+N*100`, `8500+N`, `5500+N`).
- Whether `parallel-task.sh list`'s existing (non-JSON) table output should also gain an
  agent-status column, or stay as-is with `--json` as the only way to see it (leaning: leave the
  table alone, `--json` is the dashboard's data source, not a human-facing change).
