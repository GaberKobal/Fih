# v27 — the dive log is now entirely private

```
v27/
├── worker/dive-relay.js       <- new, the Cloudflare Worker
├── worker/wrangler.toml       <- new
├── docs/index.html            <- supersedes v26
├── config.json                <- dive_relay_url
├── fish_finder.py             <- publishes it to the app
├── calibrate.py               <- --log now defaults to the private copy
└── .gitignore                 <- both logs

DELETE from the repo:
  dive_log.csv
  .github/workflows/dive-log.yml
```

---

## Why a relay, and not just a token

Writing to a GitHub repo needs a token with write access, and **anything the
browser holds is public** - the page is downloaded by every visitor. Worse, a
fine-grained `contents:write` token also grants read, so publishing one would
hand a stranger the entire private log. The token has to live somewhere the
browser cannot see. Twenty lines of Worker is the smallest thing that
qualifies, and no third party ends up holding the data.

The browser POSTs **JSON**, not a CSV line. The Worker builds the row, so the
column order lives in one place and no untrusted CSV is ever parsed on the
write side.

## What is gone

| removed | why |
| --- | --- |
| `dive_log.csv` from the repo | it was public: dates, depths, species, results, notes |
| `.github/workflows/dive-log.yml` | it existed only to service the public-issue path |
| the GitHub issue submission | public by construction, and GitHub's docs say **"anyone with read access to a repository can view a comment's edit history"** - so redacting never removed anything |

Nothing about a dive is public now: no issue, no file, no commit message, no
log line. Full precision goes straight into your private repo.

`.gitignore` covers `dive_log.csv` and `dive_log.full.csv`, so neither can be
committed by accident. `calibrate.py --log` now defaults to
`dive_log.full.csv`; point it at a clone of the private repo when you want
everyone's dives.

## Deploying it

```
npm create cloudflare@latest -- dive-relay
# copy worker/dive-relay.js over src/index.js, and wrangler.toml alongside
npx wrangler secret put GITHUB_TOKEN     # fine-grained, ONE repo, contents rw
npx wrangler deploy
```

Then put the `https://...workers.dev` URL into `config.json` as
`dive_relay_url` and re-run the model so it reaches the app.

**Empty is the safe default.** With no URL the Submit button refuses and
points at Copy row, rather than falling back to anything public.

## Verified

**13/13 assertions**, plus the real Worker module run against a stubbed
GitHub API:

| | |
| --- | --- |
| valid dive | 200, full precision stored, header created on first write |
| second dive | 200, appended |
| bad date / lat 999 / lat missing / result 7 / year 1200 | **400**, each with its reason |
| non-allowlisted origin | **403** |
| commit messages ever used | only `dive log entry` |

And the one that matters, a CSV injection through the notes field:

```
sent    x", "y\n2026-01-01,0,0,0,0,0,0,evil,1,injected
stored  ...,"x"", ""y 2026-01-01,0,0,0,0,0,0,evil,1,injected"
lines   3 -> 4        one row added, not two
```

Newline stripped, quotes doubled, the whole payload trapped in one field.

In the app itself: with no relay configured Submit refuses and says so; with
one configured it POSTs exactly the ten expected fields and nothing else, and
no GitHub issue URL is constructed anywhere.

## Honest limits

The Origin allowlist stops another site's page posting from a visitor's
browser. It is **not** a security boundary - anything can be sent outside a
browser. The real defences are the strict field validation and a token scoped
to one repository with no other permission. If the endpoint is ever abused,
the damage is junk rows in a private CSV, which you can delete.

There is no rate limiting. Cloudflare's free tier absorbs far more than this
will ever see, but if you are ever targeted, a KV-backed counter is the
addition to make.
