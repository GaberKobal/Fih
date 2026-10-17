const OM_MARINE = "https://marine-api.open-meteo.com/v1/marine";
const OM_WEATHER = "https://api.open-meteo.com/v1/forecast";

const clamp01 = v => Math.max(0, Math.min(1, v));
const norm = (x, lo, hi) => clamp01((x - lo) / (hi - lo));

function decayMemory(x, tauHours){
  const k = Math.exp(-1 / tauHours);
  let acc = 0;
  return x.map(v => (acc = acc * k + (v || 0)) * (1 - k));
}

function waveNumber(T, h, g = 9.81){
  const Tc = Math.min(25, Math.max(1, T)), hh = Math.max(0.5, h);
  const w = 2 * Math.PI / Tc;
  let k = w * w / g;
  for(let i = 0; i < 12; i++){
    const th = Math.tanh(k * hh);
    const f = g * k * th - w * w;
    const df = g * th + g * k * hh * (1 - th * th);
    k = Math.max(1e-6, k - f / Math.max(df, 1e-12));
  }
  return k;
}

function orbitalVelocity(H, T, depth){
  if(!isFinite(H) || H <= 0) return 0;
  const Tc = Math.min(25, Math.max(1, isFinite(T) ? T : 8));
  const hh = Math.max(0.5, depth);
  const k = waveNumber(Tc, hh);
  const sinh = Math.sinh(Math.min(k * hh, 50));
  return Math.PI * H / (Tc * Math.max(sinh, 1e-9));
}

function stirring(h, cfg, depth){
  const c = cfg.visibility, n = h.time.length;
  if((c.stir_model || "orbital") === "hs_cubed"){
    const ref = Math.pow(c.wave_ref_hs ?? 1, 3);
    return h.wave_height.map(v => clamp01(Math.pow(v || 0, 3) / ref));
  }
  const refT = cfg.workability?.period_ref_s ?? 8;
  const has = k => h[k] && h[k].some(isFinite);
  const split = has("wind_wave_height") || has("swell_wave_height");
  const ref = Math.pow(c.orbital_ref_ms ?? 0.35, c.orbital_exponent ?? 3);
  const out = new Array(n);
  for(let i = 0; i < n; i++){
    let ub;
    if(split){
      const a = orbitalVelocity(h.wind_wave_height?.[i],
                                h.wind_wave_period?.[i] ?? refT, depth);
      const b = orbitalVelocity(h.swell_wave_height?.[i],
                                h.swell_wave_period?.[i] ?? refT, depth);
      ub = Math.sqrt(a*a + b*b);
    }else{
      ub = orbitalVelocity(h.wave_height[i], h.wave_period?.[i] ?? refT, depth);
    }
    out[i] = clamp01(Math.pow(ub, c.orbital_exponent ?? 3) / ref);
  }
  return out;
}

function solarElevation(times, lat, lon, tzOffsetSec){
  return times.map(t => {
    const local = new Date(t + "Z");
    const u = new Date(local.getTime() - tzOffsetSec * 1000);
    const start = Date.UTC(u.getUTCFullYear(), 0, 1);
    const n = Math.floor((u.getTime() - start) / 86400000) + 1;
    const fh = u.getUTCHours() + u.getUTCMinutes() / 60;
    const g = 2 * Math.PI / 365 * (n - 1 + (fh - 12) / 24);
    const eq = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
             - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
    const dec = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g)
              - 0.006758 * Math.cos(2 * g) + 0.000907 * Math.sin(2 * g)
              - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    const tst = fh * 60 + eq + 4 * lon;
    const ha = (tst / 4 - 180) * Math.PI / 180;
    const la = lat * Math.PI / 180;
    const cz = Math.sin(la) * Math.sin(dec) + Math.cos(la) * Math.cos(dec) * Math.cos(ha);
    return Math.asin(Math.max(-1, Math.min(1, cz))) * 180 / Math.PI;
  });
}

