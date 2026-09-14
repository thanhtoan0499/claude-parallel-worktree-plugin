# `scrub_inherited_claude_env()` deliberately leaves `ANTHROPIC_BASE_URL` alone

**Date:** 2026-09-14 (recording a decision measured 2026-09-11)

## Context

`bin/parallel-task.sh:181` scrubs four variables from the tmux **server** environment before
spawning a worker — `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_PID`,
`CLAUDE_CODE_EXECPATH`. tmux hands a new session the server's environment rather than the spawning
shell's, so `env -u` at the call site does nothing; the scrub has to happen at the source.

`ANTHROPIC_BASE_URL` is conspicuously not on that list. The reasoning was written into the function
as a comment on 2026-09-11 and is preserved here.

## Decision

Do not scrub `ANTHROPIC_BASE_URL`.

## Why

Measured 2026-09-11 across all 14 live panes: the 12 healthy workers all carried
`ANTHROPIC_BASE_URL=https://api.anthropic.com`, and the only two sessions returning 401 were the two
where it was unset. Those sessions authenticate with the Claude Max OAuth login in
`~/.claude/.credentials.json`, which is valid only against the public API. Unset the variable and
`settings.json` supplies its own pair instead — a proxy URL plus a token those sessions do not use —
so every turn dies with "API key required for remote API access".

`settings.json` describes the **desktop** session's auth path, not a spawned session's. The two are
not interchangeable.

## What would change this

**This measurement has already expired once.** On 2026-09-14 the parent session was running against
a proxy (`ANTHROPIC_BASE_URL` pointing at a tunnel host, with `ANTHROPIC_DEFAULT_*_MODEL` values
carrying a `cc/` prefix that only that proxy resolves). Under those conditions the 2026-09-11
conclusion is wrong in both directions: a worker inheriting `api.anthropic.com` from the tmux server
asks the public API for a model only the proxy has, and every model reports "may not exist".

Spawned workers that day returned 401 regardless of how the variables were arranged. The cause
turned out to have nothing to do with this decision: an existing session, asked directly, reported
`You've hit your weekly limit · resets 6am (UTC)`. The account quota was exhausted, and every
spawned session failed for that reason alone.

The lesson is the cheaper diagnostic: **ask a live session what it sees before theorising about the
environment.** Two wrong hypotheses were chased — a base-URL mismatch and a missing token — because
nobody read the actual error text first.

Before trusting either conclusion, re-measure: read `/proc/<worker-pid>/environ` for the live panes
and compare against the parent. Do not carry a stale environment fact forward.
