#!/data/data/com.termux/files/usr/bin/bash
# APKが作った1行コマンドから呼ばれる初期設定。何度実行しても同じ結果になる。
#   BRIDGE_TOKEN=<APKが決めた合言葉> bash setup.sh
#
# やること:
#   1. 必要なパッケージを入れる
#   2. Termuxの設定 allow-external-apps を有効にする（APKからTermuxを起動するために必要）
#   3. APKが決めたトークンを ~/.auto-deploy/bridge_token に保存する
#   4. ブリッジを（再）起動する
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

TOKEN="${BRIDGE_TOKEN:-}"
if [ -n "$TOKEN" ]; then
  case "$TOKEN" in
    *[!A-Za-z0-9_-]*) echo "ERROR: BRIDGE_TOKEN に使えない文字があります" >&2; exit 1 ;;
  esac
  if [ "${#TOKEN}" -lt 16 ]; then echo "ERROR: BRIDGE_TOKEN が短すぎます" >&2; exit 1; fi
fi

echo "==> 1/4 パッケージを入れます"
if [ -z "$SETUP_SKIP_INSTALL" ]; then
  if command -v pkg >/dev/null 2>&1; then pkg install -y python curl; fi
  if ! pip install -r "$DIR/requirements.txt"; then
    echo "ERROR: pipが失敗しました。pydanticのビルドが原因なら、'pkg install python-pydantic' を実行してから、もう一度このコマンドを貼ってください" >&2
    exit 1
  fi
fi

echo "==> 2/4 Termuxの設定（APKからの起動を許可）"
mkdir -p "$HOME/.termux" "$HOME/.auto-deploy"
PROPS="$HOME/.termux/termux.properties"
touch "$PROPS"
if grep -Eq '^[[:space:]]*allow-external-apps[[:space:]]*=[[:space:]]*true[[:space:]]*$' "$PROPS"; then
  echo "設定済みです"
else
  if grep -Eq '^#[[:space:]]*allow-external-apps' "$PROPS"; then
    sed -i -E 's/^#[[:space:]]*allow-external-apps.*/allow-external-apps = true/' "$PROPS"
  else
    sed -i -E '/^[[:space:]]*allow-external-apps[[:space:]]*=/d' "$PROPS"
    echo "allow-external-apps = true" >> "$PROPS"
  fi
  echo "allow-external-apps を有効にしました"
fi
if command -v termux-reload-settings >/dev/null 2>&1; then termux-reload-settings || true; fi

echo "==> 3/4 トークンを保存します"
if [ -n "$TOKEN" ]; then
  umask 077
  printf '%s' "$TOKEN" > "$HOME/.auto-deploy/bridge_token"
  chmod 600 "$HOME/.auto-deploy/bridge_token"
  echo "保存しました"
else
  echo "BRIDGE_TOKEN が無いため、既存のトークンをそのまま使います"
fi

echo "==> 4/4 ブリッジを（再）起動します"
if [ -z "$SETUP_SKIP_START" ]; then
  pkill -f "uvicorn server:app" 2>/dev/null || true
  sleep 1
  bash "$DIR/start.sh"
fi

echo "完了しました。アプリに戻って「接続を確認」を押してください。"
