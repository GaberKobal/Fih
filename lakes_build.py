#!/usr/bin/env python3
"""Builds docs/lakes/ from lakes.json and the Natural Earth 1:10m lake outlines.

Natural Earth is public domain. The output is committed: outlines do not
change, so nothing at run time depends on a map server.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache" / "ne"
OUT = ROOT / "docs" / "lakes"
NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
USER_AGENT = "fish-forecast-lakes-build/1.0 (+https://github.com/GaberKobal/Fih)"


def ne_features(name, refresh=False):
    path = CACHE / f"{name}.geojson"
    if refresh or not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(NE_BASE + name + ".geojson", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as r:
            path.write_bytes(r.read())
    return json.loads(path.read_text(encoding="utf-8"))["features"]


def polygons_of(features):
    polys = []
    for f in features:
        g = f["geometry"]
        if g["type"] == "Polygon":
            polys.append(g["coordinates"])
        elif g["type"] == "MultiPolygon":
            polys.extend(g["coordinates"])
    return polys


def projector(lat0, lon0):
    kx = math.cos(math.radians(lat0)) * 111320.0
    ky = 110540.0

    def to_xy(ring):
        a = np.asarray(ring, dtype=float)
        return np.column_stack(((a[:, 0] - lon0) * kx, (a[:, 1] - lat0) * ky))

    return to_xy


def shoelace(xy):
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * abs(float(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1])))


def polygon_area(rings_xy):
    return shoelace(rings_xy[0]) - sum(shoelace(r) for r in rings_xy[1:])


def inside(px, py, rings_xy):
    result = np.zeros(px.shape, dtype=bool)
    for ring in rings_xy:
        x1, y1, x2, y2 = ring[:-1, 0], ring[:-1, 1], ring[1:, 0], ring[1:, 1]
        for i in range(len(x1)):
            crosses = (y1[i] > py) != (y2[i] > py)
            if not crosses.any():
                continue
            dy = y2[i] - y1[i]
            xint = (x2[i] - x1[i]) * (py - y1[i]) / (dy if dy != 0 else 1e-12) + x1[i]
            result ^= crosses & (px < xint)
    return result


def distance_to_boundary(px, py, rings_xy):
    best = np.full(px.shape, np.inf)
    for ring in rings_xy:
        x1, y1, x2, y2 = ring[:-1, 0], ring[:-1, 1], ring[1:, 0], ring[1:, 1]
        for i in range(len(x1)):
            dx, dy = x2[i] - x1[i], y2[i] - y1[i]
            length2 = dx * dx + dy * dy
            if length2 == 0:
                d2 = (px - x1[i]) ** 2 + (py - y1[i]) ** 2
            else:
                t = np.clip(((px - x1[i]) * dx + (py - y1[i]) * dy) / length2, 0.0, 1.0)
                d2 = (px - (x1[i] + t * dx)) ** 2 + (py - (y1[i] + t * dy)) ** 2
            best = np.minimum(best, d2)
    return np.sqrt(best)


def pole_of_water(rings_xy, bounds, steps=140):
    x0, y0, x1, y1 = bounds
    best = None
    for level in range(3):
        gx = np.linspace(x0, x1, steps)
        gy = np.linspace(y0, y1, steps)
        px, py = [a.ravel() for a in np.meshgrid(gx, gy)]
        wet = inside(px, py, rings_xy)
        if not wet.any():
            break
        px, py = px[wet], py[wet]
        d = distance_to_boundary(px, py, rings_xy)
        k = int(np.argmax(d))
        best = (float(px[k]), float(py[k]), float(d[k]))
        span_x, span_y = (x1 - x0) / steps * 3, (y1 - y0) / steps * 3
        x0, x1 = best[0] - span_x, best[0] + span_x
        y0, y1 = best[1] - span_y, best[1] + span_y
    return best


def build_lake(entry, baselines, refresh):
    src = entry["source"]
    feats = [f for f in ne_features(src["file"], refresh) if (f["properties"].get("name") or "") == src["name"]]
    if not feats:
        raise SystemExit(f"{entry['id']}: no feature named {src['name']!r} in {src['file']}")
    polys = polygons_of(feats)
    lats = [p[1] for poly in polys for ring in poly for p in ring]
    lons = [p[0] for poly in polys for ring in poly for p in ring]
    lat0, lon0 = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    to_xy = projector(lat0, lon0)
    polys_xy = [[to_xy(r) for r in poly] for poly in polys]
    flat_xy = [r for poly in polys_xy for r in poly]
    area_m2 = sum(polygon_area(poly) for poly in polys_xy)
    bx0 = min(float(r[:, 0].min()) for r in flat_xy)
    bx1 = max(float(r[:, 0].max()) for r in flat_xy)
    by0 = min(float(r[:, 1].min()) for r in flat_xy)
    by1 = max(float(r[:, 1].max()) for r in flat_xy)
    cx, cy, clearance = pole_of_water(flat_xy, (bx0, by0, bx1, by1))
    kx = math.cos(math.radians(lat0)) * 111320.0
    centre = [round(lat0 + cy / 110540.0, 4), round(lon0 + cx / kx, 4)]
    expected = entry["check_near"]
    pad = 0.25
    if not (min(lats) - pad <= expected[0] <= max(lats) + pad and min(lons) - pad <= expected[1] <= max(lons) + pad):
        raise SystemExit(
            f"{entry['id']}: the outline named {src['name']!r} in {src['file']} spans "
            f"{min(lats):.2f} to {max(lats):.2f} N, {min(lons):.2f} to {max(lons):.2f} E, "
            f"which does not contain {expected}. The source label is probably wrong."
        )
    rings = [[[round(p[1], 4), round(p[0], 4)] for p in ring] for poly in polys for ring in poly]
    clarity = entry["clarity"]
    lake = {
        "id": entry["id"],
        "name": entry["name"],
        "country": entry["country"],
        "centre": centre,
        "bbox": [round(min(lats), 4), round(min(lons), 4), round(max(lats), 4), round(max(lons), 4)],
        "area_km2": round(area_m2 / 1e6, 1),
        "mean_depth_m": entry["mean_depth_m"],
        "altitude_m": entry["altitude_m"],
        "clarity": clarity,
        "baseline": baselines[clarity],
        "ice_risk": entry["ice_risk"],
        "surface_c": entry["surface_c"],
        "ice_months": entry.get("ice_months", []),
        "hazards": entry["hazards"],
        "rings": rings,
    }
    return lake, round(clearance / 1000.0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    catalogue = json.loads((ROOT / "lakes.json").read_text(encoding="utf-8"))
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    OUT.mkdir(parents=True, exist_ok=True)
    index = []
    print(f"{'id':11s} {'area km2':>9s} {'rings':>5s} {'verts':>6s} {'centre':>18s} {'clear km':>8s} {'bytes':>7s}")
    for entry in catalogue["lakes"]:
        if wanted and entry["id"] not in wanted:
            continue
        lake, clearance = build_lake(entry, catalogue["clarity_baseline"], args.refresh)
        text = json.dumps(lake, separators=(",", ":"), ensure_ascii=False)
        (OUT / f"{lake['id']}.json").write_text(text, encoding="utf-8", newline="\n")
        verts = sum(len(r) for r in lake["rings"])
        print(f"{lake['id']:11s} {lake['area_km2']:9.1f} {len(lake['rings']):5d} {verts:6d} "
              f"{str(lake['centre']):>18s} {clearance:8.1f} {len(text):7d}")
        index.append({k: lake[k] for k in ("id", "name", "country", "centre", "bbox", "area_km2", "clarity", "baseline")})
    if wanted:
        return 0
    doc = {"source": "Natural Earth 1:10m lakes, public domain", "lakes": index}
    (OUT / "index.json").write_text(json.dumps(doc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8", newline="\n")
    print(f"\nwrote {len(index)} lakes to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
