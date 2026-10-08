#!/usr/bin/env python3
"""
Publish the R2 font catalog (ADR 125 Am. 2) to the `motionmix-library` bucket
under `fonts/`, served at https://library.motionmix.app/fonts/.

    fonts/files/<sha256>.ttf       font/ttf     public, max-age=31536000, immutable
    fonts/previews/<sha256>.ttf    font/ttf     (same)
    fonts/licenses/<sha256>.txt    text/plain   (same)
    fonts/index/<sha256>.json      JSON         (same)          ← the pair-scorer index
    fonts/index.json               JSON         public, max-age=300   (alias of the above)
    fonts/manifest.json            JSON         public, max-age=300   ← uploaded LAST

Only objects not already listed by the previously published manifest
(r2/manifest.json at git HEAD, or --previous) are uploaded, unless --all.
Content-addressed keys never change meaning, so a re-upload is harmless, and
nothing is ever deleted: a retired file (e.g. a Fonts-API static a variable
style `replaces`) stays fetchable for documents and old manifests that pin it.
A variable style's file is `variable.file` (ADR 125 Am. 3).

After the upload every object this run uploaded is fetched back from the CDN
(status + content sha256 = its name), plus --verify sampled families.

Runs wrangler (OAuth login on this Mac; never pass tokens):
    NODE_OPTIONS=--dns-result-order=ipv4first npx -y wrangler@4 r2 bulk put …
It must run outside the agent sandbox.

Usage:
    tools/publish_r2.py --build <dir> [--all] [--dry-run] [--verify N]
"""

import argparse
import hashlib
import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import varfont  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUCKET = "motionmix-library"
PREFIX = "fonts"
CDN = "https://library.motionmix.app"
UA = "mx-library"   # the zone's Browser Integrity Check refuses library-default UAs
IMMUTABLE = "public, max-age=31536000, immutable"
SHORT = "public, max-age=300"
TYPES = {"ttf": "font/ttf", "txt": "text/plain; charset=utf-8", "json": "application/json; charset=utf-8"}
WRANGLER = ["npx", "-y", "wrangler@4"]


def env():
    e = dict(os.environ)
    e["NODE_OPTIONS"] = (e.get("NODE_OPTIONS", "") + " --dns-result-order=ipv4first").strip()
    return e


def objects_of(manifest):
    """Every immutable object a manifest references → {relative path}."""
    out = set()
    for fam in manifest.get("families", []):
        for s in fam.get("styles", []):
            out.add(varfont.style_source(s)[0])
        if fam.get("preview"):
            out.add(fam["preview"])
        if fam.get("licenseFile"):
            out.add(fam["licenseFile"])
    idx = manifest.get("index")
    if idx:
        out.add(idx["file"])
    return out


def previous_manifest(path):
    if path:
        return json.loads(pathlib.Path(path).read_text())
    shown = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:r2/manifest.json"],
                           capture_output=True, text=True)
    return json.loads(shown.stdout) if shown.returncode == 0 else {}


def run_wrangler(args, attempts=10):
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(WRANGLER + args, env=env(), capture_output=True, text=True)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        if proc.returncode == 0:
            return tail
        print(f"    wrangler failed (attempt {attempt}/{attempts}): {' | '.join(tail)[:300]}")
        time.sleep(min(30, 3 * attempt))
    raise SystemExit("wrangler kept failing — re-run publish_r2.py (uploads are idempotent)")


def bulk_put(entries, content_type, cache_control, chunk, concurrency, dry_run):
    """entries: [(key, local path)] — chunked bulk puts, each retried whole."""
    for start in range(0, len(entries), chunk):
        part = entries[start:start + chunk]
        print(f"  bulk put {start + 1}–{start + len(part)} / {len(entries)}  ({content_type})")
        if dry_run:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump([{"key": k, "file": str(p)} for k, p in part], fh)
            listing = fh.name
        try:
            run_wrangler(["r2", "bulk", "put", BUCKET, "--filename", listing, "--remote", "-y",
                          "--concurrency", str(concurrency), "--ct", content_type, "--cc", cache_control])
        finally:
            os.unlink(listing)


def put_one(key, path, content_type, cache_control, dry_run):
    print(f"  put {key}")
    if not dry_run:
        run_wrangler(["r2", "object", "put", f"{BUCKET}/{key}", "--file", str(path), "--remote",
                      "--ct", content_type, "--cc", cache_control])


