# v25 — back inside every free tier, and a correction

```
v25/
├── fish_finder.py                    (one string: "every six hours")
├── README.md
├── docs/index.html                   <- supersedes v24
└── .github/workflows/update.yml      <- supersedes v16
```

`docs/sw.js`, manifest and icons stay at v24; `config.json` v20;
`s2_turbidity.py`, `dive_log.csv` v19; `regions.json` v17.

---

## 1. The schedule is now 6-hourly, because my API maths was wrong

I told you across several versions: *"16 calls a run, 96 a day, about 1% of
the free tier."* That was wrong. Open-Meteo's pricing page:

> "Requests for data covering **more than 10 weather variables**... are
> considered multiple API calls... fractional counts are used"

and their calculator carries a **Locations** field. Batching 100 locations
into one HTTP request does not make it one billable call.

| | |
| --- | --- |
| `MARINE_VARS` | 13 variables, so x1.3 |
| per run | 800 x (1.3 + 1.0) = **~1,840 calls** |
| at 6 runs a day | ~11,040 - **110% of the 10,000/day free ceiling** |
| at 4 runs a day | **~7,360 - inside it** |

Cron is now `40 1,7,13,19 * * *`.

The batching was still worth doing - it cut HTTP requests 1,518 to 16, which
is what fixed the 600/minute rate limit and the ten-minute serial prologue.
It simply never touched the billable count, and I said otherwise.

Freshness barely moves: the app re-anchors "now" to the real clock from the
hourly series it already holds, so the two dropped runs were only buying a
slightly newer upstream model.

## 2. CARTO needs a key - and I was wrong about that too

An hour ago I passed on a claim that CARTO basemaps were "available
exclusively with an Enterprise license". I had not checked it. Their current
FAQ says the opposite:

> "**There is a free tier, and it is generous.** Anyone can request an API
> key and use CARTO basemaps free of charge up to a fair use limit of
> **5 million tile requests per calendar month**. You do not need a CARTO
> account, and you do not need to tell us in advance whether your project is
> commercial."

So there is no licence problem. There **is** a live cosmetic one:

> "These are still available, but they **now require an API key and are being
> retired**"... "If the tiles in your map are covered by a repeated **'API
> key required'** watermark, they are being requested from our raster basemap
> endpoint without an API key."

**Your map is probably watermarked right now.** Get a key - free, no account,
30 seconds - at **https://carto.com/basemaps/apikey**, then paste it into
`docs/index.html`:

**TWO files, one key.** `docs/index.html` and `docs/add-region.html` each
draw a map from the same endpoint, so both take it:

```js
const CARTO_KEY = "";        // <- paste your key here
```

Paste the key into that line ONLY. Do not edit the URL below it - it appends
`?key=...` on its own, and editing both would put the key in twice. Empty
leaves today's behaviour exactly as it is, so the files are safe to upload
before the key arrives.

They cross-reference each other in comments, because add-region.html is
opened once in a while rather than every day: it is precisely the page that
would sit there watermarked for months after the main map was fixed.
`sw.js` is bumped to v25 so returning phones re-precache both.
CARTO's terms forbid hiding the watermark, and a key is free, so this is the
only correct fix.

Worth planning, not rushing: raster is being retired and CARTO recommend
vector, which needs MapLibre instead of Leaflet - a real migration.

## 3. Is there a free replacement for Open-Meteo?

Yes, several, and none is a drop-in. Verified from primary sources:

| option | free? | commercial? | catch |
| --- | --- | --- | --- |
| **Self-host Open-Meteo** | yes | yes | server is **AGPL-3.0** with a Dockerfile. Same data, same API, no licence fee - you pay in servers and ops |
| **ECMWF open data** | yes | **yes** - CC-BY-4.0, *"may be redistributed and used commercially, subject to appropriate attribution"* | raw GRIB; you build the interpolation layer |
| **NOAA GFS + WaveWatch III** | yes | yes, US public domain | raw GRIB, same work |
| **met.no** | yes | yes, CC-BY 4.0 | land weather, not a wave model |

The honest conclusion: **EUR 29/month is cheaper than all of them** once the
engineering is counted, and none of it is needed while the app stays
non-commercial. Open-Meteo's real value is the part you would be rebuilding -
model selection, interpolation, per-location timezones, one clean JSON API.

## Verified

10/10 assertions, 36-region selftest, 0 failures. Tile URL checked both ways
in a browser: empty key gives today's URL exactly, a key appends
`?key=...`, CARTO attribution retained, and every earlier fix still in place
(13 nav guards, manifest, `crossOrigin` on both tile layers).
