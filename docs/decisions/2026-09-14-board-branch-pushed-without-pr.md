# `feat/board-status-unify` is pushed to origin but deliberately not opened as a PR

**Date:** 2026-09-14

## Context

The branch carried every piece of manager-board work and had **never been pushed** — `git status -sb`
showed no upstream, `git rev-list --count master..feat/board-status-unify` showed **249 commits ahead,
0 behind**. Three finished tickets lived only there: AB#8449 (the data pump survives a timeout
mid-write), AB#8450 (answer escalations on the board), AB#8451 (per-ticket status). A disk failure
would have taken all of it.

Tests on the branch: **812 passed** (`env -u FORCE_COLOR python3 -m pytest -q`, run 2026-09-14 from
`bin/`). See `2026-09-14-board-state-tests-depend-on-raw-node-stdout.md` for why the variable matters.

## Decision

Push to `origin` with upstream tracking. Do **not** open a pull request.

## Why

Durability and reviewability are separate problems, and only the first was urgent. Pushing costs
nothing and removes the single-machine risk immediately.

A 249-commit pull request is not reviewable — nobody reads it, so it would be approved on trust,
which is worse than no review because it looks like review happened. The split shape (per-ticket
branches via cherry-pick, versus one squash with a reading guide) had not been decided, and opening
a PR would have forced that decision by accident rather than on purpose.

`upstream` (the fork's parent) was deliberately untouched; only `origin` received the branch.

## What would change this

A decision on the split shape. Per-ticket PRs require checking whether each ticket's commits touch
files that later commits rewrite — a theme whose files are rewritten downstream cannot be
cherry-picked cleanly, and that analysis has not been done.
