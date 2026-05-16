#!/usr/bin/env bash
# Start backend (FastAPI) and frontend (Vite). Ports are read from web/.env
# (BACKEND_PORT, FRONTEND_PORT) and default to 8765 / 5765.
#
# Only stops services this script started before (tracked via /tmp/ppt-*.pid).
# If the port is held by something else, we refuse to start instead of killing
# it — that previously took out unrelated apps holding the dev ports.
#
# Logs to /tmp/ppt-backend.log and /tmp/ppt-frontend.log.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/web/backend"
FRONTEND_DIR="$ROOT/web/frontend"
VENV_PY="$ROOT/.venv/bin/python"

# Load shared dev config (BACKEND_PORT, FRONTEND_PORT) from web/.env if present.
ENV_FILE="$ROOT/web/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
BACKEND_PORT="${BACKEND_PORT:-8765}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_LOG=/tmp/ppt-backend.log
FRONTEND_LOG=/tmp/ppt-frontend.log
BACKEND_PIDFILE=/tmp/ppt-backend.pid
FRONTEND_PIDFILE=/tmp/ppt-frontend.pid

# Stop only the process recorded in our pidfile. Verify the pid still exists
# before killing — if the file is stale (system rebooted, pid recycled to a
# random app), we don't touch it.
stop_tracked() {
  local pidfile=$1 label=$2 cmd_match=$3
  [[ -f "$pidfile" ]] || return 0
  local pid
  pid=$(cat "$pidfile" 2>/dev/null || true)
  rm -f "$pidfile"
  [[ -n "$pid" ]] || return 0
  if ! kill -0 "$pid" 2>/dev/null; then
    # Process is gone — pidfile was stale.
    return 0
  fi
  # Sanity check the command matches what we expect before killing. Guards
  # against pid recycling onto an unrelated process between runs.
  local cmd
  cmd=$(ps -o command= -p "$pid" 2>/dev/null || true)
  if [[ -z "$cmd" ]]; then
    return 0
  fi
  if [[ "$cmd" != *"$cmd_match"* ]]; then
    echo "[$label] pid $pid is no longer ours (cmd: $cmd) — leaving it alone"
    return 0
  fi
  echo "[$label] stopping previous instance pid $pid"
  kill "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
}

# If a stranger holds the port we want, surface it instead of killing them.
check_port_free() {
  local port=$1 label=$2
  local pids cmd
  pids=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
  [[ -z "$pids" ]] && return 0
  for pid in $pids; do
    cmd=$(ps -o command= -p "$pid" 2>/dev/null || echo "?")
    echo "[$label] port $port already in use by pid $pid: $cmd" >&2
  done
  echo "[$label] refusing to kill an unrelated process. Free the port and retry." >&2
  exit 1
}

wait_for() {
  local url=$1 label=$2 log=$3
  for i in {1..30}; do
    if curl -sf "$url" >/dev/null 2>&1; then
      echo "[$label] up at $url"
      return 0
    fi
    sleep 0.5
  done
  echo "[$label] failed to come up; last log lines:"
  tail -30 "$log" || true
  return 1
}

if [[ ! -x "$VENV_PY" ]]; then
  echo "venv python not found at $VENV_PY" >&2
  exit 1
fi

stop_tracked "$BACKEND_PIDFILE" backend uvicorn
stop_tracked "$FRONTEND_PIDFILE" frontend vite

check_port_free "$BACKEND_PORT" backend
check_port_free "$FRONTEND_PORT" frontend

echo "[backend] starting on :$BACKEND_PORT"
( cd "$BACKEND_DIR" && PPT_DEV_AUTH=1 nohup "$VENV_PY" -m uvicorn main:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" \
    > "$BACKEND_LOG" 2>&1 & echo $! > "$BACKEND_PIDFILE" )

echo "[frontend] starting on :$FRONTEND_PORT"
( cd "$FRONTEND_DIR" && nohup npm run dev -- --port "$FRONTEND_PORT" \
    > "$FRONTEND_LOG" 2>&1 & echo $! > "$FRONTEND_PIDFILE" )

wait_for "http://127.0.0.1:$BACKEND_PORT/api/health" backend "$BACKEND_LOG"
wait_for "http://127.0.0.1:$FRONTEND_PORT" frontend "$FRONTEND_LOG"

echo
echo "backend  pid $(cat "$BACKEND_PIDFILE")  log $BACKEND_LOG"
echo "frontend pid $(cat "$FRONTEND_PIDFILE") log $FRONTEND_LOG"
echo "open http://127.0.0.1:$FRONTEND_PORT"
