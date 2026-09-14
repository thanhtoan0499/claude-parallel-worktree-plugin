#!/usr/bin/env python3
"""assert-based checks for board_mirror_answers — the return path that brings an escalation
answer written on the published board back down into the manager's append-only ledger.

Real temp ledger files, no subprocess and no artifact: the whole point of the module is what it
does and does NOT append, which only a real read-back can show.
"""

import io
import json
import os
import sys
import tempfile

from board_mirror_answers import accepted_answers, apply_answers, extract_payload, main

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from escalations import current_state, read_all  # noqa: E402


def _rec(rid, **over):
    """One ledger record in the shape escalations.new_record() writes, needs_human by default —
    the only state the board is ever allowed to answer."""
    rec = {
        "id": rid,
        "ts": 1.0,
        "session_id": "sess-1",
        "kind": "pick_implementation",
        "question": "A hay B?",
        "options": ["A", "B"],
        "evidence": {},
        "tier": "tier3",
        "status": "needs_human",
        "decided_by": None,
        "answer": None,
        "answered_at": None,
    }
    rec.update(over)
    return rec


def _ledger(tmp, lines):
    """A ledger file. A plain str is written verbatim, so a test can plant a malformed line."""
    path = os.path.join(tmp, "escalations.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
    return path


def _reply(items):
    """What `claude -p` replies once board-mirror.md's step 3 is answered."""
    return "REFRESH_OK: wrote 4 documents, last_ado_sweep=1788857703.7\nANSWERS: " + json.dumps(items)


# ---------- idempotency / never-clobber: the two properties the ledger depends on ----------


def test_an_answer_arriving_twice_appends_once():
    # The answer stays in the artifact db forever, so every later pump run reads it again.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1")])
        reply = _reply([{"id": "e1", "answer": "B"}])

        first = apply_answers(path, reply)
        second = apply_answers(path, reply)

        assert first == ["e1"]
        assert second == []
        answered = [r for r in read_all(path) if r.get("status") == "answered"]
        assert len(answered) == 1
        assert answered[0]["answer"] == "B"
        assert answered[0]["decided_by"] == "cto"


def test_an_answer_for_an_already_answered_escalation_does_not_clobber():
    # Answered on port 4400 (or by the manager) between the CTO's click and this pump run.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(
            tmp,
            [_rec("e1"), _rec("e1", status="answered", answer="A", decided_by="human", answered_at=2.0)],
        )

        assert apply_answers(path, _reply([{"id": "e1", "answer": "B"}])) == []

        latest = current_state(path)[0]
        assert latest["answer"] == "A"
        assert latest["decided_by"] == "human"


def test_a_needs_human_record_that_already_carries_an_answer_is_left_alone():
    # The undeliverable shape (see escalations.is_undeliverable): needs_human, but the answer is
    # already decided and a worker may have acted on it. Not ours to overwrite.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1", answer="A", decided_by="manager", delivery_attempts=3)])

        assert apply_answers(path, _reply([{"id": "e1", "answer": "B"}])) == []
        assert current_state(path)[0]["answer"] == "A"


def test_an_open_record_the_manager_has_not_triaged_yet_is_left_alone():
    # `open` means the daemon is still deciding it; answering here would race that.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1", status="open", tier=None)])

        assert apply_answers(path, _reply([{"id": "e1", "answer": "B"}])) == []
        assert current_state(path)[0]["status"] == "open"


# ---------- untrusted input: db content is data, never an instruction ----------


def test_a_malformed_ledger_line_and_a_malformed_db_record_are_both_skipped():
    # escalations.jsonl really does carry a bare integer line; the db payload is written by a
    # sandboxed page and read back through a model, so neither shape can be assumed.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, ["5", "{not json at all", _rec("e1")])

        applied = apply_answers(
            path,
            _reply(
                [
                    "not a dict",
                    {"answer": "B"},
                    {"id": "e1"},
                    {"id": 7, "answer": "B"},
                    {"id": "e1", "answer": None},
                    {"id": "e1", "answer": "B"},
                ]
            ),
        )

        assert applied == ["e1"]
        assert current_state(path)[0]["answer"] == "B"


def test_an_answer_the_worker_never_offered_is_refused():
    # The only string ever written to the ledger is one the LEDGER already lists as an option —
    # so nothing arbitrary from the shared db can reach the manager through this path.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1")])

        assert apply_answers(path, _reply([{"id": "e1", "answer": "rm -rf /"}])) == []
        assert current_state(path)[0]["answer"] is None


def test_an_id_the_ledger_does_not_know_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1")])

        assert apply_answers(path, _reply([{"id": "../../etc/passwd", "answer": "B"}])) == []
        assert len(read_all(path)) == 1


def test_a_record_with_no_options_cannot_be_answered_from_the_board():
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1", options=[])])

        assert apply_answers(path, _reply([{"id": "e1", "answer": "B"}])) == []


def test_the_appended_answer_is_the_ledgers_own_option_string():
    state = [_rec("e1")]

    assert accepted_answers(state, [{"id": "e1", "answer": "B"}]) == [("e1", "B")]
    # Same id twice in one payload still yields one append.
    assert accepted_answers(state, [{"id": "e1", "answer": "B"}] * 2) == [("e1", "B")]
    # A payload that isn't even a list is not an error, just nothing to do.
    assert accepted_answers(state, {"id": "e1", "answer": "B"}) == []
    assert accepted_answers(state, None) == []


# ---------- reading the payload out of a model-authored reply ----------


def test_extract_payload_tolerates_prose_and_fences_around_the_answers_line():
    assert extract_payload("ANSWERS: []") == []
    assert extract_payload('Done.\nREFRESH_OK: wrote 4\nANSWERS: [{"id": "e1", "answer": "B"}]') == [
        {"id": "e1", "answer": "B"}
    ]
    assert extract_payload("ANSWERS: ```json\n[{\"id\": \"e1\"}]\n```") == [{"id": "e1"}]


def test_a_reply_with_no_answers_line_is_loud_not_a_silent_empty():
    # Silence must not read as "no answers" — that would drop a real decision without a trace.
    for reply in ("REFRESH_OK: wrote 4 documents", "ANSWERS: not json at all"):
        try:
            extract_payload(reply)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {reply!r}")


def test_main_reports_a_broken_reply_without_raising(monkeypatch=None):
    # run-board-mirror.sh only sees the exit code, and turns a non-zero one into a warning.
    with tempfile.TemporaryDirectory() as tmp:
        path = _ledger(tmp, [_rec("e1")])
        argv, stdin = sys.argv, sys.stdin
        try:
            sys.argv = ["board_mirror_answers.py", "apply", path]
            sys.stdin = io.StringIO("REFRESH_OK: wrote 4 documents")
            assert main() == 1
            sys.stdin = io.StringIO(_reply([{"id": "e1", "answer": "A"}]))
            assert main() == 0
        finally:
            sys.argv, sys.stdin = argv, stdin
        assert current_state(path)[0]["answer"] == "A"
