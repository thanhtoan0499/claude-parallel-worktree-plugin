#!/usr/bin/env python3
"""assert-based checks for the stuck-session watch. Run: python3 bin/test_stuck_sessions.py

The `claude agents` reader is injected everywhere, the same way test_board_state.py injects its
readers — nothing here shells out to the real CLI or touches the live queue.
"""

import os
import re
import tempfile

from escalations import CANONICAL_KINDS, append, classify, current_state, new_record, normalize_kind
from stuck_sessions import KIND, _unit_path, last_tool_use, scan, stuck_now

NOW = 1788940000.0
STALE = 900.0  # 15 minutes — a 5-minute cadence times CLAIM_STALE_MULTIPLIER
LIVE_WORKTREE = tempfile.mkdtemp(prefix="stuck-worktree-")
DEAD_WORKTREE = os.path.join(LIVE_WORKTREE, "removed-days-ago")


def _tmp_queue():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    return path


def _agent(name="t8317", sid="sess-8317", state="blocked", cwd=LIVE_WORKTREE, waiting="permission prompt"):
    """One row shaped like a real `claude agents --json` entry (captured live, 2026-09-09)."""
    agent = {
        "name": name,
        "sessionId": sid,
        "cwd": cwd,
        "kind": "background",
        "state": state,
        "startedAt": 1788864863956,
    }
    if waiting:
        agent["waitingFor"] = waiting
    return agent


def _registry(name="t8317"):
    return {name: {"branch": "feature/t8317", "path": LIVE_WORKTREE, "short_id": "aaaf4fe7"}}


def _probe(quiet_for=3600.0, pending="Bash: git checkout origin/main -- services/gateway/x.py"):
    def probe(_session_id):
        return {"last_activity": NOW - quiet_for, "pending": pending}

    return probe


def _scan(path, agents, registry=None, probe=None, now=NOW, stale_after=STALE):
    return scan(path, agents, registry if registry is not None else _registry(), probe or _probe(), now, stale_after)


# --- 1. a genuinely stuck session files one record -------------------------------------------

def test_a_stuck_managed_worker_files_one_record():
    path = _tmp_queue()

    actions = _scan(path, [_agent()])

    assert [a["action"] for a in actions] == ["filed"]
    records = current_state(path)
    assert len(records) == 1
    assert records[0]["session_id"] == "sess-8317"
    assert normalize_kind(records[0]["kind"]) == KIND


def test_the_filed_record_carries_what_the_manager_needs_to_act():
    """A record saying only "session X is blocked" makes the manager go dig. These four fields are
    what let it decide without investigating — the pending command above all, which is what turned
    three of the four frozen 2026-09-09 sessions into one-line fixes."""
    path = _tmp_queue()
    _scan(path, [_agent()])

    ev = current_state(path)[0]["evidence"]
    assert ev["pending"] == "Bash: git checkout origin/main -- services/gateway/x.py"
    assert ev["waiting_for"] == "permission prompt"
    assert ev["task"] == "t8317"
    assert ev["worktree"] == LIVE_WORKTREE
    assert ev["branch"] == "feature/t8317"
    assert "1h" in ev["stuck_for"], ev["stuck_for"]


def test_a_session_blocked_with_no_waiting_for_field_still_files():
    """`fix720` was `state: blocked` with no `waitingFor` key at all (captured live). The field is
    evidence, not the trigger — requiring it would have missed one of the four."""
    path = _tmp_queue()

    actions = _scan(path, [_agent(name="fix720", sid="sess-720", waiting=None)], registry=_registry("fix720"))

    assert [a["action"] for a in actions] == ["filed"]
    assert current_state(path)[0]["evidence"]["waiting_for"] == "unknown"


# --- 2. a second scan does not file a second --------------------------------------------------

def test_a_second_scan_over_the_same_stuck_session_files_nothing():
    """The check runs every few minutes against a condition that persists. Filing per scan buries
    the queue within the hour."""
    path = _tmp_queue()
    _scan(path, [_agent()])

    actions = _scan(path, [_agent()])

    assert actions == []
    assert len(current_state(path)) == 1


def test_a_record_the_manager_already_answered_does_not_get_refiled():
    """`answered` is not `dismissed`: the answer is on its way to the worker, which is still
    sitting at the prompt until it lands. Re-filing here would double-ask."""
    path = _tmp_queue()
    _scan(path, [_agent()])
    rec = current_state(path)[0]
    append(path, {**rec, "status": "answered", "answer": "run it with --force", "decided_by": "manager"})

    assert _scan(path, [_agent()]) == []


