# Novigrad dive forecast

A daily spearfishing forecast for the Istrian coast that runs entirely in
GitHub's cloud and opens as a web page on your phone. Nothing runs on your
machine, nothing needs to stay switched on, and no API key is required.

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
2. **Settings → Pages → Source:** *Deploy from a branch*, branch `main`,
   folder `/docs`.
3. **Settings → Actions → General → Workflow permissions:** *Read and write*.
   Without this the job cannot commit its own output.
4. **Actions → Update dive forecast → Run workflow.** The first run takes a
   few minutes because it downloads the bathymetry; every later run is fast.
5. Open `https://<your-username>.github.io/<repo>/` and add it to your home
   screen.

To force a fresh run before you drive out, press *Run workflow* again from
the GitHub mobile app.

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
date,lat,lon,depth_m,viz_m,wind_from_deg,result,notes
2026-07-12,45.3204,13.5691,17.5,6,120,1,two decent sarago on the edge
2026-07-12,45.3255,13.5602,11.0,6,120,0,flat sand, nothing
```

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
