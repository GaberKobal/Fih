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
from scipy.ndimage import (binary_opening, distance_transform_edt,
                           gaussian_filter, gaussian_filter1d, label,
                           maximum_filter, median_filter)

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


def regrid(src_lats, src_lons, src, dst_lats, dst_lons, fill=np.nan,
           min_valid=0.5):
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

    # min_valid is how much of a target cell's neighbourhood must be real
    # data. At 0.5 a cell straddling the coast still gets a water value, which
    # pushes the shoreline about one cell (100 m) inland. Raise it for
    # bathymetry so a partly-covered cell becomes nodata instead.
    out = np.full(gy.shape, fill, dtype=float)
    ok = fm > min_valid
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


def open_sea_mask(land, cfg):
    """Drop water that is not open sea.

    A negative elevation is not the same thing as somewhere you can hunt.
    Marina basins, the Mirna channel and coastal lagoons are all "water" to
    EMODnet, and at 100 m a cell straddling a 60 m river averages negative and
    passes. A morphological opening removes any water body narrower than the
    structuring disk, which is exactly the set of places you cannot dive.
    """
    wcfg = cfg.get("water", {})
    r = int(wcfg.get("min_width_cells", 3))
    water = ~land
    keep = water.copy()

    if r >= 1:
        yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
        disk = (yy ** 2 + xx ** 2) <= r * r
        # Pad with edge replication first. binary_opening treats everything
        # outside the array as background, so without this it erodes the whole
        # border of the water mask - which silently killed the connectivity
        # anchor at the offshore edge.
        pad = r + 1
        padded = binary_opening(np.pad(water, pad, mode="edge"), structure=disk)
        keep = padded[pad:-pad, pad:-pad]
        n = int((water & ~keep).sum())
        if n:
            log(f"water mask: dropped {n} cells in channels and basins narrower "
                f"than about {2 * r * cfg['grid_resolution_m']:.0f} m")

    # Width alone is not enough. EMODnet carries estuaries inland, so the Mirna
    # valley survives any opening wide enough to keep real coves. Once the
    # opening has severed the narrow neck at the mouth, connectivity finishes
    # the job: keep only water that reaches the open, offshore edge of the box.
    if wcfg.get("require_sea_connection", True) and keep.any():
        lab, n_comp = label(keep)
        sizes = np.bincount(lab.ravel())
        sizes[0] = 0
        sea_id = int(np.argmax(sizes))            # the open sea is the biggest
        connected = lab == sea_id
        sea_frac = float(sizes[sea_id]) / float(keep.sum())

        touches = [name for name, arr in (
            ("west", connected[:, 0]), ("east", connected[:, -1]),
            ("north", connected[-1, :]), ("south", connected[0, :])) if arr.any()]
        log(f"water mask: sea component is {sea_frac:.0%} of all water, "
            f"{n_comp} bodies total, reaches the "
            f"{', '.join(touches) if touches else 'no'} edge(s)")

        # The only assumption worth guarding is that the biggest water body
        # really is the sea. Requiring it to touch a NAMED edge was too
        # brittle - EMODnet's raster need not reach the requested bbox corner,
        # and the deepest offshore water can be masked out anyway.
        floor = wcfg.get("min_sea_fraction", 0.5)
        if sea_frac < floor:
            log(f"water mask: largest body is only {sea_frac:.0%} of water - "
                "too fragmented to trust as the sea, leaving connectivity off")
            return ~keep

        orphaned = int((keep & ~connected).sum())
        if orphaned:
            log(f"water mask: dropped {orphaned} cells in {n_comp - 1} water "
                "bodies not connected to the open sea (inland valleys, lagoons)")
        keep = connected
    return ~keep


def shore_clearance(land, cfg):
    """Distance to the nearest land, in metres."""
    return distance_transform_edt(~land) * cfg["grid_resolution_m"]


def destripe(field, sigma=8.0):
    """Remove per-column bias — the north-south survey seams in EMODnet.

    A median filter is the wrong tool: the seams are two or three cells wide,
    so killing them needs a kernel big enough to erase genuine 200 m mounds
    too. The seams are a constant offset per column, though, which is exactly
    what this removes: high-pass across the stripes, take each column's median
    bias, subtract it. Isotropic features survive untouched because their bias
    averages out over the column.
    """
    resid = field - gaussian_filter1d(field, sigma=sigma, axis=1, mode="nearest")
    return field - np.median(resid, axis=0)[None, :]


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
    # EMODnet is a composite of survey swaths, and the seams show up as
    # one-cell-wide north-south stripes that the relief term happily reports as
    # structure. A 3x3 median removes them and leaves a real 100 m mound intact.
    if cfg["structure"].get("destripe", True):
        filled = destripe(filled, sigma=cfg["structure"].get("destripe_sigma", 8.0))
    if cfg["structure"].get("despeckle", True):
        filled = median_filter(filled, size=3)

    bg_sigma_cells = max(2.0, cfg["structure"]["background_scale_m"] /
                         cfg["grid_resolution_m"])
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

    # Relief and slope are naturally strongest right where the seabed falls
    # away from shore, so a flat plateau over 4-24 m put almost every mark in
    # 5 m of water. A trapezoid with its plateau over the depths you actually
    # want to hunt spreads them across the band instead.
    best = cfg["depth"].get("best_m")
    if best:
        blo, bhi = float(best[0]), float(best[1])
        up = np.clip((depth_m - (lo - edge)) / max(blo - (lo - edge), 1e-6), 0, 1)
        down = np.clip(((hi + edge) - depth_m) / max((hi + edge) - bhi, 1e-6), 0, 1)
    else:
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


