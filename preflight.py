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
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES = []
WARNINGS = []

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


def check(label, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + label + (f"   {detail}" if detail else ""))
    if not ok:
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


def check_selftest(ff_regions):
    docs = ROOT / "docs"
    outputs = [docs / "data", docs / "r", docs / "sitemap.xml", docs / "robots.txt"]
    present = [p.name for p in outputs if p.exists()]
    if present:
        print(f"NOTE  selftest skipped: {', '.join(present)} already exist under docs/ and would be overwritten")
        return
    region = ff_regions[0]["id"]
    needed = ["now", "days", "best_window", "weights", "verdict_limits", "series", "depth_range_m", "spots"]
    try:
        run = subprocess.run(
            [sys.executable, "fish_finder.py", "--selftest", "--select", region],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
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
        check_selftest(regions)
    print()
    if FAILURES:
        print(f"PRE-FLIGHT FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("pre-flight passed" + (f" with {len(WARNINGS)} warning(s)" if WARNINGS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
