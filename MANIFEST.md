# v2

Files changed in this round, at the paths they belong at. Copy the tree over
the repository root.

```
v2/
├── README.md
├── config.json
├── regions.json
├── fish_finder.py
├── .github/workflows/update.yml
└── docs/
    ├── index.html
    ├── anywhere.js
    └── structure.js
```

`docs/sw.js` and `docs/add-region.html` are **unchanged since v1** and are not
duplicated here. If you have not applied v1 yet, take them from there. The
newest folder containing a file is always the current version of it.

---

## Model spots walking into town

The Viareggio screenshot showed marks 9–15 marching up the beach into Lido di
Camaiore. There were two independent causes.

**1. The coastline fetch was quietly losing a race.** The coastline and the
wrecks were two separate POSTs to the same public Overpass endpoint, seconds
apart. Overpass rate-limits per client, so the second call routinely came back
429 — and the coastline went first with its failure swallowed by a `.catch()`.
The common outcome was a scan with wrecks but **no land mask at all**. On a
straight sandy shore a 400 m depth cell averages the beach and the first few
hundred metres of streets to a negative elevation, so those cells were "water"
at a perfectly good depth and won the local-maximum test.

They are now **one request**, with three mirrors behind it and fall-through on
429/502/504. One request cannot rate-limit itself.

**2. Spots had no land gate of their own.** The colour wash was held back from
the shore by `overlay.land_buffer_m`; the spots were not, so they walked
inland alone whenever the mask was wrong. `findSpots()` on both sides now
refuses any mark within `land_buffer_m` of land, and any mark without a finite
depth at or past `depth.min_m`.

That second fix is deliberately independent of the first. Verified on a
synthetic Viareggio — 400 m cells, town reading as 5 m of water — that **with
the coastline fetch failing entirely**, 15 spots are found and 0 of them are
in town.

## Visibility was always the ceiling, not a forecast

Turbidity started at zero and the model only ever subtracted from it, so with
no recent swell or rain a calm dry day scored turbidity 0.003 and visibility
pinned at `viz_max_m`. It was not predicting 11 m — it was reporting the
maximum, every time. That is exactly what your screenshots showed.

`visibility.baseline` is what the water carries when nothing has happened:
plankton, current-driven resuspension, land input, fine sediment that never
settles. Measured against the same fixture:

| conditions | before | now |
| --- | --- | --- |
| flat calm, dry | 11.0 m | **7.5 m** |
| 1 m swell | 10.1 m | **4.7 m** |
| rain, no swell | 6.6 m | **1.0 m** |

Your dive log has actuals of 5–10 m against predictions of 12–13 m, so this
lands where the log does. `baseline` is the most region-specific number in the
model and `regions.json` now overrides it per coast: 0.08 in the Gulf of
Aqaba, 0.12 around Malta, Ibiza, Kas and Cyprus, 0.35 on the north Adriatic
shelf it was tuned on, 0.45–0.5 in the Versilia, Asturias and Brittany.

`wave_ref_hs` also came down 1.4 → 1.0. Cubed and normalised against 1.4 m,
anything under about 0.8 m of swell barely registered — wrong on a sandy shelf
where short chop lifts sediment.

Setting `baseline` to 0 reproduces the old behaviour exactly; that is asserted
in the tests, so the term is provably additive and disturbs nothing else.

## Radius

Reverted. "50 km" is a radius again, so the box reaches 50 km out and is
100 km across, and the menu says "50 km".

## Do regions enable themselves when clicked?

**No, and they cannot.** The app renders precomputed output, so a region that
never ran has nothing to show — clicking it would only produce "no forecast
yet". `enabled` is read by the daily GitHub Actions job, not by the browser.

So every region is enabled: **55 of them**, up from 20. Twenty-five new ones,
chosen for real spearfishing coasts and for spread:

- **Italy** — Versilia (the coast in your screenshot), Elba, Argentario,
  Capri/Amalfi, Salento, Siracusa, south-east Sardinia
- **Spain** — Ibiza, Menorca, the Strait of Gibraltar, Asturias
- **France** — Marseille and the Calanques, Quiberon
- **Croatia** — Zadar/Kornati, Dubrovnik
- **Greece** — Corfu, Halkidiki
- **Elsewhere** — Malta, Kas, Cape Greco, Dahab, La Paz, Florianópolis,
  Tasmania, Izu

Cost: the artifact is larger and the run longer, so `timeout-minutes` in
`update.yml` went 30 → 180. **A timeout would lose the whole run** — `index.json`
is written at the end, so a kill leaves the site on yesterday's data. Only the
first run after adding regions is slow; the bathymetry cache carries the rest.

Seventeen of the 55 jurisdictions have no legal pack in the repo (`MT`, `TR`,
`CY`, `EG`, `MX`, `BR`, `JP`, `HR` …), so those regions show no sizes and no
seasons. That is a gap, not permission, and the app says so.

## One world map

Leaflet repeats the basemap horizontally and does not wrap the coordinates
that come back with it — which is the root cause of the `235.6897` longitude
that made the US west coast look unsupported in v1. `noWrap` on both tile
layers plus `maxBounds` on the map means there is now exactly one Earth and
the view cannot leave it. `wrapLon()` stays as a belt for deep links.

## Verification

21 assertions, all passing, in a browser against synthetic fixtures:
visibility at three sea states plus the `baseline: 0` control, radius
semantics, the land gates with and without a coastline, spot depth floor,
shore clearance, the single combined Overpass request, way-to-centre
averaging, and mirror fall-through on 429.

The Python remains **reviewed, not run** — still no interpreter available
here. `python fish_finder.py --selftest` before pushing. Note it skips the
network, so the first real run is the actual test of the server-side
coastline mask and the new spot gates.

## Still outstanding

`legal/HR.json` does not exist. Novigrad, Zadar and Dubrovnik are all
jurisdiction `HR`, so all three run with no minimum sizes and no closed
seasons, and every species reads as open because nothing contradicts it. It
cannot be filled in by guessing — it has to come off the current *Pravilnik o
zaštiti riba i drugih morskih organizama* at ribarstvo.mps.hr.
