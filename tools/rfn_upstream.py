#!/usr/bin/env python3
"""
Reserved-Font-Name follow-through (ADR 125 Am. 2, decision 2026-10-08).

--switch   Every R1 family with `rfn: true` whose google/fonts directory has
           static TTFs is switched to those ORIGINAL files, byte-identical:
           fonts/<slug>/upstream/<upstream filename> (new paths, so a cached
           old manifest and a new one never disagree on a file), the Fonts-API
           copies are removed, LICENSE.txt is refreshed from the checkout, the
           preview is regenerated (neutral name), and the manifest entry gets
           `origin: "gf-repo"` and per style `replaces: [old sha256]` — the
           heal map for templates that pinned the old file (ADR 125 Am. 1).
           build_mirror.py / build_catalog.py carry these entries over.

--pending  Writes r2/variable_pending.json: the `rfn` families with no
           unmodified static — the ones R1 still ships as Fonts-API instances
           and the ones the R2 catalog excludes — with their upstream variable
           files, axes, named instances and the catalog styles each should
           map to once the engine renders named instances (SkFontArguments).

Usage:
    tools/rfn_upstream.py --gf-repo <google/fonts checkout incl. the files> \
        --site-metadata <json> [--switch] [--pending]
"""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

from fontTools.ttLib import TTFont

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_catalog  # noqa: E402
import gfmeta  # noqa: E402
import preview as ft_preview  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def repo_files(rec, variable):
    out = []
    for f in rec["meta"].get("fonts", []):
        fn = gfmeta.first(f, "filename", "")
        if fn.lower().endswith(".ttf") and ("[" in fn) == variable:
            out.append((int(gfmeta.first(f, "weight", 400)), gfmeta.first(f, "style", "normal") == "italic",
                        f"{rec['dir']}/{fn}"))
    return out


def switch(gf_repo, fams):
    manifest = json.loads(MANIFEST.read_text())
    changed = []
    for entry in manifest["families"]:
        if not entry.get("rfn") or entry.get("origin") == "gf-repo":
            continue
        rec = fams[entry["name"]]
        statics = {(w, i): rel for w, i, rel in repo_files(rec, variable=False)}
        if not statics:
            continue
        missing = [s["name"] for s in entry["styles"] if (s["weight"], s["italic"]) not in statics]
        if missing:
            raise SystemExit(f"{entry['name']}: no upstream static for {missing}")
        slug_dir = ROOT / "fonts" / entry["slug"]
        new_styles = []
        for s in entry["styles"]:
            rel = statics[(s["weight"], s["italic"])]
            data = (pathlib.Path(gf_repo) / rel).read_bytes()
            dest = slug_dir / "upstream" / pathlib.Path(rel).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            old = ROOT / s["file"]
            if old.exists() and old != dest:
                subprocess.run(["git", "-C", str(ROOT), "rm", "-q", "--cached", "--ignore-unmatch", s["file"]], check=True)
                old.unlink()
            new_styles.append({"name": s["name"], "weight": s["weight"], "italic": s["italic"],
                               "file": dest.relative_to(ROOT).as_posix(), "bytes": len(data),
                               "sha256": sha256(data), "replaces": [s["sha256"]] + s.get("replaces", [])})
        lic = pathlib.Path(rec["licensePath"]).read_bytes()
        (ROOT / entry["licenseFile"]).write_bytes(lic)
        src = min(new_styles, key=lambda s: abs(s["weight"] - 400) + (1 if s["italic"] else 0))
        ft_preview.make_preview(str(ROOT / src["file"]), entry["name"], str(ROOT / entry["preview"]))
        entry["styles"] = new_styles
        entry["origin"] = "gf-repo"
        changed.append(entry["name"])
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n")
    print(f"switched {len(changed)} families to upstream statics: {', '.join(changed)}")


def vf_record(gf_repo, rel, italic):
    path = pathlib.Path(gf_repo) / rel
    data = path.read_bytes()
    font = TTFont(path, lazy=True)
    name = font["name"]
    axes = [{"tag": a.axisTag, "min": a.minValue, "default": a.defaultValue, "max": a.maxValue}
            for a in font["fvar"].axes]
    instances = []
    for inst in font["fvar"].instances:
        ps = name.getDebugName(inst.postscriptNameID) if inst.postscriptNameID not in (None, 0xFFFF) else None
        instances.append({"name": name.getDebugName(inst.subfamilyNameID), "postscriptName": ps,
                          "coordinates": {k: round(v, 3) for k, v in inst.coordinates.items()}})
    font.close()
    return {"path": rel, "italic": italic, "bytes": len(data), "sha256": sha256(data),
            "axes": axes, "namedInstances": instances}


