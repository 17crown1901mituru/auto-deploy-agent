#!/data/data/com.termux/files/usr/bin/bash
# ブリッジを起動する。すでに動いていれば何もしない（APKから何度呼ばれても安全）。
cd "$(dirname "$0")" || exit 1
mkdir -p "$HOME/.auto-deploy"

if [ -f "$HOME/.auto-deploy/env" ]; then
  set -a
  . "$HOME/.auto-deploy/env"
  set +a
fi

PORT="${BRIDGE_PORT:-8000}"
if curl -fs "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
  echo "bridge is already running on port ${PORT}"
  exit 0
fi

# 画面を消してもTermuxが止まりにくくする
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

nohup python -m uvicorn server:app --host 127.0.0.1 --port "$PORT" \
  >> "$HOME/.auto-deploy/bridge.log" 2>&1 &
echo "bridge started on port ${PORT} (log: ~/.auto-deploy/bridge.log)"
