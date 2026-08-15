# v10 — public readiness

```
v10/
├── fish_finder.py
├── config.json
├── regions.json
├── README.md
└── docs/
    ├── index.html
    ├── structure.js
    └── sw.js
```

`docs/anywhere.js` and `docs/add-region.html` are current as of **v8**;
`.github/workflows/update.yml` as of v7; `calibrate.py`, `sdb.py`,
`s2_turbidity.py` as of v3.

---

## 1. Protected areas

467 of 468 regions had **no exclusion mask at all**, on a catalogue that
includes coasts where spearfishing is banned outright. Two layers now:

**Automatic.** OSM protected areas are fetched per region (cached forever)
and rasterised into the existing exclusion gate. IUCN management categories
**1a, 1b, 2 and 3 are masked**; **4, 5 and 6 are named but not masked**,
because they routinely permit fishing — the Great Barrier Reef Marine Park is
a category 6, and blanking it would delete most of the usable water in
Queensland for no reason. Multipolygon relations are assembled properly:
Overpass returns the outer boundary as unordered way fragments, so they are
chained end to end and only closed rings are used.

**Curated.** OSM alone is not enough, and Bonaire proves it. OSM has the
individual reserves — King Willem-Alexander, Klein Bonaire, Lac Baai — but
**not the island-wide marine park that actually bans spearfishing**. Masking
gave 8.2% of the box when the true answer is "all of it". So `regions.json`
carries a hand-checked flag: **6 regions banned, 33 restricted.**

**A banned region publishes no waypoints at all.** Not a warning above a
list of thirty numbered coordinates — the coordinates are what gets used.
Conditions and the structure map still run, because knowing what the sea is
doing is not itself a problem.

```
bonaire            status=banned      general spots=0   species spots=0
galapagos          status=banned      general spots=0   species spots=0
poor-knights       status=banned      general spots=0   species spots=0
fernando-noronha   status=banned      general spots=0   species spots=0
novigrad           status=None        general spots=30  species spots=96
```

The app shows a red stop banner for banned, amber for restricted, and states
plainly that OSM marine coverage is incomplete: **no mask is not evidence of
no reserve.**

## 2. A safety notice on first open

Shown before anything else, acknowledged once, remembered in localStorage,
reachable again from Sources. It says four things a first-time user cannot
infer from a colour ramp:

- the model has **never been fitted against real dives** and can be
  confidently wrong
- legal data exists for **5 of 134 jurisdictions**; everywhere else means
  unknown, not permitted
- protected-area coverage is incomplete and a spot inside a reserve is
  possible
- **breath-hold diving kills experienced people** — shallow water blackout
  gives no warning; never dive alone

Verified to fit a 375 px phone (653 px card in an 812 px viewport) and to
scroll if it does not.

## 3. Attribution

A Sources section credits OpenStreetMap under ODbL **for the derived
coastline, wrecks and protected areas, not just the tiles**, plus OpenSeaMap,
EMODnet, GMRT, NOAA, Open-Meteo (CC BY 4.0) and Copernicus. The map's own
attribution line was extended to match.

## 4. Third-party services, used within their policies

**The base map moved off `tile.openstreetmap.org`.** The OSMF tile usage
policy is explicit that those servers are not for third-party applications;
"my app got popular" is not a defence, and it is a donation-funded resource
for something else. Now Carto's free basemaps, with attribution. The URL and
credit are in one `TILES` object at the top of the map setup — that is the
only thing to change if you later take a provider account.

**Overpass responses are cached in the browser and rate-limited.** This was
the one place the app could genuinely have abused volunteer infrastructure:
every press of *Scan structure* issued a fresh query, including re-scanning
the same coast. Now cached forever in the Cache API keyed by box, and capped
at one request per three seconds per tab. Server-side was already fine —
468 regions, once each, cached on disk.

## 5. Payload

`index.json` carried a `note` for every region that the app never renders.
Removed: **542 KB → 427 KB raw, 32 KB gzipped**, on a file every visitor
downloads before anything can happen. The note still travels in each
region's own `latest.json`, fetched only when that coast is opened.

## Verification

- **All 468 through `--selftest`, 6 workers: exit 0, no failures**, 1,086 s.
- 14 assertions over generated output: all pass.
- Live runs on Bonaire and Novigrad confirming the mask, the curated flag and
  the waypoint suppression.
- `index.html` parses; the gate, banner, Sources section and "show again"
  button all present and correct; both JS modules parse.
- Artifact 212 MB, unchanged.

## What is still true, and now said in the app rather than the README

- **The weights are guesses.** Nothing has been fitted. This is the largest
  remaining problem and only dive logs fix it — `calibrate.py` wants about
  30, blanks included.
- **129 of 134 jurisdictions have no legal pack**, including HR, which
  covers Novigrad, Zadar and Dubrovnik.
- **413 of 468 regions are auto-generated** — plausible bboxes and
  baselines, not surveyed ones.
- `max_swim_km` still gates nothing, because `entry_points` is empty. You
  parked that deliberately.

If you want one more thing before launch, make it the Croatian legal pack:
it is the only jurisdiction you personally dive, and it is the one gap where
the missing data is both small and entirely knowable.
