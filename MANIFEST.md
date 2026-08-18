# v23 — found it. The service worker was serving one forecast to the world.

```
v23/
└── docs/sw.js        <- supersedes v22
```

Nothing else changes. `docs/index.html`, the manifest and icons stay at v22;
`fish_finder.py`, `config.json`, `README.md` at v20; `s2_turbidity.py`,
`dive_log.csv`, the workflow at v19; `regions.json` v17.

**This one is worth uploading on its own, immediately.**

---

## Proven, in your live browser

```js
fetch('...open-meteo.../marine?latitude=45.44&longitude=12.33')  // Venice
fetch('...open-meteo.../marine?latitude=-33.90&longitude=151.30') // Sydney

requestedSydney -> { lat: 45.375, lon: 12.29 }
SYDNEY_GOT_VENICE: true
```

A request for Sydney came back with Venice's data. Not a race, not the home
screen, not the app's JavaScript at all.

## The mechanism

`sw.js` routed fonts, then map tiles, then `/data/`, then `/r/`, then fell
through to an app-shell branch which is **cache-first** and matched with
**`{ ignoreSearch: true }`**.

There was **no cross-origin bail-out**. So every Open-Meteo call landed in
that final branch. And Open-Meteo point forecasts differ *only* in the query
string:

```
https://marine-api.open-meteo.com/v1/marine?latitude=45.44&longitude=12.33
https://marine-api.open-meteo.com/v1/marine?latitude=-33.90&longitude=151.30
                                            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                            ignoreSearch throws all of this away
```

Identical after `ignoreSearch`. The first point forecast fetched was cached
under the bare path, and **every subsequent point on Earth matched it**. It
lived in the shell cache, so it survived reloads, which is precisely why it
looked like a location that had been "saved". The same applied to the flood
API and to Overpass.

## Why my three earlier answers were wrong

Worth recording, because each was confidently argued:

- **the region race (v21)** — real, and fixed, but intermittent and about
  regions; it could never make every point on Earth resolve to one place
- **the unguarded `forecastPoint` await (v22)** — a real hole, now closed,
  but I could not make it misbehave on the live build and said so
- **the missing manifest (v22)** — real, and worth having, but it only
  explains the *launch* URL, not tapping a new point and getting Venice

The detail that broke it open was yours: *"pointing anywhere in the world"*.
A race cannot do that. Only a cache key that ignores the coordinates can.

## The fix

Three changes, all in `sw.js`:

1. **Bail out on cross-origin requests.** Anything not same-origin, and not
   already handled by the fonts or tiles branches, is an API call. Returning
   without `respondWith` hands it to the browser untouched — the only correct
   answer for a request whose whole meaning is in its query string.
2. **`ignoreSearch` only for navigations.** `/?utm=x` is still the shell;
   `structure.js?v=2` is not `structure.js?v=1`.
3. **`VERSION` to v23**, which purges the already-poisoned `shell-v22` cache
   on every device. Without this the bad entry would simply persist.

## A regression I introduced and caught

The bail-out would have broken the app **offline**: Leaflet's CSS and JS come
from unpkg, are cross-origin, and are deliberately precached in
`SHELL_URLS` — the bail-out would have stopped them ever being served from
cache, so an offline launch would have had no map at all. Added a
`PRECACHED_REMOTE` branch above the bail-out that serves exactly those two
URLs cache-first, matched exactly.

## Verified

The service worker will not register in my test browser, so I ran the **real
`sw.js`** in a sandbox with stubbed globals and recorded the route every
request takes:

| request | intercepted | cache | ignoreSearch |
| --- | --- | --- | --- |
| marine-api.open-meteo (Sydney) | **no** | — | — |
| api.open-meteo | **no** | — | — |
| overpass-api.de | **no** | — | — |
| unpkg Leaflet | yes | shell-v23 | **false** |
| cartocdn tile | yes | tiles | false |
| `/data/novigrad/latest.json` | yes | data-v23 | — |
| `/r/novigrad/` | yes | data-v23 | — |
| navigation `/` | yes | shell-v23 | **true** |
| `structure.js?v=2` | yes | shell-v23 | **false** |

Plus 9/9 assertions on the file and a 36-region selftest, 0 failures.

## After you upload

The poisoned entry is on your phone until the new worker activates. If it
still misbehaves for a moment, close every tab of the site and reopen it, or
clear site data. `VERSION` v23 handles it on its own once the new worker
takes over.
