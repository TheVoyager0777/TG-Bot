#!/usr/bin/env bash
# build-apk.sh —— 用 Bubblewrap 把 Phantom Console PWA 打成 Android APK（TWA）。
#
# 前提（重要）：
#   1) PWA 必须已部署在一个【固定 https 域名】上（trycloudflare 的随机临时域名不行，
#      因为 TWA 把域名烧进 APK + 要靠 assetlinks 校验归属）。先把隧道升级成固定域名。
#   2) 本机需 Node 16+ 与 JDK 17。Android SDK 由 bubblewrap 自动下载（首次较慢）。
#
# 用法：
#   ./build-apk.sh your-domain.com
#   PHANTOM_HOST=your-domain.com ./build-apk.sh
set -euo pipefail

HOST="${1:-${PHANTOM_HOST:-}}"
if [ -z "$HOST" ]; then
  echo "用法: ./build-apk.sh <your-domain.com>   (PWA 已部署在该 https 域名)"
  exit 1
fi
HOST="${HOST#https://}"; HOST="${HOST#http://}"; HOST="${HOST%/}"

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/build"
mkdir -p "$OUT"

if ! command -v bubblewrap >/dev/null 2>&1; then
  echo "[twa] 未找到 bubblewrap，安装 @bubblewrap/cli ..."
  npm i -g @bubblewrap/cli
fi

echo "[twa] 环境自检（JDK / Android SDK）..."
bubblewrap doctor || {
  echo "[twa] bubblewrap doctor 未通过：装好 JDK 17 后重试（Android SDK 它会自动拉）。"
  exit 1
}

# 用模板生成定制 twa-manifest.json（替换 __HOST__）
sed "s/__HOST__/$HOST/g" "$HERE/twa-manifest.template.json" > "$OUT/twa-manifest.json"
echo "[twa] 写入 $OUT/twa-manifest.json (host=$HOST)"

cd "$OUT"
# 首次：从线上 manifest 初始化工程（交互式，按提示走；签名密钥可让它新建）
if [ ! -f "$OUT/app/build.gradle" ] && [ ! -d "$OUT/app" ]; then
  echo "[twa] 初始化 TWA 工程（首次，交互式）..."
  bubblewrap init --manifest "https://$HOST/manifest.webmanifest" --directory "$OUT"
else
  echo "[twa] 已有工程，update 同步 twa-manifest.json ..."
  bubblewrap update --directory "$OUT" || true
fi

echo "[twa] 构建 APK ..."
bubblewrap build --directory "$OUT"

echo ""
echo "═══════════════ APK 就绪 ═══════════════"
echo "产物: $OUT/app-release-signed.apk"
echo ""
echo "最后一步（否则 APK 顶部会显示浏览器地址栏）："
echo "  1) 取签名指纹:   bubblewrap fingerprint list"
echo "  2) 把 SHA256 填进 ../.well-known/assetlinks.json （照 assetlinks.template.json）"
echo "  3) 确认可访问:   https://$HOST/.well-known/assetlinks.json"
echo "  4) 重新安装 APK，Chrome 校验通过后即全屏无地址栏（真·独立 app）"
echo "════════════════════════════════════════"
