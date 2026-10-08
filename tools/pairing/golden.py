#!/usr/bin/env python3
"""Golden vectors for ports of the pair scorer (the Kotlin port in appkit runs the same cases and must match).

    golden.py --write     rebuild golden/index_fixture.json + golden/golden_v1.json from r2/index.json
    golden.py --check     re-run every case against the committed files; exit 1 on any difference

The fixture is a deterministic subset of the published index (the families the cases reach, every seed family and a
spread of the rest so the gates see real rejects), so a port's test needs no network and no 1.5 MB resource.

What a port must reproduce, per case: `ranked` (the count of passing pairs), `rejects` (count per gate code), `top`
(the first TOP_N of rank(): ids + q, where q = floor(score * 1e6 + 0.5)), `terms` for the first 3 (each term
quantized the same way) and `deck` (deal(): ids + q). Ports compare q exactly; a 1-unit difference means the
operation order drifted (see pair_scorer.py's header). `splitmix64` pins the PRNG: the first outputs per seed.
"""

import argparse
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pair_scorer as P  # noqa: E402

GOLDEN_DIR = P.HERE / "golden"
FIXTURE = GOLDEN_DIR / "index_fixture.json"
GOLDEN = GOLDEN_DIR / "golden_v1.json"
SEEDS = P.ROOT / "pairing" / "seed_pairs.json"
TOP_N = 15
DECK_N = 10
SPREAD_EVERY = 15         # fixture: every 15th family of the full index, on top of the reached ones

# Cases beyond contexts.json: the other inputs a port must honour.
EXTRA = {
    "bold_hook_seed42": {"base": "bold_hook", "seed": 42},
    "editorial_exclude_shown": {"base": "editorial_quote", "exclude": ["merriweather", "lato"],
                                "shown": [["playfairdisplay", "opensans"], ["ebgaramond", "publicsans"]]},
    "luxury_locked_support": {"base": "luxury_fashion", "locked": {"role": "support", "slug": "montserrat"},
                              "seed": 5},
    "corporate_weight_intents": {"base": "corporate_lower_third",
                                 "roles": {"display": {"intents": [700]},
                                           "support": {"intents": [400, {"weight": 400, "italic": True}]}}},
    "quiet_title_italic": {"base": "handmade_wedding",
                           "roles": {"display": {"intents": [{"stem": 0.1, "italic": True}], "caps": False},
                                     "support": {"intents": [{"stem": 0.12, "wdc": 0.84}], "caps": True}},
                           "moods": {"elegant": 1.0, "calm": 0.6}},
    "playful_list_moods": {"base": "playful_kids", "moods": ["playful", "friendly", "handmade"], "seed": 9},
}


def all_cases():
    ctxs = json.loads((P.HERE / "contexts.json").read_text())["contexts"]
    out = {}
    for name, raw in ctxs.items():
        out[name] = {k: v for k, v in raw.items() if k not in ("note", "sample")}
    for name, spec in EXTRA.items():
        raw = dict(out[spec["base"]])
        for k, v in spec.items():
            if k != "base":
                raw[k] = v
        out[name] = raw
    return out


def q(x):
    return P.quantize(x)


def run_case(ix, raw):
    ctx = P.parse_context(raw)
    ranked, rejects = P.rank(ix, ctx, with_rejects=True)
    deck = P.deal(ix, ctx, ranked, DECK_N)
    top = [[r["display"]["slug"], r["display"]["style"], r["support"]["slug"], r["support"]["style"], r["q"]]
           for r in ranked[:TOP_N]]
    terms = []
    for r in ranked[:3]:
        t = r["terms"]
        terms.append({"mood": q(t["mood"]), "fit": q(t["fit"]), "balance": q(t["balance"]),
                      "harmony": q(t["harmony"]), "quality": q(t["quality"]), "boost": q(t["boost"]),
                      "penalty": q(t["penalty"]), "floor": t["floor"], "why": r["why"],
                      "styles": [r["display"]["styles"], r["support"]["styles"]]})
    return {
        "ranked": len(ranked),
        "rejects": {k: rejects[k] for k in sorted(rejects)},
        "top": top,
        "terms": terms,
        "deck": [[r["display"]["slug"], r["support"]["slug"], r["q"]] for r in deck],
    }


