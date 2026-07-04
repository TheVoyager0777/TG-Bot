#!/usr/bin/env bash
# webapp.sh —— 一键打开 Phantom Console（自动起双隧道 + 拼 URL + 打开浏览器）
#   ./webapp.sh                # 起隧道 → 打印链接 → 尝试打开浏览器
#   ./webapp.sh --url-only     # 只打印链接，不打开浏览器
#   ./webapp.sh --local        # 本地直连，不起隧道
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_PORT="${API_PORT:-8788}"
STATIC_PORT="${STATIC_PORT:-8789}"
URL_ONLY=false
LOCAL=false

for a in "$@"; do
  case "$a" in
    --url-only) URL_ONLY=true ;;
    --local) LOCAL=true ;;
  esac
done

# ── 获取 console key ──
TOKEN=$(grep -oP 'token\s*=\s*"\K[^"]+' "$SCRIPT_DIR/config.toml" | head -1)
if [ -z "$TOKEN" ]; then
  echo "未能从 config.toml 读取 token"
  exit 1
fi
KEY=$(python3 -c "
import hmac,hashlib
print(hmac.new(b'$TOKEN', b'phantom-console-v1', hashlib.sha256).hexdigest()[:32])
")

# ── 本地模式 ──
if $LOCAL; then
  STATIC_URL="http://127.0.0.1:${STATIC_PORT}"
  API_URL="http://127.0.0.1:${API_PORT}"
  FULL_URL="${STATIC_URL}/?api=${API_URL}&key=${KEY}"
  echo ""
  echo "═══ 本地直连 ═══"
  echo "$FULL_URL"
  echo "═══════════════"
  if ! $URL_ONLY; then
    xdg-open "$FULL_URL" 2>/dev/null || open "$FULL_URL" 2>/dev/null || true
  fi
  exit 0
fi

# ── 隧道模式 ──
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared 未安装，回退到本地模式"
  exec "$0" --local
fi

TMPDIR="${TMPDIR:-/tmp}"
API_LOG="$TMPDIR/cf-api-$$.log"
STATIC_LOG="$TMPDIR/cf-static-$$.log"

# 起隧道
cloudflared tunnel --url "http://localhost:${API_PORT}" > "$API_LOG" 2>&1 &
API_PID=$!
cloudflared tunnel --url "http://localhost:${STATIC_PORT}" > "$STATIC_LOG" 2>&1 &
STATIC_PID=$!

cleanup() { kill $API_PID $STATIC_PID 2>/dev/null; rm -f "$API_LOG" "$STATIC_LOG"; }
trap cleanup EXIT

# 等域名
echo -n "起隧道中"
for i in $(seq 1 20); do
  echo -n "."
  sleep 1
  API_DOMAIN=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$API_LOG" 2>/dev/null | head -1 || true)
  STATIC_DOMAIN=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$STATIC_LOG" 2>/dev/null | head -1 || true)
  if [ -n "$API_DOMAIN" ] && [ -n "$STATIC_DOMAIN" ]; then
    FULL_URL="${STATIC_DOMAIN}/?api=${API_DOMAIN}&key=${KEY}"
    echo ""
    echo ""
    echo "═══ Phantom Console ═══"
    echo "$FULL_URL"
    echo "══════════════════════"
    echo ""
    if ! $URL_ONLY; then
      xdg-open "$FULL_URL" 2>/dev/null || open "$FULL_URL" 2>/dev/null || true
    fi
    echo "Ctrl+C 关闭隧道"
    wait $API_PID $STATIC_PID 2>/dev/null || true
    exit 0
  fi
done

echo ""
echo "超时：未能获取隧道域名。cloudflared 可能未登录或网络不通。"
echo "试试: ./webapp.sh --local"
exit 1
