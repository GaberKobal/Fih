#!/usr/bin/env python3
"""
fish_finder.py — spearfishing spot scoring for shallow, structure-poor coasts
(built and tuned for Novigrad, Istria, northern Adriatic).

What changed vs. the first version, and why
-------------------------------------------
The original model was an open-ocean pelagic-fishing model: SST fronts +
chlorophyll over 4 km global grids. In 20 m of turbid, stratified, Case-2
shelf water those two layers are close to noise, and the global bathymetry
(ETOPO1, ~1.8 km) cannot resolve any structure a diver can actually swim to.

This version scores what actually decides a shore dive in the north Adriatic:

  WHERE (spatial, per 100 m cell)
    * relief   — local rises above the surrounding seabed. This is the
                 tegnue/trezze detector: biogenic outcrops standing 0.5-4 m
                 off flat mud. Computed as smoothed_depth - depth, so a mound
                 scores and a scour hole does not.
    * slope    — true metric gradient (m/m), corrected for the fact that a
                 degree of longitude at 45 N is only ~0.70 of a degree of
                 latitude. Finds ledges and reef edges.
    * shelter  — how sheltered each cell is from today's wind, from real
                 upwind fetch traced across the land mask. This is the term
                 that makes the map change day to day: in a jugo the whole
                 open coast goes dark and the lee pockets light up.
    * habitat  — optional bonus near rock / Posidonia boundaries.
    gated by:  diveable depth range (soft-tapered, not a cliff edge),
               swim distance from your entry points, and a hard exclusion
               mask for protected areas.

  WHETHER (temporal, one number for the day)
    * visibility — decaying memory of wave stirring and rainfall/river input.
                   This is the single biggest driver of a Novigrad dive and
                   it was entirely absent from the first version.
    * workability — can you physically dive in today's sea and wind.
    * water movement — tide rate of change and modelled current, as a mild
                   feeding-activity modifier.

Every data source is free and needs no API key and no account.

Outputs (all written into docs/data/ for the static site):
    score.png       transparent overlay, reprojected to Web Mercator rows
    spots.geojson   top-N candidate spots with their component scores
    spots.gpx       the same points, for a phone or dive watch
    latest.json     conditions, verdict, hourly series, best dive window

Run:
    python fish_finder.py                 # uses config.json
    python fish_finder.py --selftest      # synthetic data, no network
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import gaussian_filter, maximum_filter

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
OUT = ROOT / "docs" / "data"

EMODNET_WCS = "https://ows.emodnet-bathymetry.eu/wcs"
EMODNET_HABITAT_WFS = (
    "https://ows.emodnet-seabedhabitats.eu/geoserver/emodnet_open/wfs"
)
OPENMETEO_MARINE = "https://marine-api.open-meteo.com/v1/marine"
OPENMETEO_WEATHER = "https://api.open-meteo.com/v1/forecast"

USER_AGENT = "fish_finder/2.0 (personal spearfishing planner)"
EARTH_R = 6371008.8


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] {msg}", flush=True)


def http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_json(url: str, timeout: int = 60) -> dict:
    return json.loads(http_get(url, timeout).decode("utf-8"))


def norm(x, lo, hi):
    """Normalise to 0-1 against a FIXED physical range.

    Deliberately not min-max: min-max per tile makes today's map
    incomparable with yesterday's, so a flat, featureless day still
    produces a confident-looking red blob.
    """
    return np.clip((np.asarray(x, dtype=float) - lo) / (hi - lo), 0.0, 1.0)


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


# --------------------------------------------------------------------------
# target grid — one explicit grid that everything is interpolated onto
# --------------------------------------------------------------------------

def build_target_grid(bbox: dict, res_m: float):
    """Grid with (approximately) square metre cells at the box's centre latitude."""
    lat0 = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    dlat = res_m / 111320.0
    dlon = res_m / (111320.0 * math.cos(math.radians(lat0)))
    lats = np.arange(bbox["lat_min"], bbox["lat_max"] + 0.5 * dlat, dlat)
    lons = np.arange(bbox["lon_min"], bbox["lon_max"] + 0.5 * dlon, dlon)
    return lats, lons


