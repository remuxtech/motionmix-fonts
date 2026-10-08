#!/usr/bin/env python3
"""
Font INDEX builder (ADR 125 Am. 2) — the data the on-device pair scorer
reads. Schema: INDEX.md (the contract). One record per catalog family, one
face record per shipped style, joined to the manifest by slug + style.

    facts      category · classifications · stroke · skeleton · designers ·
               licence · rfn · popularity / trending ranks · date added ·
               subsets · primary script · upstream axes · GF quality
    tags       Google's human-assessed tags (tags/all/families.csv): the 20
               Expressive moods PER FACE (interpolated along wght at the
               face's weight) + family-level classification / theme /
               seasonal / purpose / quality tags
    metrics    measured from outlines (tools/measure.py)
    fit        display / support / caption suitability, 0–1, rule-based
               (FIT_VERSION below; tune against a review board, then usage)
    pairs      the hand-checked seed pairs (pairing/seed_pairs.json, ADR 278
               §8), verbatim, after checking every slug + style is in the index

A variable style (ADR 125 Am. 3) is measured at its named instance's
coordinates, in memory (tools/measure.py) — never written out instanced.

Metrics are cached by face (file sha256, `@coordinates` for a variable
instance) in <build>/metrics-cache.json, seeded from the published index
(git HEAD r2/index.json + r2/manifest.json, same measureVersion) unless
--no-reuse: a re-run only needs the files of NEW faces in <build>/files/.

Writes r2/index.json (compact) + <build>/index/<sha256>.json and records
{file, sha256, bytes} as `index` in r2/manifest.json.

Usage:
    tools/build_index.py --gf-repo <checkout> --site-metadata <json> --build <dir> [--no-reuse]
"""

import argparse
import concurrent.futures as futures
import datetime
import hashlib
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gfmeta  # noqa: E402
import measure  # noqa: E402
import varfont  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MEASURE_VERSION = 1          # bump when tools/measure.py changes what it reports
FIT_VERSION = "rules-1"

MOODS = ["Active", "Artistic", "Awkward", "Business", "Calm", "Childlike", "Competent", "Cute",
         "Excited", "Fancy", "Futuristic", "Happy", "Innovative", "Loud", "Playful", "Rugged",
         "Sincere", "Sophisticated", "Stiff", "Vintage"]
EXPRESSIVE = {"Active", "Artistic", "Childlike", "Cute", "Excited", "Fancy", "Futuristic", "Happy",
              "Innovative", "Loud", "Playful", "Rugged", "Sophisticated", "Vintage"}
NEUTRAL = {"Business", "Calm", "Competent", "Sincere"}
SKELETON_GROUPS = ("/Sans/", "/Serif/", "/Slab/", "/Script/", "/Monospace/")


# ── tags ────────────────────────────────────────────────────────────────────
def _spec_axes(spec):
    """'wght@400' → {'wght': 400.0}; 'wdth,wght@100,900' → {...}; '' → {}."""
    if not spec:
        return {}
    tags, _, vals = spec.partition("@")
    try:
        return dict(zip(tags.split(","), (float(v) for v in vals.split(","))))
    except ValueError:
        return None


def face_moods(family_tags, weight):
    """20 Expressive scores (0–100, absent = 0) at `weight`: piecewise-linear
    along the tagged wght points (width-axis rows at the width closest to
    100), else the family-level score."""
    points = {}   # mood → {wght: score}
    flat = {}
    widths = {a.get("wdth") for a in (_spec_axes(s) for s in family_tags) if a and "wdth" in a}
    best_w = min(widths, key=lambda w: abs(w - 100)) if widths else None
    for spec, tags in family_tags.items():
        axes = _spec_axes(spec)
        if axes is None:
            continue
        for tag, score in tags.items():
            if not tag.startswith("/Expressive/"):
                continue
            mood = tag.rsplit("/", 1)[1]
            if not axes:
                flat[mood] = score
            elif "wght" in axes and axes.get("wdth", best_w) == best_w:
                points.setdefault(mood, {})[axes["wght"]] = score
    out = []
    for mood in MOODS:
        pts = points.get(mood)
        if pts:
            xs = sorted(pts)
            if weight <= xs[0]:
                v = pts[xs[0]]
            elif weight >= xs[-1]:
                v = pts[xs[-1]]
            else:
                hi = next(x for x in xs if x >= weight)
                lo = max(x for x in xs if x <= weight)
                v = pts[lo] if hi == lo else pts[lo] + (pts[hi] - pts[lo]) * (weight - lo) / (hi - lo)
        else:
            v = flat.get(mood, 0.0)
        out.append(int(round(v)))
    return out


