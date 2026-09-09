# board-mirror systemd user timer

Runs the refresh described in `bin/board-mirror.md` every 5 minutes, independent of any Claude
session — replaces the session-scoped `CronCreate` job that died when its session exited (see
board-mirror.md's Scheduling section for what that cost).

## 0. Prerequisite — enable linger (needs a higher-privileged account, MANDATORY)

```
loginctl enable-linger azureuser
```

This needs root or sudo — a normal user cannot enable-linger for themselves. **Do not skip this
step or treat it as optional.** Without it, `systemctl --user` services are tied to a logind
session: they stop the moment the last login session for this user ends (SSH disconnect,
terminal closed), which is exactly the failure this timer exists to fix, just delayed until
logout instead of session exit. Confirm it took effect:

```
loginctl show-user azureuser -p Linger
```

Expect `Linger=yes`. If you cannot run `loginctl enable-linger` yourself, ask whoever has root on
this box to run it, and don't consider this setup done until `Linger=yes` comes back — a timer
installed without it will look correct today and die at the next logout.

## 1. Config file (outside the repo, real values only here)

```
mkdir -p ~/.config/board-mirror
cp bin/systemd/board-mirror.env.example ~/.config/board-mirror/env
chmod 600 ~/.config/board-mirror/env
$EDITOR ~/.config/board-mirror/env   # fill in ARTIFACT_URL and PWT_REPO_ROOT for real
```

Leave `CLAUDE_CODE_ENTRYPOINT=claude-desktop` as the example file sets it — don't delete it and
don't change the value. Without it, `claude -p` has no `Artifact` tool at all (not a denied
permission, the tool doesn't exist for that session), and every run dies at the write step with
the misleading `"Artifact tool is not available in this session"`. If you ever see that exact
message in the journal, this is the first thing to check.

`run-board-mirror.sh` also keeps a snapshot of what it has written at
`~/.config/board-mirror/last-writes.json`, next to `env` — not something you create, it writes
itself as each 50-document batch lands, and only sends changed documents (plus deletes for ones
that dropped out) from then on. Delete it to force a full resync on the next run; losing it just
costs one full-size run spread over a few runs, it does not lose data. Point
`BOARD_MIRROR_SNAPSHOT` somewhere else for a dry run — never let a test write this file, since it
is what tells the next real run "already synced".

If you hold more than one ADO identity, also set `PWR_ADO_ASSIGNED_TO` in that file to a
comma-separated list of all of them. Left unset, the WIQL query falls back to `@Me`, which
matches only the identity `az` is currently logged in as — tickets under any other identity
of the same person go missing from the board with no error.

## 2. Symlink the run script and unit files

```
ln -sf "$(pwd)/bin/systemd/run-board-mirror.sh" ~/.config/board-mirror/run-board-mirror.sh
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/bin/systemd/board-mirror.service" ~/.config/systemd/user/board-mirror.service
ln -sf "$(pwd)/bin/systemd/board-mirror.timer" ~/.config/systemd/user/board-mirror.timer
```

Symlinks, not copies: re-running these two commands after a `git pull` is the entire upgrade
path, no reinstall step needed.

## 3. Enable and start

```
systemctl --user daemon-reload
systemctl --user enable --now board-mirror.timer
```

## 4. Verify

```
systemctl --user list-timers board-mirror.timer
systemctl --user status board-mirror.service
journalctl --user -u board-mirror.service -n 50
```

A healthy `list-timers` row shows a `NEXT` a few minutes out and a `LAST`/`PASSED` from the run
before it. `status` on the service after a run shows `Active: inactive (dead)` with
`Main PID: ... (code=exited, status=0/SUCCESS)` — `oneshot` units go back to inactive between
runs, that's expected, not a failure. A failed refresh (bad env, `board_state.py` erroring inside
the session, a malformed `claude -p` reply) shows as `status=1/FAILURE` here and the reason in the
journal line above it — see `run-board-mirror.sh` for what each exit path logs.

### What the board reads: two clocks, two meanings

