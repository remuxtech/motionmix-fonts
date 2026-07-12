#!/usr/bin/env python3
"""
motionmix-fonts mirror builder (ADR 125 S3, hosting rung R1).

Pulls a CURATED set of Google Fonts families as static TTFs (the css2 API
with a plain UA serves per-weight static instances, even for variable-only
families), plus each family's license text, and emits the versioned
manifest.json the app's Remote font source consumes.

Layout produced (the repo root, served via jsDelivr):
    manifest.json
    fonts/<slug>/<Family>-<Style>.ttf
    fonts/<slug>/LICENSE.txt

Idempotent: existing files are kept unless --force; the manifest is always
rewritten from what's on disk + fetched metadata.

Usage:
    tools/build_mirror.py [--limit N] [--force] [--only family,family,...]
"""

import argparse
import hashlib
import json
import pathlib
import re
import sys
import time
import urllib.request

try:
    from fontTools import subset as ft_subset
except ImportError:  # previews skipped; `.venv/bin/pip install fonttools`
    ft_subset = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "fonts"
MANIFEST = ROOT / "manifest.json"

METADATA_URL = "https://fonts.google.com/metadata/fonts"
CSS2_URL = "https://fonts.googleapis.com/css2?family={spec}"
LICENSE_URLS = {
    "ofl": "https://raw.githubusercontent.com/google/fonts/main/ofl/{slug}/OFL.txt",
    "apache": "https://raw.githubusercontent.com/google/fonts/main/apache/{slug}/LICENSE.txt",
    "ufl": "https://raw.githubusercontent.com/google/fonts/main/ufl/{slug}/UFL.txt",
}
UA = "curl/7.64"  # a non-browser UA makes css2 serve static TTF urls

# ---------------------------------------------------------------------------
# The CURATED catalog (ADR 125 §4): ~90 families across the five categories a
# video editor needs. Weights requested = available ∩ POLICY below.
# ---------------------------------------------------------------------------
CURATED = [
    # Sans-serif workhorses
    "Inter", "Roboto", "Open Sans", "Lato", "Montserrat", "Poppins",
    "Raleway", "Nunito", "Work Sans", "DM Sans", "Manrope", "Rubik",
    "Karla", "Barlow", "Outfit", "Plus Jakarta Sans", "Figtree", "Sora",
    "Space Grotesk", "Archivo", "Mulish", "Heebo", "Cabin", "Ubuntu",
    "PT Sans", "Noto Sans", "Quicksand", "Comfortaa", "Fredoka", "Exo 2",
    # Serif
    "Playfair Display", "Merriweather", "Lora", "PT Serif",
    "Source Serif 4", "Libre Baskerville", "EB Garamond",
    "Cormorant Garamond", "Crimson Text", "Bitter", "Zilla Slab",
    "Roboto Slab", "Arvo", "Josefin Slab", "Noto Serif", "Fraunces",
    "DM Serif Display", "Prata", "Cinzel", "Marcellus",
    # Display / impact
    "Oswald", "Bebas Neue", "Anton", "Abril Fatface", "Alfa Slab One",
    "Righteous", "Bangers", "Luckiest Guy", "Titan One", "Passion One",
    "Archivo Black", "Russo One", "Black Ops One", "Bungee", "Monoton",
    "Audiowide", "Orbitron", "Press Start 2P", "Chewy", "Baloo 2",
    "Teko", "Rajdhani", "Yanone Kaffeesatz", "Unbounded", "Syne",
    # Handwriting / script
    "Lobster", "Pacifico", "Dancing Script", "Great Vibes", "Caveat",
    "Satisfy", "Sacramento", "Shadows Into Light", "Amatic SC",
    "Permanent Marker", "Kalam", "Indie Flower", "Courgette",
    "Kaushan Script", "Yellowtail", "Homemade Apple", "Rock Salt",
    # Monospace
    "JetBrains Mono", "Fira Code", "Source Code Pro", "IBM Plex Mono",
    "Space Mono", "Inconsolata", "Roboto Mono",
]

