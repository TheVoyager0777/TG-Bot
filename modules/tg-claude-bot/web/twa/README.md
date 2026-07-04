# Phantom Console —— 打包成独立 App（Tier 3）

把已经 PWA 化的 Phantom Console 封装成**真正可安装的原生 app**。两条路：Android 用
TWA（推荐，手机远程操控场景），桌面用 Tauri（可选）。

## 0. 共同前提：固定 https 域名

PWA 安装 / TWA 都把域名烧进 app，并靠 `assetlinks.json` 校验归属。
**`trycloudflare` 每次重启随机域名 → 不能用**。先把隧道升级为固定域名（任选其一）：

- Cloudflare Named Tunnel + 自有域名（`cloudflared tunnel create` + DNS route）
- Cloudflare Tunnel + Cloudflare Access（加 Email OTP / SSO，公网更安全）
- Tailscale Funnel（固定 `*.ts.net` 域名）

部署后确认这些都可访问：
```
https://<域名>/                       # app 壳
https://<域名>/manifest.webmanifest   # PWA manifest
https://<域名>/sw.js                   # service worker
https://<域名>/icons/icon-512.png      # 图标
```

> 仅"装到手机主屏当 PWA 用"的话，到这一步就够了：手机 Chrome/Safari 打开 →
> 菜单"添加到主屏幕"，即得独立窗口 + 图标 + 离线开壳。下面是进一步打成 APK。

## 1. Android APK（TWA · Bubblewrap）

### 最简：PWABuilder（网页，零本地环境）
1. 打开 https://www.pwabuilder.com ，输入 `https://<域名>`
2. 选 Android → Generate Package → 下载 APK/AAB + `assetlinks.json`
3. 按第 3 节部署 `assetlinks.json`

### 本地/CI：Bubblewrap（本目录脚本）
需 Node 16+ 与 JDK 17（Android SDK 由 bubblewrap 自动下载）。
```bash
cd web/twa
./build-apk.sh <你的域名>
```
脚本做了：装 `@bubblewrap/cli` → `doctor` 自检 → 用 `twa-manifest.template.json`
生成定制配置（替换 `__HOST__`）→ `init`/`build` → 输出 `build/app-release-signed.apk`。

## 2. Digital Asset Links（不做的话 APK 顶部会有地址栏）

1. 取签名指纹：`bubblewrap fingerprint list`（或 PWABuilder 下载包里给的 SHA256）
2. 复制 `assetlinks.template.json` → 仓库的 `web/.well-known/assetlinks.json`，
   把 `__SHA256_FINGERPRINT__` 换成上一步的指纹
3. 服务端已内置路由 `/.well-known/assetlinks.json`（见 `web/server.py`），
   确认 `https://<域名>/.well-known/assetlinks.json` 返回正确 JSON
4. 重装 APK → Chrome 校验通过 → 全屏无地址栏，真·独立 app

## 3. 桌面（可选 · Tauri）

桌面想要独立窗口 app，最轻是 Tauri 包一层 WebView 指向固定域名：
```bash
npm create tauri-app@latest phantom-desktop
# tauri.conf.json: build.devUrl / app.windows[0].url = "https://<域名>/?app=phantom"
# 窗口 decorations:false + 自绘标题栏可更贴 Zune
npm run tauri build
```
产物：`.AppImage`/`.deb`/`.dmg`/`.msi`。

## 文件清单
- `twa-manifest.template.json` —— Bubblewrap 配置模板（`__HOST__` 占位）
- `assetlinks.template.json` —— Digital Asset Links 模板（`__SHA256_FINGERPRINT__` 占位）
- `build-apk.sh` —— 一键引导构建脚本
- `build/` —— 构建产物（git 忽略，运行时生成）
