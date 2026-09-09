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

parse_dispatch_args "do the thing"
assert_eq "dispatch: prompt only" "||do the thing" "$DISPATCH_MODEL|$DISPATCH_EFFORT|$DISPATCH_PROMPT"

parse_dispatch_args "do it" --model opus --effort max
assert_eq "dispatch: both flags" "opus|max|do it" "$DISPATCH_MODEL|$DISPATCH_EFFORT|$DISPATCH_PROMPT"

parse_dispatch_args "do it" --model sonnet
assert_eq "dispatch: model only" "sonnet|" "$DISPATCH_MODEL|$DISPATCH_EFFORT"

parse_dispatch_args "do it" --effort low
assert_eq "dispatch: effort only" "|low" "$DISPATCH_MODEL|$DISPATCH_EFFORT"

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
  "|exactly one quoted prompt with --model inside it" "$DISPATCH_MODEL|$DISPATCH_PROMPT"

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

[[ $fail -eq 0 ]] && echo "all passed" || { echo "FAILURES ABOVE"; exit 1; }
