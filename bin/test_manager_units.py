#!/usr/bin/env python3
"""assert-based checks for the resident-manager systemd units.

Run: python3 bin/test_manager_units.py

Same shape as test_board_timer.py: read the shipped unit files as text and pin the properties an
operator cannot see from `systemctl status` — the PATH trap, the cadence/timeout relationship, and
that README.md's install commands name the exact paths the units execute.
"""

import os
import re

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEMD_DIR = os.path.join(BIN_DIR, "systemd")

# The units this file owns. board-mirror/stuck-session-watch predate it and are covered by
# test_board_timer.py / test_stuck_sessions.py.
MANAGER_SERVICES = ("manager-keepalive.service", "manager-daemon.service")
MANAGER_TIMERS = ("manager-keepalive.timer", "manager-daemon.timer")


def _read(*parts):
    with open(os.path.join(SYSTEMD_DIR, *parts), encoding="utf-8") as f:
        return f.read()


def _cadence_seconds(timer_text):
    m = re.search(r"^OnCalendar=.*\*:\d+/(\d+):", timer_text, re.MULTILINE)
    assert m, "timer must set a '*:<offset>/<n>:00' OnCalendar step"
    return int(m.group(1)) * 60


def test_manager_units_put_local_bin_on_path():
    # The trap this repo has already paid for twice (run-board-mirror.sh's CLAUDE_BIN default,
    # manager_session.claude_bin()): a `systemd --user` unit inherits the user manager's PATH,
    # which does NOT contain ~/.local/bin — where `cmew` and `claude` both live. These two units
    # execute scripts they do not own (parallel-task.sh, manager_daemon.py), so neither can be
    # fixed from the inside the way those two were; the unit has to carry PATH itself.
    for name in MANAGER_SERVICES:
        text = _read(name)
        m = re.search(r"^Environment=PATH=(.+)$", text, re.MULTILINE)
        assert m, f"{name} must set Environment=PATH — a --user unit does not inherit ~/.local/bin"
        entries = m.group(1).split(":")
        assert "%h/.local/bin" in entries, (
            f"{name}'s PATH has no %h/.local/bin, so `cmew`/`claude` are not resolvable: {entries}"
        )
        # /usr/bin too: `tmux` lives there, and a tmux server first started BY this unit hands
        # its own environment to every session it later spawns.
        assert "/usr/bin" in entries, f"{name}'s PATH is missing /usr/bin (tmux): {entries}"


def test_manager_start_runs_inside_the_repo_whose_registry_it_writes():
    # parallel-task.sh resolves REPO_ROOT with `git rev-parse --show-toplevel` of the CWD, so the
    # working directory — not the script's location — decides which .parallel-registry.json
    # manager-start records the manager's agent name into. A systemd unit's CWD is `/`.
    service = _read("manager-keepalive.service")
    assert "PWT_REPO_ROOT" in service, (
        "manager-keepalive.service must cd into $PWT_REPO_ROOT before running manager-start"
    )
    assert re.search(r"^EnvironmentFile=", service, re.MULTILINE), (
        "PWT_REPO_ROOT comes from the shared board-mirror env file, not from a second copy here"
    )
    # WorkingDirectory= cannot read an EnvironmentFile variable, so a WorkingDirectory line here
    # could only be a hardcoded checkout path — the drift this deliberately avoids.
    assert not re.search(r"^WorkingDirectory=", service, re.MULTILINE)


def test_manager_units_hardcode_no_checkout_path():
    # Every path is a %h specifier or a symlink under ~/.config, so the unit files survive the
    # repo moving. Same rule test_board_timer.py::test_no_real_secrets_committed enforces.
    for name in MANAGER_SERVICES + MANAGER_TIMERS:
        text = _read(name)
        assert "/home/" not in text, f"{name} hardcodes a home directory instead of using %h"
        assert "projects/claude-parallel-worktree-plugin" not in text, (
            f"{name} hardcodes where this repo happens to be checked out"
        )


