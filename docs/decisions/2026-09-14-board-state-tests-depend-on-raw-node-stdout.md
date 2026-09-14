# Four `test_board_state.py` tests depend on raw `node` stdout, and are left that way

**Date:** 2026-09-14

## Context

Four tests in `bin/test_board_state.py` exercise board JavaScript by shelling out to `node` and
comparing its stdout as a literal string:

| Line | Test |
|---|---|
| 3995 | `test_ticket_drift_title_is_null_with_no_drift` |
| 4234 | `test_having_any_evidence_with_no_pr_to_compare_against_is_not_drift` |
| 4258 | `test_evidence_newer_than_the_merge_is_not_drift` |
| 4271 | `test_the_newest_attachment_is_what_gets_compared_against_the_merge_date` |

The assertion is `assert out == "null", out`. With `FORCE_COLOR` exported, `node -e
'console.log(null)'` emits `\x1b[1mnull\x1b[22m`, so the comparison fails on invisible bytes and the
failure output reads `assert 'null' == 'null'` — which looks like nonsense.

Measured 2026-09-14, from `bin/`:

```
python3 -m pytest -q                 → 4 failed, 808 passed
env -u FORCE_COLOR python3 -m pytest -q → 812 passed
```

## Decision

Record the fragility; do not fix it now. Run the suite with `env -u FORCE_COLOR`.

## Why

The branch these were measured on already carried 249 unpushed commits and three finished tickets;
adding an unrelated change to it would have widened a diff that was already too wide to review. The
tests are correct about the behaviour they assert — only their comparison is environment-sensitive.

The cost of leaving it is bounded and known: any CI runner or shell with `FORCE_COLOR` set fails
four tests for a reason that reads as gibberish, which costs whoever hits it about twenty minutes.

## What would change this

Any CI job that runs `bin/` tests — a runner that sets `FORCE_COLOR` would turn this from a local
annoyance into a red build. The fix is to strip ANSI from `_run_node`'s output, or pass
`env={**os.environ, "FORCE_COLOR": "0"}` to the subprocess, in one place rather than in each test.