The refresh writes two `meta` documents, at opposite ends of the run, and they answer different
questions. A page that reads only one of them will get the other one's answer wrong.

| document | written | fields | means |
|---|---|---|---|
| `meta/pump` | first batch, **every run** | `ran_at` (epoch seconds), `batches_pending` (int ≥ 1) | the pump is alive and started a run at `ran_at`, with `batches_pending` batches to get through. `batches_pending == 1` means this run expects to finish. |
| `meta/status` | last batch, **only a run that finished** | `written_at`, `last_ado_sweep`, `last_session_scan`, `pump_period_s`, … | every row beside it is complete as of these stamps. |

So: **`meta/pump.ran_at` is the liveness signal, `meta/status.written_at` is the completeness
signal.** A staleness alarm must be sized off `ran_at` — that is the one that moves every 5
minutes. `written_at` legitimately sits still for the length of a backlog drain, and treating
that as "the pump has stopped" raises a false alarm on a healthy pump, which is worse than no
alarm at all because people learn to ignore it. Use `batches_pending > 1` to say the board is
still catching up rather than to say it is dead.

`meta/pump` deliberately carries no "as of" clock. It rides the first batch, so it lands before
the data does; if it stamped a sweep time, a partial run would be claiming rows it has not
written yet — the exact thing `meta/status` going last exists to prevent.

A journal line reading `run-board-mirror: PARTIAL: wrote 50 documents in 1 of 4 batches, ...` is
NOT a failure and exits 0. After a large change the refresh is split into 50-document batches and
a run does as many as fit in its budget, recording each one as it lands; the next fire picks up
the rest, and the backlog shrinks every run until a plain `REFRESH_OK` line comes back. Only that
`REFRESH_OK` line means the board is fully current — `PARTIAL` deliberately never writes
`meta/status`, so the board keeps showing the older sweep rather than claiming a fresh one over
rows that have not landed yet. Several `PARTIAL` runs in a row are expected after a big change;
`PARTIAL` runs that never reach `REFRESH_OK` are not, and mean the backlog is growing faster than
one run can drain it.

To force one run without waiting for the timer: `systemctl --user start board-mirror.service`.
If a run is already in flight, the second one logs `another run holds ... — stepping aside` and
exits 0 rather than racing it: two runs reading the same snapshot compute their diffs against
states that have already moved apart, and write over each other.

---

# stuck-session-watch systemd user timer

A dispatched worker that hits a permission prompt it cannot answer stops dead: `claude agents
--json` reports it `blocked` and it stays that way, because nobody is there to answer. On
2026-09-09 four sessions sat like that for up to two hours each, and every one was found only
because the CTO asked why nothing was happening. The state was queryable the whole time; nothing
carried it anywhere.

This timer runs `bin/stuck_sessions.py`, which carries it — into `escalations.jsonl` as an
ordinary **open** record, not as something waiting on a human. `manager_daemon.py` then decides it
like any other tier-2 escalation and degrades it to `needs_human` only once the manager's own
attempts are exhausted. Three of those four the manager could and did settle itself.

It is read-only with respect to every session it looks at: it never resumes, answers, stops or
kills anything. The only thing it writes is the queue.

## Why its own timer

Its own unit, not a second `ExecStart` on `board-mirror.service` and not a step inside
`run-board-mirror.sh`:

* **Its own journal.** `journalctl --user -u stuck-session-watch.service` shows this check and
  nothing else. Folded into the mirror, a failed scan would be one line inside a four-minute
  `claude -p` run's output, which is where a silent failure goes to hide.
* **It must not inherit the mirror's failure modes.** The mirror runs a real Claude session: it
  can burn its 240s budget, exit `PARTIAL`, or fail on an expired token. This scan is a
  `claude agents --json` and a few file reads — under a second, no token, no session. Chaining it
  behind the mirror would let a token problem stop the one check whose entire job is noticing
  that nothing is happening.
* **It must survive the manager's session ending**, which is the whole point — so it cannot live
  in `manager_daemon.py`, which dies with the session that started it.

## Install (a human must do this — nothing here installs itself)

