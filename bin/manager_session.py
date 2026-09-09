#!/usr/bin/env python3
"""The one serialized door to the persistent Engineering Manager session.

Everything reaching the manager — the CTO's chat, escalations, worker-completion and tick wakes —
goes through ask() or ask_result(). One session means one memory: the manager knows what it
dispatched, what it decided, and what the CTO told it. An flock serializes callers, because two
processes resuming the same session would race on its transcript.
"""

import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time

HERMES_DIR = os.path.expanduser("~/.claude/hermes")
STATE_PATH = os.path.join(HERMES_DIR, "manager-session.json")
CHAT_PATH = os.path.join(HERMES_DIR, "manager-chat.jsonl")
LOCK_PATH = os.path.join(HERMES_DIR, "manager.lock")
CHARTER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills",
    "engineering-manager",
    "SKILL.md",
)

MANAGER_MODEL = os.environ.get("PWT_MANAGER_MODEL", "claude-opus-5")
MANAGER_EFFORT = os.environ.get("PWT_MANAGER_EFFORT", "max")

# What the CTO may pick for the CHAT path. The daemon's own ticks keep MANAGER_MODEL/EFFORT
# above: a tick does real work unattended and nobody is waiting on it, while a chat turn has a
# person watching a spinner — opus-5 at max effort took 4m30s to answer one word.
MANAGER_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001", "claude-fable-5-1")
MANAGER_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # --effort's own vocabulary
PREFS_PATH = os.path.expanduser("~/.claude/hermes/manager-prefs.json")


def read_prefs(path: str = None) -> dict:
    """The chat path's model/effort. Falls back to the env defaults for anything missing or
    unrecognised — a hand-edited file must never be able to hand `claude` an argument it will
    reject, which would break every chat turn with no way to fix it from the UI."""
    prefs = {}
    try:
        with open(path or PREFS_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            prefs = loaded
    except (OSError, ValueError):
        pass
    model = prefs.get("model")
    effort = prefs.get("effort")
    return {
        "model": model if model in MANAGER_MODELS else MANAGER_MODEL,
        "effort": effort if effort in MANAGER_EFFORTS else MANAGER_EFFORT,
    }


def write_prefs(model: str, effort: str, path: str = None) -> dict:
    """Persist a validated choice. Raises ValueError on anything outside the two vocabularies."""
    if model not in MANAGER_MODELS:
        raise ValueError(f"unknown model {model!r}")
    if effort not in MANAGER_EFFORTS:
        raise ValueError(f"unknown effort {effort!r}")
    target = path or PREFS_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    prefs = {"model": model, "effort": effort}
    # Write-then-rename, into a temp file UNIQUE per writer. A fixed "<target>.tmp" is not
    # safe here: changing model and effort fires two saves back to back, and both wrote the
    # same path — one interleaved the other's bytes and the survivor was
    # `{"model": ..., "effort": "low"}m"}`, which read_prefs could only discard back to the
    # defaults. mkstemp in the target's own directory keeps os.replace atomic (same
    # filesystem) while giving each writer its own file, so concurrent saves are
    # last-writer-wins instead of corrupting.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".", prefix=".prefs-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        os.replace(tmp, target)
    except BaseException:
        # Never leave the scratch file behind on a failed save.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return prefs


def resolve_repo_root(argv_repo: str | None = None, env=None, cwd_fn=os.getcwd) -> str:
    """PWT_REPO_ROOT (from `env`) first, then an explicit repo dir an entry point resolved from
    its own argv, then `cwd_fn()`.

    dashboard.py and manager_daemon.py both feed the SAME manager session — if they used
    different precedence here, the manager's `claude` subprocess (and the Claude Code permissions
    it resolves from cwd) would depend on which of the two last woke it, not on anything the
    operator configured. The daemon takes no repo argument of its own, so calling this with
    argv_repo=None there collapses it to PWT_REPO_ROOT-or-cwd.
    """
    env = env if env is not None else os.environ
    return env.get("PWT_REPO_ROOT") or argv_repo or cwd_fn()


# Claude Code resolves permission settings from the subprocess's cwd, so the manager's tool
# access silently depends on where it runs, not on anything in this file. dashboard.py and
# manager_daemon.py both resolve this the same way, by calling resolve_repo_root() above.
REPO_ROOT = resolve_repo_root()

# ponytail: global lock, per-account locks if throughput matters. 300s = background threads never
# starve while an HTTP request waits; a short timeout drops daemon wakes with no UI retry surface.
LOCK_TIMEOUT = 300
CALL_TIMEOUT = 600

SUBPROC_ERRORS = (OSError, subprocess.SubprocessError, json.JSONDecodeError)


class ManagerBusy(RuntimeError):
    """Another caller held the lock past the timeout."""


def load_charter(path: str | None = None) -> str:
    """Read the charter. Resolved at call time so the module global stays the single source."""
    with open(path or CHARTER_PATH, encoding="utf-8") as fh:
        return fh.read()


def _read_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(session_id: str) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"session_id": session_id, "started_at": time.time()}, fh)


