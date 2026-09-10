#!/usr/bin/env bash
# run-board-mirror.sh — the ExecStart target for board-mirror.service.
#
# board-mirror.md is the single source of truth for the prompt; this script strips its
# HTML-comment scaffolding, fills in <ARTIFACT_URL>/<WRITE_ENTRIES_JSON>, and feeds the result to
# a one-shot `claude -p` run. Keeping the prompt in one place means the doc and the timer can
# never silently drift apart the way board-mirror.md's cadence and the old cron once did.
#
# board_state.py runs right here in bash, not inside the `claude -p` session: this script already
# has a shell, and running it here means a failure shows up as this script's own exit code instead
# of the model having to run a Bash step and report back — which also means `claude -p` never
# needs Bash permission (see the permission-mode comment below). The one thing this script does
# NOT run itself is reading the artifact db back for board answers — only the session has
# credentials for the artifact — so that rides along in every batch's own prompt; see the answers
# step inside the loop, right after each batch's $RESULT is decoded.
#
# board_mirror_diff.py, also run right here rather than inside the session, cuts board_state.py's
# full write set down to only what changed since the last successful run (see its own docstring
# for why) — the session downstream still just replays whatever array it's handed, same as before.
#
# That array is now ONE write_db batch (<=50 entries), not the whole diff: this script splits the
# diff and runs one `claude -p` per batch, recording each batch in the snapshot as soon as that
# batch's own reply says it landed. Before that, the snapshot was written once at the very end,
# so a run killed at TimeoutStartSec mid-write recorded nothing it had already written — 259 live
# documents against a 120-document snapshot meant a 156-document diff, ~310s against a 240s
# budget, and three consecutive runs that each wrote ~120 documents, died, recorded none of them,
# and handed the next run the identical work. The pump could not recover on its own.
#
# Splitting here rather than in the prompt is also what makes the record provable: one batch per
# session means one REFRESH_OK line attributes to exactly the entries this script handed it. When
# the model did the splitting, a single OK line covered several batches with no way to tell which
# of them actually landed.
#
# Failure must be loud: any of {missing env, board_state.py failing, a broken claude -p
# invocation, a reply with no REFRESH_OK line anywhere in it} exits non-zero, so a failed run
# shows up as `systemctl --user status` "failed" and in the journal — not as a quietly stale
# board.
set -euo pipefail

# README step 2 symlinks this script into ~/.config/board-mirror/ — dirname on BASH_SOURCE alone
# would resolve against the symlink's location, not the repo, and go looking for board-mirror.md
# next to ~/.config. readlink -f follows the symlink to the real file first.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PLUGIN_BIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

: "${ARTIFACT_URL:?ARTIFACT_URL is not set — see bin/systemd/board-mirror.env.example}"
: "${PWT_REPO_ROOT:?PWT_REPO_ROOT is not set — unset, board_state.py silently nulls branch/worktree/short_id/ado_refs instead of failing (see bin/systemd/board-mirror.env.example)}"

# Outside the repo, next to the env file from README step 1 — not tracked, not shipped, survives
# a `git pull`. Holds every doc_id this job wrote last time it actually succeeded, so this run can
# tell "unchanged" from "changed" and "gone" apart. Missing (first install) or unreadable (never
# synced, or a prior snapshot write itself failed) both mean "no previous run" to
# board_mirror_diff.py, which sends everything then — same as today's behavior, just once.
# BOARD_MIRROR_SNAPSHOT overrides it for tests, which must never point at the real one: writing
# it for documents a fake `claude` never sent is exactly the "already synced" lie described above.
SNAPSHOT_PATH="${BOARD_MIRROR_SNAPSHOT:-$HOME/.config/board-mirror/last-writes.json}"

# Type=oneshot stops systemd starting a second instance of the unit. It does NOT stop a person
# running this script by hand while the timer is enabled — which README's "force one run" step
# invites, and which happened: the manual run and the timer's run read the same snapshot, and the
# timer's diff was computed against a state the other run had already moved past. Both then wrote
# over each other and the timer's run died at the 240s budget.
#
# flock on a lockfile beside the snapshot, not on the snapshot itself: board_mirror_diff.py
# rewrites that file by rename, which would drop the lock along with the old inode.
mkdir -p "$(dirname "$SNAPSHOT_PATH")"
exec 9>"$SNAPSHOT_PATH.lock"
if ! flock -n 9; then
  # Exit 0, not 1: the work is being done by the run already holding this, so a red unit here
  # would be noise. The journal still says why nothing happened.
  echo "run-board-mirror: another run holds $SNAPSHOT_PATH.lock — stepping aside" >&2
  exit 0