def pending(gf_repo, fams, site):
    r1 = {f["name"]: f for f in json.loads(MANIFEST.read_text())["families"]}
    excluded = json.loads((ROOT / "r2/excluded.json").read_text())
    names = [(n, "shipping-gf-api") for n, f in sorted(r1.items()) if f.get("rfn") and f.get("origin") != "gf-repo"]
    names += [(n, "excluded") for n, why in sorted(excluded.items()) if why.startswith("variable-only")]
    commit = subprocess.run(["git", "-C", gf_repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = []
    for name, status in names:
        rec = fams[name]
        files = [vf_record(gf_repo, rel, italic) for _, italic, rel in repo_files(rec, variable=True)]

        def instance_for(weight, italic):
            for f in files:
                if f["italic"] != italic:
                    continue
                defaults = {a["tag"]: a["default"] for a in f["axes"]}
                for inst in f["namedInstances"]:
                    c = inst["coordinates"]
                    if c.get("wght") == weight and all(c.get(t, d) == d for t, d in defaults.items() if t != "wght"):
                        return {"file": pathlib.Path(f["path"]).name, "instance": inst["name"], "coordinates": c}
            return None

        if status == "shipping-gf-api":
            wanted = [(s["weight"], s["italic"]) for s in r1[name]["styles"]]
        else:
            ups, its = set(), set()
            for w, i in ((w, i) for w in range(100, 1000, 100) for i in (False, True)):
                if instance_for(w, i):
                    (its if i else ups).add(w)
            wanted = build_catalog.pick_styles(ups, its, build_catalog.category_of(site.get(name, {})))
        targets = []
        for w, i in wanted:
            t = {"name": build_catalog.style_name(w, i), "weight": w, "italic": i}
            t.update(instance_for(w, i) or {"file": None, "instance": None, "coordinates": None})
            if status == "shipping-gf-api":
                t["shippedSha256"] = next(s["sha256"] for s in r1[name]["styles"] if (s["weight"], s["italic"]) == (w, i))
            targets.append(t)
        lic_kind = rec["license"]
        out.append({"family": name, "slug": build_catalog.slug_of(name), "status": status,
                    "license": gfmeta.LICENSE_IDS[lic_kind], "reservedFontNames": rec["rfn"],
                    "googleFontsDir": rec["dir"], "files": files, "targetStyles": targets})
    doc = {
        "schemaVersion": 1,
        "about": ("rfn families with no unmodified static TTF (ADR 125 Am. 2). Plan: once the engine renders a "
                  "variable font's named instances (SkFontArguments), each family ships its UNMODIFIED variable "
                  "file(s) and every targetStyle maps to the named instance given here; 'shipping-gf-api' "
                  "families then retire their Fonts-API statics (shippedSha256 → heal map), 'excluded' "
                  "families join the catalog."),
        "googleFontsCommit": commit,
        "families": out,
    }
    path = ROOT / "r2/variable_pending.json"
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    unmapped = [(f["family"], t["name"]) for f in out for t in f["targetStyles"] if not t["instance"]]
    print(f"variable_pending: {len(out)} families "
          f"({sum(f['status'] == 'shipping-gf-api' for f in out)} shipping, "
          f"{sum(f['status'] == 'excluded' for f in out)} excluded) → {path}")
    if unmapped:
        print(f"  styles without an exact named instance: {unmapped}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gf-repo", required=True)
    ap.add_argument("--site-metadata", required=True)
    ap.add_argument("--switch", action="store_true")
    ap.add_argument("--pending", action="store_true")
    args = ap.parse_args()
    fams = gfmeta.GoogleFontsRepo(args.gf_repo).families()
    if args.switch:
        switch(args.gf_repo, fams)
    if args.pending:
        pending(args.gf_repo, fams, gfmeta.load_site_metadata(args.site_metadata))


if __name__ == "__main__":
    main()
