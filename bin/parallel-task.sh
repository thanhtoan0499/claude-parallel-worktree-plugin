#!/usr/bin/env bash
# parallel-task.sh — allocate + lifecycle-manage parallel worktree+dev-stack copies,
# so a single Claude Code session at the repo root can spin up N independently
# running copies of the app (own branch, own worktree, own ports) without ever
# leaving the root checkout.
#
# Wraps three existing pieces instead of reinventing them:
#   - `git worktree add`                          → isolated checkout, shared .git
#   - dev-stack.sh   (docker mode, slot N) → gateway 8081+N*100, frontend
#                                                     5173+N*100, postgres 5432+N*100,
#                                                     azurite 10000+N*100
#   - dev-native.sh  (native mode, task N) → gateway 8500+N, frontend 5500+N,
#                                                     shared postgres/azurite
#
# This script's own job is just the glue those two don't do: pick a free slot/task
# number automatically (checked LIVE, not just from the registry, so a stale entry
# after a manual `docker compose down` or crash can't cause a collision), track
# which worktree owns which number, and give one place to list/stop/remove them.
#
# What this script does NOT do any more is give a worker its brief. Briefing is a message, and a
# shell cannot send one: SendMessage is a Claude tool, and both shell-side stand-ins for it are
# dead ends — `claude --resume <id> -p` spawns a headless one-shot on the transcript that the live
# tmux session never sees, and send-keys types into a TUI where every newline in a brief submits a
# half-finished prompt. So `dispatch` provisions, records, and prints the worker's EXACT agent name;
# a resident manager session (`manager-start`) does the briefing over SendMessage.
#
# Usage:
#   parallel-task.sh start         <task-name> <native|docker> [base-ref] [--ticket <id> ...]
#   parallel-task.sh dispatch      <task-name> <prompt> [--worktree <path>] [--model <model>] [--effort low|medium|high|xhigh|max]
#   parallel-task.sh manager-start
#   parallel-task.sh list          [--json]
#   parallel-task.sh stop          <task-name>
#   parallel-task.sh rm            <task-name> [--force]
set -euo pipefail

usage() {
  sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREES_DIR="$REPO_ROOT/.claude/worktrees"
REGISTRY="$WORKTREES_DIR/.parallel-registry.json"

# The resident manager: one cmew session named `manager`, so tmux session `cc-manager` and display
# name "Manager 🔹", running in the repo root rather than a worktree because it dispatches work and
# does not do it. Opus at max effort is a standing decision, not a per-run choice — this is the
# session that decides what every worker is told, and a cheap manager writes expensive briefs.
MANAGER_TASK="manager"
MANAGER_MODEL="opus"
MANAGER_EFFORT="max"
MANAGER_CHARTER="skills/engineering-manager/SKILL.md"   # relative: cmew opens the session in REPO_ROOT

mkdir -p "$WORKTREES_DIR"
[[ -f "$REGISTRY" ]] || echo '{}' > "$REGISTRY"

# --- registry helpers --------------------------------------------------------

reg_get() { jq -r "$@" "$REGISTRY"; }

reg_set_entry() {
  # reg_set_entry <task-name> <json-object>
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/parallel-task-registry.XXXXXX.json")"
  jq --arg k "$1" --argjson v "$2" '.[$k] = $v' "$REGISTRY" > "$tmp"
  mv "$tmp" "$REGISTRY"
}

reg_del_entry() {
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/parallel-task-registry.XXXXXX.json")"
  jq --arg k "$1" 'del(.[$k])' "$REGISTRY" > "$tmp"
  mv "$tmp" "$REGISTRY"
}

reg_merge_entry() {
  # reg_merge_entry <task-name> <json-object-to-merge-in>
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/parallel-task-registry.XXXXXX.json")"
  jq --arg k "$1" --argjson v "$2" '.[$k] += $v' "$REGISTRY" > "$tmp"
  mv "$tmp" "$REGISTRY"
}

task_is_adopted() {
  # True for a row this script recorded but did not provision — see adopt_entry. `stop` and `rm`
  # ask before tearing anything down, because neither the dev stack nor the worktree is theirs.
  [[ "$(reg_get --arg k "$1" '.[$k].adopted // false')" == "true" ]]
}

adopt_entry() {
  # adopt_entry <worktree-path> — the registry row for a worktree this script did not create.
  #
  # Every field is read off the worktree, never guessed. `mode: adopted` with null num/ports says
  # plainly that no dev stack was provisioned here, so `list` reports it stopped (true — there is
  # nothing to run) and `stop`/`rm` know to keep their hands off. `ado_ids: []` because every
  # other row has the key and consumers walk it.
  local path branch
  path="$(cd "$1" && pwd)"
  # symbolic-ref, not `rev-parse --abbrev-ref`: that returns the literal string "HEAD" both for a
  # detached worktree and for a branch with no commit yet, and "HEAD" recorded as a branch name is
  # worse than null. This prints the branch or fails, and a failure means null.
  branch="$(git -C "$path" symbolic-ref --short HEAD 2>/dev/null || true)"
  jq -n --arg branch "$branch" --arg path "$path" \
    '{branch: (if $branch == "" then null else $branch end), path: $path,
      mode: "adopted", num: null, ports: null, ado_ids: [], adopted: true}'
}

