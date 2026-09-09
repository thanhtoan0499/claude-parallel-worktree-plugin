# Artifact Board Mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror the existing ledger and ADO data into an artifact `db`, and publish a board page that renders it live — with the current dashboard on :4400 left running and untouched.

**Architecture:** A pure Python module (`bin/board_state.py`) turns today's data sources into the four db collections defined by the spec. It touches no network and no session — every branch is unit-testable. A thin scheduled `claude --bg` session calls it and writes the result via the Artifact tool's `write_db`. The published page declares `capabilities: {db: {}}` and renders through `onSnapshot`, so it updates without republishing or polling.

**Tech Stack:** Python 3.12 stdlib only (no new dependencies); assert-based pytest in `bin/test_*.py`; the Artifact tool (`publish`, `write_db`) from a Claude session; artifact runtime contract 0.2.41 (`claude.use("db")`).

**Spec:** `docs/superpowers/specs/2026-09-07-session-messaging-design.md` (step 1 of five)

## Global Constraints

- **Nothing is removed in this step.** `dashboard.py`, `dashboard.html`, `manager_daemon.py` and both `.jsonl` ledgers keep working exactly as they do today. This step only adds.
- **Python stdlib only.** The repo has no third-party runtime dependencies; do not add any.
- **Tests are assert-based pytest** in `bin/test_*.py`, run with `python3 -m pytest bin/ -q` from the repo root. 239 pass today; every task must leave the suite green.
- **No network or subprocess in unit tests.** Existing tests fake `subprocess.run` and inject readers; follow that pattern.
- **Collection and field names come from the spec verbatim** — `sessions/<task-name>`, `escalations/<id>`, `tickets/<ado-id>`, `meta/status`. Do not rename.
- **`state` vocabulary for sessions:** `running` · `idle` · `waiting` · `done`. `waiting` means blocked on a permission prompt and is the state the current dashboard cannot express.
- **Escalation `kind`** is normalised with the existing `escalations.normalize_kind()`; the raw value is preserved alongside it as `kind_raw`.
- **Commit after every task.** One `<type>: <description>` subject line, no body unless the WHY is non-obvious.

---

### Task 1: Session documents from the agents listing

**Files:**
- Create: `bin/board_state.py`
- Test: `bin/test_board_state.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `session_docs(agents: list[dict], registry: dict) -> dict[str, dict]` — maps task name to a document body. `agents` is `claude agents --json --all` output; `registry` is the parsed `.parallel-registry.json`.

- [ ] **Step 1: Write the failing test**

```python
#!/usr/bin/env python3
"""assert-based checks for board_state, the pure transform behind the artifact board."""

from board_state import session_docs


def test_session_docs_key_on_task_name_and_carry_registry_fields():
    agents = [
        {
            "name": "bms-auto-match",
            "sessionId": "aaaabbbb-cccc-dddd-eeee-ffff00001111",
            "state": "running",
            "startedAt": 1788762621779,
        }
    ]
    registry = {
        "bms-auto-match": {
            "branch": "feature/bms-auto-match",
            "path": "/repo/.claude/worktrees/bms-auto-match",
            "short_id": "bac537a1",
            "ado_ids": ["7763"],
        }
    }

    docs = session_docs(agents, registry)

    assert list(docs) == ["bms-auto-match"]
    doc = docs["bms-auto-match"]
    assert doc["task"] == "bms-auto-match"
    assert doc["session_id"] == "aaaabbbb-cccc-dddd-eeee-ffff00001111"
    assert doc["short_id"] == "bac537a1"
    assert doc["state"] == "running"
    assert doc["branch"] == "feature/bms-auto-match"
    assert doc["worktree"] == "/repo/.claude/worktrees/bms-auto-match"
    assert doc["ado_refs"] == ["7763"]
    assert doc["started_at"] == 1788762621779


def test_session_docs_survive_an_agent_the_registry_never_heard_of():
    """A session dispatched by any other means still belongs on the board — the registry only
    adds branch/worktree once parallel-task.sh provisioned it."""
    docs = session_docs([{"name": "ad-hoc", "sessionId": "s1", "state": "idle"}], {})

    assert docs["ad-hoc"]["state"] == "idle"
    assert docs["ad-hoc"]["branch"] is None
    assert docs["ad-hoc"]["ado_refs"] == []


