# Decision records

One file per non-obvious decision: `YYYY-MM-DD-<slug>.md`.

The ledger (`~/.claude/hermes/assignments.jsonl`) records **what** was committed to. These records
exist for the other half — **why** a call was made — so the next session does not re-derive it, or
re-derive it wrong.

## Format

Short. Roughly fifteen lines. Four headings:

```markdown
# <the decision, as a sentence>

**Date:** YYYY-MM-DD

## Context
What was true at the time. Include the measurement and how it was taken — a number without
its date and method expires silently.

## Decision
What was chosen.

## Why
The reasoning, and what was rejected.

## What would change this
The condition under which the decision should be revisited.
```

## When to write one

- A choice that looks arbitrary from the outside (why this branch was not opened as a PR).
- A deliberate omission (why an environment variable is not scrubbed).
- A known fragility left in place on purpose (a test that depends on raw stdout).
- Anything a later session would otherwise ask "why is it like this?" about.

Not for: what the code already says, or what a commit message covers.

## Measurements expire

An environment fact measured last week may already be false — the `ANTHROPIC_BASE_URL` record is
exactly that shape. Always write the date and the method, so a reader can tell whether to re-measure
rather than trust.
