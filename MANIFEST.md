# v31 — time of day, and the model value that goes with it

```
v31/
├── docs/index.html          <- supersedes v30
└── worker/dive-relay.js     <- supersedes v27 (REDEPLOY the Worker)
```

Nothing else changes. `config.json` v30, `fish_finder.py` v28, `sw.js` v25.

---

## Why this is the field that matters

Every attempt to score the first six dives had to average conditions across
daylight hours, because not one of them recorded a time. A day contains both
8 m of morning calm and 2 m of afternoon chop, so averaging is close to
asking the model nothing at all. That is most of why six dives could not tell
us whether the forecast works.

## What was added

**A `Time in` field**, prefilled from the phone's own clock.

**And the part that is easy to miss:** `model_viz_m` is now taken from the
forecast hour of the DIVE, not from whenever the form happened to be open.
Logging an evening dive over breakfast used to record breakfast's visibility
as "what the model predicted" - then fed that into calibration as though the
model had forecast it for the dive. It was comparing the model against a
number it never produced for that moment.

Verified against two hours of one real forecast:

| hour | model said | recorded |
| --- | --- | --- |
| 06:00 | 6.5 m | **7** |
| 21:00 | 7.6 m | **8** |

Same form, same day, different hour, different recorded prediction.

## The migration problem, and how it is handled

Adding a column to a file that already exists is where data gets destroyed.
`time` is therefore **appended at the end**, not inserted after `date` where
it belongs semantically - a new column in the middle silently shifts every
value in every existing row.

`reconcileHeader()` then upgrades an older file: if the existing header is a
**prefix** of the current columns, it rewrites the header and pads the old
rows. If it is anything else, it **refuses** rather than guessing, because a
wrong guess corrupts the only copy.

Tested against a genuine 10-column file:

```
before  date,...,result,notes
        2026-08-21,...,dentex,1,old row

after   date,...,result,notes,time
        2026-08-21,...,dentex,1,old row,          <- padded, nothing shifted
        2026-08-25,...,sargus,1,dawn dive,06:30   <- new row
        2026-08-27,...,sargus,1,dawn dive,        <- time is optional
```

`25:00` and unpadded `6:30` are both rejected with
`400 time must be HH:MM, 24 hour`.

## One repair worth recording

The first version of `reconcileHeader` shipped with `"\n"` flattened into
real newlines inside its string literals, which broke the module outright -
the import failed with `SyntaxError: Invalid or unexpected token`. Caught by
running the actual file rather than reading it. Repaired, and the regression
now asserts that no string literal contains a raw newline.

## After uploading

**Redeploy the Worker** with the new `dive-relay.js` - the app will start
sending `time`, and until the Worker knows the column it will simply be
dropped. Everything else is a normal push.

Your `dive_log.full.csv` needs no migration: `calibrate.py` reads only
`viz_m` and `model_viz_m`, so a missing `time` column is harmless.

## Verified

12/12 assertions, 36-region selftest, 0 failures, plus the browser tests above.
