# v4

```
v4/
├── fish_finder.py
├── config.json
├── regions.json
├── README.md
├── .github/workflows/update.yml
└── docs/index.html
```

`docs/structure.js`, `docs/anywhere.js` and `docs/sw.js` are unchanged since
v2; `calibrate.py`, `sdb.py` and `s2_turbidity.py` since v3. The newest folder
containing a file is always the current version.

---

## How many regions can carry an 8-hourly refresh?

Measured on 8 real regions, cold and warm, not estimated:

| | per region | 264 regions |
| --- | --- | --- |
| **first ever run** (cold bathymetry + coastline), 6 workers | ~32 s | ~2.3 h |
| **first ever run**, serial | ~56 s | ~4.1 h |
| **every run after that**, 6 workers | **~2.5 s** | **~11 min** |
| pure compute, no network (`--selftest`, 6 workers) | 1.9 s | 8.3 min |
| output | 434 KB, 28 files | **129 MB, 6,408 files** |
| Open-Meteo calls | 2 | 528 per run |

The warm figure is from 16 real regions spread across the world — a fair mix
of easy and hard. Small Mediterranean boxes come in nearer 0.9 s each;
Lofoten and its 2,965 coastline ways very much do not.

**Your 30 minutes was the cold path.** Cold is 56 s a region and warm is
1 s — a factor of forty. Almost none of it is arithmetic: a profile of one
region puts the whole computation at 3.7 s, of which shelter tracing is 0.85 s
and writing 27 PNGs is 0.59 s. The rest was waiting on EMODnet, Overpass and
Open-Meteo, one region at a time.

So the answer to "how many" is **not a smaller number, it is a different
shape of run**. 264 regions now refresh in about eleven minutes once their
bathymetry is cached. The limits, in the order you meet them:

1. **The first run of any new region.** Enable a group at a time.
2. **Open-Meteo's free tier** — 10,000/day. At 3 runs a day this binds
   around **1,600 regions**.
3. **GitHub Pages' 1 GB site limit.** 264 regions is already 129 MB and
   6,408 files, so the hard stop is near 2,000 and the uncomfortable one is
   nearer **700**, where upload and deploy start to dominate the run.
4. **Actions minutes** — free on a public repo; ~1,260 of 2,000 on a private
   one at 3 runs a day.

**264 is comfortable, ~500 is the sensible ceiling on the current design,
and past that the artifact — not the compute — is what stops you.**

## What made that possible

**Regions now run in parallel** (`--jobs`, default 4). Three things had to
change before that was safe, and each was a real bug:

- `run()` reassigned a module-level `OUT`, so two regions would have written
  each other's PNGs into the wrong folder. It is a local now.
- `depth_contours()` used pyplot, whose global figure state is not
  thread-safe. It uses a bare `Figure` instead.
- **matplotlib's first import is not thread-safe.** Six workers hitting
  `import matplotlib` at once produced *"partially initialized module has no
  attribute 'rcParams'"* and failed a region outright. It is imported once,
  at module load, on the main thread. This one only appeared under load —
  exactly the kind of thing that would have shown up as a random missing
  region in production.

**Open-Meteo now survives being called in parallel.** `http_get` had no
retry at all, so a single 429 lost a region its map for the day. It now
retries with backoff and honours `Retry-After`, and a cross-thread throttle
(`_HOST_MIN_INTERVAL`) holds the whole process to ~400 calls a minute
regardless of worker count — six workers finishing warm regions in a second
each would otherwise issue ~720 and trip a 600/min limit *by succeeding*.

**Overpass is bounded.** Lofoten is 2,965 coastline ways, and Overpass can
sit on a query like that for minutes. With the old settings — 180 s socket
timeout, three mirrors, two rounds — one region could occupy a worker for
close to twenty minutes and stall everything behind it. It now makes one
pass over the mirrors with a hard 200 s overall deadline, and coming back
empty is survivable: the region falls back to the bathymetric land edge,
says so, and the spot gates still keep marks off the shore.

**The coastline rasteriser is vectorised** — bit-identical output, ~30×
faster (verified against the old implementation on three real coastlines).
It was a Python loop per segment, fine for Novigrad's 6,839 vertices and not
for the Bay of Islands' 25,757.

