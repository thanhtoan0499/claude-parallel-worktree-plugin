# Workers are dispatched into interactive tmux sessions, not `claude --bg`

**Date:** 2026-09-14 (recording the reasoning already in `skills/engineering-manager/SKILL.md`)

## Context

`bin/parallel-task.sh dispatch` launches with `claude --bg` internally. A `--bg` worker is
write-only and one-shot, and both halves hurt.

It **cannot be messaged**: `SendMessage` to one returns "session not found", the same dead end
`--resume` already is. A brief cannot be extended once work starts, a worker heading the wrong way
cannot be corrected, and a finished worker cannot be asked a follow-up. The only remaining move is
kill and re-dispatch from scratch, discarding everything it learned — that happened three times in
one afternoon on 2026-09-09.

Every prompt it hits is also **invisible**. It stalls, and waiting looks exactly like working. Five
stalls that day: `Monitor`, the browser tools twice, a `git push`, and the trust-folder dialog that a
fresh worktree raises before any work begins. In an interactive session that dialog is one keypress.

## Decision

Dispatch into a persistent interactive session:

```bash
cmew new <task-name> <worktree-dir> -e <level>
tmux send-keys -t cc-<task-name> "Read BRIEF.md in this worktree and do exactly what it says."
tmux send-keys -t cc-<task-name> Enter
```

Write the brief to `BRIEF.md` in the worktree and point at it with one short line — piping a long
brief through `send-keys` is an escaping trap, and a backtick inside a double-quoted string is
executed by the shell rather than delivered.

## Why

A session that can be messaged can be corrected, extended and questioned afterwards. A session whose
prompts are visible fails loudly instead of looking busy.

## What would change this

`claude --bg` gaining a message transport and surfacing its prompts. Until then, prefer the
interactive route even though `parallel-task.sh dispatch` remains the documented command.
