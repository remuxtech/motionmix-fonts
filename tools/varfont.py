"""
Variable fonts served at their named instances (ADR 125 Am. 3).

A family whose modified derivatives must drop its name (binding OFL Reserved
Font Name, or UFL) and that google/fonts ships ONLY as variable TTFs is
served as those UNMODIFIED variable files: every catalog style is one of the
file's own named instances (`fvar`), and the manifest style carries

    "variable": {"file", "bytes", "sha256", "instance", "postscriptName", "coordinates"}

with no top-level `file` / `sha256`, plus `minEngine: 2` on the family, so
an app that cannot render a variable instance never offers it.

Instancing happens here only in memory, for MEASUREMENT (tools/measure.py)
and for the neutrally named preview subset (tools/preview.py) — an instanced
font file is never written to the catalog.
"""

import hashlib
import pathlib

from fontTools.ttLib import TTFont

import gfmeta

ENGINE_LEVEL_VARIABLE = 2      # manifest `minEngine` of a family with any variable style


def repo_files(rec, variable):
    """[(weight, italic, repo-relative path)] of a family's static (variable=False)
    or variable (variable=True) TTFs, from METADATA.pb."""
    out = []
    for f in rec["meta"].get("fonts", []):
        fn = gfmeta.first(f, "filename", "")
        if fn.lower().endswith(".ttf") and ("[" in fn) == variable:
            out.append((int(gfmeta.first(f, "weight", 400)), gfmeta.first(f, "style", "normal") == "italic",
                        f"{rec['dir']}/{fn}"))
    return out


def variable_paths(rec):
    """{repo-relative path: italic} of a family's variable TTFs (one per italic-ness)."""
    out = {}
    for _, italic, rel in repo_files(rec, variable=True):
        out.setdefault(rel, italic)
    return out


def _num(v):
    """400.0 → 400 (an integral design coordinate prints as an integer)."""
    v = round(float(v), 3)
    return int(v) if v == int(v) else v


def vf_record(path, rel, italic):
    """One variable file → {path, italic, bytes, sha256, axes, namedInstances}.
    A named instance without a PostScript name in `fvar` gets `postscriptName:
    null` here; see postscript_name() for the catalog's value."""
    data = pathlib.Path(path).read_bytes()
    font = TTFont(path, lazy=True)
    name = font["name"]
    axes = [{"tag": a.axisTag, "min": a.minValue, "default": a.defaultValue, "max": a.maxValue}
            for a in font["fvar"].axes]
    family = name.getDebugName(16) or name.getDebugName(1)
    instances = []
    for inst in font["fvar"].instances:
        ps = name.getDebugName(inst.postscriptNameID) if inst.postscriptNameID not in (None, 0xFFFF) else None
        instances.append({"name": name.getDebugName(inst.subfamilyNameID), "postscriptName": ps,
                          "coordinates": {k: round(v, 3) for k, v in inst.coordinates.items()}})
    font.close()
    return {"path": rel, "italic": italic, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "family": family, "axes": axes, "namedInstances": instances}


def postscript_name(record, instance):
    """The instance's own fvar PostScript name, else the engine's fallback for an
    unnamed instance (FontLibrary): `<typographic family>-<subfamily>`, spaces removed."""
    if instance.get("postscriptName"):
        return instance["postscriptName"]
    return (record["family"] or "").replace(" ", "") + "-" + (instance["name"] or "").replace(" ", "")


def instance_for(records, weight, italic, name=None):
    """The named instance for a catalog style, in the file of matching italic-ness
    → (record, instance) or None:
      1. the instance whose own name IS the style name (`name`, e.g. "Regular",
         "Bold Italic") at wght = `weight` — Encode Sans names its wdth-100
         instances "Regular" … beside "Condensed Regular" at the fvar default;
      2. else the instance at wght = `weight` with every other axis at its default."""
    for rec in records:
        if rec["italic"] != italic or not name:
            continue
        for inst in rec["namedInstances"]:
            if inst["name"] == name and inst["coordinates"].get("wght") == weight:
                return rec, inst
    for rec in records:
        if rec["italic"] != italic:
            continue
        defaults = {a["tag"]: a["default"] for a in rec["axes"]}
        for inst in rec["namedInstances"]:
            c = inst["coordinates"]
            if c.get("wght") == weight and all(c.get(t, d) == d for t, d in defaults.items() if t != "wght"):
                return rec, inst
    return None


def available_weights(records, style_name):
    """(uprights, italics): the weights 100…900 with a named instance per instance_for()."""
    ups, its = set(), set()
    for w in range(100, 1000, 100):
        for i in (False, True):
            if instance_for(records, w, i, style_name(w, i)):
                (its if i else ups).add(w)
    return ups, its


def variable_style(name, weight, italic, record, instance):
    """The manifest style for one named instance of an unmodified variable file."""
    sha = record["sha256"]
    return {"name": name, "weight": weight, "italic": italic,
            "variable": {"file": f"files/{sha}.ttf", "bytes": record["bytes"], "sha256": sha,
                         "instance": instance["name"], "postscriptName": postscript_name(record, instance),
                         "coordinates": {k: _num(v) for k, v in instance["coordinates"].items()}}}


def style_source(style):
    """A manifest style → (file, sha256, bytes, coordinates or None), static or variable."""
    v = style.get("variable")
    if v:
        return v["file"], v["sha256"], v["bytes"], v["coordinates"]
    return style["file"], style["sha256"], style["bytes"], None


def face_key(style):
    """A face's identity for caches: the file sha256, plus `@axis=value,…` for a variable instance."""
    _, sha, _, coords = style_source(style)
    if not coords:
        return sha
    return sha + "@" + ",".join(f"{k}={coords[k]}" for k in sorted(coords))


def full_location(font, coords):
    """Every fvar axis pinned: the given coordinates, the default for the rest."""
    loc = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
    loc.update({k: float(v) for k, v in (coords or {}).items()})
    return loc