def regrid(src_lats, src_lons, src, dst_lats, dst_lons, fill=np.nan):
    """Coordinate-aware resampling.

    The original resample_to_shape() used array-shape ratios, which silently
    assumed every source shared an origin and cell size with the target. It
    did not, so layers were offset by several cells — on a 15 km tile that is
    the entire signal.
    """
    src = np.asarray(src, dtype=float)
    if src_lats[0] > src_lats[-1]:
        src_lats, src = src_lats[::-1], src[::-1, :]
    if src_lons[0] > src_lons[-1]:
        src_lons, src = src_lons[::-1], src[:, ::-1]

    # Interpolate the values and a validity mask separately, so NaN regions
    # do not bleed into good data.
    valid = np.isfinite(src).astype(float)
    filled = np.where(np.isfinite(src), src, 0.0)

    gy, gx = np.meshgrid(dst_lats, dst_lons, indexing="ij")
    pts = np.stack([gy.ravel(), gx.ravel()], axis=-1)

    fv = RegularGridInterpolator(
        (src_lats, src_lons), filled, bounds_error=False, fill_value=0.0
    )(pts).reshape(gy.shape)
    fm = RegularGridInterpolator(
        (src_lats, src_lons), valid, bounds_error=False, fill_value=0.0
    )(pts).reshape(gy.shape)

    out = np.full(gy.shape, fill, dtype=float)
    ok = fm > 0.5
    out[ok] = fv[ok] / fm[ok]
    return out


# --------------------------------------------------------------------------
# bathymetry — EMODnet DTM, ~115 m, cached to disk
# --------------------------------------------------------------------------

def fetch_emodnet_bathymetry(bbox: dict, res_deg: float, cache_key: str):
    """EMODnet Bathymetry DTM via WCS 1.0.0, as GeoTIFF.

    Values are elevation: negative below sea level, positive on land.
    Static data, so it is cached and never refetched.
    """
    cache_file = CACHE / f"bathy_{cache_key}.npz"
    if cache_file.exists():
        log(f"bathymetry: cache hit ({cache_file.name})")
        z = np.load(cache_file)
        return z["lats"], z["lons"], z["elev"]

    import rasterio  # imported here so --selftest works without it

    params = {
        "service": "wcs",
        "version": "1.0.0",
        "request": "getcoverage",
        "coverage": "emodnet:mean",
        "crs": "EPSG:4326",
        # WCS 1.0.0 EPSG:4326 BBOX order is minx,miny,maxx,maxy (lon,lat)
        "BBOX": f"{bbox['lon_min']},{bbox['lat_min']},{bbox['lon_max']},{bbox['lat_max']}",
        "format": "image/tiff",
        "interpolation": "nearest",
        "resx": f"{res_deg:.8f}",
        "resy": f"{res_deg:.8f}",
    }
    url = f"{EMODNET_WCS}?{urllib.parse.urlencode(params)}"
    log("bathymetry: requesting EMODnet DTM (first run only, then cached)")
    raw = http_get(url, timeout=300)

    if raw[:4] not in (b"II*\x00", b"MM\x00*"):
        raise RuntimeError(
            "EMODnet returned a non-TIFF response (usually an XML "
            f"ServiceException). First 300 bytes:\n{raw[:300]!r}"
        )

    with rasterio.MemoryFile(raw) as mem, mem.open() as ds:
        elev = ds.read(1).astype(float)
        if ds.nodata is not None:
            elev[elev == ds.nodata] = np.nan
        # very large magnitudes are EMODnet's fill values
        elev[np.abs(elev) > 12000] = np.nan
        t = ds.transform
        lons = t.c + t.a * (np.arange(ds.width) + 0.5)
        lats = t.f + t.e * (np.arange(ds.height) + 0.5)

    if lats[0] > lats[-1]:
        lats, elev = lats[::-1], elev[::-1, :]

    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache_file, lats=lats, lons=lons, elev=elev)
    log(f"bathymetry: {elev.shape[0]}x{elev.shape[1]} cells cached")
    return lats, lons, elev


# --------------------------------------------------------------------------
# structure terms
# --------------------------------------------------------------------------

def metric_gradient(field, lats, lons):
    """Gradient magnitude in metres per metre.

    The original used gaussian_gradient_magnitude directly on the lat/lon
    grid, which treats one degree of longitude as equal to one degree of
    latitude. At 45 N that stretches every east-west feature by 1/cos(45)
    = 1.41, so ledges running north-south were systematically overweighted.
    """
    lat0 = float(np.mean(lats))
    dy = np.gradient(lats).mean() * 111320.0
    dx = np.gradient(lons).mean() * 111320.0 * math.cos(math.radians(lat0))
    gy, gx = np.gradient(field, dy, dx)
    return np.hypot(gy, gx)