def family_level_tags(family_tags):
    return {t: s for t, s in family_tags.get("", {}).items() if not t.startswith("/Expressive/")}


def skeleton_of(level_tags):
    best = None
    for t, s in level_tags.items():
        if t.startswith(SKELETON_GROUPS) and (best is None or s > best[1]):
            best = (t, s)
    return best[0].strip("/") if best else None


# ── fit (rules-1) ───────────────────────────────────────────────────────────
def band(x, lo, hi, soft):
    """1 inside [lo, hi], falling linearly to 0 at lo−soft / hi+soft."""
    if x is None:
        return 0.5
    if lo <= x <= hi:
        return 1.0
    d = lo - x if x < lo else x - hi
    return max(0.0, 1.0 - d / soft)


def ramp(x, table):
    """Piecewise-linear lookup over sorted (x, y) pairs."""
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


def fit_scores(fam, face, m, moods, breadth):
    cat = fam["category"]
    mood = dict(zip(MOODS, moods))
    q = (fam.get("quality") or 70) / 100
    w, italic = face["weight"], face["italic"]
    caseless = m.get("caseless")
    contrast = m.get("contrast")
    level = fam.get("_level", {})
    theme = any(t.startswith("/Theme/") and s >= 50 for t, s in level.items())
    skel = fam.get("skeleton") or ""

    # display — titles / hooks: expressive, distinctive, has presence
    top3 = sorted((mood[k] for k in EXPRESSIVE), reverse=True)[:3]
    expressive = sum(top3) / 300
    prior = {"display": 1.0, "handwriting": 0.85, "serif": 0.6, "sans-serif": 0.45, "monospace": 0.35}.get(cat, 0.5)
    if theme or skel.startswith(("Serif/Didone", "Serif/Fat Face", "Script/Formal")):
        prior = max(prior, 0.9)
    if caseless:                                  # all caps / small caps: a title face
        prior = max(prior, 0.85)
    wdc = m.get("wdc")
    if wdc is not None and (wdc < 0.62 or wdc > 1.0):   # condensed / extended
        prior = max(prior, 0.7)
    weight_presence = ramp(m.get("stemc") or 0.12, [(0.04, 0.25), (0.12, 0.55), (0.2, 0.9), (0.26, 1.0)])
    contrast_presence = 1 - (contrast if contrast is not None else 0.8)
    presence = max(weight_presence, contrast_presence)
    display = 0.4 * expressive + 0.3 * prior + 0.2 * presence + 0.1 * q
    if italic:
        display *= 0.95

    # support — subheads, body lines: legible, calm, a real text family
    weight_fit = ramp(w, [(100, 0.2), (200, 0.3), (300, 0.65), (400, 1.0), (500, 0.95), (600, 0.75),
                          (700, 0.55), (800, 0.35), (900, 0.25)])
    neutral = sum(mood[k] for k in NEUTRAL) / 400
    support = (0.25 * band(m.get("xr"), 0.66, 0.80, 0.12)
               + 0.2 * band(contrast, 0.4, 1.0, 0.25)
               + 0.25 * weight_fit
               + 0.1 * {1: 0.4, 2: 0.7}.get(breadth, 1.0)
               + 0.1 * neutral
               + 0.1 * q)
    # proportion gates: condensed/extended or very black/very thin designs
    support *= 0.6 + 0.4 * band(m.get("wdc"), 0.65, 0.95, 0.2)
    support *= 0.6 + 0.4 * band(m.get("stemc"), 0.08, 0.2, 0.08)
    if contrast is not None and contrast < 0.3:   # Didone / fat-face hairlines
        support *= 0.8
    support *= {"sans-serif": 1.0, "serif": 0.95, "monospace": 0.75, "display": 0.45, "handwriting": 0.3}.get(cat, 0.5)
    if caseless:
        support *= 0.3
    if italic:
        support *= 0.7

    # caption — small text over video: big x-height, low contrast, sturdy
    cap_weight = ramp(w, [(100, 0.15), (200, 0.25), (300, 0.5), (400, 0.9), (500, 1.0), (600, 0.9),
                          (700, 0.75), (800, 0.5), (900, 0.35)])
    caption = (0.35 * band(m.get("xr"), 0.70, 0.82, 0.1)
               + 0.3 * band(contrast, 0.6, 1.0, 0.3)
               + 0.2 * cap_weight
               + 0.15 * q)
    caption *= 0.5 + 0.5 * band(m.get("wdc"), 0.72, 1.0, 0.2)     # open, not condensed
    caption *= 0.5 + 0.5 * band(m.get("stemc"), 0.10, 0.21, 0.07)  # sturdy (Bold ok), not black
    if "/Purpose/Easy Reading" in level:
        caption = min(1.0, caption + 0.1)
    caption *= {"sans-serif": 1.0, "serif": 0.85, "monospace": 0.8, "display": 0.5, "handwriting": 0.25}.get(cat, 0.5)
    if caseless:
        caption *= 0.2
    if italic:
        caption *= 0.6
    return {"display": round(display, 3), "support": round(support, 3), "caption": round(caption, 3)}