# --- 3. a session that starts moving clears ---------------------------------------------------

def test_a_session_that_starts_moving_again_clears_its_record():
    path = _tmp_queue()
    _scan(path, [_agent()])

    actions = _scan(path, [_agent(state="working")])

    assert [a["action"] for a in actions] == ["cleared"]
    assert current_state(path)[0]["status"] == "dismissed"


def test_a_session_that_disappears_entirely_clears_its_record():
    """A worker that finished and dropped off the list is not still waiting on a decision."""
    path = _tmp_queue()
    _scan(path, [_agent()])

    actions = _scan(path, [])

    assert [a["action"] for a in actions] == ["cleared"]
    assert current_state(path)[0]["status"] == "dismissed"


def test_a_cleared_record_is_not_re_cleared_on_the_next_scan():
    path = _tmp_queue()
    _scan(path, [_agent()])
    _scan(path, [])

    assert _scan(path, []) == []


def test_a_session_that_gets_stuck_again_after_clearing_files_a_fresh_record():
    """A dismissed record is finished business. The next freeze is a new question, not a revival
    of the old one — otherwise one resolved block silences that session forever."""
    path = _tmp_queue()
    _scan(path, [_agent()])
    first = current_state(path)[0]["id"]
    _scan(path, [_agent(state="working")])

    actions = _scan(path, [_agent()])

    assert [a["action"] for a in actions] == ["filed"]
    assert {r["id"] for r in current_state(path)} == {first, actions[0]["id"]}


# --- 4. a corpse does not file at all ---------------------------------------------------------

def test_a_session_whose_worktree_is_gone_files_nothing():
    """Five of the nine the by-hand check reported were corpses, three with the worktree already
    deleted. There is nothing left to unblock, so there is no decision for the manager to make."""
    path = _tmp_queue()

    assert _scan(path, [_agent(cwd=DEAD_WORKTREE)]) == []
    assert current_state(path) == []


def test_a_session_with_no_transcript_to_measure_files_nothing():
    """No transcript means no way to say how long it has been quiet. Filing without a measured
    duration is how a staleness alarm ends up firing on a guess."""
    path = _tmp_queue()

    assert _scan(path, [_agent()], probe=lambda _sid: None) == []
    assert current_state(path) == []


# --- 5. an unmanaged ad-hoc session does not file ----------------------------------------------

def test_an_unmanaged_ad_hoc_session_files_nothing():
    """`managed` is board_state.session_docs()'s own rule — an entry EXISTS in the registry. A
    session nobody dispatched is somebody's own terminal, and a human is sitting in front of it."""
    path = _tmp_queue()

    assert _scan(path, [_agent()], registry={}) == []
    assert current_state(path) == []


def test_stuck_now_counts_an_unmanaged_waiting_session_separately():
    """Skipping it silently is how the operator never learns the dispatch path stopped registering
    its workers — measured live on 2026-09-09, all three frozen workers were unregistered."""
    stuck, unmanaged = stuck_now([_agent()], {}, _probe(), NOW, STALE)

    assert stuck == {}
    assert unmanaged == 1


# --- 6. the filed record does not arrive pre-marked as needing a human -------------------------

def test_the_filed_record_is_open_and_untiered_not_needs_human():
    """A stuck session is the manager's problem first. Three of the four frozen on 2026-09-09 the
    manager could and did unblock itself; exactly one needed the CTO."""
    path = _tmp_queue()
    _scan(path, [_agent()])

    rec = current_state(path)[0]
    assert rec["status"] == "open"
    assert rec["tier"] is None
    assert rec["decided_by"] is None
    assert rec["answer"] is None


def test_the_kind_routes_to_the_manager_not_to_a_human():
    tier, reason = classify(new_record("sess-8317", KIND, "frozen at a prompt"))

    assert tier == "tier2", reason
    assert "unknown" not in reason


def test_the_record_offers_no_fixed_options():
    """manager.validate_decision rejects an answer outside a non-empty `options` list. The remedy
    for a freeze is open-ended — swap the command, run a step on its behalf, tell it to finish
    reporting — so a fixed menu would force a wrong pick or a needless punt to a human."""
    path = _tmp_queue()
    _scan(path, [_agent()])

    assert current_state(path)[0]["options"] == []


