#!/usr/bin/env python3
"""Live status/log dashboard for parallel-task.sh copies."""

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import assignments as ledger
import manager_session
from escalations import (
    QUEUE_PATH,
    classify,
    current_state,
    is_undeliverable,
    normalize_kind,
    normalize_options,
    record_answer,
    record_dismiss,
)

PLUGIN_BIN = os.path.dirname(os.path.abspath(__file__))
PARALLEL_TASK_SH = os.path.join(PLUGIN_BIN, "parallel-task.sh")
MAX_BODY_BYTES = 64 * 1024


class _BodyTooLarge(ValueError):
    """Raised by Handler._read_json_body() when Content-Length exceeds MAX_BODY_BYTES, so callers
    can map it to 413 — the one status code that must survive that refactor unchanged, since every
    other bad-body error there gets 400."""


# Every subprocess-wrapping function below degrades silently on failure. TimeoutExpired
# subclasses SubprocessError, not OSError, so it must be listed via SubprocessError (its actual
# parent) rather than assumed to ride along with OSError.
_SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


def get_escalations(path: str = None) -> dict:
    """What the human still has to answer, and what the manager already decided for them."""
    path = path or QUEUE_PATH
    state = current_state(path)
    # options is worker-authored against a prose schema (same tolerance assignments.py gives
    # `plan`) — normalize the in-memory copy so every consumer downstream gets a real list, no
    # matter what shape actually landed on disk. Never rewritten to the queue file itself.
    for r in state:
        r["options"] = normalize_options(r.get("options"))
        # The canonical kind rides alongside the raw one rather than replacing it: the card
        # shows what the worker actually wrote (so drift stays visible and fixable) while
        # severity keys off the name the vocabulary recognises. None means it named no kind.
        r["kind_canonical"] = normalize_kind(r.get("kind"))
    needs_human = [r for r in state if r.get("status") == "needs_human"]
    for r in state:
        if r.get("status") == "open":
            try:
                is_tier3 = classify(r)[0] == "tier3"
            except Exception:
                is_tier3 = True  # can't classify it -> fail closed, show it to a human
            if is_tier3:
                needs_human.append(r)
    decisions = [r for r in state if r.get("decided_by") == "manager"]
    decisions.sort(key=lambda r: r.get("answered_at") or 0, reverse=True)
    return {"needs_human": needs_human, "recent_decisions": decisions[:20]}


def get_assignments(path: str = None, now: float = None) -> list[dict]:
    """Every assignment, each decorated with the three derived values the UI needs."""
    path = path or ledger.LEDGER_PATH
    now = now if now is not None else time.time()
    rows = []
    for rec in current_state(path):
        rows.append(
            {
                **rec,
                "at_risk": ledger.at_risk(rec, now),
                "stalled": ledger.stalled(rec, now),
                "progress": ledger.progress(rec),
            }
        )
    return rows


# The repo whose copies we report on. Set once in main(); `parallel-task.sh` finds
# its registry via `git rev-parse --show-toplevel`, so this must be inside the
# target repo — NOT the plugin's own directory, which is typically installed under
# ~/.claude/plugins/ and isn't a git repo at all.
REPO_DIR = os.getcwd()


# Both sources shell out to CLIs costing ~0.5s each. The page polls two endpoints every
# 2s and both call get_tasks(), so uncached the calls overlap, pile up, and the server
# stops answering (measured: 15s per request). One snapshot per TTL is plenty — nothing
# here changes faster than the poll interval.
_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 1.5
# One lock PER KEY, not one lock for the cache. The server is threaded, so several endpoints
# refresh at once — and they hold their lock for as long as their source takes. `ado_backlog`
# shells out to `az` and takes tens of seconds; under a single shared lock it froze every other
# endpoint for that whole time, so one slow external CLI made the entire board unresponsive.
# Per key, a slow refresh only delays readers of that same key, which is the point of the lock.
_CACHE_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def _cache_lock(key: str) -> threading.Lock:
    """The lock for one cache key, created on first use. The guard is held only long enough to
    hand one back — never across the refresh itself, which is the mistake being fixed here."""
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.Lock())


def _cached(key: str, fn, ttl: float = _CACHE_TTL):
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    with _cache_lock(key):  # one refresh at a time per key; latecomers take the fresh value
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = fn()
        _CACHE[key] = (time.monotonic(), value)
        return value


