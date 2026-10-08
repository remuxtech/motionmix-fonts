#!/usr/bin/env python3
"""Font pair scorer — the reference implementation (ADR 278, scorer version "pair-1").

Given the font index (INDEX.md, schema v1), the hand-checked seed pairs (pairing/seed_pairs.json) and a template
context, rank (display, support) font pairs and deal a Shuffle deck. Pure stdlib. Written to port 1:1 to Kotlin:

  * Every number is an IEEE double; every constant is an explicit literal below. No transcendental functions
    (no log/exp/pow/sqrt), only + - * / min max abs, so Python and the JVM produce bit-identical doubles as long as
    the port keeps the SAME OPERATION ORDER (sums are written left to right; keep them that way; no FMA).
  * Loops are plain loops over lists in a fixed order: the index's family order, its `moods` order, CREATOR_MOODS,
    each CUES list, THEME_TAGS. Dicts here are lookups only.
  * Ranking sorts by the quantized score q = floor(score * 1e6 + 0.5) descending, then by the four ids ascending
    (display slug, display style, support slug, support style; plain code-point string compare).
  * Randomness is SplitMix64 seeded by the caller (a JSON-safe integer < 2^53): same inputs + same seed = same deck,
    so Back and the golden vectors are stable.

Pipeline (one function per step, in this order):
  1. role_pool   — family GATES per role, the role's UNARY score (mood, fitness, quality), the pool cut (top N).
  2. score_pair  — PAIR GATES, then the weighted score with a per-term breakdown and "why" codes.
  3. rank        — every display x support of the two pools (or the locked face x the other role's whole set).
  4. deal        — the Shuffle deck: MMR diversity over the top of the ranking + seeded jitter, no repeats.

CLI:  pair_scorer.py --context bold_hook [--top 20] [--deck 12]     (contexts.json names, or a context JSON file)
"""

import argparse
import json
import pathlib
import sys

SCORER_VERSION = "pair-1"

# ---------------------------------------------------------------------------------------------------------------
# Template moods: the creator vocabulary a recipe declares. Order is significant (summation order).
# ---------------------------------------------------------------------------------------------------------------
CREATOR_MOODS = ["bold", "playful", "elegant", "luxury", "editorial", "calm", "friendly", "corporate", "techy",
                 "retro", "handmade", "minimal", "cinematic"]

# Part 1 of a face's affinity to a creator mood — Google's human-scored Expressive moods (face `moods`, 0-100,
# interpolated per weight). Google's moods are sparse (a face rarely scores on every mood of a family), so the
# positive side is the best two weighted terms, not their mean: with t_g = W[g] * mood_g / 100 in index order,
# gf = clamp(GF_TOP1 * p1 + GF_TOP2 * p2 + sum_{W[g] < 0} t_g, 0, 1), p1 >= p2 the two largest positive t_g (0 if
# absent; on a tie the earlier g is p1).
MOOD_GF = {
    "bold":      {"Loud": 1.0, "Rugged": 0.4, "Active": 0.3, "Excited": 0.2, "Playful": -0.3, "Childlike": -0.4,
                  "Cute": -0.4, "Calm": -0.4, "Fancy": -0.3, "Awkward": -0.5, "Vintage": -0.3},
    "playful":   {"Playful": 1.0, "Happy": 0.8, "Cute": 0.6, "Childlike": 0.5, "Excited": 0.3,
                  "Business": -0.5, "Stiff": -0.5, "Sophisticated": -0.3},
    "elegant":   {"Sophisticated": 1.0, "Fancy": 0.8, "Calm": 0.4, "Loud": -0.5, "Childlike": -0.6,
                  "Rugged": -0.5, "Awkward": -0.5},
    "luxury":    {"Sophisticated": 1.0, "Fancy": 1.0, "Business": 0.2, "Childlike": -0.8, "Playful": -0.5,
                  "Rugged": -0.5, "Awkward": -0.6, "Cute": -0.5},
    "editorial": {"Sincere": 0.6, "Competent": 0.6, "Sophisticated": 0.6, "Calm": 0.4, "Business": 0.3,
                  "Childlike": -0.6, "Awkward": -0.5, "Cute": -0.4},
    "calm":      {"Calm": 1.0, "Sincere": 0.6, "Competent": 0.2, "Loud": -0.6, "Excited": -0.6, "Active": -0.4,
                  "Awkward": -0.4},
    "friendly":  {"Happy": 0.8, "Sincere": 0.6, "Playful": 0.5, "Calm": 0.3, "Cute": 0.3, "Stiff": -0.6,
                  "Rugged": -0.3},
    "corporate": {"Business": 1.0, "Competent": 1.0, "Calm": 0.3, "Playful": -0.6, "Childlike": -0.8,
                  "Awkward": -0.6, "Fancy": -0.4, "Artistic": -0.4},
    "techy":     {"Futuristic": 1.0, "Innovative": 0.7, "Competent": 0.2, "Vintage": -0.4, "Childlike": -0.4,
                  "Fancy": -0.5},
    "retro":     {"Vintage": 1.0, "Rugged": 0.3, "Futuristic": -0.5, "Innovative": -0.3},
    "handmade":  {"Artistic": 1.0, "Awkward": 0.3, "Active": 0.3, "Stiff": -0.8, "Business": -0.6},
    "minimal":   {"Calm": 0.6, "Competent": 0.6, "Business": 0.4, "Fancy": -0.6, "Childlike": -0.6,
                  "Awkward": -0.6, "Rugged": -0.3, "Artistic": -0.4, "Vintage": -0.4},
    "cinematic": {"Sophisticated": 0.5, "Rugged": 0.4, "Loud": 0.4, "Stiff": 0.2, "Childlike": -0.8, "Cute": -0.8,
                  "Playful": -0.6, "Awkward": -0.5},
}

