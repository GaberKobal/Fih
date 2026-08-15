# v15 — the timeout, and why raising it was not the fix

```
v15/
├── fish_finder.py
├── README.md
└── .github/workflows/update.yml
```

`regions.json` and `docs/index.html` current as of **v14**; `config.json`,
`docs/structure.js`, `docs/sw.js` v10; `docs/add-region.html` v12;
`docs/anywhere.js` v8.

---

## The ceiling is 360 and a cold 800 is ~7 hours

`timeout-minutes` is now **360**, up from 330. That is as high as it goes —
360 minutes is GitHub's hard kill for a hosted job. It does not make a cold
800 fit, and nothing can.

More importantly, **reaching that timeout is itself the danger**, which is
why the number alone was never the fix:

> `actions/cache` saves in a **post-job step**. A job killed by
> `timeout-minutes` does not reliably run its post steps. So a run that
> overruns discards the bathymetry it just spent five hours downloading, and
> the next run starts equally cold — forever.

Raising 330 to 360 moves that cliff by half an hour. It does not remove it.

## So the model stops itself

New `--max-minutes` / **`FISH_MAX_MINUTES`**, which the workflow sets to
**300** against the 360-minute kill. Once the budget is spent no further
region is started and the process exits **cleanly, code 0**: finished
regions are published, the cache is saved by a post step that actually runs,
and the rest are reported as **deferred, not failed**.

```
computed 214 region(s) in 17994 s (84.1 s each, 6 worker(s))
deferred 586 region(s) - the 300 minute budget ran out. This is not an
error: everything computed so far is published and cached, and the next
run starts with it already warm. Run again to continue.
  first not started: rab-pag, lastovo, kotor, vlore … and 582 more
```

The next run walks the same `regions.json` order, clears those 214 in ~9
minutes because their bathymetry is cached, and spends its budget going
deeper. **A cold 800 converges over a few scheduled runs, nothing
recomputed, nothing lost.** The 60-minute margin is not slack: a 334 MB,
19,200-file artifact upload is slow and the cache save sits behind it.

This retires the v14 instruction to enable groups by hand. `all` is now a
reasonable thing to set.

## Two details that decide whether this is safe

**The budget is checked before a region starts, never during it.** A
half-computed region would leave a partial folder behind, and the next run —
seeing a populated cache — would trust it.

**A run that defers everything must not write an empty catalogue.** Found
while testing: `index.json` is rewritten as each region lands, so a run that
started nothing would have written zero regions over the *previous*
catalogue restored from cache, blanking a working site. `write_index()` now
returns early when there is nothing to write, and that case exits 0 with an
explanation rather than the "every region failed" error, which would have
been a lie.

## Verified

Four behaviours, each run for real:

| | result |
| --- | --- |
| no budget (control), 3 regions | 3 computed, unchanged behaviour |
| partial budget, 36 regions | **9 computed, 27 deferred, 0 failed**, exit 0, `complete: false` |
| budget already spent | 0 started, **index.json byte-identical**, exit 0 |
| budget with room | 36/36, `complete: true` |
| `--max-minutes 0` | still means unlimited |

Plus **13/13 assertions** on output from 8 regions across 8 groups (Novigrad,
Cascais, Socotra, Gizo, Arica, Skye, Bimini, Miyako), and the workflow YAML
parsed and asserted.

No frontend change needed: the app never reads `complete`, so a partial
catalogue renders as a smaller one — which was already the behaviour when a
run was killed.

## Still true

745 of 800 regions are auto-generated. 159 of 164 jurisdictions have no
legal pack. The weights are guesses. The artifact, at 334 MB and 19,200
files against a 1 GB limit, is now the only real ceiling.
