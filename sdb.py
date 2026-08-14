#!/usr/bin/env python3
"""
sdb.py — satellite-derived bathymetry from Sentinel-2, at 20 m.

Why this is worth doing
-----------------------
EMODnet gives ~115 m and GMRT gives 100 m at best. Sentinel-2 images at 10 m,
and in clear water the blue/green band ratio tracks depth well to roughly
15-20 m — which is exactly the band you dive. Five times finer than the best
grid we otherwise have, over the only depths that matter.

The usual obstacle is calibration: turning a band ratio into metres needs
known depths. We already have known depths. The existing EMODnet or GMRT grid
becomes the training signal, so no survey is required — the satellite supplies
the detail and the chart supplies the scale.

Method
------
Stumpf and Holderied (2003) ratio transform, the standard approach for
optically shallow water:

    ratio = ln(n * Rrs_blue) / ln(n * Rrs_green)        n = 1000

The ratio is monotonic in depth and largely insensitive to bottom type, which
is why it beats single-band methods over mixed sand and rock. It is then
regressed linearly against the reference grid over water between min_depth and
max_depth, and the fit is applied everywhere.

Honesty rules built in
----------------------
  * The fit is REFUSED and nothing is written if R-squared is below the
    threshold. A bad fit dressed up as bathymetry is worse than no bathymetry.
  * It only works in optically clear water. Turbid coasts will fail the R2
    check, which is the correct outcome, not a bug.
  * Depths beyond max_depth are extrapolation and are masked out.
  * Reported RMSE is against the reference grid, so it measures agreement,
    not truth. If the reference is wrong the SDB inherits that.

Usage
-----
    python sdb.py --region novigrad
    python sdb.py --region azores --max-cloud 10 --min-r2 0.6
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

import numpy as np

import fish_finder as ff
import s2_turbidity as s2

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"

EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{
      bands: ["B02", "B03", "SCL"],
      units: ["REFLECTANCE", "REFLECTANCE", "DN"]
    }],
    output: {bands: 3, sampleType: "FLOAT32"}
  };
}
function evaluatePixel(s) {
  return [s.B02, s.B03, s.SCL];   // blue, green, scene class
}
"""


def log(m):
    print(f"[sdb] {m}", flush=True)


def fetch_bands(token, bbox, when, res_m):
    import math
    import rasterio
    lat0 = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    w = int((bbox["lon_max"] - bbox["lon_min"]) * 111320
            * math.cos(math.radians(lat0)) / res_m)
    h = int((bbox["lat_max"] - bbox["lat_min"]) * 111320 / res_m)
    if w > 2500 or h > 2500:
        raise RuntimeError(
            f"request would be {w}x{h} px; Sentinel Hub caps at 2500. "
            "Use a smaller bbox or a coarser --resolution.")
    day = when[:10]
    tif = s2.post_json(s2.PROCESS_URL, token, {
        "input": {
            "bounds": {"bbox": [bbox["lon_min"], bbox["lat_min"],
                                bbox["lon_max"], bbox["lat_max"]],
                       "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"}},
            "data": [{"type": "sentinel-2-l2a",
                      "dataFilter": {"timeRange": {"from": f"{day}T00:00:00Z",
                                                   "to": f"{day}T23:59:59Z"}}}],
        },
        "output": {"width": w, "height": h,
                   "responses": [{"identifier": "default",
                                  "format": {"type": "image/tiff"}}]},
        "evalscript": EVALSCRIPT,
    }, raw=True)

    with rasterio.MemoryFile(tif) as mem, mem.open() as ds:
        blue = ds.read(1).astype(float)
        green = ds.read(2).astype(float)
        scl = ds.read(3).astype(float)
        t = ds.transform
        lons = t.c + t.a * (np.arange(ds.width) + 0.5)
        lats = t.f + t.e * (np.arange(ds.height) + 0.5)
    if lats[0] > lats[-1]:
        lats = lats[::-1]
        blue, green, scl = blue[::-1], green[::-1], scl[::-1]
    return lats, lons, blue, green, scl


def stumpf_ratio(blue, green, n=1000.0):
    """ln(n*blue) / ln(n*green) — monotonic in depth, robust to bottom type."""
    b = np.clip(blue, 1e-5, None)
    g = np.clip(green, 1e-5, None)
    return np.log(n * b) / np.log(n * g)


