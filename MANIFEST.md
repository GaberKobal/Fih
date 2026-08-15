# v12 — final review pass

```
v12/
├── fish_finder.py
├── README.md
└── docs/
    ├── index.html
    └── add-region.html
```

`config.json`, `regions.json`, `docs/structure.js` and `docs/sw.js` are
current as of **v10**; `docs/anywhere.js` as of v8;
`.github/workflows/update.yml` as of v7; `calibrate.py`, `sdb.py`,
`s2_turbidity.py` as of v3.

---

I went looking for what a public launch would expose that a private one did
not. Four things, and the first two were holes in the work I had just
finished.

## 1. Point mode had no ban warning at all

This is the one that mattered. `anywhere.js` never set `spearfishing`, so
the **default mode** — the one every first-time visitor lands in — showed
nothing when you tapped inside Bonaire or the Galápagos. Exactly where the
warning matters most, because there is no region page to have read first.

A point forecast now inherits the status of whatever configured region it
falls inside, and where it falls outside all of them it says so plainly
rather than letting silence read as an all-clear:

> **Protected areas are not checked for an arbitrary point.** Nothing here
> has been tested against reserve boundaries. Check before you get in.

## 2. The scan walked straight around the ban

The daily job refuses to publish waypoints in a banned region. *Scan
structure* did not — it computes spots in the browser, so a user could get
the coordinates the server had deliberately withheld. It now returns none
there and says why. The structure map still paints, because seeing that
ground is interesting was never the problem.

## 3. 468 regions across 104 countries is a haystack

The picker was a flat scroll through every coast on Earth grouped by
country. There is now a search box matching name, country, group or id, and
banned and restricted coasts are badged **in the list**, not only once you
open them.

Filter tested against all four fields: `parede` → cascais, `croatia` →
novigrad, `caribbean` → bonaire, `komodo` → komodo, no match → empty.

## 4. `add-region.html` was writing stale defaults

Submissions would have carried a **24 m** depth band, **no `group`** — which
makes a region invisible to `--select <group>`, the mechanism the whole
schedule now runs on — and **`enabled: true`**, quietly adding a stranger's
box to the daily job. Now 25 m, asks for a group, defaults to off. It was
also still pulling OSM's own tiles, which the v10 policy fix had missed.

## Final run

Everything above, plus v10 and v11, verified together:

- **All 468 regions, `--selftest`, 6 workers: exit 0, no failures**, 1,228 s.
- 14 assertions over generated output: all pass.
- **Waypoint suppression holds across the whole catalogue**: 6 banned
  regions, **0 waypoints published**, no violations. 33 restricted regions
  keep their spots and carry the amber warning.
- `index.json` 443 KB raw, **33 KB gzipped**, notes confirmed absent.
- Artifact 213 MB.
- `fish_finder.py` compiles; `index.html`, `add-region.html`, `anywhere.js`
  and `structure.js` all parse.
- No live use of `tile.openstreetmap.org` remains anywhere.

## What I did not do, and why

**I did not write `legal/HR.json`.** It is the largest remaining gap and it
covers the three Croatian regions including your home one — but the README
says HR was the *only* jurisdiction whose closed seasons you had actually
researched, so that file existed and was lost in the upload rather than
never written. Reconstructing it from memory would mean inventing fishing
law, which is the one thing in this repo that must not be guessed. Look for
the original; if it is gone, it needs an hour with the current *Pravilnik o
zaštiti riba i drugih morskih organizama* at ribarstvo.mps.hr.

The failure mode is at least safe: with no pack, every species reports
`seasons_unknown`, and the app says "closed seasons not researched" rather
than implying anything is open.

**Access points are still parked** at your request, so `max_swim_km` gates
nothing.

**The weights are still guesses.** Nothing has been fitted. That is the
honest headline for launch, it is the first thing the safety notice says,
and only dive logs change it.