def structure_terms(depth_m, land, lats, lons, cfg):
    """depth_m is positive-down water depth; land is a boolean mask."""
    d = np.where(land, np.nan, depth_m)

    # Fill land with a smooth extrapolation before any filtering, otherwise
    # every coastline produces a fake gradient wall — the bright rim of
    # "fronts" the first version drew around the whole shore.
    filled = d.copy()
    if np.any(np.isnan(filled)):
        med = np.nanmedian(filled) if np.any(np.isfinite(filled)) else 0.0
        filled = np.where(np.isnan(filled), med, filled)
        for _ in range(3):
            sm = gaussian_filter(filled, sigma=4)
            filled = np.where(np.isnan(d), sm, d)
            filled = np.where(np.isnan(filled), med, filled)

    # Relief: how far this cell rises above the local seabed. Positive for a
    # mound (tegnua, boulder field, wreck mound), negative for a hole.
    bg_sigma_cells = max(2.0, cfg["structure"]["background_scale_m"] /
                         cfg["grid_resolution_m"])
    from scipy.ndimage import median_filter
    filled = median_filter(filled, size=3)      # kills 1-cell survey seams
    background = gaussian_filter(filled, sigma=bg_sigma_cells)
    relief = background - filled

    slope = metric_gradient(gaussian_filter(filled, sigma=1.0), lats, lons)

    relief_s = norm(relief, 0.0, cfg["structure"]["relief_scale_m"])
    slope_s = norm(slope, 0.0, cfg["structure"]["slope_scale"])

    relief_s[land] = 0.0
    slope_s[land] = 0.0
    return relief_s, slope_s, relief, slope


def depth_fit(depth_m, land, cfg):
    """Soft taper instead of a binary in/out mask.

    A 30.1 m ledge is not worthless just because max_depth is 30.
    """
    lo = cfg["depth"]["min_m"]
    hi = cfg["depth"]["max_m"]
    edge = max(cfg["depth"]["soft_edge_m"], 1e-6)
    up = np.clip((depth_m - (lo - edge)) / edge, 0, 1)
    down = np.clip(((hi + edge) - depth_m) / edge, 0, 1)
    fit = np.minimum(up, down)
    fit[land] = 0.0
    fit[~np.isfinite(depth_m)] = 0.0
    return fit


def access_fit(lats, lons, cfg):
    """1 near an entry point, tapering to 0 past max_swim_km (straight line).

    Straight-line distance ignores headlands you would have to swim around,
    so treat it as optimistic near a point of land.
    """
    pts = cfg.get("entry_points") or []
    if not pts:
        return np.ones((len(lats), len(lons)))
    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    best = np.full(gy.shape, np.inf)
    for p in pts:
        best = np.minimum(best, haversine_m(p["lat"], p["lon"], gy, gx))
    max_m = cfg["max_swim_km"] * 1000.0
    taper = max(cfg.get("swim_taper_km", 0.4) * 1000.0, 1.0)
    return np.clip((max_m + taper - best) / taper, 0.0, 1.0)


def upwind_shelter(land, lats, lons, wind_from_deg, cfg):
    """Trace the upwind ray from every water cell until it hits land.

    Short fetch (land close upwind) = flat, workable, often clearer water.
    """
    max_km = cfg["shelter"]["max_fetch_km"]
    step_m = cfg["shelter"]["fetch_step_m"]
    n_steps = int(max_km * 1000 / step_m)

    th = math.radians(wind_from_deg)
    east = math.sin(th)   # towards the source of the wind
    north = math.cos(th)

    lat0 = float(np.mean(lats))
    dlat = (north * step_m) / 111320.0
    dlon = (east * step_m) / (111320.0 * math.cos(math.radians(lat0)))

    lat_step = lats[1] - lats[0]
    lon_step = lons[1] - lons[0]
    ny, nx = land.shape

    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    fetch = np.full(gy.shape, max_km * 1000.0)
    done = land.copy()

    cur_lat, cur_lon = gy.copy(), gx.copy()
    for s in range(1, n_steps + 1):
        cur_lat += dlat
        cur_lon += dlon
        iy = np.rint((cur_lat - lats[0]) / lat_step).astype(int)
        ix = np.rint((cur_lon - lons[0]) / lon_step).astype(int)
        outside = (iy < 0) | (iy >= ny) | (ix < 0) | (ix >= nx)
        iyc = np.clip(iy, 0, ny - 1)
        ixc = np.clip(ix, 0, nx - 1)
        # Outside the tile we cannot see land, so assume open water.
        hit = land[iyc, ixc] & ~outside & ~done
        fetch[hit] = s * step_m
        done |= hit
        if done.all():
            break

    shelter = 1.0 - np.clip(fetch / (max_km * 1000.0), 0.0, 1.0)
    shelter[land] = 0.0
    return shelter, fetch


# --------------------------------------------------------------------------
# exclusion zones and habitat polygons
# --------------------------------------------------------------------------

