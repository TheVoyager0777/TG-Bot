#!/usr/bin/env bash
# netns-direct.sh —— 建一个直连物理出口的 network namespace（绕开 FlClash TUN/fake-ip）。
#
# 背景：FlClash 用 TUN + policy routing + fake-ip DNS 全局劫持出站，cloudflared 连
# Cloudflare edge(region*.v2.argotunnel.com) 被解析成 198.18.x.x 假地址并灌进 TUN，
# TLS 握手超时 → 隧道反复挂。NO_PROXY 无效（不是 SOCKS/HTTP 代理，是路由层劫持）。
#
# 方案：netns 'direct' 内只有 veth + 物理网关默认路由 + 公共 DNS，完全不碰 FlClash。
# cloudflared 用 `ip netns exec direct` 启动即直连。本脚本幂等：可重复执行。
#
# 用法：
#   sudo ./netns-direct.sh up      # 建/修 netns（开机或 FlClash 重启后跑一次）
#   sudo ./netns-direct.sh down     # 拆掉
#   sudo ./netns-direct.sh status   # 查看状态 + 连通性自检
set -euo pipefail

NS="direct"
VETH_HOST="veth-d0"          # 主 namespace 侧
VETH_NS="veth-d1"            # netns 侧
HOST_IP="10.200.0.1"
NS_IP="10.200.0.2"
SUBNET="10.200.0.0/24"
PHYS_IF="${PHYS_IF:-ens33}"   # 物理网卡
DNS1="1.1.1.1"
DNS2="8.8.8.8"
# 物理直连出口的实测路径 MTU（payload 1452+28=1480；ISP 路径 <1500）。
# veth 设此值 + TCP MSS 钳制到 MTU-40，避免大包黑洞（控制面小包能过、HTTP 数据面卡死）。
LINK_MTU="${CF_LINK_MTU:-1480}"
TCP_MSS="${CF_TCP_MSS:-1440}"

need_root() { [[ $EUID -eq 0 ]] || { echo "需要 root：sudo $0 $*"; exit 1; }; }

up() {
  need_root "$@"
  # 1) netns
  ip netns add "$NS" 2>/dev/null || true

  # 2) veth pair（已存在则先删重建，保证干净）
  ip link del "$VETH_HOST" 2>/dev/null || true
  ip link add "$VETH_HOST" type veth peer name "$VETH_NS"
  ip link set "$VETH_NS" netns "$NS"

  # 3) 主侧 veth 配 IP + MTU + 起
  ip addr add "${HOST_IP}/24" dev "$VETH_HOST" 2>/dev/null || true
  ip link set "$VETH_HOST" mtu "$LINK_MTU"
  ip link set "$VETH_HOST" up

  # 4) netns 内：lo + veth + MTU + 默认路由经主侧 veth
  ip netns exec "$NS" ip addr add "${NS_IP}/24" dev "$VETH_NS"
  ip netns exec "$NS" ip link set "$VETH_NS" mtu "$LINK_MTU"
  ip netns exec "$NS" ip link set "$VETH_NS" up
  ip netns exec "$NS" ip link set lo up
  ip netns exec "$NS" ip route add default via "$HOST_IP"

  # 4b) netns 内 OUTPUT 链 MSS 钳制：cloudflared→Cloudflare edge 是 netns 本地发起的
  #     连接（走 OUTPUT，不经主机 FORWARD），主机侧 FORWARD 钳制管不到它。响应经隧道
  #     回 edge 的大包会黑洞 → 表现为 cloudflared "context canceled" / 隧道 530。
  #     在 netns 内对本地出站 SYN 直接钳 MSS，根治回程黑洞。
  ip netns exec "$NS" iptables -t mangle -D OUTPUT -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss "$TCP_MSS" 2>/dev/null || true
  ip netns exec "$NS" iptables -t mangle -A OUTPUT -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss "$TCP_MSS"

  # 5) netns 内 DNS（真实公共 DNS，绕开 FlClash fake-ip）
  mkdir -p "/etc/netns/$NS"
  printf "nameserver %s\nnameserver %s\n" "$DNS1" "$DNS2" > "/etc/netns/$NS/resolv.conf"

  # 6) NAT：netns 流量经物理网卡出去（MASQUERADE）。
  #    关键：FORWARD 链放行 + 物理网卡侧 SNAT，使 10.200.0.2 的包从 ens33 真实出口走。
  sysctl -wq net.ipv4.ip_forward=1
  # 幂等加规则（先删再加，避免重复）
  iptables -t nat -D POSTROUTING -s "$SUBNET" -o "$PHYS_IF" -j MASQUERADE 2>/dev/null || true
  iptables -t nat -A POSTROUTING -s "$SUBNET" -o "$PHYS_IF" -j MASQUERADE
  iptables -D FORWARD -i "$VETH_HOST" -o "$PHYS_IF" -j ACCEPT 2>/dev/null || true
  iptables -A FORWARD -i "$VETH_HOST" -o "$PHYS_IF" -j ACCEPT
  iptables -D FORWARD -i "$PHYS_IF" -o "$VETH_HOST" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  iptables -A FORWARD -i "$PHYS_IF" -o "$VETH_HOST" -m state --state RELATED,ESTABLISHED -j ACCEPT

  # 6b) TCP MSS 钳制：物理直连出口路径 MTU=1480（<1500），PMTU 发现常被 ICMP 屏蔽打断 →
  #     大包黑洞（控制面小包能过、HTTP 响应数据面卡死，表现为 cloudflared 隧道 530/超时）。
  #     钳到 MTU-40，让 TCP 握手就协商出能过的段大小。双向都加。
  iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -s "$SUBNET" -j TCPMSS --set-mss "$TCP_MSS" 2>/dev/null || true
  iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -s "$SUBNET" -j TCPMSS --set-mss "$TCP_MSS"
  iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -d "$SUBNET" -j TCPMSS --set-mss "$TCP_MSS" 2>/dev/null || true
  iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -d "$SUBNET" -j TCPMSS --set-mss "$TCP_MSS"

  # 7) 关键：主侧 veth 来源的流量必须走 main 表（物理出口），不能进 FlClash 表 2022。
  #    FlClash 的 ip rule 9002 'not from all iif lo lookup 2022' 会把 veth 转发的包
  #    （iif=veth-d0，非 lo）导进 2022 → 又回 FlClash。加一条高优先级规则放行本子网。
  ip rule del from "$SUBNET" lookup main 2>/dev/null || true
  ip rule add from "$SUBNET" lookup main priority 8000

  echo "✅ netns '$NS' 就绪：${NS_IP} → ${HOST_IP} → ${PHYS_IF} 直连出口"
}

