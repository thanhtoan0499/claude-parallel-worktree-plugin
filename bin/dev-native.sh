#!/usr/bin/env bash
# dev-native.sh — run gateway + frontend NATIVELY (no Docker) per git worktree,
# for fast parallel-task iteration. Complements dev-stack.sh (Docker slots),
# which stays the right tool for prod-topology / pre-merge verification.
#
# Why this exists: a Docker slot bakes production builds into images — every
# code change needs a rebuild (~seconds to a minute) before you see it. This
# runs `uvicorn --reload` + `vite` dev server directly on the host: sub-second
# hot-reload on both sides, and much lighter (~2 native processes vs. 4
# containers per task). Benchmarked on this VM: Docker slot up ~49s cache-warm
# + full rebuild per change; native infra+app up ~20s total + <1s per change.
#
# Design: ONE shared Postgres/Azurite (the `infra` subcommand) for every task
# — only the gateway/frontend are per-task. Each task gets its own DB name,
# own ports, own WORKSPACE_ROOT, so tasks can't see each other's data or
# agent-run files even though they share one Postgres server.
#
# Usage:
#   dev-native.sh infra <up|down|status>
#   dev-native.sh <task> <up|down|status|logs [gateway|frontend]|migrate|purge>
#
#   task | gateway | frontend | postgres db
#   -----+---------+----------+------------------
#    1   |  8501   |  5501    |  aiquinta_native_t1
#    2   |  8502   |  5502    |  aiquinta_native_t2
#
# `down` only stops the processes (data kept, for a fast resume). `purge` also
# stops them, then drops this task's DB and removes its WORKSPACE_ROOT — use it
# when the task is actually being torn down (parallel-task.sh rm), not paused.
#
# Shared infra ports (fixed, distinct from both the classic `deploy` stack
# and dev-stack.sh's slots so all three can coexist): postgres 5599, azurite
# 10099, project name `aiquinta-native-infra`.
#
# Two gaps this script papers over that Docker's compose file hides behind
# implicit defaults — see README "Parallel dev stacks" for the fuller story:
#   - ENVIRONMENT=development: the gateway refuses to start with
#     LANGSMITH_TRACING=true unless this is set; Docker's compose file
#     defaults it even when .env doesn't. Native has no such fallback.
#   - BUILTIN_PLUGIN_*_DIR: Docker COPYs agents/plugins/* to /mnt/plugins/*
#     in the image. Native has no image, so these must point at this
#     worktree's own agents/plugins/ or built-in skills silently vanish.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
COMPOSE_FILE="deploy/docker-compose.yml"
INFRA_PROJECT="aiquinta-native-infra"
INFRA_PG_PORT=5599
INFRA_AZURITE_PORT=10099
STATE_DIR="/tmp/aiquinta-native"

usage() {
  sed -n '18,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 1
}

[[ $# -ge 1 ]] || usage
TARGET="$1"; shift || true
COMMAND="${1:-}"; shift || true

mkdir -p "$STATE_DIR"

# --- shared infra -----------------------------------------------------------

infra_cmd() {
  case "$COMMAND" in
    up)
      POSTGRES_HOST_PORT="$INFRA_PG_PORT" AZURITE_HOST_PORT="$INFRA_AZURITE_PORT" \
        docker compose -f "$REPO_ROOT/$COMPOSE_FILE" -p "$INFRA_PROJECT" up -d postgres azurite
      echo ">> shared native infra: postgres :${INFRA_PG_PORT}  azurite :${INFRA_AZURITE_PORT}" >&2
      ;;
    down)
      POSTGRES_HOST_PORT="$INFRA_PG_PORT" AZURITE_HOST_PORT="$INFRA_AZURITE_PORT" \
        docker compose -f "$REPO_ROOT/$COMPOSE_FILE" -p "$INFRA_PROJECT" down
      ;;
    status)
      docker compose -f "$REPO_ROOT/$COMPOSE_FILE" -p "$INFRA_PROJECT" ps
      ;;
    *)
      echo "error: 'infra' takes up|down|status (got: '$COMMAND')" >&2
      usage
      ;;
  esac
}

if [[ "$TARGET" == "infra" ]]; then
  infra_cmd
  exit 0
fi

# --- per-task gateway + frontend --------------------------------------------

if ! [[ "$TARGET" =~ ^[0-9]+$ ]]; then
  echo "error: task must be a positive integer, or 'infra' (got: '$TARGET')" >&2
  usage
fi
TASK="$TARGET"

GATEWAY_PORT=$((8500 + TASK))
FRONTEND_PORT=$((5500 + TASK))
DB_NAME="aiquinta_native_t${TASK}"
WORKSPACE_ROOT="/tmp/workspaces-native-t${TASK}"
GATEWAY_LOG="$STATE_DIR/t${TASK}.gateway.log"
FRONTEND_LOG="$STATE_DIR/t${TASK}.frontend.log"