# Part 2 — structural cues (skeleton, tags, category, measured metrics), because Google's moods alone are noisy
# (Playfair Display scores Sophisticated 0). cue = clamp(max positive matched value (0 if none) + min negative
# matched value (0 if none), 0, 1). Kinds:
#   ("skel", "Serif/Didone", v)        family skeleton equals
#   ("tag", "/Theme/Techno", 50, v)     family tag score >= 50
#   ("cat", "handwriting", v)          family category equals
#   ("cls", "Serif", v)                family broad class equals (skeleton prefix, else from the category)
#   ("caseless", v)                    face m.caseless is true
#   ("m", "stemc", lo, hi, v)          lo <= face metric <= hi (crisp)
#   ("all", [cue, cue, ...], v)        every sub-cue matches (the sub-cues' own values are unused)
CUES = {
    "bold": [("m", "stemc", 0.2, 9.0, 1.0), ("m", "ink", 0.45, 9.0, 1.0),
             ("all", [("m", "wdc", 0.0, 0.66, 0), ("m", "stemc", 0.15, 9.0, 0)], 1.0), ("m", "stemc", 0.16, 0.2, 0.6),
             ("m", "stemc", 0.0, 0.11, -0.6), ("cat", "handwriting", -0.3), ("cls", "Serif", -0.25),
             ("cls", "Slab", -0.25)],
    "playful": [("tag", "/Theme/Blobby", 50, 1.0), ("tag", "/Theme/Wacky", 50, 0.9), ("tag", "/Sans/Rounded", 50, 0.8),
                ("skel", "Script/Informal", 0.6), ("cat", "display", 0.3),
                ("skel", "Serif/Didone", -0.6), ("skel", "Script/Formal", -0.6), ("tag", "/Theme/Blackletter", 50, -0.8)],
    "elegant": [("skel", "Script/Formal", 1.0), ("skel", "Serif/Didone", 0.9), ("skel", "Serif/Modern", 0.7),
                ("skel", "Serif/Old Style Garalde", 0.7), ("m", "contrast", 0.0, 0.3, 0.8),
                ("m", "stemc", 0.2, 9.0, -0.5), ("tag", "/Sans/Rounded", 50, -0.4), ("tag", "/Theme/Pixel", 50, -1.0)],
    "luxury": [("skel", "Serif/Didone", 1.0), ("tag", "/Theme/Art Deco", 50, 0.9), ("m", "contrast", 0.0, 0.25, 0.9),
               ("all", [("cat", "serif", 0), ("caseless", 0)], 0.9), ("skel", "Serif/Modern", 0.8),
               ("skel", "Script/Formal", 0.7), ("m", "contrast", 0.25, 0.4, 0.6), ("m", "wdc", 0.95, 9.0, 0.6),
               ("cat", "serif", 0.5),
               ("m", "stemc", 0.22, 9.0, -0.5), ("tag", "/Sans/Rounded", 50, -0.6), ("cat", "handwriting", -0.3),
               ("tag", "/Theme/Pixel", 50, -1.0)],
    "editorial": [("skel", "Serif/Transitional", 1.0), ("skel", "Serif/Old Style Garalde", 1.0),
                  ("skel", "Serif/Humanist Venetian", 0.9), ("skel", "Serif/Scotch", 0.9), ("skel", "Serif/Didone", 0.8),
                  ("skel", "Serif/Modern", 0.8), ("skel", "Sans/Grotesque", 0.6), ("skel", "Sans/Neo Grotesque", 0.5),
                  ("skel", "Sans/Humanist", 0.5), ("cat", "handwriting", -0.5), ("tag", "/Theme/Pixel", 50, -1.0)],
    "calm": [("skel", "Serif/Old Style Garalde", 0.7), ("skel", "Serif/Humanist Venetian", 0.7),
             ("skel", "Sans/Humanist", 0.7), ("m", "stemc", 0.0, 0.14, 0.6),
             ("m", "stemc", 0.22, 9.0, -0.6), ("caseless", -0.3)],
    "friendly": [("tag", "/Sans/Rounded", 50, 1.0), ("skel", "Sans/Humanist", 0.7), ("skel", "Script/Informal", 0.6),
                 ("skel", "Slab/Humanist", 0.6), ("skel", "Sans/Geometric", 0.4), ("caseless", -0.3)],
    "corporate": [("skel", "Sans/Neo Grotesque", 1.0), ("skel", "Sans/Grotesque", 0.9), ("skel", "Sans/Humanist", 0.8),
                  ("skel", "Sans/Geometric", 0.8), ("skel", "Serif/Transitional", 0.6),
                  ("cat", "handwriting", -0.8), ("cat", "display", -0.5)],
    "techy": [("tag", "/Theme/Techno", 50, 1.0), ("skel", "Monospace/Monospace", 0.9), ("skel", "Sans/Superellipse", 0.9),
              ("tag", "/Theme/Pixel", 50, 0.7), ("skel", "Sans/Geometric", 0.6), ("skel", "Sans/Neo Grotesque", 0.5),
              ("cat", "handwriting", -0.8), ("skel", "Serif/Old Style Garalde", -0.4)],
    "retro": [("tag", "/Theme/Woodtype", 50, 1.0), ("tag", "/Theme/Tuscan", 50, 1.0), ("tag", "/Theme/Shaded", 50, 1.0),
              ("tag", "/Theme/Inline", 50, 1.0), ("tag", "/Theme/Art Deco", 50, 1.0), ("tag", "/Theme/Art Nouveau", 50, 1.0),
              ("skel", "Slab/Clarendon", 0.9), ("skel", "Serif/Fat Face", 0.9), ("skel", "Script/Upright Script", 0.7),
              ("skel", "Slab/Geometric", 0.6), ("skel", "Slab/Humanist", 0.6), ("skel", "Serif/Scotch", 0.6),
              ("tag", "/Theme/Techno", 50, -0.5)],
    "handmade": [("skel", "Script/Handwritten", 1.0), ("tag", "/Theme/Brush", 50, 1.0), ("cat", "handwriting", 0.9),
                 ("skel", "Script/Informal", 0.8), ("tag", "/Theme/Distressed", 50, 0.6),
                 ("skel", "Monospace/Monospace", -0.6)],
    "minimal": [("skel", "Sans/Neo Grotesque", 1.0), ("skel", "Sans/Geometric", 1.0), ("skel", "Sans/Grotesque", 0.8),
                ("skel", "Sans/Superellipse", 0.6), ("skel", "Monospace/Monospace", 0.6),
                ("cat", "display", -0.6), ("cat", "handwriting", -0.8), ("m", "stemc", 0.26, 9.0, -0.4)],
    "cinematic": [("caseless", 0.9), ("m", "wdc", 0.0, 0.62, 0.8), ("m", "wdc", 1.0, 9.0, 0.7),
                  ("tag", "/Theme/Art Deco", 50, 0.6), ("skel", "Serif/Old Style Garalde", 0.4),
                  ("cat", "handwriting", -0.6), ("tag", "/Sans/Rounded", 50, -0.6)],
}
GF_SHARE = 0.5                 # affinity = GF_SHARE * gf + (1 - GF_SHARE) * cue
GF_TOP1, GF_TOP2 = 0.7, 0.3

# What a template of each mood wants from its SUPPORT face (the quiet one): an ordered list, first match wins —
# "skel:<skeleton>" (family skeleton equals), "tag:<path>" (family tag >= 50), "cls:<broad class>"; no match = 0.5.
# support mood = SUP_AFF_SHARE * affinity(text face) + (1 - SUP_AFF_SHARE) * pref, averaged like the display's.
SUPPORT_PREF = {
    "bold": [("skel:Sans/Grotesque", 1.0), ("skel:Sans/Neo Grotesque", 1.0), ("skel:Sans/Geometric", 0.95),
             ("skel:Sans/Humanist", 0.85), ("skel:Sans/Superellipse", 0.85), ("cls:Sans", 0.85), ("cls:Slab", 0.6),
             ("cls:Monospace", 0.4), ("cls:Serif", 0.35)],
    "playful": [("tag:/Sans/Rounded", 1.0), ("skel:Sans/Geometric", 0.85), ("skel:Sans/Humanist", 0.85),
                ("cls:Sans", 0.7), ("cls:Slab", 0.6), ("cls:Serif", 0.3), ("cls:Monospace", 0.3)],
    "elegant": [("skel:Sans/Geometric", 0.9), ("cls:Serif", 0.9), ("skel:Sans/Humanist", 0.85), ("cls:Sans", 0.75),
                ("cls:Slab", 0.4), ("cls:Monospace", 0.3)],
    "luxury": [("skel:Sans/Geometric", 1.0), ("cls:Serif", 0.7), ("skel:Sans/Neo Grotesque", 0.85),
               ("tag:/Sans/Rounded", 0.4), ("cls:Sans", 0.75), ("cls:Slab", 0.3), ("cls:Monospace", 0.4)],
    "editorial": [("skel:Sans/Grotesque", 1.0), ("skel:Sans/Humanist", 1.0), ("skel:Sans/Neo Grotesque", 0.95),
                  ("cls:Sans", 0.85), ("cls:Serif", 0.8), ("cls:Slab", 0.6), ("cls:Monospace", 0.5)],
    "calm": [("skel:Sans/Humanist", 1.0), ("cls:Serif", 0.85), ("cls:Sans", 0.85), ("cls:Slab", 0.5),
             ("cls:Monospace", 0.4)],
    "friendly": [("tag:/Sans/Rounded", 1.0), ("skel:Sans/Humanist", 0.95), ("skel:Sans/Geometric", 0.85),
                 ("cls:Sans", 0.8), ("cls:Slab", 0.6), ("cls:Serif", 0.5), ("cls:Monospace", 0.3)],
    "corporate": [("skel:Sans/Neo Grotesque", 1.0), ("skel:Sans/Grotesque", 1.0), ("skel:Sans/Humanist", 0.95),
                  ("skel:Sans/Geometric", 0.9), ("tag:/Sans/Rounded", 0.6), ("cls:Sans", 0.85), ("cls:Serif", 0.55),
                  ("cls:Slab", 0.45), ("cls:Monospace", 0.4)],
    "techy": [("skel:Sans/Neo Grotesque", 1.0), ("skel:Sans/Geometric", 0.95), ("skel:Sans/Superellipse", 0.95),
              ("skel:Sans/Grotesque", 0.95), ("cls:Monospace", 0.9), ("cls:Sans", 0.85), ("cls:Slab", 0.4),
              ("cls:Serif", 0.3)],
    "retro": [("skel:Sans/Grotesque", 0.95), ("cls:Slab", 0.9), ("skel:Sans/Geometric", 0.85), ("cls:Sans", 0.8),
              ("cls:Serif", 0.8), ("cls:Monospace", 0.6)],
    "handmade": [("skel:Sans/Humanist", 1.0), ("tag:/Sans/Rounded", 0.9), ("cls:Sans", 0.85), ("cls:Serif", 0.8),
                 ("cls:Slab", 0.6), ("cls:Monospace", 0.5)],
    "minimal": [("skel:Sans/Neo Grotesque", 1.0), ("skel:Sans/Geometric", 1.0), ("skel:Sans/Grotesque", 1.0),
                ("tag:/Sans/Rounded", 0.6), ("cls:Sans", 0.85), ("cls:Monospace", 0.7), ("cls:Serif", 0.5),
                ("cls:Slab", 0.4)],
    "cinematic": [("skel:Sans/Geometric", 0.95), ("skel:Sans/Neo Grotesque", 0.95), ("cls:Sans", 0.85),
                  ("cls:Serif", 0.85), ("cls:Slab", 0.5), ("cls:Monospace", 0.4)],
}
SUPPORT_PREF_DEFAULT = 0.5
SUP_AFF_SHARE = 0.4

