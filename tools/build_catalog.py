#!/usr/bin/env python3
"""
motionmix-fonts R2 catalog builder (ADR 125 Am. 2, hosting rung R2).

Builds the catalog served from https://library.motionmix.app/fonts/ as a
content-addressed tree plus a manifest the shipped app can already read
(schemaVersion 1 shape; every new field is additive):

    <build>/files/<sha256>.ttf       font files            (immutable)
    <build>/previews/<sha256>.ttf    name-subset previews  (immutable)
    <build>/licenses/<sha256>.txt    licence texts         (immutable)
    r2/manifest.json                 the catalog manifest  (short cache)
    r2/excluded.json                 every refused family + why

Families:
  * FROZEN — the jsDelivr catalog (manifest.json at the repo root, ~150
    families) keeps its exact bytes and sha256s: templates pin them
    (ADR 125 Am. 1 heal-by-sha256).
  * NEW (unless --frozen-only) — Google Fonts families passing the gates
    (ADR 125 Am. 2): licence OFL / Apache-2.0 / UFL with its text present;
    no Symbols / Special-use (barcode, redaction) / Learn-to-Write / colour
    fonts; Noto only as already shipped; GF Quality-tag mean >= the p25
    cut-off. File origin, in order of preference:
      - "gf-repo": the static TTFs in github.com/google/fonts, byte-identical
        (unmodified upstream) — every family that has them;
      - "gf-api": Google's own static instances from the Fonts API (css2),
        for variable-only families. A family whose modified derivatives
        must drop the name (binding RFN / UFL) is NOT taken this way — it
        waits for engine variable-font support (excluded, reason logged).

Usage:
    tools/build_catalog.py --gf-repo <google/fonts sparse checkout> \
        --site-metadata <metadata/fonts JSON> --build <dir> [--frozen-only]
"""

import argparse
import concurrent.futures as futures
import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gfmeta  # noqa: E402
import preview as ft_preview  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
V1_MANIFEST = ROOT / "manifest.json"
OUT_DIR = ROOT / "r2"
CSS2_URL = "https://fonts.googleapis.com/css2?family={spec}"
UA = "curl/7.64"  # a non-browser UA makes css2 serve whole static TTFs

QUALITY_CUTOFF = 67.5          # p25 of the GF /Quality/* tag mean (2026-10-08 data)
HEAVY_BYTES = 4_000_000        # a Regular this big (CJK) → at most Regular + Bold
TARGET_WEIGHTS = [400, 700, 500, 900]   # priority order; nearest ±100 stands in
ITALIC_CATEGORIES = {"sans-serif", "serif", "monospace"}
WEIGHT_NAMES = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium",
                600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}

# The picker's hand-picked rail — same list the jsDelivr builder ships.
FEATURED = [
    "Inter", "Playfair Display", "Bebas Neue", "Caveat", "Montserrat",
    "Abril Fatface", "Space Grotesk", "Dancing Script", "Fraunces",
    "Bangers", "Lora", "Pacifico", "Righteous", "JetBrains Mono",
]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url, retries=4):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception:  # noqa: BLE001 — retry any transient fetch error
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def slug_of(family):
    return family.lower().replace(" ", "")


def style_name(weight, italic):
    base = WEIGHT_NAMES.get(weight, str(weight))
    if italic:
        return "Italic" if weight == 400 else f"{base} Italic"
    return base


def category_of(site_entry):
    return (site_entry.get("category", "").lower() or "sans-serif").replace(" ", "-")


def pick_styles(uprights, italics, category):
    """Weight policy: the targets 400 / 700 / 500 / 900 (≤4 uprights), each
    taken exactly or by the nearest unused weight within ±100; nothing near
    400 → the available weight closest to it. Plus one italic (400, or the
    nearest within ±100) for text categories."""
    chosen = []
    for target in TARGET_WEIGHTS:
        if target in uprights and target not in chosen:
            chosen.append(target)
            continue
        near = sorted((w for w in uprights if abs(w - target) <= 100 and w not in chosen),
                      key=lambda w: (abs(w - target), -w if target >= 700 else w))
        if near:
            chosen.append(near[0])
    if not chosen and uprights:
        chosen.append(min(uprights, key=lambda w: (abs(w - 400), w)))
    out = [(w, False) for w in sorted(chosen)]
    if category in ITALIC_CATEGORIES and italics:
        near = sorted((w for w in italics if abs(w - 400) <= 100), key=lambda w: (abs(w - 400), w))
        if near:
            out.append((near[0], True))
    return out


