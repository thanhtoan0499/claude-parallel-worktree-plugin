#!/usr/bin/env python3
"""Pure transforms from this plugin's own data sources into artifact-db documents.

No I/O, no subprocess, no session: every function here takes already-read data and returns
plain dicts. That is what makes the board's data model testable without publishing anything.
"""

import glob
import json
import os
import re
from datetime import UTC, date, datetime

from assignments import LEDGER_PATH, PRIORITIES, at_risk, progress, stalled
from escalations import QUEUE_PATH, classify, current_state, normalize_kind, normalize_options, read_all

# Kinds that mean production is already hurting. Scored off the CANONICAL name, never the raw
# one: `blocked_on_credentials` is a credentials outage and must not be scored as an unknown.
_URGENT_KINDS = frozenset({"credentials", "irreversible", "cost_anomaly", "push_or_pr"})


# `claude agents --json --all` verified LIVE (2026-09-07, 33 real sessions): blocked(4) /
# done(21) / stopped(5) / None(8) — zero running, zero idle, zero waiting. board.html's whole
# vocabulary (STATE_RANK/STATE_LABEL) is the four canonical words on the right; this is the one
# seam that reconciles the CLI's real words with it, so a new CLI spelling is fixed in one place
# instead of guessed at by every consumer.
_STATE_MAP = {
    "working": "running",
    "blocked": "waiting",
    "stopped": "done",
    "running": "running",
    "idle": "idle",
    "waiting": "waiting",
    "done": "done",
}


def normalize_state(raw) -> str:
    """Map whatever `claude agents --json` actually emits onto the board's four canonical words.

    Anything not in the map — None/missing, or a future CLI word not seen yet — becomes
    "unknown", never "idle". Folding an unrecognised state into "idle" IS the bug this exists to
    prevent: a blocked worker showing the badge "Rảnh" (free) is the opposite of the truth. An
    unrecognised state must look unrecognised so board.html can still surface it, just not lie.
    """
    if isinstance(raw, str) and raw in _STATE_MAP:
        return _STATE_MAP[raw]
    return "unknown"


# ---------------------------------------------------------------------------
# What a worker says about itself.
#
# Everything else in this file is derived from something observable — a session list, a PR, an
# ADO field. A claim is the opposite: the one thing only the worker knows (is it writing a
# failing test, implementing, verifying, or stuck — and if stuck, WHOSE move it is). It is also
# the only input here authored by an agent about itself, so it is validated at the boundary the
# way normalize_kind()/_safe_plan() validate theirs: refused, with the reason kept, never
# coerced into something plausible.
#
# Fact still wins wherever the two disagree — see _contradiction() — but the disagreement is
# published rather than resolved. A board that repeats an unverified claim as though it had
# checked is confidently wrong, which is worse than saying nothing.
# ---------------------------------------------------------------------------

# Exactly six words, closed. Closed for the same reason escalations' `kind` is: the page ranks,
# colours and glosses off this value, so a seventh word invented by a worker is not a harmless
# label — it is a state nobody agreed to, rendering as a bare English slug.
CLAIM_PHASES = frozenset({"exploring", "red_test", "implementing", "verifying", "blocked", "reporting"})

# Who can clear a block, by category. `blocked` on its own is the word this whole field exists to
# replace: a permission prompt, a product decision and an acceptance-criteria ruling are three
# different asks of three different people, and collapsing them loses the only part that matters.
BLOCKED_KINDS = frozenset({"permission", "decision", "dependency", "environment"})

# The four phases that assert forward motion. `blocked` and `reporting` claim none, so a stopped
# session agrees with them; only these four can be contradicted by a session that is not running.
_ACTIVE_PHASES = frozenset({"exploring", "red_test", "implementing", "verifying"})

# How many pump cadences of silence make a claim too old to present as current. The same number
# board.html applies to the meta clocks (its STALE_MULTIPLIER) — one definition of "too old to
# trust", not two that can drift apart. test_board_state.py fails if they ever do.
CLAIM_STALE_MULTIPLIER = 3


def claim_stale_after(pump_period_s) -> float | None:
    """The freshness window for a worker claim, in seconds, or None when it cannot be sized.

    Derived, never a literal. The pump re-reads these files once per sweep, so a claim is already
    up to one cadence old purely from sampling; the window is that real cadence — parsed out of
    the shipped timer unit by timer_period_seconds() — times CLAIM_STALE_MULTIPLIER.

    None when the cadence is unknown, and None makes every claim stale (see validate_claim). That
    is deliberate and matches what board.html does with an unknown cadence: an unsized window
    cannot certify anything as fresh, and a guessed one is exactly the fabricated baseline that
    let this board sit 14 minutes dead while rendering as live.
    """
    period = _num(pump_period_s)
    if period is None or period <= 0:
        return None
    return period * CLAIM_STALE_MULTIPLIER


def _claim_blocked_on(raw) -> tuple[dict | None, str | None]:
    """`{kind, what, who}` for a blocked claim, or the reason it was refused."""
    if not isinstance(raw, dict):
        return None, "phase là blocked nhưng không có blocked_on"
    kind = raw.get("kind")
    if kind not in BLOCKED_KINDS:
        return None, f"blocked_on.kind không hợp lệ: {kind!r}"
    who = raw.get("who")
    if not isinstance(who, str) or not who.strip():
        return None, "blocked_on không nói ai gỡ được (thiếu `who`)"
    what = raw.get("what")
    return {"kind": kind, "what": what if isinstance(what, str) else "", "who": who.strip()}, None


def validate_claim(raw, now: float, stale_after: float | None) -> tuple[dict | None, str | None]:
    """`(claim, None)` for a usable claim, `(None, reason)` for one refused, `(None, None)` for none.

    Those three outcomes are genuinely different and must never be flattened. `raw is None` means
    the worker never wrote a file — the normal case, and not a complaint to put on a board. A
    refusal is a complaint: it is how the worker's author finds out their file is being ignored.

    An undated claim is refused rather than stamped with the sweep time: dating it here would make
    every stale claim look permanently fresh, the same fabricated-baseline failure
    timer_period_seconds() refuses to commit.
    """
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"không phải JSON object ({type(raw).__name__})"

    phase = raw.get("phase")
    if phase not in CLAIM_PHASES:
        return None, f"phase không hợp lệ: {phase!r}"

    updated_at = _num(raw.get("updated_at"))
    if updated_at is None:
        return None, "updated_at không phải mốc thời gian"

    # Only a `blocked` claim carries one. Kept on any other phase it would render a worker that is
    # happily implementing as waiting on the CTO — so the field goes, not the claim: the phase
    # itself is still perfectly good information.
    blocked_on = None
    if phase == "blocked":
        blocked_on, reason = _claim_blocked_on(raw.get("blocked_on"))
        if reason:
            return None, reason

    age = max(0.0, now - updated_at)
    ticket = raw.get("ticket")
    note = raw.get("note")
    return {
        # Provenance, on every claim, always. Every other field on a session document is observed
        # fact; this one is a worker's own account of itself, and a reader that cannot tell them
        # apart reads "verifying" as though the board had checked.
        "source": "worker_claim",
        "ticket": str(ticket) if ticket not in (None, "") else None,
        "phase": phase,
        "note": note if isinstance(note, str) else "",
        "blocked_on": blocked_on,
        "updated_at": updated_at,
        "age_seconds": age,
        # Still published when stale — "the worker last said verifying, an hour ago" is real
        # information — but flagged, so the page can never print it as the phase it is in now.
        "stale": stale_after is None or age > stale_after,
    }, None


