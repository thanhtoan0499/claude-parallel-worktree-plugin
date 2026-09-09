#!/usr/bin/env python3
"""Cut board_state.py's full write set down to what actually changed since the last successful
run, turn documents that disappeared into `delete` ops, and split the result into batches the
artifact's write_db actually accepts.

Pure transforms only, like board_state.py itself — no I/O in diff_writes()/chunk_writes()/
apply_batch(), so this is testable without a real snapshot file. main() below is the only I/O,
called by run-board-mirror.sh once per run for "diff" and "chunk", then once per batch for
"apply".

The snapshot is a CHECKPOINT, not an end-of-run report. It used to be written exactly once, after
the whole write finished, which meant a run killed at TimeoutStartSec mid-write recorded nothing:
259 live documents against a 120-document snapshot produced a 156-document diff that took ~310s
against a 240s budget, so every run wrote ~120 documents, died, recorded none of them, and the
next run recomputed the identical diff and died identically. The pump could not recover on its
own. "apply" replaces that: run-board-mirror.sh folds each batch in only after that batch's own
claude -p reported it landed, so an interrupted run keeps what it actually wrote and the next run
only does the remainder.

Direction matters and is asymmetric. Re-writing a document that was already written is harmless
(write_db set is idempotent); recording one that was NOT written is unrecoverable — the next diff
calls it unchanged and skips it forever, leaving a silently wrong board with no error anywhere.
So the record always happens AFTER the write, never before: a crash in between costs one rewrite.
"""

import json
import os
import re
import sys

BATCH_LIMIT = 50  # write_db's own cap: a batch takes at most 50 entries.

# write_db validates doc_id BEFORE writing anything, so an entry carrying an id outside this
# charset does not get skipped — the server refuses the whole batch ("batch rejected before any
# write, no documents landed"), and every other document travelling with it is lost too.
# board_state.py normalizes ids at the mint now, but the guards below are what make that provable
# here rather than assumed: an id the validator rejects CANNOT be in the db, so it must never be
# sent, and must never be recorded as written. Both halves were violated live — the snapshot held
# `sessions/code review verification`, a document that had never existed.
DOC_ID_OK = re.compile(r"[A-Za-z0-9_\-.~:@+]{1,200}\Z")


def _valid(doc_id: str) -> bool:
    return bool(DOC_ID_OK.match(str(doc_id)))


def _key(entry: dict) -> str:
    return f"{entry['collection']}/{entry['doc_id']}"


def diff_writes(previous: dict, writes: list[dict]) -> list[dict]:
    """previous: {"collection/doc_id": data} from the last checkpoint, or {} if there isn't one
    yet (first run, or a snapshot that failed to save last time).

    writes: this run's full "set" list from board_state.py, one entry per document that exists
    right now, meta/status last.

    Returns what to actually send: unchanged "set" entries dropped, a "delete" for every
    previously-known doc_id no longer in `writes` at all, and meta/status always kept and always
    last — it's the refresh timestamp, so "unchanged" never applies to it.
    """
    writes = _sendable(writes)
    previous = {k: v for k, v in previous.items() if _valid(k.split("/", 1)[-1])}
    if not writes or not previous:
        return list(writes)

    meta = writes[-1]
    rest = writes[:-1]
    # Includes meta's own key, not just rest's — otherwise meta/status (always real, never
    # "gone") would look like a doc that disappeared and get turned into a spurious delete.
    current_keys = {_key(w) for w in writes}

    changed = [w for w in rest if previous.get(_key(w)) != w["data"]]
    deletes = [
        {"op": "delete", "collection": key.split("/", 1)[0], "doc_id": key.split("/", 1)[1]}
        for key in previous
        if key not in current_keys
    ]
    return changed + deletes + [meta]


