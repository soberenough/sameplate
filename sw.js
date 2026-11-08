// SamePlate beta service worker.
// - Pages (index.html): network first, so a new build shows up; the cached copy is the offline fallback.
// - Hashed assets, icons, recipe photos: cache first (photos are added as they're viewed, plus this week's
//   dinners, which the app sends with a "warm" message so the week's photos work offline).
// - A photo that can't be fetched fails as an error (the app then draws its placeholder); it never gets HTML back.
const CACHE = 'sameplate-beta-v0.1.3';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon-192.png', './icon-512.png', './qr.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

function put(req, res) {
  if (res && res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {}); }
  return res;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (req.mode === 'navigate' || url.pathname.endsWith('/index.html') || url.pathname.endsWith('/')) {
    e.respondWith(fetch(req).then((res) => put(req, res)).catch(() => caches.match(req).then((hit) => hit || caches.match('./index.html'))));
    return;
  }
  e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => put(req, res)).catch(() => Response.error())));
});

self.addEventListener('message', (e) => {
  const d = e.data || {};
  if (d.type !== 'warm' || !Array.isArray(d.urls)) return;
  e.waitUntil(caches.open(CACHE).then(async (c) => {
    for (const u of d.urls.slice(0, 40)) {
      if (!(await c.match(u))) { try { const res = await fetch(u); if (res.ok) await c.put(u, res); } catch { /* offline: try next time */ } }
    }
  }));
});