# ── measuring ───────────────────────────────────────────────────────────────
def ref_style(fam):
    """The Regular-most style: script coverage is measured on it."""
    return min(fam["styles"], key=lambda st: (st["italic"], abs(st["weight"] - 400)))


def _measure_job(args):
    path, subsets, coords = args
    try:
        return measure.measure(path, subsets, coords)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def _git_show(rel):
    shown = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True, text=True)
    return json.loads(shown.stdout) if shown.returncode == 0 else None


def published_metrics():
    """{face key: metrics} recovered from the published (git HEAD) index + manifest:
    a face's `m`, plus the family `cov` on its reference face. Empty when the two
    disagree on catalogVersion or the index used another measureVersion."""
    index, manifest = _git_show("r2/index.json"), _git_show("r2/manifest.json")
    if not index or not manifest or index.get("catalogVersion") != manifest.get("catalogVersion") \
            or index.get("measureVersion") != MEASURE_VERSION:
        print("reuse: no matching published index at git HEAD — measuring every face")
        return {}
    faces = {(f["slug"], face["style"]): (f, face) for f in index["families"] for face in f["faces"]}
    out = {}
    for fam in manifest["families"]:
        ref = ref_style(fam)
        for st in fam["styles"]:
            hit = faces.get((fam["slug"], st["name"]))
            if not hit or not hit[1].get("m"):
                continue
            m = dict(hit[1]["m"])
            if st is ref:
                m["cov"] = hit[0].get("cov", {})
            out[varfont.face_key(st)] = m
    print(f"reuse: {len(out)} faces' metrics from the published index ({index.get('catalogVersion')})")
    return out


