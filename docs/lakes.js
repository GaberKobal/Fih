const G = 9.81;
const BINS = 36;
const OM_WEATHER = "https://api.open-meteo.com/v1/forecast";
const OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive";

const DEFAULTS = {
  wind_factor: 1.15,
  fetch_cap_km: 150,
  snap_m: 2500,
  past_days: 7,
  archive_lag_days: 8,
  depth_best_m: [2, 8],
  decay_min_hours: 1,
  temperature: {
    tau_min_days: 4,
    tau_per_ln_depth_days: 5,
    tau_max_days: 40,
    overshoot_c: 1.5,
    history_days: 400,
    window_days: 365,
    min_window_days: 200,
  },
  ice: {
    freeze_c: 2.0,
    stefan: 0.025,
    melt_m_per_degree_day: 0.004,
    tau_base_days: 4,
    tau_per_sqrt_depth_days: 5,
    tau_max_days: 150,
    min_thickness_m: 0.03,
  },
};

export function lakeSettings(cfg) {
  const l = (cfg && cfg.lake) || {};
  const merged = {
    ...DEFAULTS,
    ...l,
    temperature: { ...DEFAULTS.temperature, ...(l.temperature || {}) },
    ice: { ...DEFAULTS.ice, ...(l.ice || {}) },
  };
  merged.past_days = Math.min(14, Math.max(1, Number(merged.past_days) || DEFAULTS.past_days));
  return merged;
}

let indexPromise = null;
const outlines = new Map();

export function loadLakeIndex(base = "lakes/") {
  if (!indexPromise) {
    indexPromise = fetch(base + "index.json")
      .then((r) => {
        if (!r.ok) throw new Error("lake index unavailable");
        return r.json();
      })
      .catch(() => {
        indexPromise = null;
        return { lakes: [] };
      });
  }
  return indexPromise;
}

export function loadLake(id, base = "lakes/") {
  if (!outlines.has(id)) {
    outlines.set(
      id,
      fetch(`${base}${id}.json`).then((r) => {
        if (!r.ok) throw new Error(`lake outline ${id} unavailable`);
        return r.json();
      })
    );
  }
  return outlines.get(id);
}

function scales(lat) {
  return [Math.cos((lat * Math.PI) / 180) * 111320, 110540];
}

function project(lake, lat, lon) {
  const [kx, ky] = scales(lat);
  return lake.rings.map((ring) => ring.map((p) => [(p[1] - lon) * kx, (p[0] - lat) * ky]));
}

function insideAt(rings, px, py) {
  let inside = false;
  for (const ring of rings) {
    for (let i = 0, n = ring.length - 1; i < n; i++) {
      const x1 = ring[i][0], y1 = ring[i][1], x2 = ring[i + 1][0], y2 = ring[i + 1][1];
      if (y1 > py !== y2 > py) {
        const xi = x1 + ((py - y1) * (x2 - x1)) / (y2 - y1);
        if (xi > px) inside = !inside;
      }
    }
  }
  return inside;
}

function nearestEdge(rings) {
  let best = null;
  for (const ring of rings) {
    for (let i = 0, n = ring.length - 1; i < n; i++) {
      const x1 = ring[i][0], y1 = ring[i][1], x2 = ring[i + 1][0], y2 = ring[i + 1][1];
      const dx = x2 - x1, dy = y2 - y1;
      const len2 = dx * dx + dy * dy;
      const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, (-x1 * dx - y1 * dy) / len2));
      const qx = x1 + t * dx, qy = y1 + t * dy;
      const d = Math.hypot(qx, qy);
      if (!best || d < best.dist) best = { dist: d, x: qx, y: qy };
    }
  }
  return best;
}

