# v3

```
v3/
├── fish_finder.py
├── calibrate.py
├── sdb.py
└── s2_turbidity.py
```

Only the Python changed. Everything under `docs/`, plus `config.json`,
`regions.json` and `README.md`, is unchanged since **v2** — take those from
there. The newest folder containing a file is always the current version.

---

## Why there is a v3 at all

I said in v2 that the Python was "reviewed, not run" because there was no
interpreter on this machine. **That was wrong.** I had searched a handful of
standard install paths and found only the Microsoft Store stubs. A full
search of the drive turns up **Anaconda at `C:\Users\gaber\Anaconda`** with
Python 3.12.7, numpy, scipy and matplotlib already installed.

So I ran it. It failed on the first line of real work, and then again, and
neither failure was theoretical.

## Bug 1 — the model could not start on Windows at all

```
UnicodeDecodeError: 'charmap' codec can't decode byte 0x8d in position 13240
```

`Path.read_text()` with no `encoding` uses the platform default, which is
**cp1252** on Windows. `regions.json` is UTF-8 and contains em dashes in
region names. The file could not be read, so the program never started.

It works on the GitHub Actions runner because Linux defaults to UTF-8, which
is exactly why it survived this long: CI was green and the thing was simply
unrunnable on your own machine.

Every `read_text()` and `write_text()` across all four scripts now passes
`encoding="utf-8"` explicitly, and `calibrate.py` opens `dive_log.csv` the
same way — that file will carry accented species names and note text.

## Bug 2 — a region name could kill the run half way through

```
UnicodeEncodeError: 'charmap' codec can't encode character '\u010d'
```

`log()` printing "Split and Brač channel" to a cp1252 console. This one is
worse than it looks: it fired **after two regions had already written their
output**, so `docs/data/` was left half updated and `index.json` — written
only at the very end — never appeared at all. A log line must never be able
to do that. `sys.stdout` and `sys.stderr` are now reconfigured to UTF-8 with
`errors="replace"` at import time.

## Bug 3 — the daily run would have lost most of its coastlines

Running Novigrad for real produced:

```
coastline: 64 OSM way(s) applied; water now 54% of the box
wrecks: unavailable (HTTPError) - continuing without
```

The coastline succeeded and the wrecks were rate-limited — the *same* race I
fixed on the client in v2 but had left in place server-side. With all 55
regions enabled that is **110 Overpass requests in one run**, back to back.
Most of them would have been refused, and every refusal silently costs a
region its land mask or its wrecks.

Both now come from **one request per region**, through
`fetch_osm_context()`, with three mirrors and a backoff retry
(`overpass_query()`). The superseded `fetch_wrecks()` is deleted. Re-running
Novigrad with the cache cleared:

```
coastline: 64 OSM way(s) applied; water now 54% of the box
wrecks: none charted here          <- answered, not refused
```

## What is now actually verified

Not reviewed — executed.

**`--selftest`, all 55 regions, exit 0**, no traceback, no failure, no
warning. Plus 14 assertions over the generated output: every region wrote
`latest.json`, every depth band matches its region config, no spot shallower
than its region minimum, visibility never pins at the ceiling in any region,
no hour claims "no daylight left" while light still follows it, `model.json`
bundles all twelve biome packs and carries the depth band, weights, baseline
and the affinities the client species maps need.

**Four regions run for real, with live network** — EMODnet at 86–90 m,
Overpass, Open-Meteo:

| region | coastline | visibility | note |
| --- | --- | --- | --- |
| novigrad | 64 ways, water 54% | 7 m | exclusions 2.6% masked |
| versilia | 6 ways, water 45% | 6 m | 2 wrecks found |
| faro-ria-formosa | 82 ways, water 56% | 7 m | the barrier islands |
| cascais | 17 ways, water 70% | 4 m | flagged macrotidal, 3.3 m |

Visibility 4–7 m against the old model's flat 11 m, on real forecasts.

**The land question, checked directly on real output.** I rebuilt each
region's land mask from cache and measured every published spot against it:

| region | grid | spots | on land | closer than buffer | nearest to land |
| --- | --- | --- | --- | --- | --- |
| novigrad | 100 m | 12 | 0 | 0 | 600 m |
| versilia | 100 m | 1 | 0 | 0 | 300 m |
| faro-ria-formosa | 100 m | 30 | 0 | 0 | 500 m |
| cascais | 277 m | 30 | 0 | 0 | 277 m |

Versilia yielding a single spot is correct, not a bug: it is flat sand with
no structure, which is what the region note says to expect.

`python -m py_compile` passes on all four scripts.

## Notes

- `rasterio` is in `requirements.txt` and CI installs it, but it was absent
  from your Anaconda. The first Cascais run therefore fell EMODnet → GMRT →
  SRTM15+ and finished on the global tier at 416 m — the fallback chain
  working as designed. I installed rasterio into a scratch directory only;
  **your Anaconda is untouched** (numpy still 1.26.4).
- `docs/data/` has been deleted. It is gitignored, it was mixed real and
  synthetic output by the end of testing, and CI regenerates it.
- `legal/HR.json` is still missing, and the real runs confirm the
  consequence: `legal: no rules pack for jurisdiction HR - sizes and seasons
  will not be shown`. Novigrad, Zadar and Dubrovnik are all affected.
