# Dive forecast

A spearfishing forecast that runs entirely in GitHub's cloud and opens as a
web page on your phone. Nothing runs on your machine, nothing needs to stay
switched on, and no API key is required.

Covers any coast in the world, at two very different levels of usefulness:
**468 configured regions** with precomputed structure maps, and a live
point forecast anywhere else you tap.

```
GitHub Actions (cron, every 4 hours)
        │  fetches EMODnet bathymetry (once, then cached)
        │  fetches Open-Meteo waves / wind / rain / tide / SST
        │  scores the grid, extracts the top spots
        ▼
docs/data/  score.png · spots.geojson · spots.gpx · latest.json
        │  committed back to the repo
        ▼
GitHub Pages  →  the map on your phone
```

## Where the files have to live

Paths are load-bearing here, and uploading through the GitHub web UI has a
habit of flattening them. Nothing warns you: the site just quietly loses a
feature. The layout the code expects:

```
.gitignore                      not "gitignore"
.github/workflows/update.yml    the daily forecast
.github/workflows/add-region.yml    } these two only run as workflows
.github/workflows/dive-log.yml      } if they are in .github/workflows/
legal/HR.json, ES.json, …       one per jurisdiction (see the gap noted below)
docs/.nojekyll                  not "nojekyll", and it belongs under docs/
docs/index.html
docs/add-region.html            index.html links to it by this exact name
docs/anywhere.js  docs/structure.js  docs/sw.js
```

A YAML file outside `.github/workflows/` is an inert text file. A
`add-region.html` sitting at the repository root is a 404 from the link in
the app *and* a precache miss in `sw.js`. `nojekyll` without its leading dot
lets GitHub Pages run Jekyll over `docs/`, which strips directories whose
names begin with an underscore.

## Setting it up, from a phone if you like

1. Create a new repository and upload these files (the GitHub web UI and the
   mobile app can both do this — you never need a terminal).
2. **Settings → Pages → Source: GitHub Actions.** Not "deploy from a branch" —
   the forecast output is a few MB a day across several regions, and
   committing that would bloat the repo permanently. The workflow publishes it
   as an artifact instead, so `docs/data/` is gitignored and never enters
   history.
3. **Settings → Actions → General → Workflow permissions:** *Read and write*.
   Without this the job cannot commit its own output.
4. **Actions → Update dive forecast → Run workflow.** The first run takes a
   few minutes because it downloads the bathymetry; every later run is fast.
5. Open `https://<your-username>.github.io/<repo>/` and add it to your home
   screen.

To force a fresh run before you drive out, press *Run workflow* again from
the GitHub mobile app.


## Bathymetry tiers — read this before trusting a map

| provider | where | native | what the relief term can do |
| --- | --- | --- | --- |
| **EMODnet** WCS | European seas, incl. Azores and Canaries | ~115 m | finds reef-scale structure |
| **GMRT** GridServer | everywhere else | 100 m where multibeam exists, coarser where not | depends — measured per region |
| **SRTM15+** via NOAA ERDDAP | fallback | ~320–460 m | **broad slope only** |

Picked automatically from the bbox; `bathymetry.provider` overrides it. If a
source is down the code falls down the chain rather than dropping the region,
so an outage costs resolution, not coverage.

GMRT merges cleaned research multibeam over a global base, so what you get
depends on whether anyone has surveyed that coast. The code **measures the
grid that actually arrives** instead of trusting this table, and the app
reports the real figure per region.

That gap is not a detail. The relief term looks for mounds a few cells across.
At 450 m a tegnùe is smaller than one cell, so outside Europe a mark means
"the shelf does something here", not "there is a spot here". The app measures
the resolution it actually received, refuses to resample far below it, and
shows an amber banner on any region where relief is not meaningful.

Two national sources would fix this where they exist and are the obvious next
step: **NOAA CUDEM** (US coasts, 3–10 m) and **AusSeabed** (Australia). The
provider abstraction is built to take them.

## Regions

`regions.json` is the catalogue. `python fish_finder.py --list` shows it:

```
  on  novigrad     adriatic     Novigrad, Istria        Croatia   species+exclusions
  off cascais      iberia-atl.. Cascais and Parede      Portugal  species
  off versilia     med-west     Versilia                Italy     species
  off komodo       coral-tri..  Komodo                  Indonesia species
  off valparaiso   pacific-se   Valparaiso              Chile     species
```

Flip `enabled` to add one. Each costs roughly 434 KB of output and a couple
of seconds of compute per run, and the region picker in the app doubles as a
"where is diveable today" view, sorted best-first within each country.

