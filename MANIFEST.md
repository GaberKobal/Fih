# v8

```
v8/
├── fish_finder.py
├── config.json
├── regions.json
└── docs/
    ├── index.html
    ├── anywhere.js
    ├── structure.js
    └── add-region.html
```

`.github/workflows/update.yml` and `README.md` are current as of **v7**;
`docs/sw.js` as of v2; `calibrate.py`, `sdb.py`, `s2_turbidity.py` as of v3.

---

## The em dashes

Gone from every user-facing string in the app and in the model output, and
the sentences rewritten so they still read properly rather than just having
the character deleted. Both of the ones you quoted:

> Species here are a coarse worldwide approximation: the nearest matching
> ocean-basin reference list, rather than a pack tuned to this spot. They
> carry no legal size or season information at all.

> Tidal range 2.42 m, which is macrotidal. The 8 km tide model behind the
> timing is not adequate here; use a real tide table.

28 strings across `index.html`, `anywhere.js`, `structure.js`,
`add-region.html` and `fish_finder.py`, including the verdict reasons the
model itself produces (`dark, waiting for first light`). Verified by
rendering the page and counting: **0 em dashes in visible text.** They
remain in code comments, which nobody reads in a browser.

## The name

The page title was the hardcoded string `Novigrad — dive forecast`, which
stopped being true at region two. It is `Dive forecast` on load and becomes
`Cascais · dive forecast` as you switch coasts, so a browser tab, a bookmark
and a home-screen shortcut all answer "which coast is this".

I did **not** rename the GitHub repository: that is yours to choose and the
rename itself happens in GitHub settings, not in these files. When you pick
one, the only strings to update are `repo_issue_url` and `repo_edit_url` in
`config.json`.

---

## The visibility model was dimensionally wrong

It drove turbidity with `Hs³`, which contains no wave period and no water
depth. The quantity that actually lifts sediment is the near-bed orbital
velocity from linear wave theory, `u_b = πH/(T·sinh(kh))` with `k` from
`ω² = gk·tanh(kh)`:

```
sea state                            4 m     10 m     18 m     25 m
0.6 m,  4 s  wind chop             0.310    0.074    0.010    0.002
1.0 m,  4 s  wind chop             0.517    0.124    0.017    0.003
1.0 m, 11 s  groundswell           0.748    0.440    0.294    0.225
1.5 m, 13 s  Atlantic swell        1.137    0.683    0.474    0.375
```

A 1 m sea over 25 m of water stirs the bed **77× harder at 11 s than at
4 s**. `Hs³` scored both at exactly 1.000.

Measured on live forecasts, old model versus new:

| region | Hs | swell T | old | new | @5 m | @22 m |
| --- | --- | --- | --- | --- | --- | --- |
| novigrad | 0.13 | 1.9 s | 7.3 | 7.3 | 7.3 | 7.3 |
| malta | 0.19 | 4.1 s | 9.8 | 9.8 | 9.8 | 9.8 |
| cascais | 1.18 | 6.8 s | 4.7 | **6.0** | 4.6 | **7.3** |
| sagres | 1.08 | 6.2 s | 4.8 | **6.8** | 4.7 | **7.4** |

Calm Mediterranean days do not move, which is correct: nothing is stirring,
so nothing should change. Atlantic swell coasts move by 1.3–2 m, and for the
first time **22 m reads 2.7 m clearer than 5 m** — which is what groundswell
actually does, and something the old model could not express at all, since
visibility was one number for the whole region regardless of depth.

Visibility is now quoted *for* a depth (the middle of the region's `best_m`
band) and the app says so, because a clarity figure without a depth is
meaningless.

`stir_model: "hs_cubed"` in `config.json` restores the old behaviour for
comparison.

## Wind sea and swell, separated

The API has always offered `wind_wave_*` and `swell_wave_*` alongside the
combined field; we only ever fetched the combined one. Now used in three
places:

- **workability** judges chop and swell separately and takes the worse. A
  metre of 4 s chop and a metre of 12 s swell are not the same day on the
  surface, and averaging them said they were.
- **visibility** combines the two orbital velocities in quadrature, because
  they are variances and add in energy rather than linearly.