export function snapIntoLake(lake, lat, lon, tolM) {
  const rings = project(lake, lat, lon);
  if (insideAt(rings, 0, 0)) return { lat, lon, snapped: false };
  const edge = nearestEdge(rings);
  if (!edge || edge.dist > tolM) return null;
  const [kx, ky] = scales(lat);
  for (const step of [400, 900, 1600, 2400]) {
    for (let k = 0; k < 24; k++) {
      const a = (k * Math.PI) / 12;
      const px = edge.x + step * Math.cos(a), py = edge.y + step * Math.sin(a);
      if (insideAt(rings, px, py)) {
        return { lat: lat + py / ky, lon: lon + px / kx, snapped: true };
      }
    }
  }
  return null;
}

export async function lakeAtPoint(lat, lon, cfg, base = "lakes/") {
  const index = await loadLakeIndex(base);
  const o = lakeSettings(cfg);
  const pad = 0.05;
  const near = (index.lakes || []).filter(
    (l) => lat >= l.bbox[0] - pad && lat <= l.bbox[2] + pad && lon >= l.bbox[1] - pad && lon <= l.bbox[3] + pad
  );
  for (const entry of near) {
    const lake = await loadLake(entry.id, base);
    const hit = snapIntoLake(lake, lat, lon, o.snap_m);
    if (hit) return { lake, lat: hit.lat, lon: hit.lon, snapped: hit.snapped };
  }
  return null;
}

function rayDistance(segs, dx, dy) {
  let best = Infinity;
  for (let i = 0; i < segs.length; i += 4) {
    const ax = segs[i], ay = segs[i + 1];
    const sx = segs[i + 2] - ax, sy = segs[i + 3] - ay;
    const denom = dx * sy - dy * sx;
    if (denom === 0) continue;
    const t = (ax * sy - ay * sx) / denom;
    const u = (ax * dy - ay * dx) / denom;
    if (t > 1 && u >= 0 && u <= 1 && t < best) best = t;
  }
  return best;
}

export function fetchTable(lake, lat, lon, capM) {
  const rings = project(lake, lat, lon);
  const flat = [];
  for (const ring of rings) {
    for (let i = 0, n = ring.length - 1; i < n; i++) {
      flat.push(ring[i][0], ring[i][1], ring[i + 1][0], ring[i + 1][1]);
    }
  }
  const segs = Float64Array.from(flat);
  const table = new Float64Array(BINS);
  for (let b = 0; b < BINS; b++) {
    let sum = 0;
    for (let k = -4; k <= 4; k++) {
      const ang = ((b * 10 + k * 3) * Math.PI) / 180;
      sum += Math.min(capM, rayDistance(segs, Math.sin(ang), Math.cos(ang)));
    }
    table[b] = sum / 9;
  }
  return table;
}

export function windWaves(windKmh, fetchM, o) {
  const u = (Math.max(0, windKmh) / 3.6) * o.wind_factor;
  if (!(u >= 0.5) || !(fetchM >= 50)) return { hs: 0, tp: 0, growthH: 0.25 };
  const ua = 0.71 * Math.pow(u, 1.23);
  const x = (G * fetchM) / (ua * ua);
  const hs = Math.min(((0.0016 * ua * ua) / G) * Math.sqrt(x), (0.2433 * ua * ua) / G);
  const tp = Math.min(0.286 * (ua / G) * Math.pow(x, 1 / 3), (8.134 * ua) / G);
  const minDuration = 68.8 * Math.pow(x, 2 / 3) * (ua / G);
  return { hs, tp, growthH: Math.max(0.25, minDuration / 3600 / 3) };
}

export function lakeWaveSeries(windKmh, dirDeg, table, o) {
  const n = windKmh.length;
  const hs = new Array(n), tp = new Array(n), fetchKm = new Array(n);
  let h = 0, t = 0;
  for (let i = 0; i < n; i++) {
    const dir = Number.isFinite(dirDeg[i]) ? dirDeg[i] : 0;
    const bin = Math.round((((dir % 360) + 360) % 360) / 10) % BINS;
    const f = table[bin];
    const w = Number.isFinite(windKmh[i]) ? windKmh[i] : 0;
    const eq = windWaves(w, f, o);
    const tau = eq.hs > h ? eq.growthH : Math.max(o.decay_min_hours, eq.growthH);
    const k = 1 - Math.exp(-1 / tau);
    h += (eq.hs - h) * k;
    t += (eq.tp - t) * k;
    hs[i] = h;
    tp[i] = t;
    fetchKm[i] = f / 1000;
  }
  return { hs, tp, fetchKm };
}

