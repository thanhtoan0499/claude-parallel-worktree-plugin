# Manager Dashboard: Ticket-Centric Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the dashboard's primary view from "one row per live session" into "one row per ADO
ticket" — status + next action, dispatchable straight from the UI — while keeping the existing
session/log/redaction machinery intact as a collapsed operator view.

**Architecture:** Extend the existing single-file `bin/dashboard.py` (stdlib `http.server`) +
`bin/dashboard.html` (vanilla JS, no build step). Three data sources merge client-side as they do
today: live sessions+registry (unchanged), PR data per branch (unchanged), and a new ADO backlog
source (`az boards query`, cached server-side). `parallel-task.sh start` gains a repeatable
`--ticket <id>` flag so a manager-dispatched copy records which ticket(s) it serves at the moment
of dispatch, rather than that being inferred after the fact.

**Tech Stack:** Python 3 stdlib only (no new pip dependency), `az` CLI (already authenticated on
this machine, verified live), `gh` CLI (already in use), bash (`parallel-task.sh`), vanilla JS.

## Global Constraints

- No new Python or JS dependency. Everything here is stdlib + the `az`/`gh` CLIs already in use.
- Every new pure function gets a test in the existing assert-based style (see `test_dashboard.py`)
  — no framework, no fixtures, matching what's already there.
