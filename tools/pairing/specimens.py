#!/usr/bin/env python3
"""Render specimen contact sheets of a context's Shuffle deck (or top ranking), to tune the scorer by eye.

Dev tool, not part of the scorer: needs Pillow. Fonts are fetched from the R2 catalog by the manifest's file paths,
checked against their sha256 and cached under --cache (content-addressed, reusable across runs).

    specimens.py --cache <dir> --out <dir> [--contexts bold_hook,editorial_quote] [--count 12] [--mode deck|top]
"""

import argparse
import hashlib
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pair_scorer as P  # noqa: E402

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

BASE = "https://library.motionmix.app/fonts/"
UA = "Mozilla/5.0 (motionmix-fonts specimens)"
COLS, CELL_W, CELL_H = 3, 660, 330
PAD = 28


class Fonts:
    def __init__(self, manifest_path, cache):
        man = json.loads(pathlib.Path(manifest_path).read_text())
        self.styles = {}
        for fam in man["families"]:
            for st in fam["styles"]:
                self.styles[(fam["slug"], st["name"])] = st
        self.cache = pathlib.Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)

    def path(self, slug, style):
        st = self.styles[(slug, style)]
        dst = self.cache / (st["sha256"] + ".ttf")
        if not dst.exists():
            req = urllib.request.Request(BASE + st["file"], headers={"User-Agent": UA})
            data = urllib.request.urlopen(req, timeout=60).read()
            if hashlib.sha256(data).hexdigest() != st["sha256"]:
                raise RuntimeError("sha256 mismatch for %s %s" % (slug, style))
            dst.write_bytes(data)
        return str(dst)


def fit_font(path, text, max_w, start, floor=18):
    size = start
    while size > floor:
        f = ImageFont.truetype(path, size)
        if f.getlength(text) <= max_w:
            return f
        size -= 4
    return ImageFont.truetype(path, floor)


def render(ix, fonts, name, raw, recs, out, label_font):
    sample = raw.get("sample") or {"display": "Display line", "support": "The support line under it"}
    ctx = P.parse_context(raw)
    caps = ctx["roles"]["display"]["caps"]
    scaps = ctx["roles"]["support"]["caps"]
    rows = (len(recs) + COLS - 1) // COLS
    img = Image.new("RGB", (COLS * CELL_W, 70 + rows * CELL_H), (250, 249, 246))
    dr = ImageDraw.Draw(img)
    head = ImageFont.truetype(label_font, 26)
    moods = ", ".join("%s %.1f" % (k, v) for k, v in raw.get("moods", {}).items())
    dr.text((PAD, 20), "%s  —  %s   (%s)" % (name, raw.get("note", ""), moods), font=head, fill=(40, 40, 40))
    small = ImageFont.truetype(label_font, 17)
    for k, r in enumerate(recs):
        x0 = (k % COLS) * CELL_W
        y0 = 70 + (k // COLS) * CELL_H
        dr.rectangle([x0 + 8, y0 + 8, x0 + CELL_W - 8, y0 + CELL_H - 8], fill=(255, 255, 255), outline=(220, 218, 212))
        d, s = r["display"], r["support"]
        dtext = sample["display"].upper() if caps else sample["display"]
        stext = sample["support"].upper() if scaps else sample["support"]
        try:
            df = fit_font(fonts.path(d["slug"], d["style"]), dtext, CELL_W - 2 * PAD, 92)
            sf = fit_font(fonts.path(s["slug"], s["style"]), stext, CELL_W - 2 * PAD, 34)
        except Exception as e:  # noqa: BLE001 — a sheet with a hole beats no sheet
            dr.text((x0 + PAD, y0 + 120), "load failed: %s" % e, font=small, fill=(200, 0, 0))
            continue
        dr.text((x0 + PAD, y0 + 40), dtext, font=df, fill=(17, 17, 17))
        dy = y0 + 40 + df.getbbox(dtext)[3] + 26
        dr.text((x0 + PAD, dy), stext, font=sf, fill=(70, 70, 70))
        lab = "%d. %s %s + %s %s   %.3f" % (k + 1, d["name"], d["style"], s["name"], s["style"], r["score"])
        dr.text((x0 + PAD, y0 + CELL_H - 58), lab, font=small, fill=(110, 110, 110))
        t = r["terms"]
        lab2 = "mood %.2f fit %.2f bal %.2f har %.2f q %.2f boost %.2f  %s" % (
            t["mood"], t["fit"], t["balance"], t["harmony"], t["quality"], t["boost"], ",".join(r["why"]))
        dr.text((x0 + PAD, y0 + CELL_H - 36), lab2, font=small, fill=(150, 150, 150))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--index", default=str(P.ROOT / "r2" / "index.json"))
    ap.add_argument("--manifest", default=str(P.ROOT / "r2" / "manifest.json"))
    ap.add_argument("--seeds", default=str(P.ROOT / "pairing" / "seed_pairs.json"))
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--contexts", default="")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--mode", default="deck", choices=("deck", "top"))
    ap.add_argument("--suffix", default="")
    args = ap.parse_args()
    seeds = args.seeds if pathlib.Path(args.seeds).exists() else None
    ix = P.load(args.index, seeds)
    fonts = Fonts(args.manifest, args.cache)
    label = fonts.path("inter", "Regular")
    allc = json.loads((P.HERE / "contexts.json").read_text())["contexts"]
    names = [n for n in args.contexts.split(",") if n] or list(allc)
    for name in names:
        raw = allc[name]
        ctx = P.parse_context(raw)
        ranked = P.rank(ix, ctx)
        recs = P.deal(ix, ctx, ranked, args.count) if args.mode == "deck" else ranked[:args.count]
        out = render(ix, fonts, name, raw, recs, pathlib.Path(args.out) / ("%s_%s%s.png" % (name, args.mode,
                                                                                          args.suffix)), label)
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