def _sendable(writes: list[dict]) -> list[dict]:
    """Drop entries write_db would reject the whole batch over, loudly. Dropping one document is
    strictly better than losing the 49 travelling with it — and better than the alternative that
    actually happened, where the rejected id stayed in the snapshot, turned into a `delete`
    carrying the same rejected id when its session ended, and wedged every later run permanently.
    """
    ok = []
    for w in writes:
        if _valid(w.get("doc_id", "")):
            ok.append(w)
        else:
            print(f"board_mirror_diff: skipping unwritable doc_id {_key(w)!r}", file=sys.stderr)
    return ok


def chunk_writes(entries: list[dict], limit: int = BATCH_LIMIT) -> list[list[dict]]:
    """Split in order into batches of at most `limit`. Order is never touched, so meta/status —
    last in diff_writes' output — stays the last entry of the last batch: it asserts the rows
    beside it are current, and a run that stops early must not have already claimed a sweep whose
    rows never landed.

    The caller splitting, rather than the prompt telling the model to, is what makes the
    checkpoint provable: one claude -p per batch means one REFRESH_OK line attributes to exactly
    these entries. When the model split, a single OK line covered several batches and the script
    had no way to tell which of them actually landed.
    """
    return [entries[i : i + limit] for i in range(0, len(entries), limit)]


def apply_batch(previous: dict, batch: list[dict]) -> dict:
    """Fold one batch that has ALREADY been confirmed written into the snapshot: `set` records the
    data, `delete` drops the key. Applying every batch of a diff in order leaves exactly the
    snapshot a single end-of-run write used to leave — unchanged documents are already correct in
    `previous` and are simply not touched.
    """
    snapshot = dict(previous)
    for entry in batch:
        if not _valid(entry.get("doc_id", "")):
            # Unreachable if the entry came through diff_writes, and stated here anyway: this is
            # the one direction that cannot be undone. Recording a document the validator refused
            # makes the next diff call it unchanged and skip it forever, so the board silently
            # loses it with no error anywhere.
            continue
        if entry.get("op") == "delete":
            snapshot.pop(_key(entry), None)
        else:
            snapshot[_key(entry)] = entry["data"]
    return snapshot


def _load_snapshot(path: str) -> dict:
    """Missing (first install) or unreadable (never synced, or a snapshot write that itself
    failed) both mean "no previous run" — send everything, which is correct on a first run."""
    try:
        with open(path, encoding="utf-8") as f:
            snapshot = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    # Prune ids that cannot exist in the db, so an already-damaged snapshot heals itself on the
    # next apply instead of needing the hand edit it needed the first time.
    return {k: v for k, v in snapshot.items() if _valid(k.split("/", 1)[-1])}


def main() -> int:
    """CLI. All three modes read JSON from stdin:

    `board_mirror_diff.py diff <snapshot-path>` takes the full write-set array and prints the
    entries to actually send (JSON array).

    `board_mirror_diff.py chunk [limit]` takes that diff array and prints one batch per line, as
    a JSON array each. One line per batch is safe because json.dump escapes newlines, so no batch
    can ever contain a literal one.

    `board_mirror_diff.py apply <snapshot-path>` takes ONE batch array and folds it into the
    snapshot, printing how many entries it recorded. Written via a temp file + rename so a crash
    mid-write can't leave a half-written, unparseable snapshot for the next run to trip over.
    """
    argv = sys.argv[1:]
    mode = argv[0] if argv else ""
    if mode in ("diff", "apply") and len(argv) == 2:
        path = argv[1]
    elif mode == "chunk" and len(argv) <= 2:
        path = None
    else:
        print(
            "usage: board_mirror_diff.py {diff|apply} <snapshot-path> | chunk [limit]",
            file=sys.stderr,
        )
        return 1

    payload = json.load(sys.stdin)

    if mode == "diff":
        json.dump(diff_writes(_load_snapshot(path), payload), sys.stdout, ensure_ascii=False)
        return 0

    if mode == "chunk":
        limit = int(argv[1]) if len(argv) == 2 else BATCH_LIMIT
        for batch in chunk_writes(payload, limit):
            json.dump(batch, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        return 0

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(apply_batch(_load_snapshot(path), payload), f, ensure_ascii=False)
    os.replace(tmp, path)
    print(len(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