fi

# stdout only: board_state.py's own diagnostics (e.g. a reader falling back) go to stderr and
# are left to flow straight into the journal, not merged in here — merging would splice that text
# into the middle of the JSON array WRITES needs to stay parseable.
if ! WRITES="$(python3 "$PLUGIN_BIN_DIR/board_state.py")"; then
  echo "run-board-mirror: board_state.py exited non-zero (see its stderr above)" >&2
  exit 1
fi

# Cuts $WRITES (every document, every run) down to only what changed since the snapshot below was
# last written, plus a `delete` for every doc_id the snapshot remembers that isn't in $WRITES at
# all anymore — see board_mirror_diff.py's docstring. This is the fix for the 216-document,
# every-5-minutes batch that used to take 223s end to end and never deleted anything (orphaned
# rows piled up until someone cleaned 50 of them out by hand).
if ! DIFF_WRITES="$(python3 "$SCRIPT_DIR/board_mirror_diff.py" diff "$SNAPSHOT_PATH" <<<"$WRITES")"; then
  echo "run-board-mirror: board_mirror_diff.py failed to compute the diff" >&2
  exit 1
fi

PROMPT_TEMPLATE="$(
  sed '/<!--/,/-->/d' "$PLUGIN_BIN_DIR/board-mirror.md" \
    | sed "s#<ARTIFACT_URL>#$ARTIFACT_URL#g"
)"

# One JSON array per line, each at most BATCH_LIMIT entries, in the diff's own order — so
# meta/status stays the last entry of the last batch. 50 is write_db's own cap.
BATCH_LIMIT="${BOARD_MIRROR_BATCH_LIMIT:-50}"
if ! BATCH_LINES="$(python3 "$SCRIPT_DIR/board_mirror_diff.py" chunk "$BATCH_LIMIT" <<<"$DIFF_WRITES")"; then
  echo "run-board-mirror: board_mirror_diff.py failed to split the diff into batches" >&2
  exit 1
fi
# `mapfile <<<""` would yield one empty element, not zero, and that empty "batch" would be sent
# to claude -p as an absent JSON array. diff always returns at least meta/status so this cannot
# happen today, but the loop below reads much worse if it ever does.
BATCHES=()
if [[ -n "$BATCH_LINES" ]]; then
  mapfile -t BATCHES <<<"$BATCH_LINES"
fi

# A systemd --user unit's PATH is whatever the user manager started with, not this shell's — it
# usually does NOT include ~/.local/bin. Default to the absolute path rather than bare `claude`.
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

# Stop cleanly instead of being SIGTERMed mid-batch. TimeoutStartSec is 240s and stays a backstop
# for a genuinely hung run, but with progress now durable there is nothing to gain from being
# killed inside a batch: that batch's spend is thrown away and the run is logged as failed even
# though it made real progress. So: never START a batch that the last batch's own duration says
# won't finish in time. The first batch always runs — a run that records nothing makes no
# progress, and the backlog would never drain.
#
# 210s from the measured cost of a run: 86 successful runs fit 49s fixed + 0.76s per document, so
# a full 50-entry batch is ~87s. Batch 1 ends near 90s (board_state.py is ~3s of that) and batch 2
# near 177s, both under 210; batch 3 would end near 264s and is not started. That is 2 batches —
# ~100 documents — per fire, which drains the 259-document worst case in 3 fires, ~15 minutes,
# unattended. Raising this to fit a third batch would push a run to ~267s against a 300s cadence,
# buying one batch for nearly all of the headroom that keeps fires from colliding.
DEADLINE_SEC="${BOARD_MIRROR_DEADLINE_SEC:-210}"

decode_result() {
  python3 -c '
import json, sys
try:
    payload = json.loads(sys.stdin.read())
except ValueError as e:
    print(f"run-board-mirror: claude -p did not return valid JSON: {e}", file=sys.stderr)
    sys.exit(1)
result = payload.get("result") if isinstance(payload, dict) else None
if not isinstance(result, str):
    print("run-board-mirror: claude -p JSON had no string result field", file=sys.stderr)
    sys.exit(1)
print(result)
'
}

WROTE=0
DONE_BATCHES=0
LAST_BATCH_SEC=0
LAST_OK_LINE=""

