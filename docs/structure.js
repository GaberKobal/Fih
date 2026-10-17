const GMRT = "https://www.gmrt.org/services/GridServer";
const BATHY_CACHE = "bathy-v1";
const OSM_CACHE = "osm-v1";

export const defaults = {
  radius_km: 30,
  cell_m: 200,
  depth_min_m: 2, depth_best: [4, 18], depth_max_m: 25,
  relief_scale_m: 3.0, slope_scale: 0.04, background_m: 600,
  fetch_max_km: 20, fetch_step_m: 250, swell_weight: 0.5,
  w_relief: 0.35, w_slope: 0.20, w_shelter: 0.45, w_wreck: 0.3,
  wreck_radius_m: 250,
  top_spots: 15, min_spot_score: 0.35, min_separation_m: 500,
  land_buffer_m: 220,
  species_shelter_weight: 0.3,
};

export function boxAround(lat, lon, radiusKm){
  const dLat = radiusKm / 111.32;
  const dLon = radiusKm / (111.32 * Math.max(0.05, Math.cos(lat * Math.PI / 180)));
  return {lat_min: Math.max(-89.9, lat - dLat), lat_max: Math.min(89.9, lat + dLat),
          lon_min: Math.max(-180, lon - dLon), lon_max: Math.min(180, lon + dLon)};
}

function parseAsciiGrid(text){
  const lines = text.split("\n");
  const hdr = {};
  let i = 0;
  for(; i < 10; i++){
    const m = lines[i] && lines[i].trim().match(/^(\S+)\s+(\S+)$/);
    if(!m || /^-?[\d.]+\s/.test(lines[i].trim())) break;
    hdr[m[1].toLowerCase()] = parseFloat(m[2]);
  }
  const nx = hdr.ncols, ny = hdr.nrows, cs = hdr.cellsize;
  const nodata = hdr.nodata_value ?? -9999;
  if(!nx || !ny || !cs) throw new Error("unrecognised grid header from GMRT");

  const vals = new Float32Array(nx * ny);
  let k = 0;
  for(; i < lines.length && k < nx * ny; i++){
    const row = lines[i].trim();
    if(!row) continue;
    for(const tok of row.split(/\s+/)){
      const v = parseFloat(tok);
      vals[k++] = (v === nodata || !isFinite(v)) ? NaN : v;
    }
  }
  const elev = new Float32Array(nx * ny);
  for(let y = 0; y < ny; y++)
    elev.set(vals.subarray((ny - 1 - y) * nx, (ny - y) * nx), y * nx);

  const xIsCorner = hdr.xllcorner != null, yIsCorner = hdr.yllcorner != null;
  const x0 = (xIsCorner ? hdr.xllcorner : hdr.xllcenter) + (xIsCorner ? cs / 2 : 0);
  const y0 = (yIsCorner ? hdr.yllcorner : hdr.yllcenter) + (yIsCorner ? cs / 2 : 0);

  if(Math.abs(x0) > 360){
    const R = 20037508.34;
    const toLon = x => x / R * 180;
    const toLat = y => Math.atan(Math.exp(y / R * Math.PI)) * 360 / Math.PI - 90;
    const latS = toLat(y0), latN = toLat(y0 + (ny - 1) * cs);
    const lonW = toLon(x0), lonE = toLon(x0 + (nx - 1) * cs);
    const cellDeg = (latN - latS) / (ny - 1);
    const out = new Float32Array(nx * ny);
    for(let j = 0; j < ny; j++){
      const lat = latS + j * cellDeg;
      const my = Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)) * R / Math.PI;
      const sy = (my - y0) / cs;
      const y1 = Math.max(0, Math.min(ny - 1, Math.floor(sy)));
      const y2 = Math.min(ny - 1, y1 + 1), f = sy - y1;
      for(let i = 0; i < nx; i++)
        out[j * nx + i] = elev[y1 * nx + i] * (1 - f) + elev[y2 * nx + i] * f;
    }
    return {nx, ny, elev: out, lon0: lonW, lat0: latS, cell: cellDeg,
            cellLon: (lonE - lonW) / (nx - 1), projected: true};
  }
  return {nx, ny, elev, lon0: x0, lat0: y0, cell: cs,
          cellLon: cs, projected: false};
}

