#!/usr/bin/env python3
"""Textual checks that run-board-mirror.sh wires in ado_state_sync.py the way Part 3 requires:
after the mirror step (so it reads documents this same run just wrote), and dry-run unless an
operator has explicitly opted in.
"""

import pathlib

SCRIPT = (pathlib.Path(__file__).parent / "systemd" / "run-board-mirror.sh").read_text(encoding="utf-8")


def test_ado_state_sync_is_invoked():
    assert "ado_state_sync.py" in SCRIPT


def test_ado_state_sync_runs_after_the_mirror_loop_so_it_sees_fresh_documents():
    """"AFTER the mirror step" — after the batch loop has finished writing (and checkpointing)
    this run's documents, not interleaved with it and not before."""
    loop_end = SCRIPT.index('DONE_BATCHES=$((DONE_BATCHES + 1))')
    sync_call = SCRIPT.index("ado_state_sync.py")
    assert sync_call > loop_end, "ado_state_sync.py must run after the batch loop, not before/inside it"


def test_ado_state_sync_defaults_to_dry_run():
    """--apply must be gated behind an explicit opt-in, never passed unconditionally — the first
    run of a thing that mutates ADO must not mutate anything."""
    assert "--apply" not in SCRIPT.split("ado_state_sync.py")[0], (
        "no earlier unconditional --apply before the invocation"
    )
    call_line = next(line for line in SCRIPT.splitlines() if "ado_state_sync.py" in line and "python3" in line)
    assert "--apply" not in call_line, "ado_state_sync.py must not be called with a bare --apply"


def test_ado_state_sync_is_non_fatal():
    """A state-sync problem must not take down a mirror run that already succeeded."""
    idx = SCRIPT.index("ado_state_sync.py")
    following = SCRIPT[idx:idx + 400]
    assert "non-fatal" in following.lower() or "exit 1" not in following.split("\n\n")[0]
