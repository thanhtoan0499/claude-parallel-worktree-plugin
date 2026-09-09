#!/usr/bin/env python3
"""Watch the escalation queue: let the manager settle what it can, hand the rest to the human.

Run: manager_daemon.py [queue-path]
"""

import json
import os
import subprocess
import sys
import time
import traceback

import manager_session
from assignments import open_assignments
from escalations import QUEUE_PATH, append, classify, current_state, record_answer
from manager import build_prompt, decide, deliver_answer

DELIVERY_ATTEMPTS = 3

SEEN_PATH = os.path.expanduser("~/.claude/hermes/manager-seen-sessions.json")
TICK_SECONDS = int(os.environ.get("PWT_MANAGER_TICK_SECONDS", "1800"))
TICK_RETRY_SECONDS = 60
# A failing tick is retried sooner than a full interval, but a tick that keeps failing is not a
# blip — the live case was an expired OAuth session, which no amount of retrying fixes. At a flat
# 60s that burned a call (and wrote a failure line into the CTO's chat panel) every minute until
# a human logged in, thirty times the normal rate, which is how a broken manager ends up looking
# like a spamming one. Back off to the normal interval and no further: still self-healing the
# moment credentials come back, without hammering in the meantime.
TICK_RETRY_MAX_SECONDS = TICK_SECONDS

_consecutive_tick_failures = 0


def tick_retry_delay(failures: int) -> float:
    """Seconds to wait before retrying, after `failures` consecutive failed ticks."""
    if failures <= 1:
        return TICK_RETRY_SECONDS
    return min(TICK_RETRY_SECONDS * 2 ** (failures - 1), TICK_RETRY_MAX_SECONDS)


def reset_tick_failures() -> None:
    """Forget the failure streak. Called on every success, and by tests between cases."""
    global _consecutive_tick_failures
    _consecutive_tick_failures = 0


def _note_tick_failure(now: float, detail: str) -> float:
    """Record one failed tick and return the `last_tick` that schedules its retry."""
    global _consecutive_tick_failures
    _consecutive_tick_failures += 1
    delay = tick_retry_delay(_consecutive_tick_failures)
    print(
        f"  tick failed {_consecutive_tick_failures}x, retrying in {delay:.0f}s: {detail}",
        file=sys.stderr,
    )
    return now - TICK_SECONDS + delay
DONE_STATUSES = ("idle", "done", "stopped")

SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)

REGISTRY_ENV = "PWT_REGISTRY"


def registry_path() -> str:
    """Where this plugin records the copies it provisioned."""
    return os.environ.get(REGISTRY_ENV) or os.path.join(os.getcwd(), ".claude", "worktrees", ".parallel-registry.json")


def registry_session_ids(path: str | None = None) -> set:
    """Session ids this plugin actually dispatched.

    Fails CLOSED: an unreadable registry yields an empty set and therefore no wakes. Waking on
    an unknown session is worse than waking on none — every interactive session on the machine
    goes busy->idle after each reply, and each one would cost an Opus call.
    """
    try:
        with open(path or registry_path(), encoding="utf-8") as fh:
            registry = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(registry, dict):
        return set()
    return {entry["session_id"] for entry in registry.values() if isinstance(entry, dict) and entry.get("session_id")}


