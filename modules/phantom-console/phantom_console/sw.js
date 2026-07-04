/* sw.js — Phantom Console Service Worker
 * HTML 入口必须网络优先：它包含运行时 API_BASE，缓存旧壳会导致 LAN/隧道偶发连不上。
 * 稳定静态资源走 stale-while-revalidate；
 * /api/* 与跨域（CDN、api 隧道）一律 network-only，绝不缓存动态/鉴权数据。
 * 改了壳文件清单或缓存策略时，提升 CACHE 版本号即可触发更新清理。
 */
const CACHE = 'phantom-shell-v45';
const SHELL = [
  '/css/console.css',
  '/js/bundle.js',
  '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon-maskable-512.png',
  '/manifest.webmanifest'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      // 单个壳文件 404 不应让整个 install 失败 → 逐个 add，失败忽略
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (e) => {
  if (e.data === 'skipWaiting') self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  let url;
  try { url = new URL(req.url); } catch (_) { return; }
  // 只接管【同源 GET 壳资源】；其余（POST、跨域 CDN/api 隧道、/api/* 长轮询）全部放行走网络。
  if (req.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) {
    return;
  }
  if (req.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html')) {
    e.respondWith(fetch(req, { cache: 'no-store' }).catch(() => caches.match(req)));
    return;
  }
  // stale-while-revalidate：先回缓存（秒开/离线），后台拉新写回缓存。
  e.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((resp) => {
        if (resp && resp.status === 200 && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return resp;
      }).catch(() => cached);
      return cached || network;
    })
  );
});