export async function fetchBathymetry(box, opts = {}){
  const q = new URLSearchParams({
    north: box.lat_max.toFixed(5), south: box.lat_min.toFixed(5),
    west: box.lon_min.toFixed(5), east: box.lon_max.toFixed(5),
    layer: "topo", format: "esriascii",
    resolution: opts.resolution || "high",
  });
  const url = `${GMRT}?${q}`;

  let cache = null;
  try{ cache = await caches.open(BATHY_CACHE); }catch(e){  }
  if(cache){
    const hit = await cache.match(url);
    if(hit) return parseAsciiGrid(await hit.text());
  }

  let res;
  try{
    res = await fetch(url);
  }catch(e){
    throw new Error("GMRT could not be reached from the browser. If this is a "
      + "CORS block there is no client-side fix, so use a precomputed region.");
  }
  if(!res.ok) throw new Error(
    `GMRT returned HTTP ${res.status}. 404 means the request parameters are `
    + `wrong; 413 means the box is too big, so try a smaller radius.`);
  const text = await res.text();
  if(/<html|<\?xml/i.test(text.slice(0, 200)))
    throw new Error("GMRT returned an error page rather than a grid");
  if(cache) cache.put(url, new Response(text));
  return parseAsciiGrid(text);
}

const idx = (g, y, x) => y * g.nx + x;

function boxBlur(src, nx, ny, r){
  const tmp = new Float32Array(nx * ny), out = new Float32Array(nx * ny);
  for(let y = 0; y < ny; y++){
    let sum = 0, n = 0;
    for(let x = -r; x <= r; x++){ const xi = Math.min(nx-1, Math.max(0, x));
      sum += src[y*nx+xi]; n++; }
    for(let x = 0; x < nx; x++){
      tmp[y*nx+x] = sum / n;
      const add = Math.min(nx-1, x+r+1), rem = Math.max(0, x-r);
      sum += src[y*nx+add] - src[y*nx+rem];
    }
  }
  for(let x = 0; x < nx; x++){
    let sum = 0, n = 0;
    for(let y = -r; y <= r; y++){ const yi = Math.min(ny-1, Math.max(0, y));
      sum += tmp[yi*nx+x]; n++; }
    for(let y = 0; y < ny; y++){
      out[y*nx+x] = sum / n;
      const add = Math.min(ny-1, y+r+1), rem = Math.max(0, y-r);
      sum += tmp[add*nx+x] - tmp[rem*nx+x];
    }
  }
  return out;
}
const blur = (src, nx, ny, r) =>
  boxBlur(boxBlur(boxBlur(src, nx, ny, r), nx, ny, r), nx, ny, r);

function openSea(land, nx, ny, depth, blocked){
  const wall = i => land[i] || (blocked && blocked[i]);
  let seed = -1, deepest = -Infinity;
  for(let i = 0; i < land.length; i++)
    if(!wall(i) && depth[i] > deepest){ deepest = depth[i]; seed = i; }
  const keep = new Uint8Array(land.length);
  if(seed < 0) return keep;
  const stack = [seed]; keep[seed] = 1;
  while(stack.length){
    const i = stack.pop(), y = (i / nx) | 0, x = i % nx;
    for(const [dy, dx] of [[1,0],[-1,0],[0,1],[0,-1]]){
      const ny2 = y + dy, nx2 = x + dx;
      if(ny2 < 0 || ny2 >= ny || nx2 < 0 || nx2 >= nx) continue;
      const j = ny2 * nx + nx2;
      if(!wall(j) && !keep[j]){ keep[j] = 1; stack.push(j); }
    }
  }
  return keep;
}

export async function fetchSeabedContext(box){
  const key = `https://cache.local/overpass/${box.lat_min.toFixed(4)},`
            + `${box.lon_min.toFixed(4)},${box.lat_max.toFixed(4)},`
            + `${box.lon_max.toFixed(4)}`;
  let store = null;
  try{ store = await caches.open(OSM_CACHE); }catch(e){  }
  if(store){
    const hit = await store.match(key);
    if(hit) return JSON.parse(await hit.text());
  }
  const out = await fetchSeabedContextUncached(box);
  if(store){
    try{ await store.put(key, new Response(JSON.stringify(out))); }catch(e){}
  }
  return out;
}