def test_the_new_kind_does_not_shout():
    """Recognising more kinds must never widen what looks urgent."""
    from board_state import escalation_severity

    assert escalation_severity(new_record("s", KIND, "q")) == "P2"


# --- the stuck window ------------------------------------------------------------------------

def test_a_session_that_only_just_stopped_moving_is_not_stuck_yet():
    """One cadence of silence is sampling noise, not a freeze."""
    path = _tmp_queue()

    assert _scan(path, [_agent()], probe=_probe(quiet_for=60.0)) == []


def test_an_unsized_stuck_window_files_nothing():
    """None means the cadence could not be read. A guessed baseline is exactly the failure this
    repo already shipped once."""
    path = _tmp_queue()

    assert _scan(path, [_agent()], stale_after=None) == []
    assert current_state(path) == []


# --- the pending command ----------------------------------------------------------------------

def test_last_tool_use_reads_the_command_that_raised_the_prompt():
    lines = [
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"npm test"}}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"rm -rf node_modules/.vite"}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result"}]}}',
    ]

    assert last_tool_use(lines) == "Bash: rm -rf node_modules/.vite"


def test_last_tool_use_survives_a_torn_or_odd_transcript():
    """Transcript tails are truncated mid-write all the time, and a non-Bash tool has no
    `command` key at all."""
    assert last_tool_use(["{not json", ""]) is None
    assert last_tool_use(['{"type":"assistant","message":{"content":"plain text"}}']) is None
    got = last_tool_use(['{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"/a/b.py"}}]}}'])
    assert got.startswith("Read: ") and "/a/b.py" in got


# --- vocabulary and unit-file guards -----------------------------------------------------------

def test_the_board_glosses_every_canonical_kind():
    """The `kind` vocabulary is closed and the board renders a gloss per kind. A kind added to
    escalations.py without one shows on the board as a bare English slug."""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.html"), encoding="utf-8") as f:
        block = re.search(r"const KIND_GLOSS = \{(.*?)\};", f.read(), re.S)
    assert block, "board.html has no KIND_GLOSS map"
    for kind in CANONICAL_KINDS:
        assert re.search(rf"^\s*{kind}:", block.group(1), re.M), f"{kind} is missing from KIND_GLOSS"


def test_the_watch_unit_fires_on_its_own_grid_and_names_a_cadence_board_state_can_read():
    """The stuck window is derived from this file, not from a constant. An OnCalendar that
    timer_period_seconds() cannot parse makes the watch file nothing at all — loudly, but
    nothing — so the unit is guarded the same way board-mirror.timer is."""
    from board_state import CLAIM_STALE_MULTIPLIER, claim_stale_after, timer_period_seconds

    systemd_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "systemd")
    with open(os.path.join(systemd_dir, "stuck-session-watch.timer"), encoding="utf-8") as f:
        timer = f.read()
    with open(os.path.join(systemd_dir, "board-mirror.timer"), encoding="utf-8") as f:
        board = f.read()

    assert timer_period_seconds(timer) is not None, "the watch's own cadence must be parseable"
    offsets = [re.search(r"\*:(\d+)/", t).group(1) for t in (timer, board)]
    assert offsets[0] != offsets[1], "the watch must not fire on the same instant as board-mirror"

    # The README quotes both numbers in prose, and prose is what went stale last time. Retune the
    # unit and this fails until the doc follows.
    with open(os.path.join(systemd_dir, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    period = timer_period_seconds(timer)
    window = claim_stale_after(period)
    assert f"stuck past {window / 60:.0f}m" in readme, "the README's sample log line names a stale window"
    assert f"silent for at least {int(CLAIM_STALE_MULTIPLIER)} timer periods" in readme


def test_the_unit_path_survives_being_reached_through_a_symlink():
    """bin/systemd/README.md installs this script as a symlink under ~/.config. `abspath` keeps
    the symlink's own directory, which has no `systemd/` beside it — so every scheduled run
    failed to size its window while running it by hand from the repo worked."""
    link_dir = tempfile.mkdtemp(prefix="stuck-symlink-")
    link = os.path.join(link_dir, "stuck_sessions.py")
    os.symlink(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stuck_sessions.py"), link)

    assert _unit_path(link) == _unit_path()
    assert os.path.exists(_unit_path(link))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