# Upright weights we keep when the family has them (≤4 per family), plus
# 400-italic for body categories. Bounded so the repo stays CDN-friendly.
UPRIGHT_POLICY = [400, 500, 700, 900]
ITALIC_POLICY = [400]
ITALIC_CATEGORIES = {"sans-serif", "serif", "monospace"}

WEIGHT_NAMES = {
    100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular",
    500: "Medium", 600: "SemiBold", 700: "Bold", 800: "ExtraBold",
    900: "Black",
}


def fetch(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 — retry any transient fetch error
            if attempt == retries - 1:
                raise
            print(f"    retry {attempt + 1} ({e})")
            time.sleep(1.5 * (attempt + 1))


def slug_of(family):
    return family.lower().replace(" ", "")


def style_name(weight, italic):
    base = WEIGHT_NAMES[weight]
    if italic:
        return "Italic" if weight == 400 else f"{base} Italic"
    return base


def load_metadata():
    raw = fetch(METADATA_URL).decode("utf-8")
    raw = raw.lstrip(")]}'\n")  # the anti-JSON-hijack prefix
    meta = json.loads(raw)
    return {f["family"]: f for f in meta["familyMetadataList"]}


def parse_css2(css):
    """@font-face blocks → [(weight, italic, url)]."""
    out = []
    for block in css.split("@font-face")[1:]:
        style = re.search(r"font-style:\s*(\w+)", block)
        weight = re.search(r"font-weight:\s*(\d+)", block)
        url = re.search(r"src:\s*url\((https://fonts\.gstatic\.com/[^)]+\.ttf)\)", block)
        if style and weight and url:
            out.append((int(weight.group(1)), style.group(1) == "italic", url.group(1)))
    return out


def wanted_variants(meta_entry):
    """The policy ∩ available variants for one family."""
    fonts = meta_entry.get("fonts", {})  # keys like "400", "700i"
    category = (meta_entry.get("category", "").lower()).replace(" ", "-")
    wanted = []
    for w in UPRIGHT_POLICY:
        if str(w) in fonts:
            wanted.append((w, False))
    if not wanted:  # oddball families (e.g. only 300) — take what exists
        uprights = sorted(int(k) for k in fonts if k.isdigit())
        if uprights:
            wanted.append((uprights[0], False))
    if category in ITALIC_CATEGORIES:
        for w in ITALIC_POLICY:
            if f"{w}i" in fonts:
                wanted.append((w, True))
    return wanted


def css2_spec(family, variants):
    ital_wght = ";".join(
        f"{1 if italic else 0},{weight}"
        for weight, italic in sorted(variants, key=lambda v: (v[1], v[0]))
    )
    return f"{family.replace(' ', '+')}:ital,wght@{ital_wght}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="build only the first N families")
    ap.add_argument("--only", default="", help="comma-separated family filter")
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    args = ap.parse_args()

    families = CURATED
    if args.only:
        keep = {f.strip().lower() for f in args.only.split(",")}
        families = [f for f in families if f.lower() in keep]
    if args.limit:
        families = families[: args.limit]

    print(f"metadata: {METADATA_URL}")
    metadata = load_metadata()

    manifest_families = []
    skipped = []
    for family in families:
        meta = metadata.get(family)
        if not meta:
            skipped.append((family, "not in Google metadata"))
            continue
        category = (meta.get("category", "").lower() or "sans-serif").replace(" ", "-")
        license_id = (meta.get("license") or "ofl").lower()
        variants = wanted_variants(meta)
        if not variants:
            skipped.append((family, "no usable variants"))
            continue

        slug = slug_of(family)
        fam_dir = FONTS_DIR / slug
        fam_dir.mkdir(parents=True, exist_ok=True)
        print(f"{family}  [{category}, {license_id}] {[style_name(w, i) for w, i in variants]}")

        css = fetch(CSS2_URL.format(spec=css2_spec(family, variants))).decode("utf-8")
        served = {(w, i): url for w, i, url in parse_css2(css)}

        styles = []
        for weight, italic in variants:
            url = served.get((weight, italic))
            if not url:
                print(f"    ! css2 did not serve {weight}{'i' if italic else ''} — skipped")
                continue
            fname = f"{family.replace(' ', '')}-{style_name(weight, italic).replace(' ', '')}.ttf"
            fpath = fam_dir / fname
            if args.force or not fpath.exists():
                fpath.write_bytes(fetch(url))
            data = fpath.read_bytes()
            styles.append({
                "name": style_name(weight, italic),
                "weight": weight,
                "italic": italic,
                "file": f"fonts/{slug}/{fname}",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        if not styles:
            skipped.append((family, "no styles fetched"))
            continue

        # The NAME-SUBSET preview face (ADR 125): the family's Regular-most
        # style cut down to exactly its own name's glyphs — a few KB the
        # picker renders each row's title with, without fetching real fonts.
        preview_rel = None
        if ft_subset is not None and styles:
            src = ROOT / min(styles, key=lambda s: abs(s["weight"] - 400) + (1 if s["italic"] else 0))["file"]
            preview_path = fam_dir / "preview.ttf"
            if args.force or not preview_path.exists():
                try:
                    options = ft_subset.Options()
                    options.layout_features = []       # no shaping machinery
                    options.name_IDs = []              # strip the name table noise
                    options.hinting = False
                    options.notdef_outline = False
                    subsetter = ft_subset.Subsetter(options=options)
                    font = ft_subset.load_font(str(src), options)
                    subsetter.populate(text=family)
                    subsetter.subset(font)
                    ft_subset.save_font(font, str(preview_path), options)
                    font.close()
                except Exception as e:  # noqa: BLE001 — preview is a nicety, never fatal
                    print(f"    ! preview subset failed ({e}) — row falls back to the default face")
            if preview_path.exists():
                preview_rel = f"fonts/{slug}/preview.ttf"

        lic_path = fam_dir / "LICENSE.txt"
        if args.force or not lic_path.exists():
            # The google/fonts dir layout varies (a family's metadata license
            # doesn't always match its directory) — try every candidate.
            candidates = []
            primary = LICENSE_URLS.get(license_id)
            if primary:
                candidates.append(primary.format(slug=slug))
            for d, f in (("ofl", "OFL.txt"), ("apache", "LICENSE.txt"), ("ufl", "UFL.txt")):
                url = f"https://raw.githubusercontent.com/google/fonts/main/{d}/{slug}/{f}"
                if url not in candidates:
                    candidates.append(url)
            for lic_url in candidates:
                try:
                    lic_path.write_bytes(fetch(lic_url, retries=1))
                    break
                except Exception:
                    continue
            else:
                print(f"    ! license fetch failed (all candidates) — REQUIRED for redistribution")
                skipped.append((family, "license fetch failed"))
                continue

        entry = {
            "name": family,
            "slug": slug,
            "category": category,
            "license": license_id.upper(),
            "licenseFile": f"fonts/{slug}/LICENSE.txt",
            "styles": styles,
        }
        if preview_rel:
            entry["preview"] = preview_rel  # additive — schemaVersion stays 1
        manifest_families.append(entry)

    manifest = {
        "schemaVersion": 1,
        "families": sorted(manifest_families, key=lambda f: f["name"]),
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n")

    total_bytes = sum(s["bytes"] for f in manifest_families for s in f["styles"])
    total_files = sum(len(f["styles"]) for f in manifest_families)
    print(f"\nmanifest: {len(manifest_families)} families, {total_files} files, "
          f"{total_bytes / 1e6:.1f} MB → {MANIFEST}")
    if skipped:
        print("skipped:")
        for fam, why in skipped:
            print(f"  - {fam}: {why}")
        sys.exit(2 if not manifest_families else 0)


if __name__ == "__main__":
    main()