# PR/ticket lookups hit the network (gh) or a subprocess (git) — much pricier than the
# 1.5s-TTL local calls above, and branch/PR association barely changes turn to turn.
_ENRICH_TTL = 30.0

_ADO_URL_RE = re.compile(r"https?://dev\.azure\.com/\S+/_workitems/edit/(\d+)")
_ADO_REF_RE = re.compile(r"\bAB[#-](\d+)\b", re.IGNORECASE)
_ADO_DEFAULT_BASE = "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/"

_EMPTY_PR_TICKET = {"pr_number": None, "pr_url": None, "pr_state": None, "ado_refs": []}


def _find_ado_links(text: str) -> list[dict]:
    """Every ADO reference in the text, deduplicated by id, first-seen order. A full
    dev.azure.com URL wins over a bare AB#NNNN ref for the same id (keeps whatever org/project
    form the author actually used instead of assuming this repo's default one)."""
    by_id: dict[str, str] = {}
    order: list[str] = []
    # Collect all matches with their positions
    matches = []
    for m in _ADO_URL_RE.finditer(text):
        matches.append((m.start(), "url", m.group(1), m.group(0)))
    for m in _ADO_REF_RE.finditer(text):
        matches.append((m.start(), "ref", m.group(1), _ADO_DEFAULT_BASE + m.group(1)))
    # Sort by position in text, process in order
    matches.sort(key=lambda x: x[0])
    for pos, match_type, ticket_id, url in matches:
        if ticket_id not in by_id:
            order.append(ticket_id)
            by_id[ticket_id] = url
        elif match_type == "url":
            # URL wins over bare ref for the same ID
            by_id[ticket_id] = url
    return [{"id": i, "url": by_id[i]} for i in order]


def _git_branch(path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, "branch", "--show-current"], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() or None
    except _SUBPROC_ERRORS:
        return None


def _lookup_pr_and_ticket(branch: str) -> dict:
    """This repo's convention (seen in real commit/PR history): PR titles/bodies carry an
    AB#NNNN or full ADO link — e.g. 'fix: ... (AB#7160)' or a `[AB#6541](https://dev.azure...)`
    markdown link in the body. One `gh` call gets both; no separate ADO API access needed."""
    if not branch:
        return dict(_EMPTY_PR_TICKET)
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "number,url,title,body,state",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=REPO_DIR,
        )
        prs = json.loads(result.stdout) if result.returncode == 0 else []
    except _SUBPROC_ERRORS:
        prs = []
    if not prs:
        return dict(_EMPTY_PR_TICKET)
    pr = prs[0]
    ado_refs = _find_ado_links(f"{pr.get('title', '')}\n{pr.get('body') or ''}")
    return {
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url"),
        "pr_state": pr.get("state"),
        "ado_refs": ado_refs,
    }


def _enrich_branch_and_links(cwd: str, known_branch: str | None, known_ado_ids: list[str] | None = None) -> dict:
    """cwd is the empty string for a shared checkout (the repo root, not a dedicated worktree):
    its "current branch" belongs to whichever session last ran `git checkout` there, not to any
    one session, so deriving one would misattribute a PR/ticket to every session sharing it."""
    branch = known_branch
    if not branch and cwd:
        branch = _cached(f"branch:{cwd}", lambda: _git_branch(cwd), ttl=_ENRICH_TTL)
    registry_refs = [{"id": i, "url": _ADO_DEFAULT_BASE + i} for i in (known_ado_ids or [])]
    if not branch:
        merged = list(registry_refs)
        return {"branch": branch, **_EMPTY_PR_TICKET, "ado_refs": merged}
    pr_ticket = _cached(f"prticket:{branch}", lambda: _lookup_pr_and_ticket(branch), ttl=_ENRICH_TTL)
    seen = {r["id"] for r in registry_refs}
    merged = list(registry_refs) + [r for r in pr_ticket["ado_refs"] if r["id"] not in seen]
    return {"branch": branch, **pr_ticket, "ado_refs": merged}