def get(url):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read(), dict(r.headers)


def verify_objects(rels):
    """Every content-addressed object: HTTP 200 and sha256(body) = the name's hash."""
    bad = 0
    for rel in sorted(rels):
        try:
            status, body, headers = get(f"{CDN}/{PREFIX}/{rel}")
            ok = status == 200 and rel.rsplit("/", 1)[-1].split(".")[0] == hashlib.sha256(body).hexdigest() \
                and "immutable" in (headers.get("Cache-Control") or "")
        except Exception as e:  # noqa: BLE001
            ok, status = False, str(e)
        if not ok:
            bad += 1
            print(f"  ✗ {rel}: {status}")
    print(f"verify: {len(rels)} uploaded objects, {bad} bad")
    return bad == 0


def verify(manifest, sample):
    fams = manifest["families"]
    picks = random.sample(fams, min(sample, len(fams)))
    bad = 0
    for fam in picks:
        for s in fam["styles"][:2]:
            file, sha, _, _ = varfont.style_source(s)
            try:
                status, body, _ = get(f"{CDN}/{PREFIX}/{file}")
                ok = status == 200 and hashlib.sha256(body).hexdigest() == sha
            except Exception as e:  # noqa: BLE001
                ok, status = False, str(e)
            if not ok:
                bad += 1
                print(f"  ✗ {fam['name']} {s['name']}: {status}")
        for rel in (fam.get("preview"), fam.get("licenseFile")):
            if rel:
                try:
                    status, body, _ = get(f"{CDN}/{PREFIX}/{rel}")
                    ok = status == 200 and rel.split("/")[-1].startswith(hashlib.sha256(body).hexdigest())
                except Exception as e:  # noqa: BLE001
                    ok, status = False, str(e)
                if not ok:
                    bad += 1
                    print(f"  ✗ {fam['name']} {rel}: {status}")
    status, body, headers = get(f"{CDN}/{PREFIX}/manifest.json")
    live = json.loads(body)
    print(f"  live manifest: {live.get('catalogVersion')} · {len(live.get('families', []))} families · "
          f"cache-control '{headers.get('Cache-Control')}'")
    print(f"verify: {len(picks)} families sampled, {bad} bad")
    return bad == 0 and live.get("catalogVersion") == manifest.get("catalogVersion")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", required=True)
    ap.add_argument("--manifest", default=str(ROOT / "r2/manifest.json"))
    ap.add_argument("--index", default=str(ROOT / "r2/index.json"))
    ap.add_argument("--previous", default="", help="previously published manifest (default: git HEAD)")
    ap.add_argument("--all", action="store_true", help="upload every referenced object")
    ap.add_argument("--chunk", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", type=int, default=25, help="families to spot-check on the CDN (0 = skip)")
    args = ap.parse_args()

    build = pathlib.Path(args.build)
    manifest_path = pathlib.Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    wanted = objects_of(manifest)
    done = set() if args.all else objects_of(previous_manifest(args.previous))
    todo = sorted(wanted - done)
    missing = [rel for rel in todo if not (build / rel).exists()]
    idx_local = pathlib.Path(args.index)
    if manifest.get("index") and manifest["index"]["file"] in missing and idx_local.exists():
        missing.remove(manifest["index"]["file"])
    if missing:
        raise SystemExit(f"{len(missing)} referenced objects are not in the build dir, e.g. {missing[:3]}")
    print(f"{len(wanted)} objects referenced, {len(todo)} to upload")

    groups = {}
    for rel in todo:
        local = build / rel
        if manifest.get("index") and rel == manifest["index"]["file"]:
            local = idx_local
        groups.setdefault(rel.rsplit(".", 1)[1], []).append((f"{PREFIX}/{rel}", local))
    for ext in ("ttf", "txt", "json"):
        if groups.get(ext):
            bulk_put(groups[ext], TYPES[ext], IMMUTABLE, args.chunk, args.concurrency, args.dry_run)

    if manifest.get("index") and idx_local.exists():
        put_one(f"{PREFIX}/index.json", idx_local, TYPES["json"], SHORT, args.dry_run)
    put_one(f"{PREFIX}/manifest.json", manifest_path, TYPES["json"], SHORT, args.dry_run)

    if not args.dry_run:
        time.sleep(2)
        ok = verify_objects(todo)
        if args.verify:
            ok = verify(manifest, args.verify) and ok
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
