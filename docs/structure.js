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
  w_relief: 0.35, w_slope: 0.20, w_shelter: 0.45, w_wreck: 0.3,
  wreck_radius_m: 250,
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

  // ArcInfo may give its lower-left value as a cell corner or centre.  The
  // score array is cell-centred, so preserve that distinction before turning
  // a projected grid back into WGS84.  Treating a corner as a centre shifts
  // an otherwise correct 200 m grid half a cell onto the shore.
  const xIsCorner = hdr.xllcorner != null, yIsCorner = hdr.yllcorner != null;
  const x0 = (xIsCorner ? hdr.xllcorner : hdr.xllcenter) + (xIsCorner ? cs / 2 : 0);
  const y0 = (yIsCorner ? hdr.yllcorner : hdr.yllcenter) + (yIsCorner ? cs / 2 : 0);

  // GMRT defaults to Mercator, so the grid can arrive in projected metres
  // rather than degrees. Detect that from the magnitude of the corner and
  // resample onto a uniform lat/lon grid, which is what everything downstream
  // assumes.
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
  if(!res.ok) throw new Error(
    `GMRT returned HTTP ${res.status}. 404 means the request parameters are `
    + `wrong; 413 means the box is too big — try a smaller radius.`);
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
  return {...grid, cellLon, land, depth, relief, slope, dfit, dyM, dxM, latMid};
}

/** Dynamic half: fetch traced from wherever the wind and swell are today. */
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
      if(yy < 0 || yy >= ny || xx < 0 || xx >= nx) break;   // outside = open water
      if(land[yy * nx + xx]){ hit = s; break; }
    }
    out[i] = 1 - Math.min(1, hit / maxSteps);
  }
  return out;
}

/** A soft halo around each charted wreck, tapering linearly to 0 at
 *  radiusM — the same shape as wreck_score() in fish_finder.py, just
 *  computed in true metres via T.dyM/T.dxM instead of over a Mercator
 *  raster. Point data, so it needs none of the raster's corrections. */
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
      if(Math.abs(dy) >= radiusM) continue;         // quick reject by row
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
      lon: +(lon0 + c.x * (T.cellLon ?? cell)).toFixed(5),
      score: +c.v.toFixed(3),
      depth_m: +(depth[c.i] || 0).toFixed(1),
      relief: +T.relief[c.i].toFixed(3), slope: +T.slope[c.i].toFixed(3)});
  }
  return out;
}

/** One-cell dilation of the land mask, for painting only. Global bathymetry
 *  occasionally disagrees with the vector coastline right at the coast —
 *  river mouths, breakwaters, reclaimed land, park lagoons — by a cell or
 *  two, which can otherwise show the colour wash sitting visibly on dry
 *  land. This pulls the paint back from any detected shoreline as a
 *  cosmetic margin; it does not touch T.land itself, so findSpots() can
 *  still place a real spot right up to the true coastline. */
