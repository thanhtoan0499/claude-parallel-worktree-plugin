#!/usr/bin/env bash
# assert-based checks for parallel-task.sh's pure argument parsing. Run: bash bin/test_parallel_task.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/parallel-task.sh"

fail=0
assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "PASS $desc"
  else
    echo "FAIL $desc: expected [$expected] got [$actual]"
    fail=1
  fi
}

out="$(parse_start_args my-task native)"
assert_eq "no --ticket" "$(printf 'my-task\tnative\torigin/main\t[]')" "$out"

out="$(parse_start_args --ticket 8172 my-task native)"
assert_eq "one --ticket" "$(printf 'my-task\tnative\torigin/main\t["8172"]')" "$out"

out="$(parse_start_args --ticket 8172 --ticket 8165 my-task native some-ref)"
assert_eq "two --ticket + explicit base_ref" \
  "$(printf 'my-task\tnative\tsome-ref\t["8172","8165"]')" "$out"

if parse_start_args my-task 2>/dev/null; then
  echo "FAIL missing mode should return non-zero"
  fail=1
else
  echo "PASS missing mode returns non-zero"
fi

if parse_start_args my-task native --ticket 2>/dev/null; then
  echo "FAIL: trailing --ticket with no value should return non-zero"
  fail=1
else
  echo "PASS: trailing --ticket with no value returns non-zero"
fi

# --- parse_dispatch_args --------------------------------------------------

# The defaults below are opus/max on purpose — a worker that reasons badly costs a re-dispatch and
# a wrong report, which is dearer than the tokens. An operator's own PARALLEL_TASK_* would override
# them and turn every expectation here red, so this block asks for the built-in default explicitly.
unset PARALLEL_TASK_MODEL PARALLEL_TASK_EFFORT

parse_dispatch_args "do the thing"
assert_eq "dispatch: prompt only takes the standing opus/max default" "opus|max|do the thing" \
  "$DISPATCH_MODEL|$DISPATCH_EFFORT|$DISPATCH_PROMPT"

parse_dispatch_args "do it" --model opus --effort max
assert_eq "dispatch: both flags" "opus|max|do it" "$DISPATCH_MODEL|$DISPATCH_EFFORT|$DISPATCH_PROMPT"

parse_dispatch_args "do it" --model sonnet
assert_eq "dispatch: --model overrides, effort keeps its default" "sonnet|max" \
  "$DISPATCH_MODEL|$DISPATCH_EFFORT"

parse_dispatch_args "do it" --effort low
assert_eq "dispatch: --effort overrides, model keeps its default" "opus|low" \
  "$DISPATCH_MODEL|$DISPATCH_EFFORT"

parse_dispatch_args "$(printf 'line one\nline two')" --effort high
assert_eq "dispatch: multi-line prompt survives" "$(printf 'line one\nline two')" "$DISPATCH_PROMPT"

parse_dispatch_args --effort max "flag came first"
assert_eq "dispatch: flag order does not matter" "max|flag came first" \
  "$DISPATCH_EFFORT|$DISPATCH_PROMPT"

if parse_dispatch_args "do it" --effort turbo 2>/dev/null; then
  echo "FAIL: unknown effort should return non-zero"; fail=1
else
  echo "PASS: unknown effort returns non-zero"
fi

if parse_dispatch_args "do it" --effort 2>/dev/null; then
  echo "FAIL: --effort with no value should return non-zero"; fail=1
else
  echo "PASS: --effort with no value returns non-zero"
fi

if parse_dispatch_args "do it" --model 2>/dev/null; then
  echo "FAIL: --model with no value should return non-zero"; fail=1
else
  echo "PASS: --model with no value returns non-zero"
fi

if parse_dispatch_args --model opus 2>/dev/null; then
  echo "FAIL: dispatch with no prompt should return non-zero"; fail=1
else
  echo "PASS: dispatch with no prompt returns non-zero"
fi

parse_dispatch_args "exactly one quoted prompt with --model inside it"
assert_eq "dispatch: a quoted prompt containing a flag word stays intact" \
  "opus|exactly one quoted prompt with --model inside it" "$DISPATCH_MODEL|$DISPATCH_PROMPT"

if parse_dispatch_args unquoted prompt words 2>/dev/null; then
  echo "FAIL: a multi-word unquoted prompt should return non-zero"; fail=1
else
  echo "PASS: a multi-word unquoted prompt returns non-zero"
fi

# --- cmd_dispatch argv building -----------------------------------------------

# Simulate what cmd_dispatch builds when given a prompt starting with a flag-like string
prompt="--model is the config key you need"
launch=(claude --bg -n test-task)
launch+=(--model opus)
launch+=(--effort max)
launch+=(--)
launch+=("$prompt")

