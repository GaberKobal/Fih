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
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

# Imported HERE, once, on the main thread — not lazily inside the functions
# that draw. matplotlib's first import is not thread-safe: with regions
# running in parallel, several workers reached `import matplotlib`
# simultaneously and one of them got a half-built module
# ("partially initialized module 'matplotlib' has no attribute 'rcParams'"),
# which failed that region for the day. Getting it out of the way before any
# worker starts removes the race entirely.
import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure

from scipy.interpolate import RegularGridInterpolator
from scipy.ndimage import (binary_dilation, binary_opening,
                           distance_transform_edt, gaussian_filter,
                           gaussian_filter1d, label, maximum_filter,
                           median_filter)

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

# Windows consoles default to cp1252, which cannot encode the region names in
# regions.json - "Split and Brac channel" is spelled with a c-caron, Kvarner
# with an em dash. Printing one raised UnicodeEncodeError and killed the run
# PART WAY THROUGH, after several regions had already written their output,
# leaving docs/data/ half updated and index.json never written at all. A log
# line must never be able to do that, so the streams are switched to UTF-8
# and told to substitute anything they still cannot represent.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # not a real stream (piped, captured, very old Python)


# With regions running concurrently the log becomes a braid of several
# regions at once, and an untagged line is worse than useless when you are
# trying to work out which coast just refused its coastline. Each worker
# stamps its own id here.
_LOG = threading.local()


def log_tag(tag: str) -> None:
    _LOG.tag = tag


def log(msg: str) -> None:
    tag = getattr(_LOG, "tag", "")
    print(f"[{dt.datetime.now(dt.timezone.utc):%H:%M:%S}] "
          f"{tag}{msg}", flush=True)


# Open-Meteo's free tier allows 600 calls a minute, 5000 an hour and 10000 a
# day, and every region needs two of them. Running regions in parallel means
# those arrive in bursts, and a burst that trips the limit used to surface as
# a bare HTTPError that failed the whole region — losing its map for the day
# over something that would have succeeded a second later. Retry politely
# instead, and honour Retry-After when the server sends one.
_RETRY_STATUS = (408, 425, 429, 500, 502, 503, 504)

# Minimum spacing between requests to a given host, in seconds, enforced
# across ALL worker threads. Open-Meteo's free ceiling is 600 calls a
# minute; six workers each finishing a warm region in about a second would
# issue roughly 720, so the limit gets tripped by success rather than by
# volume. 0.15 s holds the whole process to about 400 a minute no matter how
# many workers are running, which is the difference between adding regions
# and adding failures. Everything else is unthrottled.
_HOST_MIN_INTERVAL = {
    "api.open-meteo.com": 0.15,
    "marine-api.open-meteo.com": 0.15,
}
_HOST_GATE = threading.Lock()
_HOST_LAST: dict = {}


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    gap = _HOST_MIN_INTERVAL.get(host)
    if not gap:
        return
    while True:
        with _HOST_GATE:
            now = time.monotonic()
            ready = _HOST_LAST.get(host, 0.0) + gap
            if now >= ready:
                _HOST_LAST[host] = now
                return
            wait = ready - now
        time.sleep(wait)          # released the lock first, so others queue


def http_get(url: str, timeout: int = 120, attempts: int = 4) -> bytes:
    delay = 2.0
    last = None
    for i in range(attempts):
        try:
            _throttle(url)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in _RETRY_STATUS:
                raise                       # a real error; retrying is pointless
            wait = delay
            hdr = e.headers.get("Retry-After") if e.headers else None
            if hdr:
                try:
                    wait = max(wait, float(hdr))
                except ValueError:
                    pass
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e                        # transient network, worth another go
            wait = delay
        if i + 1 < attempts:
            time.sleep(min(wait, 30.0))
            delay *= 2
    raise last or RuntimeError(f"gave up fetching {url}")


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

def load_regions(path=None):
    doc = json.loads((path or (ROOT / "regions.json")).read_text(encoding="utf-8"))
    return doc.get("regions", [])


def select_regions(regions, one=None, select=""):
    """Work out which regions to compute.

    Three ways in, so that turning a coast on never requires a code change:

      --region <id>     exactly one, the old flag, unchanged
      --select ...      a comma-separated mix of region ids and GROUP names,
                        or "all", or "enabled". Also read from the
                        FISH_REGIONS environment variable, which is what
                        lets the scheduled job be repointed from repository
                        settings without a commit.
      (nothing)         every region with "enabled": true in regions.json

    Order is preserved and duplicates are dropped, so
    "--select adriatic,novigrad" runs the group once.
    """
    if one:
        hit = [r for r in regions if r["id"] == one]
        if not hit:
            raise SystemExit(f"no region with id {one!r} (try --list)")
        return hit

    select = (select or "").strip()
    if not select or select.lower() == "enabled":
        return [r for r in regions if r.get("enabled")]
    if select.lower() == "all":
        return list(regions)

    wanted = [w.strip().lower() for w in select.split(",") if w.strip()]
    ids = {r["id"].lower() for r in regions}
    groups = {(r.get("group") or "").lower() for r in regions} - {""}
    unknown = [w for w in wanted if w not in ids and w not in groups]
    if unknown:
        raise SystemExit(
            f"unknown region or group: {', '.join(unknown)}\n"
            f"  --list shows every region, --groups shows every group")

    out, seen = [], set()
    for r in regions:
        if r["id"] in seen:
            continue
        if r["id"].lower() in wanted or (r.get("group") or "").lower() in wanted:
            out.append(r)
            seen.add(r["id"])
    return out


