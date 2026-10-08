# Font index — schema v1 (the pair-scorer contract)

`https://library.motionmix.app/fonts/index.json` (short cache), or the
immutable copy the manifest names in `index.file` (`index/<sha256>.json`).
Built by `tools/build_index.py` from the R2 catalog (ADR 125 Am. 2). One
record per catalog family, one face record per shipped style (for a variable
family, per named-instance style). ~1.6 MB raw, ~220 KB gzipped (Cloudflare
serves it gzipped).

**Join with the manifest** by `slug` (family) and `style` (face = the
manifest style `name`). The manifest owns files, hashes and licences; the
index owns facts, tags, metrics and fitness. `catalogVersion` must match the
manifest's — a mismatch means a stale pair; re-fetch both. Score only the
families the app's manifest parse offered: a family with `minEngine` above
the app's font-engine level is in the index but not in the app's catalog.

Compatibility: fields may be ADDED within schemaVersion 1; a reader ignores
unknown fields. Renames/removals bump `schemaVersion`. Any value below may be
`null` when it could not be measured or Google publishes nothing for it.

## Top level

| Field | Meaning |
|---|---|
| `schemaVersion` | `1` |
| `catalogVersion` | equals `manifest.catalogVersion` |
| `generated` | UTC build time |
| `fitVersion` | the rule set behind `fit` (`"rules-1"`) |
| `measureVersion` | the metric definitions below (`1`) |
| `moods` | the 20 Google "Expressive" tag names, in the order of every face's `moods` array |
| `tagNames` | dictionary for family `tags` (full GF tag paths, e.g. `/Serif/Didone`) |
| `pairs` | the hand-checked seed pairs (below), in file order |
| `families` | records below |

## Family

