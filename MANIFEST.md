# v19 — the dive log scored, and Sentinel-2 plugged in

```
v19/
├── fish_finder.py
├── config.json
├── s2_turbidity.py
├── dive_log.csv          <- corrected
├── README.md
└── .github/workflows/update.yml
```

`regions.json` and `docs/index.html` current as of **v17**; `docs/sw.js` v18;
`docs/structure.js` v10; `docs/add-region.html` v12; `docs/anywhere.js` v8.

**Upload everything in ONE commit** — each push cancels the run before it.

---

## 1. The dive log had the wrong dates, all of them

A spreadsheet drag had incremented the year down each dive's species rows:

```
dive 3: Aug 3. 2026, Aug 3. 2027, Aug 3. 2028, Aug 3. 2029
dive 5: Aug 4. 2026, Aug 4. 2027, Aug 4. 2028, Aug 4. 2029
dive 2: one row still FILL-ME
```

Each dive is several rows, one per species, so every row of a dive shares its
date and the drag only ever incremented. **15 of 15 rows corrected** and
normalised to ISO, which is what the app already writes.

## 2. What the six dives actually say

Scored against the conditions that really occurred on those dates:

| | bias | mean abs error |
| --- | --- | --- |
| the model as it was then | **+4.4 m** | 4.4 m |
| the model now | **-0.8 m** | 2.1 m |

The `baseline` fix worked. The second finding matters more:

| | spread across the six dives |
| --- | --- |
| model predicted | 7.2 to 7.4 m, a range of **0.2 m** |
| actually seen | 5 to 10 m, a range of **5 m** |

**The model output a constant.** Swell ran 0.07-0.11 m, wind 7-9 km/h and
rain was zero all week, so every input was flat while the water moved 5 m -
twice within 500 m of the same coast on consecutive days.

I am not quoting a correlation. With n=6 and no variance in the inputs it
carries no information in either direction.

**The rain change from v18 is untestable on this data.** There were 2.2 mm in
the whole window, five days before the first dive. Old and new differ by
0.1-0.3 m across all six. It remains defensible physics and unvalidated.

## 3. Sentinel-2, which was measuring the missing signal and throwing it away

`s2_turbidity.py` looks at how dirty the water is. Its number went nowhere.
It now feeds `visibility.baseline`, under constraints taken from the
measurement rather than invented:

- **The reading is RELATIVE.** The script says so itself: Sen2Cor leaves a
  large atmospheric residual, a dark-pixel offset is subtracted, spatial
  contrast is reliable and absolute metres are not. So a single scene is
  meaningless and only its deviation from what that coast normally reads is
  used. History accumulates in `cache/s2_history_<id>.json`, keyed by scene
  so re-running the model never double counts, and **nothing at all is
  applied until 4 scenes exist**.
- **Bounded at 0.15 of baseline**, about 1.5 m. A bad scene cannot dominate.
- **Not calibrated.** `baseline_per_fnu` is a timid guess, and calibrating it
  needs dives logged on days with a clear satellite pass.

Sampling was one region per run, which at 800 regions meant the second coast
in the list would never reach 4 scenes. Now `S2_MAX_REGIONS` (default 8),
**least-recently-sampled first** so coverage spreads, and one region failing
no longer takes the rest down.

**The arithmetic is faced in the README rather than hidden:** 8 a week is 416
samples a year against 800 regions needing several each. This is a feature
for a handful of home coasts, not for the catalogue.

## Verified

**10/10 assertions**, plus a 36-region selftest, 0 failures.

The wiring was tested by feeding synthetic scenes, since CDSE cannot run here:

| fed | result |
| --- | --- |
| scenes 1 to 3 | `+0.000`, "3 of 4 scenes needed" |
| 4th scene | active, at this coast's own median |
| a bloom, 3.50 vs usual 1.10 | **+0.144** baseline, dirtier |
| clear water, 0.20 | **-0.054**, clearer |
| the same scene again | history unchanged, not double counted |
| absurd 99 and -99 FNU | clamped to exactly +0.150 / -0.150 |
| no file / malformed file | `0.0` with a reason, no exception |

End to end through the real model: a planted bloom moved Novigrad from
**4 m to 3 m** and logged
`sentinel-2: baseline 0.35 -> 0.47 (+0.12)`; the control run with no history
was silent.
