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
  return {
    input: [{bands: ["B04", "SCL"], units: "REFLECTANCE"}],
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
    res = post_json(CATALOG_URL, token, {
        "collections": ["sentinel-2-l2a"],
        "bbox": [bbox["lon_min"], bbox["lat_min"], bbox["lon_max"], bbox["lat_max"]],
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": 50,
        "query": {"eo:cloud_cover": {"lte": max_cloud}},
    })
    feats = sorted(res.get("features", []),
                   key=lambda f: f["properties"]["datetime"], reverse=True)
    if not feats:
        return None
    f = feats[0]
    return {"datetime": f["properties"]["datetime"],
            "cloud": f["properties"].get("eo:cloud_cover")}


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


def turbidity_from_red(red, scl):
    """Nechad turbidity over water pixels only."""
    water = (scl == 6)
    bad = np.isin(scl, [3, 8, 9, 10, 11])          # shadow, cloud, cirrus, snow
    ok = water & ~bad & np.isfinite(red) & (red > 0) & (red < NECHAD_C * 0.95)
    if ok.sum() < 200:
        return None, int(ok.sum())
    rho = red[ok]
    t = NECHAD_A * rho / (1.0 - rho / NECHAD_C)
    return t, int(ok.sum())


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
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-cloud", type=float, default=20.0)
    ap.add_argument("--k", type=float, default=15.0)
    args = ap.parse_args()

    cid = os.environ.get("CDSE_CLIENT_ID")
    secret = os.environ.get("CDSE_CLIENT_SECRET")
    if not (cid and secret):
        log("no CDSE_CLIENT_ID / CDSE_CLIENT_SECRET in the environment - skipping")
        return

    cfg = json.loads(Path(args.config).read_text())
    bbox = cfg["bbox"]

    token = get_token(cid, secret)
    scene = find_scene(token, bbox, args.days, args.max_cloud)
    if not scene:
        log(f"no scene under {args.max_cloud}% cloud in the last {args.days} days")
        return
    log(f"scene {scene['datetime'][:16]}, {scene['cloud']:.0f}% cloud")

    red, scl = fetch_scene(token, bbox, scene["datetime"])
    t, n = turbidity_from_red(red, scl)
    if t is None:
        log(f"only {n} usable water pixels - scene not worth using")
        return

    med = float(np.median(t))
    p25, p75 = (float(np.percentile(t, 25)), float(np.percentile(t, 75)))
    out = {
        "scene_datetime": scene["datetime"],
        "cloud_cover_pct": scene["cloud"],
        "water_pixels": n,
        "turbidity_fnu": {"median": round(med, 2),
                          "p25": round(p25, 2), "p75": round(p75, 2)},
        "observed_viz_m": round(turbidity_to_viz_m(med, args.k), 1),
        "viz_range_m": [round(turbidity_to_viz_m(p75, args.k), 1),
                        round(turbidity_to_viz_m(p25, args.k), 1)],
        "k": args.k,
        "method": "Nechad et al. 2009 single-band (B04 665 nm), SCL water pixels",
        "caveat": ("Turbidity is measured; the conversion to metres of "
                   "visibility is a rule of thumb. Tune k against the viz_m "
                   "column in dive_log.csv."),
        "fetched": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "s2_turbidity.json").write_text(json.dumps(out, indent=1))
    log(f"turbidity {med:.1f} FNU -> about {out['observed_viz_m']} m visibility "
        f"({n} water pixels)")
    log(f"wrote {OUT / 's2_turbidity.json'}")


if __name__ == "__main__":
    # This is an optional reality check, not part of the forecast. It must
    # never turn the daily run red.
    try:
        main()
    except Exception as exc:
        log(f"skipped: {exc.__class__.__name__}: {exc}")
