// Offline support. The whole point of this app is being opened in a car park
// with one bar of signal, so nothing here should need the network to render.
//
// Three strategies, chosen by what the thing is:
//   shell   cache-first, refreshed in the background - it changes rarely
//   data    network-first, falling back to cache - you want today's forecast
//           if it is reachable and yesterday's if it is not
//   tiles   cache-first with a cap - map tiles never change and are the
//           slowest thing on a bad connection

const VERSION = "v16";
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;
const TILES = "tiles";
const TILE_CAP = 900;

const SHELL_URLS = [
  "./",
  "./index.html",
  "./anywhere.js",
  "./structure.js",
  "./region-species.json",
  "./add-region.html",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(SHELL)
      // Individually, so one unreachable CDN asset does not fail the install.
      .then(c => Promise.all(SHELL_URLS.map(u =>
        c.add(u).catch(() => console.warn("sw: could not precache", u)))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(k => k !== SHELL && k !== DATA && k !== TILES)
        .map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function trimTiles() {
  const c = await caches.open(TILES);
  const keys = await c.keys();
  if (keys.length > TILE_CAP) {
    // Oldest first; the cache preserves insertion order.
    await Promise.all(keys.slice(0, keys.length - TILE_CAP).map(k => c.delete(k)));
  }
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);

  // --- map tiles: cache-first, they are immutable ---
  if (/tile\.openstreetmap\.org|tiles\.openseamap\.org/.test(url.hostname)) {
    e.respondWith((async () => {
      const c = await caches.open(TILES);
      const hit = await c.match(req);
      if (hit) return hit;
      try {
        const res = await fetch(req);
        if (res.ok) { c.put(req, res.clone()); trimTiles(); }
        return res;
      } catch (err) {
        return hit || Response.error();
      }
    })());
    return;
  }

  // --- forecast data: fresh if we can, yesterday's if we cannot ---
  if (url.pathname.includes("/data/")) {
    e.respondWith((async () => {
      const c = await caches.open(DATA);
      try {
        const res = await fetch(req, { cache: "no-store" });
        if (res.ok) c.put(req, res.clone());
        return res;
      } catch (err) {
        const hit = await c.match(req);
        if (hit) return hit;
        throw err;
      }
    })());
    return;
  }

  // --- app shell: cache-first, updated quietly in the background ---
  e.respondWith((async () => {
    const c = await caches.open(SHELL);
    const hit = await c.match(req, { ignoreSearch: true });
    const net = fetch(req).then(res => {
      if (res.ok) c.put(req, res.clone());
      return res;
    }).catch(() => null);
    return hit || (await net) || Response.error();
  })());
});