dispatch_worktree() {
  # dispatch_worktree <task-name> <--worktree value, may be empty> — print the worktree to adopt.
  #
  # Non-zero when there is nothing to adopt, which is the only case left where dispatch refuses.
  local task="$1" explicit="${2:-}"
  if [[ -n "$explicit" ]]; then
    [[ -d "$explicit" ]] || { echo "error: --worktree '$explicit' is not a directory" >&2; return 1; }
    ( cd "$explicit" && pwd )
    return 0
  fi
  [[ -d "$WORKTREES_DIR/$task" ]] || return 1
  echo "$WORKTREES_DIR/$task"
}

# --- port / slot liveness checks ---------------------------------------------

port_busy() { fuser "$1/tcp" >/dev/null 2>&1; }

docker_slot_busy() {
  # true if slot N has any live container under its compose project name
  local n="$1"
  [[ -n "$(docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" -p "aiquinta-mfg-s${n}" ps -q 2>/dev/null)" ]]
}

next_free_docker_slot() {
  local used
  used="$(reg_get '[.[] | select(.mode=="docker") | .num] | map(tostring) | join(" ")')"
  for n in 1 2 3 4 5 6 7 8 9; do
    [[ " $used " == *" $n "* ]] && continue
    docker_slot_busy "$n" && continue
    echo "$n"; return 0
  done
  echo "error: no free docker slot (1-9 all taken)" >&2
  return 1
}

next_free_native_task() {
  local used n gw
  used="$(reg_get '[.[] | select(.mode=="native") | .num] | map(tostring) | join(" ")')"
  n=1
  while :; do
    if [[ " $used " != *" $n "* ]]; then
      gw=$((8500 + n))
      port_busy "$gw" || { echo "$n"; return 0; }
    fi
    n=$((n + 1))
    [[ $n -gt 999 ]] && { echo "error: no free native task number found" >&2; return 1; }
  done
}

# --- .worktreeinclude copy (mirrors EnterWorktree's own behavior) -----------

copy_worktreeinclude() {
  local dest="$1" src rel
  [[ -f "$REPO_ROOT/.worktreeinclude" ]] || return 0
  while IFS= read -r rel; do
    [[ -z "$rel" || "$rel" == \#* ]] && continue
    src="$REPO_ROOT/$rel"
    [[ -e "$src" && ! -L "$src" ]] || continue
    mkdir -p "$(dirname "$dest/$rel")"
    cp "$src" "$dest/$rel"
  done < "$REPO_ROOT/.worktreeinclude"
}

# --- tmux panes + agent identity ---------------------------------------------

tmux_session_exists() {
  # Exact match on the session name. NOT `tmux has-session -t X`: -t resolves its argument the way
  # every other tmux target does, so it answers yes for `cc-manager` when only `cc-manager-2` is
  # alive — and a duplicate-manager guard that can be fooled by a longer name is no guard.
  tmux list-sessions -F '#S' 2>/dev/null | grep -qxF "$1"
}

scrub_inherited_claude_env() {
  # tmux hands a NEW session the SERVER's environment, not this shell's — so `env -u` here would
  # do nothing. If the tmux server was ever started from inside a Claude session, its global
  # environment still carries that session's markers, and every session spawned afterwards
  # inherits them: CLAUDE_CODE_SESSION_ID makes a worker claim the DISPATCHER's session id, and
  # CLAUDE_CODE_CHILD_SESSION stops its transcript being saved at all. Scrub them at the source.
  local v
  for v in CLAUDE_CODE_SESSION_ID CLAUDE_CODE_CHILD_SESSION CLAUDE_PID CLAUDE_CODE_EXECPATH; do
    tmux set-environment -g -u "$v" 2>/dev/null || true
  done
}

pane_has() { tmux capture-pane -p -t "$1" 2>/dev/null | grep -qF "$2"; }

wait_for_pane() {  # <pane> <needle> <seconds>
  local deadline=$((SECONDS + $3))
  while ((SECONDS < deadline)); do
    pane_has "$1" "$2" && return 0
    sleep 1
  done
  return 1
}

wait_for_tui() {
  # wait_for_tui <pane> <cmew-name> — non-zero (and loud) if the session never reached a prompt.
  #
  # Wait for the TUI, do not sleep at it. The first version of this used fixed sleeps and typed
  # into a pane that had not finished booting — the session sat at an empty prompt looking exactly
  # like one that had been told nothing, which is the failure mode the move off `claude --bg` was
  # meant to end. The trust-folder dialog a fresh worktree raises comes BEFORE that prompt: one
  # keypress here, a silent hang under --bg.
  local pane="$1" name="$2"
  if wait_for_pane "$pane" "trust" 8; then
    tmux send-keys -t "$pane" Down
    sleep 1
    tmux send-keys -t "$pane" Enter
  fi
  wait_for_pane "$pane" "❯" 90 && return 0
  echo "error: '$name' never reached a prompt in tmux session $pane" >&2
  echo "       attach and see what it is waiting on: cmew a $name" >&2
  return 1
}

resolve_agent_identity() {
  # resolve_agent_identity <cmew-name> -> "<sessionId><TAB><display name>", empty when unknown.
  #
  # Both halves come off the SAME agent row on purpose. The session id is what the board joins on;
  # the display name is what SendMessage addresses, and it cannot be derived from the task name
  # here — cmew title-cases the codename and appends its emoji (` 🔹` unless the codename is in its
  # pool), and at effort ultracode prepends `🔥 `. SendMessage matches that string EXACTLY, so
  # `T8471` is refused where the real name is `T8471 🔹`. Reading the name back beats keeping a
  # second copy of cmew's naming rule in a second language.
  #
  # The match is a case-insensitive PREFIX, not equality, for the same reason: the row for task
  # `t8419-slug` is named "T8419-slug 🔹". A miss shows an empty card on the board and says nothing
  # about why, so it is worth being lenient here and exact at the SendMessage end.
  claude agents --json --all 2>/dev/null \
    | jq -r --arg n "$1" '
        [.[] | select((.name // "") | ascii_downcase | startswith($n | ascii_downcase))]
        | sort_by(.startedAt) | last
        | select(. != null)
        | "\(.sessionId // "")\t\(.name // "")"' 2>/dev/null || true
}

session_registry_patch() {
  # session_registry_patch <session-id> <agent-name> <model> <effort> — what a launch adds to a
  # registry row, as one JSON object.
  #
  # agent_name is stored, never re-derived: it is the address a manager or a worker has to type
  # into SendMessage verbatim, emoji included. short_id is written empty because nothing prints one
  # any more (it came from `claude --bg`); consumers already fall back to the agent id.
  jq -n --arg sid "$1" --arg name "$2" --arg m "$3" --arg e "$4" \
    '{short_id:"", session_id:$sid, agent_name:$name}
       + (if $m == "" then {} else {model:$m} end)
       + (if $e == "" then {} else {effort:$e} end)'
}

task_status() {
  # task_status <task> <mode> <num> <gateway-port> — "running" | "stopped".
  #
  # A manager row owns no dev stack and no ports, so its liveness IS its tmux session; reporting it
  # off a null gateway port would print "stopped" at a manager that is answering messages.
  case "$2" in
    docker)  docker_slot_busy "$3" && echo running || echo stopped ;;
    manager) tmux_session_exists "cc-$1" && echo running || echo stopped ;;
    *)       port_busy "$4" && echo running || echo stopped ;;
  esac
}

# --- commands -----------------------------------------------------------------

# parse_start_args "$@" -> prints "task<TAB>mode<TAB>base_ref<TAB>ado_ids_json" on success.
# Pure parsing only — no filesystem/network access — so it's testable on its own.
parse_start_args() {
  local -a ticket_ids=()
  local -a positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ticket)
        [[ $# -ge 2 ]] || { echo "error: --ticket requires a value" >&2; return 1; }
        ticket_ids+=("$2"); shift 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  set -- "${positional[@]}"
  [[ $# -ge 2 ]] || { echo "error: start needs <task-name> <native|docker> [base-ref]" >&2; return 1; }
  local task="$1" mode="$2" base_ref="${3:-origin/main}"
  local ado_ids_json
  ado_ids_json="$(jq -cn --args '$ARGS.positional' -- "${ticket_ids[@]}")"
  printf '%s\t%s\t%s\t%s\n' "$task" "$mode" "$base_ref" "$ado_ids_json"
}

# parse_dispatch_args "$@" -> sets DISPATCH_MODEL, DISPATCH_EFFORT, DISPATCH_PROMPT; returns 1 on error.
# Globals rather than a printed tab-separated line: a prompt is multi-line, and a newline inside a
# tab-delimited return would break the caller's read.
parse_dispatch_args() {
  # Every dispatched engineer runs on Opus at max effort unless the caller says otherwise. This
  # is a standing decision, not a default worth re-arguing per task: a worker that reasons badly
  # costs a re-dispatch and a wrong report, which is dearer than the tokens. Override with
  # --model / --effort when a task genuinely does not need it.
  DISPATCH_MODEL="${PARALLEL_TASK_MODEL:-opus}"
  DISPATCH_EFFORT="${PARALLEL_TASK_EFFORT:-max}"
  DISPATCH_PROMPT=""; DISPATCH_WORKTREE=""
  local -a positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --worktree)
        [[ $# -ge 2 ]] || { echo "error: --worktree requires a value" >&2; return 1; }
        DISPATCH_WORKTREE="$2"; shift 2 ;;
      --model)
        [[ $# -ge 2 ]] || { echo "error: --model requires a value" >&2; return 1; }
        DISPATCH_MODEL="$2"; shift 2 ;;
      --effort)
        [[ $# -ge 2 ]] || { echo "error: --effort requires a value" >&2; return 1; }
        case "$2" in
          low|medium|high|xhigh|max) DISPATCH_EFFORT="$2" ;;
          *) echo "error: --effort must be low|medium|high|xhigh|max, got '$2'" >&2; return 1 ;;
        esac
        shift 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  [[ ${#positional[@]} -ge 1 ]] || { echo "error: dispatch needs <task-name> <prompt>" >&2; return 1; }
  if [[ ${#positional[@]} -gt 1 ]]; then
    echo "error: the prompt must be ONE quoted argument, got ${#positional[@]} — an unquoted prompt lets its own words be eaten as flags" >&2
    return 1
  fi
  DISPATCH_PROMPT="${positional[*]}"
}

cmd_start() {
  local parsed task mode base_ref ado_ids_json
  parsed="$(parse_start_args "$@")" || usage
  IFS=$'\t' read -r task mode base_ref ado_ids_json <<< "$parsed"

  [[ "$task" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "error: task-name must be kebab-case (got: '$task')" >&2; exit 1; }
  [[ "$mode" == "native" || "$mode" == "docker" ]] || { echo "error: mode must be 'native' or 'docker' (got: '$mode')" >&2; exit 1; }
  [[ "$(reg_get --arg k "$task" 'has($k)')" == "false" ]] || { echo "error: task '$task' already registered (see: $0 list)" >&2; exit 1; }
  [[ -e "$WORKTREES_DIR/$task" ]] && {
    echo "error: $WORKTREES_DIR/$task already exists" >&2
    echo "       to run a worker in it instead: $0 dispatch $task \"<brief>\"" >&2
    exit 1
  }

  local branch="feature/${task}"
  local wt_path="$WORKTREES_DIR/$task"

  git -C "$REPO_ROOT" worktree add "$wt_path" -b "$branch" "$base_ref"
  copy_worktreeinclude "$wt_path"

  # If anything below fails (e.g. dev-stack.sh's `up -d` dies on a port
  # collision), roll back the worktree+branch instead of leaving an
  # unregistered orphan behind — otherwise `list` won't show it, a retry
  # collides on "$WORKTREES_DIR/$task already exists", and recovery means
  # hand-editing the registry with jq.
  local rolled_back=false
  rollback() {
    $rolled_back && return 0
    rolled_back=true
    echo "error: start failed — rolling back worktree $wt_path" >&2
    git -C "$REPO_ROOT" worktree remove --force "$wt_path" 2>/dev/null || true
    git -C "$REPO_ROOT" branch -D "$branch" 2>/dev/null || true
  }
  trap rollback ERR

  local num ports_json gw fe
  if [[ "$mode" == "docker" ]]; then
    num="$(next_free_docker_slot)"
    ( cd "$wt_path" && dev-stack.sh "$num" up -d )
    gw=$((8081 + num * 100)); fe=$((5173 + num * 100))
    ports_json="$(jq -n --argjson gw "$gw" --argjson fe "$fe" --argjson pg $((5432 + num*100)) --argjson az $((10000 + num*100)) \
      '{gateway:$gw, frontend:$fe, postgres:$pg, azurite:$az}')"
  else
    # native mode runs the frontend directly on the host (not in a container
    # that bakes node_modules at build time) — a fresh worktree has no
    # node_modules at all (git worktrees don't carry it), so `vite` isn't on
    # PATH until pnpm install runs once, from the worktree root (pnpm
    # workspace install must run at repo root, not apps/web).
    ( cd "$wt_path" && pnpm install )
    # shared native infra, started once, idempotent
    port_busy 5599 || dev-native.sh infra up
    # dev-native.sh's own `up` immediately runs `docker exec ... psql` against
    # this container to create the per-task DB — wait for it to actually
    # accept connections first, or a just-started container loses that race.
    local infra_pg i=0
    infra_pg="$(docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" -p aiquinta-native-infra ps -q postgres 2>/dev/null)"
    until [[ -n "$infra_pg" ]] && docker exec -u postgres "$infra_pg" pg_isready >/dev/null 2>&1; do
      i=$((i + 1))
      [[ $i -gt 30 ]] && { echo "error: shared native postgres never became ready" >&2; exit 1; }
      sleep 1
      infra_pg="$(docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" -p aiquinta-native-infra ps -q postgres 2>/dev/null)"
    done
    num="$(next_free_native_task)"
    ( cd "$wt_path" && dev-native.sh "$num" up )
    gw=$((8500 + num)); fe=$((5500 + num))
    ports_json="$(jq -n --argjson gw "$gw" --argjson fe "$fe" '{gateway:$gw, frontend:$fe}')"
  fi

  reg_set_entry "$task" "$(jq -n \
    --arg branch "$branch" --arg path "$wt_path" --arg mode "$mode" \
    --argjson num "$num" --argjson ports "$ports_json" --argjson ado_ids "$ado_ids_json" \
    '{branch:$branch, path:$path, mode:$mode, num:$num, ports:$ports, ado_ids:$ado_ids}')"
  trap - ERR

  echo ">> $task ready: branch $branch  mode $mode  worktree $wt_path"
  echo "   frontend: http://localhost:${fe}"
  echo "   gateway:  http://localhost:${gw}"
  echo "   NOTE: register http://localhost:${fe}/auth/callback in the WorkOS dashboard"
  echo "   redirect-URI allow-list before logging in on this copy (no local auth bypass)."
}

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
    status="$(task_status "$task" "$mode" "$num" "$gw")"
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
  local agents_json
  agents_json="$(claude agents --json --all 2>/dev/null)" || true
  [[ -n "$agents_json" ]] || agents_json="[]"
  {
    while IFS= read -r task; do
      local entry mode num gw dev_status session_id agent_obj
      entry="$(reg_get --arg k "$task" '.[$k]')"
      mode="$(jq -r '.mode' <<<"$entry")"
      num="$(jq -r '.num' <<<"$entry")"
      gw="$(jq -r '.ports.gateway' <<<"$entry")"
      dev_status="$(task_status "$task" "$mode" "$num" "$gw")"
      session_id="$(jq -r '.session_id // empty' <<<"$entry")"
      agent_obj="{}"
      if [[ -n "$session_id" ]]; then
        agent_obj="$(jq -c --arg sid "$session_id" '([.[] | select(.sessionId==$sid)] | last) // {}' <<<"$agents_json")"
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

cmd_stop() {
  [[ $# -ge 1 ]] || { echo "error: stop needs <task-name>" >&2; usage; }
  local task="$1"
  [[ "$(reg_get --arg k "$task" 'has($k)')" == "true" ]] || { echo "error: unknown task '$task'" >&2; exit 1; }

  local short_id
  short_id="$(reg_get --arg k "$task" '.[$k].short_id // empty')"
  if [[ -n "$short_id" ]]; then
    claude stop "$short_id" || true
  fi

  if task_is_adopted "$task"; then
    # The session was ours to stop; the worktree and whatever runs in it were not. Tearing down a
    # stack this script never started would take out whichever task actually provisioned it.
    echo ">> $task stopped (adopted worktree — no dev stack of ours to bring down)"
    return 0
  fi

  local mode num path
  mode="$(reg_get --arg k "$task" '.[$k].mode')"
  num="$(reg_get --arg k "$task" '.[$k].num')"
  path="$(reg_get --arg k "$task" '.[$k].path')"
  if [[ "$mode" == "docker" ]]; then
    ( cd "$path" && dev-stack.sh "$num" down )
  else
    ( cd "$path" && dev-native.sh "$num" down )
  fi
  echo ">> $task stopped (worktree + branch kept)"
}

cmd_rm() {
  [[ $# -ge 1 ]] || { echo "error: rm needs <task-name> [--force]" >&2; usage; }
  local task="$1" force=false
  [[ "${2:-}" == "--force" ]] && force=true
  [[ "$(reg_get --arg k "$task" 'has($k)')" == "true" ]] || { echo "error: unknown task '$task'" >&2; exit 1; }
  local path
  path="$(reg_get --arg k "$task" '.[$k].path')"

  if task_is_adopted "$task"; then
    # Deleting a worktree this script did not create is not ours to do — and an adopted row can
    # point at a worktree another task owns, so `git worktree remove` here would take out that
    # task's work. Drop the row and stop; the worktree stays exactly as it was.
    cmd_stop "$task" || true
    reg_del_entry "$task"
    echo ">> $task unregistered. Adopted worktree $path left alone — remove it yourself if you own it."
    return 0
  fi

  cmd_stop "$task" || true

  if $force; then
    git -C "$REPO_ROOT" worktree remove --force "$path"
  else
    if ! git -C "$REPO_ROOT" worktree remove "$path" 2>/tmp/parallel-task-rm-err; then
      cat /tmp/parallel-task-rm-err >&2
      echo "error: worktree has uncommitted/unmerged work — pass --force to discard" >&2
      exit 1
    fi
  fi
  reg_del_entry "$task"
  echo ">> $task removed. Branch kept — delete after merge with:"
  echo "   git -C '$REPO_ROOT' branch -d <branch>"
}

cmd_dispatch() {
  [[ $# -ge 2 ]] || { echo "error: dispatch needs <task-name> <prompt>" >&2; usage; }
  local task="$1"; shift
  parse_dispatch_args "$@" || usage
  local prompt="$DISPATCH_PROMPT"
  # A task with no row is not a refusal any more, it is an adoption. `start` will not touch a
  # worktree that already exists and `dispatch` used to insist on a row, so there was no supported
  # way to launch a worker into an existing worktree — and going around both with a bare
  # `claude --bg` records nothing. An unrecorded session is indistinguishable from somebody's own
  # terminal, so every consumer that asks "did we dispatch this?" (board_state.session_docs's
  # `managed`, manager_daemon's worker-finished wakes, the stuck-session watch) answers no about a
  # real worker. Three live workers sat outside the registry on 2026-09-09 for exactly this reason.
  local wt_path
  if [[ "$(reg_get --arg k "$task" 'has($k)')" == "true" ]]; then
    wt_path="$(reg_get --arg k "$task" '.[$k].path')"
    if [[ -n "$DISPATCH_WORKTREE" ]]; then
      local given
      given="$( (cd "$DISPATCH_WORKTREE" 2>/dev/null && pwd) || echo "$DISPATCH_WORKTREE" )"
      [[ "$given" == "$wt_path" ]] || {
        echo "error: '$task' is already registered at $wt_path, but --worktree says $given" >&2
        echo "       dispatch under a different task name to run a second worker in that worktree" >&2
        exit 1
      }
    fi
  elif wt_path="$(dispatch_worktree "$task" "$DISPATCH_WORKTREE")"; then
    reg_set_entry "$task" "$(adopt_entry "$wt_path")"
    echo ">> adopted existing worktree $wt_path as task '$task' (no dev stack provisioned by us)"
  else
    echo "error: unknown task '$task' and no worktree to adopt (see: $0 list)" >&2
    echo "       $WORKTREES_DIR/$task does not exist — pass --worktree <path> to dispatch into" >&2
    echo "       a worktree under another name, or run: $0 start $task <native|docker>" >&2
    exit 1
  fi

  # An INTERACTIVE tmux session (cmew), never `claude --bg`. The CTO has to be able to walk into
  # a running worker and talk to it — "tôi muốn vào check và chat trực tiếp khi cần" — and a
  # --bg session cannot be attached, cannot receive a message, and hides every permission prompt
  # it stalls on. Five silent stalls on 2026-09-09 cost 5-25 minutes each, and three workers had
  # to be killed and re-dispatched from scratch because a wrong brief could not be corrected.
  scrub_inherited_claude_env

  local -a launch=(cmew new "$task" "$wt_path")
  if [[ -n "$DISPATCH_EFFORT" ]]; then launch+=(-e "$DISPATCH_EFFORT"); fi
  if [[ -n "$DISPATCH_MODEL" ]]; then launch+=(-m "$DISPATCH_MODEL"); fi

  local launch_out
  if ! launch_out="$( "${launch[@]}" 2>&1 )"; then
    echo "error: cmew failed to launch '$task':" >&2
    echo "$launch_out" >&2
    exit 1
  fi

  local pane="cc-$task"
  wait_for_tui "$pane" "$task" || exit 1

  # The brief is WRITTEN here and delivered by nobody — that split is the whole point of this
  # command now. cmew boots an idle TUI that takes no initial prompt, and neither way a shell could
  # speak to it afterwards works: `claude --resume <id> -p` spawns a headless one-shot the live
  # session never sees, and send-keys types into the TUI, where a brief's newlines each submit a
  # half-finished prompt and its backticks get eaten by the shell before tmux ever sees them.
  # Delivery is a SendMessage from the manager (a Claude session, so it HAS the tool); this file is
  # what that message points the worker at, and the agent name printed below is its address.
  local brief_path="$wt_path/BRIEF.md"
  printf '%s\n' "$prompt" > "$brief_path"

  # cmew renames the session for display and the rename takes a moment to reach `claude agents`;
  # reading it immediately returns nothing.
  sleep 3
  local identity session_id agent_name
  identity="$(resolve_agent_identity "$task")"
  IFS=$'\t' read -r session_id agent_name <<< "$identity"
  if [[ -z "$session_id" ]]; then
    echo "error: launched '$task' into tmux session $pane, but could not resolve its session_id" >&2
    echo "       via 'claude agents --json'. Attach and check it started: cmew a $task" >&2
    exit 1
  fi

  reg_merge_entry "$task" "$(session_registry_patch "$session_id" "$agent_name" "$DISPATCH_MODEL" "$DISPATCH_EFFORT")"
  echo ">> $task provisioned: tmux $pane  session $session_id${DISPATCH_MODEL:+  model $DISPATCH_MODEL}${DISPATCH_EFFORT:+  effort $DISPATCH_EFFORT}"
  echo "   SendMessage target (exact name, copy it verbatim):  $agent_name"
  echo "   brief written to $brief_path — NOT delivered; a shell cannot send a message."
  echo "   brief it from the manager:  SendMessage to \"$agent_name\": Đọc BRIEF.md trong thư mục này rồi làm theo."
  echo "   attach and talk to it:  cmew a $task     (detach: Ctrl-b then d)"
}

cmd_manager_start() {
  # Bring up the resident manager. Everything else in this script provisions a place for work to
  # happen; this provisions the session that decides what work happens, and it is the only session
  # that has to be reachable without SendMessage — workers message it, but the CTO's desktop
  # session has no SendMessage at all and there is no second manager to ask.
  [[ $# -eq 0 ]] || {
    echo "error: manager-start takes no arguments — $MANAGER_MODEL at effort $MANAGER_EFFORT is the standing decision" >&2
    exit 1
  }

  local pane="cc-$MANAGER_TASK"
  if tmux_session_exists "$pane"; then
    # Resident means one. A second manager would take assignments off the same ledger and brief the
    # same workers with no idea the first exists, and whichever one a worker happens to message
    # decides what it hears.
    echo "error: $pane is already running — the manager is resident, one at a time is the point" >&2
    echo "       walk in and talk to it:   cmew a $MANAGER_TASK   (detach: Ctrl-b then d)" >&2
    echo "       replace it deliberately:  cmew kill $MANAGER_TASK && $0 manager-start" >&2
    exit 1
  fi
  [[ -f "$REPO_ROOT/$MANAGER_CHARTER" ]] || {
    echo "error: no charter at $REPO_ROOT/$MANAGER_CHARTER" >&2
    echo "       a manager session without its SKILL.md is just a chat window — refusing to start one" >&2
    exit 1
  }

  scrub_inherited_claude_env

  local launch_out
  if ! launch_out="$( cmew new "$MANAGER_TASK" "$REPO_ROOT" -e "$MANAGER_EFFORT" -m "$MANAGER_MODEL" 2>&1 )"; then
    echo "error: cmew failed to launch the manager:" >&2
    echo "$launch_out" >&2
    exit 1
  fi

  wait_for_tui "$pane" "$MANAGER_TASK" || exit 1

  # The only send-keys delivery left in this script, and it is here because nothing else can reach
  # this session. It sends a POINTER, never the charter itself: SKILL.md is 400 lines, and each of
  # its newlines through send-keys would submit a separate half-finished prompt. The path is
  # relative because cmew opened the session in REPO_ROOT.
  local charter_line="Đọc $MANAGER_CHARTER rồi nhận vai đó và bắt đầu trực. Đừng thoát phiên."
  local sent=0 attempt
  for attempt in 1 2 3; do
    tmux send-keys -t "$pane" "$charter_line"
    sleep 1
    # Typed, not just fired: confirm the text actually reached the input box before pressing Enter.
    # Fire-and-hope is how a session ends up idle at an empty prompt, looking exactly like one that
    # was told nothing. The needle is the path, early in the line, so a pane that wraps the rest of
    # the sentence still matches.
    if pane_has "$pane" "$MANAGER_CHARTER"; then
      tmux send-keys -t "$pane" Enter
      sent=1
      break
    fi
    sleep 2
  done
  if ((sent == 0)); then
    echo "error: the manager is up in $pane but its charter never reached the input box" >&2
    echo "       send it by hand: cmew a $MANAGER_TASK, then type: $charter_line" >&2
    exit 1
  fi

  sleep 3
  local identity session_id agent_name
  identity="$(resolve_agent_identity "$MANAGER_TASK")"
  IFS=$'\t' read -r session_id agent_name <<< "$identity"
  if [[ -z "$session_id" ]]; then
    echo "error: the manager is up in $pane but its session_id could not be resolved via" >&2
    echo "       'claude agents --json'. Attach and check it started: cmew a $MANAGER_TASK" >&2
    exit 1
  fi

  # A registry row so every consumer that already walks the registry — the board, a worker looking
  # up who to escalate to — finds the manager by name and gets its exact SendMessage address.
  # `branch` stays null: the manager runs in the repo root, whose branch is whatever the CTO last
  # checked out, so recording it would be stale within the hour. `adopted: true` is the
  # load-bearing field — it is what stops `stop`/`rm manager` from running a dev-stack teardown and
  # `git worktree remove` against the repo root itself.
  reg_set_entry "$MANAGER_TASK" "$(jq -n --arg path "$REPO_ROOT" \
    --argjson patch "$(session_registry_patch "$session_id" "$agent_name" "$MANAGER_MODEL" "$MANAGER_EFFORT")" \
    '{branch:null, path:$path, mode:"manager", num:null, ports:null, ado_ids:[], adopted:true} + $patch')"

  echo ">> manager up: tmux $pane  session $session_id  model $MANAGER_MODEL  effort $MANAGER_EFFORT"
  echo "   SendMessage target (exact name, copy it verbatim):  $agent_name"
  echo "   charter delivered: $MANAGER_CHARTER"
  echo "   walk in and talk to it:  cmew a $MANAGER_TASK     (detach: Ctrl-b then d)"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  [[ $# -ge 1 ]] || usage
  COMMAND="$1"; shift || true
  case "$COMMAND" in
    start)    cmd_start "$@" ;;
    dispatch) cmd_dispatch "$@" ;;
    manager-start) cmd_manager_start "$@" ;;
    list)     cmd_list "$@" ;;
    stop)     cmd_stop "$@" ;;
    rm)       cmd_rm "$@" ;;
    *)
      echo "error: unknown command '$COMMAND'" >&2
      usage
      ;;
  esac
fi
