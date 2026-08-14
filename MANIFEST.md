# v5

```
v5/
├── README.md
└── .github/workflows/update.yml
```

Two files. Everything else is unchanged since v4 (and `docs/structure.js`,
`docs/anywhere.js`, `docs/sw.js` since v2; `calibrate.py`, `sdb.py`,
`s2_turbidity.py` since v3). The newest folder containing a file is always
the current version of it.

---

## One bug, and it undid the main promise of v4

v4 said the cold cost is paid once per region. With the cache key it
shipped, that was **not true for the activation path I told you to use**.

`actions/cache` skips its save step entirely when the key hits exactly. The
key was `bathy-${{ hashFiles('regions.json', 'config.json') }}`, and turning
a group on through the `FISH_REGIONS` repository variable changes no file —
that is the whole point of it. So:

1. set `FISH_REGIONS=med-west`, key unchanged → **exact hit**
2. run downloads 38 cold bathymetries, ~20 minutes
3. post-step skips the save, because the key hit
4. next run restores the *old* cache and downloads all 38 again

Permanently, every eight hours. The same trap applied to any region that
failed on one run and succeeded on the next: the success was never kept.

The key is now `bathy-${{ github.run_id }}`, which can never hit exactly, so
the cache is always written back. `restore-keys: bathy-` still restores the
most recent previous one.

## What is cached, measured

| | per region | refetched |
| --- | --- | --- |
| bathymetry grid | ~1,060 KB | never |
| OSM coastline | ~370 KB | never |
| charted wrecks | ~1 KB | never |
| Copernicus profile | small | daily, only if credentials are set |
| **Open-Meteo conditions** | — | **every run — it is the forecast** |

~1.4 MB a region, so 264 regions is about **386 MB** — well inside GitHub's
10 GB per-repository cache limit. Entries are evicted least-recently-used
past that, and a couple of weeks of history fits.

## A way to see it going wrong

The "Report output size" step now prints the cache too:

```
--- cache (must GROW when new regions run, then stay flat) ---
386M    cache
bathymetry grids cached: 264
coastlines cached:       264
```

If that count is not growing after you enable a group, the cache is not
being written and every run is paying full price. That is the single number
worth glancing at after the first run of a new group.

## Verification

Workflow YAML parses; cache key, restore-keys, 8-hourly cron, `FISH_REGIONS`,
`FISH_JOBS`, timeout and the new cache report all confirmed present. No
Python or frontend changes, so the v4 test results stand.