# Novelty damping: a themed face (Wacky, Pixel, Blackletter, ...) or one Google scores Awkward is special-purpose:
# its mood counts only when the template asks for it. mood *= 1 - NOVELTY_DAMP * novelty, where
# novelty = max(awkward, unwanted theme): awkward = clamp((Awkward - 40) / 60) * (1 - welcome), welcome = the
# template's mood share on AWKWARD_WELCOME; an unwanted theme = a THEME_TAGS tag >= 50 that no template mood (weight
# > 0) has a positive cue for, on a display / handwriting family or one with no skeleton (a classified text family's
# theme tag is secondary: Space Grotesk "Techno", and Google tags Bodoni Moda "Blackletter"); strength = score / 100;
# a face that also carries a WANTED theme is on-theme (no theme novelty).
NOVELTY_DAMP = 0.5
AWKWARD_FROM, AWKWARD_SPAN = 40.0, 60.0
AWKWARD_WELCOME = ("playful", "handmade")
THEME_TAGS = ["/Theme/Art Deco", "/Theme/Art Nouveau", "/Theme/Blackletter", "/Theme/Blobby", "/Theme/Brush",
              "/Theme/Distressed", "/Theme/Inline", "/Theme/Medieval", "/Theme/Pixel", "/Theme/Shaded",
              "/Theme/Stencil", "/Theme/Techno", "/Theme/Tuscan", "/Theme/Wacky", "/Theme/Woodtype"]

# ---------------------------------------------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------------------------------------------
COV_MIN = 0.95                 # measured coverage of each required base subset (latin, cyrillic, devanagari, ...);
                               # the host still checks the live text against the downloaded face's cmap
COV_MIN_EXT = 0.90             # ... of each required "*-ext" subset (and "vietnamese")
WEIGHT_TOL = 150               # a support/caption weight intent must be served within +-150 of its weight
STEM_TOL = 0.06                # ... a stem intent within +-0.06 stemc (Regular ~0.13, Bold ~0.21, Black ~0.29)
HEAVY_STEM = 0.17              # tie-break direction for stem intents
FIT_MIN = {"display": 0.45, "support": 0.60, "caption": 0.60}
TWIN_MAX = 0.5                 # same broad class and no flesh contrast (weight, width, modulation) above this = twin
SAME_FAMILY_MIN_DW = 300       # one family in both roles only as a weight pairing (e.g. Black + Regular)
REQUIRED_METRICS = ("xr", "wdc", "stemc", "contrast", "ink")
TEXT_INTENT = {"weight": 400, "stem": None, "wdc": None, "italic": False}   # a family's text face (support fit)
# Support width: a support face keeps the authored width — fit *= SUP_WIDTH_FLOOR + (1 - floor) * closeness, with
# closeness = band(|wdc - intent.wdc|, 0, 0.08, 0.15), or band(wdc, 0.70, 0.95, 0.12) when the intent has no wdc
# (a plain subline should not turn condensed or extended).
SUP_WIDTH_FLOOR = 0.7

# GF subset -> ISO 15924 script, for the "alias family" gate: a family whose primaryScript (not Latin) is not one the
# template needs is left out — its Latin is usually secondary or a copy of another family's, and CJK files are
# megabytes — unless it is a well-known family in its own right (Poppins, Kanit, Heebo): popularity rank <= 200,
# face <= 1 MB, not a Noto script-shelf family and no script word in its name (IBM Plex Sans Arabic, Hind Siliguri).
ALIAS_POP_MAX = 200
ALIAS_BYTES_MAX = 1000000
SUBSET_SCRIPT = {"latin": "Latn", "latin-ext": "Latn", "vietnamese": "Latn", "cyrillic": "Cyrl",
                 "cyrillic-ext": "Cyrl", "greek": "Grek", "greek-ext": "Grek", "devanagari": "Deva", "arabic": "Arab",
                 "hebrew": "Hebr", "thai": "Thai", "japanese": "Jpan", "korean": "Kore",
                 "chinese-simplified": "Hans", "chinese-traditional": "Hant", "chinese-hongkong": "Hant",
                 "bengali": "Beng", "tamil": "Taml", "telugu": "Telu", "kannada": "Knda", "gujarati": "Gujr",
                 "gurmukhi": "Guru", "malayalam": "Mlym", "sinhala": "Sinh", "oriya": "Orya", "khmer": "Khmr",
                 "armenian": "Armn", "ethiopic": "Ethi", "tibetan": "Tibt", "georgian": "Geor", "lao": "Laoo"}

# ---------------------------------------------------------------------------------------------------------------
# Unary (per role) and pair weights
# ---------------------------------------------------------------------------------------------------------------
UNARY = {  # pool ranking per role: (mood, role fitness, quality)
    "display": (0.55, 0.25, 0.20),
    "support": (0.35, 0.45, 0.20),
}
POOL = {"display": 160, "support": 120}
MOOD_REL_MIN = {"display": 0.55, "support": 0.6}  # a pool member's mood >= this x the best eligible mood
DEFAULT_ROLE_WEIGHTS = {"display": 0.7, "support": 0.3}

# score = W_MOOD*mood + W_FIT*fit + W_BAL*balance + W_HAR*harmony + W_QUAL*quality + W_BOOST*boost - penalty
W_MOOD = 0.38
W_FIT = 0.14
W_BAL = 0.12
W_HAR = 0.12
W_QUAL = 0.12
W_BOOST = 0.12

QUAL_LO = 67.5                 # the catalog gate; quality 90 and up = 1
QUAL_SPAN = 22.5
QUAL_POP_SHARE = 0.5           # popularity is a SATURATING prior: it pushes the long tail down, and the top 150
POP_TOP = 150.0                # get no edge over each other (rank <= 150 -> 1, falling linearly to 0 at rank
POP_SPAN = 1050.0              # 1200) — the "popularity trap" is recommending Inter for everything