def test_session_docs_read_state_from_either_field_name():
    """`claude agents --json` has used both `state` and `status`; the dashboard already reads
    whichever is present and this must not disagree with it."""
    assert session_docs([{"name": "a", "sessionId": "s", "status": "waiting"}], {})["a"]["state"] == "waiting"


def test_session_docs_skip_an_entry_with_no_name():
    """A document id cannot be empty. An unnamed agent is dropped rather than written to a
    garbage key that nothing can address later."""
    assert session_docs([{"sessionId": "s1", "state": "idle"}], {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/azureuser/projects/claude-parallel-worktree-plugin && python3 -m pytest bin/test_board_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'board_state'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Pure transforms from this plugin's own data sources into artifact-db documents.

No I/O, no subprocess, no session: every function here takes already-read data and returns
plain dicts. That is what makes the board's data model testable without publishing anything.
"""


def session_docs(agents: list[dict], registry: dict) -> dict[str, dict]:
    """One document per live task, keyed by task name.

    Agent-first, not registry-first: a session doing work belongs on the board immediately,
    including one dispatched by any other means, and the registry only fills in branch/worktree
    once parallel-task.sh has provisioned it.
    """
    docs = {}
    for agent in agents or []:
        name = agent.get("name")
        if not name:
            # A document id cannot be empty; an unnamed agent has no addressable key.
            continue
        reg = (registry or {}).get(name) or {}
        docs[name] = {
            "task": name,
            "session_id": agent.get("sessionId"),
            "short_id": reg.get("short_id"),
            # `claude agents --json` has used both spellings; read whichever is present.
            "state": agent.get("state") or agent.get("status"),
            "branch": reg.get("branch"),
            "worktree": reg.get("path"),
            "started_at": agent.get("startedAt"),
            "ado_refs": list(reg.get("ado_ids") or []),
        }
    return docs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest bin/test_board_state.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/board_state.py bin/test_board_state.py
git commit -m "feat: session documents for the artifact board"
```

---

### Task 2: Escalation documents with normalised kind and severity

**Files:**
- Modify: `bin/board_state.py`
- Test: `bin/test_board_state.py`

**Interfaces:**
- Consumes: `escalations.normalize_kind(raw) -> str | None` and `escalations.classify(record) -> tuple[str, str]`, both existing.
- Produces: `escalation_docs(records: list[dict]) -> dict[str, dict]` — maps escalation id to a document body carrying `kind`, `kind_raw`, `severity`.

- [ ] **Step 1: Write the failing test**

```python
from board_state import escalation_docs


def test_escalation_docs_normalise_kind_and_keep_the_raw_spelling():
    """A worker wrote `blocked_on_credentials` where the vocabulary says `credentials`. The
    board must score it as the credentials outage it is, while still showing what was written
    so the drift gets fixed."""
    docs = escalation_docs(
        [
            {
                "id": "e1",
                "ts": 1788700000.0,
                "session_id": "s1",
                "kind": "blocked_on_credentials",
                "question": "OAuth expired again",
                "options": ["Renew the credential", "Switch to a service account"],
                "status": "open",
            }
        ]
    )

    doc = docs["e1"]
    assert doc["kind"] == "credentials"
    assert doc["kind_raw"] == "blocked_on_credentials"
    assert doc["severity"] == "P0"
    assert doc["options"] == ["Renew the credential", "Switch to a service account"]
    assert doc["status"] == "open"
    assert doc["answer"] is None


def test_escalation_docs_score_an_unrecognisable_kind_below_an_urgent_one_but_still_flag_it():
    """Widening what is recognised must not widen what looks urgent — an unknown kind is still
    a human's call (P1), never silently promoted."""
    docs = escalation_docs([{"id": "e2", "kind": "totally_made_up", "question": "?"}])

    assert docs["e2"]["kind"] is None
    assert docs["e2"]["kind_raw"] == "totally_made_up"
    assert docs["e2"]["severity"] == "P1"


def test_escalation_docs_mark_a_mechanical_kind_lowest():
    docs = escalation_docs([{"id": "e3", "kind": "scope_question", "question": "in or out?"}])

    assert docs["e3"]["severity"] == "P2"


def test_escalation_docs_carry_an_answer_once_one_exists():
    docs = escalation_docs(
        [{"id": "e4", "kind": "red_tests", "question": "?", "status": "answered",
          "answer": "rerun", "answered_at": 1788700100.0}]
    )

    assert docs["e4"]["status"] == "answered"
    assert docs["e4"]["answer"] == "rerun"
    assert docs["e4"]["answered_at"] == 1788700100.0


def test_escalation_docs_tolerate_a_string_options_field():
    """`options` is worker-authored against a prose schema; a bare string is one option, not a
    sequence of characters to iterate."""
    docs = escalation_docs([{"id": "e5", "kind": "looping", "question": "?", "options": "just this"}])

    assert docs["e5"]["options"] == ["just this"]


def test_escalation_docs_skip_a_record_with_no_id():
    assert escalation_docs([{"kind": "looping", "question": "?"}]) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest bin/test_board_state.py -q -k escalation`
Expected: FAIL — `ImportError: cannot import name 'escalation_docs'`

- [ ] **Step 3: Write minimal implementation**

Add to `bin/board_state.py`:

```python
from escalations import classify, normalize_kind, normalize_options

# Kinds that mean production is already hurting. Scored off the CANONICAL name, never the raw
# one: `blocked_on_credentials` is a credentials outage and must not be scored as an unknown.
_URGENT_KINDS = frozenset({"credentials", "irreversible", "cost_anomaly", "push_or_pr"})


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
        }
    return docs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest bin/test_board_state.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/board_state.py bin/test_board_state.py
git commit -m "feat: escalation documents with normalised kind and severity"
```

---

### Task 3: Ticket documents from the ADO shape

**Files:**
- Modify: `bin/board_state.py`
- Test: `bin/test_board_state.py`

**Interfaces:**
- Consumes: the shape `dashboard._shape_ado_ticket()` already produces — `{"id", "title", "state", "sprint", "url"}`.
- Produces: `ticket_docs(tickets: list[dict], pr_by_ticket: dict[str, dict]) -> dict[str, dict]`.

- [ ] **Step 1: Write the failing test**

```python
from board_state import ticket_docs


def test_ticket_docs_key_on_ado_id_and_attach_a_known_pr():
    docs = ticket_docs(
        [{"id": "8311", "title": "Stabilise tool order", "state": "Active",
          "sprint": "Sprint 57", "url": "https://dev.azure.com/x/_workitems/edit/8311"}],
        {"8311": {"number": 698, "state": "OPEN",
                  "url": "https://github.com/o/r/pull/698"}},
    )

    doc = docs["8311"]
    assert doc["id"] == "8311"
    assert doc["state"] == "Active"
    assert doc["sprint"] == "Sprint 57"
    assert doc["pr"] == {"number": 698, "state": "OPEN", "url": "https://github.com/o/r/pull/698"}


def test_ticket_docs_leave_pr_null_when_no_pr_references_the_ticket():
    docs = ticket_docs([{"id": "5061", "title": "F1 nondeterminism", "state": "New",
                         "sprint": "Sprint 57", "url": "u"}], {})

    assert docs["5061"]["pr"] is None


def test_ticket_docs_keep_an_empty_sprint_rather_than_inventing_one():
    """Work items parked at the project root genuinely have no sprint; the board filters on
    this field and a made-up value would mis-file them."""
    docs = ticket_docs([{"id": "1", "title": "t", "state": "New", "sprint": "", "url": "u"}], {})

    assert docs["1"]["sprint"] == ""


def test_ticket_docs_skip_a_ticket_with_no_id():
    assert ticket_docs([{"title": "orphan", "state": "New"}], {}) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest bin/test_board_state.py -q -k ticket`
Expected: FAIL — `ImportError: cannot import name 'ticket_docs'`

- [ ] **Step 3: Write minimal implementation**

Add to `bin/board_state.py`:

```python
def ticket_docs(tickets: list[dict], pr_by_ticket: dict) -> dict[str, dict]:
    """One document per ADO work item, keyed by its id.

    "Not started", "in flight" and "done this sprint" are filters over `state` + `sprint` on
    the page, not three collections here.
    """
    docs = {}
    for ticket in tickets or []:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue
        docs[str(ticket_id)] = {
            "id": str(ticket_id),
            "title": ticket.get("title") or "",
            "state": ticket.get("state") or "",
            "sprint": ticket.get("sprint") or "",
            "type": ticket.get("type") or "",
            "url": ticket.get("url") or "",
            "pr": (pr_by_ticket or {}).get(str(ticket_id)),
        }
    return docs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest bin/test_board_state.py -q`
Expected: PASS (14 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/board_state.py bin/test_board_state.py
git commit -m "feat: ticket documents for the artifact board"
```

---

### Task 4: The freshness document

**Files:**
- Modify: `bin/board_state.py`
- Test: `bin/test_board_state.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `meta_status(now: float, ado_swept_at=None, sessions_scanned_at=None, manager=None) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
from board_state import meta_status


def test_meta_status_records_each_source_separately():
    """The cadences differ by design — sessions update on events, ADO on a slow cron. One
    combined timestamp would let a 30-minute-old ticket list look as fresh as a live session."""
    doc = meta_status(now=1000.0, ado_swept_at=400.0, sessions_scanned_at=990.0,
                      manager={"session_id": "m1", "started_at": 100.0})

    assert doc["last_ado_sweep"] == 400.0
    assert doc["last_session_scan"] == 990.0
    assert doc["manager_session_id"] == "m1"
    assert doc["manager_started_at"] == 100.0
    assert doc["written_at"] == 1000.0


def test_meta_status_reports_a_source_that_has_never_run_as_null_not_as_now():
    """A never-run sweep must not read as a fresh one. The page shows age from these fields,
    and `now` here would claim data that does not exist."""
    doc = meta_status(now=1000.0)

    assert doc["last_ado_sweep"] is None
    assert doc["last_session_scan"] is None
    assert doc["manager_session_id"] is None
    assert doc["written_at"] == 1000.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest bin/test_board_state.py -q -k meta`
Expected: FAIL — `ImportError: cannot import name 'meta_status'`

- [ ] **Step 3: Write minimal implementation**

Add to `bin/board_state.py`:

```python
def meta_status(now: float, ado_swept_at=None, sessions_scanned_at=None, manager=None) -> dict:
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
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest bin/test_board_state.py -q`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add bin/board_state.py bin/test_board_state.py
git commit -m "feat: per-source freshness document for the artifact board"
```

---

### Task 5: One batch of writes from all four sources

**Files:**
- Modify: `bin/board_state.py`
- Test: `bin/test_board_state.py`

**Interfaces:**
- Consumes: `session_docs`, `escalation_docs`, `ticket_docs`, `meta_status` from Tasks 1–4.
- Produces: `build_writes(agents, registry, escalations, tickets, pr_by_ticket, now, ado_swept_at=None, manager=None) -> list[dict]` — a list of `{"op": "set", "collection": str, "doc_id": str, "data": dict}` entries, the exact shape the Artifact tool's `write_db` batch takes.

- [ ] **Step 1: Write the failing test**

```python
from board_state import build_writes


def _writes_for(writes, collection):
    return [w for w in writes if w["collection"] == collection]


def test_build_writes_emits_one_set_per_document_across_all_four_collections():
    writes = build_writes(
        agents=[{"name": "t1", "sessionId": "s1", "state": "running"}],
        registry={},
        escalations=[{"id": "e1", "kind": "credentials", "question": "?"}],
        tickets=[{"id": "8311", "title": "x", "state": "Active", "sprint": "Sprint 57", "url": "u"}],
        pr_by_ticket={},
        now=1000.0,
    )

    assert {w["collection"] for w in writes} == {"sessions", "escalations", "tickets", "meta"}
    assert all(w["op"] == "set" for w in writes)
    assert _writes_for(writes, "sessions")[0]["doc_id"] == "t1"
    assert _writes_for(writes, "escalations")[0]["doc_id"] == "e1"
    assert _writes_for(writes, "tickets")[0]["doc_id"] == "8311"
    assert _writes_for(writes, "meta")[0]["doc_id"] == "status"


def test_build_writes_puts_meta_status_last():
    """`meta/status` claims the data alongside it is current. Written first, a batch that dies
    halfway would advertise a sweep whose rows never landed."""
    writes = build_writes(
        agents=[{"name": "t1", "sessionId": "s1", "state": "idle"}],
        registry={}, escalations=[], tickets=[], pr_by_ticket={}, now=1000.0,
    )

    assert writes[-1]["collection"] == "meta"
    assert writes[-1]["doc_id"] == "status"


def test_build_writes_stamps_the_session_scan_time_from_now():
    writes = build_writes(agents=[], registry={}, escalations=[], tickets=[],
                          pr_by_ticket={}, now=1234.0, ado_swept_at=999.0)

    meta = writes[-1]["data"]
    assert meta["last_session_scan"] == 1234.0
    assert meta["last_ado_sweep"] == 999.0


def test_build_writes_on_empty_sources_still_writes_meta():
    """An empty board is a real state — no workers, no escalations. It must be distinguishable
    from a sweep that never ran, and only meta/status can say which."""
    writes = build_writes(agents=[], registry={}, escalations=[], tickets=[],
                          pr_by_ticket={}, now=1000.0)

    assert len(writes) == 1
    assert writes[0]["doc_id"] == "status"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest bin/test_board_state.py -q -k build_writes`
Expected: FAIL — `ImportError: cannot import name 'build_writes'`

- [ ] **Step 3: Write minimal implementation**

Add to `bin/board_state.py`:

```python
def build_writes(
    agents,
    registry,
    escalations,
    tickets,
    pr_by_ticket,
    now: float,
    ado_swept_at=None,
    manager=None,
) -> list[dict]:
    """Every document to write, in the order to write it.

    Shaped as the Artifact tool's `write_db` batch entries so the calling session passes this
    straight through without reshaping — the transform is testable here, and the session stays
    a thin courier.
    """
    writes = []
    for collection, docs in (
        ("sessions", session_docs(agents, registry)),
        ("escalations", escalation_docs(escalations)),
        ("tickets", ticket_docs(tickets, pr_by_ticket)),
    ):
        for doc_id, data in docs.items():
            writes.append({"op": "set", "collection": collection, "doc_id": doc_id, "data": data})
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
            ),
        }
    )
    return writes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest bin/ -q`
Expected: PASS — 239 existing + 20 new = 259

- [ ] **Step 5: Commit**

```bash
git add bin/board_state.py bin/test_board_state.py
git commit -m "feat: batch every board collection into one ordered write set"
```

---

### Task 6: A CLI that prints the write set

**Files:**
- Modify: `bin/board_state.py`
- Test: `bin/test_board_state.py`

**Interfaces:**
- Consumes: `build_writes` from Task 5.
- Produces: `python3 bin/board_state.py` printing the write set as JSON on stdout. This is the seam the scheduled session calls — it reads the real files and shells out to `az`/`claude agents`, so it lives behind `main()` and is never exercised by a unit test.

- [ ] **Step 1: Write the failing test**

```python
def test_collect_uses_injected_readers_and_never_touches_the_network():
    """The readers are injected so the collection step is testable without `az`, `gh` or a live
    session — the same shape `test_dashboard.py` already uses for `ask`."""
    from board_state import collect

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "waiting"}],
        read_registry=lambda: {"t1": {"branch": "feature/x", "short_id": "ab12"}},
        read_escalations=lambda: [{"id": "e1", "kind": "credentials", "question": "?"}],
        read_tickets=lambda: [{"id": "1", "title": "t", "state": "New", "sprint": "S1", "url": "u"}],
        read_prs=lambda: {},
        now=lambda: 500.0,
    )

    sessions = [w for w in writes if w["collection"] == "sessions"]
    assert sessions[0]["data"]["state"] == "waiting"
    assert writes[-1]["data"]["written_at"] == 500.0


def test_collect_degrades_to_an_empty_source_when_one_reader_fails():
    """`az` not being authenticated must not blank the whole board — every other source is
    still worth publishing, and meta/status will show the ADO age going stale."""
    from board_state import collect

    def boom():
        raise OSError("az not logged in")

    writes = collect(
        read_agents=lambda: [{"name": "t1", "sessionId": "s1", "state": "idle"}],
        read_registry=lambda: {},
        read_escalations=lambda: [],
        read_tickets=boom,
        read_prs=lambda: {},
        now=lambda: 500.0,
    )

    assert [w for w in writes if w["collection"] == "sessions"]
    assert not [w for w in writes if w["collection"] == "tickets"]
    assert writes[-1]["data"]["last_ado_sweep"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest bin/test_board_state.py -q -k collect`
Expected: FAIL — `ImportError: cannot import name 'collect'`

- [ ] **Step 3: Write minimal implementation**

Add to `bin/board_state.py`:

```python
import json
import subprocess
import sys

_SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


def _safe(reader, fallback):
    """Read one source, or fall back. A source that cannot be read must not blank the board —
    every other source is still worth publishing, and meta/status shows the missing one aging."""
    try:
        return reader()
    except Exception:  # noqa: BLE001 — any reader failure degrades to "this source is absent"
        return fallback


def collect(read_agents, read_registry, read_escalations, read_tickets, read_prs, now) -> list[dict]:
    """Gather every source and return the write set. Readers are injected so this is testable
    without `az`, `gh`, or a live session."""
    tickets = _safe(read_tickets, None)
    stamp = now()
    return build_writes(
        agents=_safe(read_agents, []),
        registry=_safe(read_registry, {}),
        escalations=_safe(read_escalations, []),
        tickets=tickets or [],
        pr_by_ticket=_safe(read_prs, {}),
        now=stamp,
        # None, not `stamp`: a sweep that failed must not claim to have just run.
        ado_swept_at=stamp if tickets is not None else None,
    )


def main() -> int:
    """Print the write set as JSON. The scheduled session pipes this into `write_db`."""
    import time

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import dashboard
    import manager_daemon

    def read_registry():
        # The registry FILE is already keyed by task name, which is the shape session_docs
        # wants. `dashboard.get_registry()` shells out to parallel-task.sh and returns a list;
        # reading the file skips a subprocess and a reshape.
        path = os.path.join(dashboard.REPO_DIR, ".claude", "worktrees", ".parallel-registry.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    writes = collect(
        # list_agents lives in manager_daemon, not dashboard.
        read_agents=manager_daemon.list_agents,
        read_registry=read_registry,
        read_escalations=lambda: dashboard.get_escalations()["needs_human"],
        # No `or None`: an empty backlog is a successful sweep that found nothing, and must
        # stamp last_ado_sweep. Only an exception (caught by _safe) means "did not run".
        read_tickets=dashboard.get_ado_backlog,
        read_prs=lambda: {},
        now=time.time,
    )
    json.dump(writes, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add `import os` to the imports at the top of the file.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest bin/ -q`
Expected: PASS — 261

Then confirm the CLI runs against real data:

Run: `cd /home/azureuser/projects/claude-parallel-worktree-plugin && python3 bin/board_state.py | python3 -c "import sys,json; w=json.load(sys.stdin); print(len(w),'writes'); print(sorted({x['collection'] for x in w}))"`
Expected: a non-zero count and the four collection names.

- [ ] **Step 5: Commit**

```bash
git add bin/board_state.py bin/test_board_state.py
git commit -m "feat: board_state CLI emitting the write set as JSON"
```

---

### Task 7: The board page

**Files:**
- Create: `bin/board.html`

**Interfaces:**
- Consumes: the four collections written by Task 5, read live via `claude.use("db")`.
- Produces: a page published once with `capabilities: {db: {}}`; nothing else reads it.

There is no unit test for this task — it is a browser page against a live store. Its check is Task 8's manual verification.

- [ ] **Step 1: Write the page**

Create `bin/board.html`. Requirements, each of which Task 8 verifies:

- Declares nothing itself; capabilities are passed at publish time.
- `const db = await claude.use("db")` — **branch on `null`** and render an explicit "cannot reach data" panel. Never a blank board.
- Four subscriptions via `onSnapshot`, one per collection: `sessions`, `escalations`, `tickets`, and the single doc `meta/status`.
- Renders at rest: before any snapshot arrives, show a "connecting" state, not an empty page.
- **Escalations first**, sorted P0 → P1 → P2, each showing `severity`, `kind` (with a ⚠ when `kind !== kind_raw`), `question`, waiting time from `ts`, and one button per entry in `options`.
- **Sessions** next, with `waiting` visually distinct from `running` — that state is the whole reason this collection exists.
- **Tickets** last, filterable by `sprint` and `state`, sorted by ticket number, with done work collapsed.
- A freshness line per source from `meta/status`: "ADO as of N minutes ago", "sessions as of N seconds ago". When an age exceeds 3× its expected cadence, mark it — a growing age is the failure signal.
- Clicking an option writes `db.doc("escalations/<id>").update({answer, answered_at, status: "answered"})`. The manager picks it up later; the page must say so ("sent — the manager will act on this shortly") rather than implying it took effect.
- Theme-aware per the artifact design rules; no external scripts.

The db wiring is the part with a real contract and must not be guessed — use exactly this
skeleton (runtime contract 0.2.41; `DocumentSnapshot` is `{id, exists, data()}`, `QuerySnapshot`
is `{docs, size, empty}`):

```html
<script>
const view = { sessions: [], escalations: [], tickets: [], meta: null, db: null };

function render() { /* draw from `view`; called after every snapshot */ }

(async () => {
  render();                                   // at rest, before any data
  const db = await claude.use("db");          // null: not served, not granted, or failed
  if (!db) {
    document.getElementById("board").replaceChildren(
      Object.assign(document.createElement("div"), {
        className: "unavailable",
        textContent: "Không đọc được dữ liệu bảng.",
      }));
    return;                                   // never leave the page blank
  }
  view.db = db;

  const onErr = (where) => (e) => {
    // A terminal snapshot error kills that listener; say which source went dark rather than
    // letting its section quietly freeze at its last value.
    console.error(where, e);
    view[where] = [];
    render();
  };

  db.collection("sessions").onSnapshot(
    (snap) => { view.sessions = snap.docs.map(d => ({ id: d.id, ...d.data() })); render(); },
    onErr("sessions"));
  db.collection("escalations").onSnapshot(
    (snap) => { view.escalations = snap.docs.map(d => ({ id: d.id, ...d.data() })); render(); },
    onErr("escalations"));
  db.collection("tickets").onSnapshot(
    (snap) => { view.tickets = snap.docs.map(d => ({ id: d.id, ...d.data() })); render(); },
    onErr("tickets"));
  db.doc("meta/status").onSnapshot(
    (snap) => { view.meta = snap.exists ? snap.data() : null; render(); },
    onErr("meta"));
})();

async function answerEscalation(id, option) {
  if (!view.db) return;
  await view.db.doc(`escalations/${id}`).update({
    answer: option,
    answered_at: Date.now() / 1000,
    status: "answered",
  });
  // The manager is asleep and a db write is not a wake — say what actually happens next.
  toast("Đã gửi — manager sẽ xử lý trong vài phút.");
}
</script>
```

- [ ] **Step 2: Commit**

```bash
git add bin/board.html
git commit -m "feat: artifact board page reading the db live"
```

---

### Task 8: Publish once and verify against real data

**Files:**
- Modify: `docs/superpowers/plans/2026-09-07-artifact-board-mirror.md` (record the URL)

This task is manual: it publishes a real artifact and writes real rows.

- [ ] **Step 1: Seed the store from the real sources**

```bash
cd /home/azureuser/projects/claude-parallel-worktree-plugin
python3 bin/board_state.py > /tmp/board-writes.json
python3 -c "import json;w=json.load(open('/tmp/board-writes.json'));print(len(w),'writes')"
```

- [ ] **Step 2: Publish the page**

Publish `bin/board.html` with the Artifact tool, passing `capabilities: {db: {}}`, a title, a description and a favicon. Record the URL in this plan file.

**Published 2026-09-07:** https://claude.ai/code/artifact/bc6f123e-bd44-4840-87e3-d78ffc95529e
(contract 0.2.41, capabilities `db`, sharing `owner`; first seed was 44 documents —
31 sessions, 1 escalation, 11 tickets, 1 meta — committed atomically.)

- [ ] **Step 3: Write the seeded rows**

Use the Artifact tool's `write_db` with `db_op: "batch"`, passing the entries from `/tmp/board-writes.json` (at most 50 per batch — split if the file is longer).

- [ ] **Step 4: Verify in the browser**

Open the URL and confirm, against what `dashboard.py` on :4400 shows at the same moment:
- the same open escalations, with the same severities;
- the same worker tasks and states, including any `waiting`;
- the same ticket counts per sprint;
- freshness lines showing plausible ages.

Any disagreement is a bug in `board_state.py`, not in the page — fix it there, with a test, and re-run the batch.

- [ ] **Step 5: Verify live update without republishing**

With the page open, run `python3 bin/board_state.py` again and write one changed document via `write_db`. The open page must update **without a reload and without a republish**. If it does not, `onSnapshot` is not wired correctly.

- [ ] **Step 6: Commit the recorded URL**

```bash
git add docs/superpowers/plans/2026-09-07-artifact-board-mirror.md
git commit -m "docs: record the published board artifact URL"
```

---

### Task 9: Schedule the mirror

**Files:**
- Create: `bin/board-mirror.md` (the prompt the scheduled session runs)

**Interfaces:**
- Consumes: `python3 bin/board_state.py` from Task 6; the artifact URL from Task 8.
- Produces: a repeatable refresh, run by a scheduled `claude --bg` session.

- [ ] **Step 1: Write the session prompt**

Create `bin/board-mirror.md`:

```markdown
Refresh the manager board. Do exactly this and nothing else.

1. Run: `python3 <plugin bin dir>/board_state.py`
   It prints a JSON array of write entries on stdout.
2. Write those entries to the artifact at <URL recorded in the plan> using the Artifact
   tool's `write_db` with `db_op: "batch"`. Batches take at most 50 entries — split if needed.
3. Report one line: how many documents were written, and the value of `last_ado_sweep`.

Do not publish the page. Do not edit any file. If step 1 fails, report the error and stop —
a failed refresh must leave the previous rows standing rather than write partial state.
```

- [ ] **Step 2: Run it once by hand**

```bash
cd /home/azureuser/projects/claude-parallel-worktree-plugin
claude --bg -n board-mirror -- "$(cat bin/board-mirror.md)"
```

Wait for it to finish (`claude agents --json --all`), then confirm the page updated.

- [ ] **Step 3: Schedule it**

Create a cron entry that runs the same prompt every 15 minutes, using `CronCreate` from a session. Record the cron id in `bin/board-mirror.md` as a comment so it can be found and removed later.

- [ ] **Step 4: Verify two consecutive runs**

Wait for two scheduled runs. Confirm `meta/status.last_ado_sweep` advances each time and the page reflects it without a republish.

- [ ] **Step 5: Commit**

```bash
git add bin/board-mirror.md
git commit -m "feat: scheduled prompt refreshing the board artifact"
```

---

## Done when

- `python3 -m pytest bin/ -q` passes, 261 tests.
- The artifact URL shows the same escalations, sessions and tickets as `:4400` does at the same moment.
- An open page updates from a `write_db` without a republish.
- Two consecutive scheduled runs have advanced `meta/status`.
- `dashboard.py`, `manager_daemon.py` and both ledgers are byte-identical to their state before this plan started.

## Out of scope for this step

Everything that removes something. The manager stays a `-p --resume` subprocess (spec step 3), the
dashboard keeps serving :4400 (step 4), and the ledgers keep being written (step 5). Workers do not
write db here — that is step 2, and the mirror reads the ledger on their behalf until then.
