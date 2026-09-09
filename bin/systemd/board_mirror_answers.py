#!/usr/bin/env python3
"""Bring an escalation answer recorded on the published board back down into the manager's ledger.

Everything else the pump does travels one way: board_state.py reads this plugin's data and the
scheduled session pushes it into the artifact db. An answer clicked on the board goes the other
way — board.html writes the chosen option to the artifact's own `escalation_answers` collection,
the same session reads that collection back on its next run, and this module decides what of it
may be appended to the escalation ledger.

Deliberately a SEPARATE collection from `escalations`: that one is board_state.py's write set, so
anything the page wrote there would be overwritten by the next mirror run — or, once the document
dropped out of board_state.py's output, turned into a `delete` by board_mirror_diff.py.

Two rules do all the work, and both are decided against the LEDGER, never against the payload:

  never clobber   an answer is appended only when the ledger's newest record for that id is
                  `needs_human` and carries no answer yet. Already answered (on port 4400, or by
                  the manager), dismissed, still `open` and untriaged by the daemon, or
                  needs_human carrying an undeliverable answer someone may already have acted on
                  — every one of those is left exactly as it is, and the incoming answer is
                  dropped with a line on stderr rather than layered on top.

  idempotent      falls out of that same rule. The answer is never deleted from the artifact db,
                  so every later pump run reads it again; the first append flips the ledger to
                  `answered`, which fails the rule above from then on. No dedupe file, no
                  delete-after-apply, nothing that can drift out of sync with the ledger.

The payload is untrusted — written by a sandboxed page, read back through a model — so it is
treated as data throughout. The only string ever appended is one the LEDGER itself lists among
that record's options: matched against the ledger's copy, then taken from it. Nothing arbitrary
out of shared storage can reach the manager through this path.
"""

import json
import os
import sys

# escalations.py lives one directory up, in bin/. board_state.py does the same insert inside its
# main(); this module needs it at import time instead, because both of its callers — the pump and
# the checks beside it — reach it with only bin/systemd on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from escalations import QUEUE_PATH, current_state, normalize_options, record_answer  # noqa: E402

# The artifact-db collection board.html writes a chosen option into, and the only one this reads.
# Never a collection board_state.py emits — see the module docstring.
#
# ponytail: applied answers are left in the collection rather than deleted, so the ANSWERS line
# the pump session prints carries every answer ever given from the board — a few dozen bytes each,
# and bounded by how many escalations get answered here at all. Prune applied documents (a delete
# arm in the same batch, once the ledger append succeeded) if that line ever gets long enough to
# matter; do NOT make idempotency depend on the delete, which is what the ledger gate is for.
ANSWERS_COLLECTION = "escalation_answers"

# Stamped on what this appends, alongside "human" (port 4400) and "manager" (the daemon), so the
# ledger keeps saying where each decision actually came from.
DECIDED_BY = "cto"

_MARKER = "ANSWERS:"


def extract_payload(reply: str) -> list:
    """The JSON array the pump session printed after board-mirror.md's `ANSWERS:` marker.

    Raises ValueError when the marker is missing or nothing after it parses — never an empty list
    for those. A reply that did not answer the question is not the same fact as "there are no
    answers", and reading one as the other would drop a real decision in silence, which is the
    exact failure this return path exists to prevent.

    Tolerant about what surrounds the array, for the same reason run-board-mirror.sh's REFRESH_OK
    grep is: a live run has already answered that prompt with a lead-in sentence. The marker's own
    line is tried first and everything after the marker second, so trailing prose containing a `]`
    cannot swallow the array.
    """
    idx = reply.find(_MARKER)
    if idx < 0:
        raise ValueError(f"reply carried no {_MARKER} line")
    tail = reply[idx + len(_MARKER) :]
    for candidate in (tail.split("\n", 1)[0], tail):
        start, end = candidate.find("["), candidate.rfind("]")
        if start < 0 or end <= start:
            continue
        try:
            return json.loads(candidate[start : end + 1])
        except ValueError:
            continue
    raise ValueError(f"no JSON array after the {_MARKER} marker")


def accepted_answers(state: list[dict], payload) -> list[tuple[str, str]]:
    """Pure: the (id, answer) pairs in one payload that may be appended to `state`'s ledger.

    `state` is current_state()'s fold — the newest record per id. Everything else is dropped
    without raising: a payload that is not a list at all, an entry that is not a dict, a missing
    or non-string field, an id the ledger never heard of, a record in any state but "still
    waiting on a human", an answer the worker never offered, or a second entry for an id this
    payload already spent.
    """
    by_id = {rec["id"]: rec for rec in state if isinstance(rec.get("id"), str)}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        rid, answer = item.get("id"), item.get("answer")
        if not isinstance(rid, str) or not isinstance(answer, str) or rid in seen:
            continue
        rec = by_id.get(rid)
        if rec is None or rec.get("status") != "needs_human" or rec.get("answer") is not None:
            continue
        # The ledger's own copy of the matching option, not the payload's string. Identical by
        # construction — it is the ledger that decides what may be written to the ledger.
        match = next((opt for opt in normalize_options(rec.get("options")) if opt == answer), None)
        if match is None:
            continue
        seen.add(rid)
        out.append((rid, match))
    return out


def apply_answers(path: str, reply: str) -> list[str]:
    """Append every acceptable answer in `reply` to the ledger at `path`. Returns the ids appended.

    record_answer() re-folds the ledger per append, so this is O(ledger) per answer. The accepted
    set is bounded by how many escalations sit needs_human at once — in practice one or two.
    """
    applied = []
    for rid, answer in accepted_answers(current_state(path), extract_payload(reply)):
        record_answer(path, rid, answer, DECIDED_BY)
        applied.append(rid)
    return applied


def main() -> int:
    """`board_mirror_answers.py apply [<ledger-path>]` reads the pump session's reply on stdin and
    appends what it may, defaulting to the real queue.

    Non-zero only when the reply could not be read at all — run-board-mirror.sh turns that into a
    journal warning rather than a failed run, so neither direction of the pump can take the other
    down. A payload that parsed but held nothing acceptable is a success with nothing to do.
    """
    if len(sys.argv) < 2 or sys.argv[1] != "apply" or len(sys.argv) > 3:
        print("usage: board_mirror_answers.py apply [<ledger-path>]", file=sys.stderr)
        return 1
    path = sys.argv[2] if len(sys.argv) == 3 else QUEUE_PATH
    try:
        applied = apply_answers(path, sys.stdin.read())
    except ValueError as e:
        print(f"board_mirror_answers: could not read the answers payload: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"board_mirror_answers: could not update {path}: {e}", file=sys.stderr)
        return 1
    print(f"board_mirror_answers: appended {len(applied)} answer(s){': ' + ', '.join(applied) if applied else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
