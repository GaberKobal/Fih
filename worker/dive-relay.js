/**
 * Dive log relay - a Cloudflare Worker.
 *
 * WHY THIS EXISTS. Writing to a GitHub repository needs a token with write
 * access, and anything the browser holds is public: the page is downloaded by
 * every visitor and the token can be read straight out of it. A fine-grained
 * contents:write token also grants READ, so publishing one would hand a
 * stranger the whole private dive log. The token therefore has to live
 * somewhere the browser cannot see, and twenty lines of Worker is the
 * smallest thing that qualifies.
 *
 * The browser POSTs the dive as JSON. This builds the CSV row itself rather
 * than accepting one, so there is no untrusted CSV being parsed on the write
 * side and the column order can never drift from what calibrate.py expects.
 *
 * Nothing about a dive is ever public: no issue, no public file, no commit
 * message, no log line. Full precision goes straight into the private repo.
 *
 * DEPLOY
 *   npm create cloudflare@latest -- dive-relay     (or: npx wrangler init)
 *   copy this file over src/index.js, and wrangler.toml from beside it
 *   npx wrangler secret put GITHUB_TOKEN           fine-grained, ONE repo,
 *                                                  contents: read and write
 *   npx wrangler deploy
 * then put the deployed https://...workers.dev URL into config.json as
 * dive_relay_url and re-run the model so it reaches the app.
 */

const FILE = "dive_log.csv";

// The column order calibrate.py reads. The Worker owns it; the browser only
// sends names, so a reordered form cannot silently corrupt the file.
const COLUMNS = [
  "date", "lat", "lon", "depth_m", "viz_m", "model_viz_m",
  "wind_from_deg", "species", "result", "notes",
  // APPENDED, not inserted after "date" where it belongs semantically. A new
  // column in the middle would silently shift every value in every existing
  // row by one. On the end, an old 10-field row and a new 11-field row still
  // agree about what the first ten mean, and reconcileHeader below pads the
  // old ones.
  "time",
];

const LIMITS = {
  notes: 400,
  species: 60,
};

function num(v, lo, hi) {
  if (v === "" || v === null || v === undefined) return "";
  const n = Number(v);
  if (!Number.isFinite(n) || n < lo || n > hi) return null;   // null = reject
  return String(n);
}

/** Returns { row } or { error }. Never throws on bad input. */
function buildRow(body) {
  const d = String(body.date || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return { error: "date must be YYYY-MM-DD" };
  const year = Number(d.slice(0, 4));
  if (year < 2000 || year > 2100) return { error: "date out of range" };

  const lat = num(body.lat, -90, 90);
  const lon = num(body.lon, -180, 180);
  if (lat === null || lon === null) return { error: "lat/lon out of range" };
  if (lat === "" || lon === "") return { error: "lat/lon required" };

  const depth = num(body.depth_m, 0, 500);
  const viz = num(body.viz_m, 0, 100);
  const model = num(body.model_viz_m, 0, 100);
  const wind = num(body.wind_from_deg, 0, 360);
  if ([depth, viz, model, wind].includes(null)) return { error: "numeric field out of range" };

  // Optional, but the single most useful field in the file. HH:MM, 24h.
  let time = String(body.time ?? "").trim();
  if (time && !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)) {
    return { error: "time must be HH:MM, 24 hour" };
  }

  const result = String(body.result ?? "").trim();
  if (result !== "" && result !== "0" && result !== "1") return { error: "result must be 0 or 1" };

  // Free text is kept, but bounded and stripped of anything that could break
  // out of one CSV row. Quoting handles commas and quotes; newlines are cut.
  const clean = (s, max) =>
    String(s ?? "").replace(/[\r\n]+/g, " ").trim().slice(0, max);

  const values = {
    date: d, lat, lon,
    depth_m: depth, viz_m: viz, model_viz_m: model, wind_from_deg: wind,
    species: clean(body.species, LIMITS.species),
    result, time,
    notes: clean(body.notes, LIMITS.notes),
  };

  const csv = COLUMNS.map((c) => {
    const v = values[c] ?? "";
    return /[",]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  }).join(",");
  return { row: csv };
}

const b64encode = (s) => {
  const bytes = new TextEncoder().encode(s);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
};
const b64decode = (s) => {
  const bin = atob(s.replace(/\n/g, ""));
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
};

/**
 * Bring an existing file up to the current COLUMNS.
 *
 * Adding "time" means a file written by an older Worker has a 10-column
 * header while new rows carry 11. Appending regardless would leave the
 * header lying about the data. So: if the old header is a PREFIX of
 * COLUMNS - columns only ever added at the end - rewrite the header and
 * pad the old rows. Anything else is a real mismatch and is refused
 * rather than guessed at, because a wrong guess corrupts the only copy.
 */
function reconcileHeader(content) {
  const want = COLUMNS.join(",");
  const lines = content.split("\n");
  const have = (lines[0] || "").trim();
  if (have === want) return { content };
  if (!have) return { content: want + "\n" };

  const old = have.split(",");
  const isPrefix = old.length <= COLUMNS.length
    && old.every((c, i) => c === COLUMNS[i]);
  if (!isPrefix) {
    return { error: `header mismatch: file has "${have}"` };
  }
  const pad = ",".repeat(COLUMNS.length - old.length);
  lines[0] = want;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i].trim()) lines[i] += pad;
  }
  return { content: lines.join("\n") };
}

