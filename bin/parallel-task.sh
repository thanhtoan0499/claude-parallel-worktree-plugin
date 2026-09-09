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
# Usage:
#   parallel-task.sh start    <task-name> <native|docker> [base-ref] [--ticket <id> ...]
#   parallel-task.sh dispatch <task-name> <prompt> [--model <model>] [--effort low|medium|high|xhigh|max]
#   parallel-task.sh list     [--json]
#   parallel-task.sh stop     <task-name>
#   parallel-task.sh rm       <task-name> [--force]
set -euo pipefail

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREES_DIR="$REPO_ROOT/.claude/worktrees"
REGISTRY="$WORKTREES_DIR/.parallel-registry.json"

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
  DISPATCH_MODEL=""; DISPATCH_EFFORT=""; DISPATCH_PROMPT=""
  local -a positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
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
  [[ -e "$WORKTREES_DIR/$task" ]] && { echo "error: $WORKTREES_DIR/$task already exists" >&2; exit 1; }

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
      if [[ "$mode" == "docker" ]]; then
        docker_slot_busy "$num" && dev_status="running" || dev_status="stopped"
      else
        port_busy "$gw" && dev_status="running" || dev_status="stopped"
      fi
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
  [[ "$(reg_get --arg k "$task" 'has($k)')" == "true" ]] || { echo "error: unknown task '$task' (see: $0 list)" >&2; exit 1; }
  local wt_path
  wt_path="$(reg_get --arg k "$task" '.[$k].path')"

  local -a launch=(claude --bg -n "$task")
  if [[ -n "$DISPATCH_MODEL" ]]; then launch+=(--model "$DISPATCH_MODEL"); fi
  if [[ -n "$DISPATCH_EFFORT" ]]; then launch+=(--effort "$DISPATCH_EFFORT"); fi
  launch+=(--)
  launch+=("$prompt")

  local launch_out
  if ! launch_out="$( cd "$wt_path" && "${launch[@]}" 2>&1 )"; then
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

  reg_merge_entry "$task" "$(jq -n \
    --arg sid "$short_id" --arg fid "$session_id" \
    --arg m "$DISPATCH_MODEL" --arg e "$DISPATCH_EFFORT" \
    '{short_id:$sid, session_id:$fid}
       + (if $m == "" then {} else {model:$m} end)
       + (if $e == "" then {} else {effort:$e} end)')"
  echo ">> $task dispatched: short id $short_id  session $session_id${DISPATCH_MODEL:+  model $DISPATCH_MODEL}${DISPATCH_EFFORT:+  effort $DISPATCH_EFFORT}"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  [[ $# -ge 1 ]] || usage
  COMMAND="$1"; shift || true
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