| Field | Meaning |
|---|---|
| `slug`, `name` | as in the manifest |
| `category` | `sans-serif` · `serif` · `display` · `handwriting` · `monospace` (Google's) |
| `classifications` | Google site classifications, lower-case (`display`, `handwriting`, `monospace`) |
| `stroke` | Google site stroke: `sans-serif` · `serif` · `slab-serif` · `null` |
| `skeleton` | the strongest family-level classification tag — the Font Matrix skeleton, e.g. `Sans/Neo Grotesque`, `Serif/Didone`, `Script/Formal`, `Slab/Clarendon`, `Monospace/Monospace` |
| `designers` | list |
| `license` | `OFL-1.1` · `Apache-2.0` · `UFL-1.0` |
| `rfn` | modified derivatives may not use the name (see README) |
| `popularity`, `trending` | Google Fonts ranks (1 = most), public site metadata. A weak prior only — popularity is a trap as a pairing signal |
| `dateAdded` | ISO date the family joined Google Fonts (era proxy) |
| `subsets` | Google subsets the family supports |
| `primaryScript` | ISO 15924 code for non-Latin-first families (`Deva`, `Arab`…), else `null` |
| `axes` | the UPSTREAM variable axes `[tag, min, max]` — informational (a `minEngine: 2` family ships that variable file; its faces are named instances) |
| `weights` | shipped upright weights |
| `quality` | mean of Google's four `/Quality/*` tags (0–100); the catalog gate is ≥ 67.5 |
| `tags` | `[[tagNames index, score 0–100], …]` — family-level classification, theme, seasonal, purpose, quality tags |
| `cov` | measured coverage per subset, 0–1: the share of a representative assigned-code-point set present in the cmap (Regular-most face). Fractions below 1 for `*-ext` subsets are normal |
| `minEngine` | present (= 2) only on a family the manifest serves as a variable file at named instances (ADR 125 Am. 3); absent = 1 |
| `faces` | below |

## Face

| Field | Meaning |
|---|---|
| `style`, `weight`, `italic` | as in the manifest |
| `bytes` | size of the file the face downloads (uncompressed; for a variable face the whole variable file, shared by the family's faces of that italic-ness) — lets the scorer avoid multi-MB CJK faces when prefetching |
| `moods` | 20 ints, 0–100, aligned with top-level `moods`. Google scores some moods per weight (`wght@100/400/900`…): the value is interpolated linearly at this face's weight and clamped to the end points outside the assessed range; width-axis rows use the width closest to 100. A mood with no per-weight rows uses the family-level score; absent in Google's data = 0 |
| `m` | measured metrics (below) |
| `fit` | `{display, support, caption}`, 0–1 |

### Metrics (`m`) — from outlines, never from OS/2 fields

Lengths are in em (font units / unitsPerEm). Fill rule non-zero, so
overlapping contours measure as one shape. A variable face is measured at its
instance's `coordinates` (in memory; advances from the varied glyphs).

| Key | Definition |
|---|---|
| `upm`, `glyphs` | unitsPerEm, glyph count |
| `cap` | top of `H` (else `I`, `E`, `L`) |
| `xh` | top of `x` (else `z`, `v`, `w`) |
| `xr` | `xh / cap` — the pairing "harmony" anchor (typical 0.70–0.76) |
| `asc`, `desc` | highest of `d h k l b`, lowest of `p q g y j` (desc < 0) |
| `caseless` | no true lowercase: `p` has no descender, or `xr ≥ 0.9` (caps-only and small-caps faces — Bebas Neue, Amatic SC, Cinzel) |
| `wd` | mean advance of "Hamburgefonstiv" |
| `wdc` | `wd / cap` — width at matched cap height (condensed < 0.62, typical 0.72–0.82, extended > 1.0) |
| `stem` | mean of `H`'s two vertical stems, cut at 30 % of cap height (else `I` / `l`) |
| `stemc` | `stem / cap` — stroke weight at matched size (Regular ~0.10–0.14, Bold ~0.20–0.25, Black ~0.24–0.34) |
| `contrast` | thinnest horizontal stroke ÷ thickest vertical stroke of `o` (1 = monoline; Didone ~0.1–0.3) |
| `ink` | inked area of "Hamburgefonstiv" ÷ (its advance × cap) — typographic colour |
| `overlap` | share of A–Z a–z 0–9 drawn with overlapping contours (outline-stroke joins; ADR 125 research defect 2) |

### Fitness (`fit`, `fitVersion: "rules-1"`)

Rule-based, unvalidated starting weights — tune on a review board, then on
usage; a change bumps `fitVersion`. `band(x, lo, hi, soft)` is 1 inside
`[lo, hi]` and falls linearly to 0 at `lo − soft` / `hi + soft`.

- **display** (titles, hooks) = 0.4·expressive + 0.3·prior + 0.2·presence + 0.1·quality, ×0.95 italic.
  expressive = mean of the 3 highest of Active, Artistic, Childlike, Cute,
  Excited, Fancy, Futuristic, Happy, Innovative, Loud, Playful, Rugged,
  Sophisticated, Vintage. prior by category (display 1.0, handwriting 0.85,
  serif 0.6, sans 0.45, mono 0.35), raised to 0.9 by a Theme tag ≥ 50 or a
  Didone / Fat Face / Formal-script skeleton, to 0.85 when `caseless`, to 0.7
  when condensed/extended. presence = max(weight ramp on `stemc`, 1 − contrast).
- **support** (subheads, body lines) = 0.25·band(xr, .66, .80, .12) +
  0.2·band(contrast, .4, 1, .25) + 0.25·weight fit (400 best, 900 0.25) +
  0.1·breadth (1 weight 0.4, 2 0.7, 3+ 1) + 0.1·neutral moods (Business, Calm,
  Competent, Sincere) + 0.1·quality; then ×(0.6 + 0.4·band(wdc, .65, .95, .2))
  ×(0.6 + 0.4·band(stemc, .08, .2, .08)), ×0.8 if contrast < 0.3, × category
  (sans 1, serif 0.95, mono 0.75, display 0.45, handwriting 0.3), ×0.3
  caseless, ×0.7 italic.
- **caption** (small text over video) = 0.35·band(xr, .70, .82, .1) +
  0.3·band(contrast, .6, 1, .3) + 0.2·weight fit (500 best, Bold 0.75) +
  0.15·quality; ×(0.5 + 0.5·band(wdc, .72, 1, .2)) ×(0.5 + 0.5·band(stemc,
  .10, .21, .07)); +0.1 for `/Purpose/Easy Reading`; × category (sans 1,
  serif 0.85, mono 0.8, display 0.5, handwriting 0.25), ×0.2 caseless, ×0.6
  italic.

Reference points (rules-1): Inter Regular d .45 / s .94 / c .95 · Playfair
Display Regular d .73 / s .62 / c .56 · Bebas Neue d .63 / s .14 / c .07 ·
Lobster d .86 / s .33 / c .21 · Atkinson Hyperlegible Regular c .95.

## Pairs (ADR 278 §8)

`pairs` carries `pairing/seed_pairs.json` verbatim — the hand-checked pairs
the scorer treats as a quality floor and boost, matched by (display slug,
support slug):

```json
{"display": {"slug": "anton", "name": "Anton", "style": "Regular"},
 "support": {"slug": "archivo", "name": "Archivo", "style": "Bold"},
 "moods": ["bold", "cinematic"], "why": "…"}
```

`style` is the reviewed face (slots still map by their own intents); `moods`
are creator moods (`CREATOR_MOODS` in `tools/pairing/pair_scorer.py`). The
build fails if a pair names a face the index lacks. Learned per-mood boosts
(ADR 278 §9) will be added to these records as new fields.

## Example (abridged)

```json
{"slug":"inter","name":"Inter","category":"sans-serif","classifications":[],"stroke":"sans-serif",
 "skeleton":"Sans/Neo Grotesque","designers":["Rasmus Andersson"],"license":"OFL-1.1","rfn":false,
 "popularity":5,"trending":957,"dateAdded":"2020-01-24","subsets":["cyrillic","greek","latin","…"],
 "primaryScript":null,"axes":[["opsz",14,32],["wght",100,900]],"weights":[400,500,700,900],
 "quality":77.5,"tags":[[2,70],[3,70],[4,90],[5,80],[6,20],[9,10],[10,100]],
 "faces":[{"style":"Regular","weight":400,"italic":false,"bytes":324820,
   "moods":[0,0,5,85,80,0,91,0,0,0,5,0,0,50,1,4,0,0,20,37],
   "m":{"upm":2048,"glyphs":2871,"cap":0.728,"xh":0.546,"xr":0.75,"asc":0.728,"desc":-0.216,
        "caseless":false,"wd":0.545,"wdc":0.749,"stem":0.093,"stemc":0.128,"contrast":0.88,
        "ink":0.292,"overlap":0.34},
   "fit":{"display":0.452,"support":0.941,"caption":0.946}}],
 "cov":{"cyrillic":1.0,"latin":1.0,"latin-ext":0.99,"vietnamese":1.0}}
```

## Provenance and limits

- Tags: `github.com/google/fonts/tags/all/families.csv` (human-assessed; the
  method is undocumented and the CSV format may change — google/fonts #9647,
  #10799). Ranks and classifications: `fonts.google.com/metadata/fonts`
  (public, unofficial). No CC BY-NC data (the O'Donovan 2014 attribute set and
  models trained on it) is used.
- Not yet in v1: engine-rendered specimen embeddings (`balance`, research
  P3), the Font Matrix "flesh" axis, learned pair boosts. They will be added
  as new fields.
