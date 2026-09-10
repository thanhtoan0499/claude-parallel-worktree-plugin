#!/usr/bin/env python3
"""assert-based checks for ado_state_sync — Part 3: writing back ONLY the New+PR-exists direction
state_drift can prove, dry-run by default, owner's tickets only, never Closed/Removed.
"""

import json

from ado_state_sync import eligible_state_syncs, read_ticket_docs, sync_tickets


def _doc(state="New", drift=None, handed_off=False, **over):
    doc = {"id": "1", "title": "x", "state": state, "state_drift": drift, "handed_off": handed_off}
    doc.update(over)
    return doc


def _fixable(to_state="Active", reason="PR #726 đang chờ review"):
    return {"proposed_state": to_state, "reason": reason, "fixable": True}


def _unfixable(reason="đã merged"):
    return {"proposed_state": None, "reason": reason, "fixable": False}


def test_eligible_state_syncs_picks_up_a_fixable_new_pr_drift():
    docs = {"8471": _doc(state="New", drift=_fixable())}
    plans = eligible_state_syncs(docs)
    assert plans == [{"id": "8471", "from_state": "New", "to_state": "Active", "reason": "PR #726 đang chờ review"}]


def test_eligible_state_syncs_skips_a_ticket_with_no_drift():
    docs = {"1": _doc(drift=None)}
    assert eligible_state_syncs(docs) == []


def test_eligible_state_syncs_skips_an_unfixable_drift():
    """merged_not_closed and the Blocked-no-reason rule both report but propose nothing — this is
    the hard limit that keeps Part 3 from ever acting on them."""
    docs = {"1": _doc(state="Active", drift=_unfixable())}
    assert eligible_state_syncs(docs) == []


def test_eligible_state_syncs_never_writes_a_ticket_handed_off_to_someone_else():
    """ONLY tickets assigned to the board owner — reusing ticket_docs()'s own handed_off field,
    not a second notion of ownership."""
    docs = {"1": _doc(drift=_fixable(), handed_off=True)}
    assert eligible_state_syncs(docs) == []


def test_eligible_state_syncs_never_proposes_closed_or_removed_even_if_marked_fixable():
    """Defence in depth: closing needs a human, however the drift got flagged fixable."""
    docs = {
        "1": _doc(drift=_fixable(to_state="Closed")),
        "2": _doc(drift=_fixable(to_state="Removed")),
    }
    assert eligible_state_syncs(docs) == []


def test_read_ticket_docs_filters_the_snapshot_down_to_the_tickets_collection(tmp_path):
    snapshot = tmp_path / "last-writes.json"
    snapshot.write_text(json.dumps({
        "tickets/8471": {"id": "8471", "state": "New", "state_drift": _fixable(), "handed_off": False},
        "sessions/t1": {"task": "t1"},
        "meta/status": {"written_at": 1.0},
    }), encoding="utf-8")

    docs = read_ticket_docs(str(snapshot))

    assert list(docs) == ["8471"]
    assert docs["8471"]["state"] == "New"


def test_sync_tickets_dry_run_writes_nothing_and_says_what_it_would_do(capsys):
    docs = {"8471": _doc(drift=_fixable())}
    calls = []

    results = sync_tickets(docs, org="https://dev.azure.com/x", dry_run=True, run=lambda *a, **k: calls.append(a))

    assert calls == [], "dry-run must never shell out"
    assert results == [{"id": "8471", "from_state": "New", "to_state": "Active",
                         "reason": "PR #726 đang chờ review", "applied": False, "error": None}]
    out = capsys.readouterr().out
    assert "8471" in out and "New" in out and "Active" in out and "PR #726 đang chờ review" in out


def test_sync_tickets_live_run_shells_out_with_the_exact_az_command():
    docs = {"8471": _doc(drift=_fixable())}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return None

    results = sync_tickets(docs, org="https://dev.azure.com/agentiqai", dry_run=False, run=fake_run)

    assert calls == [[
        "az", "boards", "work-item", "update",
        "--org", "https://dev.azure.com/agentiqai", "--id", "8471", "--state", "Active",
    ]]
    assert results[0]["applied"] is True
    assert results[0]["error"] is None


def test_sync_tickets_logs_ticket_old_state_new_state_and_reason_on_a_live_write(capsys):
    docs = {"8471": _doc(drift=_fixable())}
    sync_tickets(docs, org="https://dev.azure.com/x", dry_run=False, run=lambda *a, **k: None)
    out = capsys.readouterr().out
    assert "8471" in out and "New" in out and "Active" in out and "PR #726 đang chờ review" in out


def test_a_failed_az_call_does_not_abort_the_remaining_tickets():
    docs = {
        "1": _doc(drift=_fixable(), state="New"),
        "2": _doc(drift=_fixable(), state="New"),
    }
    calls = []

    def flaky_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[cmd.index("--id") + 1] == "1":
            raise RuntimeError("az: connection reset")
        return None

    results = sync_tickets(docs, org="https://dev.azure.com/x", dry_run=False, run=flaky_run)

    assert len(calls) == 2, "the second ticket must still be attempted"
    by_id = {r["id"]: r for r in results}
    assert by_id["1"]["applied"] is False and "connection reset" in by_id["1"]["error"]
    assert by_id["2"]["applied"] is True and by_id["2"]["error"] is None