def list_agents(run=subprocess.run, all_sessions: bool = True) -> list[dict]:
    """Every session Claude Code knows about, or an empty list if the call fails.

    Degrading to empty rather than raising keeps a transient CLI failure from aborting the
    escalation pass, which is the more important half of a daemon cycle.

    `all_sessions=False` drops `--all` and asks only for the sessions still current. The daemon
    wants `--all` — it watches for a worker TRANSITIONING to done, and a session that dropped off
    the live list has still finished. A caller looking for sessions to act on wants the opposite:
    `--all` returned 69 rows here against 18 live ones, 32 of them for worktrees deleted days or
    months ago, and those corpses are what a by-hand stuck-session check cried wolf about.
    """
    try:
        proc = run(
            [manager_session.claude_bin(), "agents", "--json"] + (["--all"] if all_sessions else []),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        agents = json.loads(proc.stdout)
    except SUBPROC_ERRORS as exc:
        # Named, not swallowed. "Could not look" and "nothing is running" were the same empty
        # list here, and the pump ran for days on the first while everything downstream read the
        # second — see claude_bin() for what that cost.
        print(f"manager_daemon: list_agents failed, reporting no sessions: {exc}", file=sys.stderr)
        return []
    return agents if isinstance(agents, list) else []


def finished_sessions(agents: list[dict], seen: dict, known: set) -> tuple[list[dict], dict]:
    """Sessions that just stopped working, and the status map to persist.

    A session absent from `seen` is recorded silently: on a daemon restart, work that finished days
    ago must not be re-announced. A session absent from `known` (this plugin's worktree registry) is
    skipped entirely and never even recorded — every interactive session on the machine goes
    busy->idle after each ordinary reply, and treating that as "a worker finished" would fire an
    Opus call on someone else's unrelated chat.
    """
    fired, updated = [], dict(seen)
    for agent in agents:
        sid = agent.get("sessionId")
        if not sid:
            continue
        if sid not in known:
            continue
        status = agent.get("state") or agent.get("status")
        previous = seen.get(sid)
        if previous is not None and previous != status and status in DONE_STATUSES:
            fired.append(agent)
        updated[sid] = status
    return fired, updated


def _read_seen() -> dict:
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_seen(seen: dict) -> None:
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)


def should_tick(last_tick: float, now: float, open_count: int, running_count: int) -> bool:
    """Tick only when the interval has passed AND there is something to chase.

    An idle dashboard must not burn tokens on a manager with nothing to manage.
    """
    if now - last_tick < TICK_SECONDS:
        return False
    return open_count > 0 or running_count > 0


def wake_pass(
    last_tick: float,
    ask=manager_session.ask_result,
    agents_fn=list_agents,
    known_fn=registry_session_ids,
    open_fn=open_assignments,
    now_fn=time.time,
    read_seen=None,
    write_seen=None,
) -> float:
    """One wake pass. Returns the new last_tick.

    Can raise — write_seen and the injected callables are unguarded here. main() wraps every call
    in try/except; the worst case of a raise is a duplicate wake on the next pass, not a crash.

    Each wake is marked seen only AFTER it has been delivered — the inverse ordering silently
    drops a notification the moment the manager is busy, and it is never re-detected. `ask` must be
    ask_result's (ok, text) contract, not ask()'s bare string: ask() returns a failure NOTE on a
    subprocess error instead of raising, so a caller that only guards with try/except reads that
    failure as a delivered wake and never retries it.
    """
    read_seen = read_seen or _read_seen
    write_seen = write_seen or _write_seen
    agents = agents_fn()
    seen = read_seen()
    known = known_fn()
    fired, updated = finished_sessions(agents, seen, known)

    fired_ids = {a.get("sessionId") for a in fired}
    for sid, status in updated.items():
        if sid not in fired_ids:
            seen[sid] = status
    write_seen(seen)

    for agent in fired:
        name = agent.get("name") or agent.get("sessionId")
        try:
            ok, text = ask(
                f"Worker '{name}' (session {agent.get('sessionId')}) finished. "
                "Check its work, update the ledger, and dispatch what comes next.",
                "daemon:worker-finished",
            )
        except Exception as e:
            print(f"  wake for {name} failed, will retry: {e}", file=sys.stderr)
            continue
        if not ok:
            print(f"  wake for {name} failed, will retry: {text}", file=sys.stderr)
            continue
        seen[agent["sessionId"]] = agent.get("state") or agent.get("status")
        write_seen(seen)

    now = now_fn()
    # Registry-scoped only (a stranger's interactive session must not hold the tick open), and
    # `state or status` (a background worker carries `state` and has no `status` key at all — see
    # finished_sessions above, which gets this right).
    running = sum(
        1
        for a in agents
        if a.get("sessionId") in known and (a.get("state") or a.get("status")) not in (None, *DONE_STATUSES)
    )
    if not should_tick(last_tick, now, len(open_fn()), running):
        return last_tick
    try:
        ok, text = ask(
            "Tick. Walk the open assignments: chase anything past its ETA or still unplanned, "
            "update each note, and write a report if you have not written one in 24 hours.",
            "daemon:tick",
        )
    except Exception as e:
        return _note_tick_failure(now, str(e))
    if not ok:
        return _note_tick_failure(now, text)
    reset_tick_failures()
    return now