down() {
  need_root "$@"
  ip netns del "$NS" 2>/dev/null || true
  ip link del "$VETH_HOST" 2>/dev/null || true
  iptables -t nat -D POSTROUTING -s "$SUBNET" -o "$PHYS_IF" -j MASQUERADE 2>/dev/null || true
  iptables -D FORWARD -i "$VETH_HOST" -o "$PHYS_IF" -j ACCEPT 2>/dev/null || true
  iptables -D FORWARD -i "$PHYS_IF" -o "$VETH_HOST" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
  iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -s "$SUBNET" -j TCPMSS --set-mss "$TCP_MSS" 2>/dev/null || true
  iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -d "$SUBNET" -j TCPMSS --set-mss "$TCP_MSS" 2>/dev/null || true
  ip rule del from "$SUBNET" lookup main 2>/dev/null || true
  rm -rf "/etc/netns/$NS"
  echo "🗑  netns '$NS' 已拆除"
}

status() {
  echo "=== netns 列表 ==="
  ip netns list 2>/dev/null | grep "$NS" || echo "(无 $NS)"
  echo ""
  echo "=== netns 内路由 ==="
  ip netns exec "$NS" ip route 2>/dev/null || echo "(netns 不存在)"
  echo ""
  echo "=== 连通性自检（绕过 FlClash 直连）==="
  if ip netns exec "$NS" true 2>/dev/null; then
    echo -n "DNS 解析 region1.v2.argotunnel.com: "
    ip netns exec "$NS" getent hosts region1.v2.argotunnel.com 2>/dev/null | awk '{print $1}' | head -1 || echo "失败"
    echo -n "HTTPS 到 cloudflare.com: "
    ip netns exec "$NS" curl -sI --max-time 8 https://www.cloudflare.com 2>/dev/null | head -1 || echo "失败"
  else
    echo "(netns 不存在，先跑 up)"
  fi
}

case "${1:-}" in
  up) up "$@";;
  down) down "$@";;
  status) status;;
  *) echo "用法: sudo $0 {up|down|status}"; exit 1;;
esac
