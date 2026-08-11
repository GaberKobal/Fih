// structure.js — the spatial half of the model, running in the browser.
//
// This is what removes the need for configured regions. GMRT's GridServer can
// return a plain ArcInfo ASCII grid, which parses with a split() and no
// library at all, so the browser can pull bathymetry for a box around any
// point on Earth and score it directly.
//
// What is cached and what is not:
//   bathymetry   static. Cached in the Cache API, keyed by bbox — fetched
//                once per area, ever.
//   relief, slope  derived from bathymetry only, so also static. Computed
//                once per box and kept alongside it.
//   shelter      NOT static. It is upwind fetch, so it turns with the wind
//                and is recomputed every time the forecast changes. Cheap,
//                because the land mask is already in hand.
//
// The one real risk is CORS: if gmrt.org does not allow cross-origin reads
// the browser cannot fetch it and there is no way around that without a
// server. The error path says so plainly rather than failing silently.

const GMRT = "https://www.gmrt.org/services/GridServer";
const BATHY_CACHE = "bathy-v1";

export const defaults = {
  radius_km: 30,          // half-width of the box around the tapped point
  cell_m: 200,            // working grid; GMRT resolves ~100 m at best
  depth_min_m: 2, depth_best: [4, 18], depth_max_m: 30,
  relief_scale_m: 3.0, slope_scale: 0.04, background_m: 600,
  fetch_max_km: 20, fetch_step_m: 250, swell_weight: 0.5,
  w_relief: 0.35, w_slope: 0.20, w_shelter: 0.45,
  top_spots: 15, min_spot_score: 0.35, min_separation_m: 500,
};

/* ---------------------------------------------------------------- fetching */

export function boxAround(lat, lon, radiusKm){
  const dLat = radiusKm / 111.32;
  const dLon = radiusKm / (111.32 * Math.cos(lat * Math.PI / 180));
  return {lat_min: lat - dLat, lat_max: lat + dLat,
          lon_min: lon - dLon, lon_max: lon + dLon};
}

/** ArcInfo ASCII grid — six header lines then rows, north to south. */
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
  // Rows arrive north-first; flip so index 0 is the southern edge.
  const elev = new Float32Array(nx * ny);
  for(let y = 0; y < ny; y++)
    elev.set(vals.subarray((ny - 1 - y) * nx, (ny - y) * nx), y * nx);

  const x0 = hdr.xllcorner ?? hdr.xllcenter, y0 = hdr.yllcorner ?? hdr.yllcenter;
  return {nx, ny, elev, lon0: x0 + cs / 2, lat0: y0 + cs / 2, cell: cs};
}

export async function fetchBathymetry(box, opts = {}){
  const q = new URLSearchParams({
    north: box.lat_max.toFixed(5), south: box.lat_min.toFixed(5),
    west: box.lon_min.toFixed(5), east: box.lon_max.toFixed(5),
    layer: "topo", format: "arcascii",
    resolution: opts.resolution || "high",
  });
  const url = `${GMRT}?${q}`;

  // Static data: once fetched for an area, never fetch it again.
  let cache = null;
  try{ cache = await caches.open(BATHY_CACHE); }catch(e){ /* private mode */ }
  if(cache){
    const hit = await cache.match(url);
    if(hit) return parseAsciiGrid(await hit.text());
  }

  let res;
  try{
    res = await fetch(url);
  }catch(e){
    throw new Error("GMRT could not be reached from the browser. If this is a "
      + "CORS block there is no client-side fix — use a precomputed region.");
  }
  if(!res.ok) throw new Error(`GMRT returned HTTP ${res.status}`);
  const text = await res.text();
  if(/<html|<\?xml/i.test(text.slice(0, 200)))
    throw new Error("GMRT returned an error page rather than a grid");
  if(cache) cache.put(url, new Response(text));
  return parseAsciiGrid(text);
}

