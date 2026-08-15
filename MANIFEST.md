# v13 — 4-hourly schedule

```
v13/
├── README.md
└── .github/workflows/update.yml
```

Two files. Everything else is current as of **v12**.

---

```yaml
- cron: "40 3,7,11,15,19,23 * * *"
```

Six runs a day instead of three. Offset to :40 because scheduled workflows
queue behind everyone else's on the hour and get delayed.

## What it buys, and what it does not

Less than it looks like, and worth knowing which is which.

**It does not fix staleness of "now".** The app already re-anchors the
current hour against the real clock, using the hourly series inside whatever
file it has (`reanchorNow()` in `docs/index.html`). A six-hour-old file still
reports the correct current conditions — that was fixed back in v1 and is
why the "no diveable light left today at midday" bug existed in the first
place.

**What it does buy** is a newer upstream model run and a forecast horizon
that reaches further forward. Open-Meteo's marine models refresh on their own
cycle, so some of these six runs will fetch data that has not changed.

## The two costs

**Open-Meteo quota is now the tight one.** Two calls per region per run, six
runs a day: 468 regions is **5,616 calls a day against a 10,000 free
allowance — 56%**. That halves the headroom: roughly **800 regions** before
the daily quota binds, down from 1,600 on the 8-hourly schedule. Still fine
at 468, but it is no longer the constraint you can ignore.

**Actions minutes, if the repo is private.** Six runs at ~25 minutes is
about **4,500 minutes a month against a 2,000 free allowance**. On a public
repository Actions minutes are free and unlimited, so this is a non-issue —
but if the repo is private this schedule does not fit, and the options are:
make it public, go back to 8-hourly, or run fewer regions.

Worth checking which yours is before the first billing surprise.

## Unchanged

The frontend-only push path still skips the model entirely, so editing the
page is still three or four minutes regardless of how often the schedule
runs. `cancel-in-progress` still means a push during a run cancels it, which
matters slightly more at six runs a day than at three — though since v6 that
costs only the unfinished tail.