def upwind_shelter(land, lats, lons, wind_from_deg, cfg, nodata=None):
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

    # Cells with no bathymetry are not land. EMODnet's raster does not always
    # reach the requested bbox, and treating that empty western band as a
    # coastline made offshore cells read as sheltered from exactly the
    # direction they are most exposed to.
    blocks = land if nodata is None else (land & ~nodata)

    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    fetch = np.full(gy.shape, max_km * 1000.0)
    done = blocks.copy()

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
        hit = blocks[iyc, ixc] & ~outside & ~done
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
        "past_days": past, "forecast_days": fwd, "timezone": "auto",
    }))
    w = http_json(f"{OPENMETEO_WEATHER}?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "hourly": ",".join(weather_vars),
        "daily": "sunrise,sunset",
        "past_days": past, "forecast_days": fwd, "timezone": "auto",
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
    return h, w.get("daily", {}), int(w.get("utc_offset_seconds", 0))


def solar_elevation_deg(times_local, lat, lon, tz_offset_s):
    """Sun elevation for each hour, via the NOAA solar position algorithm.

    Pure arithmetic, no API. Replaces the binary daylight test: dawn and dusk
    are not merely "not dark", they are when fish feed and when they let you
    close. Flat noon sun is the worst light of the day to hunt in.
    """
    out = []
    for t in times_local:
        u = dt.datetime.fromisoformat(t) - dt.timedelta(seconds=tz_offset_s)
        n = u.timetuple().tm_yday
        fh = u.hour + u.minute / 60.0
        g = 2 * math.pi / 365.0 * (n - 1 + (fh - 12) / 24.0)
        eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g)
                           - 0.032077 * math.sin(g)
                           - 0.014615 * math.cos(2 * g)
                           - 0.040849 * math.sin(2 * g))
        decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
                - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
                - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
        tst = fh * 60.0 + eqtime + 4.0 * lon
        ha = math.radians(tst / 4.0 - 180.0)
        la = math.radians(lat)
        cz = (math.sin(la) * math.sin(decl)
              + math.cos(la) * math.cos(decl) * math.cos(ha))
        out.append(math.degrees(math.asin(max(-1.0, min(1.0, cz)))))
    return np.array(out)


def light_quality(elev_deg, cfg):
    """0 when the sun is down, best at low sun, worst at high noon.

    Hard zero below the horizon is deliberate and not just about fish:
    spearfishing at night is prohibited in Croatia.
    """
    c = cfg.get("light", {})
    golden = c.get("golden_max_deg", 12.0)
    high = c.get("high_sun_deg", 45.0)
    floor = c.get("high_sun_factor", 0.6)
    q = 1.0 - (1.0 - floor) * np.clip((elev_deg - golden) / max(high - golden, 1e-6), 0, 1)
    return np.where(elev_deg > 0, q, 0.0)


def classify_wind(dir_deg, speed_kmh, gust_kmh, cfg):
    """Name the wind, because on this coast the name carries the forecast.

    Bora off the land, jugo onshore and warm, maestral the afternoon thermal.
    They do very different things to visibility and to whether you can work.
    """
    regs = cfg.get("wind_regimes", {})
    name, spec = "other", regs.get("other", {})
    d = float(dir_deg) % 360.0
    for key, r in regs.items():
        if key == "other" or "from_deg" not in r:
            continue
        lo, hi = r["from_deg"]
        inside = (lo <= d <= hi) if lo <= hi else (d >= lo or d <= hi)
        if inside:
            name, spec = key, r
            break
    viz_f = float(spec.get("viz_factor", 1.0))
    work_f = float(spec.get("work_factor", 1.0))

    # Gustiness is the bora signature and it is what makes the surface
    # unworkable even when mean wind looks fine.
    gust_ratio = 1.0
    if speed_kmh and speed_kmh > 3 and np.isfinite(gust_kmh):
        gust_ratio = float(gust_kmh) / float(speed_kmh)
        if gust_ratio > cfg.get("gust", {}).get("ratio_threshold", 1.6):
            work_f *= cfg.get("gust", {}).get("penalty", 0.8)
    return {"name": name, "viz_factor": viz_f, "work_factor": work_f,
            "gust_ratio": round(gust_ratio, 2),
            "note": spec.get("note", "")}


