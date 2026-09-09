# Manager Tier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let dispatched workers run to completion without babysitting — a short-lived Fable "manager" decides mechanical questions automatically and logs every decision, and only genuinely human calls surface in the dashboard as blocking cards with the evidence already assembled.

**Architecture:** An append-only JSONL queue replaces the freeform `ESCALATIONS.md`. A pure-function tier classifier sorts each report into Tier 2 (manager decides) or Tier 3 (human decides). A daemon routes Tier 2 records to a freshly-spawned `claude --bg --model claude-fable-5` process that returns structured JSON, and delivers answers back into blocked worker sessions via `claude --resume <sid> -p`. The dashboard renders Tier 3 as blocking Decision/Diff-review cards and Tier 2 as a non-blocking audit feed.

**Tech Stack:** Python 3 stdlib only (`json`, `subprocess`, `http.server`, `os`, `time`, `uuid`, `fcntl`), vanilla JS/CSS, no build step, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-25-manager-tier-design.md`

## Global Constraints

- **Python 3 stdlib ONLY.** No new dependency, no build step. This is the plugin's existing hard rule.
- **Tests are plain `assert` functions**, run as `python3 bin/<file>.py`, collected by the existing footer idiom (`tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]`). **Never pytest.**
- **Every subprocess-invoking piece is split** so the decision logic is unit-testable without spawning anything. The untestable part must be a thin, obvious shell.
- **TDD, strictly:** every task writes the failing test first, runs it to confirm RED with the real error, then implements to GREEN. Never write the implementation first.
- `claude agents --json --all` — `--all` is **mandatory**; never pass `--cwd` (unreliable exact-path matching, already proven).
- The queue is **append-only**. Answers are recorded by appending an update record, never by rewriting history.
- **Tier 3 wins ties.** A record matching both a Tier 2 and a Tier 3 condition is Tier 3.
- Branch: continue on `feature/agent-status-dashboard` (PR #1 is already open against it; these commits extend that PR).
- Never put ADO/ticket ids in committed files.

## File Structure

| File | Responsibility |
|---|---|
| `bin/escalations.py` (new) | Queue record shape, append/read/answer, tier classifier. Pure logic + tiny file I/O. No subprocess. |
| `bin/manager.py` (new) | Prompt building, decision parsing/validation, argv builders for the Fable call and the resume-delivery. Pure functions + thin `subprocess` shells. |
| `bin/manager_daemon.py` (new) | The loop: read queue → route Tier 2 to manager → deliver answers. Thin orchestration over the two modules above. |
| `bin/dashboard.py` (modify) | New endpoints: `GET /api/escalations`, `POST /api/escalations/<id>/answer`. |
| `bin/dashboard.html` (modify) | Tier 3 blocking cards, Tier 2 audit feed, Approve control. |
| `bin/test_escalations.py` (new) | Tests for queue + classifier. |
| `bin/test_manager.py` (new) | Tests for prompt/parse/validate/argv. |

Splitting `escalations.py` from `manager.py` matters: the classifier is the highest-value test target and must not drag `subprocess` into its import graph.

---

### Task 1: Queue record + append-only store

**Files:**
- Create: `bin/escalations.py`
- Create: `bin/test_escalations.py`

**Interfaces:**
- Produces: `new_record(session_id, kind, question, options=None, evidence=None) -> dict` (fills `id`, `ts`, `status="open"`, `tier=None`, `answer=None`, `decided_by=None`, `answered_at=None`); `append(path, record) -> None`; `read_all(path) -> list[dict]`; `QUEUE_PATH` default constant. Tasks 2-6 consume these exact names.

- [ ] **Step 1: Write the failing test**

Create `bin/test_escalations.py`:

```python
#!/usr/bin/env python3
"""assert-based checks for the escalation queue and tier classifier. Run: python3 bin/test_escalations.py"""

import json
import os
import tempfile

from escalations import append, new_record, read_all


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return path


def test_new_record_has_required_fields():
    r = new_record("sess-1", "red_tests", "Tests fail, retry or reassign?")
    for key in ("id", "ts", "session_id", "kind", "question", "options",
                "evidence", "tier", "status", "decided_by", "answer", "answered_at"):
        assert key in r, f"missing {key}"
    assert r["session_id"] == "sess-1"
    assert r["kind"] == "red_tests"
    assert r["status"] == "open"
    assert r["tier"] is None
    assert r["answer"] is None
    assert r["options"] == []
    assert r["evidence"] == {}


def test_new_record_ids_are_unique():
    a = new_record("s", "k", "q")
    b = new_record("s", "k", "q")
    assert a["id"] != b["id"]


def test_append_then_read_all_roundtrips():
    path = _tmp()
    try:
        a = new_record("s1", "k1", "q1")
        b = new_record("s2", "k2", "q2")
        append(path, a)
        append(path, b)
        got = read_all(path)
        assert [r["id"] for r in got] == [a["id"], b["id"]]
        assert got[0]["question"] == "q1"
    finally:
        os.unlink(path)


def test_read_all_missing_file_is_empty():
    assert read_all("/nonexistent/path/queue.jsonl") == []


