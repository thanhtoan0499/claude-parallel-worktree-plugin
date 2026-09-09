# Agent Status Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the plugin's Agent-tool subagent dispatch with independent, addressable top-level Claude Code sessions (`claude --bg`), and add a local web dashboard showing every running task's live status and output.

**Architecture:** `parallel-task.sh` gains a `dispatch` subcommand that launches `claude --bg` in the task's worktree and records its `short_id`/`session_id` in the registry, and a `list --json` flag that merges registry data with live `claude agents --json` status. A new stdlib-only `bin/dashboard.py` HTTP server reads `list --json` for the task table and parses each task's session transcript JSONL for its live log, served to a static `bin/dashboard.html` page.

**Tech Stack:** Bash (existing plugin convention) + Python 3 stdlib only (`http.server`, `json`, `subprocess`) — no new dependencies, no build step, matching this plugin's existing zero-dependency bin/*.sh tooling.

**Spec:** `docs/superpowers/specs/2026-08-21-agent-status-dashboard-design.md`

## Global Constraints

- No new runtime dependencies — Python stdlib only, no npm/pip packages, no build step (spec: "matches the plugin's existing zero-dependency bash tooling").
- `claude agents --json` calls MUST include `--all` — without it, finished background sessions are omitted (verified live 2026-08-21; a `--bg` session that already completed only appeared in `--all` output).
- Registry field names are fixed by this plan and must not drift between tasks: `short_id`, `session_id` (Task 1) → consumed as-is by `list --json` (Task 2) → consumed as-is by `dashboard.py` (Task 4).
- `list --json` output field names are fixed: `task`, `branch`, `path`, `mode`, `num`, `ports`, `short_id`, `session_id`, `dev_status` (`"running"`/`"stopped"`), `agent_status` (from `claude agents --json`'s `status`, or `null`), `agent_state` (from its `state`, or `null`).
- This repo has no bash test framework (matches existing `bin/*.sh` precedent — no tests today). Bash tasks below use an exact manual smoke-check command + expected output as their "test," run before implementation (expected to fail/error) and after (expected to succeed) — same red/green discipline, no framework. Only `dashboard.py`'s transcript-rendering logic gets real `assert`-based tests (`bin/test_dashboard.py`), per the spec's Testing section.
- Dashboard binds `127.0.0.1` only, default port `4400` (chosen clear of all existing port ranges: docker `8081/5173/5432/10000 + N*100`, native `8500+N`/`5500+N`).
- Branch name: `feature/agent-status-dashboard`, created off `master` before Task 1's first commit.

---

### Task 1: Registry schema + `parallel-task.sh dispatch` subcommand

**Files:**
- Modify: `bin/parallel-task.sh` (add `reg_merge_entry` helper, add `cmd_dispatch`, wire into the `case` dispatcher, update the top-of-file usage comment)

**Interfaces:**
- Consumes: existing `reg_get`, `REGISTRY`, `REPO_ROOT` from the current file.
- Produces: `reg_merge_entry <task-name> <json-fragment>` (merges a JSON object into an existing registry entry — used by Task 1 itself and available for any future subcommand). `cmd_dispatch <task-name> <prompt>` — on success, writes `short_id` and `session_id` string fields into that task's registry entry. Later tasks (2, 4) read `.session_id`, `.short_id`, `.path`, `.mode`, `.num`, `.ports.gateway` from registry entries — these field names must not change.

- [ ] **Step 1: Create a branch and confirm current state**

```bash
git -C ~/projects/claude-parallel-worktree-plugin checkout -b feature/agent-status-dashboard
git -C ~/projects/claude-parallel-worktree-plugin status
```

Expected: new branch created off `master`, working tree clean except the pre-existing untracked `.claude-plugin/marketplace.json` and `docs/` (leave those — `docs/` is this plan + the spec, `.claude-plugin/marketplace.json` is unrelated local dev tooling from an earlier session; do not delete or stage the marketplace.json file as part of this feature).

- [ ] **Step 2: Write the smoke-check for `dispatch` and confirm it fails (red)**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -c 'cmd_dispatch' bin/parallel-task.sh
```

Expected: `0` (function doesn't exist yet) — this is the "red" state.

- [ ] **Step 3: Add `reg_merge_entry` helper**

In `bin/parallel-task.sh`, immediately after the existing `reg_del_entry()` function (right before the `# --- port / slot liveness checks ---` comment), add:

```bash
reg_merge_entry() {
  # reg_merge_entry <task-name> <json-object-to-merge-in>
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/parallel-task-registry.XXXXXX.json")"
  jq --arg k "$1" --argjson v "$2" '.[$k] += $v' "$REGISTRY" > "$tmp"
  mv "$tmp" "$REGISTRY"
}
```

- [ ] **Step 4: Add `cmd_dispatch`**

In `bin/parallel-task.sh`, immediately after the existing `cmd_rm()` function (right before the `case "$COMMAND" in` dispatcher at the bottom), add:

```bash
cmd_dispatch() {
  [[ $# -ge 2 ]] || { echo "error: dispatch needs <task-name> <prompt>" >&2; usage; }
  local task="$1"; shift
  local prompt="$1"
  [[ "$(reg_get --arg k "$task" 'has($k)')" == "true" ]] || { echo "error: unknown task '$task' (see: $0 list)" >&2; exit 1; }
  local wt_path
  wt_path="$(reg_get --arg k "$task" '.[$k].path')"

  local launch_out
  if ! launch_out="$( cd "$wt_path" && claude --bg -n "$task" "$prompt" 2>&1 )"; then
    echo "error: claude --bg failed to launch for '$task':" >&2
    echo "$launch_out" >&2
    exit 1
  fi

  local short_id
  if [[ "$launch_out" =~ backgrounded[[:space:]]·[[:space:]]([a-f0-9]+)[[:space:]]· ]]; then
    short_id="${BASH_REMATCH[1]}"
  else
    echo "error: could not find a 'backgrounded · <id> · ...' line in claude --bg output for '$task':" >&2
    echo "$launch_out" >&2
    exit 1
  fi

  local session_id
  session_id="$(claude agents --json --all \
    | jq -r --arg n "$task" '[.[] | select(.name==$n)] | sort_by(.startedAt) | last | .sessionId // empty')" || true
  if [[ -z "$session_id" ]]; then
    echo "error: dispatched '$task' (short id $short_id) but could not resolve its session_id via 'claude agents --json'" >&2
    exit 1
  fi

  reg_merge_entry "$task" "$(jq -n --arg sid "$short_id" --arg fid "$session_id" '{short_id:$sid, session_id:$fid}')"
  echo ">> $task dispatched: short id $short_id  session $session_id"
}
```

Wire it into the dispatcher — change:

```bash
case "$COMMAND" in
  start) cmd_start "$@" ;;
  list)  cmd_list "$@" ;;
  stop)  cmd_stop "$@" ;;
  rm)    cmd_rm "$@" ;;
```

to:

```bash
case "$COMMAND" in
  start)    cmd_start "$@" ;;
  dispatch) cmd_dispatch "$@" ;;
  list)     cmd_list "$@" ;;
  stop)     cmd_stop "$@" ;;
  rm)       cmd_rm "$@" ;;
```

Also update the top-of-file usage comment block (the lines the `usage()` function prints via `sed -n '2,20p'`) — change:

```
# Usage:
#   parallel-task.sh start <task-name> <native|docker> [base-ref]
#   parallel-task.sh list
#   parallel-task.sh stop  <task-name>
#   parallel-task.sh rm    <task-name> [--force]
```

to:

```
# Usage:
#   parallel-task.sh start    <task-name> <native|docker> [base-ref]
#   parallel-task.sh dispatch <task-name> <prompt>
#   parallel-task.sh list     [--json]
#   parallel-task.sh stop     <task-name>
#   parallel-task.sh rm       <task-name> [--force]
```

- [ ] **Step 5: Run the smoke-check again, confirm green**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -c 'cmd_dispatch' bin/parallel-task.sh
bash -n bin/parallel-task.sh
```

Expected: first command prints `2` or more (function definition + case wiring), second command (bash syntax check) prints nothing and exits 0.

- [ ] **Step 6: End-to-end smoke test against a real task**

`parallel-task.sh start <task> native` does NOT work in this repo — it has no `package.json`/`docker-compose.yml` of its own for `dev-native.sh`/`dev-stack.sh` to manage (this plugin's own README says as much: `bin/*.sh` is a reference implementation for repos shaped like the one it was extracted from, not for this repo itself). This is true for every task in this plan that needs a live registry entry — bypass `cmd_start` with a manual worktree + registry seed that still exercises the REAL `cmd_dispatch`/`list_json` logic under test:

```bash
cd ~/projects/claude-parallel-worktree-plugin
TASK=dispatch-smoke
WT=".claude/worktrees/$TASK"
git worktree add "$WT" -b "feature/$TASK" master
jq --arg k "$TASK" --arg branch "feature/$TASK" --arg path "$PWD/$WT" \
  '.[$k] = {branch:$branch, path:$path, mode:"native", num:1, ports:{gateway:19000,frontend:19001}}' \
  .claude/worktrees/.parallel-registry.json > /tmp/reg-seed.json && mv /tmp/reg-seed.json .claude/worktrees/.parallel-registry.json
PATH="$PWD/bin:$PATH" parallel-task.sh dispatch "$TASK" "Reply with the single word DONE, nothing else."
jq --arg k "$TASK" '.[$k]' .claude/worktrees/.parallel-registry.json
```

Expected: `dispatch` prints `>> dispatch-smoke dispatched: short id <hex>  session <uuid>`; the `jq` output shows an object with `branch`, `path`, `mode`, `num`, `ports`, `short_id`, and `session_id` all populated (non-null).

Clean up before committing (works despite the fake dev-stack — `cmd_rm` calls `cmd_stop || true`, so a dev-stack-down failure doesn't block worktree/registry cleanup):

```bash
PATH="$PWD/bin:$PATH" parallel-task.sh rm dispatch-smoke --force
```

- [ ] **Step 7: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/parallel-task.sh
git commit -m "feat: add dispatch subcommand launching addressable top-level sessions"
```

---

### Task 2: `parallel-task.sh list --json`

**Files:**
- Modify: `bin/parallel-task.sh` (split `cmd_list` into a table branch and a new `list_json` function)

**Interfaces:**
- Consumes: registry entries including Task 1's `short_id`/`session_id` fields; existing `docker_slot_busy`, `port_busy`.
- Produces: `parallel-task.sh list --json` prints a JSON array on stdout; each element has exactly the fields listed in Global Constraints above. Task 4's `dashboard.py` shells out to this and depends on those exact field names.

- [ ] **Step 1: Write the smoke-check and confirm it fails (red)**

```bash
cd ~/projects/claude-parallel-worktree-plugin
PATH="$PWD/bin:$PATH" parallel-task.sh list --json
```

Expected (pre-implementation): today's `cmd_list` ignores its arguments and just prints the human table (or `(no parallel copies registered)`) — it does NOT print valid JSON. Confirm this red state by also running `PATH="$PWD/bin:$PATH" parallel-task.sh list --json | jq empty` and observing a `jq` parse error (or it silently parsing the table text as invalid JSON and erroring).

- [ ] **Step 2: Implement `list_json` and split `cmd_list`**

In `bin/parallel-task.sh`, replace the existing `cmd_list()` function body with:

```bash
cmd_list() {
  if [[ "${1:-}" == "--json" ]]; then
    list_json
    return
  fi
  local tasks
  tasks="$(reg_get 'keys[]')"
  [[ -z "$tasks" ]] && { echo "(no parallel copies registered)"; return 0; }
  printf '%-24s %-10s %-40s %-8s %-30s %s\n' "TASK" "MODE" "BRANCH" "NUM" "PORTS" "STATUS"
  while IFS= read -r task; do
    local mode branch num fe gw status ports
    mode="$(reg_get --arg k "$task" '.[$k].mode')"
    branch="$(reg_get --arg k "$task" '.[$k].branch')"
    num="$(reg_get --arg k "$task" '.[$k].num')"
    fe="$(reg_get --arg k "$task" '.[$k].ports.frontend')"
    gw="$(reg_get --arg k "$task" '.[$k].ports.gateway')"
    ports="fe:${fe} gw:${gw}"
    if [[ "$mode" == "docker" ]]; then
      docker_slot_busy "$num" && status="running" || status="stopped"
    else
      port_busy "$gw" && status="running" || status="stopped"
    fi
    printf '%-24s %-10s %-40s %-8s %-30s %s\n' "$task" "$mode" "$branch" "$num" "$ports" "$status"
  done <<< "$tasks"
}

list_json() {
  local tasks
  tasks="$(reg_get 'keys[]')"
  if [[ -z "$tasks" ]]; then
    echo "[]"
    return 0
  fi
  {
    while IFS= read -r task; do
      local entry mode num gw dev_status session_id agent_obj
      entry="$(reg_get --arg k "$task" '.[$k]')"
      mode="$(jq -r '.mode' <<<"$entry")"
      num="$(jq -r '.num' <<<"$entry")"
      gw="$(jq -r '.ports.gateway' <<<"$entry")"
      if [[ "$mode" == "docker" ]]; then
        docker_slot_busy "$num" && dev_status="running" || dev_status="stopped"
      else
        port_busy "$gw" && dev_status="running" || dev_status="stopped"
      fi
      session_id="$(jq -r '.session_id // empty' <<<"$entry")"
      agent_obj="{}"
      if [[ -n "$session_id" ]]; then
        agent_obj="$(claude agents --json --all 2>/dev/null \
          | jq -c --arg sid "$session_id" '([.[] | select(.sessionId==$sid)] | last) // {}')"
        [[ -n "$agent_obj" ]] || agent_obj="{}"
      fi
      jq -c --arg task "$task" --arg dev_status "$dev_status" --argjson agent "$agent_obj" \
        '. + {task: $task, dev_status: $dev_status,
              agent_status: ($agent.status // null),
              agent_state: ($agent.state // null)}' \
        <<<"$entry"
    done <<< "$tasks"
  } | jq -s '.'
}
```

(This moves the original table-printing loop into the `--json`-less branch unchanged, and adds `list_json` as a sibling function.)

- [ ] **Step 3: Run the smoke-check again, confirm green**

```bash
cd ~/projects/claude-parallel-worktree-plugin
bash -n bin/parallel-task.sh
PATH="$PWD/bin:$PATH" parallel-task.sh list --json | jq empty && echo "valid JSON"
```

Expected: syntax check passes silently; `jq empty` prints nothing and `echo` prints `valid JSON` (empty registry still yields the valid JSON `[]`).

- [ ] **Step 4: End-to-end smoke test with a dispatched task**

`parallel-task.sh start` doesn't work in this repo (see Task 1 Step 6) — same manual worktree + registry seed:

```bash
cd ~/projects/claude-parallel-worktree-plugin
TASK=json-smoke
WT=".claude/worktrees/$TASK"
git worktree add "$WT" -b "feature/$TASK" master
jq --arg k "$TASK" --arg branch "feature/$TASK" --arg path "$PWD/$WT" \
  '.[$k] = {branch:$branch, path:$path, mode:"native", num:1, ports:{gateway:19000,frontend:19001}}' \
  .claude/worktrees/.parallel-registry.json > /tmp/reg-seed.json && mv /tmp/reg-seed.json .claude/worktrees/.parallel-registry.json
PATH="$PWD/bin:$PATH" parallel-task.sh dispatch "$TASK" "Reply with the single word DONE, nothing else."
PATH="$PWD/bin:$PATH" parallel-task.sh list --json | jq --arg k "$TASK" '.[] | select(.task==$k)'
PATH="$PWD/bin:$PATH" parallel-task.sh rm "$TASK" --force
```

Expected: the `jq` filter prints one object with `task`, `branch`, `path`, `mode`, `num`, `ports`, `short_id`, `session_id` all non-null, `dev_status` either `"running"` or `"stopped"`, and `agent_status` either `"idle"`/`"busy"` or `null` (null only if the session hasn't registered with `claude agents` yet — retry the `list --json` call once if so).

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/parallel-task.sh
git commit -m "feat: add list --json flag merging registry with live agent status"
```

---

### Task 3: `bin/dashboard.py` transcript renderer (TDD)

**Files:**
- Create: `bin/dashboard.py`
- Test: `bin/test_dashboard.py`

**Interfaces:**
- Produces: `render_transcript_line(obj: dict) -> list[str]` (zero or more rendered log lines for one parsed JSONL record) and `render_task_log(transcript_path: str, limit: int = 200) -> list[str]` (reads a transcript file, returns the last `limit` rendered lines). Task 4 imports and calls these two exact names from the same file.

- [ ] **Step 1: Write the failing tests**

Create `bin/test_dashboard.py`:

```python
#!/usr/bin/env python3
"""assert-based checks for dashboard.py's transcript rendering. Run: python3 bin/test_dashboard.py"""
import json
import os
import tempfile

from dashboard import render_transcript_line, render_task_log


def test_skips_non_conversation_lines():
    assert render_transcript_line({"type": "custom-title", "customTitle": "x"}) == []
    assert render_transcript_line({"type": "last-prompt", "lastPrompt": "x"}) == []
    assert render_transcript_line({"type": "attachment", "attachment": {}}) == []
    assert render_transcript_line({"type": "mode", "mode": "normal"}) == []


def test_renders_plain_string_user_content():
    obj = {"type": "user", "message": {"role": "user", "content": "hello"}}
    assert render_transcript_line(obj) == ["hello"]


def test_renders_assistant_text():
    obj = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "DONE"}]}}
    assert render_transcript_line(obj) == ["DONE"]


def test_renders_tool_use():
    obj = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}
        ]},
    }
    lines = render_transcript_line(obj)
    assert len(lines) == 1
    assert lines[0].startswith("→ Bash(")


def test_renders_tool_result_string_content():
    obj = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "hi"}]},
    }
    assert render_transcript_line(obj) == ["← hi"]


def test_renders_tool_result_block_content():
    obj = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "block result"}]}
        ]},
    }
    assert render_transcript_line(obj) == ["← block result"]


def test_render_task_log_reads_file_and_applies_limit():
    records = [{"type": "custom-title"}] + [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": f"line{i}"}]}}
        for i in range(5)
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        path = f.name
    try:
        result = render_task_log(path, limit=3)
        assert result == ["line2", "line3", "line4"]
    finally:
        os.unlink(path)


def test_render_task_log_skips_malformed_lines():
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write('{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}\n')
        f.write("not json at all\n")
        f.write("\n")
        path = f.name
    try:
        assert render_task_log(path) == ["ok"]
    finally:
        os.unlink(path)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"{len(tests)} passed")
```

- [ ] **Step 2: Run it, confirm it fails (red)**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin
python3 test_dashboard.py
```

Expected: `ModuleNotFoundError: No module named 'dashboard'` (the file doesn't exist yet).

- [ ] **Step 3: Write the minimal implementation**

Create `bin/dashboard.py`:

```python
#!/usr/bin/env python3
"""Live status/log dashboard for parallel-task.sh copies."""
import json


def _flatten_tool_result(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts)
    return ""


def render_transcript_line(obj: dict) -> list[str]:
    """Return zero or more human-readable log lines for one transcript JSON record."""
    if obj.get("type") not in ("user", "assistant"):
        return []
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    lines = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                lines.append(text)
        elif item_type == "tool_use":
            name = item.get("name", "?")
            args = json.dumps(item.get("input", {}))[:80]
            lines.append(f"→ {name}({args})")
        elif item_type == "tool_result":
            lines.append(f"← {_flatten_tool_result(item.get('content'))[:200]}")
    return lines


def render_task_log(transcript_path: str, limit: int = 200) -> list[str]:
    """Read a session transcript JSONL file and return the last `limit` rendered log lines."""
    lines: list[str] = []
    with open(transcript_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            lines.extend(render_transcript_line(obj))
    return lines[-limit:]
```

- [ ] **Step 4: Run the tests again, confirm green**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin
python3 test_dashboard.py
```

Expected: all 7 `PASS` lines, then `7 passed`.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/dashboard.py bin/test_dashboard.py
git commit -m "feat: add transcript-to-log-lines renderer for dashboard"
```

---

### Task 4: `bin/dashboard.py` HTTP server

**Files:**
- Modify: `bin/dashboard.py` (append the server; Task 3's functions stay in the same file, called directly — no import needed)

**Interfaces:**
- Consumes: Task 3's `render_task_log(path, limit=200)`; Task 2's `parallel-task.sh list --json` output shape (field names `task`, `path`, `session_id`, `mode`, etc.).
- Produces: `GET /api/tasks` → the JSON array from `list --json`, verbatim. `GET /api/tasks/<name>/log` → `{"lines": [...]}` or `{"status": "not-available"}`. `GET /` → serves `bin/dashboard.html` (created in Task 5 — this task's manual test will 404 on `/` until Task 5 lands; that's expected and checked in Task 5, not here).

- [ ] **Step 1: Write the smoke-check and confirm it fails (red)**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin
python3 -c "import dashboard; dashboard.main()" &
SERVER_PID=$!
sleep 1
curl -s http://127.0.0.1:4400/api/tasks
kill $SERVER_PID
```

Expected: `curl` fails to connect (`main()` doesn't exist yet in `dashboard.py`, so the background process exits immediately with an `AttributeError`).

- [ ] **Step 2: Implement the server**

Append to `bin/dashboard.py` (after `render_task_log`):

```python
import http.server
import os
import subprocess
import sys
from urllib.parse import urlparse

PLUGIN_BIN = os.path.dirname(os.path.abspath(__file__))
PARALLEL_TASK_SH = os.path.join(PLUGIN_BIN, "parallel-task.sh")


def get_tasks() -> list[dict]:
    result = subprocess.run([PARALLEL_TASK_SH, "list", "--json"], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _slugify_cwd(path: str) -> str:
    return path.replace("/", "-").replace(".", "-")


def transcript_path_for(task: dict) -> str | None:
    session_id = task.get("session_id")
    wt_path = task.get("path")
    if not session_id or not wt_path:
        return None
    home = os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects", _slugify_cwd(wt_path), f"{session_id}.jsonl")


class Handler(http.server.BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/tasks":
            try:
                self._json(get_tasks())
            except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, status=500)
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/log"):
            name = parsed.path[len("/api/tasks/"):-len("/log")]
            try:
                tasks = {t["task"]: t for t in get_tasks()}
            except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, status=500)
                return
            task = tasks.get(name)
            path = transcript_path_for(task) if task else None
            if not path or not os.path.exists(path):
                self._json({"status": "not-available"})
                return
            self._json({"lines": render_task_log(path)})
            return
        if parsed.path == "/":
            html_path = os.path.join(PLUGIN_BIN, "dashboard.html")
            if not os.path.exists(html_path):
                self.send_response(404)
                self.end_headers()
                return
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # ponytail: quiet by default; add real logging if this needs debugging later


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4400
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dashboard: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the tests again, confirm dashboard.py still imports clean**

```bash
cd ~/projects/claude-parallel-worktree-plugin/bin
python3 test_dashboard.py
```

Expected: still all 7 `PASS` — the server code must not break the Task 3 functions or their tests.

- [ ] **Step 4: End-to-end smoke test**

`parallel-task.sh start` doesn't work in this repo (see Task 1 Step 6) — same manual worktree + registry seed:

```bash
cd ~/projects/claude-parallel-worktree-plugin
TASK=server-smoke
WT=".claude/worktrees/$TASK"
git worktree add "$WT" -b "feature/$TASK" master
jq --arg k "$TASK" --arg branch "feature/$TASK" --arg path "$PWD/$WT" \
  '.[$k] = {branch:$branch, path:$path, mode:"native", num:1, ports:{gateway:19000,frontend:19001}}' \
  .claude/worktrees/.parallel-registry.json > /tmp/reg-seed.json && mv /tmp/reg-seed.json .claude/worktrees/.parallel-registry.json
PATH="$PWD/bin:$PATH" parallel-task.sh dispatch "$TASK" "Reply with the single word DONE, nothing else."
python3 bin/dashboard.py 4400 &
DASH_PID=$!
sleep 1
curl -s http://127.0.0.1:4400/api/tasks | jq --arg k "$TASK" '.[] | select(.task==$k)'
curl -s "http://127.0.0.1:4400/api/tasks/$TASK/log" | jq .
kill $DASH_PID
PATH="$PWD/bin:$PATH" parallel-task.sh rm "$TASK" --force
```

Expected: `/api/tasks` shows the `server-smoke` object with the same fields verified in Task 2 Step 4; `/api/tasks/server-smoke/log` shows either `{"lines": [...]}` with readable text lines (once the session has produced output) or `{"status": "not-available"}` if dispatched too recently for `claude agents` / the transcript file to exist yet — re-run the `curl` after a couple seconds if so.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/dashboard.py
git commit -m "feat: add dashboard.py HTTP server for task status and logs"
```

---

### Task 5: `bin/dashboard.html`

**Files:**
- Create: `bin/dashboard.html`

**Interfaces:**
- Consumes: Task 4's `/api/tasks` (array with `task`, `mode`, `dev_status`, `agent_status` fields) and `/api/tasks/<name>/log` (`{"lines": [...]}` or `{"status": "not-available"}`).
- Produces: nothing consumed by later tasks — this is the final UI layer.

- [ ] **Step 1: Write the smoke-check and confirm it fails (red)**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 bin/dashboard.py 4400 &
DASH_PID=$!
sleep 1
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4400/
kill $DASH_PID
```

Expected: `404` (Task 4's `do_GET` explicitly 404s when `dashboard.html` doesn't exist).

- [ ] **Step 2: Create `bin/dashboard.html`**

```html
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>parallel-task dashboard</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 0; display: flex; height: 100vh; }
  #tasks { width: 380px; overflow-y: auto; border-right: 1px solid #ccc; }
  #tasks table { width: 100%; border-collapse: collapse; font-size: 13px; }
  #tasks th, #tasks td { padding: 4px 8px; text-align: left; border-bottom: 1px solid #eee; }
  #tasks tr { cursor: pointer; }
  #tasks tr.selected { background: #e8f0fe; }
  #log { flex: 1; overflow-y: auto; padding: 8px; font-family: monospace; font-size: 12px; white-space: pre-wrap; }
  .status-running, .status-idle { color: #1a7f37; }
  .status-stopped { color: #999; }
  .status-busy { color: #b35900; }
</style>
</head>
<body>
<div id="tasks"><table>
  <thead><tr><th>Task</th><th>Mode</th><th>Dev</th><th>Agent</th></tr></thead>
  <tbody id="taskRows"></tbody>
</table></div>
<div id="log">select a task</div>
<script>
let selected = null;

function checkResponse(res, data) {
  if (res.ok && !data.error) return null;
  return data.error || `HTTP ${res.status}`;
}

async function pollTasks() {
  const res = await fetch("/api/tasks");
  const data = await res.json();
  const rows = document.getElementById("taskRows");
  const err = checkResponse(res, data);
  if (err) {
    rows.innerHTML = `<tr><td colspan="4">error: ${err}</td></tr>`;
    return;
  }
  const tasks = data;
  rows.innerHTML = "";
  for (const t of tasks) {
    const tr = document.createElement("tr");
    if (t.task === selected) tr.className = "selected";
    tr.innerHTML = `<td>${t.task}</td><td>${t.mode}</td>` +
      `<td class="status-${t.dev_status}">${t.dev_status}</td>` +
      `<td class="status-${t.agent_status || 'none'}">${t.agent_status || '-'}</td>`;
    tr.onclick = () => { selected = t.task; pollLog(); };
    rows.appendChild(tr);
  }
}

async function pollLog() {
  if (!selected) return;
  const res = await fetch(`/api/tasks/${encodeURIComponent(selected)}/log`);
  const data = await res.json();
  const log = document.getElementById("log");
  const err = checkResponse(res, data);
  if (err) {
    log.textContent = `error: ${err}`;
    return;
  }
  if (data.status === "not-available") {
    log.textContent = "no session dispatched yet";
    return;
  }
  log.textContent = data.lines.join("\n");
  log.scrollTop = log.scrollHeight;
}

pollTasks();
setInterval(pollTasks, 2000);
setInterval(pollLog, 2000);
</script>
</body>
</html>
```

- [ ] **Step 3: Run the smoke-check again, confirm green**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 bin/dashboard.py 4400 &
DASH_PID=$!
sleep 1
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4400/
curl -s http://127.0.0.1:4400/ | grep -c "taskRows"
kill $DASH_PID
```

Expected: `200`, then a count ≥ 1.

- [ ] **Step 4: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add bin/dashboard.html
git commit -m "feat: add dashboard.html static UI"
```

---

### Task 6: Update `SKILL.md` Step 3 to dispatch instead of using the `Agent` tool

**Files:**
- Modify: `skills/parallel-worktree-run/SKILL.md`

**Interfaces:**
- Consumes: Task 1's `dispatch` subcommand.
- Produces: nothing consumed by later tasks — documentation only.

- [ ] **Step 1: Replace the "Step 3" section**

In `skills/parallel-worktree-run/SKILL.md`, replace the entire `## Step 3 — dispatch the implementing subagent` section (from that heading through the line ending "...send multiple `Agent` calls in a single message so they run concurrently.") with:

```markdown
## Step 3 — dispatch the implementing session

Build the task prompt (do NOT skip any of these — this becomes the literal `<prompt>` argument
to `dispatch`):

1. The actual task/requirements verbatim.
2. The dev URLs from Step 2, so it can verify its work against a live server instead of only
   static analysis.
3. The repo's normal gates: any rules under `.claude/rules/*` or `CLAUDE.md`, commit message
   format, and any post-task note-taking convention the repo has.
4. **Forbid `--frontend-only` as a workaround.** If this slot's `dev-stack.sh N up -d` (or a
   restart of it) fails, it must diagnose and retry the full stack — never fall back to
   `dev-stack.sh N up --frontend-only`. That flag repoints the frontend at the *shared* slot-0
   gateway, silently breaking the own-DB/own-backend isolation this skill exists to guarantee (a
   real regression: a dispatched session hit a half-created stack from a prior failed run and
   "fixed" it by switching to `--frontend-only`, so the task ran against the wrong tenant's DB
   until caught). `--frontend-only` belongs to `worktree-new-feature` only.
5. Ask it to end with a summary: files changed, tests run + result, URLs.

Then dispatch:

```bash
parallel-task.sh dispatch <task-name> "<prompt>"
```

This launches an independent, addressable top-level Claude Code session in that worktree
(`claude --bg`) and records its id in the registry — it is NOT a subagent of this root session.
There is no `EnterWorktree` step to instruct here: the dispatched session's process starts with
its cwd already inside the worktree (`dispatch` runs `claude --bg` from there directly), so the
split-brain trap `worktree-new-feature` warns about for subagents (drifting back to the root
session's cwd) doesn't apply to this path.

`dispatch` returns immediately once the session is backgrounded — no need to batch multiple calls
in one message the way background `Agent` calls did; issue them one after another.

Because the dispatched session is a real, addressable Claude Code session (not a one-shot
subagent), it can also be talked to mid-task — `claude attach <short-id>` opens it in the current
terminal, or another session can message it once it appears in that session's peer list.
```

- [ ] **Step 2: Verify the doc change**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -c "cmd_dispatch\|Agent\` call" skills/parallel-worktree-run/SKILL.md
grep -n "parallel-task.sh dispatch" skills/parallel-worktree-run/SKILL.md
```

Expected: the second command finds the new `dispatch` invocation line; re-read the file (`cat skills/parallel-worktree-run/SKILL.md`) to confirm no leftover references to spawning an `Agent` tool call remain in the Step 3 section.

- [ ] **Step 3: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add skills/parallel-worktree-run/SKILL.md
git commit -m "docs: update SKILL.md Step 3 for dispatch-based session launch"
```

---

### Task 7: README update

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing from earlier tasks beyond their existence (this documents them).
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Add a Dashboard section**

In `README.md`, immediately after the existing `## Usage` section's closing code block (the one ending with `parallel-task.sh rm my-task --force`), add:

```markdown
## Dashboard

See every running copy's live status and output in one page:

```bash
dashboard.py            # http://127.0.0.1:4400
dashboard.py 5000        # custom port
```

Lists each task's dev-stack status (`dev_status`, from the docker/native port checks) alongside
its dispatched session's live status (`agent_status`: `idle`/`busy`, from `claude agents --json`),
and streams that session's transcript as a readable log — click a task to follow it.
```

Also update the existing `## What's in here` list to add a line for the new files, immediately after the existing `bin/dev-native.sh` bullet:

```markdown
- `bin/dashboard.py` / `bin/dashboard.html` — local web dashboard (see Dashboard below):
  status + live log per running copy, reading `parallel-task.sh list --json` and each task's
  session transcript. Stdlib-only, no build step.
```

- [ ] **Step 2: Verify**

```bash
cd ~/projects/claude-parallel-worktree-plugin
grep -n "dashboard.py" README.md
```

Expected: at least 3 matches (the two new bullets/blocks above).

- [ ] **Step 3: Commit**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git add README.md
git commit -m "docs: document the dashboard in README"
```

---

### Task 8: Full end-to-end smoke test, push, and PR

**Files:** none (verification + git/gh operations only)

**Interfaces:**
- Consumes: everything from Tasks 1–7 together.
- Produces: a pushed branch and an open PR — the deliverable of this plan.

- [ ] **Step 1: Full end-to-end run**

`parallel-task.sh start` doesn't work in this repo (see Task 1 Step 6) — same manual worktree + registry seed:

```bash
cd ~/projects/claude-parallel-worktree-plugin
TASK=e2e-smoke
WT=".claude/worktrees/$TASK"
git worktree add "$WT" -b "feature/$TASK" master
jq --arg k "$TASK" --arg branch "feature/$TASK" --arg path "$PWD/$WT" \
  '.[$k] = {branch:$branch, path:$path, mode:"native", num:1, ports:{gateway:19000,frontend:19001}}' \
  .claude/worktrees/.parallel-registry.json > /tmp/reg-seed.json && mv /tmp/reg-seed.json .claude/worktrees/.parallel-registry.json
PATH="$PWD/bin:$PATH" parallel-task.sh dispatch "$TASK" "List the files in this directory, then reply with the single word DONE."
python3 bin/dashboard.py 4400 &
DASH_PID=$!
sleep 3
curl -s http://127.0.0.1:4400/api/tasks | jq --arg k "$TASK" '.[] | select(.task==$k)'
curl -s "http://127.0.0.1:4400/api/tasks/$TASK/log" | jq -r '.lines[]'
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4400/
kill $DASH_PID
PATH="$PWD/bin:$PATH" parallel-task.sh rm "$TASK" --force
```

Expected: the task object shows `dev_status: "running"`, a non-null `agent_status`; the log lines show real content (a tool_use line for the `ls`/list-files action, and eventually `DONE`); the root path returns `200`.

- [ ] **Step 2: Full test suite + lint pass**

```bash
cd ~/projects/claude-parallel-worktree-plugin
python3 bin/test_dashboard.py
bash -n bin/parallel-task.sh
bash -n bin/dev-stack.sh
bash -n bin/dev-native.sh
```

Expected: all pass (7 `PASS` + `7 passed` from the Python tests, no output/errors from the three `bash -n` syntax checks).

- [ ] **Step 3: Review the full diff before pushing**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git log --oneline master..feature/agent-status-dashboard
git diff --stat master..feature/agent-status-dashboard
```

Expected: at least 7 commits (one per Task 1–7, plus one more per fix round any task needed — check the ledger for the exact count), touching only `bin/parallel-task.sh`, `bin/dashboard.py`, `bin/dashboard.html`, `bin/test_dashboard.py`, `skills/parallel-worktree-run/SKILL.md`, `README.md` (confirm `.claude-plugin/marketplace.json` and the two `docs/superpowers/` files from earlier in this session are NOT in this diff, since they were never `git add`ed by any task above).

- [ ] **Step 4: Push and open the PR**

```bash
cd ~/projects/claude-parallel-worktree-plugin
git push -u origin feature/agent-status-dashboard
gh pr create --repo thanhtoan0499/claude-parallel-worktree-plugin \
  --base master --head ttanlocc:feature/agent-status-dashboard \
  --title "Add live agent status/log dashboard, dispatch via top-level sessions" \
  --body "$(cat <<'EOF'
## Summary
- Replaces the Agent-tool subagent dispatch in Step 3 with `claude --bg`, launching independent, addressable top-level sessions instead of one-shot subagents.
- Adds `parallel-task.sh dispatch` (records each session's id in the registry) and `parallel-task.sh list --json`.
- Adds a stdlib-only `bin/dashboard.py` + `bin/dashboard.html` — a local web dashboard showing every running task's dev-stack status, dispatched-session status, and live log.

## Test plan
- [x] `bin/test_dashboard.py` — transcript-rendering unit tests
- [x] Manual smoke test per task (see plan, Tasks 1–5)
- [x] Full end-to-end run: start → dispatch → dashboard shows live status + log → rm
EOF
)"
```

Expected: `git push` succeeds against the `ttanlocc` fork; `gh pr create` prints the new PR URL against `thanhtoan0499/claude-parallel-worktree-plugin`.

- [ ] **Step 5: Report the PR URL back**

Paste the URL `gh pr create` printed — this is the deliverable.
