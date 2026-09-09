#!/usr/bin/env python3
"""assert-based checks for board_mirror_diff, the incremental-write/delete transform behind
run-board-mirror.sh."""

from board_mirror_diff import apply_batch, chunk_writes, diff_writes

META = {"op": "set", "collection": "meta", "doc_id": "status", "data": {"written_at": 2}}


def _set(collection, doc_id, data):
    return {"op": "set", "collection": collection, "doc_id": doc_id, "data": data}


def test_first_run_with_no_previous_sends_everything():
    writes = [_set("sessions", "a", {"state": "running"}), META]

    assert diff_writes({}, writes) == writes


def test_unchanged_doc_is_dropped_but_meta_status_always_kept():
    writes = [_set("sessions", "a", {"state": "running"}), META]
    previous = apply_batch({}, writes)

    result = diff_writes(previous, writes)

    assert result == [META]


def test_changed_doc_is_sent_new_doc_is_sent():
    previous = apply_batch({}, [_set("sessions", "a", {"state": "running"})])
    writes = [
        _set("sessions", "a", {"state": "waiting"}),  # changed
        _set("sessions", "b", {"state": "running"}),  # new
        META,
    ]

    result = diff_writes(previous, writes)

    assert result == [writes[0], writes[1], META]


def test_doc_missing_from_this_run_becomes_a_delete_before_meta_status():
    previous = apply_batch({}, 
        [_set("sessions", "a", {"state": "running"}), _set("tickets", "7763", {"state": "done"})]
    )
    # "tickets/7763" no longer exists this run — e.g. handed off to QC and closed out.
    writes = [_set("sessions", "a", {"state": "running"}), META]

    result = diff_writes(previous, writes)

    assert result == [
        {"op": "delete", "collection": "tickets", "doc_id": "7763"},
        META,
    ]


def test_apply_batch_keys_by_collection_slash_doc_id_and_keeps_only_data():
    writes = [_set("sessions", "a", {"state": "running"}), META]

    assert apply_batch({}, writes) == {
        "sessions/a": {"state": "running"},
        "meta/status": {"written_at": 2},
    }


# --- checkpointing: what an interrupted run is allowed to remember ------------------------------
# The pump is killed at TimeoutStartSec mid-write when the diff is large (259 documents live,
# a 156-document diff, ~310s against a 240s budget). Before checkpointing, the snapshot was
# written once at the very end, so a run that wrote 120 of 156 documents recorded none of them
# and the next run recomputed the identical diff and died identically — forever. These pin the
# per-batch record that replaces it.


def _key(entry):
    return f"{entry['collection']}/{entry['doc_id']}"


def _many(n):
    """n ticket documents plus meta/status last — the shape board_state.py emits."""
    return [_set("tickets", str(i), {"n": i}) for i in range(n)] + [META]


def test_chunk_writes_splits_in_order_at_the_limit_and_keeps_meta_status_last():
    entries = _many(120)

    batches = chunk_writes(entries, 50)

    assert [len(b) for b in batches] == [50, 50, 21]
    # Order preserved end to end, and meta/status is the very last entry of the last batch:
    # it asserts the rows beside it are current, so it must never land before they do.
    assert [e for b in batches for e in b] == entries
    assert batches[-1][-1] == META


def test_apply_batch_records_exactly_the_entries_it_was_handed():
    batches = chunk_writes(diff_writes({}, _many(120)), 50)

    snapshot = apply_batch({}, batches[0])

    assert set(snapshot) == {_key(e) for e in batches[0]}


def test_apply_batch_removes_a_deleted_doc_rather_than_recording_it():
    previous = apply_batch({}, [_set("tickets", "retired-1", {"state": "done"})])

    snapshot = apply_batch(
        previous, [{"op": "delete", "collection": "tickets", "doc_id": "retired-1"}]
    )

    assert snapshot == {}


def test_an_interrupted_run_never_records_a_document_it_did_not_write():
    # The run wrote batch 1, reported it, then was killed part-way through batch 2. Recording a
    # document that never landed is the one unrecoverable direction: the next diff would call it
    # unchanged and skip it forever, leaving a silently wrong board with no error anywhere.
    batches = chunk_writes(diff_writes({}, _many(120)), 50)

    snapshot = apply_batch({}, batches[0])

    unwritten = {_key(e) for b in batches[1:] for e in b}
    assert unwritten, "fixture must have more than one batch or this proves nothing"
    assert unwritten.isdisjoint(snapshot)


