// Offline support. The whole point of this app is being opened in a car park
// with one bar of signal, so nothing here should need the network to render.
//
// Three strategies, chosen by what the thing is:
//   shell   cache-first, refreshed in the background - it changes rarely
//   data    network-first, falling back to cache - you want today's forecast
//           if it is reachable and yesterday's if it is not
//   tiles   cache-first with a cap - map tiles never change and are the
//           slowest thing on a bad connection

// Bump this whenever ANY file in SHELL_URLS changes. The shell is served
// cache-first, so a stale entry is what a returning phone sees; a new
// VERSION forces a fresh install, a fresh precache and a cache purge.
const VERSION = "v25";
const SHELL = `shell-${VERSION}`;
const DATA = `data-${VERSION}`;
const TILES = "tiles";
const FONTS = "fonts";          // versionless: font files are immutable
const TILE_CAP = 900;

const SHELL_URLS = [
  "./",
  "./index.html",
  "./anywhere.js",
  "./structure.js",
  "./region-species.json",
  "./add-region.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
  "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
];

// The cross-origin entries of SHELL_URLS - Leaflet from unpkg. They are
// precached on install, so they MUST still be served from cache when the
// network is gone, which means they need a branch of their own above the
// cross-origin bail-out below. Without one the bail-out would hand them
// straight to the browser and the app would not render offline at all:
// no Leaflet, no map.
const PRECACHED_REMOTE = new Set(SHELL_URLS.filter(u => /^https?:/i.test(u)));

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(SHELL)
      // Individually, so one unreachable CDN asset does not fail the install.
      .then(c => Promise.all(SHELL_URLS.map(u =>
        c.add(u).catch(() => console.warn("sw: could not precache", u)))))
      .then(() => self.skipWaiting())
  );
});

// NOTE BEFORE YOU TIDY THIS. The filter keeps only the four caches named
// here, which means it also deletes structure.js's bathy-v1 and osm-v1 on
// every version bump. That is wasteful - they are static, and refetching
// them hammers the volunteer Overpass mirrors - so it looks like something
// to fix with a keep-list. It is, BUT NOT YET: that deletion is currently
// the only thing clearing the GMRT grids poisoned by the bug described in
// the fetch handler below. A keep-list must ship in the same change as a
// one-shot `caches.delete("bathy-v1")`, or it will preserve wrong seabed
// data on every device that ever ran a scan.
self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys
        .filter(k => k !== SHELL && k !== DATA && k !== TILES && k !== FONTS)
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

  // --- webfont: cache-first, and kept across app updates ---
  // The page loads Nunito Sans non-blocking with font-display:swap, so the
  // first visit paints in the system fallback and never waits on the
  // network. After that this serves it from cache, which is what keeps the
  // car-park-with-one-bar case honest. Not in SHELL_URLS because the CSS
  // response varies by user agent and the font file URL inside it is not
  // knowable ahead of time; caching whatever the browser actually asks for
  // is both simpler and correct.
  if (/fonts\.googleapis\.com|fonts\.gstatic\.com/.test(url.hostname)) {
    e.respondWith((async () => {
      const c = await caches.open(FONTS);
      const hit = await c.match(req);
      if (hit) return hit;
      try {
        const res = await fetch(req);
        if (res.ok) c.put(req, res.clone());
        return res;
      } catch (err) {
        // No font, no problem: the page falls back to the system stack.
        return hit || Response.error();
      }
    })());
    return;
  }

  // --- map tiles: cache-first, they are immutable ---
  if (/basemaps\.cartocdn\.com|tile\.openstreetmap\.org|tiles\.openseamap\.org/.test(url.hostname)) {
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

  // --- precached remote assets (Leaflet): cache-first, matched EXACTLY ---
  if (PRECACHED_REMOTE.has(url.href)) {
    e.respondWith((async () => {
      const c = await caches.open(SHELL);
      const hit = await c.match(req);      // exact: these carry a version in the path
      if (hit) return hit;
      try {
        const res = await fetch(req);
        if (res.ok) c.put(req, res.clone());
        return res;
      } catch (err) {
        return Response.error();
      }
    })());
    return;
  }

  // --- ANYTHING ELSE CROSS-ORIGIN: do not touch it ---
  //
  // THE VENICE BUG. Open-Meteo (marine and forecast) and GMRT's GridServer
  // are cross-origin GETs to a FIXED path whose coordinates live entirely in
  // the query string. With no bail-out
  // here they fell through to the app-shell branch at the bottom, which is
  // cache-first and matched with { ignoreSearch: true } - and ignoreSearch
  // throws the query away. So the first point forecast anyone fetched was
  // cached under the bare path, and every later point on Earth matched it.
  //
  // Reproduced in a browser against the live site: a request for Sydney
  // (-33.90, 151.30) came back with latitude 45.375, longitude 12.29. Venice.
  // It survived reloads because it lived in the shell cache, which is exactly
  // why it looked like a location that had been "saved".
  //
  // GMRT was the worse victim, because the damage outlived the request.
  // structure.js writes what it gets back into its own bathy-v1 cache under
  // the CORRECT key and never refetches ("Static data: once fetched for an
  // area, never fetch it again"), so a poisoned scan wrote one coast's seabed
  // in under every other coast's name, permanently.
  //
  // Overpass is NOT affected: it is a POST and exits at the method check
  // above. There is no browser-side flood API - that is server-side Python.
  //
  // Fonts and tiles are cross-origin too, which is why this sits after them:
  // they are immutable, they are matched exactly, and they have their own
  // branches above. Everything else cross-origin is an API call. Returning
  // without calling respondWith hands it to the browser untouched, which is
  // the only correct answer for a request whose meaning is in its query.
  if (url.origin !== self.location.origin) return;

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

  // --- region pages: a forecast, so never serve a stale one by choice ---
  // These carry today's verdict and visibility as text. Under the app-shell
  // rule below they are cache-first, which would hand a repeat visitor a
  // verdict several hours old with nothing saying so. Same treatment as
  // /data/: current if the network answers, last known if it does not.
  if (url.pathname.includes("/r/")) {
    e.respondWith((async () => {
      const c = await caches.open(DATA);
      try {
        const res = await fetch(req);
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
    // ignoreSearch ONLY for navigations. A page opened as "/?utm=..." is
    // still the shell, but "structure.js?v=2" is not "structure.js?v=1", and
    // treating them as interchangeable is the same mistake that cached one
    // point forecast for the whole planet.
    const hit = await c.match(req, { ignoreSearch: req.mode === "navigate" });
    const net = fetch(req).then(res => {
      if (res.ok) c.put(req, res.clone());
      return res;
    }).catch(() => null);
    return hit || (await net) || Response.error();
  })());
});