def ask_via_session(record: dict, ask=manager_session.ask_result) -> str:
    """Put one escalation to the persistent manager and hand back its raw reply.

    Raises on a failed call rather than returning the failure note: the note is ordinary text,
    and letting it reach parse_decision is how a timeout gets read as an approval.
    """
    ok, raw = ask(build_prompt(record), "daemon:escalation")
    if not ok:
        raise RuntimeError(raw)
    return raw


def _try_deliver(path: str, rec: dict, message: str, deliver) -> str:
    """Carry one answer back to its worker, marking it delivered only once it actually landed.

    Marking before delivering is how an answer gets silently lost: the queue reads as delivered
    while the worker is still blocked. After DELIVERY_ATTEMPTS failures the record goes back to a
    human — an undeliverable answer must surface, never vanish.
    """
    try:
        deliver(rec["session_id"], message)
    except Exception as e:
        attempts = int(rec.get("delivery_attempts") or 0) + 1
        update = {**rec, "delivery_attempts": attempts}
        if attempts >= DELIVERY_ATTEMPTS:
            update["status"] = "needs_human"
            update["reason"] = f"could not deliver to {rec.get('session_id', '?')} after {attempts} tries: {e}"
        append(path, update)
        return "delivery_failed"
    append(path, {**rec, "delivered": True})
    return "delivered"


def process_open(path: str, ask_model, deliver) -> list[dict]:
    """One pass. Returns what this pass acted on, so a caller can log or test it."""
    acted = []
    for rec in current_state(path):
        status = rec.get("status")

        if status == "open":
            outcome = decide(rec, ask_model)
            if outcome["outcome"] == "answered":
                updated = record_answer(path, rec["id"], outcome["answer"], "manager")
                _try_deliver(path, updated, outcome["answer"], deliver)
            else:
                pending = dict(rec)
                pending["status"] = "needs_human"
                try:
                    pending["tier"] = classify(rec)[0]
                except Exception:
                    pending["tier"] = "tier3"
                pending["reason"] = outcome["reason"]
                append(path, pending)
            acted.append(outcome)

        elif status == "answered" and not rec.get("delivered"):
            result = _try_deliver(path, rec, rec.get("answer"), deliver)
            acted.append(
                {
                    "outcome": result,
                    "reason": f"{rec.get('session_id', '?')} ← {rec.get('answer', '?')}",
                    "answer": rec.get("answer"),
                    "decided_by": rec.get("decided_by"),
                }
            )

    return acted


def main() -> None:
    # Same precedence as dashboard.py's main() — both resolve via manager_session.resolve_repo_root(),
    # the one place PWT_REPO_ROOT / argv / cwd precedence is decided, so the two entry points that
    # feed the SAME manager session can never disagree on where its `claude` subprocess runs. The
    # daemon takes no repo argument of its own, so this collapses to PWT_REPO_ROOT-or-cwd. Not
    # derived from registry_path(): PWT_REGISTRY can point that file at an arbitrary location with
    # no repo above it, and stripping `.claude/worktrees/...` off of it would silently produce the
    # wrong directory instead of a clear one.
    manager_session.REPO_ROOT = manager_session.resolve_repo_root()
    path = sys.argv[1] if len(sys.argv) > 1 else QUEUE_PATH
    # flush=True: stdout is block-buffered once it is not a tty (the normal case for a
    # backgrounded daemon), so without it these lines can sit in the buffer indefinitely and an
    # operator tailing the log sees nothing even though the daemon is alive and working.
    print(f"manager daemon watching {path}", flush=True)
    last_tick = time.time()
    while True:
        try:
            for outcome in process_open(path, ask_via_session, deliver_answer):
                print(f"  {outcome['outcome']}: {outcome['reason']}", flush=True)
        except Exception as e:  # a bad pass must not kill the daemon
            print(f"  pass failed: {e}", file=sys.stderr)
            traceback.print_exc()

        try:
            last_tick = wake_pass(last_tick)
        except Exception as e:  # a bad wake pass must not kill the daemon
            print(f"  wake pass failed: {e}", file=sys.stderr)

        time.sleep(5)


if __name__ == "__main__":
    main()
