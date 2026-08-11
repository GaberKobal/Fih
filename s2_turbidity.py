#!/usr/bin/env python3
"""
s2_turbidity.py — measured water clarity from Sentinel-2, as a reality check
on the modelled visibility.

Why this is a separate script and not a daily input
---------------------------------------------------
Sentinel-2 revisits every ~5 days, and on this coast a good fraction of those
passes are clouded out. So you cannot plan Saturday's dive on it. What it IS
good for: telling you whether the visibility model is lying. The model says
"8 m today" every day with equal confidence and has never once been checked.
This gives you an actual measurement to compare it against, every week or so.

Method
------
Nechad et al. (2009) single-band turbidity from red reflectance (665 nm),
which is the standard semi-analytical retrieval for coastal waters:

    T = A * rho / (1 - rho / C)        [FNU]

restricted to pixels the scene classification calls water, with cloud,
cloud-shadow and cirrus pixels thrown out. Turbidity is then converted to a
rough horizontal visibility with a Secchi-style inverse relation. That last
step is the weak link — the conversion constant is a coastal rule of thumb,
not a calibration for the Adriatic. Fix it against your own dive log: the
`viz_m` column is exactly the data needed.

Setup (free, ~5 minutes)
------------------------
1. Register at https://dataspace.copernicus.eu
2. Create an OAuth client: User Settings -> OAuth clients -> Create
3. Put the id and secret in the environment, or as GitHub Actions secrets:
       CDSE_CLIENT_ID
       CDSE_CLIENT_SECRET

Usage
-----
    python s2_turbidity.py                 # most recent usable scene
    python s2_turbidity.py --days 30       # search further back
    python s2_turbidity.py --max-cloud 15
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
OUT = ROOT / "docs" / "data"

TOKEN_URL = ("https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
             "protocol/openid-connect/token")
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"
CATALOG_URL = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"

# Nechad et al. (2009), Sentinel-2 B04 (665 nm)
NECHAD_A = 366.14
NECHAD_C = 0.19563

EVALSCRIPT = """//VERSION=3
function setup() {
  // Per-band units go in a units ARRAY parallel to bands, inside a single
  // input entry. Two separate entries mean two DATASOURCES to Sentinel Hub,
  // which is why it went looking for "dataset with id: 1".
  return {
    input: [{
      bands: ["B04", "SCL"],
      units: ["REFLECTANCE", "DN"]
    }],
    output: {bands: 2, sampleType: "FLOAT32"}
  };
}
function evaluatePixel(s) {
  // band 0: red reflectance, band 1: scene class (6 = water)
  return [s.B04, s.SCL];
}
"""


def log(m):
    print(f"[s2] {m}", flush=True)


def get_token(cid, secret):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_secret": secret,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())["access_token"]


def post_json(url, token, payload, timeout=180, raw=False, accept=None):
    """POST JSON and get JSON (or bytes) back.

    The Accept header matters here: the Catalog API answers STAC search with
    application/geo+json, so asking for application/json alone gets a bare
    406 with no explanation.
    """
    if accept is None:
        accept = ("image/tiff" if raw
                  else "application/json, application/geo+json, */*")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read() if raw else json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Sentinel Hub puts a useful message in the body; surface it instead
        # of a bare status code.
        try:
            detail = e.read().decode("utf-8", "replace")[:600]
        except Exception:
            detail = ""
        raise RuntimeError(f"{url} -> HTTP {e.code} {e.reason}\n{detail}") from None


def find_scene(token, bbox, days, max_cloud):
    """Most recent Sentinel-2 L2A pass over the box under the cloud limit."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    # The CDSE catalog rejects the STAC `query` extension, so cloud filtering
    # happens here instead. Over two weeks that is at most a handful of
    # passes, so there is nothing to gain from server-side filtering anyway.
    res = post_json(CATALOG_URL, token, {
        "collections": ["sentinel-2-l2a"],
        "bbox": [bbox["lon_min"], bbox["lat_min"], bbox["lon_max"], bbox["lat_max"]],
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": 100,
    })
    feats = res.get("features", [])
    log(f"catalog: {len(feats)} passes in the last {days} days")
    if not feats:
        return None

    scored = []
    for f in feats:
        pr = f.get("properties", {})
        cloud = pr.get("eo:cloud_cover")
        if cloud is None or cloud > max_cloud:
            continue
        scored.append((pr["datetime"], cloud))
    if not scored:
        clouds = sorted(f.get("properties", {}).get("eo:cloud_cover", 100)
                        for f in feats)
        log(f"none under {max_cloud}% cloud - cleanest was {clouds[0]:.0f}%. "
            "Raise --max-cloud or --days.")
        return None

    scored.sort(reverse=True)                 # most recent first
    when, cloud = scored[0]
    return {"datetime": when, "cloud": cloud}