**Clicking a region in the app does not enable it, and cannot.** The app
renders precomputed output, so a region that has never run has nothing to
show and would only say "no forecast yet". `enabled` is read by the daily
GitHub Actions job, not by the browser.

**That is what improves the species list.** Point mode covers conditions and
structure anywhere on Earth, but the species it names come from
`regionContaining()` in `docs/index.html`, and that only sees regions that
actually ran. Outside every one of them the app falls back to the coarse
ocean-basin classifier in `BIOME_RULES` — a reference list for that stretch
of ocean rather than a pack tuned to the coast, carrying no legal sizes or
seasons at all.

### Turning regions on

`regions.json` has **468 regions in 22 groups**, and one, `novigrad`, is
enabled, so the site is never empty and tests stay fast. Three ways to run
more, none of which need a code change:

```bash
python fish_finder.py --groups                    # what exists
python fish_finder.py --select med-west           # a whole group, once
python fish_finder.py --select cascais,elba,malta # named regions, once
python fish_finder.py --select all                # everything
```

For the scheduled job, set the **`FISH_REGIONS` repository variable**
(Settings → Secrets and variables → Actions → Variables) to the same kind of
string — `med-west,adriatic`, or `all`. No commit required. The manual
*Run workflow* button takes the same value as an input. Failing all that, it
falls back to whatever has `"enabled": true` in `regions.json`, which is the
setting to change if you want it permanent.

Groups, with sizes: `med-west` 60, `coral-triangle` 37, `caribbean` 35,
`med-east` 33, `indian-west` 29, `nw-europe` 28, `pacific-islands` 27,
`iberia-atlantic` 26, `adriatic` 23, `pacific-ne` 19, `macaronesia` 18,
`atlantic-sw` 16, `australia-temperate` 15, `atlantic-nw` 14,
`med-south` 13, `pacific-nw` 13, `africa-west` 12, `africa-south` 11,
`nz` 11, `red-sea` 11, `australia-tropical` 9, `pacific-se` 8.

`pacific-se` is the Humboldt current, and it is the one group where the
species pack is an outright analogue rather than an approximation: Chile and
Peru have no pack of their own here, so the Benguela one is used because
both are cold eastern-boundary upwelling systems. The region notes say so.

**Enable a group at a time, not everything at once.** The first run of a
region downloads its bathymetry and its coastline; after that both are
cached and it is roughly forty times cheaper. See the numbers below.

### How many regions can this actually carry?

Measured, not estimated, on this hardware:

| | per region | 468 regions |
| --- | --- | --- |
| **first ever run** (cold bathymetry + coastline), 6 workers | ~32 s | ~4.2 h |
| **first ever run**, serial | ~56 s | ~7.3 h |
| **every run after that**, 6 workers | **~2.5 s** | **~20 min** |
| pure compute, no network (`--selftest`, 6 workers) | 2.2 s | 17 min |
| output | 466 KB, 24 files | **213 MB, 11,248 files** |
| Open-Meteo calls | 2 | 936 per run |

The warm figure comes from 16 real regions spread across the world, which is
a fair mix of easy and hard — small Mediterranean boxes alone come in nearer
0.9 s each, Lofoten and its 2,965 coastline ways very much do not.

So the steady state is **about 20 minutes of compute plus CI overhead, six
times a day** on the 4-hourly schedule with everything enabled. The binding
limits, in the order you will hit them:

1. **The first run.** Cold bathymetry dominates everything else by a factor
   of thirteen. Turning on all 468 at once is a **~4.2 hour job at six
   workers** and would need `FISH_JOBS` raised; the workflow timeout is 330
   minutes, so it fits, but only just. Enable a group at a time instead and
   every later run is minutes.
2. **Open-Meteo's free tier** — 10,000 calls a day, 600 a minute. Two calls
   per region per run, six runs a day: 468 regions uses **5,616 a day, about
   56%**. That is the constraint the 4-hourly schedule moved most — it
   leaves room to roughly **800 regions** before the daily quota binds,
   down from 1,600 on the 8-hourly one. The per-minute limit is handled in code by a cross-thread throttle
   (`_HOST_MIN_INTERVAL`), which holds the whole process to ~400/min no
   matter how many workers run.
3. **GitHub Pages** will not publish a site over 1 GB, and the artifact
   upload degrades well before that. 468 regions measures **213 MB and
   11,248 files**, so the hard stop is near 2,000 regions and the
   *uncomfortable* one is nearer **900**, where upload and deploy start to
   dominate the run. This is now the tightest of the four.
