# v24 — what the audit found after v23

```
v24/
└── docs/
    ├── sw.js          <- supersedes v23
    └── index.html     <- supersedes v22
```

Everything else unchanged: manifest and icons v22; `fish_finder.py`,
`config.json`, `README.md` v20; `s2_turbidity.py`, `dive_log.csv`, workflow
v19; `regions.json` v17.

**v23 already fixes Venice.** This corrects things the audit turned up
afterwards, including one bug that is arguably worse.

---

## GMRT was poisoned too, and that damage outlived the request

Open-Meteo was not the only victim. `structure.js`:

```js
const GMRT = "https://www.gmrt.org/services/GridServer";
const url  = `${GMRT}?${q}`;      // the whole bbox lives in the query
res = await fetch(url);           // bare cross-origin GET, fixed path
```

Same shape, same poisoning. But then:

```js
// Static data: once fetched for an area, never fetch it again.
if(cache) cache.put(url, new Response(text));
```

The wrong grid was written into `bathy-v1` **under the correct key**, and
that cache is never revalidated. So "Scan structure" on a new coast returned
the first coast's seabed, and it stayed wrong.

**Already handled, by luck rather than design.** `activate` deletes every
cache except SHELL/DATA/TILES/FONTS, so the v23 version bump purges
`bathy-v1` on each device. I have written a warning above that filter,
because it looks exactly like something to optimise later - and a keep-list
without a one-shot `caches.delete("bathy-v1")` would preserve wrong seabed
data forever.

## My v23 comment named the wrong hosts

It said "Open-Meteo, the flood API and Overpass". Checked:

- **Overpass is not affected** - `structure.js` sends it as a **POST**, and
  the handler exits at `if (req.method !== "GET") return;`
- **There is no browser-side flood API** - `grep -rn "flood-api" docs/`
  returns nothing; it is server-side Python only
- **GMRT was affected** and I had not mentioned it

Corrected, because that comment is the record of a real incident and it was
wrong in both directions.

## The offline map never worked

`sw.js` has a tile cache with a 900-tile cap and a header promising the app
"opens in a car park with one bar of signal". Neither `L.tileLayer` call
passed `crossOrigin`, so Leaflet's `<img>` requests were **no-cors**, the
responses came back **opaque**, `res.ok` is false for an opaque response, and

```js
if (res.ok) { c.put(req, res.clone()); trimTiles(); }
```

never ran. **Not one tile was ever cached.** `TILE_CAP` described nothing.
Both CDNs answer `Access-Control-Allow-Origin: *` - verified with curl before
changing anything, since adding `crossOrigin` to a host that refused it would
have broken the map outright - so both layers now request with CORS and the
cache is real.

## Verified

Routing of the **real sw.js**, run in a sandbox:

```
NOT INTERCEPTED           <-  marine-api.open-meteo.com/v1/marine?latitude=-33.9
NOT INTERCEPTED           <-  gmrt.org/services/GridServer?north=1&south=0
NOT INTERCEPTED           <-  overpass-api.de/api/interpreter
shell-v24                 <-  unpkg.com/leaflet@1.9.4/dist/leaflet.js
tiles                     <-  basemaps.cartocdn.com/.../5/1/2.png
shell-v24 (ignoreSearch)  <-  /                      (navigation)
shell-v24                 <-  /structure.js?v=2      (exact)
```

10/10 assertions, 36-region selftest, 0 failures.

## Still open, not changed here

Real findings I have deliberately not rushed into this deploy:

1. **No timeout on the `/data/` network-first fetch.** On one bar a connect
   can hang for a minute before the cache is consulted. Wants a ~3 s race.
2. **`if (res.ok)` trusts any 2xx.** A captive-portal login page returns 200
   and would overwrite the last good `latest.json`.
3. **Install is non-atomic and activate is destructive.** A flaky update can
   leave a partial shell with the previous one already deleted.

Each is a behaviour change to offline handling and deserves its own testing
rather than riding along with a fix you need now.