export function dailyMeans(times, values) {
  const sums = new Map();
  times.forEach((t, i) => {
    if (!Number.isFinite(values[i])) return;
    const d = t.slice(0, 10);
    const s = sums.get(d) || [0, 0];
    s[0] += values[i];
    s[1] += 1;
    sums.set(d, s);
  });
  return new Map([...sums].map(([d, s]) => [d, s[0] / s[1]]));
}

export function lakeWaterModel(dates, air, lake, o) {
  const T = o.temperature, I = o.ice;
  const depth = Math.max(lake.mean_depth_m, 2);
  const tau = Math.min(T.tau_max_days, Math.max(T.tau_min_days, T.tau_min_days + T.tau_per_ln_depth_days * Math.log(depth / 2)));
  const tauIce = Math.min(I.tau_max_days, I.tau_base_days + I.tau_per_sqrt_depth_days * Math.sqrt(depth));
  const [lo, hi] = lake.surface_c;
  const seasonal = lake.ice_risk === "seasonal";
  const months = new Set(lake.ice_months || []);
  const warm = air.slice(0, 30).filter(Number.isFinite);
  const start = warm.length ? warm.reduce((a, b) => a + b, 0) / warm.length : (lo + hi) / 2;
  let w = start, wi = start, h = 0, fdd = 0;
  const n = air.length;
  const E = new Array(n), thickness = new Array(n);
  for (let i = 0; i < n; i++) {
    const ta = Number.isFinite(air[i]) ? air[i] : w;
    const iceOn = h > I.min_thickness_m;
    if (iceOn) w = lo;
    else w += (ta - w) / tau;
    wi += (ta - wi) / tauIce;
    const month = Number(dates[i].slice(5, 7));
    if (!seasonal || !months.has(month)) {
      h = 0;
      fdd = 0;
    } else if (!iceOn) {
      if (wi <= I.freeze_c && ta < 0) {
        fdd += -ta;
        h = I.stefan * Math.sqrt(fdd);
      } else {
        fdd = 0;
        h = 0;
      }
    } else if (ta < 0) {
      fdd += -ta;
      h = Math.max(h, I.stefan * Math.sqrt(fdd));
    } else {
      h -= I.melt_m_per_degree_day * ta;
      if (h <= 0) {
        h = 0;
        fdd = 0;
      } else {
        fdd = Math.pow(h / I.stefan, 2);
      }
    }
    E[i] = w;
    thickness[i] = h;
  }
  const temp = new Array(n);
  for (let i = 0; i < n; i++) {
    const from = i - T.window_days >= 0 && i >= T.min_window_days ? i - T.window_days : 0;
    let emin = Infinity, emax = -Infinity;
    for (let j = from; j <= i; j++) {
      if (E[j] < emin) emin = E[j];
      if (E[j] > emax) emax = E[j];
    }
    const x = emax - emin < 1 ? 0.5 : (E[i] - emin) / (emax - emin);
    const t = lo + x * (hi - lo);
    temp[i] = thickness[i] > I.min_thickness_m ? lo : Math.max(0, Math.min(hi + T.overshoot_c, Math.max(lo - T.overshoot_c, t)));
  }
  return { temp, thickness };
}