def region_config(base, region):
    """Merge a region's overrides onto the shared model config.

    config.json holds the model - decay times, weights, thresholds. regions.json
    holds geography and what data that place actually has. Keeping them apart
    means tuning the model once instead of once per region.
    """
    cfg = json.loads(json.dumps(base))            # deep copy
    cfg["region_id"] = region["id"]
    cfg["region_name"] = region["name"]
    cfg["country"] = region.get("country", "")
    cfg["jurisdiction"] = region.get("jurisdiction", "")
    cfg["region_note"] = region.get("note", "")
    cfg["bbox"] = region["bbox"]
    cfg["species_file"] = region.get("species_file")
    cfg["exclusions_file"] = region.get("exclusions_file")
    for key in ("depth", "thermocline", "entry_points", "max_swim_km",
                "weights", "visibility", "overlay", "water", "structure",
                "bathymetry", "shelter", "movement", "contours", "cmems",
                "wind_regimes", "grid_resolution_m", "coastline", "wrecks"):
        if key in region:
            if region[key] is None:
                cfg[key] = None                    # explicit "not applicable here"
            elif isinstance(region[key], dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(region[key])
            else:
                cfg[key] = region[key]
    return cfg


def build_target_grid(bbox: dict, res_m: float):
    """Grid with (approximately) square metre cells at the box's centre latitude."""
    lat0 = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    dlat = res_m / 111320.0
    dlon = res_m / (111320.0 * math.cos(math.radians(lat0)))
    lats = np.arange(bbox["lat_min"], bbox["lat_max"] + 0.5 * dlat, dlat)
    lons = np.arange(bbox["lon_min"], bbox["lon_max"] + 0.5 * dlon, dlon)
    return lats, lons


def raster_bounds(lats, lons):
    """Geographic OUTER edges of a centre-sampled raster.

    Scores and spot coordinates live at cell centres.  Leaflet ImageOverlay,
    on the other hand, maps the outer pixels to its supplied bounds.  Giving
    it the requested bbox shifts the whole image by roughly half a cell (and
    can put the coastal score band visibly on land).  Publish the true raster
    edges so the PNG, contours and point coordinates share one reference.
    """
    dlat = float(np.median(np.diff(lats)))
    dlon = float(np.median(np.diff(lons)))
    return [[float(lats[0] - dlat / 2), float(lons[0] - dlon / 2)],
            [float(lats[-1] + dlat / 2), float(lons[-1] + dlon / 2)]]


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

# EMODnet covers European seas at ~115 m. Everywhere else the best free
# numeric source is SRTM15+ at 15 arc-seconds, which is 300-450 m depending on
# latitude. That is not a small difference: the relief term exists to find
# mounds a few cells across, and at 450 m a reef is smaller than one cell.
EMODNET_FOOTPRINT = {"lon_min": -36.0, "lat_min": 24.0,
                     "lon_max": 43.0, "lat_max": 90.0}
ERDDAP_BASE = "https://coastwatch.pfeg.noaa.gov/erddap/griddap"


def inside(bbox, outer):
    return (bbox["lon_min"] >= outer["lon_min"] and bbox["lon_max"] <= outer["lon_max"]
            and bbox["lat_min"] >= outer["lat_min"] and bbox["lat_max"] <= outer["lat_max"])


def choose_provider(bbox, cfg):
    """EMODnet where it reaches, the global grid otherwise."""
    forced = (cfg.get("bathymetry") or {}).get("provider")
    if forced and forced != "auto":
        return forced
    return "emodnet" if inside(bbox, EMODNET_FOOTPRINT) else "gmrt"


def parse_erddap_json(raw):
    payload = json.loads(raw)
    cols = payload["table"]["columnNames"]
    rows = payload["table"]["rows"]
    li, oi = cols.index("latitude"), cols.index("longitude")
    vi = next(i for i, c in enumerate(cols) if i not in (li, oi))
    lats = sorted({r[li] for r in rows})
    lons = sorted({r[oi] for r in rows})
    grid = np.full((len(lats), len(lons)), np.nan)
    ly = {v: i for i, v in enumerate(lats)}
    lx = {v: i for i, v in enumerate(lons)}
    for r in rows:
        if r[vi] is not None:
            grid[ly[r[li]], lx[r[oi]]] = r[vi]
    return np.array(lats), np.array(lons), grid


def fetch_erddap_bathymetry(bbox, cfg):
    """Global 15 arc-second bathymetry via NOAA ERDDAP griddap.

    Values are elevation: negative below sea level, matching EMODnet, so the
    rest of the pipeline does not care which provider it came from.
    """
    b = (cfg.get("bathymetry") or {})
    dataset = b.get("erddap_dataset", "srtm15plus")
    var = b.get("erddap_variable", "z")
    lon_min, lon_max = bbox["lon_min"], bbox["lon_max"]

    # The Lon0360 twin exists for the Pacific; a box that crosses the
    # antimeridian is unrepresentable in -180..180.
    if lon_min > lon_max or lon_min < -180 or lon_max > 180:
        dataset = b.get("erddap_dataset_360", "srtm15plus_Lon0360")
        lon_min %= 360.0
        lon_max %= 360.0

    query = (f"{var}"
             f"[({bbox['lat_min']}):1:({bbox['lat_max']})]"
             f"[({lon_min}):1:({lon_max})]")
    url = f"{ERDDAP_BASE}/{dataset}.json?{urllib.parse.quote(query, safe='(),:.-')}"
    log(f"bathymetry: requesting {dataset} from NOAA ERDDAP (global tier)")
    lats, lons, elev = parse_erddap_json(http_get(url, timeout=300))
    lons = np.where(lons > 180.0, lons - 360.0, lons)   # back to -180..180
    order = np.argsort(lons)
    return lats, lons[order], elev[:, order]


GMRT_URL = "https://www.gmrt.org/services/GridServer"


def fetch_gmrt_bathymetry(bbox, cfg):
    """Global Multi-Resolution Topography, to 100 m where multibeam exists.

    GMRT merges cleaned multibeam from research cruises worldwide over a
    GEBCO/SRTM base, so the resolution you actually get depends on whether
    anyone has surveyed that coast. That is fine and in fact the point: the
    grid is measured after it arrives rather than assumed, so a well-surveyed
    coast gets a real structure map and an unsurveyed one is told it is only
    seeing slope.
    """
    import rasterio
    b = (cfg.get("bathymetry") or {})
    params = {
        "north": bbox["lat_max"], "south": bbox["lat_min"],
        "west": bbox["lon_min"], "east": bbox["lon_max"],
        "layer": "topo",                      # includes land; we mask on elev >= 0
        "format": "geotiff",
        "resolution": b.get("gmrt_resolution", "high"),
    }
    url = f"{GMRT_URL}?{urllib.parse.urlencode(params)}"
    log("bathymetry: requesting GMRT (multibeam where it exists)")
    raw = http_get(url, timeout=300)
    if raw[:4] not in (b"II*\x00", b"MM\x00*"):
        raise RuntimeError(f"GMRT returned a non-TIFF response: {raw[:200]!r}")

    with rasterio.MemoryFile(raw) as mem, mem.open() as ds:
        elev = ds.read(1).astype(float)
        if ds.nodata is not None:
            elev[elev == ds.nodata] = np.nan
        elev[np.abs(elev) > 12000] = np.nan
        t = ds.transform
        lons = t.c + t.a * (np.arange(ds.width) + 0.5)
        lats = t.f + t.e * (np.arange(ds.height) + 0.5)
    if lats[0] > lats[-1]:
        lats, elev = lats[::-1], elev[::-1, :]
    return lats, lons, elev


def native_resolution_m(lats, lons):
    """Actual cell size of whatever came back, rather than what we asked for."""
    lat0 = float(np.mean(lats))
    dy = float(np.abs(np.diff(lats)).mean()) * 111320.0
    dx = float(np.abs(np.diff(lons)).mean()) * 111320.0 * math.cos(math.radians(lat0))
    return float(np.hypot(dy, dx) / math.sqrt(2))


def fetch_bathymetry(bbox, res_deg, cache_key, cfg):
    """One entry point; the provider is an implementation detail downstream."""
    provider = choose_provider(bbox, cfg)
    cache_file = CACHE / f"bathy_{provider}_{cache_key}.npz"
    if cache_file.exists():
        log(f"bathymetry: cache hit ({cache_file.name})")
        z = np.load(cache_file)
        return z["lats"], z["lons"], z["elev"], provider, float(z["native_m"])

    chain = {"emodnet": [("emodnet", lambda: fetch_emodnet_bathymetry(bbox, res_deg, cache_key)),
                         ("gmrt",    lambda: fetch_gmrt_bathymetry(bbox, cfg)),
                         ("erddap",  lambda: fetch_erddap_bathymetry(bbox, cfg))],
             "gmrt":    [("gmrt",    lambda: fetch_gmrt_bathymetry(bbox, cfg)),
                         ("erddap",  lambda: fetch_erddap_bathymetry(bbox, cfg))],
             "erddap":  [("erddap",  lambda: fetch_erddap_bathymetry(bbox, cfg))]}[provider]

    lats = lons = elev = None
    for name, fn in chain:
        try:
            lats, lons, elev = fn()
            provider = name
            break
        except Exception as e:
            log(f"bathymetry: {name} failed ({e.__class__.__name__}: {e})"
                + (" - trying the next source" if fn is not chain[-1][1] else ""))
    if elev is None:
        raise RuntimeError("every bathymetry source failed for this region")

    native = native_resolution_m(lats, lons)
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache_file, lats=lats, lons=lons, elev=elev,
                        native_m=native)
    log(f"bathymetry: {provider}, {elev.shape[0]}x{elev.shape[1]} cells, "
        f"native ~{native:.0f} m")
    return lats, lons, elev, provider, native


def fetch_emodnet_bathymetry(bbox: dict, res_deg: float, cache_key: str):
    """EMODnet Bathymetry DTM via WCS 1.0.0, as GeoTIFF.

    Values are elevation: negative below sea level, positive on land.
    Static data, so it is cached and never refetched.
    """
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
    wcfg = (cfg.get("water") or {})
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


def fetch_from(land, lats, lons, from_deg, cfg, nodata=None):
    """Distance to land along the ray a wave or wind is arriving from.

    Shared by wind and swell: both are 'how much open water is upstream of
    this cell', they just come from different directions.
    """
    max_km = cfg["shelter"]["max_fetch_km"]
    step_m = cfg["shelter"]["fetch_step_m"]
    n_steps = int(max_km * 1000 / step_m)

    th = math.radians(float(from_deg) % 360.0)
    east, north = math.sin(th), math.cos(th)
    lat0 = float(np.mean(lats))
    dlat = (north * step_m) / 111320.0
    dlon = (east * step_m) / (111320.0 * math.cos(math.radians(lat0)))

    blocks = land if nodata is None else (land & ~nodata)
    lat_step, lon_step = lats[1] - lats[0], lons[1] - lons[0]
    ny, nx = land.shape
    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    fetch = np.full(gy.shape, max_km * 1000.0)
    done = blocks.copy()
    cur_lat, cur_lon = gy.copy(), gx.copy()

    for step in range(1, n_steps + 1):
        cur_lat += dlat
        cur_lon += dlon
        iy = np.rint((cur_lat - lats[0]) / lat_step).astype(int)
        ix = np.rint((cur_lon - lons[0]) / lon_step).astype(int)
        outside = (iy < 0) | (iy >= ny) | (ix < 0) | (ix >= nx)
        hit = blocks[np.clip(iy, 0, ny - 1), np.clip(ix, 0, nx - 1)] & ~outside & ~done
        fetch[hit] = step * step_m
        done |= hit
        if done.all():
            break
    return fetch


def combined_shelter(land, lats, lons, wind_from, swell_from, cfg, nodata=None):
    """Shelter from wind chop AND from swell, which rarely agree.

    Wind fetch alone missed the common case here: a light onshore breeze with
    an old swell still running in from a different quarter. The two are traced
    separately and the worse one dominates.
    """
    max_m = cfg["shelter"]["max_fetch_km"] * 1000.0
    w = 1.0 - np.clip(fetch_from(land, lats, lons, wind_from, cfg, nodata) / max_m, 0, 1)
    sw_weight = float(cfg["shelter"].get("swell_weight", 0.5))
    if swell_from is None or not np.isfinite(swell_from) or sw_weight <= 0:
        w[land] = 0.0
        return w, None
    sw = 1.0 - np.clip(fetch_from(land, lats, lons, swell_from, cfg, nodata) / max_m, 0, 1)
    both = (1.0 - sw_weight) * w + sw_weight * sw
    both[land] = 0.0
    return both, sw


def depth_contours(depth_m, land, lats, lons, cfg):
    """Depth lines as GeoJSON, so the map shows where the shelf actually is.

    A colour wash tells you a cell scores well; a 10 m line tells you whether
    you can reach the bottom. Decimated hard - these are for orientation, not
    navigation.
    """
    c = (cfg.get("contours") or {})
    levels = c.get("levels_m", [5, 10, 15, 20, 30])
    every = max(1, int(c.get("decimate", 3)))
    min_pts = int(c.get("min_points", 8))

    # Fill land with the mean water depth BEFORE smoothing, then mask it out
    # afterwards — the same technique buildTerrain()/contourLines() use in
    # docs/structure.js. Smoothing a -5 m sentinel into the coast built a
    # false wall there, and every shallow level picked up a spurious ring
    # running along the shoreline; masking alone would not have helped,
    # because the damage was already baked into the blurred field. Masked
    # cells are then genuinely skipped by contour(), so no line is ever drawn
    # across dry land.
    water = ~land
    mean_d = float(np.nanmean(depth_m[water])) if water.any() else 0.0
    filled = np.where(water, np.nan_to_num(depth_m, nan=mean_d), mean_d)
    field = np.ma.masked_array(gaussian_filter(filled, sigma=1.5), mask=land)

    feats = []
    fig = Figure()
    try:
        cs = fig.add_subplot(111).contour(lons, lats, field, levels=levels)
        for lvl, segs in zip(cs.levels, cs.allsegs):
            for seg in segs:
                if len(seg) < min_pts:
                    continue
                pts = seg[::every]
                if len(pts) < 3:
                    continue
                feats.append({
                    "type": "Feature",
                    "properties": {"depth_m": float(lvl)},
                    "geometry": {"type": "LineString",
                                 "coordinates": [[round(float(x), 5),
                                                  round(float(y), 5)] for x, y in pts]},
                })
    finally:
        fig.clear()          # a bare Figure holds no global state to close
    return {"type": "FeatureCollection", "features": feats}


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
        gj = json.loads(p.read_text(encoding="utf-8"))
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


