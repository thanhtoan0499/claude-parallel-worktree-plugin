#!/usr/bin/env python3
"""File a worker frozen on a prompt nobody will answer into the queue the manager already works.

Run: stuck_sessions.py [queue-path]

A dispatched worker that hits a permission prompt it cannot answer stops dead: `claude agents
--json` reports it `blocked` and it stays that way forever, because nobody is there to answer.
On 2026-09-09 four sessions sat like that for up to two hours each and every one was found only
because the CTO asked why nothing was happening. The state was queryable the whole time; nothing
carried it anywhere. This carries it.

It carries it into `escalations.jsonl` as an ORDINARY open record, not as something waiting on a
human — see the `stuck_session` note in escalations.py for why. `manager_daemon.process_open`
then decides it exactly like any other tier-2 record, and degrades it to `needs_human` only once
the manager's own attempts are exhausted. There is no second path here.

Read-only with respect to every session it looks at: it never resumes, answers, stops or kills
anything. The only thing it writes is the queue.
"""

import collections
import json
import os
import sys

from board_state import claim_stale_after, session_docs, session_transcripts, timer_period_seconds
from escalations import QUEUE_PATH, append, current_state, new_record, normalize_kind, record_dismiss

KIND = "stuck_session"
DECIDED_BY = "stuck-session-watch"

# How much of a transcript's tail to read looking for the command that raised the prompt. The
# tool_use that blocked is the last one written, so this only has to outrun the handful of
# system/attachment lines that follow it.
TAIL_LINES = 200

# Long enough to name the command and its first argument, short enough that a queue record stays
# a queue record — the board renders every evidence value inline.
PENDING_CHARS = 400


def last_tool_use(lines) -> str | None:
    """`"<Tool>: <command>"` for the last tool call in a transcript tail, or None.

    This is the field that turns "session X is blocked" into something the manager can act on
    without investigating: three of the four sessions frozen on 2026-09-09 were one line away
    from moving, and the line was the pending command (`rm -rf ...` → `vite --force`,
    a `git checkout` the manager could run itself).

    Every line is untrusted, model-authored and routinely torn mid-write, so a line that will not
    parse or is not shaped like a message is skipped rather than raised on.
    """
    found = None
    for raw in lines:
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            payload = block.get("input")
            if isinstance(payload, dict) and isinstance(payload.get("command"), str):
                detail = payload["command"]
            else:
                detail = json.dumps(payload, ensure_ascii=False)
            found = f"{block.get('name') or '?'}: {detail}"[:PENDING_CHARS]
    return found


def probe_transcript(session_id: str) -> dict | None:
    """`{"last_activity": <epoch>, "pending": <str|None>}`, or None when there is no transcript.

    `last_activity` is the transcript's mtime, NOT the session's `startedAt`. A frozen session
    writes nothing, so the mtime is exactly when it stopped moving — measured live on 2026-09-09,
    `fix720` had been silent 1.3h while `startedAt` said 2.7h. Sizing the freeze off `startedAt`
    would have called a session stuck for hours one second after it hit its first prompt.

    None is "cannot measure", and the caller files nothing on it. A duration is the whole basis
    for calling something stuck; guessing one is how a staleness alarm ends up crying wolf.
    """
    paths = session_transcripts(session_id)
    if not paths:
        return None
    path = paths[-1]  # newest by mtime
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = collections.deque(f, maxlen=TAIL_LINES)
        return {"last_activity": os.path.getmtime(path), "pending": last_tool_use(tail)}
    except OSError:
        return None


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def stuck_now(agents, registry, probe, now: float, stale_after) -> tuple[dict, int]:
    """`({session_id: evidence}, unmanaged_count)` — who counts as stuck right this second.

    Four filters, each answering one way this check could cry wolf. A majority-false alarm gets
    ignored, and then the real one is ignored too.

    * `managed` — board_state.session_docs()'s own rule (a registry entry EXISTS), reused rather
      than restated. A session nobody dispatched is somebody's own terminal, and the human in
      front of it can answer their own prompt. The skipped ones are COUNTED and returned so the
      caller can say so out loud: silently filing nothing is how an operator never learns their
      dispatch path stopped writing registry rows.
    * `state == "waiting"` — normalize_state()'s canonical word for the CLI's `blocked`/`waiting`.
    * the worktree still exists — a session whose worktree was deleted is dead, not stuck. There
      is nothing left to unblock, so there is no decision for the manager to make. Five of the
      nine a by-hand check turned up were corpses, three of them exactly this shape.
    * silent for at least `stale_after` — and `stale_after` of None means the window could not be
      sized, which files NOTHING. This repo has already shipped a staleness alarm whose threshold
      was a stale constant; an unsized window is the same bug one step earlier.
    """
    if not stale_after:
        return {}, 0
    docs = session_docs(agents or [], registry or {})
    stuck, unmanaged = {}, 0
    for agent in agents or []:
        doc = docs.get(agent.get("name"))
        if not doc or doc["state"] != "waiting":
            continue
        if not doc["managed"]:
            unmanaged += 1
            continue
        sid = doc["session_id"]
        cwd = agent.get("cwd")
        if not sid or not cwd or not os.path.isdir(cwd):
            continue
        seen = probe(sid)
        if not isinstance(seen, dict) or seen.get("last_activity") is None:
            continue
        quiet = now - seen["last_activity"]
        if quiet < stale_after:
            continue
        stuck[sid] = {
            "pending": seen.get("pending") or "unknown",
            "waiting_for": agent.get("waitingFor") or "unknown",
            "stuck_for": _duration(quiet),
            "task": doc["task"],
            "worktree": doc["worktree"] or cwd,
            "branch": doc["branch"] or "unknown",
        }
    return stuck, unmanaged