def wind_regime_series(h, cfg):
    names, vizf, workf = [], [], []
    for i in range(len(h["time"])):
        r = classify_wind(np.nan_to_num(h["wind_direction_10m"][i], nan=0.0),
                          np.nan_to_num(h["wind_speed_10m"][i], nan=0.0),
                          np.nan_to_num(h["wind_gusts_10m"][i], nan=0.0), cfg)
        names.append(r["name"]); vizf.append(r["viz_factor"]); workf.append(r["work_factor"])
    return names, np.array(vizf), np.array(workf)


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


def best_window(h, daily, viz, work, move, cfg, now_iso, light=None):
    """Best contiguous block of dive hours from now forward."""
    times = h["time"]
    base = (cfg["day_weights"]["visibility"] * viz
            + cfg["day_weights"]["workability"] * work
            + cfg["day_weights"]["movement"] * move)
    hourly = base * (work > cfg["workability"]["hard_floor"])
    hourly = hourly * (light if light is not None
                       else daylight_mask(times, daily).astype(float))

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


def hourly_scores(h, daily, viz, work, move, cfg, light=None):
    """The blended dive score for every hour in the series."""
    base = (cfg["day_weights"]["visibility"] * viz
            + cfg["day_weights"]["workability"] * work
            + cfg["day_weights"]["movement"] * move)
    ok = work > cfg["workability"]["hard_floor"]
    if light is None:
        light = daylight_mask(h["time"], daily).astype(float)
    # Light is a quality factor now, not a yes/no: it zeroes the dark hours
    # and still prefers dawn and dusk over flat midday sun.
    return base * ok * light, ok, light


def daily_breakdown(h, daily, viz, viz_m, work, move, cfg, now_iso,
                    light=None, regimes=None):
    """Group the forecast into local days so you can pick tomorrow's hour.

    Times are already local because the API is queried with timezone=auto,
    so slicing the date off the timestamp is enough.
    """
    scores, ok, light = hourly_scores(h, daily, viz, work, move, cfg, light)
    win = cfg["conditions"]["window_hours"]
    today = now_iso[:10]

    by_date = {}
    for i, t in enumerate(h["time"]):
        if t[:10] < today:
            continue                       # past days are history, not a plan
        by_date.setdefault(t[:10], []).append(i)

    days = []
    for date, idx in sorted(by_date.items()):
        hours = [{
            "t": h["time"][i],
            "hour": int(h["time"][i][11:13]),
            "score": round(float(scores[i]), 3),
            "viz_m": round(float(viz_m[i]), 1),
            "wave_m": round(float(h["wave_height"][i]), 2),
            "wind_kmh": round(float(h["wind_speed_10m"][i]), 1),
            "sea_temp_c": round(float(h["sea_surface_temperature"][i]), 1),
            "daylight": bool(light[i] > 0),
            "light": round(float(light[i]), 2),
            "wind_regime": (regimes[i] if regimes else None),
            "workable": bool(ok[i]),
            "past": h["time"][i] < now_iso,
        } for i in idx]

        # best contiguous block of `win` hours inside this day
        best = None
        vals = [x["score"] if not x["past"] else -1.0 for x in hours]
        if len(vals) >= win:
            sums = [(sum(vals[j:j + win]) / win, j) for j in range(len(vals) - win + 1)]
            top, j = max(sums)
            if top > 0:
                blk = hours[j:j + win]
                best = {
                    "start": blk[0]["t"], "end": blk[-1]["t"],
                    "score": round(top, 3),
                    "viz_m": round(sum(b["viz_m"] for b in blk) / win, 1),
                    "wave_m": round(sum(b["wave_m"] for b in blk) / win, 2),
                    "wind_kmh": round(sum(b["wind_kmh"] for b in blk) / win, 1),
                }
        peak = max((x for x in hours if not x["past"]),
                   key=lambda x: x["score"], default=None)
        days.append({
            "date": date,
            "is_today": date == today,
            "best": best,
            "peak_hour": peak["t"] if peak and peak["score"] > 0 else None,
            "peak_score": peak["score"] if peak else 0.0,
            "hours": hours,
        })
    return days


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

def trapezoid(x, lo, best_lo, best_hi, hi):
    """1.0 across [best_lo, best_hi], ramping linearly to 0 at lo and hi."""
    x = np.asarray(x, dtype=float)
    up = np.clip((x - lo) / max(best_lo - lo, 1e-9), 0, 1)
    down = np.clip((hi - x) / max(hi - best_hi, 1e-9), 0, 1)
    return np.minimum(up, down)