- No writes to ADO from the dashboard. Ticket data is read-only context; the only ADO-adjacent
  action is *dispatching a session*, which may itself later post to ADO on its own (that's the
  session's job, driven by its own tools — not something this dashboard triggers).
- `ado_refs` is the canonical shape everywhere a ticket reference is carried: `list[{"id": str,
  "url": str}]`. Two producers, one shape: `parallel-task.sh`'s registry field (`ado_ids: list[str]`,
  bash can't easily know URLs) gets converted to `ado_refs` pairs in Python by applying the default
  ADO base URL; PR-text scraping already knows real URLs and produces `ado_refs` pairs directly.
- Every subprocess call gets a `timeout=`. Every subprocess-wrapping function that fails degrades to
  an empty/default value — never raises past the HTTP handler (matches every existing function in
  `dashboard.py`: `get_registry`, `get_sessions`, `_lookup_pr_and_ticket` all do this already).

---

## File Structure

- Modify `bin/dashboard.py` — generalize ADO-ref extraction to multiple matches; merge registry +
  PR-scraped refs; add the ADO-backlog fetch/shape functions; add HTML-stripping for ticket
  descriptions; add dispatch prompt/slug builders; add two new HTTP routes
  (`GET /api/ado-tickets`, `POST /api/tickets/dispatch`).
- Modify `bin/parallel-task.sh` — `cmd_start` accepts repeatable `--ticket <id>`, persists
  `ado_ids` in the registry entry. Argument parsing extracted into a small pure function
  (`parse_start_args`) so it's testable without touching git/docker/pnpm.
- Modify `bin/dashboard.html` — fetch the new ADO-backlog endpoint; rewrite `computeTickets` to
  iterate the ADO backlog (not sessions) as its primary loop; extend `renderTickets` for the
  Not-started/"Giao việc" case and the always-shown dual ADO+PR link; add the inline dispatch form;
  wrap the stat cards / Sessions table / Session details / Live logs / Decided-for-you in one
  collapsed-by-default disclosure.
- Modify `bin/test_dashboard.py` — rewrite the 3 existing `_find_ado_link` tests for the new
  plural function; add tests for the ADO-shaping, HTML-stripping, and prompt/slug builder
  functions.
- Create `bin/test_parallel_task.sh` — new file, asserts on `parse_start_args`'s parsing/JSON
  output only (no real worktree/docker/pnpm side effects — matches this repo's existing convention
  of not automating the expensive parts of `cmd_start`, which have only ever been verified by a
  live smoke test).

---

### Task 1: Generalize ADO-ref extraction to multiple matches

**Files:**
- Modify: `bin/dashboard.py:175-184` (current `_find_ado_link`), `:197-236` (`_lookup_pr_and_ticket`),
  `:172` (`_EMPTY_PR_TICKET`)
- Test: `bin/test_dashboard.py` (existing `test_find_ado_link_*` tests, lines ~163-176)

**Interfaces:**
- Produces: `_find_ado_links(text: str) -> list[dict]`, each dict `{"id": str, "url": str}`,
  deduplicated by `id`, in the order first seen. `_lookup_pr_and_ticket(branch)` now returns
  `{"pr_number", "pr_url", "pr_state", "ado_refs": list[dict]}` (no more singular `ado_id`/`ado_url`).

- [ ] **Step 1: Write the failing tests**

Replace the three existing `_find_ado_link` tests in `bin/test_dashboard.py` with:

```python
def test_find_ado_links_returns_all_matches_deduplicated():
    text = (
        "See (AB#1) and again AB#1, but really "
        "[AB#7160](https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160)"
    )
    refs = _find_ado_links(text)
    assert refs == [
        {"id": "1", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/1"},
        {"id": "7160", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160"},
    ]


def test_find_ado_links_full_url_wins_over_bare_ref_for_same_id():
    text = "(AB#7160) ... [AB#7160](https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160)"
    refs = _find_ado_links(text)
    assert refs == [{"id": "7160", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/7160"}]


def test_find_ado_links_empty_when_absent():
    assert _find_ado_links("just a normal PR title") == []
```

Update the import line at the top of `bin/test_dashboard.py` from `_find_ado_link` to
`_find_ado_links`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd bin && python3 test_dashboard.py`
Expected: `ImportError: cannot import name '_find_ado_links'` (function doesn't exist yet)

- [ ] **Step 3: Implement `_find_ado_links`, update `_lookup_pr_and_ticket` and `_EMPTY_PR_TICKET`**

Replace `bin/dashboard.py:172` (`_EMPTY_PR_TICKET`):

```python
_EMPTY_PR_TICKET = {"pr_number": None, "pr_url": None, "pr_state": None, "ado_refs": []}
```

Replace `bin/dashboard.py:175-184` (`_find_ado_link`):

```python
def _find_ado_links(text: str) -> list[dict]:
    """Every ADO reference in the text, deduplicated by id, first-seen order. A full
    dev.azure.com URL wins over a bare AB#NNNN ref for the same id (keeps whatever org/project
    form the author actually used instead of assuming this repo's default one)."""
    by_id: dict[str, str] = {}
    order: list[str] = []
    for m in _ADO_URL_RE.finditer(text):
        ticket_id = m.group(1)
        if ticket_id not in by_id:
            order.append(ticket_id)
        by_id[ticket_id] = m.group(0)
    for m in _ADO_REF_RE.finditer(text):
        ticket_id = m.group(1)
        if ticket_id not in by_id:
            order.append(ticket_id)
            by_id[ticket_id] = _ADO_DEFAULT_BASE + ticket_id
    return [{"id": i, "url": by_id[i]} for i in order]
```

Replace `bin/dashboard.py:228-236` (the tail of `_lookup_pr_and_ticket`, after `pr = prs[0]`):

```python
    pr = prs[0]
    ado_refs = _find_ado_links(f"{pr.get('title', '')}\n{pr.get('body') or ''}")
    return {
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url"),
        "pr_state": pr.get("state"),
        "ado_refs": ado_refs,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd bin && python3 test_dashboard.py`
Expected: `14 passed` (11 previous minus 3 replaced, plus 3 new = same count net; confirm no
failures)

- [ ] **Step 5: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: extract every ADO ticket ref from a PR, not just the first"
```

---

### Task 2: `parallel-task.sh` gains a repeatable `--ticket` flag

**Files:**
- Modify: `bin/parallel-task.sh:122-196` (`cmd_start`), `:20-25` (usage comment), `:344-354`
  (bottom dispatch — needs a sourceable guard)
- Test: `bin/test_parallel_task.sh` (new)

**Interfaces:**
- Produces: `parse_start_args "$@"` prints `task<TAB>mode<TAB>base_ref<TAB>ado_ids_json` on stdout
  and returns 0, or prints an error to stderr and returns 1. Registry entries written by
  `cmd_start` now carry `ado_ids: list[str]` (possibly empty).

- [ ] **Step 1: Write the failing test**

Create `bin/test_parallel_task.sh`:

```bash
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

[[ $fail -eq 0 ]] && echo "all passed" || { echo "FAILURES ABOVE"; exit 1; }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bash bin/test_parallel_task.sh`
Expected: fails at `source` or at the first `parse_start_args` call — the function doesn't exist
yet, and sourcing the script as-is would also immediately hit its own `[[ $# -ge 1 ]] || usage`
top-level guard and exit (there's no `parse_start_args` function and no sourceable guard yet).

- [ ] **Step 3: Extract `parse_start_args`, wire it into `cmd_start`, add the sourceable guard**

Insert this new function into `bin/parallel-task.sh` right before `cmd_start` (i.e., just above
line 122):

```bash
# parse_start_args "$@" -> prints "task<TAB>mode<TAB>base_ref<TAB>ado_ids_json" on success.
# Pure parsing only — no filesystem/network access — so it's testable on its own.
parse_start_args() {
  local -a ticket_ids=()
  local -a positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ticket) ticket_ids+=("$2"); shift 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  set -- "${positional[@]}"
  [[ $# -ge 2 ]] || { echo "error: start needs <task-name> <native|docker> [base-ref]" >&2; return 1; }
  local task="$1" mode="$2" base_ref="${3:-origin/main}"
  local ado_ids_json
  ado_ids_json="$(jq -n --args '$ARGS.positional' -- "${ticket_ids[@]}")"
  printf '%s\t%s\t%s\t%s\n' "$task" "$mode" "$base_ref" "$ado_ids_json"
}
```

Replace the first two lines of `cmd_start` (`bin/parallel-task.sh:123-124`):

```bash
cmd_start() {
  local parsed task mode base_ref ado_ids_json
  parsed="$(parse_start_args "$@")" || usage
  IFS=$'\t' read -r task mode base_ref ado_ids_json <<< "$parsed"
```

The line right after (`[[ "$mode" == "native" || "$mode" == "docker" ]] || ...`, currently line
127) needs no change — it now validates the `mode` variable set by the `read` above instead of
`$2` directly, same check either way.

Update `reg_set_entry` at `bin/parallel-task.sh:185-188` to include `ado_ids`:

```bash
  reg_set_entry "$task" "$(jq -n \
    --arg branch "$branch" --arg path "$wt_path" --arg mode "$mode" \
    --argjson num "$num" --argjson ports "$ports_json" --argjson ado_ids "$ado_ids_json" \
    '{branch:$branch, path:$path, mode:$mode, num:$num, ports:$ports, ado_ids:$ado_ids}')"
```

Update the usage comment at `bin/parallel-task.sh:21`:

```
#   parallel-task.sh start    <task-name> <native|docker> [base-ref] [--ticket <id> ...]
```

Add a sourceable guard around the bottom dispatch (`bin/parallel-task.sh:344-354`) so the script
can be `source`d for its functions without running the CLI dispatch:

```bash
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "$COMMAND" in
    start)    cmd_start "$@" ;;
    dispatch) cmd_dispatch "$@" ;;
    list)     cmd_list "$@" ;;
    stop)     cmd_stop "$@" ;;
    rm)       cmd_rm "$@" ;;
    *)
      echo "error: unknown command '$COMMAND'" >&2
      usage
      ;;
  esac
fi
```

This also means the top-level `[[ $# -ge 1 ]] || usage` at line 37 and the `COMMAND="$1"; shift`
right after it must move inside that same guard (they currently run unconditionally at source
time, which would break `source parallel-task.sh` with zero args in the test). Move lines 37-38
(`[[ $# -ge 1 ]] || usage` and `COMMAND="$1"; shift || true`) down to right before the new
`if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then` block, still outside it (they only assign/validate;
the dispatch guard wraps just the `case`).

- [ ] **Step 4: Run test to verify it passes**

Run: `bash bin/test_parallel_task.sh`
Expected: `all passed`

- [ ] **Step 5: Run the existing full test suite to confirm nothing else broke**

Run: `cd bin && python3 test_dashboard.py && python3 test_escalations.py && python3 test_manager.py`
Expected: all still pass (this task didn't touch Python, but `dashboard.py` shells out to this
script — confirm `parallel-task.sh list --json` still runs standalone: `bash bin/parallel-task.sh list --json`)
Expected: `[]` or the current real registry contents, no bash syntax error.

- [ ] **Step 6: Commit**

```bash
git add bin/parallel-task.sh bin/test_parallel_task.sh
git commit -m "feat: parallel-task.sh start accepts repeatable --ticket <id>"
```

---

### Task 3: Merge registry `ado_ids` with PR-scraped `ado_refs`

**Files:**
- Modify: `bin/dashboard.py:239-249` (`_enrich_branch_and_links`)
- Test: `bin/test_dashboard.py`

**Interfaces:**
- Consumes: `_lookup_pr_and_ticket(branch)["ado_refs"]` (Task 1), a registry entry's
  `ado_ids: list[str] | None` (Task 2).
- Produces: `_enrich_branch_and_links(cwd, known_branch, known_ado_ids=None) -> dict` with an
  `"ado_refs"` key that's the union of the registry's own ids (converted to `{id, url}` via the
  default base) and whatever the PR body/title scrape found, deduplicated by id, registry-first.

- [ ] **Step 1: Write the failing test**

Add `import dashboard` near the top of `bin/test_dashboard.py`, alongside the existing
`from dashboard import ...` line — this test needs the module itself, to swap one of its
functions out temporarily (matching this file's no-framework, no-mocking-library style; every
other test here calls real functions directly).

Add to `bin/test_dashboard.py`:

```python
def test_enrich_merges_registry_ado_ids_with_pr_scraped_refs():
    original_lookup = dashboard._lookup_pr_and_ticket
    original_branch = dashboard._git_branch
    dashboard._git_branch = lambda cwd: "irrelevant"
    dashboard._lookup_pr_and_ticket = lambda branch: {
        "pr_number": 42,
        "pr_url": "https://github.com/x/y/pull/42",
        "pr_state": "OPEN",
        "ado_refs": [{"id": "8165", "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/8165"}],
    }
    try:
        result = dashboard._enrich_branch_and_links("some/path", "feature/x", known_ado_ids=["8172", "8165"])
    finally:
        dashboard._lookup_pr_and_ticket = original_lookup
        dashboard._git_branch = original_branch
    ids = sorted(r["id"] for r in result["ado_refs"])
    assert ids == ["8165", "8172"]
    assert result["pr_number"] == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: `TypeError: _enrich_branch_and_links() got an unexpected keyword argument 'known_ado_ids'`

- [ ] **Step 3: Implement the merge**

Replace `bin/dashboard.py:239-249`:

```python
def _enrich_branch_and_links(cwd: str, known_branch: str | None, known_ado_ids: list[str] | None = None) -> dict:
    """cwd is the empty string for a shared checkout (the repo root, not a dedicated worktree):
    its "current branch" belongs to whichever session last ran `git checkout` there, not to any
    one session, so deriving one would misattribute a PR/ticket to every session sharing it."""
    branch = known_branch
    if not branch and cwd:
        branch = _cached(f"branch:{cwd}", lambda: _git_branch(cwd), ttl=_ENRICH_TTL)
    registry_refs = [{"id": i, "url": _ADO_DEFAULT_BASE + i} for i in (known_ado_ids or [])]
    if not branch:
        merged = list(registry_refs)
        return {"branch": branch, **_EMPTY_PR_TICKET, "ado_refs": merged}
    pr_ticket = _cached(f"prticket:{branch}", lambda: _lookup_pr_and_ticket(branch), ttl=_ENRICH_TTL)
    seen = {r["id"] for r in registry_refs}
    merged = list(registry_refs) + [r for r in pr_ticket["ado_refs"] if r["id"] not in seen]
    return {"branch": branch, **{**pr_ticket, "ado_refs": merged}}
```

Update the two call sites in `get_tasks()` (`bin/dashboard.py:317` and `:330`) to pass the
registry's `ado_ids`:

```python
                **_enrich_branch_and_links(
                    cwd if os.path.realpath(cwd) != repo else "", reg.get("branch"), reg.get("ado_ids")
                ),
```

and

```python
                    **_enrich_branch_and_links(r.get("path", ""), r.get("branch"), r.get("ado_ids")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bin && python3 test_dashboard.py`
Expected: all pass, including the new merge test.

- [ ] **Step 5: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: merge a copy's own registered tickets with PR-scraped ones"
```

---

### Task 4: ADO backlog fetch + shaping

**Files:**
- Modify: `bin/dashboard.py` (new functions, near `get_registry`/`get_sessions`, `:252-278`)
- Test: `bin/test_dashboard.py`

**Interfaces:**
- Produces: `_shape_ado_ticket(raw: dict) -> dict` (pure) returning
  `{"id": str, "title": str, "state": str, "url": str}`; `get_ado_backlog() -> list[dict]`
  (impure, cached 60s, degrades to `[]` on any failure — same pattern as `get_registry`).

- [ ] **Step 1: Write the failing test**

Add to `bin/test_dashboard.py`:

```python
def test_shape_ado_ticket_extracts_known_fields():
    raw = {
        "id": 8148,
        "fields": {
            "System.Id": 8148,
            "System.State": "New",
            "System.Title": "Confirm agent run/trace tracked fields",
        },
    }
    assert _shape_ado_ticket(raw) == {
        "id": "8148",
        "title": "Confirm agent run/trace tracked fields",
        "state": "New",
        "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/8148",
    }


def test_shape_ado_ticket_handles_missing_fields():
    assert _shape_ado_ticket({"id": 1, "fields": {}}) == {
        "id": "1",
        "title": "",
        "state": "",
        "url": "https://dev.azure.com/agentiqai/AgentIQ/_workitems/edit/1",
    }
```

Add `_shape_ado_ticket` to the import line at the top of `bin/test_dashboard.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: `ImportError: cannot import name '_shape_ado_ticket'`

- [ ] **Step 3: Implement**

Add to `bin/dashboard.py`, near `get_registry`/`get_sessions` (after `get_sessions`, before
`get_tasks`, i.e. after current line 278):

```python
_ADO_ORG = "https://dev.azure.com/agentiqai"
_ADO_PROJECT = "AgentIQ"
_ADO_BACKLOG_WIQL = (
    "SELECT [System.Id], [System.Title], [System.State] FROM WorkItems "
    f"WHERE [System.TeamProject] = '{_ADO_PROJECT}' AND [System.AssignedTo] = @Me "
    "AND [System.State] NOT IN ('Closed', 'Removed')"
)


def _shape_ado_ticket(raw: dict) -> dict:
    fields = raw.get("fields") or {}
    ticket_id = str(raw.get("id") or fields.get("System.Id") or "")
    return {
        "id": ticket_id,
        "title": fields.get("System.Title") or "",
        "state": fields.get("System.State") or "",
        "url": _ADO_DEFAULT_BASE + ticket_id,
    }


def get_ado_backlog() -> list[dict]:
    """Tickets assigned to you, not closed — the manager's read-only view into ADO. Any
    failure (az not authenticated, network down) degrades to an empty backlog, same as every
    other subprocess-backed source in this file — a dashboard that can't reach ADO still shows
    live sessions."""

    def run():
        result = subprocess.run(
            ["az", "boards", "query", "--org", _ADO_ORG, "--wiql", _ADO_BACKLOG_WIQL, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            return []
        return [_shape_ado_ticket(r) for r in json.loads(result.stdout)]

    try:
        return _cached("ado_backlog", run, ttl=60.0)
    except (OSError, json.JSONDecodeError):
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bin && python3 test_dashboard.py`
Expected: all pass.

- [ ] **Step 5: Add the `GET /api/ado-tickets` route**

In `bin/dashboard.py`'s `Handler.do_GET` (`:360-406`), add a branch alongside the existing
`/api/escalations` one:

```python
        if parsed.path == "/api/ado-tickets":
            try:
                self._json(get_ado_backlog())
            except Exception as e:
                self._json({"error": str(e)}, status=500)
            return
```

- [ ] **Step 6: Verify live**

Run: `cd bin && python3 dashboard.py 4401 /home/azureuser/aiq/aiquinta-platform &` then
`curl -s http://127.0.0.1:4401/api/ado-tickets | python3 -m json.tool`
Expected: the 4 real backlog tickets, same ones seen during the design's data-source check.
Kill the test server after: `kill %1` (or `pkill -f "dashboard.py 4401"`).

- [ ] **Step 7: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: expose the ADO backlog assigned to the manager over the dashboard API"
```

---

### Task 5: HTML-stripping + ticket-description fetch

**Files:**
- Modify: `bin/dashboard.py`
- Test: `bin/test_dashboard.py`

**Interfaces:**
- Produces: `_strip_html(raw: str) -> str` (pure); `_fetch_ado_description(ticket_id: str) -> str`
  (impure, degrades to `""` on failure).

- [ ] **Step 1: Write the failing test**

```python
def test_strip_html_removes_tags_and_unescapes_entities():
    raw = "<h2><b>Title</b></h2><div>Body&nbsp;text with <i>emphasis</i>.</div>"
    assert _strip_html(raw) == "Title Body text with emphasis ."


def test_strip_html_collapses_whitespace():
    assert _strip_html("<p>a</p>\n\n<p>   b   </p>") == "a b"


def test_strip_html_handles_empty_string():
    assert _strip_html("") == ""
```

Add `_strip_html` to the import line.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: `ImportError: cannot import name '_strip_html'`

Note: hand-verify the first expected string against Python's actual `re.sub(r"\s+", " ", ...)`
behavior before trusting it — whitespace-collapsing edge cases (e.g. a trailing space before a
period) are exactly where an assert-first test earns its keep. Adjust the expected string to match
what the implementation below actually produces if it differs after Step 4's run, and re-run.

- [ ] **Step 3: Implement**

Add near the top of `bin/dashboard.py`, after the `import re` line:

```python
import html
```

Add the function (near `_shape_ado_ticket`, from Task 4):

```python
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_ado_description(ticket_id: str) -> str:
    try:
        result = subprocess.run(
            ["az", "boards", "work-item", "show", "--id", ticket_id, "--org", _ADO_ORG, "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ""
        fields = json.loads(result.stdout).get("fields") or {}
        return _strip_html(fields.get("System.Description") or "")
    except (OSError, json.JSONDecodeError):
        return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bin && python3 test_dashboard.py`
Expected: all pass (adjust the whitespace-collapse test's expected string first if Step 2's note
applied).

- [ ] **Step 5: Verify `_fetch_ado_description` live against a real ticket**

Run: `cd bin && python3 -c "from dashboard import _fetch_ado_description; print(_fetch_ado_description('8148')[:200])"`
Expected: plain-text description content, no `<tags>` visible.

- [ ] **Step 6: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: strip ADO's rich-text description down to plain text for prompts"
```

---

### Task 6: Dispatch prompt + task-slug builders

**Files:**
- Modify: `bin/dashboard.py`
- Test: `bin/test_dashboard.py`

**Interfaces:**
- Produces: `_ticket_task_slug(title: str) -> str` (pure, kebab-case, ASCII, max ~40 chars);
  `_build_dispatch_prompt(tickets: list[dict], instructions: str) -> str` (pure). Each ticket dict
  here has `{"id", "title", "description"}`.

- [ ] **Step 1: Write the failing test**

```python
def test_ticket_task_slug_kebab_cases_and_truncates():
    assert _ticket_task_slug("Fix the Setpoint Guard!") == "fix-the-setpoint-guard"
    long_title = "A very long ticket title that goes on and on past forty characters easily"
    slug = _ticket_task_slug(long_title)
    assert len(slug) <= 40
    assert slug == slug.lower()
    assert " " not in slug


def test_ticket_task_slug_handles_non_ascii():
    assert _ticket_task_slug("Cùng 1 input, câu trả lời không đổi") != ""


def test_build_dispatch_prompt_includes_all_ticket_content_and_instructions():
    tickets = [
        {"id": "8172", "title": "Fix setpoint guard", "description": "Root cause is X."},
        {"id": "8165", "title": "Related follow-up", "description": "Second half of the fix."},
    ]
    prompt = _build_dispatch_prompt(tickets, "Focus on the backend only, skip the UI part.")
    assert "AB#8172" in prompt
    assert "Fix setpoint guard" in prompt
    assert "Root cause is X." in prompt
    assert "AB#8165" in prompt
    assert "Second half of the fix." in prompt
    assert "Focus on the backend only, skip the UI part." in prompt
    assert "verify" in prompt.lower()  # the Hermes verify-before-done reminder is present


def test_build_dispatch_prompt_omits_instructions_section_when_blank():
    tickets = [{"id": "1", "title": "T", "description": "D"}]
    prompt = _build_dispatch_prompt(tickets, "")
    assert "Extra instructions" not in prompt
```

Add both functions to the import line.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: `ImportError: cannot import name '_ticket_task_slug'`

- [ ] **Step 3: Implement**

```python
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


def _ticket_task_slug(title: str) -> str:
    ascii_title = title.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP_RE.sub("-", ascii_title.lower()).strip("-")
    return slug[:40].strip("-") or "ticket"


def _build_dispatch_prompt(tickets: list[dict], instructions: str) -> str:
    sections = []
    for t in tickets:
        sections.append(f"AB#{t['id']}: {t['title']}\n{t['description']}")
    body = "\n\n".join(sections)
    parts = [
        "You've been assigned the following ticket(s):",
        body,
    ]
    if instructions.strip():
        parts.append(f"Extra instructions from the manager:\n{instructions.strip()}")
    parts.append(
        "Follow this repo's CLAUDE.md conventions. Before reporting done: run the relevant "
        "tests and confirm they're green, and verify the actual behavior — don't mark this "
        "complete on a self-report alone."
    )
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bin && python3 test_dashboard.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: build a dispatch prompt and task slug from one or more tickets"
```

---

### Task 7: `POST /api/tickets/dispatch` endpoint

**Files:**
- Modify: `bin/dashboard.py` (`Handler.do_POST`, `:408-455`)
- Test: `bin/test_dashboard.py`

**Interfaces:**
- Produces: `_build_dispatch_argv(slug: str, mode: str, ticket_ids: list[str]) -> list[str]` (pure)
  — the argv for `parallel-task.sh start`.
- Consumes: `_ticket_task_slug`, `_build_dispatch_prompt`, `_fetch_ado_description` (Tasks 5-6).

- [ ] **Step 1: Write the failing test**

```python
def test_build_dispatch_argv_includes_one_ticket_flag_per_id():
    argv = _build_dispatch_argv("fix-setpoint-guard", "native", ["8172", "8165"])
    assert argv == [
        PARALLEL_TASK_SH,
        "start",
        "fix-setpoint-guard",
        "native",
        "--ticket",
        "8172",
        "--ticket",
        "8165",
    ]


def test_build_dispatch_argv_with_no_tickets():
    argv = _build_dispatch_argv("some-task", "native", [])
    assert argv == [PARALLEL_TASK_SH, "start", "some-task", "native"]
```

Add `_build_dispatch_argv` to the import line.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd bin && python3 test_dashboard.py`
Expected: `ImportError: cannot import name '_build_dispatch_argv'`

- [ ] **Step 3: Implement `_build_dispatch_argv` and the endpoint**

Add near the other builders:

```python
def _build_dispatch_argv(slug: str, mode: str, ticket_ids: list[str]) -> list[str]:
    argv = [PARALLEL_TASK_SH, "start", slug, mode]
    for tid in ticket_ids:
        argv += ["--ticket", tid]
    return argv
```

In `Handler.do_POST`, add a new branch before the final `self.send_response(404)`:

```python
        if parsed.path == "/api/tickets/dispatch":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._json({"error": "request body too large"}, status=413)
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                ticket_ids = [str(i) for i in (body.get("ticket_ids") or [])]
                instructions = str(body.get("instructions") or "")
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": f"bad request body: {e}"}, status=400)
                return
            if not ticket_ids:
                self._json({"error": "ticket_ids is required and must be non-empty"}, status=400)
                return

            backlog_by_id = {t["id"]: t for t in get_ado_backlog()}
            tickets = []
            for tid in ticket_ids:
                title = backlog_by_id.get(tid, {}).get("title", f"ticket {tid}")
                tickets.append({"id": tid, "title": title, "description": _fetch_ado_description(tid)})

            slug = _ticket_task_slug(tickets[0]["title"])
            prompt = _build_dispatch_prompt(tickets, instructions)
            start_argv = _build_dispatch_argv(slug, "native", ticket_ids)

            start_result = subprocess.run(start_argv, capture_output=True, text=True, cwd=REPO_DIR, timeout=180)
            if start_result.returncode != 0:
                self._json({"error": f"start failed: {start_result.stderr[-2000:]}"}, status=500)
                return

            dispatch_result = subprocess.run(
                [PARALLEL_TASK_SH, "dispatch", slug, prompt], capture_output=True, text=True, cwd=REPO_DIR, timeout=60
            )
            if dispatch_result.returncode != 0:
                self._json({"error": f"dispatch failed: {dispatch_result.stderr[-2000:]}"}, status=500)
                return

            self._json({"ok": True, "task": slug})
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd bin && python3 test_dashboard.py`
Expected: all pass.

- [ ] **Step 5: Verify the error path live (no real dispatch)**

Run:
```bash
cd bin && python3 dashboard.py 4401 /home/azureuser/aiq/aiquinta-platform &
curl -s -X POST http://127.0.0.1:4401/api/tickets/dispatch -H 'Content-Type: application/json' -d '{"ticket_ids": []}'
```
Expected: `{"error": "ticket_ids is required and must be non-empty"}`, HTTP 400. Kill the test
server: `pkill -f "dashboard.py 4401"`.

**Do not** POST a real, non-empty `ticket_ids` in this verification step — that triggers a real
worktree + dev-stack provisioning (10–60s, consumes real ports/resources) and a real dispatched
session. Confirming the full success path with a real ticket is a deliberate action for whoever
uses the shipped feature, not part of this task's verification.

- [ ] **Step 6: Commit**

```bash
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: add POST /api/tickets/dispatch"
```

---

### Task 8: Client — ADO backlog fetch, ticket-centric `computeTickets`, always-shown links

**Files:**
- Modify: `bin/dashboard.html` (script section: `computeTickets`, `renderTickets`, `pollTasks`/
  `pollEscalations` wiring, plus a new `pollAdoTickets`)

**Interfaces:**
- Consumes: `GET /api/ado-tickets` (Task 4) → `list[{"id","title","state","url"}]`; each task row
  from `GET /api/tasks` now carries `ado_refs: list[{"id","url"}]` instead of `ado_id`/`ado_url`
  (Tasks 1 & 3).
- Produces: `renderTickets` rows carry `{label, href, prTitle, prHref, title, status, action,
  actionHref}` — `label`/`href` is the `AB#NNNN` link, `prTitle`/`prHref` is the separate, always-
  shown `PR #NNN` link (present only once a PR exists).

- [ ] **Step 1: Add the ADO-backlog poll**

In `bin/dashboard.html`'s `<script>`, add a new global near `lastEscalations`:

```js
let lastAdoTickets = [];
```

Add a new poll function near `pollEscalations`:

```js
async function pollAdoTickets() {
  try {
    const res = await fetch("/api/ado-tickets");
    const data = await res.json();
    if (checkResponse(res, data)) return;
    lastAdoTickets = data;
    renderTickets(computeTickets(lastAdoTickets, lastData, lastEscalations));
  } catch (e) { /* leave the last-known backlog in place on a transient failure */ }
}
```

Add it to the startup/interval block at the bottom (near the existing `pollTasks()`/
`setInterval(pollTasks, 2000)` calls):

```js
pollAdoTickets();
setInterval(pollAdoTickets, 5000);
```

(5s, not 2s — the backlog is server-cached for 60s already; polling faster than the cache TTL
buys nothing.)

- [ ] **Step 2: Rewrite `computeTickets` to iterate the ADO backlog**

Replace the existing `computeTickets` function entirely:

```js
function computeTickets(adoTickets, tasks, esc) {
  const escBySession = {};
  (esc.needs_human || []).forEach(e => { if (e.session_id) escBySession[e.session_id] = e; });

  const rows = [];
  for (const ticket of adoTickets) {
    const matched = tasks.filter(t => (t.ado_refs || []).some(r => r.id === ticket.id));
    const pendingEsc = matched.map(t => escBySession[t.session_id]).find(Boolean);
    const withPr = matched.find(t => t.pr_url) || matched[0];

    let status, action, actionHref = null;
    if (pendingEsc) {
      status = "decision"; action = pendingEsc.question || "Needs a decision";
    } else if (matched.some(t => stateOf(t) === "blocked")) {
      status = "blocked"; action = "Check the blocker";
    } else if (!matched.length) {
      status = "not-started"; action = "Giao việc";
    } else if (withPr && withPr.pr_state === "MERGED") {
      status = "done"; action = "—";
    } else if (withPr && withPr.pr_state === "OPEN") {
      status = "progress"; action = "Review PR #" + withPr.pr_number; actionHref = withPr.pr_url;
    } else {
      status = "progress"; action = "—";
    }

    rows.push({
      id: ticket.id,
      label: "AB#" + ticket.id,
      href: ticket.url,
      title: ticket.title,
      prLabel: withPr && withPr.pr_number ? "PR #" + withPr.pr_number : null,
      prHref: withPr ? withPr.pr_url : null,
      status, action, actionHref,
    });
  }
  const rank = { decision: 0, blocked: 1, "not-started": 2, progress: 3, done: 4 };
  rows.sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9));
  return rows;
}
```

- [ ] **Step 3: Update `TICKET_STATUS_LABEL` and `renderTickets` for the new fields**

Replace the `TICKET_STATUS_LABEL` constant:

```js
const TICKET_STATUS_LABEL = {
  done: "Done", decision: "Needs your decision", blocked: "Blocked",
  progress: "In progress", "not-started": "Not started",
};
```

Replace `renderTickets`:

```js
function renderTickets(rows) {
  const box = document.getElementById("tickets");
  if (!rows.length) {
    box.replaceChildren(el("div", "empty", "No tickets assigned — nothing to show yet."));
    return;
  }
  const frag = document.createDocumentFragment();
  rows.forEach(r => {
    const row = el("div", "ticket-row");
    const left = el("div", "ticket-left");
    const idLink = el("a", "ticket-id", r.label);
    idLink.href = r.href; idLink.target = "_blank"; idLink.rel = "noopener";
    left.append(idLink);
    if (r.prLabel) {
      const prLink = el("a", "ticket-id", r.prLabel);
      prLink.href = r.prHref; prLink.target = "_blank"; prLink.rel = "noopener";
      left.append(prLink);
    }
    left.append(el("span", "ticket-title", r.title || ""));
    row.append(left, pill(r.status, TICKET_STATUS_LABEL[r.status] || r.status));

    if (r.status === "not-started") {
      const btn = el("button", "btn primary", "Giao việc");
      btn.onclick = () => openDispatchForm(row, r);
      row.append(btn);
    } else if (r.actionHref) {
      const a = el("a", "ticket-action", r.action);
      a.href = r.actionHref; a.target = "_blank"; a.rel = "noopener";
      row.append(a);
    } else {
      row.append(el("span", "ticket-action" + (r.action === "—" ? " muted" : ""), r.action));
    }
    frag.append(row);
  });
  box.replaceChildren(frag);
}
```

`openDispatchForm` is defined in Task 9 — this task will show as a JS error in the console
(`openDispatchForm is not defined`) until Task 9 lands; that's expected and resolved there.

- [ ] **Step 4: Remove the now-stale `computeTickets`/`renderTickets` call sites that used the old
  signature**

In `renderAll()`, remove the line `renderTickets(computeTickets(lastData, lastEscalations));` —
ticket rendering is now driven by `pollAdoTickets` (Step 1), not by every task/escalation poll
tick. In `pollEscalations`, replace the line `renderTickets(computeTickets(lastData,
lastEscalations));` at the end with `renderTickets(computeTickets(lastAdoTickets, lastData,
lastEscalations));` so an escalation update also refreshes ticket status immediately rather than
waiting up to 5s for the next ADO poll.

- [ ] **Step 5: Verify live**

Restart the dashboard (`pkill -f "dashboard.py 4400"`, relaunch as done earlier this session),
open it in a browser or via the Playwright MCP tools, and confirm: the 4 real backlog tickets
render as "Not started" rows with a "Giao việc" button, no JS console errors other than the
expected `openDispatchForm is not defined` (resolved in Task 9).

- [ ] **Step 6: Commit**

```bash
git add bin/dashboard.html
git commit -m "feat: drive the Tickets panel from the ADO backlog, not from live sessions"
```

---

### Task 9: Client — inline dispatch form + collapsible operator section

**Files:**
- Modify: `bin/dashboard.html` (new `openDispatchForm`, CSS for the collapsible section and the
  dispatch form, HTML restructuring of `<main id="content">`)

**Interfaces:**
- Produces: `openDispatchForm(rowEl, ticketRow)` — expands `rowEl` in place with a form; posts to
  `/api/tickets/dispatch` (Task 7) with `{ticket_ids, instructions}`.

- [ ] **Step 1: Implement `openDispatchForm`**

Add to the `<script>` section:

```js
function openDispatchForm(rowEl, ticket) {
  if (rowEl.nextSibling && rowEl.nextSibling.classList && rowEl.nextSibling.classList.contains("dispatch-form")) {
    rowEl.nextSibling.remove();
    return; // toggle closed if already open
  }
  const bundleCandidates = computeTickets(lastAdoTickets, lastData, lastEscalations)
    .filter(r => r.status === "not-started" && r.id !== ticket.id);

  const form = el("div", "dispatch-form");
  const checks = el("div", "dispatch-bundle");
  const checkboxByIds = {};
  bundleCandidates.forEach(c => {
    const label = el("label", "dispatch-bundle-item");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = c.id;
    checkboxByIds[c.id] = cb;
    label.append(cb, document.createTextNode(" " + c.label + " — " + c.title));
    checks.append(label);
  });
  if (bundleCandidates.length) form.append(el("div", "dispatch-hint", "Bundle with related not-started tickets:"), checks);

  const textarea = document.createElement("textarea");
  textarea.className = "dispatch-instructions";
  textarea.placeholder = "Extra instructions (optional)";
  form.append(textarea);

  const status = el("div", "dispatch-status");
  const actions = el("div", "dispatch-actions");
  const confirmBtn = el("button", "btn primary", "Xác nhận & giao");
  const cancelBtn = el("button", "btn", "Hủy");
  cancelBtn.onclick = () => form.remove();
  confirmBtn.onclick = async () => {
    const ticketIds = [ticket.id, ...Object.keys(checkboxByIds).filter(id => checkboxByIds[id].checked)];
    confirmBtn.disabled = true; confirmBtn.textContent = "Đang tạo…";
    status.textContent = "";
    try {
      const res = await fetch("/api/tickets/dispatch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticket_ids: ticketIds, instructions: textarea.value }),
      });
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      form.remove();
      pollTasks();
    } catch (e) {
      status.textContent = "Lỗi: " + (e.message || e);
      confirmBtn.disabled = false; confirmBtn.textContent = "Xác nhận & giao";
    }
  };
  actions.append(confirmBtn, cancelBtn);
  form.append(actions, status);
  rowEl.after(form);
}
```

- [ ] **Step 2: Add CSS for the dispatch form**

Add to the `<style>` block, near the existing `.ticket-*` rules:

```css
.dispatch-form { padding: 14px 22px; border-top: 1px solid var(--line); background: #FBFCFE; display: flex; flex-direction: column; gap: 10px; }
.dispatch-hint { font-size: 12.5px; color: var(--muted); }
.dispatch-bundle { display: flex; flex-direction: column; gap: 6px; }
.dispatch-bundle-item { font-size: 13px; display: flex; align-items: center; gap: 8px; }
.dispatch-instructions { width: 100%; min-height: 64px; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font-family: var(--sans); font-size: 13px; resize: vertical; }
.dispatch-actions { display: flex; gap: 9px; }
.dispatch-status { font-size: 12.5px; color: var(--stop-fg); }
```

- [ ] **Step 3: Wrap the operator section in a collapsed-by-default disclosure**

In the HTML body, wrap everything from `<section class="stats" id="stats">` through the
"Decided for you" panel (i.e. `<section class="stats" ...></section>`, the `<div class="cols">...
</div>` block, the Live logs `<section>`, and the "Decided for you" `<section>`) inside:

```html
<details id="operator-section">
  <summary>Chi tiết vận hành</summary>
  <!-- existing stats / cols / live-logs / decided-for-you sections go here, unchanged -->
</details>
```

Add CSS so the `<summary>` reads as a normal disclosure control matching the rest of the UI:

```css
#operator-section { margin-top: 4px; }
#operator-section summary {
  cursor: pointer; padding: 10px 4px; font-size: 13.5px; font-weight: 600; color: var(--muted);
  list-style: none;
}
#operator-section summary::-webkit-details-marker { display: none; }
#operator-section summary::before { content: "▸ "; }
#operator-section[open] summary::before { content: "▾ "; }
```

Update the summary text to include the live session count — add a small script snippet inside
`renderStats` (or right after it's called) that sets the summary text to
`` `Chi tiết vận hành (${data.length})` ``: in `renderStats(data)`, after the existing body, add:

```js
  const summary = document.querySelector("#operator-section summary");
  if (summary) summary.textContent = `Chi tiết vận hành (${data.length})`;
```

(A native `<details>` needs no JS to open/close — the browser handles that; this only keeps the
count in the label current.)

- [ ] **Step 4: Verify live end-to-end**

Restart the dashboard, load it (browser or Playwright MCP), confirm:
- The operator section is collapsed on load, expands on click, and its label shows a session
  count.
- Clicking "Giao việc" on a Not-started ticket expands an inline form with a textarea and (if
  other not-started tickets exist) bundle checkboxes.
- Clicking a different "Giao việc" while one form is open, or clicking the same one again, doesn't
  leave two forms open under the same row (the toggle-closed branch in `openDispatchForm` Step 1
  handles the same-row case; confirm visually that opening a second row's form doesn't collide).
- Do **not** click "Xác nhận & giao" for real during this check — same reasoning as Task 7 Step 5,
  a live provisioning run is a deliberate action for later, not a verification step here.

- [ ] **Step 5: Commit**

```bash
git add bin/dashboard.html
git commit -m "feat: dispatch tickets from the dashboard, collapse the operator view by default"
```

---

### Task 10: A sixth status rung — a dead session with no PR reads "Stalled", not "In progress" forever

Added after the final whole-branch review (Important finding #4): a dispatched session that exits
without ever opening a PR currently falls through every status check to the terminal `else` in
`computeTickets` — "In progress", action "—", indefinitely. For a redesign whose whole point is
"stop tracking work manually," a silently-dead stream reading as healthy is exactly the failure
this feature exists to prevent.

**Files:**
- Modify: `bin/dashboard.html` (`computeTickets`, `TICKET_STATUS_LABEL`, `renderTickets`)

**Interfaces:**
- Consumes: a matched task row's `kind` field — `null` means "registered copy whose session
  already exited" (set explicitly by `dashboard.py`'s `get_tasks()` second loop), a non-null value
  (`"interactive"`/`"background"`) means a live session. No backend change needed — this signal
  already exists in every `/api/tasks` row.
- Produces: a new `"stalled"` status value in the rank table, ranked between `blocked` and
  `not-started` (a dead run is worse than one that never started).

- [ ] **Step 1: Add the stalled branch to `computeTickets`**

In `computeTickets` (`bin/dashboard.html`), insert a new condition after the blocked check and
before the not-started check:

```js
} else if (matched.length && matched.every(t => t.kind === null) && !matched.some(t => t.pr_url)) {
  status = "stalled"; action = "Giao lại việc";
```

(Insert this as a new `else if` branch between the existing `matched.some(g => stateOf(g) ===
"blocked")` branch and the `!matched.length` branch — the full updated chain reads: decision →
blocked → stalled → not-started → done/in-progress, each an `else if` off the same `if`.)

Update the rank table:

```js
const rank = { decision: 0, blocked: 1, stalled: 2, "not-started": 3, progress: 4, done: 5 };
```

- [ ] **Step 2: Add the status label**

In `TICKET_STATUS_LABEL`, add:

```js
stalled: "Stalled",
```

- [ ] **Step 3: Render the re-dispatch button for a stalled ticket**

In `renderTickets`, the current action-rendering branch is:

```js
if (r.status === "not-started") {
  const btn = el("button", "btn primary", "Giao việc");
  btn.onclick = () => openDispatchForm(row, r);
  row.append(btn);
}
```

Extend the condition to also cover `stalled`, reusing the same `openDispatchForm` (a stalled
ticket is re-dispatched exactly like a not-started one — same form, same endpoint):

```js
if (r.status === "not-started" || r.status === "stalled") {
  const btn = el("button", "btn primary", r.status === "stalled" ? "Giao lại việc" : "Giao việc");
  btn.onclick = () => openDispatchForm(row, r);
  row.append(btn);
}
```

- [ ] **Step 4: Add a pill color for `stalled`**

In the CSS, add (reusing the existing `--stop-bg`/`--stop-fg` red already used for `blocked` — a
dead run and an active blocker are both "something's wrong, needs you," and sharing the color is
intentional, not an oversight):

```css
.pill.stalled { background: var(--stop-bg); color: var(--stop-fg); }
```

Add `"stalled"` to the `pill()` function's dot-eligible list (the array checked before appending
the small colored dot) alongside the existing status names.

- [ ] **Step 5: Verify live**

Restart the dashboard, confirm the 4 real backlog tickets still render as "Not started" (none are
currently stalled — there's no dead-session case in the live data right now, so this rung can't be
exercised against real data). Instead verify by hand-tracing: temporarily add a synthetic call to
`computeTickets` in the browser console with a fake `adoTickets`/`tasks` pair matching the stalled
condition (a ticket whose only matched task has `kind: null` and `pr_url: null`), confirm it
renders "Stalled" with a "Giao lại việc" button, then reload the page (don't leave the console
override in place).

- [ ] **Step 6: Commit**

```bash
git add bin/dashboard.html
git commit -m "feat: add a Stalled status for a dead dispatch with no PR"
```

---

### Task 11: Full-suite regression check and PR

- [ ] **Step 1: Run every test file**

```bash
cd bin && python3 test_dashboard.py && python3 test_escalations.py && python3 test_manager.py && bash test_parallel_task.sh
```
Expected: all pass.

- [ ] **Step 2: Live smoke test of the read paths**

```bash
pkill -f "dashboard.py 4400" 2>/dev/null
cd /home/azureuser/projects/claude-parallel-worktree-plugin/bin
setsid nohup python3 dashboard.py 4400 /home/azureuser/aiq/aiquinta-platform > /tmp/dashboard-smoke.log 2>&1 < /dev/null &
disown -a
sleep 3
curl -s http://127.0.0.1:4400/api/ado-tickets | python3 -m json.tool | head -20
curl -s http://127.0.0.1:4400/api/tasks | python3 -m json.tool | head -20
```
Expected: both return real data, no 500s, `dashboard-smoke.log` has no traceback.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feature/agent-status-dashboard
gh pr create --title "Ticket-centric dashboard: ADO backlog, dispatch-from-UI, collapsed operator view" --body "$(cat <<'EOF'
## Summary
- Tickets panel now driven by the ADO backlog (assigned to the manager, not closed), merged with
  live session/PR status — Not started / In progress / Blocked / Needs your decision / Done.
- "Giao việc" dispatches a not-started ticket (optionally bundled with others) straight from the
  dashboard, with an optional extra-instructions field.
- Every ticket row always shows both its ADO link and its PR link (once one exists), independent
  of status.
- Raw session table, stat cards, live logs, and the manager's audit feed move behind one
  collapsed-by-default disclosure — the ticket view is what loads first.

Spec: docs/superpowers/specs/2026-08-27-manager-dashboard-tickets-design.md

## Test plan
- [ ] `python3 test_dashboard.py` — all pass
- [ ] `bash test_parallel_task.sh` — all pass
- [ ] Dashboard loads, Tickets panel shows real ADO backlog
- [ ] "Giao việc" opens the inline form; bundling checkboxes appear when other not-started
      tickets exist
- [ ] Operator section collapsed by default, expands on click
EOF
)"
```