async function appendToRepo(env, row) {
  const url = `https://api.github.com/repos/${env.DIVELOG_REPO}/contents/${FILE}`;
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "dive-relay",
    "X-GitHub-Api-Version": "2022-11-28",
  };

  // Two writers at once both read the same sha and the second PUT is rejected
  // with 409. That is the API doing its job; just read again and retry.
  for (let attempt = 0; attempt < 4; attempt++) {
    const get = await fetch(url, { headers });
    let content = COLUMNS.join(",") + "\n";
    let sha;
    if (get.status === 200) {
      const meta = await get.json();
      sha = meta.sha;
      content = b64decode(meta.content);
      if (!content.endsWith("\n")) content += "\n";
      const fixed = reconcileHeader(content);
      if (fixed.error) return { ok: false, status: 500, detail: fixed.error };
      content = fixed.content;
    } else if (get.status !== 404) {
      return { ok: false, status: 502, detail: `read failed (${get.status})` };
    }

    const put = await fetch(url, {
      method: "PUT",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: "dive log entry",       // never the row: commit messages persist
        content: b64encode(content + row + "\n"),
        ...(sha ? { sha } : {}),
      }),
    });
    if (put.ok) return { ok: true };
    if (put.status !== 409) {
      return { ok: false, status: 502, detail: `write failed (${put.status})` };
    }
    await new Promise((r) => setTimeout(r, 150 * (attempt + 1)));
  }
  return { ok: false, status: 503, detail: "busy, try again" };
}

function corsHeaders(env, origin) {
  const allowed = (env.ALLOWED_ORIGIN || "").split(",").map((s) => s.trim()).filter(Boolean);
  const ok = allowed.length === 0 || allowed.includes(origin);
  return {
    headers: {
      "Access-Control-Allow-Origin": ok ? (origin || "*") : "null",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Max-Age": "86400",
    },
    ok,
  };
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const { headers: cors, ok: originOk } = corsHeaders(env, origin);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (request.method !== "POST") {
      return new Response("POST only", { status: 405, headers: cors });
    }
    // An Origin allowlist stops a browser on another site posting here. It is
    // not a security boundary - curl can send anything - which is why the
    // validation above is the real defence and the token is scoped to one
    // repository with no other permission.
    if (!originOk) return new Response("forbidden", { status: 403, headers: cors });

    if (!env.GITHUB_TOKEN || !env.DIVELOG_REPO) {
      return new Response("relay not configured", { status: 500, headers: cors });
    }

    let body;
    try {
      const text = await request.text();
      if (text.length > 4000) return new Response("too large", { status: 413, headers: cors });
      body = JSON.parse(text);
    } catch {
      return new Response("bad JSON", { status: 400, headers: cors });
    }

    const built = buildRow(body || {});
    if (built.error) {
      return new Response(built.error, { status: 400, headers: cors });
    }

    const res = await appendToRepo(env, built.row);
    if (!res.ok) {
      return new Response(res.detail, { status: res.status, headers: cors });
    }
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { ...cors, "Content-Type": "application/json" },
    });
  },
};