def get_registry() -> list[dict]:
    """Copies provisioned by `parallel-task.sh start` — branch, ports, dev-stack state."""

    def run():
        result = subprocess.run(
            [PARALLEL_TASK_SH, "list", "--json"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_DIR,
            timeout=20,
        )
        return json.loads(result.stdout)

    return _cached("registry", run)


def get_sessions() -> list[dict]:
    """Every live Claude session, whether or not parallel-task.sh created it."""

    def run():
        result = subprocess.run(
            [manager_session.claude_bin(), "agents", "--json", "--all"],
            capture_output=True, text=True, check=True, timeout=20,
        )
        return json.loads(result.stdout)

    return _cached("sessions", run)


def get_github_prs(repo_root: str) -> list[dict]:
    """Every PR `gh` knows about for `repo_root`'s remote, open and closed/merged alike — a
    merged PR is exactly what a reviewer wants to see on a ticket that's already done.
    board_state.prs_by_ticket() turns this into the ticket-id-keyed map the board publishes;
    this function only fetches.

    `repo_root` is taken as an argument rather than read off module-level REPO_DIR: this can be
    called from board_state.py's own entry point, which never runs dashboard.main() and so never
    updates REPO_DIR — the same trap read_registry() in board_state.py works around already.
    """

    def gh_pr_list(state, fields):
        result = subprocess.run(
            ["gh", "pr", "list", "--state", state, "--limit", "500", "--json", fields],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
            cwd=repo_root,
        )
        return json.loads(result.stdout)

    def run():
        # `reviewDecision` rides along free; `statusCheckRollup` does NOT. GitHub resolves the
        # check rollup per PR, so asking for it across `--state all --limit 500` took 42.5s
        # against this call's own 20s timeout — measured on the repo the board pump actually
        # sweeps. Every run hit the timeout, fell back to {} and blanked the board's PR column,
        # while still spending 20s of the pump's budget to do it.
        #
        # Split, measured on that same repo: the wide sweep without the rollup is 12.5s for all
        # 500 PRs, and an open-only rollup lookup is 1.6s for the 11 that have one. Nothing is
        # lost — board_state.ticket_status() reads `checks` only inside `if pr["state"] ==
        # "OPEN"`, so the rollup was being fetched for 489 PRs that never consult it. A merged
        # PR's CI is history, not a signal.
        #
        # Two calls CAN disagree about which PRs exist, which is why the second is joined onto
        # the first by number rather than replacing it: a PR that closed between the two simply
        # gets no rollup, and one that opened is absent from the wide sweep either way.
        prs = gh_pr_list("all", "number,title,url,state,isDraft,reviewDecision")
        rollups = {
            pr.get("number"): pr.get("statusCheckRollup")
            for pr in gh_pr_list("open", "number,statusCheckRollup")
        }
        for pr in prs:
            if pr.get("state") == "OPEN" and pr.get("number") in rollups:
                pr["statusCheckRollup"] = rollups[pr["number"]]
        return prs

    return _cached(f"github_prs:{repo_root}", run, ttl=_ENRICH_TTL)


_ADO_ORG = "https://dev.azure.com/agentiqai"
_ADO_PROJECT = "AgentIQ"
# Done work stays on the board so a manager can see it was finished, not just that it
# vanished — but only while it is still recent. An unbounded Closed set is a year of history
# that buries the handful of tickets still needing a decision.
_ADO_DONE_WINDOW_DAYS = 14


def _ado_identities() -> list[str]:
    """The board owner's ADO identities, unescaped and comma-split — the one source of "who is
    the board owner" that _ado_assignee_clause() (WIQL, SQL-escaped) and
    board_state._ticket_ownership() (compared against AssignedTo/ActivatedBy) both read, so a
    ticket's handoff status can never disagree with which tickets the backlog query fetched in
    the first place. Empty when PWR_ADO_ASSIGNED_TO is unset.
    """
    raw = os.environ.get("PWR_ADO_ASSIGNED_TO", "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _ado_assignee_clause() -> str:
    """Who counts as "me" on this board.

    Matches EITHER System.AssignedTo or Microsoft.VSTS.Common.ActivatedBy, so a ticket the owner
    started but has since handed off (to QC, to another dev) still reaches the backlog instead of
    silently disappearing the moment AssignedTo changes — board_state._ticket_ownership() is what
    turns that into "done, handed off to X" rather than just leaving it there unmarked.

    Defaults to WIQL's own @Me, which resolves to the identity `az` is logged in as. That is
    wrong wherever one person holds two ADO identities (a company UPN and a client-tenant one):
    @Me then matches whichever one `az` authenticated, and the other's tickets never appear —
    a silently half-empty board, not an error. PWR_ADO_ASSIGNED_TO takes a comma-separated
    list of identities to union instead.
    """
    id_fields = ("[System.AssignedTo]", "[Microsoft.VSTS.Common.ActivatedBy]")
    people = [p.replace("'", "''") for p in _ado_identities()]
    if not people:
        # Said out loud, because this is the one degradation here that looks exactly like a
        # correct answer: @Me returns a real, well-formed, SHORTER backlog. Measured on this
        # project 2026-09-09 — two identities union to 168 tickets, @Me alone returns 21 — so a
        # run that loses this variable publishes an eighth of the board with no error anywhere,
        # and board_mirror_diff.py reconciles the missing seven eighths away. run-board-mirror.sh
        # guards ARTIFACT_URL and PWT_REPO_ROOT with `:?` and cannot guard this one, because a
        # single-identity install is a legitimate configuration; a journal line is what makes the
        # difference between the two visible after the fact.
        print(
            "dashboard: PWR_ADO_ASSIGNED_TO is unset — the backlog covers only the identity `az` "
            "is logged in as (WIQL @Me), not every identity of the board owner",
            file=sys.stderr,
        )
        return "(" + " OR ".join(f"{f} = @Me" for f in id_fields) + ")"
    joined = ", ".join(f"'{p}'" for p in people)
    return "(" + " OR ".join(f"{f} IN ({joined})" for f in id_fields) + ")"


def _ado_backlog_wiql() -> str:
    return (
        "SELECT [System.Id], [System.Title], [System.State], [System.IterationPath], "
        "[System.AssignedTo], [Microsoft.VSTS.Common.ActivatedBy] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{_ADO_PROJECT}' AND {_ado_assignee_clause()} "
        "AND [System.State] <> 'Removed' "
        "AND ([System.State] <> 'Closed' "
        f"OR [System.ChangedDate] >= @Today - {_ADO_DONE_WINDOW_DAYS})"
    )


def _shape_ado_ticket(raw: dict) -> dict:
    fields = raw.get("fields") or {}
    ticket_id = str(raw.get("id") or fields.get("System.Id") or "")
    return {
        "id": ticket_id,
        "title": fields.get("System.Title") or "",
        "state": fields.get("System.State") or "",
        # Only the leaf of the iteration path — "AgentIQ\\Sprint 57" is how ADO stores it and
        # "Sprint 57" is the only part anyone filters by. Tickets parked at the project root
        # have no sprint leaf to speak of and come back "".
        "sprint": (fields.get("System.IterationPath") or "").split("\\")[-1],
        "url": _ADO_DEFAULT_BASE + ticket_id,
    }


def _ado_identity_ref(value) -> dict:
    """{"email", "name"} off one ADO identity field, or both None.

    `az boards query` omits an identity field's key entirely when it was never set (AssignedTo on
    a brand-new ticket, ActivatedBy on one never moved to Active) rather than returning it as
    null, and a set field comes back as a dict carrying GraphProfile/avatar links this board has
    no use for besides `uniqueName` (the email both WIQL and PWR_ADO_ASSIGNED_TO key on) and
    `displayName`.
    """
    if not isinstance(value, dict):
        return {"email": None, "name": None}
    return {"email": value.get("uniqueName"), "name": value.get("displayName")}


def get_ado_backlog() -> list[dict]:
    """Tickets assigned to you, not closed — the manager's read-only view into ADO.

    RAISES when the sweep did not run, and returns [] only for a sweep that ran and matched
    nothing. Those two were the same value here until 2026-09-09, and collapsing them is what
    let a failed `az` call delete ticket rows off the published board: board_state.py's contract
    (see its `read_tickets=` comment) is that only an exception means "did not run", and a
    function written never to raise made that contract unenforceable — `_safe` stamped
    last_ado_sweep on a sweep that never happened, and board_mirror_diff.py turned the resulting
    empty ticket set into a delete per row.

    This is the one reader in this file that does NOT degrade to empty, on purpose. The others
    (get_ado_iterations, get_registry) degrade because their failure costs a derived nicety;
    this one's failure costs the rows themselves. Both callers are already built for it:
    board_state.py wraps it in `_safe()`, and /api/ado-tickets already answers 500 on any
    exception — a better answer for a human than an empty table that reads as a finished sprint.

    `az` prints `null`, not `[]`, when the query matches nothing. That IS a successful empty
    sweep, so it is normalized to [] here rather than left to raise on iteration — otherwise the
    one case the contract calls "successful and empty" would be reported as a failure.
    """

    def run():
        result = subprocess.run(
            ["az", "boards", "query", "--org", _ADO_ORG, "--wiql", _ado_backlog_wiql(), "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"az boards query exited {result.returncode}: {(result.stderr or '').strip()[:300]}"
            )
        tickets = []
        for r in json.loads(result.stdout) or []:
            fields = r.get("fields") or {}
            tickets.append(
                {
                    **_shape_ado_ticket(r),
                    "assigned_to": _ado_identity_ref(fields.get("System.AssignedTo")),
                    "activated_by": _ado_identity_ref(fields.get("Microsoft.VSTS.Common.ActivatedBy")),
                }
            )
        return tickets

    return _cached("ado_backlog", run, ttl=60.0)


def _iteration_leaves(node: dict) -> list[dict]:
    """Every leaf iteration under `node`, walked recursively. This project's tree is flat today
    (verified live: every child is already a leaf) but a group node is exactly as valid a shape
    for `az` to hand back, and assuming one level deep would silently drop half the tree the day
    this project (or another one this ever gets pointed at) organises iterations under a release
    node. Each leaf keeps only what board_state.py's date math needs — name, start, finish — the
    date parsing itself stays out of this file, same split as `_shape_ado_ticket` vs. the pure
    transforms in board_state.py."""
    children = node.get("children") or []
    if not children:
        attrs = node.get("attributes") or {}
        return [{"name": node.get("name"), "start": attrs.get("startDate"), "finish": attrs.get("finishDate")}]
    leaves = []
    for child in children:
        leaves.extend(_iteration_leaves(child))
    return leaves


def get_ado_iterations() -> list[dict]:
    """Every sprint/iteration this project defines, with its date range — the ONLY place sprint
    boundaries live. A ticket's `System.IterationPath` is just a name; ADO never puts a date on
    the ticket itself, so knowing which sprint is "today" requires this separate lookup. Unlike
    get_ado_backlog(), this one DOES degrade to empty: `az` failing here costs only the default
    sprint filter (the page falls back to "tất cả"), never a row, so there is nothing for a
    caller to tell apart."""

    def run():
        result = subprocess.run(
            [
                "az", "boards", "iteration", "project", "list",
                "--project", _ADO_PROJECT, "--org", _ADO_ORG, "--depth", "3", "-o", "json",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return []
        return _iteration_leaves(json.loads(result.stdout))

    try:
        return _cached("ado_iterations", run, ttl=60.0)
    except _SUBPROC_ERRORS:
        return []


def get_tasks() -> list[dict]:
    """Sessions running in this repo, enriched with registry data where it exists.

    Session-first, not registry-first: a session doing work is visible immediately —
    during provisioning, and for worktrees created by any other means. The registry
    only adds branch/ports/dev-stack once a copy has been fully provisioned.
    """
    try:
        registry = get_registry()
    except Exception:
        registry = []  # a broken/absent registry must not hide live sessions
    by_session = {r["session_id"]: r for r in registry if r.get("session_id")}

    repo = os.path.realpath(REPO_DIR)
    rows, seen = [], set()
    for s in get_sessions():
        cwd = s.get("cwd") or ""
        if not os.path.realpath(cwd).startswith(repo):
            continue  # other repos aren't this dashboard's business
        sid = s.get("sessionId")
        reg = by_session.get(sid, {})
        seen.add(sid)
        rows.append(
            {
                "task": reg.get("task") or s.get("name") or (cwd.rsplit("/", 1)[-1] or "-"),
                "path": reg.get("path") or cwd,
                "session_id": sid,
                "short_id": reg.get("short_id") or s.get("id"),
                "agent_status": s.get("status"),
                "agent_state": s.get("state"),
                "kind": s.get("kind"),
                "started_at": s.get("startedAt"),
                "mode": reg.get("mode"),
                "ports": reg.get("ports"),
                "dev_status": reg.get("dev_status"),
                "managed": bool(reg),
                **_enrich_branch_and_links(
                    cwd if os.path.realpath(cwd) != repo else "", reg.get("branch"), reg.get("ado_ids")
                ),
            }
        )

    # Registered copies whose session already exited still matter (stack may be up).
    for r in registry:
        if r.get("session_id") not in seen:
            rows.append(
                {
                    **r,
                    "kind": None,
                    "started_at": None,
                    "managed": True,
                    **_enrich_branch_and_links(r.get("path", ""), r.get("branch"), r.get("ado_ids")),
                }
            )

    rows.sort(key=lambda r: (not r["managed"], r["task"] or ""))
    return rows


def start_manager_turn(text: str, source: str, ask=None) -> threading.Thread:
    """Run one manager turn off the request thread.

    A manager call takes tens of seconds and may take ten minutes; holding an HTTP request open for
    it would stall the browser and time out the fetch. The turn is already recorded in the chat log
    by ask(), so the UI learns the answer by polling GET /api/manager/chat.

    The target is a wrapper, not ask() itself: ask() can still raise ManagerBusy if the lock wait
    times out (a second turn arriving while this one already holds the lock past LOCK_TIMEOUT), and
    manager_session logs that to the chat before re-raising — so all that's left to do here is stop
    it (or anything else ask() raises) from escaping as an unhandled-thread traceback.
    """
    ask = ask or manager_session.ask
    # Read at spawn time, not at import: the CTO can change this between turns and the next
    # turn must honour it without restarting the dashboard.
    prefs = manager_session.read_prefs()

    def _run() -> None:
        try:
            ask(text, source, model=prefs["model"], effort=prefs["effort"])
        except Exception as e:
            print(f"manager turn failed: {e}", file=sys.stderr)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def manager_chat_payload(history=None, busy=None) -> dict:
    """Chat entries plus whether a turn is in flight. Degrades to empty, never raises."""
    history = history or manager_session.history
    busy = busy or manager_session.busy
    try:
        entries = history()
    except (OSError, ValueError):
        entries = []
    try:
        in_flight = busy()
    except OSError:
        in_flight = False
    return {"entries": entries, "busy": in_flight}


# Serialises the busy-check-and-spawn pair so a burst of requests cannot each spawn a paid
# manager turn in the window before the first one acquires the session lock.
# ponytail: process-local only — a second dashboard process, or the daemon, can still race
# this; manager_session's own flock is what keeps that safe, at the cost of a redundant call.
_MANAGER_SPAWN_LOCK = threading.Lock()


def manager_prefs_payload() -> dict:
    """The chat path's current model/effort plus everything it may be set to, so the UI never
    hardcodes a vocabulary that lives in manager_session."""
    return {
        **manager_session.read_prefs(),
        "models": list(manager_session.MANAGER_MODELS),
        "efforts": list(manager_session.MANAGER_EFFORTS),
    }


def _manager_prefs_post(body: dict) -> tuple[int, dict]:
    """(status, body) for a prefs POST. A rejected value returns 400 with the reason, never a
    silent fallback — a picker that appears to accept a choice it did not store is worse than
    an error."""
    if not isinstance(body, dict):
        return 400, {"error": "expected a JSON object"}
    try:
        saved = manager_session.write_prefs(body.get("model"), body.get("effort"))
    except ValueError as e:
        return 400, {"error": str(e)}
    except OSError as e:
        return 500, {"error": f"could not save: {e}"}
    return 200, saved


def _manager_chat_post(text: str, busy=None, start=None) -> tuple[int, dict]:
    """(status, body) for a manager/chat POST.

    The busy check happens here, on the request thread, before anything is spawned:
    start_manager_turn returns as soon as its thread is launched, so the ManagerBusy that thread's
    ask() can raise happens inside it, never on the thread that's still holding this request — a
    `try/except ManagerBusy` wrapped around start_manager_turn would never see it. Checking busy()
    first gives the caller the same 503 signal from a place that can actually return it.

    busy() and start() are serialised by _MANAGER_SPAWN_LOCK: without it, a burst of concurrent
    requests can each observe busy()==False in the window before the first one's spawned thread
    reaches manager_session's own flock, and each would spawn its own paid manager call. The lock
    covers only this fast check-then-spawn pair, never the model call itself, so no request is held
    across it. start() can also raise RuntimeError (thread.start() under OS thread exhaustion) —
    caught here so this route returns 503 instead of dropping the connection.
    """
    busy = busy or manager_session.busy
    start = start or start_manager_turn
    with _MANAGER_SPAWN_LOCK:
        if busy():
            return 503, {"error": "manager busy"}
        try:
            start(text, "cto")
        except RuntimeError as e:
            return 503, {"error": f"could not start manager turn: {e}"}
    return 202, {"ok": True}


_SAFE_URL_SCHEMES = ("http://", "https://")


def _parse_ado_refs(raw):
    """Normalise the ado_refs field, raising ValueError with a caller-facing message.

    A list of bare id strings is the shape the retired ticket-dispatch route used, so it is the
    mistake a caller is most likely to make. It must produce a 400, not a dropped connection.

    `url` ends up in an `<a href>` in dashboard.html with no CSP on the page — the manager also
    writes ledger records straight through `assignments.append(rec)` with no validation at all, so
    this is the only check some of these values ever see. Anything other than http(s)
    (`javascript:`, `data:`, a scheme-relative `//host`) becomes "" rather than reaching the page,
    the same default-to-safe treatment a non-string url already got.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("ado_refs must be a list")
    refs = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each ado_refs entry must be an object with id and url")
        rid = item.get("id")
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError("each ado_refs entry needs a non-empty string id")
        url = item.get("url")
        if not (isinstance(url, str) and url.lower().startswith(_SAFE_URL_SCHEMES)):
            url = ""
        refs.append({"id": rid, "url": url})
    return refs


def _assignments_post(body, append_fn=None, start=None):
    """Record one assignment and tell the manager. Returns (status, payload); never raises.

    The ledger write is what must not be lost — it is the durable commitment. Failing to notify
    the manager is reported but does not fail the request, because the daemon's tick walks open
    assignments and will rediscover it.
    """
    append_fn = append_fn or ledger.append
    start = start or start_manager_turn
    try:
        refs = _parse_ado_refs(body.get("ado_refs"))
        title = body.get("title") or ""
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        record = ledger.new_assignment(
            title,
            priority=body.get("priority") or "P1",
            deadline=body.get("deadline"),
            ado_refs=refs,
        )
    except (ValueError, AttributeError, TypeError) as e:
        # Broad on purpose: this is a request boundary, and every JSON type a caller can send must
        # become a 400. Dropping the connection with no response is strictly worse than a 400 that
        # names the wrong cause.
        return 400, {"error": str(e)}
    try:
        append_fn(record)
    except OSError as e:
        return 500, {"error": f"could not write the ledger: {e}"}
    names = ", ".join(f"AB#{r['id']}" for r in record["ado_refs"]) or "none"
    try:
        start(
            f"New assignment {record['id']}: {record['title']}\n"
            f"Priority {record['priority']}, deadline {record['deadline'] or 'none'}, "
            f"ADO refs: {names}.\n"
            "It is already in the ledger. Plan it, size each step, dispatch, and confirm in one line.",
            "cto",
        )
    except Exception as e:
        return 202, {"ok": True, "record": record, "warning": f"manager not notified: {e}"}
    return 202, {"ok": True, "record": record}


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        """Parse the POST body as a JSON object, enforcing MAX_BODY_BYTES. Raises ValueError with
        the message to show the client — _BodyTooLarge (a ValueError subclass) for an oversized
        body so callers can still give it its own status code, plain ValueError for anything else
        wrong with the body."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise ValueError(f"bad request body: {e}") from e
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge("request body too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"bad request body: {e}") from e
        if not isinstance(body, dict):
            raise ValueError("bad request body: body must be a JSON object")
        return body

    def _body_error(self, e: ValueError) -> None:
        """The response for a _read_json_body() failure — 413 for an oversized body, 400 for
        everything else _read_json_body() (or a route's own body-shape validation) raises."""
        self._json({"error": str(e)}, status=413 if isinstance(e, _BodyTooLarge) else 400)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tasks":
            try:
                self._json(get_tasks())
            except (*_SUBPROC_ERRORS, subprocess.CalledProcessError) as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/api/escalations":
            try:
                self._json(get_escalations())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/api/assignments":
            try:
                self._json(get_assignments())
            except (OSError, ValueError) as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/api/ado-tickets":
            try:
                self._json(get_ado_backlog())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path == "/api/manager/chat":
            self._json(manager_chat_payload())
            return
        if parsed.path == "/api/manager/prefs":
            self._json(manager_prefs_payload())
            return
        if parsed.path == "/":
            html_path = os.path.join(PLUGIN_BIN, "dashboard.html")
            if not os.path.exists(html_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        # Browsers omit Origin entirely for a same-origin request (which is how the dashboard's
        # own fetch("/api/...") calls look) but always send it on a cross-origin one — so this
        # blocks another open tab from firing a drive-by POST (worktree provisioning, a real
        # dispatched session) at this server without needing CORS preflight to save it.
        origin = self.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{self.server.server_address[1]}":
            self._json({"error": "cross-origin POST refused"}, status=403)
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/escalations/") and parsed.path.endswith("/answer"):
            rid = parsed.path[len("/api/escalations/") : -len("/answer")]
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._body_error(e)
                return
            answer = (body.get("answer") or "").strip()
            if not answer:
                self._json({"error": "answer is required"}, status=400)
                return
            try:
                state = {r["id"]: r for r in current_state(QUEUE_PATH) if r.get("id")}
            except OSError as e:
                self._json({"error": str(e)}, status=500)
                return
            existing = state.get(rid)
            if existing is None:
                self._json({"error": f"no escalation {rid}"}, status=404)
                return
            # Decline rather than append a conflicting state: someone may already have acted
            # on the existing answer, and this endpoint cannot undo that.
            if existing.get("status") != "needs_human":
                self._json(
                    {"error": f"escalation {rid} is already {existing.get('status')}", "record": existing},
                    status=409,
                )
                return
            try:
                updated = record_answer(QUEUE_PATH, rid, answer, "human")
            except OSError as e:
                self._json({"error": str(e)}, status=500)
                return
            if updated is None:
                self._json({"error": f"no escalation {rid}"}, status=404)
                return
            self._json({"ok": True, "record": updated})
            return
        if parsed.path.startswith("/api/escalations/") and parsed.path.endswith("/dismiss"):
            rid = parsed.path[len("/api/escalations/") : -len("/dismiss")]
            try:
                state = {r["id"]: r for r in current_state(QUEUE_PATH) if r.get("id")}
            except OSError as e:
                self._json({"error": str(e)}, status=500)
                return
            existing = state.get(rid)
            if existing is None:
                self._json({"error": f"no escalation {rid}"}, status=404)
                return
            # Scoped to the undeliverable shape on purpose, not every needs_human record: a
            # genuine open escalation still wants a real answer, not a silent close.
            if not is_undeliverable(existing):
                self._json(
                    {"error": f"escalation {rid} is not an undeliverable record", "record": existing},
                    status=409,
                )
                return
            try:
                updated = record_dismiss(QUEUE_PATH, rid, "cto")
            except OSError as e:
                self._json({"error": str(e)}, status=500)
                return
            if updated is None:
                self._json({"error": f"no escalation {rid}"}, status=404)
                return
            self._json({"ok": True, "record": updated})
            return
        if parsed.path == "/api/assignments":
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._body_error(e)
                return
            status, resp = _assignments_post(body)
            self._json(resp, status=status)
            return
        if parsed.path == "/api/manager/chat":
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._body_error(e)
                return
            text = (body.get("text") or "").strip()
            if not text:
                self._json({"error": "text is required"}, status=400)
                return
            status, resp = _manager_chat_post(text)
            self._json(resp, status=status)
            return
        if parsed.path == "/api/manager/prefs":
            try:
                body = self._read_json_body()
            except ValueError as e:
                self._body_error(e)
                return
            status, resp = _manager_prefs_post(body)
            self._json(resp, status=status)
            return
        if parsed.path == "/api/manager/reset":
            manager_session.reset()
            self._json({"ok": True})
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # ponytail: quiet by default; add real logging if this needs debugging later


def _argv_repo_dir(args: list[str]) -> str | None:
    """The explicit repo dir from dashboard.py's own argv (port and flags already stripped), or
    None when the caller didn't pass one. See manager_session.resolve_repo_root() for how this
    combines with PWT_REPO_ROOT and cwd — the same precedence manager_daemon.py's main() uses."""
    repo_args = [a for a in args if not a.isdigit()]
    return os.path.abspath(repo_args[0]) if repo_args else None


def main():
    global REPO_DIR
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    port = int(args[0]) if args and args[0].isdigit() else 4400
    # PWT_REPO_ROOT first, then this argv, then cwd — manager_daemon.py's main() uses the same
    # precedence (it just has no argv tier of its own). Both feed the SAME manager session, so the
    # manager's `claude` subprocess (and the Claude Code permissions it resolves from cwd) must
    # land in the same place regardless of which entry point last woke it.
    REPO_DIR = manager_session.resolve_repo_root(_argv_repo_dir(args))
    manager_session.REPO_ROOT = REPO_DIR

    # Fail loudly at startup rather than serving 500s: a wrong repo dir is the
    # difference between "no copies running" and "you're looking at the wrong repo".
    try:
        tasks = get_tasks()
    except Exception as e:
        print(f"error: cannot read parallel-task registry in {REPO_DIR}\n  {e}", file=sys.stderr)
        print("hint: run from your repo root, or pass it: dashboard.py [port] <repo-dir>", file=sys.stderr)
        raise SystemExit(1)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}  (repo: {REPO_DIR}, {len(tasks)} copies)")
    server.serve_forever()


if __name__ == "__main__":
    main()