def _rings_from_geojson(gj):
    rings = []
    feats = gj.get("features", []) if gj.get("type") == "FeatureCollection" else [gj]
    for f in feats:
        geom = f.get("geometry") or {}
        gt, co = geom.get("type"), geom.get("coordinates")
        if gt == "Polygon":
            rings.append(np.asarray(co[0], dtype=float))
        elif gt == "MultiPolygon":
            for poly in co:
                rings.append(np.asarray(poly[0], dtype=float))
    return rings


def rasterize_polygons(path_or_gj, lats, lons):
    """Point-in-polygon without shapely — matplotlib.path is enough here."""
    from matplotlib.path import Path as MplPath

    if isinstance(path_or_gj, (str, os.PathLike)):
        p = Path(path_or_gj)
        if not p.exists():
            return np.zeros((len(lats), len(lons)), dtype=bool)
        gj = json.loads(p.read_text())
    else:
        gj = path_or_gj

    rings = _rings_from_geojson(gj)
    mask = np.zeros((len(lats), len(lons)), dtype=bool)
    if not rings:
        return mask

    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel()], axis=-1)  # GeoJSON is lon,lat
    for ring in rings:
        if len(ring) < 3:
            continue
        mask |= MplPath(ring[:, :2]).contains_points(pts).reshape(gy.shape)
    return mask


def fetch_seabed_habitat(bbox, cfg):
    """Optional EUSeaMap polygons from EMODnet Seabed Habitats (cached).

    Off by default: the WFS layer names change between EUSeaMap releases,
    so this is the most likely thing to break. If it fails, the run
    continues without a habitat term.
    """
    cache_file = CACHE / "habitat.geojson"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": cfg["habitat"]["wfs_layer"],
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "bbox": (f"{bbox['lat_min']},{bbox['lon_min']},"
                 f"{bbox['lat_max']},{bbox['lon_max']},EPSG:4326"),
        "count": "2000",
    }
    url = f"{EMODNET_HABITAT_WFS}?{urllib.parse.urlencode(params)}"
    gj = http_json(url, timeout=180)
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(gj))
    return gj


def habitat_edge_score(gj, lats, lons, cfg):
    """Reward the boundary between substrate types, not the middle of a patch.

    Fish hold where rock meets sand and where a Posidonia meadow ends.
    """
    keys = [k.lower() for k in cfg["habitat"]["reward_substrates"]]
    feats = [
        f for f in gj.get("features", [])
        if any(k in json.dumps(f.get("properties", {})).lower() for k in keys)
    ]
    if not feats:
        return np.zeros((len(lats), len(lons)))
    inside = rasterize_polygons({"type": "FeatureCollection", "features": feats},
                                lats, lons).astype(float)
    sigma = max(1.0, cfg["habitat"]["edge_width_m"] / cfg["grid_resolution_m"])
    blurred = gaussian_filter(inside, sigma=sigma)
    # peaks at the boundary (blurred ~0.5), falls off deep inside or outside
    return np.clip(1.0 - np.abs(2.0 * blurred - 1.0), 0.0, 1.0)


# --------------------------------------------------------------------------
# conditions — Open-Meteo, keyless
# --------------------------------------------------------------------------

def fetch_conditions(lat, lon, cfg):
    past = cfg["conditions"]["past_days"]
    fwd = cfg["conditions"]["forecast_days"]

    marine_vars = [
        "wave_height", "wave_period", "wave_direction",
        "sea_surface_temperature", "sea_level_height_msl",
        "ocean_current_velocity",
    ]
    weather_vars = [
        "precipitation", "wind_speed_10m", "wind_direction_10m",
        "wind_gusts_10m", "cloud_cover",
    ]

    m = http_json(f"{OPENMETEO_MARINE}?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(marine_vars),
        "past_days": past, "forecast_days": fwd, "timezone": "UTC",
    }))
    w = http_json(f"{OPENMETEO_WEATHER}?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(weather_vars),
        "daily": "sunrise,sunset",
        "past_days": past, "forecast_days": fwd, "timezone": "UTC",
    }))

    times = m["hourly"]["time"]
    if w["hourly"]["time"] != times:
        raise RuntimeError("marine and weather time axes differ; cannot merge")

    h = {"time": times}
    for k in marine_vars:
        h[k] = np.array([np.nan if v is None else v
                         for v in m["hourly"].get(k, [None] * len(times))], float)
    for k in weather_vars:
        h[k] = np.array([np.nan if v is None else v
                         for v in w["hourly"].get(k, [None] * len(times))], float)
    return h, w.get("daily", {})


