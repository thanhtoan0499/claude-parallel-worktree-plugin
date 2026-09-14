# A dispatched session's name must equal its worktree task name, exactly

**Date:** 2026-09-14 (recording the reasoning already in `skills/engineering-manager/SKILL.md`)

## Context

The board joins a live session to its branch and its ticket **by name** — `board_state.build(name,
agent, reg)` looks up `registry[name]`. There is no other key.

A worktree provisioned as `t8309-confirm-tool` and a session dispatched as `t8309d` are two halves
that never meet. The session document comes out with `ado_refs: []`, `branch: null`,
`managed: false`, and the assignment grid draws a card with no ticket, no branch and no elapsed time.

Nothing errors. The board simply goes quiet, which looks exactly like no work running.

## Decision

The session name and the worktree task name are the same string. No suffix, ever.

## Why

The failure is silent, and silence is indistinguishable from idleness — so the cost is not a broken
card, it is a worker nobody knows is running.

A **re-dispatch is the trap**: the natural instinct is to append `b`, `c`, `d` to avoid a name
collision. Adopt the existing worktree instead:

```bash
parallel-task.sh dispatch <task-name> "<brief>" --model <model> --effort <level>
parallel-task.sh dispatch <new-name> "<brief>" --worktree <path-of-the-existing-worktree>
```

The first form adopts `.claude/worktrees/<task-name>`; `--worktree` is for when the session name and
the worktree name genuinely differ.

Related: never launch a worker with a bare `claude --bg`. An unrecorded session is missing from the
board's `managed` sessions, gets no worker-finished wake, and is invisible to the stuck-session
watch — so when it freezes on a prompt nobody answers, nothing notices. Three workers sat outside
the registry for exactly this reason on 2026-09-09, and all three froze.

## What would change this

The board keying on something other than the name — a session id recorded in the registry at
dispatch, say. Until then the name is the join key and must match.