def build_fixture(full, seeds, cases):
    ix = P.Index(full, seeds)
    keep = set()
    for raw in cases.values():
        ctx = P.parse_context(raw)
        ranked = P.rank(ix, ctx)
        for r in ranked[:60]:
            keep.add(r["display"]["slug"])
            keep.add(r["support"]["slug"])
        for r in P.deal(ix, ctx, ranked, DECK_N):
            keep.add(r["display"]["slug"])
            keep.add(r["support"]["slug"])
    for p in seeds["pairs"]:
        keep.add(p["display"]["slug"])
        keep.add(p["support"]["slug"])
    for i, f in enumerate(full["families"]):
        if i % SPREAD_EVERY == 0:
            keep.add(f["slug"])
    fx = {k: v for k, v in full.items() if k != "families"}
    fx["fixtureOf"] = full.get("catalogVersion")
    fx["families"] = [f for f in full["families"] if f["slug"] in keep]
    return fx


def splitmix_vectors():
    out = {}
    for seed in (0, 1, 42, 9007199254740991):
        g = P.SplitMix64(seed)
        out[str(seed)] = {"u64": [str(g.next_u64()) for _ in range(4)]}
        g = P.SplitMix64(seed)
        out[str(seed)]["double"] = [g.next_double() for _ in range(4)]
    return out


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", action="store_true")
    g.add_argument("--check", action="store_true")
    ap.add_argument("--index", default=str(P.ROOT / "r2" / "index.json"))
    args = ap.parse_args()
    seeds = json.loads(SEEDS.read_text())
    cases = all_cases()
    if args.write:
        GOLDEN_DIR.mkdir(exist_ok=True)
        full = json.loads(pathlib.Path(args.index).read_text())
        fx = build_fixture(full, seeds, cases)
        FIXTURE.write_text(json.dumps(fx, separators=(",", ":"), ensure_ascii=False) + "\n")
    fx = json.loads(FIXTURE.read_text())
    ix = P.Index(fx, seeds)
    results = {name: {"input": raw, "expect": run_case(ix, raw)} for name, raw in cases.items()}
    doc = {
        "scorer": P.SCORER_VERSION,
        "about": __doc__.strip().split("\n\n")[0],
        "index": {"file": "index_fixture.json", "sha256": sha(FIXTURE), "families": len(fx["families"]),
                  "fixtureOf": fx.get("fixtureOf")},
        "seeds": {"file": "../../../pairing/seed_pairs.json", "sha256": sha(SEEDS)},
        "topN": TOP_N, "deckN": DECK_N,
        "splitmix64": splitmix_vectors(),
        "cases": results,
    }
    if args.write:
        GOLDEN.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        print("wrote %s (%d cases, fixture %d families, %d KB)" % (GOLDEN, len(results), len(fx["families"]),
                                                                  FIXTURE.stat().st_size // 1024))
        return 0
    want = json.loads(GOLDEN.read_text())
    bad = []
    if want["seeds"]["sha256"] != doc["seeds"]["sha256"]:
        bad.append("seed_pairs.json changed since the goldens were written (re-run --write and review)")
    if want["splitmix64"] != doc["splitmix64"]:
        bad.append("splitmix64 vectors differ")
    for name, case in want["cases"].items():
        got = doc["cases"].get(name)
        if got is None or got["expect"] != case["expect"]:
            bad.append("case %s differs" % name)
    for b in bad:
        print("FAIL", b)
    print("%d cases, %s" % (len(want["cases"]), "OK" if not bad else "%d failures" % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