**`species_maps_days`** (default 1) caps how many days get a per-species
raster. Three days × eight species was 24 PNGs and 24 GPX per region per
run; at 1 that is 8. Scores, spots, seasons and legal status are still
produced for every day — only the picture is capped, and the app falls back
to today's map for later days with a note saying so.

## Regions: 55 → 264, in 21 groups

All disabled except `novigrad`, so tests stay fast and the site is never
empty. Twenty-one groups: `adriatic` (15), `med-west` (38), `med-east` (19),
`med-south` (7), `iberia-atlantic` (18), `macaronesia` (13), `nw-europe`
(12), `africa-west` (6), `africa-south` (6), `red-sea` (7), `indian-west`
(17), `coral-triangle` (17), `pacific-nw` (7), `pacific-islands` (15),
`pacific-ne` (11), `atlantic-nw` (8), `atlantic-sw` (9), `caribbean` (21),
`australia-temperate` (8), `australia-tropical` (4), `nz` (6).

### Turning them on — three ways, none needing a code change

```bash
python fish_finder.py --groups              # what exists
python fish_finder.py --select med-west     # a whole group, once
python fish_finder.py --select cascais,elba # named regions, once
python fish_finder.py --select all          # everything
```

For the **scheduled** job, set the `FISH_REGIONS` repository variable
(Settings → Secrets and variables → Actions → Variables) to the same kind of
string. No commit. The manual *Run workflow* button takes it as an input too.
Falling through all of those, `"enabled": true` in `regions.json` still
decides.

The cron is now `40 3,11,19 * * *` — every 8 hours.

### A caveat that matters

The 209 new regions are **auto-generated from a table**: their bboxes, depth
bands and visibility baselines are reasonable starting values, not surveyed
ones. Each note says so. I sanity-checked every box for shape and size, and
smoke-tested 16 spread across the world — Adriatic, Ligurian, Aegean,
Moroccan, Red Sea, Zanzibar, Komodo, Okinawa, Hawaii, California, Cape Cod,
Bonaire, Brazil, Sydney, New Zealand, Lofoten. All 16 produced 26–88% water
coverage and sensible depths, and all 16 completed in **41 s** warm with six
workers. But a box you intend to dive deserves a look at the map before you
trust a mark in it.

Regions with a bad bbox fail loudly and alone: `run()` raises when under 5%
of a box is water, the region is logged and skipped, and the others still
publish.

## What else is worth adding

Full write-up in the README. Ordered by value, the first three all beat
adding more coasts:

1. **Protected areas, automatically.** All 264 regions say "no exclusion
   zones defined". Unlike fishing law this is automatable — the World
   Database on Protected Areas publishes global MPA polygons and
   `rasterize_polygons()` already takes GeoJSON. Several regions now in the
   catalogue sit inside reserves where spearfishing is banned outright.
2. **Calibration.** The weights are still guesses, and `visibility.baseline`
   is now the number that most decides whether a day looks worth driving to.
   `calibrate.py` needs ~30 logged dives, blanks included.
3. **Real bathymetry outside Europe** — NOAA CUDEM (US, 3–10 m) and
   AusSeabed. Everything outside EMODnet is on a 300–450 m grid where a reef
   is smaller than one cell.
4. **Batch the conditions fetch.** Open-Meteo takes multiple coordinates per
   request; two calls per *hundred* regions instead of per region removes
   the daily-quota ceiling entirely.
5. **Ship terrain, not pictures.** The browser can already score a species
   itself (`speciesScore()`). A region shipping four compact terrain rasters
   instead of eleven rendered maps could compute any species, any day, any
   wind, at similar size — and `species_maps_days` would disappear.
6. **Real tide tables** for the macrotidal coasts, which the new catalogue
   has a lot of.

## Verification

- `--selftest --select all` across **all 264 regions**, 6 workers: exit 0,
  no failures.
- 14 assertions over generated output (depth bands, spot depths, visibility
  never at the ceiling, daylight consistency, model.json completeness).
- 16 real regions worldwide, live network: 0 failures, 41 s warm.
- Coastline rasteriser proven bit-identical to the old one on three real
  coastlines while ~30× faster.
- `py_compile` clean; `docs/index.html` parses.