def fetch_scene(token, bbox, when, res_m=60):
    lat0 = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    import math
    w = int((bbox["lon_max"] - bbox["lon_min"]) * 111320
            * math.cos(math.radians(lat0)) / res_m)
    h = int((bbox["lat_max"] - bbox["lat_min"]) * 111320 / res_m)
    w, h = min(w, 2500), min(h, 2500)
    day = when[:10]
    tif = post_json(PROCESS_URL, token, {
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

    import rasterio
    with rasterio.MemoryFile(tif) as mem, mem.open() as ds:
        red = ds.read(1).astype(float)
        scl = ds.read(2).astype(float)
    return red, scl


def scene_depth(bbox, shape, cfg):
    """Water depth on the scene's own grid, from the cached EMODnet DTM.

    Needed because Nechad assumes OPTICALLY DEEP water. Over shallow shelf
    the seabed reflects red light back to the sensor and the retrieval cannot
    separate that from suspended sediment, so clear shallow water reads as
    violently turbid. Without this mask the number is meaningless here.
    """
    import fish_finder as ff
    b = bbox
    key = (f"{b['lon_min']}_{b['lat_min']}_{b['lon_max']}_{b['lat_max']}_"
           f"{cfg['grid_resolution_m']}")
    blats, blons, elev = ff.fetch_emodnet_bathymetry(
        b, cfg["grid_resolution_m"] / 111320.0, key)
    # scene rows run north -> south; regrid wants ascending latitude
    lats = np.linspace(b["lat_min"], b["lat_max"], shape[0])
    lons = np.linspace(b["lon_min"], b["lon_max"], shape[1])
    elev_g = ff.regrid(blats, blons, elev, lats, lons)
    return -elev_g[::-1, :]                      # positive metres, north first


def turbidity_from_red(red, scl, depth=None, min_depth_m=0.0, dark_pct=5.0):
    """Nechad turbidity over deep, cloud-free water, after a dark-pixel offset.

    Sentinel-2 L2A is atmospherically corrected by Sen2Cor, which is tuned for
    land. Over dark water it routinely leaves a large residual offset: measured
    red reflectance here came out near 0.044 across deep water, where clear
    water should be 0.001-0.005. Feeding that to Nechad gives 20-plus FNU
    everywhere, which is an artefact, not turbidity.

    Dark-pixel subtraction is the standard remedy: at 665 nm the clearest water
    in a coastal scene is very nearly black, so the low percentile of red
    reflectance over deep water is essentially all atmosphere. Subtracting it
    leaves the water signal.

    The consequence is honest and worth stating: turbidity becomes RELATIVE to
    the clearest water in the scene. Spatial contrast (plume against offshore)
    stays trustworthy; absolute values inherit the assumption.
    """
    water = (scl == 6)
    bad = np.isin(scl, [3, 8, 9, 10, 11])          # shadow, cloud, cirrus, snow
    ok = water & ~bad & np.isfinite(red) & (red > 0) & (red < NECHAD_C * 0.95)
    if depth is not None and min_depth_m > 0:
        deep = np.isfinite(depth) & (depth >= min_depth_m)
        log(f"depth mask: {int((ok & deep).sum())} of {int(ok.sum())} water "
            f"pixels are deeper than {min_depth_m:.0f} m")
        ok = ok & deep
    if ok.sum() < 200:
        return None, int(ok.sum()), None

    rho = red[ok]
    offset = 0.0
    if dark_pct > 0:
        offset = float(np.percentile(rho, dark_pct))
        log(f"dark-pixel offset: subtracting rho={offset:.4f} "
            f"(p{dark_pct:g} of deep water red reflectance)")
        rho = np.clip(rho - offset, 1e-5, None)

    t = NECHAD_A * rho / (1.0 - rho / NECHAD_C)
    return t, int(ok.sum()), offset


def turbidity_to_viz_m(t_fnu, k=15.0):
    """Rough horizontal visibility from turbidity.

    A coastal rule of thumb, NOT an Adriatic calibration. k is set so that
    1 FNU (clean offshore water) lands near 15 m, which is about right for a
    good Adriatic day. It is the one number to tune once you have dive-log
    viz values to compare against.
    """
    return float(np.clip(k / max(t_fnu, 0.05) ** 0.6, 0.3, 30.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--regions", default=str(ROOT / "regions.json"))
    ap.add_argument("--region", default=None,
                    help="region id from regions.json; default is the first enabled one")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-cloud", type=float, default=20.0)
    ap.add_argument("--k", type=float, default=15.0)
    ap.add_argument("--dark-pct", type=float, default=5.0,
                    help="percentile of deep-water red reflectance treated as "
                         "pure atmosphere and subtracted; 0 disables")
    ap.add_argument("--min-depth", type=float, default=15.0,
                    help="ignore pixels shallower than this; below about 15 m "
                         "seabed reflectance corrupts the retrieval")
    args = ap.parse_args()

    cid = os.environ.get("CDSE_CLIENT_ID")
    secret = os.environ.get("CDSE_CLIENT_SECRET")
    if not (cid and secret):
        log("no CDSE_CLIENT_ID / CDSE_CLIENT_SECRET in the environment - skipping")
        return

    cfg = json.loads(Path(args.config).read_text())

    # Regions each get their own scene and their own output folder.
    regions = json.loads(Path(args.regions).read_text()).get("regions", [])
    picked = ([r for r in regions if r["id"] == args.region] if args.region
              else [r for r in regions if r.get("enabled")])
    if not picked:
        log("no matching region in regions.json - skipping")
        return
    region = picked[0]
    cfg["bbox"] = region["bbox"]
    cfg["region_id"] = region["id"]
    out_dir = ROOT / "docs" / "data" / region["id"]
    log(f"region {region['id']}: {region['name']}")
    bbox = cfg["bbox"]

    token = get_token(cid, secret)
    scene = find_scene(token, bbox, args.days, args.max_cloud)
    if not scene:
        log(f"no scene under {args.max_cloud}% cloud in the last {args.days} days")
        return
    log(f"scene {scene['datetime'][:16]}, {scene['cloud']:.0f}% cloud")

    red, scl = fetch_scene(token, bbox, scene["datetime"])
    depth = None
    try:
        depth = scene_depth(bbox, red.shape, cfg)
    except Exception as e:
        log(f"no depth mask ({e.__class__.__name__}) - the number will be "
            "contaminated by seabed reflectance in shallow water")
    t, n, offset = turbidity_from_red(red, scl, depth, args.min_depth,
                                      args.dark_pct)
    if t is None:
        log(f"only {n} usable water pixels - scene not worth using")
        return

    med = float(np.median(t))
    p10 = float(np.percentile(t, 10))
    p75 = float(np.percentile(t, 75))
    p90 = float(np.percentile(t, 90))
    # Contrast is the robust product here: it survives the offset assumption,
    # because subtracting a constant does not change differences much.
    contrast = p90 - p10
    out = {
        "scene_datetime": scene["datetime"],
        "cloud_cover_pct": scene["cloud"],
        "water_pixels": n,
        "min_depth_m": args.min_depth,
        "dark_pixel_offset_rho": round(offset, 5) if offset else 0,
        "relative": bool(offset),
        # Relative, not absolute. Track these across dates; do not read them
        # as metres.
        "turbidity_relative_fnu": {"median": round(med, 2),
                                   "p10": round(p10, 2),
                                   "p75": round(p75, 2),
                                   "p90": round(p90, 2)},
        "contrast_fnu": round(contrast, 2),
        "k": args.k,
        "method": "Nechad et al. 2009 single-band (B04 665 nm), SCL water pixels",
        "caveat": ("Sen2Cor L2A leaves a large atmospheric residual over water, "
                   "so a dark-pixel offset is subtracted and turbidity is "
                   "RELATIVE to the clearest water in the scene. Spatial "
                   "contrast is reliable; absolute metres are not. Tune k "
                   "against the viz_m column in dive_log.csv."),
        "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "s2_turbidity.json").write_text(json.dumps(out, indent=1))
    log(f"relative turbidity median {med:.2f}, p90 {p90:.2f}, "
        f"contrast {contrast:.2f} FNU over {n} deep pixels")
    log(f"spread p10 {p10:.2f} to p90 {p90:.2f} - watch this and the median "
        "across dates rather than any single value")
    log(f"wrote {out_dir / 's2_turbidity.json'}")


if __name__ == "__main__":
    # This is an optional reality check, not part of the forecast. It must
    # never turn the daily run red.
    try:
        main()
    except Exception as exc:
        log(f"skipped: {exc.__class__.__name__}: {exc}")
