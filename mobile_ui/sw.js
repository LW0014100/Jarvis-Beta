// JARVIS Mobile Service Worker - Offline & Standalone Cache
const CACHE_NAME = "jarvis-mobile-v1.0.3";
const ASSETS_TO_CACHE = [
  "./",
  "index.html",
  "mobile.css",
  "jsqr.js",
  "mobile.js",
  "manifest.json",
  "/mobile",
  "/mobile/",
  "/mobile/index.html",
  "/mobile/mobile.css",
  "/mobile/jsqr.js",
  "/mobile/mobile.js",
  "/mobile/manifest.json"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return Promise.allSettled(
        ASSETS_TO_CACHE.map((asset) => cache.add(asset))
      );
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API-Anfragen immer live über das Netzwerk leiten
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(event.request).catch(() => {
        return new Response(JSON.stringify({ ok: false, error: "Network offline" }), {
          headers: { "Content-Type": "application/json" },
          status: 503
        });
      })
    );
    return;
  }

  // Für App-Assets: Stale-While-Revalidate Strategie
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      const fetchPromise = fetch(event.request).then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const responseToCache = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return networkResponse;
      }).catch(() => cachedResponse);

      return cachedResponse || fetchPromise;
    })
  );
});