/* ------------------------------------------------------------- grid maths */

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
/** Three box blurs approximate a Gaussian closely and run in linear time. */
const blur = (src, nx, ny, r) =>
  boxBlur(boxBlur(boxBlur(src, nx, ny, r), nx, ny, r), nx, ny, r);

/** Keep only water reachable from the deepest cell — kills inland lakes,
 *  river valleys and marina basins without needing a full labelling pass. */
function openSea(land, nx, ny, depth){
  let seed = -1, deepest = -Infinity;
  for(let i = 0; i < land.length; i++)
    if(!land[i] && depth[i] > deepest){ deepest = depth[i]; seed = i; }
  const keep = new Uint8Array(land.length);
  if(seed < 0) return keep;
  const stack = [seed]; keep[seed] = 1;
  while(stack.length){
    const i = stack.pop(), y = (i / nx) | 0, x = i % nx;
    for(const [dy, dx] of [[1,0],[-1,0],[0,1],[0,-1]]){
      const ny2 = y + dy, nx2 = x + dx;
      if(ny2 < 0 || ny2 >= ny || nx2 < 0 || nx2 >= nx) continue;
      const j = ny2 * nx + nx2;
      if(!land[j] && !keep[j]){ keep[j] = 1; stack.push(j); }
    }
  }
  return keep;
}

/** Static half: everything derivable from bathymetry alone. */
export function buildTerrain(grid, o = defaults){
  const {nx, ny, elev, cell, lat0, lon0} = grid;
  const latMid = lat0 + (ny / 2) * cell;
  const dyM = cell * 111320, dxM = cell * 111320 * Math.cos(latMid * Math.PI / 180);

  const land = new Uint8Array(nx * ny), depth = new Float32Array(nx * ny);
  for(let i = 0; i < nx * ny; i++){
    const e = elev[i];
    land[i] = (!isFinite(e) || e >= 0) ? 1 : 0;
    depth[i] = land[i] ? NaN : -e;
  }
  const sea = openSea(land, nx, ny, depth);
  for(let i = 0; i < nx * ny; i++) if(!sea[i]) land[i] = 1;

  // Fill land with the local median-ish value so the coast does not read as a
  // cliff in the derivatives.
  let acc = 0, n = 0;
  for(let i = 0; i < nx*ny; i++) if(!land[i]){ acc += depth[i]; n++; }
  const mean = n ? acc / n : 0;
  const filled = new Float32Array(nx * ny);
  for(let i = 0; i < nx * ny; i++) filled[i] = land[i] ? mean : depth[i];

  // Three box blurs of radius r approximate a Gaussian of sigma ~= 1.4r, so
  // the radius has to be the target sigma divided by 1.4 - not by 3, which
  // made the background too tight to see anything above it.
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

  // Depth gate: a trapezoid over the band you actually hunt.
  const dfit = new Float32Array(nx * ny);
  const [bLo, bHi] = o.depth_best;
  for(let i = 0; i < nx * ny; i++){
    if(land[i]){ dfit[i] = 0; continue; }
    const d = depth[i];
    dfit[i] = Math.max(0, Math.min(
      Math.min(1, (d - o.depth_min_m) / Math.max(bLo - o.depth_min_m, 1e-6)),
      Math.min(1, (o.depth_max_m - d) / Math.max(o.depth_max_m - bHi, 1e-6))));
  }
  return {...grid, land, depth, relief, slope, dfit, dyM, dxM, latMid};
}