async function fetchSeabedContextUncached(box){
  const b = `${box.lat_min},${box.lon_min},${box.lat_max},${box.lon_max}`;
  const q = `[out:json][timeout:90];(
    way["natural"="coastline"](${b});
    node["seamark:type"="wreck"](${b});
    way["seamark:type"="wreck"](${b});
    node["historic"="wreck"](${b});
    way["historic"="wreck"](${b});
    node["seamark:type"="obstruction"](${b});
  );out geom tags;`;

  const doc = await overpass(q);
  const coast = [], feats = [];
  for(const el of doc.elements || []){
    const tg = el.tags || {};
    if(tg.natural === "coastline"){
      if(el.geometry && el.geometry.length >= 2)
        coast.push(el.geometry.map(p => [p.lon, p.lat]));
      continue;
    }
    let lat = el.lat, lon = el.lon;
    if(lat == null && el.geometry && el.geometry.length){
      lat = el.geometry.reduce((a, p) => a + p.lat, 0) / el.geometry.length;
      lon = el.geometry.reduce((a, p) => a + p.lon, 0) / el.geometry.length;
    }
    if(lat == null || lon == null) continue;
    const depthRaw = tg["seamark:wreck:depth"] ?? tg.depth ?? null;
    const depth = depthRaw != null && isFinite(+depthRaw) ? +depthRaw : null;
    feats.push({
      type: "Feature",
      geometry: {type: "Point", coordinates: [+lon.toFixed(6), +lat.toFixed(6)]},
      properties: {
        name: tg["seamark:name"] || tg.name || "wreck",
        kind: tg["seamark:type"] || tg.historic || "wreck",
        category: tg["seamark:wreck:category"] || null,
        depth_m: depth, osm_id: el.id,
      },
    });
  }
  return {coast, wrecks: {type: "FeatureCollection", features: feats}};
}

function coastlineMask(nx, ny, lat0, lon0, cell, cellLon, lines){
  const blocked = new Uint8Array(nx * ny);
  const put = (x, y) => {
    if(x >= 0 && x < nx && y >= 0 && y < ny) blocked[y * nx + x] = 1;
  };
  for(const line of lines){
    for(let k = 1; k < line.length; k++){
      const ax = (line[k-1][0] - lon0) / cellLon, ay = (line[k-1][1] - lat0) / cell;
      const bx = (line[k][0]   - lon0) / cellLon, by = (line[k][1]   - lat0) / cell;
      const steps = Math.max(1, Math.ceil(2 * Math.max(Math.abs(bx-ax), Math.abs(by-ay))));
      for(let s = 0; s <= steps; s++){
        const t = s / steps;
        put(Math.round(ax + (bx-ax)*t), Math.round(ay + (by-ay)*t));
      }
    }
  }
  return blocked;
}

