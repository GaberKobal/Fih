# v21 — switching coast could put you back on the previous one

```
v21/
└── docs/index.html
```

**Supersedes the `docs/index.html` in v20** and includes everything that was
in it, the GoatCounter endpoint included. Every other file is unchanged:
`fish_finder.py`, `config.json`, `README.md` at v20; `s2_turbidity.py`,
`dive_log.csv`, the workflow at v19; `regions.json` v17; `docs/sw.js` v18.

---

## What was happening

Every loader is async and read the **global** `REGION`, including
`dataUrl`. Pick coast A, then pick B before A's `latest.json` has arrived,
and both loads are in flight. Whichever resolves LAST wins.

Reproduced against a server that delays one region by 2.5 s while serving
everything else normally:

```
renderOrder: ["split", "novigrad"]    <- the older load finished last and won
hash:        #split
heading:     "Split and Brač channel"
verdict:     "Novigrad, Istria"       <- the coast you had just left
```

The page ends up split-brain: the URL and the heading say one coast, the
forecast you are reading is the previous one. That is the "puts me back to a
previous location" you saw.

It is a race, so it is intermittent, and it is far worse on a phone where
the network is slower and more variable. That is exactly where you noticed
it and why the laptop seemed fine.

Contours, wrecks and the turbidity band had the same flaw and could paint
the previous coast's shapes and numbers onto the new map.

## The fix

Every navigation takes a ticket. After each await, a loader holding a stale
ticket returns instead of rendering. Region ids are captured into a local
rather than read back off the global once the response arrives.

Same test on the fixed build:

```
renderOrder: ["split"]                <- novigrad's stale load never rendered
```

## A second bug found in the same code

The region picker did `overlay=null` **without removing the layer**. That
orphaned the previous coast's score raster on the map, and `renderAll`'s own
`if(overlay) map.removeLayer(overlay)` then had nothing to remove, so the old
image stayed painted under the new one. The hashchange handler had always
done this correctly, which is why it only happened when switching from the
picker. Now it removes the layer.

Verified: after switching coast, exactly **1** `L.ImageOverlay` on the map.

## A note on how this was tested

The first harness did not reproduce the bug and appeared to exonerate the
old code. It was wrong: `socketserver.TCPServer` is single-threaded, so the
artificial delay blocked every request and serialised them, destroying the
very concurrency the race needs. The give-away was in the timings - the two
renders landed 15 ms apart, in order.

Rebuilt on `ThreadingHTTPServer` and confirmed genuinely concurrent (the
fast request returned in 228 ms while the slow one was still in flight), the
bug reproduced immediately.

## Verified

- **10/10 static checks** on the patched file
- the race, before and after, on a concurrent server
- ordinary sequential navigation still correct: hash, `REGION`, heading and
  verdict all agree on Cascais, and one image overlay on the map
- 36-region selftest, 0 failures, static pages and sitemap still generated
- the GoatCounter endpoint survives the edit
