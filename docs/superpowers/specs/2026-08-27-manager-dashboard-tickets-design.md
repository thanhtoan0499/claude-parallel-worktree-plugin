# Manager Dashboard: Ticket-Centric Redesign

## Problem

The dashboard (`bin/dashboard.py` + `bin/dashboard.html`, served on :4400) currently shows one row
per live Claude session — branch, ports, dev-stack status, raw logs. That's an operator view. It
answers "what's running," not "what do I need to do."

The user's actual need, stated directly: they assign tickets to a manager (this root Claude
session, operating under the `hermes` skill's rubric) and want to stop tracking work manually.
They want to open the dashboard and see, in order: what's done, what's blocked, what needs a
decision from them, and what hasn't been started yet — nothing else, unless they choose to dig in.

## Goals

- One glanceable "Tickets" view: status + next action per ticket, ADO-backlog-driven.
- A ticket can be dispatched (or bundled with other related tickets) directly from the dashboard.
- Every ticket row traces through to its ADO work item and, once one exists, its GitHub PR —
  regardless of status, so a **Done** row still shows what shipped it.
- Everything that isn't "status + next action" (raw session list, ports, terminal logs, the
  manager's audit trail) stays available but collapsed by default — one click away, not competing
  for attention with the ticket view.

## Non-goals

- **No writes back to ADO from the dashboard.** No comment button, no sub-task creation, no state
  transitions triggered from the UI. ADO is read-only context here — title, description, id, link.
  Dispatched sessions already comment on their own tickets when relevant (observed: a live session
  updated `AB#6541` with a follow-up note after pushing a fix) — if the user wants that for a
  specific dispatch, it goes in the "extra instructions" field at dispatch time, same as any other
  instruction. Building a dedicated ADO-write API is explicitly out of scope.
- **No ticket-selection UI for prioritization/backlog grooming.** The ADO query already scopes to
  "assigned to me, not closed" — reordering or filtering that list beyond the status ladder isn't
  part of this design.
- **No framework rewrite.** Stays a single Python stdlib `http.server` + one `dashboard.html` with
  vanilla JS. No build step, no new dependency, no client framework.

## Architecture

### Data sources (three, merged)