# The -- should come right before the prompt, so the prompt is never parsed as a flag
if [[ "${launch[-2]}" == "--" && "${launch[-1]}" == "$prompt" ]]; then
  echo "PASS cmd_dispatch: separator before flag-like prompt"
else
  echo "FAIL cmd_dispatch: expected separator -- then prompt, got ${launch[-2]} ${launch[-1]}"
  fail=1
fi

# --- dispatching into a worktree this script did not provision -----------------
#
# The gap that put three live workers outside the registry on 2026-09-09: `start` refuses when
# the worktree already exists, `dispatch` refuses when the task has no row, so there was no
# supported way to launch a worker into an existing worktree — and going around both with a raw
# `claude --bg` records nothing, which is what makes a session read as somebody's own terminal.

parse_dispatch_args "do it" --worktree /some/path
assert_eq "dispatch: --worktree is captured" "/some/path|do it" "$DISPATCH_WORKTREE|$DISPATCH_PROMPT"

parse_dispatch_args "do it"
assert_eq "dispatch: --worktree defaults to empty" "" "$DISPATCH_WORKTREE"

if parse_dispatch_args "do it" --worktree 2>/dev/null; then
  echo "FAIL: --worktree with no value should return non-zero"; fail=1
else
  echo "PASS: --worktree with no value returns non-zero"
fi

# A scratch registry and worktree dir — nothing below touches the real ones.
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/parallel-task-test.XXXXXX")"
trap 'rm -rf "$SCRATCH"' EXIT
WORKTREES_DIR="$SCRATCH/worktrees"
REGISTRY="$SCRATCH/registry.json"
mkdir -p "$WORKTREES_DIR"
echo '{"already-there": {"path": "/repo/.claude/worktrees/already-there", "mode": "native"},
       "adopted-one": {"path": "/x", "mode": "adopted", "adopted": true}}' > "$REGISTRY"

# t8317's shape: a worktree named after the task, sitting there unregistered.
mkdir -p "$WORKTREES_DIR/t8317"
assert_eq "dispatch_worktree: falls back to the worktree named after the task" \
  "$WORKTREES_DIR/t8317" "$(dispatch_worktree t8317 '')"

if dispatch_worktree no-such-task '' 2>/dev/null; then
  echo "FAIL: a task with neither a row nor a worktree should return non-zero"; fail=1
else
  echo "PASS: a task with neither a row nor a worktree returns non-zero"
fi

# fix720's shape: the session name and the worktree name differ, so only --worktree can say it.
mkdir -p "$WORKTREES_DIR/t5061"
assert_eq "dispatch_worktree: an explicit --worktree wins and is absolute" \
  "$WORKTREES_DIR/t5061" "$(cd / && dispatch_worktree fix720 "$WORKTREES_DIR/t5061")"

if dispatch_worktree fix720 "$SCRATCH/not-a-directory" 2>/dev/null; then
  echo "FAIL: --worktree pointing at nothing should return non-zero"; fail=1
else
  echo "PASS: --worktree pointing at nothing returns non-zero"
fi

# The row an adoption writes: enough for board_state.session_docs() to call the session managed,
# and honest about the fact that this script provisioned no dev stack for it.
git -C "$WORKTREES_DIR/t8317" init -q -b feature/t8317 2>/dev/null
entry="$(adopt_entry "$WORKTREES_DIR/t8317")"
assert_eq "adopt_entry: records the real path" "$WORKTREES_DIR/t8317" "$(jq -r '.path' <<<"$entry")"
assert_eq "adopt_entry: reads the branch off the worktree" "feature/t8317" "$(jq -r '.branch' <<<"$entry")"
assert_eq "adopt_entry: marks the row adopted" "true" "$(jq -r '.adopted' <<<"$entry")"
assert_eq "adopt_entry: claims no dev stack it did not start" "adopted|null|null" \
  "$(jq -r '"\(.mode)|\(.num)|\(.ports)"' <<<"$entry")"
assert_eq "adopt_entry: carries the ado_ids key every other row has" "[]" \
  "$(jq -c '.ado_ids' <<<"$entry")"

# stop/rm must not tear down a stack or delete a worktree this script never provisioned.
task_is_adopted adopted-one && r=yes || r=no
assert_eq "task_is_adopted: true for an adopted row" "yes" "$r"
task_is_adopted already-there && r=yes || r=no
assert_eq "task_is_adopted: false for a provisioned row" "no" "$r"
task_is_adopted missing && r=yes || r=no
assert_eq "task_is_adopted: false for a row that is not there" "no" "$r"

# --- dispatch provisions; it does not brief ------------------------------------
#
# A shell cannot deliver a brief. `claude --resume <id> -p` spawns a headless one-shot the live
# tmux session never sees, and send-keys types into a TUI where a brief's newlines each submit a
# half-finished prompt. So the keystroke delivery is gone and the brief is a FILE that the manager
# points the worker at over SendMessage — dispatch's job ends at provisioning and recording.

