# v20 — counting the pages that get the search traffic

```
v20/
├── fish_finder.py
├── config.json
├── README.md
└── docs/index.html
```

`s2_turbidity.py`, `dive_log.csv` and the workflow current as of **v19**;
`regions.json` v17; `docs/sw.js` v18; `docs/structure.js` v10;
`docs/add-region.html` v12; `docs/anywhere.js` v8.

**Upload in ONE commit** — each push cancels the run before it.

---

## The gap

The 800 region pages added in v18 contained **no script tag at all**. Good
for speed, wrong for the purpose: those pages exist to attract search
traffic, so the visits you most want to see would have been the only ones
invisible. Views that reached the map would count; views that landed on
`/r/sardinia-nw/` would not.

They now send the same single GET on the same terms as the app: no
third-party script, no cookies, no storage, Do Not Track and Global Privacy
Control honoured, and nothing emitted at all when no endpoint is set.

The path reported is `/r/<id>` — **deliberately identical to what the app
already reports** for the same coast, so it reads as one row rather than
splitting in two. Search landings stay distinguishable anyway: they arrive
with an external referrer and in-app views do not.

The endpoint comes from `analytics.endpoint` in `config.json`; if that is
empty it is read straight out of `docs/index.html`, so the one line the
README documents still works and 800 generated files never carry a stale
copy. The run says which source it used, or warns that counting is off.

## The bug found while testing it

Watching a local server receive the beacon:

```
POST /count?p=/r/novigrad&t=... 501     <- before
GET  /count?p=/r/novigrad&t=... 404     <- after
```

`navigator.sendBeacon` **always issues a POST**. A GoatCounter-style
`/count` endpoint answers **GET**. So the transport was the one method the
endpoint does not accept.

This was not only my new code. **The app's own tracker preferred
`sendBeacon` too** ([docs/index.html](docs/index.html)), which means that
for as long as it has existed, every view it believed it counted was very
likely discarded at the far end — and it would have looked like working
code, because nothing on the page ever sees the response.

Both now use `fetch` with `keepalive`: a GET, and it still survives the page
being closed. `Image` remains the fallback for anything without `fetch`.

If you had ever set an endpoint and seen no numbers, this is why.

## Switched on

`docs/index.html` now carries the live endpoint:

```js
const ANALYTICS = { endpoint: "https://fishcounter.goatcounter.com/count", ... }
```

**Set in one file, not two.** The app reads its own constant; the page
generator finds the same value by reading `docs/index.html` when
`analytics.endpoint` in `config.json` is empty, which it deliberately is.
One line to change if the account ever moves.

### Why not GoatCounter's own snippet

Their onboarding suggests:

```html
<script data-goatcounter="..." async src="//gc.zgo.at/count.js"></script>
```

That is a third-party script on every page. The direct GET to `/count` is a
documented GoatCounter integration, records the same pageview, and keeps
three properties the snippet would cost: no third-party code, nothing to
declare in a consent banner, and an offline story where a failed request is
simply ignored. Adblockers may still block the goatcounter.com host, so
treat all counts as a floor rather than a total.

## Verified

**10/10 assertions**, plus a 36-region selftest, 0 failures.

| tested | result |
| --- | --- |
| no endpoint anywhere | no `<script>` in the page at all, and the run says counting is off |
| endpoint only in `docs/index.html` | picked up, logged as *from docs/index.html* |
| endpoint in both | `config.json` wins, logged as such |
| beacon in a real browser | **`GET /count?p=/r/novigrad&t=...`** observed at the server |
| hub page | reports `/r` |
| DNT / GPC | checked before anything is sent |
| third-party scripts | none; no `<script src>` on any page |