def test_read_all_skips_malformed_lines():
    path = _tmp()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(new_record("s", "k", "good")) + "\n")
            f.write("not json at all\n")
            f.write("\n")
        got = read_all(path)
        assert len(got) == 1
        assert got[0]["question"] == "good"
    finally:
        os.unlink(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_escalations.py
```
Expected: `ModuleNotFoundError: No module named 'escalations'`.

- [ ] **Step 3: Write minimal implementation**

Create `bin/escalations.py`:

```python
#!/usr/bin/env python3
"""Escalation queue: one append-only JSONL record per report a worker cannot decide alone."""

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
    """Append one record. Append-only: history is never rewritten, only added to."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


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
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_escalations.py
```
Expected: 5 `PASS` lines, then `5 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/escalations.py bin/test_escalations.py
git commit -m "feat: add append-only escalation queue"
```

---

### Task 2: Tier classifier

**Files:**
- Modify: `bin/escalations.py`
- Modify: `bin/test_escalations.py`

**Interfaces:**
- Consumes: `new_record` from Task 1.
- Produces: `classify(record) -> tuple[str, str]` returning `("tier2"|"tier3", reason)`; `SENSITIVE_PATH_MARKERS` constant. Tasks 3-6 consume `classify`.

Kinds the classifier recognises (the vocabulary workers emit): `red_tests`, `looping`, `pick_implementation`, `scope_question`, `diff_review`, `irreversible`, `push_or_pr`, `credentials`, `spec_ambiguity`, `no_convergence`, `worktree_collision`.

- [ ] **Step 1: Write the failing test**

Append to `bin/test_escalations.py`, before the `if __name__` block:

```python
from escalations import classify


def test_tier2_mechanical_kinds():
    for kind in ("red_tests", "looping", "pick_implementation", "scope_question"):
        tier, reason = classify(new_record("s", kind, "q"))
        assert tier == "tier2", f"{kind} should be tier2, got {tier} ({reason})"
        assert reason


def test_tier3_always_human_kinds():
    for kind in ("irreversible", "push_or_pr", "credentials",
                 "spec_ambiguity", "no_convergence", "worktree_collision"):
        tier, reason = classify(new_record("s", kind, "q"))
        assert tier == "tier3", f"{kind} should be tier3, got {tier} ({reason})"
        assert reason


def test_clean_diff_is_tier2():
    r = new_record("s", "diff_review", "merge?", evidence={
        "tests": "green", "deps_added": [], "changed_files": ["bin/dashboard.py"],
        "migration": False,
    })
    tier, reason = classify(r)
    assert tier == "tier2", reason


def test_diff_with_red_tests_is_tier3():
    r = new_record("s", "diff_review", "merge?", evidence={
        "tests": "red", "deps_added": [], "changed_files": ["bin/dashboard.py"], "migration": False,
    })
    assert classify(r)[0] == "tier3"


def test_diff_adding_dependency_is_tier3():
    r = new_record("s", "diff_review", "merge?", evidence={
        "tests": "green", "deps_added": ["requests"], "changed_files": ["bin/x.py"], "migration": False,
    })
    tier, reason = classify(r)
    assert tier == "tier3"
    assert "depend" in reason.lower()


def test_diff_with_migration_is_tier3():
    r = new_record("s", "diff_review", "merge?", evidence={
        "tests": "green", "deps_added": [], "changed_files": ["bin/x.py"], "migration": True,
    })
    assert classify(r)[0] == "tier3"


def test_diff_touching_sensitive_path_is_tier3():
    for path in ("services/gateway/auth/token.py", "config/.env", "app/secrets.yaml",
                 "migrations/0042_add.py"):
        r = new_record("s", "diff_review", "merge?", evidence={
            "tests": "green", "deps_added": [], "changed_files": ["bin/ok.py", path], "migration": False,
        })
        tier, reason = classify(r)
        assert tier == "tier3", f"{path} should force tier3 ({reason})"


def test_tier3_wins_ties():
    # A mechanical kind that nonetheless carries an irreversible flag stays tier3.
    r = new_record("s", "red_tests", "q", evidence={"irreversible": True})
    assert classify(r)[0] == "tier3"


def test_unknown_kind_defaults_to_tier3():
    tier, reason = classify(new_record("s", "something_new", "q"))
    assert tier == "tier3"
    assert "unknown" in reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_escalations.py
```
Expected: `ImportError: cannot import name 'classify' from 'escalations'`.

- [ ] **Step 3: Write minimal implementation**

Append to `bin/escalations.py`:

```python
# Path fragments that make a diff a human's call regardless of how clean it looks.
SENSITIVE_PATH_MARKERS = ("auth", "secret", ".env", "credential", "migration", "token", "password")

_TIER2_KINDS = {"red_tests", "looping", "pick_implementation", "scope_question"}
_TIER3_KINDS = {
    "irreversible", "push_or_pr", "credentials",
    "spec_ambiguity", "no_convergence", "worktree_collision",
}


def _sensitive(changed_files) -> str | None:
    for path in changed_files or []:
        low = path.lower()
        for marker in SENSITIVE_PATH_MARKERS:
            if marker in low:
                return path
    return None


def classify(record: dict) -> tuple[str, str]:
    """Route one record: ("tier2", reason) the manager may decide, or ("tier3", reason) for a human.

    Tier 3 wins ties — anything that smells irreversible or security-shaped goes to the human even
    when the rest of the record looks mechanical.
    """
    ev = record.get("evidence") or {}
    kind = record.get("kind")

    if ev.get("irreversible"):
        return "tier3", "evidence marks this irreversible"
    if kind in _TIER3_KINDS:
        return "tier3", f"{kind} always needs a human"

    if kind == "diff_review":
        if ev.get("tests") != "green":
            return "tier3", f"tests are {ev.get('tests') or 'unknown'}, not green"
        if ev.get("deps_added"):
            return "tier3", f"adds dependency: {', '.join(ev['deps_added'])}"
        if ev.get("migration"):
            return "tier3", "changes a migration or schema"
        hit = _sensitive(ev.get("changed_files"))
        if hit:
            return "tier3", f"touches a sensitive path: {hit}"
        return "tier2", "tests green, no new deps, no migration, no sensitive path"

    if kind in _TIER2_KINDS:
        return "tier2", f"{kind} is a mechanical call"

    return "tier3", f"unknown kind {kind!r} — defaulting to a human"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_escalations.py
```
Expected: 14 `PASS` lines, then `14 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/escalations.py bin/test_escalations.py
git commit -m "feat: classify escalations into manager-decidable and human-only tiers"
```

---

### Task 3: Recording an answer (append-only update)

**Files:**
- Modify: `bin/escalations.py`
- Modify: `bin/test_escalations.py`

**Interfaces:**
- Consumes: `append`, `read_all`, `new_record` from Task 1.
- Produces: `record_answer(path, record_id, answer, decided_by) -> dict | None`; `current_state(path) -> list[dict]` (folds the append-only log into latest-state-per-id). Tasks 4-6 consume both.

- [ ] **Step 1: Write the failing test**

Append to `bin/test_escalations.py`, before the `if __name__` block:

```python
from escalations import current_state, record_answer


def test_record_answer_appends_and_marks_answered():
    path = _tmp()
    try:
        r = new_record("s1", "red_tests", "retry?")
        append(path, r)
        updated = record_answer(path, r["id"], "retry once", "manager")
        assert updated is not None
        assert updated["status"] == "answered"
        assert updated["answer"] == "retry once"
        assert updated["decided_by"] == "manager"
        assert updated["answered_at"] is not None
        # append-only: the original line is still there, the update is a second line
        assert len(read_all(path)) == 2
    finally:
        os.unlink(path)


def test_record_answer_unknown_id_returns_none():
    path = _tmp()
    try:
        append(path, new_record("s", "k", "q"))
        assert record_answer(path, "nope", "x", "manager") is None
        assert len(read_all(path)) == 1
    finally:
        os.unlink(path)


def test_current_state_folds_to_latest_per_id():
    path = _tmp()
    try:
        a = new_record("s1", "red_tests", "q1")
        b = new_record("s2", "diff_review", "q2")
        append(path, a)
        append(path, b)
        record_answer(path, a["id"], "go", "human")
        state = current_state(path)
        assert len(state) == 2
        by_id = {r["id"]: r for r in state}
        assert by_id[a["id"]]["status"] == "answered"
        assert by_id[a["id"]]["answer"] == "go"
        assert by_id[b["id"]]["status"] == "open"
    finally:
        os.unlink(path)


def test_current_state_preserves_first_seen_order():
    path = _tmp()
    try:
        ids = []
        for i in range(3):
            r = new_record(f"s{i}", "red_tests", f"q{i}")
            ids.append(r["id"])
            append(path, r)
        record_answer(path, ids[0], "done", "manager")
        assert [r["id"] for r in current_state(path)] == ids
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_escalations.py
```
Expected: `ImportError: cannot import name 'current_state' from 'escalations'`.

- [ ] **Step 3: Write minimal implementation**

Append to `bin/escalations.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_escalations.py
```
Expected: 18 `PASS` lines, then `18 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/escalations.py bin/test_escalations.py
git commit -m "feat: record escalation answers as append-only updates"
```

---

### Task 4: Manager prompt, decision parsing, and argv builders

**Files:**
- Create: `bin/manager.py`
- Create: `bin/test_manager.py`

**Interfaces:**
- Consumes: record shape from Task 1.
- Produces: `build_prompt(record) -> str`; `parse_decision(text) -> dict | None`; `validate_decision(decision, record) -> str | None` (returns an error string, or `None` when valid); `manager_argv(prompt) -> list[str]`; `resume_argv(session_id, message) -> list[str]`; `MANAGER_MODEL` constant. Task 5 consumes all of these.

The manager is asked to answer with a single JSON object: `{"answer": str, "reason": str, "confidence": "high"|"low"}`. `confidence == "low"` means it declines — Task 5 escalates those to Tier 3.

- [ ] **Step 1: Write the failing test**

Create `bin/test_manager.py`:

```python
#!/usr/bin/env python3
"""assert-based checks for manager prompt building, decision parsing, and argv. Run: python3 bin/test_manager.py"""

from escalations import new_record
from manager import (
    MANAGER_MODEL,
    build_prompt,
    manager_argv,
    parse_decision,
    resume_argv,
    validate_decision,
)


def test_prompt_contains_question_and_options():
    r = new_record("s1", "red_tests", "Retry or reassign?", options=["retry", "reassign"])
    p = build_prompt(r)
    assert "Retry or reassign?" in p
    assert "retry" in p and "reassign" in p
    assert "JSON" in p


def test_prompt_includes_evidence():
    r = new_record("s1", "diff_review", "merge?", evidence={"tests": "green", "branch": "feature/x"})
    p = build_prompt(r)
    assert "green" in p
    assert "feature/x" in p


def test_parse_decision_plain_json():
    got = parse_decision('{"answer": "retry", "reason": "flaky", "confidence": "high"}')
    assert got == {"answer": "retry", "reason": "flaky", "confidence": "high"}


def test_parse_decision_json_in_fenced_block():
    text = 'Here is my call:\n```json\n{"answer": "retry", "reason": "r", "confidence": "high"}\n```\nthanks'
    got = parse_decision(text)
    assert got["answer"] == "retry"


def test_parse_decision_json_embedded_in_prose():
    text = 'I think {"answer": "reassign", "reason": "stuck", "confidence": "low"} is right.'
    got = parse_decision(text)
    assert got["answer"] == "reassign"
    assert got["confidence"] == "low"


def test_parse_decision_garbage_returns_none():
    assert parse_decision("I cannot help with that.") is None
    assert parse_decision("") is None
    assert parse_decision("{not json}") is None


def test_validate_decision_accepts_well_formed():
    r = new_record("s", "red_tests", "q")
    assert validate_decision({"answer": "go", "reason": "why", "confidence": "high"}, r) is None


def test_validate_decision_rejects_missing_fields():
    r = new_record("s", "red_tests", "q")
    assert validate_decision({"answer": "go"}, r) is not None
    assert validate_decision({"reason": "x", "confidence": "high"}, r) is not None


def test_validate_decision_rejects_bad_confidence():
    r = new_record("s", "red_tests", "q")
    err = validate_decision({"answer": "a", "reason": "b", "confidence": "maybe"}, r)
    assert err is not None
    assert "confidence" in err


def test_validate_decision_rejects_answer_outside_options():
    r = new_record("s", "pick_implementation", "which?", options=["A", "B"])
    err = validate_decision({"answer": "C", "reason": "r", "confidence": "high"}, r)
    assert err is not None
    assert "option" in err.lower()
    assert validate_decision({"answer": "A", "reason": "r", "confidence": "high"}, r) is None


def test_validate_decision_rejects_non_dict():
    r = new_record("s", "red_tests", "q")
    assert validate_decision(None, r) is not None
    assert validate_decision(["a"], r) is not None


def test_manager_argv_uses_fable_and_print_mode():
    argv = manager_argv("hello")
    assert argv[0] == "claude"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == MANAGER_MODEL
    assert "-p" in argv
    assert argv[-1] == "hello"


def test_resume_argv_targets_the_session():
    argv = resume_argv("sess-abc", "the answer")
    assert argv[0] == "claude"
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-abc"
    assert "-p" in argv
    assert argv[-1] == "the answer"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_manager.py
```
Expected: `ModuleNotFoundError: No module named 'manager'`.

- [ ] **Step 3: Write minimal implementation**

Create `bin/manager.py`:

```python
#!/usr/bin/env python3
"""The manager tier: turn one escalation into a decision, and deliver it back to the worker.

Everything here except run_manager()/deliver_answer() is pure, so the judgement logic is testable
without spawning a model.
"""

import json
import re
import subprocess

MANAGER_MODEL = "claude-fable-5"

_INSTRUCTIONS = """You are the manager for a team of autonomous coding sessions.

One worker cannot decide something alone and has escalated it to you. Decide it. You are the last
step before a human gets interrupted, so decide when the evidence supports a decision — but say so
honestly when it does not.

Reply with ONE JSON object and nothing else:
{"answer": "<what the worker should do, imperative and specific>",
 "reason": "<why, in one sentence, grounded in the evidence>",
 "confidence": "high" | "low"}

Use "low" when the evidence genuinely does not settle it — a human will then be asked instead.
Never invent evidence you were not given."""


def build_prompt(record: dict) -> str:
    """Assemble the manager's prompt: instructions, the question, its options, and the evidence."""
    parts = [_INSTRUCTIONS, "", f"Kind: {record.get('kind')}", f"Question: {record.get('question')}"]
    options = record.get("options") or []
    if options:
        parts.append("Options (your answer must be exactly one of these):")
        parts.extend(f"  - {o}" for o in options)
    evidence = record.get("evidence") or {}
    if evidence:
        parts.append("Evidence:")
        for key, value in evidence.items():
            parts.append(f"  {key}: {json.dumps(value) if not isinstance(value, str) else value}")
    return "\n".join(parts)


def parse_decision(text: str) -> dict | None:
    """Pull the decision object out of the model's reply — bare, fenced, or embedded in prose."""
    if not text:
        return None
    for candidate in re.findall(r"\{.*?\}", text, re.S):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "answer" in obj:
            return obj
    return None


def validate_decision(decision, record: dict) -> str | None:
    """Return an error string if this decision is not usable, else None."""
    if not isinstance(decision, dict):
        return "decision is not a JSON object"
    for field in ("answer", "reason", "confidence"):
        if not decision.get(field):
            return f"missing {field}"
    if decision["confidence"] not in ("high", "low"):
        return f"confidence must be high or low, got {decision['confidence']!r}"
    options = record.get("options") or []
    if options and decision["answer"] not in options:
        return f"answer {decision['answer']!r} is not one of the offered options"
    return None


def manager_argv(prompt: str) -> list[str]:
    """Argv for one short-lived manager call. Print mode: one question in, one answer out."""
    return ["claude", "--model", MANAGER_MODEL, "-p", prompt]


def resume_argv(session_id: str, message: str) -> list[str]:
    """Argv that delivers a message into an existing session (verified: works, exits 0)."""
    return ["claude", "--resume", session_id, "-p", message]


def run_manager(record: dict, timeout: int = 180) -> str:
    """Thin shell: spawn the manager and hand back its raw reply."""
    result = subprocess.run(
        manager_argv(build_prompt(record)),
        capture_output=True, text=True, check=True, timeout=timeout,
    )
    return result.stdout


def deliver_answer(session_id: str, message: str, timeout: int = 180) -> str:
    """Thin shell: push an answer into a blocked worker session."""
    result = subprocess.run(
        resume_argv(session_id, message),
        capture_output=True, text=True, check=True, timeout=timeout,
    )
    return result.stdout
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_manager.py
```
Expected: 13 `PASS` lines, then `13 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/manager.py bin/test_manager.py
git commit -m "feat: add manager prompt building, decision parsing, and CLI argv builders"
```

---

### Task 5: Routing one record end to end

**Files:**
- Modify: `bin/manager.py`
- Modify: `bin/test_manager.py`

**Interfaces:**
- Consumes: `classify` (Task 2), `record_answer` (Task 3), `build_prompt`/`parse_decision`/`validate_decision` (Task 4).
- Produces: `decide(record, ask_model) -> dict` returning `{"outcome": "answered"|"needs_human", "answer": str|None, "reason": str, "decided_by": str}`. `ask_model` is injected so the router is testable without spawning anything. Task 6 consumes `decide`.

Routing rules: Tier 3 → `needs_human` immediately, never call the model. Tier 2 → call the model; a decision that fails to parse, fails validation, or comes back `confidence == "low"` degrades to `needs_human` rather than guessing.

- [ ] **Step 1: Write the failing test**

Append to `bin/test_manager.py`, before the `if __name__` block:

```python
from manager import decide


def _ok(payload):
    return lambda record: payload


def test_tier3_record_never_calls_the_model():
    called = []

    def spy(record):
        called.append(record)
        return '{"answer": "x", "reason": "y", "confidence": "high"}'

    r = new_record("s", "push_or_pr", "push to main?")
    out = decide(r, spy)
    assert out["outcome"] == "needs_human"
    assert called == [], "tier3 must not spend a model call"
    assert "human" in out["reason"].lower() or "push_or_pr" in out["reason"]


def test_tier2_high_confidence_is_answered():
    r = new_record("s", "red_tests", "retry?")
    out = decide(r, _ok('{"answer": "retry once", "reason": "looks flaky", "confidence": "high"}'))
    assert out["outcome"] == "answered"
    assert out["answer"] == "retry once"
    assert out["decided_by"] == "manager"


def test_tier2_low_confidence_falls_back_to_human():
    r = new_record("s", "red_tests", "retry?")
    out = decide(r, _ok('{"answer": "maybe", "reason": "unclear", "confidence": "low"}'))
    assert out["outcome"] == "needs_human"
    assert "confidence" in out["reason"].lower()


def test_tier2_unparseable_reply_falls_back_to_human():
    r = new_record("s", "red_tests", "retry?")
    out = decide(r, _ok("I'm not sure what you mean."))
    assert out["outcome"] == "needs_human"
    assert "pars" in out["reason"].lower()


def test_tier2_invalid_decision_falls_back_to_human():
    r = new_record("s", "pick_implementation", "which?", options=["A", "B"])
    out = decide(r, _ok('{"answer": "C", "reason": "r", "confidence": "high"}'))
    assert out["outcome"] == "needs_human"
    assert "option" in out["reason"].lower()


def test_model_failure_falls_back_to_human():
    def boom(record):
        raise RuntimeError("model unavailable")

    r = new_record("s", "red_tests", "retry?")
    out = decide(r, boom)
    assert out["outcome"] == "needs_human"
    assert "model unavailable" in out["reason"]


def test_clean_diff_gets_approved_by_manager():
    r = new_record("s", "diff_review", "merge?", evidence={
        "tests": "green", "deps_added": [], "changed_files": ["bin/dashboard.py"], "migration": False,
    })
    out = decide(r, _ok('{"answer": "approve", "reason": "clean", "confidence": "high"}'))
    assert out["outcome"] == "answered"
    assert out["answer"] == "approve"


def test_diff_touching_auth_reaches_human_without_model_call():
    called = []
    r = new_record("s", "diff_review", "merge?", evidence={
        "tests": "green", "deps_added": [], "migration": False,
        "changed_files": ["services/gateway/auth/token.py"],
    })
    out = decide(r, lambda rec: called.append(rec) or '{"answer":"a","reason":"b","confidence":"high"}')
    assert out["outcome"] == "needs_human"
    assert called == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_manager.py
```
Expected: `ImportError: cannot import name 'decide' from 'manager'`.

- [ ] **Step 3: Write minimal implementation**

Add to the imports at the top of `bin/manager.py`:

```python
from escalations import classify
```

Then append to `bin/manager.py`:

```python
def decide(record: dict, ask_model) -> dict:
    """Route one record. `ask_model(record) -> str` is injected so this stays testable.

    Anything the manager cannot settle cleanly degrades to needs_human — the failure mode of this
    function is "ask the person", never "guess".
    """
    tier, reason = classify(record)
    if tier == "tier3":
        return {"outcome": "needs_human", "answer": None, "reason": reason, "decided_by": None}

    try:
        raw = ask_model(record)
    except Exception as e:  # a dead model must not wedge the queue
        return {"outcome": "needs_human", "answer": None,
                "reason": f"manager call failed: {e}", "decided_by": None}

    decision = parse_decision(raw)
    if decision is None:
        return {"outcome": "needs_human", "answer": None,
                "reason": "could not parse a decision from the manager's reply", "decided_by": None}

    invalid = validate_decision(decision, record)
    if invalid:
        return {"outcome": "needs_human", "answer": None,
                "reason": f"manager returned an unusable decision: {invalid}", "decided_by": None}

    if decision["confidence"] != "high":
        return {"outcome": "needs_human", "answer": None,
                "reason": f"manager declined with low confidence: {decision['reason']}",
                "decided_by": None}

    return {"outcome": "answered", "answer": decision["answer"],
            "reason": decision["reason"], "decided_by": "manager"}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_manager.py
```
Expected: 21 `PASS` lines, then `21 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/manager.py bin/test_manager.py
git commit -m "feat: route escalations to the manager, degrading to human on any doubt"
```

---

### Task 6: Daemon loop

**Files:**
- Create: `bin/manager_daemon.py`

**Interfaces:**
- Consumes: `current_state`, `record_answer`, `QUEUE_PATH` (Tasks 1/3); `decide`, `run_manager`, `deliver_answer` (Tasks 4/5).
- Produces: `process_open(path, ask_model, deliver) -> list[dict]` (one pass over the queue, injectable for testing) and a `main()` that loops. Task 7 does not consume this; the dashboard reads the queue file directly.

- [ ] **Step 1: Write the failing test**

Append to `bin/test_manager.py`, before the `if __name__` block:

```python
import os as _os
import tempfile as _tempfile

from escalations import append as _append
from escalations import current_state as _current_state
from manager_daemon import process_open


def _queue():
    fd, path = _tempfile.mkstemp(suffix=".jsonl")
    _os.close(fd)
    return path


def test_process_open_answers_tier2_and_delivers():
    path = _queue()
    delivered = []
    try:
        r = new_record("sess-9", "red_tests", "retry?")
        _append(path, r)
        acted = process_open(
            path,
            ask_model=lambda rec: '{"answer": "retry once", "reason": "flaky", "confidence": "high"}',
            deliver=lambda sid, msg: delivered.append((sid, msg)),
        )
        assert len(acted) == 1
        assert acted[0]["outcome"] == "answered"
        state = {x["id"]: x for x in _current_state(path)}
        assert state[r["id"]]["status"] == "answered"
        assert state[r["id"]]["decided_by"] == "manager"
        assert delivered == [("sess-9", "retry once")]
    finally:
        _os.unlink(path)


def test_process_open_leaves_tier3_for_the_human():
    path = _queue()
    delivered = []
    try:
        r = new_record("sess-9", "push_or_pr", "push?")
        _append(path, r)
        acted = process_open(path, ask_model=lambda rec: "", deliver=lambda s, m: delivered.append(1))
        assert len(acted) == 1
        assert acted[0]["outcome"] == "needs_human"
        state = {x["id"]: x for x in _current_state(path)}
        assert state[r["id"]]["status"] == "needs_human"
        assert delivered == [], "nothing is delivered until a human answers"
    finally:
        _os.unlink(path)


def test_process_open_skips_already_handled_records():
    path = _queue()
    try:
        r = new_record("sess-9", "red_tests", "retry?")
        _append(path, r)
        process_open(path, lambda rec: '{"answer": "a", "reason": "b", "confidence": "high"}',
                     lambda s, m: None)
        again = process_open(path, lambda rec: '{"answer": "a", "reason": "b", "confidence": "high"}',
                             lambda s, m: None)
        assert again == [], "an answered record must not be reprocessed"
    finally:
        _os.unlink(path)


def test_process_open_delivers_human_answers_once():
    path = _queue()
    delivered = []
    try:
        r = new_record("sess-9", "push_or_pr", "push?")
        _append(path, r)
        process_open(path, lambda rec: "", lambda s, m: delivered.append((s, m)))
        # a human answers through the dashboard
        from escalations import record_answer as _record_answer
        _record_answer(path, r["id"], "yes, push it", "human")
        process_open(path, lambda rec: "", lambda s, m: delivered.append((s, m)))
        assert delivered == [("sess-9", "yes, push it")]
        # and not again on the next pass
        process_open(path, lambda rec: "", lambda s, m: delivered.append((s, m)))
        assert len(delivered) == 1
    finally:
        _os.unlink(path)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_manager.py
```
Expected: `ModuleNotFoundError: No module named 'manager_daemon'`.

- [ ] **Step 3: Write minimal implementation**

Create `bin/manager_daemon.py`:

```python
#!/usr/bin/env python3
"""Watch the escalation queue: let the manager settle what it can, hand the rest to the human.

Run: manager_daemon.py [queue-path]
"""

import sys
import time

from escalations import QUEUE_PATH, append, current_state, record_answer
from manager import decide, deliver_answer, run_manager


def process_open(path: str, ask_model, deliver) -> list[dict]:
    """One pass. Returns what this pass acted on, so a caller can log or test it."""
    acted = []
    for rec in current_state(path):
        status = rec.get("status")

        if status == "open":
            outcome = decide(rec, ask_model)
            if outcome["outcome"] == "answered":
                record_answer(path, rec["id"], outcome["answer"], "manager")
                deliver(rec["session_id"], outcome["answer"])
            else:
                pending = dict(rec)
                pending["status"] = "needs_human"
                pending["tier"] = "tier3"
                pending["reason"] = outcome["reason"]
                append(path, pending)
            acted.append(outcome)

        elif status == "answered" and not rec.get("delivered"):
            # A human answered through the dashboard; carry it back to the worker exactly once.
            if rec.get("decided_by") == "human":
                deliver(rec["session_id"], rec["answer"])
            done = dict(rec)
            done["delivered"] = True
            append(path, done)

    return acted


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else QUEUE_PATH
    print(f"manager daemon watching {path}")
    while True:
        try:
            for outcome in process_open(path, run_manager, deliver_answer):
                print(f"  {outcome['outcome']}: {outcome['reason']}")
        except Exception as e:  # a bad pass must not kill the daemon
            print(f"  pass failed: {e}", file=sys.stderr)
        time.sleep(5)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_manager.py
```
Expected: 25 `PASS` lines, then `25 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/manager_daemon.py bin/test_manager.py
git commit -m "feat: add the manager daemon loop over the escalation queue"
```

---

### Task 7: Dashboard endpoints

**Files:**
- Modify: `bin/dashboard.py`

**Interfaces:**
- Consumes: `current_state`, `record_answer`, `QUEUE_PATH` (Tasks 1/3).
- Produces: `GET /api/escalations` → `{"needs_human": [...], "recent_decisions": [...]}`; `POST /api/escalations/<id>/answer` with body `{"answer": "..."}` → `{"ok": true, "record": {...}}` or 404. Task 8 consumes both.

- [ ] **Step 1: Write the failing check (manual, matching this repo's convention for HTTP routes)**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -c "api/escalations" bin/dashboard.py
```
Expected: `0` — the routes do not exist yet. (The HTTP layer here has no unit-test harness in this repo; its check is the live smoke test in Step 4, matching how `/api/tasks` was verified.)

- [ ] **Step 2: Add the imports and the escalation reader**

At the top of `bin/dashboard.py`, alongside the existing imports, add:

```python
from escalations import QUEUE_PATH, current_state, record_answer
```

Then add this function next to `get_tasks`:

```python
def get_escalations() -> dict:
    """What the human still has to answer, and what the manager already decided for them."""
    state = current_state(QUEUE_PATH)
    needs_human = [r for r in state if r.get("status") == "needs_human"]
    decisions = [r for r in state if r.get("decided_by") == "manager"]
    decisions.sort(key=lambda r: r.get("answered_at") or 0, reverse=True)
    return {"needs_human": needs_human, "recent_decisions": decisions[:20]}
```

- [ ] **Step 3: Add the routes**

In `Handler.do_GET`, before the final 404, add:

```python
        if parsed.path == "/api/escalations":
            try:
                self._json(get_escalations())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
```

Add a `do_POST` method to `Handler`:

```python
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/escalations/") and parsed.path.endswith("/answer"):
            rid = parsed.path[len("/api/escalations/") : -len("/answer")]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                answer = (body.get("answer") or "").strip()
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": f"bad request body: {e}"}, status=400)
                return
            if not answer:
                self._json({"error": "answer is required"}, status=400)
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
        self.send_response(404)
        self.end_headers()
```

- [ ] **Step 4: Verify with a live smoke test**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 - <<'EOF'
import os, sys
sys.path.insert(0, "bin")
from escalations import QUEUE_PATH, append, new_record
r = new_record("smoke-session", "push_or_pr", "Push the branch?", options=["yes", "no"])
r["status"] = "needs_human"
r["tier"] = "tier3"
append(QUEUE_PATH, r)
print("seeded", r["id"])
EOF
python3 bin/dashboard.py 4477 . &
sleep 2
curl -s http://127.0.0.1:4477/api/escalations | head -c 400; echo
ID=$(curl -s http://127.0.0.1:4477/api/escalations | python3 -c 'import json,sys; print(json.load(sys.stdin)["needs_human"][0]["id"])')
curl -s -X POST -H 'Content-Type: application/json' -d '{"answer":"yes"}' \
  http://127.0.0.1:4477/api/escalations/$ID/answer | head -c 300; echo
curl -s -o /dev/null -w "unknown-id: %{http_code}\n" -X POST -H 'Content-Type: application/json' \
  -d '{"answer":"x"}' http://127.0.0.1:4477/api/escalations/nope/answer
kill %1
```
Expected: the GET shows the seeded record under `needs_human`; the POST returns `{"ok": true, ...}` with `decided_by: "human"`; the unknown id returns `404`.

- [ ] **Step 5: Confirm nothing regressed and commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin && python3 test_dashboard.py && python3 test_escalations.py && python3 test_manager.py
cd ~/projects/claude-parallel-worktree-plugin
git add bin/dashboard.py
git commit -m "feat: expose escalations and answering over the dashboard API"
```
Expected: `8 passed`, `18 passed`, `25 passed`.

---

### Task 8: Dashboard UI — decision cards and audit feed

**Files:**
- Modify: `bin/dashboard.html`

**Interfaces:**
- Consumes: `GET /api/escalations`, `POST /api/escalations/<id>/answer` (Task 7).
- Produces: nothing downstream — this is the final layer.

Match the existing visual language in this file exactly: `.panel` / `.panel-head` cards, `.pill` status chips, `.btn` buttons, the `--navy` / `--ok-*` / `--warn-*` / `--stop-*` tokens, and `el()` + `textContent` for all dynamic values (never `innerHTML` with server data).

- [ ] **Step 1: Write the failing check**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -c "api/escalations" bin/dashboard.html
```
Expected: `0`.

- [ ] **Step 2: Add the two panels to the markup**

In `bin/dashboard.html`, immediately after the `<h2 class="page-title">` line, insert:

```html
    <section class="panel" id="decisions-panel" style="margin-bottom:20px; display:none;">
      <div class="panel-head">
        <h2>Decision required</h2>
        <p>The manager stopped here because this one is yours to make.</p>
      </div>
      <div id="decisions"></div>
    </section>
```

And immediately before the closing `</main>`, insert:

```html
    <section class="panel" style="margin-top:16px;">
      <div class="panel-head">
        <h2>Decided for you</h2>
        <p>What the manager settled without interrupting you.</p>
      </div>
      <div id="audit"></div>
    </section>
```

- [ ] **Step 3: Add the styles**

Inside the existing `<style>` block, before the closing `</style>`, add:

```css
  .esc { padding: 18px 22px; border-top: 1px solid var(--line); }
  .esc:first-child { border-top: 0; }
  .esc-q { font-size: 14.5px; font-weight: 650; color: var(--navy); }
  .esc-why { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
  .esc-ev { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .esc-files { margin-top: 12px; border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  .esc-file { display: flex; align-items: center; gap: 10px; padding: 9px 13px; border-top: 1px solid var(--line);
              font-family: var(--mono); font-size: 11.5px; color: var(--ink); }
  .esc-file:first-child { border-top: 0; }
  .esc-file .add { color: var(--ok-fg); margin-left: auto; }
  .esc-file .del { color: var(--stop-fg); }
  .esc-actions { display: flex; gap: 9px; margin-top: 14px; flex-wrap: wrap; }
  .btn.primary { background: var(--navy); border-color: var(--navy); color: #fff; font-weight: 600; }
  .btn.primary:hover { background: #12305c; border-color: #12305c; }
  .audit-row { display: flex; align-items: flex-start; gap: 12px; padding: 12px 22px; border-top: 1px solid var(--line); }
  .audit-row:first-child { border-top: 0; }
  .audit-body { flex: 1; min-width: 0; }
  .audit-q { font-size: 13px; color: var(--ink); }
  .audit-a { font-size: 12.5px; color: var(--muted); margin-top: 3px; }
  .audit-a b { color: var(--ok-fg); font-weight: 650; }
  .audit-age { font-family: var(--mono); font-size: 11px; color: var(--faint); flex-shrink: 0; }
```

- [ ] **Step 4: Add the rendering and polling**

Inside the `<script>` block, before the final `pollTasks();` line, add:

```javascript
async function answerEscalation(id, answer) {
  try {
    const res = await fetch(`/api/escalations/${encodeURIComponent(id)}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    pollEscalations();
  } catch (e) {
    alert("Could not send that answer: " + (e.message || e));
  }
}

function renderEscalation(r) {
  const box = el("div", "esc");
  box.append(el("div", "esc-q", r.question || "(no question)"));
  if (r.reason) box.append(el("div", "esc-why", r.reason));

  const ev = r.evidence || {};
  const chips = el("div", "esc-ev");
  if (ev.tests) chips.append(pill(ev.tests === "green" ? "done" : "blocked", "tests " + ev.tests));
  if (r.session_id) chips.append(pill("surface", r.session_id));
  if (ev.branch) chips.append(pill("surface", ev.branch));
  if (ev.diff_stat) chips.append(pill("surface", ev.diff_stat));
  (ev.deps_added || []).forEach(d => chips.append(pill("blocked", "new dep: " + d)));
  if (chips.childNodes.length) box.append(chips);

  const files = ev.changed_files || [];
  if (files.length) {
    const list = el("div", "esc-files");
    files.slice(0, 12).forEach(f => {
      const row = el("div", "esc-file");
      row.append(el("span", null, typeof f === "string" ? f : f.path || String(f)));
      if (f && f.additions != null) row.append(el("span", "add", "+" + f.additions));
      if (f && f.deletions != null) row.append(el("span", "del", "−" + f.deletions));
      list.append(row);
    });
    if (files.length > 12) list.append(el("div", "esc-file", `… ${files.length - 12} more`));
    box.append(list);
  }

  const actions = el("div", "esc-actions");
  const options = (r.options && r.options.length) ? r.options : ["Approve", "Reject"];
  options.forEach((opt, i) => {
    const b = el("button", "btn" + (i === 0 ? " primary" : ""), opt);
    b.onclick = () => answerEscalation(r.id, opt);
    actions.append(b);
  });
  box.append(actions);
  return box;
}

function renderAudit(rows) {
  const box = document.getElementById("audit");
  if (!rows.length) {
    box.replaceChildren(el("div", "empty", "The manager hasn't had to decide anything yet."));
    return;
  }
  const frag = document.createDocumentFragment();
  rows.forEach(r => {
    const row = el("div", "audit-row");
    row.append(el("span", "dot"));
    const body = el("div", "audit-body");
    body.append(el("div", "audit-q", r.question || ""));
    const a = el("div", "audit-a");
    a.append(el("b", null, r.answer || ""));
    a.append(document.createTextNode(" — " + (r.reason || "")));
    body.append(a);
    row.append(body, el("span", "audit-age", fmtAge((r.answered_at || 0) * 1000)));
    frag.append(row);
  });
  box.replaceChildren(frag);
}

async function pollEscalations() {
  let data;
  try {
    const res = await fetch("/api/escalations");
    data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  } catch (e) {
    return;  // the sessions view still works if this endpoint is down
  }
  const panel = document.getElementById("decisions-panel");
  const list = document.getElementById("decisions");
  const pending = data.needs_human || [];
  panel.style.display = pending.length ? "" : "none";
  if (pending.length) list.replaceChildren(...pending.map(renderEscalation));
  renderAudit(data.recent_decisions || []);
}

pollEscalations();
setInterval(pollEscalations, 2000);
```

- [ ] **Step 5: Verify live and commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 - <<'EOF'
import sys
sys.path.insert(0, "bin")
from escalations import QUEUE_PATH, append, new_record
r = new_record("ui-smoke", "diff_review", "Merge the auth adapter change?", options=["Approve", "Reject"])
r["status"] = "needs_human"; r["tier"] = "tier3"
r["reason"] = "touches a sensitive path: services/gateway/auth/token.py"
r["evidence"] = {"tests": "green", "branch": "feature/auth-adapter", "diff_stat": "3 files, +18 −6",
                 "changed_files": [{"path": "services/gateway/auth/token.py", "additions": 18, "deletions": 6}]}
append(QUEUE_PATH, r)
print("seeded", r["id"])
EOF
python3 bin/dashboard.py 4478 . &
sleep 2
curl -s http://127.0.0.1:4478/ | grep -c "api/escalations"
curl -s http://127.0.0.1:4478/api/escalations | python3 -c 'import json,sys; d=json.load(sys.stdin); print("needs_human:", len(d["needs_human"]))'
kill %1
cd bin && python3 test_dashboard.py && python3 test_escalations.py && python3 test_manager.py
```
Expected: grep count ≥ 1, `needs_human: 1`, and `8 passed` / `18 passed` / `25 passed`.

Then open `http://127.0.0.1:4478/` in a browser (or via `ssh -N -L 4478:127.0.0.1:4478 …`) and confirm the "Decision required" panel shows the question, the green `tests green` pill, the changed file with `+18 −6`, and the Approve/Reject buttons; clicking Approve makes the panel disappear and the item appear under "Decided for you".

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/dashboard.html
git commit -m "docs: surface decisions and the manager audit feed in the dashboard"
```

---

### Task 9: Wire the Hermes skill to the queue, and document it

**Files:**
- Modify: `skills/parallel-worktree-run/SKILL.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing downstream.

- [ ] **Step 1: Add a Step 6 to the skill**

In `skills/parallel-worktree-run/SKILL.md`, after the existing Step 5 section and before `## Troubleshooting`, add:

```markdown
## Step 6 — escalate instead of stalling

When a dispatched session hits something it cannot decide alone, it appends one record to the
escalation queue rather than stopping and waiting for you:

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "<plugin bin dir>")
from escalations import QUEUE_PATH, append, new_record
append(QUEUE_PATH, new_record(
    session_id="<this session's id>",
    kind="diff_review",              # or red_tests / looping / pick_implementation / scope_question
    question="Merge the adapter change?",
    options=["Approve", "Reject"],
    evidence={"tests": "green", "branch": "feature/x", "deps_added": [],
              "migration": False, "changed_files": ["bin/dashboard.py"]},
))
PY
```

`manager_daemon.py` picks it up within seconds. Mechanical calls are settled by the manager and
delivered straight back into the worker; anything irreversible, security-shaped, or genuinely
ambiguous waits for you in the dashboard's "Decision required" panel. Every manager decision is
listed under "Decided for you" so nothing is decided on your behalf invisibly.

Start the daemon alongside the dashboard:

```bash
manager_daemon.py       # watches ~/.claude/hermes/escalations.jsonl
```
```

- [ ] **Step 2: Add the manager tier to the README**

In `README.md`, in the `## What's in here` list right after the `bin/dashboard.py` bullet, add:

```markdown
- `bin/escalations.py` / `bin/manager.py` / `bin/manager_daemon.py` — the manager tier: workers
  append what they cannot decide to a queue, a short-lived Fable manager settles the mechanical
  ones and answers the worker directly, and only irreversible or genuinely ambiguous calls reach
  you in the dashboard. Every automatic decision is logged where you can audit it.
```

- [ ] **Step 3: Verify and commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -c "escalation" skills/parallel-worktree-run/SKILL.md README.md
cd bin && python3 test_dashboard.py && python3 test_escalations.py && python3 test_manager.py
```
Expected: both files match at least once; `8 passed` / `18 passed` / `25 passed`.

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add skills/parallel-worktree-run/SKILL.md README.md
git commit -m "docs: document the manager tier and the escalation queue"
```

---

### Task 10: End-to-end run with a real manager call

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything above.
- Produces: evidence the loop closes with a real model, not a stub.

- [ ] **Step 1: Confirm the whole suite is green**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin
python3 test_dashboard.py && python3 test_escalations.py && python3 test_manager.py
```
Expected: `8 passed`, `18 passed`, `25 passed`.

- [ ] **Step 2: Drive one Tier 2 record through the real manager**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 - <<'EOF'
import sys, tempfile, os
sys.path.insert(0, "bin")
from escalations import append, new_record, current_state
from manager import run_manager, deliver_answer
from manager_daemon import process_open

fd, path = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
r = new_record("e2e-test", "pick_implementation",
               "Store the queue as JSONL or as one JSON array?",
               options=["JSONL", "JSON array"],
               evidence={"note": "concurrent appends from several processes"})
append(path, r)
delivered = []
acted = process_open(path, run_manager, lambda sid, msg: delivered.append((sid, msg)))
print("outcome:", acted[0]["outcome"])
print("answer :", acted[0].get("answer"))
print("reason :", acted[0]["reason"])
print("state  :", [x["status"] for x in current_state(path)])
print("delivered:", delivered)
os.unlink(path)
EOF
```
Expected: `outcome: answered`, an `answer` that is exactly `JSONL` or `JSON array`, a non-empty reason, and one delivery tuple. A real Fable call takes a few seconds. If it returns `needs_human` with `manager call failed`, the model or auth is unavailable — report that rather than editing the test to pass.

- [ ] **Step 3: Drive one Tier 3 record and confirm no model call happens**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 - <<'EOF'
import sys, tempfile, os
sys.path.insert(0, "bin")
from escalations import append, new_record, current_state
from manager_daemon import process_open

fd, path = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
append(path, new_record("e2e-test", "push_or_pr", "Push feature/x to origin?", options=["Yes", "No"]))
calls = []
acted = process_open(path, lambda rec: calls.append(rec) or "", lambda s, m: None)
print("outcome:", acted[0]["outcome"], "| model calls:", len(calls))
print("status :", [x["status"] for x in current_state(path)])
os.unlink(path)
EOF
```
Expected: `outcome: needs_human | model calls: 0` and a final status of `needs_human` — a push must never be auto-approved.

- [ ] **Step 4: Review the full branch diff**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git log --oneline master..HEAD
git diff --stat master..HEAD
```
Confirm the new files are exactly `bin/escalations.py`, `bin/manager.py`, `bin/manager_daemon.py`, `bin/test_escalations.py`, `bin/test_manager.py`, plus modifications to `bin/dashboard.py`, `bin/dashboard.html`, `skills/parallel-worktree-run/SKILL.md`, `README.md` — and that no `docs/` or `.superpowers/` file is staged.

- [ ] **Step 5: Push to the open PR**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git push origin feature/agent-status-dashboard
```
This updates PR #1 in place. Report the commit range that was pushed.