def css2_spec(family, variants):
    ital_wght = ";".join(f"{1 if i else 0},{w}" for w, i in sorted(variants, key=lambda v: (v[1], v[0])))
    return f"{family.replace(' ', '+')}:ital,wght@{ital_wght}"


def parse_css2(css):
    out = {}
    for block in css.split("@font-face")[1:]:
        style = re.search(r"font-style:\s*(\w+)", block)
        weight = re.search(r"font-weight:\s*(\d+)", block)
        url = re.search(r"src:\s*url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)", block)
        if style and weight and url:
            out[(int(weight.group(1)), style.group(1) == "italic")] = url.group(1)
    return out


# ── selection ───────────────────────────────────────────────────────────────
def repo_static_files(rec):
    """{(weight, italic): repo-relative path} of a family's static TTFs."""
    out = {}
    for f in rec["meta"].get("fonts", []):
        fn = gfmeta.first(f, "filename", "")
        if not fn or "[" in fn or not fn.lower().endswith(".ttf"):
            continue
        key = (int(gfmeta.first(f, "weight", 400)), gfmeta.first(f, "style", "normal") == "italic")
        out.setdefault(key, f"{rec['dir']}/{fn}")
    return out


def select(repo, site, tags, frozen_names, frozen_only):
    """→ (plans, excluded). A plan = one family to build."""
    plans, excluded = [], {}
    for name in sorted(set(site) | set(frozen_names)):
        entry = site.get(name, {})
        rec = repo.get(name)
        if name in frozen_names:
            plans.append({"name": name, "kind": "frozen", "site": entry, "rec": rec})
            continue
        if frozen_only:
            continue
        t = tags.get(name, {})
        all_tags = {k for spec in t.values() for k in spec}
        quality = gfmeta.quality_score(t)
        why = None
        if not rec:
            why = "not in the google/fonts checkout"
        elif rec["license"] not in gfmeta.ALLOWED_LICENSES:
            why = f"licence {rec['license']}"
        elif not rec["licenseText"] or gfmeta.licence_kind_from_text(rec["licenseText"]) != rec["license"]:
            why = "licence text missing or not matching METADATA.pb"
        elif entry.get("isNoto"):
            why = "Noto (only the shipped per-script shelf)"
        elif "Symbols" in entry.get("classifications", []) or any(k.startswith("/Special use/") for k in all_tags):
            why = "symbols / special use"
        elif "/Purpose/Learn To Write" in all_tags:
            why = "learn-to-write"
        elif entry.get("colorCapabilities"):
            why = "colour font (engine colour-glyph path unverified)"
        elif quality is None:
            why = "no GF quality tags"
        elif quality < QUALITY_CUTOFF:
            why = f"quality {quality:.1f} < {QUALITY_CUTOFF}"
        if why:
            excluded[name] = why
            continue
        category = category_of(entry)
        statics = repo_static_files(rec)
        if statics:
            ups = {w for (w, i) in statics if not i}
            its = {w for (w, i) in statics if i}
            variants = pick_styles(ups, its, category)
            plans.append({"name": name, "kind": "gf-repo", "site": entry, "rec": rec,
                          "variants": [(w, i, statics[(w, i)]) for w, i in variants]})
            continue
        if gfmeta.must_rename_if_modified(name, rec["license"], rec["rfn"]):
            excluded[name] = ("variable-only + binding RFN/UFL: no unmodified static exists "
                              "(waits for engine variable-font support)")
            continue
        fonts = entry.get("fonts", {})
        ups = {int(k) for k in fonts if k.isdigit()}
        its = {int(k[:-1]) for k in fonts if k.endswith("i") and k[:-1].isdigit()}
        plans.append({"name": name, "kind": "gf-api", "site": entry, "rec": rec,
                      "variants": [(w, i, None) for w, i in pick_styles(ups, its, category)]})
    return plans, excluded


# ── building ────────────────────────────────────────────────────────────────
def put(build, sub, data, ext):
    digest = sha256(data)
    path = build / sub / f"{digest}.{ext}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")   # unique per writer
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)   # atomic; same bytes if two families share a file
    return digest, path