def _contradiction(claim, state: str) -> dict | None:
    """The disagreement between a live claim and the observed session state, or None.

    Not resolved into one value: publishing "verifying" alone would repeat a claim nobody checked,
    and publishing the state alone would throw away the only account of what the work actually is.
    Both, and the page says which one it trusts.

    "Not running" rather than the narrower blocked/waiting/gone: a session the CLI reports as
    idle or done, or that has disappeared from the list entirely, is equally not doing the
    implementing its worker claims. A stale claim contradicts nothing — it is not evidence of
    anything current, so it cannot disagree with anything current either.
    """
    if not claim or claim["stale"] or claim["phase"] not in _ACTIVE_PHASES or state == "running":
        return None
    return {"claim_phase": claim["phase"], "session_state": state}


def session_docs(agents: list[dict], registry: dict, claims: dict | None = None,
                 now: float = 0.0, stale_after: float | None = None) -> dict[str, dict]:
    """One document per live task, keyed by task name.

    Agent-first, not registry-first: a session doing work belongs on the board immediately,
    including one dispatched by any other means, and the registry only fills in branch/worktree
    once parallel-task.sh has provisioned it.

    `claims` is `{task_name: raw_json}` straight off read_worker_claims() — unvalidated on
    purpose, because validating it here is what keeps the reader a dumb file-getter and the
    vocabulary in one place. A task with a claim but NO agent still gets a document: a worker
    saying "verifying" whose session no longer exists is the single most important thing on this
    board, and agent-first alone would drop it exactly when it matters.
    """
    docs = {}
    registry = registry or {}
    claims = claims if isinstance(claims, dict) else {}

    def build(name, agent, reg):
        claim, ignored = validate_claim(claims.get(name), now, stale_after)
        state = normalize_state(agent.get("state") or agent.get("status"))
        return {
            "task": name,
            "session_id": agent.get("sessionId"),
            "short_id": reg.get("short_id"),
            # `claude agents --json` has used both spellings; read whichever is present, then
            # translate it — see normalize_state() above.
            "state": state,
            "branch": reg.get("branch"),
            "worktree": reg.get("path"),
            "started_at": agent.get("startedAt"),
            "ado_refs": list(reg.get("ado_ids") or []),
            # What the worker says about itself, why a claim was refused, and where the two
            # sources disagree. All three are null for the many sessions that never write a file.
            "claim": claim,
            "claim_ignored": ignored,
            "contradiction": _contradiction(claim, state),
            # True iff parallel-task.sh actually dispatched this task — an entry EXISTS in the
            # registry, not "branch happens to be truthy". A registry row with a blank branch
            # field is still work the manager provisioned; `bool(reg)` or `bool(branch)` would
            # both misclassify it as ad-hoc the same way dashboard.py's `managed` signal does not
            # (that one gets away with `bool(reg)` only because it is keyed off session_id lookups
            # that never store an empty dict — this dict is keyed by task name straight off the
            # registry file, so existence has to be checked explicitly).
            "managed": name in registry,
        }

    for agent in agents or []:
        name = agent.get("name")
        if not name:
            # A document id cannot be empty; an unnamed agent has no addressable key.
            continue
        docs[name] = build(name, agent, registry.get(name) or {})
    for name in claims:
        if name and name not in docs:
            docs[name] = build(name, {}, registry.get(name) or {})
    return docs


def escalation_severity(record: dict) -> str:
    """P0 / P1 / P2 from what the record already says.

    An unrecognised kind stays P1 — a human's call. Recognising more kinds must never widen
    what looks urgent, or the vocabulary becomes a way to shout.
    """
    kind = normalize_kind(record.get("kind"))
    if kind in _URGENT_KINDS:
        return "P0"
    tier, _ = classify(record)
    return "P1" if tier == "tier3" else "P2"