# Overpass is a free, shared, volunteer-run service that rate-limits per
# client. Every region in regions.json is enabled, so a daily run makes one
# request per region back to back; on a single host most of them come back
# 429 and the regions silently lose their coastline, their wrecks, or both.
# These are the public mirrors, all serving the same planet database.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


# Overpass gives each client a small number of concurrent query slots — two,
# typically — and hands out 429s beyond that. Six workers each asking for a
# coastline at once would spend most of its budget being refused, so the
# whole process is held to two in flight regardless of worker count. The
# other workers are computing while they wait, so this costs almost nothing.
_OVERPASS_SLOTS = threading.Semaphore(2)


def overpass_query(q, timeout=75, deadline_s=200):
    """POST one Overpass query, walking the mirrors once each.

    Bounded on purpose. Some coastlines are enormous — Lofoten is thousands
    of islands and skerries — and Overpass can sit on a query like that for
    minutes before answering or giving up. With a 180 s socket timeout, three
    mirrors and two rounds of retries, ONE region could occupy a worker for
    the better part of twenty minutes and stall the whole run behind it.

    So: one pass over the mirrors, a shorter per-request timeout, and a hard
    overall deadline. Coming back empty-handed is not a disaster — the caller
    falls back to the bathymetric land edge and says so, and the spot gates
    in find_spots() still hold the line. Waiting forever is worse than that.
    """
    started = time.monotonic()
    last = None
    with _OVERPASS_SLOTS:
        for url in OVERPASS_MIRRORS:
            if time.monotonic() - started > deadline_s:
                break
            try:
                req = urllib.request.Request(
                    url, data=urllib.parse.urlencode({"data": q}).encode(),
                    headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in (429, 502, 503, 504):
                    raise                  # a real error, not congestion
            except Exception as e:
                last = e
    raise last or RuntimeError("no Overpass mirror answered in time")


def fetch_osm_context(bbox, cfg, region_id):
    """Coastline and wrecks from OpenStreetMap, in ONE request.

    The coastline is here because bathymetry cannot answer "is this land". A
    grid cell reports the average of what is under it, so any strip of land
    narrower than a cell or two averages to a negative elevation and is
    scored as good diving water. Barrier islands, spits, breakwaters and
    reclaimed land all vanish that way, and the wash, the depth contours and
    the model spots end up on shore.

    Fetching it together with the wrecks matters as much as the data does.
    These were two separate POSTs to the same endpoint moments apart, and
    with 55 regions enabled that is 110 requests per run: the second call
    routinely came back 429, so a region would get its coastline and lose
    its wrecks, or the reverse. One request per region cannot rate-limit
    itself against its own sibling.

    Both halves are cached separately, because they are consumed at
    different points and neither moves.

    Returns (coastline_lines, wrecks_geojson).
    """
    cf = CACHE / f"coastline_{region_id}.json"
    wf = CACHE / f"wrecks_{region_id}.geojson"
    if cf.exists() and wf.exists():
        return (json.loads(cf.read_text(encoding="utf-8")),
                json.loads(wf.read_text(encoding="utf-8")))

    b = (f'{bbox["lat_min"]},{bbox["lon_min"]},'
         f'{bbox["lat_max"]},{bbox["lon_max"]}')
    q = f"""[out:json][timeout:60];
(
  way["natural"="coastline"]({b});
  node["seamark:type"="wreck"]({b});
  way["seamark:type"="wreck"]({b});
  node["historic"="wreck"]({b});
  way["historic"="wreck"]({b});
  node["seamark:type"="obstruction"]({b});
);
out geom tags;"""
    doc = overpass_query(q)

    lines, feats = [], []
    for el in doc.get("elements", []):
        tg = el.get("tags", {}) or {}
        geom = el.get("geometry") or []
        if tg.get("natural") == "coastline":
            if len(geom) >= 2:
                lines.append([[p["lon"], p["lat"]] for p in geom])
            continue
        # `out geom` gives nodes their lat/lon and ways their full geometry,
        # so a wreck mapped as a way is averaged to a centre here instead of
        # needing a second `out center` pass. `or` would treat a coordinate
        # of exactly 0.0 as missing, which drops everything on the Greenwich
        # meridian and the equator, so test for None.
        lat, lon = el.get("lat"), el.get("lon")
        if lat is None and geom:
            lat = sum(p["lat"] for p in geom) / len(geom)
            lon = sum(p["lon"] for p in geom) / len(geom)
        if lat is None or lon is None:
            continue
        depth = tg.get("seamark:wreck:depth") or tg.get("depth")
        try:
            depth = float(depth) if depth is not None else None
        except (TypeError, ValueError):
            depth = None
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            "properties": {
                "name": tg.get("seamark:name") or tg.get("name") or "wreck",
                "kind": tg.get("seamark:type") or tg.get("historic") or "wreck",
                "category": tg.get("seamark:wreck:category"),
                "depth_m": depth,
                "osm_id": el.get("id"),
            },
        })

    wrecks = {"type": "FeatureCollection", "features": feats}
    CACHE.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(lines), encoding="utf-8")
    wf.write_text(json.dumps(wrecks), encoding="utf-8")
    return lines, wrecks


def coastline_mask(lines, lats, lons):
    """Burn the coastline polylines onto the grid as an impassable wall.

    Sampled at half-cell steps so a segment running diagonally cannot slip
    between two cells. One gap is all it takes for the flood fill in
    open_sea_mask() to escape inland, which would make the mask worse than
    having none.
    """
    ny, nx = len(lats), len(lons)
    blocked = np.zeros((ny, nx), dtype=bool)
    if not lines or nx < 2 or ny < 2:
        return blocked
    lat0, lon0 = float(lats[0]), float(lons[0])
    dlat, dlon = float(lats[1] - lats[0]), float(lons[1] - lons[0])

    # Every segment of every way at once. This was a Python loop per segment,
    # which is fine for the 6,800 vertices Novigrad's coastline has and not
    # fine for the tens of thousands a fjord or an archipelago brings — the
    # Bay of Islands alone is 610 ways and 25,757 vertices, and Lofoten is
    # far worse. Vectorised it is one pass over a handful of arrays.
    xs, ys = [], []
    for line in lines:
        arr = np.asarray(line, dtype=float)
        if arr.ndim != 2 or arr.shape[0] < 2:
            continue
        xs.append((arr[:, 0] - lon0) / dlon)
        ys.append((arr[:, 1] - lat0) / dlat)
    if not xs:
        return blocked

    x0 = np.concatenate([a[:-1] for a in xs])
    x1 = np.concatenate([a[1:] for a in xs])
    y0 = np.concatenate([a[:-1] for a in ys])
    y1 = np.concatenate([a[1:] for a in ys])

    # Sample each segment at half-cell steps so a diagonal cannot slip
    # between two cells and leave a hole for the flood fill to escape.
    steps = np.maximum(1, np.ceil(2 * np.maximum(np.abs(x1 - x0),
                                                 np.abs(y1 - y0))).astype(np.int64))
    counts = steps + 1

    # Chunked so a pathological coastline cannot allocate a gigabyte here.
    CHUNK = 2_000_000
    start = 0
    while start < len(counts):
        stop = start
        taken = 0
        while stop < len(counts) and taken + counts[stop] <= CHUNK:
            taken += int(counts[stop])
            stop += 1
        stop = max(stop, start + 1)              # always make progress
        c = counts[start:stop]
        total = int(c.sum())
        seg = np.repeat(np.arange(stop - start), c)
        offs = np.concatenate(([0], np.cumsum(c)[:-1]))
        t = (np.arange(total) - offs[seg]) / np.maximum(c[seg] - 1, 1)
        sx0, sx1 = x0[start:stop], x1[start:stop]
        sy0, sy1 = y0[start:stop], y1[start:stop]
        xi = np.rint(sx0[seg] + (sx1[seg] - sx0[seg]) * t).astype(np.int64)
        yi = np.rint(sy0[seg] + (sy1[seg] - sy0[seg]) * t).astype(np.int64)
        ok = (xi >= 0) & (xi < nx) & (yi >= 0) & (yi < ny)
        blocked[yi[ok], xi[ok]] = True
        start = stop
    return blocked



def wreck_score(gj, lats, lons, cfg):
    """A soft halo around each charted wreck, tapering over a swimmable radius."""
    c = (cfg.get("wrecks") or {})
    radius = float(c.get("radius_m", 250.0))
    pts = [f["geometry"]["coordinates"] for f in gj.get("features", [])]
    field = np.zeros((len(lats), len(lons)))
    if not pts:
        return field
    gy, gx = np.meshgrid(lats, lons, indexing="ij")
    for lon, lat in pts:
        d = haversine_m(lat, lon, gy, gx)
        field = np.maximum(field, np.clip(1.0 - d / radius, 0.0, 1.0))
    return field