def load_species(cfg):
    path = ROOT / cfg.get("species_file", "species.json")
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    wanted = cfg.get("species_ids")          # null/absent = all of them
    out = [sp for sp in doc.get("species", [])
           if not wanted or sp["id"] in wanted]
    return out


def fetch_cmems_profile(lat, lon, cfg):
    """Measured temperature profile at depth from Copernicus Marine.

    Replaces the guessed thermocline drop with the real thing. Needs a free
    Copernicus account; credentials come from the environment, so nothing
    secret is ever committed. Returns None on any problem and the caller
    falls back to the estimate.
    """
    c = cfg.get("cmems", {})
    if not c.get("enabled"):
        return None
    if not (os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
            and os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")):
        log("cmems: enabled but no credentials in the environment "
            "- falling back to the thermocline estimate")
        return None

    cache_file = CACHE / f"cmems_{dt.date.today().isoformat()}.json"
    if cache_file.exists():
        log("cmems: cache hit for today")
        return json.loads(cache_file.read_text())

    try:
        import copernicusmarine
        pad = c.get("pad_deg", 0.1)
        today = dt.date.today()
        ds = copernicusmarine.open_dataset(
            dataset_id=c["dataset_id"],
            variables=[c.get("variable", "thetao")],
            minimum_longitude=lon - pad, maximum_longitude=lon + pad,
            minimum_latitude=lat - pad, maximum_latitude=lat + pad,
            minimum_depth=0.0, maximum_depth=c.get("max_depth_m", 50.0),
            start_datetime=f"{today - dt.timedelta(days=2)}T00:00:00",
            end_datetime=f"{today}T23:59:59",
        )
        var = ds[c.get("variable", "thetao")]
        prof = var.isel(time=-1).mean(dim=["latitude", "longitude"], skipna=True)
        depths = [float(d) for d in prof["depth"].values]
        temps = [float(t) for t in prof.values]
        keep = [(d, t) for d, t in zip(depths, temps) if np.isfinite(t)]
        if len(keep) < 2:
            log("cmems: profile came back empty - using the estimate")
            return None
        out = {"depths": [k[0] for k in keep], "temps": [k[1] for k in keep],
               "source": c["dataset_id"], "fetched": today.isoformat()}
        CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(out))
        log(f"cmems: profile {out['temps'][0]:.1f} C at "
            f"{out['depths'][0]:.0f} m -> {out['temps'][-1]:.1f} C at "
            f"{out['depths'][-1]:.0f} m")
        return out
    except Exception as e:
        log(f"cmems: unavailable ({e.__class__.__name__}: {e}) "
            "- falling back to the thermocline estimate")
        return None


def temp_at_depth(sst_c, depth_m, month, cfg, profile=None):
    """Estimate temperature at depth from SST plus a seasonal stratification drop.

    Using bare SST to decide where a demersal fish sits is wrong for half the
    year here: in July the surface can be 28 C while 15 m is 19 C. This is a
    crude correction, not a measurement - the honest fix is CMEMS Med physics
    temperature at depth, which needs a (free) Copernicus account.
    """
    if profile:
        return float(np.interp(depth_m, profile["depths"], profile["temps"]))
    tc = cfg.get("thermocline")
    if not tc:
        return sst_c
    drop = tc["monthly_drop_c"][int(month) - 1]
    frac = min(max(depth_m / tc["reference_depth_m"], 0.0), 1.2)
    return sst_c - drop * frac


def legal_status(sp, on_date):
    """Is this species legally takeable today?

    Closed seasons run on real dates, not whole months: kavala closes on
    15 May and reopens on 15 July, so a month-granularity model would have
    pointed you at a closed fish for two weeks either side.
    """
    L = sp.get("legal") or {}
    md = f"{on_date.month:02d}-{on_date.day:02d}"
    for w in L.get("closed_seasons", []):
        a, b = w["from"], w["to"]
        closed = (a <= md <= b) if a <= b else (md >= a or md <= b)
        if closed:
            y = on_date.year + (1 if b < a and md >= a else 0)
            return {"open": False,
                    "reason": f"closed season {a} to {b}",
                    "reopens": f"{y}-{b}"}
    return {"open": True, "reason": "", "reopens": None}