def reset() -> None:
    """Forget the session so the next ask() bootstraps a fresh one."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(STATE_PATH)


def append_chat(role: str, source: str, text: str) -> None:
    """One line per turn. `role` says who spoke, `source` says what prompted the exchange."""
    os.makedirs(os.path.dirname(CHAT_PATH), exist_ok=True)
    with open(CHAT_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "role": role, "source": source, "text": text}) + "\n")


def history(limit: int = 200) -> list[dict]:
    """The newest `limit` turns, oldest first. A corrupt line is skipped, never fatal."""
    try:
        with open(CHAT_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@contextlib.contextmanager
def _locked(timeout: int | None = None):
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= deadline:
                fh.close()
                raise ManagerBusy(f"manager busy: lock held longer than {timeout}s")
            time.sleep(0.2)
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def busy() -> bool:
    """True when a call is in flight. Drives the chat's thinking indicator."""
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        with open(LOCK_PATH, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def ask_argv(session_id, text: str, model: str = None, effort: str = None) -> list[str]:
    """Argv for one manager turn.

    Model and effort go on every call, resumes included: --effort applies to the invocation, not to
    the stored session, so omitting it on a resume silently downgrades the manager.
    """
    argv = [
        "claude",
        "--model", model or MANAGER_MODEL,
        "--effort", effort or MANAGER_EFFORT,
        "--output-format", "json",
    ]
    if session_id:
        argv += ["--resume", session_id]
    return argv + ["-p", "--", text]


def _log_quietly(role: str, source: str, text: str) -> None:
    """Append a chat entry, never raising — a failure to log must not mask the failure it records.

    Still swallowed on purpose, but named on stderr: a silently broken CHAT_PATH (disk full,
    permissions, a path collision) would otherwise stop every turn from being recorded with no
    signal anywhere, leaving an idle manager indistinguishable from a broken log.
    """
    try:
        append_chat(role, source, text)
    except OSError as e:
        print(f"_log_quietly: could not append to chat log: {e}", file=sys.stderr)


def _cli_error_detail(e: subprocess.CalledProcessError) -> str:
    """The reason the CLI actually gave, wherever it put it.

    `claude -p --output-format json` reports a failed run by exiting non-zero with an EMPTY
    stderr and a well-formed JSON object on stdout carrying `is_error` and a human-readable
    `result` — "Failed to authenticate: OAuth session expired and could not be refreshed" was
    the live case. Reading only stderr therefore threw away the one actionable sentence and
    left the operator with "không có stderr", which says nothing and cannot be acted on.

    stdout first, then stderr, because stdout is where this CLI puts it; unparseable output
    falls back to its last line rather than being dropped.
    """
    raw = (e.stdout or "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("result") or payload.get("error")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()[:200]
        # Not JSON, or JSON without a message: the last line is still better than nothing.
        return raw.splitlines()[-1][:200]
    tail = (e.stderr or "").strip().splitlines()
    return tail[-1][:200] if tail else "không có stderr"


def _failure_note(e: Exception) -> str:
    """A failure note the CTO can act on, with no prompt in it.

    CalledProcessError and TimeoutExpired both stringify by embedding the whole argv, and argv
    carries the charter plus the caller's text — so an f"{e}" here dumps the entire system prompt
    into the chat panel as the manager's reply. Report the exit status and the last line of
    stderr instead; the prompt is never the operator's problem.
    """
    if isinstance(e, subprocess.CalledProcessError):
        return f"manager call failed: claude thoát với mã {e.returncode} — {_cli_error_detail(e)}"
    if isinstance(e, subprocess.TimeoutExpired):
        return f"manager call failed: không có phản hồi trong {e.timeout:.0f}s"
    return f"manager call failed: {type(e).__name__}: {e}"


def _ask_locked(text: str, source: str, run, timeout: int, model=None, effort=None) -> tuple[bool, str]:
    """One turn, with the lock already held. Returns (ok, text) on every path."""
    _log_quietly("cto" if source == "cto" else "system", source, text)
    try:
        session_id = _read_state().get("session_id")
        prompt = text if session_id else f"{load_charter()}\n\n---\n\n{text}"
    except OSError as e:
        reply = f"manager call failed before dispatch: {e}"
        _log_quietly("manager", source, reply)
        return False, reply

    try:
        proc = run(
            ask_argv(session_id, prompt, model, effort),
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            cwd=REPO_ROOT,
        )
        payload = json.loads(proc.stdout)
        if not isinstance(payload, dict):
            raise ValueError(f"expected a JSON object, got {type(payload).__name__}")
    except SUBPROC_ERRORS + (ValueError,) as e:
        reply = _failure_note(e)
        _log_quietly("manager", source, reply)
        return False, reply

    reply = payload.get("result") or ""
    if not session_id:
        new_id = payload.get("session_id")
        if not new_id:
            _log_quietly(
                "manager",
                source,
                "WARNING: the manager replied without a session id — the next turn will start a "
                "new session and lose this context",
            )
        else:
            try:
                _write_state(new_id)
            except OSError as e:
                _log_quietly(
                    "manager",
                    source,
                    f"WARNING: replied but could not save the session id ({e}) — the next turn "
                    "will start a new session and lose this context",
                )
    _log_quietly("manager", source, reply)
    return True, reply


def ask_result(
    text: str,
    source: str,
    run=subprocess.run,
    timeout: int = CALL_TIMEOUT,
    model: str = None,
    effort: str = None,
) -> tuple[bool, str]:
    """One turn. Returns (ok, text) — ok is False when the call failed and text is the failure note.

    Any caller that parses the manager's output as a contract MUST use this rather than ask():
    a failure note is ordinary text, and a subprocess error's string embeds the whole prompt,
    so a parser handed one can extract worker-authored content out of it as if it were a reply.
    """
    try:
        with _locked():
            return _ask_locked(text, source, run, timeout, model, effort)
    except ManagerBusy:
        _log_quietly("manager", source, "manager busy — this turn was not delivered")
        raise


def ask(
    text: str,
    source: str,
    run=subprocess.run,
    timeout: int = CALL_TIMEOUT,
    model: str = None,
    effort: str = None,
) -> str:
    """The manager's reply, or a note describing why there wasn't one. For display only —
    parsers use ask_result(), which can tell those two apart."""
    return ask_result(text, source, run, timeout, model, effort)[1]
