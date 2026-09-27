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
#   parallel-task.sh start <task-name> <native|docker> [base-ref]
#   parallel-task.sh list
#   parallel-task.sh stop  <task-name>
#   parallel-task.sh rm    <task-name> [--force]
set -euo pipefail

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
WORKTREES_DIR="$REPO_ROOT/.claude/worktrees"
REGISTRY="$WORKTREES_DIR/.parallel-registry.json"

[[ $# -ge 1 ]] || usage
COMMAND="$1"; shift || true

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

# --- port / slot liveness checks ---------------------------------------------

port_busy() { fuser "$1/tcp" >/dev/null 2>&1; }

docker_slot_busy() {
  # true if slot N has any live container under its compose project name
  local n="$1"
  [[ -n "$(docker compose -f "$REPO_ROOT/deploy/docker-compose.yml" -p "aiquinta-mfg-s${n}" ps -q 2>/dev/null)" ]]
}

# Slot N's frontend port (5173+N*100) must be a registered WorkOS redirect
# URI — there's no local auth bypass, so an unregistered slot's login just
# fails. Cap allocation to the slots actually registered; extend this list
# (and register the new port in the WorkOS dashboard) to allow more.
DOCKER_SLOTS_ALLOWED="${DOCKER_SLOTS_ALLOWED:-1 2 3 4}"

next_free_docker_slot() {
  local used
  used="$(reg_get '[.[] | select(.mode=="docker") | .num] | map(tostring) | join(" ")')"
  for n in $DOCKER_SLOTS_ALLOWED; do
    [[ " $used " == *" $n "* ]] && continue
    docker_slot_busy "$n" && continue
    echo "$n"; return 0
  done
  echo "error: no free docker slot in allow-list ($DOCKER_SLOTS_ALLOWED) — each slot's port must be a registered WorkOS redirect URI. Stop an idle task (parallel-task.sh stop <task>), or register more ports and set DOCKER_SLOTS_ALLOWED to extend." >&2
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

cmd_start() {
  [[ $# -ge 2 ]] || { echo "error: start needs <task-name> <native|docker> [base-ref]" >&2; usage; }
  local task="$1" mode="$2" base_ref="${3:-origin/main}"

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
    --argjson num "$num" --argjson ports "$ports_json" \
    '{branch:$branch, path:$path, mode:$mode, num:$num, ports:$ports}')"
  trap - ERR

  echo ">> $task ready: branch $branch  mode $mode  worktree $wt_path"
  echo "   frontend: http://localhost:${fe}"
  echo "   gateway:  http://localhost:${gw}"
  echo "   NOTE: register http://localhost:${fe}/auth/callback in the WorkOS dashboard"
  echo "   redirect-URI allow-list before logging in on this copy (no local auth bypass)."
}

cmd_list() {
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

cmd_stop() {
  [[ $# -ge 1 ]] || { echo "error: stop needs <task-name>" >&2; usage; }
  local task="$1"
  [[ "$(reg_get --arg k "$task" 'has($k)')" == "true" ]] || { echo "error: unknown task '$task'" >&2; exit 1; }
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
  local path mode num
  path="$(reg_get --arg k "$task" '.[$k].path')"
  mode="$(reg_get --arg k "$task" '.[$k].mode')"
  num="$(reg_get --arg k "$task" '.[$k].num')"

  # rm is a final teardown, not a pause — unlike `stop`, also purge this
  # slot/task's data (volumes + locally-built images for docker; native's
  # scratch workspace) so removing N copies doesn't leave N sets of orphaned
  # DB/blob data behind forever. Runs even if the worktree dir is already
  # gone (path may not exist) — dev-stack.sh/dev-native.sh only need REPO_ROOT.
  if [[ -d "$path" ]]; then
    if [[ "$mode" == "docker" ]]; then
      ( cd "$path" && dev-stack.sh "$num" down -v --rmi local ) || true
    else
      ( cd "$path" && dev-native.sh "$num" purge ) || true
    fi
  fi

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

case "$COMMAND" in
  start) cmd_start "$@" ;;
  list)  cmd_list "$@" ;;
  stop)  cmd_stop "$@" ;;
  rm)    cmd_rm "$@" ;;
  *)
    echo "error: unknown command '$COMMAND'" >&2
    usage
    ;;
esac