def species_day_factors(sp, sea_temp_c, viz_score, month, cfg=None, profile=None,
                        on_date=None):
    """How available this species is today, independent of location.

    Three independent gates, multiplied: temperature, season, and whether
    today's water clarity suits how you hunt it.
    """
    t_lo, t_hi = sp["temp_c"]
    tb_lo, tb_hi = sp["temp_best_c"]
    # Evaluate at the depth the species actually holds, not at the surface.
    d_mid = 0.5 * (sp["depth_best_m"][0] + sp["depth_best_m"][1])
    t_at = temp_at_depth(sea_temp_c, d_mid, month, cfg or {}, profile)
    temp_fit = float(trapezoid(t_at, t_lo, tb_lo, tb_hi, t_hi))

    season_fit = 1.0 if month in sp["months"] else 0.0

    # A legal closure is an absolute gate, and it overrides everything else.
    law = legal_status(sp, on_date or dt.date.today())
    if not law["open"]:
        season_fit = 0.0

    pref = sp.get("viz_pref", "any")
    if pref == "clear":
        viz_fit = float(viz_score)
    elif pref == "murky":
        # The inversion that matters: bass and orada get easier as the
        # water colours up, which is exactly when the generic map says stay home.
        viz_fit = float(1.0 - 0.65 * viz_score)
    else:
        viz_fit = float(0.55 + 0.45 * viz_score)

    return {
        "temp_fit": round(temp_fit, 3),
        "season_fit": round(season_fit, 3),
        "viz_fit": round(viz_fit, 3),
        "today": round(temp_fit * season_fit * viz_fit, 3),
        "temp_at_depth_c": round(float(t_at), 1),
        "legally_open": law["open"],
        "closed_reason": law["reason"],
        "reopens": law["reopens"],
    }


def species_depth_fit(sp, depth_m, land, cfg):
    """Species depth envelope, intersected with what you can actually dive.

    A dentex at 40 m is irrelevant if your limit is 24.
    """
    lo, hi = sp["depth_m"]
    blo, bhi = sp["depth_best_m"]
    lo = max(lo, cfg["depth"]["min_m"])
    hi = min(hi, cfg["depth"]["max_m"])
    blo = min(max(blo, lo), hi)
    bhi = max(min(bhi, hi), lo)
    if hi <= lo:
        return np.zeros_like(depth_m)
    fit = trapezoid(np.nan_to_num(depth_m, nan=-999.0), lo, blo, bhi, hi)
    fit[land] = 0.0
    fit[~np.isfinite(depth_m)] = 0.0
    return fit


def species_spatial_score(sp, terms, depth_m, land, gates, cfg):
    """Reweight the structure terms by what this species actually associates with."""
    r = sp["relief_affinity"]
    sl = sp["slope_affinity"]
    parts = {"relief": r * terms["relief"], "slope": sl * terms["slope"]}
    denom = r + sl

    if "habitat" in terms:
        parts["habitat"] = terms["habitat"]
        denom += 1.0

    structure = sum(parts.values()) / max(denom, 1e-9)

    # Shelter keeps its configured pull: it is about you being able to hunt,
    # not about the fish.
    ws = cfg["weights"].get("shelter", 0.3)
    score = (1.0 - ws) * structure + ws * terms["shelter"]

    score = score * species_depth_fit(sp, depth_m, land, cfg)
    for g in gates:
        score = score * g
    return np.clip(score, 0.0, 1.0)


def representative_hour(day, times):
    """The hour a day should be judged on.

    The best window if there is one, because that is when you would actually
    be in the water; otherwise the day's best remaining hour; otherwise midday.
    """
    if day.get("best"):
        t = day["best"]["start"]
    elif day.get("peak_hour"):
        t = day["peak_hour"]
    else:
        t = f"{day['date']}T12:00"
    for i, ts in enumerate(times):
        if ts[:13] == t[:13]:
            return i
    return min(range(len(times)),
               key=lambda i: abs(int(times[i][11:13]) - 12)
               if times[i][:10] == day["date"] else 99)


def combine_score(terms, weights, gates):
    total_w = sum(weights[k] for k in terms)
    if total_w <= 0:
        raise ValueError("all weights are zero")
    s = sum(weights[k] * terms[k] for k in terms) / total_w
    for g in gates:
        s = s * g
    return np.clip(s, 0.0, 1.0)


def find_spots(score, lats, lons, depth_m, terms, cfg, limit=None):
    """Local maxima with non-maximum suppression, so you get 12 distinct
    marks rather than 12 pixels of the same reef."""
    sep_cells = max(1, int(cfg["min_spot_separation_m"] / cfg["grid_resolution_m"]))
    size = 2 * sep_cells + 1
    peaks = (score >= maximum_filter(score, size=size)) & (score > cfg["min_spot_score"])
    ys, xs = np.nonzero(peaks)
    order = np.argsort(-score[ys, xs])
    ys, xs = ys[order], xs[order]

    cap = limit or cfg["top_spots"]
    out = []
    for y, x in zip(ys, xs):
        if len(out) >= cap:
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


