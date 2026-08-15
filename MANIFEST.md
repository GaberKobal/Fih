# v16 — the budget had to fit the cron, not the timeout

```
v16/
├── README.md
└── .github/workflows/update.yml
```

`fish_finder.py` current as of **v15** — no code change, the logic was right,
the number was wrong. `regions.json` and `docs/index.html` v14.

---

## Yes, you can run all 800 now. One correction first.

v15 set `FISH_MAX_MINUTES` to 300 to stay clear of the 360-minute job kill.
That was the wrong constraint to measure against:

```yaml
- cron: "40 3,7,11,15,19,23 * * *"   # every 240 minutes
concurrency:
  cancel-in-progress: true
```

Runs are **four hours apart and cancel each other**. A 300-minute run is
still working when the next one fires, so during a cold build **every run
would be killed by its own schedule at ~240 minutes** — never reaching the
clean stop, and putting the cache save back in exactly the jeopardy v15
existed to remove.

**`FISH_MAX_MINUTES` is now 180**, leaving an hour for cache restore
(~1.4 GB at 800), the 334 MB artifact upload, and the deploy. The 360-minute
timeout stays as the backstop.

## What actually happens when you set it to `all`

Modelled on the **measured** cold cost — 84.9 s/region at 6 workers, from
the 24-region live run in v14, not the older optimistic 32 s:

| run | re-walking warm | new cold | total |
| --- | --- | --- | --- |
| 1 | — | 127 | 127 |
| 2 | 5 min | 123 | 250 |
| 3 | 10 min | 119 | 369 |
| 4 | 15 min | 116 | 485 |
| 5 | 20 min | 112 | 597 |
| 6 | 25 min | 109 | 706 |
| 7 | 29 min | 94 | **800** |

**Seven runs, about 28 hours**, unattended. The site grows each run and is
live and correct throughout — it just has fewer coasts on it at first.

Note the shape: the re-walk grows while the new-cold count shrinks. This
converges more slowly the larger the catalogue. At 800 it is comfortable; it
would not scale to 5,000.

## Set these three

| variable | value |
| --- | --- |
| `FISH_REGIONS` | `all` |
| `FISH_JOBS` | `6` |
| `FISH_MAX_MINUTES` | leave unset — 180 is the default |

Settings → Secrets and variables → Actions → Variables.

## Two things to check before you start

**Is the repository public?** Actions minutes are free and unlimited on
public repos. On a private one the cold build alone is roughly **7 runs x 3.5
h = 1,500 minutes against a 2,000/month allowance**, and the steady state
after that is ~4,500 a month. Private is not viable on the free tier.

**Watch the artifact.** The "Report output size" step prints it every run.
334 MB and 19,200 files at 800 regions, against a 1 GB Pages limit. If the
upload starts dragging, that is the ceiling arriving, and the fix is the
roadmap item: ship terrain rasters instead of rendered per-species maps.

## How to tell it is working

Each run should end with either a `deferred` line or a clean completion, and
**the cache must grow**:

```
computed 127 region(s) in 10783 s (84.9 s each, 6 worker(s))
deferred 673 region(s) - the 180 minute budget ran out. This is not an error...
bathymetry grids cached: 127
```

If `bathymetry grids cached` is not larger than the previous run, the cache
is not being saved and every run is starting cold — stop and look at that
before letting it churn for a day.
