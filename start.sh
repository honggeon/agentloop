#!/usr/bin/env bash
# agentloop 本地开发看板启动脚本
# 用法:
#   ./start.sh            # 启动后端 + 前端
#   ./start.sh status     # 查看运行状态
#   ./start.sh stop       # 停止后端 + 前端
#   ./start.sh restart    # 重启
# 环境变量(可用 .env 覆盖):
#   BACKEND_HOST / BACKEND_PORT / FRONTEND_PORT / RELOAD(1=后端热重载)

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/.logs"
mkdir -p "$LOG_DIR"
BACKEND_LOG="$LOG_DIR/backend.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

# 加载 .env(若存在)
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_PORT="${BACKEND_PORT:-18080}"
FRONTEND_PORT="${FRONTEND_PORT:-6173}"
RELOAD="${RELOAD:-0}"

is_port_listening() {
  lsof -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
}

is_backend_healthy() {
  curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$BACKEND_PORT/api/health" | grep -q '"status": *"ok"'
}

wait_healthy() {
  local desc="$1" fn="$2" tries=30
  printf '等待 %s 就绪 ...' "$desc"
  for _ in $(seq 1 "$tries"); do
    if $fn; then
      printf ' OK\n'
      return 0
    fi
    printf '.'
    sleep 1
  done
  printf ' 超时!\n'
  return 1
}

start_backend() {
  if is_port_listening "$BACKEND_PORT"; then
    echo "后端已在运行 (端口 $BACKEND_PORT)，跳过"
    return 0
  fi
  echo "启动后端: uvicorn backend.app.main:app --host $BACKEND_HOST --port $BACKEND_PORT"
  local reload_args=""
  if [ "$RELOAD" = "1" ]; then reload_args="--reload"; fi
  nohup uv run uvicorn backend.app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" $reload_args \
    >>"$BACKEND_LOG" 2>&1 &
  wait_healthy "后端" is_backend_healthy || {
    echo "后端启动失败，日志: $BACKEND_LOG"
    tail -20 "$BACKEND_LOG"
    return 1
  }
}

start_frontend() {
  if is_port_listening "$FRONTEND_PORT"; then
    echo "前端已在运行 (端口 $FRONTEND_PORT)，跳过"
    return 0
  fi
  if [ ! -d "$ROOT/frontend/node_modules" ]; then
    echo "frontend/node_modules 不存在，先执行 npm install ..."
    (cd "$ROOT/frontend" && npm install) || return 1
  fi
  echo "启动前端: npm run dev -- --host 0.0.0.0 --port $FRONTEND_PORT"
  (cd "$ROOT/frontend" && nohup npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" \
    >>"$FRONTEND_LOG" 2>&1 &)
  sleep 3
  if is_port_listening "$FRONTEND_PORT"; then
    echo "前端就绪 (端口 $FRONTEND_PORT)"
  else
    echo "前端启动失败，日志: $FRONTEND_LOG"
    tail -20 "$FRONTEND_LOG"
    return 1
  fi
}

stop_all() {
  local stopped=0
  # 按端口找 PID 停止
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    local pids
    pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$pids" ]; then
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
      stopped=1
    fi
  done
  # 兜底: 清理残留的后端/前端进程
  pkill -f "uvicorn backend.app.main:app" 2>/dev/null || true
  pkill -f "vite.*--port $FRONTEND_PORT" 2>/dev/null || true
  sleep 1
  if [ "$stopped" = "1" ]; then
    echo "已停止后端(18080) + 前端(5173)"
  else
    echo "没有正在运行的服务"
  fi
}

status() {
  local backend="未运行" frontend="未运行"
  if is_port_listening "$BACKEND_PORT"; then
    backend="运行中(端口 $BACKEND_PORT)"
    is_backend_healthy && backend="运行中(端口 $BACKEND_PORT, 健康)"
  fi
  if is_port_listening "$FRONTEND_PORT"; then
    frontend="运行中(端口 $FRONTEND_PORT)"
  fi
  echo "后端: $backend"
  echo "前端: $frontend"
  echo "看板: http://localhost:$FRONTEND_PORT"
}

case "${1:-start}" in
start)
  start_backend || exit 1
  start_frontend || exit 1
  echo ""
  echo "✅ 全部就绪"
  echo "   看板:    http://localhost:$FRONTEND_PORT"
  echo "   后端:    http://localhost:$BACKEND_PORT/api/health"
  echo "   日志:    $LOG_DIR"
  ;;
stop)
  stop_all
  ;;
restart)
  stop_all
  sleep 1
  start_backend || exit 1
  start_frontend || exit 1
  echo "✅ 重启完成"
  ;;
status)
  status
  ;;
*)
  echo "用法: $0 [start|stop|restart|status]"
  exit 1
  ;;
esac