def fit_depth(ratio, ref_depth, water, min_d, max_d, min_r2):
    """Linear fit of the ratio to reference depths over the usable band."""
    ok = (water & np.isfinite(ratio) & np.isfinite(ref_depth)
          & (ref_depth >= min_d) & (ref_depth <= max_d))
    n = int(ok.sum())
    if n < 500:
        return None, {"reason": f"only {n} usable training pixels", "n": n}

    x, y = ratio[ok], ref_depth[ok]
    if x.std() < 1e-9:
        return None, {"reason": "band ratio has no variance", "n": n}

    m1 = float(np.cov(x, y, bias=True)[0, 1] / np.var(x))
    m0 = float(y.mean() - m1 * x.mean())
    pred = m1 * x + m0
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = float(np.sqrt(ss_res / n))

    stats = {"n": n, "r2": round(r2, 3), "rmse_m": round(rmse, 2),
             "slope": round(m1, 3), "intercept": round(m0, 3)}
    if r2 < min_r2:
        stats["reason"] = (f"R2 {r2:.2f} below the {min_r2} threshold — this "
                           "water is probably too turbid for optical depth")
        return None, stats
    return (m1, m0), stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--regions", default=str(ROOT / "regions.json"))
    ap.add_argument("--region", required=True)
    ap.add_argument("--resolution", type=float, default=20.0,
                    help="metres per pixel to request (10 is native, 20 is safer)")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--max-cloud", type=float, default=10.0)
    ap.add_argument("--min-depth", type=float, default=2.0)
    ap.add_argument("--max-depth", type=float, default=18.0,
                    help="optical depth limit; beyond this the ratio saturates")
    ap.add_argument("--min-r2", type=float, default=0.5)
    args = ap.parse_args()

    cid, secret = os.environ.get("CDSE_CLIENT_ID"), os.environ.get("CDSE_CLIENT_SECRET")
    if not (cid and secret):
        log("no CDSE credentials in the environment - skipping")
        return

    base = json.loads(Path(args.config).read_text(encoding="utf-8"))
    region = next((r for r in ff.load_regions(Path(args.regions))
                   if r["id"] == args.region), None)
    if not region:
        raise SystemExit(f"no region {args.region!r}")
    cfg = ff.region_config(base, region)
    bbox = cfg["bbox"]

    # Reference depths: whatever bathymetry this region already uses.
    key = (f"{bbox['lon_min']}_{bbox['lat_min']}_{bbox['lon_max']}_"
           f"{bbox['lat_max']}_{cfg['grid_resolution_m']}")
    rl, ro, relev, provider, native = ff.fetch_bathymetry(
        bbox, cfg["grid_resolution_m"] / 111320.0, key, cfg)
    log(f"reference grid: {provider} at ~{native:.0f} m")

    token = s2.get_token(cid, secret)
    scene = s2.find_scene(token, bbox, args.days, args.max_cloud)
    if not scene:
        log(f"no scene under {args.max_cloud}% cloud in {args.days} days - "
            "SDB needs a clear day, try widening the window")
        return
    log(f"scene {scene['datetime'][:16]}, {scene['cloud']:.0f}% cloud")

    lats, lons, blue, green, scl = fetch_bands(token, bbox, scene["datetime"],
                                               args.resolution)
    log(f"bands: {blue.shape[0]}x{blue.shape[1]} at {args.resolution:.0f} m")

    ref = ff.regrid(rl, ro, -relev, lats, lons)       # positive-down depth
    water = (scl == 6) & ~np.isin(scl, [3, 8, 9, 10, 11])
    ratio = stumpf_ratio(blue, green)

    fit, stats = fit_depth(ratio, ref, water, args.min_depth,
                           args.max_depth, args.min_r2)
    log(f"fit on {stats['n']} pixels: " + ", ".join(
        f"{k}={v}" for k, v in stats.items() if k not in ("n", "reason")))
    if fit is None:
        log(f"REFUSED: {stats.get('reason')}")
        log("nothing written - the existing bathymetry stays in use")
        return

    m1, m0 = fit
    depth = m1 * ratio + m0
    usable = water & np.isfinite(depth) & (depth >= args.min_depth) & (depth <= args.max_depth)
    depth = np.where(usable, depth, np.nan)
    cover = float(usable.mean())

    CACHE.mkdir(parents=True, exist_ok=True)
    out = CACHE / f"sdb_{args.region}.npz"
    np.savez_compressed(out, lats=lats, lons=lons, depth=depth,
                        native_m=args.resolution, r2=stats["r2"],
                        rmse_m=stats["rmse_m"], scene=scene["datetime"],
                        reference=provider)
    log(f"wrote {out}")
    log(f"{cover*100:.0f}% of the frame has a usable depth "
        f"({args.min_depth:.0f}-{args.max_depth:.0f} m); everything outside that "
        "band falls back to the original grid")
    log(f"set bathymetry.use_sdb true for {args.region} to put it into service")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"failed: {exc.__class__.__name__}: {exc}")