def measure_all(build, manifest, jobs, reuse):
    cache_path = build / "metrics-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if cache.get("_version") != MEASURE_VERSION:
        cache = {"_version": MEASURE_VERSION}
    if reuse:
        for key, m in published_metrics().items():
            cache.setdefault(key, m)
    todo = {}
    for fam in manifest["families"]:
        ref = ref_style(fam)
        for s in fam["styles"]:
            key = varfont.face_key(s)
            entry = cache.get(key)
            if entry is None or (s is ref and "cov" not in entry and "error" not in entry):
                file, _, _, coords = varfont.style_source(s)
                todo[key] = (str(build / file), tuple(fam["subsets"]), coords)
    missing = sorted({args[0] for args in todo.values() if not pathlib.Path(args[0]).exists()})
    if missing:
        raise SystemExit(f"{len(missing)} font files to measure are not in the build dir, e.g. {missing[:3]}")
    print(f"measuring {len(todo)} faces ({len(cache) - 1} cached)")
    with futures.ProcessPoolExecutor(max_workers=jobs) as pool:
        for sha, result in zip(todo, pool.map(_measure_job, todo.values(), chunksize=8)):
            cache[sha] = result
    cache_path.write_text(json.dumps(cache, separators=(",", ":")))
    return cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gf-repo", required=True)
    ap.add_argument("--site-metadata", required=True)
    ap.add_argument("--build", required=True)
    ap.add_argument("--manifest", default=str(ROOT / "r2/manifest.json"))
    ap.add_argument("--out", default=str(ROOT / "r2/index.json"))
    ap.add_argument("--seeds", default=str(ROOT / "pairing/seed_pairs.json"),
                    help="hand-checked pairs folded in as `pairs` ('' = none)")
    ap.add_argument("--no-reuse", action="store_true", help="measure every face (ignore the published index)")
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args()

    build = pathlib.Path(args.build)
    manifest_path = pathlib.Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    repo = gfmeta.GoogleFontsRepo(args.gf_repo).families()
    site = gfmeta.load_site_metadata(args.site_metadata)
    tags = gfmeta.load_tags(pathlib.Path(args.gf_repo) / "tags/all/families.csv")
    metrics = measure_all(build, manifest, args.jobs, reuse=not args.no_reuse)

    tag_names = sorted({t for fam in manifest["families"]
                        for t in family_level_tags(tags.get(fam["name"], {}))})
    tag_ix = {t: i for i, t in enumerate(tag_names)}

    families, errors = [], []
    for fam in manifest["families"]:
        name = fam["name"]
        s = site.get(name, {})
        rec = repo.get(name)
        ftags = tags.get(name, {})
        level = family_level_tags(ftags)
        quality = gfmeta.quality_score(ftags)
        axes = []
        if rec:
            for a in rec["meta"].get("axes", []):
                axes.append([gfmeta.first(a, "tag"), gfmeta.first(a, "min_value"), gfmeta.first(a, "max_value")])
        uprights = sorted({st["weight"] for st in fam["styles"] if not st["italic"]})
        out = {
            "slug": fam["slug"],
            "name": name,
            "category": fam["category"],
            "classifications": sorted(c.lower() for c in s.get("classifications", [])),
            "stroke": (s.get("stroke") or "").lower().replace(" ", "-") or None,
            "skeleton": skeleton_of(level),
            "designers": s.get("designers") or ([fam["designer"]] if fam.get("designer") else []),
            "license": fam["license"],
            "rfn": fam["rfn"],
            "popularity": s.get("popularity"),
            "trending": s.get("trending"),
            "dateAdded": s.get("dateAdded"),
            "subsets": fam["subsets"],
            "primaryScript": s.get("primaryScript") or None,
            "axes": axes,
            "weights": uprights,
            "quality": round(quality, 2) if quality is not None else None,
            "tags": [[tag_ix[t], int(round(v))] for t, v in sorted(level.items())],
            "faces": [],
        }
        if fam.get("minEngine", 1) > 1:
            out["minEngine"] = fam["minEngine"]     # ADR 125 Am. 3: variable-instance faces
        scoring = dict(out, _level=level)
        for st in fam["styles"]:
            m = metrics.get(varfont.face_key(st), {})
            if "error" in m:
                errors.append((name, st["name"], m["error"]))
                m = {}
            moods = face_moods(ftags, st["weight"])
            face = {"style": st["name"], "weight": st["weight"], "italic": st["italic"],
                    "bytes": varfont.style_source(st)[2], "moods": moods,
                    "m": {k: v for k, v in m.items() if k != "cov"}}
            face["fit"] = fit_scores(scoring, face, m, moods, len(uprights))
            out["faces"].append(face)
        # script coverage is a family property: measured on the Regular-most face
        out["cov"] = metrics.get(varfont.face_key(ref_style(fam)), {}).get("cov", {})
        families.append(out)

    pairs = []
    if args.seeds:
        seeds = json.loads(pathlib.Path(args.seeds).read_text())
        faces = {(f["slug"], face["style"]) for f in families for face in f["faces"]}
        bad = [(side[k]["slug"], side[k]["style"]) for side in seeds["pairs"] for k in ("display", "support")
               if (side[k]["slug"], side[k]["style"]) not in faces]
        if bad:
            raise SystemExit(f"seed pairs name faces the catalog does not have: {bad}")
        pairs = seeds["pairs"]

    index = {
        "schemaVersion": 1,
        "catalogVersion": manifest.get("catalogVersion"),
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fitVersion": FIT_VERSION,
        "measureVersion": MEASURE_VERSION,
        "moods": MOODS,
        "tagNames": tag_names,
        "pairs": pairs,
        "families": families,
    }
    text = json.dumps(index, separators=(",", ":"), ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    pathlib.Path(args.out).write_bytes(data)
    (build / "index").mkdir(parents=True, exist_ok=True)
    (build / "index" / f"{digest}.json").write_bytes(data)
    manifest["index"] = {"file": f"index/{digest}.json", "sha256": digest, "bytes": len(data)}
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"index: {len(families)} families, {sum(len(f['faces']) for f in families)} faces, {len(pairs)} pairs, "
          f"{len(data) / 1e3:.0f} KB → {args.out} (index/{digest[:12]}….json)")
    for e in errors[:20]:
        print("  ! measure failed:", *e)


if __name__ == "__main__":
    main()
