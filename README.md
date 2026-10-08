# motionmix-fonts

The MotionMix hosted font catalog (ADR 125). Two rungs are live:

| Rung | Base URL | Catalog | Status |
|---|---|---|---|
| **R2 — own bucket** | `https://library.motionmix.app/fonts` | `r2/manifest.json`: 1,112 families, 2,622 static TTFs, 1.10 GB + `r2/index.json` (pair-scorer index) | current |
| R1 — jsDelivr | `https://cdn.jsdelivr.net/gh/remuxtech/motionmix-fonts@<ref>` | `manifest.json` at the repo root, 150 families | frozen; shipped apps still read `@main` |

Both manifests use the same `schemaVersion: 1` shape (every newer field is
additive), and every path in them is relative to the base URL — so moving an
app from R1 to R2 is a base-URL change only (desktop honours
`-Dmotionmix.fonts.url`; point it at `file:///…` for local mirrors).

## R2 layout (bucket `motionmix-library`, prefix `fonts/`)

```
fonts/manifest.json            catalog manifest          public, max-age=300
fonts/index.json               pair-scorer index         public, max-age=300
fonts/index/<sha256>.json      the same index, immutable public, max-age=31536000, immutable
fonts/files/<sha256>.ttf       font files                (immutable)
fonts/previews/<sha256>.ttf    name-subset preview faces (immutable)
fonts/licenses/<sha256>.txt    licence texts             (immutable)
```

Everything except the two entry files is content-addressed and never
changes. Cloudflare gzips `font/ttf` on the wire (Inter Regular 325 → 153 KB)
when the client sends `Accept-Encoding: gzip`. Fetch with a non-library
User-Agent (the zone's Browser Integrity Check refuses e.g. Python's default).

### Manifest (`schemaVersion: 1`)

Top level: `catalog` (`"r2"`), `catalogVersion`, `generated`, `sources`
(the google/fonts commit), `index` (`{file, sha256, bytes}` of the immutable
index), `featured` (ordered names), `families`.

Per family: `name · slug · category · subsets · license` (SPDX:
`OFL-1.1` / `Apache-2.0` / `UFL-1.0`, derived from the licence text) `·
licenseFile · rfn · reservedFontNames? · origin · designer? · preview? ·
styles[{name, weight, italic, file, bytes, sha256, replaces?}]`. `replaces`
lists earlier sha256s of the same style (a template that pinned one heals to
the current file).

- `rfn: true` — a *modified* derivative of this family may not use its name
  (an OFL Reserved Font Name that reaches the family name, or any UFL
  family). Its files are served unmodified; its preview is renamed (below).
- `origin` — `gf-repo`: the static TTF from github.com/google/fonts,
  byte-identical; `gf-api`: Google's static instance from the Fonts API
  (Google-processed: subset glyphs, trimmed name table) — variable-only
  families and the R1 families without `rfn`. On R1 the 20 `rfn` families
  with static originals live at `fonts/<slug>/upstream/`.

The index schema (the contract for the studio pair scorer) is in
[`INDEX.md`](INDEX.md).

### What is in the R2 catalog

Google Fonts families with an allowed licence, minus Symbols / Special use /
Learn-to-Write / colour fonts, minus Noto except the shipped per-script shelf,
minus the bottom quartile on Google's quality tags; the 150 R1 families always
stay (same bytes). Static files only — the engine renders a variable font's
default instance only (`SkFontMgr::makeFromData`, no `SkFontArguments`).
Weight policy: ≤4 uprights (400/700/500/900, or the nearest weight within
±100) + one italic near 400 for sans / serif / mono; a family whose Regular is
over 4 MB (CJK) ships Regular + Bold. Every refused family is in
`r2/excluded.json` with its reason.

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
- `rfn` families are served as unmodified files. The 12 variable-only ones
  R1 already shipped (Raleway, Playfair Display, Lora, Merriweather, …) stay
  on their Fonts-API statics for now, and 21 more are not added (IBM Plex
  Sans, Source Sans 3, Lexend, …): no unmodified static exists, and the
  engine renders only a variable font's default instance today. Once it
  renders named instances, all 33 switch to their unmodified variable files —
  `r2/variable_pending.json` lists them (upstream paths, axes, named
  instances, target styles). `r2/excluded.json` lists every refused family.

Rules and the decision record: ADR 125 Amendment 2
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

.venv/bin/python tools/build_catalog.py --gf-repo gf --site-metadata gf_metadata.json --build <dir>
.venv/bin/python tools/build_index.py   --gf-repo gf --site-metadata gf_metadata.json --build <dir>
.venv/bin/python tools/publish_r2.py    --build <dir>      # outside the agent sandbox
git add r2 && git commit                                   # r2/manifest.json = what is published
```

`publish_r2.py` uploads only objects the committed `r2/manifest.json` does
not already reference, uploads `manifest.json` last, and spot-checks the CDN
(sha256 of sampled files). It runs wrangler with the account's OAuth login
(`NODE_OPTIONS=--dns-result-order=ipv4first npx -y wrangler@4 r2 bulk put …`).

The frozen R1 catalog is still rebuilt by `tools/build_mirror.py` (Google
Fonts API statics; weight policy ≤4 uprights from 400/500/700/900 + 400
italic for text categories).