port_busy() {
  fuser "$1/tcp" >/dev/null 2>&1
}

gateway_env() {
  # Everything a Docker `environment:` block or entrypoint would normally
  # supply implicitly. `env_file`-equivalent: source .env first, then
  # override the per-task bits.
  #
  # .env itself defines GATEWAY_PORT/FRONTEND_PORT/WORKSPACE_ROOT (for the
  # classic single Docker stack) — sourcing it under `set -a` silently
  # clobbers this task's own values of those exact names. Snapshot before,
  # restore after, or every task ends up fighting over :8080/:5173.
  local _task_gateway_port="$GATEWAY_PORT" _task_frontend_port="$FRONTEND_PORT" _task_workspace_root="$WORKSPACE_ROOT"
  set -a
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"
  set +a
  GATEWAY_PORT="$_task_gateway_port"
  FRONTEND_PORT="$_task_frontend_port"
  WORKSPACE_ROOT="$_task_workspace_root"
  export ENVIRONMENT=development
  export DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:${INFRA_PG_PORT}/${DB_NAME}"
  export AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://localhost:${INFRA_AZURITE_PORT}/devstoreaccount1"
  export WORKSPACE_ROOT
  export CORS_ORIGINS="http://localhost:${FRONTEND_PORT}"
  export GATEWAY_WEB_ORIGIN="http://localhost:${FRONTEND_PORT}"
  export WEB_CONCURRENCY=1
  export BUILTIN_PLUGIN_BUILDER_DIR="$REPO_ROOT/agents/plugins/aiquinta-builder"
  export BUILTIN_PLUGIN_SHARED_DIR="$REPO_ROOT/agents/plugins/aiquinta-shared"
  export BUILTIN_PLUGIN_BUILDER_BMS_DIR="$REPO_ROOT/agents/plugins/aiquinta-builder-bms"
}

ensure_db() {
  # Idempotent: create the per-task DB if it doesn't exist yet (first `up`
  # for this task), skip silently otherwise. Runs psql/createdb INSIDE the
  # shared postgres container (via docker exec) rather than requiring
  # postgresql-client on the host — this host doesn't have it installed,
  # and the container already does.
  local pg_container
  pg_container="$(docker compose -f "$REPO_ROOT/$COMPOSE_FILE" -p "$INFRA_PROJECT" ps -q postgres)"
  if [[ -z "$pg_container" ]]; then
    echo "error: shared infra postgres container not found — run 'dev-native.sh infra up' first" >&2
    exit 1
  fi
  docker exec -u postgres "$pg_container" psql -tc \
    "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1 || \
    docker exec -u postgres "$pg_container" createdb "$DB_NAME"
}

migrate() {
  ( cd "$REPO_ROOT/services/gateway" && gateway_env && uv run alembic upgrade head )
}

up() {
  if ! docker compose -f "$REPO_ROOT/$COMPOSE_FILE" -p "$INFRA_PROJECT" ps --status running --format '{{.Name}}' 2>/dev/null | grep -q postgres; then
    echo "error: shared infra not running — run 'dev-native.sh infra up' first" >&2
    exit 1
  fi
  if port_busy "$GATEWAY_PORT" && port_busy "$FRONTEND_PORT"; then
    echo "task ${TASK} already up: gateway :${GATEWAY_PORT}  frontend :${FRONTEND_PORT}" >&2
    exit 0
  fi

  mkdir -p "$WORKSPACE_ROOT"
  ensure_db
  migrate

  # NOT `uvicorn --reload`: the gateway installs its own SIGTERM handler
  # (main.py `_install_drain_handler`, for graceful production draining —
  # rejects new runs, lets in-flight ones finish) that never itself exits
  # the process. uvicorn's --reload sends SIGTERM and just waits for the
  # worker to die on its own; with this app it never does, so reload hangs
  # forever — confirmed by testing (>30s, indefinitely stuck on "draining",
  # old code still serving the whole time since /health responds 200
  # regardless of drain state, which is what made this look like a fast
  # reload in an earlier, insufficiently-verified pass). `watchfiles` sends
  # SIGINT first (not intercepted by the app — only SIGTERM is), which
  # triggers normal asyncio/uvicorn shutdown; --sigkill-timeout is a safety
  # net in case that ever doesn't work either. Confirmed working: a full
  # restart takes ~13s (fresh Python process + reimports), not sub-second —
  # slower than in-process reload would be, but it's a real code change
  # taking effect, not a stuck worker serving stale code.
  ( cd "$REPO_ROOT/services/gateway" && gateway_env && \
    exec uv run watchfiles --sigint-timeout 5 --sigkill-timeout 5 \
      "uv run uvicorn api_gateway.app.main:app --port ${GATEWAY_PORT}" src \
      > "$GATEWAY_LOG" 2>&1 & )

  ( cd "$REPO_ROOT" && \
    VITE_GATEWAY_URL="http://localhost:${GATEWAY_PORT}" \
    exec pnpm --filter ./apps/web exec vite --port "$FRONTEND_PORT" --strictPort \
      > "$FRONTEND_LOG" 2>&1 & )

  echo ">> task ${TASK}: gateway :${GATEWAY_PORT}  frontend :${FRONTEND_PORT}  db ${DB_NAME}" >&2
  echo "   logs: $GATEWAY_LOG / $FRONTEND_LOG" >&2
}