def visibility_series(h, cfg):
    """Underwater visibility as a decaying memory of stirring and runoff.

    Two independent sources of turbidity on this coast:
      * wave resuspension of the sandy/muddy shelf — scales roughly with
        wave energy, so Hs^3, and settles out over about a day
      * the Mirna plume and general runoff after rain — slower to clear
    """
    c = cfg["visibility"]
    hs = np.nan_to_num(h["wave_height"], nan=0.0)
    pr = np.nan_to_num(h["precipitation"], nan=0.0)

    def decay_memory(x, tau_h):
        k = math.exp(-1.0 / tau_h)
        out = np.zeros_like(x)
        acc = 0.0
        for i, v in enumerate(x):
            acc = acc * k + v
            out[i] = acc
        return out * (1.0 - k)   # normalised so a constant input -> that value

    stir = decay_memory(hs ** 3, c["wave_tau_h"])
    plume = decay_memory(pr, c["rain_tau_h"])

    turbidity = (c["wave_weight"] * norm(stir, 0.0, c["wave_ref_hs"] ** 3)
                 + c["rain_weight"] * norm(plume, 0.0, c["rain_ref_mm_per_h"]))
    viz = np.clip(1.0 - turbidity, 0.0, 1.0)
    # rough metres, for a number you can actually reason about
    viz_m = c["viz_min_m"] + viz * (c["viz_max_m"] - c["viz_min_m"])
    return viz, viz_m


def workability_series(h, cfg):
    c = cfg["workability"]
    hs = np.nan_to_num(h["wave_height"], nan=1.0)
    wind = np.nan_to_num(h["wind_speed_10m"], nan=30.0)
    sea = 1.0 - norm(hs, c["hs_good_m"], c["hs_bad_m"])
    air = 1.0 - norm(wind, c["wind_good_kmh"], c["wind_bad_kmh"])
    return np.minimum(sea, air), sea, air


def movement_series(h, cfg):
    """Water movement: tide rate of change plus modelled current."""
    lvl = np.nan_to_num(h["sea_level_height_msl"], nan=0.0)
    rate = np.abs(np.gradient(lvl))          # m per hour
    cur = np.nan_to_num(h["ocean_current_velocity"], nan=0.0)
    c = cfg["movement"]
    return np.clip(0.5 * norm(rate, 0.0, c["tide_rate_ref_m_per_h"])
                   + 0.5 * norm(cur, 0.0, c["current_ref_ms"]), 0.0, 1.0)


def daylight_mask(times, daily):
    """True for hours between sunrise and sunset."""
    rises = [dt.datetime.fromisoformat(s) for s in daily.get("sunrise", [])]
    sets = [dt.datetime.fromisoformat(s) for s in daily.get("sunset", [])]
    ok = np.zeros(len(times), dtype=bool)
    ts = [dt.datetime.fromisoformat(t) for t in times]
    for r, s in zip(rises, sets):
        ok |= np.array([r <= t <= s for t in ts])
    if not ok.any():
        # No usable sunrise/sunset coverage — do not silently veto every hour.
        return np.ones(len(times), dtype=bool)
    return ok


def best_window(h, daily, viz, work, move, cfg, now_iso):
    """Best contiguous block of dive hours from now forward."""
    times = h["time"]
    base = (cfg["day_weights"]["visibility"] * viz
            + cfg["day_weights"]["workability"] * work
            + cfg["day_weights"]["movement"] * move)
    hourly = base * (work > cfg["workability"]["hard_floor"])
    hourly = hourly * daylight_mask(times, daily)

    future = np.array([t >= now_iso for t in times])
    hourly = np.where(future, hourly, -1.0)

    win = cfg["conditions"]["window_hours"]
    if len(hourly) < win:
        return None
    kern = np.ones(win) / win
    rolled = np.convolve(hourly, kern, mode="valid")
    if rolled.max() <= 0:
        # Every future daylight hour was vetoed. Fall back to ignoring
        # daylight rather than reporting "no window" and hiding the reason.
        hourly = np.where(future, base * (work > cfg["workability"]["hard_floor"]), -1.0)
        rolled = np.convolve(hourly, kern, mode="valid")
    i = int(np.argmax(rolled))
    if rolled[i] <= 0:
        return None
    return {
        "start": times[i],
        "end": times[i + win - 1],
        "score": round(float(rolled[i]), 3),
        "wave_height_m": round(float(np.nanmean(h["wave_height"][i:i + win])), 2),
        "wind_speed_kmh": round(float(np.nanmean(h["wind_speed_10m"][i:i + win])), 1),
        "sea_temp_c": round(float(np.nanmean(h["sea_surface_temperature"][i:i + win])), 1),
    }


