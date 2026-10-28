#!/usr/bin/env python3
"""Fast checks that run before the model and before anything is deployed.

A broken fish_finder.py already fails the build. This catches what would
otherwise reach the public site: a page or script that does not parse, a
precached file that is missing, a service worker left on an old VERSION, and
the verdict logic or its browser mirror drifting. Exit status 1 stops the
deploy.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES = []
WARNINGS = []
INFO = {}

SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)

PARITY_JS = """
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
const root = process.argv[2];
const cfg = JSON.parse(readFileSync(root + "/config.json", "utf8"));
const { verdictLimits } = await import(pathToFileURL(root + "/docs/anywhere.js").href);
const out = {};
for (const b of JSON.parse(process.argv[3])) {
  out[b] = verdictLimits({
    verdict: cfg.verdict,
    workability: cfg.workability,
    visibility: { ...cfg.visibility, baseline: b },
  });
}
console.log(JSON.stringify(out));
"""

POINT_JS = """
import { pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
const root = process.argv[2];
const cfg = JSON.parse(readFileSync(root + "/config.json", "utf8"));
const keys = ["visibility", "workability", "movement", "light", "wind_regimes", "gust", "day_weights", "verdict", "conditions", "thermocline", "shelter", "pressure", "depth", "weights", "lake"];
const model = Object.fromEntries(keys.map((k) => [k, cfg[k]]));
model.species = [];
const { forecastPoint } = await import(pathToFileURL(root + "/docs/anywhere.js").href);
const N = 8 * 24;
const start = Date.UTC(2026, 0, 1);
const times = Array.from({ length: N }, (_, i) => new Date(start + i * 3600e3).toISOString().slice(0, 13) + ":00");
const col = (v) => times.map(() => v);
function marine(waves, sst) {
  const p = waves === null ? null : 5;
  return {
    utc_offset_seconds: 0,
    hourly: {
      time: times, wave_height: col(waves), wave_period: col(p), wave_direction: col(90),
      wind_wave_height: col(waves), wind_wave_period: col(p), wind_wave_direction: col(90),
      swell_wave_height: col(waves), swell_wave_period: col(p), swell_wave_direction: col(90),
      sea_surface_temperature: col(sst), sea_level_height_msl: col(0),
      ocean_current_velocity: col(null), ocean_current_direction: col(null),
    },
  };
}
const days = [...new Set(times.map((t) => t.slice(0, 10)))];
const weather = {
  utc_offset_seconds: 0,
  hourly: {
    time: times, precipitation: col(0), wind_speed_10m: col(8), wind_direction_10m: col(90),
    wind_gusts_10m: col(10), cloud_cover: col(20), pressure_msl: col(1015),
  },
  daily: { time: days, sunrise: days.map((d) => d + "T06:30"), sunset: days.map((d) => d + "T19:30") },
};
Date.now = () => Date.UTC(2026, 0, 5, 12, 0);
async function run(waves, sst) {
  globalThis.fetch = async (url) => new Response(JSON.stringify(/marine-api/.test(url) ? marine(waves, sst) : weather));
  try {
    const d = await forecastPoint(50, 0, model, null);
    return { ok: true, sea_temp_c: d.now.sea_temp_c, verdict: d.now.verdict };
  } catch (e) {
    return { ok: false, code: e.code || null, message: e.message };
  }
}
console.log(JSON.stringify({ noWaves: await run(null, null), noTemp: await run(0.4, null), warm: await run(0.4, 18) }));
"""

LAKES_JS = """
import { pathToFileURL } from "node:url";
import { readFileSync, readdirSync } from "node:fs";
const root = process.argv[2];
const lk = await import(pathToFileURL(root + "/docs/lakes.js").href);
const cfgFile = JSON.parse(readFileSync(root + "/config.json", "utf8"));
const o = lk.lakeSettings(cfgFile);
const near = (a, b, tol) => Math.abs(a - b) <= tol;
const res = {};
const lat0 = 46, lon0 = 10;
const kx = Math.cos((lat0 * Math.PI) / 180) * 111320, ky = 110540;
const rect = { rings: [[[lat0 - 1000 / ky, lon0 - 5000 / kx], [lat0 + 1000 / ky, lon0 - 5000 / kx], [lat0 + 1000 / ky, lon0 + 5000 / kx], [lat0 - 1000 / ky, lon0 + 5000 / kx], [lat0 - 1000 / ky, lon0 - 5000 / kx]]] };
const table = lk.fetchTable(rect, lat0, lon0 - 4000 / kx, 150000);
res.rect_fetch = near(table[9] / 1000, 7.5, 0.3) && near(table[27] / 1000, 1.0, 0.1) && near(table[0] / 1000, 1.0, 0.1);
const centre = lk.snapIntoLake(rect, lat0, lon0, 2500);
const far = lk.snapIntoLake(rect, lat0 + 0.2, lon0, 2500);
const shore = lk.snapIntoLake(rect, lat0, lon0 - 5800 / kx, 2500);
res.snap = !!centre && !centre.snapped && far === null && !!shore && shore.snapped === true;
const w = lk.windWaves(36, 10000, o);
res.waves_formula = near(w.hs, 0.731, 0.01) && near(w.tp, 3.27, 0.05);
res.calm_is_flat = lk.windWaves(1, 10000, o).hs === 0;
res.past_days_clamped = lk.lakeSettings({ lake: { past_days: 90 } }).past_days <= 14;
const dev = lk.windWaves(150, 1e7, o);
res.fully_developed_cap = dev.hs <= (0.2433 * Math.pow(0.71 * Math.pow((150 / 3.6) * o.wind_factor, 1.23), 2)) / 9.81 + 1e-9;
const s = lk.lakeWaveSeries([0, 0, 36, 36, 36, 36, 0, 0, 0, 0], new Array(10).fill(90), new Float64Array(36).fill(10000), o);
res.wave_series = s.hs[2] > 0 && s.hs[3] > s.hs[2] && s.hs[5] <= 0.74 && s.hs[6] < s.hs[5] && s.hs[9] < s.hs[6];
const isoDays = (n) => Array.from({ length: n }, (_, i) => new Date(Date.UTC(2025, 0, 1) + i * 86400e3).toISOString().slice(0, 10));
const d400 = isoDays(400);
const tropical = lk.lakeWaterModel(d400, d400.map(() => 22), { mean_depth_m: 40, surface_c: [24, 27], ice_risk: "none", ice_months: [] }, o);
res.water_tropical = tropical.temp[399] >= 24 && tropical.temp[399] <= 27;
const d800 = isoDays(800);
const airYear = d800.map((d, i) => 10 + 10 * Math.sin((2 * Math.PI * (i - 100)) / 365));
const temperate = lk.lakeWaterModel(d800, airYear, { mean_depth_m: 90, surface_c: [6, 20], ice_risk: "none", ice_months: [] }, o);
let peakAir = 400, peakWater = 400, lowest = Infinity;
for (let i = 400; i < 765; i++) {
  if (airYear[i] > airYear[peakAir]) peakAir = i;
  if (temperate.temp[i] > temperate.temp[peakWater]) peakWater = i;
  lowest = Math.min(lowest, temperate.temp[i]);
}
res.water_seasonal = temperate.temp[peakWater] >= 18 && lowest <= 8 && peakWater - peakAir > 10;
const airCold = d800.map((d, i) => 4 - 14 * Math.cos((2 * Math.PI * (i - 15)) / 365));
const frozen = { mean_depth_m: 12, surface_c: [0.5, 20], ice_risk: "seasonal", ice_months: [11, 12, 1, 2, 3, 4, 5] };
const iceRun = lk.lakeWaterModel(d800, airCold, frozen, o);
const open = lk.lakeWaterModel(d800, airCold, { ...frozen, ice_risk: "none", ice_months: [] }, o);
const summerIce = d800.some((d, i) => i >= 400 && [6, 7, 8, 9, 10].includes(Number(d.slice(5, 7))) && iceRun.thickness[i] > 0);
res.ice_cycle = Math.max(...iceRun.thickness.slice(400)) > 0.2 && !summerIce && Math.max(...open.thickness) === 0;
const allCold = lk.lakeWaterModel(d800, d800.map(() => -10), frozen, o);
res.ice_window = Math.max(...allCold.thickness) > 0.2 && d800.every((d, i) => ![6, 7, 8, 9, 10].includes(Number(d.slice(5, 7))) || allCold.thickness[i] === 0);
const bad = [];
for (const f of readdirSync(root + "/docs/lakes")) {
  if (f === "index.json") continue;
  const lake = JSON.parse(readFileSync(root + "/docs/lakes/" + f, "utf8"));
  const hit = lk.snapIntoLake(lake, lake.centre[0], lake.centre[1], 0);
  const t = lk.fetchTable(lake, lake.centre[0], lake.centre[1], 150000);
  if (!hit || hit.snapped || !(Math.max(...t) > 1000)) bad.push(lake.id);
}
res.lake_centres_in_water = bad.length === 0;
res.lake_centres_bad = bad;
const keys = ["visibility", "workability", "movement", "light", "wind_regimes", "gust", "day_weights", "verdict", "conditions", "thermocline", "shelter", "pressure", "depth", "weights", "lake"];
const model = Object.fromEntries(keys.map((k) => [k, cfgFile[k]]));
model.species = [];
const { forecastPoint } = await import(pathToFileURL(root + "/docs/anywhere.js").href);
function weather(nowUtc, offH, air, wind, dir) {
  const start = nowUtc - 7 * 86400e3 + offH * 3600e3;
  const n = 10 * 24;
  const times = Array.from({ length: n }, (_, i) => new Date(start + i * 3600e3).toISOString().slice(0, 13) + ":00");
  const col = (v) => times.map(() => v);
  return { utc_offset_seconds: offH * 3600, hourly: { time: times, precipitation: col(0), wind_speed_10m: col(wind), wind_direction_10m: col(dir), wind_gusts_10m: col(wind * 1.3), cloud_cover: col(25), pressure_msl: col(1015), temperature_2m: col(air) } };
}
async function forecast(id, lat, lon, nowUtc, offH, air, wind, dir, archiveOk = true) {
  const lake = JSON.parse(readFileSync(root + "/docs/lakes/" + id + ".json", "utf8"));
  const urls = [];
  globalThis.fetch = async (url) => {
    const u = String(url);
    urls.push(u);
    if (/archive-api/.test(u)) {
      if (!archiveOk) return new Response("{}", { status: 500 });
      const s =/start_date=([0-9-]+)/.exec(u)[1], e = /end_date=([0-9-]+)/.exec(u)[1];
      const n = Math.round((Date.parse(e) - Date.parse(s)) / 86400e3) + 1;
      const time = Array.from({ length: n }, (_, i) => new Date(Date.parse(s) + i * 86400e3).toISOString().slice(0, 10));
      return new Response(JSON.stringify({ daily: { time, temperature_2m_mean: time.map(() => air) } }));
    }
    return new Response(JSON.stringify(weather(nowUtc, offH, air, wind, dir)));
  };
  Date.now = () => nowUtc;
  const d = await forecastPoint(lat, lon, model, null, lake);
  return { d, marine: urls.some((u) => /marine-api/.test(u)), calls: urls.length };
}
const summer = await forecast("garda", 45.7, 10.7, Date.UTC(2026, 6, 10, 12, 0), 2, 24, 4, 180);
res.lake_forecast_calm = summer.d.now.verdict === "go" && !summer.marine && summer.calls === 2 && summer.d.lake?.name === "Lake Garda" && !summer.d.lake?.rings && summer.d.species.length === 0 && summer.d.now.sea_temp_c >= 8 && summer.d.now.sea_temp_c <= 27;
const winter = await forecast("baikal", 54.18, 108.98, Date.UTC(2027, 0, 15, 4, 0), 8, -20, 10, 45);
res.lake_forecast_ice = winter.d.now.verdict === "stay home" && winter.d.now.limited_by === "ice" && winter.d.lake?.ice_now === true;
const filledGap = lk.fillDaily(new Map([["2026-01-01", 0], ["2026-01-04", 6], ["2026-01-05", 8]]));
res.fill_daily = filledGap.dates.length === 5 && near(filledGap.values[1], 2, 1e-9) && near(filledGap.values[2], 4, 1e-9) && filledGap.filled === 2;
const noHistory = await forecast("garda", 45.7, 10.7, Date.UTC(2026, 6, 10, 12, 0), 2, 24, 4, 180, false);
res.lake_forecast_no_history = ["go", "marginal", "poor", "stay home", "not now"].includes(noHistory.d.now.verdict) && noHistory.d.lake?.history_days === 0 && Number.isFinite(noHistory.d.now.sea_temp_c);
const gale = await forecast("garda", 45.7, 10.7, Date.UTC(2026, 6, 10, 12, 0), 2, 24, 40, 180);
res.lake_forecast_gale = gale.d.now.verdict === "stay home" && gale.d.now.limited_by !== "ice" && gale.d.now.wave_height_m > 0.3;
console.log(JSON.stringify(res));
"""


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + (f"   {detail}" if detail else ""))
    if ok:
        INFO["passed"] = INFO.get("passed", 0) + 1
    else:
        FAILURES.append(label)


def sh(cmd, **kwargs):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kwargs)


def tracked():
    out = sh(["git", "ls-files"])
    if out.returncode == 0 and out.stdout.strip():
        return [line for line in out.stdout.split("\n") if line and (ROOT / line).is_file()]
    skip = {".git", "cache", "__pycache__", "data"}
    return [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*")
        if p.is_file() and not skip & set(p.relative_to(ROOT).parts)
    ]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def check_python(files):
    bad = 0
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            compile(read(f), f, "exec")
        except SyntaxError as e:
            bad += 1
            check(f"python parses: {f}", False, f"line {e.lineno}: {e.msg}")
    check("python files parse", bad == 0, f"{sum(f.endswith('.py') for f in files)} files")
    return bad == 0


def check_json(files):
    bad = 0
    total = 0
    for f in files:
        if f.endswith((".json", ".geojson", ".webmanifest")):
            total += 1
            try:
                json.loads(read(f))
            except ValueError as e:
                bad += 1
                check(f"json parses: {f}", False, str(e)[:100])
    check("json files parse", bad == 0, f"{total} files")


def node_syntax(node, source, name):
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for ext in (".mjs", ".cjs"):
            path = Path(tmp) / (name + ext)
            path.write_text(source, encoding="utf-8")
            run = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            if run.returncode == 0:
                return True, ""
            lines = [l for l in run.stderr.splitlines() if "Error" in l]
            errors.append(lines[0] if lines else run.stderr.strip()[:120])
    return False, errors[0]


def inline_scripts(html):
    for m in SCRIPT_RE.finditer(html):
        attrs, body = m.group(1).lower(), m.group(2)
        if "src=" in attrs or not body.strip():
            continue
        kind = re.search(r"type\s*=\s*[\"']?([^\"'\s>]+)", attrs)
        if kind and kind.group(1) not in ("module", "text/javascript", "application/javascript"):
            continue
        yield body


def check_javascript(files, node):
    if not node:
        print("NOTE  node is not available: JavaScript syntax and parity checks skipped")
        return
    version = sh([node, "--version"]).stdout.strip()
    INFO["node"] = version
    bad = 0
    count = 0
    for f in files:
        sources = []
        if f.endswith(".js"):
            sources.append((f, read(f)))
        elif f.endswith(".html"):
            sources.extend((f"{f} inline script {i}", s) for i, s in enumerate(inline_scripts(read(f))))
        for label, source in sources:
            count += 1
            ok, detail = node_syntax(node, source, "check")
            if not ok:
                bad += 1
                check(f"javascript parses: {label}", False, detail)
    check("javascript parses", bad == 0, f"{count} scripts, node {version}")


def shell_entries():
    sw = read("docs/sw.js")
    m = re.search(r"const SHELL_URLS = \[(.*?)\];", sw, re.S)
    if not m:
        return None
    return re.findall(r'"([^"]+)"', m.group(1))


def check_shell_urls():
    entries = shell_entries()
    if entries is None:
        check("service worker lists its precached files", False, "SHELL_URLS not found in docs/sw.js")
        return None
    missing = []
    for e in entries:
        if re.match(r"https?:", e):
            continue
        rel = "index.html" if e == "./" else (e[2:] if e.startswith("./") else e)
        if not (ROOT / "docs" / rel).is_file():
            missing.append(e)
    check("every precached file exists", not missing, ", ".join(missing))
    return entries


def check_version_bump(entries):
    before = os.environ.get("BEFORE_SHA", "").strip()
    if not before or set(before) == {"0"} or not entries:
        return
    if sh(["git", "cat-file", "-e", before + "^{commit}"]).returncode != 0:
        return
    changed = set(sh(["git", "diff", "--name-only", before, "HEAD"]).stdout.split())
    shell = {"docs/" + e[2:] for e in entries if e.startswith("./") and e != "./"} | {"docs/index.html"}
    touched = sorted((changed & shell) - {"docs/sw.js"})
    if not touched:
        return
    old = sh(["git", "show", f"{before}:docs/sw.js"]).stdout
    pattern = r'const VERSION = "([^"]+)"'
    was, now = re.search(pattern, old), re.search(pattern, read("docs/sw.js"))
    if was and now and was.group(1) == now.group(1):
        WARNINGS.append("sw.js VERSION not bumped")
        print(
            f"::warning title=sw.js VERSION not bumped::{', '.join(touched)} changed but "
            f"docs/sw.js is still {now.group(1)}. Phones serve the app shell from cache, so a "
            "behavioural change needs a new VERSION. Ignore this for changes to bytes only."
        )


def check_verdicts(ff, base, regions):
    stuck = []
    for r in regions:
        cfg = ff.region_config(base, r)
        if float(cfg["verdict"].get("local_bar", 1.0)) < 1.0:
            continue
        best = 1.0 - float(cfg["visibility"]["baseline"])
        word = ff.verdict(best, 0.0, 1.0, 1.0, 1.0, cfg, 1.0, True)[0]
        if word != "go":
            stuck.append(r["id"])
    check(
        "every region can reach Go on its best day",
        not stuck,
        f"{len(stuck)} cannot: {', '.join(stuck[:5])}" if stuck else f"{len(regions)} regions",
    )
    cfg = ff.region_config(base, regions[0])
    gate = ff.verdict(1.0, 10.0, 0.0, 0.0, 1.0, cfg, 1.0, True)[0]
    check("stay home overrides perfect visibility", gate == "stay home", gate)
    check("go bar sits above marginal bar", base["verdict"]["go"] > base["verdict"]["marginal"])


def check_parity(ff, base, regions, node):
    if not node:
        return
    baselines = sorted({float(ff.region_config(base, r)["visibility"]["baseline"]) for r in regions})
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "parity.mjs"
        script.write_text(PARITY_JS, encoding="utf-8")
        run = subprocess.run(
            [node, str(script), str(ROOT), json.dumps(baselines)], capture_output=True, text=True
        )
    if run.returncode != 0:
        lines = [l for l in run.stderr.splitlines() if "Error" in l]
        check("browser and python verdict limits agree", False, (lines or ["node failed"])[0][:140])
        return
    js = json.loads(run.stdout)
    wrong = []
    for b in baselines:
        py = ff.verdict_limits({**base, "visibility": {**base["visibility"], "baseline": b}})
        got = js.get(str(b))
        if got is None or set(py) != set(got):
            wrong.append(f"baseline {b}: keys differ")
            continue
        for k, v in py.items():
            if isinstance(v, bool):
                same = v == got[k]
            else:
                same = abs(float(v) - float(got[k])) <= 0.051
            if not same:
                wrong.append(f"baseline {b}: {k} python {v} vs browser {got[k]}")
    check("browser and python verdict limits agree", not wrong, f"{len(baselines)} baselines" if not wrong else "; ".join(wrong[:3]))


def check_point_model(node):
    if not node:
        return
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "point.mjs"
        script.write_text(POINT_JS, encoding="utf-8")
        run = subprocess.run([node, str(script), str(ROOT)], capture_output=True, text=True)
    if run.returncode != 0:
        lines = [l for l in run.stderr.splitlines() if "Error" in l]
        check("point model runs on synthetic forecasts", False, (lines or ["node failed"])[0][:140])
        return
    got = json.loads(run.stdout)
    check(
        "a spot with no wave data is refused, not forecast",
        not got["noWaves"]["ok"] and got["noWaves"]["code"] == "NO_MARINE",
        json.dumps(got["noWaves"])[:120],
    )
    check(
        "missing water temperature stays missing",
        got["noTemp"]["ok"] and got["noTemp"]["sea_temp_c"] is None,
        json.dumps(got["noTemp"]),
    )
    check(
        "a real water temperature passes through",
        got["warm"]["ok"] and got["warm"]["sea_temp_c"] == 18,
        json.dumps(got["warm"]),
    )


def check_lake_data():
    catalogue = json.loads(read("lakes.json"))
    ids = [e["id"] for e in catalogue["lakes"]]
    index = json.loads(read("docs/lakes/index.json"))["lakes"]
    problems = []
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids in lakes.json")
    if [e["id"] for e in index] != ids:
        problems.append("docs/lakes/index.json does not list the catalogue in order")
    for e in catalogue["lakes"]:
        path = ROOT / "docs" / "lakes" / f"{e['id']}.json"
        if not path.is_file():
            problems.append(f"{e['id']}: outline file missing")
            continue
        lake = json.loads(path.read_text(encoding="utf-8"))
        if lake["baseline"] != catalogue["clarity_baseline"].get(e["clarity"]):
            problems.append(f"{e['id']}: baseline is out of date with its clarity")
        if e["ice_risk"] not in ("none", "rare", "seasonal"):
            problems.append(f"{e['id']}: unknown ice_risk {e['ice_risk']}")
        if any(len(r) < 4 or r[0] != r[-1] for r in lake["rings"]):
            problems.append(f"{e['id']}: an outline ring is not closed")
        span = lake.get("surface_c")
        if not (isinstance(span, list) and len(span) == 2 and span[0] < span[1]):
            problems.append(f"{e['id']}: surface_c must be [low, high]")
        if (e["ice_risk"] == "seasonal") != bool(lake.get("ice_months")):
            problems.append(f"{e['id']}: ice_months must be set for seasonal-ice lakes, and only for them")
    check("lake outlines match the catalogue", not problems, "; ".join(problems[:3]) or f"{len(ids)} lakes")


def check_lakes(node):
    if not node:
        return
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "lakes.mjs"
        script.write_text(LAKES_JS, encoding="utf-8")
        run = subprocess.run([node, str(script), str(ROOT)], capture_output=True, text=True)
    if run.returncode != 0:
        lines = [l for l in run.stderr.splitlines() if "Error" in l]
        check("lake model runs", False, (lines or ["node failed"])[0][:140])
        return
    got = json.loads(run.stdout)
    labels = {
        "rect_fetch": "wind fetch is measured correctly on a rectangular lake",
        "snap": "points just off a shore snap into the lake, far ones are refused",
        "waves_formula": "wind waves match the published fetch-limited formulas",
        "calm_is_flat": "no wind means no waves",
        "past_days_clamped": "a stale config cannot ask the forecast API for months of hourly history",
        "fully_developed_cap": "waves never exceed the fully developed limit",
        "wave_series": "waves grow when the wind rises and die when it drops",
        "water_tropical": "a tropical lake stays inside its typical temperature range",
        "water_seasonal": "water temperature follows the seasons, damped and lagging the air",
        "ice_cycle": "ice forms only in an ice lake's winter and never on an ice-free lake",
        "ice_window": "ice never forms outside the lake's ice months, even in a cold summer",
        "lake_centres_in_water": "every lake's centre is in water and has open fetch",
        "lake_forecast_calm": "a calm lake day forecasts Go with no marine call and no species",
        "lake_forecast_ice": "a frozen lake reports ice instead of a forecast",
        "fill_daily": "missing days in the temperature history are filled by interpolation",
        "lake_forecast_no_history": "a lake still forecasts, and says so, when the temperature history cannot load",
        "lake_forecast_gale": "a gale on a lake is Stay home for waves or wind",
    }
    for key, label in labels.items():
        detail = ", ".join(got.get("lake_centres_bad", [])) if key == "lake_centres_in_water" else ""
        check(label, got.get(key) is True, detail)


def check_selftest(ff_regions):
    docs = ROOT / "docs"
    outputs = [docs / "data", docs / "r", docs / "sitemap.xml", docs / "robots.txt"]
    present = [p.name for p in outputs if p.exists()]
    if present:
        print(f"NOTE  selftest skipped: {', '.join(present)} already exist under docs/ and would be overwritten")
        INFO["selftest"] = "skipped"
        return
    region = ff_regions[0]["id"]
    started = time.monotonic()
    needed = ["now", "days", "best_window", "weights", "verdict_limits", "series", "depth_range_m", "spots"]
    try:
        run = subprocess.run(
            [sys.executable, "fish_finder.py", "--selftest", "--select", region],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
        INFO["selftest"] = f"ran on {region} in {time.monotonic() - started:.1f}s"
        if run.returncode != 0:
            tail = (run.stdout + run.stderr).strip().splitlines()[-1:] or [""]
            check(f"selftest runs ({region})", False, tail[0][:140])
            return
        index = json.loads((docs / "data" / "index.json").read_text(encoding="utf-8"))
        latest = json.loads((docs / "data" / region / "latest.json").read_text(encoding="utf-8"))
        ok_index = len(index["regions"]) == 1 and not index["failed"] and index.get("failure_reasons") == {}
        check("selftest publishes one region with no failures", ok_index, str(index.get("failed")))
        missing = [k for k in needed if k not in latest]
        check("latest.json has every field the page reads", not missing, ", ".join(missing))
        verdicts = {"go", "marginal", "poor", "stay home", "not now"}
        check("selftest verdict is a known word", latest["now"]["verdict"] in verdicts, str(latest["now"]["verdict"]))
        model = json.loads((docs / "data" / "model.json").read_text(encoding="utf-8"))
        check("model.json carries the verdict and workability constants", all(k in model for k in ("verdict", "workability", "visibility")))
    finally:
        for p in outputs:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()


def main():
    files = tracked()
    parses = check_python(files)
    check_json(files)
    node = shutil.which("node")
    check_javascript(files, node)
    entries = check_shell_urls()
    check_version_bump(entries)
    ff = None
    if parses:
        sys.path.insert(0, str(ROOT))
        try:
            import fish_finder as ff
        except Exception as e:
            check("fish_finder imports", False, f"{e.__class__.__name__}: {e}")
    if ff is not None:
        base = json.loads(read("config.json"))
        regions = ff.load_regions(ROOT / "regions.json")
        check_verdicts(ff, base, regions)
        check_parity(ff, base, regions, node)
        check_point_model(node)
        check_lake_data()
        check_lakes(node)
        check_selftest(regions)
    summary = (
        f"{INFO.get('passed', 0)} checks passed, {len(FAILURES)} failed, "
        f"{len(WARNINGS)} warning(s), selftest {INFO.get('selftest', 'not run')}, "
        f"node {INFO.get('node', 'not available')}"
    )
    print()
    if FAILURES:
        print(f"PRE-FLIGHT FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        print(f"::error title=Pre-flight failed::{summary}. Failed: {'; '.join(FAILURES)}")
        return 1
    print("pre-flight passed" + (f" with {len(WARNINGS)} warning(s)" if WARNINGS else ""))
    print(f"::notice title=Pre-flight::{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