# balance(D, S): different enough (one strong contrast), the display carries more presence, compatible classes.
#   contrasts: class = CLASS_DIST (0 for the same skeleton), weight = |d stemc| / 0.10, width = |d wdc| / 0.25,
#   modulation = |d contrast| / 0.45, case = 1 when one role renders in caps and the other does not.
#   diff = band(max(contrasts), DIFF_LO, inf, DIFF_SOFT); order = band(D.ink - S.ink, ORDER_LO, inf, ORDER_SOFT)
#   (1 when the display is a script or high-contrast); balance = (0.6 diff + 0.4 order) * CLASS_COMPAT.
C_WEIGHT, C_WIDTH, C_MOD = 0.10, 0.25, 0.45
DIFF_LO, DIFF_SOFT = 0.6, 0.35
BAL_DIFF_SHARE = 0.6
ORDER_LO, ORDER_SOFT = -0.02, 0.08
ORDER_EXEMPT_CONTRAST = 0.35
CLASS_DIST = {
    ("Sans", "Sans"): 0.3, ("Serif", "Serif"): 0.3, ("Slab", "Slab"): 0.3, ("Script", "Script"): 0.6,
    ("Monospace", "Monospace"): 0.3, ("Display", "Display"): 0.5,
    ("Sans", "Serif"): 0.8, ("Sans", "Slab"): 0.6, ("Sans", "Script"): 1.0, ("Sans", "Monospace"): 0.6,
    ("Sans", "Display"): 0.8, ("Serif", "Slab"): 0.5, ("Serif", "Script"): 1.0, ("Serif", "Monospace"): 0.8,
    ("Serif", "Display"): 0.8, ("Slab", "Script"): 1.0, ("Slab", "Monospace"): 0.6, ("Slab", "Display"): 0.8,
    ("Script", "Monospace"): 1.0, ("Script", "Display"): 1.0, ("Monospace", "Display"): 0.8,
}
CLASS_COMPAT = {("Script", "Monospace"): 0.4, ("Monospace", "Script"): 0.4, ("Display", "Monospace"): 0.7,
                ("Monospace", "Monospace"): 0.6, ("Serif", "Serif"): 0.8}

# harmony(D, S): x-height closeness + the same construction "skeleton axis" across classes (Font Matrix rows)
HAR_XR_SHARE = 0.4
XR_SPAN = 0.20                 # 1 - |d xr| / 0.20
XR_NEUTRAL = 0.8               # display rendered in caps: its x-height is not on show
AXIS = {
    "Serif/Humanist Venetian": "humanist", "Serif/Old Style Garalde": "humanist", "Sans/Humanist": "humanist",
    "Slab/Humanist": "humanist", "Sans/Glyphic": "humanist",
    "Serif/Transitional": "rational", "Serif/Didone": "rational", "Serif/Scotch": "rational",
    "Serif/Modern": "rational", "Serif/Fat Face": "rational", "Sans/Grotesque": "rational",
    "Sans/Neo Grotesque": "rational", "Slab/Clarendon": "rational",
    "Sans/Geometric": "geometric", "Sans/Rounded": "geometric", "Slab/Geometric": "geometric",
    "Sans/Superellipse": "geometric",
}
AXIS_ORDER = {"humanist": 0, "rational": 1, "geometric": 2}
AXIS_SCORE = (1.0, 0.75, 0.4)  # same axis, neighbours (Didone + geometric sans is the Vogue pairing),
                               # humanist<->geometric; unknown either = 0.5

# boost(D, S) = max of: a seed pair 0.4, a cross-class superfamily 0.35 (Roboto Slab + Roboto), one family as a
# weight pairing 0.25 (Inter Bold + Inter Regular), a shared designer 0.2. Two families of one design in the same
# class (Playfair Display + Playfair, Saira Condensed + Saira) count as ONE family: the same-family gate applies.
BOOST_SEED = 0.4               # only when the seed's moods meet the template's (seed_meets); else no seed boost
BOOST_SUPERFAMILY = 0.35
BOOST_SAME_FAMILY = 0.25
BOOST_DESIGNER = 0.2
CLASS_WORDS = {"sans", "serif", "slab", "mono", "display", "text", "condensed", "semi", "extra", "narrow",
               "caption", "sc", "round", "rounded", "letters", "headline", "titling", "micro", "small", "deck",
               "subhead", "expanded", "variable", "pro", "one", "two", "4", "2", "3", "tight", "flex", "alternates",
               "next", "new"}
SCRIPT_WORDS = {"arabic", "thai", "jp", "kr", "tc", "hk", "hebrew", "bengali", "devanagari", "gujarati", "tamil",
                "telugu", "kannada", "malayalam", "gurmukhi", "sinhala", "khmer", "lao", "siliguri", "vadodara",
                "madurai", "guntur", "malar", "da", "bhai", "bhaijaan", "bhaina", "chettan", "paaji", "tamma",
                "thambi", "urdu", "kufi", "naskh", "nastaliq"}
SEED_GAP = 0.04                # quality floor: a seed pair whose moods meet the template's scores at least
SEED_MOOD_MIN = 0.5            # (best non-seed score - SEED_GAP); "meet" = the seed's moods carry >= 0.5 of the
                               # template's mood weight. Relative, because score ranges differ by mood.

# penalties (subtracted)
PEN_OUTLINE = 0.15             # template strokes text: x max(0, overlap - 0.1) per face (until the engine unions)
PEN_CASELESS_MIXED = 0.04      # display authored in mixed case, candidate is caps-only
PEN_WIDE = 0.06                # display wider than WIDE_FROM: shrink-to-fit makes it tiny on a phone-width line;
WIDE_FROM, WIDE_SPAN = 1.25, 0.5   # penalty = PEN_WIDE * clamp((wdc - 1.25) / 0.5)

# Shuffle deck
DECK_TOP = 60                  # the deck draws from the top of the ranking (minus pairs already shown): at most
DECK_PER_DISPLAY = 2           # 2 pairs per display design and
DECK_PER_SUPPORT = 4           # 4 per support design, until 60 candidates,
DECK_MARGIN = 0.10             # and only pairs within 0.10 of the best unseen NON-SEED score (>= DECK_MIN of them)
DECK_MIN = 16
DIV_LAMBDA = 0.15              # MMR value = score - DIV_LAMBDA * max sim to the last DIV_WINDOW cards + JITTER * u
DIV_WINDOW = 4
JITTER = 0.03
SUPPORT_REPEAT_MAX = 2         # per deck: a support family at most twice, a display family once, and no two
                               # curated (seed) cards in a row while computed ones remain
SIM_SAME, SIM_SKEL, SIM_CLASS = 1.0, 0.6, 0.45
SIM_D_SHARE = 0.6              # pair similarity = 0.6 * sim(displays) + 0.4 * sim(supports)
SIM_KIND = 0.5                 # + 0.5 when both are one-family pairs (Inter Bold + Inter): vary the kind too

MASK64 = 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------
def clamp01(x):
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def band(x, lo, hi, soft):
    """1 inside [lo, hi], falling linearly to 0 at lo - soft / hi + soft (same as build_index.band)."""
    if lo <= x <= hi:
        return 1.0
    d = lo - x if x < lo else x - hi
    v = 1.0 - d / soft
    return v if v > 0.0 else 0.0