Prerequisite: **linger**, exactly as for board-mirror above. Same command, same reason; if you
have already done it for board-mirror it is done for this too.

This unit reuses board-mirror's config file for the one variable it needs — `PWT_REPO_ROOT`,
which says whose `.claude/worktrees/.parallel-registry.json` to join sessions against. If
board-mirror is installed there is nothing new to fill in. It runs no `claude -p` session, so it
needs neither `CLAUDE_CODE_ENTRYPOINT` nor a token.

```
ln -sf "$(pwd)/bin/stuck_sessions.py" ~/.config/board-mirror/stuck_sessions.py
mkdir -p ~/.config/systemd/user
ln -sf "$(pwd)/bin/systemd/stuck-session-watch.service" ~/.config/systemd/user/stuck-session-watch.service
ln -sf "$(pwd)/bin/systemd/stuck-session-watch.timer" ~/.config/systemd/user/stuck-session-watch.timer
systemctl --user daemon-reload
systemctl --user enable --now stuck-session-watch.timer
```

Symlinks, not copies — same upgrade path as board-mirror. `stuck_sessions.py` imports its
siblings out of the repo `bin/` next to the symlink target, so the checkout stays the source of
truth.

## Verify

```
systemctl --user list-timers stuck-session-watch.timer
journalctl --user -u stuck-session-watch.service -n 20
```

Every run logs one summary line whether or not it found anything:

```
stuck-session-watch: 19 live sessions, 0 stuck past 15m, 0 queue writes -> ~/.claude/hermes/escalations.jsonl
```

The boring line is the point — it is what tells you the timer is alive. A run that files or
clears something prints a `filed:` / `cleared:` line above it with the task name and record id.

A run that cannot do its job **fails loudly and files nothing**, exiting non-zero so
`systemctl --user status` shows `status=1/FAILURE`:

| Journal line | What is wrong |
|---|---|
| `no parseable cadence in .../stuck-session-watch.timer` | Someone retuned `OnCalendar` to a form `board_state.timer_period_seconds()` cannot read. The silence window is derived from that value, and a guessed window is exactly the failure this repo already shipped once. |
| `cannot read registry ...` | `PWT_REPO_ROOT` points somewhere with no registry under it. |
| `` `claude agents --json` returned nothing `` | The CLI failed or timed out. |
| `skipped N waiting session(s) with no registry row` | **Not** a failure — exit stays 0. But if N is not zero while sessions are visibly frozen, the dispatch path has stopped writing registry rows, and *that* is the bug. A session with no registry row cannot be told apart from somebody's own terminal, so the watch leaves it alone. |

## What it deliberately does not report

The check that ran by hand before this one reported nine stuck sessions of which five were
corpses — abandoned days or months earlier, three with the worktree already deleted. A
majority-false alarm gets ignored, and then the real one is ignored too. Four filters, each
cutting one class of false positive:

| Filter | Why |
|---|---|
| `claude agents --json` **without** `--all` | `--all` returns every session the CLI has ever known — 69 here against 19 live, 32 of them for worktrees deleted days or months ago. Those are the corpses. |
| the worktree still exists | A session whose worktree is gone is dead, not stuck: there is nothing left to unblock, so there is no decision to make. |
| silent for at least 3 timer periods | Measured from the session transcript's mtime, **not** from `startedAt`. A frozen session writes nothing, so the mtime is when it stopped moving; `startedAt` would have called `fix720` stuck for 2.7h when it had been silent 1.3h, and would call any long-running session stuck one second after its first prompt. No transcript means no measurable duration, and the watch files nothing rather than guess one. |
| `managed` — a registry entry exists | `board_state.session_docs()`'s own rule, reused rather than restated. A session nobody dispatched is somebody's own terminal, and the human in front of it can answer their own prompt. Counted and reported, never silently dropped. |

Filing is idempotent and self-clearing. A record is filed once per freeze, not once per scan; a
second scan over the same frozen session writes nothing; and when the session starts moving (or
drops off the list) the record is dismissed so it stops asking for a decision nobody needs. A
later freeze of the same session files a fresh record.