function dilateLand(land, nx, ny){
  const out = new Uint8Array(land.length);
  for(let y = 0; y < ny; y++) for(let x = 0; x < nx; x++){
    const i = y * nx + x;
    if(land[i]){ out[i] = 1; continue; }
    let hit = false;
    for(let dy = -1; dy <= 1 && !hit; dy++) for(let dx = -1; dx <= 1; dx++){
      const yy = y + dy, xx = x + dx;
      if(yy < 0 || yy >= ny || xx < 0 || xx >= nx) continue;
      if(land[yy * nx + xx]){ hit = true; break; }
    }
    out[i] = hit ? 1 : 0;
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

/** Paint the score field to a data URL for a Leaflet ImageOverlay. */
export function toDataURL(T, s, alphaFloor = 0.35, alphaFull = 0.80){
  const {nx, ny, lat0, cell, land} = T;
  const cv = document.createElement("canvas");
  cv.width = nx; cv.height = ny;
  const ctx = cv.getContext("2d");
  const img = ctx.createImageData(nx, ny);
  // viridis, sampled
  const ramp = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
  const landEdge = land && dilateLand(land, nx, ny);

  // Leaflet stretches this canvas linearly in Mercator y across the plain
  // lat/lon corners bounds() gives it, but the source grid s[]/land[] is
  // evenly spaced in plain latitude. The two only line up at the very top
  // and bottom row; every row in between lands tens of metres — over a
  // hundred at the 50 km radius option — north or south of where it should
  // be. Resample each output row from its true Mercator-y position instead,
  // the same correction write_overlay_png makes on the Python side, and
  // pick the land mask with NEAREST rather than blending it, so this fix
  // does not reintroduce its own smear across the coastline.
  const latTop = lat0 + (ny - 1) * cell, latBot = lat0;
  const yTop = mercatorY(latTop), yBot = mercatorY(latBot);
  for(let y = 0; y < ny; y++){
    const yMerc = yTop - (y / Math.max(ny - 1, 1)) * (yTop - yBot);
    let rf = (invMercatorY(yMerc) - lat0) / cell;      // fractional source row, 0 = south
    rf = Math.min(ny - 1, Math.max(0, rf));
    const r0 = Math.floor(rf), r1 = Math.min(ny - 1, r0 + 1), f = rf - r0;
    const rNear = f < 0.5 ? r0 : r1;
    for(let x = 0; x < nx; x++){
      const o = (y * nx + x) * 4;
      if(landEdge && landEdge[rNear * nx + x]){ img.data[o+3] = 0; continue; }
      const v = (1 - f) * s[r0 * nx + x] + f * s[r1 * nx + x];
      if(v <= alphaFloor){ img.data[o+3] = 0; continue; }
      const t = Math.min(1, (v - alphaFloor) / Math.max(alphaFull - alphaFloor, 1e-6));
      const p = t * (ramp.length - 1), k = Math.min(ramp.length - 2, p | 0), fr = p - k;
      img.data[o]   = ramp[k][0] + (ramp[k+1][0] - ramp[k][0]) * fr;
      img.data[o+1] = ramp[k][1] + (ramp[k+1][1] - ramp[k][1]) * fr;
      img.data[o+2] = ramp[k][2] + (ramp[k+1][2] - ramp[k][2]) * fr;
      img.data[o+3] = 217 * t;
    }
  }
  ctx.putImageData(img, 0, 0);
  return cv.toDataURL("image/png");
}

// Case -> pair(s) of edges crossed, for 4-corner marching squares. Corners
// are bl=bit0, br=bit1, tr=bit2, tl=bit3, each set when depth > level.
// Cases 5 and 10 are the ambiguous saddles and resolve to two segments.
const MS_CASES = {
  1: [["L","B"]],           2: [["B","R"]],
  3: [["L","R"]],            4: [["R","T"]],
  5: [["L","B"],["R","T"]],  6: [["B","T"]],
  7: [["L","T"]],            8: [["T","L"]],
  9: [["B","T"]],           10: [["B","R"],["T","L"]],
  11:[["R","T"]],           12: [["L","R"]],
  13:[["B","R"]],           14: [["L","B"]],
};

/** Depth contour lines via marching squares over the depth grid — a vector
 *  result, so unlike toDataURL() there is no Mercator restretch to correct
 *  for; each vertex is a true lat/lon and Leaflet projects it directly.
 *  Land reads as a sentinel well below the shallowest level asked for, so no
 *  line is ever drawn running onto the shore, matching depth_contours() in
 *  fish_finder.py. */
export function contourLines(T, levels = [5, 10, 15, 20, 30]){
  const {nx, ny, land, depth, lat0, lon0, cell} = T;
  const cellLon = T.cellLon ?? cell;

  // Fill land with the local water mean before smoothing — the same
  // technique buildTerrain() uses — so the coast does not read as a cliff
  // and blur out a spurious ring of crossings right at the shoreline. Cells
  // that actually touch land are still excluded below using the true mask,
  // not this filled proxy, so a line can never end up drawn onto the shore.
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
          return [lonAt(x), latAt(y + t(level,v00,v01))];            // "L"
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

const OVERPASS = "https://overpass-api.de/api/interpreter";

/** Charted wrecks from OpenStreetMap via Overpass — same tags and shape as
 *  fetch_wrecks() server-side, run directly from the browser since Overpass
 *  is CORS-enabled. Nothing is cached: wrecks do not move, but a point scan
 *  has no per-region data folder to keep a copy in. */
export async function fetchWrecks(box){
  const b = `${box.lat_min},${box.lon_min},${box.lat_max},${box.lon_max}`;
  const q = `[out:json][timeout:25];(
    node["seamark:type"="wreck"](${b});
    way["seamark:type"="wreck"](${b});
    node["historic"="wreck"](${b});
    way["historic"="wreck"](${b});
    node["seamark:type"="obstruction"](${b});
  );out center tags;`;

  let res;
  try{
    res = await fetch(OVERPASS, {method: "POST", body: "data=" + encodeURIComponent(q)});
  }catch(e){
    throw new Error("Overpass could not be reached from the browser");
  }
  if(!res.ok) throw new Error(`Overpass returned HTTP ${res.status}`);
  const doc = await res.json();

  const feats = [];
  for(const el of doc.elements || []){
    const lat = el.lat ?? el.center?.lat, lon = el.lon ?? el.center?.lon;
    if(lat == null || lon == null) continue;
    const tg = el.tags || {};
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
  return {type: "FeatureCollection", features: feats};
}

export function bounds(T){
  // T.lat0/T.lon0 are the CENTRE of the first (southernmost/westernmost)
  // cell, not the edge of the scanned box. Leaflet's ImageOverlay stretches
  // the picture so its outer pixel edges land exactly on the bounds it is
  // given — so handing it cell centres pulls both edges inward by half a
  // cell, which is small near the middle of the box but, right where it
  // matters, means the outermost row/column of colour (often the strip
  // hugging the coast) is drawn up to half a cell into the wrong side of
  // the shoreline. Extend to the true outer edges instead, matching
  // raster_bounds() in fish_finder.py.
  const halfLat = T.cell / 2, halfLon = (T.cellLon ?? T.cell) / 2;
  return [[T.lat0 - halfLat, T.lon0 - halfLon],
          [T.lat0 + (T.ny - 1) * T.cell + halfLat,
           T.lon0 + (T.nx - 1) * (T.cellLon ?? T.cell) + halfLon]];
}