def verdict(viz_now, viz_m_now, work_now, sea_now, air_now, cfg):
    """A single word plus the reason, because that is the actual decision."""
    day = min(viz_now, work_now)
    if work_now < cfg["workability"]["hard_floor"]:
        limit = "sea state" if sea_now <= air_now else "wind"
        return "stay home", limit, day
    if day >= cfg["verdict"]["go"]:
        return "go", "nothing limiting", day
    if day >= cfg["verdict"]["marginal"]:
        limit = "visibility" if viz_now < work_now else (
            "sea state" if sea_now <= air_now else "wind")
        return "marginal", limit, day
    limit = "visibility" if viz_now < work_now else (
        "sea state" if sea_now <= air_now else "wind")
    return "poor", limit, day


# --------------------------------------------------------------------------
# scoring and spot extraction
# --------------------------------------------------------------------------

def combine_score(terms, weights, gates):
    total_w = sum(weights[k] for k in terms)
    if total_w <= 0:
        raise ValueError("all weights are zero")
    s = sum(weights[k] * terms[k] for k in terms) / total_w
    for g in gates:
        s = s * g
    return np.clip(s, 0.0, 1.0)


def find_spots(score, lats, lons, depth_m, terms, cfg):
    """Local maxima with non-maximum suppression, so you get 12 distinct
    marks rather than 12 pixels of the same reef."""
    sep_cells = max(1, int(cfg["min_spot_separation_m"] / cfg["grid_resolution_m"]))
    size = 2 * sep_cells + 1
    peaks = (score >= maximum_filter(score, size=size)) & (score > cfg["min_spot_score"])
    ys, xs = np.nonzero(peaks)
    order = np.argsort(-score[ys, xs])
    ys, xs = ys[order], xs[order]

    out = []
    for y, x in zip(ys, xs):
        if len(out) >= cfg["top_spots"]:
            break
        if any(haversine_m(lats[y], lons[x], s["lat"], s["lon"])
               < cfg["min_spot_separation_m"] for s in out):
            continue
        out.append({
            "lat": round(float(lats[y]), 6),
            "lon": round(float(lons[x]), 6),
            "score": round(float(score[y, x]), 3),
            "depth_m": round(float(depth_m[y, x]), 1),
            "relief": round(float(terms["relief"][y, x]), 3),
            "slope": round(float(terms["slope"][y, x]), 3),
            "shelter": round(float(terms["shelter"][y, x]), 3),
        })
    return out


# --------------------------------------------------------------------------
# outputs
# --------------------------------------------------------------------------

def mercator_y(lat_deg):
    lat = np.radians(np.clip(lat_deg, -85.05, 85.05))
    return np.log(np.tan(math.pi / 4 + lat / 2))


def write_overlay_png(score, lats, lons, path):
    """Transparent PNG, rows resampled to equal Web Mercator spacing.

    Leaflet stretches an ImageOverlay linearly in Mercator y. A plate-carree
    PNG dropped straight in is therefore shifted north-south by tens of
    metres at 45 N — enough to matter when you are looking for a 40 m reef.
    """
    import matplotlib
    matplotlib.use("Agg")

    ny = len(lats)
    y_edges = np.linspace(mercator_y(lats[0]), mercator_y(lats[-1]), ny)
    lat_merc = np.degrees(2 * np.arctan(np.exp(y_edges)) - math.pi / 2)
    src = RegularGridInterpolator((lats, lons), score,
                                  bounds_error=False, fill_value=0.0)
    gy, gx = np.meshgrid(lat_merc, lons, indexing="ij")
    resampled = src(np.stack([gy.ravel(), gx.ravel()], -1)).reshape(gy.shape)

    rgba = matplotlib.colormaps["viridis"](np.clip(resampled, 0, 1))
    # fade out weak cells rather than painting the whole box
    rgba[..., 3] = np.clip(resampled * 1.6, 0.0, 0.88)
    rgba[resampled <= 0.02, 3] = 0.0

    import matplotlib.pyplot as plt
    plt.imsave(path, np.flipud(rgba))   # PNG rows run north to south
    log(f"wrote {path}")


def write_geojson(spots, path, meta):
    gj = {
        "type": "FeatureCollection",
        "properties": meta,
        "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
             "properties": {k: v for k, v in s.items() if k not in ("lat", "lon")}}
            for s in spots
        ],
    }
    Path(path).write_text(json.dumps(gj, indent=1))
    log(f"wrote {path} ({len(spots)} spots)")


