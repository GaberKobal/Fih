# European dive forecast

A daily spearfishing forecast that runs entirely in GitHub's cloud and opens
as a web page on your phone. Nothing runs on your machine, nothing needs to
stay switched on, and no API key is required.

Covers any coast in the world, at two very different levels of usefulness.

```
GitHub Actions (cron, 03:40 UTC)
        │  fetches EMODnet bathymetry (once, then cached)
        │  fetches Open-Meteo waves / wind / rain / tide / SST
        │  scores the grid, extracts the top spots
        ▼
docs/data/  score.png · spots.geojson · spots.gpx · latest.json
        │  committed back to the repo
        ▼
GitHub Pages  →  the map on your phone
```

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
  on  novigrad       Novigrad, Istria             Croatia    species+exclusions
  on  kvarner        Kvarner — Krk and Cres       Croatia    species
  on  split          Split and Brač channel       Croatia    species
  on  piran          Piran and the Gulf           Slovenia   structure only
  off cyclades       Paros and Naxos              Greece     structure only
```

Flip `enabled` to add one. Each costs roughly 600 KB of output and 5 seconds
of compute per day, and the region picker in the app doubles as a "where is
diveable today" view, sorted best-first within each country.

**Coverage is deliberately uneven, and the app says so.** Three things are
region-specific and do not travel:

| | |
| --- | --- |
| `species_file` | Biology only — depth, temperature, substrate, season of presence. Travels across the Mediterranean. Portugal is Atlantic and has no pack. |
| `jurisdiction` | Picks `legal/<CC>.json`. Law does **not** travel, so this is separate from biology. |
| `exclusions_file` | Protected areas, port limits. Only Novigrad has one. Every other region shows an amber warning telling you to check for yourself. |
| `thermocline` | Monthly temperature drops estimated for the north Adriatic. Deeper, clearer regions need their own. |

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
| `depth.min_m` / `max_m` | Your working depth band. |
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
like a ski forecast with no snow. Viz is modelled as a decaying memory of wave
stirring (`Hs³`, ~22 h e-folding time) and of rainfall and river input
(~40 h). That is the number that decides whether the day is worth the drive.

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
python fish_finder.py              # the real thing
```

`--selftest` is the fastest way to check the pipeline after editing anything.

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

## Species and rules

`species.json` holds **biology** — where each animal lives, what temperature it
tolerates, how it responds to water clarity. That generalises across the
Mediterranean, so every Mediterranean region shares it.

`legal/<CC>.json` holds **law**, one file per jurisdiction, merged in at run
time by the region's `jurisdiction` field. A closed season in Croatia means
nothing in Slovenia, so these are kept strictly apart.

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
