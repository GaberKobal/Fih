# v28 — Submit never worked in the default view

```
v28/
├── fish_finder.py       <- model.json now carries the relay URL
├── config.json          <- reformatted, dive_relay_url still empty
└── docs/index.html      <- supersedes v27
```

Everything else unchanged: `worker/` v27, `docs/sw.js` v25, the rest v25-v27.

---

## The bug

"No dive_relay_url set in config.json" appears even when it **is** set.

`dive_relay_url` was published only into each region's `latest.json`. But in
**point mode** `DATA` is built client-side by `forecastPoint()` and carries
only `region_id`, `spearfishing`, `spearfishing_note` and `species_source` -
none of the config. And point mode is the **default view**.

So Submit worked only if you had first opened a region, and silently refused
otherwise. The old `repo_issue_url` had exactly the same flaw, which means
this has been broken since the log-a-dive button existed.

`model.json` - the one file point mode always loads - now carries both, and
the app falls back to it:

```js
const relay = (DATA && DATA.dive_relay_url)
           || (MODEL && MODEL.dive_relay_url) || "";
```

Verified in the browser, in the default view:

```
mode           point (the default)
dataHasRelay   false      <- why it used to refuse
modelHasRelay  true
postedTo       https://dive-relay.example.workers.dev
message        Logged. Thank you - dives are what calibrate this.
```

## Your other reason for seeing this message

`dive_relay_url` is still **empty in the repo** - I checked
raw.githubusercontent. So even with this fix you will keep seeing the refusal
until you paste the Worker URL into `config.json` and let the model re-run.
Empty stays the safe default: it refuses rather than falling back to anything
public.

## Note on config.json

It has been rewritten with 2-space indentation by tooling. All 46 top-level
keys are intact, including every `_note`. Nothing but formatting changed, and
`dive_relay_url` is back to empty after testing.

## Verified

8/8 assertions, 36-region selftest, 0 failures.