def _safe_evidence(raw) -> dict[str, str]:
    """Coerce worker-authored evidence into a flat str->str dict the page can walk directly.

    Same tolerance as normalize_kind/normalize_options: evidence is written by a model against
    a prose schema, so "not even a dict" is normal drift, not an error — the whole thing is
    dropped rather than guessed at. A value that is not itself a string is stringified, never
    dropped: a value that says something is NOT affected ("git_push: khong anh huong") is exactly
    as load-bearing to a manager as one that says something broke.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(k): v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in raw.items()}


def _str_or_none(value) -> str | None:
    """`reason`/`tier` must reach the page as a string or None — never some other JSON-native
    type a future producer might write."""
    return value if isinstance(value, str) else None


def escalation_docs(records: list[dict]) -> dict[str, dict]:
    """One document per escalation, keyed by its id."""
    docs = {}
    for rec in records or []:
        rec_id = rec.get("id")
        if not rec_id:
            continue
        docs[str(rec_id)] = {
            "ts": rec.get("ts"),
            "session": rec.get("session_id"),
            "kind": normalize_kind(rec.get("kind")),
            # Kept so the board can mark drift instead of absorbing it silently.
            "kind_raw": rec.get("kind"),
            "severity": escalation_severity(rec),
            "question": rec.get("question") or "",
            "options": normalize_options(rec.get("options")),
            "status": rec.get("status") or "open",
            "answer": rec.get("answer"),
            "answered_at": rec.get("answered_at"),
            # Why this reached a human, and what it touches — daemon/worker-authored context a
            # manager needs to act, not just triage. See docstrings above for the shape guards.
            "evidence": _safe_evidence(rec.get("evidence")),
            "reason": _str_or_none(rec.get("reason")),
            "tier": _str_or_none(rec.get("tier")),
        }
    return docs


# Plan-step states the page has a word for. Anything else becomes "unknown" — never "todo" —
# for the same reason normalize_state() refuses to fold an unrecognised session state into
# "idle": a step the manager wrote as `in_progress` would render as "chưa làm" and understate
# work already underway, while folding it into "done" would overstate it. Both are lies about
# the plan; "unknown" is the only honest answer, and the page has a label for it.
_STEP_STATES = frozenset({"todo", "doing", "done"})


def _safe_plan(raw) -> list[dict]:
    """Coerce a model-authored plan into the flat list of steps the page walks directly.

    Same tolerance as _safe_evidence(): `plan` is written by the manager against a prose schema,
    so a plan that is not a list, or a step that is not a dict, is dropped rather than raised on.
    The steps kept here are exactly the ones assignments.progress() counts, so the progress
    number and the step list under it can never disagree about the denominator.
    """
    if not isinstance(raw, list):
        return []
    steps = []
    for step in raw:
        if not isinstance(step, dict):
            continue
        deps = step.get("depends_on")
        state = step.get("state")
        steps.append(
            {
                "step": str(step.get("step") or ""),
                "owner": _str_or_none(step.get("owner")),
                "depends_on": [str(d) for d in deps] if isinstance(deps, list) else [],
                "eta": _str_or_none(step.get("eta")),
                "state": state if state in _STEP_STATES else "unknown",
            }
        )
    return steps


# The four counters a transcript line reports, and the ONE of them that is not spend.
#
# Measured on this machine while building the card: a single session logged 2.2M output tokens
# against 954M cache_read_input_tokens. Summing all four and calling the result "tokens used"
# reports fifty times the real cost, which is not a rounding error — it is a different claim.
# Cache reads are re-reads of a prefix that was already paid for, priced at a fraction of a
# fresh token, so they are counted and reported SEPARATELY rather than folded into the headline.
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_SPEND_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens")

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")


def sum_usage(records) -> dict:
    """Total each usage counter over one session transcript's already-parsed lines.

    `usage` appears under `message.usage` on assistant turns and at the top level on others;
    reading only one shape silently drops half the count. Same tolerance as every other reader
    here: a line that is not a dict, a usage block that is not a dict, or a counter that is not
    an int is skipped rather than raised on — a transcript is an append-only log written by a
    process that can be killed mid-line.
    """
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        message = rec.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            usage = rec.get("usage")
        if not isinstance(usage, dict):
            continue
        for field in USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[field] += value
    return totals


def session_transcripts(session_id: str, projects_root: str = PROJECTS_ROOT) -> list[str]:
    """Every transcript file for one session id, newest-mtime last, or [] when there is none.

    A worktree session's transcript lives under a project directory named after the WORKTREE
    path, not the repo root, so the lookup is by session id across every project directory
    rather than by re-deriving that encoding here.
    """
    return sorted(glob.glob(os.path.join(projects_root, "*", session_id + ".jsonl")), key=_mtime)


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def read_session_usage(session_id: str, projects_root: str = PROJECTS_ROOT) -> dict | None:
    """Totals for one session's transcript, or None when there is no transcript to read.

    None, never a zeroed dict: "no file" and "a file that recorded nothing" are different facts,
    and only one of them may reach the page as a number.
    """
    matches = session_transcripts(session_id, projects_root)
    if not matches:
        return None
    records = []
    for path in matches:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None
    return sum_usage(records)


def _num(value):
    """A real number, or None. `True` is an int in Python and is never a timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _first_seen(records) -> dict[str, float]:
    """The EARLIEST `ts` per id across the whole append-only ledger.

    current_state() keeps only the newest record per id, whose `ts` is when the assignment was
    last UPDATED — an assignment the manager touched a minute ago would report as a minute old
    however long it has actually been running. The start is the oldest record, which only the
    unfolded ledger knows.
    """
    first = {}
    for rec in records or []:
        rec_id = rec.get("id")
        ts = _num(rec.get("ts"))
        if not rec_id or ts is None:
            continue
        key = str(rec_id)
        if key not in first or ts < first[key]:
            first[key] = ts
    return first


def _elapsed(rec: dict, first_ts, now: float):
    """How long this assignment has been running, in seconds, or None with no usable start.

    The clock STOPS at the last record once the assignment is done or cancelled. Left running,
    a job finished last week would keep growing and eventually top the board as its longest —
    a closed assignment took as long as it took.

    The stop is `updated_at`, the moment assignments.append() wrote that closing record. `ts` is
    only a fallback for it: every revision of a record copies `ts` from the creation record
    verbatim, so on a ledger written before that stamp existed the closing record's `ts` equals
    the opening one's and subtracting them yields 0 — a finished assignment reported as having
    taken no time at all. There is genuinely no close time in such a record, so the answer is
    None ("chưa rõ"), not zero.
    """
    if first_ts is None:
        return None
    if rec.get("status") in ("done", "cancelled"):
        end = _num(rec.get("updated_at"))
        if end is None:
            end = _num(rec.get("ts"))
        if end is None or end <= first_ts:
            return None
    else:
        end = now
    return max(0.0, end - first_ts)


# What parallel-task.sh accepts as a task name (see its `task-name must be kebab-case` guard).
# An owner matching this is a worktree task even when the registry no longer holds it — a task
# whose worktree was removed is still not a person.
_TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _is_person(owner) -> bool:
    """True when a plan step's `owner` names a human or a placeholder rather than a task.

    `owner` is supposed to be a worktree task name, but the manager also writes "CTO",
    "self (EM)" and "chưa giao" into it, and those are the ones that mean nobody is running
    anything. Decided on the SHAPE of the name, not on registry membership: the registry only
    holds tasks whose worktree still exists, so an owner missing from it is just as likely a
    finished worker as a person — and announcing "chờ người (board-systemd-timer)" for a retired
    worker is the same kind of confident wrong answer as showing 0 for an unmeasured cost.
    """
    return bool(owner) and not _TASK_NAME.match(str(owner))


def _owner_session(owner, registry: dict):
    """The session id behind a plan step's `owner`, or None when the owner is not a worktree task.

    `owner` is free text the manager writes: "self (EM)", "CTO" and "chưa giao" are people and
    placeholders, and none of them has a transcript. The registry is the only thing that says
    which owners are actually dispatched tasks.
    """
    entry = registry.get(owner) if owner and isinstance(registry, dict) else None
    if not isinstance(entry, dict):
        return None
    sid = entry.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _tokens(steps, registry: dict, usage_by_session: dict):
    """What this assignment actually cost, or None when nothing about it could be measured.

    None rather than zero, for the same reason progress() returns None rather than zero: 0 means
    "measured, and it was nothing", which is a claim. An assignment whose every owner is a human,
    or whose transcripts are gone, was not measured at all.

    `partial` marks a total that is real but too low — some session in the join resolved to a
    task whose transcript could not be read (its worktree was removed, say). Without it the
    remaining sum would present as the full cost, which is the same class of lie as showing 0.
    """
    spend = cache_read = 0
    measured = partial = False
    seen = set()
    for step in steps:
        sid = _owner_session(step.get("owner"), registry)
        if not sid or sid in seen:
            continue
        seen.add(sid)
        totals = usage_by_session.get(sid) if isinstance(usage_by_session, dict) else None
        if not isinstance(totals, dict):
            partial = True
            continue
        measured = True
        spend += sum(_num(totals.get(f)) or 0 for f in _SPEND_FIELDS)
        cache_read += _num(totals.get("cache_read_input_tokens")) or 0
    if not measured:
        return None
    return {"spend": int(spend), "cache_read": int(cache_read), "partial": partial}


