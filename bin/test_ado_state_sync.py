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
    assert plans == [{"id": "8471", "from_state": "New", "to_state": "Active",
                      "reason": "PR #726 đang chờ review", "assign_to": None}]


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
    assert results == [{"id": "8471", "from_state": "New", "to_state": "Active", "assign_to": None,
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


# ---------------------------------------------------------------------------
# Rule D's hand-off: a merged Bug moves AND changes hands. A state written without the
# assignee leaves the ticket in a queue with nobody's name on it, which is the failure the
# rule exists to prevent.
# ---------------------------------------------------------------------------


def test_a_qc_handoff_carries_the_assignee_through_to_the_plan():
    from ado_state_sync import eligible_state_syncs

    plans = eligible_state_syncs({
        "8309": {"state": "Active", "handed_off": False, "state_drift": {
            "proposed_state": "Ready for QC verify on Stag", "reason": "PR #719 đã merged",
            "assign_to": "QC", "fixable": True}},
    })
    assert len(plans) == 1
    assert plans[0]["assign_to"] == "QC"


def test_a_plan_without_a_hand_off_names_nobody_rather_than_defaulting_to_someone():
    from ado_state_sync import eligible_state_syncs

    plans = eligible_state_syncs({
        "8382": {"state": "New", "handed_off": False, "state_drift": {
            "proposed_state": "Active", "reason": "PR #733 đang chờ review", "fixable": True}},
    })
    assert plans[0]["assign_to"] is None


def test_the_qc_role_resolves_to_a_real_person_before_it_reaches_az():
    """A role name is what the rule can know; an ADO identity is what the write needs. The
    mapping lives in one place so a QC handover does not mean editing the rule."""
    from ado_state_sync import ROLE_IDENTITY, sync_tickets

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "{}"
        return R()

    sync_tickets({
        "8309": {"state": "Active", "handed_off": False, "state_drift": {
            "proposed_state": "Ready for QC verify on Stag", "reason": "đã merged",
            "assign_to": "QC", "fixable": True}},
    }, "https://dev.azure.com/agentiqai", dry_run=False, run=fake_run)

    assert len(calls) == 1, "the state and the assignee must land in one update, not two"
    assert "--assigned-to" in calls[0]
    assert calls[0][calls[0].index("--assigned-to") + 1] == ROLE_IDENTITY["QC"]


def test_a_state_only_plan_never_passes_an_empty_assignee_to_az():
    """`--assigned-to ''` clears the field in ADO — a silent unassignment on every sync."""
    from ado_state_sync import sync_tickets

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        class R:
            returncode = 0
            stdout = "{}"
        return R()

    sync_tickets({
        "8382": {"state": "New", "handed_off": False, "state_drift": {
            "proposed_state": "Active", "reason": "chờ review", "fixable": True}},
    }, "https://dev.azure.com/agentiqai", dry_run=False, run=fake_run)

    assert "--assigned-to" not in calls[0]