def test_service_timeouts_stay_under_their_timer_cadence():
    # A run allowed to outlive its own cadence queues the next fire up behind it. The daemon one
    # matters most: if a rework ever reinstates an internal `while True`, this timeout is what
    # turns "unit blocks forever" into a loud status=1/FAILURE.
    for service_name in MANAGER_SERVICES:
        timer_name = service_name.replace(".service", ".timer")
        cadence = _cadence_seconds(_read(timer_name))
        m = re.search(r"^TimeoutStartSec=(\d+)$", _read(service_name), re.MULTILINE)
        assert m, f"{service_name} must set TimeoutStartSec"
        assert int(m.group(1)) < cadence, (
            f"{service_name} TimeoutStartSec={m.group(1)} is not under its {cadence}s cadence"
        )


def test_every_timer_in_this_directory_fires_on_its_own_offset():
    # Four 5-minute jobs now share this directory and two of them drive the `claude` CLI. Landing
    # two on the same instant is how they start contending; each timer's comment names the offsets
    # the others took, and this is what keeps those comments true.
    offsets = {}
    for name in sorted(os.listdir(SYSTEMD_DIR)):
        if not name.endswith(".timer"):
            continue
        m = re.search(r"^OnCalendar=.*\*:(\d+)/(\d+):", _read(name), re.MULTILINE)
        assert m, f"{name} must set a '*:<offset>/<n>:00' OnCalendar step"
        offset, step = int(m.group(1)), int(m.group(2))
        assert offset % step != 0, f"{name} lands on the round {step}-minute marks"
        assert offset not in offsets, f"{name} fires on the same minute as {offsets[offset]}"
        offsets[offset] = name
    assert len(offsets) >= 4, f"expected every shipped timer to be checked, saw {offsets}"


def test_timers_are_installable_and_run_as_the_logged_in_user():
    for name in MANAGER_TIMERS:
        text = _read(name)
        assert re.search(r"^WantedBy=timers\.target$", text, re.MULTILINE), (
            f"{name} has no [Install] WantedBy — `systemctl --user enable` would refuse it"
        )
        # Persistent=true would fire a catch-up burst on wake; neither job has anything to replay.
        assert re.search(r"^Persistent=false$", text, re.MULTILINE), f"{name} must set Persistent=false"
    for name in MANAGER_SERVICES:
        text = _read(name)
        assert re.search(r"^Type=oneshot$", text, re.MULTILINE), (
            f"{name} must be a plain oneshot unit — that is what makes systemd coalesce an "
            f"overlapping fire into a no-op instead of starting a second run"
        )
        assert "User=" not in text  # a --user unit: it runs as whoever owns the session
        assert not os.path.exists(os.path.join(SYSTEMD_DIR, name.replace(".", "@."))), (
            f"a templated {name} would allow concurrent instances and defeat that guard"
        )


def test_readme_installs_exactly_the_paths_the_units_execute():
    # The drift that actually bites an operator: the unit executes
    # ~/.config/board-mirror/<name>, README tells them to symlink some other filename there, and
    # the unit fails with a bare "No such file or directory" that names a path nothing in the repo
    # mentions. Every ~/.config path a unit references must appear in an install command.
    readme = _read("README.md")
    for name in MANAGER_SERVICES + MANAGER_TIMERS:
        assert name in readme, f"README.md never mentions {name}"
        for referenced in re.findall(r"%h(/\.config/[^\s'\"]+)", _read(name)):
            assert referenced in readme, f"{name} executes ~{referenced}, but README.md never says to create it"


def test_worker_doc_tells_a_blocked_worker_where_to_send_the_block():
    # The failure this closes: a worker asked a question and sat idle for an hour because no
    # channel was documented. The escalation section must survive an edit, and must keep naming
    # the two things that make it actually work — the exact agent name, read from ListAgents.
    doc_path = os.path.join(os.path.dirname(BIN_DIR), "docs", "evidence-report-template.md")
    with open(doc_path, encoding="utf-8") as f:
        doc = f.read()
    assert "SendMessage" in doc, "the worker doc must name the channel a blocked worker uses"
    assert "ListAgents" in doc, (
        "the doc must tell the worker to READ the manager's exact name — cmew renames sessions "
        "for display, and SendMessage refuses a name that is off by an emoji"
    )
    assert "Manager 🔹" in doc, "show the shape of the real name, not just the bare word"


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
