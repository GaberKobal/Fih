# v14 — 800 regions, and the API limit removed

```
v14/
├── fish_finder.py
├── regions.json
├── README.md
└── docs/index.html
```

`config.json`, `docs/structure.js`, `docs/sw.js` current as of **v10**;
`docs/add-region.html` v12; `docs/anywhere.js` v8; the workflow v13.

---

## First: 800 at 4-hourly did not fit

Two calls per region, six runs a day, 800 regions = **9,600 Open-Meteo calls
against a 10,000 free allowance**. At the ceiling, with nothing spare for a
retry or a manual run. So the regions came second and this came first.

**Open-Meteo accepts comma-separated coordinate lists** and returns one
result per location. The part that made it usable here: `timezone=auto`
resolves **per location** — Novigrad comes back on Europe/Zagreb and Honolulu
on Pacific/Honolulu in the same response, which matters because every hour in
this model is local wall-clock time. Verified before writing any code, along
with the batch ceiling (200 locations succeeded; 100 is used).

| | before | after |
| --- | --- | --- |
| calls per run, 800 regions | 1,600 | **16** |
| calls per day, 4-hourly | 9,600 | **96** |
| share of the free tier | 96% | **1%** |

Any location the batch cannot answer for falls back to its own single
request, so one bad point never poisons a batch of a hundred.

Testing it immediately caught a bug: `now_i` — which hour is "now" — was
computed inside the single-fetch branch only, so every batched region failed
with `UnboundLocalError`. Hoisted so both paths compute it.

## Then: 468 → 800 regions

All disabled, `novigrad` still the only one on. **128 countries**, 164
jurisdictions, 22 groups.

`med-west` 100, `coral-triangle` 66, `caribbean` 61, `nw-europe` 59,
`med-east` 57, `indian-west` 52, `pacific-islands` 46, `iberia-atlantic` 38,
`adriatic` 36, `pacific-ne` 34, `atlantic-sw` 27, `australia-temperate` 25,
`africa-west` 25, `macaronesia` 24, `atlantic-nw` 24, `pacific-nw` 23,
`med-south` 21, `africa-south` 18, `pacific-se` 18, `nz` 16, `red-sea` 15,
`australia-tropical` 15.

New ground includes Socotra, the Cocos (Keeling) and Christmas Islands,
Jardines de la Reina, Tubbataha, Misool and Raja Ampat's south, the Marquesas,
Kiritimati, Isla Guadalupe, Prince William Sound, Bornholm and Helgoland,
Principe and Bioko, the Mergui archipelago and the whole Chilean and Peruvian
coast.

I aimed for 800 and got to 759 before running out of coasts I was confident
about; the last 41 are ones I could place properly rather than padding to hit
a round number.

Four boxes came out spanning separate island groups and were tightened:
Maio, Alphonse, Miyako and Rurutu.

## A bug the scale exposed

The first 800-run reported `hvar-vis FAILED: PermissionError ... index.json.tmp`.
The region was fine. `write_index()` — the incremental catalogue write added
in v6 — runs inside the per-region completion handler, so when the atomic
rename hit a transient Windows lock (OneDrive holding the file), **the
exception was attributed to whichever region happened to land at that
instant** and a good result was marked failed.

Now retried four times with backoff and swallowed if it still fails: the next
region to land rewrites the catalogue anyway, and the write at the end of the
run is the one that counts. Wrapped a second time at the call site so nothing
about publishing the index can ever sink a region.

Re-run clean: **800 regions, 0 failures.**

## Verification

- **All 800 through `--selftest`, 6 workers: exit 0, no failures**, 1,131 s.
- 14 assertions over generated output: all pass.
- **24 new regions on live network**, spread across every new area — Krk,
  Portofino, Naxos, Sousse, Figueira, Pico, Skye, Safi, Saldanha, Nuweiba,
  Khasab, Misool, Miyako, Gizo, Catalina, Arica, Acadia, Bimini, Recife,
  Wollongong, Whangarei, Socotra, Cocos Keeling, Mona. **0 failures**, water
  coverage 12–78%, and the batch fetch confirmed live: *24 regions from 2
  requests*.
- Structural checks on all 800: no duplicate ids, no malformed bbox, none
  over 130 km or under 2 km, every depth band ordered, every region grouped,
  every species pack present.

## Cost at 800

| | per region | 800 regions |
| --- | --- | --- |
| first ever run, 6 workers | ~32 s | **~7 h** |
| every run after that | ~2.5 s | **~33 min** |
| pure compute (`--selftest`) | 1.4 s | 19 min |
| output | 427 KB, 24 files | **334 MB, 19,200 files** |
| Open-Meteo | — | **16 calls/run, 96/day** |
| index.json every visitor loads | — | 755 KB raw, **54 KB gzipped** |

**The artifact is now the only real constraint.** 334 MB and 19,200 files
against a 1 GB Pages limit; upload and deploy time grow with it. Lifting that
needs the remaining roadmap item — shipping terrain rasters instead of
rendered per-species maps.

**A cold run of all 800 is about 7 hours and does NOT fit the 330-minute
timeout.** It has to be done a group at a time. That is no longer painful:
since v6 a timeout costs only the unfinished tail, `index.json` is rewritten
as each region lands, and every finished group is permanently cheap.

Set `FISH_REGIONS` cumulatively — `adriatic,med-west`, then add the next
group once the first has landed — and `FISH_JOBS` to 6.

## Unchanged and still true

745 of 800 regions are auto-generated: plausible bboxes and baselines, not
surveyed ones. 159 of 164 jurisdictions have no legal pack. The weights are
still guesses. All three are said in the app, not just here.
