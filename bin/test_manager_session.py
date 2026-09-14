#!/usr/bin/env python3
"""assert-based checks for the persistent manager session. Run: python3 bin/test_manager_session.py"""

import contextlib
import io
import json
import os as _os
import subprocess
import tempfile
import threading

import manager_session as ms

_CHARTER_BACKUP = ms.CHARTER_PATH
# Captured before any test can reassign ms.REPO_ROOT — the true import-time default, computed
# with whatever PWT_REPO_ROOT was (or wasn't) set in this process's environment.
_REPO_ROOT_AT_IMPORT = ms.REPO_ROOT


class _Recorder:
    """Stand-in for subprocess.run that records argv and replays a scripted reply."""

    def __init__(self, session_id="sess-1", result="ok", raises=None):
        self.session_id = session_id
        self.result = result
        self.raises = raises
        self.calls = []
        self.kwargs = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        if self.raises:
            raise self.raises
        payload_dict = {"result": self.result}
        if self.session_id is not None:
            payload_dict["session_id"] = self.session_id
        payload = json.dumps(payload_dict)
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")


def _isolate():
    """Point every state path at a fresh temp dir and return it."""
    d = tempfile.mkdtemp()
    ms.STATE_PATH = _os.path.join(d, "state.json")
    ms.CHAT_PATH = _os.path.join(d, "chat.jsonl")
    ms.LOCK_PATH = _os.path.join(d, "lock")
    return d


def test_argv_bootstraps_without_resume():
    argv = ms.ask_argv(None, "hello")
    assert "--resume" not in argv
    assert argv[-3:] == ["-p", "--", "hello"]
    assert "--model" in argv and "--effort" in argv


def test_argv_resumes_with_a_session_id():
    argv = ms.ask_argv("sess-9", "hello")
    assert argv[argv.index("--resume") + 1] == "sess-9"


def test_effort_and_model_are_sent_on_every_call():
    """--effort applies per invocation, so a resume that omits it silently downgrades."""
    for sid in (None, "sess-9"):
        argv = ms.ask_argv(sid, "x")
        assert argv[argv.index("--model") + 1] == ms.MANAGER_MODEL
        assert argv[argv.index("--effort") + 1] == ms.MANAGER_EFFORT


def test_argv_separates_the_prompt_so_a_leading_dash_is_not_read_as_a_flag():
    """-p is a boolean flag and the prompt is positional; the charter starts with '---'."""
    argv = ms.ask_argv(None, "--- charter starts like this")
    assert argv[-3:] == ["-p", "--", "--- charter starts like this"]
    assert argv.index("--") > argv.index("--model"), "every flag must precede the separator"


def test_bootstrap_saves_the_session_id_and_prepends_the_charter():
    _isolate()
    run = _Recorder(session_id="fresh-1")
    reply = ms.ask("first message", "cto", run=run)
    assert reply == "ok"
    assert json.load(open(ms.STATE_PATH))["session_id"] == "fresh-1"
    sent = run.calls[0][-1]
    assert "first message" in sent
    assert "Engineering Manager" in sent, "charter must be prepended to the first message"


def test_second_call_resumes_and_does_not_resend_the_charter():
    _isolate()
    run = _Recorder(session_id="fresh-1")
    ms.ask("first", "cto", run=run)
    ms.ask("second", "cto", run=run)
    second = run.calls[1]
    assert "--resume" in second
    assert second[-1] == "second", "the charter must not be resent on a resume"


def test_ask_passes_cwd_as_the_resolved_repo_root():
    """Claude Code resolves permission settings from cwd — the manager's subprocess must run in
    the target repo, not wherever the caller happened to launch the dashboard/daemon from."""
    _isolate()
    d = tempfile.mkdtemp()
    ms.REPO_ROOT = d
    run = _Recorder()
    ms.ask("x", "cto", run=run)
    assert run.kwargs[0]["cwd"] == d


def test_reassigning_repo_root_changes_the_next_calls_cwd():
    _isolate()
    run = _Recorder(session_id="s")
    ms.REPO_ROOT = "/repo-a"
    ms.ask("first", "cto", run=run)
    ms.REPO_ROOT = "/repo-b"
    ms.ask("second", "cto", run=run)
    assert run.kwargs[0]["cwd"] == "/repo-a"
    assert run.kwargs[1]["cwd"] == "/repo-b"


def test_repo_root_falls_back_to_cwd_when_the_env_var_is_unset():
    assert "PWT_REPO_ROOT" not in _os.environ, "test env must not already override this"
    assert _os.getcwd() == _REPO_ROOT_AT_IMPORT