function lightQuality(elev, cfg, cloudPct){
  const c = cfg.light || {};
  const golden = c.golden_max_deg ?? 12, high = c.high_sun_deg ?? 45,
        floor = c.high_sun_factor ?? 0.6, soften = c.cloud_softening ?? 0.5;
  return elev.map((e, i) => {
    if(e <= 0) return 0;
    let floorEff = floor;
    if(cloudPct){
      const frac = clamp01((isFinite(cloudPct[i]) ? cloudPct[i] : 0) / 100);
      floorEff = floor + (1 - floor) * soften * frac;
    }
    return 1 - (1 - floorEff) * clamp01((e - golden) / Math.max(high - golden, 1e-6));
  });
}

function pressureFactor(h, cfg){
  const c = cfg.pressure || {};
  const p = h.pressure_msl;
  if(!p || !p.some(isFinite)) return null;
  const win = Math.max(1, c.trend_window_h ?? 3);
  const sens = c.sensitivity ?? 0.02;
  const cap = c.max_swing ?? 0.08;
  return p.map((v, i) => {
    if(i < win || !isFinite(v) || !isFinite(p[i-win])) return 1.0;
    const trend = (v - p[i-win]) / win;
    return 1 + Math.max(-cap, Math.min(cap, -trend * sens));
  });
}

function classifyWind(dirDeg, speed, gust, cfg){
  const regs = cfg.wind_regimes;
  if(!regs) return {name:"", viz:1, work:1};
  const d = ((dirDeg % 360) + 360) % 360;
  let name = "other", spec = regs.other || {};
  for(const [key, r] of Object.entries(regs)){
    if(key === "other" || key.startsWith("_") || !r || !r.from_deg) continue;
    const [lo, hi] = r.from_deg;
    if(lo <= hi ? (d >= lo && d <= hi) : (d >= lo || d <= hi)){ name = key; spec = r; break; }
  }
  let work = spec.work_factor ?? 1;
  const gc = cfg.gust || {};
  if(speed > 3 && isFinite(gust) && gust / speed > (gc.ratio_threshold ?? 1.6))
    work *= (gc.penalty ?? 0.8);
  return {name, viz: spec.viz_factor ?? 1, work, note: spec.note || ""};
}

function tempAtDepth(sst, depth, month, cfg){
  const tc = cfg.thermocline;
  if(!tc) return sst;
  const drop = tc.monthly_drop_c[month - 1] ?? 0;
  return sst - drop * Math.min(Math.max(depth / tc.reference_depth_m, 0), 1.2);
}

function trapezoid(x, lo, bLo, bHi, hi){
  return Math.min(clamp01((x - lo) / Math.max(bLo - lo, 1e-9)),
                  clamp01((hi - x) / Math.max(hi - bHi, 1e-9)));
}

function legalStatus(sp, date, legalPack){
  const L = (legalPack && legalPack.species && legalPack.species[sp.id]) || {};
  if(!legalPack || !legalPack.closed_seasons_researched)
    return {open:true, reason:"", unknown:true, legal:L};
  const md = String(date.getUTCMonth()+1).padStart(2,"0") + "-"
           + String(date.getUTCDate()).padStart(2,"0");
  for(const w of (L.closed_seasons || [])){
    const closed = w.from <= w.to ? (md >= w.from && md <= w.to)
                                  : (md >= w.from || md <= w.to);
    if(closed) return {open:false, reason:`closed season ${w.from} to ${w.to}`,
                       unknown:false, legal:L};
  }
  return {open:true, reason:"", unknown:false, legal:L};
}