1. **ADO backlog** — `az boards query` (WIQL), org `agentiqai`, project `AgentIQ`:
   ```
   SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType]
   FROM WorkItems
   WHERE [System.TeamProject] = 'AgentIQ'
     AND [System.AssignedTo] = @Me
     AND [System.State] NOT IN ('Closed', 'Removed')
   ```
   Verified working live against the real org during this brainstorm. Cached ~60s (same `_cached()`
   helper already in `dashboard.py`, just a longer TTL than the 1.5s session-data cache — this is a
   network call to a service that doesn't change turn to turn).

2. **Live sessions + registry** — unchanged, already shipped this session: `claude agents --json
   --all` merged with `parallel-task.sh list --json`, enriched with branch (derived live via `git
   branch --show-current` for a session's own worktree — explicitly **not** derived for a session
   whose cwd is the shared repo root, since that "current branch" belongs to whichever session most
   recently ran `git checkout` there, not to any one session) and with PR data (`gh pr list --head
   <branch>`, one call per distinct branch, cached ~30s).

3. **Ticket↔session matching**, two tiers, both already partially built:
   - **Authoritative**: a new `--ticket <id>` flag, repeatable, on `parallel-task.sh start` and
     `dispatch`. Persists as `ado_ids: [...]` in the registry entry at dispatch time. This is how
     anything the manager dispatches gets tracked — recorded at the moment of action, not
     reconstructed later.
   - **Inferred fallback**, for sessions not dispatched by the manager (a teammate's work, the
     user's own ad hoc session): scrape `AB#NNNN` refs and full `dev.azure.com/.../_workitems/edit/N`
     links out of the branch's PR title+body. Already working in `_find_ado_link`; generalize from
     "first match" to "all matches" (`_find_ado_links` → `list[tuple[url, id]]`) since one PR can
     close two tickets — verified in real PR history (`AB#7160` in a title, `AB#6541` as a markdown
     link in a body).

   A ticket can match zero, one, or several sessions; a session's `ado_ids` can populate several
   ticket rows (the shared-session / bundled-dispatch case). No UI grouping device beyond both rows
   showing the same PR number — that's sufficient signal on its own.

### Status ladder

Computed per ticket, first match wins, ranked top-to-bottom for display order:

| Rank | Status | Condition | Action shown |
|---|---|---|---|
| 0 | Needs your decision | a pending escalation (`/api/escalations` `needs_human`) tied to a matched session | the escalation's own question |
| 1 | Blocked | a matched session has `agent_state == "blocked"` | "Check the blocker" |
| 2 | Not started | zero matched sessions | **"Giao việc"** button |
| 3 | In progress | matched session running; PR open or no PR yet | "Review PR #N" (if a PR exists) else "—" |
| 4 | Done | matched session's PR is merged | "—" |

Not-started ranks above in-progress deliberately: an untouched ticket needs *you* to act (dispatch
it); an in-progress one needs nothing from you until a PR shows up.

### Row content (always, regardless of status)

`AB#NNNN` (links to the ADO work item) and, once one exists, `PR #NNN` (links to the GitHub PR) —
shown together, independent of status. A **Done** row keeps its PR link; that's the point of
carrying it as separate row metadata rather than folding it into the action column, which only
ever shows what needs a *decision or action*, not provenance.

## Dispatch flow ("Giao việc")

1. Click "Giao việc" on a Not-started row → the row expands inline (no modal, no new page):
   ticket title + description (read-only, from the ADO query), checkboxes for any other
   Not-started tickets — manually ticked by whoever is dispatching, there is no automatic grouping
   — for bundling tickets that are related (files/context overlap is the same judgment call
   `hermes` already applies when deciding two streams share one worktree), and an optional
   "Extra instructions" textarea.
2. Confirm → `POST /api/tickets/dispatch` with `{ticket_ids: [...], instructions: "..."}`.
3. Server: picks a task-name slug from the first ticket's title, runs `parallel-task.sh start
   <slug> native --ticket <id> [--ticket <id> ...]`, then `dispatch` with a prompt built from:
   ticket title(s) + description(s), the extra instructions, a repo-conventions reminder
   (`CLAUDE.md`), and the Hermes verify-before-done rule (tests green, evidence seen, before a
   stream counts as done).
4. The button shows a pending state ("Đang tạo…") while provisioning runs — this is genuinely slow
   (pnpm/uv install, DB migrations; 10–60s observed live this session) — without blocking the rest
   of the dashboard's normal 2s polling.
5. Success: the row flips to In progress on the next poll, no special transition needed. Failure
   (e.g. port collision): the error renders inline in the same expanded row, not silently dropped.

## Layout

Always visible, top of page:
1. **Tickets** panel (this design's primary surface)
2. **Decision required** (unchanged — already auto-hides when empty)

Collapsed by default behind one disclosure — **"▸ Chi tiết vận hành (N sessions)"**:
3. The 4 stat cards (sessions / working now / idle or done / blocked)
4. The Sessions table (unchanged, including its existing Managed-only toggle)
5. Session details sidebar + the terminal-styled Live logs view (both unchanged)
6. "Decided for you" audit feed

Expanding the disclosure reveals exactly what exists today, unmodified internals — this is a
visibility change, not a rebuild of the operator view.

## What's already shipped (this session, uncommitted in the working tree)

- Credential redaction in the log renderer (`render_transcript_line`/`render_task_log` now return
  structured `{kind, role, tool, text}` records; a command naming a credential and its matching
  result are both replaced before ever reaching the client).
- Terminal-styled Live logs panel with a live "what's it doing" summary bar.
- PR/ADO enrichment per session (`_lookup_pr_and_ticket`, `_find_ado_link`, `_git_branch`,
  `_enrich_branch_and_links`), including the shared-root-checkout guard.
- A first-draft Tickets panel (`computeTickets`/`renderTickets` in `dashboard.html`) grouping by
  `ado_id`/`pr_number` — this design's status ladder extends it with "Not started" and the
  dual ADO+PR link; the registry-field pluralization (`ado_id` → `ado_ids`) is the only breaking
  change to what's already there.
- "Managed only" toggle on the Sessions table, default on.

## Testing

- `_find_ado_links` (generalized multi-match): unit tests for zero/one/two refs in one text blob,
  full-URL-wins-over-bare-ref (already covered for the single-match version, extend for multiplicity).
- Ticket status ladder: one test per rank with synthetic session+escalation+PR-state combinations,
  confirming first-match-wins ordering.
- Dispatch endpoint: a fake `parallel-task.sh` (or a recorded subprocess call) confirming the
  constructed prompt includes ticket description + instructions + conventions reminder; a failure
  path (non-zero exit) surfaces as the row's inline error, not a 500 with no body.
- Manual/live: the ADO WIQL query already verified against the real org during this brainstorm
  (returned 4 real tickets). Re-verify after implementation that the same query still authenticates
  and returns the same shape through the cached path.