def write_overlay_png(score, lats, lons, path, land=None):
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
    pts = None
    src = RegularGridInterpolator((lats, lons), score,
                                  bounds_error=False, fill_value=0.0)
    gy, gx = np.meshgrid(lat_merc, lons, indexing="ij")
    pts = np.stack([gy.ravel(), gx.ravel()], -1)
    resampled = src(pts).reshape(gy.shape)

    # The Mercator resample interpolates linearly, so a cell straddling the
    # coast picks up a fraction of the neighbouring water score and paints it
    # onto the shore. At 100 m cells that is a visible smear of colour on land.
    # Carry the land mask through with NEAREST sampling and force it clear.
    if land is not None:
        lm = RegularGridInterpolator((lats, lons), land.astype(float),
                                     method="nearest",
                                     bounds_error=False, fill_value=1.0)
        resampled = np.where(lm(pts).reshape(gy.shape) > 0.5, 0.0, resampled)

    rgba = matplotlib.colormaps["viridis"](np.clip(resampled, 0, 1))
    # fade out weak cells rather than painting the whole box
    rgba[..., 3] = np.clip(resampled * 1.6, 0.0, 0.88)
    rgba[resampled <= 0.02, 3] = 0.0

    arr = (np.flipud(rgba) * 255).astype(np.uint8)   # PNG rows run north to south
    try:
        # Quantise to a palette: visually identical, roughly a quarter the
        # size. These get committed every day, so it adds up.
        from PIL import Image
        im = Image.fromarray(arr, mode="RGBA")
        im.quantize(colors=64, method=Image.Quantize.FASTOCTREE).save(
            path, optimize=True)
    except Exception:
        import matplotlib.pyplot as plt
        plt.imsave(path, np.flipud(rgba))
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
    # Sea to the WEST, land to the east, matching this coast - so the
    # sea-connectivity check is actually exercised by --selftest.
    shore = bbox["lon_min"] + 0.60 * (bbox["lon_max"] - bbox["lon_min"])
    elev = (gx - shore) * 260.0             # negative (deeper) to the west
    elev += 4.0 * np.sin(gy * 900) * np.cos(gx * 700)
    rng = np.random.default_rng(7)
    # an inland valley the connectivity check should discard
    inland = ((gy > bbox["lat_min"] + 0.45 * (bbox["lat_max"] - bbox["lat_min"])) &
              (gy < bbox["lat_min"] + 0.52 * (bbox["lat_max"] - bbox["lat_min"])) &
              (gx > shore))
    elev[inland] = -6.0
    for _ in range(6):                       # tegnue-like mounds
        cy = rng.uniform(bbox["lat_min"], bbox["lat_max"])
        cx = rng.uniform(bbox["lon_min"], shore)
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

    elev_g = regrid(blats, blons, elev, lats, lons,
                    min_valid=cfg.get("water", {}).get("bathy_min_valid", 0.9))
    nodata = ~np.isfinite(elev_g)
    land = nodata | (elev_g >= 0)
    if nodata.any():
        log(f"bathymetry: {nodata.mean():.0%} of the box has no EMODnet data "
            "(raster does not reach the bbox) - treated as unusable, not as land")
    land = open_sea_mask(land, cfg)
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
        tz_offset = 0
    else:
        log("conditions: Open-Meteo marine + weather")
        h, daily, tz_offset = fetch_conditions(clat, clon, cfg)
        # API returned local wall-clock times, so compare against local now.
        now = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=tz_offset)
               ).replace(minute=0, second=0, microsecond=0, tzinfo=None)
        now_i = int(np.argmin([abs((dt.datetime.fromisoformat(t) - now).total_seconds())
                               for t in h["time"]]))

    viz, viz_m = visibility_series(h, cfg)

    # A fitted correction from calibrate.py --fit-viz, if one exists. Written
    # only when the model actually tracks reality, so its presence means the
    # visibility number has been checked against real dives.
    vzc = (json.loads((ROOT / "weights.json").read_text()).get("visibility_correction")
           if (ROOT / "weights.json").exists() else None)
    if vzc:
        viz_m = np.clip(vzc["scale"] * viz_m + vzc["offset"], 0.3, 40.0)
        log(f"visibility: applying fitted correction "
            f"({vzc['scale']:.2f}x {vzc['offset']:+.1f} m, n={vzc['n']}, "
            f"r={vzc['r']}, residual sd {vzc['residual_sd_m']} m)")
    work, sea_ok, air_ok = workability_series(h, cfg)
    move = movement_series(h, cfg)
    now_iso = h["time"][now_i]

    # Named wind regimes: bora, jugo, maestral do different things to the
    # water and to whether you can work in it.
    regimes, viz_f, work_f = wind_regime_series(h, cfg)
    viz = np.clip(viz * viz_f, 0.0, 1.0)
    vc = cfg["visibility"]
    viz_m = vc["viz_min_m"] + viz * (vc["viz_max_m"] - vc["viz_min_m"])
    work = np.clip(work * work_f, 0.0, 1.0)

    elev = solar_elevation_deg(h["time"], clat, clon, tz_offset)
    light = light_quality(elev, cfg)

    word, limiter, day_score = verdict(
        float(viz[now_i]), float(viz_m[now_i]), float(work[now_i]),
        float(sea_ok[now_i]), float(air_ok[now_i]), cfg)
    window = best_window(h, daily, viz, work, move, cfg, now_iso, light)
    days = daily_breakdown(h, daily, viz, viz_m, work, move, cfg, now_iso,
                           light, regimes)
    log(f"verdict: {word} (limited by {limiter}), viz ~{viz_m[now_i]:.0f} m")

    # ---- spatial terms ----
    relief_s, slope_s, relief_raw, slope_raw = structure_terms(
        depth_m, land, lats, lons, cfg)

    profile = None if selftest else fetch_cmems_profile(clat, clon, cfg)
    wind_from_now = float(h["wind_direction_10m"][now_i])
    if not np.isfinite(wind_from_now):
        wind_from_now = 0.0
    sea_temp = float(h["sea_surface_temperature"][now_i])
    if not np.isfinite(sea_temp):
        sea_temp = float(np.nanmedian(h["sea_surface_temperature"]))

    weights_cfg = dict(cfg["weights"])
    wpath = ROOT / "weights.json"
    if wpath.exists():
        fitted = json.loads(wpath.read_text())
        weights_cfg.update(fitted.get("weights", {}))
        log(f"weights: using fitted values from {wpath.name} "
            f"(n={fitted.get('n_dives', '?')}, auc={fitted.get('auc', '?')})")

    # ---- gates: static, so computed once ----
    dfit = depth_fit(depth_m, land, cfg)
    afit = access_fit(lats, lons, cfg)
    clear_min = cfg.get("water", {}).get("min_shore_clearance_m", 0)
    cfit = np.ones_like(dfit)
    if clear_min > 0:
        cfit = np.clip(shore_clearance(land, cfg) / max(clear_min, 1e-6), 0, 1)
    excl = rasterize_polygons(ROOT / "exclusions.geojson", lats, lons)
    if excl.any():
        log(f"exclusions: {excl.mean():.1%} of the box masked out")
    gates = [afit, cfit, (~excl).astype(float)]

    habitat = None
    if cfg["habitat"]["enabled"] and not selftest:
        try:
            habitat = habitat_edge_score(fetch_seabed_habitat(bbox, cfg),
                                         lats, lons, cfg)
            log("habitat: EUSeaMap edges included")
        except Exception as e:
            log(f"habitat: unavailable ({e.__class__.__name__}), continuing without")

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    day_scores = []
    species_defs = load_species(cfg)
    sp_cap = cfg.get("species_top_spots", 12)

    # ---- one map per forecast day ----
    # Only shelter varies with the day, but it varies a lot: it is driven by
    # wind direction, so a jugo day and a bora day put the good water in
    # completely different places. Relief, slope and the gates are static.
    for di, day in enumerate(days):
        ri = representative_hour(day, h["time"])
        wind_d = float(h["wind_direction_10m"][ri])
        if not np.isfinite(wind_d):
            wind_d = 0.0
        shelter_d, _ = upwind_shelter(land, lats, lons, wind_d, cfg, nodata)
        terms_d = {"relief": relief_s, "slope": slope_s, "shelter": shelter_d}
        if habitat is not None:
            terms_d["habitat"] = habitat
        w_d = {k: weights_cfg.get(k, 0.0) for k in terms_d}

        score_d = combine_score(terms_d, w_d, gates)
        score_d[land] = 0.0
        spots_d = find_spots(score_d, lats, lons, depth_m, terms_d, cfg)
        write_overlay_png(score_d, lats, lons, OUT / f"score_d{di}.png", land)
        write_gpx(spots_d, OUT / f"spots_d{di}.gpx",
                  f"{cfg['region_name']} {day['date']}")

        temp_d = float(h["sea_surface_temperature"][ri])
        if not np.isfinite(temp_d):
            temp_d = sea_temp
        month_d = int(day["date"][5:7])
        date_d = dt.date.fromisoformat(day["date"])

        sp_out = []
        for sp in species_defs:
            fac = species_day_factors(sp, temp_d, float(viz[ri]), month_d,
                                      cfg, profile, date_d)
            smap = species_spatial_score(sp, terms_d, depth_m, land, gates, cfg)
            smap[land] = 0.0
            ss = find_spots(smap, lats, lons, depth_m, terms_d, cfg, limit=sp_cap)
            write_overlay_png(smap, lats, lons,
                              OUT / f"score_{sp['id']}_d{di}.png", land)
            write_gpx(ss, OUT / f"spots_{sp['id']}_d{di}.gpx",
                      f"{sp.get('name_en', sp['common'])} {day['date']}")
            sp_out.append({
                "id": sp["id"], "common": sp["common"],
                "name_en": sp.get("name_en", sp["common"]),
                "name_local": sp.get("name_local", ""),
                "scientific": sp["scientific"], "note": sp.get("note", ""),
                "viz_pref": sp.get("viz_pref", "any"), "wary": sp.get("wary"),
                "depth_best_m": sp["depth_best_m"], "legal": sp.get("legal", {}),
                "in_season": bool(fac["season_fit"]), **fac, "spots": ss,
            })
        sp_out.sort(key=lambda x: -x["today"])

        day["wind_from_deg"] = round(wind_d)
        day["wind_regime"] = regimes[ri]
        day["viz_m"] = round(float(viz_m[ri]), 1)
        day["sea_temp_c"] = round(temp_d, 1)
        day["at"] = h["time"][ri]
        day["spots"] = spots_d
        day["species"] = sp_out
        day_scores.append(score_d)
        log(f"day {day['date']} (judged at {h['time'][ri][11:16]}, wind {wind_d:.0f} deg): "
            + ", ".join(f"{x['name_en']} {x['today']:.2f}" for x in sp_out[:3]))

    # Say plainly whether the days actually differ. Only shelter varies, and it
    # is wind-driven - so if the wind holds, identical maps are the correct
    # answer, not a broken one. Without this you cannot tell the two apart.
    if len(day_scores) > 1:
        water = ~land
        for i in range(1, len(day_scores)):
            d_abs = float(np.abs(day_scores[i] - day_scores[0])[water].mean())
            same = "essentially identical" if d_abs < 0.01 else "different"
            log(f"map day {i} vs day 0: mean score change {d_abs:.4f} - {same} "
                f"(wind {days[0]['wind_from_deg']}deg -> {days[i]['wind_from_deg']}deg)")

    # today's maps are also the default ones the page loads first
    import shutil
    for src, dst in [("score_d0.png", "score.png"), ("spots_d0.gpx", "spots.gpx")]:
        if (OUT / src).exists():
            shutil.copyfile(OUT / src, OUT / dst)
    spots = days[0]["spots"] if days else []
    species_out = days[0]["species"] if days else []
    write_geojson(spots, OUT / "spots.geojson", {"generated": stamp})

    keep = slice(max(0, now_i - 24), min(len(h["time"]), now_i + 60))
    payload = {
        "generated": stamp,
        "schema_version": 3,
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
            "wind_from_deg": round(wind_from_now if np.isfinite(wind_from_now) else 0),
            "sea_temp_c": round(float(h["sea_surface_temperature"][now_i]), 1),
            "current_ms": round(float(h["ocean_current_velocity"][now_i]), 2),
            "wind_regime": regimes[now_i],
            "wind_from_deg_now": round(wind_from_now),
            "wind_regime_note": cfg.get("wind_regimes", {}).get(
                regimes[now_i], {}).get("note", ""),
            "sun_elevation_deg": round(float(elev[now_i]), 1),
            "light": round(float(light[now_i]), 2),
        },
        "best_window": window,
        "days": days,
        "utc_offset_seconds": tz_offset,
        "weights": weights_cfg,
        "depth_range_m": [cfg["depth"]["min_m"], cfg["depth"]["max_m"]],
        "entry_points": cfg.get("entry_points", []),
        "repo_edit_url": cfg.get("repo_edit_url"),
        "repo_issue_url": cfg.get("repo_issue_url"),
        "series": {
            "time": h["time"][keep],
            "visibility_m": [round(float(v), 1) for v in viz_m[keep]],
            "wave_height_m": [round(float(v), 2) for v in h["wave_height"][keep]],
            "wind_speed_kmh": [round(float(v), 1) for v in h["wind_speed_10m"][keep]],
            "precipitation_mm": [round(float(v), 1) for v in h["precipitation"][keep]],
            "sea_level_m": [round(float(v), 2) for v in h["sea_level_height_msl"][keep]],
            "workability": [round(float(v), 3) for v in work[keep]],
            "light": [round(float(v), 2) for v in light[keep]],
            "wind_regime": regimes[keep],
        },
        "now_index": now_i - max(0, now_i - 24),
        "sea_temp_now_c": round(sea_temp, 1),
        "species_top_spots": sp_cap,
        "visibility_calibrated": bool(vzc),
        "visibility_correction": vzc,
        "temp_source": ("Copernicus Marine measured profile" if profile
                        else "estimated from SST + seasonal thermocline"),
        "temp_profile": profile,
        "month": int(now_iso[5:7]),
        "spots": spots,
        "species": species_out,
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
