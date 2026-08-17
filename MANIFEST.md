# v17 — three bugs the 800-region run exposed, and the phone legend

```
v17/
├── fish_finder.py
├── config.json
├── regions.json
├── README.md
└── docs/index.html
```

`.github/workflows/update.yml` current as of **v16**; `docs/structure.js`
and `docs/sw.js` v10; `docs/add-region.html` v12; `docs/anywhere.js` v8.

**Do not push while a run is in progress.** `cancel-in-progress: true` plus
the `push` trigger means a commit kills the running job, and a cancelled job
does not reliably save the cache.

---

## 1. CMEMS was dead everywhere outside the Mediterranean

Every global region in the production log:

```
cmems: unavailable (VariableDoesNotExistInTheDataset: The variable 'thetao'
is neither a variable or a standard name in the dataset.)
```

Mediterranean regions were fine, which is exactly what hid it. Copernicus
splits **both** products into one dataset per variable — which is why the
Med id already carried `-tem` — but the global id was the bare bundle:

| | id | temperature |
| --- | --- | --- |
| Med | `cmems_mod_med_phy**-tem**_anfc_4.2km_P1D-m` | yes |
| global, before | `cmems_mod_glo_phy_anfc_0.083deg_P1D-m` | **no** |
| global, now | `cmems_mod_glo_phy**-thetao**_anfc_0.083deg_P1D-m` | yes |

Confirmed against the public STAC catalogue rather than reasoned from the
pattern: `GLOBAL_ANALYSISFORECAST_PHY_001_024` lists both ids, and only the
`-thetao` one carries temperature. Its version suffix is `_202406`, which is
the version the production log printed.

It fell back to the thermocline estimate every time, so nothing broke — the
CMEMS credentials were simply buying nothing outside the Med.

## 2. `costa-blanca` had no coastline in its box

`lon -0.2 … 0.25` at that latitude is open sea; the coast is near `-0.5`.
The log agreed: `no OSM coastline in this box`, water 99%, **minimum depth
70.6 m**, `contours.geojson (0 lines)`.

Moved to `lon -0.62 … -0.15`, `lat 38.10 … 38.45` — 41 x 39 km, containing
Santa Pola, Tabarca and Alicante. Verified on the live network:

| | before | after |
| --- | --- | --- |
| OSM coastline ways | 0 | **33** |
| shallowest water | 70.6 m | **0.0 m** |
| spots | unusable | **30** |
| depth contours | 0 | **13** |

## 3. Ten minutes of every run bought nothing

The batch fetch looped chunks serially. Invisible at 468 regions; at 800 it
was **16 back-to-back requests taking 10 min 18 s before a single region
started**, with all six workers idle — and it came out of the 180-minute
budget on every run.

The chunks share no state. They now go through a thread pool, and
`_throttle()` already paces requests per host across threads, so the API
sees nothing new. Same 16 calls, one chunk's wait instead of eight.

Measured on 250 real region centres, 3 chunks: **250/250 in 3 s.** Worth
saying plainly — the CI run's ~39 s per request did not reproduce locally,
so the size of the saving depends on API load. The shape of the fix does not.

Also fixed: the no-data message hard-coded "EMODnet", so every GMRT and
SRTM15+ region reported a gap in a source it had never queried.

## 4. The coastline cache would have defeated fix 2

Found while working out what a push actually invalidates. Bathymetry is
cached **by bbox**, so a moved box refetches correctly. Coastline and wrecks
were cached **by region id alone**:

```
cache/coastline_costa-blanca.json     <- the OLD box's result
```

That box had no coastline in it. So in CI — where that file exists from the
production run — the corrected box would have loaded fresh bathymetry and
then reapplied the **empty** cached coastline, and Costa Blanca would have
looked exactly as broken as before. It only worked on my machine because
this cache had never held that region.

Both are now keyed by id **and** bbox, so a moved box simply misses:

```
cache/coastline_costa-blanca_-0.62_38.1_-0.15_38.45.json   -> 33 ways
```

**Cost: every region refetches its coastline once.** Bathymetry — the
expensive part, ~1 MB and most of the cold time — is untouched and stays
cached. Expect one run to be somewhat longer, not a rebuild.

## 5. The legend took two-thirds of the phone map

Measured at 375x812: the map is `50vh` = 406 px and the legend was **268 px
of it**. It now collapses to the score ramp, which is the only part you read
at a glance; everything else is a setting you touch once.

| 375x812 | before | after |
| --- | --- | --- |
| default | 268 px, **66%** | **106 px, 26%** |
| opened | — | 179 px, 44%, then scrolls |

The first attempt collapsed to 64 px behind a bare `+` in the corner, and
that was wrong: it read as the legend having lost half of itself rather than
as something you could open. The affordance is now a **full-width labelled
row** — "Key and controls" with a caret that flips from down to up — in the
readable text colour, on a 34 px tap target. It costs 42 px against the
icon, and it is worth all of them.

The choice persists in `localStorage`, because the controls inside are ones
you set and want to stay set. **Desktop is untouched** — verified at
1280x860: no toggle, no height cap, and 210 x 332 px with the scan box
shown, which is exactly what it measured before any of this.

One correction worth recording: my first cap was `33vh`, which I described
as "a third of the map". It is a third of the *viewport*, and the map is
only `50vh` — so it was still 66% of the map and fixed nothing. The comment
in the CSS now says to halve any `vh` figure to get the number that matters.

## Upload all of these in ONE commit

Each push starts a run, and `cancel-in-progress` means the next push kills
the last one. Two uploads a minute apart cancel each other; that is what
happened to run #178. One commit, then leave it alone.

Note also that a commit touching **only** `docs/index.html` takes the
frontend-only path and does **not** run the model — it republishes the
cached forecast in about 90 seconds and reports green.

## Verified

- **10/10 assertions**, plus a 36-region and an 8-region selftest, 0 failures.
- Costa Blanca **on the live network** — the table above.
- The batch fetch **on the live API** — 250/250, and per-location timezones
  still resolving correctly through the thread pool (Novigrad +2, Oahu -10,
  Rottnest +8, Poor Knights +12).
- The legend **in a real browser** at 375x812 and 1280x860: first load
  collapses on the phone and stays open on the desktop, the toggle persists,
  and `#ctoggle`, `#wtoggle`, `#opac`, `#scanbox`, `#scan`, `#radius` all
  remain reachable inside the scroll area.

Not verified: the rotate-across-breakpoint handler. Its first-load
equivalent is verified at both sizes; the live `matchMedia` change did not
fire in my test harness, so that one path is reasoned, not measured.
