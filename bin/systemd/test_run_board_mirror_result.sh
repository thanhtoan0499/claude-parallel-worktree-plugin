#!/usr/bin/env bash
# assert-based checks for the REFRESH_OK detection in run-board-mirror.sh (the `OK_LINE=...` grep
# right after $RESULT is decoded). Keep this pattern in sync with that line if it ever changes.
set -euo pipefail

check() {
  local name="$1" result="$2" want_ok="$3"
  local ok_line
  ok_line="$(grep -m1 'REFRESH_OK:' <<<"$result" || true)"
  if [[ "$want_ok" == "ok" && -z "$ok_line" ]]; then
    echo "FAIL $name: expected REFRESH_OK to be recognized, got no match" >&2
    exit 1
  fi
  if [[ "$want_ok" == "fail" && -n "$ok_line" ]]; then
    echo "FAIL $name: expected failure, got OK_LINE='$ok_line'" >&2
    exit 1
  fi
  echo "ok $name"
}

check "prose before REFRESH_OK is still success" \
  "$(printf 'All 216 entries written across 5 ordered batches, `meta/status` last.\n\nREFRESH_OK: wrote 216 documents, last_ado_sweep=1788857703.782373')" \
  ok

check "bare REFRESH_OK is still success" \
  "REFRESH_OK: wrote 216 documents, last_ado_sweep=1788857703.782373" \
  ok

check "REFRESH_FAILED is failure" \
  "REFRESH_FAILED: write_db batch returned an error" \
  fail

check "no marker at all is failure" \
  "Something went wrong and I stopped." \
  fail

echo "all checks passed"
