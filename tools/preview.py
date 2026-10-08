"""
Name-subset preview faces (ADR 125 §2.4; RFN policy ADR 125 Am. 2).

A preview is the family's Regular-most style cut down to exactly the glyphs
of its own name (~1–2 KB) so the picker can title each row in its own face
without downloading the font.

A subset is a MODIFIED VERSION under the OFL (FAQ 2.2/2.6) and under the UFL,
so no preview may carry the family's name: every preview gets the SAME
neutral name table — family "MotionMix Preview", PostScript
"MotionMixPreview-<8 hex of the source file's sha256>" — never a Reserved
Font Name. The source's copyright notice (name ID 0) and licence description
/ URL (IDs 13 / 14) are kept, so each file still carries its notice; the
family's full licence text ships beside it (manifest `licenseFile`).

A variable source (ADR 125 Am. 3) is instanced in memory at the style's
named-instance coordinates first, so the preview shows that style rather than
the file's default instance (often Thin); the catalog's font file itself is
never instanced.
"""

import hashlib

from fontTools import subset as ft_subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

PREVIEW_FAMILY = "MotionMix Preview"
_KEEP_FROM_SOURCE = (0, 13, 14)       # copyright, licence description, licence URL


def _source_names(src_font):
    names = {}
    for rec in src_font["name"].names:
        if rec.nameID in _KEEP_FROM_SOURCE and rec.nameID not in names:
            try:
                names[rec.nameID] = rec.toUnicode()
            except Exception:  # noqa: BLE001 — undecodable legacy record
                continue
    return names


def make_preview(src_path, text, out_path, location=None):
    """Subset `src_path` to the glyphs of `text`, write a neutrally named
    preview to `out_path`. Returns the written bytes. `location` ({axis:
    value}) instances a variable source there first (unnamed axes at their
    defaults)."""
    src_bytes = open(src_path, "rb").read()
    tag = hashlib.sha256(src_bytes).hexdigest()[:8]
    src_font = TTFont(src_path, lazy=True)
    kept = _source_names(src_font)
    src_font.close()

    options = ft_subset.Options()
    options.layout_features = []          # no shaping machinery
    options.name_IDs = []                 # rebuilt below — never the family's own name
    options.hinting = False
    options.notdef_outline = False
    options.drop_tables += ["STAT", "fvar", "avar", "gvar", "HVAR", "MVAR", "DSIG", "meta"]
    font = ft_subset.load_font(src_path, options)
    if location and "fvar" in font:
        loc = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
        loc.update({k: float(v) for k, v in location.items()})
        instancer.instantiateVariableFont(font, loc, inplace=True)
    subsetter = ft_subset.Subsetter(options=options)
    subsetter.populate(text=text)
    subsetter.subset(font)

    name = font["name"]
    name.names = []
    ps = f"MotionMixPreview-{tag}"
    records = {
        1: PREVIEW_FAMILY,
        2: "Regular",
        3: f"{PREVIEW_FAMILY} {tag}",
        4: f"{PREVIEW_FAMILY} {tag}",
        5: "Version 1.000",
        6: ps,
    }
    for name_id, value in kept.items():
        records[name_id] = value
    for name_id, value in sorted(records.items()):
        name.setName(value, name_id, 3, 1, 0x409)          # Windows / Unicode BMP / en-US
        if all(ord(c) < 128 for c in value):
            name.setName(value, name_id, 1, 0, 0)          # Mac Roman (ASCII only)
    if "CFF " in font:
        font["CFF "].cff.fontNames = [ps]
    ft_subset.save_font(font, out_path, options)
    font.close()
    return open(out_path, "rb").read()


def preview_is_neutral(path):
    """True when an existing preview already carries the neutral name table."""
    try:
        font = TTFont(path, lazy=True)
        fam = font["name"].getDebugName(1)
        font.close()
        return fam == PREVIEW_FAMILY
    except Exception:  # noqa: BLE001
        return False