async function archiveDaily(lat, lon, o) {
  const dayMs = 86400e3;
  const end = new Date(Date.now() - o.archive_lag_days * dayMs);
  const start = new Date(end.getTime() - o.temperature.history_days * dayMs);
  const iso = (d) => d.toISOString().slice(0, 10);
  const q = `latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}&start_date=${iso(start)}&end_date=${iso(end)}&daily=temperature_2m_mean&timezone=auto`;
  try {
    const r = await fetch(`${OM_ARCHIVE}?${q}`);
    if (!r.ok) return new Map();
    const j = await r.json();
    const d = j.daily;
    if (!d || !d.time) return new Map();
    return new Map(d.time.map((t, i) => [t, d.temperature_2m_mean[i]]).filter((e) => Number.isFinite(e[1])));
  } catch (e) {
    return new Map();
  }
}

export function fillDaily(map) {
  const known = [...map].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([d, v]) => [Date.parse(d), v]);
  if (!known.length) return { dates: [], values: [], filled: 0 };
  const dayMs = 86400e3;
  const dates = [], values = [];
  let k = 0, filled = 0;
  for (let t = known[0][0]; t <= known[known.length - 1][0]; t += dayMs) {
    while (k + 1 < known.length && known[k + 1][0] <= t) k++;
    dates.push(new Date(t).toISOString().slice(0, 10));
    if (known[k][0] === t) {
      values.push(known[k][1]);
    } else {
      const a = known[k], b = known[k + 1];
      values.push(a[1] + ((b[1] - a[1]) * (t - a[0])) / (b[0] - a[0]));
      filled++;
    }
  }
  return { dates, values, filled };
}

export async function lakeConditions(lat, lon, cfg, lake) {
  const o = lakeSettings(cfg);
  const fwd = cfg.conditions?.forecast_days ?? 3;
  const vars = ["precipitation", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "cloud_cover", "pressure_msl", "temperature_2m"];
  const q = `latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}&past_days=${o.past_days}&forecast_days=${fwd}&timezone=auto`;
  const [w, history] = await Promise.all([
    fetch(`${OM_WEATHER}?${q}&hourly=${vars.join(",")}`).then((r) => r.json()),
    archiveDaily(lat, lon, o),
  ]);
  if (w.error || !w.hourly || !w.hourly.time) throw new Error(w.reason || "Open-Meteo error");
  const times = w.hourly.time;
  const col = (k) => (w.hourly[k] || []).map((v) => (v == null ? NaN : v));
  const h = { time: times };
  vars.forEach((k) => (h[k] = col(k)));
  const table = fetchTable(lake, lat, lon, o.fetch_cap_km * 1000);
  const waves = lakeWaveSeries(h.wind_speed_10m, h.wind_direction_10m, table, o);
  const zeros = times.map(() => 0);
  const dir = h.wind_direction_10m.map((v) => (Number.isFinite(v) ? v : 0));
  const merged = new Map([...history, ...dailyMeans(times, h.temperature_2m)]);
  const { dates, values, filled } = fillDaily(merged);
  const model = lakeWaterModel(dates, values, lake, o);
  const at = new Map(dates.map((d, i) => [d, i]));
  const temp = times.map((t) => model.temp[at.get(t.slice(0, 10))]);
  const thickness = times.map((t) => model.thickness[at.get(t.slice(0, 10))]);
  Object.assign(h, {
    wave_height: waves.hs,
    wave_period: waves.tp,
    wave_direction: dir,
    wind_wave_height: waves.hs,
    wind_wave_period: waves.tp,
    wind_wave_direction: dir,
    swell_wave_height: zeros,
    swell_wave_period: zeros,
    swell_wave_direction: dir,
    sea_level_height_msl: zeros,
    ocean_current_velocity: zeros,
    ocean_current_direction: zeros,
    sea_surface_temperature: temp,
  });
  return {
    h,
    times,
    tz: (w.utc_offset_seconds ?? 0) | 0,
    meta: {
      ice: thickness.map((x) => x > o.ice.min_thickness_m),
      thickness,
      fetchKm: waves.fetchKm,
      table: Array.from(table),
      settings: o,
      historyDays: history.size,
      filledDays: filled,
    },
  };
}