def fetch_seabed_habitat(bbox, cfg):
    """Optional EUSeaMap polygons from EMODnet Seabed Habitats (cached).

    Off by default: the WFS layer names change between EUSeaMap releases,
    so this is the most likely thing to break. If it fails, the run
    continues without a habitat term.
    """
    cache_file = CACHE / "habitat.geojson"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
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
    cache_file.write_text(json.dumps(gj), encoding="utf-8")
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
        "wind_gusts_10m", "cloud_cover", "pressure_msl",
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


def light_quality(elev_deg, cfg, cloud_pct=None):
    """0 when the sun is down, best at low sun, worst at high noon.

    Hard zero below the horizon is deliberate and not just about fish:
    spearfishing at night is prohibited in Croatia.

    Cloud cover, when available, eases the high-noon penalty rather than
    fixing it at a constant: heavy overcast diffuses the overhead glare that
    the penalty exists to model in the first place. It never raises
    dawn/dusk quality above 1.0 and never reopens the night gate.
    """
    c = cfg.get("light", {})
    golden = c.get("golden_max_deg", 12.0)
    high = c.get("high_sun_deg", 45.0)
    floor = c.get("high_sun_factor", 0.6)

    floor_eff = floor
    if cloud_pct is not None:
        soften = c.get("cloud_softening", 0.5)
        cloud_frac = np.clip(np.nan_to_num(np.asarray(cloud_pct, dtype=float),
                                           nan=0.0) / 100.0, 0.0, 1.0)
        floor_eff = floor + (1.0 - floor) * soften * cloud_frac

    q = 1.0 - (1.0 - floor_eff) * np.clip((elev_deg - golden) / max(high - golden, 1e-6), 0, 1)
    return np.where(elev_deg > 0, q, 0.0)


def classify_wind(dir_deg, speed_kmh, gust_kmh, cfg):
    """Name the wind, because on this coast the name carries the forecast.

    Bora off the land, jugo onshore and warm, maestral the afternoon thermal.
    They do very different things to visibility and to whether you can work.
    """
    regs = cfg.get("wind_regimes") or {}
    if not regs:
        # bora, jugo and maestral mean nothing in Hawaii. With no named set the
        # wind still counts through fetch and workability, it just is not
        # given a local name or a clarity multiplier.
        return {"name": "", "viz_factor": 1.0, "work_factor": 1.0,
                "gust_ratio": 1.0, "note": ""}
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

    # Baseline turbidity: what the water carries when NOTHING has happened.
    #
    # Without this the model starts from perfectly clear water and only ever
    # subtracts, so a calm dry day scored turbidity 0.003 and visibility
    # pinned at viz_max_m - the ceiling - every single time. That is why the
    # number always looked optimistic: it was not a forecast, it was the
    # maximum, reported whenever there had been no recent swell or rain.
    #
    # Real coastal water is never optically clean. There is always plankton,
    # always some resuspension from tidal and wind-driven currents, always
    # river and land input, and on a shallow shelf always fine sediment that
    # never fully settles. baseline is that floor, 0-1, and it is the single
    # most region-specific number in this file: a turbid northern Adriatic
    # shelf and a Cycladic drop-off are not the same water. Override it per
    # region in regions.json.
    turbidity = (float(c.get("baseline", 0.0))
                 + c["wave_weight"] * norm(stir, 0.0, c["wave_ref_hs"] ** 3)
                 + c["rain_weight"] * norm(plume, 0.0, c["rain_ref_mm_per_h"]))
    viz = np.clip(1.0 - turbidity, 0.0, 1.0)
    # rough metres, for a number you can actually reason about
    viz_m = c["viz_min_m"] + viz * (c["viz_max_m"] - c["viz_min_m"])
    return viz, viz_m


def workability_series(h, cfg):
    c = cfg["workability"]
    hs = np.nan_to_num(h["wave_height"], nan=1.0)
    wind = np.nan_to_num(h["wind_speed_10m"], nan=30.0)

    # A given significant height feels very different depending on period:
    # short-period wind chop is steep and tiring to work in, long-period
    # ground swell of the same height is smooth. Scale the height fed to the
    # comfort test by how its period compares to a reference, rather than
    # judging height alone. Bounded so a bad/missing reading cannot swing the
    # result too far — this is a real but secondary effect next to height.
    ref = c.get("period_ref_s", 8.0)
    period = np.nan_to_num(h.get("wave_period", np.array([])), nan=ref)
    if period.size != hs.size:
        period = np.full_like(hs, ref)
    period = np.clip(period, 2.0, 20.0)
    hs_eff = hs * np.clip(ref / period, 0.6, 1.6)

    sea = 1.0 - norm(hs_eff, c["hs_good_m"], c["hs_bad_m"])
    air = 1.0 - norm(wind, c["wind_good_kmh"], c["wind_bad_kmh"])
    return np.minimum(sea, air), sea, air


def pressure_factor_series(h, cfg):
    """A falling barometer ahead of a front is a well-worn angling
    heuristic; the evidence for it is much weaker than for visibility or
    sea state, so this only ever nudges the composite score by a few
    percent. It is applied to the score alone — never to workability,
    never to the verdict gate, and it is never surfaced as a 'limited by'
    reason. Returns None (no-op) if pressure was not fetched.
    """
    c = cfg.get("pressure") or {}
    if "pressure_msl" not in h:
        return None
    p = np.nan_to_num(np.asarray(h["pressure_msl"], dtype=float), nan=np.nan)
    if np.all(np.isnan(p)):
        return None
    win = max(1, int(c.get("trend_window_h", 3)))
    sens = c.get("sensitivity", 0.02)
    cap = c.get("max_swing", 0.08)
    trend = np.full(p.shape, np.nan)          # hPa/h; negative = falling
    if p.size > win:
        trend[win:] = (p[win:] - p[:-win]) / win
    trend = np.nan_to_num(trend, nan=0.0)
    return 1.0 + np.clip(-trend * sens, -cap, cap)


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
    pf = pressure_factor_series(h, cfg)
    if pf is not None:
        base = base * pf
    hourly = base * (work > cfg["workability"]["hard_floor"])
    hourly = hourly * (light if light is not None
                       else daylight_mask(times, daily).astype(float))

    future = np.array([t >= now_iso for t in times])
    if not future.any():
        return None
    start0 = int(np.argmax(future))   # index of the first future hour

    win = cfg["conditions"]["window_hours"]
    avail = hourly[start0:]
    if len(avail) < win:
        return None
    kern = np.ones(win) / win
    rolled = np.convolve(avail, kern, mode="valid")
    if rolled.max() <= 0:
        # Do not manufacture a night-time "best" window just because the
        # weather is calm. A zero is useful: no daylight dive window remains.
        return None
    j = int(np.argmax(rolled))
    i = j + start0
    return {
        "start": times[i],
        "end": times[i + win - 1],
        "score": round(float(rolled[j]), 3),
        "wave_height_m": round(float(np.nanmean(h["wave_height"][i:i + win])), 2),
        "wind_speed_kmh": round(float(np.nanmean(h["wind_speed_10m"][i:i + win])), 1),
        "sea_temp_c": round(float(np.nanmean(h["sea_surface_temperature"][i:i + win])), 1),
    }


def hourly_scores(h, daily, viz, work, move, cfg, light=None):
    """The blended dive score for every hour in the series."""
    base = (cfg["day_weights"]["visibility"] * viz
            + cfg["day_weights"]["workability"] * work
            + cfg["day_weights"]["movement"] * move)
    pf = pressure_factor_series(h, cfg)
    if pf is not None:
        base = base * pf
    ok = work > cfg["workability"]["hard_floor"]
    if light is None:
        light = daylight_mask(h["time"], daily).astype(float)
    # Light is a quality factor now, not a yes/no: it zeroes the dark hours
    # and still prefers dawn and dusk over flat midday sun.
    return base * ok * light, ok, light


def daily_breakdown(h, daily, viz, viz_m, work, move, cfg, now_iso,
                    light=None, regimes=None, sea_ok=None, air_ok=None):
    """Group the forecast into local days so you can pick tomorrow's hour.

    Times are already local because the API is queried with timezone=auto,
    so slicing the date off the timestamp is enough.

    Each hour also carries its own verdict/limited_by, computed the same way
    as the headline "now" verdict. The page only regenerates once a day
    (see .github/workflows/update.yml), so by the time someone loads it the
    real local clock has usually moved on from generation time. Shipping a
    verdict per hour lets the frontend re-anchor "now" to the real clock
    instead of presenting a stale, possibly pre-dawn, snapshot for the next
    24 hours.
    """
    scores, ok, light = hourly_scores(h, daily, viz, work, move, cfg, light)
    win = cfg["conditions"]["window_hours"]
    today = now_iso[:10]
    wind_dir = h.get("wind_direction_10m")
    current = h.get("ocean_current_velocity")

    by_date = {}
    for i, t in enumerate(h["time"]):
        if t[:10] < today:
            continue                       # past days are history, not a plan
        by_date.setdefault(t[:10], []).append(i)

    days = []
    for date, idx in sorted(by_date.items()):
        hours = []
        for k, i in enumerate(idx):
            # Is the sun coming back up again later in THIS day? Passing a
            # flat True here labelled 22:00 "waiting for first light", which
            # is only true at 04:00. The frontend re-anchors "now" onto one
            # of these hour records (see reanchorNow() in docs/index.html),
            # so a wrong answer here becomes the wrong headline all evening.
            more_light = any(float(light[j]) > 0 for j in idx[k + 1:])
            vw, lim, _ = verdict(
                float(viz[i]), float(viz_m[i]), float(work[i]),
                float(sea_ok[i]) if sea_ok is not None else 1.0,
                float(air_ok[i]) if air_ok is not None else 1.0,
                cfg, float(light[i]), more_light)
            wd = float(wind_dir[i]) if wind_dir is not None else float("nan")
            cur = float(current[i]) if current is not None else float("nan")
            hours.append({
                "t": h["time"][i],
                "hour": int(h["time"][i][11:13]),
                "score": round(float(scores[i]), 3),
                "verdict": vw,
                "limited_by": lim,
                "viz_m": round(float(viz_m[i]), 1),
                "wave_m": round(float(h["wave_height"][i]), 2),
                "wind_kmh": round(float(h["wind_speed_10m"][i]), 1),
                "wind_from_deg": round(wd) if np.isfinite(wd) else None,
                "sea_temp_c": round(float(h["sea_surface_temperature"][i]), 1),
                "current_ms": round(cur, 2) if np.isfinite(cur) else None,
                "daylight": bool(light[i] > 0),
                "light": round(float(light[i]), 2),
                "wind_regime": (regimes[i] if regimes else None),
                "workable": bool(ok[i]),
                "past": h["time"][i] < now_iso,
            })

        # best contiguous block of `win` hours inside this day, entirely future
        best = None
        if len(hours) >= win:
            sums = [(sum(x["score"] for x in hours[j:j + win]) / win, j)
                    for j in range(len(hours) - win + 1)
                    if not any(x["past"] for x in hours[j:j + win])]
            if sums:
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