export function buildTerrain(grid, o = defaults, coast = null){
  const {nx, ny, elev, cell, lat0, lon0} = grid;
  const cellLon = grid.cellLon ?? cell;
  const latMid = lat0 + (ny / 2) * cell;
  const dyM = cell * 111320;
  const dxM = cellLon * 111320 * Math.cos(latMid * Math.PI / 180);

  const land = new Uint8Array(nx * ny), depth = new Float32Array(nx * ny);
  for(let i = 0; i < nx * ny; i++){
    const e = elev[i];
    land[i] = (!isFinite(e) || e >= 0) ? 1 : 0;
    depth[i] = land[i] ? NaN : -e;
  }

  let coastUsed = false;
  const bathyWater = land.reduce((a, v) => a + (v ? 0 : 1), 0);
  if(coast && coast.length){
    const blocked = coastlineMask(nx, ny, lat0, lon0, cell, cellLon, coast);
    const sea2 = openSea(land, nx, ny, depth, blocked);
    let kept = 0;
    for(let i = 0; i < nx * ny; i++) if(sea2[i]) kept++;
    if(kept > 0.2 * bathyWater){
      for(let i = 0; i < nx * ny; i++) if(!sea2[i]) land[i] = 1;
      coastUsed = true;
    }
  }
  if(!coastUsed){
    const sea = openSea(land, nx, ny, depth);
    for(let i = 0; i < nx * ny; i++) if(!sea[i]) land[i] = 1;
  }
  for(let i = 0; i < nx * ny; i++) if(land[i]) depth[i] = NaN;

  let acc = 0, n = 0;
  for(let i = 0; i < nx*ny; i++) if(!land[i]){ acc += depth[i]; n++; }
  const mean = n ? acc / n : 0;
  const filled = new Float32Array(nx * ny);
  for(let i = 0; i < nx * ny; i++) filled[i] = land[i] ? mean : depth[i];

  const cellM = cell * 111320;
  const bgR = Math.max(2, Math.round((o.background_m / cellM) / 1.4));
  const bg = blur(filled, nx, ny, bgR);
  const relief = new Float32Array(nx * ny), slope = new Float32Array(nx * ny);
  const sm = blur(filled, nx, ny, 1);
  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    const i = idx(grid, y, x);
    relief[i] = land[i] ? 0 : Math.min(1, Math.max(0, (bg[i] - filled[i]) / o.relief_scale_m));
    const gy = (sm[idx(grid, Math.min(ny-1,y+1), x)] - sm[idx(grid, Math.max(0,y-1), x)]) / (2*dyM);
    const gx = (sm[idx(grid, y, Math.min(nx-1,x+1))] - sm[idx(grid, y, Math.max(0,x-1))]) / (2*dxM);
    slope[i] = land[i] ? 0 : Math.min(1, Math.hypot(gy, gx) / o.slope_scale);
  }

  const dfit = new Float32Array(nx * ny);
  const [bLo, bHi] = o.depth_best;
  for(let i = 0; i < nx * ny; i++){
    if(land[i]){ dfit[i] = 0; continue; }
    const d = depth[i];
    dfit[i] = Math.max(0, Math.min(
      Math.min(1, (d - o.depth_min_m) / Math.max(bLo - o.depth_min_m, 1e-6)),
      Math.min(1, (o.depth_max_m - d) / Math.max(o.depth_max_m - bHi, 1e-6))));
  }
  return {...grid, cellLon, land, depth, relief, slope, dfit, dyM, dxM, latMid,
          coastUsed};
}

function speciesDepthFit(T, sp, o){
  const n = T.nx * T.ny;
  const out = new Float32Array(n);
  const lo = Math.max(sp.depth_m[0], o.depth_min_m);
  const hi = Math.min(sp.depth_m[1], o.depth_max_m);
  if(hi <= lo) return out;
  const blo = Math.min(Math.max(sp.depth_best_m[0], lo), hi);
  const bhi = Math.max(Math.min(sp.depth_best_m[1], hi), lo);
  for(let i = 0; i < n; i++){
    const d = T.depth[i];
    if(T.land[i] || !isFinite(d)) continue;
    out[i] = Math.max(0, Math.min(
      Math.min(1, (d - lo) / Math.max(blo - lo, 1e-9)),
      Math.min(1, (hi - d) / Math.max(hi - bhi, 1e-9))));
  }
  return out;
}

export function speciesScore(T, sp, shelter, wreck, o = defaults){
  const n = T.nx * T.ny;
  const r = sp.relief_affinity ?? 0.5, sl = sp.slope_affinity ?? 0.5;
  let denom = r + sl;
  const hasWreck = !!wreck;
  if(hasWreck) denom += 1;
  const ws = o.species_shelter_weight ?? 0.3;
  const dfit = speciesDepthFit(T, sp, o);
  const out = new Float32Array(n);
  for(let i = 0; i < n; i++){
    if(T.land[i]) continue;
    let parts = r * T.relief[i] + sl * T.slope[i];
    if(hasWreck) parts += wreck[i];
    const structure = parts / Math.max(denom, 1e-9);
    const v = (1 - ws) * structure + ws * shelter[i];
    out[i] = Math.max(0, Math.min(1, v * dfit[i]));
  }
  return out;
}