- **shelter** traces the swell ray from `swell_wave_direction`.
  `combined_shelter()` exists precisely for "a light onshore breeze with an
  old swell running in from a different quarter", and it was being fed the
  *combined* direction, which is dominated by whichever component is bigger.
  On the days the split mattered most, both rays were traced from roughly
  the same bearing.

## Current is now a reason to stay out

Nothing gated on it. `ocean_current_velocity` only ever fed the *movement*
reward, so a screaming spring tide read as **more** attractive rather than as
a warning. Workability now includes a soft gate (`current_good_ms` 0.25,
`current_bad_ms` 1.0 — about half a knot to two knots). Set
`current_bad_ms: 0` to switch it off.

## River discharge

Local rainfall is not the same signal: a catchment that emptied 200 km
inland arrives two days later under a clear sky, and the rain term never sees
it. Open-Meteo's flood API is free and keyless, cached per region per day.

Normalised against **the river's own median** over 60 days, because the
absolute figure means nothing across regions — the Mirna runs at half a cubic
metre a second and the Tejo at hundreds. Live on Cascais:

```
river: median flow 0.39 m3/s, now 1.05x normal, turbidity contribution up to 0.03
```

A normal day is barely penalised, which is right. Regions with no river at
the queried point return nothing and fall back to rainfall alone, which is
the correct answer for an island.

## The thermocline was six months wrong for a quarter of the catalogue

The inherited profile is North Adriatic: no stratification in winter, 8 °C of
it in July and August. Handed unchanged to **57 southern-hemisphere regions**
it put the summer thermocline on their winter, and handed to **80 tropical
regions** it invented an 8 °C drop over 25 m in water that is near-uniform to
well below diving depth. Between them that is most of the file.

`region_config()` now shifts the year by six months below the equator and
scales the drops down towards the tropics, applied only where a region has
not supplied its own numbers:

```
novigrad       lat  45.3   Aug drop 8.00 C   (region supplied its own)
tasmania-east  lat -42.9   Aug drop 0.00 C   seasons shifted six months
komodo         lat  -8.5   Aug drop 0.00 C   shifted; drops scaled to 0.25
dahab          lat  28.5   Aug drop 2.50 C   explicit Red Sea profile
```

Latitude alone gets the Red Sea wrong — it is subtropical by latitude but a
hot enclosed basin that barely stratifies in the dive band — so the seven
`red-sea` regions carry an explicit profile instead.

## Observed, reported, deliberately not scored

Tide state (flood/ebb/slack), 24-hour sea-temperature trend, moon
illumination and current direction are computed, put in the payload, and
shown in an "Also happening" line under the vitals. **None of them touches
the score.**

That is a deliberate choice rather than an omission. The existing weights are
already guesses and `calibrate.py` has nothing to fit against yet; adding
unfitted terms to an unfitted model does not make it more accurate, it makes
it more confident-looking, which is the failure this repo has avoided
everywhere else. They are surfaced so you can judge them and so `dive_log.csv`
records what the conditions actually were — and calibration can then decide
whether any of them predicts anything.

The three things that *were* wired into the score are corrections, not new
guesses: the orbital velocity replaces a wrong term with a right one, the
wind/swell split uses fields that were always meant to be there, and the
current gate closes a hole where a dangerous condition read as attractive.

## Habitat

Left disabled globally but no longer pointless where it is on: EUSeaMap is a
European product, so the request is only made inside the EMODnet footprint
rather than firing 264 WFS calls a run that can only come back empty
elsewhere. Outside it the log says substrate is unknown rather than scoring
as though it were uniform.

## Verification

- `--selftest --select all`, **264 regions, 6 workers, exit 0, no failures**,
  706 s.
- 14 assertions over generated output: all pass.
- Live runs on Cascais, Sagres, Novigrad, Malta: river discharge, coastline,
  orbital visibility and the new payload fields all confirmed present and
  sane.
- Old-vs-new visibility measured on four real forecasts (table above).
- Python compiles; `anywhere.js` and `structure.js` parse; `index.html`
  parses and renders with **0 em dashes in visible text**.

## Still outstanding

Access points remain unbuilt at your request, so `max_swim_km` still gates
nothing in any region. `legal/HR.json` is still missing.