def _current_step(steps):
    """The step an ETA should be read off: the one being worked, else the first not yet finished.

    Second pass is "not done" rather than "todo" so a step whose state _safe_plan() could not
    recognise still counts as outstanding — treating it as finished would skip straight past the
    work actually in front of the assignment.
    """
    for step in steps:
        if step.get("state") == "doing":
            return step
    for step in steps:
        if step.get("state") != "done":
            return step
    return None


def _eta(rec: dict, steps, registry: dict, elapsed):
    """What can honestly be said about when this finishes — or None once it already has.

    `text` is the current step's `eta` verbatim. It is prose a person wrote ("2-3 giờ", "chờ
    CTO", "chưa đặt"); it is NOT parsed into a date, because there is no date in it to find and
    inventing one would turn a note into a commitment.

    `projected_seconds` is the only number here, and it is derived, not promised: time spent so
    far divided by steps finished, times steps left. It needs at least one finished step to
    divide by, and it is withheld entirely when the current step is owned by a person — no
    machine is burning time on it, so the measured rate says nothing about when they will answer.
    """
    if rec.get("status") in ("done", "cancelled"):
        return None
    step = _current_step(steps)
    if step is None:
        return None
    owner = step.get("owner")
    # Two different questions, deliberately answered by two different sources. Whether to CALL it
    # a person is the name's shape (see _is_person). Whether a projection is honest is the
    # registry: a rate measured over finished steps says nothing about a step no running worker
    # is behind — whether that is because a human owns it or because its worktree is gone.
    waiting_human = _is_person(owner)
    done = sum(1 for s in steps if s.get("state") == "done")
    left = len(steps) - done
    projected = None
    if _owner_session(owner, registry) and done and left > 0 and elapsed:
        projected = elapsed / done * left
    return {
        "text": step.get("eta"),
        "owner": owner,
        "waiting_human": waiting_human,
        "projected_seconds": projected,
    }


def assignment_docs(records: list[dict], now: float, registry: dict | None = None,
                    usage_by_session: dict | None = None) -> dict[str, dict]:
    """One document per assignment, keyed by its id.

    Keyed by id and built by overwrite, so an append-only ledger folded oldest-first lands on the
    newest record per id — the same shape escalation_docs() relies on.

    `progress`, `at_risk` and `stalled` are NOT ledger fields and must never become ones: they
    are recomputed from the plan steps and the clock on every pump, through assignments.py's own
    helpers rather than a second copy of the rules living here. Anything a writer stored in the
    record under those names loses — a derived value that can be stored is one that can go stale,
    and a stale "on track" is the failure this board exists to prevent.

    `elapsed_seconds`, `tokens` and `eta` join that rule and are computed here for the same
    reason: the page cannot read a file, so every number it shows has to arrive already measured.
    `records` is expected to be the WHOLE append-only ledger, not the folded latest-per-id view —
    folding still happens here by overwrite, but the oldest record per id is what says when the
    work started, and current_state() has already thrown it away.
    """
    registry = registry if isinstance(registry, dict) else {}
    usage_by_session = usage_by_session if isinstance(usage_by_session, dict) else {}
    first_seen = _first_seen(records)
    docs = {}
    for rec in records or []:
        rec_id = rec.get("id")
        if not rec_id:
            continue
        steps = _safe_plan(rec.get("plan"))
        elapsed = _elapsed(rec, first_seen.get(str(rec_id)), now)
        docs[str(rec_id)] = {
            "id": str(rec_id),
            "ts": rec.get("ts"),
            "title": rec.get("title") or "",
            # An unrecognised priority stays P1 — a human's call — for the same reason
            # escalation_severity() refuses to promote an unknown kind to P0: a wider vocabulary
            # must never become a way to shout.
            "priority": rec.get("priority") if rec.get("priority") in PRIORITIES else "P1",
            "deadline": rec.get("deadline"),
            "ado_refs": [str(r) for r in rec.get("ado_refs") or []],
            "status": rec.get("status") or "assigned",
            "plan": steps,
            "note": rec.get("note") or "",
            "progress": progress(rec),
            "at_risk": at_risk(rec, now),
            "stalled": stalled(rec, now),
            "elapsed_seconds": elapsed,
            "tokens": _tokens(steps, registry, usage_by_session),
            "eta": _eta(rec, steps, registry, elapsed),
        }
    return docs


def _ticket_ownership(ticket: dict, owners: list[str]) -> dict:
    """Whose desk a ticket is on right now, from ADO's own AssignedTo/ActivatedBy fields — never
    a hardcoded ticket list.

    `owners` is the board owner's identities (dashboard._ado_identities()); the backlog query
    already unions AssignedTo/ActivatedBy over that same list, so anything reaching here matched
    at least one of them. AssignedTo wins ON PURPOSE: a ticket the owner both started and still
    holds is their own work regardless of who else's identity also appears on it, and only
    ActivatedBy pointing at the owner while AssignedTo points elsewhere counts as handed off —
    which is also what keeps a ticket matching both from ever being counted twice, since it can
    only land in one branch below. An empty `owners` list (PWR_ADO_ASSIGNED_TO unset, @Me alone)
    has no email to compare against, so every ticket reads as the owner's own — the behaviour
    before this existed.
    """
    owners_lower = {o.strip().lower() for o in owners or [] if o and o.strip()}

    def email_of(identity) -> str | None:
        email = (identity or {}).get("email")
        return email.strip().lower() if isinstance(email, str) and email.strip() else None

    def is_owner(identity) -> bool:
        email = email_of(identity)
        return email is not None and email in owners_lower

    if not owners_lower or is_owner(ticket.get("assigned_to")):
        return {"handed_off": False, "handed_off_to": None}
    if is_owner(ticket.get("activated_by")):
        assignee = ticket.get("assigned_to") or {}
        return {"handed_off": True, "handed_off_to": assignee.get("name") or email_of(assignee)}
    return {"handed_off": False, "handed_off_to": None}


_AB_REF_RE = re.compile(r"\bAB#(\d+)\b", re.IGNORECASE)

# OPEN still needs a human decision; MERGED is done; CLOSED (unmerged) is abandoned or
# superseded. That is the order a reviewer scanning the board cares about, highest first.
_PR_STATE_RANK = {"OPEN": 2, "MERGED": 1, "CLOSED": 0}


