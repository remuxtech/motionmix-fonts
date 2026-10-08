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

The variable-only `rfn` families (no unmodified static) are no longer
pending: tools/build_catalog.py serves their unmodified variable files at
named instances (ADR 125 Am. 3, kind "gf-variable", tools/varfont.py).
r2/variable_pending.json is the record of that switch (catalog 2026.10.08.3)
and is not regenerated.

Usage:
    tools/rfn_upstream.py --gf-repo <google/fonts checkout incl. the files> --switch
"""

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gfmeta  # noqa: E402
import preview as ft_preview  # noqa: E402
import varfont  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "manifest.json"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def switch(gf_repo, fams):
    manifest = json.loads(MANIFEST.read_text())
    changed = []
    for entry in manifest["families"]:
        if not entry.get("rfn") or entry.get("origin") == "gf-repo":
            continue
        rec = fams[entry["name"]]
        statics = {(w, i): rel for w, i, rel in varfont.repo_files(rec, variable=False)}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gf-repo", required=True)
    ap.add_argument("--switch", action="store_true")
    args = ap.parse_args()
    fams = gfmeta.GoogleFontsRepo(args.gf_repo).families()
    if args.switch:
        switch(args.gf_repo, fams)


if __name__ == "__main__":
    main()
