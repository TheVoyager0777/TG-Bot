#!/usr/bin/env bash
# tunnel.sh —— 两条 cloudflared 隧道分拆静态 UI 与数据 API。
#   ./tunnel.sh            # API_PORT=8788 STATIC_PORT=8789
#   API_PORT=8765 ./tunnel.sh
# 起好后：[static] = 公开隧道（分享给浏览器），[api] = 私有隧道（带 key）。
# 浏览器打开: https://<static-domain>/?api=https://<api-domain>&key=<console-key>
set -euo pipefail
API_PORT="${API_PORT:-8788}"
STATIC_PORT="${STATIC_PORT:-8789}"

if ! command -v cloudflared >/dev/null 2>&1; then
    cat <<'EOF'
[tunnel] 未找到 cloudflared，安装:
  curl -fsSL -o /tmp/cloudflared.deb \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  sudo dpkg -i /tmp/cloudflared.deb
EOF
    exit 1
fi

TMPDIR="${TMPDIR:-/tmp}"
API_LOG="$TMPDIR/cf-api-tunnel.log"
STATIC_LOG="$TMPDIR/cf-static-tunnel.log"

echo "[tunnel] API    : localhost:${API_PORT} → cloudflared (私有)"
echo "[tunnel] Static : localhost:${STATIC_PORT} → cloudflared (公开)"
echo ""

# 起两条隧道，后台跑
cloudflared tunnel --url "http://localhost:${API_PORT}" \
    > "$API_LOG" 2>&1 &
API_PID=$!

cloudflared tunnel --url "http://localhost:${STATIC_PORT}" \
    > "$STATIC_LOG" 2>&1 &
STATIC_PID=$!

# 等域名出现（cloudflared 启动约 3-6s）
for i in {1..15}; do
    sleep 1
    API_DOMAIN=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$API_LOG" 2>/dev/null | head -1 || true)
    STATIC_DOMAIN=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$STATIC_LOG" 2>/dev/null | head -1 || true)
    if [ -n "$API_DOMAIN" ] && [ -n "$STATIC_DOMAIN" ]; then
        echo ""
        echo "═══ 隧道就绪 ═══"
        echo "[api]    $API_DOMAIN  （私有，需要 ?key=）"
        echo "[static] $STATIC_DOMAIN  （公开）"
        echo ""
        echo "浏览器访问:"
        echo "  ${STATIC_DOMAIN}/?api=${API_DOMAIN}&key=<console-key>"
        echo "═══════════════"
        echo ""
        echo "PID: api=$API_PID static=$STATIC_PID  日志: $API_LOG / $STATIC_LOG"
        echo "停止: kill $API_PID $STATIC_PID"
        # 等任意一个退出
        wait $API_PID $STATIC_PID 2>/dev/null || true
        exit 0
    fi
done

echo "[tunnel] 超时：未能获取域名，杀掉后台进程"
kill $API_PID $STATIC_PID 2>/dev/null || true
echo "日志: $API_LOG / $STATIC_LOG"
exit 1