def _pr_priority(pr: dict) -> tuple:
    """Which of several PRs claiming the same ticket wins the column: state first (see
    _PR_STATE_RANK), then a non-draft PR over a draft one (a draft isn't asking for review yet),
    then the higher PR number — the more recent attempt — as the final tiebreak."""
    return (
        _PR_STATE_RANK.get(pr.get("state"), -1),
        0 if pr.get("isDraft") else 1,
        pr.get("number") or 0,
    )


def prs_by_ticket(prs: list[dict]) -> dict[str, dict]:
    """`{ticket_id: {"number", "state", "url"}}` from a flat `gh pr list --json
    number,title,url,state,isDraft` result — the only join ADO and GitHub have, since ADO has no
    link relation to a PR. CI enforces every PR title carry the work item(s) it closes as
    `AB#NNNN` (commit 6ace7898e), and a title may name more than one — "(AB#8196, AB#8197)" is a
    real shape — so every id found in a title maps to that same PR.

    Includes closed and merged PRs on purpose, not just open ones: a merged PR is exactly what a
    reviewer wants to see on a ticket that is already done. When two PRs name the same ticket
    (an old attempt superseded by a new one), _pr_priority() picks the one still worth a look;
    callers only ever see the winner.
    """
    winners: dict[str, dict] = {}
    for pr in prs or []:
        for ticket_id in _AB_REF_RE.findall(pr.get("title") or ""):
            current = winners.get(ticket_id)
            if current is None or _pr_priority(pr) > _pr_priority(current):
                winners[ticket_id] = pr
    return {
        ticket_id: {
            "number": pr.get("number"),
            "state": pr.get("state"),
            "url": pr.get("url"),
            # "" is what `gh` returns when the repo requires no review at all; that is "no
            # decision", not a decision, and it must not read as one.
            "review": pr.get("reviewDecision") or None,
            "checks": check_rollup(pr.get("statusCheckRollup")),
        }
        for ticket_id, pr in winners.items()
    }


# GitHub's own words, split three ways. A CheckRun reports `conclusion` once it has finished; a
# StatusContext reports `state`. Read whichever is present.
_CHECK_FAILING = frozenset({"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"})
# NEUTRAL and SKIPPED are green enough to merge on — GitHub's own branch protection treats them
# that way, and a skipped job holding a PR at "not ready" would park every conditional workflow.
_CHECK_PASSING = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


def check_rollup(rollup) -> str | None:
    """"failing" / "pending" / "passing" for a PR's checks, or None when it has none.

    None, never "passing": a repo with no CI has nothing to be green, and calling that green
    would be a claim nobody measured — the same distinction read_session_usage() draws between
    "no transcript" and "a transcript that recorded nothing".

    Anything GitHub says that is in neither list — a word added after this was written, or a
    check run with no conclusion yet — is "pending". That is the fail-toward-caution default
    normalize_state() uses: an unrecognised word must never become the answer that tells the CTO
    to click merge, and it must not shout "failing" either.
    """
    if not isinstance(rollup, list) or not rollup:
        return None
    verdicts = []
    for check in rollup:
        if not isinstance(check, dict):
            continue
        verdicts.append(check.get("conclusion") or check.get("state"))
    if not verdicts:
        return None
    if any(v in _CHECK_FAILING for v in verdicts):
        return "failing"
    if all(v in _CHECK_PASSING for v in verdicts):
        return "passing"
    return "pending"


# Same list board.html uses to split "not done" from "done" (its TICKET_DONE_STATES), and the same
# one the localhost dashboard uses — kept here so ticket_status() can reason about it, with a test
# that fails the moment the two copies disagree.
TICKET_DONE_STATES = frozenset({"Closed", "Removed", "Done"})

# Every derived status, in the precedence order ticket_status() applies them. The order IS the
# argument, so it lives here rather than being implied by the shape of an if-chain:
#
#  1. waiting_decision  — an open escalation is a HARD STOP. Every other status names work that
#     can proceed; this one names work that cannot, and it is the only one on this list a machine
#     can never clear. Showing "waiting for review" on a ticket whose scope is still in dispute
#     sends a reviewer to read a diff that may be thrown away.
#  2. checks_failing    — beats waiting_merge because approval and a red check coexist constantly
#     (a reviewer approves, a later push breaks CI). If waiting_merge won, the board would tell
#     the CTO to click a button GitHub will refuse. The reverse mistake is cheap and
#     self-correcting: a mergeable PR reads as failing until the rollup settles.
#  3. waiting_merge     — approved and green. The one the CTO is looking for.
#  4. waiting_review    — open, not yet approved. Mutually exclusive with 3 in practice; ordered
#     under it so the strongest positive claim wins if `gh` ever reports both.
#  5. merged_not_closed — only reachable once no OPEN PR claims the ticket, because
#     _pr_priority() already hands this function the single most interesting PR per ticket.
#  6. waiting_push      — some assignment owns it, nothing has been pushed.
#  7. unclaimed         — nothing references it at all. Exclusive with 6 by construction.
#
# Only two of those orderings are genuinely contested (1 over everything, and 2 over 3); the rest
# are near-exclusive and ordered for determinism rather than for judgement.
TICKET_STATUSES = (
    "waiting_decision", "checks_failing", "waiting_merge", "waiting_review",
    "merged_not_closed", "waiting_push", "unclaimed",
)


def ticket_status(ticket_state, pr: dict | None, claimed: bool, escalated: bool) -> str | None:
    """Whose move is it now — or None when there is nothing left to chase.

    None is a real answer and not a gap: a Closed ticket whose PR merged has no next move, and
    "chưa ai nhận" printed against it would be a lie with an action attached (it invites a manager
    to dispatch a worker at finished work).

    `claimed` is "some non-cancelled assignment lists this ticket". It is deliberately NOT "there
    are commits on the branch": answering that needs a subprocess per worktree, against branches
    whose worktrees are routinely removed, and it would not change whose move it is — pushed or
    not, the ticket's owner is the one who has to act. See the report for that trade.
    """
    if escalated:
        return "waiting_decision"
    pr = pr or {}
    if pr.get("state") == "OPEN":
        if pr.get("checks") == "failing":
            return "checks_failing"
        # A repo with no checks at all (None) is not held back; one whose checks have not
        # finished (pending) is — "waiting to merge" must mean it can actually be merged.
        if pr.get("review") == "APPROVED" and pr.get("checks") != "pending":
            return "waiting_merge"
        return "waiting_review"
    if ticket_state in TICKET_DONE_STATES:
        return None
    if pr.get("state") == "MERGED":
        return "merged_not_closed"
    # A CLOSED-unmerged PR is a superseded attempt; whose move it is now is the same as if it had
    # never existed, so it falls through rather than getting a status of its own.
    return "waiting_push" if claimed else "unclaimed"