assert_eq "dispatch: no keystroke delivery left in cmd_dispatch" "0" \
  "$(declare -f cmd_dispatch | grep -c 'send-keys' || true)"

if declare -f cmd_dispatch | grep -q 'BRIEF.md'; then
  echo "PASS dispatch: still writes BRIEF.md, which is what the manager points the worker at"
else
  echo "FAIL dispatch: BRIEF.md is not written — a briefing message would have nothing to point at"
  fail=1
fi

if grep -q 'manager-start) cmd_manager_start' "$SCRIPT_DIR/parallel-task.sh"; then
  echo "PASS manager-start: the subcommand is routed, not just defined"
else
  echo "FAIL manager-start: cmd_manager_start exists but no subcommand reaches it"; fail=1
fi

# --- the exact agent name is recorded, never re-derived ------------------------
#
# SendMessage matches the DISPLAY name exactly, emoji included — sending to `T8471` is refused when
# the real name is `T8471 🔹`. cmew's rule (title-case the codename, append its emoji, prepend `🔥 `
# at effort ultracode) stays in cmew: the name is read back off `claude agents` and stored, so
# there is one copy of it and not two.

patch="$(session_registry_patch "sid-1" "T8471 🔹" opus max)"
assert_eq "registry patch: the agent name is stored verbatim, emoji and all" \
  "T8471 🔹" "$(jq -r '.agent_name' <<<"$patch")"
assert_eq "registry patch: session_id, model and effort ride along" "sid-1|opus|max" \
  "$(jq -r '"\(.session_id)|\(.model)|\(.effort)"' <<<"$patch")"

patch="$(session_registry_patch "sid-2" "Manager 🔹" "" "")"
assert_eq "registry patch: an unset model/effort adds no key" "false|false" \
  "$(jq -r '"\(has("model"))|\(has("effort"))"' <<<"$patch")"
assert_eq "registry patch: the name lands even with no model/effort" \
  "Manager 🔹" "$(jq -r '.agent_name' <<<"$patch")"

# `claude agents --json` stubbed: the id and the display name must come off the SAME row, and the
# newest one, or the registry addresses one session and joins another.
claude() {
  [[ "$1" == "agents" ]] || return 1
  cat <<'JSON'
[{"name": "T8471 🔹",       "sessionId": "sid-old",   "startedAt": 1},
 {"name": "t8471-retry 🔹", "sessionId": "sid-new",   "startedAt": 2},
 {"name": "🔥 Tornado 🌀",  "sessionId": "sid-other", "startedAt": 3}]
JSON
}
assert_eq "resolve_agent_identity: newest matching row, id and exact display name together" \
  "$(printf 'sid-new\tt8471-retry 🔹')" "$(resolve_agent_identity t8471)"
assert_eq "resolve_agent_identity: no match is empty, not a row of nulls" \
  "" "$(resolve_agent_identity nobody)"
unset -f claude

# --- manager-start: resident means exactly one ---------------------------------

tmux() { return 1; }   # no sessions at all, so only the argument guard can refuse
if out="$(cmd_manager_start --model sonnet 2>&1)"; then
  echo "FAIL manager-start: an unexpected argument should refuse, not be ignored"; fail=1
else
  case "$out" in
    *"takes no arguments"*) echo "PASS manager-start: refuses an argument instead of ignoring it" ;;
    *) echo "FAIL manager-start: wrong refusal for an argument, got: $out"; fail=1 ;;
  esac
fi

tmux() { [[ "$1" == "list-sessions" ]] && printf 'cc-manager\ncc-t8471\n'; return 0; }
if out="$(cmd_manager_start 2>&1)"; then
  echo "FAIL manager-start: a second manager should refuse — two would brief the same workers blind"
  fail=1
else
  echo "PASS manager-start: refuses while cc-manager is already up"
fi
case "$out" in
  *"cmew a manager"*) echo "PASS manager-start: the refusal says how to walk into the running one" ;;
  *) echo "FAIL manager-start: refusal never printed 'cmew a manager', got: $out"; fail=1 ;;
esac

# A longer session name is not the manager — `tmux has-session -t cc-manager` would say it is, which
# is why the guard reads the session list itself.
tmux() { [[ "$1" == "list-sessions" ]] && printf 'cc-manager-2\n'; return 0; }
tmux_session_exists cc-manager && r=yes || r=no
assert_eq "tmux_session_exists: a longer session name is not a match" "no" "$r"
tmux() { [[ "$1" == "list-sessions" ]] && printf 'cc-manager\n'; return 0; }
tmux_session_exists cc-manager && r=yes || r=no
assert_eq "tmux_session_exists: the exact name is a match" "yes" "$r"
unset -f tmux

[[ $fail -eq 0 ]] && echo "all passed" || { echo "FAILURES ABOVE"; exit 1; }
