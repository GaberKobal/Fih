# v33 — the watermark, and opening on the world

```
v33/
└── docs/
    ├── index.html    <- supersedes v31
    └── sw.js         <- supersedes v25. THIS is the watermark fix.
```

---

## The watermark: your key was fine, my cache handling was not

Measured against CARTO directly:

```
no key      17699 B   watermarked
real key    19919 B   clean
fake key    17699 B   watermarked
```

So the key works, and the live page has it. What you were seeing was a
**cached, pre-key `index.html`** requesting unkeyed tiles.

That is my mistake, and the file itself says so at the top: *"Bump this
whenever ANY file in SHELL_URLS changes."* `index.html` changed in v28, v30
and v31 and I never touched `VERSION`. The shell is served cache-first, so a
returning browser kept handing you the old page.

`VERSION` is now **v26**. That forces a fresh precache and the keyed page.

No tile purge is needed: the watermarked tiles were cached under URLs with no
`?key=`, so once the new page asks for keyed URLs they simply miss and refetch
clean. The dead entries age out under `TILE_CAP`.

## Opening on the world

It used to jump to `INDEX.regions[0]` - whichever coast is first in
`regions.json` - and immediately run a point forecast there. For you that
reads as your home spot. For everyone else it reads as an app about Istria
that has guessed wrong, and it spent an Open-Meteo call before anyone asked
for anything.

Now: zoom 2, centred on nothing in particular, with *"Tap any coast"* and a
line explaining what to do.

```
zoom 2, POINT null, REGION null, openMeteoCalls 0
```

## A regression I caused and caught

Opening with no point made the map **inert**. The click handler read:

```js
if(POINT !== null) loadPoint(...)
```

which was only ever true because startup auto-loaded a point. Removing that
startup left nothing to tap into.

What the guard actually means is *not showing a region* - in region mode a tap
should not throw away the structure map you are reading. Now:

```js
if(POINT !== null || REGION === null) loadPoint(...)
```

Verified both directions:

| | |
| --- | --- |
| tap from the world view | point placed at 45.2, 13.2 |
| tap while showing Cascais | region unchanged, still Cascais |

## Verified

8/8 assertions, 36-region selftest, 0 failures, plus the browser tests above.