/** Dynamic half: fetch traced from wherever the wind and swell are today. */
export function shelterFrom(T, fromDeg, o = defaults){
  const {nx, ny, land, cell} = T;
  const th = ((fromDeg % 360) + 360) % 360 * Math.PI / 180;
  const stepLat = Math.cos(th) * o.fetch_step_m / 111320 / cell;
  const stepLon = Math.sin(th) * o.fetch_step_m / (111320 * Math.cos(T.latMid*Math.PI/180)) / cell;
  const maxSteps = Math.round(o.fetch_max_km * 1000 / o.fetch_step_m);
  const out = new Float32Array(nx * ny);

  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    const i = y * nx + x;
    if(land[i]){ out[i] = 0; continue; }
    let fy = y, fx = x, hit = maxSteps;
    for(let s = 1; s <= maxSteps; s++){
      fy += stepLat; fx += stepLon;
      const yy = Math.round(fy), xx = Math.round(fx);
      if(yy < 0 || yy >= ny || xx < 0 || xx >= nx) break;   // outside = open water
      if(land[yy * nx + xx]){ hit = s; break; }
    }
    out[i] = 1 - Math.min(1, hit / maxSteps);
  }
  return out;
}

export function score(T, windFrom, swellFrom, o = defaults){
  const n = T.nx * T.ny;
  const wS = shelterFrom(T, windFrom, o);
  const sS = (swellFrom == null) ? null : shelterFrom(T, swellFrom, o);
  const shelter = new Float32Array(n);
  for(let i = 0; i < n; i++)
    shelter[i] = sS ? (1 - o.swell_weight) * wS[i] + o.swell_weight * sS[i] : wS[i];

  const wsum = o.w_relief + o.w_slope + o.w_shelter;
  const out = new Float32Array(n);
  for(let i = 0; i < n; i++){
    if(T.land[i]){ out[i] = 0; continue; }
    out[i] = ((o.w_relief*T.relief[i] + o.w_slope*T.slope[i] + o.w_shelter*shelter[i])
              / wsum) * T.dfit[i];
  }
  return {score: out, shelter};
}

export function findSpots(T, s, o = defaults){
  const {nx, ny, cell, lat0, lon0, depth} = T;
  const sep = Math.max(1, Math.round(o.min_separation_m / (cell * 111320)));
  const cands = [];
  for(let y = sep; y < ny - sep; y++) for(let x = sep; x < nx - sep; x++){
    const i = y * nx + x;
    if(s[i] < o.min_spot_score) continue;
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
      lon: +(lon0 + c.x * cell).toFixed(5),
      score: +c.v.toFixed(3),
      depth_m: +(depth[c.i] || 0).toFixed(1),
      relief: +T.relief[c.i].toFixed(3), slope: +T.slope[c.i].toFixed(3)});
  }
  return out;
}

/** Paint the score field to a data URL for a Leaflet ImageOverlay. */
export function toDataURL(T, s, alphaFloor = 0.35, alphaFull = 0.80){
  const {nx, ny} = T;
  const cv = document.createElement("canvas");
  cv.width = nx; cv.height = ny;
  const ctx = cv.getContext("2d");
  const img = ctx.createImageData(nx, ny);
  // viridis, sampled
  const ramp = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    const v = s[(ny - 1 - y) * nx + x];         // canvas rows run north-first
    const o = (y * nx + x) * 4;
    if(v <= alphaFloor){ img.data[o+3] = 0; continue; }
    const t = Math.min(1, (v - alphaFloor) / Math.max(alphaFull - alphaFloor, 1e-6));
    const p = t * (ramp.length - 1), k = Math.min(ramp.length - 2, p | 0), f = p - k;
    img.data[o]   = ramp[k][0] + (ramp[k+1][0] - ramp[k][0]) * f;
    img.data[o+1] = ramp[k][1] + (ramp[k+1][1] - ramp[k][1]) * f;
    img.data[o+2] = ramp[k][2] + (ramp[k+1][2] - ramp[k][2]) * f;
    img.data[o+3] = 217 * t;
  }
  ctx.putImageData(img, 0, 0);
  return cv.toDataURL("image/png");
}

export function bounds(T){
  return [[T.lat0, T.lon0],
          [T.lat0 + (T.ny - 1) * T.cell, T.lon0 + (T.nx - 1) * T.cell]];
}
