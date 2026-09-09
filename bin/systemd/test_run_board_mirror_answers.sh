#!/usr/bin/env bash
# assert-based checks for how run-board-mirror.sh wires the return path in — the two properties
# that are structural rather than logical, so a unit test on board_mirror_answers.py cannot see
# them:
#   1. bringing answers down cannot fail the run (the script is `set -e`; an unguarded call would)
#   2. it happens BEFORE the per-batch OK_LINE branch that can exit the whole run, so a batch
#      whose write failed still lets a decided answer come down.
# It runs once per batch, inside the batch loop, so both checks look inside that loop rather than
# at top level. Check 3 runs the script's own guard text, verbatim, against a deliberately failing
# step.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUMP="$SCRIPT_DIR/run-board-mirror.sh"

fail() { echo "FAIL $1" >&2; exit 1; }

grep -q 'if ! .*board_mirror_answers\.py' "$PUMP" \
  || fail "board_mirror_answers.py is not called inside an 'if !' guard — under set -e it would abort the run"
echo "ok the return path runs inside a failure-tolerating guard"

answers_at="$(grep -n 'board_mirror_answers\.py' "$PUMP" | head -1 | cut -d: -f1)"
ok_at="$(grep -n 'OK_LINE="\$(grep' "$PUMP" | head -1 | cut -d: -f1)"
[[ -n "$answers_at" && -n "$ok_at" ]] || fail "could not locate both the answers step and the OK_LINE branch in $PUMP"
(( answers_at < ok_at )) || fail "the answers step runs after the OK_LINE branch — a failed batch would skip it"
echo "ok answers come down before the batch's own success/failure branch decides the exit code"

STUB="$(mktemp -d)"
trap 'rm -rf "$STUB"' EXIT
printf '#!/usr/bin/env bash\necho "stub python3 says no" >&2\nexit 3\n' > "$STUB/python3"
chmod +x "$STUB/python3"

FRAGMENT="$(awk '/if ! .*board_mirror_answers\.py/,/^  fi$/' "$PUMP")"
[[ -n "$FRAGMENT" ]] || fail "could not extract the answers guard from $PUMP"

OUT="$(PATH="$STUB:$PATH" SCRIPT_DIR="$SCRIPT_DIR" RESULT="REFRESH_OK: wrote 4 documents" i=0 \
  bash -c "set -euo pipefail
$FRAGMENT
echo STILL_RUNNING" 2>&1)" || fail "the answers guard aborted the run when its step failed"
grep -q STILL_RUNNING <<<"$OUT" || fail "the run did not continue past a failing answers step: $OUT"
echo "ok a failing return path leaves the rest of the run alive"

# The reverse: a batch whose WRITE failed (RESULT carries no REFRESH_OK) must still run the
# answers step, and must still fail the batch afterward — one direction breaking must not paper
# over the other. Real board_mirror_answers.py, run against a fake SCRIPT_DIR so it can't reach
# the real escalation ledger; it only needs to prove it ran.
FAKE_DIR="$(mktemp -d)"
trap 'rm -rf "$STUB" "$FAKE_DIR"' EXIT
cat > "$FAKE_DIR/board_mirror_answers.py" <<'PYEOF'
import sys
open(__file__ + ".marker", "w").write(sys.stdin.read())
PYEOF

FRAGMENT2="$(awk '
  /if ! .*board_mirror_answers\.py/ { grab=1 }
  grab { print; if ($0 == "  fi") { n++; if (n == 2) exit } }
' "$PUMP")"
[[ -n "$FRAGMENT2" ]] || fail "could not extract the answers-guard-through-OK_LINE fragment from $PUMP"

if OUT2="$(SCRIPT_DIR="$FAKE_DIR" RESULT="REFRESH_FAILED: boom" i=0 \
  bash -c "set -euo pipefail
BATCHES=(x)
$FRAGMENT2
echo SHOULD_NOT_PRINT" 2>&1)"; then
  fail "a batch with no REFRESH_OK did not fail: $OUT2"
fi
[[ -f "$FAKE_DIR/board_mirror_answers.py.marker" ]] \
  || fail "the answers step did not run when the batch's own write failed"
grep -q SHOULD_NOT_PRINT <<<"$OUT2" && fail "the batch continued past its own REFRESH_FAILED result"
echo "ok a failed batch write still lets answers come down, and still fails the batch"

echo "all checks passed"