def _question(ev: dict) -> str:
    return (
        f"Worker '{ev['task']}' has been frozen at a prompt for {ev['stuck_for']} and nobody is "
        "there to answer it. Say what it should do next."
    )


def _live_records(path: str) -> dict:
    """`{session_id: record}` for every stuck-session record still asking for something.

    Anything not yet `dismissed` counts — `open`, `needs_human`, and `answered` alike. `answered`
    especially: the answer is still on its way to the worker, which is sitting at the prompt until
    it lands, so treating that as settled would double-ask.
    """
    live = {}
    for rec in current_state(path):
        if normalize_kind(rec.get("kind")) != KIND:
            continue
        if rec.get("status") == "dismissed":
            live.pop(rec.get("session_id"), None)
        else:
            live[rec.get("session_id")] = rec
    return live


def scan(path: str, agents, registry, probe, now: float, stale_after, stuck=None) -> list[dict]:
    """One pass: file what is newly stuck, retire what is not stuck any more. Returns the actions.

    The condition persists across scans and the ledger is append-only, so both halves work off
    the folded latest-per-id state rather than trying to remember anything between runs. Filing a
    fresh record every scan would bury the queue within the hour; leaving one open after the
    session moved would leave the manager holding a decision nobody needs.

    `stuck` is for a caller that already ran stuck_now() and wants its counts — main() does — so
    the pass does not read every waiting session's transcript a second time to learn the same
    thing. Left None, this runs it itself.
    """
    if stuck is None:
        stuck, _ = stuck_now(agents, registry, probe, now, stale_after)
    live = _live_records(path)
    actions = []
    for sid, ev in stuck.items():
        if sid in live:
            continue
        rec = new_record(sid, KIND, _question(ev), evidence=ev)
        append(path, rec)
        actions.append({"action": "filed", "session_id": sid, "id": rec["id"], "task": ev["task"]})
    for sid, rec in live.items():
        if sid not in stuck:
            record_dismiss(path, rec["id"], DECIDED_BY)
            actions.append({"action": "cleared", "session_id": sid, "id": rec["id"],
                            "task": (rec.get("evidence") or {}).get("task")})
    return actions


def _unit_path(module_file: str = __file__) -> str:
    """This watch's own timer, which is what sizes the stuck window — no constant to go stale.

    realpath, not abspath: bin/systemd/README.md installs this script as a SYMLINK under
    ~/.config, and abspath keeps the symlink's own directory — which has no `systemd/` beside it,
    so every scheduled run died at "cannot read .../stuck-session-watch.timer" while the same
    script worked by hand from the repo. Python already resolves the link for sys.path, so the
    imports gave no hint. Resolving it here is what makes the shipped install path work.
    """
    return os.path.join(os.path.dirname(os.path.realpath(module_file)), "systemd", "stuck-session-watch.timer")


def main() -> int:
    """One scan, then exit. Every failure path returns non-zero so systemd records it.

    A check that silently does nothing is worse than no check: this repo's pump history is a
    catalogue of exactly that. Whatever happens here ends up in `journalctl --user -u
    stuck-session-watch.service` — including the boring "nothing is stuck" line, which is what
    tells an operator the timer is alive at all.
    """
    import time

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import manager_daemon
    import manager_session

    path = sys.argv[1] if len(sys.argv) > 1 else QUEUE_PATH
    try:
        with open(_unit_path(), encoding="utf-8") as f:
            stale_after = claim_stale_after(timer_period_seconds(f.read()))
    except OSError as e:
        print(f"stuck-session-watch: cannot read {_unit_path()}: {e} — filed nothing", file=sys.stderr)
        return 1
    if not stale_after:
        print(f"stuck-session-watch: no parseable cadence in {_unit_path()} — filed nothing", file=sys.stderr)
        return 1

    # No `--all`: that flag brings back every session the CLI has ever known (69 of them here
    # today, 32 with a worktree that no longer exists), and the corpses among them are exactly
    # what a by-hand version of this check cried wolf about. The live list is the population.
    agents = manager_daemon.list_agents(all_sessions=False)
    if not agents:
        print("stuck-session-watch: `claude agents --json` returned nothing — filed nothing", file=sys.stderr)
        return 1

    registry_file = os.environ.get(manager_daemon.REGISTRY_ENV) or os.path.join(
        manager_session.resolve_repo_root(), ".claude", "worktrees", ".parallel-registry.json"
    )
    try:
        with open(registry_file, encoding="utf-8") as f:
            registry = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"stuck-session-watch: cannot read registry {registry_file}: {e} — filed nothing", file=sys.stderr)
        return 1

    now = time.time()
    stuck, unmanaged = stuck_now(agents, registry, probe_transcript, now, stale_after)
    actions = scan(path, agents, registry, probe_transcript, now, stale_after, stuck=stuck)
    for a in actions:
        print(f"  {a['action']}: {a['task']} ({a['session_id']}) -> {a['id']}")
    print(
        f"stuck-session-watch: {len(agents)} live sessions, {len(stuck)} stuck past "
        f"{stale_after / 60:.0f}m, {len(actions)} queue writes -> {path}"
    )
    if unmanaged:
        # Not a failure, but never silent. A worker missing from the registry cannot be told
        # apart from someone's own terminal, so it is skipped — and if that number is not zero
        # while sessions are visibly frozen, the dispatch path has stopped writing registry rows
        # and THAT is the bug to fix, not this threshold.
        print(f"stuck-session-watch: skipped {unmanaged} waiting session(s) with no registry row", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
