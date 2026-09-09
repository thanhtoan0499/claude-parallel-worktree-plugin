---
name: parallel-worktree-run
description: Spin up a fully running, independent copy of the current repo (own git worktree + branch + own docker/native dev stack + own ports) from the ROOT Claude Code session, and dispatch a background session to implement a task inside it — all without the root session ever leaving the repo root. Trigger when the user wants to work on a new task WHILE another task is already in flight, explicitly asks to run things "song song" / "chạy song song" / "clone thêm bản" / in parallel, or wants multiple live preview URLs up at once for different branches. Covers provisioning via `parallel-task.sh`, dispatching the implementing session, and listing/stopping/removing copies. For a task that only needs isolated CODE (no separate running dev server), a plain `git worktree` is enough — this skill is for when a live, independently-addressable dev stack is also needed.
---

# Parallel worktree + dev-stack, controlled from one root session

Run N independent, fully working copies of the app side by side — each on its own git
branch, in its own worktree, with its own running dev stack on its own ports — while the
user only ever talks to **one** Claude Code session, parked at the repo root.

The root session's own `pwd` must never change for this. It provisions copies and dispatches
background sessions to work inside them; it does not `EnterWorktree` itself.

## Prerequisites — repo layout this plugin assumes

Ported from a monorepo (aiquinta-platform) with this shape; a target repo needs the same
conventions for `bin/dev-stack.sh` / `bin/dev-native.sh` to work unmodified:
- `deploy/docker-compose.yml` — compose file with `gateway`, `frontend`, `postgres`,
  `azurite`, `redis` services, each host port driven by a `${..._HOST_PORT:-default}` var.
- `.env` at repo root (for native mode's shared infra + gateway env).
- pnpm workspace (`apps/web` frontend) + a Python workspace (`uv`) for the gateway, OR adapt
  `bin/dev-native.sh` to your own native run commands.
- If your repo doesn't match this shape, treat `bin/*.sh` as a reference implementation and
  adjust the `up`/`down` command bodies — the orchestration logic in `bin/parallel-task.sh`
  (slot allocation, worktree lifecycle, registry) doesn't need to change.

## Relationship to a plain worktree

- **Code-only isolation**: a plain `git worktree add` (no separate dev server) — the task
  either doesn't need one, or reuses the shared slot-0 gateway (`dev-stack.sh N up --frontend-only`).
  If a paired "worktree-new-feature"-style skill exists in this project's own `.claude/skills/`,
  prefer it for that case instead of this one.
- **This skill**: isolate code AND run a separate live dev stack (own ports, own URLs) AND
  dispatch the implementation to a background session, so multiple tasks can be live and
  being worked simultaneously.

If you're not sure a separate running stack is actually needed, ask — don't create
infrastructure the task doesn't need.

## Step 1 — ask what's needed

Unless already given in the request, use `AskUserQuestion` for:
- **task-name**: short kebab-case slug (becomes `.claude/worktrees/<task-name>/` and branch
  `feature/<task-name>`)
- **mode**: `native` (default suggestion — gateway+frontend run directly on the host,
  hot-reload, shared Postgres/Azurite; see README "Native mode") or `docker` (fully isolated
  containers incl. own DB, but a rebuild per code change — pick this only when the task needs
  full container/prod-topology parity)

## Step 2 — provision

From the repo root:

```bash
parallel-task.sh start <task-name> <native|docker>
```

This does, in one call: `git worktree add` under `.claude/worktrees/<task-name>/` on a fresh
branch from `origin/main`, copies `.worktreeinclude` files (`.env`) into it, picks the next
free port slot/task number automatically (checked live — never collides with what's already
running), and starts that copy's dev stack. It prints the frontend/gateway URLs — read them
back to the user, and don't skip the WorkOS redirect-URI reminder it prints (each new frontend
port needs its callback registered in the WorkOS dashboard before login works there; no local
auth bypass exists).

Port scheme, so the user knows what to expect:
- `docker` mode: slot *N* offsets every port by `N*100` — frontend `5173`, `5273`, `5373`, …
- `native` mode: task *N* → gateway `8500+N`, frontend `5500+N` (a different, smaller offset;
  shared Postgres/Azurite across all native tasks)

## Step 3 — dispatch the implementing session

Build the task prompt (do NOT skip any of these — this becomes the literal `<prompt>` argument
to `dispatch`):

1. The actual task/requirements verbatim.
2. The dev URLs from Step 2, so it can verify its work against a live server instead of only
   static analysis.
3. The repo's normal gates: any rules under `.claude/rules/*` or `CLAUDE.md`, commit message
   format, and any post-task note-taking convention the repo has.
4. **Forbid `--frontend-only` as a workaround.** If this slot's `dev-stack.sh N up -d` (or a
   restart of it) fails, it must diagnose and retry the full stack — never fall back to
   `dev-stack.sh N up --frontend-only`. That flag repoints the frontend at the *shared* slot-0
   gateway, silently breaking the own-DB/own-backend isolation this skill exists to guarantee (a
   real regression: a dispatched session hit a half-created stack from a prior failed run and
   "fixed" it by switching to `--frontend-only`, so the task ran against the wrong tenant's DB
   until caught). `--frontend-only` belongs to `worktree-new-feature` only.
