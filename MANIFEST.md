# v6

```
v6/
├── fish_finder.py
├── docs/index.html
└── .github/workflows/update.yml
```

Everything else is unchanged: `config.json`, `regions.json`, `README.md`
since v4; `docs/structure.js`, `docs/anywhere.js`, `docs/sw.js` since v2;
`calibrate.py`, `sdb.py`, `s2_turbidity.py` since v3. The newest folder
containing a file is always the current version of it.

---

## The coloured square behind the verdict

It was a `<canvas>` sized in JavaScript from `clientWidth`/`clientHeight` at
draw time. Explicit pixel dimensions beat `inset: 0`, so the moment anything
reflowed afterwards the canvas stayed at its old size and became a
hard-edged rectangle sitting in the middle of the panel.

Three things reflow it, and I introduced the first one myself in v1:

- the Nunito webfont swapping in after first paint
- the vitals wrapping to a second row on a narrow screen — which is exactly
  what your screenshot shows
- a longer "limited by" line, or a caveat banner above it

Now it is a CSS `linear-gradient` on an `inset: 0` div. The layout engine
paints it at whatever size the panel currently is, so it cannot go stale —
no measuring, no device-pixel-ratio maths, and the resize handler is gone.

Two things on top, for the uniformity you asked for:

- **Opacity down from .30 to .17.** The full red-amber-green ramp at 30%
  over the panel colour came out an opaque olive that read as a block rather
  than a tint.
- **A downward scrim and a verdict-coloured left edge.** The text now always
  sits on the same ground regardless of what the outlook underneath is
  doing, and the band carries one deliberate cue in the same colour as the
  word — green for go, amber for marginal, red for stay home, grey for not
  now.

Verified at 375 px wide with the vitals wrapped to two rows: the gradient
measures 373×262 inside a 375×263 panel — full coverage, the difference
being the 3 px accent border and the 1 px bottom rule.

## Running all 264 overnight

Short answer: **yes, but set `FISH_JOBS` to 6, and it is still better done
group by group.**

The arithmetic, from measurements:

| workers | per region (cold) | 264 regions |
| --- | --- | --- |
| 1 | 55.6 s | 4.1 h |
| 4 *(the old default)* | ~37 s | **2.7 h** |
| 6 | 31.9 s | 2.3 h |
| 8 | ~27 s | 2.0 h |

The timeout was 180 minutes. At the default four workers a full cold run is
163 minutes — **seventeen minutes of headroom on a two-and-three-quarter-hour
job**, and Lofoten alone once took 160 seconds. That was going to fail.

Three changes so that it does not, and so that failing costs less:

**`index.json` is written as each region lands, not at the end.** It used to
be written once, after everything finished, so a killed run threw away every
region it had already computed — however many hours in it was. It is now
rewritten atomically (temp file, then rename, so it is never caught
half-written) each time a region completes, and carries `complete: false`
and `expected: 264` until the run finishes. Tested by killing a run 25
seconds in: 17 of 38 regions on disk, valid JSON, a site that renders.
Before this change that same kill left nothing at all.

**`model.json` is written before the first region.** It depends only on
config and the species packs, never on a forecast, and without it the
anywhere-on-Earth half of the app cannot do anything. No reason for it to
share the fate of a long region run.

**Overpass is limited to two concurrent queries** across the whole process,
regardless of worker count. Overpass hands each client about two slots and
429s beyond them, so six workers all asking for a coastline at once would
have spent most of the run being refused. The other workers compute while
they wait, so it costs almost nothing.

Timeout raised 180 → 330 minutes (the GitHub-hosted job ceiling is 360).

### What I would actually do

```
FISH_REGIONS = adriatic,med-west      # ~53 regions, ~30 min cold
```

then add a group or two each day. Every group you finish is permanently
cheap afterwards — 2.5 s a region — and you never have a single job holding
hours of unrepeatable work. If you would rather do the lot in one go, set
`FISH_JOBS` to 6 first, expect 2-3 hours, and know that a timeout now costs
you the unfinished tail rather than everything.

Watch the cache line in the run summary. If the count of cached grids is not
growing, the cache is not being written and every run is paying full price.

## Verification

- `--selftest --select all`, 264 regions, 6 workers: exit 0, no failures,
  503 s.
- 14 assertions over generated output: all pass.
- Interrupted-run test: model.json present, index.json valid with 17 of 38
  regions and `complete: false`.
- Verdict wash measured at 375 px with wrapped vitals: full coverage, no
  canvas references left in the page.
- `py_compile` clean, workflow YAML parses, `docs/index.html` parses.