4. **Actions minutes** — free and unlimited on a **public** repository.
   On a **private** one this is now the hard blocker: six runs a day at
   ~25 minutes is roughly **4,500 minutes a month against a 2,000 free
   allowance**. If the repo is private, either make it public, go back to
   the 8-hourly cron, or run fewer regions.

**So: 468 is close to the sensible ceiling on the current design, and past
about 700 the artifact, not the compute, is what stops you.**
The two changes that lift it are batching the conditions fetch and shipping
terrain instead of rendered maps; both are in the roadmap below.

### What is cached, and why that decides everything

The cold cost is paid **once per region, ever** — but only because three
things are cached permanently:

| | size per region | refetched |
| --- | --- | --- |
| bathymetry grid | ~1,060 KB | never |
| OSM coastline | ~370 KB | never |
| charted wrecks | ~1 KB | never |
| Copernicus temperature profile | small | once a day, if credentials are set |
| **Open-Meteo conditions** | — | **every run — it is the forecast** |

That is ~1.4 MB a region, so a full 264-region cache is about **386 MB**,
comfortably inside GitHub's 10 GB per-repository limit.

**The cache key must be unique per run.** `actions/cache` skips its save step
entirely when the key hits exactly, so keying on `hashFiles('regions.json')`
had a trap in it: turning a group on through the `FISH_REGIONS` variable
changes no file, so the key hit exactly, the run downloaded every cold
bathymetry it needed, threw them away at the end, and did the same thing
again on the next run — permanently. The key is `github.run_id` now, which
never hits exactly, so the cache is always written back. `restore-keys:
bathy-` pulls the most recent previous one.

The "Report output size" step prints the cache contents every run. **If the
number of cached grids is not growing after you enable a group, the cache is
not being written and every run is paying full price.**

`docs/data` is cached alongside it, which is what makes the next section
possible.

### Editing the page does not recompute the forecast

`docs/data/` is gitignored — it only ever exists as a build artifact — so a
push had nothing to publish unless the model ran. That meant **a CSS tweak
recomputed every enabled region**, which is fine at one region and about
fifteen minutes at 264.

The build now diffs the push. If everything that changed is `docs/index.html`,
`anywhere.js`, `structure.js`, `sw.js`, `add-region.html`,
`region-species.json` or `README.md`, it republishes the cached forecast with
the new page and skips the model entirely — three or four minutes, nearly all
of it artifact upload and deploy. The log says which forecast it published
and how old it is, and the next scheduled run refreshes it.

It fails safe in every direction: an unreadable diff, an empty diff, a first
push on a new branch, or a missing cache all fall through to running the
model. Anything touching `config.json`, `regions.json`, `species*.json`,
`exclusions.geojson` or `fish_finder.py` runs it too, because those do change
the answer.

One thing to know while a long cold run is going: `concurrency` is set to
`cancel-in-progress`, so pushing during it cancels it. That is no longer
expensive — `index.json` is written as each region lands, so the regions
already computed are published and their bathymetry stays cached — but it
does mean the tail is left for the next run.

**Coverage is deliberately uneven, and the app says so.** Three things are
region-specific and do not travel:

| | |
| --- | --- |
| `species_file` | Biology only — depth, temperature, substrate, season of presence. Travels across the Mediterranean. Portugal is Atlantic and has no pack. |
| `jurisdiction` | Picks `legal/<CC>.json`. Law does **not** travel, so this is separate from biology. |
| `exclusions_file` | Protected areas, port limits. Only Novigrad has one. Every other region shows an amber warning telling you to check for yourself. |
| `thermocline` | Monthly temperature drops estimated for the north Adriatic. Deeper, clearer regions need their own. |
| `docs/region-species.json` | A five-species identification guide for every configured coast. It is display-only: it does not score a fish or claim it is legal to take. |

The visibility model was tuned on a turbid shallow shelf and will understate
clarity badly in Sardinia or the Cyclades. It is uncalibrated everywhere,
including at home.

### Adding one

Copy a block in `regions.json`, give it an id, a name and a bbox inside
EMODnet coverage, set `species_file` and `exclusions_file` to `null` unless
you have built them, and set `enabled: true`. Commit — the workflow picks it
up. A region that fails is logged and skipped; the others still publish.

## Editing it

Everything tunable lives in `config.json`. Edit it in the browser; the push
triggers a rebuild automatically.