function speciesDayFactors(sp, sst, vizScore, date, cfg, legalPack){
  const month = date.getUTCMonth() + 1;
  const dMid = (sp.depth_best_m[0] + sp.depth_best_m[1]) / 2;
  const tAt = tempAtDepth(sst, dMid, month, cfg);
  const tempFit = trapezoid(tAt, sp.temp_c[0], sp.temp_best_c[0],
                                 sp.temp_best_c[1], sp.temp_c[1]);
  const law = legalStatus(sp, date, legalPack);
  const seasonFit = (sp.months.includes(month) && law.open) ? 1 : 0;
  const pref = sp.viz_pref || "any";
  const vizFit = pref === "clear" ? vizScore
               : pref === "murky" ? 1 - 0.65 * vizScore
               : 0.55 + 0.45 * vizScore;
  return {
    id: sp.id, name_en: sp.name_en || sp.common, common: sp.common,
    name_local: sp.name_local,
    scientific: sp.scientific, note: sp.note, viz_pref: pref,
    depth_best_m: sp.depth_best_m, depth_m: sp.depth_m,
    relief_affinity: sp.relief_affinity, slope_affinity: sp.slope_affinity,
    substrate: sp.substrate, wary: sp.wary,
    temp_fit: +tempFit.toFixed(3), viz_fit: +vizFit.toFixed(3),
    season_fit: seasonFit, temp_at_depth_c: +tAt.toFixed(1),
    today: +(tempFit * seasonFit * vizFit).toFixed(3),
    in_season: !!seasonFit, legally_open: law.open,
    closed_reason: law.reason, seasons_unknown: law.unknown,
    legal: law.legal, spots: [],
  };
}

export function verdictLimits(cfg){
  const v = cfg.verdict || {}, vis = cfg.visibility || {}, wc = cfg.workability || {};
  const lo = vis.viz_min_m ?? 1, hi = vis.viz_max_m ?? 11, span = hi - lo;
  const k = v.local_bar ?? 1, base = vis.baseline ?? 0;
  const ceil = Math.max(1 - k * base, 0.05);
  const floor = wc.hard_floor ?? 0.2;
  const atFloor = (good, bad) => good + (1 - floor) * (bad - good);
  const r1 = x => Math.round(x * 10) / 10;
  const out = {
    viz_go_m: r1(lo + (v.go ?? 0.62) * ceil * span),
    viz_marginal_m: r1(lo + (v.marginal ?? 0.4) * ceil * span),
    viz_best_m: r1(lo + (1 - base) * span),
    swell_stay_home_m: Math.round(atFloor(wc.hs_good_m ?? 0.25, wc.hs_bad_m ?? 0.9) * 100) / 100,
    wind_stay_home_kmh: Math.round(atFloor(wc.wind_good_kmh ?? 10, wc.wind_bad_kmh ?? 35)),
    period_ref_s: wc.period_ref_s ?? 8,
    local_bar: k > 0,
  };
  if((wc.current_bad_ms ?? 0) > 0)
    out.current_stay_home_ms =
      Math.round(atFloor(wc.current_good_ms ?? 0.25, wc.current_bad_ms) * 100) / 100;
  return out;
}

