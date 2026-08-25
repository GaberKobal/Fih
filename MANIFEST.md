# v30 — every setting now filled in

```
v30/
├── config.json              dive_relay_url
├── docs/index.html          CARTO_KEY
└── docs/add-region.html     CARTO_KEY (the same one)
```

Nothing else changed. `fish_finder.py` v28, `worker/` v27, `docs/sw.js` v25.

There are no blank settings left in the project.

## Verified in a browser

The map now requests tiles with the key attached:

```
https://b.basemaps.cartocdn.com/rastertiles/voyager/10/550/367.png
    ?key=cb1_255u_1_4382ef54d116fc3875415230
```

and the relay URL reaches point mode, which is the default view:

```
MODEL.dive_relay_url = https://dive-relay.gaber-kobal1.workers.dev
```

CARTO and OpenStreetMap attribution is intact in both files, which is a
condition of the free tier.

**What I could NOT verify:** that the watermark actually disappears. Tiles
fetched with and without the key came back byte-identical, which probably
means CARTO's CDN caches by path and ignores the query. You will see the real
answer on your own map once this is deployed.

9/9 assertions, 36-region selftest, 0 failures.

## Two blockers remain, neither of them in these files

1. **The Worker has no variables.** It answers `405 POST only`, so it is
   deployed and running the right code, but a POST returns
   `500 relay not configured`. Add `GITHUB_TOKEN` (Secret), `DIVELOG_REPO`
   and `ALLOWED_ORIGIN` under Settings -> Variables and Secrets, then
   **Deploy** - saving alone does not always push new bindings.

2. **Pages is still building from the branch.** Until Settings -> Pages ->
   Source -> GitHub Actions, and the committed `docs/data/` folder is
   deleted, none of this reaches the live site - the CARTO key, the relay
   URL, the Venice fix, the static pages. This is the one that blocks
   everything else.
