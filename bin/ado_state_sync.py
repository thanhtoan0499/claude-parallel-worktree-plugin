#!/usr/bin/env python3
"""Writes back the one class of state_drift board_state.ticket_state_drift() can prove: a ticket
sitting at New while its PR proves work has started. Everything else state_drift reports
(merged_not_closed, a Blocked ticket with no ledger note) is `fixable: false` and is never touched
here — those need a human.

Reads the pump's own last-successful-write snapshot (the same file board_mirror_diff.py
checkpoints to, `~/.config/board-mirror/last-writes.json` by default) rather than talking to the
artifact db itself — this script has no artifact credentials, and the snapshot already holds
exactly what the board last published, keyed `<collection>/<doc_id>`.

dry-run is the default; writing for real needs `--apply`. A failed `az` call is logged and does
not stop the remaining tickets — one flaky ticket must not silently strand the rest of the batch.
"""

import json
import os
import subprocess
import sys

from dashboard import _ADO_ORG

DEFAULT_SNAPSHOT_PATH = os.path.expanduser("~/.config/board-mirror/last-writes.json")

# Closing a ticket is a human decision — never written here, however a bug upstream might manage
# to mark a drift towards one of these "fixable". Defence in depth: eligible_state_syncs() must
# refuse these even if ticket_state_drift() is ever wrong.
_NEVER_WRITE_STATES = frozenset({"Closed", "Removed"})


def snapshot_path() -> str:
    """Same override run-board-mirror.sh honours, so a test run never touches the real snapshot."""
    return os.environ.get("BOARD_MIRROR_SNAPSHOT", DEFAULT_SNAPSHOT_PATH)


def read_ticket_docs(path: str) -> dict[str, dict]:
    """The `tickets` collection out of the pump's last-written snapshot, keyed by ticket id."""
    with open(path, encoding="utf-8") as f:
        snapshot = json.load(f)
    docs = {}
    for key, data in snapshot.items():
        collection, _, doc_id = key.partition("/")
        if collection == "tickets" and doc_id:
            docs[doc_id] = data
    return docs


def eligible_state_syncs(tickets: dict[str, dict]) -> list[dict]:
    """The tickets Part 3 may actually write, in ticket-id order for a deterministic log.

    Three gates, all of them hard limits from the brief: `fixable` (only Rule A's New+PR-exists
    direction ever proposes a state at all), `handed_off` is false (owner's tickets only — reusing
    ticket_docs()'s own ownership field, not a second notion of who owns a ticket), and the
    proposed state is never Closed or Removed.
    """
    plans = []
    for ticket_id, doc in (tickets or {}).items():
        drift = doc.get("state_drift")
        if not isinstance(drift, dict) or not drift.get("fixable"):
            continue
        if doc.get("handed_off"):
            continue
        to_state = drift.get("proposed_state")
        if not to_state or to_state in _NEVER_WRITE_STATES:
            continue
        plans.append({
            "id": str(ticket_id),
            "from_state": doc.get("state") or "",
            "to_state": to_state,
            "reason": drift.get("reason") or "",
        })
    return sorted(plans, key=lambda p: p["id"])


def _log(plan: dict, *, dry_run: bool, error: str | None) -> None:
    head = f"ado_state_sync: AB#{plan['id']} {plan['from_state']} -> {plan['to_state']} ({plan['reason']})"
    if error:
        print(f"{head} — FAILED: {error}")
    elif dry_run:
        print(f"[dry-run] {head}")
    else:
        print(f"{head} — written")


def sync_tickets(tickets: dict[str, dict], org: str, dry_run: bool = True, run=subprocess.run) -> list[dict]:
    """Apply (or, dry-run, only announce) every eligible plan. One `az` failure is caught and
    logged so the remaining tickets in the batch are still attempted."""
    results = []
    for plan in eligible_state_syncs(tickets):
        if dry_run:
            _log(plan, dry_run=True, error=None)
            results.append({**plan, "applied": False, "error": None})
            continue
        try:
            run(
                ["az", "boards", "work-item", "update", "--org", org,
                 "--id", plan["id"], "--state", plan["to_state"]],
                check=True, capture_output=True, text=True,
            )
        except Exception as exc:
            _log(plan, dry_run=False, error=str(exc))
            results.append({**plan, "applied": False, "error": str(exc)})
            continue
        _log(plan, dry_run=False, error=None)
        results.append({**plan, "applied": True, "error": None})
    return results


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                         help="Actually write to ADO. Without this flag, prints the plan and writes nothing.")
    parser.add_argument("--org", default=_ADO_ORG)
    args = parser.parse_args(argv)

    path = snapshot_path()
    try:
        tickets = read_ticket_docs(path)
    except (OSError, ValueError) as exc:
        print(f"ado_state_sync: cannot read {path}: {exc}", file=sys.stderr)
        return 1

    results = sync_tickets(tickets, args.org, dry_run=not args.apply)
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
