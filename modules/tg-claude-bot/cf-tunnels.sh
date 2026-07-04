#!/usr/bin/env bash
# cf-tunnels.sh —— 持久双隧道守护（供 systemd 调用）
# 把 cloudflared 域名写进 ~/.config/tg-cf-tunnels.json，bot 启动时自动读取。
#
# 关键：cloudflared 跑在 'direct' network namespace 内（见 netns-direct.sh），
# 绕开 FlClash TUN + fake-ip DNS 劫持——否则连 Cloudflare edge 会被解析成
# 198.18.x.x 假地址、TLS 握手超时、隧道反复挂。netns 内：
#   · 真实公共 DNS（拿到真 edge IP）
#   · 默认路由经 veth → 物理网卡直连出口
#   · 回连 bot web server 不能用 localhost（netns 隔离），改用主侧 veth IP 10.200.0.1
set -euo pipefail

API_PORT="${API_PORT:-8788}"
STATIC_PORT="${STATIC_PORT:-8789}"
STATE_FILE="${STATE_FILE:-$HOME/.config/tg-cf-tunnels.json}"

# netns 配置（与 netns-direct.sh 对齐）
NS="${CF_NETNS:-direct}"
HOST_VETH_IP="${CF_HOST_VETH_IP:-10.200.0.1}"   # netns 内回连 bot web server 用
NETNS_DIR_SCRIPT="$(dirname "$(readlink -f "$0")")/netns-direct.sh"

mkdir -p "$(dirname "$STATE_FILE")"

if ! command -v cloudflared >/dev/null 2>&1; then
    echo '{"error":"cloudflared not installed"}' > "$STATE_FILE"
    exit 1
fi
# 绝对路径：sudo ip netns exec 下 root 的 PATH 不含 ~/.local/bin，必须用全路径
CLOUDFLARED="$(command -v cloudflared)"

# ── 确保 netns 就绪（幂等；FlClash/网络重启后自愈）──
# 用 sudo 非交互；需在 sudoers 配免密或预先 up 好。失败则退回直跑（仍可能被代理打挂）。
NS_READY=0
if ip netns list 2>/dev/null | grep -q "\b${NS}\b"; then
    NS_READY=1
elif [ -x "$NETNS_DIR_SCRIPT" ]; then
    if sudo -n "$NETNS_DIR_SCRIPT" up >/dev/null 2>&1; then
        NS_READY=1
    fi
fi

# 在 netns 内跑命令的包装：就绪则 netns exec，否则裸跑（降级）
run_cf() {
    if [ "$NS_READY" = "1" ]; then
        sudo -n ip netns exec "$NS" "$@"
    else
        "$@"
    fi
}

# netns 内回连 bot web server 的主机地址（netns 外 localhost 即可）
if [ "$NS_READY" = "1" ]; then
    API_HOST="${HOST_VETH_IP}"
    echo "cf-tunnels: 使用 netns '${NS}'（绕过 FlClash），回连 ${API_HOST}"
else
    API_HOST="localhost"
    echo "cf-tunnels: ⚠ netns 未就绪，裸跑（可能被 FlClash 打挂）"
fi

# ── 隧道守护函数：在错误时自动重建 ──────────────────────────────────────────
# trycloudflare 免费隧道 ~4-24h 后被 Cloudflare 回收（Unauthorized: Tunnel not found）。
# cloudflared 陷入无限重试——必须杀掉重建才能拿到新域名。
# 本函数把域名提取 + 错误重启逻辑收敛在一起, 带 restart 的 while 循环。
run_tunnel() {
    local label="$1" port="$2" key="$3"  # key = api_url | static_url
    local host="${API_HOST}"
    while true; do
        local cf_pid
        # shellcheck disable=SC2086
        run_cf "$CLOUDFLARED" tunnel --protocol http2 --no-autoupdate \
            --http-host-header localhost --url "http://${host}:${port}" 2>&1 | \
        while IFS= read -r line; do
            echo "[$label] $line"
            # 域名提取
            domain=$(echo "$line" | grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1 || true)
            if [ -n "$domain" ]; then
                tmp="${STATE_FILE}.tmp.$$"
                python3 -c "
import json,os
try: data=json.load(open('$STATE_FILE'))
except: data={}
data['$key']='$domain'
json.dump(data,open('$tmp','w'),indent=2)
os.replace('$tmp','$STATE_FILE')
"
            fi
            # 致命错误检测: 隧道被回收→必须重建
            if echo "$line" | grep -qE "Unauthorized: Tunnel not found|Tunnel.*deleted|connection.*refused.*trycloudflare"; then
                echo "[$label] FATAL: tunnel expired — restarting cloudflared" >&2
                # 找到本 label 的 cloudflared 进程并杀掉（跳出自循环重建）
                pkill -f "cloudflared tunnel.*${host}:${port}" 2>/dev/null || true
                break
            fi
        done
        sleep 2  # 避免死循环刷爆日志
    done
}

# 并行起两条隧道守护
run_tunnel "api"    "$API_PORT"    "api_url"    &
API_PID=$!
run_tunnel "static" "$STATIC_PORT" "static_url" &
STATIC_PID=$!

echo "cf-tunnels: api_pid=$API_PID static_pid=$STATIC_PID"
echo "state file: $STATE_FILE"

wait $API_PID $STATIC_PID || true
