# CLAUDE.md

Guidance for Claude Code in this repo. **Loaded every session** — it carries only what shapes a
typical task or is easy to get wrong. Detail lives in the files it points to.

This repo is the engineering-manager tooling: the ledger, the board, and the dispatch scripts a
long-lived manager session runs on. It is NOT the product repo — product work happens in the target
repo the worktrees point at.

---

## Read first

- `skills/engineering-manager/SKILL.md` — the manager role: the eight actions, the ledger format,
  how to dispatch, how to accept a report, the hard boundaries. **Single source of truth. Do not
  restate it here; do not contradict it.**
- `skills/parallel-worktree-run/SKILL.md` — the worktree/slot/port workflow `bin/parallel-task.sh`
  implements.
- `docs/decisions/` — why past calls were made. Read before re-litigating one.

---

## State lives outside this repo

`~/.claude/hermes/` — not in git, does not survive the machine:

| File | Holds |
|---|---|
| `assignments.jsonl` | the ledger: one record per commitment, latest per `id` wins |
| `escalations.jsonl` | decisions asked of the CTO, and their answers |
| `manager-*.json` | session id, prefs, tick state |

Both `.jsonl` files are **append-only and lock-protected**. Append with `assignments.append` /
`escalations.append` (Python, `sys.path.insert(0, "bin")`) — **never** `echo '{...}' >> file`. The
dashboard and daemon take a lock while writing the same file; an unlocked append lands mid-write and
produces a torn line, which every reader drops silently, including your own next read.

These files record WHAT was committed to. They do not record WHY — that is what `docs/decisions/`
is for.

---

## Dispatch

Prefer the interactive route. `parallel-task.sh dispatch` still launches `claude --bg` internally,
and a `--bg` worker cannot be messaged and hides every permission prompt — reasons in SKILL.md.

```bash
cmew new <task-name> <worktree-dir> -e <level>          # tmux session cc-<task-name>
tmux send-keys -t cc-<task-name> "Read BRIEF.md in this worktree and do exactly what it says."
tmux send-keys -t cc-<task-name> Enter
```

Write the brief to `BRIEF.md` inside the worktree and point at it with one short line — piping a
long brief through `send-keys` is an escaping trap.

**The session name must equal the worktree task name, exactly** — the board joins session to branch
and ticket by name, and a mismatch silently draws an empty card
(`docs/decisions/2026-09-14-session-name-must-equal-worktree-name.md`).

Worktree commands: `parallel-task.sh start|list|stop|rm`.

---

## Hard boundaries

The manager session does not:

- edit repository files — dispatch a worker, including when the thing to fix is this tooling
- `git push`, open a pull request, or commit to `main` / `master`
- answer a permission prompt on a human's behalf
- steer a session the CTO opened themselves

Its only writes are the ledger and the escalation queue.

---

## Tests

```bash
cd bin && python3 -m pytest -q
```

Four tests in `test_board_state.py` compare raw `node` stdout, so an environment with `FORCE_COLOR`
set fails them — `env -u FORCE_COLOR python3 -m pytest -q` is green
(`docs/decisions/2026-09-14-board-state-tests-depend-on-raw-node-stdout.md`).

---

## Decision records

One file per non-obvious call, `docs/decisions/YYYY-MM-DD-<slug>.md`, recording **why** — see
`docs/decisions/README.md`. Write one when a choice would otherwise have to be re-derived, and
put the measurement date in it: an environment fact measured last week may already be false.