def ticket_docs(tickets: list[dict], pr_by_ticket: dict, owners: list[str] | None = None,
                assignment_refs=None, escalated_refs=None) -> dict[str, dict]:
    """One document per ADO work item, keyed by its id.

    "Not started", "in flight" and "done this sprint" are filters over `state` + `sprint` on
    the page, not three collections here. `handed_off`/`handed_off_to` are a fourth: a ticket the
    owner activated but no longer holds stays visible instead of vanishing the moment AssignedTo
    changes — see _ticket_ownership().

    `derived_status` is published BESIDE `state`, never instead of it. `state` is a real ADO field
    a person maintains; the derived one is what this board worked out from the evidence. Five
    tickets reading "Active" while one needed a merge click, one a reviewer, one a product
    decision and one a push is exactly why both are needed — and where the two disagree, that
    disagreement is worth seeing rather than hiding.
    """
    assignment_refs = assignment_refs or set()
    escalated_refs = escalated_refs or set()
    docs = {}
    for ticket in tickets or []:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue
        ownership = _ticket_ownership(ticket, owners or [])
        key = str(ticket_id)
        pr = (pr_by_ticket or {}).get(key)
        docs[key] = {
            "id": key,
            "title": ticket.get("title") or "",
            "state": ticket.get("state") or "",
            "sprint": ticket.get("sprint") or "",
            "type": ticket.get("type") or "",
            "url": ticket.get("url") or "",
            "pr": pr,
            "derived_status": ticket_status(
                ticket.get("state") or "", pr, key in assignment_refs, key in escalated_refs
            ),
            "handed_off": ownership["handed_off"],
            "handed_off_to": ownership["handed_off_to"],
        }
    return docs