| Field | What to change it for |
| --- | --- |
| `bbox` | The area covered. Keep it small — a 15 km box is plenty. |
| `depth.min_m` / `max_m` | Your working depth band. Currently 2–25 m. This is the ceiling of the *scored* band, not a cliff: `depth_fit()` tapers from `best_m[1]` down to zero at `max_m + soft_edge_m`, so a 25 m ledge still scores, just low. |
| `overlay.land_buffer_m` | How far the colour wash is held back from the coastline, in metres. Purely cosmetic — it never changes a score or moves a spot. Raise it if the wash still touches the beach, lower it if it is hiding water you want to see. |
| `entry_points` | **Edit these.** Only one placeholder is filled in. Add every place you actually get in the water. |
| `max_swim_km` | How far out you will swim from an entry point. |
| `weights` | The relative pull of relief, slope, shelter, habitat. |
| `species_ids` | `null` scores every species in `species.json`. Set to a list like `["sargus","aurata"]` to cut the run short. |
| `visibility.*` | How fast you think the water clears after a blow or rain. |
| `workability.*` | The sea state and wind you personally will and will not dive. |

`exclusions.geojson` is a hard mask: anything inside those polygons is scored
zero. Put protected areas, the port limits and any no-take zones in there.
It ships empty, which means it is protecting you from nothing yet.

## The three big changes from the first version

**Bathymetry.** ETOPO1 at ~1.8 km cannot see anything a diver can swim to.
This uses the EMODnet DTM at ~115 m, and instead of raw gradient it looks for
*relief* — how far a cell rises above the surrounding seabed — which is the
right detector for the tegnùe-style biogenic outcrops that hold fish on an
otherwise flat north Adriatic shelf.

**Visibility.** The first version had no notion of it, which for this coast is
like a ski forecast with no snow. Viz is modelled as a **baseline** plus a
decaying memory of wave stirring (`Hs³`, ~22 h e-folding time) and of rainfall
and river input (~40 h). That is the number that decides whether the day is
worth the drive.

The baseline is not decoration. Without it turbidity started at zero, so with
no recent swell or rain the model reported `viz_max_m` — the ceiling — on
every calm dry day. It was not forecasting 11 m, it was reporting the maximum,
which is why the number always looked optimistic. Real coastal water is never
optically clean: there is always plankton, always current-driven
resuspension, always land input, and on a shallow shelf always fine sediment
that never fully settles. At the default 0.35 a flat calm day reads about
7.5 m instead of 11, a metre of swell about 4.7 m, and a wet day about 1 m —
which is where the dive log actually sits.

`visibility.baseline` is the most region-specific number in the model, so
`regions.json` overrides it per coast: 0.08 in the Gulf of Aqaba, 0.12 around
Malta and Ibiza, 0.35 on the north Adriatic shelf it was tuned on, 0.5 in
Brittany. `wave_ref_hs` also came down from 1.4 to 1.0 — cubed and normalised
against 1.4 m, anything under about 0.8 m of swell barely registered, which
is wrong on a sandy shelf where short chop lifts sediment.

**Shelter.** For each cell the model traces the upwind ray across the land
mask until it hits shore. Short fetch means flat, workable, often clearer
water. This is why the map genuinely changes day to day rather than showing
you the same reef every morning.

Dropped: SST fronts and chlorophyll. In 20 m of stratified, sediment-loaded
Case-2 water they are close to noise, and standard chlorophyll algorithms
overestimate there because they cannot separate pigment from suspended
sediment and CDOM.

## Calibration — the part that turns this into a model

The weights in `config.json` are **guesses**. A map made from guessed weights
looks exactly as confident as one made from fitted weights, which is the
dangerous thing about it.

Fill in `dive_log.csv` as you dive — one row per dive, **including the
blanks**, which matter more than the good days:

```csv
date,lat,lon,depth_m,viz_m,wind_from_deg,species,result,notes
2026-07-12,45.3204,13.5691,17.5,6,120,sargus,1,two decent sarago on the edge
2026-07-12,45.3255,13.5602,11.0,6,120,sargus,0,flat sand nothing
```

Use the `id` from `species.json` in the species column. Once you have enough
rows for one species you can fit that species on its own:
`python calibrate.py --species sargus`.

Then run `python calibrate.py`. It fits a logistic regression of `result` on
the terms sampled at your points, writes `weights.json`, and the daily job
picks that up automatically. It prints an AUC so you can see whether the model
beats guessing, and it will tell you plainly when it does not.

Below about 30 dives the fit will overfit whichever spot you happened to visit
most; the script says so rather than quietly producing numbers.

## Running it locally anyway

```bash
pip install -r requirements.txt
python fish_finder.py --selftest   # synthetic seabed, no network at all
python fish_finder.py              # whatever is enabled
```

`--selftest` is the fastest way to check the pipeline after editing anything;
it exercises all 264 regions without touching the network.

