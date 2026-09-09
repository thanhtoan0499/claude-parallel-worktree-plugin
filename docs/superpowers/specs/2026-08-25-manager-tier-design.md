# Manager tier — design spec

Date: 2026-08-25
Status: approved (design), pre-implementation

## Problem

The plugin can now dispatch independent Claude sessions into isolated worktrees and watch them
(`parallel-task.sh dispatch`, the dashboard). What it cannot do is **run work to completion without
the user babysitting it**. Today every question a worker hits — red tests, an ambiguous scope line,
"is this diff OK to merge?" — either stalls that worker or lands in
`~/.claude/hermes/ESCALATIONS.md`, a freeform markdown file nobody sees, with no way to answer it
except opening a terminal and typing at the orchestrator session.

The loop is broken at exactly one place: **the escalation has no path out and no path back in.**

The goal is autonomy with an audit trail: a worker's report gets decided automatically when the
decision is mechanical, and reaches the human only when it genuinely needs a human — with the
evidence already assembled.

## Three tiers

| Tier | Who | Model | Decides |
|---|---|---|---|
| Worker | session in its own worktree | Sonnet | Implementation detail the task spec already answers: naming, file layout, test idiom, retry after a transient failure |
| **Manager** | **spawned fresh per report, short-lived** | **Fable** | Mechanical judgement over one report + its evidence (see Tier 2 rubric) |
| Human | the user, via the dashboard | — | Tier 3 only (see below) |

The manager is deliberately **stateless and short-lived**: one report in, one structured decision
out, process exits. This is not a cost optimisation — it is what makes the design possible at all,
given the CLI constraint below.

## Verified feasibility (spikes run 2026-08-25 — settled, do not re-test)

- **`claude --resume <session-id> -p "<message>"` works.** Delivers a message into an existing
  session and returns its reply (exit 0, expected output). This is how an answer gets back into a
  blocked worker.
- **There is no `claude send` subcommand.** No supported way to push a message into a *live*
  session from outside. This is why the manager is spawned fresh per report rather than kept
  running: a long-lived manager would need exactly the mechanism that does not exist.
- **`claude --bg -n <name> "<prompt>"`** spawns an independent background session and prints
  `backgrounded · <short-id> · <name>`. It **ignores `--session-id`**, so the id must be parsed
  from that line.
- **`claude agents --json --all`** lists sessions with `sessionId` / `status` / `state` / `cwd` /
  `startedAt`. `--all` is mandatory (finished sessions are omitted without it). Do **not** pass
  `--cwd` — its exact-path matching is unreliable, proven earlier.
- Transcripts live at `~/.claude/projects/<cwd with / and . replaced by ->/<sessionId>.jsonl`.

## The rubric

### Tier 2 — the manager decides, and the decision is logged

Every Tier 2 decision is written to the queue and rendered in the dashboard as a **non-blocking
audit feed**. This is a hard requirement, not a nicety: an autonomous manager the user cannot
inspect is a black box, and the point of the feed is that the user can see what was decided on
their behalf without having to approve each one.

- Red tests → dispatch a fix worker, or send the failure back to the same worker
- Worker looping with no progress → stop it and re-brief
- Two implementations both satisfying the spec → pick one
- A scope question the ticket/PRD already answers
- **Approve a diff — only when ALL four hold:** tests green **AND** no new dependency **AND** no
  migration/schema change **AND** no auth/secret file touched

### Tier 3 — must reach the human, blocks until answered

- Irreversible: delete / drop / force-push, data migration
- `git push`, opening a PR, anything touching `main`
- Credentials, secrets, auth changes
- Genuine spec ambiguity: two readings produce two different products
- Fails to converge after N attempts, or a cost anomaly
- **Diff touches a sensitive path OR adds a dependency** → render the Diff review screen (changed
  files, +/− counts, test result, checklist)
- Worktree collision that requires stopping someone's in-flight work

Tier 3 wins ties: if a record matches both a Tier 2 and a Tier 3 condition, it is Tier 3.

## Components

1. **Queue** (`escalations.jsonl`) — append-only record per report, replacing the freeform
   `ESCALATIONS.md`. Fields: `id`, `ts`, `session_id`, `kind`, `question`, `options[]`,
   `evidence{tests, diff_stat, changed_files, branch, deps_added, sensitive_paths}`, `tier`,
   `status`, `decided_by`, `answer`, `answered_at`.
2. **Tier classifier** — a pure function `record -> (tier, reason)`. The rubric above is its
   specification; this is the highest-value unit-test target in the feature.
3. **Manager invocation** — build a prompt from a queue record, call the Fable CLI, parse and
   validate the structured JSON decision it returns. Prompt-building, parsing and validation are
   pure functions and are unit tested, including against malformed model output.
4. **Answer delivery** — build the `claude --resume <sid> -p <answer>` argv (pure, tested); the
   subprocess call itself is a thin shell around it.
5. **Daemon loop** — watches the queue, routes Tier 2 to the manager, leaves Tier 3 for the
   dashboard, and delivers answers back to workers.
6. **Dashboard additions** — Tier 3 blocking cards ("Decision required" with evidence; "Diff
   review" with changed files, +/− counts, tests and checklist, plus an Approve control), the
   Tier 2 audit feed, and `POST /api/escalations/<id>/answer`.

## Design constraints

- Python 3 **stdlib only**. No new dependency, no build step — the plugin's existing hard rule.
- Tests extend the existing plain-`assert` pattern (`python3 bin/test_dashboard.py`). **Not pytest.**
- Every subprocess-invoking piece is split so the decision logic is unit-testable without spawning
  anything. The untestable part must be a thin, obvious shell.
- The queue is append-only; answers are recorded by appending an update record, never by rewriting
  history. Concurrent writers (daemon, dashboard, workers) must not corrupt it.

## Non-goals (this iteration)

- No multi-user auth on the dashboard — it stays localhost-bound (it renders transcripts, which
  routinely contain secrets).
- No automatic merging or pushing. The manager may *approve* a diff; landing it stays a human act
  because `git push` / PR is Tier 3 by rubric.
- No retry/backoff policy engine — "fails to converge after N attempts" uses a simple counter.
- No replacement of the Hermes skill's dispatch logic. This formalises its escalation channel; the
  rest of Hermes is unchanged.