def quantize(score):
    """Integer score for sorting and comparing: floor(score * 1e6 + 0.5) (Kotlin: floor(...).toLong())."""
    return int((score * 1000000.0 + 0.5) // 1.0)


class SplitMix64:
    """SplitMix64 (Steele, Lea, Flood 2014). Kotlin: Long arithmetic, `ushr` for >>, wrapping multiply."""

    def __init__(self, seed):
        self.s = seed & MASK64

    def next_u64(self):
        self.s = (self.s + 0x9E3779B97F4A7C15) & MASK64
        z = self.s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def next_double(self):
        """[0, 1): the top 53 bits."""
        return (self.next_u64() >> 11) * (1.0 / 9007199254740992.0)


# ---------------------------------------------------------------------------------------------------------------
# Index access
# ---------------------------------------------------------------------------------------------------------------
class Index:
    def __init__(self, data, seeds=None):
        self.data = data
        self.families = data["families"]
        self.by_slug = {f["slug"]: f for f in self.families}
        self.mood_names = data["moods"]
        self.tag_names = data["tagNames"]
        self.mood_w = {}  # creator mood -> weights aligned with mood_names
        for cm in CREATOR_MOODS:
            self.mood_w[cm] = [MOOD_GF[cm].get(n, 0.0) for n in self.mood_names]
        self.awkward_idx = self.mood_names.index("Awkward")
        self.seeds = {}   # (display slug, support slug) -> seed record
        self.seed_list = list((seeds or {}).get("pairs", []))   # file order
        for p in self.seed_list:
            self.seeds[(p["display"]["slug"], p["support"]["slug"])] = p

    def tag(self, fam, path):
        for i, v in fam["tags"]:
            if self.tag_names[i] == path:
                return v
        return 0


def broad_class(fam):
    sk = fam.get("skeleton")
    if sk:
        return sk.split("/")[0]
    return {"sans-serif": "Sans", "serif": "Serif", "display": "Display", "handwriting": "Script",
            "monospace": "Monospace"}.get(fam["category"], "Display")


def class_dist(a, b):
    if (a, b) in CLASS_DIST:
        return CLASS_DIST[(a, b)]
    return CLASS_DIST[(b, a)]


def is_script(fam):
    sk = fam.get("skeleton") or ""
    return sk.startswith("Script/") or fam["category"] == "handwriting"


def pick_face(fam, intent):
    """The face a slot with this intent gets. Same italic-ness when the family has it (else upright). Then the
    nearest by measured stem weight (`stem` intent: the authored face's m.stemc — weight names differ between
    families: Anton Regular is as heavy as Montserrat Bold) or by numeric weight (`weight` intent). A tie goes to
    the heavier face for heavy intents (stem >= 0.17 / weight >= 600), else to the lighter."""
    italic = intent["italic"]
    faces = [f for f in fam["faces"] if f["italic"] == italic]
    if not faces:
        faces = [f for f in fam["faces"] if not f["italic"]]
    if not faces:
        faces = fam["faces"]
    stem = intent.get("stem")
    best = None
    best_d = 0.0
    for f in faces:
        if stem is not None:
            fs = (f.get("m") or {}).get("stemc")
            d = abs(fs - stem) if fs is not None else 1000.0
            heavy = stem >= HEAVY_STEM
        else:
            d = float(abs(f["weight"] - intent["weight"]))
            heavy = intent["weight"] >= 600
        if best is None or d < best_d or (d == best_d and ((heavy and f["weight"] > best["weight"]) or
                                                           (not heavy and f["weight"] < best["weight"]))):
            best = f
            best_d = d
    return best


def intent_ok(face, intent):
    """Whether a picked face is close enough to its intent (support/caption weight gate)."""
    if intent.get("stem") is not None:
        fs = (face.get("m") or {}).get("stemc")
        return fs is not None and abs(fs - intent["stem"]) <= STEM_TOL
    return abs(face["weight"] - intent["weight"]) <= WEIGHT_TOL


def metrics_ok(face):
    m = face.get("m") or {}
    for k in REQUIRED_METRICS:
        if m.get(k) is None:
            return False
    return True


def quality(fam):
    q = clamp01((fam["quality"] - QUAL_LO) / QUAL_SPAN) if fam.get("quality") is not None else 0.0
    pop = fam.get("popularity")
    if pop is None:
        p = 0.0
    elif pop <= POP_TOP:
        p = 1.0
    else:
        p = clamp01(1.0 - (pop - POP_TOP) / POP_SPAN)
    return (1.0 - QUAL_POP_SHARE) * q + QUAL_POP_SHARE * p


def cue_match(ix, fam, face, cue):
    kind = cue[0]
    if kind == "skel":
        return fam.get("skeleton") == cue[1]
    if kind == "tag":
        return ix.tag(fam, cue[1]) >= cue[2]
    if kind == "cat":
        return fam["category"] == cue[1]
    if kind == "cls":
        return broad_class(fam) == cue[1]
    if kind == "caseless":
        return bool(face["m"].get("caseless"))
    if kind == "m":
        v = face["m"].get(cue[1])
        return v is not None and cue[2] <= v <= cue[3]
    if kind == "all":
        for sub in cue[1]:
            if not cue_match(ix, fam, face, sub):
                return False
        return True
    raise ValueError(kind)


def affinity(ix, fam, face, cm):
    """A face's fit to one creator mood, 0-1 (Google moods half, structural cues half)."""
    w = ix.mood_w[cm]
    p1 = 0.0
    p2 = 0.0
    neg = 0.0
    moods = face["moods"]
    for i in range(len(w)):
        if w[i] > 0.0:
            t = w[i] * moods[i] / 100.0
            if t > p1:
                p2 = p1
                p1 = t
            elif t > p2:
                p2 = t
        elif w[i] < 0.0:
            neg += w[i] * moods[i] / 100.0
    gf = clamp01(GF_TOP1 * p1 + GF_TOP2 * p2 + neg)
    best_pos = 0.0
    worst_neg = 0.0
    for cue in CUES[cm]:
        v = cue[-1]
        if cue_match(ix, fam, face, cue):
            if v > best_pos:
                best_pos = v
            if v < worst_neg:
                worst_neg = v
    cue = clamp01(best_pos + worst_neg)
    return GF_SHARE * gf + (1.0 - GF_SHARE) * cue


def novelty(ix, ctx, fam, face):
    """0-1: how special-purpose a face is for THIS template (see NOVELTY_DAMP)."""
    awk = clamp01((face["moods"][ix.awkward_idx] - AWKWARD_FROM) / AWKWARD_SPAN)
    awk = awk * (1.0 - ctx["welcome"])
    theme = 0.0
    on_theme = False
    themed = fam["category"] in ("display", "handwriting") or not fam.get("skeleton")
    for t in (THEME_TAGS if themed else []):
        v = ix.tag(fam, t)
        if v >= 50:
            if t in ctx["wantedThemes"]:
                on_theme = True
            elif v / 100.0 > theme:
                theme = v / 100.0
    if on_theme:
        theme = 0.0
    return awk if awk > theme else theme


def mood_fit(ix, ctx, fam, face):
    """Weighted mean of the face's affinity over the template's moods (CREATOR_MOODS order), novelty-damped."""
    moods = ctx["moods"]
    num = 0.0
    den = 0.0
    for cm in CREATOR_MOODS:
        wt = moods.get(cm, 0.0)
        if wt > 0.0:
            num += wt * affinity(ix, fam, face, cm)
            den += wt
    m = num / den if den > 0.0 else 0.5
    return m * (1.0 - NOVELTY_DAMP * novelty(ix, ctx, fam, face))


def support_pref(ix, cm, fam):
    for key, v in SUPPORT_PREF[cm]:
        kind, arg = key.split(":", 1)
        if kind == "skel" and fam.get("skeleton") == arg:
            return v
        if kind == "tag" and ix.tag(fam, arg) >= 50:
            return v
        if kind == "cls" and broad_class(fam) == arg:
            return v
    return SUPPORT_PREF_DEFAULT


def support_mood_fit(ix, ctx, fam, text):
    """The support role's mood: the text face's affinity blended with the mood's support preference."""
    moods = ctx["moods"]
    num = 0.0
    den = 0.0
    for cm in CREATOR_MOODS:
        wt = moods.get(cm, 0.0)
        if wt > 0.0:
            v = SUP_AFF_SHARE * affinity(ix, fam, text, cm) + (1.0 - SUP_AFF_SHARE) * support_pref(ix, cm, fam)
            num += wt * v
            den += wt
    m = num / den if den > 0.0 else 0.5
    return m * (1.0 - NOVELTY_DAMP * novelty(ix, ctx, fam, text))


# ---------------------------------------------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------------------------------------------
def _intent(x):
    """An intent is a number (upright weight) or {"weight": w | "stem": stemc, "wdc": width?, "italic": b}. The host
    fills it from the authored face (m.stemc, m.wdc, italic), so a swap keeps the authored weight and width."""
    if isinstance(x, (int, float)):
        return {"weight": int(x), "stem": None, "wdc": None, "italic": False}
    stem = x.get("stem")
    wdc = x.get("wdc")
    return {"weight": int(x.get("weight", 400)), "stem": float(stem) if stem is not None else None,
            "wdc": float(wdc) if wdc is not None else None, "italic": bool(x.get("italic", False))}


def parse_context(ctx):
    """Normalise a template context. JSON shape (contexts.json has examples):
      moods        {creator mood: weight} or [moods]          from the template (library index typography.moods)
      roles        {display|support|caption: {intents: [intent, ...], caps: bool}}   display + support required;
                   intent = weight number or {stem|weight, wdc?, italic?} — the host fills it from the authored face
      roleWeights  {display: w, support: w}                   optional, default 0.7 / 0.3 (normalised)
      scripts      GF subsets the live text needs, e.g. ["latin"] or ["devanagari", "latin"]
      outline      true when the template strokes its text
      locked       {role, slug, style?}                       Lock: only the other role varies
      exclude      [slug, ...]                                families never offered
      shown        [[display slug, support slug], ...]        pairs already shown (the deck skips them)
      seed         integer < 2^53                             the deck's SplitMix64 seed"""
    roles = {}
    for r in ("display", "support", "caption"):
        spec = (ctx.get("roles") or {}).get(r)
        if spec is None:
            continue
        intents = [_intent(x) for x in (spec.get("intents") or [400])]
        roles[r] = {"intents": intents, "caps": bool(spec.get("caps", False))}
    if "display" not in roles or "support" not in roles:
        raise ValueError("pair-1 ranks pairs: the context needs a display and a support role")
    moods_in = ctx.get("moods") or {}
    if isinstance(moods_in, list):
        moods_in = {m: 1.0 for m in moods_in}
    moods = {}
    for m, w in moods_in.items():
        if m not in CREATOR_MOODS:
            raise ValueError("unknown mood " + m)
        moods[m] = float(w)
    tot = 0.0
    welcome = 0.0
    wanted = set()
    for cm in CREATOR_MOODS:
        w = moods.get(cm, 0.0)
        if w > 0.0:
            tot += w
            if cm in AWKWARD_WELCOME:
                welcome += w
            for cue in CUES[cm]:
                if cue[0] == "tag" and cue[-1] > 0.0 and cue[1] in THEME_TAGS:
                    wanted.add(cue[1])
    rw = dict(DEFAULT_ROLE_WEIGHTS)
    for k, v in (ctx.get("roleWeights") or {}).items():
        rw[k] = float(v)
    rsum = rw["display"] + rw["support"]
    return {
        "moods": moods, "roles": roles,
        "roleWeights": {"display": rw["display"] / rsum, "support": rw["support"] / rsum},
        "welcome": welcome / tot if tot > 0.0 else 0.0,
        "wantedThemes": wanted,
        "scripts": list(ctx.get("scripts") or ["latin"]),
        "outline": bool(ctx.get("outline", False)),
        "locked": ctx.get("locked"),
        "exclude": set(ctx.get("exclude") or []),
        "shown": [tuple(p) for p in (ctx.get("shown") or [])],
        "seed": int(ctx.get("seed", 1)),
    }


# ---------------------------------------------------------------------------------------------------------------
# 1. Role pools
# ---------------------------------------------------------------------------------------------------------------
def family_gate(ix, ctx, fam, role, seeded=False):
    """None when the family may serve `role`, else the reject code. A seeded family (one side of a hand-checked seed
    pair whose moods meet the template's) skips the fitness gates; every hard gate still applies."""
    if fam["slug"] in ctx["exclude"]:
        return "excluded"
    cov = fam.get("cov") or {}
    need_iso = set()
    for s in ctx["scripts"]:
        lim = COV_MIN_EXT if (s.endswith("-ext") or s == "vietnamese") else COV_MIN
        if cov.get(s, 0.0) < lim:
            return "script"
        need_iso.add(SUBSET_SCRIPT.get(s, ""))
    spec = ctx["roles"][role]
    rep = pick_face(fam, spec["intents"][0])
    ps = fam.get("primaryScript")
    if ps is not None and ps != "Latn" and ps not in need_iso:
        if (fam["popularity"] > ALIAS_POP_MAX or rep["bytes"] > ALIAS_BYTES_MAX
                or fam["name"].startswith("Noto ") or has_script_word(fam)):
            return "alias-script"
    if not metrics_ok(rep):
        return "metrics"
    if role == "display":
        if spec["caps"] and is_script(fam):
            return "script-caps"
        if not seeded and rep["fit"]["display"] < FIT_MIN["display"]:
            return "fit"
        return None
    # support (caption slots are served by the support family: two fonts per template)
    if is_script(fam):
        return "script-support"
    for it in spec["intents"]:
        if not intent_ok(pick_face(fam, it), it):
            return "weight"
    text = pick_face(fam, TEXT_INTENT)
    if not metrics_ok(text) or text["m"].get("caseless"):
        return "caseless"
    if not seeded and text["fit"]["support"] < FIT_MIN["support"]:
        return "fit"
    if "caption" in ctx["roles"]:
        cap = ctx["roles"]["caption"]["intents"][0]
        cf = pick_face(fam, cap)
        if not intent_ok(cf, cap):
            return "weight"
        if cf["m"].get("caseless") or (not seeded and cf["fit"]["caption"] < FIT_MIN["caption"]):
            return "caption"
    return None


def role_entry(ix, ctx, fam, role):
    """What the pair score needs about one family in one role."""
    spec = ctx["roles"][role]
    rep = pick_face(fam, spec["intents"][0])
    styles = []
    for it in spec["intents"]:
        styles.append(pick_face(fam, it)["style"])
    if role == "display":
        fit = rep["fit"]["display"]
        mood = mood_fit(ix, ctx, fam, rep)
    else:
        # support fitness and mood are family properties, read on its text face (Regular): the intent face's
        # weight is the template's choice, not the family's character
        text = pick_face(fam, TEXT_INTENT)
        want = spec["intents"][0].get("wdc")
        if want is not None:
            close = band(abs(rep["m"]["wdc"] - want), 0.0, 0.08, 0.15)
        else:
            close = band(rep["m"]["wdc"], 0.70, 0.95, 0.12)
        fit = text["fit"]["support"] * (SUP_WIDTH_FLOOR + (1.0 - SUP_WIDTH_FLOOR) * close)
        mood = support_mood_fit(ix, ctx, fam, text)
    q = quality(fam)
    a, b, c = UNARY[role]
    return {"fam": fam, "slug": fam["slug"], "face": rep, "style": rep["style"], "styles": styles,
            "mood": mood, "fit": fit, "q": q, "unary": a * mood + b * fit + c * q, "cls": broad_class(fam),
            "caps": spec["caps"] or bool(rep["m"].get("caseless"))}


def role_pool(ix, ctx, role, limit=True):
    elig = []
    top = 0.0
    for fam in ix.families:
        if family_gate(ix, ctx, fam, role) is None:
            e = role_entry(ix, ctx, fam, role)
            elig.append(e)
            if e["mood"] > top:
                top = e["mood"]
    out = []
    for e in elig:
        if e["mood"] >= MOOD_REL_MIN[role] * top:
            out.append(e)
    out.sort(key=lambda e: (-quantize(e["unary"]), e["slug"]))
    if limit:
        out = out[:POOL[role]]
    return out


def locked_entry(ix, ctx, role, lock):
    """The locked face skips the family gates (the person chose it); pair gates still apply."""
    fam = ix.by_slug[lock["slug"]]
    e = role_entry(ix, ctx, fam, role)
    if lock.get("style"):
        for f in fam["faces"]:
            if f["style"] == lock["style"]:
                e["face"] = f
                e["style"] = f["style"]
    return e


# ---------------------------------------------------------------------------------------------------------------
# 2. Pair score
# ---------------------------------------------------------------------------------------------------------------
def name_root(fam):
    out = []
    for t in fam["name"].lower().split(" "):
        if t not in CLASS_WORDS and t not in SCRIPT_WORDS:
            out.append(t)
    return " ".join(out)


def has_script_word(fam):
    for t in fam["name"].lower().split(" "):
        if t in SCRIPT_WORDS:
            return True
    return False


def superfamily(fd, fs):
    """Same family, or names equal once class words are dropped (Roboto / Roboto Slab, PT Serif / PT Sans) AND a
    shared designer."""
    if fd["slug"] == fs["slug"]:
        return True
    if name_root(fd) != name_root(fs):
        return False
    for x in fd.get("designers") or []:
        if x in (fs.get("designers") or []):
            return True
    return False


def deck_key(fam):
    """Looser than design_root, for the deck only: first name word + first designer (Cormorant / Cormorant Garamond /
    Cormorant Infant are one design to a person shuffling)."""
    ds = fam.get("designers") or [""]
    return fam["name"].lower().split(" ")[0] + "|" + ds[0]


def design_root(fam):
    """Families of one design (Playfair / Playfair Display / Playfair Display SC) share this key: the name with
    class words dropped. The deck shows one design once; the same-class gate treats them as one family."""
    return name_root(fam)


def score_pair(ix, ctx, d, s):
    """(record, None) or (None, reject code). d and s are role entries."""
    fd, fs = d["fam"], s["fam"]
    a, b = d["face"], s["face"]
    ma, mb = a["m"], b["m"]
    same = fd["slug"] == fs["slug"] or (d["cls"] == s["cls"] and design_root(fd) == design_root(fs))
    if same and abs(a["weight"] - b["weight"]) < SAME_FAMILY_MIN_DW:
        return None, "same-family"
    c_weight = abs(ma["stemc"] - mb["stemc"]) / C_WEIGHT
    c_width = abs(ma["wdc"] - mb["wdc"]) / C_WIDTH
    c_mod = abs(ma["contrast"] - mb["contrast"]) / C_MOD
    c_case = 1.0 if d["caps"] != s["caps"] else 0.0
    flesh = c_weight
    if c_width > flesh:
        flesh = c_width
    if c_mod > flesh:
        flesh = c_mod
    if not same and d["cls"] == s["cls"] and flesh < TWIN_MAX:
        return None, ("twin" if fd.get("skeleton") == fs.get("skeleton") else "matrix")
    if d["cls"] in ("Script", "Display") and fs["category"] in ("display", "handwriting"):
        return None, "both-expressive"

    # balance
    if same or (fd.get("skeleton") and fd.get("skeleton") == fs.get("skeleton")):
        c_class = 0.0
    else:
        c_class = class_dist(d["cls"], s["cls"])
    strongest = c_class
    for v in (c_weight, c_width, c_mod, c_case):
        if v > strongest:
            strongest = v
    diff = band(strongest, DIFF_LO, 1000.0, DIFF_SOFT)
    if is_script(fd) or ma["contrast"] < ORDER_EXEMPT_CONTRAST:
        order = 1.0
    else:
        order = band(ma["ink"] - mb["ink"], ORDER_LO, 1000.0, ORDER_SOFT)
    compat = CLASS_COMPAT.get((d["cls"], s["cls"]), 1.0)
    balance = (BAL_DIFF_SHARE * diff + (1.0 - BAL_DIFF_SHARE) * order) * compat

    # harmony
    if d["caps"]:
        xr = XR_NEUTRAL
    else:
        xr = 1.0 - min(1.0, abs(ma["xr"] - mb["xr"]) / XR_SPAN)
    ad = AXIS.get(fd.get("skeleton") or "")
    as_ = AXIS.get(fs.get("skeleton") or "")
    if ad is None or as_ is None:
        axis = 0.5
    else:
        axis = AXIS_SCORE[abs(AXIS_ORDER[ad] - AXIS_ORDER[as_])]
    harmony = HAR_XR_SHARE * xr + (1.0 - HAR_XR_SHARE) * axis

    # boost
    seed = ix.seeds.get((fd["slug"], fs["slug"]))
    meets = seed is not None and seed_meets(ctx, seed)
    boost = 0.0
    if meets:
        boost = BOOST_SEED
    elif same:
        boost = BOOST_SAME_FAMILY
    elif superfamily(fd, fs):
        boost = BOOST_SUPERFAMILY
    else:
        for x in fd.get("designers") or []:
            if x in (fs.get("designers") or []):
                boost = BOOST_DESIGNER

    # penalty
    pen = 0.0
    if ctx["outline"]:
        pen += PEN_OUTLINE * (max(0.0, ma["overlap"] - 0.1) + max(0.0, mb["overlap"] - 0.1))
    if (not ctx["roles"]["display"]["caps"]) and ma.get("caseless"):
        pen += PEN_CASELESS_MIXED
    if ma["wdc"] > WIDE_FROM:
        pen += PEN_WIDE * clamp01((ma["wdc"] - WIDE_FROM) / WIDE_SPAN)

    rw = ctx["roleWeights"]
    mood = rw["display"] * d["mood"] + rw["support"] * s["mood"]
    fit = rw["display"] * d["fit"] + rw["support"] * s["fit"]
    qual = 0.5 * d["q"] + 0.5 * s["q"]
    score = (W_MOOD * mood + W_FIT * fit + W_BAL * balance + W_HAR * harmony + W_QUAL * qual
             + W_BOOST * boost - pen)

    why = []
    if same:
        why.append("same-family")
    elif boost == BOOST_SUPERFAMILY:
        why.append("superfamily")
    else:
        why.append(d["cls"].lower() + "+" + s["cls"].lower())
    if meets:
        why.append("curated")
    if xr >= 0.8 and not d["caps"]:
        why.append("x-height")
    if axis == 1.0 and not same and c_class > 0.0:
        why.append("same-skeleton")
    if c_weight >= 1.0 and order >= 1.0:
        why.append("weight-contrast")
    if c_width >= 1.0:
        why.append("width-contrast")

    rec = {
        "display": {"slug": fd["slug"], "name": fd["name"], "style": a["style"], "styles": d["styles"]},
        "support": {"slug": fs["slug"], "name": fs["name"], "style": b["style"], "styles": s["styles"]},
        "score": score, "q": quantize(score),
        "terms": {"mood": mood, "fit": fit, "balance": balance, "harmony": harmony, "quality": qual,
                  "boost": boost, "penalty": pen, "floor": False},
        "why": why,
        "seedMeets": meets,
    }
    return rec, None


def seed_meets(ctx, seed):
    moods = ctx["moods"]
    tot = 0.0
    hit = 0.0
    sm = seed.get("moods", [])
    for cm in CREATOR_MOODS:
        w = moods.get(cm, 0.0)
        if w > 0.0:
            tot += w
            if cm in sm:
                hit += w
    return tot > 0.0 and hit >= SEED_MOOD_MIN * tot


def pair_key(rec):
    return (-rec["q"], rec["display"]["slug"], rec["display"]["style"], rec["support"]["slug"],
            rec["support"]["style"])


# ---------------------------------------------------------------------------------------------------------------
# 3. Rank
# ---------------------------------------------------------------------------------------------------------------
def rank(ix, ctx, with_rejects=False):
    """All passing pairs, best first. With a lock only the other role varies, over its whole eligible set."""
    lock = ctx["locked"]
    if lock and lock["role"] == "display":
        ds = [locked_entry(ix, ctx, "display", lock)]
        ss = role_pool(ix, ctx, "support", limit=False)
    elif lock and lock["role"] == "support":
        ds = role_pool(ix, ctx, "display", limit=False)
        ss = [locked_entry(ix, ctx, "support", lock)]
    else:
        ds = role_pool(ix, ctx, "display")
        ss = role_pool(ix, ctx, "support")
    jobs = []
    for d in ds:
        for s in ss:
            jobs.append((d, s))
    # seed pairs whose moods meet the template's are always scored (the pools' mood and size cuts skip them)
    in_d = {e["slug"] for e in ds}
    in_s = {e["slug"] for e in ss}
    for p in ix.seed_list:
        dslug, sslug = p["display"]["slug"], p["support"]["slug"]
        if (dslug in in_d and sslug in in_s) or not seed_meets(ctx, p):
            continue
        if dslug not in ix.by_slug or sslug not in ix.by_slug:
            continue
        if lock and lock["role"] == "display" and dslug != lock["slug"]:
            continue
        if lock and lock["role"] == "support" and sslug != lock["slug"]:
            continue
        d = ds[0] if (lock and lock["role"] == "display") else None
        s = ss[0] if (lock and lock["role"] == "support") else None
        if d is None:
            if family_gate(ix, ctx, ix.by_slug[dslug], "display", seeded=True) is not None:
                continue
            d = role_entry(ix, ctx, ix.by_slug[dslug], "display")
        if s is None:
            if family_gate(ix, ctx, ix.by_slug[sslug], "support", seeded=True) is not None:
                continue
            s = role_entry(ix, ctx, ix.by_slug[sslug], "support")
        jobs.append((d, s))
    out = []
    rejects = {}
    for d, s in jobs:
        rec, code = score_pair(ix, ctx, d, s)
        if rec is None:
            rejects[code] = rejects.get(code, 0) + 1
        else:
            out.append(rec)
    # the seed floor (second pass: it needs the best non-seed score)
    best = None
    for r in out:
        if not r["seedMeets"] and (best is None or r["score"] > best):
            best = r["score"]
    if best is not None:
        floor = best - SEED_GAP
        for r in out:
            if r["seedMeets"] and r["score"] < floor:
                r["score"] = floor
                r["q"] = quantize(floor)
                r["terms"]["floor"] = True
    out.sort(key=pair_key)
    if with_rejects:
        return out, rejects
    return out


# ---------------------------------------------------------------------------------------------------------------
# 4. Shuffle deck
# ---------------------------------------------------------------------------------------------------------------
def fam_sim(ix, a, b):
    fa, fb = ix.by_slug[a], ix.by_slug[b]
    if a == b or deck_key(fa) == deck_key(fb):
        return SIM_SAME
    if fa.get("skeleton") and fa.get("skeleton") == fb.get("skeleton"):
        return SIM_SKEL
    if broad_class(fa) == broad_class(fb):
        return SIM_CLASS
    return 0.0


def one_family(ix, p):
    return design_root(ix.by_slug[p["display"]["slug"]]) == design_root(ix.by_slug[p["support"]["slug"]])


def pair_sim(ix, p, r):
    v = (SIM_D_SHARE * fam_sim(ix, p["display"]["slug"], r["display"]["slug"])
         + (1.0 - SIM_D_SHARE) * fam_sim(ix, p["support"]["slug"], r["support"]["slug"]))
    if one_family(ix, p) and one_family(ix, r):
        v += SIM_KIND
    return v


def deal(ix, ctx, ranked, n):
    """The Shuffle deck: n pairs, deterministic for (inputs, seed). Pairs in ctx.shown ([dslug, sslug]) are
    skipped, so a new deck after a lock or a refill never repeats what the person saw. Back is the UI's history."""
    shown = set(ctx["shown"])
    locked_role = ctx["locked"]["role"] if ctx["locked"] else None
    cand = []
    per_d = {}
    per_s = {}
    best_score = None
    for r in ranked:
        if not r["seedMeets"] and (r["display"]["slug"], r["support"]["slug"]) not in shown:
            best_score = r["score"]
            break
    for r in ranked:
        if (r["display"]["slug"], r["support"]["slug"]) in shown:
            continue
        if best_score is not None and r["score"] < best_score - DECK_MARGIN and len(cand) >= DECK_MIN:
            break
        kd = deck_key(ix.by_slug[r["display"]["slug"]])
        ks = deck_key(ix.by_slug[r["support"]["slug"]])
        if locked_role != "display" and per_d.get(kd, 0) >= DECK_PER_DISPLAY:
            continue
        if locked_role != "support" and per_s.get(ks, 0) >= DECK_PER_SUPPORT:
            continue
        per_d[kd] = per_d.get(kd, 0) + 1
        per_s[ks] = per_s.get(ks, 0) + 1
        cand.append(r)
        if len(cand) == DECK_TOP:
            break
    rng = SplitMix64(ctx["seed"])
    jit = [rng.next_double() for _ in cand]
    taken = [False] * len(cand)
    picked = []
    used_d = {}
    used_s = {}
    while len(picked) < n:
        best = -1
        best_v = 0.0
        for strict in (True, False):   # the repeat limits relax only when nothing else is left
            for i in range(len(cand)):
                if taken[i]:
                    continue
                c = cand[i]
                if strict:
                    if picked and picked[-1]["seedMeets"] and c["seedMeets"]:
                        continue
                    kd = deck_key(ix.by_slug[c["display"]["slug"]])
                    ks = deck_key(ix.by_slug[c["support"]["slug"]])
                    if locked_role != "display" and used_d.get(kd, 0) >= 1:
                        continue
                    if locked_role != "support" and used_s.get(ks, 0) >= SUPPORT_REPEAT_MAX:
                        continue
                sim = 0.0
                start = len(picked) - DIV_WINDOW if len(picked) > DIV_WINDOW else 0
                for j in range(start, len(picked)):
                    v = pair_sim(ix, c, picked[j])
                    if v > sim:
                        sim = v
                val = c["score"] - DIV_LAMBDA * sim + JITTER * jit[i]
                if best < 0 or val > best_v:
                    best = i
                    best_v = val
            if best >= 0:
                break
        if best < 0:
            break
        taken[best] = True
        c = cand[best]
        picked.append(c)
        kd = deck_key(ix.by_slug[c["display"]["slug"]])
        ks = deck_key(ix.by_slug[c["support"]["slug"]])
        used_d[kd] = used_d.get(kd, 0) + 1
        used_s[ks] = used_s.get(ks, 0) + 1
    return picked


# ---------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------
HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(index_path, seeds_path):
    data = json.loads(pathlib.Path(index_path).read_text())
    seeds = json.loads(pathlib.Path(seeds_path).read_text()) if seeds_path else None
    return Index(data, seeds)


def context_by_name(name):
    p = pathlib.Path(name)
    if p.exists():
        return json.loads(p.read_text())
    return json.loads((HERE / "contexts.json").read_text())["contexts"][name]


def fmt(rec):
    t = rec["terms"]
    return (f"{rec['score']:.4f}  {rec['display']['name']} {rec['display']['style']}  +  "
            f"{rec['support']['name']} {rec['support']['style']}   "
            f"[mood {t['mood']:.2f} fit {t['fit']:.2f} bal {t['balance']:.2f} har {t['harmony']:.2f} "
            f"q {t['quality']:.2f} boost {t['boost']:.2f}{' pen %.2f' % t['penalty'] if t['penalty'] else ''}"
            f"{' FLOOR' if t['floor'] else ''}]  {','.join(rec['why'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--index", default=str(ROOT / "r2" / "index.json"))
    ap.add_argument("--seeds", default=str(ROOT / "pairing" / "seed_pairs.json"))
    ap.add_argument("--context", required=True, help="a name in contexts.json, or a context JSON file")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--deck", type=int, default=0)
    args = ap.parse_args()
    ix = load(args.index, args.seeds if pathlib.Path(args.seeds).exists() else None)
    ctx = parse_context(context_by_name(args.context))
    ranked, rejects = rank(ix, ctx, with_rejects=True)
    print(f"{len(ranked)} pairs; rejects {rejects}")
    for r in ranked[:args.top]:
        print(fmt(r))
    if args.deck:
        print("-- deck (seed %d)" % ctx["seed"])
        for r in deal(ix, ctx, ranked, args.deck):
            print(fmt(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