def verdict(viz_now, viz_m_now, work_now, sea_now, air_now, cfg,
            light_now=1.0, daylight_left=True):
    """A single word plus the reason, because that is the actual decision.

    daylight_left is an ASTRONOMICAL fact — is the sun going to be up again
    before midnight local — and nothing else. It used to be computed as
    "light AND workable", which meant a blown-out but brilliantly sunny
    midday was reported as "no diveable light left today": a statement about
    the sun, made because of the wind, on a day the user could see was only
    half over. Whether the sea is diveable is the separate branch below, and
    "nothing diveable today" is already stated per-day in the hour strip.
    """
    day = min(viz_now, work_now)

    # Conditions alone are not a verdict. "Go" at 22:00 is wrong however flat
    # the sea is, and it contradicted the panel below saying the day was over.
    if light_now <= 0:
        return ("not now",
                "dark — waiting for first light" if daylight_left
                else "dark — no daylight left today", day)

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
    name = cfg.get("species_file")
    if not name:
        # Deliberate: a region without a validated pack shows structure and
        # conditions only. Croatian closed seasons in Greek water would be
        # worse than no species view at all.
        return []
    path = ROOT / name
    if not path.exists():
        log(f"species: {name} not found - running structure only")
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    wanted = cfg.get("species_ids")          # null/absent = all of them
    out = [sp for sp in doc.get("species", [])
           if not wanted or sp["id"] in wanted]

    # Law is per jurisdiction, biology is not. Merge the right pack in here so
    # the same species file can serve any Mediterranean coast.
    law = load_legal(cfg)
    for sp in out:
        sp["legal"] = dict(law.get("species", {}).get(sp["id"], {}))
        sp["legal"]["jurisdiction"] = law.get("jurisdiction", "")
        sp["legal"]["seasons_researched"] = law.get("closed_seasons_researched", False)
    return out


def load_legal(cfg):
    """Fishing rules for this region's jurisdiction.

    Returns an empty pack rather than guessing if there is no file: showing a
    Croatian closed season in Greek water is worse than showing nothing.
    """
    cc = (cfg.get("jurisdiction") or "").upper()
    # Prefer the documented legal/ directory, but accept country packs at the
    # repository root too. Older exports used the latter layout.
    candidates = [ROOT / "legal" / f"{cc}.json", ROOT / f"{cc}.json"]
    path = next((p for p in candidates if cc and p.exists()), None)
    if path is None:
        log(f"legal: no rules pack for jurisdiction {cc or '(none)'} - "
            "sizes and seasons will not be shown")
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    n = sum(1 for v in doc.get("species", {}).values() if v.get("closed_seasons"))
    log(f"legal: {cc} pack ({doc.get('confidence')}), "
        f"{n} species with a closed season"
        + ("" if doc.get("closed_seasons_researched")
           else " - SEASONS NOT RESEARCHED for this country"))
    return doc


def fetch_cmems_profile(lat, lon, cfg):
    """Measured temperature profile at depth from Copernicus Marine.

    Replaces the guessed thermocline drop with the real thing. Needs a free
    Copernicus account; credentials come from the environment, so nothing
    secret is ever committed. Returns None on any problem and the caller
    falls back to the estimate.
    """
    c = (cfg.get("cmems") or {})
    if not c.get("enabled"):
        return None
    if not (os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME")
            and os.environ.get("COPERNICUSMARINE_SERVICE_PASSWORD")):
        log("cmems: enabled but no credentials in the environment "
            "- falling back to the thermocline estimate")
        return None

    # Keyed by region as well as by date. It was keyed by date alone, so with
    # more than one region enabled the SECOND region loaded the FIRST one's
    # profile from cache — the Adriatic thermocline silently applied to the
    # Azores, and every region after the first got a temperature at depth
    # from somewhere else entirely.
    region = cfg.get("region_id", "default")
    cache_file = CACHE / f"cmems_{region}_{dt.date.today().isoformat()}.json"
    if cache_file.exists():
        log("cmems: cache hit for today")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    # The Mediterranean physics product does not cover the Atlantic, Pacific
    # or Indian oceans, so outside its footprint the request came back empty
    # and every non-Med region fell back to the Adriatic thermocline guess.
    # config.json has carried dataset_id_global all along without anything
    # ever reading it.
    med = (-6.0 <= lon <= 36.5) and (30.0 <= lat <= 46.0)
    dataset = c["dataset_id"] if med else (c.get("dataset_id_global")
                                           or c["dataset_id"])
    if not med and dataset != c["dataset_id"]:
        log(f"cmems: outside the Mediterranean product, using {dataset}")

    try:
        import copernicusmarine
        pad = c.get("pad_deg", 0.1)
        today = dt.date.today()
        ds = copernicusmarine.open_dataset(
            dataset_id=dataset,
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
               "source": dataset, "fetched": today.isoformat()}
        CACHE.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(out), encoding="utf-8")
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
    if not L.get("seasons_researched", True):
        # No closure data for this country. Say so rather than implying open.
        return {"open": True, "reason": "", "reopens": None, "unknown": True}
    md = f"{on_date.month:02d}-{on_date.day:02d}"
    for w in L.get("closed_seasons", []):
        a, b = w["from"], w["to"]
        closed = (a <= md <= b) if a <= b else (md >= a or md <= b)
        if closed:
            y = on_date.year + (1 if b < a and md >= a else 0)
            return {"open": False,
                    "reason": f"closed season {a} to {b}",
                    "reopens": f"{y}-{b}"}
    return {"open": True, "reason": "", "reopens": None, "unknown": False}


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
        "seasons_unknown": law.get("unknown", False),
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

    # A charted wreck is real structure for any species that holds near
    # relief at all — the general map already gets this via combine_score's
    # weighted blend of every term in terms_d, including "wreck" when one is
    # present. This hand-rolled combination was missing it, so a species map
    # could stay flat right on top of a wreck the generic map lit up.
    if "wreck" in terms:
        parts["wreck"] = terms["wreck"]
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

    # Two hard gates that do not depend on the score field being right. A
    # waypoint on the beach - or in the street behind it - is the most
    # damaging output this produces, and it stayed reachable whenever the
    # land mask was wrong: a coarse grid over a sandy shore averages the
    # first few hundred metres of town to a negative elevation, so those
    # cells read as water at a good depth and won the local-maximum test.
    # The overlay was already held back by overlay.land_buffer_m; the spots
    # were not, so they marched inland on their own.
    # depth_m is NaN exactly where the land mask is set (see run()), so the
    # mask is recoverable here without threading another argument through
    # every call site.
    wet = np.isfinite(depth_m)
    buf_m = float((cfg.get("overlay") or {}).get("land_buffer_m", 200.0))
    if buf_m > 0 and (~wet).any():
        clear = distance_transform_edt(wet) * cfg["grid_resolution_m"]
        peaks &= clear >= buf_m
    peaks &= wet & (np.nan_to_num(depth_m, nan=-1.0) >= cfg["depth"]["min_m"])

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