export function shelterFrom(T, fromDeg, o = defaults){
  const {nx, ny, land, cell} = T;
  const th = ((fromDeg % 360) + 360) % 360 * Math.PI / 180;
  const stepLat = Math.cos(th) * o.fetch_step_m / 111320 / cell;
  const stepLon = Math.sin(th) * o.fetch_step_m
                / (111320 * Math.cos(T.latMid*Math.PI/180)) / (T.cellLon ?? cell);
  const maxSteps = Math.round(o.fetch_max_km * 1000 / o.fetch_step_m);
  const out = new Float32Array(nx * ny);

  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    const i = y * nx + x;
    if(land[i]){ out[i] = 0; continue; }
    let fy = y, fx = x, hit = maxSteps;
    for(let s = 1; s <= maxSteps; s++){
      fy += stepLat; fx += stepLon;
      const yy = Math.round(fy), xx = Math.round(fx);
      if(yy < 0 || yy >= ny || xx < 0 || xx >= nx) break;
      if(land[yy * nx + xx]){ hit = s; break; }
    }
    out[i] = 1 - Math.min(1, hit / maxSteps);
  }
  return out;
}

export function wreckScore(T, wrecks, radiusM = 250){
  const {nx, ny, lat0, lon0, cell, dyM, dxM} = T;
  const cellLon = T.cellLon ?? cell;
  const field = new Float32Array(nx * ny);
  const pts = (wrecks && wrecks.features) || [];
  if(!pts.length) return field;

  for(const f of pts){
    const [lon, lat] = f.geometry.coordinates;
    const fy = (lat - lat0) / cell, fx = (lon - lon0) / cellLon;
    for(let y = 0; y < ny; y++){
      const dy = (y - fy) * dyM;
      if(Math.abs(dy) >= radiusM) continue;
      const rowBudget = Math.sqrt(radiusM*radiusM - dy*dy) / Math.abs(dxM || 1);
      const xLo = Math.max(0, Math.floor(fx - rowBudget));
      const xHi = Math.min(nx - 1, Math.ceil(fx + rowBudget));
      for(let x = xLo; x <= xHi; x++){
        const dx = (x - fx) * dxM;
        const d = Math.hypot(dy, dx);
        if(d >= radiusM) continue;
        const v = 1 - d / radiusM;
        const i = y * nx + x;
        if(v > field[i]) field[i] = v;
      }
    }
  }
  return field;
}

export function score(T, windFrom, swellFrom, o = defaults, wrecks = null){
  const n = T.nx * T.ny;
  const wS = shelterFrom(T, windFrom, o);
  const sS = (swellFrom == null) ? null : shelterFrom(T, swellFrom, o);
  const shelter = new Float32Array(n);
  for(let i = 0; i < n; i++)
    shelter[i] = sS ? (1 - o.swell_weight) * wS[i] + o.swell_weight * sS[i] : wS[i];

  const wreck = (wrecks && wrecks.features && wrecks.features.length)
    ? wreckScore(T, wrecks, o.wreck_radius_m ?? 250) : null;
  const wsum = o.w_relief + o.w_slope + o.w_shelter + (wreck ? o.w_wreck : 0);
  const out = new Float32Array(n);
  for(let i = 0; i < n; i++){
    if(T.land[i]){ out[i] = 0; continue; }
    let v = o.w_relief*T.relief[i] + o.w_slope*T.slope[i] + o.w_shelter*shelter[i];
    if(wreck) v += o.w_wreck * wreck[i];
    out[i] = (v / wsum) * T.dfit[i];
  }
  return {score: out, shelter, wreck};
}

export function findSpots(T, s, o = defaults){
  const {nx, ny, cell, lat0, lon0, depth} = T;
  const sep = Math.max(1, Math.round(o.min_separation_m / (cell * 111320)));

  const bufM = o.land_buffer_m ?? 0;
  const nearLand = bufM > 0 ? dilateLand(T.land, nx, ny,
      Math.round(bufM / Math.max(T.dyM || cell * 111320, 1)),
      Math.round(bufM / Math.max(Math.abs(T.dxM) || cell * 111320, 1))) : T.land;
  const dMin = o.depth_min_m ?? 0;

  const cands = [];
  for(let y = sep; y < ny - sep; y++) for(let x = sep; x < nx - sep; x++){
    const i = y * nx + x;
    if(s[i] < o.min_spot_score) continue;
    if(nearLand[i]) continue;
    if(!isFinite(depth[i]) || depth[i] < dMin) continue;
    let peak = true;
    for(let dy = -sep; dy <= sep && peak; dy++)
      for(let dx = -sep; dx <= sep; dx++)
        if(s[(y+dy)*nx + x+dx] > s[i]){ peak = false; break; }
    if(peak) cands.push({i, y, x, v: s[i]});
  }
  cands.sort((a, b) => b.v - a.v);
  const out = [];
  for(const c of cands){
    if(out.length >= o.top_spots) break;
    if(out.some(p => Math.hypot(p.y - c.y, p.x - c.x) < sep)) continue;
    out.push({...c,
      lat: +(lat0 + c.y * cell).toFixed(5),
      lon: +(lon0 + c.x * (T.cellLon ?? cell)).toFixed(5),
      score: +c.v.toFixed(3),
      depth_m: +(depth[c.i] || 0).toFixed(1),
      relief: +T.relief[c.i].toFixed(3), slope: +T.slope[c.i].toFixed(3)});
  }
  return out;
}

