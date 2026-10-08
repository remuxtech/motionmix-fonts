# motionmix-fonts

The MotionMix hosted font catalog (ADR 125). Two rungs are live:

| Rung | Base URL | Catalog | Status |
|---|---|---|---|
| **R2 — own bucket** | `https://library.motionmix.app/fonts` | `r2/manifest.json`: 1,133 families (33 of them variable, `minEngine: 2`), 2,709 styles from 2,624 files, 1.12 GB + `r2/index.json` (pair-scorer index + seed pairs) | current — new apps read it |
| R1 — jsDelivr | `https://cdn.jsdelivr.net/gh/remuxtech/motionmix-fonts@<ref>` | `manifest.json` at the repo root, 150 families, static files only | frozen; shipped apps still read `@main` |

Both manifests use the same `schemaVersion: 1` shape (every newer field is
additive), and every path in them is relative to the base URL — so moving an
app from R1 to R2 is a base-URL change plus parsing the `variable` styles
(desktop honours `-Dmotionmix.fonts.url`; point it at `file:///…` for local
mirrors). R1 never changes: the 12 families R2 now serves as variable files
keep their Fonts-API statics there for the apps already shipped.

## R2 layout (bucket `motionmix-library`, prefix `fonts/`)

```
fonts/manifest.json            catalog manifest          public, max-age=300
fonts/index.json               pair-scorer index         public, max-age=300
fonts/index/<sha256>.json      the same index, immutable public, max-age=31536000, immutable
fonts/files/<sha256>.ttf       font files, static or variable (immutable)
fonts/previews/<sha256>.ttf    name-subset preview faces (immutable)
fonts/licenses/<sha256>.txt    licence texts             (immutable)
```

Everything except the two entry files is content-addressed and never
changes or goes away (a retired file stays for documents that pin it). Cloudflare gzips `font/ttf` on the wire (Inter Regular 325 → 153 KB)
when the client sends `Accept-Encoding: gzip`. Fetch with a non-library
User-Agent (the zone's Browser Integrity Check refuses e.g. Python's default).

### Manifest (`schemaVersion: 1`)

Top level: `catalog` (`"r2"`), `catalogVersion`, `generated`, `sources`
(the google/fonts commit), `index` (`{file, sha256, bytes}` of the immutable
index), `featured` (ordered names), `families`.

Per family: `name · slug · category · subsets · license` (SPDX:
`OFL-1.1` / `Apache-2.0` / `UFL-1.0`, derived from the licence text) `·
licenseFile · rfn · reservedFontNames? · origin · minEngine? · designer? ·
preview? · styles[…]`. A style is static —
`{name, weight, italic, file, bytes, sha256, replaces?}` — or, ADR 125 Am. 3,
a named instance of a variable file:

```json
{"name": "Bold", "weight": 700, "italic": false,
 "variable": {"file": "files/<sha256>.ttf", "bytes": 312352, "sha256": "<sha256>",
              "instance": "Bold", "postscriptName": "RalewayRoman-Bold", "coordinates": {"wght": 700}},
 "replaces": ["<the retired Fonts-API static's sha256>"]}
```

- A variable style has NO top-level `file` / `sha256`: a v1 parser skips it
  and drops a family left without styles, so an app that cannot render a
  named instance never offers the family. Several styles share one file; the
  app registers that file at `coordinates` (every axis of the instance)
  under the style's pair name, verified by `variable.sha256`.
- `minEngine` (family; default 1) — the lowest font-engine level that renders
  the family: 1 static, 2 a variable file at a named instance. Every family
  with a variable style carries `minEngine: 2`.
- `instance` is the font's own `fvar` instance name; `postscriptName` is the
  instance's `fvar` PostScript name, else the engine's fallback
  `<Family>-<Subfamily>` with spaces removed.
- `replaces` lists earlier sha256s of the same style (a template that pinned
  one heals to the current file — for a variable style, the Fonts-API static
  it retired).

- `rfn: true` — a *modified* derivative of this family may not use its name
  (an OFL Reserved Font Name that reaches the family name, or any UFL
  family). Its files are served unmodified; its preview is renamed (below).
- `origin` — `gf-repo`: the TTF from github.com/google/fonts,
  byte-identical (static, or the variable file of a `minEngine: 2` family);
  `gf-api`: Google's static instance from the Fonts API (Google-processed:
  subset glyphs, trimmed name table) — variable-only families without a
  binding RFN, and the R1 families without `rfn`. On R1 the 20 `rfn`
  families with static originals live at `fonts/<slug>/upstream/`.

The index schema (the contract for the studio pair scorer) is in
[`INDEX.md`](INDEX.md).

### Font pairing (ADR 278)

`tools/pairing/pair_scorer.py` is the reference pair scorer (stdlib only, written to port 1:1 to Kotlin):
`pair_scorer.py --context bold_hook --deck 12` ranks pairs for a template context and deals a Shuffle deck.
`contexts.json` holds the tuning contexts, `golden.py --check` verifies the port's golden vectors
(`golden/golden_v1.json` over `golden/index_fixture.json`), and `specimens.py --cache <dir> --out <dir>` (needs
Pillow) renders contact sheets of a deck from the real font files. `pairing/seed_pairs.json` holds the
hand-checked pairs the scorer uses as a quality floor; `build_index.py` folds them into the index as `pairs`
(ADR 278 §8), which is what the scorer reads unless `--seeds <file>` is given. A constant change bumps
`SCORER_VERSION`: look at the sheets, then `golden.py --write`.

