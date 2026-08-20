# v26 — your dive spots were public

```
v26/
├── dive_log.csv                        <- coordinates rounded
├── .gitignore                          <- ignores the private copy
├── docs/index.html                     <- supersedes v25
└── .github/workflows/dive-log.yml      <- supersedes v19
```

Everything else unchanged. `docs/sw.js`, `add-region.html` v25;
`fish_finder.py`, `README.md`, `update.yml` v25; `regions.json` v17.

---

## What was exposed

```
2026-08-02,45.3397638,13.5380641,10,10,12,273,dentex,1
```

Seven decimals is centimetre precision. Anyone could read your exact spot off
Novigrad, what you took there, and on what date. And your "log a dive" button
would have done the same to every stranger who used it.

Reading the workflow properly, it leaked through **four** channels, not one:

| channel | how |
| --- | --- |
| `dive_log.csv` | the row, verbatim |
| the **commit message** | `git commit -m "dive log: ${{ steps.append.outputs.row }}"` - into public history, permanently |
| the **workflow log** | `print(f"appended: {row}")` - Actions logs on a public repo are public |
| the **issue body** | and closed issues stay readable |

## What it does now

**Precision is split.** The precise row is written to `/tmp` and pushed to a
**private repo**; only a copy rounded to ~1 km reaches the public CSV, the
commit messages and the logs. Nothing derived from the precise values is ever
printed or committed.

**With no private repo configured the precise row is DISCARDED.** The safe
default is losing precision, never publishing it. To keep it:

```
secret  DIVELOG_TOKEN    fine-grained, ONE repo, contents:write, nothing else
var     DIVELOG_REPO     e.g. GaberKobal/Fih-divelog
```

**The issue is redacted immediately** after parsing, before any step that can
fail - so a later error costs a row, not a spot.

Rounding costs the calibration nothing that matters: Open-Meteo's marine grid
is ~8 km, so two decimals is already far finer than the data the visibility
model is fitted against. `DIVELOG_PUBLIC_DP` changes it if you disagree.

## Two more things testing turned up

**The notes field is free text and goes to the public log verbatim.** No
amount of coordinate rounding helps if someone types "the reef north of the
marina". The box now says so: *"Public - do not name the spot."*

**The workflow double-filed every dive.** An issue created *with* a label
fires both `opened` and `labeled`, so it ran twice and appended the row twice.
Now serialised per issue, and the second run finds it closed and stops. That
explains any duplicate rows you may have seen.

## Your six existing dives

`dive_log.csv` is rounded to 2 dp here, and the precise original is kept as
**`dive_log.full.csv`**, which `.gitignore` now excludes. Keep that file - it
is your only full-precision copy.

**Git history still holds the originals.** Rounding the file does not remove
what is already in past commits, and they are on GitHub now. Options, in
order of effort: accept it (they are your own spots, and two weeks stale), or
rewrite history with a force-push, which breaks any clone. Your call - the
important thing is that it stops here rather than growing with every dive.

## Verified

**12/12 assertions.** The real parser was extracted from the workflow and run
against a genuine submission:

```
public CSV   2026-08-20,45.34,13.54,10,9,7,215,dentex,1,...
step output  total=16  summary=2026-08-20 - approx 45.34, 13.54 - dentex
private file 2026-08-20,45.3397638,13.5380641,10,9,7,215,dentex,1,...
```

Rounded everywhere it is published, precise only in the file that never gets
printed. Plus YAML valid, 36-region selftest, 0 failures.
