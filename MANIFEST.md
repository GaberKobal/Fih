# v7

```
v7/
├── README.md
└── .github/workflows/update.yml
```

Two files. `fish_finder.py` and `docs/index.html` are current as of **v6**;
`config.json` and `regions.json` as of v4; `docs/structure.js`,
`docs/anywhere.js`, `docs/sw.js` as of v2; `calibrate.py`, `sdb.py`,
`s2_turbidity.py` as of v3.

---

## "If I push a changed index.html, will the run be short?"

As it stood: **short today, and about fifteen minutes once you turn the
regions on** — for a change that does not alter a single number in the
forecast.

The reason is structural rather than obvious. `docs/data/` is gitignored; it
only ever exists as a build artifact. So a push had nothing to publish unless
the model regenerated everything first. A one-line CSS edit therefore
recomputed every enabled region purely to have a site to deploy.

Two changes fix it.

**`docs/data` is now cached** alongside the bathymetry, so the previous run's
output is on disk at the start of every build.

**The build diffs the push.** If everything that moved is `docs/index.html`,
`anywhere.js`, `structure.js`, `sw.js`, `add-region.html`,
`region-species.json` or `README.md`, the model is skipped and the cached
forecast is republished with the new page. The log says what it published and
how old it is:

```
Frontend-only change — the model did not run.
  publishing 264 region(s) from a forecast 2.7 h old
  the next scheduled run refreshes it (every 8 h)
```

So a frontend push is **three to four minutes regardless of region count**,
nearly all of it artifact upload and Pages deploy, and it stays that way at
264 regions.

### It fails safe

The decision logic was unit-tested against twelve cases before it went in:

| changed | model runs? | |
| --- | --- | --- |
| `docs/index.html` | no | the case you asked about |
| several frontend files | no | |
| `docs/index.html` + `config.json` | **yes** | config changes the answer |
| `regions.json`, `fish_finder.py`, `species.json` | **yes** | |
| `docs/index.html`, no cached forecast | **yes** | nothing to republish |
| empty or unreadable diff | **yes** | fail safe |
| `docs/index.html.bak`, `other/docs/index.html` | **yes** | anchored patterns |

Anything it cannot confidently classify as frontend-only runs the model.

### One caveat

`concurrency` is `cancel-in-progress`, so pushing during a long cold run
cancels it. That is no longer expensive — since v6 `index.json` is written as
each region lands, so the regions already computed still publish and their
bathymetry stays cached — but the tail is left for the next run. Worth not
pushing in the middle of the first overnight build.

## Verification

- Workflow YAML parses; steps, conditions, cache paths, artifact path,
  timeout and cron all confirmed by parsing the file rather than reading it.
- Skip logic unit-tested: 12/12 cases, including every fail-safe path.
- The `Run model`, `Reusing the cached forecast` and Sentinel-2 steps are
  correctly gated on the decision; `Report output size` and the upload are
  not, so the artifact is published either way.
