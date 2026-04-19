#!/usr/bin/env bash
# Claude Web Server — Linux / macOS 启动脚本（与 Windows 下 start.bat 行为一致）
# 用法: ./start.sh [可选 Token]
# 首次使用: chmod +x start.sh

set -u
cd "$(dirname "$0")"

echo "================================"
echo "  Claude Web Server 启动脚本"
echo "================================"
echo

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[错误] 未检测到 Python，请先安装 Python 3.8+"
  exit 1
fi

SERVER_PORT="$("$PY" -c "import sys; sys.path.insert(0, '.'); from claude_web import config; print(config.SERVER_PORT)" 2>/dev/null | tr -d '\r\n' || true)"
if [ -z "${SERVER_PORT:-}" ]; then
  SERVER_PORT=8080
fi

echo "[1/3] 检查依赖..."
if ! "$PY" -m pip install -r requirements.txt -q 2>/dev/null; then
  echo "[安装] 正在安装 requirements.txt ..."
  "$PY" -m pip install -r requirements.txt
fi

TOKEN="${1:-}"
if [ -z "$TOKEN" ]; then
  echo "[Token] 未设置，局域网内无需认证"
else
  echo "[Token] $TOKEN"
fi

echo "[2/3] 清理旧进程（端口 ${SERVER_PORT}）..."
KILL_ANY=0
if command -v fuser >/dev/null 2>&1; then
  if fuser "${SERVER_PORT}/tcp" >/dev/null 2>&1; then
    fuser -k "${SERVER_PORT}/tcp" >/dev/null 2>&1 || true
    KILL_ANY=1
    echo "  已尝试释放占用端口 ${SERVER_PORT} 的进程（fuser）"
  else
    echo "  未发现占用端口 ${SERVER_PORT} 的进程"
  fi
elif command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti ":${SERVER_PORT}" 2>/dev/null || true)
  if [ -n "${PIDS:-}" ]; then
    for pid in $PIDS; do
      kill -9 "$pid" 2>/dev/null || true
    done
    KILL_ANY=1
    echo "  已尝试终止占用端口的进程（lsof）"
  else
    echo "  未发现占用端口 ${SERVER_PORT} 的进程"
  fi
else
  echo "  [提示] 未找到 fuser/lsof，跳过端口清理；若启动失败请检查端口是否被占用"
fi

if [ "$KILL_ANY" -eq 1 ]; then
  sleep 1
fi

echo "[3/3] 启动服务..."
echo
echo "局域网访问地址（端口 ${SERVER_PORT}）:"
echo "  http://127.0.0.1:${SERVER_PORT}"
if [ -n "${TOKEN:-}" ]; then
  echo "  http://127.0.0.1:${SERVER_PORT}?token=${TOKEN}"
fi
if command -v hostname >/dev/null 2>&1; then
  # Linux 常见；macOS 无 hostname -I，忽略即可
  _HI=$(hostname -I 2>/dev/null || true)
  if [ -n "${_HI:-}" ]; then
    for ip in $_HI; do
      case "$ip" in
        127.*) continue ;;
        ::1) continue ;;
        *)
          if [ -z "${TOKEN:-}" ]; then
            echo "  http://${ip}:${SERVER_PORT}"
          else
            echo "  http://${ip}:${SERVER_PORT}?token=${TOKEN}"
          fi
          ;;
      esac
    done
  fi
fi
echo
echo "提示: 每个浏览器有独立的会话，互不干扰"
echo

if [ -z "${TOKEN:-}" ]; then
  exec "$PY" server.py
else
  exec "$PY" server.py "$TOKEN"
fi