def test_applying_every_batch_in_order_equals_one_full_end_of_run_snapshot():
    # Equivalence with the old all-at-the-end write: a run that finishes every batch must leave
    # exactly the snapshot the single final write used to leave, or the incremental path would
    # drift from the full one over time.
    previous = apply_batch({}, [_set("tickets", "gone", {"state": "old"}), _set("sessions", "a", {"s": 1})])
    writes = [_set("sessions", "a", {"s": 1}), _set("tickets", "1", {"n": 1}), META]

    snapshot = previous
    for batch in chunk_writes(diff_writes(previous, writes), 50):
        snapshot = apply_batch(snapshot, batch)

    assert snapshot == apply_batch({}, writes)


def test_repeated_interrupted_runs_shrink_the_backlog_to_nothing():
    # Property 2: kill after batch 1, run again, kill after batch 1 again... the backlog must
    # shrink every time and reach the steady-state floor (meta/status alone, always resent).
    writes = _many(120)
    snapshot = {}
    backlog = []

    for _ in range(10):
        pending = diff_writes(snapshot, writes)
        backlog.append(len(pending))
        if pending == [META]:
            break
        snapshot = apply_batch(snapshot, chunk_writes(pending, 50)[0])

    assert backlog == [121, 71, 21, 1], f"backlog did not drain monotonically: {backlog}"


def test_steady_state_costs_one_batch_and_nothing_extra():
    # Property 4: five changed documents is still one batch and one apply, exactly as before.
    writes = _many(120)
    synced = {}
    for batch in chunk_writes(diff_writes({}, writes), 50):
        synced = apply_batch(synced, batch)
    changed = [_set("tickets", str(i), {"n": i, "touched": True}) for i in range(5)]
    writes = changed + writes[5:]

    batches = chunk_writes(diff_writes(synced, writes), 50)

    assert len(batches) == 1
    assert len(batches[0]) == 6  # the five changed documents plus meta/status


# --- an id write_db cannot accept must never be sent, and never recorded -------------------------
# doc_id must match ^[A-Za-z0-9_\-.~:@+]{1,200}$. write_db validates before writing anything, so a
# batch carrying one bad id lands NOTHING — the failure is not "that row is skipped", it is "every
# other document in the batch is lost too". Two real session names ("code review verification",
# "git checkout test verification") hit this live. Worse, they were then RECORDED in the snapshot,
# which is impossible: an id the validator rejects cannot exist in the db. The snapshot was being
# inferred from "the run ended" instead of confirmed per document.

BAD = "code review verification"


def test_a_doc_id_write_db_would_reject_is_never_sent():
    writes = [_set("sessions", BAD, {"state": "running"}), _set("sessions", "ok", {"n": 1}), META]

    result = diff_writes({}, writes)

    assert [w["doc_id"] for w in result] == ["ok", "status"]


def test_a_bad_id_already_in_the_snapshot_does_not_become_an_undeletable_delete():
    # The wedge that had to be undone by hand: the snapshot remembered a bad id, the session went
    # away, diff turned it into a `delete` carrying that same rejected id, and every batch from
    # then on was refused — permanently, with no run able to make progress.
    previous = {f"sessions/{BAD}": {"state": "running"}, "sessions/ok": {"n": 1}}
    writes = [_set("sessions", "ok", {"n": 1}), META]

    result = diff_writes(previous, writes)

    assert all(w["op"] != "delete" for w in result), f"emitted a delete that cannot succeed: {result}"


def test_apply_batch_refuses_to_record_a_document_that_cannot_exist():
    # Property 1, in the direction that is unrecoverable. An id the validator rejects was never
    # written, so recording it would make the next diff call it unchanged and skip it forever.
    snapshot = apply_batch({}, [_set("sessions", BAD, {"state": "running"}), _set("sessions", "ok", {"n": 1})])

    assert set(snapshot) == {"sessions/ok"}