function dilateLand(land, nx, ny, ry, rx){
  ry = Math.max(1, ry | 0); rx = Math.max(1, rx | 0);
  const tmp = new Uint8Array(land.length), out = new Uint8Array(land.length);
  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    let hit = 0;
    const lo = Math.max(0, x - rx), hi = Math.min(nx - 1, x + rx);
    for(let k = lo; k <= hi && !hit; k++) hit = land[y * nx + k];
    tmp[y * nx + x] = hit;
  }
  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    let hit = 0;
    const lo = Math.max(0, y - ry), hi = Math.min(ny - 1, y + ry);
    for(let k = lo; k <= hi && !hit; k++) hit = tmp[k * nx + x];
    out[y * nx + x] = hit;
  }
  return out;
}

function mercatorY(latDeg){
  const lat = latDeg * Math.PI / 180;
  return Math.log(Math.tan(Math.PI / 4 + lat / 2));
}
function invMercatorY(y){
  return (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
}

export function toDataURL(T, s, alphaFloor = 0.35, alphaFull = 0.80, o = defaults){
  const {nx, ny, lat0, cell, land} = T;
  const cv = document.createElement("canvas");
  cv.width = nx; cv.height = ny;
  const ctx = cv.getContext("2d");
  const img = ctx.createImageData(nx, ny);
  const ramp = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
  const bufM = (o && o.land_buffer_m) ?? defaults.land_buffer_m;
  const landEdge = land && dilateLand(land, nx, ny,
    Math.round(bufM / Math.max(T.dyM || cell * 111320, 1)),
    Math.round(bufM / Math.max(Math.abs(T.dxM) || cell * 111320, 1)));

  const half = cell / 2;
  const latTop = lat0 + (ny - 1) * cell + half, latBot = lat0 - half;
  const yTop = mercatorY(latTop), yBot = mercatorY(latBot);
  for(let y = 0; y < ny; y++){
    const yMerc = yTop - ((y + 0.5) / ny) * (yTop - yBot);
    let rf = (invMercatorY(yMerc) - lat0) / cell;
    rf = Math.min(ny - 1, Math.max(0, rf));
    const r0 = Math.floor(rf), r1 = Math.min(ny - 1, r0 + 1), f = rf - r0;
    for(let x = 0; x < nx; x++){
      const o2 = (y * nx + x) * 4;
      if(landEdge && (landEdge[r0 * nx + x] || landEdge[r1 * nx + x])){
        img.data[o2+3] = 0; continue;
      }
      const v = (1 - f) * s[r0 * nx + x] + f * s[r1 * nx + x];
      if(v <= alphaFloor){ img.data[o2+3] = 0; continue; }
      const t = Math.min(1, (v - alphaFloor) / Math.max(alphaFull - alphaFloor, 1e-6));
      const p = t * (ramp.length - 1), k = Math.min(ramp.length - 2, p | 0), fr = p - k;
      img.data[o2]   = ramp[k][0] + (ramp[k+1][0] - ramp[k][0]) * fr;
      img.data[o2+1] = ramp[k][1] + (ramp[k+1][1] - ramp[k][1]) * fr;
      img.data[o2+2] = ramp[k][2] + (ramp[k+1][2] - ramp[k][2]) * fr;
      img.data[o2+3] = 217 * t;
    }
  }
  ctx.putImageData(img, 0, 0);
  return cv.toDataURL("image/png");
}

const MS_CASES = {
  1: [["L","B"]],           2: [["B","R"]],
  3: [["L","R"]],            4: [["R","T"]],
  5: [["L","B"],["R","T"]],  6: [["B","T"]],
  7: [["L","T"]],            8: [["T","L"]],
  9: [["B","T"]],           10: [["B","R"],["T","L"]],
  11:[["R","T"]],           12: [["L","R"]],
  13:[["B","R"]],           14: [["L","B"]],
};

export function contourLines(T, levels = [5, 10, 15, 20, 30]){
  const {nx, ny, land, depth, lat0, lon0, cell} = T;
  const cellLon = T.cellLon ?? cell;

  let acc = 0, n = 0;
  for(let i = 0; i < nx * ny; i++) if(!land[i]){ acc += depth[i]; n++; }
  const mean = n ? acc / n : 0;
  const filled = new Float32Array(nx * ny);
  for(let i = 0; i < nx * ny; i++) filled[i] = land[i] ? mean : depth[i];
  const sm = blur(filled, nx, ny, 1);

  const lonAt = x => lon0 + x * cellLon;
  const latAt = y => lat0 + y * cell;
  const t = (level, va, vb) => vb === va ? 0.5 : (level - va) / (vb - va);

  const feats = [];
  for(const level of levels){
    const lines = [];
    for(let y = 0; y < ny - 1; y++){
      for(let x = 0; x < nx - 1; x++){
        if(land[y*nx+x] || land[y*nx+x+1] || land[(y+1)*nx+x] || land[(y+1)*nx+x+1])
          continue;
        const v00 = sm[y*nx+x], v10 = sm[y*nx+x+1],
              v01 = sm[(y+1)*nx+x], v11 = sm[(y+1)*nx+x+1];
        const c = (v00>level?1:0) | (v10>level?2:0) | (v11>level?4:0) | (v01>level?8:0);
        const pairs = MS_CASES[c];
        if(!pairs) continue;

        const pt = edge => {
          if(edge === "B") return [lonAt(x + t(level,v00,v10)), latAt(y)];
          if(edge === "R") return [lonAt(x+1), latAt(y + t(level,v10,v11))];
          if(edge === "T") return [lonAt(x + t(level,v01,v11)), latAt(y+1)];
          return [lonAt(x), latAt(y + t(level,v00,v01))];
        };
        for(const [e0, e1] of pairs) lines.push([pt(e0), pt(e1)]);
      }
    }
    if(lines.length) feats.push({
      type: "Feature", properties: {depth_m: level},
      geometry: {type: "MultiLineString", coordinates: lines},
    });
  }
  return {type: "FeatureCollection", features: feats};
}

const OVERPASS_MIRRORS = [
  "https://overpass-api.de/api/interpreter",
  "https://overpass.kumi.systems/api/interpreter",
  "https://overpass.private.coffee/api/interpreter",
];

const OVERPASS_MIN_GAP_MS = 3000;
let _opChain = Promise.resolve(), _opLast = 0;

function overpassSlot(){
  const run = _opChain.then(async () => {
    const wait = OVERPASS_MIN_GAP_MS - (Date.now() - _opLast);
    if(wait > 0) await new Promise(r => setTimeout(r, wait));
    _opLast = Date.now();
  });
  _opChain = run.catch(() => {});
  return run;
}

async function overpass(query){
  await overpassSlot();
  let last = null;
  for(const url of OVERPASS_MIRRORS){
    try{
      const res = await fetch(url, {method: "POST",
                                    body: "data=" + encodeURIComponent(query)});
      if(res.status === 429 || res.status === 504 || res.status === 502){
        last = new Error(`Overpass ${new URL(url).hostname} returned HTTP ${res.status}`);
        continue;
      }
      if(!res.ok) throw new Error(`Overpass returned HTTP ${res.status}`);
      return await res.json();
    }catch(e){
      last = e;
    }
  }
  throw last || new Error("no Overpass mirror could be reached");
}

export function bounds(T){
  const halfLat = T.cell / 2, halfLon = (T.cellLon ?? T.cell) / 2;
  return [[T.lat0 - halfLat, T.lon0 - halfLon],
          [T.lat0 + (T.ny - 1) * T.cell + halfLat,
           T.lon0 + (T.nx - 1) * (T.cellLon ?? T.cell) + halfLon]];
}