def build_family(plan, args, frozen):
    build = pathlib.Path(args.build)
    name = plan["name"]
    site = plan["site"]
    rec = plan["rec"]
    slug = slug_of(name)

    if plan["kind"] == "frozen":
        v1 = frozen[name]
        lic_bytes = (ROOT / v1["licenseFile"]).read_bytes()
        files = []
        for s in v1["styles"]:
            data = (ROOT / s["file"]).read_bytes()
            if sha256(data) != s["sha256"]:
                raise RuntimeError(f"{name}: frozen file {s['file']} does not match its sha256")
            files.append((s["weight"], s["italic"], data))
        category, subsets = v1["category"], v1["subsets"]
        origin = "gf-api"  # the jsDelivr catalog was built from the Fonts API
    else:
        lic_bytes = pathlib.Path(rec["licensePath"]).read_bytes()
        category = category_of(site)
        subsets = sorted(s for s in site.get("subsets", []) if s != "menu")
        origin = plan["kind"]
        files = []
        if origin == "gf-repo":
            for w, i, rel in plan["variants"]:
                files.append((w, i, (pathlib.Path(args.gf_repo) / rel).read_bytes()))
        else:
            css = fetch(CSS2_URL.format(spec=css2_spec(name, [(w, i) for w, i, _ in plan["variants"]])))
            served = parse_css2(css.decode("utf-8"))
            # Regular-most first, so a heavy family stops after one more file.
            order = sorted(((w, i) for w, i, _ in plan["variants"]), key=lambda v: (v[1], abs(v[0] - 400)))
            for w, i in order:
                if files and len(files[0][2]) > HEAVY_BYTES:
                    rest = [v for v in order if not v[1] and v != (files[0][0], files[0][1])]
                    if rest and (w, i) != min(rest, key=lambda v: abs(v[0] - 700)):
                        continue
                url = served.get((w, i))
                if url:
                    files.append((w, i, fetch(url)))
        # Heavy (CJK-size) families: Regular + the boldest-near-700 only.
        if files:
            regular = min(files, key=lambda f: (f[1], abs(f[0] - 400)))
            if len(regular[2]) > HEAVY_BYTES:
                bold = [f for f in files if not f[1] and f is not regular]
                bold = min(bold, key=lambda f: abs(f[0] - 700)) if bold else None
                files = [regular] + ([bold] if bold else [])

    if not files:
        raise RuntimeError(f"{name}: no files")

    lic_kind = gfmeta.licence_kind_from_text(lic_bytes.decode("utf-8", "replace"))
    if lic_kind not in gfmeta.ALLOWED_LICENSES:
        raise RuntimeError(f"{name}: licence text not OFL / Apache-2.0 / UFL")
    reserved = gfmeta.reserved_font_names(lic_bytes.decode("utf-8", "replace")) if lic_kind == "OFL" else []
    lic_sha, _ = put(build, "licenses", lic_bytes, "txt")

    styles = []
    for w, i, data in sorted(files, key=lambda f: (f[1], f[0])):
        digest, _ = put(build, "files", data, "ttf")
        styles.append({"name": style_name(w, i), "weight": w, "italic": i,
                       "file": f"files/{digest}.ttf", "bytes": len(data), "sha256": digest})

    src_style = min(styles, key=lambda s: abs(s["weight"] - 400) + (1 if s["italic"] else 0))
    tmp_prev = build / "tmp" / f"{slug}.preview.ttf"
    tmp_prev.parent.mkdir(parents=True, exist_ok=True)
    preview_rel = None
    try:
        prev_bytes = ft_preview.make_preview(str(build / src_style["file"]), name, str(tmp_prev))
        prev_sha, _ = put(build, "previews", prev_bytes, "ttf")
        preview_rel = f"previews/{prev_sha}.ttf"
    except Exception as e:  # noqa: BLE001 — a preview is a nicety
        print(f"  ! {name}: preview failed ({e})")
    finally:
        tmp_prev.unlink(missing_ok=True)

    entry = {
        "name": name,
        "slug": slug,
        "category": category,
        "subsets": subsets,
        "license": gfmeta.LICENSE_IDS[lic_kind],
        "licenseFile": f"licenses/{lic_sha}.txt",
        "rfn": gfmeta.must_rename_if_modified(name, lic_kind, reserved),
        "origin": origin,
        "styles": styles,
    }
    if reserved:
        entry["reservedFontNames"] = reserved
    designer = gfmeta.first(rec["meta"], "designer") if rec else None
    if designer:
        entry["designer"] = designer
    if preview_rel:
        entry["preview"] = preview_rel
    return entry


