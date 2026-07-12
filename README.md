# motionmix-fonts

The MotionMix hosted font catalog (ADR 125, hosting rung R1): a curated,
version-pinned mirror of ~100 Google Fonts families as static TTFs, served as
plain static files via jsDelivr:

```
https://cdn.jsdelivr.net/gh/remuxtech/motionmix-fonts@<ref>/manifest.json
https://cdn.jsdelivr.net/gh/remuxtech/motionmix-fonts@<ref>/fonts/<slug>/<file>
```

The app reads ONE base URL (config; desktop honors `-Dmotionmix.fonts.url`
for local mirrors — point it at `file:///…/motionmix-fonts` in dev). All
manifest paths are repo-relative, so moving hosts (rung R2: own bucket/CDN)
is a base-URL change only. `schemaVersion` gates parsing — bump it when the
shape changes and keep serving v1 until shipped apps migrate.

## Contents

- `manifest.json` — schemaVersion 1: per family `name / slug / category /
  license / licenseFile / styles[{name, weight, italic, file, bytes, sha256}]`.
- `fonts/<slug>/` — the family's TTFs + its license text (`LICENSE.txt`).
- `tools/build_mirror.py` — the builder: curated list → Google css2 (static
  per-weight TTFs, works for variable-only families too) + license from
  google/fonts → manifest. Idempotent; `--only`, `--limit`, `--force`.

## Licensing

Every family is OFL / Apache-2.0 / UFL (self-hosting, app-embedding, and
redistribution WITH the license text are permitted; selling font files
standalone is not). Each family directory carries its license; keep it when
adding families — the builder fails a family whose license can't be fetched.

## Updating

```
python3 tools/build_mirror.py          # refresh / add curated families
git add -A && git commit && git tag vN # pin a release; apps reference @tag
```

Weight policy: available ∩ {400, 500, 700, 900} upright (≤4), + 400-italic
for body categories — bounded so the repo stays CDN-friendly (~45 MB).