export async function forecastPoint(lat, lon, cfg, legalPack){
  const past = (cfg.conditions?.past_days ?? 5), fwd = (cfg.conditions?.forecast_days ?? 3);
  const mVars = ["wave_height","wave_period","wave_direction",
                 "wind_wave_height","wind_wave_period","wind_wave_direction",
                 "swell_wave_height","swell_wave_period","swell_wave_direction",
                 "sea_surface_temperature","sea_level_height_msl",
                 "ocean_current_velocity","ocean_current_direction"];
  const wVars = ["precipitation","wind_speed_10m","wind_direction_10m",
                 "wind_gusts_10m","cloud_cover","pressure_msl"];
  const q = `latitude=${lat.toFixed(4)}&longitude=${lon.toFixed(4)}`
          + `&past_days=${past}&forecast_days=${fwd}&timezone=auto`;

  const [m, w] = await Promise.all([
    fetch(`${OM_MARINE}?${q}&hourly=${mVars.join(",")}`).then(r=>r.json()),
    fetch(`${OM_WEATHER}?${q}&hourly=${wVars.join(",")}&daily=sunrise,sunset`).then(r=>r.json()),
  ]);
  if(m.error || w.error) throw new Error(m.reason || w.reason || "Open-Meteo error");
  if(!m.hourly || !m.hourly.wave_height)
    throw new Error("no marine data here, this looks like an inland point");

  const times = m.hourly.time;
  const h = {time: times};
  mVars.forEach(k => h[k] = (m.hourly[k] || []).map(v => v == null ? NaN : v));

  const wTimes = (w.hourly && w.hourly.time) || [];
  let wAt = i => i;
  if(wTimes.length !== times.length || wTimes[0] !== times[0]){
    const pos = new Map(wTimes.map((t, i) => [t, i]));
    const hits = times.filter(t => pos.has(t)).length;
    if(hits < times.length * 0.5)
      throw new Error("marine and weather forecasts cover different hours here");
    wAt = i => (pos.has(times[i]) ? pos.get(times[i]) : -1);
  }
  wVars.forEach(k => {
    const src = w.hourly[k] || [];
    h[k] = times.map((_, i) => {
      const j = wAt(i);
      const v = j < 0 ? null : src[j];
      return v == null ? NaN : v;
    });
  });

  const tz = (w.utc_offset_seconds ?? m.utc_offset_seconds ?? 0) | 0;

  const c = cfg.visibility;
  const dBest = cfg.depth?.best_m || [4, 12];
  const vizDepth = 0.5 * (dBest[0] + dBest[1]);
  const stir = decayMemory(stirring(h, cfg, vizDepth), c.wave_tau_h);
  const plume = decayMemory(h.precipitation.map(v => v||0), c.rain_tau_h);
  const vBase = c.baseline ?? 0;
  let viz = stir.map((s,i) => clamp01(1 - (vBase
    + c.wave_weight * clamp01(s)
    + c.rain_weight * norm(plume[i], 0, c.rain_ref_mm_per_h))));

  const wc = cfg.workability;
  const regimes = times.map((_,i) => classifyWind(h.wind_direction_10m[i]||0,
    h.wind_speed_10m[i]||0, h.wind_gusts_10m[i], cfg));
  viz = viz.map((v,i) => clamp01(v * regimes[i].viz));
  const vizM = viz.map(v => c.viz_min_m + v * (c.viz_max_m - c.viz_min_m));

  const periodRef = wc.period_ref_s ?? 8.0;
  const steep = (H, T) => {
    const hv = isFinite(H) ? H : 0;
    const pv = Math.min(20, Math.max(2, isFinite(T) ? T : periodRef));
    return hv * Math.min(1.6, Math.max(0.6, periodRef / pv));
  };
  const hasSplit = (h.wind_wave_height || []).some(isFinite)
                || (h.swell_wave_height || []).some(isFinite);
  const hsEff = times.map((_,i) => {
    const comb = steep(isFinite(h.wave_height[i]) ? h.wave_height[i] : 1, h.wave_period[i]);
    if(!hasSplit) return comb;
    const a = steep(h.wind_wave_height?.[i], h.wind_wave_period?.[i]);
    const b = steep(h.swell_wave_height?.[i], h.swell_wave_period?.[i]);
    return Math.max(a, b, 0.8 * comb);
  });
  const seaOk = hsEff.map(v => 1 - norm(v, wc.hs_good_m, wc.hs_bad_m));
  const airOk = h.wind_speed_10m.map(v => 1 - norm(isFinite(v)?v:30, wc.wind_good_kmh, wc.wind_bad_kmh));
  const curBad = wc.current_bad_ms ?? 0;
  const flowOk = times.map((_,i) => curBad > 0
    ? 1 - norm(isFinite(h.ocean_current_velocity[i]) ? h.ocean_current_velocity[i] : 0,
               wc.current_good_ms ?? 0.25, curBad)
    : 1);
  const work = seaOk.map((s,i) =>
    clamp01(Math.min(s, airOk[i], flowOk[i]) * regimes[i].work));

  const mc = cfg.movement || {};
  const lvl = h.sea_level_height_msl.map(v => isFinite(v) ? v : 0);
  const rate = lvl.map((v,i,a) => Math.abs((a[Math.min(i+1,a.length-1)] - a[Math.max(i-1,0)]) / 2));
  const move = rate.map((r,i) => clamp01(
      0.5 * norm(r, 0, mc.tide_rate_ref_m_per_h ?? 0.08)
    + 0.5 * norm(isFinite(h.ocean_current_velocity[i]) ? h.ocean_current_velocity[i] : 0,
                 0, mc.current_ref_ms ?? 0.3)));
  const tideRange = Math.max(...lvl) - Math.min(...lvl);

  const light = lightQuality(solarElevation(times, lat, lon, tz), cfg, h.cloud_cover);
  const pf = pressureFactor(h, cfg);

  const dw = cfg.day_weights;
  const hourly = times.map((_,i) =>
    (dw.visibility*viz[i] + dw.workability*work[i] + dw.movement*move[i])
    * (pf ? pf[i] : 1)
    * (work[i] > wc.hard_floor ? 1 : 0) * light[i]);

  const nowLocal = new Date(Date.now() + tz*1000);
  const nowIso = nowLocal.toISOString().slice(0,13) + ":00";
  let nowI = 0, best = Infinity;
  times.forEach((t,i) => { const d = Math.abs(new Date(t+"Z") - new Date(nowIso+"Z"));
                           if(d < best){ best = d; nowI = i; } });

  const win = cfg.conditions?.window_hours ?? 3;
  const today = times[nowI].slice(0,10);
  const byDate = {};
  times.forEach((t,i) => { if(t.slice(0,10) >= today) (byDate[t.slice(0,10)] ||= []).push(i); });

  const days = Object.keys(byDate).sort().map(date => {
    const idx = byDate[date];
    const hours = idx.map(i => ({
      t: times[i], hour: +times[i].slice(11,13), score: +hourly[i].toFixed(3),
      viz_m: +vizM[i].toFixed(1), wave_m: +(h.wave_height[i]||0).toFixed(2),
      wind_kmh: +(h.wind_speed_10m[i]||0).toFixed(1),
      sea_temp_c: +(h.sea_surface_temperature[i]||0).toFixed(1),
      daylight: light[i] > 0, light: +light[i].toFixed(2),
      workable: work[i] > wc.hard_floor, past: times[i] < nowIso,
      wind_regime: regimes[i].name,
    }));
    let bw = null;
    for(let j = 0; j + win <= hours.length; j++){
      const blk = hours.slice(j, j+win);
      if(blk.some(x => x.past)) continue;
      const s = blk.reduce((a,b)=>a+b.score,0) / win;
      if(s > 0 && (!bw || s > bw.score)){
        bw = {start: blk[0].t, end: blk[win-1].t, score: +s.toFixed(3),
              viz_m: +(blk.reduce((a,b)=>a+b.viz_m,0)/win).toFixed(1),
              wave_m: +(blk.reduce((a,b)=>a+b.wave_m,0)/win).toFixed(2),
              wind_kmh: +(blk.reduce((a,b)=>a+b.wind_kmh,0)/win).toFixed(1)};
      }
    }
    const future = hours.filter(x => !x.past);
    const peak = future.length ? future.reduce((a,b)=>b.score>a.score?b:a) : null;

    const repT = bw ? bw.start : (peak && peak.score > 0 ? peak.t : `${date}T12:00`);
    let rep = idx[0];
    for(const i of idx) if(times[i].slice(0,13) === repT.slice(0,13)){ rep = i; break; }

    return {date, is_today: date === today, best: bw, hours,
            peak_hour: peak && peak.score > 0 ? peak.t : null,
            peak_score: peak ? peak.score : 0,
            at: times[rep], _rep: rep,
            wind_from_deg: Math.round(h.wind_direction_10m[rep] || 0),
            swell_from_deg: isFinite(h.wave_direction[rep])
                            ? Math.round(h.wave_direction[rep]) : null,
            sea_temp_c: +(h.sea_surface_temperature[rep] || 0).toFixed(1),
            viz_m: +vizM[rep].toFixed(1), species: []};
  });

  const vc = cfg.verdict;
  const vizCeil = 1 - (vc.local_bar ?? 1) * (c.baseline ?? 0);
  const vizLocal = i => vizCeil > 0.05 ? Math.min(1, viz[i] / vizCeil) : viz[i];
  const dayScore = Math.min(vizLocal(nowI), work[nowI]);
  const lightLeft = times.some((t,i) => i >= nowI && light[i] > 0
                                      && t.slice(0,10) === today);
  let word, limit;
  if(light[nowI] <= 0){
    word = "not now";
    limit = lightLeft ? "dark, waiting for first light"
                      : "dark, no daylight left today";
  }
  else if(work[nowI] < wc.hard_floor)
    { word = "stay home"; limit = seaOk[nowI] <= airOk[nowI] ? "sea state" : "wind"; }
  else if(dayScore >= vc.go)      { word = "go"; limit = "nothing limiting"; }
  else {
    word = dayScore >= vc.marginal ? "marginal" : "poor";
    limit = vizLocal(nowI) < work[nowI] ? "visibility"
          : (seaOk[nowI] <= airOk[nowI] ? "sea state" : "wind");
  }

  const speciesOn = (i, dateStr) => {
    const date = new Date(dateStr + "T12:00Z");
    return (cfg.species || []).map(sp =>
        speciesDayFactors(sp, isFinite(h.sea_surface_temperature[i])
                              ? h.sea_surface_temperature[i] : 20,
                          viz[i], date, cfg, legalPack))
      .sort((a,b) => b.today - a.today);
  };
  days.forEach(d => { d.species = speciesOn(d._rep, d.date); delete d._rep; });
  const species = days.length ? days[0].species
                             : speciesOn(nowI, times[nowI].slice(0,10));

  const keep = (a) => a.slice(Math.max(0, nowI-24), Math.min(a.length, nowI+60));

  const depthCfg = cfg.depth || {};
  return {
    schema_version: 18, point: true,
    depth_range_m: [depthCfg.min_m ?? 2, depthCfg.max_m ?? 25],
    weights: cfg.weights || {},
    weights_tuned: false,
    verdict_limits: verdictLimits(cfg),
    temp_source: "Open-Meteo sea surface temperature (no depth profile for a point)",
    region: `${lat.toFixed(3)}, ${lon.toFixed(3)}`,
    generated: new Date().toISOString(),
    bounds: [[lat-0.001, lon-0.001],[lat+0.001, lon+0.001]],
    now: {
      time: times[nowI], verdict: word, limited_by: limit,
      score: +dayScore.toFixed(3), visibility_m: +vizM[nowI].toFixed(1),
      wave_height_m: +(h.wave_height[nowI]||0).toFixed(2),
      wind_speed_kmh: +(h.wind_speed_10m[nowI]||0).toFixed(1),
      wind_from_deg: Math.round(h.wind_direction_10m[nowI]||0),
      sea_temp_c: +(h.sea_surface_temperature[nowI]||0).toFixed(1),
      current_ms: +(h.ocean_current_velocity[nowI]||0).toFixed(2),
      wind_regime: regimes[nowI].name, wind_regime_note: regimes[nowI].note,
      sun_elevation_deg: +solarElevation([times[nowI]], lat, lon, tz)[0].toFixed(1),
      light: +light[nowI].toFixed(2),
    },
    best_window: days[0] ? days[0].best : null,
    days, species, spots: [],
    tide_range_m: +tideRange.toFixed(2),
    macrotidal: tideRange > (mc.macrotidal_range_m ?? 1.5),
    has_species: species.length > 0, has_exclusions: false,
    relief_meaningful: null, bathymetry_provider: "none (point forecast)",
    bathymetry_native_m: null, grid_resolution_m: null,
    legal_pack: legalPack || {},
    now_index: nowI - Math.max(0, nowI - 24),
    series: {
      time: keep(times), visibility_m: keep(vizM).map(v => +v.toFixed(1)),
      wave_height_m: keep(h.wave_height).map(v => +(v||0).toFixed(2)),
      wind_speed_kmh: keep(h.wind_speed_10m).map(v => +(v||0).toFixed(1)),
      precipitation_mm: keep(h.precipitation).map(v => +(v||0).toFixed(1)),
      sea_level_m: keep(lvl).map(v => +v.toFixed(2)),
      workability: keep(work).map(v => +v.toFixed(3)),
      light: keep(light).map(v => +v.toFixed(2)),
      wind_regime: keep(regimes).map(r => r.name),
    },
  };
}