Useful flags:

```bash
python fish_finder.py --groups              # the 21 groups and their sizes
python fish_finder.py --list                # every region, one per line
python fish_finder.py --select med-west     # a group, ignoring enabled flags
python fish_finder.py --region cascais      # exactly one
python fish_finder.py --jobs 8              # regions computed at once
```

Everything reads UTF-8 explicitly and the log is UTF-8 with replacement, so
region names with a `č` or an em dash no longer break the run on a Windows
console — which they did, fatally and half way through, until v3.

## Before this goes public

Four things had to change once other people would be using it, and they are
done. What is left is listed after them, honestly.

**Protected areas are masked.** Every region but Novigrad shipped with no
exclusion mask at all, on a catalogue that includes coasts where
spearfishing is banned outright. OSM protected areas are now fetched per
region and IUCN categories 1a, 1b, 2 and 3 are masked out of the score;
categories 4 to 6 routinely permit fishing, so those are named in the app
rather than blanked. On top of that, a curated flag in `regions.json` marks
**6 regions as banned and 33 as restricted** — a banned region publishes
**no waypoints at all**, because a warning above a list of thirty numbered
coordinates is not a deterrent.

OSM is not the WDPA and its marine coverage is incomplete. Bonaire proves
it: OSM has the individual reserves but not the island-wide marine park that
actually bans spearfishing, which is exactly why the curated flag exists.
The app says in as many words that no mask is not evidence of no reserve.

**A safety notice on first open.** Not a footnote. It says the model has
never been fitted against real dives, that legal data exists for 5 of 134
jurisdictions, that protected-area coverage is incomplete, and that
breath-hold diving kills experienced people through shallow water blackout.
Acknowledged once, remembered, and reachable again from Sources.

**Attribution and licences.** A Sources section credits OpenStreetMap (ODbL,
for the derived coastline, wrecks and reserves, not just the tiles),
OpenSeaMap, EMODnet, GMRT, NOAA, Open-Meteo and Copernicus.

**Third-party services are used within their policies.** The base map moved
off `tile.openstreetmap.org`, whose usage policy excludes third-party apps,
to Carto's free basemaps. Overpass responses are now cached in the browser
and rate-limited to one request per three seconds per tab, because
client-side scanning was the one place this app could genuinely have abused
a volunteer-funded service.

### Counting how many people use it

GitHub Pages gives you no server logs, so counting has to happen in the
browser. It is **off until you set an endpoint**, at the top of the script in
`docs/index.html`:

```js
const ANALYTICS = {
  endpoint: "https://YOURCODE.goatcounter.com/count",
  countScans: true,
};
```