for ((i = 0; i < ${#BATCHES[@]}; i++)); do
  if ((i > 0)) && ((SECONDS + LAST_BATCH_SEC > DEADLINE_SEC)); then
    break
  fi
  BATCH_STARTED_AT=$SECONDS

  # Plain prefix/suffix splitting, not sed and not `${PROMPT//pat/$DIFF_WRITES}`: the batch is
  # arbitrary JSON and its escaped backslashes are exactly the kind of content both of those
  # reinterpret — sed's replacement treats `&`/`\` specially, and bash's `${.../pat/string}`
  # replacement does its own backslash escaping too (a literal `\"` in $DIFF_WRITES can lose a
  # backslash). `${var%%pat*}` / `${var#*pat}` only ever match `pat` to find a split point; the
  # pieces are then joined by plain `${var}` expansion, which never reinterprets its contents.
  PROMPT="${PROMPT_TEMPLATE%%<WRITE_ENTRIES_JSON>*}${BATCHES[i]}${PROMPT_TEMPLATE#*<WRITE_ENTRIES_JSON>}"

  # `claude -p` is non-interactive: nothing can click "approve" a permission prompt, so without a
  # grant covering every tool the prompt actually uses, the run dies with "requires permission
  # approval that was not granted". The prompt above only ever asks Claude to write_db and
  # read_db — board_state.py, board_mirror_diff.py and board_mirror_answers.py all run in this
  # shell, not inside the session.
  #
  # `--allowedTools Artifact` looks like it should be enough and ISN'T — verified live (2026-09-08)
  # with a real `claude -p` run that actually tried to write_db one document and read back the
  # result:
  #   --allowedTools Artifact                                            -> PROBE_FAIL:permission_denied
  #   --permission-mode bypassPermissions
  #     --disallowedTools Bash Edit Write Agent Workflow Skill ToolSearch -> PROBE_OK
  # Artifact IS present in a headless session's tool list, but write_db is still a separate
  # approval that --allowedTools has no specifier for (unlike Bash's command patterns or WebFetch's
  # domain matching, Artifact has no per-action allow syntax) — so `Artifact` alone still hits the
  # interactive-approval wall with nothing able to click through it, and the run dies exactly like
  # an ungranted tool would. `--permission-mode bypassPermissions` clears every such approval;
  # `--disallowedTools` then narrows back down explicitly (this one-line "write_db a batch" prompt
  # has no business touching Bash/Edit/Write/Agent/Workflow/Skill/ToolSearch), which is why this is
  # bypass-then-restrict rather than the wide-open default bypassPermissions would otherwise be.
  if ! RAW_OUTPUT="$("$CLAUDE_BIN" -p --permission-mode bypassPermissions \
    --disallowedTools Bash Edit Write Agent Workflow Skill ToolSearch \
    --output-format json -- "$PROMPT" 2>&1)"; then
    echo "run-board-mirror: claude -p exited non-zero on batch $((i + 1))/${#BATCHES[@]}: $RAW_OUTPUT" >&2
    exit 1
  fi

  RESULT="$(decode_result <<<"$RAW_OUTPUT")" || exit 1

  # The one thing here that travels the other way: everything else in this loop pushes state up,
  # this brings a decision the CTO made ON the board back down into the manager's ledger.
  # board-mirror.md's step 2 asks this SAME batch session (no dedicated `claude -p` of its own —
  # see board-mirror.md's comment on why) to read the `escalation_answers` collection back and
  # print it after an `ANSWERS:` marker; board_mirror_answers.py decides what of that may be
  # appended. Its docstring holds the never-clobber and idempotency rules, both decided against
  # the ledger rather than against the payload — which is also why running this once per batch
  # rather than once per round is safe: an answer already applied by an earlier batch this round
  # is simply not accepted again.
  #
  # Guarded, and deliberately placed BEFORE the OK_LINE branch below, so the two directions cannot
  # take each other down: a failure here is a journal warning and nothing more — this batch's
  # write has already landed — and a batch whose write FAILED still gets its answers applied,
  # because this runs before that branch exits the whole script.
  if ! printf '%s' "$RESULT" | python3 "$SCRIPT_DIR/board_mirror_answers.py" apply; then
    echo "run-board-mirror: bringing board answers down failed on batch $((i + 1)) (non-fatal) — the mirror itself is unaffected" >&2
  fi

  # board-mirror.md now asks for a two-line reply, but the model isn't guaranteed to comply —
  # a live run answered with a lead-in sentence before the REFRESH_OK line. Match REFRESH_OK: as a
  # substring of any line, not just an exact-match whole string, so a stray prefix doesn't get read
  # as failure (which, via the checkpoint below, would silently defeat the whole
  # diff-instead-of-full-resend point of this script forever). REFRESH_FAILED or no REFRESH_OK
  # anywhere still falls through to the failure branch below.
  OK_LINE="$(grep -m1 'REFRESH_OK:' <<<"$RESULT" || true)"
  if [[ -z "$OK_LINE" ]]; then
    echo "run-board-mirror: batch $((i + 1))/${#BATCHES[@]} did not report success: $RESULT" >&2
    exit 1
  fi

  # Checkpoint, and only now: this batch's own reply said it landed. Recording before the write,
  # or recording entries some LATER batch was going to send, would make the next run believe it's
  # already synced and silently skip real changes forever — a stale board with no error anywhere,
  # the exact kind of quiet failure that cost a full day before (see board-mirror.md's Scheduling
  # section). The reverse — dying after the write and before this line — only costs one idempotent
  # rewrite next run, which is why the order is write-then-record and not the other way round.
  # A failure here does NOT fail the run: the write itself already succeeded, so this only means
  # next run resends this batch — safe, just not free.
  if ! BATCH_COUNT="$(python3 "$SCRIPT_DIR/board_mirror_diff.py" apply "$SNAPSHOT_PATH" <<<"${BATCHES[i]}")"; then
    echo "run-board-mirror: batch $((i + 1)) landed but the snapshot update failed — next run resends it" >&2
    BATCH_COUNT=0
  fi

  WROTE=$((WROTE + BATCH_COUNT))
  DONE_BATCHES=$((DONE_BATCHES + 1))
  LAST_BATCH_SEC=$((SECONDS - BATCH_STARTED_AT))
  LAST_OK_LINE="$OK_LINE"
done

# ado_state_sync.py — AFTER every batch above, never before, so it reads the snapshot this same
# run just wrote rather than one up to a cadence stale. It only ever acts on state_drift's one
# `fixable` direction (New + PR-exists) and reads BOARD_MIRROR_SNAPSHOT itself, so pointing it at
# the same $SNAPSHOT_PATH this run just checkpointed to is enough — no other flag is required.
#
# Dry-run unless an operator has explicitly opted in: writing to ADO is the CTO's call, not this
# script's, so --apply is gated behind BOARD_MIRROR_APPLY_STATE_SYNC=1 rather than ever passed
# unconditionally. Non-fatal, the same as board_mirror_answers.py above — a state-sync problem
# must not turn a mirror run that already succeeded into a failed unit.
SYNC_FLAGS=()
if [[ "${BOARD_MIRROR_APPLY_STATE_SYNC:-}" == "1" ]]; then
  SYNC_FLAGS+=(--apply)
fi
if ! BOARD_MIRROR_SNAPSHOT="$SNAPSHOT_PATH" python3 "$PLUGIN_BIN_DIR/ado_state_sync.py" "${SYNC_FLAGS[@]}"; then
  echo "run-board-mirror: ado_state_sync.py reported a failure (non-fatal — see above)" >&2
fi

REMAINING=$((${#BATCHES[@]} - DONE_BATCHES))
if ((REMAINING > 0)); then
  # Deliberately exit 0 and deliberately NOT the REFRESH_OK line: real, durable progress was made
  # and the next timer fire picks up the rest, so this is not a failed run — but meta/status is in
  # the final batch and did not land, so nothing here may read as a completed refresh to the
  # journal or to anything watching for staleness.
  echo "run-board-mirror: PARTIAL: wrote $WROTE documents in $DONE_BATCHES of ${#BATCHES[@]} batches, $REMAINING batches remaining for the next run"
  exit 0
fi

# Unchanged from before checkpointing, on purpose: this is the line the journal and anything
# watching for staleness read as "the board is fully current", and only a run that got through
# every batch — meta/status included, it is in the last one — may print it. last_ado_sweep comes
# from that final batch's own reply.
SWEEP="$(sed -n 's/.*last_ado_sweep=\([^ ,]*\).*/\1/p' <<<"$LAST_OK_LINE")"
echo "run-board-mirror: REFRESH_OK: wrote $WROTE documents, last_ado_sweep=${SWEEP:-unknown}"