def write_gpx(spots, path, name):
    gpx = ET.Element("gpx", version="1.1", creator="fish_finder",
                     xmlns="http://www.topografix.com/GPX/1/1")
    md = ET.SubElement(gpx, "metadata")
    ET.SubElement(md, "name").text = name
    for i, s in enumerate(spots, 1):
        wpt = ET.SubElement(gpx, "wpt", lat=f"{s['lat']:.6f}", lon=f"{s['lon']:.6f}")
        ET.SubElement(wpt, "name").text = f"{i:02d} ({s['depth_m']:.0f} m)"
        ET.SubElement(wpt, "desc").text = (
            f"score {s['score']:.2f} | relief {s['relief']:.2f} "
            f"| slope {s['slope']:.2f} | shelter {s['shelter']:.2f}")
        ET.SubElement(wpt, "sym").text = "Fishing Hot Spot"
    ET.ElementTree(gpx).write(path, encoding="utf-8", xml_declaration=True)
    log(f"wrote {path}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def synthetic_bathymetry(bbox, res_deg):
    """A fake shelf with a few mounds, so the whole pipeline can be run
    and checked without touching the network."""
    lats = np.arange(bbox["lat_min"], bbox["lat_max"], res_deg)
    lons = np.arange(bbox["lon_min"], bbox["lon_max"], res_deg)
    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    shore = bbox["lon_min"] + 0.25 * (bbox["lon_max"] - bbox["lon_min"])
    elev = -((gx - shore) * 260.0)          # deepens offshore
    elev += 4.0 * np.sin(gy * 900) * np.cos(gx * 700)
    rng = np.random.default_rng(7)
    for _ in range(6):                       # tegnue-like mounds
        cy = rng.uniform(bbox["lat_min"], bbox["lat_max"])
        cx = rng.uniform(shore, bbox["lon_max"])
        r = np.hypot((gy - cy) * 111.0, (gx - cx) * 78.0)
        elev += 3.5 * np.exp(-(r / 0.35) ** 2)
    return lats, lons, elev


def run(cfg, selftest=False):
    OUT.mkdir(parents=True, exist_ok=True)
    bbox = cfg["bbox"]
    res_deg = cfg["grid_resolution_m"] / 111320.0
    lats, lons = build_target_grid(bbox, cfg["grid_resolution_m"])
    log(f"target grid: {len(lats)} x {len(lons)} at {cfg['grid_resolution_m']} m")

    # ---- bathymetry ----
    if selftest:
        blats, blons, elev = synthetic_bathymetry(bbox, res_deg)
    else:
        key = (f"{bbox['lon_min']}_{bbox['lat_min']}_{bbox['lon_max']}_"
               f"{bbox['lat_max']}_{cfg['grid_resolution_m']}")
        blats, blons, elev = fetch_emodnet_bathymetry(bbox, res_deg, key)

    elev_g = regrid(blats, blons, elev, lats, lons)
    land = ~np.isfinite(elev_g) | (elev_g >= 0)
    depth_m = np.where(land, np.nan, -elev_g)

    water_frac = float((~land).mean())
    if water_frac < 0.05:
        raise RuntimeError(
            f"only {water_frac:.1%} of the box is water — check the bbox "
            "(lon/lat order) before trusting anything below")
    log(f"water covers {water_frac:.0%} of the box; "
        f"depth {np.nanmin(depth_m):.1f}-{np.nanmax(depth_m):.1f} m")

    # ---- conditions ----
    clat = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    clon = 0.5 * (bbox["lon_min"] + bbox["lon_max"])
    if selftest:
        n = 96
        h = {"time": [(dt.datetime(2026, 7, 28) + dt.timedelta(hours=i)).isoformat()
                      for i in range(n)]}
        h["wave_height"] = np.abs(np.sin(np.linspace(0, 6, n))) * 0.6
        h["wave_period"] = np.full(n, 4.0)
        h["wave_direction"] = np.full(n, 200.0)
        h["sea_surface_temperature"] = np.full(n, 24.5)
        h["sea_level_height_msl"] = 0.3 * np.sin(np.linspace(0, 20, n))
        h["ocean_current_velocity"] = np.full(n, 0.12)
        h["precipitation"] = np.zeros(n)
        h["precipitation"][10:14] = 3.0
        h["wind_speed_10m"] = 8 + 6 * np.sin(np.linspace(0, 5, n))
        h["wind_direction_10m"] = np.full(n, 130.0)
        h["wind_gusts_10m"] = h["wind_speed_10m"] * 1.4
        h["cloud_cover"] = np.full(n, 20.0)
        daily = {"sunrise": [f"2026-07-{28+d}T03:30" for d in range(4)],
                 "sunset": [f"2026-07-{28+d}T18:40" for d in range(4)]}
        now_i = 24
    else:
        log("conditions: Open-Meteo marine + weather")
        h, daily = fetch_conditions(clat, clon, cfg)
        now = dt.datetime.now(dt.timezone.utc).replace(
            minute=0, second=0, microsecond=0, tzinfo=None)
        now_i = int(np.argmin([abs((dt.datetime.fromisoformat(t) - now).total_seconds())
                               for t in h["time"]]))

    viz, viz_m = visibility_series(h, cfg)
    work, sea_ok, air_ok = workability_series(h, cfg)
    move = movement_series(h, cfg)
    now_iso = h["time"][now_i]

    word, limiter, day_score = verdict(
        float(viz[now_i]), float(viz_m[now_i]), float(work[now_i]),
        float(sea_ok[now_i]), float(air_ok[now_i]), cfg)
    window = best_window(h, daily, viz, work, move, cfg, now_iso)
    log(f"verdict: {word} (limited by {limiter}), viz ~{viz_m[now_i]:.0f} m")

    # ---- spatial terms ----
    relief_s, slope_s, relief_raw, slope_raw = structure_terms(
        depth_m, land, lats, lons, cfg)

    wind_from = float(h["wind_direction_10m"][now_i])
    if not np.isfinite(wind_from):
        wind_from = 0.0
    shelter, fetch_m = upwind_shelter(land, lats, lons, wind_from, cfg)

    terms = {"relief": relief_s, "slope": slope_s, "shelter": shelter}

    if cfg["habitat"]["enabled"] and not selftest:
        try:
            gj = fetch_seabed_habitat(bbox, cfg)
            terms["habitat"] = habitat_edge_score(gj, lats, lons, cfg)
            log("habitat: EUSeaMap edges included")
        except Exception as e:
            log(f"habitat: unavailable ({e.__class__.__name__}), continuing without it")

    weights = dict(cfg["weights"])
    wpath = ROOT / "weights.json"
    if wpath.exists():
        fitted = json.loads(wpath.read_text())
        weights.update(fitted.get("weights", {}))
        log(f"weights: using fitted values from {wpath.name} "
            f"(n={fitted.get('n_dives', '?')}, auc={fitted.get('auc', '?')})")
    weights = {k: weights.get(k, 0.0) for k in terms}

    # ---- gates ----
    dfit = depth_fit(depth_m, land, cfg)
    afit = access_fit(lats, lons, cfg)
    excl = rasterize_polygons(ROOT / "exclusions.geojson", lats, lons)
    if excl.any():
        log(f"exclusions: {excl.mean():.1%} of the box masked out")

    score = combine_score(terms, weights, [dfit, afit, (~excl).astype(float)])
    score[land] = 0.0

    # ---- outputs ----
    spots = find_spots(score, lats, lons, depth_m, terms, cfg)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    write_overlay_png(score, lats, lons, OUT / "score.png")
    write_geojson(spots, OUT / "spots.geojson", {"generated": stamp})
    write_gpx(spots, OUT / "spots.gpx", f"{cfg['region_name']} {stamp[:10]}")

    keep = slice(max(0, now_i - 24), min(len(h["time"]), now_i + 60))
    payload = {
        "generated": stamp,
        "region": cfg["region_name"],
        "bounds": [[bbox["lat_min"], bbox["lon_min"]],
                   [bbox["lat_max"], bbox["lon_max"]]],
        "now": {
            "time": now_iso,
            "verdict": word,
            "limited_by": limiter,
            "score": round(float(day_score), 3),
            "visibility_m": round(float(viz_m[now_i]), 1),
            "wave_height_m": round(float(h["wave_height"][now_i]), 2),
            "wind_speed_kmh": round(float(h["wind_speed_10m"][now_i]), 1),
            "wind_from_deg": round(wind_from),
            "sea_temp_c": round(float(h["sea_surface_temperature"][now_i]), 1),
            "current_ms": round(float(h["ocean_current_velocity"][now_i]), 2),
        },
        "best_window": window,
        "weights": weights,
        "depth_range_m": [cfg["depth"]["min_m"], cfg["depth"]["max_m"]],
        "entry_points": cfg.get("entry_points", []),
        "series": {
            "time": h["time"][keep],
            "visibility_m": [round(float(v), 1) for v in viz_m[keep]],
            "wave_height_m": [round(float(v), 2) for v in h["wave_height"][keep]],
            "wind_speed_kmh": [round(float(v), 1) for v in h["wind_speed_10m"][keep]],
            "precipitation_mm": [round(float(v), 1) for v in h["precipitation"][keep]],
            "sea_level_m": [round(float(v), 2) for v in h["sea_level_height_msl"][keep]],
            "workability": [round(float(v), 3) for v in work[keep]],
        },
        "now_index": now_i - max(0, now_i - 24),
        "spots": spots,
    }
    (OUT / "latest.json").write_text(json.dumps(payload, indent=1))
    log(f"wrote {OUT / 'latest.json'}")
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--selftest", action="store_true",
                    help="run the whole pipeline on synthetic data, no network")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text())
    try:
        run(cfg, selftest=args.selftest)
    except Exception as e:
        log(f"FAILED: {e.__class__.__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