def test_resolve_repo_root_prefers_env_over_argv_and_cwd():
    """PWT_REPO_ROOT must win even when an entry point also resolved an explicit argv repo —
    dashboard.py used to let its own argv-or-cwd value win unconditionally, ignoring the env var
    entirely. `env`/`cwd_fn` are injected rather than mutating the real os.environ/os.getcwd, so
    this needs no monkeypatch-and-restore."""
    resolved = ms.resolve_repo_root("/argv/repo", env={"PWT_REPO_ROOT": "/env/repo"}, cwd_fn=lambda: "/cwd/repo")
    assert resolved == "/env/repo"


def test_resolve_repo_root_falls_back_to_argv_when_env_is_unset():
    resolved = ms.resolve_repo_root("/argv/repo", env={}, cwd_fn=lambda: "/cwd/repo")
    assert resolved == "/argv/repo"


def test_resolve_repo_root_falls_back_to_cwd_when_neither_env_nor_argv_is_set():
    """This is manager_daemon.py's own case exactly: it calls resolve_repo_root() with no argv
    repo of its own (it takes no repo argument and serves no repo)."""
    resolved = ms.resolve_repo_root(None, env={}, cwd_fn=lambda: "/cwd/repo")
    assert resolved == "/cwd/repo"


def test_resolve_repo_root_uses_the_real_environ_and_cwd_by_default():
    """The injected env/cwd_fn parameters must not change the function's real-world behaviour —
    calling it exactly as both main()s do (module defaults only) still reads the process's own
    environment and working directory."""
    assert "PWT_REPO_ROOT" not in _os.environ, "test env must not already override this"
    assert ms.resolve_repo_root() == _os.getcwd()


def test_dashboard_and_daemon_entry_points_agree_on_repo_root_when_env_is_set():
    """The bug this pins: dashboard.py's main() used to ignore PWT_REPO_ROOT entirely and always
    win with its own argv-or-cwd value, so with the env var set the CTO's chat (dashboard.py) and
    the daemon's tick/escalation/wake turns (manager_daemon.py) resolved manager_session.REPO_ROOT
    to two different directories — the same manager session, run from two working directories,
    depending only on which entry point last woke it. Both entry points now call this one function
    with the same env, so they cannot disagree by construction; this proves it with dashboard.py's
    own argv-parsing helper feeding it, exactly as dashboard.main() does."""
    import dashboard

    env = {"PWT_REPO_ROOT": "/env/repo"}
    dashboard_argv_repo = dashboard._argv_repo_dir(["4400", "/some/other/repo"])
    dashboard_resolved = ms.resolve_repo_root(dashboard_argv_repo, env=env, cwd_fn=lambda: "/cwd/repo")
    daemon_resolved = ms.resolve_repo_root(env=env, cwd_fn=lambda: "/cwd/repo")  # daemon has no argv of its own
    assert dashboard_resolved == daemon_resolved == "/env/repo"


def test_both_turns_reach_the_chat_log_with_their_source():
    _isolate()
    ms.ask("wake up", "daemon:tick", run=_Recorder(result="on it"))
    entries = ms.history()
    assert [e["role"] for e in entries] == ["system", "manager"]
    assert all(e["source"] == "daemon:tick" for e in entries)
    assert entries[1]["text"] == "on it"


def test_a_cto_turn_is_logged_as_cto():
    _isolate()
    ms.ask("status?", "cto", run=_Recorder())
    assert ms.history()[0]["role"] == "cto"


def test_a_failing_call_is_recorded_not_swallowed():
    _isolate()
    reply = ms.ask("x", "cto", run=_Recorder(raises=subprocess.TimeoutExpired("claude", 600)))
    assert "failed" in reply.lower()
    entries = ms.history()
    assert len(entries) == 2
    assert entries[0]["text"] == "x"
    assert entries[-1]["role"] == "manager"
    assert "failed" in entries[-1]["text"].lower()


def test_a_failure_note_reports_status_not_the_whole_charter_and_argv():
    """CalledProcessError/TimeoutExpired stringify by embedding the whole argv, and the argv
    carries the charter prepended to the prompt — an f"{e}" here used to dump the entire system
    prompt into the chat panel as the manager's reply (observed: two 6,397-character entries)."""
    _isolate()

    def blows_up(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv, output="", stderr="no conversation found\n")

    reply = ms.ask("do the thing", "cto", run=blows_up)
    charter_first_line = ms.load_charter().splitlines()[0]
    assert charter_first_line not in reply
    assert "--model" not in reply
    assert len(reply) < 500, "a failure note must not dump the whole command"