[GoatCounter](https://www.goatcounter.com/) is the suggested one: free for
non-commercial use, open source, cookieless, and it accepts a plain GET so no
third-party script has to be loaded at all. Anything taking
`?p=<path>&t=<title>&r=<referrer>` works the same way.

**The path is sanitised, and that is the whole point.** The hash of this app
carries real coordinates — `#@38.6900,-9.3500` is somewhere a person is
thinking about diving, to about eleven metres. An off-the-shelf snippet
reports `location.href` verbatim, which would hand a third party a list of
people's dive spots. So:

| on screen | reported as |
| --- | --- |
| `#cascais` | `/r/cascais` — a public region id |
| `#@38.6900,-9.3500` | **`/point`** — never the coordinates |
| a structure scan | `/scan` |
| anything unrecognised | `/` |

No cookies, no identifiers, no tracking between visits, no third-party
script. Do Not Track and Global Privacy Control are honoured. Because it sets
no cookie and stores nothing, it does not need a consent banner under
ePrivacy — but the Sources section in the app says exactly what is sent
regardless, which is the part that actually matters.

It also cannot break anything: the whole thing is wrapped, and offline the
request just fails unnoticed.

### Things that only matter once other people are looking

A handful of gaps that were invisible while you were the only user:

- **Point mode had no ban warning at all.** It is the default mode, so a tap
  inside Bonaire or the Galapagos showed nothing — precisely where the
  warning matters most, since there is no region page to have read first. A
  point forecast now inherits the status of whatever configured region it
  falls inside, and says plainly that protected areas are *not* checked for
  an arbitrary point rather than letting silence read as an all-clear.
- **The client-side scan would have walked straight around the ban.** The
  daily job refuses to publish waypoints in a banned region; *Scan
  structure* did not. It does now, and says why.
- **468 regions across 104 countries is a haystack, not a list.** The region
  picker has a search box (name, country, group or id) and marks banned and
  restricted coasts in the list itself, not only after you open them.
- **`add-region.html` was writing stale defaults** — a 24 m depth band, no
  `group` (so the region would be invisible to `--select <group>`), and
  `enabled: true`, which would have quietly added a stranger's submission to
  the schedule. It now writes 25 m, asks for a group, and defaults to off.
  It was also still pulling OSM's own tiles.

### Still true, and stated in the app

- The weights are **guesses**. Nothing has been fitted. This is the biggest
  remaining problem and only dive logs fix it.
- **129 of 134 jurisdictions have no legal pack**, including HR. No sizes,
  no seasons, anywhere but ES, FR, GR, IT and PT.
- **413 of 468 regions are auto-generated**: plausible bboxes and baselines,
  not surveyed ones.
- `max_swim_km` still gates nothing, because `entry_points` is empty.

## Where this should go next

Ordered by how much they would improve the thing, not by how hard they are.
The first three are all bigger wins than adding more coasts.

### 1. Protected areas, automatically

Every one of the 264 regions says *"no exclusion zones defined — check
protected areas yourself"*, because `exclusions.geojson` was hand-drawn for
Novigrad and nowhere else. Several of the coasts now in the catalogue sit
inside a reserve where spearfishing is banned outright: the Plemmirio, the
Calanques, Tabarca, the Kornati, Ses Salines, Cabo Pulmo, Ningaloo, the
Poor Knights.

Unlike fishing *law*, this is automatable. The **World Database on Protected
Areas** publishes global MPA polygons, and `rasterize_polygons()` already
takes GeoJSON. Fetch the polygons intersecting each bbox, cache them next to
the bathymetry, and the existing exclusion gate does the rest. It turns a
warning that everyone learns to ignore into an actual mask, and it is the
single highest-value thing left in this repo.

It does not make the app a legal authority and must not be described as one —
a WDPA polygon is a research dataset, not a chart — but "the model refuses to
mark this reef" is a much better failure mode than a footnote.

### 2. Calibration

`weights` in `config.json` are still guesses, and a map built from guessed
weights looks exactly as confident as one built from fitted weights. That is
the most dishonest thing about the whole model. `calibrate.py` already fits
them against `dive_log.csv` — it needs about 30 logged dives, blanks
included, and blanks matter more than the good days. The same applies to
`visibility.baseline`, which is now the number that most directly decides
whether a day looks worth driving to: `calibrate.py --fit-viz` will replace
the 0.35 guess with a fit as soon as there is a log to fit it to.

### 3. Real bathymetry outside Europe

EMODnet gives European coasts ~90 m and the relief term genuinely finds
structure. Everywhere else falls to GMRT or SRTM15+ at 300–450 m, where a
reef is smaller than one cell and the app has to say so. Two national
sources would fix most of the new catalogue:

- **NOAA CUDEM** — US coasts at 3–10 m, which would transform Hawaii, the
  Keys, California and the whole US east coast
- **AusSeabed** — Australia, same story

The provider abstraction in `fetch_bathymetry()` is built to take them.

### 4. Batch the conditions fetch

The next scaling step past ~1,000 regions is not more workers. Open-Meteo
accepts **multiple coordinates in one request**, so a run that currently
makes two calls per region could make two calls per hundred regions. That
turns the daily quota from the second-tightest constraint into a non-issue
and removes most of the per-region latency at the same time.

### 5. Ship terrain, not pictures

Per-species PNGs are the entire output budget. The browser can already score
a species itself — `speciesScore()` in `docs/structure.js` does exactly that
for scanned points. If a region shipped its *terrain* (relief, slope, depth,
land as four compact rasters) instead of eleven rendered maps, the app could
compute any species, on any day, for any wind, at a similar file size — and
the `species_maps_days` compromise would disappear entirely.

### 6. Tides that are actually tides

Open-Meteo's sea level is an 8 km model, and the app already flags anything
over 1.5 m as macrotidal. That flag now fires on a lot of the catalogue —
Brittany, Normandy, Pembrokeshire, Cascais, Darwin, Puerto Madryn. On those
coasts the timing is driven by tide rather than wind, so a real harmonic
tide source would matter more than any other single input.

### 7. Smaller things worth having

- **Alerts** — "tell me when Cascais goes green", which is the actual
  question a spearo has and one the region index already answers daily.
- **More species packs.** Twelve cover the world only approximately; the
  Humboldt current has none at all, which is why Chile and Peru correctly
  return nothing.
- **More legal packs.** Seventeen jurisdictions in the catalogue have none,
  so they show no sizes and no seasons — a gap, never permission.
- **Offline pinning** of a region before a trip, on top of the existing
  service worker.

## Limits worth keeping in mind

- The relief term finds *bumps in a 115 m grid*. Real tegnùe are often smaller
  than one cell. Treat a mark as "there is something structurally interesting
  within a couple of hundred metres", not as a waypoint to a rock.
- Swim distance is straight-line. It does not know you would have to go around
  a headland.
- Open-Meteo's tide model runs at 8 km and is not a navigational tide table.
- Nothing here knows about the law. Croatia requires a recreational fishing
  licence with a separate underwater endorsement, bans spearfishing at night
  and on scuba, and restricts it near swimming areas and in protected zones.
  Confirm the current rules with the Croatian fisheries ministry, and fill in
  `exclusions.geojson` before you trust a mark near a protected area.
- Never dive alone, whatever the map says.

## Picking a target

Choosing a species does not just filter a list — it redraws the map. The
structure map asks "where is there reachable relief and shelter"; a species
map asks "where should *this animal* be", by reweighting relief and slope
with its affinities and gating on its own depth envelope intersected with
your working band. A gilthead that lives in 1–12 m and a dentex that wants
the 20 m edge get genuinely different maps and genuinely different spots.

Precomputed regions ship one PNG and one GPX per species per day. A scanned
point now does the same thing live in the browser: shelter and the wreck halo
are computed once and reused, so switching target costs one pass over the
grid and no extra network calls at all. `speciesScore()` in
`docs/structure.js` mirrors `species_spatial_score()` in `fish_finder.py`, so
both halves answer the question the same way.

## Species and rules

`species.json` holds **biology** — where each animal lives, what temperature it
tolerates, how it responds to water clarity. That generalises across the
Mediterranean, so every Mediterranean region shares it.

`legal/<CC>.json` holds **law**, one file per jurisdiction, merged in at run
time by the region's `jurisdiction` field. A closed season in Croatia means
nothing in Slovenia, so these are kept strictly apart.

> **`legal/HR.json` is missing from this repository.** ES, FR, GR, IT and PT
> are present; HR is not. Novigrad is the only enabled region and its
> `jurisdiction` is `HR`, so right now the one region that actually runs has
> **no minimum sizes and no closed seasons applied at all** — `load_legal()`
> logs `no rules pack for jurisdiction HR` and returns an empty pack, and
> every species shows as open because nothing says otherwise. This is the
> single most consequential gap in the repo and it cannot be filled by
> guessing: the closed seasons and sizes have to be read off the current
> *Pravilnik o zaštiti riba i drugih morskih organizama* at ribarstvo.mps.hr
> and written into `legal/HR.json` by hand. Until then, treat every species
> panel for Novigrad as "unknown", not "legal".

`docs/region-species.json` is a separate, display-only guide for every
configured coast. It makes an unfamiliar region less opaque without turning a
likely encounter into a model score or legal recommendation.

### Three tiers of confidence, and the app shows which is which

| tier | what it is | how far to trust it |
| --- | --- | --- |
| **EU minimum sizes** | Reg (EU) 2019/1241 Annex IX | Real law and a **floor** across every EU Mediterranean state. National rules may be stricter and often are. |
| **Framework rules** | licence, night ban, scuba ban, distances, marker buoy | Widely reported and stable, but taken from secondary sources for everything except Croatia. The app labels these **"framework only"** and links to the national regulator. |
| **Closed seasons** | species-by-species closures | Researched for **Croatia only**. Every other pack sets `closed_seasons_researched: false`, and the app says *"closed seasons not researched for XX — an open species means unknown, not legal."* |

That last distinction matters more than anything else in this file. An empty
`closed_seasons` list is a gap in my research, not a statement that fishing is
open. The app never implies otherwise.

### Adding a jurisdiction properly

Open the national regulator (each pack carries its `official_source`), find
the current closed seasons and minimum sizes, fill them into
`legal/<CC>.json`, set `closed_seasons_researched: true` and update `checked`.
The warnings disappear on their own.

## Going worldwide: what else breaks

**Tides.** The movement term assumes a Mediterranean tide of 30–80 cm. Every
run now measures the forecast tidal range and flags anything over 1.5 m as
macrotidal, because Open-Meteo's 8 km sea-level model is not adequate on those
coasts. Brittany, the UK, north-west Australia: use a real tide table.

**Named winds.** Bora, jugo and maestral mean nothing in Hawaii. Regions
outside the Adriatic set `wind_regimes: null`, and wind still counts through
fetch and workability — it just is not given a local name or a clarity
multiplier it has not earned.

**Species and law.** Unchanged and still the dominant cost. Every non-European
region ships with `species_file: null`, so it runs structure and conditions
only. An Indo-Pacific or Caribbean pack is real research, not a config edit.

**Marine reserves.** None of the worldwide regions have exclusion polygons.
The Poor Knights is a no-take reserve; large parts of the Florida Keys are
closed to spearfishing. The app warns, but it does not know.

## The coastline is not the bathymetry

Bathymetry cannot answer "is this land". A grid cell reports the **average**
of what is under it, so any strip of land narrower than a cell or two
averages out to a negative elevation and is scored as perfectly good diving
water. The Ria Formosa barrier islands off Faro are 200–600 m wide, and on a
global grid that is exactly what happened: the score wash and the depth
contours were drawn straight across the beach, the dunes and the lagoon.

No amount of buffering the bathymetric mask fixes that. The mask was not a
bit too small — it was absent. So the real shoreline is fetched separately,
as OpenStreetMap `natural=coastline` ways, through the same Overpass endpoint
the wreck layer already uses:

1. the coastline is burned onto the grid as an impassable wall,
2. the sea is flood-filled inward from the deepest cell,
3. anything the sea cannot reach — spits, barrier islands, lagoons, harbour
   basins — becomes land however deep the depth grid claims it is.

Overpass clips ways at the bbox, so a coastline can arrive with an open end.
If the wall has a hole the fill leaks and you are no worse off than before;
but on a mostly-enclosed box the fill can instead be strangled down to a
puddle. Both sides therefore **refuse** the mask when applying it would
delete more than 80% of the water, and say so — a bad fetch must not be able
to turn a whole region into land.

`coastline.enabled` in `config.json` turns it off. The app states per region
and per scan whether the coastline was actually applied, because "the land
edge is bathymetry only" is something you want to know before trusting a mark
near the shore.

**The coastline is fetched in the same Overpass request as the wrecks**, and
that matters as much as the data does. They used to be two POSTs to the same
public endpoint seconds apart, and Overpass rate-limits per client: the second
routinely came back 429. The coastline went first and its failure was
swallowed, so the common outcome was a scan with wrecks but no land mask —
which is the state in which model spots march up the beach and into the town
behind it. One request cannot rate-limit itself, and there are three mirrors
behind it now.

**Spots have their own gates, independent of all of the above.** A waypoint
on the beach is the most damaging thing this can output, so `findSpots()` on
both sides refuses any mark within `overlay.land_buffer_m` of land and any
mark without a real depth at or past `depth.min_m`. The colour wash was
already held back; the spots were not, so they marched inland on their own
whenever the land mask was wrong. Now they cannot, even with no coastline at
all.

## Wrecks

Charted wrecks and obstructions come from OpenStreetMap via Overpass, cached
per region, and feed a `wreck` scoring term with a 250 m halo. They are the
best structure a spearo gets, they are point data rather than a raster, and
they are **exactly as useful on a 450 m grid as on a 115 m one** — which makes
them the most valuable thing in the whole model outside Europe.

## Satellite-derived bathymetry

`python sdb.py --region <id>` estimates depth from Sentinel-2 at 20 m, five
times finer than any chart grid available, over 2–18 m — the band you dive.

It uses the Stumpf ratio transform, `ln(1000·blue) / ln(1000·green)`, which is
monotonic in depth and largely blind to bottom type. Calibration normally
needs surveyed depths; here **the existing EMODnet or GMRT grid is the
training signal**, so the satellite supplies detail and the chart supplies
scale. No field survey.

It refuses itself when it does not work:

```
clear water   ACCEPTED   R2 0.976  RMSE 0.71 m
turbid water  REFUSED    R2 0.018  "probably too turbid for optical depth"
```

That refusal is the feature. Optical depth only works in clear water, so this
will do least for Novigrad and most for the Azores, Canaries, Keys, Bali and
Okinawa. Set `bathymetry.use_sdb` on a region to put a passing fit into
service; it is laid over the chart grid, so fine detail where the satellite
can see and the original everywhere else.

## Offline

`docs/sw.js` caches the app shell and map tiles aggressively and the forecast
network-first, so the map opens instantly at the coast and still works with no
signal — using the last forecast it managed to fetch. Tiles are capped at 900.

## Adding a region from the map

`add-region.html` — drag a box over a coast, fill in a name, and submit. It
opens a prefilled GitHub issue and `add-region.yml` validates the bbox, checks
for id collisions and appends to `regions.json`. Same no-token pattern as the
dive log. It warns you when the box is unreasonably large or the id is taken.
