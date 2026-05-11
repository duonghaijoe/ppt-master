#!/usr/bin/env bash
# Start backend (FastAPI on 8787) and frontend (Vite on 5173).
# Kills any existing instances on those ports first.
# Logs to /tmp/ppt-backend.log and /tmp/ppt-frontend.log.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/web/backend"
FRONTEND_DIR="$ROOT/web/frontend"
VENV_PY="$ROOT/.venv/bin/python"
BACKEND_PORT=8787
FRONTEND_PORT=5173
BACKEND_LOG=/tmp/ppt-backend.log
FRONTEND_LOG=/tmp/ppt-frontend.log

kill_port() {
  local port=$1 label=$2
  local pids
  pids=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
  if [[ -n "$pids" ]]; then
    echo "[$label] killing existing pids on :$port -> $pids"
    kill -9 $pids 2>/dev/null || true
    sleep 1
  fi
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

kill_port "$BACKEND_PORT" backend
kill_port "$FRONTEND_PORT" frontend

echo "[backend] starting on :$BACKEND_PORT"
( cd "$BACKEND_DIR" && nohup "$VENV_PY" -m uvicorn main:app \
    --host 127.0.0.1 --port "$BACKEND_PORT" \
    > "$BACKEND_LOG" 2>&1 & echo $! > /tmp/ppt-backend.pid )

echo "[frontend] starting on :$FRONTEND_PORT"
( cd "$FRONTEND_DIR" && nohup npm run dev -- --port "$FRONTEND_PORT" \
    > "$FRONTEND_LOG" 2>&1 & echo $! > /tmp/ppt-frontend.pid )

wait_for "http://127.0.0.1:$BACKEND_PORT/api/projects" backend "$BACKEND_LOG"
wait_for "http://127.0.0.1:$FRONTEND_PORT" frontend "$FRONTEND_LOG"

echo
echo "backend  pid $(cat /tmp/ppt-backend.pid)  log $BACKEND_LOG"
echo "frontend pid $(cat /tmp/ppt-frontend.pid) log $FRONTEND_LOG"
echo "open http://127.0.0.1:$FRONTEND_PORT"