def test_a_failing_call_releases_the_lock():
    _isolate()
    ms.ask("x", "cto", run=_Recorder(raises=OSError("boom")))
    assert ms.busy() is False


def test_busy_is_false_when_idle():
    _isolate()
    assert ms.busy() is False


def test_reset_drops_the_session_so_the_next_call_bootstraps():
    _isolate()
    run = _Recorder(session_id="a")
    ms.ask("one", "cto", run=run)
    ms.reset()
    assert not _os.path.exists(ms.STATE_PATH)
    run2 = _Recorder(session_id="b")
    ms.ask("two", "cto", run=run2)
    assert "--resume" not in run2.calls[0]
    assert json.load(open(ms.STATE_PATH))["session_id"] == "b"


def test_history_respects_its_limit_and_returns_the_newest():
    _isolate()
    run = _Recorder()
    for i in range(5):
        ms.ask(f"m{i}", "cto", run=run)
    tail = ms.history(limit=3)
    assert len(tail) == 3
    assert tail[-1]["role"] == "manager"


def test_charter_has_its_load_bearing_sections():
    text = ms.load_charter()
    for heading in ("## The eight actions", "## Routing work", "## Hard boundaries"):
        assert heading in text, heading


def test_a_broken_chat_log_does_not_cost_the_caller_its_reply():
    d = _isolate()
    blocker = _os.path.join(d, "blocker")
    open(blocker, "w").close()
    ms.CHAT_PATH = _os.path.join(blocker, "chat.jsonl")
    reply = ms.ask("x", "cto", run=_Recorder())
    assert reply == "ok", "a broken chat log must not cost the caller its reply"


def test_log_quietly_swallows_and_names_the_failure_on_stderr():
    """_log_quietly must still swallow the OSError (the end-to-end swallow is already proven by
    the test above) but must not do so in total silence — an operator watching an empty chat
    panel otherwise has no way to tell an idle manager from a broken log."""
    d = _isolate()
    blocker = _os.path.join(d, "blocker2")
    open(blocker, "w").close()
    ms.CHAT_PATH = _os.path.join(blocker, "chat.jsonl")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        ms._log_quietly("manager", "cto", "hello")  # must not raise
    assert buf.getvalue().strip() != "", "a broken chat log must still be named on stderr"


def test_a_failed_state_write_does_not_discard_the_reply():
    d = _isolate()
    blocker = _os.path.join(d, "sblock")
    open(blocker, "w").close()
    ms.STATE_PATH = _os.path.join(blocker, "state.json")
    reply = ms.ask("x", "cto", run=_Recorder())
    assert reply == "ok", "a bookkeeping failure must not discard a reply already received"
    assert any("could not save the session id" in e["text"] for e in ms.history())


def test_a_second_caller_gets_manager_busy_while_the_first_holds_the_lock():
    _isolate()
    ms.LOCK_TIMEOUT = 1
    started, release = threading.Event(), threading.Event()

    def slow_run(argv, **kwargs):
        started.set()
        release.wait(5)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"session_id": "s", "result": "ok"}), stderr="")

    first = threading.Thread(target=ms.ask, args=("first", "cto"), kwargs={"run": slow_run})
    first.start()
    try:
        assert started.wait(5), "the first call never reached the subprocess"
        assert ms.busy() is True
        try:
            ms.ask("second", "cto", run=_Recorder())
        except ms.ManagerBusy:
            pass
        else:
            raise AssertionError("a second caller should have hit ManagerBusy")
        release.set()
        first.join(5)
        assert ms.busy() is False
    finally:
        ms.LOCK_TIMEOUT = 300


def test_a_busy_timeout_is_recorded_before_it_raises():
    _isolate()
    ms.LOCK_TIMEOUT = 1
    started, release = threading.Event(), threading.Event()

    def slow_run(argv, **kwargs):
        started.set()
        release.wait(5)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"session_id": "s", "result": "ok"}), stderr="")

    first = threading.Thread(target=ms.ask, args=("first", "cto"), kwargs={"run": slow_run})
    first.start()
    try:
        assert started.wait(5)
        try:
            ms.ask("lost one", "cto", run=_Recorder())
        except ms.ManagerBusy:
            pass
        release.set()
        first.join(5)
        assert any("busy" in e["text"] for e in ms.history()), "a dropped turn must be visible in the log"
    finally:
        ms.LOCK_TIMEOUT = 300


def test_a_reply_without_a_session_id_warns_instead_of_silently_rebootstrapping():
    _isolate()
    run = _Recorder()
    run.session_id = None
    reply = ms.ask("first", "cto", run=run)
    assert reply == "ok"
    assert any("without a session id" in e["text"] for e in ms.history())