def _iter_date(value) -> date | None:
    """An ADO iteration date ("2026-09-07T00:00:00Z") cut down to its calendar date, or None for
    anything not shaped like one. ADO's own granularity here is the day — ISO dates always land
    on midnight UTC — so the time-of-day component carries no information worth keeping."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def current_sprint_name(iterations: list[dict], now: float) -> str | None:
    """The leaf name of whichever ADO iteration's date range contains today, or None.

    None covers two real cases the same way: today falling in the gap between two sprints (this
    project has one — Sprint 57 ends 09-04, Sprint 58 starts 09-07), and an iteration that has
    never had dates set at all. Either way there is no honest sprint to default to.

    `iterations` entries are already the leaf shape `dashboard.get_ado_iterations()` returns —
    {"name", "start", "finish"} — so the value returned here compares equal, with no further
    reshaping, to `_shape_ado_ticket()`'s own `sprint` field (see resolve_default_sprint()).
    """
    today = datetime.fromtimestamp(now, tz=UTC).date()
    for it in iterations or []:
        start = _iter_date(it.get("start"))
        finish = _iter_date(it.get("finish"))
        if start and finish and start <= today <= finish:
            name = it.get("name")
            return str(name) if name else None
    return None


def resolve_default_sprint(iterations: list[dict], tickets: dict[str, dict], now: float) -> str | None:
    """The sprint the backlog filter should open on, or None to mean "tất cả".

    Today's ADO sprint, but only when the backlog actually has a ticket in it — an empty table
    under an auto-picked filter reads exactly like a broken board, not a filtered one, and "tất
    cả" is the honest fallback the page already has a word for.
    """
    sprint = current_sprint_name(iterations, now)
    if sprint is None:
        return None
    if not any((t or {}).get("sprint") == sprint for t in (tickets or {}).values()):
        return None
    return sprint


# `OnCalendar=*-*-* *:2/5:00` — systemd's minute-step form: fire every 5 minutes starting at :02.
# The 5 is the cadence; the 2 is only the offset off the round marks.
_ONCALENDAR_MINUTE_STEP = re.compile(r"^OnCalendar=.*?\*:\d+/(\d+):", re.MULTILINE)


def timer_period_seconds(unit_text) -> int | None:
    """How often the pump fires, read off the systemd unit rather than copied into a constant.

    board.html cannot read a unit file — it is a published artifact with no filesystem — so it
    used to carry the cadence as a literal, and that literal rotted: the page said 15 minutes for
    weeks after the timer was retuned to 5, which meant every staleness check on the board was
    computed off a baseline three times too long. Parsing it here and stamping it onto
    meta/status is what keeps the two from drifting again.

    None for anything unreadable — a unit written in a form other than a minute step, a missing
    file, a zero step. None is "cadence unknown", never a guess: a fabricated baseline is exactly
    the failure being fixed.
    """
    m = _ONCALENDAR_MINUTE_STEP.search(unit_text or "")
    if not m:
        return None
    minutes = int(m.group(1))
    return minutes * 60 if minutes > 0 else None


def meta_status(
    now: float,
    ado_swept_at=None,
    sessions_scanned_at=None,
    manager=None,
    default_sprint=None,
    pump_period_s=None,
) -> dict:
    """When each source was last read, and who the manager is.

    One document, several clocks: the page shows the age of each source separately, because a
    30-minute-old ticket list must not be presentable as though it were as live as a session
    state. This doubles as the failure signal — an age that keeps growing IS the alarm.
    """
    manager = manager or {}
    return {
        "written_at": now,
        "last_ado_sweep": ado_swept_at,
        "last_session_scan": sessions_scanned_at,
        "manager_session_id": manager.get("session_id"),
        "manager_started_at": manager.get("started_at"),
        "default_sprint": default_sprint,
        # How often the pump that wrote this doc is supposed to fire, in seconds. The page sizes
        # its "this board has stopped updating" threshold off it, because a published artifact
        # has no way to read the timer unit itself. None = cadence unknown; see
        # timer_period_seconds() and board.html's staleThreshold().
        "pump_period_s": pump_period_s,
    }


# write_db validates doc_id against this before writing ANYTHING, so one bad id does not skip a
# row — it makes the server refuse the whole batch ("batch rejected before any write, no documents
# landed"). Session documents are keyed on the task name, which Claude writes as natural English
# and therefore full of spaces, so this is not a hypothetical: "code review verification" and
# "git checkout test verification" each stopped an entire refresh from landing.
DOC_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_\-.~:@+]")
DOC_ID_MAX = 200


def as_doc_id(name: str) -> str:
    """The name coerced into something write_db accepts, or "" if nothing usable is left.

    Only the KEY is normalized — the document's own fields keep the original name, which is what
    the board actually displays. The mapping is deterministic, so the same session keeps the same
    key run after run; that is what lets the diff recognise it as unchanged.
    """
    return DOC_ID_ALLOWED.sub("_", str(name or "").strip())[:DOC_ID_MAX].strip("_") or ""


def _assignment_refs(assignments_docs: dict) -> set[str]:
    """Every ticket some live assignment owns.

    Cancelled is excluded and done is not: cancelled means the work was abandoned, so the ticket
    genuinely needs dispatching again, while a finished assignment is proof somebody DID pick the
    ticket up — calling it "chưa ai nhận" the moment the work completes would be worse than
    useless.
    """
    return {
        ref
        for doc in assignments_docs.values()
        if doc.get("status") != "cancelled"
        for ref in doc.get("ado_refs") or []
    }


def _escalated_refs(escalations_docs: dict, registry: dict) -> set[str]:
    """Every ticket sitting behind an escalation nobody has answered yet.

    An escalation record carries no ticket field at all — only `session_id` — so the join runs
    through the registry, which is what knows the tickets a session's worktree was provisioned
    for. The same hop the token sum already makes, in the other direction.
    """
    by_session = {}
    for entry in (registry or {}).values():
        sid = entry.get("session_id") if isinstance(entry, dict) else None
        if sid:
            by_session.setdefault(sid, []).extend(str(r) for r in entry.get("ado_ids") or [])
    return {
        ref
        for doc in escalations_docs.values()
        if doc.get("status") not in ("answered", "dismissed")
        for ref in by_session.get(doc.get("session"), [])
    }


def build_writes(
    agents,
    registry,
    escalations,
    tickets,
    pr_by_ticket,
    now: float,
    ado_swept_at=None,
    manager=None,
    assignments=None,
    usage_by_session=None,
    iterations=None,
    owners=None,
    pump_period_s=None,
    claims=None,
) -> list[dict]:
    """Every document to write, in the order to write it.

    Shaped as the Artifact tool's `write_db` batch entries so the calling session passes this
    straight through without reshaping — the transform is testable here, and the session stays
    a thin courier.
    """
    # First, always: proof the pump is alive, and nothing else. It carries no "as of" clock, so
    # it can never be mistaken for the completeness claim meta/status makes at the other end of
    # the run. Being first means it rides batch 1, so it lands even on a run that stops part-way
    # through a backlog — which, since the refresh started checkpointing per batch, is every run
    # during a drain. meta/status stayed the board's only clock through that change and froze for
    # the whole drain, so a board with data visibly flowing rendered as hours stale and tripped
    # the staleness alarm sized off this same cadence. See board_mirror_diff.stamp_heartbeat()
    # for the batch counts the chunker adds, and bin/systemd/README.md for the field contract.
    writes = [{"op": "set", "collection": "meta", "doc_id": "pump", "data": {"ran_at": now}}]
    # Built before the tickets, because the tickets' derived status is a join over both: which
    # tickets an assignment owns, and which are stuck behind an escalation nobody has answered.
    # Reusing the folded documents rather than re-walking the raw ledgers is what keeps the
    # assignment card and the ticket row from ever disagreeing about the same record.
    assignments_docs = assignment_docs(assignments, now, registry, usage_by_session)
    escalations_docs = escalation_docs(escalations)
    tickets_docs = ticket_docs(
        tickets, pr_by_ticket, owners,
        assignment_refs=_assignment_refs(assignments_docs),
        escalated_refs=_escalated_refs(escalations_docs, registry),
    )
    for collection, docs in (
        # The claim window is sized off the very cadence stamped onto meta/status below, so the
        # page and the pump can never disagree about how old is too old.
        ("sessions", session_docs(agents, registry, claims, now, claim_stale_after(pump_period_s))),
        ("escalations", escalations_docs),
        ("tickets", tickets_docs),
        ("assignments", assignments_docs),
    ):
        for doc_id, data in docs.items():
            key = as_doc_id(doc_id)
            if not key:
                print(f"board_state: dropping {collection} doc with unusable id {doc_id!r}", file=sys.stderr)
                continue
            writes.append({"op": "set", "collection": collection, "doc_id": key, "data": data})
    # Last, always: this document asserts the rows above it are current, so a batch that dies
    # halfway must not have already claimed a sweep that did not land.
    writes.append(
        {
            "op": "set",
            "collection": "meta",
            "doc_id": "status",
            "data": meta_status(
                now=now,
                ado_swept_at=ado_swept_at,
                sessions_scanned_at=now,
                manager=manager,
                default_sprint=resolve_default_sprint(iterations or [], tickets_docs, now),
                pump_period_s=pump_period_s,
            ),
        }
    )
    return writes


import sys


def _safe(reader, fallback, name):
    """Read one source, or fall back. A source that cannot be read must not blank the board —
    every other source is still worth publishing, and meta/status shows the missing one aging.

    Prints which reader failed and why to stderr. This exact broad catch once swallowed a
    FileNotFoundError in the registry join for a full day: every session document silently wrote
    branch/worktree/short_id/ado_refs as null, and a broken join looked identical to a working
    one from the scheduled run's output. The catch stays broad — that part was always correct —
    but silence about WHICH reader gave up is what let it go unnoticed.
    """
    try:
        return reader()
    except Exception as exc:
        print(f"board_state: {name} reader failed, falling back to {fallback!r}: {exc}", file=sys.stderr)
        return fallback


def collect(
    read_agents,
    read_registry,
    read_escalations,
    read_tickets,
    read_prs,
    now,
    read_manager=dict,
    read_assignments=list,
    read_usage=lambda registry: {},
    read_iterations=list,
    owners=None,
    read_timer_period=lambda: None,
    read_claims=lambda registry: {},
) -> list[dict]:
    """Gather every source and return the write set. Readers are injected so this is testable
    without `az`, `gh`, or a live session.

    `read_manager` defaults to `dict` (a zero-arg callable returning `{}`, the same idiom
    `read_prs=dict` already uses elsewhere) so every existing caller that has no manager reader
    to give keeps getting the same null manager fields as before this parameter existed.
    `read_assignments` defaults to `list` for the same reason, and so does `read_iterations`:
    a caller with no iteration reader gets `default_sprint: None` — the page falls back to "tất
    cả", not an error. `read_usage` is the one reader that takes an argument — the registry,
    because the join from a plan step's `owner` to a session transcript runs through it, and
    reading the registry a second time inside the reader would let the two copies disagree about
    which task owns which session. `owners` is not a reader — dashboard._ado_identities() never
    fails the way `az`/`gh` can — so it is passed straight through to ticket_docs() via
    build_writes() rather than wrapped in _safe().
    """
    tickets = _safe(read_tickets, None, "tickets")
    registry = _safe(read_registry, {}, "registry")
    stamp = now()
    return build_writes(
        agents=_safe(read_agents, [], "agents"),
        registry=registry,
        escalations=_safe(read_escalations, [], "escalations"),
        tickets=tickets or [],
        pr_by_ticket=_safe(read_prs, {}, "prs"),
        now=stamp,
        # None, not `stamp`: a sweep that failed must not claim to have just run.
        ado_swept_at=stamp if tickets is not None else None,
        manager=_safe(read_manager, {}, "manager"),
        assignments=_safe(read_assignments, [], "assignments"),
        # {} on failure, not zeroed totals: every assignment then reads "chưa rõ" rather than
        # claiming a measured spend of nothing.
        usage_by_session=_safe(lambda: read_usage(registry), {}, "usage"),
        # [] on failure: az being unreachable degrades default_sprint to null, same as no
        # current sprint at all — it must never blank the sessions/escalations/tickets already
        # gathered above.
        iterations=_safe(read_iterations, [], "iterations"),
        owners=owners,
        # None on failure, never a guessed cadence: the page treats an unknown cadence as its own
        # reason to distrust the data, which is the right answer when the unit file that defines
        # the pump's schedule cannot even be read.
        pump_period_s=_safe(read_timer_period, None, "timer"),
        # {} on failure: every session keeps its observed state and simply carries no claim,
        # which is what the great majority of them look like anyway. `read_claims` takes the
        # registry for the same reason `read_usage` does — the registry is what says where each
        # worker's worktree is, and reading it twice would let the two copies disagree.
        claims=_safe(lambda: read_claims(registry), {}, "claims"),
    )


WORKER_CLAIM_PATH = os.path.join(".claude", "worker-status.json")


def read_worker_claims(registry: dict) -> dict:
    """`{task_name: raw_json}` from each worktree's own `.claude/worker-status.json`.

    One file per worker, inside that worker's own worktree: each worker owns exactly one file, so
    there is no lock to take and nothing two workers can tear. The same filesystem-first pattern
    the registry itself uses.

    A worktree with no file is simply absent from the result — "no claim", not an empty one. A
    file that will not parse is returned as its RAW TEXT instead: validate_claim() then refuses it
    with a reason the board can show, which is what tells a worker's author their file is being
    ignored. Dropping it here would look identical to never having written one.
    """
    claims = {}
    for name, entry in (registry or {}).items():
        path = entry.get("path") if isinstance(entry, dict) else None
        if not name or not path:
            continue
        try:
            with open(os.path.join(path, WORKER_CLAIM_PATH), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        try:
            claims[name] = json.loads(text)
        except json.JSONDecodeError:
            claims[name] = text
    return claims


def _timer_unit_path() -> str:
    """The unit file shipped in this repo, which is what bin/systemd/README.md tells the operator
    to symlink and install. Deliberately the repo copy and not `systemctl --user cat`: this must
    not shell out, and an installed unit that has drifted from the repo is drift the operator has
    to fix at the source anyway (test_board_timer.py already guards unit/doc agreement)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "systemd", "board-mirror.timer")