### What is in the R2 catalog

Google Fonts families with an allowed licence, minus Symbols / Special use /
Learn-to-Write / colour fonts, minus Noto except the shipped per-script shelf,
minus the bottom quartile on Google's quality tags; the 150 R1 families always
stay (same bytes, except the 12 below). Static files, except the variable-only
families whose modified derivatives must drop the name (below): those ship
the unmodified variable file, one style per named instance (the engine
renders a named instance since ADR 125 Am. 3).
Weight policy: ≤4 uprights (400/700/500/900, or the nearest weight within
±100) + one italic near 400 for sans / serif / mono; a family whose Regular is
over 4 MB (CJK) ships Regular + Bold. A variable family takes the same policy
over its named instances (the instance the font names after the style, e.g.
Encode Sans "Regular" at wdth 100, else the one at wght with every other axis
at its default). Every refused family is in `r2/excluded.json` with its reason.

## Licensing

Only OFL-1.1, Apache-2.0 and UFL-1.0 families are hosted. Every family's
licence text is published (`licenseFile`) and must travel with its files
(also inside exported `.mmproject` / `.mmtemplate` bundles).

- Font files are never modified by us.
- **Previews** (`previews/`, and `fonts/<slug>/preview.ttf` on R1) are
  subsets, i.e. OFL/UFL Modified Versions: every preview carries the neutral
  name `MotionMix Preview` (PostScript `MotionMixPreview-<sha8>`), never the
  family's (Reserved) name; the source copyright and licence name records are
  kept.
- `rfn` families are served as unmodified files. The 33 variable-only ones
  (no unmodified static upstream) ship their unmodified variable files with
  `minEngine: 2` since catalog 2026.10.08.3: the 12 R1 had shipped as
  Fonts-API statics (Raleway, Playfair Display, Lora, Merriweather, …;
  their old sha256s are each style's `replaces`, and R1 keeps serving the
  statics) and 21 that were excluded until then (IBM Plex Sans, Source Sans 3,
  Lexend, …). `r2/variable_pending.json` is the record of that switch.
  Instancing happens only in memory, for the index metrics and the
  neutrally named preview; no instanced file is distributed.
  `r2/excluded.json` lists every refused family.

Rules and the decision record: ADR 125 Amendments 2 and 3
(`planning/motionmix_master/decisions/125_font_system_standard.md` in the
workspace).

## Building and publishing

Needs a sparse checkout of google/fonts (metadata, licences, tags; the
builder adds the font files it needs) and the site metadata JSON:

```
git clone --filter=blob:none --no-checkout --depth 1 --sparse https://github.com/google/fonts.git gf
git -C gf sparse-checkout set --no-cone '/ofl/*/METADATA.pb' '/apache/*/METADATA.pb' '/ufl/*/METADATA.pb' \
    '/ofl/*/OFL.txt' '/apache/*/LICENSE.txt' '/ufl/*/UFL.txt' '/tags/all/*'
git -C gf checkout main
curl -A curl/7.64 -o gf_metadata.json https://fonts.google.com/metadata/fonts

.venv/bin/python tools/build_catalog.py --gf-repo gf --site-metadata gf_metadata.json --build <dir> --version <yyyy.mm.dd.n>
.venv/bin/python tools/build_index.py   --gf-repo gf --site-metadata gf_metadata.json --build <dir>
.venv/bin/python tools/pairing/golden.py --check
.venv/bin/python tools/publish_r2.py    --build <dir>      # outside the agent sandbox; BEFORE committing r2/
git add r2 && git commit                                   # r2/manifest.json = what is published
```

Pin the checkout to the manifest's `sources.google/fonts` commit to reproduce
a catalog byte for byte. `build_catalog.py --only "A,B,…"` rebuilds just those
families and merges them into `r2/manifest.json` / `r2/excluded.json` (how
2026.10.08.3 switched the 33 variable families); only their files have to be
in `<dir>`. `build_index.py` reuses the published index's metrics for every
face it already measured (git HEAD `r2/index.json` + `r2/manifest.json`), so
it needs only the NEW faces' files; `--no-reuse` measures all.

`publish_r2.py` uploads only objects the committed `r2/manifest.json` does
not already reference (so run it before committing the new manifest),
uploads `manifest.json` last, then fetches every uploaded object back from
the CDN (status, sha256 = name, immutable cache) and spot-checks sampled
families. Nothing is ever deleted from the bucket. It runs wrangler with the
account's OAuth login
(`NODE_OPTIONS=--dns-result-order=ipv4first npx -y wrangler@4 r2 bulk put …`).

The frozen R1 catalog is still rebuilt by `tools/build_mirror.py` (Google
Fonts API statics; weight policy ≤4 uprights from 400/500/700/900 + 400
italic for text categories).