def test_a_non_dict_payload_is_reported_not_raised():
    _isolate()

    def weird(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="[1, 2, 3]", stderr="")

    reply = ms.ask("x", "cto", run=weird)
    assert "failed" in reply.lower()
    assert ms.history()[-1]["role"] == "manager"


def test_a_missing_charter_is_reported_not_raised():
    d = _isolate()
    ms.CHARTER_PATH = _os.path.join(d, "nope.md")
    try:
        reply = ms.ask("x", "cto", run=_Recorder())
        assert "failed" in reply.lower()
        assert ms.history()[0]["text"] == "x", "the incoming turn must survive the failure"
    finally:
        ms.CHARTER_PATH = _CHARTER_BACKUP


def test_ask_result_flags_a_failure_so_a_parser_cannot_mistake_it_for_a_reply():
    _isolate()
    ok, text = ms.ask_result("x", "cto", run=_Recorder(raises=subprocess.TimeoutExpired("claude", 600)))
    assert ok is False
    assert "failed" in text.lower()


def test_ask_result_flags_a_real_reply():
    _isolate()
    ok, text = ms.ask_result("x", "cto", run=_Recorder(result="fine"))
    assert ok is True
    assert text == "fine"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")


def test_write_prefs_survives_concurrent_saves(tmp_path):
    """Changing model and effort fires two saves back to back. Sharing one "<target>.tmp"
    between them interleaved their bytes and left
    `{"model": ..., "effort": "low"}m"}` on disk — valid-looking, unparseable, and silently
    discarded back to the defaults by read_prefs."""
    import threading

    import manager_session as ms

    path = str(tmp_path / "prefs.json")
    combos = [("claude-opus-5", "max"), ("claude-sonnet-5", "low"), ("claude-haiku-4-5-20251001", "high")]
    start = threading.Barrier(len(combos) * 4)
    errors = []

    def save(model, effort):
        try:
            start.wait(5)
            for _ in range(20):
                ms.write_prefs(model, effort, path=path)
        except Exception as e:  # noqa: BLE001 - the assertion below reports it
            errors.append(e)

    threads = [threading.Thread(target=save, args=c) for c in combos for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"a concurrent save failed: {errors[:2]}"
    # Whichever writer landed last, the file must be one of the values actually written —
    # never a blend of two, and never so broken that read_prefs falls back to the defaults.
    got = ms.read_prefs(path)
    assert (got["model"], got["effort"]) in combos, f"corrupted result: {got}"


def test_write_prefs_leaves_no_scratch_files_behind(tmp_path):
    import os

    import manager_session as ms

    path = str(tmp_path / "prefs.json")
    for _ in range(5):
        ms.write_prefs("claude-sonnet-5", "medium", path=path)
    leftovers = [n for n in os.listdir(tmp_path) if n != "prefs.json"]
    assert not leftovers, f"scratch files left behind: {leftovers}"


def test_claude_bin_is_absolute_so_a_systemd_unit_can_find_it():
    """The live defect: a systemd --user unit inherits the user manager's PATH, which has no
    ~/.local/bin, so bare "claude" raised FileNotFoundError inside board_state.py. Every caller
    degraded to an empty list, so the pump published ZERO sessions on every run — the board's
    session rows went stale, and because the mirror only deletes documents it remembers writing,
    session orphans became permanently uncleanable."""
    before = _os.environ.pop("CLAUDE_BIN", None)
    path_before = _os.environ["PATH"]
    _os.environ["PATH"] = "/usr/bin:/bin"  # a systemd --user PATH: no ~/.local/bin
    try:
        resolved = ms.claude_bin()
    finally:
        _os.environ["PATH"] = path_before
        if before is not None:
            _os.environ["CLAUDE_BIN"] = before

    assert _os.path.isabs(resolved), f"bare {resolved!r} is exactly what the unit cannot find"
    assert _os.path.basename(resolved) == "claude"


def test_claude_bin_honours_the_same_env_var_the_pump_script_already_uses():
    """run-board-mirror.sh reads $CLAUDE_BIN for its own `claude -p`. The subprocesses it spawns
    underneath must resolve the binary the same way, or the two disagree about which CLI runs."""
    before = _os.environ.get("CLAUDE_BIN")
    _os.environ["CLAUDE_BIN"] = "/opt/custom/claude"
    try:
        assert ms.claude_bin() == "/opt/custom/claude"
    finally:
        if before is None:
            _os.environ.pop("CLAUDE_BIN", None)
        else:
            _os.environ["CLAUDE_BIN"] = before