def write_overlay_png(score, lats, lons, path, land=None, cfg_overlay=None):
    """Transparent PNG, rows resampled to equal Web Mercator spacing.

    Leaflet stretches an ImageOverlay linearly in Mercator y. A plate-carree
    PNG dropped straight in is therefore shifted north-south by tens of
    metres at 45 N — enough to matter when you are looking for a 40 m reef.
    """
    # Was placed above the string literal, which demoted the docstring to a
    # discarded expression and left this function undocumented to help().
    cfg_overlay = cfg_overlay or {}
    # Supersample. The grid is 100 m, which at dive zoom is a block the size of
    # a building; drawing it 1:1 with crisp edges is accurate and unreadable.
    # Interpolating inside the water and masking the land with nearest-
    # neighbour gives a smooth field with a coastline still exactly where the
    # data puts it.
    ss = max(1, int(cfg_overlay.get("supersample", 2)))
    ny = len(lats) * ss
    lons_hi = np.linspace(lons[0], lons[-1], len(lons) * ss)   # target only
    y_edges = np.linspace(mercator_y(lats[0]), mercator_y(lats[-1]), ny)
    lat_merc = np.degrees(2 * np.arctan(np.exp(y_edges)) - math.pi / 2)
    pts = None
    src = RegularGridInterpolator((lats, lons), score,
                                  bounds_error=False, fill_value=0.0)
    gy, gx = np.meshgrid(lat_merc, lons_hi, indexing="ij")
    pts = np.stack([gy.ravel(), gx.ravel()], -1)
    resampled = src(pts).reshape(gy.shape)

    # The Mercator resample interpolates linearly, so a cell straddling the
    # coast picks up a fraction of the neighbouring water score and paints it
    # onto the shore. At 100 m cells that is a visible smear of colour on land.
    # Carry the land mask through with NEAREST sampling and force it clear.
    if land is not None:
        # Pull the visible wash back from any detected shoreline. Chart
        # bathymetry disagrees with the vector coastline right at the coast
        # — river mouths, breakwaters, marinas, reclaimed land — and the
        # disagreement is a DISTANCE, not a cell count, so a fixed one-cell
        # dilation was ~100 m at Novigrad's 100 m grid and only ~40 m
        # wherever the grid came out finer. Express the margin in metres and
        # convert to cells for the grid in hand, so the pull-back is the
        # same on the ground in every region.
        #
        # Cosmetic only: it does not touch the land mask used for scoring or
        # spot-finding, so real nearshore spots are unaffected.
        buf_m = float(cfg_overlay.get("land_buffer_m", 200.0))
        cell_m = (abs(float(np.gradient(lats).mean())) * 111320.0
                  if np.size(lats) > 1 else 100.0)
        cells = max(1, int(round(buf_m / max(cell_m, 1e-6))))
        # A disk, not binary_dilation's default cross. Iterating the cross
        # grows a diamond, which reaches the full radius along the axes but
        # only 1/sqrt(2) of it diagonally — so a coast running NE-SW, which
        # most of this one does, got the smallest margin of all.
        yy, xx = np.ogrid[-cells:cells + 1, -cells:cells + 1]
        disk = (yy ** 2 + xx ** 2) <= cells * cells
        land_buffered = (binary_dilation(land, structure=disk)
                         if land.any() else land)
        lm = RegularGridInterpolator((lats, lons), land_buffered.astype(float),
                                     method="nearest",
                                     bounds_error=False, fill_value=1.0)
        resampled = np.where(lm(pts).reshape(gy.shape) > 0.5, 0.0, resampled)

    rgba = matplotlib.colormaps["viridis"](np.clip(resampled, 0, 1))

    # Paint only water worth looking at. The old ramp made a 0.3 score almost
    # as visible as a 0.9 one, so every bay read as promising and the chart
    # underneath disappeared.
    floor = float(cfg_overlay.get("alpha_floor_score", 0.35))
    top = float(cfg_overlay.get("alpha_full_score", 0.80))
    peak = float(cfg_overlay.get("alpha_max", 0.85))
    rgba[..., 3] = np.clip((resampled - floor) / max(top - floor, 1e-6), 0, 1) * peak
    rgba[resampled <= floor, 3] = 0.0

    arr = (np.flipud(rgba) * 255).astype(np.uint8)   # PNG rows run north to south
    try:
        # Quantise to a palette: visually identical, roughly a quarter the
        # size. These get committed every day, so it adds up.
        from PIL import Image
        im = Image.fromarray(arr, mode="RGBA")
        # Fewer palette entries: the field is mostly transparent and the
        # visible band is narrow, so 32 is indistinguishable and much smaller.
        im.quantize(colors=int(cfg_overlay.get("palette_colors", 32)),
                    method=Image.Quantize.FASTOCTREE).save(path, optimize=True)
    except Exception:
        # Pillow missing or the quantiser refused. Write the RGBA straight
        # out; matplotlib.image.imsave is the pyplot-free equivalent and,
        # unlike plt.imsave, touches no global figure state.
        from matplotlib.image import imsave
        imsave(path, np.flipud(rgba))
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
    Path(path).write_text(json.dumps(gj, indent=1), encoding="utf-8")
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


