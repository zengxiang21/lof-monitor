/* sw.js — LOF 面板 Service Worker
 * 策略：
 *  - 静态资源（页面/图标/manifest）：cache-first，后台更新
 *  - /api 读接口 + /data 静态 JSON：network-first，失败时回退缓存（弱网/离线也能看到上次数据）
 *  - POST /api/collect：强制走网络，不缓存
 */
const VERSION = "v11";
const SHELL = [
  "/",
  "/index.html",
  "/calibration.html",
  "/manifest.webmanifest",
  "/icon-192.png",
  "/icon-512.png",
  "/icon.svg",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE()).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith("lof-") && k !== SHELL_CACHE())
        .map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function SHELL_CACHE() { return "lof-shell-" + VERSION; }
function API_CACHE() { return "lof-api-" + VERSION; }

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;                    // POST /api/collect 直接透传
  const url = new URL(req.url);

  // /api 读接口：network-first + 缓存回退
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(API_CACHE()).then((c) => c.put(req, copy));
          return res;
        })
        .catch(async () => {
          const hit = await caches.match(req);
          if (hit) return hit;
          return new Response(JSON.stringify({ error: "网络不可用，稍后自动重试" }),
            { status: 200, headers: { "Content-Type": "application/json" } });
        })
    );
    return;
  }

  // /data 静态 JSON（GitHub Pages 数据源）：network-first + 缓存回退
  // （前端带 ?t= 时间戳防缓存，回退匹配时忽略查询参数）
  if (url.pathname.startsWith("/data/")) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(API_CACHE()).then((c) => c.put(req, copy));
          return res;
        })
        .catch(async () => {
          const cache = await caches.open(API_CACHE());
          const keys = await cache.keys();
          const hit = keys.find((k) => new URL(k.url).pathname === url.pathname);
          if (hit) return cache.match(hit);
          return new Response(JSON.stringify({ error: "离线且无缓存数据，稍后自动重试" }),
            { status: 200, headers: { "Content-Type": "application/json" } });
        })
    );
    return;
  }

  // 同源静态资源：cache-first + 后台更新
  if (url.origin === self.location.origin) {
    e.respondWith(
      caches.match(req).then((hit) => {
        const network = fetch(req).then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(SHELL_CACHE()).then((c) => c.put(req, copy));
          }
          return res;
        }).catch(() => hit);
        return hit || network;
      })
    );
  }
});
