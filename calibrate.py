#!/usr/bin/env python3
"""
calibrate.py — replace the guessed weights with fitted ones.

The weights in config.json are invented. They look authoritative on a map
and they are not. This script fits them against your own dive log by
logistic regression, so the model learns what actually predicts fish on
your coast rather than what sounded plausible.

Log format (dive_log.csv), one row per dive, negatives included:

    date,lat,lon,depth_m,viz_m,wind_from_deg,result
    2026-07-12,45.3204,13.5691,17.5,6,120,1
    2026-07-12,45.3255,13.5602,11.0,6,120,0

    result   1 if you saw or took a decent target fish, 0 if you did not.
             Logging the blanks matters more than logging the good days —
             a model trained only on successes learns nothing.

Usage:
    python calibrate.py                 # fits and writes weights.json
    python calibrate.py --dry-run       # report only, write nothing
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

import fish_finder as ff

ROOT = Path(__file__).resolve().parent
MIN_DIVES = 30


def sample_at(grid, lats, lons, plat, plon):
    iy = int(np.clip(np.rint((plat - lats[0]) / (lats[1] - lats[0])), 0, len(lats) - 1))
    ix = int(np.clip(np.rint((plon - lons[0]) / (lons[1] - lons[0])), 0, len(lons) - 1))
    return float(grid[iy, ix])


def auc(y, p):
    """Rank-based AUC, ties handled."""
    order = np.argsort(p)
    ranks = np.empty(len(p), float)
    ranks[order] = np.arange(1, len(p) + 1)
    pos, neg = y == 1, y == 0
    n1, n0 = pos.sum(), neg.sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[pos].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def fit_logistic(X, y, l2=1.0):
    """Plain L2-penalised logistic regression, no scikit-learn dependency."""
    n, k = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])

    def nll(b):
        z = np.clip(Xb @ b, -30, 30)
        ll = np.sum(y * z - np.logaddexp(0.0, z))
        return -ll + l2 * np.sum(b[1:] ** 2)

    def grad(b):
        z = np.clip(Xb @ b, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        g = -Xb.T @ (y - p)
        g[1:] += 2 * l2 * b[1:]
        return g

    res = minimize(nll, np.zeros(k + 1), jac=grad, method="L-BFGS-B")
    return res.x, res


def fit_visibility(rows, cfg):
    """Fit a linear correction to the visibility model against logged dives.

    The model output is compared with the viz_m you actually recorded, and a
    simple a*model + b correction is fitted. That is deliberately modest: with
    a handful of dives you can honestly correct scale and offset, and nothing
    more. If the correlation is near zero the correction is refused, because
    rescaling a number that carries no information just makes it wrong with
    more confidence.
    """
    pairs = [(float(r["model_viz_m"]), float(r["viz_m"])) for r in rows
             if r.get("model_viz_m") and r.get("viz_m")]
    if len(pairs) < 4:
        print(f"\nvisibility: only {len(pairs)} dives have both a model value and "
              "an observed one - need at least 4 to fit anything")
        return None

    m = np.array([p[0] for p in pairs])
    o = np.array([p[1] for p in pairs])
    r = float(np.corrcoef(m, o)[0, 1]) if m.std() > 1e-9 else 0.0
    bias = float((m - o).mean())
    print(f"\nvisibility: {len(pairs)} paired dives")
    print(f"  model  mean {m.mean():.1f} m (range {m.min():.0f}-{m.max():.0f})")
    print(f"  actual mean {o.mean():.1f} m (range {o.min():.0f}-{o.max():.0f})")
    print(f"  mean over-prediction {bias:+.1f} m, correlation r = {r:+.3f}")

    if abs(r) < 0.3:
        print("  correlation is near zero - the model is not tracking reality, so "
              "no correction is written. Rescaling an uninformative number would "
              "only make it confidently wrong. Keep logging.")
        return None
    if m.std() < 1e-6:
        print("  model output has no variance across these dives - nothing to fit")
        return None

    a = float(np.cov(m, o, bias=True)[0, 1] / np.var(m))
    b = float(o.mean() - a * m.mean())
    resid = o - (a * m + b)
    print(f"  fitted correction: viz = {a:.3f} x model {b:+.2f} m, "
          f"residual sd {resid.std(ddof=1):.1f} m")
    return {"scale": round(a, 4), "offset": round(b, 3), "n": len(pairs),
            "r": round(r, 3), "residual_sd_m": round(float(resid.std(ddof=1)), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--log", default=str(ROOT / "dive_log.csv"))
    ap.add_argument("--l2", type=float, default=1.0)
    ap.add_argument("--species", default=None,
                    help="fit one species only, using its id from species.json")
    ap.add_argument("--fit-viz", action="store_true",
                    help="also fit a correction to the visibility model, using "
                         "the model_viz_m and viz_m columns of the log")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    # Explicit encoding: the dive log carries local species names and note
    # text, and open() would otherwise decode it with the platform default —
    # cp1252 on Windows, which cannot read UTF-8 and raises on the first
    # accented character.
    with open(args.log, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rows = [r for r in rows if r.get("lat") and r.get("result") not in (None, "")]
    if args.species:
        rows = [r for r in rows if r.get("species", "").strip() == args.species]
        print(f"filtered to species={args.species}")
    if not rows:
        raise SystemExit("dive_log.csv has no usable rows yet")

    y = np.array([float(r["result"]) > 0 for r in rows], float)
    print(f"{len(rows)} dives, {int(y.sum())} positive, {int((1 - y).sum())} blank")
    # Visibility is fitted from the log alone - it needs no bathymetry, so it
    # runs before anything that touches the network.
    viz_fit = fit_visibility(rows, cfg) if args.fit_viz else None
    if args.fit_viz and viz_fit is None and len(rows) < MIN_DIVES:
        print("\nSkipping the spatial fit as well - not enough dives yet.")
        return

    if len(rows) < MIN_DIVES:
        print(f"WARNING: under {MIN_DIVES} dives. The fit will be unstable and "
              "will overfit whichever spot you happened to visit most. "
              "Treat the output as a sanity check, not a model.")
    if y.sum() == 0 or y.sum() == len(y):
        raise SystemExit("need both successes and blanks in the log to fit anything")

    # Rebuild the static spatial terms from the cached bathymetry.
    bbox = cfg["bbox"]
    lats, lons = ff.build_target_grid(bbox, cfg["grid_resolution_m"])
    key = (f"{bbox['lon_min']}_{bbox['lat_min']}_{bbox['lon_max']}_"
           f"{bbox['lat_max']}_{cfg['grid_resolution_m']}")
    blats, blons, elev = ff.fetch_emodnet_bathymetry(
        bbox, cfg["grid_resolution_m"] / 111320.0, key)
    elev_g = ff.regrid(blats, blons, elev, lats, lons)
    land = ~np.isfinite(elev_g) | (elev_g >= 0)
    depth_m = np.where(land, np.nan, -elev_g)

    relief_s, slope_s, _, _ = ff.structure_terms(depth_m, land, lats, lons, cfg)
    terms = {"relief": relief_s, "slope": slope_s}

    # Shelter depends on the wind on the day, so it is only fitted if the log
    # records a wind direction.
    have_wind = all(r.get("wind_from_deg") for r in rows)
    shelter_cache = {}
    if have_wind:
        for r in rows:
            wd = round(float(r["wind_from_deg"]) / 30.0) * 30.0   # 12 sectors
            if wd not in shelter_cache:
                shelter_cache[wd], _ = ff.upwind_shelter(land, lats, lons, wd, cfg)
    else:
        print("no wind_from_deg column — shelter keeps its prior weight, unfitted")

    names = ["relief", "slope"] + (["shelter"] if have_wind else [])
    X = []
    for r in rows:
        plat, plon = float(r["lat"]), float(r["lon"])
        row = [sample_at(terms[n], lats, lons, plat, plon) for n in ("relief", "slope")]
        if have_wind:
            wd = round(float(r["wind_from_deg"]) / 30.0) * 30.0
            row.append(sample_at(shelter_cache[wd], lats, lons, plat, plon))
        X.append(row)
    X = np.array(X, float)

    beta, res = fit_logistic(X, y, l2=args.l2)
    p = 1.0 / (1.0 + np.exp(-np.clip(np.hstack([np.ones((len(X), 1)), X]) @ beta, -30, 30)))
    a = auc(y, p)

    print(f"\nconverged={res.success}  AUC={a:.3f}  "
          f"(0.5 = no better than guessing)")
    print("raw coefficients:")
    for n, b in zip(names, beta[1:]):
        print(f"  {n:>9}: {b:+.3f}")

    coefs = np.clip(beta[1:], 0, None)
    if (beta[1:] < 0).any():
        neg = [n for n, b in zip(names, beta[1:]) if b < 0]
        print(f"\nnegative coefficient(s) for {', '.join(neg)} — on your data that "
              "term predicts fewer fish, not more. Clipped to zero rather than "
              "inverted; if it persists past ~60 dives, drop the term.")
    if coefs.sum() <= 0:
        raise SystemExit("every term came out non-positive; nothing to write")

    weights = dict(cfg["weights"])
    fitted = {n: round(float(c / coefs.sum()), 3) for n, c in zip(names, coefs)}
    weights.update(fitted)

    out = {
        "weights": weights,
        "fitted_terms": names,
        "n_dives": len(rows),
        "n_positive": int(y.sum()),
        "auc": round(float(a), 3),
        "l2": args.l2,
        "note": "generated by calibrate.py; delete this file to fall back to config.json",
    }
    print("\nfitted weights:", json.dumps(fitted))
    if args.dry_run:
        print("(dry run, nothing written)")
        return
    if viz_fit:
        out["visibility_correction"] = viz_fit

    out["species"] = args.species
    name = f"weights_{args.species}.json" if args.species else "weights.json"
    (ROOT / name).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {ROOT / name}")


if __name__ == "__main__":
    main()