def run(cfg, selftest=False, out_dir=None):
    # OUT is a LOCAL, not the module global it used to reassign. Two regions
    # computed at the same time would otherwise race on it and write each
    # other's PNGs into the wrong folder — which is the one thing that has to
    # be true before regions can run in parallel at all.
    OUT = out_dir or (ROOT / "docs" / "data")
    OUT.mkdir(parents=True, exist_ok=True)
    bbox = cfg["bbox"]
    res_deg = cfg["grid_resolution_m"] / 111320.0
    lats, lons = build_target_grid(bbox, cfg["grid_resolution_m"])
    log(f"target grid: {len(lats)} x {len(lons)} at {cfg['grid_resolution_m']} m")

    # ---- bathymetry ----
    if selftest:
        blats, blons, elev = synthetic_bathymetry(bbox, res_deg)
        provider, native = "synthetic", 100.0
    else:
        key = (f"{bbox['lon_min']}_{bbox['lat_min']}_{bbox['lon_max']}_"
               f"{bbox['lat_max']}_{cfg['grid_resolution_m']}")
        blats, blons, elev, provider, native = fetch_bathymetry(
            bbox, res_deg, key, cfg)

        # Satellite-derived depth, if sdb.py produced one that passed its own
        # R2 check. It only covers the optically shallow band, so it is laid
        # OVER the chart grid rather than replacing it - fine detail where the
        # satellite can see, the original everywhere else.
        sdb_file = CACHE / f"sdb_{cfg.get('region_id','default')}.npz"
        if (cfg.get("bathymetry") or {}).get("use_sdb") and sdb_file.exists():
            z = np.load(sdb_file)
            sd = regrid(z["lats"], z["lons"], -z["depth"], blats, blons,
                        min_valid=0.6)
            merged = np.where(np.isfinite(sd), sd, elev)
            filled = int(np.isfinite(sd).sum())
            elev = merged
            native = float(z["native_m"])
            provider = f"{provider}+sdb"
            log(f"bathymetry: satellite depth merged over {filled} cells "
                f"(R2 {float(z['r2']):.2f}, RMSE {float(z['rmse_m']):.1f} m, "
                f"scene {str(z['scene'])[:10]}) - native now ~{native:.0f} m")

    # Do not resample far below what the source actually resolves. A 100 m
    # target grid on a 450 m global tile is fake precision, and it quadruples
    # the output for no information.
    floor = max(native / float((cfg.get("bathymetry") or {}).get("min_cells_per_native", 1.5)),
                float((cfg.get("bathymetry") or {}).get("min_grid_resolution_m", 40.0)))
    if cfg["grid_resolution_m"] < floor:
        log(f"grid: source resolves ~{native:.0f} m, so using {floor:.0f} m "
            f"cells instead of {cfg['grid_resolution_m']} m")
        cfg["grid_resolution_m"] = round(floor)
        res_deg = cfg["grid_resolution_m"] / 111320.0
        lats, lons = build_target_grid(bbox, cfg["grid_resolution_m"])
        log(f"target grid: {len(lats)} x {len(lons)} at {cfg['grid_resolution_m']} m")

    relief_ok = native <= float((cfg.get("bathymetry") or {}).get(
        "relief_meaningful_below_m", 200.0))
    if not relief_ok:
        log(f"bathymetry: {native:.0f} m cells cannot resolve reef-scale relief - "
            "the structure map here shows broad slope only")

    elev_g = regrid(blats, blons, elev, lats, lons,
                    min_valid=(cfg.get("water") or {}).get("bathy_min_valid", 0.9))
    nodata = ~np.isfinite(elev_g)
    land = nodata | (elev_g >= 0)
    if nodata.any():
        log(f"bathymetry: {nodata.mean():.0%} of the box has no EMODnet data "
            "(raster does not reach the bbox) - treated as unusable, not as land")

    # Intersect with the real coastline before deciding what is sea. Without
    # it, any land narrower than a cell or two is invisible to the model and
    # gets scored, painted and contoured as open water.
    #
    # Coastline AND wrecks come back from one Overpass request here; the
    # wrecks are held until the scoring section further down. Two requests
    # per region across 55 regions is 110 in a run, and the second of each
    # pair was reliably rate-limited.
    coast_used = False
    osm_wrecks = None
    if not selftest:
        try:
            osm_lines, osm_wrecks = fetch_osm_context(
                bbox, cfg, cfg.get("region_id", "default"))
        except Exception as e:
            osm_lines = None
            log(f"OpenStreetMap: unavailable ({e.__class__.__name__}) - no "
                "coastline and no wrecks for this region")
    else:
        osm_lines = None

    if (cfg.get("coastline") or {}).get("enabled", True) and osm_lines is not None:
        try:
            lines = osm_lines
            if lines:
                blocked = coastline_mask(lines, lats, lons)
                land_c = open_sea_mask(land | blocked, cfg)
                # Overpass clips ways at the bbox, so a coastline can arrive
                # with an open end. A hole lets the fill leak and we are no
                # worse off than before; but on a mostly-enclosed box the
                # fill can instead be strangled to a puddle. Refuse the mask
                # rather than turn the whole region to land.
                if (~land_c).mean() > 0.2 * (~land).mean():
                    land, coast_used = land_c, True
                    log(f"coastline: {len(lines)} OSM way(s) applied; "
                        f"water now {(~land).mean():.0%} of the box")
                else:
                    log("coastline: applying it would remove most of the water "
                        "(clipped or incomplete ways) - ignoring it")
            else:
                log("coastline: no OSM coastline in this box - all open water?")
        except Exception as e:
            log(f"coastline: could not be applied ({e.__class__.__name__}) - "
                "falling back to the bathymetric land edge")
    if not coast_used:
        land = open_sea_mask(land, cfg)
    depth_m = np.where(land, np.nan, -elev_g)
    image_bounds = raster_bounds(lats, lons)

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
        h["pressure_msl"] = 1015.0 - 3.0 * np.linspace(0, 1, n)   # slow, steady fall
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
    work, sea_ok, air_ok = workability_series(h, cfg)
    move = movement_series(h, cfg)
    now_iso = h["time"][now_i]

    # Named wind regimes: bora, jugo, maestral do different things to the
    # water and to whether you can work in it.
    lvl = np.nan_to_num(h["sea_level_height_msl"], nan=0.0)
    tide_range = float(np.nanmax(lvl) - np.nanmin(lvl)) if lvl.size else 0.0
    macrotidal = tide_range > float((cfg.get("movement") or {}).get(
        "macrotidal_range_m", 1.5))
    if macrotidal:
        log(f"tides: forecast range {tide_range:.1f} m - macrotidal. The movement "
            "term and Open-Meteo's 8 km tide model are not adequate here; treat "
            "timing as unreliable and use a real tide table.")

    regimes, viz_f, work_f = wind_regime_series(h, cfg)
    viz = np.clip(viz * viz_f, 0.0, 1.0)
    vc = cfg["visibility"]
    viz_m = vc["viz_min_m"] + viz * (vc["viz_max_m"] - vc["viz_min_m"])
    work = np.clip(work * work_f, 0.0, 1.0)

    # A fitted correction from calibrate.py --fit-viz, if one exists. Written
    # only when the model actually tracks reality, so its presence means the
    # visibility number has been checked against real dives. Applied last,
    # to the final viz_m (after the wind-regime adjustment above) — that is
    # the number calibrate.py was actually fitted against, since it is the
    # one that reaches the log and the app. Correcting the pre-wind-regime
    # value here would just get overwritten by the recompute above and the
    # fit would silently have no effect.
    vzc = (json.loads((ROOT / "weights.json").read_text(encoding="utf-8")).get("visibility_correction")
           if (ROOT / "weights.json").exists() else None)
    if vzc:
        viz_m = np.clip(vzc["scale"] * viz_m + vzc["offset"], 0.3, 40.0)
        log(f"visibility: applying fitted correction "
            f"({vzc['scale']:.2f}x {vzc['offset']:+.1f} m, n={vzc['n']}, "
            f"r={vzc['r']}, residual sd {vzc['residual_sd_m']} m)")

    elev = solar_elevation_deg(h["time"], clat, clon, tz_offset)
    light = light_quality(elev, cfg, h.get("cloud_cover"))

    today_str = now_iso[:10]
    # Sun only — see verdict(). Whether any of that light is workable is a
    # different question and gets its own answer.
    light_left = any(light[i] > 0
                     and h["time"][i] >= now_iso and h["time"][i][:10] == today_str
                     for i in range(len(h["time"])))
    word, limiter, day_score = verdict(
        float(viz[now_i]), float(viz_m[now_i]), float(work[now_i]),
        float(sea_ok[now_i]), float(air_ok[now_i]), cfg,
        float(light[now_i]), light_left)
    window = best_window(h, daily, viz, work, move, cfg, now_iso, light)
    days = daily_breakdown(h, daily, viz, viz_m, work, move, cfg, now_iso,
                           light, regimes, sea_ok, air_ok)
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
        fitted = json.loads(wpath.read_text(encoding="utf-8"))
        weights_cfg.update(fitted.get("weights", {}))
        log(f"weights: using fitted values from {wpath.name} "
            f"(n={fitted.get('n_dives', '?')}, auc={fitted.get('auc', '?')})")

    # ---- gates: static, so computed once ----
    dfit = depth_fit(depth_m, land, cfg)
    afit = access_fit(lats, lons, cfg)
    clear_min = (cfg.get("water") or {}).get("min_shore_clearance_m", 0)
    cfit = np.ones_like(dfit)
    if clear_min > 0:
        cfit = np.clip(shore_clearance(land, cfg) / max(clear_min, 1e-6), 0, 1)
    excl_name = cfg.get("exclusions_file")
    excl = (rasterize_polygons(ROOT / excl_name, lats, lons) if excl_name
            else np.zeros((len(lats), len(lons)), dtype=bool))
    if not excl_name:
        log("exclusions: none defined for this region - "
            "check protected areas yourself")
    if excl.any():
        log(f"exclusions: {excl.mean():.1%} of the box masked out")
    gates = [afit, cfit, (~excl).astype(float)]

    # Already fetched, in the same Overpass request as the coastline.
    wrecks_gj, wrecks = None, None
    if (cfg.get("wrecks") or {}).get("enabled", True) and osm_wrecks is not None:
        try:
            wrecks_gj = osm_wrecks
            n = len(wrecks_gj["features"])
            if n:
                wrecks = wreck_score(wrecks_gj, lats, lons, cfg)
                log(f"wrecks: {n} charted within the box")
            else:
                log("wrecks: none charted here")
            (OUT / "wrecks.geojson").write_text(json.dumps(wrecks_gj), encoding="utf-8")
        except Exception as e:
            log(f"wrecks: could not be scored ({e.__class__.__name__}) - continuing without")

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
        swell_d = float(h["wave_direction"][ri]) if "wave_direction" in h else None
        shelter_d, _ = combined_shelter(land, lats, lons, wind_d, swell_d,
                                        cfg, nodata)
        terms_d = {"relief": relief_s, "slope": slope_s, "shelter": shelter_d}
        if habitat is not None:
            terms_d["habitat"] = habitat
        if wrecks is not None:
            terms_d["wreck"] = wrecks
        w_d = {k: weights_cfg.get(k, 0.0) for k in terms_d}

        score_d = combine_score(terms_d, w_d, gates)
        score_d[land] = 0.0
        spots_d = find_spots(score_d, lats, lons, depth_m, terms_d, cfg)
        write_overlay_png(score_d, lats, lons, OUT / f"score_d{di}.png", land,
                          cfg.get("overlay"))
        write_gpx(spots_d, OUT / f"spots_d{di}.gpx",
                  f"{cfg['region_name']} {day['date']}")

        temp_d = float(h["sea_surface_temperature"][ri])
        if not np.isfinite(temp_d):
            temp_d = sea_temp
        month_d = int(day["date"][5:7])
        date_d = dt.date.fromisoformat(day["date"])

        # Per-species MAPS are the whole output budget: three days times
        # eight species is 24 PNGs and 24 GPX files per region per run, on
        # top of the three general ones. Scores, spots and legal status
        # still come out for every day - those are a few kilobytes of JSON -
        # but the rasters can be limited to the first N days. The app falls
        # back to day 0's map for later days and says so (see selectTarget()
        # in docs/index.html), which is a small honesty cost for roughly a
        # 60% cut in artifact size and write time.
        write_species_maps = di < int(cfg.get("species_maps_days", 3))

        sp_out = []
        for sp in species_defs:
            fac = species_day_factors(sp, temp_d, float(viz[ri]), month_d,
                                      cfg, profile, date_d)
            smap = species_spatial_score(sp, terms_d, depth_m, land, gates, cfg)
            smap[land] = 0.0
            ss = find_spots(smap, lats, lons, depth_m, terms_d, cfg, limit=sp_cap)
            if write_species_maps:
                write_overlay_png(smap, lats, lons,
                                  OUT / f"score_{sp['id']}_d{di}.png", land,
                                  cfg.get("overlay"))
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
        day["swell_from_deg"] = (round(swell_d) if swell_d is not None
                                 and np.isfinite(swell_d) else None)
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
    if (cfg.get("contours") or {}).get("enabled", True):
        try:
            gj = depth_contours(depth_m, land, lats, lons, cfg)
            (OUT / "contours.geojson").write_text(json.dumps(gj), encoding="utf-8")
            log(f"wrote {OUT / 'contours.geojson'} "
                f"({len(gj['features'])} lines at "
                f"{cfg.get('contours', {}).get('levels_m', [5,10,15,20,30])} m)")
        except Exception as e:
            log(f"contours: skipped ({e.__class__.__name__}: {e})")

    keep = slice(max(0, now_i - 24), min(len(h["time"]), now_i + 60))
    payload = {
        "generated": stamp,
        "schema_version": 18,
        "region": cfg["region_name"],
        "region_id": cfg.get("region_id", "default"),
        "country": cfg.get("country", ""),
        "jurisdiction": cfg.get("jurisdiction", ""),
        "region_note": cfg.get("region_note", ""),
        "bathymetry_provider": provider,
        "bathymetry_native_m": round(native),
        "relief_meaningful": bool(relief_ok),
        "coastline_applied": bool(coast_used),
        "grid_resolution_m": cfg["grid_resolution_m"],
        "has_wrecks": bool(wrecks_gj and wrecks_gj["features"]),
        "has_species": bool(cfg.get("species_file")),
        "legal_pack": load_legal(cfg),
        "has_exclusions": bool(cfg.get("exclusions_file")),
        "bounds": image_bounds,
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
            "wind_regime_note": (cfg.get("wind_regimes") or {}).get(
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
        "tide_range_m": round(tide_range, 2),
        "macrotidal": macrotidal,
        "species_top_spots": sp_cap,
        # How many days actually have a per-species raster on disk. The app
        # reads this instead of discovering the 404 the hard way.
        "species_maps_days": int(cfg.get("species_maps_days", 3)),
        "visibility_calibrated": bool(vzc),
        "visibility_correction": vzc,
        "temp_source": ("Copernicus Marine measured profile" if profile
                        else "estimated from SST + seasonal thermocline"),
        "temp_profile": profile,
        "month": int(now_iso[5:7]),
        "spots": spots,
        "species": species_out,
    }
    (OUT / "latest.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    log(f"wrote {OUT / 'latest.json'}")
    return payload


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--regions", default=str(ROOT / "regions.json"))
    ap.add_argument("--region", default=None,
                    help="run one region by id; default is every enabled one")
    ap.add_argument("--select", default=os.environ.get("FISH_REGIONS", ""),
                    help="comma-separated region ids and/or group names, or "
                         "'all', or 'enabled'. Overrides the enabled flags in "
                         "regions.json. Also read from the FISH_REGIONS "
                         "environment variable, so the schedule can be "
                         "changed from repository settings without a commit.")
    ap.add_argument("--jobs", type=int,
                    default=int(os.environ.get("FISH_JOBS", "4")),
                    help="how many regions to compute at once (default 4). "
                         "The work is dominated by waiting on EMODnet, "
                         "Overpass and Open-Meteo, so this is close to a "
                         "linear speed-up until the services start throttling.")
    ap.add_argument("--list", action="store_true", help="list regions and exit")
    ap.add_argument("--groups", action="store_true",
                    help="list the region groups and how many are in each")
    ap.add_argument("--selftest", action="store_true",
                    help="run the whole pipeline on synthetic data, no network")
    args = ap.parse_args()

    base = json.loads(Path(args.config).read_text(encoding="utf-8"))
    regions = load_regions(Path(args.regions))

    if args.groups:
        counts = {}
        for r in regions:
            g = r.get("group", "ungrouped")
            counts.setdefault(g, [0, 0])
            counts[g][0] += 1
            counts[g][1] += 1 if r.get("enabled") else 0
        print(f"  {'group':<22} {'total':>6} {'on':>4}")
        for g in sorted(counts):
            n, on = counts[g]
            print(f"  {g:<22} {n:>6} {on:>4}")
        print(f"\n  {len(regions)} regions, "
              f"{sum(1 for r in regions if r.get('enabled'))} enabled")
        print("\n  Run a whole group without editing anything:")
        print("      python fish_finder.py --select med-west")
        return

    if args.list:
        for r in regions:
            flags = []
            if r.get("species_file"):    flags.append("species")
            if r.get("exclusions_file"): flags.append("exclusions")
            print(f"  {'on ' if r.get('enabled') else 'off'} {r['id']:<20} "
                  f"{r.get('group',''):<16} {r['name']:<34} "
                  f"{r.get('country',''):<14} "
                  f"{'+'.join(flags) or 'structure only'}")
        return

    chosen = select_regions(regions, args.region, args.select)
    if not chosen:
        raise SystemExit(
            "no regions selected.\n"
            "  Turn some on with \"enabled\": true in regions.json, or pick "
            "them for one run without editing anything:\n"
            "      python fish_finder.py --select novigrad,cascais\n"
            "      python fish_finder.py --select med-west   (a whole group; "
            "see --groups)\n"
            "      python fish_finder.py --select all")

    log(f"running {len(chosen)} region(s) on {max(1, args.jobs)} worker(s): "
        + ", ".join(r["id"] for r in chosen[:12])
        + (f" … and {len(chosen) - 12} more" if len(chosen) > 12 else ""))

    # First, before anything can go wrong: the point model's constants.
    write_model(base, regions)

    # index.json is what the app reads to know which coasts exist, and it
    # used to be written only after every region had finished. A run that
    # was killed — a workflow timeout, a cancelled job — therefore threw away
    # every region it had already computed, however many hours in it was.
    # With a full cold catalogue taking hours that is not a theoretical
    # risk, so the catalogue is rewritten each time a region lands. It is a
    # few tens of kB; writing it 264 times costs nothing next to losing the
    # lot. Partial output is a perfectly good site — it just has fewer
    # coasts on it.
    idx_path = ROOT / "docs" / "data" / "index.json"
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_lock = threading.Lock()

    def write_index(entries, failures, done=False):
        payload = {
            "schema_version": 18,
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "complete": done,
            "expected": len(chosen),
            "regions": entries,
            "failed": failures,
        }
        tmp = idx_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(idx_path)          # atomic: never a half-written index

    def compute(r):
        """One region, start to finish. Runs on a worker thread."""
        log_tag(f"{r['id']}: ")
        cfg = region_config(base, r)
        out = ROOT / "docs" / "data" / r["id"]
        log(f"--- {r['name']} ---")
        payload = run(cfg, selftest=args.selftest, out_dir=out)
        return {
            "id": r["id"], "name": r["name"], "country": r.get("country", ""),
            "jurisdiction": r.get("jurisdiction", ""),
            "group": r.get("group", ""),
            "note": r.get("note", ""),
            "has_species": bool(r.get("species_file")),
            "species_file": r.get("species_file"),
            "has_exclusions": bool(r.get("exclusions_file")),
            "bathymetry_provider": payload.get("bathymetry_provider"),
            "bathymetry_native_m": payload.get("bathymetry_native_m"),
            "relief_meaningful": payload.get("relief_meaningful"),
            "coastline_applied": payload.get("coastline_applied"),
            "macrotidal": payload.get("macrotidal"),
            "bbox": r["bbox"],
            "centre": [round((r["bbox"]["lat_min"] + r["bbox"]["lat_max"]) / 2, 4),
                       round((r["bbox"]["lon_min"] + r["bbox"]["lon_max"]) / 2, 4)],
            "verdict": payload["now"]["verdict"],
            "score": payload["now"]["score"],
            "visibility_m": payload["now"]["visibility_m"],
            "wind_from_deg": payload["now"]["wind_from_deg"],
            "best_window": payload.get("best_window"),
            "generated": payload["generated"],
        }

    # Regions in parallel. Almost all of the wall time is spent waiting on
    # EMODnet, Overpass and Open-Meteo rather than computing, and numpy
    # releases the GIL for the array work that is left, so threads give
    # close to a linear speed-up here without any of the pickling that
    # processes would need. run() writes only into its own region folder and
    # keeps no global state, which is what makes this safe.
    t_start = time.time()
    jobs = max(1, min(int(args.jobs), len(chosen)))
    # Keep the catalogue in the order regions.json declares them, not the
    # order the threads happened to finish in, so the region picker does not
    # reshuffle itself between runs.
    order = {r["id"]: i for i, r in enumerate(chosen)}
    results = []

    def landed(r, entry, err):
        """A region has finished, one way or the other. Publish immediately."""
        with idx_lock:
            results.append((r, entry, err))
            if err is not None:
                # One region failing must not take the others down with it.
                log(f"{r['id']} FAILED: {err.__class__.__name__}: {err}")
            done = sorted(results, key=lambda t: order[t[0]["id"]])
            write_index([e for _, e, x in done if x is None],
                        [rr["id"] for rr, _, x in done if x is not None])
            n = len(results)
            if n % 10 == 0 or n == len(chosen):
                bad = sum(1 for _, _, x in results if x is not None)
                rate = (time.time() - t_start) / n
                left = (len(chosen) - n) * rate
                log(f"progress: {n}/{len(chosen)} regions, {bad} failed, "
                    f"{rate:.1f} s each, ~{left / 60:.0f} min left")

    if jobs == 1:
        for r in chosen:
            try:
                landed(r, compute(r), None)
            except Exception as e:
                landed(r, None, e)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(compute, r): r for r in chosen}
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    landed(r, fut.result(), None)
                except Exception as e:
                    landed(r, None, e)
    log_tag("")

    ordered = sorted(results, key=lambda x: order[x[0]["id"]])
    index = [e for _, e, x in ordered if x is None]
    failed = [r["id"] for r, _, x in ordered if x is not None]

    elapsed = time.time() - t_start
    log(f"computed {len(index)} region(s) in {elapsed:.0f} s "
        f"({elapsed / max(len(index), 1):.1f} s each, {jobs} worker(s))"
        + (f" — {len(failed)} failed" if failed else ""))

    if not index:
        raise SystemExit("every region failed - nothing written")

    write_index(index, failed, done=True)
    log(f"wrote {idx_path} ({len(index)} region(s)"
        + (f", {len(failed)} failed" if failed else "") + ")")


def write_model(base, regions):
    """The constants the in-browser point model needs.

    Written BEFORE any region is computed. It depends only on config.json
    and the species/legal packs, never on a forecast, and the app cannot do
    anything in point mode without it — so a run that is killed part way
    through should still leave it in place rather than take the whole
    anywhere-on-Earth half of the app down with it.
    """
    # "depth" and "weights" join the model constants: the in-browser point
    # model needs the working depth band and the term weights to report the
    # same footer the region maps do, and reading them from here keeps the
    # two halves from drifting the way duplicated numbers always do.
    model_keys = ("visibility", "workability", "movement", "light",
                  "wind_regimes", "gust", "day_weights", "verdict",
                  "conditions", "thermocline", "shelter", "pressure",
                  "depth", "weights")
    model = {k: base.get(k) for k in model_keys}

    # Every species pack in the repository, not just the ones a region
    # happens to declare. The ocean-basin fallback in biomeSpeciesFile()
    # (docs/index.html) can name a pack no enabled region uses, and a pack
    # that is named but not bundled fails silently as "no species data for
    # this point" — which is indistinguishable, from the map, from genuinely
    # having no match. Globbing means adding a biome rule is a one-file edit.
    species_filenames = {p.name for p in ROOT.glob("species*.json")}
    species_filenames |= {r.get("species_file") for r in regions
                          if r.get("species_file")}
    model["species_packs"] = {}
    for name in sorted(species_filenames):
        try:
            model["species_packs"][name] = json.loads((ROOT / name).read_text(encoding="utf-8"))["species"]
        except Exception as e:
            log(f"species pack {name} failed to load: {e}")
    model["legal"] = {}
    legal_files = list((ROOT / "legal").glob("*.json")) if (ROOT / "legal").exists() else []
    for r in regions:
        cc = (r.get("jurisdiction") or "").upper()
        root_pack = ROOT / f"{cc}.json"
        if cc and root_pack.exists() and root_pack not in legal_files:
            legal_files.append(root_pack)
    for f in sorted(legal_files):
        try:
            model["legal"][f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    model["schema_version"] = 18
    mpath = ROOT / "docs" / "data" / "model.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(model, indent=1), encoding="utf-8")
    log(f"wrote {mpath} (constants for the in-browser model)")


if __name__ == "__main__":
    main()
