# v22 — the home-screen icon was the culprit

```
v22/
└── docs/
    ├── index.html              <- supersedes v21
    ├── sw.js                   <- supersedes v18, VERSION bumped
    ├── manifest.webmanifest    <- new
    ├── icon-192.png            <- new
    ├── icon-512.png            <- new
    └── icon-maskable-512.png   <- new
```

Everything else unchanged: `fish_finder.py`, `config.json`, `README.md` at
v20; `s2_turbidity.py`, `dive_log.csv`, workflow at v19; `regions.json` v17.

---

## Start here: I could not reproduce a code fault

The v21 race fix **is** deployed and correct — I checked the live file. So I
tested the live build against your exact gesture: sit on a point, then choose
a coast, with a real Open-Meteo call in flight.

```
renderOrder: ["POINT@10ms", "cascais@2172ms"]     -> ended on Cascais, correct
```

It did not revert. Two of my earlier test rigs were also broken in ways that
produced confident nonsense: the first used a single-threaded server, so the
artificial delay serialised every request and destroyed the concurrency the
race needs; the second had a stale `REGION`, so switching "to" a coast the
app was already on was a no-op. **My "a point forecast reliably finishes
last" explanation was wrong**, and I am flagging it rather than leaving it in
the record as if it had been established.

## What actually fits "always" and "from a while ago"

The app had **no web app manifest**, no `apple-mobile-web-app-capable` and no
apple-touch-icon. Without a manifest, "Add to Home Screen" saves **the exact
URL on screen at that moment, hash included**. Add the icon while looking at
a tapped point and that point is the shortcut, permanently. iOS then
relaunches a standalone web app from its saved URL every time the system
evicts it from memory, which is constantly.

That needs no bug at all, and it explains what a race cannot: why it is
*always* Venice, and why Venice is a coast you chose *a while ago*.

Demonstrated:

```
URL when you would have added the icon:  .../index.html#@45.4400,12.3300
where start_url now launches:            .../            (no hash)
hash leaks into launch:                  false
```

## The fix, and the bit you have to do yourself

`manifest.webmanifest` pins `start_url` and `scope` to the app root, so a
launch always begins with no hash. Plus proper icons, standalone display and
the iOS meta tags.

**A manifest cannot retroactively fix an icon that already exists.** The
shortcut on your phone still carries the Venice URL baked into it. **Delete
the icon and add it again** and it will launch clean from then on.

To confirm the diagnosis in ten seconds first: open
`https://gaberkobal.github.io/Fih/` typed fresh in the browser, with no hash.
If that opens on Novigrad while the icon still opens on Venice, this is it.

## Two real things fixed alongside

**`loadPoint` had an unguarded await before rendering.** v21 guarded the
model fetch and the dynamic import and missed `await forecastPoint(...)`,
which is the only slow one — it goes to Open-Meteo. I could not get it to
misbehave on the live build, so I am not claiming it was your symptom, but an
unguarded await before a render is a hole and it is now closed. Its `catch`
is guarded too, so a stale point that fails cannot post "Could not get a
forecast here" over a coast that has since loaded fine.

**The service worker version was never bumped when the shell changed.** The
shell is served cache-first, so a returning phone sees its cached
`index.html` first and only picks up a new one on a later load. v20 and v21
both changed `index.html` without touching `VERSION`, which slowed the fix
reaching your phone. Now `v22`, with a comment saying to bump it whenever a
shell file changes.

## Verified

**12/12 assertions**, plus a 36-region selftest, 0 failures.

- `start_url` resolves to the app root and is unaffected by the current hash
- manifest, all three icons and `sw.js` all serve with correct content types
- manifest linked, apple meta present, app still loads and renders
- 13 stale-guards in `index.html`, `forecastPoint` among them
- the GoatCounter endpoint and the static pages survive the edits