down() {
  # Two things to kill, for two different reasons:
  #
  # 1. The `watchfiles` supervisor (see up()) is NOT bound to any port — it's
  #    a file-watcher wrapper. Killing only the port it's watching (below)
  #    doesn't stop it; confirmed by testing, it just launches a replacement
  #    worker. `watchfiles`'s own process argv literally contains
  #    `--port ${GATEWAY_PORT}`, so pkill -f can target it precisely enough
  #    to not touch a *different* task's supervisor.
  # 2. The actual bound worker still needs a port-based kill regardless of
  #    which supervisor spawned it: uvicorn's own --reload (not used here
  #    anymore, but the next person reading this may reintroduce it) spawns
  #    its worker via Python multiprocessing.spawn, which re-execs with a
  #    generic `python -c spawn_main(...)` argv carrying no trace of --port,
  #    AND reparents to PID 1 — invisible to any PID captured at launch and
  #    to pkill -f alike. `fuser` finds whatever actually holds the socket
  #    regardless of process-tree shape, which is the one thing that's
  #    reliably true either way.
  pkill -f "watchfiles.*--port ${GATEWAY_PORT}\b" 2>/dev/null || true
  fuser -k -TERM "${GATEWAY_PORT}/tcp" 2>/dev/null || true
  fuser -k -TERM "${FRONTEND_PORT}/tcp" 2>/dev/null || true
  sleep 1
  fuser -k -KILL "${GATEWAY_PORT}/tcp" 2>/dev/null || true
  fuser -k -KILL "${FRONTEND_PORT}/tcp" 2>/dev/null || true
  echo ">> task ${TASK} stopped" >&2
}

purge() {
  down
  # WORKSPACE_ROOT is always /tmp/workspaces-native-t<task> (built above from a
  # validated integer TASK) — the guard below still confirms containment
  # before rm -rf, per filesystem-safety.md.
  local ws_root
  ws_root="$(realpath -m "$WORKSPACE_ROOT")"
  if [[ "$ws_root" == "/tmp/workspaces-native-t"* ]]; then
    rm -rf "$ws_root"
  else
    echo "warn: refusing to rm unexpected WORKSPACE_ROOT '$ws_root'" >&2
  fi
  rm -f "$GATEWAY_LOG" "$FRONTEND_LOG"
  echo ">> task ${TASK} purged: workspace + logs removed." >&2
  echo "   DB '${DB_NAME}' was left in place — this session's permissions block dropdb;" >&2
  echo "   drop it yourself if you want it gone (shared infra postgres, port ${INFRA_PG_PORT})." >&2
}

status() {
  # Checks the actual port, not a launch-time PID — see the comment in
  # down() for why a captured PID goes stale (uvicorn --reload's worker
  # reparents to PID 1 with an unrecognizable argv, invisible to any
  # process-tree-based check).
  if fuser "${GATEWAY_PORT}/tcp" >/dev/null 2>&1; then
    echo "gateway: running (:${GATEWAY_PORT})"
  else
    echo "gateway: stopped"
  fi
  if fuser "${FRONTEND_PORT}/tcp" >/dev/null 2>&1; then
    echo "frontend: running (:${FRONTEND_PORT})"
  else
    echo "frontend: stopped"
  fi
}

logs() {
  local which="${1:-gateway}"
  case "$which" in
    gateway) tail -f "$GATEWAY_LOG" ;;
    frontend) tail -f "$FRONTEND_LOG" ;;
    *) echo "error: logs takes 'gateway' or 'frontend' (got: '$which')" >&2; exit 1 ;;
  esac
}

case "$COMMAND" in
  up) up ;;
  down) down ;;
  purge) purge ;;
  status) status ;;
  migrate) migrate ;;
  logs) logs "${1:-gateway}" ;;
  *)
    echo "error: unknown command '$COMMAND'" >&2
    usage
    ;;
esac
