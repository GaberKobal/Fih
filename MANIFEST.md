# v9

```
v9/
├── regions.json
└── README.md
```

Two files. `fish_finder.py`, `config.json` and everything under `docs/` are
current as of **v8**; `.github/workflows/update.yml` as of v7; `calibrate.py`,
`sdb.py`, `s2_turbidity.py` as of v3.

---

## 264 to 468 regions

204 added, all disabled, `novigrad` still the only one on. 22 groups now,
**104 countries**, 134 jurisdictions.

| group | was | now | | group | was | now |
| --- | --- | --- | --- | --- | --- | --- |
| med-west | 38 | **60** | | atlantic-sw | 9 | **16** |
| coral-triangle | 17 | **37** | | australia-temperate | 8 | **15** |
| caribbean | 21 | **35** | | atlantic-nw | 8 | **14** |
| med-east | 19 | **33** | | med-south | 7 | **13** |
| indian-west | 17 | **29** | | pacific-nw | 7 | **13** |
| nw-europe | 12 | **28** | | africa-west | 6 | **12** |
| pacific-islands | 15 | **27** | | africa-south | 6 | **11** |
| iberia-atlantic | 18 | **26** | | nz | 6 | **11** |
| adriatic | 15 | **23** | | red-sea | 7 | **11** |
| pacific-ne | 11 | **19** | | australia-tropical | 4 | **9** |
| macaronesia | 13 | **18** | | **pacific-se** | — | **8** (new) |

New ground worth calling out: **Iceland and the Faroes**, the **Humboldt
current** (Chile and Peru, a group that did not exist), **Bermuda**,
**Lord Howe and Norfolk**, the **Tuamotus**, **Alaska and Haida Gwaii**,
**Socotra-adjacent Oman**, the **Bijagos**, **Guadeloupe, Martinique and the
Grenadines**, and a lot more of Indonesia, the Philippines, Greece, Italy,
Spain and Croatia.

### The Humboldt group is honest about being an analogue

Chile and Peru have no species pack in this repository, and the Benguela one
is used because both are cold eastern-boundary upwelling systems. That is a
genuine ecological analogy, not a tuned pack, and every region in the group
says so in its note. It is the weakest species assignment in the file and it
is labelled as such rather than quietly presented as equivalent to the rest.

## Capacity: this is close to the ceiling

Measured on the full 468, not estimated:

| | per region | 468 regions |
| --- | --- | --- |
| first ever run, 6 workers | ~32 s | **~4.2 h** |
| every run after that, 6 workers | ~2.5 s | **~20 min** |
| pure compute (`--selftest`, 6 workers) | 2.2 s | 17 min |
| output | 466 KB, 24 files | **213 MB, 11,248 files** |
| Open-Meteo calls | 2 | 936 per run, 2,808 a day |

The tightest constraint is no longer compute, it is the **artifact**. 213 MB
and 11,248 files is fine, but Pages caps a site at 1 GB and the upload
degrades well before that, so **about 900 regions is the practical ceiling on
the current design** — not the ~1,600 that the Open-Meteo quota would allow.

The two changes that lift it are both in the roadmap: batching the conditions
fetch, and shipping terrain rasters instead of rendered per-species maps.

**A full cold run of all 468 is now ~4.2 hours at six workers against a
330-minute timeout.** It fits, but with little to spare, and only at
`FISH_JOBS=6` — at the default 4 it would not. Enable a group at a time
instead; each finished group is permanently cheap afterwards, and since v6 a
timeout costs only the unfinished tail rather than the whole run.

## Validation

- **All 468 through `--selftest`, 6 workers: exit 0, no failures**, 1,012 s.
- 14 assertions over generated output: all pass.
- **22 of the new regions run against live network** across every new area:
  Sibenik, Palermo, Santorini, Alexandria, Ericeira, Tenerife north, Iceland
  Reykjanes, Essaouira, Algoa Bay, Jeddah, Muscat, Alor, Amami, Marovo,
  Santa Barbara, Valparaiso, Bermuda, Negril, Salvador, Coffs, Marlborough,
  Mancora. All 22 completed with water coverage between 11% and 89% and
  sensible depth ranges.
- Structural checks on the whole file: no duplicate ids, no malformed bbox,
  no box over 130 km or under 2 km on a side, no missing species pack, every
  depth band ordered, every region grouped.

Four boxes came out spanning two separate island groups and were tightened
to a single locality: Shetland (was reaching Orkney), Addu (was reaching
Laamu), Rangiroa (was reaching Fakarava) and Kangaroo Island.

## The same caveat as before, and it now applies to 413 regions

These are **auto-generated from a table**. The bboxes, depth bands and
visibility baselines are reasonable starting values, not surveyed ones, and
every note says so. A box you intend to dive deserves a look at the map
first. A bad one fails loudly and alone: `run()` raises when under 5% of a
box is water, that region is logged and skipped, and the rest still publish.

134 jurisdictions now appear and only five have a legal pack (ES, FR, GR, IT,
PT), so the overwhelming majority show no sizes and no seasons at all. That
is a gap, never permission.

## Also in this version

The README header no longer says "European dive forecast", the cron line no
longer says 03:40 UTC, and the `--list` example is not four Croatian regions
any more. All three had quietly stopped being true.
