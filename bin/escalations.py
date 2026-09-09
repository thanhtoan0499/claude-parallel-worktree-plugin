#!/usr/bin/env python3
"""Escalation queue: one append-only JSONL record per report a worker cannot decide alone."""

import fcntl
import json
import os
import time
import uuid

QUEUE_PATH = os.path.expanduser("~/.claude/hermes/escalations.jsonl")


def new_record(session_id: str, kind: str, question: str, options=None, evidence=None) -> dict:
    """A fresh, undecided queue record. `tier` is filled in later by classify()."""
    return {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "session_id": session_id,
        "kind": kind,
        "question": question,
        "options": list(options or []),
        "evidence": dict(evidence or {}),
        "tier": None,
        "status": "open",
        "decided_by": None,
        "answer": None,
        "answered_at": None,
    }


def append(path: str, record: dict) -> None:
    """Append one record. Append-only: history is never rewritten, only added to.

    Locked around the write: several processes (daemon, dashboard, worker sessions) append
    concurrently, and an unlocked interleaved write produces a line `read_all` can only skip
    as garbage — silently losing the escalation.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(record) + "\n")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_all(path: str) -> list[dict]:
    """Every record, oldest first. A truncated or garbage line is skipped, never fatal."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


# Path fragments that make a diff a human's call regardless of how clean it looks.
SENSITIVE_PATH_MARKERS = (
    "auth",
    "secret",
    ".env",
    "credential",
    "migration",
    "token",
    "password",
    "passwd",
    ".pem",
    "id_rsa",
    ".key",
    "keystore",
    ".netrc",
    ".npmrc",
    "schema.sql",
    "alembic/",
)

_TIER2_KINDS = {"red_tests", "looping", "pick_implementation", "scope_question"}
_TIER3_KINDS = {
    "irreversible",
    "push_or_pr",
    "credentials",
    "spec_ambiguity",
    "no_convergence",
    "cost_anomaly",
    "worktree_collision",
}

# The closed vocabulary. A worker picks one of these; anything else is drift.
CANONICAL_KINDS = frozenset(_TIER2_KINDS | _TIER3_KINDS | {"diff_review"})

# Longest first, so a raw kind containing two canonical names resolves to the more specific
# one ("push_or_pr" wins over a bare "pr" were one ever added) rather than to whichever the
# set happened to yield first.
_KINDS_BY_LENGTH = tuple(sorted(CANONICAL_KINDS, key=len, reverse=True))


def normalize_kind(raw) -> str | None:
    """Map a worker-authored `kind` onto the canonical vocabulary, or None if it does not fit.

    `kind` is written by a model against a prose schema, so near-misses are the norm rather
    than the exception — the live queue carried `blocked_on_credentials` where the vocabulary
    says `credentials`. That near-miss is not harmless: it falls through to the unknown branch,
    and while the TIER stays safe (unknown defaults to a human), every consumer keying off the
    kind treats a production credentials outage as an unrecognised one. The dashboard scored it
    P1 instead of P0 for exactly this reason.

    Matching is containment on a normalised form, not equality, because the drift seen in
    practice decorates the canonical name rather than replacing it (`blocked_on_credentials`,
    `credentials_expired`). A raw value that contains none of them returns None — this widens
    what is recognised, and never invents a kind for text that does not name one.
    """
    if not isinstance(raw, str):
        return None
    flat = "".join(ch if ch.isalnum() else "_" for ch in raw.strip().lower())
    if not flat:
        return None
    if flat in CANONICAL_KINDS:
        return flat
    for kind in _KINDS_BY_LENGTH:
        if kind in flat:
            return kind
    return None


def _as_text(value) -> str:
    """Render a field for a reason string whether it arrived as a str or a list."""
    if isinstance(value, str):
        return value
    return ", ".join(str(v) for v in value)


def _sensitive(changed_files) -> str | None:
    # A bare string is one path, not a sequence of characters — iterating it per-character
    # would match no marker and silently pass every secret file as clean.
    if isinstance(changed_files, str):
        changed_files = [changed_files]
    if isinstance(changed_files, (list, tuple)):
        for path in changed_files:
            low = str(path).lower()
            for marker in SENSITIVE_PATH_MARKERS:
                if marker in low:
                    return str(path)
        return None
    # Any other shape (dict, nested container, model-authored oddity) is untrusted,
    # worker-authored evidence — scan its whole string form rather than assume a structure.
    if changed_files:
        low = str(changed_files).lower()
        for marker in SENSITIVE_PATH_MARKERS:
            if marker in low:
                return str(changed_files)
    return None


