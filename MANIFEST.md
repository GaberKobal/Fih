# v18 — findable coasts, and rain that actually costs you visibility

```
v18/
├── fish_finder.py
├── config.json
├── README.md
└── docs/sw.js
```

`regions.json` and `docs/index.html` current as of **v17**; the workflow v16;
`docs/structure.js` v10; `docs/add-region.html` v12; `docs/anywhere.js` v8.

**v17 must be uploaded too, if it is not in yet.** And upload in ONE commit:
each push cancels the run before it.

---

## 1. Google could see one page

Regions are selected through the location hash (`#novigrad`). A fragment is
never sent to a server and is not indexed as a page, so 800 coasts of unique
content were, to a search engine, a single document titled "Dive forecast".
No sitemap, no robots.txt. Nobody searches for a dive forecast app; they
search for the conditions in front of them, and nothing here could match.

Every run now also writes:

```
docs/r/<id>/index.html    one real page per region
docs/r/index.html         a hub linking every coast, grouped by country
docs/sitemap.xml          every page, absolute URLs, lastmod
docs/robots.txt           pointing at the sitemap
```

Each page carries its own `<title>`, meta description, canonical link, Open
Graph tags, and **the verdict, visibility, sea temperature and best window
as text in the markup** rather than fetched by script — a crawler that has
to run the app to see the content usually will not. Open Graph matters for
the plan you actually have: a link pasted into a forum or a Facebook group
unfurls with the coast's name and today's conditions instead of a bare URL.

Set `site_url` in `config.json`. It must match the Pages address exactly,
including case, or the canonical tags point at nothing. Empty skips the
whole thing.

**Cost: ~3.3 MB and 801 files**, against an artifact already carrying 334 MB
and 19,200. Generation is wrapped, so nothing about discoverability can fail
a run that has already produced a good forecast.

Two things found by testing rather than by reading:

- `best_window` carries ISO `start`/`end`, not a label. My first version
  looked for a label and printed **"see the map"** on every page — the least
  useful thing a page about when to dive could say. Now formatted as
  `Thu 04:00 to 06:00`, and sea temperature is surfaced alongside it since
  it was already in the same record and people search for it.
- `sw.js` served the shell cache-first, which would have handed a repeat
  visitor a verdict several hours old with nothing saying so. Region pages
  now get the same network-first treatment as `/data/`.

## 2. Rain barely touched visibility

Measured before changing anything, at 12 m:

| | before | after |
| --- | --- | --- |
| 2 mm in 1 h | 0.3 m | **1.2 m** |
| 5 mm in 1 h | 0.7 m | **2.0 m** |
| 20 mm over 6 h | 2.5 m | **4.1 m** |
| 60 mm over 12 h | 6.5 m | 6.5 m |
| 20 mm storm, 24 h later | 1.3 m | **3.1 m** |

The cause is in `decay_memory`: it normalises so a **constant** input
converges to that input, so a short sharp downpour never approaches the
reference. 5 mm in an hour peaked at 0.12 against a reference of 1.2.

Lowering the reference is the obvious fix and it is the wrong one. Every
setting that made 5 mm bite also pinned a 20 mm storm and a 60 mm deluge to
the same floor, so the model lost the ability to tell a bad day from an
unthinkable one. The problem is the shape of the response, not its scale, so
the rain term is now **concave** — `rain_exponent` 0.55, alongside
`rain_ref` 1.2 to 1.0, `rain_weight` 0.65 to 0.68 and `rain_tau_h` 40 to 46.
That lifts the bottom of the range without spending the top: small rain
costs three to four times what it did, and 60 mm still costs the full range.

Setting `rain_exponent` to 1.0 restores the old straight-line behaviour.

**This is still an uncalibrated guess.** `config.json` continues to record
that the model checked r=+0.07 against 6 logged dives. Rain now behaves more
like the coast does; that is not the same as being right, and the fix is
still `calibrate.py --fit-viz` once there are enough logged dives.

## Verified

**12/12 assertions**, plus a 36-region selftest, 0 failures:

- sitemap parses against the sitemaps.org namespace, all URLs absolute and
  unique; robots.txt points at it
- a region page has a unique title, description, canonical and Open Graph,
  all four data cells present as markup, no external hosts, and a working
  link into the live map
- the hub links every region
- rendered in a real browser: the full text is there without scripts
- `sw.js` parses, and its `/r/` branch precedes the shell rule
- rain figures in the table above are measured through `visibility_series`