def fetch_repo_blobs(gf_repo, plans):
    """Add the chosen static TTFs to the sparse checkout (one batched fetch)."""
    paths = sorted({rel for p in plans if p["kind"] == "gf-repo" for _, _, rel in p["variants"]})
    missing = [p for p in paths if not (pathlib.Path(gf_repo) / p).exists()]
    if not missing:
        return
    current = subprocess.run(["git", "-C", gf_repo, "sparse-checkout", "list"],
                             capture_output=True, text=True, check=True).stdout.split()
    patterns = sorted(set(current) | {"/" + p for p in paths})
    print(f"sparse checkout: +{len(missing)} font files")
    subprocess.run(["git", "-C", gf_repo, "sparse-checkout", "set", "--no-cone", "--stdin"],
                   input="\n".join(patterns) + "\n", text=True, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gf-repo", required=True, help="sparse checkout of github.com/google/fonts")
    ap.add_argument("--site-metadata", required=True, help="fonts.google.com/metadata/fonts JSON")
    ap.add_argument("--build", required=True, help="output tree (files/ previews/ licenses/)")
    ap.add_argument("--out", default=str(OUT_DIR), help="where manifest.json / excluded.json go")
    ap.add_argument("--frozen-only", action="store_true", help="only the jsDelivr catalog's families")
    ap.add_argument("--only", default="", help="comma-separated family filter (debug)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--version", default=datetime.date.today().strftime("%Y.%m.%d") + ".1")
    args = ap.parse_args()

    repo = gfmeta.GoogleFontsRepo(args.gf_repo)
    fams = repo.families()
    site = gfmeta.load_site_metadata(args.site_metadata)
    tags = gfmeta.load_tags(pathlib.Path(args.gf_repo) / "tags/all/families.csv")
    v1 = json.loads(V1_MANIFEST.read_text())
    frozen = {f["name"]: f for f in v1["families"]}

    plans, excluded = select(fams, site, tags, set(frozen), args.frozen_only)
    if args.only:
        keep = {x.strip().lower() for x in args.only.split(",")}
        plans = [p for p in plans if p["name"].lower() in keep]
    slugs = {}
    for p in plans:
        s = slug_of(p["name"])
        if s in slugs:
            raise SystemExit(f"slug collision: {p['name']} / {slugs[s]}")
        slugs[s] = p["name"]
    kinds = {}
    for p in plans:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"selected {len(plans)} families {kinds}; excluded {len(excluded)}")

    fetch_repo_blobs(args.gf_repo, plans)

    entries, failed = [], {}
    with futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        jobs = {pool.submit(build_family, p, args, frozen): p["name"] for p in plans}
        for n, job in enumerate(futures.as_completed(jobs), 1):
            name = jobs[job]
            try:
                entries.append(job.result())
            except Exception as e:  # noqa: BLE001 — a family failing never sinks the catalog
                failed[name] = str(e)[:200]
                print(f"  ! {name}: {e}")
            if n % 50 == 0:
                print(f"  {n}/{len(plans)}")
    for name, why in failed.items():
        excluded[name] = f"build failed: {why}"

    commit = subprocess.run(["git", "-C", args.gf_repo, "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    names = {e["name"] for e in entries}
    manifest = {
        "schemaVersion": 1,
        "catalog": "r2",
        "catalogVersion": args.version,
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"google/fonts": commit, "metadata": "https://fonts.google.com/metadata/fonts"},
        "featured": [f for f in FEATURED if f in names],
        "families": sorted(entries, key=lambda e: e["name"]),
    }
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False) + "\n"
    (out / "manifest.json").write_text(text)
    (out / "excluded.json").write_text(json.dumps(dict(sorted(excluded.items())), indent=0, ensure_ascii=False) + "\n")
    shutil.rmtree(pathlib.Path(args.build) / "tmp", ignore_errors=True)

    files = [s for e in entries for s in e["styles"]]
    print(f"\nmanifest: {len(entries)} families, {len(files)} files, "
          f"{sum(s['bytes'] for s in files) / 1e6:.1f} MB, manifest {len(text) / 1e3:.0f} KB → {out / 'manifest.json'}")
    if failed:
        print(f"failed: {len(failed)} (see excluded.json)")


if __name__ == "__main__":
    main()