def normalize_options(raw) -> list[str]:
    """Coerce a record's `options` field to a list of strings, tolerating on-disk drift.

    `options` is worker-authored against a prose schema, not a validated contract — the same
    tolerance _as_text/_sensitive give evidence's fields above. A list keeps only its string
    items (a stray non-string entry is dropped, not stringified — it is not a label any worker
    actually offered). A bare string is one option, not a sequence of characters to iterate
    one-by-one, so it becomes a single-item list. Anything else yields no options rather than
    raising — a caller that does `for opt in options` must never receive something it cannot
    safely walk.
    """
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    if isinstance(raw, str):
        return [raw]
    return []


def classify(record: dict) -> tuple[str, str]:
    """Route one record: ("tier2", reason) the manager may decide, or ("tier3", reason) for a human.

    Tier 3 wins ties — these signals hold whatever the kind is, so a mechanical-looking record
    carrying an irreversible, dependency, migration, or secret-shaped change still goes to a human.
    """
    ev = record.get("evidence") or {}
    raw_kind = record.get("kind")
    kind = normalize_kind(raw_kind)

    if ev.get("irreversible"):
        return "tier3", "evidence marks this irreversible"
    if ev.get("deps_added"):
        return "tier3", f"adds dependency: {_as_text(ev['deps_added'])}"
    if ev.get("migration"):
        return "tier3", "changes a migration or schema"
    hit = _sensitive(ev.get("changed_files"))
    if hit:
        return "tier3", f"touches a sensitive path: {hit}"
    branch = str(ev.get("branch") or "").strip().lower()
    if branch in ("main", "master"):
        return "tier3", f"target branch is {branch}"

    if kind in _TIER3_KINDS:
        return "tier3", f"{kind} always needs a human"

    if kind == "diff_review":
        if ev.get("tests") != "green":
            return "tier3", f"tests are {ev.get('tests') or 'unknown'}, not green"
        # Silence is not consent. The record's author benefits from approval, so an undisclosed
        # field is treated as undisclosed rather than as clean. `key in ev` is not enough:
        # an explicit null is a routine shape for model-authored JSON, and the party filling
        # in evidence is the party asking for approval — so an undisclosed value must read as
        # undisclosed, never as clean.
        for key in ("deps_added", "migration", "changed_files"):
            if ev.get(key) is None:
                return "tier3", f"evidence does not disclose {key}"
        # A number/bool/other scalar here isn't a file list at all — _sensitive() can scan its
        # str() form and find nothing, but that's not the same as a disclosed, clean file list.
        if not isinstance(ev.get("changed_files"), (str, list, tuple, dict)):
            return "tier3", "changed_files is not a file list"
        return "tier2", "tests green, no new deps, no migration, no sensitive path"

    if kind in _TIER2_KINDS:
        return "tier2", f"{kind} is a mechanical call"

    return "tier3", f"unknown kind {raw_kind!r} — defaulting to a human"


def is_undeliverable(record: dict) -> bool:
    """True for a `needs_human` record that already carries a decided answer nobody could
    deliver — not a genuine open question still waiting on human judgement.

    manager_daemon.py's `_try_deliver` is the only place that ever stamps `delivery_attempts`
    onto a record, and it flips `status` to `needs_human` only once attempts are exhausted — so
    this trio (status, a non-null answer, a non-null delivery_attempts) co-occurs only on a
    record that failed delivery, whether the answer came from the manager or a human. A record
    that reaches needs_human via process_open's other branch (the manager punting on an open
    question) carries neither field: new_record() never sets them, and that branch never touches
    them either.
    """
    return (
        record.get("status") == "needs_human"
        and record.get("answer") is not None
        and record.get("delivery_attempts") is not None
    )


def current_state(path: str) -> list[dict]:
    """Fold the append-only log into the latest state of each record, in first-seen order."""
    latest: dict[str, dict] = {}
    order: list[str] = []
    for rec in read_all(path):
        rid = rec.get("id")
        if not rid:
            continue
        if rid not in latest:
            order.append(rid)
        latest[rid] = rec
    return [latest[rid] for rid in order]


def record_answer(path: str, record_id: str, answer: str, decided_by: str) -> dict | None:
    """Answer a record by appending its updated copy. Returns the update, or None if unknown id."""
    for rec in current_state(path):
        if rec.get("id") == record_id:
            updated = dict(rec)
            updated["answer"] = answer
            updated["decided_by"] = decided_by
            updated["status"] = "answered"
            updated["answered_at"] = time.time()
            append(path, updated)
            return updated
    return None


def record_dismiss(path: str, record_id: str, decided_by: str) -> dict | None:
    """Retire a record by appending an updated copy with a terminal status. Returns the update,
    or None if unknown id.

    Append-only, like record_answer: the prior needs_human line is never edited, only
    superseded — current_state() folds to this newer copy, so get_escalations() stops offering
    it under needs_human without needing any filtering logic of its own.
    """
    for rec in current_state(path):
        if rec.get("id") == record_id:
            updated = dict(rec)
            updated["status"] = "dismissed"
            updated["decided_by"] = decided_by
            updated["answered_at"] = time.time()
            append(path, updated)
            return updated
    return None