5. Ask it to end with a summary: files changed, tests run + result, URLs.

Then dispatch:

```bash
parallel-task.sh dispatch <task-name> "<prompt>" [--model <model>] [--effort low|medium|high|xhigh|max]
```

This launches an independent, addressable top-level Claude Code session in that worktree
(`claude --bg`) and records its id in the registry — it is NOT a subagent of this root session.
There is no `EnterWorktree` step to instruct here: the dispatched session's process starts with
its cwd already inside the worktree (`dispatch` runs `claude --bg` from there directly), so the
split-brain trap `worktree-new-feature` warns about for subagents (drifting back to the root
session's cwd) doesn't apply to this path.

`dispatch` returns immediately once the session is backgrounded — no need to batch multiple calls
in one message the way background `Agent` calls did; issue them one after another.

Because the dispatched session is a real, addressable Claude Code session (not a one-shot
subagent), it can also be talked to mid-task — `claude attach <short-id>` opens it in the current
terminal, or another session can message it once it appears in that session's peer list.

## Step 4 — repeat for more tasks

Nothing above requires leaving the root session. For the next independent task: Step 1 again
(new task-name), Step 2, Step 3. The root session stays the single point of control the whole
time.

## Step 5 — manage running copies

```bash
parallel-task.sh list              # every copy: branch, mode, ports, live status
parallel-task.sh stop  <task-name> # stop the dev stack, keep worktree + branch
parallel-task.sh rm    <task-name> # stop + remove the worktree (branch kept)
parallel-task.sh rm    <task-name> --force  # also discard uncommitted changes
```

`rm` never deletes the branch — after the PR merges, clean up with
`git branch -d feature/<task-name>` same as `worktree-new-feature`'s convention.

Check on a dispatched session's progress via `parallel-task.sh list --json` (or the dashboard, if running)
for its live status, or `claude attach <short-id>` to check in directly. When it reports done, relay
its summary to the user and ask whether to keep that copy running (for review / follow-up) or tear it
down with `rm`.

## Step 6 — escalate instead of stalling

When a dispatched session hits something it cannot decide alone, it appends one record to the
escalation queue rather than stopping and waiting for you:

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "<plugin bin dir>")
from escalations import QUEUE_PATH, append, new_record
append(QUEUE_PATH, new_record(
    session_id="<this session's id>",
    kind="diff_review",              # or red_tests / looping / pick_implementation / scope_question
    question="Merge the adapter change?",
    options=["Approve", "Reject"],
    evidence={"tests": "green", "branch": "feature/x", "deps_added": [],
              "migration": False, "changed_files": ["bin/dashboard.py"]},
))
PY
```

`manager_daemon.py` picks it up within seconds and puts it to the persistent Engineering Manager
session — one long-lived session for the whole team, not a fresh model per escalation, so it
remembers what it already decided and what else is in flight. Mechanical calls are settled there
and delivered straight back into the worker; anything irreversible, security-shaped, or genuinely
ambiguous waits for you in the dashboard's decision panel. Every automatic decision
also shows up as a tinted line in the manager's chat, so nothing is decided on your behalf
invisibly. The manager's charter — what it decides itself vs. escalates, how it dispatches and
sizes work — is `skills/engineering-manager/SKILL.md`.

Start the daemon alongside the dashboard:

```bash
manager_daemon.py       # watches ~/.claude/hermes/escalations.jsonl
```

## Troubleshooting

- **`start` fails partway (e.g. port collision on `up -d`)**: `cmd_start` now rolls back the
  worktree + branch it just created on any failure, so the task-name is free to retry
  immediately. (Older behavior left an unregistered orphan worktree behind — `list` wouldn't
  show it, and a retry hit `$WORKTREES_DIR/$task already exists`; recovery meant hand-editing
  `.parallel-registry.json` with `jq`. If you ever see that state — a directory under
  `.claude/worktrees/<task>` with no matching `list` entry — remove it with
  `git worktree remove --force` and retry `start`.)
- **All slots seem to collide on the same host port**: every per-slot port (gateway, frontend,
  postgres, azurite, **and redis**) must be exported by `dev-stack.sh` with the `N*100` offset.
  If a new service is ever added to `deploy/docker-compose.yml` with a `${SOMETHING_HOST_PORT:-default}`
  mapping, it needs the same slot-offset treatment in `dev-stack.sh` — an unoffset service will
  silently try to bind the same host port across every slot and only the first `up -d` wins.
- **Verifying isolation actually held**: don't stop at "frontend returns 200" — that's true even
  in `--frontend-only` mode. Confirm the backend is this slot's own by checking
  `curl http://localhost:<fe-port>/runtime-config.js` shows `gatewayUrl` matching *this* slot's
  gateway port, and `docker compose -p aiquinta-mfg-s<N> ps` lists all five services (postgres,
  azurite, redis, gateway, frontend) — not just frontend.

## Quick reference

| Situation | Action |
|---|---|
| New independent task, want a live preview too | Steps 1-3 above |
| Just need isolated code, no separate server | use `worktree-new-feature` instead |
| See what's running | `parallel-task.sh list` |
| Pause without losing work | `parallel-task.sh stop <task>` |
| Done / abandoned | `parallel-task.sh rm <task>` |
| Root session's own cwd | never changes — always the repo root |
