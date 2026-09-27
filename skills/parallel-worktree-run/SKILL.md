---
name: parallel-worktree-run
description: Spin up a fully running, independent copy of the current repo (own git worktree + branch + own docker/native dev stack + own ports) from the ROOT Claude Code session, and dispatch a background subagent to implement a task inside it — all without the root session ever leaving the repo root. Trigger when the user wants to work on a new task WHILE another task is already in flight, explicitly asks to run things "song song" / "chạy song song" / "clone thêm bản" / in parallel, or wants multiple live preview URLs up at once for different branches. Covers provisioning via `parallel-task.sh`, dispatching the implementing subagent, and listing/stopping/removing copies. For a task that only needs isolated CODE (no separate running dev server), a plain `git worktree` is enough — this skill is for when a live, independently-addressable dev stack is also needed.
---

# Parallel worktree + dev-stack, controlled from one root session

Run N independent, fully working copies of the app side by side — each on its own git
branch, in its own worktree, with its own running dev stack on its own ports — while the
user only ever talks to **one** Claude Code session, parked at the repo root.

The root session's own `pwd` must never change for this. It provisions copies and dispatches
background subagents to work inside them; it does not `EnterWorktree` itself.

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
  dispatch the implementation to a background subagent, so multiple tasks can be live and
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

**`docker` mode is capped by `DOCKER_SLOTS_ALLOWED`** (default `1 2 3 4`, set once per repo to
match whichever frontend ports are actually registered as WorkOS redirect URIs — a slot outside
that list can run, but nobody can log into it). If `start` fails with `no free docker slot in
allow-list`, that is NOT a signal to raise `DOCKER_SLOTS_ALLOWED` yourself — a slot you invent
isn't a registered redirect URI either, so the copy comes up unloginable and the person only
finds out later. Stop and ask instead: run `parallel-task.sh list`, show the user which
tasks/slots are `stopped` (idle, safe to free) vs `running` (still in use), and let them choose
one of:
1. Free a slot — `parallel-task.sh rm <stopped-task>` (or `stop` if they want to keep the
   worktree) — then retry `start` on the freed slot number.
2. Register one more port in the WorkOS dashboard and only then extend
   `DOCKER_SLOTS_ALLOWED` for that session.
3. Use `native` mode instead for this task (different port range, not part of this cap).

## Step 3 — dispatch the implementing subagent

Spawn a **background** `Agent` call (default subagent type is fine — don't pass
`isolation: "worktree"`, the worktree already exists from Step 2). The prompt must:

1. Tell it the worktree's **absolute path** and instruct it to call
   `EnterWorktree(path="<abs path>")` as its very first action — this moves only *that
   subagent's* own cwd/statusline/hooks, never the root session's. Then verify with `pwd` and
   `git branch --show-current` before touching any file (same split-brain trap
   `worktree-new-feature` warns about, just via `path` instead of `name` since the worktree
   already exists). Don't just describe the path in prose and hope — the prompt must literally
   instruct the `EnterWorktree` tool call; a subagent that only reads the path from prose and
   never calls it can drift back to the root session's cwd.
2. Give it the actual task/requirements verbatim.
3. Give it the dev URLs from Step 2, so it can verify its work against a live server instead
   of only static analysis.
4. Point it at the repo's normal gates: any rules under `.claude/rules/*` or `CLAUDE.md`,
   commit message format, and any post-task note-taking convention the repo has.
5. **Forbid `--frontend-only` as a workaround.** If this slot's `dev-stack.sh N up -d` (or a
   restart of it) fails, the subagent must diagnose and retry the full stack — never fall back
   to `dev-stack.sh N up --frontend-only`. That flag repoints the frontend at the
   *shared* slot-0 gateway, silently breaking the own-DB/own-backend isolation this skill
   exists to guarantee (a real regression: a subagent hit a half-created stack from a prior
   failed run and "fixed" it by switching to `--frontend-only`, so the task ran against the
   wrong tenant's DB until caught). `--frontend-only` belongs to `worktree-new-feature` only.
6. Ask it to end with a summary: files changed, tests run + result, URLs — this is what gets
   relayed to the user when the task-notification arrives.

Launch one `Agent` call per task-name. To start several at once, send multiple `Agent` calls
in a single message so they run concurrently.

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

When a dispatched subagent's task-notification arrives, relay its summary to the user and ask
whether to keep that copy running (for review / follow-up) or tear it down with `rm`.

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
- **`start` failed on `no free docker slot`, and you were tempted to just extend
  `DOCKER_SLOTS_ALLOWED` to get unblocked**: don't. That was a real incident — a slot outside
  the allow-list still starts, but its frontend port has no matching WorkOS redirect URI, so the
  user can't log in and won't find out until they try. Treat the error as a stop-and-ask, per
  Step 2 above, not an obstacle to route around.
- **Cleaning up a task whose worktree directory is already gone (but `list`/registry still shows
  it)**: `parallel-task.sh rm` shells out to `git worktree remove`, which fails with "is not a
  working tree" when the directory was already deleted outside the tool. Confirm with
  `git worktree list` (if git itself has no record either, nothing is lost) then delete the
  stale entry directly: `jq 'del(."<task-name>")' .claude/worktrees/.parallel-registry.json`
  (write to a temp file and `mv` it back — don't edit the registry in place mid-read).
- **Moving a running copy to a different slot without losing its work**: `parallel-task.sh stop
  <task>` (downs the old slot's containers) → `cd` into that task's worktree and run
  `dev-stack.sh <new-slot> up -d` directly → update its registry entry's `num` and `ports` to
  match the new slot. This keeps the worktree's commits; re-running `start` under the same
  task-name would instead try to create a brand-new worktree/branch and collide.
- **`parallel-task.sh` / `dev-stack.sh` reported "command not found" even though `which
  parallel-task.sh` found it earlier**: the Bash tool's shell state (including `PATH`) does not
  persist between calls the way `pwd` does — each call can start from a plain, non-interactive
  shell. Export the plugin's `bin/` directory onto `PATH` explicitly in any command that calls
  these scripts (`export PATH="<plugin-repo>/bin:$PATH"`), don't assume a prior successful call
  proves it's on `PATH` for the next one.

## Quick reference

| Situation | Action |
|---|---|
| New independent task, want a live preview too | Steps 1-3 above |
| Just need isolated code, no separate server | use `worktree-new-feature` instead |
| See what's running | `parallel-task.sh list` |
| Pause without losing work | `parallel-task.sh stop <task>` |
| Done / abandoned | `parallel-task.sh rm <task>` |
| Root session's own cwd | never changes — always the repo root |
