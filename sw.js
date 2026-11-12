// SamePlate beta service worker.
// - Pages (index.html): network first, so a new build shows up; the cached copy is the offline fallback.
// - Hashed assets, icons, recipe photos: cache first (photos are added as they're viewed, plus this week's
//   dinners, which the app sends with a "warm" message so the week's photos work offline).
// - A photo that can't be fetched fails as an error (the app then draws its placeholder); it never gets HTML back.
const CACHE = 'sameplate-beta-v0.1.4';
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
  if (url.pathname.includes('/api/') || url.pathname.endsWith('/healthz')) return; // live data: never cached
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

// Reminders sent by the SamePlate server (only when the app was built with a server; see src/api.ts).
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data ? e.data.text() : '' }; }
  e.waitUntil(self.registration.showNotification(d.title || 'SamePlate', {
    body: d.body || '', tag: d.tag || 'sameplate', icon: './icon-192.png', badge: './icon-192.png', data: { url: d.url || './' },
  }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || './', self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const c of list) if (c.url.startsWith(self.registration.scope) && 'focus' in c) return c.focus();
    return self.clients.openWindow ? self.clients.openWindow(target) : undefined;
  }));
});