def _registry_path(repo_root: str) -> str:
    """Where parallel-task.sh recorded what it provisioned, for a given repo root."""
    return os.path.join(repo_root, ".claude", "worktrees", ".parallel-registry.json")


def main() -> int:
    """Print the write set as JSON. The scheduled session pipes this into `write_db`."""
    import time

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dashboard
    import manager_daemon
    import manager_session

    def read_registry():
        # The registry FILE is already keyed by task name, which is the shape session_docs
        # wants. `dashboard.get_registry()` shells out to parallel-task.sh and returns a list;
        # reading the file skips a subprocess and a reshape.
        #
        # NOT dashboard.REPO_DIR: that is os.getcwd() frozen at IMPORT time. dashboard.main()
        # reassigns it, but this script never calls dashboard.main() — board-mirror.md runs it
        # directly with no cwd of its own, so that reassignment never happens and REPO_DIR
        # silently resolves against whatever cwd the scheduled session happened to have. This
        # swallowed the whole registry join for a full day: every session doc wrote
        # branch/worktree/short_id/ado_refs as null. Resolve the root explicitly instead, the
        # same precedence dashboard.main()/manager_daemon.py's own entry points use (this script
        # has no argv tier of its own, same as manager_daemon.py).
        path = _registry_path(manager_session.resolve_repo_root())
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def read_usage(registry):
        """Token totals per session, for every task the registry names.

        Keyed by session id because that is what a plan step's `owner` resolves to through the
        registry. A session with no transcript maps to None, not to zeroed counters — see
        read_session_usage().
        """
        usage = {}
        for entry in (registry or {}).values():
            sid = entry.get("session_id") if isinstance(entry, dict) else None
            if sid:
                usage[sid] = read_session_usage(sid)
        return usage

    def read_prs():
        # Same repo-root resolution as read_registry() above, and for the same reason: this
        # entry point never runs dashboard.main(), so dashboard.REPO_DIR would silently stay
        # frozen at whatever cwd this process happened to import under.
        repo_root = manager_session.resolve_repo_root()
        return prs_by_ticket(dashboard.get_github_prs(repo_root))

    def read_timer_period():
        with open(_timer_unit_path(), encoding="utf-8") as f:
            return timer_period_seconds(f.read())

    writes = collect(
        # list_agents lives in manager_daemon, not dashboard.
        read_agents=manager_daemon.list_agents,
        read_registry=read_registry,
        # current_state(), not get_escalations()["needs_human"]: the latter only ever contains
        # records still open (or freshly needs_human) — the moment a record is answered or
        # dismissed anywhere (this plugin's CLI dashboard, the manager) it drops out of that dict
        # and the mirror stops writing it, freezing the board's copy open forever. current_state()
        # folds the append-only ledger to the latest record per id regardless of status, so every
        # escalation the ledger knows about — including one just resolved — keeps being mirrored.
        read_escalations=lambda: current_state(QUEUE_PATH),
        # No `or None`: an empty backlog is a successful sweep that found nothing, and must
        # stamp last_ado_sweep. Only an exception (caught by _safe) means "did not run".
        read_tickets=dashboard.get_ado_backlog,
        read_prs=read_prs,
        now=time.time,
        # _read_state() returns {"session_id", "started_at"} — exactly the shape
        # meta_status(manager=...) reads. Without this, `collect()` had no way at all to pass a
        # manager through, so meta/status.manager_session_id stayed null forever and the board
        # showed "Manager: chưa có phiên nào nhận việc" even while the daemon was running.
        read_manager=manager_session._read_state,
        # read_all(), not open_assignments() and not current_state(). open_assignments() is the
        # trap the escalation reader above documents: it drops everything done or cancelled, so
        # an assignment would vanish off the board at the exact moment it was finished, and the
        # board would keep publishing the last open copy of it forever. current_state() avoids
        # that but folds the ledger to the newest record per id, whose `ts` is when the manager
        # last TOUCHED the assignment — which is not when the work started. assignment_docs()
        # folds by overwrite itself, so handing it the raw ledger publishes the same latest
        # record AND lets it find the oldest one to measure elapsed time from.
        read_assignments=lambda: read_all(LEDGER_PATH),
        read_usage=read_usage,
        read_iterations=dashboard.get_ado_iterations,
        owners=dashboard._ado_identities(),
        read_timer_period=read_timer_period,
        read_claims=read_worker_claims,
    )
    json.dump(writes, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
